"""Persistent FOMO App /ws/alerts listener (paper-safe social layer)."""

from __future__ import annotations

import asyncio
import json
import logging

import websockets

from ..config import settings
from ..db import session_scope
from ..research.fomo_alerts import (
    fomo_alerts_heartbeat_note,
    fomo_alerts_ws_url,
    parse_ws_payload,
    persist_fomo_alert,
)

log = logging.getLogger("launchfinder.fomo_alerts_ws")

# Under production_gate fomo_alerts budget (600s): keep stamp fresh + heal zombie ws=up.
FOMO_ALERTS_HEARTBEAT_S = 60.0
FOMO_ALERTS_IDLE_RECONNECT_S = 150.0
FOMO_ALERTS_RECV_POLL_S = 15.0


async def listen_fomo_alerts(stop: asyncio.Event, beat) -> None:
    """Reconnecting WSS client. ``beat`` is sync callable(name, note=)."""
    backoff = 2.0
    while not stop.is_set():
        url = fomo_alerts_ws_url()
        if not url:
            beat("fomo_alerts", note=fomo_alerts_heartbeat_note(sit_out="no FOMO_API_KEY"))
            try:
                await asyncio.wait_for(stop.wait(), timeout=60.0)
            except TimeoutError:
                pass
            continue
        received = 0
        inserted = 0
        try:
            async with websockets.connect(
                url,
                ping_interval=25,
                ping_timeout=25,
                max_size=4_000_000,
            ) as ws:
                log.info("FOMO /ws/alerts connected")
                backoff = 2.0
                loop = asyncio.get_running_loop()
                beat("fomo_alerts", note=fomo_alerts_heartbeat_note(connected=True))
                last_beat = loop.time()
                last_recv = loop.time()
                while not stop.is_set():
                    now = loop.time()
                    idle_s = now - last_recv
                    if idle_s >= FOMO_ALERTS_IDLE_RECONNECT_S:
                        log.warning(
                            "FOMO alerts idle %.0fs (recv=%s), forcing reconnect",
                            idle_s,
                            received,
                        )
                        beat(
                            "fomo_alerts",
                            note=fomo_alerts_heartbeat_note(
                                connected=False,
                                received=received,
                                inserted=inserted,
                                error=f"idle {int(idle_s)}s",
                            ),
                        )
                        break
                    if now - last_beat >= FOMO_ALERTS_HEARTBEAT_S:
                        beat(
                            "fomo_alerts",
                            note=fomo_alerts_heartbeat_note(
                                connected=True,
                                received=received,
                                inserted=inserted,
                            ),
                        )
                        last_beat = now
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=FOMO_ALERTS_RECV_POLL_S)
                    except TimeoutError:
                        continue
                    last_recv = loop.time()
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    rows = parse_ws_payload(payload)
                    if not rows:
                        continue
                    received += len(rows)
                    new_n = await asyncio.to_thread(_persist_batch, rows)
                    inserted += new_n
                    now = loop.time()
                    beat(
                        "fomo_alerts",
                        note=fomo_alerts_heartbeat_note(
                            connected=True,
                            received=received,
                            inserted=inserted,
                        ),
                    )
                    last_beat = now
        except (websockets.exceptions.ConnectionClosed, websockets.exceptions.InvalidStatus) as exc:
            log.warning("FOMO alerts ws dropped (%s), reconnect in %.0fs", exc, backoff)
            beat(
                "fomo_alerts",
                note=fomo_alerts_heartbeat_note(
                    connected=False,
                    received=received,
                    inserted=inserted,
                    error=type(exc).__name__,
                ),
            )
            await asyncio.sleep(backoff)
            backoff = min(90.0, backoff * 1.7)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("FOMO alerts ws failed")
            beat("fomo_alerts", note=fomo_alerts_heartbeat_note(error=type(exc).__name__))
            await asyncio.sleep(backoff)
            backoff = min(90.0, backoff * 1.7)


def _persist_batch(rows: list[dict]) -> int:
    n = 0
    with session_scope() as session:
        for row in rows:
            if persist_fomo_alert(session, row):
                n += 1
    return n
