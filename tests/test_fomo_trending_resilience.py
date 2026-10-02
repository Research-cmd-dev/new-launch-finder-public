"""fomo_trending audit: no idle txn across HTTP; save retry on lock class."""

from __future__ import annotations

import asyncio
import time

from sqlalchemy.exc import OperationalError

from launchfinder.db import init_db, session_scope
from launchfinder.research.fomo_coverage import (
    fomo_error_note,
    fomo_heartbeat_note,
    run_fomo_trending_audit,
)
from launchfinder.models import ScanState


def test_fomo_trending_chair_matches_poll_seconds(monkeypatch):
    import launchfinder.research.fomo_coverage as cov

    class _Cfg:
        fomo_poll_seconds = 600.0

    cfg = _Cfg()
    monkeypatch.setattr("launchfinder.config.settings", cfg)
    assert cov.fomo_trending_chair_seconds() == 600.0
    cfg.fomo_poll_seconds = 120.0
    assert cov.fomo_trending_chair_seconds() == 300.0


def test_fomo_error_note_includes_message():
    exc = OperationalError("stmt", {}, Exception("lock timeout"))
    assert "OperationalError" in fomo_error_note(exc)
    assert "lock" in fomo_error_note(exc).lower()
    assert fomo_heartbeat_note(error="lock timeout").startswith("defer ")


def test_resolve_trending_board_fetch_timeout_falls_back_to_snapshot(monkeypatch):
    import launchfinder.research.fomo_coverage as cov

    from launchfinder.research.fomo_api import TrendingFetchResult

    async def slow_fetch(**_kw):
        await asyncio.sleep(10)
        return TrendingFetchResult(rows=[], upstream={"board_stale": False})

    monkeypatch.setattr(cov, "FOMO_HTTP_TIMEOUT_S", 0.05)
    monkeypatch.setattr(cov, "fetch_trending", slow_fetch)

    snap = {"at": "2026-01-01T00:00:00+00:00", "items": [{"mint": "abc", "chain": "sol"}]}

    async def _run():
        rows, meta, persist = await cov._resolve_trending_board(snap)
        assert len(rows) == 1
        assert meta.get("fetch_timeout") is True
        assert persist is None

    asyncio.run(_run())


def test_run_audit_coverage_timeout_propagates(monkeypatch):
    import launchfinder.research.fomo_coverage as cov

    async def slow_coverage(_session=None):
        await asyncio.sleep(10)
        return {}

    monkeypatch.setattr(cov, "FOMO_COVERAGE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(cov, "fomo_trending_coverage", slow_coverage)
    init_db()

    async def _run():
        try:
            await cov.run_fomo_trending_audit(force=True)
            assert False, "expected timeout"
        except TimeoutError:
            pass

    t0 = time.monotonic()
    asyncio.run(_run())
    assert time.monotonic() - t0 < 3.0


def test_save_audit_lock_defer_returns_partial_without_raise(monkeypatch):
    import launchfinder.research.fomo_coverage as cov

    calls = {"n": 0}

    async def always_lock_fail(*_a, **_k):
        calls["n"] += 1
        raise OperationalError("stmt", {}, Exception("lock timeout"))

    monkeypatch.setattr(cov, "_commit_audit_payload", always_lock_fail)
    init_db()

    async def _run():
        payload = {
            "at": "2026-09-30T18:00:00+00:00",
            "board": 1,
            "sanity": {"seen_rate": 1.0, "miss_n": 0, "veto_hijack_n": 0},
            "by_bucket": {},
            "items": [],
        }
        note, persisted = await cov._save_audit_and_heartbeat_async(
            payload,
            now=cov.utcnow(),
            sit_out=None,
            sanity=payload["sanity"],
        )
        assert not persisted
        assert calls["n"] >= 2
        assert note.startswith("defer ")

    asyncio.run(_run())


def test_audit_save_retries_once_on_operational_error(monkeypatch):
    import launchfinder.research.fomo_coverage as cov

    calls = {"n": 0}

    async def fake_coverage(_session=None):
        return {
            "items": [],
            "counts": {"board": 0},
            "by_bucket": {},
            "misses": [],
            "vetoed": [],
            "sanity": {"actionable": 0, "seen_rate": None, "miss_n": 0, "veto_hijack_n": 0},
            "source": "test",
        }

    def flaky_save(session, payload, *, now, history=True):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("stmt", {}, Exception("lock timeout"))

    monkeypatch.setattr(cov, "fomo_trending_coverage", fake_coverage)
    monkeypatch.setattr(cov, "_save_audit", flaky_save)
    init_db()

    async def _run():
        out = await run_fomo_trending_audit(force=True)
        assert out["ok"] and calls["n"] == 2
        with session_scope() as session:
            beat = session.query(ScanState).filter(ScanState.key == "loop:fomo_trending").one_or_none()
            assert beat is not None

    asyncio.run(_run())
