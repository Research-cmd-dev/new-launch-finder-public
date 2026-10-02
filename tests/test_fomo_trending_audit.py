"""FOMO trending hourly sanity check — Learn only, not a buy list."""

from __future__ import annotations

import asyncio

from launchfinder.db import init_db, session_scope
from launchfinder.research.fomo_coverage import (
    AUDIT_KEY,
    _bucket,
    load_fomo_trending_audit,
    run_fomo_trending_audit,
)
from launchfinder.models import ScanState


def test_bucket_taxonomy_for_sanity():
    assert _bucket({"status": "miss"}) == "miss"
    assert _bucket({"status": "leftover"}) == "leftover"
    assert _bucket({"status": "caught", "gate_veto": "hijack", "paper": {}}) == "veto_hijack"
    assert _bucket({"status": "caught", "gate_veto": "copycat spam", "paper": {}}) == "veto_other"
    assert _bucket({"status": "caught", "gate_veto": "", "paper": {"paper_v1": "queued"}}) == "short_list"
    assert _bucket({"status": "caught", "gate_veto": "", "paper": {"gated90": "open"}}) == "wide_paper"
    assert _bucket({"status": "caught", "gate_veto": "", "on_hunt": True, "paper": {}}) == "on_hunt"


def test_audit_persists_when_board_empty(monkeypatch):
    import launchfinder.research.fomo_coverage as cov

    async def fake_coverage(session):
        return {
            "items": [],
            "counts": {"board": 0, "caught": 0, "miss": 0, "leftover": 0, "thin": 0, "no_dex": 0},
            "by_bucket": {},
            "misses": [],
            "vetoed": [],
            "sanity": {
                "actionable": 0,
                "seen_rate": None,
                "miss_n": 0,
                "veto_hijack_n": 0,
                "short_list_n": 0,
            },
            "source": "test",
        }

    monkeypatch.setattr(cov, "fomo_trending_coverage", fake_coverage)
    init_db()

    async def _run():
        with session_scope() as session:
            out = await run_fomo_trending_audit(session, force=True)
            assert out["ok"] and not out["skipped"]
            saved = load_fomo_trending_audit(session)
            assert saved and saved.get("source") == "test"
            assert session.query(ScanState).filter(ScanState.key == AUDIT_KEY).count() == 1
            again = await run_fomo_trending_audit(session, force=False)
            assert again.get("skipped") is True
            beat = session.query(ScanState).filter(ScanState.key == "loop:fomo_trending").one_or_none()
            assert beat is not None
            assert "sit-out" not in (beat.value or "")
            assert "audit fresh" in (beat.value or "") or "seen=" in (beat.value or "")

    asyncio.run(_run())


def test_sit_out_stamps_heartbeat_and_is_not_a_buy(monkeypatch):
    import launchfinder.research.fomo_coverage as cov
    from launchfinder.research.fomo_coverage import fomo_heartbeat_note
    from launchfinder.models import PaperFill

    assert "sit-out 402" in fomo_heartbeat_note(sit_out=402)
    assert "skipped" not in fomo_heartbeat_note(sit_out=402, skipped=True)

    async def fake_coverage(session):
        return {
            "items": [],
            "counts": {"board": 0, "caught": 0, "miss": 0, "leftover": 0, "thin": 0, "no_dex": 0},
            "by_bucket": {},
            "misses": [],
            "vetoed": [],
            "sanity": {
                "actionable": 0,
                "seen_rate": None,
                "miss_n": 0,
                "veto_hijack_n": 0,
                "short_list_n": 0,
            },
            "source": "sit-out",
            "sit_out": 402,
        }

    monkeypatch.setattr(cov, "fomo_trending_coverage", fake_coverage)
    init_db()

    async def _run():
        with session_scope() as session:
            out = await run_fomo_trending_audit(session, force=True)
            assert out["ok"] and not out["skipped"]
            assert out.get("sit_out") == 402
            assert "sit-out 402" in (out.get("note") or "")
            saved = load_fomo_trending_audit(session)
            assert saved and saved.get("sit_out") == 402
            beat = session.query(ScanState).filter(ScanState.key == "loop:fomo_trending").one()
            assert "sit-out 402" in (beat.value or "")
            assert session.query(PaperFill).count() == 0

    asyncio.run(_run())
