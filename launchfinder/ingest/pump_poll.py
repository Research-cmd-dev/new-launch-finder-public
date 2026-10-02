from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..chains import token_chain
from ..config import settings
from ..models import ScanState, Token, utcnow
from ..research import gmgn, pumpfun
from .store import ingest_and_research

log = logging.getLogger("launchfinder.poll")
FRESH_CREATE = timedelta(hours=18)
JUST_FLIPPED = timedelta(minutes=20)
# FOMO desk stubs: null created_at + $0 last — park only after Dex has a chance to hydrate.
_FOMO_STUB_SOURCES = frozenset({"fomo_board"})


def _fomo_stub_pending_hydrate(token: Token) -> bool:
    if (token.source or "") not in _FOMO_STUB_SOURCES:
        return False
    oc = token.outcome
    last = float(oc.last_mcap or 0.0) if oc is not None else 0.0
    return last <= 0.0
WATCH_KEY = "watch_incomplete"
GRAD_KEY = "watch_preview:sol"
CLOCK_KEY = "watch_clocks:sol"


def _age(ts: datetime | None) -> timedelta | None:
    if not ts:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return utcnow() - ts


def is_fresh_migration(coin: dict[str, Any], watched: set[str]) -> bool:
    """Keep the live book to coins that just graduated, not old PumpSwap majors."""
    mint = coin.get("mint") or ""
    if mint in watched:
        return True
    mcap = float(coin.get("mcap_usd") or 0.0)
    # Live 08:20: last_trade handed NTDA / NIKE / CLAWDHOOD at $5–15M
    # leftover FDV. created_at was inside 18h so we ingested them as
    # new graduates. Do not late-ingest fat old books. Watch-list
    # just-flipped still lands. Do not raise MAX_HONEST_ENTRY_MULTIPLE.
    from ..scoring.outcomes import SOL_LEFTOVER_MAJOR_MCAP

    if mcap >= SOL_LEFTOVER_MAJOR_MCAP:
        return False
    created_age = _age(coin.get("created_at"))
    updated_age = _age(coin.get("updated_at"))
    if created_age is not None and created_age <= FRESH_CREATE:
        return True
    if updated_age is not None and updated_age <= JUST_FLIPPED and 8_000 <= mcap <= 400_000:
        return True
    return False


# In-memory preview of GMGN near-completion trenches for the dashboard.
# Rebuilt every third poll cycle; a restart leaves it empty briefly.
_watch_preview: list[dict[str, Any]] = []
_first_seen_clocks: dict[str, str] = {}
_cycle_count = 0
# Same dust line as live hunt / paper (DEAD_POOL_LIQ). Bonded books and
# leftover same-ticker majors are not "about to graduate".
_GRAD_MIN_MCAP = 800.0
_GRAD_PREVIEW = 15
# Live 17:40: ZERO sat $29876 / 0.941 for hours. A frozen
# almost-bonded trench is not graduating. RH already drops
# frozen 8h+ / progress < 0.40 (EQUIN); Sol matches that and
# also drops stuck-at-the-door books (progress >= 0.90).
# Live 18:25: HOODINU 0.999 / $43k / 2.1h sat watch #1. The
# 8h / 0.90 door waits. A 2h book parked at 0.99+ is bonded
# leftover, not graduating. SONIC 0.84 / 11h stays.
# Do not change stuck-door 0.90. Do not add 10h / <0.80.
FROZEN_TRENCH_HOURS = 8.0
FROZEN_TRENCH_PROGRESS = 0.40
STUCK_DOOR_PROGRESS = 0.90
BONDED_STUCK_HOURS = 2.0
BONDED_STUCK_PROGRESS = 0.99


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


def is_frozen_trench(row: dict[str, Any], now: datetime | None = None) -> bool:
    launched = _parse_iso(row.get("first_seen"))
    if launched is None:
        return False
    now = now or datetime.now(timezone.utc)
    age_h = (now - launched).total_seconds() / 3600.0
    prog = float(row.get("progress") or 0.0)
    if age_h >= BONDED_STUCK_HOURS and prog >= BONDED_STUCK_PROGRESS:
        return True
    if age_h < FROZEN_TRENCH_HOURS:
        return False
    return prog < FROZEN_TRENCH_PROGRESS or prog >= STUCK_DOOR_PROGRESS


def _sol_fat_hunt_twin_tickers(keep_mints: set[str]) -> set[str]:
    """Tickers that already have a fat hunt book on a different mint."""
    if not keep_mints:
        return set()
    try:
        from ..db import session_scope
        from ..models import Outcome, Research
        from ..scoring.outcomes import SKINNY_HUNT_LIQ, is_thin_approaching_print

        with session_scope() as session:
            rows = (
                session.query(
                    Token.symbol, Token.mint, Outcome.last_liq, Research.holder_count
                )
                .join(Outcome, Outcome.token_id == Token.id)
                .join(Research, Research.token_id == Token.id)
                .filter(Token.chain == "sol")
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
        if holders is None or is_thin_approaching_print(holders, "sol"):
            continue
        s = (sym or "").strip().lower()
        if s:
            twins.add(s)
    return twins


def _sol_same_mint_on_hunt(keep_mints: set[str]) -> set[str]:
    """Same mint already on the hunt desk is not graduating.

    Live FARTATM 0.999 / hunt 1.04× / $18k / 322w. Live 12:00:
    solshiba 0.999 sat watch #1 after the same mint was hunt
    #4 / 1.27× / $17k / 285w. Fat/crowded was the FARTATM
    example, not a gate — a launched book (even skinny, even
    missing holders) is not a trench. RH same-mint climbing
    still waits for 2× (HA). PULSE / MvC stay (no hunt row).
    """
    if not keep_mints:
        return set()
    try:
        from ..db import session_scope
        from ..models import Outcome

        with session_scope() as session:
            rows = (
                session.query(Token.mint)
                .join(Outcome, Outcome.token_id == Token.id)
                .filter(Token.chain == "sol", Token.mint.in_(keep_mints))
                .all()
            )
    except Exception:
        return set()
    # migrated_at is stamped on still-graduating trenches (BFONE
    # 0.937 / 1h). Only a hunt Outcome means launched. PULSE / MvC
    # stay (no hunt row).
    return {mint for (mint,) in rows if mint}


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
    Live 05:20: CHESTER 02:50 / 0.77 bounced at 05:16 with GAMBLER's
    stamp. Keep the oldest clock so 8h freeze can still fire."""
    if _first_seen_clocks:
        return
    try:
        from ..db import session_scope

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
        from ..db import session_scope

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
        log.debug("Sol graduating clocks persist failed", exc_info=True)


def _apply_known_clocks(rows: list[dict[str, Any]]) -> None:
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
    Stamp from Token so the frozen filter can fire on GET."""
    need = [r.get("mint") for r in rows if r.get("mint") and not r.get("first_seen")]
    if not need:
        return
    try:
        from ..db import session_scope

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


def _stamp_legacy_first_seen(rows: list[dict[str, Any]]) -> bool:
    """Old persist has no first_seen. ZERO-class stuck-at-the-door
    rows (never a Token, still on the curve) are stamped aged so
    they do not bounce back. Mid-fill leftovers get now so the 8h
    frozen clock can start. Live 18:00: OPTIMUS/FUN sat without a
    clock after the BANNED persist."""
    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=FROZEN_TRENCH_HOURS)).isoformat()
    now_iso = now.isoformat()
    changed = False
    for row in rows:
        if row.get("first_seen"):
            continue
        if float(row.get("progress") or 0.0) >= STUCK_DOOR_PROGRESS:
            row["first_seen"] = old
        else:
            row["first_seen"] = now_iso
        changed = True
    return changed


def _store_watch_preview_rows(rows: list[dict[str, Any]]) -> None:
    """Near-completion strip only: drop bonded, dust, ticker twins,
    and frozen leftovers."""
    global _watch_preview
    # Live 18:05: deploy boot left memory empty, trenches rewrote ZERO
    # at 0.941 with first_seen=now, and the 8h clock reset. Reload
    # persist (and age leftover stuck-door rows) before stamping.
    _restore_watch_preview()
    _restore_clocks()
    _backfill_first_seen(_watch_preview)
    _stamp_legacy_first_seen(_watch_preview)
    _apply_known_clocks(_watch_preview)
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
    for row in rows:
        mint = row.get("address") or ""
        if mint and mint not in prev_seen:
            need.append(mint)
    token_seen: dict[str, str] = {}
    if need:
        try:
            from ..db import session_scope

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
    cleaned: list[dict[str, Any]] = []
    for row in rows:
        mint = row.get("address") or ""
        # near_completion rows carry market_cap; completed rows carry usd_market_cap
        mcap = float(row.get("usd_market_cap") or row.get("market_cap") or 0.0)
        progress = float(row.get("progress") or row.get("launchpad_progress") or 0.0)
        if progress >= 1.0:
            continue
        if mcap < _GRAD_MIN_MCAP:
            continue
        cleaned.append(
            {
                "mint": mint,
                "symbol": (row.get("symbol") or "").strip(),
                "name": (row.get("name") or "").strip(),
                "mcap_usd": mcap,
                "progress": progress,
                "twitter": (row.get("twitter") or row.get("twitter_username") or "").strip(),
                "website": (row.get("website") or "").strip(),
                "creator": (row.get("creator_address") or row.get("creator") or "").strip(),
                "gmgn": f"https://gmgn.ai/sol/token/{mint}",
                "first_seen": prev_seen.get(mint) or token_seen.get(mint) or now_iso,
            }
        )
        _remember_clock(mint, cleaned[-1]["first_seen"])
    # Closest to graduate first; fattest book breaks ties so leftover
    # $2M RST cannot occupy the chair of a real 0.91 RST.
    cleaned.sort(key=lambda r: (-float(r.get("progress") or 0.0), -float(r.get("mcap_usd") or 0.0)))
    picked: dict[str, dict[str, Any]] = {}
    unnamed: list[dict[str, Any]] = []
    for row in cleaned:
        sym = (row.get("symbol") or "").strip().lower()
        if not sym:
            unnamed.append(row)
            continue
        if sym not in picked:
            picked[sym] = row
    preview = list(picked.values()) + unnamed
    preview.sort(key=lambda r: (-float(r.get("progress") or 0.0), -float(r.get("mcap_usd") or 0.0)))
    # Live 06:00: FARTATM 0.999 sat watch #1 while the same mint
    # was already hunt #5 / 1.04× / $18k / 322w. BEAR 0.66 sat
    # watch while a fat same-ticker hunt book printed 1.98×.
    # RH already drops fat hunt twins + same-mint climbs. A
    # launched Sol book is not graduating. PULSE / MvC stay
    # (no fat hunt twin). Do not change stuck-door 0.90.
    keep_mints = {r.get("mint") for r in preview if r.get("mint")}
    twins = _sol_fat_hunt_twin_tickers(keep_mints)
    if twins:
        preview = [
            r
            for r in preview
            if (r.get("symbol") or "").strip().lower() not in twins
        ]
    launched = _sol_same_mint_on_hunt(keep_mints)
    if launched:
        preview = [r for r in preview if r.get("mint") not in launched]
    preview.sort(key=lambda r: (-float(r.get("progress") or 0.0), -float(r.get("mcap_usd") or 0.0)))
    preview = [r for r in preview if not is_frozen_trench(r)]
    preview = preview[:_GRAD_PREVIEW]
    # Live 14:52 BANNED boot: trenches skipped and graduating went 0
    # after ZERO/BFONE were on the strip. Keep the last real books.
    if not preview:
        _persist_clocks()
        return
    _watch_preview = preview
    _persist_watch_preview(preview)
    _persist_clocks()


def _persist_watch_preview(preview: list[dict[str, Any]]) -> None:
    try:
        from ..db import session_scope

        payload = json.dumps(preview, default=str)
        with session_scope() as session:
            row = session.query(ScanState).filter(ScanState.key == GRAD_KEY).one_or_none()
            if row:
                row.value = payload
                row.updated_at = utcnow()
            else:
                session.add(ScanState(key=GRAD_KEY, value=payload))
    except Exception:
        log.debug("Sol graduating persist failed", exc_info=True)


def _restore_watch_preview() -> None:
    """Deploy boots wipe the in-memory strip. Reload the last real books."""
    global _watch_preview
    if _watch_preview:
        return
    try:
        from ..db import session_scope

        with session_scope() as session:
            row = session.query(ScanState).filter(ScanState.key == GRAD_KEY).one_or_none()
            if not row:
                return
            data = json.loads(row.value or "[]")
        if isinstance(data, list) and any(float(r.get("mcap_usd") or 0) > 0 for r in data if isinstance(r, dict)):
            _watch_preview = [r for r in data if isinstance(r, dict)][:_GRAD_PREVIEW]
    except Exception:
        return


def watch_preview() -> list[dict[str, Any]]:
    _restore_watch_preview()
    _restore_clocks()
    _backfill_first_seen(_watch_preview)
    if _stamp_legacy_first_seen(_watch_preview):
        _persist_watch_preview(_watch_preview)
    _apply_known_clocks(_watch_preview)
    # Live 09:24: Bulljak 0.986 sat watch #1 after the same mint
    # was already hunt #2 / 1.33× / $16k / 199w. Store-path
    # same-mint drop only runs on a trench write; an empty or
    # skipped poll keeps the persist. Re-apply on read. Do not
    # change stuck-door 0.90. PULSE / MvC stay (no fat hunt).
    rows = [r for r in _watch_preview if not is_frozen_trench(r)]
    keep_mints = {r.get("mint") for r in rows if r.get("mint")}
    twins = _sol_fat_hunt_twin_tickers(keep_mints)
    if twins:
        rows = [
            r
            for r in rows
            if (r.get("symbol") or "").strip().lower() not in twins
        ]
    launched = _sol_same_mint_on_hunt(keep_mints)
    if launched:
        rows = [r for r in rows if r.get("mint") not in launched]
    return rows


def _watchlist(session: Session) -> set[str]:
    row = session.query(ScanState).filter(ScanState.key == WATCH_KEY).one_or_none()
    if not row:
        return set()
    try:
        return set(json.loads(row.value or "[]"))
    except json.JSONDecodeError:
        return set()


def _save_watchlist(session: Session, mints: set[str]) -> None:
    row = session.query(ScanState).filter(ScanState.key == WATCH_KEY).one_or_none()
    if row is None:
        row = ScanState(key=WATCH_KEY)
        session.add(row)
    row.value = json.dumps(sorted(mints)[-400:])
    row.updated_at = utcnow()


async def _ingest_watch_door_completions(*, watched: set[str], known: set[str]) -> list[str]:
    """Books ≥ stuck-door progress that skip 0.80–0.89 prewarm — probe Pump API each poll."""
    from ..db import ingest_lock, session_scope

    out: list[str] = []
    preview = watch_preview()
    candidates = [
        r
        for r in preview
        if r.get("mint")
        and float(r.get("progress") or 0.0) >= STUCK_DOOR_PROGRESS
        and r["mint"] not in known
    ][:10]
    for row in candidates:
        mint = str(row["mint"])
        coin = await pumpfun.get_coin(mint)
        if not coin or not coin.get("complete"):
            continue
        try:
            async with ingest_lock:
                with session_scope() as session:
                    if session.query(Token.mint).filter(Token.mint == mint).first() is not None:
                        continue
                    token = await ingest_and_research(
                        session,
                        mint=mint,
                        source="watch_door",
                        coin=coin,
                    )
                    if token:
                        out.append(mint)
                        watched.discard(mint)
                        known.add(mint)
                        log.info("watch-door migration %s %s", token.symbol, mint)
        except IntegrityError:
            continue
    return out


async def poll_new_migrations() -> list[str]:
    """One poll cycle. Each new token is committed in its own short
    transaction so research-heavy cycles don't hold the ingest lock or hide
    results until the whole cycle finishes."""
    from ..db import ingest_lock, session_scope

    found: list[str] = []
    graduating = await pumpfun.list_coins(complete=False, sort="market_cap", limit=40)

    pages = [
        await pumpfun.list_coins(complete=True, sort="created_timestamp", limit=60),
        await pumpfun.list_coins(complete=True, sort="last_trade_timestamp", limit=40),
    ]
    seen: set[str] = set()
    coins: list[dict[str, Any]] = []
    for page in pages:
        for coin in page:
            mint = coin.get("mint")
            if not mint or mint in seen or not coin.get("complete"):
                continue
            seen.add(mint)
            coins.append(coin)

    global _cycle_count
    _cycle_count += 1
    near: list[dict[str, Any]] = []
    # Trenches add breadth (near_completion watch strip). Every 2nd poll (~24s)
    # so instant-curve graduates are not invisible between GMGN windows.
    if settings.has_gmgn and gmgn.gmgn_available() and _cycle_count % 2 == 1:
        try:
            # One weight-3 POST (completed + near + new_creation). Two
            # separate POSTs after a BANNED sit-out re-banned us at 07:15.
            completed, near, created = await gmgn.trenches(limit=40)
            _store_watch_preview_rows(near)
            for row in completed:
                coin = gmgn.trench_to_coin(row)
                mint = coin.get("mint")
                if not mint or mint in seen:
                    continue
                seen.add(mint)
                coins.append(coin)
            # Same POST. LaunchLab deploys land on new_creation, not
            # completed. Pump creates stay watch-only.
            for row in created:
                pad = row.get("launchpad_platform") or row.get("launchpad") or ""
                if not gmgn.is_sol_gmgn_create_pad(pad):
                    continue
                coin = gmgn.trench_to_coin(row)
                mint = coin.get("mint")
                if not mint or mint in seen:
                    continue
                seen.add(mint)
                coins.append(coin)
        except Exception:
            log.exception("GMGN trenches poll failed")

    mints = [c["mint"] for c in coins]
    async with ingest_lock:
        with session_scope() as session:
            watched = _watchlist(session)
            watched |= {c["mint"] for c in graduating if c.get("mint")}
            watched |= {r.get("address") for r in near if r.get("address")}
            _save_watchlist(session, watched)
            _reclassify_old_majors(session)
            known = {row.mint for row in session.query(Token.mint).filter(Token.mint.in_(mints)).all()} if mints else set()

    found.extend(
        await _ingest_watch_door_completions(watched=watched, known=known)
    )

    for coin in coins:
        mint = coin["mint"]
        if mint in known or not is_fresh_migration(coin, watched):
            continue
        source = "gmgn_trenches" if coin.get("gmgn_row") else "poll"
        try:
            async with ingest_lock:
                with session_scope() as session:
                    if session.query(Token.mint).filter(Token.mint == mint).first() is not None:
                        continue
                    token = await ingest_and_research(session, mint=mint, source=source, coin=coin)
                    if token:
                        found.append(mint)
                        watched.discard(mint)
                        log.info("new migration %s %s", token.symbol, mint)
        except IntegrityError:
            # Another instance (deploy overlap) ingested this mint first.
            log.debug("mint %s ingested elsewhere, skipping", mint)

    if found:
        async with ingest_lock:
            with session_scope() as session:
                _save_watchlist(session, watched)
    return found


def _aware(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _reclassify_old_majors(session: Session) -> None:
    """Park stale Solana majors. RH has its own quiet-retire + ingest grace.

    Catch-up RH rows stamp trench open as migrated_at (hours/days ago) and
    often omit created_at. Treating them as Solana old-majors parked
    ZAZU/ATHENIX every poll (~12s); outcome restore then unparked them.
    """
    now = utcnow()
    rows = session.query(Token).filter(Token.is_historical.is_(False)).all()
    for token in rows:
        if token_chain(token) != "sol":
            continue
        created = _aware(token.created_at_chain)
        migrated = _aware(token.migrated_at)
        just_flipped = migrated is not None and now - migrated <= JUST_FLIPPED
        old_launch = created is not None and now - created > FRESH_CREATE
        unknown_age = created is None
        if ((old_launch or unknown_age) and not just_flipped):
            # RESI-class: tape hydrated a live book; do not re-park every poll.
            oc = token.outcome
            if oc is not None and float(oc.last_mcap or 0.0) >= 100_000.0:
                continue
            # AQUA-class FOMO stubs: unknown_age + $0 last — hydrate before historical park.
            if _fomo_stub_pending_hydrate(token):
                continue
            token.is_historical = True


async def backfill_history(limit: int = 80) -> int:
    """Seed the outcome book from recent graduations.

    Each token is researched and committed in its own transaction so progress
    survives restarts and is visible on the dashboard immediately. Runs
    concurrently with the live poll loop, which takes priority.
    """
    from ..db import ingest_lock, session_scope

    async with ingest_lock:
        with session_scope() as session:
            if session.query(Token).count() >= 15:
                return 0
    added = 0
    offset = 0
    while added < limit:
        page = await pumpfun.list_coins(complete=True, sort="created_timestamp", offset=offset, limit=50)
        if not page:
            break
        for coin in page:
            if not coin.get("complete"):
                continue
            try:
                async with ingest_lock:
                    with session_scope() as session:
                        before = session.query(Token).filter(Token.mint == coin["mint"]).one_or_none()
                        if before is not None:
                            continue
                        await ingest_and_research(session, mint=coin["mint"], source="backfill", coin=coin, historical=True)
            except IntegrityError:
                continue
            added += 1
            if added % 10 == 0:
                log.info("backfill progress: %s tokens", added)
            if added >= limit:
                break
        offset += len(page)
        if len(page) < 50:
            break
    log.info("backfilled %s historical migrations", added)
    return added
