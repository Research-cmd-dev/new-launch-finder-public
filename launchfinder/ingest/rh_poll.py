from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import DataError, IntegrityError

from ..chains import gmgn_token_url, normalize_mint
from ..research.pons import pons_token_url
from ..config import settings
from ..db import session_scope
from ..models import Outcome, Research, ScanState, Token, utcnow
from ..research import gmgn
from ..research.gmgn import is_pons_platform
from ..research.holders import holders_from_gmgn
from .dex_poll import poll_rh_dex
from .pons_poll import poll_pons_launches, watch_preview as pons_watch
from .store import ingest_and_research

log = logging.getLogger("launchfinder.rh")
CHAIN = "robinhood"
WATCH_KEY = "watch_incomplete:robinhood"
GRAD_KEY = "watch_preview:robinhood"
CLOCK_KEY = "watch_clocks:robinhood"

_watch_preview: list[dict[str, Any]] = []
_first_seen_clocks: dict[str, str] = {}


GRADUATING_PREVIEW = 15
# Live 16:20: EQUIN sat $41994.35 / 0.30 from 00:27 through the
# afternoon. Live 20:20: GLDR 0.404 / first_seen Sep 1 22:25 sat
# #1. Live 21:40: MEMESTOCK / HOOD / ROX / MOONCOIN 0.18–0.23 /
# first_seen 14:10 sat 7.5h. A frozen mid-progress leftover is
# not graduating.
FROZEN_TRENCH_HOURS = 7.0
FROZEN_TRENCH_PROGRESS = 0.45
# Live 13:08: EBT 0.488 / first_seen Sep 2 13:32 sat #2
# after 23h. The 7h / <0.45 freeze missed the 0.45–0.55
# overnight band. HO 0.50 / 8h stays. CATARM 0.78 stays.
OVERNIGHT_TRENCH_HOURS = 16.0
OVERNIGHT_TRENCH_PROGRESS = 0.55
# Live 13:32: DRILL 0.334 / 5.9h sat #3. The 7h / <0.45
# freeze waits another hour on a book that never cleared
# 0.35. FUMU 5.5h / 0.34 stays. HO 0.50 / 8h stays.
SIX_HOUR_TRENCH_HOURS = 6.0
SIX_HOUR_TRENCH_PROGRESS = 0.35
# Live 14:58: Doughe 0.193 / 4.4h sat #6 and EAGLE 0.07 /
# 4.0h sat #13. The 6h / <0.35 freeze waits on a trench
# that never cleared 0.25. FUMU 5.5h / 0.34 stays.
# HO 0.50 / 8h stays. CATARM 0.79 stays.
FOUR_HOUR_TRENCH_HOURS = 4.0
FOUR_HOUR_TRENCH_PROGRESS = 0.25
# Live 15:10: CLUD 0.12 / $4.1k / 1.6h sat #8 after the 4h /
# <0.25 freeze. Sub-$5k still under 0.15 after 90m is leftover
# trench dust. CONDOM $34k stays. GMGNCOIN 0.23 stays.
# CATARM 0.79 stays. FUMU 0.34 stays.
DUST_TRENCH_HOURS = 1.5
DUST_TRENCH_PROGRESS = 0.15
DUST_TRENCH_MCAP = 5_000.0
# Live 16:40: HOT / SHREKBONER $20k / 0.0 occupied chairs after the
# $0-mcap hide. New_creation stubs now print a placeholder book.
MIN_GRAD_PROGRESS = 0.05
# Live 17:00: U $1448 / 0.30 sat with S&P500 $2775 after progress-first
# filled the strip with $3–7k PONS names. Live 21:20: S&P500 $2774 /
# 0.26 / 7h still occupied a chair. Live 22:00: 🚀 $3006 / monke $3061
# / Jacket $3067 / APRO $3055 sat after the $3k cut. Live 22:40:
# CHATJIPITI $3245 / WHMD $3236 sat after the $3.2k cut. Sub-$3.3k
# is not graduating; JUGGERNAUT $3344 stays.
MIN_GRAD_MCAP = 3_300.0


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def is_frozen_low_progress(row: dict[str, Any], now: datetime | None = None) -> bool:
    launched = _parse_iso(row.get("first_seen"))
    if launched is None:
        return False
    now = now or datetime.now(timezone.utc)
    age_h = (now - launched).total_seconds() / 3600.0
    prog = float(row.get("progress") or 0.0)
    return (
        age_h >= FROZEN_TRENCH_HOURS and prog < FROZEN_TRENCH_PROGRESS
    ) or (
        age_h >= OVERNIGHT_TRENCH_HOURS and prog < OVERNIGHT_TRENCH_PROGRESS
    ) or (
        age_h >= SIX_HOUR_TRENCH_HOURS and prog < SIX_HOUR_TRENCH_PROGRESS
    ) or (
        age_h >= FOUR_HOUR_TRENCH_HOURS and prog < FOUR_HOUR_TRENCH_PROGRESS
    )


def is_dust_trench(row: dict[str, Any], now: datetime | None = None) -> bool:
    launched = _parse_iso(row.get("first_seen"))
    if launched is None:
        return False
    now = now or datetime.now(timezone.utc)
    age_h = (now - launched).total_seconds() / 3600.0
    if age_h < DUST_TRENCH_HOURS:
        return False
    if float(row.get("progress") or 0.0) >= DUST_TRENCH_PROGRESS:
        return False
    return 0 < float(row.get("mcap_usd") or 0.0) < DUST_TRENCH_MCAP


def _rank_graduating(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Live 10:40: SOLCC / YOO $0 sat above GMN $22k. Ranked real first.
    # Live 11:20: KMIKEYM / QUARTER / "1" $0 still filled 10 of 15 after
    # PAPER $39k. Empty trench stubs are not graduating books.
    # Live 12:21: the 80-row store left 62 real names on the strip.
    # Keep the soon list short — fattest 15 only.
    # Live 16:40: HOT / SHREKBONER 0% sat above FAMI 0.16. Sol already
    # ranks nearest-to-graduate first; RH now matches after the $0 hide.
    ranked = sorted(
        [
            r
            for r in rows
            if float(r.get("mcap_usd") or 0) >= MIN_GRAD_MCAP
            and float(r.get("progress") or 0) >= MIN_GRAD_PROGRESS
            and not is_frozen_low_progress(r)
            and not is_dust_trench(r)
        ],
        key=lambda r: (-float(r.get("progress") or 0.0), -float(r.get("mcap_usd") or 0.0)),
    )
    # Live 17:00: two Shiitake mints (0.708 / $11k and 0.216 / $4k)
    # occupied two chairs. Sol already keeps one ticker; RH now matches.
    picked: dict[str, dict[str, Any]] = {}
    unnamed: list[dict[str, Any]] = []
    for row in ranked:
        sym = (row.get("symbol") or "").strip().lower()
        if not sym:
            unnamed.append(row)
            continue
        if sym not in picked:
            picked[sym] = row
    ranked = list(picked.values()) + unnamed
    # Live 01:00: Epstein $3578 / 0.127 sat on the trench while a
    # different Epstein mint already had a $24k / 71w hunt book.
    # egg $7.6k stub vs a $20k hunt twin. Same-mint CATARM 1.25×
    # stays — still bonding. Hunt-ticker twins are not graduating.
    keep_mints = {r.get("mint") for r in ranked if r.get("mint")}
    twins = _rh_fat_hunt_twin_tickers(keep_mints)
    if twins:
        ranked = [
            r
            for r in ranked
            if (r.get("symbol") or "").strip().lower() not in twins
        ]
    # Live 04:00: HA 0.55 / $79k sat watch while the same mint was
    # already 2.29× / $60k on hunt + approaching. Live 10:00:
    # TIGER 0.06 sat watch while the same mint was already hunt
    # #19 / 1.67× / $28k. Live 10:21: OWL 0.086 sat watch while
    # hunt #1 / 1.03× / 36w / 0.25h. A 1.5×+ hunt book or a
    # this-window fat crowded same-mint is not graduating.
    # CATARM 1.25× leftover stays. MSTOCK 1.00× thin stubs stay.
    climbing = _rh_same_mint_climbing({r.get("mint") for r in ranked if r.get("mint")})
    if climbing:
        ranked = [r for r in ranked if r.get("mint") not in climbing]
    ranked.sort(key=lambda r: (-float(r.get("progress") or 0.0), -float(r.get("mcap_usd") or 0.0)))
    return ranked[:GRADUATING_PREVIEW]


def _rh_fat_hunt_twin_tickers(keep_mints: set[str]) -> set[str]:
    """Tickers that already have a fat hunt book on a different mint."""
    if not keep_mints:
        return set()
    try:
        from ..scoring.outcomes import RH_HUNT_THIN_HOLDERS, SKINNY_HUNT_LIQ

        with session_scope() as session:
            rows = (
                session.query(
                    Token.symbol, Token.mint, Outcome.last_liq, Research.holder_count
                )
                .join(Outcome, Outcome.token_id == Token.id)
                .join(Research, Research.token_id == Token.id)
                .filter(Token.chain == CHAIN)
                .all()
            )
    except Exception:
        return set()
    twins: set[str] = set()
    for sym, mint, liq, holders in rows:
        if mint in keep_mints:
            continue
        if float(liq or 0) < SKINNY_HUNT_LIQ:
            continue
        # Live 03:50: OILRIG graduating 0.77 / $38k dropped because a
        # 6-wallet $20k / 14× wick shared the ticker. A thin hunt wick
        # is not a fat book. Epstein 71w / $24k still counts.
        if holders is None or int(holders) <= RH_HUNT_THIN_HOLDERS:
            continue
        s = (sym or "").strip().lower()
        if s:
            twins.add(s)
    return twins


def _rh_same_mint_climbing(keep_mints: set[str]) -> set[str]:
    """Same mint already printing a launched hunt book is not graduating.

    Live 10:00: TIGER 0.06 sat watch while hunt printed 1.67×.
    Live 10:21: OWL 0.086 sat watch while hunt #1 / 1.03× / 36w.
    A 1.5×+ book, a this-window fat crowded book, or a 1–2 wallet
    factory print is launched. CATARM 1.25× leftover (11h) stays.
    MSTOCK 1.00× thin stubs stay.
    """
    if not keep_mints:
        return set()
    try:
        from ..scoring.outcomes import RH_HUNT_THIN_HOLDERS, SKINNY_HUNT_LIQ

        with session_scope() as session:
            rows = (
                session.query(
                    Token.mint,
                    Outcome.multiple,
                    Outcome.last_liq,
                    Research.holder_count,
                    Token.created_at_chain,
                    Token.first_seen_at,
                )
                .join(Outcome, Outcome.token_id == Token.id)
                .outerjoin(Research, Research.token_id == Token.id)
                .filter(Token.chain == CHAIN, Token.mint.in_(keep_mints))
                .all()
            )
    except Exception:
        return set()
    climbing: set[str] = set()
    now = datetime.now(timezone.utc)
    for mint, multiple, liq, holders, created, first_seen in rows:
        if float(liq or 0) < SKINNY_HUNT_LIQ:
            continue
        if float(multiple or 0) >= 1.5:
            climbing.add(mint)
            continue
        # Live 11:11: BBD 0.089 sat watch while hunt was 2w / $28k /
        # 1.00×. A 1–2 wallet hunt book is launched factory, not
        # graduating. MSTOCK 12w / 1.00× stub stays. CATARM 48w /
        # 1.25× leftover stays.
        if holders is not None and 1 <= int(holders) <= 2:
            climbing.add(mint)
            continue
        if holders is None or int(holders) <= RH_HUNT_THIN_HOLDERS:
            continue
        launched = created or first_seen
        age_h = 0.0
        if launched is not None:
            if launched.tzinfo is None:
                launched = launched.replace(tzinfo=timezone.utc)
            age_h = (now - launched).total_seconds() / 3600.0
        if age_h < 5.5:
            climbing.add(mint)
    return climbing


def _older_iso(a: str | None, b: str | None) -> str | None:
    da, db = _parse_iso(a), _parse_iso(b)
    if da is None:
        return b
    if db is None:
        return a
    return a if da <= db else b


def _remember_clock(mint: str | None, ts: str | None) -> None:
    if not mint or not ts:
        return
    prev = _first_seen_clocks.get(mint)
    _first_seen_clocks[mint] = _older_iso(prev, ts) or ts


def _restore_clocks() -> None:
    """Persist-evicted trench rows lose first_seen when no Token exists.
    Live 04:40: accomparts 22:41 / 0.15 froze off, then bounced at
    04:27 with a new clock. Keep the oldest stamp."""
    if _first_seen_clocks:
        return
    try:
        with session_scope() as session:
            row = session.query(ScanState).filter(ScanState.key == CLOCK_KEY).one_or_none()
            if not row:
                return
            data = json.loads(row.value or "{}")
        if isinstance(data, dict):
            for mint, ts in data.items():
                if mint and ts:
                    _first_seen_clocks[str(mint)] = str(ts)
    except Exception:
        return


def _persist_clocks() -> None:
    try:
        items = list(_first_seen_clocks.items())[-800:]
        payload = json.dumps(dict(items), default=str)
        with session_scope() as session:
            row = session.query(ScanState).filter(ScanState.key == CLOCK_KEY).one_or_none()
            if row:
                row.value = payload
                row.updated_at = utcnow()
            else:
                session.add(ScanState(key=CLOCK_KEY, value=payload))
    except Exception:
        log.debug("RH graduating clocks persist failed", exc_info=True)


def _apply_known_clocks(rows: list[dict[str, Any]]) -> None:
    """Prefer the oldest known first_seen so a pons/trench re-add
    cannot reset the 7h freeze."""
    _restore_clocks()
    for row in rows:
        mint = row.get("mint")
        if not mint:
            continue
        older = _older_iso(row.get("first_seen"), _first_seen_clocks.get(mint))
        if older:
            row["first_seen"] = older
            _remember_clock(mint, older)


def _backfill_first_seen(rows: list[dict[str, Any]]) -> None:
    """Deploy restore loads the last persist without first_seen.
    Live EQUIN 00:31 / 0.30 stayed on the strip until a trench store.
    Stamp from Token so the frozen filter can fire on GET."""
    need = [r.get("mint") for r in rows if r.get("mint") and not r.get("first_seen")]
    if not need:
        return
    try:
        with session_scope() as session:
            found = {
                mint: ts.isoformat()
                for mint, ts in (
                    session.query(Token.mint, Token.first_seen_at)
                    .filter(Token.mint.in_(need), Token.first_seen_at.isnot(None))
                    .all()
                )
            }
    except Exception:
        return
    for row in rows:
        mint = row.get("mint")
        if mint and not row.get("first_seen") and mint in found:
            row["first_seen"] = found[mint]


def _rh_watch_pool() -> list[dict[str, Any]]:
    """GMGN persist + PONS climbs, before the $3.3k hunt-strip cut."""
    _restore_watch_preview()
    _restore_clocks()
    _backfill_first_seen(_watch_preview)
    _apply_known_clocks(_watch_preview)
    rows = list(_watch_preview)
    seen = {r.get("mint") for r in rows}
    for row in pons_watch():
        mint = row.get("mint")
        if mint and mint not in seen:
            rows.append(row)
            seen.add(mint)
    _apply_known_clocks(rows)
    return rows


def prewarm_source_rows() -> list[dict[str, Any]]:
    """Near-grad candidates including $0 PONS climbs. Desk strip unchanged."""
    return _rh_watch_pool()


def watch_preview() -> list[dict[str, Any]]:
    return _rank_graduating(_rh_watch_pool())


# Live 11:40: created+near sliced at 15, then $0 hide left only
# HOUSECOIN. New_creation stubs occupy the first 15; real near
# books sat at 16+. Keep a wider window so rank can pick them.
GRADUATING_STORE = 80


def _persist_watch_preview(preview: list[dict[str, Any]]) -> None:
    try:
        payload = json.dumps(preview, default=str)
        with session_scope() as session:
            row = session.query(ScanState).filter(ScanState.key == GRAD_KEY).one_or_none()
            if row:
                row.value = payload
                row.updated_at = utcnow()
            else:
                session.add(ScanState(key=GRAD_KEY, value=payload))
    except Exception:
        log.debug("RH graduating persist failed", exc_info=True)


def _restore_watch_preview() -> None:
    """Deploy boots wipe the in-memory strip. Live 13:00 BANNED sat
    900s with graduating=0 after the wallet-map deploy. Reload the
    last real books; do not add GMGN HTTP."""
    global _watch_preview
    if _watch_preview:
        return
    try:
        with session_scope() as session:
            row = session.query(ScanState).filter(ScanState.key == GRAD_KEY).one_or_none()
            if not row:
                return
            data = json.loads(row.value or "[]")
        if isinstance(data, list) and any(float(r.get("mcap_usd") or 0) > 0 for r in data if isinstance(r, dict)):
            _watch_preview = [r for r in data if isinstance(r, dict)]
    except Exception:
        return


def _store_watch_preview_rows(rows: list[dict[str, Any]]) -> None:
    global _watch_preview
    # Deploy boot leaves memory empty. Reload persist so first_seen
    # clocks survive the first trench rewrite (Sol already does this).
    _restore_watch_preview()
    _restore_clocks()
    for row in _watch_preview:
        _remember_clock(row.get("mint"), row.get("first_seen"))
    prev_seen = {
        r.get("mint"): r.get("first_seen")
        for r in _watch_preview
        if r.get("mint") and r.get("first_seen")
    }
    prev_seen.update(
        {mint: ts for mint, ts in _first_seen_clocks.items() if mint and ts}
    )
    need = []
    for row in rows[:GRADUATING_STORE]:
        mint = normalize_mint(row.get("address") or "", CHAIN)
        if mint and mint not in prev_seen:
            need.append(mint)
    token_seen: dict[str, str] = {}
    if need:
        try:
            with session_scope() as session:
                for mint, ts in (
                    session.query(Token.mint, Token.first_seen_at)
                    .filter(Token.mint.in_(need))
                    .all()
                ):
                    if ts is not None:
                        token_seen[mint] = ts.isoformat()
        except Exception:
            token_seen = {}
    now_iso = datetime.now(timezone.utc).isoformat()
    preview = []
    for row in rows[:GRADUATING_STORE]:
        mint = normalize_mint(row.get("address") or "", CHAIN)
        pad = row.get("launchpad_platform") or row.get("launchpad") or ""
        preview.append(
            {
                "mint": mint,
                "symbol": row.get("symbol") or "",
                "name": row.get("name") or "",
                "mcap_usd": float(row.get("usd_market_cap") or row.get("market_cap") or 0.0),
                "progress": float(row.get("progress") or row.get("launchpad_progress") or 0.0),
                "twitter": (row.get("twitter") or row.get("twitter_username") or "").strip(),
                "website": (row.get("website") or "").strip(),
                "creator": (row.get("creator_address") or row.get("creator") or "").strip(),
                "gmgn": gmgn_token_url(mint, CHAIN),
                "pons": pons_token_url(mint) if is_pons_platform(pad) else "",
                "chain": CHAIN,
                "first_seen": prev_seen.get(mint) or token_seen.get(mint) or now_iso,
            }
        )
        _remember_clock(mint, preview[-1]["first_seen"])
    # Live 13:02 BANNED boot: trenches skipped, store never ran, and a
    # later empty/all-$0 write would wipe SHELLY/ATOMS. Keep the last
    # real strip until a poll with live books arrives.
    if not any(float(r.get("mcap_usd") or 0) > 0 for r in preview):
        return
    _watch_preview = preview
    _persist_watch_preview(preview)
    _persist_clocks()


def _watchlist(session) -> set[str]:
    row = session.query(ScanState).filter(ScanState.key == WATCH_KEY).one_or_none()
    if not row:
        return set()
    try:
        return set(json.loads(row.value or "[]"))
    except json.JSONDecodeError:
        return set()


def _refresh_known_from_trench(session, coin: dict[str, Any]) -> None:
    """Reuse the free trench row to fill card fields we used to drop.
    No extra GMGN HTTP — factory/Dex names pick this up when they appear
    on trenches later."""
    mint = coin.get("mint") or ""
    row = coin.get("gmgn_row") or coin.get("raw") or {}
    if not mint or not isinstance(row, dict):
        return
    token = session.query(Token).filter(Token.mint == mint).one_or_none()
    if token is None or token.research is None:
        return
    snap = gmgn.summarize_trench_row(row, mint, chain=CHAIN)
    if not snap:
        return
    raw: dict[str, Any] = {}
    try:
        raw = json.loads(token.research.raw_json or "{}")
    except json.JSONDecodeError:
        raw = {}
    prev = raw.get("gmgn") if isinstance(raw.get("gmgn"), dict) else {}
    raw["gmgn"] = {**prev, **snap}
    fallback = holders_from_gmgn(snap)
    if fallback:
        holders = raw.get("holders") if isinstance(raw.get("holders"), dict) else {}
        if not holders.get("holder_count"):
            raw["holders"] = {**holders, **fallback}
            if not token.research.holder_count:
                token.research.holder_count = int(fallback["holder_count"])
            if not token.research.top10_pct and fallback.get("top10_pct"):
                token.research.top10_pct = float(fallback["top10_pct"])
            if not token.research.creator_hold_pct and fallback.get("creator_hold_pct"):
                token.research.creator_hold_pct = float(fallback["creator_hold_pct"])
    token.research.raw_json = json.dumps(raw, default=str)
    twitter = str(coin.get("twitter") or "")
    if twitter and "status/" not in twitter and (not token.twitter or "status/" in token.twitter):
        token.twitter = twitter
    if not token.website and coin.get("website"):
        token.website = str(coin["website"])
    if not token.telegram and coin.get("telegram"):
        token.telegram = str(coin["telegram"])
    handle = str(snap.get("twitter_username") or "")
    if handle and "/" not in handle:
        if not token.research.twitter_handle or "/" in token.research.twitter_handle:
            token.research.twitter_handle = handle
        followers = int(snap.get("twitter_followers") or 0)
        if followers and (not token.research.twitter_followers or token.research.twitter_followers > 80_000_000):
            token.research.twitter_followers = followers
    elif token.research.twitter_handle and "/" in token.research.twitter_handle:
        token.research.twitter_handle = ""
        if token.research.twitter_followers > 80_000_000:
            token.research.twitter_followers = 0


def _save_watchlist(session, mints: set[str]) -> None:
    row = session.query(ScanState).filter(ScanState.key == WATCH_KEY).one_or_none()
    if row is None:
        row = ScanState(key=WATCH_KEY)
        session.add(row)
    row.value = json.dumps(sorted(mints)[-400:])
    row.updated_at = utcnow()


async def poll_robinhood() -> list[str]:
    """Official pons factory first (GMGN-independent), then trenches."""
    from ..db import ingest_lock, session_scope

    if not settings.robinhood_enabled:
        return []

    found = await poll_pons_launches()
    found.extend(await poll_rh_dex())
    if not settings.has_gmgn or not gmgn.gmgn_available():
        return found

    gmgn_found: list[str] = []
    try:
        completed, near, created = await gmgn.trenches(chain=CHAIN, limit=60)
    except Exception:
        log.exception("Robinhood trenches poll failed")
        return found
    pons_n = sum(
        1
        for row in (*completed, *created)
        if is_pons_platform(row.get("launchpad_platform") or row.get("launchpad"))
    )
    log.info(
        "robinhood trenches: %s completed, %s near, %s new_creation (%s pons)",
        len(completed),
        len(near),
        len(created),
        pons_n,
    )
    # Near-completion first. Live 11:40: created-first [:15] were $0
    # new_creation stubs; HOUSECOIN was the only survivor after the
    # $0 hide. New PONS names still follow the bonding-curve rows.
    _store_watch_preview_rows(list(near) + list(created))

    coins: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in (*completed, *created):
        coin = gmgn.trench_to_coin(row, chain=CHAIN)
        mint = normalize_mint(coin.get("mint") or "", CHAIN)
        if not mint or mint in seen:
            continue
        coin["mint"] = mint
        coin["chain"] = CHAIN
        seen.add(mint)
        coins.append(coin)

    mints = [c["mint"] for c in coins]
    async with ingest_lock:
        with session_scope() as session:
            watched = _watchlist(session)
            watched |= {normalize_mint(r.get("address") or "", CHAIN) for r in near if r.get("address")}
            _save_watchlist(session, watched)
            known = (
                {row.mint for row in session.query(Token.mint).filter(Token.mint.in_(mints)).all()}
                if mints
                else set()
            )

    refresh: list[dict[str, Any]] = []
    for coin in coins:
        mint = coin["mint"]
        # The completed trench page IS the RH freshness filter. Solana's
        # 18h PumpSwap gate (is_fresh_migration) drops Flap/Klik rows that
        # omit created_timestamp / usd_market_cap — live desk was missing
        # most of the 60-name GMGN completed list.
        if mint in known:
            refresh.append(coin)
            continue
        try:
            async with ingest_lock:
                with session_scope() as session:
                    if session.query(Token.mint).filter(Token.mint == mint).first() is not None:
                        continue
                    pad = (coin.get("launchpad") or "").strip()
                    source = "rh_pons" if is_pons_platform(pad) else "rh_trenches"
                    token = await ingest_and_research(
                        session, mint=mint, source=source, coin=coin
                    )
                    if token:
                        gmgn_found.append(mint)
                        watched.discard(mint)
                        log.info("new robinhood launch %s %s", token.symbol, mint)
        except (IntegrityError, DataError) as exc:
            log.warning("robinhood mint %s skipped: %s", mint, exc.__class__.__name__)
        await asyncio.sleep(0)

    if refresh:
        async with ingest_lock:
            with session_scope() as session:
                for coin in refresh:
                    _refresh_known_from_trench(session, coin)
    if gmgn_found or refresh:
        async with ingest_lock:
            with session_scope() as session:
                _save_watchlist(session, watched)
    return found + gmgn_found


async def backfill_robinhood(limit: int = 12) -> int:
    """Seed a small RH book from recent graduated trenches so the dashboard
    is not empty on first deploy. Caps GMGN spend — one trenches call."""
    from ..db import ingest_lock, session_scope
    from .dex_poll import ensure_boner_reference

    if settings.robinhood_enabled:
        try:
            await ensure_boner_reference()
        except Exception:
            log.debug("BONER reference seed skipped", exc_info=True)
    if not settings.robinhood_enabled or not settings.has_gmgn or not gmgn.gmgn_available():
        return 0
    async with ingest_lock:
        with session_scope() as session:
            if session.query(Token).filter(Token.chain == CHAIN).count() >= 6:
                return 0
    try:
        rows, _near, created = await gmgn.trenches(chain=CHAIN, limit=min(30, limit))
        rows = list(rows) + list(created)
    except Exception:
        log.exception("Robinhood backfill trenches failed")
        return 0
    added = 0
    for row in rows:
        coin = gmgn.trench_to_coin(row, chain=CHAIN)
        mint = normalize_mint(coin.get("mint") or "", CHAIN)
        if not mint:
            continue
        coin["mint"] = mint
        coin["chain"] = CHAIN
        try:
            async with ingest_lock:
                with session_scope() as session:
                    if session.query(Token).filter(Token.mint == mint).one_or_none() is not None:
                        continue
                    await ingest_and_research(
                        session, mint=mint, source="rh_backfill", coin=coin, historical=True
                    )
                    added += 1
        except (IntegrityError, DataError):
            continue
        if added >= limit:
            break
    if added:
        log.info("backfilled %s Robinhood launches", added)
    return added
