"""Seed ZCAT as a historical analytics runner.

Anonymous Cat / StonkFun. Not a new Pump.fun launch. Parked
historical so a $159M week-old book never sits on the live hunt.
skip_gmgn. No extra GMGN HTTP.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import DataError, IntegrityError

from ..chains import normalize_mint
from ..db import ingest_lock, session_scope
from ..models import Outcome, Token
from ..research.dexscreener import token_market
from ..research.early_wallets import fetch_early_swaps, holder_owners, parse_early_buys, record_early_buys
from .store import ingest_and_research

log = logging.getLogger("launchfinder.zcat")

ZCAT_MINT = "HcRLc9VDgjLeK154xDawfb1dmVJ98DoSqcwTHGqiDeJR"
ZCAT_PAIR = "BTccxxTFi7a9xJTE1exKn38Jgie35s6gNeRxd8DM61Rc"
ZCAT_CREATOR = "5CEbueQnq1Ym2uSSx2xXds3jQAqT1BDnkA59RZobSPAG"
ZCAT_CREATED = datetime(2026, 8, 30, 23, 29, 55, tzinfo=timezone.utc)
CHAIN = "sol"


def park_zcat_reference(session) -> int:
    token = session.query(Token).filter(Token.mint == ZCAT_MINT).one_or_none()
    if token is None:
        return 0
    changed = 0
    if token.source != "backfill":
        token.source = "backfill"
        changed = 1
    if not token.is_historical:
        token.is_historical = True
        changed = 1
    if token.outcome is not None:
        # Do not keep a $69k graduation proxy on a week-old analytics book.
        if token.outcome.t0_mcap:
            token.outcome.t0_mcap = 0.0
            token.outcome.multiple = 0.0
            changed = 1
        token.outcome.used_for_train = True
    if changed:
        session.flush()
        log.info("parked ZCAT reference as historical backfill")
    return changed


async def ensure_zcat_reference() -> bool:
    mint = normalize_mint(ZCAT_MINT, CHAIN)
    async with ingest_lock:
        with session_scope() as session:
            row = session.query(Token).filter(Token.mint == mint).one_or_none()
            if row is not None and row.research is not None:
                park_zcat_reference(session)
                return False
    try:
        market = await token_market(mint, chain=CHAIN)
    except Exception:
        market = {}
    coin = {
        "mint": mint,
        "name": "Anonymous Cat",
        "symbol": "ZCAT",
        "creator": ZCAT_CREATOR,
        "description": "Anonymous Cat was launched on StonkFun.",
        "created_at": ZCAT_CREATED,
        "pool_address": ZCAT_PAIR,
        "launchpad": "stonkfun",
        "skip_gmgn": True,
        "chain": CHAIN,
        "mcap_usd": float((market or {}).get("mcap_usd") or 0.0),
        "liquidity_usd": float((market or {}).get("liquidity_usd") or 0.0),
    }
    try:
        async with ingest_lock:
            with session_scope() as session:
                token = await ingest_and_research(
                    session,
                    mint=mint,
                    source="backfill",
                    coin=coin,
                    pool_address=ZCAT_PAIR,
                    migrated_at=ZCAT_CREATED,
                    historical=True,
                )
                if token is None:
                    return False
                park_zcat_reference(session)
                if token.outcome is None:
                    mcap = float(coin.get("mcap_usd") or 0.0)
                    session.add(
                        Outcome(
                            token_id=token.id,
                            t0_mcap=0.0,
                            max_mcap=mcap,
                            last_mcap=mcap,
                            last_liq=float(coin.get("liquidity_usd") or 0.0),
                            multiple=0.0,
                            label=1,
                        )
                    )
                    session.flush()
                log.info("seeded ZCAT analytics reference %s", mint)
                return True
    except (IntegrityError, DataError):
        return False


async def refresh_zcat_early_wallets() -> int:
    """First-hour buys on the launch pool, then roll into EarlyWallet."""
    mint = normalize_mint(ZCAT_MINT, CHAIN)
    end = ZCAT_CREATED + timedelta(hours=1)
    try:
        txs = await fetch_early_swaps(ZCAT_PAIR, start=ZCAT_CREATED, end=end)
    except Exception:
        log.exception("ZCAT early swap fetch failed")
        txs = []
    mark = 0.0
    try:
        market = await token_market(mint, chain=CHAIN)
        # Entry is SOL per token. Use Dex native price so mark × is not USD/SOL.
        mark = float((market or {}).get("price_native") or 0.0)
    except Exception:
        mark = 0.0
    buys = parse_early_buys(
        txs,
        mint=mint,
        pool=ZCAT_PAIR,
        creator=ZCAT_CREATOR,
        launched=ZCAT_CREATED,
        mark_price=mark,
    )
    try:
        still = await holder_owners(mint)
    except Exception:
        still = set()
    async with ingest_lock:
        with session_scope() as session:
            token = session.query(Token).filter(Token.mint == mint).one_or_none()
            if token is None:
                return 0
            n = record_early_buys(session, token, buys, still_in=still)
            log.info("ZCAT early wallets recorded %s", n)
            return n
