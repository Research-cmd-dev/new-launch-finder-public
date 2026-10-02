"""Learn alarms when Hunt last_mcap is stale vs FOMO / Dex reality."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..chains import normalize_chain, normalize_mint
from ..models import HuntCard, Outcome, Token
from .fomo_coverage import max_age_hours

FOMO_MCAP_GAP_RATIO = 5.0
DESK_HIGH_VS_FOMO_RATIO = 1.45
MIN_FOMO_BOARD_MCAP = 250_000.0


def _fomo_mcap_index(fomo_items: list[dict[str, Any]] | None, chain: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in fomo_items or []:
        if str(item.get("chain") or "") != chain:
            continue
        mint = normalize_mint(str(item.get("mint") or ""), chain)
        if not mint:
            continue
        try:
            mcap = float(item.get("mcap_usd") or 0.0)
        except (TypeError, ValueError):
            mcap = 0.0
        if mcap > out.get(mint, 0.0):
            out[mint] = mcap
    return out


def hunt_mcap_hydrate_gaps(
    session: Session,
    fomo_items: list[dict[str, Any]] | None = None,
    *,
    chain: str = "robinhood",
    limit: int = 40,
) -> list[dict[str, Any]]:
    """Hunt rows where desk last_mcap disagrees with FOMO board (Sol + RH)."""
    chain = normalize_chain(chain)
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=max_age_hours(chain))
    rows = (
        session.query(HuntCard, Token, Outcome)
        .join(Token, Token.id == HuntCard.token_id)
        .join(Outcome, Outcome.token_id == Token.id)
        .filter(HuntCard.chain == chain)
        .filter((HuntCard.launched_at >= since) | (HuntCard.first_seen_at >= since))
        .order_by(HuntCard.first_seen_at.desc())
        .limit(max(1, min(int(limit) * 3, 120)))
        .all()
    )
    fomo_by_mint = _fomo_mcap_index(fomo_items, chain)

    out: list[dict[str, Any]] = []
    for card, token, outcome in rows:
        mint = normalize_mint(token.mint, chain)
        fomo_mcap = fomo_by_mint.get(mint, 0.0)
        last = float(outcome.last_mcap or 0.0)
        reason = ""
        if last <= 0 and fomo_mcap >= MIN_FOMO_BOARD_MCAP:
            reason = "on_hunt_last_zero_fomo_board"
        elif last <= 0:
            continue
        elif fomo_mcap >= MIN_FOMO_BOARD_MCAP and last >= fomo_mcap * DESK_HIGH_VS_FOMO_RATIO:
            reason = "desk_mcap_high_vs_fomo"
        elif fomo_mcap >= last * FOMO_MCAP_GAP_RATIO:
            reason = "fomo_board_mcap_gap"
        else:
            continue
        out.append(
            {
                "mint": mint,
                "symbol": token.symbol or card.mint[:10],
                "chain": chain,
                "last_mcap": last,
                "last_liq": float(outcome.last_liq or 0.0),
                "fomo_board_mcap": fomo_mcap or None,
                "pool_address": (token.pool_address or "")[:20] + ("…" if len(token.pool_address or "") > 20 else ""),
                "source": token.source,
                "reason": reason,
                "note": "Learn only — check Dex pair / FDV vs mcap. Does not arm or open fills.",
            }
        )
        if len(out) >= limit:
            break
    return out


def rh_hunt_hydrate_gaps(
    session: Session,
    fomo_items: list[dict[str, Any]] | None = None,
    *,
    chain: str = "robinhood",
    limit: int = 40,
) -> list[dict[str, Any]]:
    """Backward-compatible RH-only wrapper."""
    return hunt_mcap_hydrate_gaps(session, fomo_items, chain=chain, limit=limit)


def all_hunt_mcap_hydrate_gaps(
    session: Session,
    fomo_items: list[dict[str, Any]] | None = None,
    *,
    limit_per_chain: int = 20,
) -> list[dict[str, Any]]:
    alarms: list[dict[str, Any]] = []
    for chain in ("robinhood", "sol"):
        alarms.extend(hunt_mcap_hydrate_gaps(session, fomo_items, chain=chain, limit=limit_per_chain))
    return alarms
