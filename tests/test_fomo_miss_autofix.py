"""FOMO / secondary miss classify + capped auto-repair (stack-v193)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from launchfinder.db import init_db, session_scope
from launchfinder.research.fomo_miss_repair import (
    MISS_REPAIR_CAP,
    classify_miss_reason,
    enrich_miss_audit,
)


def test_classify_never_ingested_when_status_miss_no_token():
    init_db()
    item = {
        "mint": "MintZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ",
        "chain": "sol",
        "status": "miss",
        "bucket": "miss",
        "on_desk": False,
    }
    with session_scope() as session:
        reason = classify_miss_reason(session, item, market={"liquidity_usd": 9000, "created_at": None})
    assert reason == "never_ingested"


def test_classify_ingest_lag_when_very_young():
    from datetime import timedelta

    from launchfinder.models import utcnow

    init_db()
    item = {"mint": "MintLagLagLagLagLagLagLagLagLagLagLagLagLagLag", "chain": "sol", "status": "miss", "bucket": "miss"}
    young = utcnow() - timedelta(minutes=20)
    with session_scope() as session:
        reason = classify_miss_reason(
            session,
            item,
            market={"liquidity_usd": 12000, "created_at": young},
        )
    assert reason == "ingest_lag"


def test_repair_cap_only_high_confidence_repairable(monkeypatch):
    audit = {
        "misses": [
            {
                "mint": f"m{i}",
                "chain": "sol",
                "status": "miss",
                "bucket": "miss",
                "high_confidence_miss": True,
                "miss_sources": ["graduated", "gmgn_trending"],
                "mcap_usd": 1000 * i,
            }
            for i in range(8)
        ],
        "union_miss": 8,
    }
    calls = 0

    async def fake_repair(row):
        nonlocal calls
        calls += 1
        return {"ok": True, "via": "test"}

    monkeypatch.setattr("launchfinder.research.fomo_miss_repair.attempt_miss_repair", fake_repair)
    monkeypatch.setattr(
        "launchfinder.research.fomo_miss_repair._load_markets_for_misses",
        AsyncMock(return_value={}),
    )

    async def _run():
        return await enrich_miss_audit(audit, trending_items=[])

    out = asyncio.run(_run())
    assert out["repairs_attempted"] == MISS_REPAIR_CAP
    assert calls == MISS_REPAIR_CAP
    assert sum(1 for r in out["misses"] if r.get("repair_attempted")) == MISS_REPAIR_CAP


def test_fomo_mirror_ignores_repairs():
    from launchfinder.models import ScanState
    from launchfinder.research.fomo_coverage import AUDIT_KEY
    from launchfinder.scoring.production_gate import _fomo_mirror_bar

    init_db()
    with session_scope() as session:
        row = session.query(ScanState).filter(ScanState.key == AUDIT_KEY).one_or_none()
        payload = '{"board_stale": false, "secondary_miss_audit": {"repairs_ok": 5, "repairs_attempted": 5}}'
        if row is None:
            session.add(ScanState(key=AUDIT_KEY, value=payload))
        else:
            row.value = payload
        session.commit()
        bar = _fomo_mirror_bar(session)
    assert bar["value"] == "fresh"
