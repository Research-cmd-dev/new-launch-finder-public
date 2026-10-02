"""FOMO Tokens→Trending vs the desk.

Sol and Robinhood only. Leftover majors on the board are not a door
miss. Hourly audit stamps a sanity snapshot for Learn. FEATURE_NAMES
stays 66. No extra GMGN. Do not leftover-sort.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..chains import normalize_chain, normalize_mint
from ..db import ingest_lock, session_scope
from ..models import Decision, HuntCard, PaperFill, ScanState, Token, utcnow
from ..research.dexscreener import token_markets
from ..config import settings
from ..research.fomo_api import FomoSitOut, fetch_graduated, fetch_trending
from ..research.fomo_miss_repair import enrich_miss_audit
from ..ingest.fomo_poll import (
    MIN_LIQ_USD,
    TREND_KEY,
    load_trending_snapshot,
    max_age_hours,
    save_trending_snapshot,
)

log = logging.getLogger("launchfinder.fomo_coverage")

AUDIT_KEY = "fomo_trending_audit"
AUDIT_HISTORY_KEY = "fomo_trending_audit_history"
AUDIT_HISTORY_KEEP = 24  # ~1 day of hourly stamps
AUDIT_EVERY_S = 3600.0
# Worker chair: rewrite /health loop note on fomo_poll cadence; full audit stays hourly.
FOMO_AUDIT_TIMEOUT_S = 120.0
FOMO_COVERAGE_TIMEOUT_S = 75.0
FOMO_HTTP_TIMEOUT_S = 60.0
DEX_HTTP_TIMEOUT_S = 60.0
FOMO_DB_STATEMENT_MS = 8_000
FOMO_DB_RETRY_STATEMENT_MS = 5_000
FOMO_AUDIT_WRITE_TIMEOUT_S = 12.0
INGEST_LOCK_TRY_S = 3.0


def _fomo_db_defer(exc: BaseException) -> bool:
    from ..scoring.hunt import _hunt_row_lock_contention, _hunt_session_defer

    return _hunt_row_lock_contention(exc) or _hunt_session_defer(exc)


def fomo_trending_chair_seconds() -> float:
    """How often the worker rewrites loop:fomo_trending (not the hourly audit stamp)."""
    from ..config import settings

    every = float(getattr(settings, "fomo_poll_seconds", None) or 600.0)
    return max(300.0, every)


def fomo_error_note(exc: BaseException) -> str:
    """Short operator-facing error for /health loop notes."""
    name = type(exc).__name__
    msg = str(exc).strip().replace("\n", " ")
    if msg and msg != name:
        bit = msg[:72]
        return f"{name}: {bit}"
    return name


def fomo_heartbeat_note(
    *,
    sit_out: int | None = None,
    skipped: bool = False,
    sanity: dict[str, Any] | None = None,
    error: str = "",
) -> str:
    """Fresh /health note. A 402 sit-out is a heartbeat, not a sticky skip."""
    if error:
        err = error.strip()
        if len(err) > 96:
            err = err[:93] + "..."
        return f"defer {err}" if "lock" in err.lower() or "timeout" in err.lower() else f"error {err}"
    bits: list[str] = []
    if sit_out:
        bits.append(f"sit-out {int(sit_out)}")
    sanity = sanity if isinstance(sanity, dict) else {}
    bits.append(f"seen={sanity.get('seen_rate')}")
    bits.append(f"miss={sanity.get('miss_n')}")
    bits.append(f"hijack={sanity.get('veto_hijack_n')}")
    if sanity.get("board_stale"):
        age_h = sanity.get("capture_age_hours")
        bits.append(f"mirror_stale={age_h if age_h is not None else '?'}h")
    if skipped and not sit_out:
        bits.append("audit fresh")
    return " · ".join(bits)


def stamp_fomo_trending_heartbeat(
    session: Session,
    *,
    sit_out: int | None = None,
    skipped: bool = False,
    sanity: dict[str, Any] | None = None,
    error: str = "",
) -> str:
    """Rewrite the loop note every cycle so a sit-out cannot leave it stale."""
    from ..ledger import beat

    note = fomo_heartbeat_note(sit_out=sit_out, skipped=skipped, sanity=sanity, error=error)
    beat(session, "fomo_trending", note=note)
    return note


@contextmanager
def _fomo_db_scope(*, statement_ms: int = FOMO_DB_STATEMENT_MS):
    """Short statement budget — fail fast under hunt/tape lock contention."""
    from ..db import apply_report_guards

    with session_scope() as session:
        apply_report_guards(session, timeout_ms=statement_ms)
        yield session


async def _try_acquire_ingest_lock() -> bool:
    if ingest_lock.locked():
        try:
            await asyncio.wait_for(ingest_lock.acquire(), timeout=INGEST_LOCK_TRY_S)
            return True
        except TimeoutError:
            return False
    await ingest_lock.acquire()
    return True


async def _persist_trending_snapshot(rows: list[dict]) -> bool:
    """Best-effort trending board stamp; never blocks the audit chair on hunt ingest."""

    def _write() -> None:
        with _fomo_db_scope(statement_ms=FOMO_DB_RETRY_STATEMENT_MS) as write_sess:
            save_trending_snapshot(write_sess, rows, utcnow())

    if not await _try_acquire_ingest_lock():
        log.info("fomo trending snapshot skip (ingest_lock busy)")
        return False
    try:
        await asyncio.wait_for(asyncio.to_thread(_write), timeout=INGEST_LOCK_TRY_S)
        return True
    except TimeoutError:
        log.warning("fomo trending snapshot write timed out")
        return False
    except Exception as exc:
        if _fomo_db_defer(exc):
            log.info("fomo trending snapshot defer (%s)", type(exc).__name__)
            return False
        raise
    finally:
        ingest_lock.release()


def _age_s(updated_at: str | None) -> float | None:
    if not updated_at:
        return None
    try:
        ts = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0.0, (utcnow() - ts).total_seconds())


async def _resolve_trending_board(
    snap: dict[str, Any] | None,
) -> tuple[list[dict], dict[str, Any], list[dict] | None]:
    """Always hit keyed FOMO trending REST for upstream mirror meta.

    Local snapshot is fallback only on timeout / sit-out / transport error — never
    to skip HTTP when the snap row is young (v188: stale API capture hid behind snap).
    """
    age = _age_s((snap or {}).get("at") or (snap or {}).get("updated_at"))
    meta: dict[str, Any] = {
        "snapshot_age_s": age,
        "key": TREND_KEY,
    }
    try:
        fetched = await asyncio.wait_for(fetch_trending(limit=50), timeout=FOMO_HTTP_TIMEOUT_S)
    except TimeoutError:
        log.warning("FOMO trending fetch timed out after %ss", FOMO_HTTP_TIMEOUT_S)
        rows = list((snap or {}).get("items") or [])
        meta["source"] = "snapshot" if rows else "error"
        meta["fetch_timeout"] = True
        return rows, meta, None
    except FomoSitOut as exc:
        log.warning("FOMO trending sit-out %s", exc.status)
        rows = list((snap or {}).get("items") or [])
        meta["source"] = "snapshot" if rows else "sit-out"
        meta["sit_out"] = exc.status
        return rows, meta, None
    except Exception:
        log.exception("FOMO trending fetch failed")
        rows = list((snap or {}).get("items") or [])
        meta["source"] = "snapshot" if rows else "error"
        return rows, meta, None
    rows = list(fetched.rows or [])
    upstream = dict(fetched.upstream or {})
    meta.update(upstream)
    meta["board_live"] = not bool(upstream.get("board_stale"))
    if upstream.get("board_stale"):
        meta["source"] = "fomo_api_capture_stale"
        log.warning(
            "FOMO trending API mirror stale (source=%s capture_age_h=%s)",
            upstream.get("api_source"),
            upstream.get("capture_age_hours"),
        )
        persist = None
    else:
        meta["source"] = "live"
        persist = rows if rows else None
    return rows, meta, persist


async def _board_rows(session: Session) -> tuple[list[dict], dict[str, Any]]:
    snap = load_trending_snapshot(session)
    rows, meta, persist = await _resolve_trending_board(snap)
    if persist:
        save_trending_snapshot(session, persist, utcnow())
    return rows, meta


def _desk_row(session: Session, mint: str, chain: str) -> dict[str, Any] | None:
    chain = normalize_chain(chain)
    mint = normalize_mint(mint, chain)
    token = (
        session.query(Token)
        .filter(Token.chain == chain, Token.mint == mint)
        .one_or_none()
    )
    if token is None and chain == "robinhood":
        token = (
            session.query(Token)
            .filter(Token.chain == chain, Token.mint == mint.lower())
            .one_or_none()
        )
    if token is None:
        token = session.query(Token).filter(Token.mint == mint).one_or_none()
        if token is not None and normalize_chain(token.chain or "") != chain:
            token = None
    if token is None:
        return None
    hunt = (
        session.query(HuntCard)
        .filter(HuntCard.chain == chain, HuntCard.mint == token.mint)
        .first()
    )
    research = token.research
    outcome = token.outcome
    flags = []
    try:
        flags = json.loads((research.risk_flags_json if research else "[]") or "[]")
    except Exception:
        flags = []
    gate = (
        session.query(Decision)
        .filter(Decision.token_id == token.id, Decision.kind == "gate")
        .order_by(Decision.id.desc())
        .first()
    )
    entry = (
        session.query(Decision)
        .filter(Decision.token_id == token.id, Decision.kind == "entry")
        .order_by(Decision.id.desc())
        .first()
    )
    papers = (
        session.query(PaperFill.line, PaperFill.status)
        .filter(PaperFill.mint == token.mint)
        .all()
    )
    paper_lines = {str(line): str(status) for line, status in papers}
    veto = (gate.veto if gate is not None else "") or ""
    return {
        "on_desk": True,
        "on_hunt": hunt is not None,
        "symbol": token.symbol,
        "source": token.source,
        "first_seen_at": token.first_seen_at.isoformat() if token.first_seen_at else None,
        "entry_p": float(
            (entry.entry_p if entry is not None else 0.0)
            or (research.p_good if research and research.p_good is not None else 0.0)
            or 0.0
        )
        or None,
        "live_p": float(hunt.conviction_p or 0.0) if hunt is not None else None,
        "last_mcap": float(outcome.last_mcap or 0.0) if outcome else 0.0,
        "last_liq": float(outcome.last_liq or 0.0) if outcome else 0.0,
        "multiple": float(outcome.multiple or 0.0) if outcome else 0.0,
        "gate_veto": veto,
        "paper": paper_lines,
        "ingest_miss": any("ingest score is the t0 miss" in str(f).lower() for f in flags),
    }


def _bucket(item: dict[str, Any]) -> str:
    """Sanity taxonomy for Learn / hourly audit."""
    status = str(item.get("status") or "")
    if status in ("miss", "leftover", "thin", "no_dex"):
        return status
    if status != "caught":
        return status or "unknown"
    veto = str(item.get("gate_veto") or "").strip()
    paper = item.get("paper") if isinstance(item.get("paper"), dict) else {}
    if paper.get("paper_v1") in ("open", "queued", "closed"):
        return "short_list"
    if paper.get("gated90") in ("open", "closed"):
        return "wide_paper"
    if veto:
        # Seen + filtered — the e/acc class lands here.
        if "hijack" in veto or "celebrity/brand" in veto:
            return "veto_hijack"
        return "veto_other"
    if item.get("on_hunt"):
        return "on_hunt"
    return "on_desk"


async def _classify_unknown(rows: list[dict]) -> dict[str, str]:
    """Dex-hydrate desk misses. leftover / thin / no_dex / miss."""
    by_chain: dict[str, list[str]] = {"sol": [], "robinhood": []}
    for row in rows:
        chain = normalize_chain(row.get("chain"))
        mint = row.get("mint") or ""
        if mint and chain in by_chain:
            by_chain[chain].append(mint)
    out: dict[str, str] = {}
    now = utcnow()
    for chain, mints in by_chain.items():
        if not mints:
            continue
        try:
            markets = await asyncio.wait_for(
                token_markets(mints, chain),
                timeout=DEX_HTTP_TIMEOUT_S,
            )
        except TimeoutError:
            log.warning("FOMO coverage dex timed out %s after %ss", chain, DEX_HTTP_TIMEOUT_S)
            markets = {}
        except Exception:
            log.debug("FOMO coverage dex failed %s", chain, exc_info=True)
            markets = {}
        age_limit = max_age_hours(chain)
        for mint in mints:
            market = markets.get(normalize_mint(mint, chain)) or {}
            created = market.get("created_at")
            if not market or created is None:
                out[mint] = "no_dex"
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_h = (now - created).total_seconds() / 3600.0
            liq = float(market.get("liquidity_usd") or 0.0)
            if age_h < 0 or age_h > age_limit:
                out[mint] = "leftover"
            elif liq < MIN_LIQ_USD:
                out[mint] = "thin"
            else:
                out[mint] = "miss"
    return out


def _desk_items_from_rows(session: Session, rows: list[dict]) -> tuple[list[dict[str, Any]], list[dict]]:
    desk_items: list[dict[str, Any]] = []
    unknown: list[dict] = []
    for raw in rows:
        chain = normalize_chain(raw.get("chain") or raw.get("network"))
        if chain not in {"sol", "robinhood"}:
            continue
        mint = normalize_mint(raw.get("mint") or "", chain)
        if not mint:
            continue
        item = {
            "mint": mint,
            "chain": chain,
            "symbol": raw.get("symbol") or "",
            "name": raw.get("name") or "",
            "rank": raw.get("rank"),
            "mcap_usd": float(raw.get("mcap_usd") or 0.0),
            "status": "unknown",
        }
        found = _desk_row(session, mint, chain)
        if found:
            item.update(found)
            item["status"] = "caught"
        else:
            item["on_desk"] = False
            item["on_hunt"] = False
            unknown.append(item)
        desk_items.append(item)
    return desk_items, unknown


def _ws_alert_hot_mints(session: Session, *, window_min: int = 90, limit: int = 12) -> list[dict[str, Any]]:
    """Recent /ws/alerts buy flow — not FOMO app trending rank (secondary when mirror stale)."""
    from sqlalchemy import func

    from ..models import FomoAlertEvent

    since = utcnow() - timedelta(minutes=max(15, window_min))
    floor = float(getattr(settings, "fomo_alerts_min_usd", 2000))
    rows = (
        session.query(
            FomoAlertEvent.chain,
            FomoAlertEvent.mint,
            FomoAlertEvent.token_symbol,
            func.count(FomoAlertEvent.id),
            func.sum(FomoAlertEvent.usd_value),
        )
        .filter(
            FomoAlertEvent.received_at >= since,
            FomoAlertEvent.alert_type == "buy",
            FomoAlertEvent.usd_value >= floor,
        )
        .group_by(FomoAlertEvent.chain, FomoAlertEvent.mint, FomoAlertEvent.token_symbol)
        .order_by(func.sum(FomoAlertEvent.usd_value).desc())
        .limit(max(1, min(int(limit), 20)))
        .all()
    )
    out: list[dict[str, Any]] = []
    for chain, mint, symbol, n_buys, usd_sum in rows:
        out.append(
            {
                "chain": chain,
                "mint": mint,
                "symbol": symbol or "",
                "buy_events": int(n_buys or 0),
                "usd_sum": round(float(usd_sum or 0.0), 2),
                "note": "ws/alerts buy flow — not FOMO trending rank",
            }
        )
    return out


def _secondary_board_overlap(
    fomo_mints: set[str],
    *,
    graduated: list[dict[str, Any]],
    gmgn: list[dict[str, Any]],
    dexscreener: list[dict[str, Any]],
) -> dict[str, Any]:
    """Informational mint overlap — does not affect fomo_mirror gate."""
    def _mints(items: list[dict[str, Any]]) -> set[str]:
        return {str(i.get("mint") or "") for i in items if i.get("mint")}

    return {
        "fomo_trending": len(fomo_mints),
        "graduated": len(fomo_mints & _mints(graduated)),
        "gmgn_trending": len(fomo_mints & _mints(gmgn)),
        "dexscreener_trending": len(fomo_mints & _mints(dexscreener)),
    }


def _secondary_pack_usable(pack: dict[str, Any] | None) -> bool:
    """Board contributed rows — skip empty or explicitly skipped packs."""
    if not pack:
        return False
    if pack.get("skipped"):
        return False
    return bool(pack.get("items"))


def _item_is_miss(item: dict[str, Any]) -> bool:
    return str(item.get("status") or "") == "miss" or str(item.get("bucket") or "") == "miss"


def _build_secondary_miss_audit(
    *,
    trending_items: list[dict[str, Any]],
    graduated_pack: dict[str, Any] | None,
    gmgn_pack: dict[str, Any] | None,
    dex_pack: dict[str, Any] | None,
    grad_desk: list[dict[str, Any]],
    gmgn_desk: list[dict[str, Any]],
    dex_desk: list[dict[str, Any]],
    overlap_with_fomo: dict[str, Any] | None,
) -> dict[str, Any]:
    """Union secondary mints — door miss sanity (always on; not a buy list)."""
    boards: list[tuple[str, list[dict[str, Any]], dict[str, Any] | None]] = [
        ("graduated", grad_desk, graduated_pack),
        ("gmgn_trending", gmgn_desk, gmgn_pack),
        ("dexscreener_trending", dex_desk, dex_pack),
    ]
    counts_by_source: dict[str, dict[str, int]] = {}
    by_mint: dict[str, dict[str, Any]] = {}
    for source, chunk, pack in boards:
        if not _secondary_pack_usable(pack):
            counts_by_source[source] = {"board": 0, "miss": 0, "caught": 0, "skipped": 1}
            continue
        counts_by_source[source] = {
            "board": len(chunk),
            "miss": sum(1 for i in chunk if _item_is_miss(i)),
            "caught": sum(1 for i in chunk if i.get("status") == "caught"),
            "skipped": 0,
        }
        for item in chunk:
            mint = str(item.get("mint") or "")
            if not mint:
                continue
            slot = by_mint.setdefault(
                mint,
                {
                    "mint": mint,
                    "chain": item.get("chain") or "sol",
                    "symbol": item.get("symbol") or "",
                    "rank": item.get("rank"),
                    "mcap_usd": float(item.get("mcap_usd") or 0.0),
                    "sources": [],
                    "by_source": {},
                },
            )
            if source not in slot["sources"]:
                slot["sources"].append(source)
            slot["by_source"][source] = item
            sym = str(item.get("symbol") or "")
            if sym and (not slot["symbol"] or len(sym) < len(str(slot["symbol"]))):
                slot["symbol"] = sym
            r = item.get("rank")
            if r is not None and (slot["rank"] is None or int(r) < int(slot["rank"])):
                slot["rank"] = r
            mcap = float(item.get("mcap_usd") or 0.0)
            if mcap > float(slot.get("mcap_usd") or 0.0):
                slot["mcap_usd"] = mcap

    trending_by_mint = {str(i.get("mint") or ""): i for i in trending_items if i.get("mint")}
    union_items: list[dict[str, Any]] = []
    miss_rows: list[dict[str, Any]] = []
    high_conf: list[dict[str, Any]] = []
    for mint, slot in by_mint.items():
        # Prefer the richest desk merge among source rows (caught > classified).
        merged: dict[str, Any] = {}
        for src in slot["sources"]:
            it = slot["by_source"].get(src) or {}
            if not merged or (it.get("on_desk") and not merged.get("on_desk")):
                merged = dict(it)
        if not merged:
            merged = dict(next(iter(slot["by_source"].values())))
        merged["mint"] = mint
        merged["sources"] = list(slot["sources"])
        merged["bucket"] = _bucket(merged)
        union_items.append(merged)

        if not _item_is_miss(merged):
            continue
        miss_sources = [s for s in slot["sources"] if _item_is_miss(slot["by_source"].get(s) or {})]
        on_trend = mint in trending_by_mint
        trend_miss = on_trend and _item_is_miss(trending_by_mint[mint])
        hc = len(miss_sources) >= 2 or (len(miss_sources) >= 1 and trend_miss)
        row = {
            "mint": mint,
            "chain": merged.get("chain") or "sol",
            "symbol": slot.get("symbol") or merged.get("symbol") or "",
            "rank": slot.get("rank"),
            "mcap_usd": slot.get("mcap_usd") or merged.get("mcap_usd"),
            "sources": list(slot["sources"]),
            "miss_sources": miss_sources,
            "on_fomo_trending": on_trend,
            "fomo_trending_miss": trend_miss,
            "high_confidence_miss": hc,
            "bucket": merged.get("bucket"),
            "ingest_miss": bool(merged.get("ingest_miss")),
        }
        miss_rows.append(row)
        if hc:
            high_conf.append(row)

    miss_rows.sort(key=lambda r: (-len(r.get("miss_sources") or []), -(float(r.get("mcap_usd") or 0))))
    return {
        "note": (
            "Union of FOMO graduated + GMGN trending + DexScreener boosts/profiles — "
            "door miss sanity only; not buys; does not green fomo_mirror."
        ),
        "counts_by_source": counts_by_source,
        "union_board": len(union_items),
        "union_miss": len(miss_rows),
        "high_confidence_miss_n": len(high_conf),
        "overlap_with_fomo_trending": overlap_with_fomo or {},
        "misses": miss_rows[:40],
        "high_confidence_misses": high_conf[:20],
    }


def _pack_secondary_board(
    *,
    board_kind: str,
    default_note: str,
    upstream: dict[str, Any],
    desk_items: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "board_kind": board_kind,
        "note": upstream.get("board_note") or default_note,
        "api_source": upstream.get("api_source"),
        "captured_at": upstream.get("captured_at"),
        "capture_age_hours": upstream.get("capture_age_hours"),
        "board_stale": upstream.get("board_stale"),
        "board_live": upstream.get("board_live"),
        "skipped": upstream.get("skipped"),
        "skip_reason": upstream.get("skip_reason"),
        "empty_reason": upstream.get("empty_reason"),
        "error": upstream.get("error"),
        "cooldown_s": upstream.get("cooldown_s"),
        "interval": upstream.get("interval"),
        "fetch_paths": upstream.get("fetch_paths"),
        "response_keys": upstream.get("response_keys"),
        "merged_raw_n": upstream.get("merged_raw_n"),
        "items": desk_items,
        "counts": {
            "board": len(desk_items),
            "caught": sum(1 for i in desk_items if i.get("status") == "caught"),
            "miss": sum(1 for i in desk_items if i.get("status") == "miss"),
        },
    }


async def _desk_items_for_secondary_rows(
    rows: list[dict[str, Any]],
    *,
    board_kind: str,
) -> list[dict[str, Any]]:
    if not rows:
        return []
    with _fomo_db_scope() as sess:
        desk, unknown = _desk_items_from_rows(sess, rows)
    for item in desk:
        item["board_kind"] = board_kind
    if unknown:
        reasons = await _classify_unknown(unknown)
        for item in unknown:
            item["status"] = reasons.get(item["mint"], "miss")
    return desk


def _finalize_coverage(
    session: Session,
    desk_items: list[dict[str, Any]],
    meta: dict[str, Any],
    *,
    sanity_items: list[dict[str, Any]] | None = None,
    graduated_board: dict[str, Any] | None = None,
    gmgn_trending_board: dict[str, Any] | None = None,
    dexscreener_trending_board: dict[str, Any] | None = None,
    secondary_overlap: dict[str, Any] | None = None,
    secondary_miss_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    for item in desk_items:
        item.setdefault("board_kind", "trending")
        item["bucket"] = _bucket(item)
    sanity_pool = sanity_items if sanity_items is not None else desk_items
    for item in sanity_pool:
        item.setdefault("board_kind", item.get("board_kind") or "sanity")
        if "bucket" not in item:
            item["bucket"] = _bucket(item)
    counts = {
        "board": len(sanity_pool),
        "caught": sum(1 for i in sanity_pool if i["status"] == "caught"),
        "miss": sum(1 for i in sanity_pool if i["status"] == "miss"),
        "leftover": sum(1 for i in sanity_pool if i["status"] == "leftover"),
        "thin": sum(1 for i in sanity_pool if i["status"] == "thin"),
        "no_dex": sum(1 for i in sanity_pool if i["status"] == "no_dex"),
    }
    trending_counts = {
        "board": len(desk_items),
        "caught": sum(1 for i in desk_items if i["status"] == "caught"),
        "miss": sum(1 for i in desk_items if i["status"] == "miss"),
    }
    by_bucket: dict[str, int] = {}
    for item in sanity_pool:
        b = str(item.get("bucket") or "unknown")
        by_bucket[b] = by_bucket.get(b, 0) + 1
    actionable = [
        i
        for i in sanity_pool
        if i.get("bucket") not in ("leftover", "thin", "no_dex")
    ]
    n_act = len(actionable)
    seen = sum(1 for i in actionable if i.get("on_desk"))
    last_audit = load_fomo_trending_audit(session)
    from .rh_hydrate_alarm import all_hunt_mcap_hydrate_gaps

    rh_hydrate_alarms: list[dict[str, Any]] = []
    try:
        rh_hydrate_alarms = all_hunt_mcap_hydrate_gaps(session, desk_items)
    except Exception as exc:
        if not _fomo_db_defer(exc):
            raise
        log.info("fomo coverage rh hydrate defer (%s)", type(exc).__name__)
    fomo_no_hunt: dict[str, Any] = {}
    try:
        from ..scoring.fomo_trend_no_hunt import annotate_fomo_items, capture_fomo_trend_no_hunt

        fomo_no_hunt = capture_fomo_trend_no_hunt(session, desk_items)
        annotate_fomo_items(desk_items, fomo_no_hunt)
        if sanity_pool is not desk_items:
            annotate_fomo_items(sanity_pool, fomo_no_hunt)
    except Exception as exc:
        if not _fomo_db_defer(exc):
            raise
        log.info("fomo coverage no-hunt learn defer (%s)", type(exc).__name__)
    board_stale = bool(meta.get("board_stale"))
    sanity_note = (
        "Hourly sanity check — not a buy list. leftover/thin ignored. "
        "miss = door bug; veto_hijack = seen but filtered (e/acc class)."
    )
    if board_stale:
        sanity_note = (
            (meta.get("mirror_note") or "FOMO API trending mirror is stale vs the app.")
            + " Trending rows are informational; door sanity uses secondary boards (graduated / GMGN / DexScreener) — not FOMO trending rank."
        )
    ws_hot: list[dict[str, Any]] = []
    if board_stale:
        try:
            ws_hot = _ws_alert_hot_mints(session)
        except Exception as exc:
            if not _fomo_db_defer(exc):
                raise
            log.info("fomo coverage ws hot defer (%s)", type(exc).__name__)
    return {
        "items": desk_items,
        "trending_items": desk_items,
        "trending_counts": trending_counts,
        "counts": counts,
        "by_bucket": by_bucket,
        "misses": [i for i in sanity_pool if i["status"] == "miss"],
        "vetoed": [i for i in sanity_pool if str(i.get("bucket") or "").startswith("veto_")],
        "short_list": [i for i in sanity_pool if i.get("bucket") == "short_list"],
        "graduated_board": graduated_board,
        "gmgn_trending_board": gmgn_trending_board,
        "dexscreener_trending_board": dexscreener_trending_board,
        "secondary_overlap": secondary_overlap,
        "secondary_miss_audit": secondary_miss_audit,
        "secondary_sanity_sources": meta.get("secondary_sanity_sources") or [],
        "sanity_board": meta.get("sanity_board") or ("trending" if not board_stale else "trending_only"),
        "board_stale": board_stale,
        "board_live": meta.get("board_live"),
        "api_source": meta.get("api_source"),
        "captured_at": meta.get("captured_at"),
        "capture_age_hours": meta.get("capture_age_hours"),
        "capture_age_s": meta.get("capture_age_s"),
        "mirror_note": meta.get("mirror_note") or "",
        "ws_alert_hot_mints": ws_hot,
        "sanity": {
            "actionable": n_act,
            "seen_rate": round(seen / n_act, 3) if n_act else None,
            "miss_n": sum(1 for i in actionable if i.get("bucket") == "miss"),
            "veto_hijack_n": sum(1 for i in actionable if i.get("bucket") == "veto_hijack"),
            "short_list_n": sum(1 for i in actionable if i.get("bucket") == "short_list"),
            "no_hunt_n": int((fomo_no_hunt or {}).get("n") or 0),
            "desk_no_hunt_n": int((fomo_no_hunt or {}).get("n_desk_no_hunt") or 0),
            "board_stale": board_stale,
            "capture_age_hours": meta.get("capture_age_hours"),
            "note": sanity_note,
        },
        "last_audit": last_audit,
        "rh_hydrate_alarms": rh_hydrate_alarms,
        "fomo_trend_no_hunt": fomo_no_hunt,
        **meta,
    }


async def _fetch_gmgn_trending_secondary() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from ..research import gmgn

    try:
        return await asyncio.wait_for(
            gmgn.sol_trending_rank_rows(limit=50, interval="1h"),
            timeout=FOMO_HTTP_TIMEOUT_S,
        )
    except TimeoutError:
        log.warning("GMGN trending fetch timed out after %ss", FOMO_HTTP_TIMEOUT_S)
        return [], {"fetch_timeout": True, "board_kind": "gmgn_trending", "api_source": "gmgn"}
    except Exception:
        log.exception("GMGN trending fetch failed")
        return [], {"board_kind": "gmgn_trending", "api_source": "gmgn", "error": True}


async def _fetch_dexscreener_trending_secondary() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from ..research.dexscreener import sol_trending_boost_rows

    try:
        return await asyncio.wait_for(sol_trending_boost_rows(limit=50), timeout=DEX_HTTP_TIMEOUT_S)
    except TimeoutError:
        log.warning("DexScreener trending fetch timed out after %ss", DEX_HTTP_TIMEOUT_S)
        return [], {"fetch_timeout": True, "board_kind": "dexscreener_trending", "api_source": "dexscreener"}
    except Exception:
        log.exception("DexScreener trending fetch failed")
        return [], {"board_kind": "dexscreener_trending", "api_source": "dexscreener", "error": True}


async def _fetch_graduated_secondary() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Live-fomo graduated board when trending capture is stale."""
    try:
        fetched = await asyncio.wait_for(fetch_graduated(limit=50), timeout=FOMO_HTTP_TIMEOUT_S)
    except TimeoutError:
        log.warning("FOMO graduated fetch timed out after %ss", FOMO_HTTP_TIMEOUT_S)
        return [], {"fetch_timeout": True, "board_kind": "graduated"}
    except FomoSitOut as exc:
        log.warning("FOMO graduated sit-out %s", exc.status)
        return [], {"sit_out": exc.status, "board_kind": "graduated"}
    except Exception:
        log.exception("FOMO graduated fetch failed")
        return [], {"board_kind": "graduated", "error": True}
    upstream = dict(fetched.upstream or {})
    upstream["board_live"] = not bool(upstream.get("board_stale"))
    return list(fetched.rows or []), upstream


async def fomo_trending_coverage(session: Session | None = None) -> dict[str, Any]:
    """Compare the FOMO trending board to Hunt / Token rows.

    Does not hold a DB transaction across FOMO/Dex HTTP (worker lock-timeout class).
    """
    with _fomo_db_scope() as read_sess:
        snap = load_trending_snapshot(read_sess)
    rows, meta, persist = await _resolve_trending_board(snap)
    if persist:
        await _persist_trending_snapshot(persist)

    with _fomo_db_scope() as desk_sess:
        desk_items, unknown = _desk_items_from_rows(desk_sess, rows)

    if unknown:
        reasons = await _classify_unknown(unknown)
        for item in unknown:
            item["status"] = reasons.get(item["mint"], "miss")

    grad_rows, grad_upstream = await _fetch_graduated_secondary()
    gmgn_rows, gmgn_upstream = await _fetch_gmgn_trending_secondary()
    dex_rows, dex_upstream = await _fetch_dexscreener_trending_secondary()

    grad_desk = await _desk_items_for_secondary_rows(grad_rows, board_kind="graduated")
    gmgn_desk = await _desk_items_for_secondary_rows(gmgn_rows, board_kind="gmgn_trending")
    dex_desk = await _desk_items_for_secondary_rows(dex_rows, board_kind="dexscreener_trending")

    graduated_pack = _pack_secondary_board(
        board_kind="graduated",
        default_note="Secondary FOMO Tokens→Graduated — not Trending rank.",
        upstream=grad_upstream,
        desk_items=grad_desk,
    )
    gmgn_pack = _pack_secondary_board(
        board_kind="gmgn_trending",
        default_note="Secondary GMGN swap trending — not FOMO Tokens→Trending.",
        upstream=gmgn_upstream,
        desk_items=gmgn_desk,
    )
    dex_pack = _pack_secondary_board(
        board_kind="dexscreener_trending",
        default_note="Secondary DexScreener profiles/boosts — not FOMO Tokens→Trending.",
        upstream=dex_upstream,
        desk_items=dex_desk,
    )

    fomo_mints = {str(i.get("mint") or "") for i in desk_items if i.get("mint")}
    overlap = _secondary_board_overlap(
        fomo_mints,
        graduated=grad_desk,
        gmgn=gmgn_desk,
        dexscreener=dex_desk,
    )

    sanity_items: list[dict[str, Any]] = desk_items
    if meta.get("board_stale"):
        merged: list[dict[str, Any]] = []
        seen_mint: set[str] = set()
        sources: list[str] = []
        for kind, chunk in (
            ("graduated", grad_desk),
            ("gmgn_trending", gmgn_desk),
            ("dexscreener_trending", dex_desk),
        ):
            if not chunk:
                continue
            sources.append(kind)
            for item in chunk:
                mint = str(item.get("mint") or "")
                if not mint or mint in seen_mint:
                    continue
                seen_mint.add(mint)
                item["board_kind"] = kind
                merged.append(item)
        if merged:
            sanity_items = merged
            meta["sanity_board"] = "secondary_boards"
            meta["secondary_sanity_sources"] = sources

    miss_audit = _build_secondary_miss_audit(
        trending_items=desk_items,
        graduated_pack=graduated_pack,
        gmgn_pack=gmgn_pack,
        dex_pack=dex_pack,
        grad_desk=grad_desk,
        gmgn_desk=gmgn_desk,
        dex_desk=dex_desk,
        overlap_with_fomo=overlap,
    )
    miss_audit = await enrich_miss_audit(miss_audit, trending_items=desk_items)
    for item in desk_items:
        if _item_is_miss(item):
            mint = str(item.get("mint") or "")
            for row in (miss_audit.get("trending_misses") or []):
                if str(row.get("mint") or "") == mint:
                    item["miss_reason"] = row.get("miss_reason")
                    break

    with _fomo_db_scope() as fin_sess:
        return _finalize_coverage(
            fin_sess,
            desk_items,
            meta,
            sanity_items=sanity_items,
            graduated_board=graduated_pack,
            gmgn_trending_board=gmgn_pack,
            dexscreener_trending_board=dex_pack,
            secondary_overlap=overlap,
            secondary_miss_audit=miss_audit,
        )


def fomo_desk_metrics_for_chain(session: Session, chain: str) -> dict[str, dict[str, Any]]:
    """Sync desk merge for FOMO board mints — same multiples as ``/api/fomo-trending``.

    Worker paper-sync uses this when ``Outcome`` / shadow rows are flat ~1× but the
    hourly board merge still shows an honest printer multiple.
    """
    chain = normalize_chain(chain)
    out: dict[str, dict[str, Any]] = {}
    snap = load_trending_snapshot(session)
    for raw in (snap or {}).get("items") or []:
        c = normalize_chain(raw.get("chain") or "sol")
        if c != chain:
            continue
        mint = normalize_mint(str(raw.get("mint") or ""), c)
        if not mint:
            continue
        desk = _desk_row(session, mint, c) or {}
        item = {
            "mint": mint,
            "chain": c,
            "symbol": raw.get("symbol") or desk.get("symbol") or "",
            "mcap_usd": float(raw.get("mcap_usd") or desk.get("last_mcap") or 0.0),
            "status": "caught" if desk else "unknown",
            **desk,
        }
        item["multiple"] = float(desk.get("multiple") or 0.0)
        item["last_mcap"] = float(desk.get("last_mcap") or 0.0)
        item["bucket"] = _bucket(item)
        row = {
            "mint": mint,
            "chain": c,
            "symbol": item.get("symbol") or "",
            "mcap_usd": float(item.get("mcap_usd") or 0.0),
            "multiple": float(item.get("multiple") or 0.0),
            "last_mcap": float(item.get("last_mcap") or 0.0),
            "bucket": str(item.get("bucket") or ""),
            "gate_veto": str(item.get("gate_veto") or ""),
            "on_hunt": bool(item.get("on_hunt")),
            "ingest_miss": bool(item.get("ingest_miss")),
        }
        out[mint] = row
        if c == "robinhood":
            out.setdefault(mint.lower(), row)
    audit = load_fomo_trending_audit(session) or {}
    for key in ("items", "top", "vetoed"):
        for item in audit.get(key) or []:
            if not isinstance(item, dict):
                continue
            if normalize_chain(item.get("chain") or "sol") != chain:
                continue
            mint = normalize_mint(str(item.get("mint") or ""), chain)
            if not mint:
                continue
            prev = out.get(mint) or out.get(mint.lower()) if chain == "robinhood" else out.get(mint)
            mult = float(item.get("multiple") or 0.0)
            merged = dict(prev or {})
            merged.update(
                {
                    "mint": mint,
                    "chain": chain,
                    "symbol": item.get("symbol") or merged.get("symbol") or "",
                    "multiple": max(mult, float(merged.get("multiple") or 0.0)),
                    "bucket": item.get("bucket") or merged.get("bucket") or "",
                    "gate_veto": item.get("gate_veto") or merged.get("gate_veto") or "",
                }
            )
            out[mint] = merged
            if chain == "robinhood":
                out[mint.lower()] = merged
    return out


def load_fomo_trending_audit(session: Session) -> dict[str, Any] | None:
    row = session.query(ScanState).filter(ScanState.key == AUDIT_KEY).one_or_none()
    if row is None or not row.value:
        return None
    try:
        data = json.loads(row.value)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _audit_due(session: Session, *, now: datetime | None = None) -> bool:
    now = now or utcnow()
    prev = load_fomo_trending_audit(session)
    if not prev or not prev.get("at"):
        return True
    age = _age_s(str(prev.get("at")))
    return age is None or age >= AUDIT_EVERY_S


def _save_audit(
    session: Session,
    payload: dict[str, Any],
    *,
    now: datetime,
    history: bool = True,
) -> None:
    raw = json.dumps(payload)
    row = session.query(ScanState).filter(ScanState.key == AUDIT_KEY).one_or_none()
    if row is None:
        session.add(ScanState(key=AUDIT_KEY, value=raw, updated_at=now))
    else:
        row.value = raw
        row.updated_at = now
    if history:
        hist_row = session.query(ScanState).filter(ScanState.key == AUDIT_HISTORY_KEY).one_or_none()
        hist: list[dict[str, Any]] = []
        if hist_row and hist_row.value:
            try:
                parsed = json.loads(hist_row.value)
                if isinstance(parsed, list):
                    hist = [x for x in parsed if isinstance(x, dict)]
            except Exception:
                hist = []
        hist.insert(0, payload)
        hist = hist[:AUDIT_HISTORY_KEEP]
        hist_raw = json.dumps(hist)
        if hist_row is None:
            session.add(ScanState(key=AUDIT_HISTORY_KEY, value=hist_raw, updated_at=now))
        else:
            hist_row.value = hist_raw
            hist_row.updated_at = now
    session.flush()


async def _commit_audit_payload(
    payload: dict[str, Any],
    *,
    now: datetime,
    sit_out: int | None,
    sanity: dict[str, Any],
    statement_ms: int = FOMO_DB_STATEMENT_MS,
    history: bool = True,
) -> str:
    """Persist audit + loop note without ingest_lock (scan_state only)."""

    def _write() -> str:
        with _fomo_db_scope(statement_ms=statement_ms) as session:
            _save_audit(session, payload, now=now, history=history)
            return stamp_fomo_trending_heartbeat(
                session, sit_out=sit_out, skipped=False, sanity=sanity
            )

    return await asyncio.wait_for(asyncio.to_thread(_write), timeout=FOMO_AUDIT_WRITE_TIMEOUT_S)


async def _save_audit_and_heartbeat_async(
    payload: dict[str, Any],
    *,
    now: datetime,
    sit_out: int | None,
    sanity: dict[str, Any],
) -> tuple[str, bool]:
    """Returns (note, audit_row_persisted). One short retry on lock class."""
    last_exc: BaseException | None = None
    for attempt, ms in enumerate((FOMO_DB_STATEMENT_MS, FOMO_DB_RETRY_STATEMENT_MS)):
        try:
            note = await _commit_audit_payload(
                payload,
                now=now,
                sit_out=sit_out,
                sanity=sanity,
                statement_ms=ms,
                history=attempt == 0,
            )
            return note, True
        except Exception as exc:
            last_exc = exc
            if attempt == 0 and _fomo_db_defer(exc):
                log.info("fomo trending audit save defer retry (%s)", type(exc).__name__)
                continue
            break
    if last_exc is not None and _fomo_db_defer(last_exc):
        try:
            note = await _commit_audit_payload(
                payload,
                now=now,
                sit_out=sit_out,
                sanity=sanity,
                statement_ms=FOMO_DB_RETRY_STATEMENT_MS,
                history=False,
            )
            return note, True
        except Exception as exc2:
            log.info("fomo trending audit save partial fail (%s)", type(exc2).__name__)
            return fomo_heartbeat_note(sanity=sanity, error="lock defer"), False
    if last_exc is not None:
        raise last_exc
    return fomo_heartbeat_note(sanity=sanity, error="lock defer"), False


async def run_fomo_trending_audit(
    session: Session | None = None,
    *,
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Hourly sanity stamp. Paper/learn only — does not open fills."""
    now = now or utcnow()
    with _fomo_db_scope() as due_sess:
        due = force or _audit_due(due_sess, now=now)
        if not due:
            prev = load_fomo_trending_audit(due_sess) or {}
            sit_out = prev.get("sit_out") if isinstance(prev, dict) else None
            note = stamp_fomo_trending_heartbeat(
                due_sess,
                sit_out=sit_out if isinstance(sit_out, int) else None,
                skipped=True,
                sanity=(prev.get("sanity") if isinstance(prev, dict) else None),
            )
            return {"ok": True, "skipped": True, "last_audit": prev, "sit_out": sit_out, "note": note}
    try:
        coverage = await asyncio.wait_for(
            fomo_trending_coverage(session),
            timeout=FOMO_COVERAGE_TIMEOUT_S,
        )
    except TimeoutError:
        log.warning("fomo trending coverage timed out after %ss", FOMO_COVERAGE_TIMEOUT_S)
        raise
    except Exception as exc:
        if not _fomo_db_defer(exc):
            raise
        log.warning("fomo trending coverage lock defer (%s)", type(exc).__name__)
        with _fomo_db_scope(statement_ms=FOMO_DB_RETRY_STATEMENT_MS) as prev_sess:
            prev = load_fomo_trending_audit(prev_sess) or {}
        sanity_prev = (prev.get("sanity") if isinstance(prev, dict) else None) or {}
        sit_out = prev.get("sit_out") if isinstance(prev, dict) else None
        note = fomo_heartbeat_note(
            sit_out=sit_out if isinstance(sit_out, int) else None,
            sanity=sanity_prev,
            error="lock defer",
        )
        return {
            "ok": True,
            "skipped": True,
            "partial": True,
            "last_audit": prev,
            "sit_out": sit_out,
            "note": note,
        }
    sanity = coverage.get("sanity") or {}
    by_bucket = coverage.get("by_bucket") or {}
    items = coverage.get("items") or []
    sit_out = coverage.get("sit_out")
    try:
        sit_out = int(sit_out) if sit_out is not None else None
    except (TypeError, ValueError):
        sit_out = None
    payload = {
        "at": now.isoformat(),
        "source": coverage.get("source"),
        "board_stale": coverage.get("board_stale"),
        "capture_age_hours": coverage.get("capture_age_hours"),
        "captured_at": coverage.get("captured_at"),
        "api_source": coverage.get("api_source"),
        "mirror_note": coverage.get("mirror_note"),
        "sanity_board": coverage.get("sanity_board"),
        "graduated_board": coverage.get("graduated_board"),
        "gmgn_trending_board": coverage.get("gmgn_trending_board"),
        "dexscreener_trending_board": coverage.get("dexscreener_trending_board"),
        "secondary_overlap": coverage.get("secondary_overlap"),
        "secondary_miss_audit": coverage.get("secondary_miss_audit"),
        "secondary_sanity_sources": coverage.get("secondary_sanity_sources"),
        "sit_out": sit_out,
        "board": int((coverage.get("counts") or {}).get("board") or 0),
        "sanity": sanity,
        "by_bucket": by_bucket,
        "items": [
            {
                "rank": i.get("rank"),
                "symbol": i.get("symbol"),
                "mint": i.get("mint"),
                "chain": i.get("chain"),
                "bucket": i.get("bucket"),
                "status": i.get("status"),
                "gate_veto": i.get("gate_veto") or "",
                "entry_p": i.get("entry_p"),
                "multiple": i.get("multiple"),
                "last_mcap": i.get("last_mcap"),
                "mcap_usd": i.get("mcap_usd"),
                "on_hunt": bool(i.get("on_hunt")),
                "on_desk": bool(i.get("on_desk")),
                "why_not_hunt": i.get("why_not_hunt") or "",
            }
            for i in items[:40]
        ],
        "top": [
            {
                "rank": i.get("rank"),
                "symbol": i.get("symbol"),
                "mint": i.get("mint"),
                "chain": i.get("chain"),
                "bucket": i.get("bucket"),
                "gate_veto": i.get("gate_veto") or "",
                "entry_p": i.get("entry_p"),
                "multiple": i.get("multiple"),
            }
            for i in items[:12]
        ],
        "misses": [
            {"symbol": i.get("symbol"), "mint": i.get("mint"), "chain": i.get("chain")}
            for i in (coverage.get("misses") or [])[:8]
        ],
        "vetoed": [
            {
                "symbol": i.get("symbol"),
                "mint": i.get("mint"),
                "chain": i.get("chain"),
                "gate_veto": i.get("gate_veto") or "",
                "multiple": i.get("multiple"),
            }
            for i in (coverage.get("vetoed") or [])[:8]
        ],
        "fomo_trend_no_hunt": {
            "n": (coverage.get("fomo_trend_no_hunt") or {}).get("n"),
            "n_joined": (coverage.get("fomo_trend_no_hunt") or {}).get("n_joined"),
            "n_total": (coverage.get("fomo_trend_no_hunt") or {}).get("n_total"),
            "n_desk_no_hunt": (coverage.get("fomo_trend_no_hunt") or {}).get("n_desk_no_hunt"),
            "n_door_miss": (coverage.get("fomo_trend_no_hunt") or {}).get("n_door_miss"),
            "n_ran": (coverage.get("fomo_trend_no_hunt") or {}).get("n_ran"),
            "n_runners": (coverage.get("fomo_trend_no_hunt") or {}).get("n_runners"),
            "n_duds": (coverage.get("fomo_trend_no_hunt") or {}).get("n_duds"),
            "by_why": (coverage.get("fomo_trend_no_hunt") or {}).get("by_why") or {},
            "separators": (coverage.get("fomo_trend_no_hunt") or {}).get("separators") or [],
            "autopsies": (coverage.get("fomo_trend_no_hunt") or {}).get("autopsies") or [],
            "side_key": "fomo_trend_no_hunt",
            "open": False,
        },
        "note": "Hourly FOMO trending sanity check. Not a buy list.",
    }
    note, persisted = await _save_audit_and_heartbeat_async(
        payload, now=now, sit_out=sit_out, sanity=sanity if isinstance(sanity, dict) else {}
    )
    log.info(
        "fomo trending audit board=%s persisted=%s sit_out=%s seen_rate=%s miss=%s veto_hijack=%s",
        payload["board"],
        persisted,
        sit_out,
        (sanity or {}).get("seen_rate"),
        (sanity or {}).get("miss_n"),
        (sanity or {}).get("veto_hijack_n"),
    )
    out: dict[str, Any] = {
        "ok": True,
        "skipped": False,
        "partial": not persisted,
        "audit": payload,
        "sit_out": sit_out,
        "note": note,
    }
    if persisted:
        out["last_audit"] = payload
    return out
