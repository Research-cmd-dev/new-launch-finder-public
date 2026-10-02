"""FOMO /ws/alerts parse, filter, dedupe — no live WSS in CI."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from launchfinder.research.fomo_alerts import (
    fomo_alerts_heartbeat_note,
    iter_ws_alert_rows,
    normalize_fomo_alert_row,
    parse_trader_wallet_from_row,
    parse_ws_payload,
    passes_fomo_alert_filters,
    persist_fomo_alert,
)


TRADER_WALLET_SOL = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"


def test_parse_trader_wallet_nested_and_flat():
    raw_nested = {
        "eventId": "ev-wallet-nested",
        "tokenAddress": "So11111111111111111111111111111111111111112",
        "chain": "sol",
        "alertType": "buy",
        "usdValue": 5000,
        "trader": {"handle": "alpha", "wallet": TRADER_WALLET_SOL},
    }
    assert parse_trader_wallet_from_row(raw_nested, "sol") == TRADER_WALLET_SOL
    norm = normalize_fomo_alert_row(raw_nested)
    assert norm is not None
    assert norm["trader_wallet"] == TRADER_WALLET_SOL

    raw_flat = dict(raw_nested)
    raw_flat.pop("trader")
    raw_flat["traderWallet"] = TRADER_WALLET_SOL
    assert parse_trader_wallet_from_row(raw_flat, "sol") == TRADER_WALLET_SOL

    raw_missing = {
        "eventId": "ev-no-wallet",
        "tokenAddress": "So11111111111111111111111111111111111111112",
        "chain": "sol",
        "alertType": "buy",
        "usdValue": 5000,
    }
    assert parse_trader_wallet_from_row(raw_missing, "sol") == ""


def test_parse_sol_buy_alert():
    raw = {
        "eventId": "ev-sol-1",
        "userId": "u1",
        "trader": "alpha_trader",
        "token": "PEPE",
        "tokenAddress": "So11111111111111111111111111111111111111112",
        "chain": "sol",
        "alertType": "buy",
        "usdValue": 5000,
        "tradeId": "t1",
        "text": "loading",
        "ts": "2026-09-30T12:00:00Z",
    }
    norm = normalize_fomo_alert_row(raw)
    assert norm is not None
    assert norm["chain"] == "sol"
    assert norm["mint"] == "So11111111111111111111111111111111111111112"
    assert passes_fomo_alert_filters(norm, min_usd=2000)


def test_drop_small_usd_and_wrong_type():
    raw = {
        "eventId": "ev-small",
        "tokenAddress": "0xa3602804e096cb73bd8344afc1ff3f3390b899c5",
        "chain": "robinhood",
        "alertType": "buy",
        "usdValue": 50,
    }
    norm = normalize_fomo_alert_row(raw)
    assert norm is not None
    assert not passes_fomo_alert_filters(norm, min_usd=2000)
    raw["alertType"] = "like"
    norm2 = normalize_fomo_alert_row(raw)
    assert norm2 is not None
    assert not passes_fomo_alert_filters(norm2, min_usd=10)


def test_replay_batch_and_dedupe_fields():
    payload = {
        "type": "replay",
        "alerts": [
            {
                "eventId": "ev-rh-1",
                "tokenAddress": "0xa3602804e096cb73bd8344afc1ff3f3390b899c5",
                "chain": "robinhood",
                "alertType": "thesis",
                "usdValue": 3000,
                "thesis": "community takeover",
            },
            {
                "eventId": "ev-rh-1",
                "tokenAddress": "0xa3602804e096cb73bd8344afc1ff3f3390b899c5",
                "chain": "robinhood",
                "alertType": "thesis",
                "usdValue": 3000,
            },
        ],
    }
    rows = iter_ws_alert_rows(payload)
    assert len(rows) == 2
    parsed = parse_ws_payload(payload)
    assert len(parsed) == 2
    assert parsed[0]["event_id"] == "ev-rh-1"
    assert parsed[0]["text_snippet"] == "community takeover"


def test_welcome_yields_no_alerts():
    assert iter_ws_alert_rows({"type": "welcome", "message": "ok"}) == []
    assert parse_ws_payload({"type": "welcome"}) == []


def test_persist_keeps_payload_wallet_without_http(monkeypatch):
    import launchfinder.research.fomo_api as api
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import FomoAlertEvent

    api.reset_wallet_lookup_skip_state()
    monkeypatch.delenv("FOMO_USER_WALLET_LOOKUP", raising=False)

    def _boom(*_a, **_k):
        raise AssertionError("payload wallet must not trigger /v2/users/id")

    monkeypatch.setattr(api.httpx, "Client", _boom)
    init_db()
    row = {
        "event_id": "wallet-keep-payload",
        "user_id": "u-has-wallet",
        "trader": "alpha",
        "trader_wallet": TRADER_WALLET_SOL,
        "token_symbol": "X",
        "mint": "So11111111111111111111111111111111111111112",
        "chain": "sol",
        "alert_type": "buy",
        "usd_value": 2500.0,
        "trade_id": "",
        "text_snippet": "",
        "event_ts": None,
    }
    with session_scope() as session:
        assert persist_fomo_alert(session, row) is True
        ev = session.query(FomoAlertEvent).filter(FomoAlertEvent.event_id == "wallet-keep-payload").one()
        assert ev.trader_wallet == TRADER_WALLET_SOL
        assert ev.user_id == "u-has-wallet"
        assert ev.trader == "alpha"
    assert api.WALLET_LOOKUP_SKIPS == 0


def test_persist_without_wallet_skips_user_profile_http(monkeypatch):
    import launchfinder.research.fomo_api as api
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import FomoAlertEvent

    api.reset_wallet_lookup_skip_state()
    monkeypatch.delenv("FOMO_USER_WALLET_LOOKUP", raising=False)
    calls = 0

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            nonlocal calls
            calls += 1
            raise AssertionError(f"disabled persist must not HTTP {url}")

    monkeypatch.setattr(api, "_auth_headers", lambda: {"authorization": "Bearer test"})
    monkeypatch.setattr(api.httpx, "Client", _Client)
    init_db()
    row = {
        "event_id": "wallet-missing-skip",
        "user_id": "u-no-wallet",
        "trader": "beta",
        "trader_wallet": "",
        "token_symbol": "Y",
        "mint": "So11111111111111111111111111111111111111112",
        "chain": "sol",
        "alert_type": "buy",
        "usd_value": 4000.0,
        "trade_id": "",
        "text_snippet": "loading",
        "event_ts": None,
    }
    with session_scope() as session:
        assert persist_fomo_alert(session, row) is True
        ev = session.query(FomoAlertEvent).filter(FomoAlertEvent.event_id == "wallet-missing-skip").one()
        assert ev.trader_wallet == ""
        assert ev.user_id == "u-no-wallet"
        assert ev.trader == "beta"
    assert calls == 0
    assert api.WALLET_LOOKUP_SKIPS == 1
    assert "wallet_lookup=off" in fomo_alerts_heartbeat_note(connected=True, received=1, inserted=1)


def test_persist_stores_trader_wallet():
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import FomoAlertEvent

    init_db()
    row = {
        "event_id": "wallet-persist-1",
        "user_id": "u-wallet",
        "trader": "t",
        "trader_wallet": TRADER_WALLET_SOL,
        "token_symbol": "X",
        "mint": "So11111111111111111111111111111111111111112",
        "chain": "sol",
        "alert_type": "sell",
        "usd_value": 2500.0,
        "trade_id": "",
        "text_snippet": "",
        "event_ts": None,
    }
    with session_scope() as session:
        assert persist_fomo_alert(session, row) is True
        ev = session.query(FomoAlertEvent).filter(FomoAlertEvent.event_id == "wallet-persist-1").one()
        assert ev.trader_wallet == TRADER_WALLET_SOL


def test_persist_dedupe_integration():
    from launchfinder.db import init_db, session_scope

    init_db()
    row = {
        "event_id": "dedupe-test-1",
        "user_id": "",
        "trader": "t",
        "trader_wallet": "",
        "token_symbol": "X",
        "mint": "So11111111111111111111111111111111111111112",
        "chain": "sol",
        "alert_type": "sell",
        "usd_value": 2500.0,
        "trade_id": "",
        "text_snippet": "",
        "event_ts": None,
    }
    with session_scope() as session:
        assert persist_fomo_alert(session, row) is True
        assert persist_fomo_alert(session, row) is False


def test_fomo_alerts_heartbeat_while_idle(monkeypatch):
    import launchfinder.ingest.fomo_alerts_ws as ws_mod

    monkeypatch.setattr(ws_mod, "FOMO_ALERTS_HEARTBEAT_S", 0.05)
    monkeypatch.setattr(ws_mod, "FOMO_ALERTS_IDLE_RECONNECT_S", 10.0)
    monkeypatch.setattr(ws_mod, "FOMO_ALERTS_RECV_POLL_S", 0.02)
    monkeypatch.setattr(ws_mod, "fomo_alerts_ws_url", lambda: "wss://test.example/ws")

    beats: list[str] = []

    def beat(_name: str, *, note: str = "") -> None:
        beats.append(note)

    class _FakeWS:
        async def recv(self) -> str:
            await asyncio.sleep(999)

        async def close(self) -> None:
            return None

    fake = _FakeWS()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=fake)
    ctx.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(ws_mod.websockets, "connect", lambda *a, **k: ctx)

    stop = asyncio.Event()

    async def _run() -> None:
        task = asyncio.create_task(ws_mod.listen_fomo_alerts(stop, beat))
        await asyncio.sleep(0.12)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(_run())
    assert any("ws=up" in b and "recv=" in b for b in beats)


def test_fomo_alerts_idle_forces_reconnect(monkeypatch):
    import launchfinder.ingest.fomo_alerts_ws as ws_mod

    monkeypatch.setattr(ws_mod, "FOMO_ALERTS_HEARTBEAT_S", 0.05)
    monkeypatch.setattr(ws_mod, "FOMO_ALERTS_IDLE_RECONNECT_S", 0.08)
    monkeypatch.setattr(ws_mod, "FOMO_ALERTS_RECV_POLL_S", 0.02)
    monkeypatch.setattr(ws_mod, "fomo_alerts_ws_url", lambda: "wss://test.example/ws")

    beats: list[str] = []
    connects = 0

    def beat(_name: str, *, note: str = "") -> None:
        beats.append(note)

    class _FakeWS:
        async def recv(self) -> str:
            await asyncio.sleep(999)

    def _connect(*_a, **_k):
        nonlocal connects
        connects += 1
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=_FakeWS())
        ctx.__aexit__ = AsyncMock(return_value=None)
        return ctx

    monkeypatch.setattr(ws_mod.websockets, "connect", _connect)

    stop = asyncio.Event()

    async def _run() -> None:
        task = asyncio.create_task(ws_mod.listen_fomo_alerts(stop, beat))
        await asyncio.sleep(0.25)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(_run())
    assert connects >= 2
    assert any("idle" in b.lower() or "ws=down" in b for b in beats)
