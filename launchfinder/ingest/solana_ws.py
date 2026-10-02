from __future__ import annotations

import asyncio
import json
import logging
import re

import websockets
from sqlalchemy.exc import IntegrityError

from ..config import MIGRATE_PROGRAM, RAYDIUM_LAUNCHPAD_PROGRAM, settings
from ..db import ingest_lock, session_scope
from ..httputil import post_json
from ..research import pumpfun
from .sol_launch import is_pump_mint, is_raydium_launch_logs, pick_launch_mints
from .store import ingest_and_research

log = logging.getLogger("launchfinder.ws")
MINT_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


async def listen_migrations(stop: asyncio.Event) -> None:
    url = settings.ws_url
    if not url:
        log.info("no SOLANA_WS_URL / HELIUS_API_KEY — websocket listener disabled")
        return
    backoff = 2.0
    while not stop.is_set():
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=8_000_000) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "logsSubscribe",
                            "params": [{"mentions": [MIGRATE_PROGRAM]}, {"commitment": "processed"}],
                        }
                    )
                )
                await ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "logsSubscribe",
                            "params": [{"mentions": [RAYDIUM_LAUNCHPAD_PROGRAM]}, {"commitment": "processed"}],
                        }
                    )
                )
                log.info("subscribed to migrate + raydium launchpad logs")
                backoff = 2.0
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    except TimeoutError:
                        continue
                    msg = json.loads(raw)
                    result = (msg.get("params") or {}).get("result") or {}
                    value = result.get("value") or {}
                    sig = value.get("signature") or ""
                    logs = value.get("logs") or []
                    if not sig or value.get("err"):
                        continue
                    if RAYDIUM_LAUNCHPAD_PROGRAM in "\n".join(str(x) for x in logs) and not is_raydium_launch_logs(
                        [str(x) for x in logs]
                    ):
                        continue
                    mint = await _mint_from_sig(sig, logs)
                    if not mint:
                        continue
                    coin = await pumpfun.get_coin(mint)
                    if not coin or not coin.get("mint"):
                        coin = {
                            "mint": mint,
                            "complete": True,
                            "chain": "sol",
                            "launchpad": "ray_launchpad" if not is_pump_mint(mint) else "",
                        }
                    try:
                        async with ingest_lock:
                            with session_scope() as session:
                                await ingest_and_research(
                                    session,
                                    mint=mint,
                                    source="websocket",
                                    coin=coin,
                                    signature=sig,
                                )
                    except IntegrityError:
                        continue
                    log.info("ws migration %s", mint)
        except (websockets.exceptions.ConnectionClosed, websockets.exceptions.InvalidStatus) as exc:
            # Routine idle drop or Helius/Cloudflare HTTP 520 handshake
            # reject. The loop already backs off; do not traceback-spam
            # ERROR on every reconnect (live 09:37–09:40).
            log.warning("websocket connection dropped (%s), reconnecting in %.0fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 1.7)
        except Exception:
            log.exception("websocket listener dropped")
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 1.7)


async def _mint_from_sig(signature: str, logs: list[str]) -> str:
    tx = await post_json(
        settings.rpc_url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        },
    )
    result = (tx or {}).get("result") if isinstance(tx, dict) else None
    if isinstance(result, dict):
        message = ((result.get("transaction") or {}).get("message") or {})
        keys = message.get("accountKeys") or []
        for key in keys:
            pubkey = key.get("pubkey") if isinstance(key, dict) else str(key)
            if isinstance(pubkey, str) and pubkey.endswith("pump"):
                return pubkey
        meta = result.get("meta") or {}
        event = {"signature": signature, "meta": meta, "accountKeys": keys, "logs": logs}
        picked = pick_launch_mints(event, logs)
        if picked:
            return picked[0]
        for balance in meta.get("postTokenBalances") or []:
            mint = balance.get("mint")
            if mint and mint.endswith("pump"):
                return mint
    blob = "\n".join(logs)
    for cand in MINT_RE.findall(blob):
        if cand.endswith("pump"):
            return cand
    if is_raydium_launch_logs(logs):
        for cand in MINT_RE.findall(blob):
            if cand and not cand.endswith("pump"):
                from .sol_launch import is_skip_account

                if not is_skip_account(cand):
                    return cand
    return ""
