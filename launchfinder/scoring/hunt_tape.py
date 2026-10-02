"""60s Hunt last + Live. Weak-entry climbers can still reach Doing well.

Writes ``outcome.last_mcap`` and Hunt Live from Dex. Fills blank
name/symbol from that same Dex print so RH Bitquery rows do not stay
stuck as ``0x…``. RH holder_count refreshes from Blockscout (not GMGN).
Never rewrites ``research.p_good``. No extra GMGN. No Bitquery stream.
No auto-buy. Full ``refresh_outcomes`` chairs stay as-is.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, selectinload

from ..chains import normalize_chain, normalize_mint, token_chain
from ..models import HuntCard, Outcome, Token
from ..research import dexscreener
from .hunt import historical_hydrate_tape_mints, this_window_hunt_tape_mints, upsert_hunt

log = logging.getLogger("launchfinder.hunt_tape")

# One Hunt tape cycle must stay near 60s. Cap Blockscout so a 80-card
# RH board does not add 160 sequential GETs on top of Dex.
RH_HOLDER_TAPE_CAP = 24
# One-minute bars are a rolling window, not an archive.
TAPE_BAR_KEEP_HOURS = 48.0


def tape_book_is_dead(liquidity_usd: float | None, volume_h1: float | None = None) -> bool:
    """Skip a Hunt tape write only when there is no live pool.

    Dead pool (< $800 liq) or a Dex miss (0/0). Quiet 1h volume on a
    real book is a print, not a ghost. Do not use ``is_ghost_book`` here
    — that function is the leftover-LP *entry* gate.
    """
    from .outcomes import DEAD_POOL_LIQ

    liq = float(liquidity_usd or 0.0)
    vol = float(volume_h1 or 0.0)
    if 0 < liq < DEAD_POOL_LIQ:
        return True
    return liq <= 0 and vol <= 0


_SOL_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def looks_like_contract(value: str | None) -> bool:
    """True when the stored ticker is the mint, not a name."""
    raw = (value or "").strip()
    if len(raw) >= 10 and raw.lower().startswith("0x"):
        return True
    # Pump ingest parks the Sol mint as symbol until Dex names it.
    # AGE sort then looks like a broken board (7x5ZQFHuB1ybDJyM…).
    if 32 <= len(raw) <= 44 and _SOL_MINT_RE.fullmatch(raw):
        return True
    return False


# RESI-class: stored last from a wash pair; liq-first Dex print is lower.
_STALE_LAST_VS_DEX_RATIO = 1.35


def hunt_tape_may_hydrate_historical(
    token: Token,
    outcome: Outcome,
    market: dict | None,
    *,
    mcap: float,
    liq: float,
) -> bool:
    """Allow tape writes on historical rows when the live book disagrees (VRAX / RESI).

    Does not arm fills or rewrite Entry — only last/Live + Hunt card sync.
    """
    from ..models import Outcome as OutcomeModel

    if token.source == "backfill" or not token.is_historical:
        return False
    if not isinstance(outcome, OutcomeModel):
        return False
    if not dexscreener.market_has_quote(market or {}):
        return False
    chain = token_chain(token)
    vol_h1 = float((market or {}).get("volume_h1") or 0.0)
    last = float(outcome.last_mcap or 0.0)
    if chain == "robinhood":
        if tape_book_is_dead(liq, vol_h1):
            return False
        if last > 0:
            return False
        pool = str(getattr(token, "pool_address", "") or "")
        if dexscreener.is_dex_pair_id(pool):
            return True
        if liq >= 20_000.0:
            return True
        holders = int((token.research.holder_count if token.research else 0) or 0)
        if holders >= 40:
            return True
        return False
    if tape_book_is_dead(liq, vol_h1):
        return False
    if last <= 0:
        return True
    if mcap <= 0:
        return False
    ratio = last / mcap
    # RESI-class: wash pair above Dex or liq-first print below stale last.
    if ratio >= _STALE_LAST_VS_DEX_RATIO or ratio <= (1.0 / _STALE_LAST_VS_DEX_RATIO):
        return True
    return False


def apply_hunt_tape_identity(token: Token, market: dict | None) -> bool:
    """Fill blank / 0x name-symbol-image from Dex. Never overwrite a real ticker."""
    market = market or {}
    name = str(market.get("name") or "").strip()
    symbol = str(market.get("symbol") or "").strip()
    image = str(market.get("image_url") or "").strip()
    wrote = False
    if name and (not (token.name or "").strip() or looks_like_contract(token.name)):
        token.name = name[:128]
        wrote = True
    if (
        symbol
        and not looks_like_contract(symbol)
        and (not (token.symbol or "").strip() or looks_like_contract(token.symbol))
    ):
        token.symbol = symbol[:32]
        wrote = True
    if image and not (token.image_url or "").strip():
        token.image_url = image
        wrote = True
    return wrote


def apply_hunt_tape_market(
    session: Session,
    token: Token,
    market: dict | None,
    *,
    now: datetime | None = None,
    nested_savepoint: bool = True,
    write_bar: bool = True,
) -> bool:
    """Write one Dex print onto last + Hunt Live. Entry stays frozen."""
    from .features import prefer_rh_dust_liq
    from .outcomes import (
        honest_tracked_peak,
        reanchor_ghost_robinhood,
        record_live_last,
        revert_unbacked_last,
        sane_mcap,
    )

    now = now or datetime.now(timezone.utc)
    outcome = token.outcome
    if outcome is None or token.research is None:
        return False
    if token.source == "backfill":
        return False
    start = token.migrated_at or token.first_seen_at
    if start is None:
        return False
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    chain = token_chain(token)
    market = market or {}
    apply_hunt_tape_identity(token, market)
    pair_id = str(market.get("pair_address") or "").strip()
    if dexscreener.is_dex_pair_id(pair_id) and not dexscreener.is_dex_pair_id(
        str(getattr(token, "pool_address", "") or "")
    ):
        token.pool_address = pair_id
    liq = float(market.get("liquidity_usd") or 0.0)
    vol_h1 = float(market.get("volume_h1") or 0.0)
    mcap = sane_mcap(market.get("mcap_usd"), liq, now - start)
    unpark_historical = hunt_tape_may_hydrate_historical(
        token, outcome, market, mcap=mcap, liq=liq
    )
    from .hunt import token_has_hunt_card

    hunt_visible = token_has_hunt_card(session, token)
    if token.is_historical and not unpark_historical and not hunt_visible:
        return False
    # Tape write ≠ leftover-LP t0. is_ghost_book(vol_h1 < $100) is the
    # entry-price gate (METH $105 liq). A quiet $80k RH book still prints.
    if chain == "robinhood" and tape_book_is_dead(liq, vol_h1):
        revert_unbacked_last(session, token, outcome)
        upsert_hunt(
            session,
            token,
            now=now,
            touch_updated=False,
            nested_savepoint=nested_savepoint,
        )
        return False
    if mcap <= 0:
        return False
    parked_liq = float(outcome.last_liq or 0.0)
    reanchor_ghost_robinhood(session, token, outcome, mcap, liq, vol_h1)
    t0_now = float(outcome.t0_mcap or 0.0)
    if t0_now > 0:
        peak, tracked = honest_tracked_peak(t0_now, float(outcome.max_mcap or 0.0), mcap)
        outcome.max_mcap = peak
        outcome.multiple = tracked
    else:
        outcome.max_mcap = max(float(outcome.max_mcap or 0.0), mcap)
    if chain == "robinhood":
        outcome.last_liq = prefer_rh_dust_liq(parked_liq, outcome.last_liq)
        outcome.last_liq = prefer_rh_dust_liq(outcome.last_liq, liq)
    elif liq > 0:
        outcome.last_liq = liq
    if unpark_historical:
        token.is_historical = False
        log.info(
            "hunt tape unpark historical %s %s last_mcap=%.0f",
            chain,
            (token.mint or "")[:16],
            float(mcap),
        )
    record_live_last(session, token, outcome, market, mcap, now, start=start)
    from .first_sight import finalize_first_sight_entry

    # Async GitHub scrape/lookup runs in refresh_hunt_tape before finalize.
    # Sync path still stamps thesis from raw_json inside finalize.
    finalize_first_sight_entry(session, token, market)
    if write_bar:
        write_tape_bar(
            session,
            token,
            market,
            mcap=mcap,
            liq=outcome.last_liq if chain == "robinhood" else liq,
            now=now,
        )
    upsert_hunt(
        session,
        token,
        now=now,
        touch_updated=False,
        nested_savepoint=nested_savepoint,
    )
    return True


def write_tape_bar(session: Session, token: Token, market: dict | None, *, mcap: float, liq: float, now: datetime) -> bool:
    """One-minute bar per Hunt mint. Feeds drawdown, fills and the Live model."""
    from ..models import TapeBar

    market = market or {}
    minute = now.replace(second=0, microsecond=0)
    chain = token_chain(token)
    exists = (
        session.query(TapeBar.id)
        .filter(TapeBar.chain == chain, TapeBar.mint == token.mint, TapeBar.minute == minute)
        .first()
    )
    if exists:
        return False
    session.add(
        TapeBar(
            chain=chain,
            mint=token.mint,
            token_id=token.id,
            minute=minute,
            mcap_usd=float(mcap or 0.0),
            price_usd=float(market.get("price_usd") or 0.0),
            liquidity_usd=float(liq or 0.0),
            volume_h1=float(market.get("volume_h1") or 0.0),
            holders=int((token.research.holder_count if token.research else 0) or 0),
        )
    )
    return True


def prune_tape_bars(session: Session, *, now: datetime | None = None) -> int:
    from ..models import TapeBar

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=TAPE_BAR_KEEP_HOURS)
    n = session.query(TapeBar).filter(TapeBar.minute < cutoff).delete(synchronize_session=False)
    return int(n or 0)


async def refresh_hunt_tape(session: Session) -> int:
    """Batch-Dex this-window Hunt cards. Last + Live only."""
    from ..config import settings

    by_chain = this_window_hunt_tape_mints(session)
    wrote = 0
    now = datetime.now(timezone.utc)
    for chain, mints in by_chain.items():
        if not mints:
            continue
        chain_n = normalize_chain(chain)
        norm_mints = [normalize_mint(m, chain_n) for m in mints]
        lookup = {m for m in norm_mints if m}
        lookup.update(m for m in mints if m)
        cards = (
            session.query(HuntCard)
            .filter(HuntCard.chain == chain_n, HuntCard.mint.in_(list(lookup)))
            .all()
        )
        card_token_ids = {int(c.token_id) for c in cards if c.token_id}
        hydrate_mints = set(historical_hydrate_tape_mints(session, chain_n, limit=24))
        lookup.update(hydrate_mints)
        token_filters = [Token.mint.in_(list(lookup))]
        if card_token_ids:
            token_filters.append(Token.id.in_(list(card_token_ids)))
        if hydrate_mints:
            token_filters.append(
                and_(
                    Token.is_historical.is_(True),
                    Token.mint.in_(list(hydrate_mints)),
                )
            )
        tokens = (
            session.query(Token)
            .options(selectinload(Token.research), selectinload(Token.outcome))
            .filter(Token.chain == chain_n, or_(*token_filters))
            .all()
        )
        by_mint = {normalize_mint(token.mint, chain_n): token for token in tokens}
        by_token_id = {token.id: token for token in tokens}
        card_by_norm = {normalize_mint(c.mint, chain_n): c for c in cards}
        markets = await dexscreener.token_markets(norm_mints, chain_n)
        markets = await dexscreener.enrich_markets_with_pool_fallback(markets, tokens, chain_n)
        tokens_in_order = [by_mint[normalize_mint(m, chain_n)] for m in mints if normalize_mint(m, chain_n) in by_mint]
        # Last chance: enrich thin thesis on awaiting_fill books before
        # finalize freezes Decision.features_json (budgeted website GETs).
        try:
            from .first_sight import research_awaiting_fill
            from .thesis_enrich import enrich_thesis_before_entry, reset_website_budget, thesis_keys_thin

            reset_website_budget()
            for token in tokens_in_order:
                research = token.research
                if research is None or not research_awaiting_fill(research):
                    continue
                try:
                    feats = json.loads(research.features_json or "{}")
                except Exception:
                    feats = {}
                if not thesis_keys_thin(feats if isinstance(feats, dict) else {}):
                    continue
                try:
                    await enrich_thesis_before_entry(session, token, research)
                except Exception:
                    log.exception("thesis enrich on tape failed for %s", token.mint[:12])
        except Exception:
            log.exception("thesis enrich pass on hunt tape failed")
        from .hunt import _hunt_session_defer

        for mint in mints:
            key = normalize_mint(mint, chain_n)
            token = by_mint.get(key)
            if token is None:
                card = card_by_norm.get(key)
                if card and card.token_id:
                    token = by_token_id.get(int(card.token_id))
            if token is None:
                continue
            market = markets.get(key) or markets.get(mint) or {}
            if not dexscreener.market_has_quote(market):
                try:
                    market = await dexscreener.token_market_for_token(token, chain_n)
                except Exception:
                    market = market or {}
            from .hunt import recover_session_after_lock

            applied = False
            try:
                with session.begin_nested():
                    applied = apply_hunt_tape_market(
                        session,
                        token,
                        market,
                        now=now,
                        nested_savepoint=False,
                        write_bar=False,
                    )
            except Exception as exc:
                if _hunt_session_defer(exc):
                    recover_session_after_lock(session, exc)
                    log.info(
                        "hunt tape defer %s (%s)",
                        mint[:16],
                        type(exc).__name__,
                    )
                else:
                    raise
            # Bar is a sibling savepoint so a hunt_cards lock cannot wipe
            # ROB / TYPING leftover tape when upsert rolls back.
            try:
                with session.begin_nested():
                    bar_liq = float((token.outcome.last_liq if token.outcome else 0.0) or 0.0)
                    bar_mcap = float((token.outcome.last_mcap if token.outcome else 0.0) or 0.0)
                    if bar_mcap <= 0:
                        bar_mcap = float(market.get("mcap_usd") or 0.0)
                    if bar_mcap > 0 and write_tape_bar(
                        session,
                        token,
                        market,
                        mcap=bar_mcap,
                        liq=bar_liq or float(market.get("liquidity_usd") or 0.0),
                        now=now,
                    ):
                        wrote += 1
                    elif applied:
                        wrote += 1
            except Exception as exc:
                if _hunt_session_defer(exc):
                    recover_session_after_lock(session, exc)
                    log.info(
                        "hunt tape bar defer %s (%s)",
                        mint[:16],
                        type(exc).__name__,
                    )
                    continue
                raise
        # Release the hunt_cards / outcomes row locks before the holder HTTP.
        # Live v67: Sol DAS paging (12 mints x up to 5 pages) held them long
        # enough for refresh_outcomes' upsert_hunt to hit lock_timeout.
        try:
            session.commit()
        except Exception as exc:
            from .hunt import recover_session_after_lock

            if recover_session_after_lock(session, exc, outer=True):
                log.warning("hunt tape commit lock defer %s (%s)", chain_n, type(exc).__name__)
            else:
                raise
        if chain == "robinhood":
            from ..research.holders import refresh_rh_hunt_holder_meta

            await refresh_rh_hunt_holder_meta(
                session, tokens_in_order, now=now, limit=RH_HOLDER_TAPE_CAP
            )
        else:
            from ..research.holders import refresh_sol_hunt_holder_meta

            try:
                await refresh_sol_hunt_holder_meta(
                    session, tokens_in_order, now=now, limit=settings.sol_holder_tape_cap
                )
            except Exception:
                log.exception("sol hunt holder refresh failed")
        try:
            session.commit()
        except Exception as exc:
            from .hunt import recover_session_after_lock

            if recover_session_after_lock(session, exc, outer=True):
                log.warning("hunt tape holder commit lock defer %s (%s)", chain_n, type(exc).__name__)
            else:
                raise
    try:
        pruned = prune_tape_bars(session, now=now)
        if pruned:
            session.commit()
    except Exception:
        log.exception("tape bar prune failed")
    if wrote:
        log.info("hunt tape wrote last on %s cards", wrote)
    return wrote
