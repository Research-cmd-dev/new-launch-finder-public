import asyncio
import json
from datetime import timedelta

from launchfinder.db import init_db, session_scope
from launchfinder.models import Outcome, Research, TapeBar, Token, utcnow
from launchfinder.research.holders import apply_sol_holder_meta, holder_tape, refresh_sol_hunt_holder_meta
from launchfinder.scoring.hunt_tape import TAPE_BAR_KEEP_HOURS, apply_hunt_tape_market, prune_tape_bars, write_tape_bar


def _tok(now):
    tok = Token(mint="TapeBar11111111111111111111111111111111111", symbol="TB", chain="sol", first_seen_at=now - timedelta(minutes=30), migrated_at=now - timedelta(minutes=30), source="poll")
    tok.research = Research(features_json="{}", p_good=0.7, holder_count=80, raw_json="{}")
    tok.outcome = Outcome(t0_mcap=69_000, max_mcap=70_000, last_mcap=70_000, last_liq=20_000)
    return tok


def test_hunt_tape_writes_one_bar_per_minute_and_prunes():
    init_db()
    now = utcnow().replace(second=30, microsecond=0)
    with session_scope() as session:
        tok = _tok(now)
        session.add(tok)
        session.flush()
        market = {"mcap_usd": 120_000, "liquidity_usd": 25_000, "volume_h1": 9_000, "price_usd": 0.00012}
        assert apply_hunt_tape_market(session, tok, market, now=now)
        assert apply_hunt_tape_market(session, tok, market, now=now + timedelta(seconds=20))
        bars = session.query(TapeBar).all()
        assert len(bars) == 1
        assert bars[0].mcap_usd == 120_000 and bars[0].liquidity_usd == 25_000 and bars[0].holders == 80
        assert bars[0].minute.replace(tzinfo=None) == now.replace(second=0, tzinfo=None)
        assert write_tape_bar(session, tok, market, mcap=125_000, liq=25_000, now=now + timedelta(minutes=1))
        session.add(TapeBar(chain="sol", mint=tok.mint, token_id=tok.id, minute=now - timedelta(hours=TAPE_BAR_KEEP_HOURS + 1), mcap_usd=1))
        session.flush()
        assert prune_tape_bars(session, now=now) == 1
        assert session.query(TapeBar).count() == 2
        # Desk tape strip: our bars + stored snapshots, oldest first, 48h window.
        from launchfinder.ledger import tape_series

        series = tape_series(session, tok, now=now + timedelta(minutes=2))
        assert series["mint"] == tok.mint and series["chain"] == "sol"
        assert [b["mcap"] for b in series["bars"]] == [120_000, 125_000]
        assert series["bars"][0]["holders"] == 80 and series["bars"][0]["liq"] == 25_000
        assert isinstance(series["snaps"], list) and series["t0_mcap"] == float(tok.outcome.t0_mcap or 0.0)
        mint = tok.mint
    import asyncio

    from launchfinder.app import get_token_tape

    out = asyncio.run(get_token_tape(mint, hours=48.0))
    assert len(out["bars"]) == 2 and out["bars"][-1]["mcap"] == 125_000
    missing = asyncio.run(get_token_tape("NoSuchMint111111111111111111111111111111111", hours=48.0))
    assert missing.status_code == 404


def test_sol_holder_refresh_writes_history_without_lifting_p(monkeypatch):
    init_db()
    now = utcnow()
    with session_scope() as session:
        tok = _tok(now)
        session.add(tok)
        session.flush()
        meta = {"holder_count": 140, "top10_pct": 31.5, "top_wallets": [{"owner": "A", "pct": 5.0, "label": ""}], "source": "helius"}
        assert apply_sol_holder_meta(tok, meta, now=now - timedelta(minutes=6))
        assert tok.research.holder_count == 140 and tok.research.top10_pct == 31.5 and tok.research.p_good == 0.7
        assert apply_sol_holder_meta(tok, {"holder_count": 190}, now=now)
        tape = holder_tape(tok.research, now=now)
        assert tape["n"] == 190 and tape["prev"] == 140 and tape["growing"] is True
        raw = json.loads(tok.research.raw_json)
        assert raw["holders"]["source"] == "helius" and len(raw["holders"]["history"]) == 2

        pages_seen: list[int] = []

        async def _fake_das(mint, creator, pool, *, max_pages=5):
            pages_seen.append(max_pages)
            return {"holder_count": 260, "top10_pct": 28.0, "source": "helius"}

        from types import SimpleNamespace

        import launchfinder.research.holders as holders_mod

        holders_mod.reset_helius_budget()
        monkeypatch.setattr("launchfinder.research.holders._das_owners", _fake_das)
        monkeypatch.setattr("launchfinder.research.holders.settings", SimpleNamespace(helius_api_key="k", sol_holder_das_pages_per_hour=60))
        # Just refreshed: not due yet (300s cadence, not RH's 120s).
        assert asyncio.run(refresh_sol_hunt_holder_meta(session, [tok], now=now)) == 0
        assert asyncio.run(refresh_sol_hunt_holder_meta(session, [tok], now=now + timedelta(minutes=3))) == 0
        assert asyncio.run(refresh_sol_hunt_holder_meta(session, [tok], now=now + timedelta(minutes=6))) == 1
        assert tok.research.holder_count == 260
        assert pages_seen == [holders_mod.SOL_HOLDER_TAPE_PAGES]
        monkeypatch.setattr("launchfinder.research.holders.settings", SimpleNamespace(helius_api_key="", sol_holder_das_pages_per_hour=60))
        assert asyncio.run(refresh_sol_hunt_holder_meta(session, [tok], now=now + timedelta(minutes=12))) == 0


def test_sol_holder_refresh_is_fillable_budgeted_and_parks_on_429(monkeypatch):
    """10M-credit Helius Developer plan: refresh fillable / watch-line /
    2×-runner Sol books. Thin dust still skips. Hourly page budget and
    the 429 park stay so ingest keeps the key."""
    from types import SimpleNamespace

    import launchfinder.research.holders as holders_mod

    init_db()
    now = utcnow()
    with session_scope() as session:
        line = _tok(now)
        line.research.p_good = 0.91
        dust = Token(mint="TapeBar22222222222222222222222222222222222", symbol="SB", chain="sol", first_seen_at=now - timedelta(minutes=30), migrated_at=now - timedelta(minutes=30), source="poll")
        dust.research = Research(features_json="{}", p_good=0.04, holder_count=10, raw_json="{}")
        dust.outcome = Outcome(t0_mcap=69_000, max_mcap=70_000, last_mcap=1_800, last_liq=1_500)
        fillable = Token(mint="TapeBar44444444444444444444444444444444444", symbol="FS", chain="sol", first_seen_at=now - timedelta(minutes=30), migrated_at=now - timedelta(minutes=30), source="poll")
        fillable.research = Research(features_json="{}", p_good=0.1465, holder_count=10, raw_json="{}")
        fillable.outcome = Outcome(t0_mcap=69_000, max_mcap=640_000, last_mcap=640_000, last_liq=80_000)
        runner = Token(mint="TapeBar33333333333333333333333333333333333", symbol="RN", chain="sol", first_seen_at=now - timedelta(minutes=30), migrated_at=now - timedelta(minutes=30), source="poll")
        runner.research = Research(features_json="{}", p_good=0.12, holder_count=10, raw_json="{}")
        runner.outcome = Outcome(t0_mcap=69_000, max_mcap=210_000, last_mcap=200_000, last_liq=4_000, multiple=3.0)
        session.add_all([line, dust, fillable, runner])
        session.flush()

        assert holders_mod.sol_holder_refresh_due(dust.research, dust.outcome) is False
        assert holders_mod.sol_holder_refresh_due(fillable.research, fillable.outcome) is True
        assert holders_mod.sol_holder_refresh_due(runner.research, runner.outcome) is True

        calls: list[str] = []

        async def _fake_das(mint, creator, pool, *, max_pages=5):
            calls.append(mint)
            return {"holder_count": 300, "source": "helius"}

        holders_mod.reset_helius_budget()
        monkeypatch.setattr("launchfinder.research.holders._das_owners", _fake_das)
        # Budget for exactly one card this hour.
        monkeypatch.setattr("launchfinder.research.holders.settings", SimpleNamespace(helius_api_key="k", sol_holder_das_pages_per_hour=holders_mod.SOL_HOLDER_TAPE_PAGES))
        assert asyncio.run(refresh_sol_hunt_holder_meta(session, [dust, runner, fillable, line], now=now)) == 1
        assert calls == [line.mint]  # highest Entry first; dust never queued
        assert asyncio.run(refresh_sol_hunt_holder_meta(session, [dust, runner, fillable], now=now)) == 0  # budget spent
        assert dust.research.holder_count == 10 and fillable.research.holder_count == 10

        # Fresh hour: fillable first-sight (0.1465, fat book) now qualifies.
        holders_mod.reset_helius_budget()
        monkeypatch.setattr("launchfinder.research.holders.settings", SimpleNamespace(helius_api_key="k", sol_holder_das_pages_per_hour=60))
        assert asyncio.run(refresh_sol_hunt_holder_meta(session, [dust, fillable], now=now)) == 1
        assert calls[-1] == fillable.mint and dust.research.holder_count == 10

        # Fresh hour: the 2x runner qualifies even under the watch line on a thin book.
        holders_mod.reset_helius_budget()
        assert asyncio.run(refresh_sol_hunt_holder_meta(session, [dust, runner], now=now)) == 1
        assert calls[-1] == runner.mint and dust.research.holder_count == 10

        # A 429 parks the refresh; nothing is spent while capped.
        holders_mod._note_helius_status(429, "max usage reached")
        assert holders_mod.helius_capped() is True
        assert asyncio.run(refresh_sol_hunt_holder_meta(session, [line], now=now + timedelta(minutes=10))) == 0
        assert len(calls) == 3
        holders_mod.reset_helius_budget()
        assert holders_mod.helius_capped() is False


def test_sol_holder_refresh_gives_runners_more_das_pages(monkeypatch):
    """A 2× book spends 5 DAS pages so unique owners can leave a 2-page
    sample. Hourly cap stays 300. Entry is not lifted."""
    from types import SimpleNamespace

    import launchfinder.research.holders as holders_mod

    init_db()
    now = utcnow()
    with session_scope() as session:
        watch = _tok(now)
        watch.mint = "TapeBarWatch11111111111111111111111111111"
        watch.research.p_good = 0.1465
        watch.research.holder_count = 80
        watch.outcome.last_mcap = 70_000
        watch.outcome.multiple = 1.01
        runner = Token(
            mint="TapeBarTipp111111111111111111111111111111",
            symbol="TIP",
            chain="sol",
            first_seen_at=now - timedelta(minutes=30),
            migrated_at=now - timedelta(minutes=30),
            source="poll",
        )
        runner.research = Research(features_json="{}", p_good=0.1918, holder_count=522, raw_json="{}")
        runner.outcome = Outcome(t0_mcap=68_244, max_mcap=2_800_000, last_mcap=2_800_000, last_liq=80_000, multiple=41.0)
        session.add_all([watch, runner])
        session.flush()

        assert holders_mod.sol_holder_tape_pages(watch.research, watch.outcome) == holders_mod.SOL_HOLDER_TAPE_PAGES
        assert holders_mod.sol_holder_tape_pages(runner.research, runner.outcome) == holders_mod.SOL_HOLDER_TAPE_PAGES_RUNNER

        pages_by_mint: dict[str, int] = {}

        async def _fake_das(mint, creator, pool, *, max_pages=5):
            pages_by_mint[mint] = max_pages
            return {"holder_count": 1800, "source": "helius"}

        holders_mod.reset_helius_budget()
        monkeypatch.setattr("launchfinder.research.holders._das_owners", _fake_das)
        monkeypatch.setattr(
            "launchfinder.research.holders.settings",
            SimpleNamespace(helius_api_key="k", sol_holder_das_pages_per_hour=holders_mod.SOL_HOLDER_TAPE_PAGES_RUNNER),
        )
        assert asyncio.run(refresh_sol_hunt_holder_meta(session, [watch, runner], now=now)) == 1
        assert pages_by_mint == {runner.mint: holders_mod.SOL_HOLDER_TAPE_PAGES_RUNNER}
        assert runner.research.holder_count == 1800 and runner.research.p_good == 0.1918
        assert watch.research.holder_count == 80


def test_ingest_holder_stats_skip_das_and_use_public_rpc_while_helius_is_capped(monkeypatch):
    from types import SimpleNamespace

    import launchfinder.research.holders as holders_mod

    seen: list[tuple[str, str]] = []

    async def _fake_post(url, payload, *, headers=None, params=None, on_status=None):
        seen.append((payload["method"], url))
        if "helius" in url:
            if on_status is not None:
                on_status(429, "max usage reached")
            return None
        return {"result": {"value": [{"uiAmount": 60.0}, {"uiAmount": 40.0}]}}

    holders_mod.reset_helius_budget()
    monkeypatch.setattr("launchfinder.research.holders.post_json", _fake_post)
    monkeypatch.setattr(
        "launchfinder.research.holders.settings",
        SimpleNamespace(helius_api_key="k", rpc_url="https://mainnet.helius-rpc.com/?api-key=k", sol_holder_das_pages_per_hour=60),
    )
    try:
        # First call: DAS 429s, breaker trips, fallback re-routes to the public node.
        stats = asyncio.run(holders_mod.holder_stats("Mint1111111111111111111111111111111111111111"))
        assert holders_mod.helius_capped() is True
        assert stats["source"] == "rpc_largest" and stats["top1_pct"] == 60.0
        assert seen[0] == ("getTokenAccounts", "https://mainnet.helius-rpc.com/?api-key=k")
        assert seen[-1] == ("getTokenLargestAccounts", holders_mod.PUBLIC_SOL_RPC)
        # While capped, ingest does not spend a DAS page at all.
        seen.clear()
        asyncio.run(holders_mod.holder_stats("Mint2222222222222222222222222222222222222222"))
        assert [m for m, _ in seen] == ["getTokenLargestAccounts"]
    finally:
        holders_mod.reset_helius_budget()


def test_rh_leftover_hunt_card_writes_bar_without_unpark():
    """ROB / TYPING class: multi-day RH leftover on Hunt still gets a minute bar."""
    from launchfinder.models import HuntCard
    from launchfinder.scoring.hunt_tape import apply_hunt_tape_market

    init_db()
    now = utcnow()
    with session_scope() as session:
        tok = Token(
            mint="0xe1041b2adb0119716637bdd2a1d1a98183d9341e",
            symbol="ROB",
            name="robs.wtf",
            chain="robinhood",
            source="rh_bitquery",
            is_historical=True,
            first_seen_at=now - timedelta(hours=88),
            migrated_at=now - timedelta(hours=88),
            pool_address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        tok.research = Research(features_json="{}", p_good=0.5, holder_count=200, raw_json="{}")
        tok.outcome = Outcome(t0_mcap=1_742_623, max_mcap=15_000_000, last_mcap=15_143_643, last_liq=250_591)
        session.add(tok)
        session.flush()
        session.add(
            HuntCard(
                chain="robinhood",
                mint=tok.mint,
                token_id=tok.id,
                first_seen_at=tok.first_seen_at,
                launched_at=tok.migrated_at,
                entry_p=0.5,
                t0_mcap=1_742_623,
                last_mcap=15_143_643,
                last_liq=250_591,
            )
        )
        session.flush()
        market = {
            "mcap_usd": 15_200_000.0,
            "liquidity_usd": 251_000.0,
            "volume_h1": 40_000.0,
            "price_usd": 0.015,
            "pair_address": tok.pool_address,
        }
        assert apply_hunt_tape_market(session, tok, market, now=now) is True
        assert tok.is_historical is True
        bars = session.query(TapeBar).filter(TapeBar.mint == tok.mint).all()
        assert len(bars) == 1
        assert bars[0].mcap_usd == 15_200_000.0


def test_tape_bar_survives_upsert_lock_in_sibling_savepoint():
    """Hunt upsert LockNotAvailable must not roll back the leftover bar."""
    from sqlalchemy.exc import OperationalError

    from launchfinder.models import HuntCard
    from launchfinder.scoring.hunt import recover_session_after_lock
    from launchfinder.scoring.hunt_tape import write_tape_bar

    init_db()
    now = utcnow().replace(second=0, microsecond=0)
    with session_scope() as session:
        tok = Token(
            mint="0xfb1050682e982036c4e802f49355cc23fad53e6c",
            symbol="TYPING",
            chain="robinhood",
            source="rh_bitquery",
            first_seen_at=now - timedelta(days=11),
            migrated_at=now - timedelta(days=11),
        )
        tok.research = Research(features_json="{}", p_good=0.01, holder_count=80, raw_json="{}")
        tok.outcome = Outcome(t0_mcap=49_611, last_mcap=2_202_920, last_liq=516_424)
        session.add(tok)
        session.flush()
        session.add(
            HuntCard(
                chain="robinhood",
                mint=tok.mint,
                token_id=tok.id,
                first_seen_at=tok.first_seen_at,
                last_mcap=2_202_920,
                last_liq=516_424,
            )
        )
        session.flush()
        try:
            with session.begin_nested():
                raise OperationalError("INSERT", {}, Exception("LockNotAvailable"))
        except OperationalError as exc:
            assert recover_session_after_lock(session, exc) is True
        with session.begin_nested():
            assert write_tape_bar(
                session,
                tok,
                {"volume_h1": 1000, "price_usd": 0.002},
                mcap=2_202_920,
                liq=516_424,
                now=now,
            )
        assert session.query(TapeBar).filter(TapeBar.mint == tok.mint).count() == 1
