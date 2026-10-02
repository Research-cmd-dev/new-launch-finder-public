"""Ingest new pons launches from the official factories (not GMGN).

https://www.ponsfamily.com/launchpad is the UI. TokenLaunched on the
V2 + active + legacy factories is the source of truth. V1 never
migrates (same pool); V2 does (curve → Uniswap V4). GMGN "completed"
trenches miss official launches, and a GMGN sit-out must not blind
the desk.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import DataError, IntegrityError

from ..chains import normalize_mint
from ..config import settings
from ..models import ScanState, Token, utcnow
from ..research import pons
from .store import ingest_and_research

_EPOCH_CUTOFF = datetime(2024, 1, 1, tzinfo=timezone.utc)

log = logging.getLogger("launchfinder.pons")
CHAIN = "robinhood"
CURSOR_KEY = "pons_factory_cursor:robinhood"

_climbing: list[dict[str, Any]] = []
# Live 08:00 / 09:13: getLogs 429'd the public RPC. After a catch-up
# scan we used to poll again in ~24s; tip moved just past slack and
# we poked again. Sit out after every scan. TokenLaunched is minutes
# apart; 2000 blocks is ~3 min of slack.
TIP_SLACK_BLOCKS = 2_000
# Live 10:28–10:39: 90s quiet + 180s 429 sit-out still re-poked the
# public RPC every few minutes. Stay dark a bit longer at tip.
QUIET_AT_TIP_S = 120.0
_quiet_until = 0.0


def watch_preview() -> list[dict[str, Any]]:
    return list(_climbing)


async def refresh_climbing_progress(*, limit: int = 4) -> int:
    """Update PONS curve % so 80–89% climbs can pre-warm. No extra GMGN.

    Does not raise the 900s sit-out. Skips when the public RPC is dark.
    """
    global _climbing
    if not pons.pons_available() or not _climbing:
        return 0
    out: list[dict[str, Any]] = []
    checked = 0
    changed = 0
    for row in list(_climbing):
        mint = row.get("mint")
        if not mint or checked >= max(1, int(limit)):
            out.append(row)
            continue
        factory = row.get("factory") or pons.V2_FACTORY
        try:
            grad = await pons.graduation_status(factory, mint)
        except Exception:
            out.append(row)
            continue
        checked += 1
        if grad.get("graduated"):
            changed += 1
            continue
        try:
            prog = float(grad.get("progress") or 0.0)
        except (TypeError, ValueError):
            prog = 0.0
        if abs(prog - float(row.get("progress") or 0.0)) >= 0.01:
            row = dict(row)
            row["progress"] = prog
            changed += 1
        out.append(row)
    _store_climbing(out)
    return changed


def _store_climbing(rows: list[dict[str, Any]]) -> None:
    global _climbing
    _climbing = rows[:15]


def _cursors(session) -> dict[str, int]:
    row = session.query(ScanState).filter(ScanState.key == CURSOR_KEY).one_or_none()
    if not row or not row.value:
        return {}
    try:
        raw = json.loads(row.value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key, val in raw.items():
        try:
            out[str(key)] = int(val)
        except (TypeError, ValueError):
            continue
    return out


async def _backfill_blank_pons_meta() -> int:
    """Fill name/symbol for V2 rows ingested during a metadata 429."""
    from ..db import ingest_lock, session_scope

    if not pons.pons_available():
        return 0
    async with ingest_lock:
        with session_scope() as session:
            blanks = (
                session.query(Token.mint)
                .filter(Token.source == "rh_pons", Token.chain == CHAIN, Token.symbol == "")
                .order_by(Token.first_seen_at.desc())
                .limit(pons.MAX_META_PER_POLL)
                .all()
            )
            mints = [row.mint for row in blanks]
    filled = 0
    for mint in mints:
        if not pons.pons_available():
            break
        try:
            meta = await pons.token_metadata(mint)
        except Exception:
            continue
        name = (meta.get("name") or "").strip()
        symbol = (meta.get("symbol") or "").strip()
        if not (name or symbol):
            continue
        async with ingest_lock:
            with session_scope() as session:
                token = session.query(Token).filter(Token.mint == mint).one_or_none()
                if token is None:
                    continue
                token.name = name or token.name
                token.symbol = symbol or token.symbol
                if meta.get("image_url"):
                    token.image_url = meta["image_url"]
                if meta.get("description"):
                    token.description = meta["description"]
                pool = (meta.get("pool_address") or "")[:128]
                if pool:
                    token.pool_address = pool
                filled += 1
    if filled:
        log.info("pons backfilled names on %s blank V2 rows", filled)
    return filled


def _repair_epoch_pons_created(session) -> int:
    """Public RH getLogs stamped V2 TokenLaunched as 1970 (blockTimestamp 0x0)."""
    rows = (
        session.query(Token)
        .filter(Token.source == "rh_pons", Token.created_at_chain < _EPOCH_CUTOFF)
        .all()
    )
    n = 0
    for token in rows:
        token.created_at_chain = token.first_seen_at or utcnow()
        n += 1
    return n


def _save_cursors(session, cursors: dict[str, int]) -> None:
    row = session.query(ScanState).filter(ScanState.key == CURSOR_KEY).one_or_none()
    if row is None:
        row = ScanState(key=CURSOR_KEY)
        session.add(row)
    row.value = json.dumps(cursors)
    row.updated_at = utcnow()


def factories_at_tip(cursors: dict[str, int], tip: int) -> bool:
    if tip <= 0:
        return False
    return all(int(cursors.get(name, 0) or 0) >= tip - TIP_SLACK_BLOCKS for name, _, _ in pons.FACTORIES)


def scan_from_block(cursor: int, start: int, tip: int) -> int:
    """Resume after the cursor, or start at the live tip window (not genesis)."""
    if cursor:
        return cursor + 1
    return max(start, tip - pons.LOOKBACK_BLOCKS)


async def _scan_factory(name: str, address: str, start: int, cursor: int, tip: int) -> tuple[list[dict[str, Any]], int]:
    if tip <= 0:
        return [], cursor
    from_block = scan_from_block(cursor, start, tip)
    if from_block > tip:
        return [], cursor
    events: list[dict[str, Any]] = []
    scanned = cursor
    chunks = 0
    block = from_block
    while block <= tip and chunks < pons.MAX_CHUNKS_PER_POLL:
        end = min(tip, block + pons.CHUNK_BLOCKS - 1)
        logs = await pons.get_logs(
            address=address,
            from_block=block,
            to_block=end,
            topic=pons.launched_topic(address),
        )
        if logs is None:
            break
        for raw in logs:
            decoded = pons.decode_token_launched(raw)
            if decoded:
                events.append(decoded)
        scanned = end
        chunks += 1
        block = end + 1
        if not pons.pons_available():
            break
    return events, scanned


async def poll_pons_launches() -> list[str]:
    """Read TokenLaunched since the last cursor and ingest unknown mints."""
    from ..db import ingest_lock, session_scope

    global _quiet_until
    if not settings.robinhood_enabled:
        return []
    # DB-only: fix 1970 stamps even while the public RPC is sitting out.
    async with ingest_lock:
        with session_scope() as session:
            repaired = _repair_epoch_pons_created(session)
            if repaired:
                log.info("pons repaired %s epoch created_at stamps", repaired)
    if not pons.pons_available() or time.time() < _quiet_until:
        return []
    # Nameless V2 from the boot 429 — spend this poll on names, then resume.
    if await _backfill_blank_pons_meta():
        return []

    tip = await pons.latest_block()
    if not tip:
        return []

    async with ingest_lock:
        with session_scope() as session:
            cursors = _cursors(session)

    if factories_at_tip(cursors, tip):
        _quiet_until = time.time() + QUIET_AT_TIP_S
        return []

    events: list[dict[str, Any]] = []
    updated = dict(cursors)
    # One factory per poll — two wide getLogs bursts 429 the public RPC.
    # Drain an in-progress cursor (V2 leftover in this chunk) before
    # opening a sibling that is still at 0.
    behind = [
        item
        for item in pons.FACTORIES
        if int(cursors.get(item[0], 0) or 0) < tip - TIP_SLACK_BLOCKS
    ]
    if not behind:
        _quiet_until = time.time() + QUIET_AT_TIP_S
        return []
    ranked = sorted(
        behind,
        key=lambda item: (
            0 if int(cursors.get(item[0], 0) or 0) > 0 else 1,
            int(cursors.get(item[0], 0) or 0),
        ),
    )
    name, address, start = ranked[0]
    rows, scanned = await _scan_factory(name, address, start, cursors.get(name, 0), tip)
    events.extend(rows)
    if scanned:
        updated[name] = scanned

    async with ingest_lock:
        with session_scope() as session:
            known = {
                row.mint
                for row in session.query(Token.mint)
                .filter(Token.mint.in_([e["mint"] for e in events] or ["_"]))
                .all()
            }

    found: list[str] = []
    climbing: list[dict[str, Any]] = list(_climbing)
    seen_climb = {r.get("mint") for r in climbing}
    ingested = 0
    for event in events:
        mint = normalize_mint(event.get("mint") or "", CHAIN)
        if not mint or mint in known:
            continue
        if ingested >= pons.MAX_META_PER_POLL:
            break
        try:
            meta = await pons.token_metadata(mint)
        except Exception:
            log.debug("pons metadata failed for %s", mint, exc_info=True)
            meta = {}
        if not (meta.get("name") or meta.get("symbol")):
            # Boot 429 left nameless 0x… cards. Rewind and retry when RPC is up.
            continue
        factory = event.get("factory") or pons.ACTIVE_FACTORY
        try:
            grad = await pons.graduation_status(factory, mint)
        except Exception:
            grad = {}
        coin = pons.launch_to_coin(event, meta)
        try:
            async with ingest_lock:
                with session_scope() as session:
                    if session.query(Token.mint).filter(Token.mint == mint).first() is not None:
                        known.add(mint)
                        continue
                    token = await ingest_and_research(
                        session, mint=mint, source="rh_pons", coin=coin
                    )
                    if token:
                        found.append(mint)
                        known.add(mint)
                        ingested += 1
                        log.info("new pons launch %s %s", token.symbol or mint[:10], mint)
        except (IntegrityError, DataError) as exc:
            log.warning("pons mint %s skipped: %s", mint, exc.__class__.__name__)
            continue
        if mint not in seen_climb and not grad.get("graduated"):
            climbing.append(
                {
                    "mint": mint,
                    "symbol": coin.get("symbol") or "",
                    "name": coin.get("name") or "",
                    "mcap_usd": 0.0,
                    "progress": float(grad.get("progress") or 0.0),
                    "twitter": coin.get("twitter") or "",
                    "website": coin.get("website") or "",
                    "creator": coin.get("creator") or "",
                    "gmgn": pons.pons_token_url(mint),
                    "pons": pons.pons_token_url(mint),
                    "chain": CHAIN,
                    "launchpad": "pons",
                    "factory": factory,
                    "first_seen": datetime.now(timezone.utc).isoformat(),
                }
            )
            seen_climb.add(mint)

    leftover = [
        event
        for event in events
        if (mint := normalize_mint(event.get("mint") or "", CHAIN)) and mint not in known
    ]
    if leftover:
        # Do not skip uningested launches — V2 can emit 100+ TokenLaunched
        # per 8k-block chunk and MAX_META_PER_POLL is 8.
        first_left = int(leftover[0].get("block") or 0)
        if first_left:
            updated[name] = first_left - 1

    async with ingest_lock:
        with session_scope() as session:
            _save_cursors(session, updated)

    # Live 09:13: catch-up then another poll 24s later 429'd. Sit out
    # only when every factory is at the tip and this chunk is drained.
    # Do not apply 120s while V2 still has leftover launches — 38 × 120s
    # would miss the live window.
    if not leftover and factories_at_tip(updated, tip):
        _quiet_until = time.time() + QUIET_AT_TIP_S

    if updated != cursors:
        log.info(
            "pons factory scan tip=%s events=%s ingested=%s cursors=%s",
            tip,
            len(events),
            len(found),
            updated,
        )
    _store_climbing(climbing)
    return found
