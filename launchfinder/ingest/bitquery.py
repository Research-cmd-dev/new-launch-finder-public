"""Robinhood Uni V4 pool-create stream via Bitquery GraphQL.

ROUTE-class books (native ETH V4) never appear on GMGN trenches or
Dex quote pages. Bitquery `EVM(network: robinhood) { Events }` on the
Uniswap v4 PoolManager `Initialize` is the pair-open tick.

wss://streaming.bitquery.io/graphql?token=…  (graphql-transport-ws)
Token from account.bitquery.io — BITQUERY_API_TOKEN. Never log it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import websockets
from sqlalchemy.exc import DataError, IntegrityError

from ..chains import normalize_chain, normalize_mint
from ..config import settings
from ..db import ingest_lock, session_scope
from ..httputil import client
from ..models import Token, utcnow
from ..research.dexscreener import RH_QUOTE_SET, token_market
from .dex_poll import BOOST_ETH_MID_HOURS, BOOST_ETH_MID_MCAP, BOOST_LATE_MCAP, BOOST_YOUNG_HOURS, pair_to_coin
from .store import ingest_and_research

log = logging.getLogger("launchfinder.bitquery")

CHAIN = "robinhood"
BITQUERY_WS = "wss://streaming.bitquery.io/graphql"
BITQUERY_HTTP = "https://streaming.bitquery.io/graphql"
# Shared Uni V4 singleton on Robinhood — not pools.trade-only.
# https://docs.bitquery.io/docs/blockchain/robinhood/pools-trade-api/
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
NATIVE_ETH = "0x0000000000000000000000000000000000000000"
QUOTE_SET = {addr.lower() for addr in RH_QUOTE_SET} | {NATIVE_ETH}
MAX_AGE_HOURS = 4.0
MIN_LIQ_USD = 800.0

INITIALIZE_SUB = """
subscription {
  EVM(network: robinhood) {
    Events(
      where: {
        LogHeader: { Address: { is: "%s" } }
        Log: { Signature: { Name: { is: "Initialize" } } }
      }
    ) {
      Block { Time Number }
      Transaction { Hash }
      Arguments {
        Name
        Value {
          ... on EVM_ABI_Address_Value_Arg { address }
          ... on EVM_ABI_Bytes_Value_Arg { hex }
        }
      }
    }
  }
}
""" % POOL_MANAGER


def bitquery_token() -> str:
    return (settings.bitquery_api_token or "").strip()


def bitquery_ws_url(token: str | None = None) -> str:
    raw = (token if token is not None else bitquery_token()).strip()
    if not raw:
        return ""
    return f"{BITQUERY_WS}?token={quote(raw, safe='')}"


# Time-correct DEX prints for Live-sample backfill. The WS listener is
# initialize-only; this query is the historical warehouse we actually have.
_SOL_TRADES = """
query SolTrades($mint: String!, $since: DateTime!, $till: DateTime!) {
  Solana {
    DEXTradeByTokens(
      limit: {count: 5000}
      where: {
        Trade: { Currency: { MintAddress: { is: $mint } } }
        Block: { Time: { since: $since, till: $till } }
      }
    ) {
      Block { Time }
      Trade { PriceInUSD AmountInUSD }
    }
  }
}
"""

_RH_TRADES = """
query RhTrades($mint: String!, $since: DateTime!, $till: DateTime!) {
  EVM(network: robinhood) {
    DEXTradeByTokens(
      limit: {count: 5000}
      where: {
        Trade: { Currency: { SmartContract: { is: $mint } } }
        Block: { Time: { since: $since, till: $till } }
      }
    ) {
      Block { Time }
      Trade { PriceInUSD AmountInUSD }
    }
  }
}
"""


def _iso(ts: datetime | None) -> str:
    if ts is None:
        ts = utcnow()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _trade_time(row: dict[str, Any]) -> datetime | None:
    block = row.get("Block") if isinstance(row.get("Block"), dict) else {}
    raw = block.get("Time") or row.get("time") or row.get("Time")
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _trade_px_vol(row: dict[str, Any]) -> tuple[float, float]:
    trade = row.get("Trade") if isinstance(row.get("Trade"), dict) else row
    try:
        px = float(trade.get("PriceInUSD") or trade.get("price") or 0.0)
    except (TypeError, ValueError):
        px = 0.0
    try:
        vol = float(trade.get("AmountInUSD") or trade.get("volume") or 0.0)
    except (TypeError, ValueError):
        vol = 0.0
    return px, vol


def trades_to_candles(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bucket Bitquery DEX trades into 1-minute OHLCV (USD volume). No holders."""
    buckets: dict[datetime, list[tuple[float, float]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = _trade_time(row)
        px, vol = _trade_px_vol(row)
        if ts is None or px <= 0:
            continue
        minute = ts.replace(second=0, microsecond=0)
        buckets.setdefault(minute, []).append((px, vol))
    out: list[dict[str, Any]] = []
    for minute, ticks in sorted(buckets.items()):
        prices = [p for p, _v in ticks if p > 0]
        if not prices:
            continue
        out.append(
            {
                "time": minute,
                "open": prices[0],
                "high": max(prices),
                "low": min(prices),
                "close": prices[-1],
                "volume": sum(v for _p, v in ticks),
            }
        )
    return out


async def bitquery_graphql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    token = bitquery_token()
    if not token or not query:
        return {}
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "X-API-KEY": token,
    }
    try:
        resp = await client().post(
            BITQUERY_HTTP,
            json={"query": query, "variables": variables or {}},
            headers=headers,
        )
        if resp.status_code >= 400:
            log.debug("bitquery graphql %s", resp.status_code)
            return {}
        body = resp.json()
    except Exception:
        log.debug("bitquery graphql failed", exc_info=True)
        return {}
    if not isinstance(body, dict):
        return {}
    data = body.get("data")
    return data if isinstance(data, dict) else {}


def _trade_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("Solana", "EVM"):
        block = payload.get(key)
        if not isinstance(block, dict):
            continue
        rows = block.get("DEXTradeByTokens") or block.get("DEXTrades") or []
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
    return []


async def historical_trade_candles(
    mint: str,
    *,
    chain: str,
    from_ts: datetime,
    to_ts: datetime | None = None,
) -> list[dict[str, Any]]:
    """Time-correct DEX trades → 1m candles on the *realtime* stream.

    Bitquery's product includes archive DEXTradeByTokens / OHLCV; our token
    is realtime-only. Streaming returns rows for ~last hours; older windows
    are empty. graphql.bitquery.io archive cubes 403 until we add archive.
    Prefer this for fresh opens; use Helius (Sol) / GMGN·Gecko (day tape)
    for year-old runners until the plan upgrades.
    """
    if not mint or not bitquery_token():
        return []
    until = to_ts or (from_ts + timedelta(minutes=60))
    chain = normalize_chain(chain)
    query = _SOL_TRADES if chain == "sol" else _RH_TRADES
    payload = await bitquery_graphql(
        query,
        {"mint": mint, "since": _iso(from_ts), "till": _iso(until)},
    )
    return trades_to_candles(_trade_rows(payload))


def _norm_addr(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if not raw.startswith("0x"):
        raw = "0x" + raw
    return normalize_mint(raw, CHAIN)


def argument_map(arguments: list[Any] | None) -> dict[str, str]:
    """Decoded Bitquery Arguments → lowercased name → address/hex."""
    out: dict[str, str] = {}
    for row in arguments or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("Name") or row.get("name") or "").strip().lower()
        if not name:
            continue
        val = row.get("Value") or row.get("value") or {}
        if not isinstance(val, dict):
            continue
        addr = val.get("address") or val.get("hex") or ""
        if addr:
            out[name] = str(addr)
    return out


def mint_from_initialize(arguments: list[Any] | None) -> tuple[str, str]:
    """Non-quote side of a new Uni V4 pool. Quote-quote pairs are skipped.

    Live ROUTE: currency0 native ETH, currency1 the token.
    """
    args = argument_map(arguments)
    c0 = _norm_addr(args.get("currency0") or args.get("currency_0"))
    c1 = _norm_addr(args.get("currency1") or args.get("currency_1"))
    pool = _norm_addr(args.get("id") or args.get("poolid") or args.get("pool_id"))
    if len(pool) != 66:  # 0x + 64 hex
        pool = ""
    sides = [a for a in (c0, c1) if a]
    tokens = [a for a in sides if a not in QUOTE_SET]
    if len(tokens) != 1:
        return "", pool
    return tokens[0], pool


def _parse_block_time(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def should_skip_late_book(market: dict[str, Any] | None, opened: datetime | None) -> bool:
    """Do not late-ingest a fat leftover as a new $40k launch (LEGS class)."""
    created = None
    if market:
        created = market.get("created_at")
    created = created or opened
    if created is None:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_h = (utcnow() - created).total_seconds() / 3600.0
    if age_h < 0 or age_h > MAX_AGE_HOURS:
        return True
    if not market:
        return False
    mcap = float(market.get("mcap_usd") or 0.0)
    if age_h > BOOST_YOUNG_HOURS and mcap >= BOOST_LATE_MCAP:
        quote = (market.get("quote_symbol") or "").upper()
        quote_mint = (market.get("quote_mint") or "").lower()
        native = quote == "ETH" and (not quote_mint or quote_mint == NATIVE_ETH)
        if not (native and mcap < BOOST_ETH_MID_MCAP and age_h < BOOST_ETH_MID_HOURS):
            return True
    liq = float(market.get("liquidity_usd") or 0.0)
    # Leftover graduation LP after the open, not a just-initialized pool.
    if age_h > 0.25 and 0 < liq < MIN_LIQ_USD:
        return True
    return False


def _events_from_payload(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return []
    evm = data.get("EVM") if isinstance(data.get("EVM"), dict) else {}
    rows = evm.get("Events")
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)]


async def _ingest_initialize(event: dict[str, Any]) -> str | None:
    mint, pool_id = mint_from_initialize(event.get("Arguments") or event.get("arguments"))
    if not mint:
        return None
    opened = _parse_block_time(((event.get("Block") or {}).get("Time")))
    tx = str(((event.get("Transaction") or {}).get("Hash") or ""))
    async with ingest_lock:
        with session_scope() as session:
            if session.query(Token.mint).filter(Token.mint == mint).first() is not None:
                return None
    market: dict[str, Any] | None = None
    try:
        market = await token_market(mint, chain=CHAIN)
    except Exception:
        log.debug("bitquery dex hydrate failed %s", mint[:12], exc_info=True)
        market = None
    if should_skip_late_book(market, opened):
        log.info("bitquery skip late/fat %s", mint[:12])
        return None
    if market and market.get("created_at"):
        coin = pair_to_coin(market)
    else:
        coin = {
            "mint": mint,
            "name": "",
            "symbol": "",
            "created_at": opened or utcnow(),
            "updated_at": opened or utcnow(),
            "mcap_usd": 0.0,
            "pool_address": pool_id,
            "chain": CHAIN,
            "launchpad": "uniswap_v4",
            "quote_symbol": "ETH",
            "skip_gmgn": True,
        }
    coin["skip_gmgn"] = True
    coin["launchpad"] = coin.get("launchpad") or "uniswap_v4"
    coin.setdefault("chain", CHAIN)
    try:
        async with ingest_lock:
            with session_scope() as session:
                if session.query(Token.mint).filter(Token.mint == mint).first() is not None:
                    return None
                token = await ingest_and_research(
                    session,
                    mint=mint,
                    source="rh_bitquery",
                    coin=coin,
                    signature=tx,
                    pool_address=pool_id or str(coin.get("pool_address") or ""),
                    migrated_at=opened,
                )
                if token:
                    log.info("bitquery v4 initialize %s %s", token.symbol or mint[:10], mint)
                    return mint
    except (IntegrityError, DataError) as exc:
        log.warning("bitquery mint %s skipped: %s", mint[:12], exc.__class__.__name__)
    return None


async def listen_rh_pools(stop: asyncio.Event) -> None:
    """Subscribe to Uni V4 Initialize on Robinhood. No-op without a token."""
    if not settings.robinhood_enabled:
        return
    token = bitquery_token()
    if not token:
        log.info("no BITQUERY_API_TOKEN — Robinhood Uni V4 stream disabled")
        return
    url = bitquery_ws_url(token)
    backoff = 2.0
    while not stop.is_set():
        try:
            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=20,
                max_size=8_000_000,
                subprotocols=["graphql-transport-ws", "graphql-ws"],
                additional_headers={"Content-Type": "application/json"},
            ) as ws:
                await ws.send(json.dumps({"type": "connection_init", "payload": {}}))
                ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
                if ack.get("type") not in {"connection_ack", "connection_keep_alive"}:
                    log.warning("bitquery handshake %s", ack.get("type") or ack)
                proto = ws.subprotocol or "graphql-transport-ws"
                if proto == "graphql-ws":
                    start = {"id": "1", "type": "start", "payload": {"query": INITIALIZE_SUB}}
                else:
                    start = {"id": "1", "type": "subscribe", "payload": {"query": INITIALIZE_SUB}}
                await ws.send(json.dumps(start))
                log.info("subscribed to Robinhood Uni V4 Initialize")
                backoff = 2.0
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=45)
                    except TimeoutError:
                        continue
                    msg = json.loads(raw)
                    kind = msg.get("type")
                    if kind in {"ping", "ka", "connection_keep_alive"}:
                        if kind == "ping":
                            await ws.send(json.dumps({"type": "pong", "payload": msg.get("payload")}))
                        continue
                    if kind in {"error", "connection_error"}:
                        log.warning("bitquery stream error %s", str(msg.get("payload") or "")[:160])
                        break
                    if kind not in {"next", "data"}:
                        continue
                    for event in _events_from_payload(msg.get("payload")):
                        try:
                            await _ingest_initialize(event)
                        except Exception:
                            log.exception("bitquery initialize ingest failed")
        except asyncio.CancelledError:
            raise
        except (websockets.exceptions.ConnectionClosed, websockets.exceptions.InvalidStatus) as exc:
            log.warning("bitquery socket dropped (%s), reconnecting in %.0fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 1.7)
        except Exception:
            log.exception("bitquery listener dropped")
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 1.7)
