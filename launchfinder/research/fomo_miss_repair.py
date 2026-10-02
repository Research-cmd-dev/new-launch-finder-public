"""Classify FOMO/secondary trending misses and paper-safe ingest repair."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..chains import normalize_chain, normalize_mint
from ..db import ingest_lock, session_scope
from ..ingest.store import ingest_and_research
from ..models import Token, utcnow
from ..research import pumpfun
from ..research.dexscreener import token_market, token_markets
from ..scoring.hunt import hunt_hours, sol_pair_in_hunt_window

log = logging.getLogger("launchfinder.fomo_miss_repair")

MISS_REPAIR_CAP = 5
INGEST_LAG_MAX_AGE_H = 0.75
REPAIRABLE_REASONS = frozenset({"never_ingested", "ingest_lag"})


def classify_miss_reason(
    session: Session,
    item: dict[str, Any],
    *,
    market: dict[str, Any] | None = None,
) -> str:
    """Door-miss taxonomy for Learn / auto-repair (not paperV1 opens)."""
    chain = normalize_chain(item.get("chain") or "sol")
    if chain not in ("sol", "robinhood"):
        return "wrong_chain"
    mint = normalize_mint(str(item.get("mint") or ""), chain)
    if not mint:
        return "unknown"

    status = str(item.get("status") or "")
    bucket = str(item.get("bucket") or "")
    token = session.query(Token).filter(Token.mint == mint).one_or_none()

    veto = str(item.get("gate_veto") or "").strip()
    if veto or bucket.startswith("veto_"):
        return "filtered_veto"
    if status in ("leftover", "thin") or bucket in ("leftover", "thin"):
        return "filtered_veto"

    if token is not None or item.get("on_desk"):
        since = utcnow() - timedelta(hours=hunt_hours(chain))
        if token is not None and not item.get("on_hunt") and not sol_pair_in_hunt_window(token, since=since):
            return "already_seen_off_hunt"
        if token is not None:
            return "already_seen_off_hunt"

    if status == "no_dex":
        return "unknown"

    if status == "miss" or bucket == "miss":
        created = (market or {}).get("created_at")
        if created is not None:
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_h = (utcnow() - created).total_seconds() / 3600.0
            if 0 <= age_h <= INGEST_LAG_MAX_AGE_H:
                return "ingest_lag"
        return "never_ingested"

    return "unknown"


async def _load_markets_for_misses(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_chain: dict[str, list[str]] = {"sol": [], "robinhood": []}
    for row in rows:
        chain = normalize_chain(row.get("chain") or "sol")
        mint = normalize_mint(str(row.get("mint") or ""), chain)
        if mint and chain in by_chain:
            by_chain[chain].append(mint)
    out: dict[str, dict[str, Any]] = {}
    for chain, mints in by_chain.items():
        if not mints:
            continue
        try:
            markets = await token_markets(mints, chain)
        except Exception:
            log.debug("miss repair market fetch failed %s", chain, exc_info=True)
            markets = {}
        for mint in mints:
            key = f"{chain}:{mint}"
            out[key] = markets.get(mint) or {}
    return out


async def repair_sol_miss_ingest(mint: str) -> dict[str, Any]:
    """Paper-safe ingest only — no paperV1, no GMGN spend."""
    mint = normalize_mint(mint, "sol")
    if not mint:
        return {"ok": False, "via": "bad_mint"}

    async with ingest_lock:
        with session_scope() as session:
            if session.query(Token.mint).filter(Token.mint == mint).first() is not None:
                return {"ok": True, "via": "already_on_desk"}

    coin: dict[str, Any] | None = None
    try:
        coin = await pumpfun.get_coin(mint)
    except Exception:
        log.debug("pump get_coin failed %s", mint, exc_info=True)

    if coin and coin.get("complete"):
        try:
            async with ingest_lock:
                with session_scope() as session:
                    if session.query(Token.mint).filter(Token.mint == mint).first() is not None:
                        return {"ok": True, "via": "already_on_desk"}
                    token = await ingest_and_research(
                        session,
                        mint=mint,
                        source="fomo_miss_repair",
                        coin=coin,
                    )
                    return {"ok": bool(token), "via": "pump_complete"}
        except Exception as exc:
            log.info("fomo_miss_repair pump ingest %s (%s)", mint, type(exc).__name__)
            return {"ok": False, "via": "pump_complete", "error": type(exc).__name__}

    try:
        market = await token_market(mint, chain="sol")
    except Exception:
        market = None
    if market and float(market.get("liquidity_usd") or 0.0) >= 5000.0:
        dex_coin = {
            "mint": mint,
            "symbol": market.get("symbol") or "",
            "chain": "sol",
            "mcap_usd": market.get("mcap_usd"),
            "name": market.get("name") or "",
        }
        try:
            async with ingest_lock:
                with session_scope() as session:
                    if session.query(Token.mint).filter(Token.mint == mint).first() is not None:
                        return {"ok": True, "via": "already_on_desk"}
                    token = await ingest_and_research(
                        session,
                        mint=mint,
                        source="fomo_miss_repair",
                        coin=dex_coin,
                    )
                    return {"ok": bool(token), "via": "dex_market"}
        except Exception as exc:
            log.info("fomo_miss_repair dex ingest %s (%s)", mint, type(exc).__name__)
            return {"ok": False, "via": "dex_market", "error": type(exc).__name__}

    return {"ok": False, "via": "no_repair_path"}


async def attempt_miss_repair(row: dict[str, Any]) -> dict[str, Any]:
    chain = normalize_chain(row.get("chain") or "sol")
    if chain != "sol":
        return {"ok": False, "via": "skip_non_sol"}
    return await repair_sol_miss_ingest(str(row.get("mint") or ""))


def _trending_miss_rows(trending_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in trending_items:
        if not _item_is_miss(item):
            continue
        out.append(
            {
                "mint": item.get("mint"),
                "chain": item.get("chain") or "sol",
                "symbol": item.get("symbol") or "",
                "rank": item.get("rank"),
                "mcap_usd": item.get("mcap_usd"),
                "sources": ["fomo_trending"],
                "miss_sources": ["fomo_trending"],
                "on_fomo_trending": True,
                "fomo_trending_miss": True,
                "high_confidence_miss": False,
                "bucket": item.get("bucket"),
                "status": item.get("status"),
            }
        )
    return out


def _item_is_miss(item: dict[str, Any]) -> bool:
    return str(item.get("status") or "") == "miss" or str(item.get("bucket") or "") == "miss"


async def enrich_miss_audit(
    audit: dict[str, Any],
    *,
    trending_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Classify misses, optional capped repair, summary fields on audit."""
    trending_misses = _trending_miss_rows(trending_items)
    audit = dict(audit)
    audit["trending_misses"] = trending_misses

    by_mint: dict[str, dict[str, Any]] = {}
    for row in (audit.get("misses") or []) + trending_misses:
        mint = str(row.get("mint") or "")
        if not mint:
            continue
        by_mint.setdefault(mint, row)

    rows = list(by_mint.values())
    markets = await _load_markets_for_misses(rows)

    with session_scope() as session:
        for row in rows:
            chain = normalize_chain(row.get("chain") or "sol")
            mint = normalize_mint(str(row.get("mint") or ""), chain)
            market = markets.get(f"{chain}:{mint}")
            row["miss_reason"] = classify_miss_reason(session, row, market=market)

    breakdown = Counter(str(r.get("miss_reason") or "unknown") for r in rows)
    audit["miss_reason_breakdown"] = dict(breakdown)

    repair_candidates = [
        r
        for r in audit.get("misses") or []
        if r.get("high_confidence_miss") and r.get("miss_reason") in REPAIRABLE_REASONS
    ]
    repair_candidates.sort(
        key=lambda r: (-len(r.get("miss_sources") or []), -(float(r.get("mcap_usd") or 0))),
    )

    repairs_attempted = 0
    repairs_ok = 0
    for row in repair_candidates[:MISS_REPAIR_CAP]:
        row["repair_attempted"] = True
        outcome = await attempt_miss_repair(row)
        row["repair_outcome"] = outcome
        repairs_attempted += 1
        if outcome.get("ok"):
            repairs_ok += 1
            with session_scope() as session:
                row["miss_reason_after"] = classify_miss_reason(
                    session,
                    {**row, "on_desk": True, "status": "caught"},
                )

    for tm in trending_misses:
        if tm.get("miss_reason") in REPAIRABLE_REASONS and tm.get("high_confidence_miss"):
            continue  # trending-only misses repair only when HC via secondary union

    audit["repairs_attempted"] = repairs_attempted
    audit["repairs_ok"] = repairs_ok
    audit["repair_cap"] = MISS_REPAIR_CAP
    audit["auto_repair_note"] = (
        "Paper-safe ingest only (pump complete / dex market). "
        "Does not open paperV1 or green fomo_mirror."
    )
    return audit

