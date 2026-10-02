"""Durable four-tier pings. Does not rewrite entry p_good. FEATURE_NAMES stays 66."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..models import ScanState, Token, utcnow
from .bloom import BLOOM_MIN_AGE, leftover_fdv

log = logging.getLogger("launchfinder.alerts")

TIERS = ("new", "lift", "runner", "bloom")


def ping_key(tier: str, mint: str) -> str:
    return f"ping:{tier}:{mint}"[:64]


def _pending_keys(session: Session) -> set[str]:
    keys: set[str] = set()
    for obj in list(session.new) + list(session.dirty):
        if isinstance(obj, ScanState) and obj.key:
            keys.add(str(obj.key))
    return keys


def already_pinged(session: Session, tier: str, mint: str) -> bool:
    key = ping_key(tier, mint)
    if key in _pending_keys(session):
        return True
    return session.query(ScanState).filter(ScanState.key == key).one_or_none() is not None


def claim_ping(session: Session, tier: str, mint: str) -> bool:
    """True once. Survives process restart. In-process notify sets are not enough."""
    if already_pinged(session, tier, mint):
        return False
    session.add(ScanState(key=ping_key(tier, mint), value="1", updated_at=utcnow()))
    return True


async def notify_tier(
    *,
    tier: str,
    symbol: str,
    name: str,
    mint: str,
    p: float,
    entry_p: float,
    mcap_usd: float,
    flags: list[str],
    reasons: list[str] | None = None,
    thesis: str = "",
    chain: str = "sol",
) -> bool:
    from ..alerts import notify_bloom, notify_high_score

    if tier == "bloom":
        return await notify_bloom(
            symbol=symbol,
            name=name,
            mint=mint,
            promise_p=p,
            entry_p=entry_p,
            mcap_usd=mcap_usd,
            thesis=thesis,
            reasons=reasons,
            flags=flags,
            chain=chain,
        )
    if tier == "lift":
        text_name = f"{name} — 15m lift (entry {entry_p:.0%} → live {p:.0%})"
        await notify_high_score(
            symbol=symbol,
            name=text_name,
            mint=mint,
            p_good=p,
            mcap_usd=mcap_usd,
            flags=flags,
            reasons=reasons or ["Tape improved after migrate"],
            chain=chain,
        )
        return True
    if tier == "runner":
        await notify_high_score(
            symbol=symbol,
            name=f"{name} — 1h runner",
            mint=mint,
            p_good=p,
            mcap_usd=mcap_usd,
            flags=flags,
            reasons=reasons or ["Runner trajectory"],
            chain=chain,
        )
        return True
    await notify_high_score(
        symbol=symbol,
        name=name,
        mint=mint,
        p_good=p,
        mcap_usd=mcap_usd,
        flags=flags,
        reasons=reasons,
        chain=chain,
    )
    return True


async def maybe_lift_ping(
    session: Session,
    token: Token,
    *,
    entry_p: float,
    conviction_p: float,
    multiple: float,
    last_liq: float,
    last_mcap: float,
    t0_mcap: float,
    flags: list[str],
    chain: str,
) -> None:
    """15m lift: weak entry, live tape now looks worth a look."""
    from .bloom import BLOOM_MIN_LIQ, BLOOM_MIN_MULTIPLE
    from ..config import settings

    if already_pinged(session, "new", token.mint) or already_pinged(session, "lift", token.mint):
        return
    if leftover_fdv(chain, t0_mcap, last_mcap):
        return
    age = None
    launched = token.migrated_at or token.first_seen_at
    if launched is not None:
        from datetime import datetime, timezone

        if launched.tzinfo is None:
            launched = launched.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - launched
    if age is None or age < BLOOM_MIN_AGE:
        return
    min_p = float(settings.bloom_min_promise or 0.58)
    if entry_p >= min_p:
        return
    if conviction_p < min_p and not (multiple >= BLOOM_MIN_MULTIPLE and last_liq >= BLOOM_MIN_LIQ):
        return
    if not claim_ping(session, "lift", token.mint):
        return
    try:
        flags_l = [str(f) for f in flags]
        await notify_tier(
            tier="lift",
            symbol=token.symbol or "",
            name=token.name or "",
            mint=token.mint,
            p=max(conviction_p, entry_p),
            entry_p=entry_p,
            mcap_usd=last_mcap,
            flags=flags_l,
            reasons=["15m tape improved after a weak entry"],
            chain=chain,
        )
    except Exception:
        log.exception("lift ping failed for %s", token.mint)
