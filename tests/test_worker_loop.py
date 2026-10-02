"""Live ingest must not wait on the Dex tape refresh."""

from __future__ import annotations

import inspect

from launchfinder import worker


def test_live_poll_does_not_await_tape_refresh():
    src = inspect.getsource(worker.loop)
    assert "await refresh_outcomes" not in src
    assert "create_task(_tape_refresh_loop())" in src


def test_tape_refresh_does_not_lock_across_http():
    src = inspect.getsource(worker._tape_refresh_loop)
    assert "await refresh_outcomes" in src
    assert "async with ingest_lock" not in src.split("await refresh_outcomes")[0]


def test_tape_refresh_beats_at_cycle_start_before_long_refresh():
    src = inspect.getsource(worker._tape_refresh_loop)
    start = src.index('log.info("tape refresh cycle')
    before_refresh = src.split("await refresh_outcomes")[0]
    assert "cycle" in before_refresh and "start" in before_refresh
    assert 'to_thread(_beat, "tape_refresh"' in before_refresh


def test_tape_refresh_defers_lock_without_poisoning_cycle():
    src = inspect.getsource(worker._tape_refresh_loop)
    assert "_hunt_session_defer" in src
    assert "lock-defer" in src
    assert "lock_deferred" in src


def test_fomo_alerts_ws_started_as_background_task():
    src = inspect.getsource(worker.loop)
    assert "listen_fomo_alerts" in src
    assert "fomo_alerts_task" in src
