"""Instant-curve watch door completions (BANDIT-class)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from launchfinder.ingest import pump_poll


def test_ingest_watch_door_completions_skips_incomplete(monkeypatch):
    monkeypatch.setattr(
        pump_poll,
        "watch_preview",
        lambda: [{"mint": "Mint1111111111111111111111111111111111", "progress": 0.95}],
    )
    monkeypatch.setattr(
        pump_poll.pumpfun,
        "get_coin",
        AsyncMock(return_value={"mint": "Mint1111111111111111111111111111111111", "complete": False}),
    )
    out = asyncio.run(
        pump_poll._ingest_watch_door_completions(watched=set(), known=set())
    )
    assert out == []


def test_ingest_watch_door_completions_ingests_when_complete(monkeypatch):
    mint = "Mint2222222222222222222222222222222222"
    monkeypatch.setattr(pump_poll, "watch_preview", lambda: [{"mint": mint, "progress": 0.92}])
    monkeypatch.setattr(
        pump_poll.pumpfun,
        "get_coin",
        AsyncMock(return_value={"mint": mint, "complete": True, "symbol": "FAST"}),
    )

    class _Tok:
        symbol = "FAST"

    async def fake_ingest(session, *, mint, source, coin):
        assert source == "watch_door"
        return _Tok()

    import asyncio
    from contextlib import contextmanager

    class _Sess:
        def query(self, *_a, **_k):
            return self

        def filter(self, *_a, **_k):
            return self

        def first(self):
            return None

    @contextmanager
    def _scope():
        yield _Sess()

    monkeypatch.setattr("launchfinder.db.ingest_lock", asyncio.Lock())
    monkeypatch.setattr(pump_poll, "ingest_and_research", fake_ingest)
    monkeypatch.setattr("launchfinder.db.session_scope", _scope)

    out = asyncio.run(pump_poll._ingest_watch_door_completions(watched=set(), known=set()))
    assert out == [mint]
