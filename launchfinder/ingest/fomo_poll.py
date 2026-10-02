"""Ingest Sol + Robinhood mints from live FOMO activity.

Keyless GET /v2/alerts plus robinhoodtrenches radar/tokens (the
FOMO-wallet tape Rekt Fencer posted) plus the keyed Tokens→Trending
board (Sol and RH only). Dex hydrates pair age / liq. skip_gmgn.
No extra GMGN HTTP.
Fat FOMO books are allowed — that is the door — but undated or
this-window-aged-out prints are skipped so a week-old major is
not a new launch.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.exc import DataError, IntegrityError

from ..chains import normalize_chain, normalize_mint
from ..config import settings
from ..db import ingest_lock, session_scope
from ..models import ScanState, Token, utcnow
from ..research.dexscreener import token_market
from ..research.fomo_api import FomoSitOut, fetch_alerts, fetch_trending
from ..research.rht import fetch_rht_radar, fetch_rht_tokens
from .dex_poll import pair_to_coin
from .store import ingest_and_research

log = logging.getLogger("launchfinder.fomo_api")

POLL_KEY = "fomo_rht_poll_at"
TREND_KEY = "fomo_trending_board"
POLL_EVERY_S = float(settings.fomo_poll_seconds or 600.0)
MIN_LIQ_USD = 800.0
MAX_NEW_PER_CHAIN = 8

# Live FOMO app Tokens→Trending 2026-09-06. The API token board was a
# 2026-09-01 snapshot, so these never arrived via trending. Alerts do
# not always include every board row in one slice. Age/liq gates still
# drop them once they are old or thin.
FOMO_APP_SEEDS = (
    {
        "mint": "0xa3602804e096cb73bd8344afc1ff3f3390b899c5",
        "symbol": "PEZ",
        "name": "PEZ",
        "chain": "robinhood",
    },
    {
        "mint": "0x275bd09e2dd9ea3da99e9160277d8691e33f47fa",
        "symbol": "hoodrat",
        "name": "hoodrat",
        "chain": "robinhood",
    },
)


def _stamp(session, key: str) -> ScanState | None:
    return session.query(ScanState).filter(ScanState.key == key).one_or_none()


def _set_stamp(session, key: str, value: str, when: datetime) -> None:
    row = _stamp(session, key)
    if row is None:
        session.add(ScanState(key=key, value=value, updated_at=when))
    else:
        row.value = value
        row.updated_at = when


def _due(session) -> bool:
    now = utcnow()
    last = _stamp(session, POLL_KEY)
    prev = last.updated_at if last else None
    if prev is None:
        return True
    if prev.tzinfo is None:
        prev = prev.replace(tzinfo=timezone.utc)
    every = float(getattr(settings, "fomo_poll_seconds", None) or POLL_EVERY_S)
    return (now - prev).total_seconds() >= max(300.0, every)


def _merge_rows(chain: str, *groups: list[dict] | tuple[dict, ...]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for group in groups:
        for row in group:
            mint = normalize_mint(row.get("mint") or "", chain)
            if not mint or mint in seen:
                continue
            seen.add(mint)
            merged = dict(row)
            merged["mint"] = mint
            merged["chain"] = chain
            out.append(merged)
    return out


def save_trending_snapshot(session, rows: list[dict], now: datetime | None = None) -> None:
    now = now or utcnow()
    payload = {
        "at": now.isoformat(),
        "items": [
            {
                "mint": str(row.get("mint") or ""),
                "chain": normalize_chain(row.get("chain") or row.get("network") or "sol"),
                "symbol": str(row.get("symbol") or ""),
                "name": str(row.get("name") or ""),
                "mcap_usd": float(row.get("mcap_usd") or 0.0),
                "rank": row.get("rank"),
            }
            for row in rows
            if row.get("mint")
        ],
    }
    _set_stamp(session, TREND_KEY, json.dumps(payload), now)


def load_trending_snapshot(session) -> dict | None:
    row = _stamp(session, TREND_KEY)
    if row is None or not row.value:
        return None
    try:
        data = json.loads(row.value)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    data["updated_at"] = row.updated_at.isoformat() if row.updated_at else None
    return data


def max_age_hours(chain: str) -> float:
    from ..scoring.hunt import hunt_hours

    return hunt_hours(chain)


def market_to_coin(market: dict, chain: str) -> dict:
    """Dex print → ingest coin. skip_gmgn. FEATURE_NAMES stays 66."""
    chain = normalize_chain(chain)
    if chain == "robinhood":
        coin = pair_to_coin(market)
        coin["skip_gmgn"] = True
        coin["launchpad"] = coin.get("launchpad") or "fomo"
        return coin
    created = market.get("created_at")
    return {
        "mint": market.get("mint") or "",
        "name": market.get("name") or "",
        "symbol": market.get("symbol") or "",
        "description": "",
        "image_url": market.get("image_url") or "",
        "twitter": market.get("twitter") or "",
        "website": market.get("website") or "",
        "telegram": "",
        "creator": "",
        "complete": True,
        "nsfw": False,
        "banned": False,
        "reply_count": 0,
        "created_at": created,
        "updated_at": created,
        "mcap_usd": float(market.get("mcap_usd") or 0.0),
        "ath_mcap": 0.0,
        "pool_address": market.get("pair_address") or "",
        "chain": "sol",
        "launchpad": "fomo",
        "skip_gmgn": True,
        "raw": market,
    }


def source_for(chain: str) -> str:
    return "rh_fomo" if normalize_chain(chain) == "robinhood" else "sol_fomo"


async def _ingest_unknown(rows: list[dict], chain: str, *, known: set[str], cap: int) -> list[str]:
    ingested: list[str] = []
    age_limit = max_age_hours(chain)
    src = source_for(chain)
    for row in rows:
        if len(ingested) >= cap:
            break
        mint = row.get("mint") or ""
        if not mint or mint in known:
            continue
        try:
            market = await token_market(mint, chain=chain)
        except Exception:
            log.debug("FOMO dex hydrate failed %s", mint[:12], exc_info=True)
            continue
        if not market or not market.get("created_at"):
            continue
        created = market["created_at"]
        age_h = (utcnow() - created).total_seconds() / 3600.0
        if age_h < 0 or age_h > age_limit:
            continue
        if float(market.get("liquidity_usd") or 0.0) < MIN_LIQ_USD:
            continue
        market["mint"] = mint
        if not market.get("symbol"):
            market["symbol"] = row.get("symbol") or ""
        if not market.get("name"):
            market["name"] = row.get("name") or ""
        coin = market_to_coin(market, chain)
        try:
            async with ingest_lock:
                with session_scope() as session:
                    if session.query(Token.mint).filter(Token.mint == mint).first() is not None:
                        known.add(mint)
                        continue
                    token = await ingest_and_research(session, mint=mint, source=src, coin=coin)
                    if token:
                        ingested.append(mint)
                        known.add(mint)
                        log.info(
                            "new %s fomo launch %s %s liq=%.0f",
                            chain,
                            token.symbol or mint[:10],
                            mint,
                            float(market.get("liquidity_usd") or 0.0),
                        )
        except (IntegrityError, DataError) as exc:
            log.warning("fomo mint %s skipped: %s", mint[:12], exc.__class__.__name__)
    return ingested


async def poll_fomo_launches() -> list[str]:
    """Ingest unknown this-window Sol + RH mints from FOMO trending + RH tape."""
    async with ingest_lock:
        with session_scope() as session:
            if not _due(session):
                return []
            _set_stamp(session, POLL_KEY, utcnow().isoformat(), utcnow())

    alerts: list[dict] = []
    radar: list[dict] = []
    hot: list[dict] = []
    if settings.robinhood_enabled:
        try:
            alerts = await fetch_alerts(limit=100)
        except Exception:
            log.exception("FOMO alerts fetch failed")
            alerts = []
        try:
            radar = await fetch_rht_radar(minutes=180, limit=40)
        except Exception:
            log.exception("robinhoodtrenches radar fetch failed")
            radar = []
        try:
            hot = await fetch_rht_tokens(window="24h", limit=60)
        except Exception:
            log.exception("robinhoodtrenches tokens fetch failed")
            hot = []
    trend_fetch = None
    trending: list[dict] = []
    try:
        trend_fetch = await fetch_trending(limit=50)
        trending = list(trend_fetch.rows or [])
        if (trend_fetch.upstream or {}).get("board_stale"):
            log.warning(
                "FOMO trending API mirror stale (source=%s age_h=%s) — not refreshing board snapshot",
                (trend_fetch.upstream or {}).get("api_source"),
                (trend_fetch.upstream or {}).get("capture_age_hours"),
            )
    except FomoSitOut as exc:
        log.warning("FOMO trending sit-out %s", exc.status)
        trending = []
        try:
            from ..research.fomo_coverage import stamp_fomo_trending_heartbeat

            with session_scope() as session:
                stamp_fomo_trending_heartbeat(session, sit_out=exc.status)
        except Exception:
            log.exception("FOMO sit-out heartbeat failed")
    except Exception:
        log.exception("FOMO trending fetch failed")
        trending = []

    upstream_stale = bool((trend_fetch.upstream or {}).get("board_stale")) if trend_fetch else False
    if trending and not upstream_stale:
        async with ingest_lock:
            with session_scope() as session:
                save_trending_snapshot(session, trending, utcnow())

    rh_trend = [row for row in trending if normalize_chain(row.get("chain")) == "robinhood"]
    sol_trend = [row for row in trending if normalize_chain(row.get("chain")) == "sol"]
    rh_rows = _merge_rows("robinhood", radar, alerts, hot, FOMO_APP_SEEDS, rh_trend) if settings.robinhood_enabled else []
    sol_rows = _merge_rows("sol", sol_trend)
    rows_by_chain = (("robinhood", rh_rows), ("sol", sol_rows))
    mints = [row["mint"] for _, group in rows_by_chain for row in group]
    if not mints:
        return []

    async with ingest_lock:
        with session_scope() as session:
            known = {row.mint for row in session.query(Token.mint).filter(Token.mint.in_(mints)).all()}

    ingested: list[str] = []
    for chain, group in rows_by_chain:
        if not group:
            continue
        ingested.extend(await _ingest_unknown(group, chain, known=known, cap=MAX_NEW_PER_CHAIN))
    if ingested:
        log.info("FOMO tape ingested %s of %s board/tape rows", len(ingested), len(mints))
    return ingested
