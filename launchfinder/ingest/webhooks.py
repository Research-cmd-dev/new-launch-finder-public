from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.exc import IntegrityError

from ..db import ingest_lock, session_scope
from ..research import pumpfun
from .sol_launch import is_pump_mint, pick_launch_mints
from .store import ingest_and_research

log = logging.getLogger("launchfinder.webhook")


def _mints_from_payload(payload: Any) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    events = payload if isinstance(payload, list) else [payload]
    for event in events:
        if not isinstance(event, dict):
            continue
        sig = event.get("signature") or event.get("transactionSignature") or ""
        logs = event.get("logs") or event.get("logMessages") or []
        if not isinstance(logs, list):
            logs = []
        for mint in pick_launch_mints(event, [str(x) for x in logs]):
            found.append((mint, sig))
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for mint, sig in found:
        if mint in seen:
            continue
        seen.add(mint)
        out.append((mint, sig))
    return out


async def handle_webhook(payload: Any, source: str = "webhook") -> list[str]:
    ingested: list[str] = []
    for mint, sig in _mints_from_payload(payload):
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
                    token = await ingest_and_research(session, mint=mint, source=source, coin=coin, signature=sig)
                    if token:
                        ingested.append(mint)
                        log.info("webhook migration %s", mint)
        except IntegrityError:
            continue
    return ingested
