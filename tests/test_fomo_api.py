from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from launchfinder.research.fomo_api import FomoSitOut, parse_alert_tokens, parse_board_tokens
from launchfinder.research.rht import parse_rht_rows
from launchfinder.scoring.features import FEATURE_NAMES


def test_parse_board_keeps_robinhood_drops_bsc() -> None:
    payload = {
        "board": "trending",
        "tokens": [
            {
                "rank": 1,
                "network": "robinhood",
                "fomoBuyers": 12,
                "marketCapUsd": 210_000,
                "token": {
                    "name": "PEZ",
                    "symbol": "PEZ",
                    "address": "0xa3602804E096cb73BD8344AFc1ff3F3390B899C5",
                },
            },
            {
                "rank": 2,
                "network": "bsc",
                "token": {
                    "name": "BEN",
                    "symbol": "BEN",
                    "address": "0x7bc70ae0f2a67d2fdc0180a697a611f21c857777",
                },
            },
            {
                "rank": 3,
                "network": "4663",
                "token": {"symbol": "FOX", "address": "0x92788a4870000000000000000000000000000001"},
            },
            {
                "rank": 4,
                "network": "solana",
                "marketCapUsd": 1_472_562,
                "token": {
                    "name": "TIPPED",
                    "symbol": "TIPPED",
                    "address": "tipp4C4Jnpft26HC9VXNjUPidojZqxXf8nzKvrKf5BS",
                },
            },
        ],
    }
    rows = parse_board_tokens(payload)
    assert [r["mint"] for r in rows] == [
        "0xa3602804e096cb73bd8344afc1ff3f3390b899c5",
        "0x92788a4870000000000000000000000000000001",
        "tipp4C4Jnpft26HC9VXNjUPidojZqxXf8nzKvrKf5BS",
    ]
    assert [r["chain"] for r in rows] == ["robinhood", "robinhood", "sol"]
    assert rows[0]["symbol"] == "PEZ"
    assert rows[0]["fomo_buyers"] == 12
    assert rows[2]["symbol"] == "TIPPED"


def test_parse_alerts_keeps_unique_robinhood() -> None:
    payload = {
        "available": True,
        "count": 3,
        "alerts": [
            {
                "type": "buy",
                "token": "PORT",
                "tokenAddress": "0xafa57c4c5a72d36530c8e816ad6e9a5947941536",
                "chainId": 4663,
                "chain": "robinhood",
                "usdValue": 20000,
            },
            {
                "type": "buy",
                "token": "PORT",
                "tokenAddress": "0xAFA57C4C5A72D36530C8E816AD6E9A5947941536",
                "chainId": 4663,
                "chain": "robinhood",
                "usdValue": 8000,
            },
            {
                "type": "buy",
                "token": "BEN",
                "tokenAddress": "0x7bc70ae0f2a67d2fdc0180a697a611f21c857777",
                "chainId": 56,
                "chain": "bsc",
                "usdValue": 5000,
            },
        ],
    }
    rows = parse_alert_tokens(payload)
    assert [r["mint"] for r in rows] == ["0xafa57c4c5a72d36530c8e816ad6e9a5947941536"]
    assert rows[0]["symbol"] == "PORT"


def test_fetch_trending_noops_without_key(monkeypatch) -> None:
    from launchfinder.research import fomo_api

    monkeypatch.setattr(fomo_api, "settings", SimpleNamespace(fomo_api_key=""))
    out = asyncio.run(fomo_api.fetch_trending())
    assert out.rows == []
    assert out.upstream.get("board_stale") is False


def test_fetch_alerts_is_keyless(monkeypatch) -> None:
    from launchfinder.research import fomo_api

    seen = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {
                "alerts": [
                    {
                        "token": "PORT",
                        "tokenAddress": "0xafa57c4c5a72d36530c8e816ad6e9a5947941536",
                        "chain": "robinhood",
                        "chainId": 4663,
                    }
                ]
            }

    class _Client:
        async def get(self, url, params=None, headers=None):
            seen["url"] = url
            seen["params"] = params
            seen["headers"] = headers
            return _Resp()

    monkeypatch.setattr(fomo_api, "settings", SimpleNamespace(fomo_api_key="must-not-be-sent"))
    monkeypatch.setattr(fomo_api, "client", lambda: _Client())
    rows = asyncio.run(fomo_api.fetch_alerts(limit=100))
    assert seen["headers"] is None
    assert seen["params"]["chain"] == "robinhood"
    assert rows[0]["symbol"] == "PORT"


def test_fetch_trending_sit_out_on_402(monkeypatch) -> None:
    from launchfinder.research import fomo_api

    class _Resp:
        status_code = 402

    class _Client:
        async def get(self, *_a, **_k):
            return _Resp()

    monkeypatch.setattr(fomo_api, "settings", SimpleNamespace(fomo_api_key="test-key"))
    monkeypatch.setattr(fomo_api, "client", lambda: _Client())
    try:
        asyncio.run(fomo_api.fetch_trending())
        raise AssertionError("expected FomoSitOut")
    except FomoSitOut as exc:
        assert exc.status == 402


def test_parse_rht_drops_stocks_and_drained() -> None:
    rows = parse_rht_rows(
        [
            {
                "token": "0xafa57c4c5a72d36530c8e816ad6e9a5947941536",
                "symbol": "PORT",
                "name": "Port",
                "is_stock": 0,
                "buyers": 1,
            },
            {
                "token": "0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec",
                "symbol": "NVDA",
                "is_stock": 1,
                "buyers": 14,
            },
            {
                "token": "0xf7829cbb00000000000000000000000000000001",
                "symbol": "sue",
                "drained": True,
                "buyers": 15,
            },
        ]
    )
    assert [r["mint"] for r in rows] == ["0xafa57c4c5a72d36530c8e816ad6e9a5947941536"]
    assert rows[0]["symbol"] == "PORT"


def test_poll_skips_when_robinhood_off(monkeypatch) -> None:
    from launchfinder.ingest import fomo_poll

    monkeypatch.setattr(
        fomo_poll,
        "settings",
        SimpleNamespace(robinhood_enabled=False, has_fomo=False, fomo_api_key=""),
    )
    assert asyncio.run(fomo_poll.poll_fomo_launches()) == []


def test_poll_ingests_unknown_rh_and_skips_gmgn(monkeypatch) -> None:
    from launchfinder.db import SessionLocal, init_db
    from launchfinder.ingest import fomo_poll
    from launchfinder.models import Token

    init_db()
    mint = "0xa3602804e096cb73bd8344afc1ff3f3390b899c5"
    created = datetime.now(timezone.utc) - timedelta(hours=4)
    ingested_coins = []

    async def _alerts(*, limit=100):
        return [{"mint": mint, "symbol": "PEZ", "name": "PEZ"}]

    async def _market(addr, chain="robinhood"):
        assert addr == mint
        return {
            "mint": mint,
            "name": "PEZ",
            "symbol": "PEZ",
            "created_at": created,
            "liquidity_usd": 12_000,
            "mcap_usd": 180_000,
            "pair_address": "0xabc",
            "quote_symbol": "ETH",
            "quote_mint": "0x0000000000000000000000000000000000000000",
            "dex_labels": ["v4"],
            "website": "",
        }

    async def _ingest(session, *, mint, source, coin):
        ingested_coins.append(coin)
        token = Token(mint=mint, symbol="PEZ", chain="robinhood", source=source)
        session.add(token)
        session.flush()
        return token

    monkeypatch.setattr(
        fomo_poll,
        "settings",
        SimpleNamespace(robinhood_enabled=True, has_fomo=False, fomo_api_key=""),
    )
    monkeypatch.setattr(fomo_poll, "fetch_alerts", _alerts)
    monkeypatch.setattr(fomo_poll, "FOMO_APP_SEEDS", ())

    async def _empty(**_k):
        return []

    monkeypatch.setattr(fomo_poll, "fetch_rht_radar", _empty)
    monkeypatch.setattr(fomo_poll, "fetch_rht_tokens", _empty)
    monkeypatch.setattr(fomo_poll, "token_market", _market)
    monkeypatch.setattr(fomo_poll, "ingest_and_research", _ingest)
    monkeypatch.setattr(fomo_poll, "_due", lambda _session: True)

    news = asyncio.run(fomo_poll.poll_fomo_launches())
    assert news == [mint]
    assert ingested_coins[0]["skip_gmgn"] is True
    db = SessionLocal()
    try:
        row = db.query(Token).filter(Token.mint == mint).one()
        assert row.source == "rh_fomo"
    finally:
        db.close()


def test_poll_seeds_app_board_when_alerts_omit_it(monkeypatch) -> None:
    from launchfinder.db import init_db
    from launchfinder.ingest import fomo_poll

    init_db()
    pez = "0xa3602804e096cb73bd8344afc1ff3f3390b899c5"
    created = datetime.now(timezone.utc) - timedelta(hours=8)
    seen = []

    async def _alerts(*, limit=100):
        return []

    async def _market(addr, chain="robinhood"):
        seen.append(addr)
        if addr != pez:
            return {}
        return {
            "mint": pez,
            "name": "PEZ",
            "symbol": "PEZ",
            "created_at": created,
            "liquidity_usd": 200_000,
            "mcap_usd": 4_900_000,
            "dex_labels": ["v4"],
            "quote_symbol": "ETH",
        }

    async def _ingest(session, *, mint, source, coin):
        token = SimpleNamespace(symbol="PEZ", mint=mint)
        return token

    async def _empty(**_k):
        return []

    monkeypatch.setattr(fomo_poll, "settings", SimpleNamespace(robinhood_enabled=True))
    monkeypatch.setattr(fomo_poll, "fetch_alerts", _alerts)
    monkeypatch.setattr(fomo_poll, "fetch_rht_radar", _empty)
    monkeypatch.setattr(fomo_poll, "fetch_rht_tokens", _empty)
    monkeypatch.setattr(fomo_poll, "token_market", _market)
    monkeypatch.setattr(fomo_poll, "ingest_and_research", _ingest)
    monkeypatch.setattr(fomo_poll, "_due", lambda _session: True)
    monkeypatch.setattr(
        fomo_poll,
        "FOMO_APP_SEEDS",
        ({"mint": pez, "symbol": "PEZ", "name": "PEZ"},),
    )

    news = asyncio.run(fomo_poll.poll_fomo_launches())
    assert news == [pez]
    assert pez in seen


def test_poll_skips_old_or_thin_books(monkeypatch) -> None:
    from launchfinder.db import init_db
    from launchfinder.ingest import fomo_poll

    init_db()
    old = datetime.now(timezone.utc) - timedelta(hours=40)

    async def _alerts(*, limit=100):
        return [{"mint": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "symbol": "OLD"}]

    async def _market(addr, chain="robinhood"):
        return {
            "mint": addr,
            "created_at": old,
            "liquidity_usd": 50_000,
            "mcap_usd": 2_000_000,
            "dex_labels": ["v4"],
        }

    called = []

    async def _ingest(*_a, **_k):
        called.append(1)
        return None

    monkeypatch.setattr(
        fomo_poll,
        "settings",
        SimpleNamespace(robinhood_enabled=True, has_fomo=True, fomo_api_key="k"),
    )
    async def _empty(**_k):
        return []

    monkeypatch.setattr(fomo_poll, "fetch_alerts", _alerts)
    monkeypatch.setattr(fomo_poll, "FOMO_APP_SEEDS", ())
    monkeypatch.setattr(fomo_poll, "fetch_rht_radar", _empty)
    monkeypatch.setattr(fomo_poll, "fetch_rht_tokens", _empty)
    monkeypatch.setattr(fomo_poll, "token_market", _market)
    monkeypatch.setattr(fomo_poll, "ingest_and_research", _ingest)
    monkeypatch.setattr(fomo_poll, "_due", lambda _session: True)
    assert asyncio.run(fomo_poll.poll_fomo_launches()) == []
    assert called == []


def test_poll_ingests_rht_radar_mint(monkeypatch) -> None:
    from launchfinder.db import init_db
    from launchfinder.ingest import fomo_poll

    init_db()
    mint = "0xafa57c4c5a72d36530c8e816ad6e9a5947941536"
    created = datetime.now(timezone.utc) - timedelta(hours=1)

    async def _empty(**_k):
        return []

    async def _radar(*, minutes=180, limit=40):
        return [{"mint": mint, "symbol": "PORT", "name": "Port"}]

    async def _market(addr, chain="robinhood"):
        return {
            "mint": addr,
            "name": "Port",
            "symbol": "PORT",
            "created_at": created,
            "liquidity_usd": 50_000,
            "mcap_usd": 1_200_000,
            "dex_labels": ["v4"],
            "quote_symbol": "ETH",
        }

    ingested = []

    async def _ingest(session, *, mint, source, coin):
        ingested.append(mint)
        return SimpleNamespace(symbol="PORT", mint=mint)

    monkeypatch.setattr(fomo_poll, "settings", SimpleNamespace(robinhood_enabled=True))
    monkeypatch.setattr(fomo_poll, "fetch_alerts", _empty)
    monkeypatch.setattr(fomo_poll, "fetch_rht_radar", _radar)
    monkeypatch.setattr(fomo_poll, "fetch_rht_tokens", _empty)
    monkeypatch.setattr(fomo_poll, "FOMO_APP_SEEDS", ())
    monkeypatch.setattr(fomo_poll, "token_market", _market)
    monkeypatch.setattr(fomo_poll, "ingest_and_research", _ingest)
    monkeypatch.setattr(fomo_poll, "_due", lambda _session: True)
    assert asyncio.run(fomo_poll.poll_fomo_launches()) == [mint]
    assert ingested == [mint]


def test_alerts_poll_default_is_under_an_hour() -> None:
    from launchfinder.config import settings

    # Keyless alerts are free. 10 min default; never slower than 1h unless set.
    assert settings.fomo_poll_seconds <= 3600
    assert settings.fomo_poll_seconds >= 300


def test_poll_ingests_unknown_sol_from_trending(monkeypatch) -> None:
    from launchfinder.db import init_db
    from launchfinder.ingest import fomo_poll

    init_db()
    mint = "tipp4C4Jnpft26HC9VXNjUPidojZqxXf8nzKvrKf5BS"
    created = datetime.now(timezone.utc) - timedelta(hours=1)
    ingested = []

    async def _empty(**_k):
        return []

    from launchfinder.research.fomo_api import TrendingFetchResult

    async def _trend(*, limit=25):
        return TrendingFetchResult(
            rows=[{"mint": mint, "symbol": "TIPPED", "name": "TIPPED", "chain": "sol", "network": "sol"}],
            upstream={"board_stale": False},
        )

    async def _market(addr, chain="sol"):
        assert chain == "sol"
        return {
            "mint": addr,
            "name": "TIPPED",
            "symbol": "TIPPED",
            "created_at": created,
            "liquidity_usd": 115_000,
            "mcap_usd": 1_470_000,
            "pair_address": "pair1",
        }

    async def _ingest(session, *, mint, source, coin):
        ingested.append((mint, source, coin.get("skip_gmgn"), coin.get("chain")))
        return SimpleNamespace(symbol="TIPPED", mint=mint)

    monkeypatch.setattr(fomo_poll, "settings", SimpleNamespace(robinhood_enabled=True))
    monkeypatch.setattr(fomo_poll, "fetch_alerts", _empty)
    monkeypatch.setattr(fomo_poll, "fetch_rht_radar", _empty)
    monkeypatch.setattr(fomo_poll, "fetch_rht_tokens", _empty)
    monkeypatch.setattr(fomo_poll, "fetch_trending", _trend)
    monkeypatch.setattr(fomo_poll, "FOMO_APP_SEEDS", ())
    monkeypatch.setattr(fomo_poll, "token_market", _market)
    monkeypatch.setattr(fomo_poll, "ingest_and_research", _ingest)
    monkeypatch.setattr(fomo_poll, "_due", lambda _session: True)
    assert asyncio.run(fomo_poll.poll_fomo_launches()) == [mint]
    assert ingested == [(mint, "sol_fomo", True, "sol")]


def test_poll_sit_out_stamps_heartbeat_and_does_not_buy(monkeypatch) -> None:
    from launchfinder.db import init_db, session_scope
    from launchfinder.ingest import fomo_poll
    from launchfinder.models import PaperFill, ScanState
    from launchfinder.research.fomo_api import FomoSitOut

    init_db()
    ingested = []

    async def _empty(**_k):
        return []

    async def _trend(*, limit=25):
        raise FomoSitOut(402)

    async def _ingest(session, *, mint, source, coin):
        ingested.append(mint)
        return SimpleNamespace(symbol="NOPE", mint=mint)

    monkeypatch.setattr(fomo_poll, "settings", SimpleNamespace(robinhood_enabled=True))
    monkeypatch.setattr(fomo_poll, "fetch_alerts", _empty)
    monkeypatch.setattr(fomo_poll, "fetch_rht_radar", _empty)
    monkeypatch.setattr(fomo_poll, "fetch_rht_tokens", _empty)
    monkeypatch.setattr(fomo_poll, "FOMO_APP_SEEDS", ())
    monkeypatch.setattr(fomo_poll, "fetch_trending", _trend)
    monkeypatch.setattr(fomo_poll, "ingest_and_research", _ingest)
    monkeypatch.setattr(fomo_poll, "_due", lambda _session: True)
    assert asyncio.run(fomo_poll.poll_fomo_launches()) == []
    assert ingested == []
    with session_scope() as session:
        beat = session.query(ScanState).filter(ScanState.key == "loop:fomo_trending").one()
        assert "sit-out 402" in (beat.value or "")
        assert session.query(PaperFill).count() == 0


def test_coverage_marks_caught_leftover_and_miss(monkeypatch) -> None:
    from launchfinder.db import SessionLocal, init_db
    from launchfinder.ingest.fomo_poll import save_trending_snapshot
    from launchfinder.models import Outcome, Research, Token, utcnow
    from launchfinder.research.fomo_coverage import fomo_trending_coverage

    init_db()
    now = utcnow()
    caught_mint = "tipp4C4Jnpft26HC9VXNjUPidojZqxXf8nzKvrKf5BS"
    leftover_mint = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    miss_mint = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    db = SessionLocal()
    try:
        tok = Token(
            mint=caught_mint,
            symbol="TIPPED",
            chain="sol",
            source="poll",
            first_seen_at=now,
        )
        db.add(tok)
        db.flush()
        db.add(Research(token_id=tok.id, features_json="{}", p_good=0.1918, scorer="first_sight"))
        db.add(Outcome(token_id=tok.id, t0_mcap=68_244, last_mcap=1_472_562, last_liq=115_000))
        save_trending_snapshot(
            db,
            [
                {"mint": caught_mint, "chain": "sol", "symbol": "TIPPED", "mcap_usd": 1_472_562, "rank": 1},
                {"mint": leftover_mint, "chain": "robinhood", "symbol": "OLD", "mcap_usd": 2_000_000, "rank": 2},
                {"mint": miss_mint, "chain": "robinhood", "symbol": "NEW", "mcap_usd": 180_000, "rank": 3},
            ],
            now,
        )
        db.commit()

        async def _markets(mints, chain="sol"):
            out = {}
            for mint in mints:
                if mint == leftover_mint:
                    out[mint] = {
                        "mint": mint,
                        "created_at": now - timedelta(hours=40),
                        "liquidity_usd": 50_000,
                    }
                elif mint == miss_mint:
                    out[mint] = {
                        "mint": mint,
                        "created_at": now - timedelta(hours=2),
                        "liquidity_usd": 20_000,
                    }
            return out

        monkeypatch.setattr(
            "launchfinder.research.fomo_coverage.token_markets",
            _markets,
        )

        async def _resolve(snap):
            rows = list((snap or {}).get("items") or [])
            return rows, {"source": "snapshot", "board_stale": False}, None

        monkeypatch.setattr(
            "launchfinder.research.fomo_coverage._resolve_trending_board",
            _resolve,
        )

        async def _empty_sec():
            return [], {}

        monkeypatch.setattr(
            "launchfinder.research.fomo_coverage._fetch_graduated_secondary",
            _empty_sec,
        )
        monkeypatch.setattr(
            "launchfinder.research.fomo_coverage._fetch_gmgn_trending_secondary",
            _empty_sec,
        )
        monkeypatch.setattr(
            "launchfinder.research.fomo_coverage._fetch_dexscreener_trending_secondary",
            _empty_sec,
        )
        card = asyncio.run(fomo_trending_coverage(db))
        by_mint = {row["mint"]: row["status"] for row in card["items"]}
        assert by_mint[caught_mint] == "caught"
        assert by_mint[leftover_mint] == "leftover"
        assert by_mint[miss_mint] == "miss"
        assert card["counts"]["miss"] == 1
        assert card["misses"][0]["mint"] == miss_mint
    finally:
        db.close()


def test_feature_names_stay_sixty_six() -> None:
    assert len(FEATURE_NAMES) == 66
