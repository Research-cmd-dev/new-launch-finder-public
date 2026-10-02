"""Worker must not block ingest / hunt_tape on slow boot repairs."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from launchfinder.db import init_db, session_scope
from launchfinder.models import HuntCard, Outcome, Research, ScanState, Token, utcnow
from launchfinder.scoring.outcomes import REPAIR_HUNT_MINT_ALIGN_KEY, repair_hunt_mint_case
from launchfinder.worker import BOOT_REPAIR_BUDGET_SECONDS, _boot_repairs, loop


def test_repair_hunt_mint_case_sql_aligns_mismatched_mint():
    init_db()
    mint = "0x94641b97010608c3827fb058074889f19868ff33"
    with session_scope() as session:
        session.query(ScanState).filter(ScanState.key == REPAIR_HUNT_MINT_ALIGN_KEY).delete(
            synchronize_session=False
        )
        token = Token(
            mint=mint,
            symbol="VRAX",
            chain="robinhood",
            source="rh_bitquery",
            first_seen_at=utcnow(),
            migrated_at=utcnow(),
        )
        token.research = Research(p_good=0.5, features_json="{}")
        token.outcome = Outcome(t0_mcap=69_000.0, last_mcap=0.0)
        session.add(token)
        session.flush()
        session.add(
            HuntCard(
                chain="robinhood",
                mint=mint.upper(),
                token_id=token.id,
                first_seen_at=utcnow(),
            )
        )
        session.flush()
        repair_hunt_mint_case(session)
        card = session.query(HuntCard).filter(HuntCard.token_id == token.id).one()
        assert card.mint == mint


def test_boot_repair_budget_constant():
    assert BOOT_REPAIR_BUDGET_SECONDS >= 60.0


@pytest.mark.asyncio
async def test_worker_loop_up_before_slow_boot(monkeypatch):
    init_db()
    import launchfinder.worker as w

    monkeypatch.setattr(w, "init_db", lambda: None)
    monkeypatch.setattr(w, "restore_watch_boards", lambda: None)
    monkeypatch.setattr(w, "_seed_ledger", lambda: None)
    monkeypatch.setattr(w, "poll_new_migrations", lambda: asyncio.sleep(3600))
    monkeypatch.setattr(w, "_run_backfill", lambda: asyncio.sleep(3600))
    monkeypatch.setattr(w, "_run_rh_backfill", lambda: asyncio.sleep(3600))
    monkeypatch.setattr(w, "_run_zcat_analytics", lambda: asyncio.sleep(3600))
    monkeypatch.setattr(w, "listen_migrations", lambda _stop: asyncio.sleep(3600))
    monkeypatch.setattr(w, "listen_rh_pools", lambda _stop: asyncio.sleep(3600))
    monkeypatch.setattr(w, "_tape_refresh_loop", lambda: asyncio.sleep(3600))
    monkeypatch.setattr(w, "_hunt_tape_loop", lambda: asyncio.sleep(3600))
    monkeypatch.setattr(w, "_batch_fit_loop", lambda: asyncio.sleep(3600))
    monkeypatch.setattr(w, "_fomo_trending_audit_loop", lambda: asyncio.sleep(3600))

    async def slow_boot():
        await asyncio.sleep(30.0)

    monkeypatch.setattr(w, "_boot_maintenance", slow_boot)

    async def fake_fomo(_stop, _beat):
        await asyncio.sleep(3600)

    monkeypatch.setattr("launchfinder.ingest.fomo_alerts_ws.listen_fomo_alerts", fake_fomo)

    messages: list[str] = []
    real_info = w.log.info

    def capture_info(msg, *args, **kwargs):
        messages.append(msg % args if args else str(msg))
        real_info(msg, *args, **kwargs)

    monkeypatch.setattr(w.log, "info", capture_info)
    task = asyncio.create_task(loop())
    up = False
    for _ in range(40):
        await asyncio.sleep(0.05)
        if any("worker ingest loop up" in m for m in messages):
            up = True
            break
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert up, "worker ingest loop up must log before slow boot maintenance finishes"
