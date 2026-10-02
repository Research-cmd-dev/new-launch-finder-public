from datetime import datetime, timedelta, timezone

from launchfinder.db import init_db, session_scope
from launchfinder.ingest.pump_poll import _reclassify_old_majors, is_fresh_migration
from launchfinder.models import Outcome, Token, utcnow


def test_recent_create_is_fresh():
    coin = {
        "mint": "abc",
        "created_at": datetime.now(timezone.utc) - timedelta(hours=2),
        "updated_at": datetime.now(timezone.utc),
        "mcap_usd": 80_000,
    }
    assert is_fresh_migration(coin, set())


def test_fat_leftover_fdv_is_not_fresh_even_when_created_today():
    # Live 08:20: NTDA / NIKE / CLAWDHOOD $5–15M on last_trade with
    # created_at inside 18h. That is leftover FDV, not a $69k graduate.
    coin = {
        "mint": "ntda",
        "created_at": datetime.now(timezone.utc) - timedelta(hours=2),
        "updated_at": datetime.now(timezone.utc),
        "mcap_usd": 8_600_000,
    }
    assert not is_fresh_migration(coin, set())
    assert is_fresh_migration(coin, {"ntda"})


def test_old_major_is_not_fresh():
    coin = {
        "mint": "wofi",
        "created_at": datetime.now(timezone.utc) - timedelta(days=10),
        "updated_at": datetime.now(timezone.utc) - timedelta(minutes=1),
        "mcap_usd": 700_000_000,
    }
    assert not is_fresh_migration(coin, set())


def test_watched_incomplete_flip_is_fresh():
    coin = {
        "mint": "watchedmint",
        "created_at": datetime.now(timezone.utc) - timedelta(days=3),
        "updated_at": datetime.now(timezone.utc),
        "mcap_usd": 90_000,
    }
    assert is_fresh_migration(coin, {"watchedmint"})


def test_rh_poll_ingests_completed_row_sol_freshness_would_drop(monkeypatch):
    # No created_at / usd_market_cap — is_fresh_migration is False unless watched.
    import asyncio

    from launchfinder.db import init_db
    from launchfinder.ingest import rh_poll
    from launchfinder.models import Token

    row = {"address": "0xmissing00000000000000000000000000000001", "symbol": "MISS", "name": "Missed"}
    coin = {
        "mint": "0xmissing00000000000000000000000000000001",
        "created_at": None,
        "updated_at": None,
        "mcap_usd": 0,
    }
    assert is_fresh_migration(coin, set()) is False

    ingested: list[str] = []

    async def fake_trenches(*, chain="sol", limit=40):
        return [row], [], []

    async def fake_ingest(session, **kw):
        ingested.append(kw["mint"])
        return Token(mint=kw["mint"], symbol="MISS", chain="robinhood")

    from types import SimpleNamespace

    async def fake_pons():
        return []

    async def fake_dex():
        return []

    monkeypatch.setattr(rh_poll, "settings", SimpleNamespace(robinhood_enabled=True, has_gmgn=True))
    monkeypatch.setattr(rh_poll.gmgn, "gmgn_available", lambda: True)
    monkeypatch.setattr(rh_poll.gmgn, "trenches", fake_trenches)
    monkeypatch.setattr(rh_poll, "ingest_and_research", fake_ingest)
    monkeypatch.setattr(rh_poll, "poll_pons_launches", fake_pons)
    monkeypatch.setattr(rh_poll, "poll_rh_dex", fake_dex)

    init_db()
    found = asyncio.run(rh_poll.poll_robinhood())
    assert "0xmissing00000000000000000000000000000001" in ingested
    assert found == ingested


def test_rh_poll_ingests_pons_new_creation(monkeypatch):
    # PONS deploys straight to Uniswap V3 — it never appears as a
    # bonding-curve "completed" row. new_creation is the feed.
    import asyncio

    from launchfinder.ingest import rh_poll
    from launchfinder.models import Token

    row = {
        "address": "0xponsnew000000000000000000000000000000001",
        "symbol": "PONSX",
        "name": "Pons Launch",
        "launchpad_platform": "pons",
        "usd_market_cap": 18_000,
    }
    ingested: list[tuple[str, str]] = []

    async def fake_trenches(*, chain="sol", limit=40):
        return [], [], [row]

    async def fake_ingest(session, **kw):
        ingested.append((kw["mint"], kw["source"]))
        return Token(mint=kw["mint"], symbol="PONSX", chain="robinhood", source=kw["source"])

    from types import SimpleNamespace

    async def fake_pons():
        return []

    async def fake_dex():
        return []

    monkeypatch.setattr(rh_poll, "settings", SimpleNamespace(robinhood_enabled=True, has_gmgn=True))
    monkeypatch.setattr(rh_poll.gmgn, "gmgn_available", lambda: True)
    monkeypatch.setattr(rh_poll.gmgn, "trenches", fake_trenches)
    monkeypatch.setattr(rh_poll, "ingest_and_research", fake_ingest)
    monkeypatch.setattr(rh_poll, "poll_pons_launches", fake_pons)
    monkeypatch.setattr(rh_poll, "poll_rh_dex", fake_dex)

    init_db()
    found = asyncio.run(rh_poll.poll_robinhood())
    assert ingested == [("0xponsnew000000000000000000000000000000001", "rh_pons")]
    assert found == ["0xponsnew000000000000000000000000000000001"]


def test_reclassify_old_majors_skips_robinhood_catchup():
    init_db()
    with session_scope() as session:
        zazu = Token(
            mint="0xzazureclass000000000000000000000000001",
            symbol="ZAZU",
            chain="robinhood",
            source="rh_trenches",
            is_historical=False,
            first_seen_at=utcnow(),
            migrated_at=utcnow() - timedelta(hours=30),
            created_at_chain=None,
        )
        stale_sol = Token(
            mint="SolStaleMajor111111111111111111111111111",
            symbol="OLD",
            chain="sol",
            source="poll",
            is_historical=False,
            first_seen_at=utcnow() - timedelta(days=2),
            migrated_at=utcnow() - timedelta(days=2),
            created_at_chain=utcnow() - timedelta(days=3),
        )
        session.add_all([zazu, stale_sol])
        session.flush()
        _reclassify_old_majors(session)
        assert zazu.is_historical is False
        assert stale_sol.is_historical is True


def test_reclassify_old_majors_skips_sol_with_last_mcap_even_when_liq_thin():
    init_db()
    with session_scope() as session:
        token = Token(
            mint="resideK2Ejv9apu1op2VBv89poumxCDCs8nzBxQ5q1y",
            symbol="RESI",
            chain="sol",
            source="websocket",
            is_historical=False,
            first_seen_at=utcnow() - timedelta(days=2),
            migrated_at=utcnow() - timedelta(days=2),
            created_at_chain=None,
        )
        token.outcome = Outcome(t0_mcap=69_000.0, last_mcap=595_000.0, last_liq=50.0, multiple=8.0)
        session.add(token)
        session.flush()
        _reclassify_old_majors(session)
        assert token.is_historical is False


def test_reclassify_old_majors_keeps_fomo_board_stub_zero_last_live():
    """AQUA-class: FOMO stub must not historical-park before Dex can hydrate."""
    init_db()
    with session_scope() as session:
        token = Token(
            mint="AQVcP67Stub1111111111111111111111111",
            symbol="AQUA",
            chain="sol",
            source="fomo_board",
            is_historical=False,
            first_seen_at=utcnow(),
            created_at_chain=None,
            migrated_at=None,
        )
        token.outcome = Outcome(t0_mcap=0.0, last_mcap=0.0, multiple=0.0)
        session.add(token)
        session.flush()
        _reclassify_old_majors(session)
        assert token.is_historical is False


def test_historical_hydrate_queues_sol_fomo_board_zero_last():
    from launchfinder.models import Research
    from launchfinder.scoring.hunt import historical_hydrate_tape_mints

    init_db()
    mint = "AQVcP67Hydrate11111111111111111111111"
    with session_scope() as session:
        token = Token(
            mint=mint,
            symbol="AQUA",
            chain="sol",
            source="fomo_board",
            is_historical=True,
            first_seen_at=utcnow(),
            created_at_chain=None,
            migrated_at=None,
        )
        token.research = Research(p_good=0.0, features_json="{}", risk_flags_json="[]")
        token.outcome = Outcome(t0_mcap=0.0, last_mcap=0.0, multiple=0.0)
        session.add(token)
        session.flush()
        mints = historical_hydrate_tape_mints(session, "sol", limit=12)
        assert mint in mints


def test_reclassify_old_majors_skips_hydrated_sol_live_book():
    """RESI-class: unknown_age Sol row with hydrated last/liq must not re-park."""
    init_db()
    resi = "resideK2Ejv9apu1op2VBv89poumxCDCs8nzBxQ5q1y"
    with session_scope() as session:
        token = Token(
            mint=resi,
            symbol="RESI",
            chain="sol",
            source="websocket",
            is_historical=False,
            first_seen_at=utcnow() - timedelta(days=2),
            migrated_at=utcnow() - timedelta(days=2),
            created_at_chain=None,
        )
        token.outcome = Outcome(
            t0_mcap=69_000.0,
            last_mcap=920_000.0,
            last_liq=95_000.0,
            multiple=13.0,
        )
        session.add(token)
        session.flush()
        _reclassify_old_majors(session)
        assert token.is_historical is False
