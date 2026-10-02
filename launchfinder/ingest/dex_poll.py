"""DexScreener discovery for RH books not on pons, and Sol PEPE AMM.

BONER (Dex pair 0x9c89b043… vs HIMS) ran to a real book on Long.xyz /
Uniswap V4. PONS factory logs never see that venue. Poll quote-token
pair lists (HIMS, WETH, USDG) for fresh launches.

WOJAK (DUe1qhee…redsRC) was Raydium CPMM vs PEPE — mint does not end
in pump, so Pump-family Sol doors never saw the migrate. Poll the PEPE
quote page plus Dex profiles/boosts. skip_gmgn. No extra GMGN.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import DataError, IntegrityError

from ..chains import normalize_mint
from ..config import settings
from ..models import Token
from ..research.dexscreener import (
    RH_QUOTE_TOKENS,
    SOL_QUOTE_TOKENS,
    boosted_rh_markets,
    boosted_sol_markets,
    quoted_pairs,
)
from .sol_launch import is_pump_mint
from .store import ingest_and_research

log = logging.getLogger("launchfinder.dex")
CHAIN = "robinhood"
MAX_AGE_HOURS = 24.0
MIN_LIQ_USD = 800.0
# Live 01:37: COST+stock quotes widen the net. Newest-first + 8
# starved HOTDOG $180k / 5.5h behind newer thin HIMS dust. Fat
# unknown books first; 16 chairs so the first cycles catch up.
MAX_NEW = 16
# Boosted ETH V4 books (LEGS) are invisible on quote lists. Do not
# ingest a 5h-old $2M print as a new $40k launch.
BOOST_LATE_MCAP = 150_000.0
BOOST_YOUNG_HOURS = 2.0
# Live 03:49: LDX 0x62cd…0a5b / native ETH / $315k / 12h sat on
# token-boosts/latest with no Token row. BOOST_LATE_MCAP dropped it.
# Native-ETH mid books under $400k / <18h pass. LEGS $2.1M stays
# out. SEND (WETH / 248h) and GI (GLD / $1.2M) stay out.
# Do not recap LEGS late-ingest. Do not raise BOOST_MARKET_CAP.
BOOST_ETH_MID_MCAP = 400_000.0
BOOST_ETH_MID_HOURS = 18.0
NATIVE_ETH_MINT = "0x0000000000000000000000000000000000000000"
# Live 04:10: PRESS 0x1a74…a81e hit the COIN quote page as a 3h
# $1.2k satellite, then _best_pair stamped the Sep-8 SPY book as
# t0=$993k. Boost-path late skip stays mcap-only so LDX-class
# native ETH mid still ingest. Quote pages also skip leftover
# fat books on skinny satellites. HOTDOG $180k liq still passes.
BOOST_QUOTE_PAGE_MIN_LIQ = 8_000.0
# WOJAK-class Sol Dex door. PEPE quote page + profiles/boosts.
# Age follows Hunt Sol (18h). Fillable liq so first-sight can freeze.
# After 2h a ≥$150k print is leftover — do not stamp it as t0.
# Do not leftover-sort WOJAK. Do not scrape every Sol pair.
# Do not raise BOOST_MARKET_CAP.
SOL_DEX_MIN_LIQ_USD = 5_000.0
SOL_DEX_MAX_NEW = 8
# Live 04:19: GL1TTR EcSgxs…TVGR / $10M / 0.7h / 8 holders hit
# token-profiles as a "young" book. Fast WOJAK-class runners stay
# under this. Do not leftover-sort the $10M print.
SOL_DEX_LEFTOVER_MCAP = 1_500_000.0
SOL_CURVE_DEX = frozenset({"pumpfun", "pump.fun"})


def _native_eth_quote(row: dict[str, Any]) -> bool:
    quote = (row.get("quote_symbol") or "").upper()
    quote_mint = (row.get("quote_mint") or "").lower()
    if quote != "ETH":
        return False
    return (not quote_mint) or quote_mint == NATIVE_ETH_MINT


def _native_eth_mid_boost(row: dict[str, Any], age_h: float) -> bool:
    mcap = float(row.get("mcap_usd") or 0.0)
    return (
        _native_eth_quote(row)
        and mcap < BOOST_ETH_MID_MCAP
        and age_h < BOOST_ETH_MID_HOURS
    )


def _late_quote_page_satellite(row: dict[str, Any], age_h: float) -> bool:
    """Fat leftover book on a skinny quote-page satellite (PRESS vs COIN)."""
    if age_h <= BOOST_YOUNG_HOURS:
        return False
    if _native_eth_mid_boost(row, age_h):
        return False
    mcap = float(row.get("mcap_usd") or 0.0)
    liq = float(row.get("liquidity_usd") or 0.0)
    if mcap < BOOST_LATE_MCAP:
        return False
    return liq < BOOST_QUOTE_PAGE_MIN_LIQ


# Official DexScreener pair the user sent as a reference runner.
# Seeded historical / backfill so paper and the young RH model do not
# treat a $13M→$67M continuation as a $40k new-launch 5x.
BONER_MINT = "0x98096d17e191b3da1d5f99a6d7b3584351b11e18"
BONER_PAIR = "0x9c89b04303dfa76f3f6fb02c2b77be0e8a00ab8fa00d507119acd54ab3e8640d"
BONER_CREATED = datetime(2026, 8, 20, 20, 59, 46, tzinfo=timezone.utc)


def _sol_curve_book(row: dict[str, Any]) -> bool:
    return str(row.get("dex_id") or "").strip().lower() in SOL_CURVE_DEX


def _sol_late_leftover(row: dict[str, Any], age_h: float) -> bool:
    """Fat leftover Sol book (WOJAK $1.5M / 13h, GL1TTR $10M / 0.7h)."""
    mcap = float(row.get("mcap_usd") or 0.0)
    if mcap >= SOL_DEX_LEFTOVER_MCAP:
        return True
    if age_h <= BOOST_YOUNG_HOURS:
        return False
    return mcap >= BOOST_LATE_MCAP


def sol_max_age_hours() -> float:
    from ..scoring.hunt import hunt_hours

    return hunt_hours("sol")


def pair_to_coin(row: dict[str, Any]) -> dict[str, Any]:
    website = row.get("website") or ""
    labels = row.get("dex_labels") or []
    quote = (row.get("quote_symbol") or "").upper()
    launchpad = "long" if "long.xyz" in website.lower() else ("uniswap_v4" if "v4" in [str(x).lower() for x in labels] else "dex")
    created = row.get("created_at")
    return {
        "mint": row.get("mint") or "",
        "name": row.get("name") or "",
        "symbol": row.get("symbol") or "",
        "description": "",
        "image_url": "",
        "twitter": row.get("twitter") or "",
        "website": website,
        "telegram": row.get("telegram") or "",
        "creator": "",
        "complete": True,
        "nsfw": False,
        "banned": False,
        "reply_count": 0,
        "created_at": created,
        "updated_at": created,
        "mcap_usd": float(row.get("mcap_usd") or 0.0),
        "ath_mcap": 0.0,
        "pool_address": row.get("pair_address") or "",
        "chain": CHAIN,
        "launchpad": launchpad,
        "quote_symbol": quote,
        "skip_gmgn": True,
        "raw": row,
    }


def sol_pair_to_coin(row: dict[str, Any]) -> dict[str, Any]:
    """Dex print → Sol ingest coin. skip_gmgn. FEATURE_NAMES stays 66."""
    created = row.get("created_at")
    quote = (row.get("quote_symbol") or "").upper()
    return {
        "mint": row.get("mint") or "",
        "name": row.get("name") or "",
        "symbol": row.get("symbol") or "",
        "description": "",
        "image_url": row.get("image_url") or "",
        "twitter": row.get("twitter") or "",
        "website": row.get("website") or "",
        "telegram": row.get("telegram") or "",
        "creator": "",
        "complete": True,
        "nsfw": False,
        "banned": False,
        "reply_count": 0,
        "created_at": created,
        "updated_at": created,
        "mcap_usd": float(row.get("mcap_usd") or 0.0),
        "ath_mcap": 0.0,
        "pool_address": row.get("pair_address") or "",
        "chain": "sol",
        "launchpad": "dex",
        "quote_symbol": quote,
        "skip_gmgn": True,
        "raw": row,
    }


def sol_dex_row_ok(row: dict[str, Any], age_h: float) -> bool:
    """Young fillable non-pump Sol AMM. Skip leftover WOJAK-class fat."""
    mint = row.get("mint") or ""
    if not mint or is_pump_mint(mint):
        return False
    if age_h < 0:
        return False
    if _sol_curve_book(row):
        return False
    if float(row.get("liquidity_usd") or 0.0) < SOL_DEX_MIN_LIQ_USD:
        return False
    if _sol_late_leftover(row, age_h):
        return False
    return True


async def discover_fresh_quoted(*, max_age_hours: float = MAX_AGE_HOURS) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _name, quote in RH_QUOTE_TOKENS:
        try:
            rows = await quoted_pairs(quote, chain=CHAIN)
        except Exception:
            log.debug("dex quoted_pairs failed for %s", quote, exc_info=True)
            continue
        for row in rows:
            mint = normalize_mint(row.get("mint") or "", CHAIN)
            if not mint or mint in seen:
                continue
            created = row.get("created_at")
            if not created:
                continue
            age_h = (now - created).total_seconds() / 3600.0
            if age_h < 0 or age_h > max_age_hours:
                continue
            if float(row.get("liquidity_usd") or 0.0) < MIN_LIQ_USD:
                continue
            if _late_quote_page_satellite(row, age_h):
                continue
            row["mint"] = mint
            seen.add(mint)
            found.append(row)
    try:
        boosted = await boosted_rh_markets()
    except Exception:
        log.debug("dex boosted markets failed", exc_info=True)
        boosted = []
    for row in boosted:
        mint = normalize_mint(row.get("mint") or "", CHAIN)
        if not mint or mint in seen:
            continue
        created = row.get("created_at")
        if not created:
            continue
        age_h = (now - created).total_seconds() / 3600.0
        if age_h < 0 or age_h > max_age_hours:
            continue
        if (
            age_h > BOOST_YOUNG_HOURS
            and float(row.get("mcap_usd") or 0.0) >= BOOST_LATE_MCAP
            and not _native_eth_mid_boost(row, age_h)
        ):
            continue
        if float(row.get("liquidity_usd") or 0.0) < MIN_LIQ_USD:
            continue
        row["mint"] = mint
        seen.add(mint)
        found.append(row)
    found.sort(
        key=lambda r: (
            float(r.get("liquidity_usd") or 0.0),
            r.get("created_at") or now,
        ),
        reverse=True,
    )
    return found


async def poll_rh_dex() -> list[str]:
    """Ingest unknown fresh RH Dex pairs (Long / V4 / HIMS books)."""
    from ..db import ingest_lock, session_scope

    if not settings.robinhood_enabled:
        return []
    try:
        await ensure_boner_reference()
    except Exception:
        log.debug("BONER reference seed skipped", exc_info=True)
    try:
        rows = await discover_fresh_quoted()
    except Exception:
        log.exception("Robinhood Dex discovery failed")
        return []
    if not rows:
        return []

    mints = [r["mint"] for r in rows]
    async with ingest_lock:
        with session_scope() as session:
            known = {row.mint for row in session.query(Token.mint).filter(Token.mint.in_(mints)).all()}

    ingested: list[str] = []
    for row in rows:
        mint = row["mint"]
        if mint in known:
            continue
        if len(ingested) >= MAX_NEW:
            break
        coin = pair_to_coin(row)
        try:
            async with ingest_lock:
                with session_scope() as session:
                    if session.query(Token.mint).filter(Token.mint == mint).first() is not None:
                        known.add(mint)
                        continue
                    token = await ingest_and_research(session, mint=mint, source="rh_dex", coin=coin)
                    if token:
                        ingested.append(mint)
                        known.add(mint)
                        log.info(
                            "new rh dex launch %s %s quote=%s liq=%.0f",
                            token.symbol or mint[:10],
                            mint,
                            coin.get("quote_symbol") or "",
                            float(row.get("liquidity_usd") or 0.0),
                        )
        except (IntegrityError, DataError) as exc:
            log.warning("rh dex mint %s skipped: %s", mint, exc.__class__.__name__)
    if ingested:
        log.info("robinhood dex discovery ingested %s of %s fresh pairs", len(ingested), len(rows))
    return ingested


async def ensure_boner_reference() -> bool:
    """Keep the user-supplied BONER pair resolvable. Historical, not trained."""
    from ..db import ingest_lock, session_scope

    if not settings.robinhood_enabled:
        return False
    async with ingest_lock:
        with session_scope() as session:
            if session.query(Token.mint).filter(Token.mint == BONER_MINT).first() is not None:
                return False
    coin = pair_to_coin(
        {
            "mint": BONER_MINT,
            "name": "Boner Coin",
            "symbol": "BONER",
            "quote_symbol": "HIMS",
            "pair_address": BONER_PAIR,
            "website": "https://app.long.xyz/tokens/0x98096d17e191b3da1d5f99a6d7b3584351b11e18",
            "twitter": "https://x.com/bonercoinlong",
            "created_at": BONER_CREATED,
            "dex_labels": ["v4"],
            "liquidity_usd": 0.0,
            "mcap_usd": 0.0,
        }
    )
    try:
        async with ingest_lock:
            with session_scope() as session:
                if session.query(Token.mint).filter(Token.mint == BONER_MINT).first() is not None:
                    return False
                await ingest_and_research(
                    session, mint=BONER_MINT, source="backfill", coin=coin, historical=True
                )
                log.info("seeded BONER reference %s", BONER_MINT)
                return True
    except (IntegrityError, DataError):
        return False


async def discover_fresh_sol_quoted(*, max_age_hours: float | None = None) -> list[dict[str, Any]]:
    """PEPE-quoted + profiled/boosted non-pump Sol books (WOJAK-class).

    Quote page is a 30-pair popularity list — a brand-new $20k book can
    sit off it (ROUTE-class). Profiles/boosts cover paid Dex visibility
    the same way LEGS/CLAWDHOOD did on RH. Age ≤ Hunt Sol 18h. After 2h
    a ≥$150k print is leftover (WOJAK $1.5M / 13h stays out). Pump mints
    and pump.fun curves stay on the Pump doors. Do not leftover-sort.
    Do not scrape every Sol pair. Do not raise BOOST_MARKET_CAP.
    """
    now = datetime.now(timezone.utc)
    age_limit = float(max_age_hours if max_age_hours is not None else sol_max_age_hours())
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _name, quote in SOL_QUOTE_TOKENS:
        try:
            rows = await quoted_pairs(quote, chain="sol")
        except Exception:
            log.debug("sol dex quoted_pairs failed for %s", quote, exc_info=True)
            continue
        for row in rows:
            mint = normalize_mint(row.get("mint") or "", "sol")
            if not mint or mint in seen:
                continue
            created = row.get("created_at")
            if not created:
                continue
            age_h = (now - created).total_seconds() / 3600.0
            if age_h > age_limit:
                continue
            if not sol_dex_row_ok(row, age_h):
                continue
            row["mint"] = mint
            seen.add(mint)
            found.append(row)
    try:
        boosted = await boosted_sol_markets()
    except Exception:
        log.debug("sol dex boosted markets failed", exc_info=True)
        boosted = []
    for row in boosted:
        mint = normalize_mint(row.get("mint") or "", "sol")
        if not mint or mint in seen:
            continue
        created = row.get("created_at")
        if not created:
            continue
        age_h = (now - created).total_seconds() / 3600.0
        if age_h > age_limit:
            continue
        if not sol_dex_row_ok(row, age_h):
            continue
        row["mint"] = mint
        seen.add(mint)
        found.append(row)
    found.sort(
        key=lambda r: (
            float(r.get("liquidity_usd") or 0.0),
            r.get("created_at") or now,
        ),
        reverse=True,
    )
    return found


async def poll_sol_dex() -> list[str]:
    """Ingest unknown this-window PEPE-quoted / profiled Sol AMM books."""
    from ..db import ingest_lock, session_scope

    try:
        rows = await discover_fresh_sol_quoted()
    except Exception:
        log.exception("Sol Dex discovery failed")
        return []
    if not rows:
        return []

    mints = [r["mint"] for r in rows]
    async with ingest_lock:
        with session_scope() as session:
            known = {row.mint for row in session.query(Token.mint).filter(Token.mint.in_(mints)).all()}

    ingested: list[str] = []
    for row in rows:
        mint = row["mint"]
        if mint in known:
            continue
        if len(ingested) >= SOL_DEX_MAX_NEW:
            break
        coin = sol_pair_to_coin(row)
        try:
            async with ingest_lock:
                with session_scope() as session:
                    if session.query(Token.mint).filter(Token.mint == mint).first() is not None:
                        known.add(mint)
                        continue
                    token = await ingest_and_research(session, mint=mint, source="sol_dex", coin=coin)
                    if token:
                        ingested.append(mint)
                        known.add(mint)
                        log.info(
                            "new sol dex launch %s %s quote=%s liq=%.0f",
                            token.symbol or mint[:10],
                            mint,
                            coin.get("quote_symbol") or "",
                            float(row.get("liquidity_usd") or 0.0),
                        )
        except (IntegrityError, DataError) as exc:
            log.warning("sol dex mint %s skipped: %s", mint, exc.__class__.__name__)
    if ingested:
        log.info("sol dex discovery ingested %s of %s fresh pairs", len(ingested), len(rows))
    return ingested
