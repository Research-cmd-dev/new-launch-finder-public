import asyncio
from datetime import timedelta

from launchfinder.db import init_db, session_scope
from launchfinder.models import Outcome, Research, Token, utcnow
from launchfinder.scoring import outcomes as outcomes_mod


def test_young_tokens_are_always_refreshed(monkeypatch):
    init_db()
    refreshed: list[str] = []

    async def fake_market(mint, chain="sol"):
        refreshed.append(mint)
        return {"mcap_usd": 100_000.0, "liquidity_usd": 20_000.0, "price_usd": 0.0001}

    async def fake_coin(mint):
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market)
    monkeypatch.setattr(outcomes_mod.pumpfun, "get_coin", fake_coin)

    with session_scope() as session:
        # 60 old pending outcomes that used to monopolize the window
        for i in range(60):
            t = Token(
                mint=f"OldPending{i:03d}Mint",
                symbol=f"O{i}",
                source="poll",
                first_seen_at=utcnow() - timedelta(hours=20),
                migrated_at=utcnow() - timedelta(hours=20),
            )
            t.research = Research(p_good=0.4, features_json="{}")
            session.add(t)
            session.flush()
            session.add(Outcome(token_id=t.id, t0_mcap=69_000.0, max_mcap=69_000.0))
        # one young token whose t15m snapshot is due
        young = Token(
            mint="YoungTokenMint111",
            symbol="YNG",
            source="poll",
            first_seen_at=utcnow() - timedelta(minutes=16),
            migrated_at=utcnow() - timedelta(minutes=16),
        )
        young.research = Research(p_good=0.5, features_json="{}")
        session.add(young)
        session.flush()
        session.add(Outcome(token_id=young.id, t0_mcap=69_000.0, max_mcap=69_000.0))

    with session_scope() as session:
        asyncio.run(outcomes_mod.refresh_outcomes(session))

    assert "YoungTokenMint111" in refreshed


def test_sol_leftover_fdv_gets_reserved_refresh_slots(monkeypatch):
    # Live NIKE: last $17.5M / t0 $69k sat on hunt while 60 old
    # unlabeled Sol books ate the random window.
    init_db()
    refreshed: list[str] = []

    async def fake_market(mint, chain="sol"):
        refreshed.append(mint)
        return {"mcap_usd": 2_673.0, "liquidity_usd": 2_623.0, "price_usd": 0.0001}

    async def fake_coin(mint):
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market)
    monkeypatch.setattr(outcomes_mod.pumpfun, "get_coin", fake_coin)

    with session_scope() as session:
        for i in range(60):
            t = Token(
                mint=f"OldSolPend{i:03d}Mint1",
                symbol=f"O{i}",
                source="poll",
                chain="sol",
                first_seen_at=utcnow() - timedelta(hours=10),
                migrated_at=utcnow() - timedelta(hours=10),
            )
            t.research = Research(p_good=0.3, features_json="{}")
            session.add(t)
            session.flush()
            session.add(Outcome(token_id=t.id, t0_mcap=69_000.0, max_mcap=69_000.0, last_mcap=70_000.0))
        nike = Token(
            mint="pr9pvSiNsNwHBkbBVn3YRYETKFnLxUMaW9Dtf7hpump",
            symbol="NIKE",
            source="poll",
            chain="sol",
            first_seen_at=utcnow() - timedelta(hours=8),
            migrated_at=utcnow() - timedelta(hours=8),
        )
        nike.research = Research(p_good=0.25, features_json="{}")
        session.add(nike)
        session.flush()
        session.add(
            Outcome(
                token_id=nike.id,
                t0_mcap=69_000.0,
                max_mcap=17_588_629.0,
                last_mcap=17_588_629.0,
                last_liq=356_511.0,
                multiple=1.0,
            )
        )

    with session_scope() as session:
        asyncio.run(outcomes_mod.refresh_outcomes(session))

    assert "pr9pvSiNsNwHBkbBVn3YRYETKFnLxUMaW9Dtf7hpump" in refreshed


def test_sol_doing_well_labeled_gets_reserved_post_label_slots(monkeypatch):
    # Live 05:00: MSFT last $2.16M / t0 $165k sat on Doing well
    # while Dex PumpSwap was $2.6k. leftover-FDV chairs only hit
    # last/t0>80. Reserved 2×+ chairs rewrite last. Do not change
    # leftover chairs. Do not leftover-sort 2×+.
    init_db()
    refreshed: list[str] = []

    async def fake_market(mint, chain="sol"):
        refreshed.append(mint)
        return {"mcap_usd": 2_657.0, "liquidity_usd": 2_957.0, "price_usd": 0.0001}

    async def fake_coin(mint):
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market)
    monkeypatch.setattr(outcomes_mod.pumpfun, "get_coin", fake_coin)

    msft = "28ZHuzqJwtg97KhJMSFTdwtickstest111111111"
    with session_scope() as session:
        for i in range(40):
            t = Token(
                mint=f"OldLabSol{i:03d}Mint11",
                symbol=f"L{i}",
                source="poll",
                chain="sol",
                first_seen_at=utcnow() - timedelta(hours=10),
                migrated_at=utcnow() - timedelta(hours=10),
            )
            t.research = Research(p_good=0.4, features_json="{}")
            session.add(t)
            session.flush()
            session.add(
                Outcome(
                    token_id=t.id,
                    t0_mcap=69_000.0,
                    max_mcap=76_000.0,
                    last_mcap=76_000.0,
                    last_liq=12_000.0,
                    multiple=1.1,
                    label=1,
                )
            )
        tok = Token(
            mint=msft,
            symbol="MSFT",
            source="poll",
            chain="sol",
            first_seen_at=utcnow() - timedelta(hours=8),
            migrated_at=utcnow() - timedelta(hours=8),
        )
        tok.research = Research(p_good=0.49, features_json="{}")
        session.add(tok)
        session.flush()
        session.add(
            Outcome(
                token_id=tok.id,
                t0_mcap=164_509.0,
                max_mcap=2_163_579.0,
                last_mcap=2_163_579.0,
                last_liq=80_000.0,
                multiple=13.2,
                label=1,
            )
        )

    with session_scope() as session:
        asyncio.run(outcomes_mod.refresh_outcomes(session))
        row = session.query(Token).filter(Token.mint == msft).one()
        assert abs(row.outcome.last_mcap - 2_657.0) < 1e-6
        assert row.outcome.last_liq == 2_957.0

    assert msft in refreshed


def test_sol_post_label_keeps_last_liq_on_dex_miss(monkeypatch):
    # Live 10:00: Laptop GsewXp last $209k / label=1 sat last_liq=0
    # after a post tick with mcap and no liq. Runners hid it from
    # Doing well. A Dex miss is not a dead book.
    init_db()
    refreshed: list[str] = []

    async def fake_market(mint, chain="sol"):
        refreshed.append(mint)
        return {"mcap_usd": 209_003.0, "liquidity_usd": 0.0, "price_usd": 0.0001}

    async def fake_coin(mint):
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market)
    monkeypatch.setattr(outcomes_mod.pumpfun, "get_coin", fake_coin)

    laptop = "GsewXpKeepLiqMint1111111111111111111111"
    with session_scope() as session:
        tok = Token(
            mint=laptop,
            symbol="Laptop",
            source="poll",
            chain="sol",
            first_seen_at=utcnow() - timedelta(hours=3),
            migrated_at=utcnow() - timedelta(hours=3),
        )
        tok.research = Research(p_good=0.87, holder_count=344, features_json="{}")
        session.add(tok)
        session.flush()
        session.add(
            Outcome(
                token_id=tok.id,
                t0_mcap=40_834.0,
                max_mcap=209_003.0,
                last_mcap=209_003.0,
                last_liq=35_387.0,
                multiple=5.12,
                label=1,
            )
        )

    with session_scope() as session:
        asyncio.run(outcomes_mod.refresh_outcomes(session))
        row = session.query(Token).filter(Token.mint == laptop).one()
        assert abs(row.outcome.last_liq - 35_387.0) < 1e-6
        assert abs(row.outcome.last_mcap - 209_003.0) < 1e-6

    assert laptop in refreshed


def test_sol_labeled_liq_miss_restores_last_liq(monkeypatch):
    # Live 10:00: Laptop GsewXp last_liq=0 was excluded from the
    # last_liq≥$800 labeled pool, so Dex $43k never wrote back.
    init_db()
    refreshed: list[str] = []

    async def fake_market(mint, chain="sol"):
        refreshed.append(mint)
        return {"mcap_usd": 253_678.0, "liquidity_usd": 43_771.0, "price_usd": 0.0001}

    async def fake_coin(mint):
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market)
    monkeypatch.setattr(outcomes_mod.pumpfun, "get_coin", fake_coin)

    laptop = "GsewXpRestoreLiqMint1111111111111111111"
    with session_scope() as session:
        tok = Token(
            mint=laptop,
            symbol="Laptop",
            source="poll",
            chain="sol",
            first_seen_at=utcnow() - timedelta(hours=3),
            migrated_at=utcnow() - timedelta(hours=3),
        )
        tok.research = Research(p_good=0.87, holder_count=344, features_json="{}")
        session.add(tok)
        session.flush()
        session.add(
            Outcome(
                token_id=tok.id,
                t0_mcap=40_834.0,
                max_mcap=253_678.0,
                last_mcap=209_003.0,
                last_liq=0.0,
                multiple=5.12,
                label=1,
            )
        )

    with session_scope() as session:
        asyncio.run(outcomes_mod.refresh_outcomes(session))
        row = session.query(Token).filter(Token.mint == laptop).one()
        assert abs(row.outcome.last_liq - 43_771.0) < 1e-6

    assert laptop in refreshed


def test_sol_near_well_laptop_gets_reserved_tick(monkeypatch):
    # Live 09:10: Laptop last $77k / 1.89× / Dex PumpSwap $279k.
    # 2×+ chairs skip under 2.0; leftover-FDV chairs need last/t0>80.
    # Do not change leftover chairs. Do not leftover-sort 2×+.
    init_db()
    refreshed: list[str] = []

    async def fake_market(mint, chain="sol"):
        refreshed.append(mint)
        return {"mcap_usd": 279_016.0, "liquidity_usd": 45_587.0, "price_usd": 0.0001}

    async def fake_coin(mint):
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market)
    monkeypatch.setattr(outcomes_mod.pumpfun, "get_coin", fake_coin)

    laptop = "GsewXpKnn1ei7YfwfcBgdF7B7giXjfLemVjnRPyupump"
    with session_scope() as session:
        for i in range(60):
            t = Token(
                mint=f"OldNearPend{i:03d}Mint1",
                symbol=f"O{i}",
                source="poll",
                chain="sol",
                first_seen_at=utcnow() - timedelta(hours=10),
                migrated_at=utcnow() - timedelta(hours=10),
            )
            t.research = Research(p_good=0.4, features_json="{}")
            session.add(t)
            session.flush()
            session.add(
                Outcome(
                    token_id=t.id,
                    t0_mcap=69_000.0,
                    max_mcap=76_000.0,
                    last_mcap=76_000.0,
                    last_liq=12_000.0,
                    multiple=1.1,
                )
            )
        tok = Token(
            mint=laptop,
            symbol="Laptop",
            source="poll",
            chain="sol",
            first_seen_at=utcnow() - timedelta(hours=3),
            migrated_at=utcnow() - timedelta(hours=3),
        )
        tok.research = Research(p_good=0.87, holder_count=344, features_json="{}")
        session.add(tok)
        session.flush()
        session.add(
            Outcome(
                token_id=tok.id,
                t0_mcap=40_834.0,
                max_mcap=77_269.0,
                last_mcap=77_269.0,
                last_liq=22_201.0,
                multiple=1.89,
            )
        )

    with session_scope() as session:
        asyncio.run(outcomes_mod.refresh_outcomes(session))
        row = session.query(Token).filter(Token.mint == laptop).one()
        assert abs(row.outcome.last_mcap - 279_016.0) < 1e-6
        assert abs(row.outcome.last_liq - 45_587.0) < 1e-6

    assert laptop in refreshed


def test_sol_doing_well_skips_historical_so_namekcoin_gets_a_tick(monkeypatch):
    # Live 06:40: NamekCoin last $713k / Dex $2.3k sat on Doing well.
    # Historical leftovers (fih $2.1M) ate the 8 chairs and never
    # rewrote. Doing well already drops historical Sol. Do not change
    # leftover chairs. Do not leftover-sort 2×+.
    init_db()
    refreshed: list[str] = []

    async def fake_market(mint, chain="sol"):
        refreshed.append(mint)
        return {"mcap_usd": 2_282.0, "liquidity_usd": 2_563.0, "price_usd": 0.0001}

    async def fake_coin(mint):
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market)
    monkeypatch.setattr(outcomes_mod.pumpfun, "get_coin", fake_coin)

    namek = "5oncwp2yzdNPox3nF1q7Uwd39b9F17oPZPtdE6LYpump"
    fih = "9ApS1vkNYMrdSibKoCud7gYX99soSBe9KHjoBWhDpump"
    with session_scope() as session:
        for i in range(8):
            t = Token(
                mint=f"HistFat{i:03d}SolMint1",
                symbol=f"H{i}",
                source="gmgn_trenches",
                chain="sol",
                is_historical=True,
                first_seen_at=utcnow() - timedelta(days=5),
                migrated_at=utcnow() - timedelta(days=5),
            )
            t.research = Research(p_good=0.5, features_json="{}")
            session.add(t)
            session.flush()
            session.add(
                Outcome(
                    token_id=t.id,
                    t0_mcap=162_089.0,
                    max_mcap=6_691_431.0,
                    last_mcap=2_118_014.0,
                    last_liq=124_934.0,
                    multiple=13.1,
                    label=1,
                )
            )
        tok = Token(
            mint=namek,
            symbol="NamekCoin",
            source="poll",
            chain="sol",
            is_historical=False,
            first_seen_at=utcnow() - timedelta(hours=4),
            migrated_at=utcnow() - timedelta(hours=4),
        )
        tok.research = Research(p_good=0.11, features_json="{}")
        session.add(tok)
        session.flush()
        session.add(
            Outcome(
                token_id=tok.id,
                t0_mcap=48_755.0,
                max_mcap=838_339.0,
                last_mcap=712_562.0,
                last_liq=74_933.0,
                multiple=14.6,
                label=1,
            )
        )
        parked = Token(
            mint=fih,
            symbol="fih",
            source="gmgn_trenches",
            chain="sol",
            is_historical=True,
            first_seen_at=utcnow() - timedelta(days=5),
            migrated_at=utcnow() - timedelta(days=5),
        )
        parked.research = Research(p_good=0.53, features_json="{}")
        session.add(parked)
        session.flush()
        session.add(
            Outcome(
                token_id=parked.id,
                t0_mcap=162_089.0,
                max_mcap=6_691_431.0,
                last_mcap=2_118_014.0,
                last_liq=124_934.0,
                multiple=41.3,
                label=1,
            )
        )

    with session_scope() as session:
        asyncio.run(outcomes_mod.refresh_outcomes(session))
        row = session.query(Token).filter(Token.mint == namek).one()
        assert abs(row.outcome.last_mcap - 2_282.0) < 1e-6
        parked = session.query(Token).filter(Token.mint == fih).one()
        assert parked.outcome.last_mcap == 2_118_014.0

    assert namek in refreshed
    assert fih not in refreshed


def test_robinhood_unlabeled_get_reserved_refresh_slots(monkeypatch):
    init_db()
    refreshed: list[str] = []

    async def fake_market(mint, chain="sol"):
        refreshed.append(mint)
        return {"mcap_usd": 40_000.0, "liquidity_usd": 15_000.0, "price_usd": 0.0001}

    async def fake_coin(mint):
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market)
    monkeypatch.setattr(outcomes_mod.pumpfun, "get_coin", fake_coin)

    with session_scope() as session:
        for i in range(60):
            t = Token(
                mint=f"SolOld{i:03d}Mint1111",
                symbol=f"S{i}",
                source="poll",
                chain="sol",
                first_seen_at=utcnow() - timedelta(hours=10),
                migrated_at=utcnow() - timedelta(hours=10),
            )
            t.research = Research(p_good=0.3, features_json="{}")
            session.add(t)
            session.flush()
            session.add(Outcome(token_id=t.id, t0_mcap=69_000.0, max_mcap=69_000.0))
        rh = Token(
            mint="0xrhbricked111111111111111111111111111111",
            symbol="BRICKED",
            source="poll",
            chain="robinhood",
            first_seen_at=utcnow() - timedelta(hours=2),
            migrated_at=utcnow() - timedelta(hours=2),
        )
        rh.research = Research(p_good=0.5, features_json="{}")
        session.add(rh)
        session.flush()
        session.add(Outcome(token_id=rh.id, t0_mcap=20_000.0, max_mcap=48_000.0, multiple=2.4))

    with session_scope() as session:
        asyncio.run(outcomes_mod.refresh_outcomes(session))

    assert "0xrhbricked111111111111111111111111111111" in refreshed


def test_robinhood_six_hour_due_tokens_beat_random_solana(monkeypatch):
    init_db()
    from launchfinder.models import Outcome as _Outcome
    from launchfinder.models import Research as _Research
    from launchfinder.models import Snapshot as _Snapshot
    from launchfinder.models import Token as _Token

    with session_scope() as session:
        session.query(_Snapshot).delete()
        session.query(_Outcome).delete()
        session.query(_Research).delete()
        session.query(_Token).delete()
    refreshed: list[str] = []

    async def fake_market(mint, chain="sol"):
        refreshed.append(mint)
        return {"mcap_usd": 18_000.0, "liquidity_usd": 400.0, "price_usd": 0.0001}

    async def fake_coin(mint):
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market)
    monkeypatch.setattr(outcomes_mod.pumpfun, "get_coin", fake_coin)

    with session_scope() as session:
        for i in range(60):
            t = Token(
                mint=f"SolStarve{i:03d}Mint11",
                symbol=f"S{i}",
                source="poll",
                chain="sol",
                first_seen_at=utcnow() - timedelta(hours=10),
                migrated_at=utcnow() - timedelta(hours=10),
            )
            t.research = Research(p_good=0.3, features_json="{}")
            session.add(t)
            session.flush()
            session.add(Outcome(token_id=t.id, t0_mcap=69_000.0, max_mcap=69_000.0))
        due = []
        for i in range(12):
            mint = f"0xrhdue{i:02d}000000000000000000000000000000"
            t = Token(
                mint=mint,
                symbol=f"DUE{i}",
                source="rh_trenches",
                chain="robinhood",
                first_seen_at=utcnow() - timedelta(hours=7, minutes=i),
                migrated_at=utcnow() - timedelta(hours=7, minutes=i),
            )
            t.research = Research(p_good=0.3, features_json="{}")
            session.add(t)
            session.flush()
            session.add(Outcome(token_id=t.id, t0_mcap=20_000.0, max_mcap=20_000.0, last_liq=400.0))
            due.append(mint)

    with session_scope() as session:
        asyncio.run(outcomes_mod.refresh_outcomes(session))

    for mint in due:
        assert mint in refreshed
    # Leftover $400 LP / no volume is quiet-retired, not rug-labeled.
    with session_scope() as session:
        rh = (
            session.query(Token)
            .filter(Token.chain == "robinhood", Token.symbol.like("DUE%"))
            .all()
        )
        assert len(rh) >= 12
        assert all(t.is_historical for t in rh)
        labeled = (
            session.query(Outcome)
            .join(Token, Token.id == Outcome.token_id)
            .filter(Token.symbol.like("DUE%"), Outcome.label.is_not(None))
            .count()
        )
        assert labeled == 0


def test_quiet_robinhood_leaves_live_desk_without_a_rug_label():
    from launchfinder.scoring.outcomes import HORIZONS, retire_quiet_robinhood

    quiet = Token(mint="0xquiet0000000000000000000000000000000001", chain="robinhood", is_historical=False)
    flat = Outcome(multiple=1.0)
    assert retire_quiet_robinhood(quiet, flat, HORIZONS["t6h"] + timedelta(minutes=5), 0.0) is True
    assert quiet.is_historical is True
    assert flat.label is None

    climber = Token(mint="0xclimb0000000000000000000000000000000001", chain="robinhood", is_historical=False)
    moving = Outcome(multiple=2.44)
    assert retire_quiet_robinhood(climber, moving, HORIZONS["t6h"] + timedelta(hours=1), 0.0) is False
    assert climber.is_historical is False

    flowing = Token(mint="0xflow00000000000000000000000000000000001", chain="robinhood", is_historical=False)
    assert retire_quiet_robinhood(flowing, Outcome(multiple=1.0, last_liq=20_000.0), HORIZONS["t6h"] + timedelta(hours=1), 2_400.0) is False
    assert flowing.is_historical is False

    # Live TAM: leftover $204 LP still prints $10k vol, so the 6h+low-vol
    # rule never fired. Ghost books leave after 2h even with residual vol.
    leftover = Token(mint="0xtamleftover000000000000000000000000001", chain="robinhood", is_historical=False)
    leftover_o = Outcome(multiple=1.0, last_liq=204.5)
    assert retire_quiet_robinhood(leftover, leftover_o, timedelta(hours=1), 10_886.0) is False
    assert leftover.is_historical is False
    assert retire_quiet_robinhood(leftover, leftover_o, timedelta(hours=2, minutes=5), 10_886.0) is True
    assert leftover.is_historical is True
    assert leftover_o.label is None

    live_young = Token(mint="0xliveyoung00000000000000000000000000001", chain="robinhood", is_historical=False)
    assert retire_quiet_robinhood(live_young, Outcome(multiple=1.0, last_liq=20_000.0), timedelta(hours=2, minutes=5), 0.0) is False
    assert live_young.is_historical is False

    # Live SSB-class: Dex missed this poll but last_liq is still a real book.
    # Fabricating vol=0 used to park it at 6h (log: "no live book" / mult=1.00).
    dex_miss = Token(mint="0xssbdexmiss000000000000000000000000001", chain="robinhood", is_historical=False)
    assert retire_quiet_robinhood(
        dex_miss,
        Outcome(multiple=1.0, last_liq=30_000.0),
        HORIZONS["t6h"] + timedelta(minutes=5),
        0.0,
        seen_book=False,
    ) is False
    assert dex_miss.is_historical is False

    leftover_miss = Token(mint="0xleftovermiss0000000000000000000000001", chain="robinhood", is_historical=False)
    leftover_o = Outcome(multiple=1.0, last_liq=148.0)
    assert retire_quiet_robinhood(
        leftover_miss, leftover_o, timedelta(hours=2, minutes=5), 0.0, seen_book=False
    ) is True
    assert leftover_miss.is_historical is True

    # Catch-up: trench open time is 3h ago so age>=2h, but we just ingested
    # (first_seen now, last_liq still 0). Do not park before Dex refreshes.
    just_in = Token(
        mint="0xzazucatchup000000000000000000000000001",
        chain="robinhood",
        is_historical=False,
        first_seen_at=utcnow(),
    )
    assert retire_quiet_robinhood(
        just_in, Outcome(multiple=1.0, last_liq=0.0), timedelta(hours=3), 0.0
    ) is False
    assert just_in.is_historical is False

    aged_in = Token(
        mint="0xzazuaged000000000000000000000000000001",
        chain="robinhood",
        is_historical=False,
        first_seen_at=utcnow() - timedelta(hours=1),
    )
    assert retire_quiet_robinhood(
        aged_in, Outcome(multiple=1.0, last_liq=0.0), timedelta(hours=3), 0.0
    ) is True
    assert aged_in.is_historical is True

    labeled = Token(mint="0xssblabeled000000000000000000000000001", chain="robinhood", is_historical=False)
    assert retire_quiet_robinhood(
        labeled, Outcome(multiple=1.0, last_liq=100.0, label=1), HORIZONS["t6h"], 0.0
    ) is False
    assert labeled.is_historical is False

    sol = Token(mint="SolQuietMint1111111111111111111111111111", chain="sol", is_historical=False)
    assert retire_quiet_robinhood(sol, Outcome(multiple=1.0), HORIZONS["t6h"] + timedelta(hours=1), 0.0) is False
    assert sol.is_historical is False


def test_repair_rh_quiet_ghosts_retires_aiaiai_without_a_refresh_slot():
    from launchfinder.scoring.outcomes import repair_rh_quiet_ghosts

    init_db()
    with session_scope() as session:
        ghost = Token(
            mint="0xaiaiaileftover000000000000000000000001",
            symbol="AIAIAI",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow() - timedelta(hours=3),
        )
        young = Token(
            mint="0xtamyoung000000000000000000000000000001",
            symbol="TAM",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow() - timedelta(hours=1),
        )
        live = Token(
            mint="0xbricklive00000000000000000000000000001",
            symbol="BRICKED",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow() - timedelta(hours=9),
        )
        session.add_all([ghost, young, live])
        session.flush()
        session.add(Outcome(token_id=ghost.id, t0_mcap=40_000.0, max_mcap=40_000.0, multiple=1.0, last_liq=148.67))
        session.add(Outcome(token_id=young.id, t0_mcap=40_000.0, max_mcap=40_000.0, multiple=1.0, last_liq=204.5))
        session.add(Outcome(token_id=live.id, t0_mcap=41_774.0, max_mcap=101_919.0, multiple=2.44, last_liq=30_000.0))
        session.flush()
        assert repair_rh_quiet_ghosts(session) >= 1
        assert ghost.is_historical is True
        assert young.is_historical is False
        assert live.is_historical is False


def test_restore_rh_ingest_grace_unparks_just_seen_catchup():
    from launchfinder.scoring.outcomes import restore_rh_ingest_grace

    init_db()
    with session_scope() as session:
        zazu = Token(
            mint="0xzazurestore000000000000000000000000001",
            symbol="ZAZU",
            source="rh_trenches",
            chain="robinhood",
            is_historical=True,
            first_seen_at=utcnow(),
        )
        old = Token(
            mint="0xaiaiaistale000000000000000000000000001",
            symbol="AIAIAI",
            source="rh_trenches",
            chain="robinhood",
            is_historical=True,
            first_seen_at=utcnow() - timedelta(hours=3),
        )
        session.add_all([zazu, old])
        session.flush()
        assert restore_rh_ingest_grace(session) >= 1
        assert zazu.is_historical is False
        assert old.is_historical is True


def test_rh_dex_miss_quiet_retires_and_ghost_peak_is_stripped(monkeypatch):
    # Live LIBERI/ONLY: Dex miss (0/0/0) never entered the mcap>0 path, so
    # they sat on the live desk past 6h. STLSPCXMTGIN: leftover $28M pair
    # with $0.10 vol minted a 710x that blocked quiet-retire.
    init_db()
    refreshed: list[str] = []

    async def fake_market(mint, chain="sol"):
        refreshed.append(mint)
        if "ghostpeak" in mint:
            return {"mcap_usd": 28_431_114.0, "liquidity_usd": 28_431_114.0, "volume_h1": 0.1, "price_usd": 1.0}
        return {}

    async def fake_coin(mint):
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market)
    monkeypatch.setattr(outcomes_mod.pumpfun, "get_coin", fake_coin)

    from launchfinder.models import Snapshot

    with session_scope() as session:
        miss = Token(
            mint="0xliberimiss0000000000000000000000000001",
            symbol="LIBERI",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow() - timedelta(hours=12),
            migrated_at=utcnow() - timedelta(hours=12),
        )
        miss.research = Research(p_good=0.28, holder_count=4, features_json="{}")
        session.add(miss)
        session.flush()
        session.add(Outcome(token_id=miss.id, t0_mcap=40_000.0, max_mcap=40_000.0, multiple=0.0, last_liq=0.0))
        session.add(Snapshot(token_id=miss.id, kind="t0", mcap_usd=0.0, liquidity_usd=0.0, volume_h1=0.0))

        peak = Token(
            mint="0xstlsghostpeak00000000000000000000000001",
            symbol="STLSPCXMTGIN",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow() - timedelta(hours=12),
            migrated_at=utcnow() - timedelta(hours=12),
        )
        peak.research = Research(p_good=0.02, holder_count=5, features_json="{}")
        session.add(peak)
        session.flush()
        session.add(Outcome(token_id=peak.id, t0_mcap=40_000.0, max_mcap=28_431_114.0, multiple=710.78, last_liq=28_431_114.0))
        session.add(Snapshot(token_id=peak.id, kind="t0", mcap_usd=0.0, liquidity_usd=28_431_112.0, volume_h1=0.1))
        session.add(Snapshot(token_id=peak.id, kind="t6h", mcap_usd=28_431_114.0, liquidity_usd=28_431_114.0, volume_h1=0.0))

        brick = Token(
            mint="0xbrickedlive000000000000000000000000001",
            symbol="BRICKED",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow() - timedelta(hours=9),
            migrated_at=utcnow() - timedelta(hours=9),
        )
        brick.research = Research(p_good=0.54, holder_count=176, features_json="{}")
        session.add(brick)
        session.flush()
        session.add(Outcome(token_id=brick.id, t0_mcap=41_774.0, max_mcap=101_919.0, multiple=2.44, last_liq=16_896.0))
        session.add(Snapshot(token_id=brick.id, kind="t0", mcap_usd=41_774.0, liquidity_usd=22_502.0, volume_h1=14_730.0))

        # Live SSB-class: 6h+, last_liq still a real book, Dex miss this poll.
        # Fabricating vol=0 used to quiet-retire it off the desk.
        ssb = Token(
            mint="0xssbdexmisslive000000000000000000000001",
            symbol="SSB",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow() - timedelta(hours=6, minutes=5),
            migrated_at=utcnow() - timedelta(hours=6, minutes=5),
        )
        ssb.research = Research(p_good=0.51, holder_count=205, features_json="{}")
        session.add(ssb)
        session.flush()
        session.add(Outcome(token_id=ssb.id, t0_mcap=22_840.0, max_mcap=22_840.0, multiple=1.0, last_liq=30_000.0))
        session.add(Snapshot(token_id=ssb.id, kind="t0", mcap_usd=22_840.0, liquidity_usd=30_000.0, volume_h1=40_000.0))

    # BRICKED needs a live book so the ghost-tick branch does not catch it.
    async def fake_market2(mint, chain="sol"):
        refreshed.append(mint)
        if "brickedlive" in mint:
            return {"mcap_usd": 101_919.0, "liquidity_usd": 16_896.0, "volume_h1": 4_200.0, "price_usd": 0.0001}
        if "ghostpeak" in mint:
            return {"mcap_usd": 28_431_114.0, "liquidity_usd": 28_431_114.0, "volume_h1": 0.1, "price_usd": 1.0}
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market2)

    with session_scope() as session:
        asyncio.run(outcomes_mod.refresh_outcomes(session))
        miss = session.query(Token).filter(Token.mint == "0xliberimiss0000000000000000000000000001").one()
        peak = session.query(Token).filter(Token.mint == "0xstlsghostpeak00000000000000000000000001").one()
        brick = session.query(Token).filter(Token.mint == "0xbrickedlive000000000000000000000000001").one()
        ssb = session.query(Token).filter(Token.mint == "0xssbdexmisslive000000000000000000000001").one()
        miss_o = session.query(Outcome).filter(Outcome.token_id == miss.id).one()
        peak_o = session.query(Outcome).filter(Outcome.token_id == peak.id).one()
        brick_o = session.query(Outcome).filter(Outcome.token_id == brick.id).one()
        assert miss.is_historical is True
        assert miss_o.label is None
        assert peak.is_historical is True
        assert peak_o.max_mcap == 40_000.0
        assert peak_o.multiple <= 1.01
        assert peak_o.label is None
        assert brick.is_historical is False
        assert brick_o.multiple >= 2.0
        assert brick_o.t0_mcap == 41_774.0
        assert ssb.is_historical is False


def test_rh_quiet_retired_24h_keeps_live_t0(monkeypatch):
    init_db()

    async def fake_market(mint, chain="sol"):
        return {}

    async def fake_coin(mint):
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market)
    monkeypatch.setattr(outcomes_mod.pumpfun, "get_coin", fake_coin)

    with session_scope() as session:
        fat = Token(
            mint="0xfatquiet24h000000000000000000000000001",
            symbol="FAT",
            source="rh_trenches",
            chain="robinhood",
            is_historical=True,
            first_seen_at=utcnow() - timedelta(hours=25),
            migrated_at=utcnow() - timedelta(hours=25),
        )
        fat.research = Research(p_good=0.54, holder_count=40, features_json="{}")
        session.add(fat)
        session.flush()
        session.add(Outcome(token_id=fat.id, t0_mcap=20_364.0, max_mcap=20_364.0, multiple=1.0, last_liq=400.0))

    with session_scope() as session:
        asyncio.run(outcomes_mod.refresh_outcomes(session))
        fat = session.query(Token).filter(Token.mint == "0xfatquiet24h000000000000000000000000001").one()
        out = session.query(Outcome).filter(Outcome.token_id == fat.id).one()
        assert out.label == 0
        assert out.t0_mcap == 20_364.0
        assert abs((out.multiple or 0) - 1.0) < 0.05
        # Leftover $400 LP / no live snaps: do not invent a 24h print.
        assert out.t24h_mcap is None


def test_rh_dex_miss_24h_seeds_t24h_from_last_live_snap(monkeypatch):
    # Live FAT-class $20k book: Dex miss at the 24h judgment used to leave
    # t24h empty, and paper closed the flat book as a -85% drained-pool rug.
    init_db()

    async def fake_market(mint, chain="sol"):
        return {}

    async def fake_coin(mint):
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market)
    monkeypatch.setattr(outcomes_mod.pumpfun, "get_coin", fake_coin)

    from launchfinder.models import Snapshot

    with session_scope() as session:
        fat = Token(
            mint="0xfatlive24h0000000000000000000000000001",
            symbol="FAT",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow() - timedelta(hours=25),
            migrated_at=utcnow() - timedelta(hours=25),
        )
        fat.research = Research(p_good=0.54, holder_count=40, features_json="{}")
        session.add(fat)
        session.flush()
        session.add(
            Outcome(
                token_id=fat.id,
                t0_mcap=20_364.0,
                max_mcap=20_364.0,
                multiple=1.0,
                last_liq=8_200.0,
            )
        )
        session.add(
            Snapshot(
                token_id=fat.id,
                kind="t6h",
                mcap_usd=20_364.0,
                liquidity_usd=8_200.0,
                volume_h1=1_200.0,
            )
        )

    with session_scope() as session:
        asyncio.run(outcomes_mod.refresh_outcomes(session))
        out = (
            session.query(Outcome)
            .join(Token, Token.id == Outcome.token_id)
            .filter(Token.mint == "0xfatlive24h0000000000000000000000000001")
            .one()
        )
        assert out.label == 0
        assert out.t0_mcap == 20_364.0
        assert out.t24h_mcap == 20_364.0


def test_early_rh_five_x_does_not_keep_runnerwatch_row(monkeypatch):
    # Live ROCK: hit 5.4x in minutes and is already a runner. Watch is
    # for climbers; /api/runners owns the confirmed 5x tape. Do not keep
    # a runnerp row (or spend GMGN forensics) on a name that already ran.
    from launchfinder.models import ScanState

    init_db()

    async def fake_market(mint, chain="sol"):
        return {"mcap_usd": 175_082.0, "liquidity_usd": 82_369.0, "volume_h1": 40_000.0, "price_usd": 1.0}

    async def fake_coin(mint):
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market)
    monkeypatch.setattr(outcomes_mod.pumpfun, "get_coin", fake_coin)
    monkeypatch.setattr(outcomes_mod, "settings", outcomes_mod.settings)

    from launchfinder.research import gmgn as gmgn_mod

    monkeypatch.setattr(gmgn_mod, "gmgn_available", lambda: False)

    with session_scope() as session:
        rock = Token(
            mint="0xrockearlywin00000000000000000000000001",
            symbol="ROCK",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow() - timedelta(minutes=8),
            migrated_at=utcnow() - timedelta(minutes=8),
        )
        rock.research = Research(p_good=0.5084, holder_count=79, features_json="{}", risk_flags_json="[]")
        session.add(rock)
        session.flush()
        session.add(
            Outcome(
                token_id=rock.id,
                t0_mcap=32_561.0,
                max_mcap=32_561.0,
                multiple=1.0,
                last_liq=12_000.0,
            )
        )

    with session_scope() as session:
        asyncio.run(outcomes_mod.refresh_outcomes(session))
        out = (
            session.query(Outcome)
            .join(Token, Token.id == Outcome.token_id)
            .filter(Token.mint == "0xrockearlywin00000000000000000000000001")
            .one()
        )
        assert out.label == 1
        row = session.query(ScanState).filter(ScanState.key == "runnerp:0xrockearlywin00000000000000000000000001").one_or_none()
        assert row is None


def test_rh_t15m_climb_writes_runnerwatch_row(monkeypatch):
    # Live HELLO 2.38 / BUILDERMAN 2.76 sat on approaching with t15m
    # snaps while watch waited for t1h. Hour-one 2x climbs belong there.
    from launchfinder.models import ScanState

    init_db()

    async def fake_market(mint, chain="sol"):
        return {"mcap_usd": 55_190.0, "liquidity_usd": 45_089.0, "volume_h1": 12_000.0, "price_usd": 1.0}

    async def fake_coin(mint):
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market)
    monkeypatch.setattr(outcomes_mod.pumpfun, "get_coin", fake_coin)

    from launchfinder.research import gmgn as gmgn_mod

    monkeypatch.setattr(gmgn_mod, "gmgn_available", lambda: False)
    monkeypatch.setattr(gmgn_mod, "gmgn_deep_available", lambda: False)

    mint = "0xhelloclimbwatcht15m000000000000000001"
    with session_scope() as session:
        hello = Token(
            mint=mint,
            symbol="HELLO",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow() - timedelta(minutes=20),
            migrated_at=utcnow() - timedelta(minutes=20),
        )
        hello.research = Research(p_good=0.59, holder_count=77, features_json="{}", risk_flags_json="[]")
        session.add(hello)
        session.flush()
        session.add(
            Outcome(
                token_id=hello.id,
                t0_mcap=22_913.0,
                max_mcap=22_913.0,
                multiple=1.0,
                last_liq=22_812.0,
            )
        )

    with session_scope() as session:
        asyncio.run(outcomes_mod.refresh_outcomes(session))
        row = session.query(ScanState).filter(ScanState.key == f"runnerp:{mint}").one_or_none()
        assert row is not None
        payload = __import__("json").loads(row.value)
        assert payload.get("chain") == "robinhood"
        assert payload.get("mcap_ratio", 0) >= 2.0
        assert payload.get("mcap_ratio", 0) < 5.0


def test_rh_climb_rewrites_watch_after_horizon_already_filled(monkeypatch):
    # Live BUILDERMAN: t15m/t1h already stamped at 1.x, then the book
    # ran to 2.76. Horizons do not fire again; approaching used the
    # later snap and watch stayed empty.
    from launchfinder.models import ScanState

    init_db()

    async def fake_market(mint, chain="sol"):
        return {"mcap_usd": 59_731.0, "liquidity_usd": 49_258.0, "volume_h1": 8_000.0, "price_usd": 1.0}

    async def fake_coin(mint):
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market)
    monkeypatch.setattr(outcomes_mod.pumpfun, "get_coin", fake_coin)

    from launchfinder.research import gmgn as gmgn_mod

    monkeypatch.setattr(gmgn_mod, "gmgn_available", lambda: False)
    monkeypatch.setattr(gmgn_mod, "gmgn_deep_available", lambda: False)

    from launchfinder.models import Snapshot

    mint = "0xbuildermanwatchrewrite000000000000002"
    with session_scope() as session:
        tok = Token(
            mint=mint,
            symbol="BUILDERMAN",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow() - timedelta(minutes=20),
            migrated_at=utcnow() - timedelta(minutes=20),
        )
        tok.research = Research(p_good=0.76, holder_count=143, features_json="{}", risk_flags_json="[]")
        session.add(tok)
        session.flush()
        session.add(
            Outcome(
                token_id=tok.id,
                t0_mcap=21_640.0,
                max_mcap=21_640.0,
                t15m_mcap=24_000.0,
                t1h_mcap=24_000.0,
                multiple=1.11,
                last_liq=22_000.0,
            )
        )
        # Young pool always refreshes. t15m is already filled and a
        # fresh early snap blocks the 5-min early writer — only the
        # per-refresh hook should create the row.
        session.add(
            Snapshot(
                token_id=tok.id,
                kind="early",
                mcap_usd=24_000.0,
                liquidity_usd=22_000.0,
                volume_h1=1_000.0,
                taken_at=utcnow() - timedelta(minutes=1),
            )
        )

    with session_scope() as session:
        asyncio.run(outcomes_mod.refresh_outcomes(session))
        row = session.query(ScanState).filter(ScanState.key == f"runnerp:{mint}").one_or_none()
        assert row is not None
        payload = __import__("json").loads(row.value)
        assert payload.get("mcap_ratio", 0) >= 2.0


def test_rh_six_hour_due_prefers_real_books_over_catchup_leftovers(monkeypatch):
    # Live 08:40: catch-up leftovers stamp trench open as migrated_at
    # (12h ago) with last_liq=0 and hog the 16 reserved slots. WOODY-class
    # books we have actually watched never get the 6h tick.
    init_db()
    from launchfinder.models import Outcome as _Outcome
    from launchfinder.models import Research as _Research
    from launchfinder.models import Snapshot as _Snapshot
    from launchfinder.models import Token as _Token

    with session_scope() as session:
        session.query(_Snapshot).delete()
        session.query(_Outcome).delete()
        session.query(_Research).delete()
        session.query(_Token).delete()
    refreshed: list[str] = []

    async def fake_market(mint, chain="sol"):
        refreshed.append(mint)
        if "woodydue" in mint:
            return {"mcap_usd": 43_332.0, "liquidity_usd": 34_451.0, "volume_h1": 12_000.0, "price_usd": 1.0}
        return {}

    async def fake_coin(mint):
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market)
    monkeypatch.setattr(outcomes_mod.pumpfun, "get_coin", fake_coin)

    woody = "0xwoodydue0000000000000000000000000000001"
    with session_scope() as session:
        for i in range(20):
            t = Token(
                mint=f"0xleftoverhog{i:02d}00000000000000000000000",
                symbol=f"HOG{i}",
                source="rh_trenches",
                chain="robinhood",
                first_seen_at=utcnow() - timedelta(minutes=10),
                migrated_at=utcnow() - timedelta(hours=12),
            )
            t.research = Research(p_good=0.33, holder_count=4, features_json="{}")
            session.add(t)
            session.flush()
            session.add(Outcome(token_id=t.id, t0_mcap=40_000.0, max_mcap=40_000.0, multiple=1.0, last_liq=0.0))
        real = Token(
            mint=woody,
            symbol="WOODY",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow() - timedelta(hours=7),
            migrated_at=utcnow() - timedelta(hours=7),
        )
        real.research = Research(p_good=0.91, holder_count=119, features_json="{}")
        session.add(real)
        session.flush()
        session.add(
            Outcome(
                token_id=real.id,
                t0_mcap=43_332.0,
                max_mcap=43_332.0,
                multiple=1.0,
                last_liq=34_451.0,
            )
        )
        for i in range(60):
            t = Token(
                mint=f"SolHog{i:03d}Mint1111",
                symbol=f"S{i}",
                source="poll",
                chain="sol",
                first_seen_at=utcnow() - timedelta(hours=10),
                migrated_at=utcnow() - timedelta(hours=10),
            )
            t.research = Research(p_good=0.3, features_json="{}")
            session.add(t)
            session.flush()
            session.add(Outcome(token_id=t.id, t0_mcap=69_000.0, max_mcap=69_000.0))

    with session_scope() as session:
        asyncio.run(outcomes_mod.refresh_outcomes(session))
        out = (
            session.query(Outcome)
            .join(Token, Token.id == Outcome.token_id)
            .filter(Token.mint == woody)
            .one()
        )
        assert woody in refreshed
        assert out.t6h_mcap is not None


def test_rh_24h_due_real_book_still_gets_a_label(monkeypatch):
    # After t6h is filled, a 24h-due flat book dropped to priority 3 and
    # never got the judgment tick. n_train sat at 154 waiting for 24h.
    init_db()
    refreshed: list[str] = []

    async def fake_market(mint, chain="sol"):
        refreshed.append(mint)
        return {"mcap_usd": 42_269.0, "liquidity_usd": 39_372.0, "volume_h1": 1_400.0, "price_usd": 1.0}

    async def fake_coin(mint):
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market)
    monkeypatch.setattr(outcomes_mod.pumpfun, "get_coin", fake_coin)

    woody = "0xwoody24hlabel0000000000000000000001"
    with session_scope() as session:
        from launchfinder.scoring.outcomes import DEAD_POOL_LIQ

        # Shared pytest SQLite keeps other 24h-due RH rows. Park them so
        # this book is the reserved-slot winner.
        for out, _tok in (
            session.query(Outcome, Token)
            .join(Token, Token.id == Outcome.token_id)
            .filter(
                Token.chain == "robinhood",
                Outcome.label.is_(None),
                Outcome.t24h_mcap.is_(None),
                Outcome.last_liq >= DEAD_POOL_LIQ,
            )
            .all()
        ):
            out.t24h_mcap = out.max_mcap or out.t0_mcap
        real = Token(
            mint=woody,
            symbol="WOODY",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow() - timedelta(hours=25),
            migrated_at=utcnow() - timedelta(hours=25),
        )
        real.research = Research(p_good=0.91, holder_count=119, features_json="{}")
        session.add(real)
        session.flush()
        session.add(
            Outcome(
                token_id=real.id,
                t0_mcap=43_332.0,
                max_mcap=43_452.0,
                multiple=1.0,
                last_liq=39_894.0,
                t6h_mcap=42_269.0,
            )
        )

    with session_scope() as session:
        asyncio.run(outcomes_mod.refresh_outcomes(session))
        out = (
            session.query(Outcome)
            .join(Token, Token.id == Outcome.token_id)
            .filter(Token.mint == woody)
            .one()
        )
        assert woody in refreshed
        assert out.label is not None
        assert out.t24h_mcap is not None


def test_rh_dex_miss_early_labels_five_x_from_stored_book(monkeypatch):
    # Dex miss used to skip _early_label. A 5x with last_liq still a real
    # book waited until 24h (or the next live tick) to count as a win.
    init_db()

    async def fake_market(mint, chain="sol"):
        return {}

    async def fake_coin(mint):
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market)
    monkeypatch.setattr(outcomes_mod.pumpfun, "get_coin", fake_coin)

    mint = "0xsharkdexmiss5x000000000000000000000001"
    with session_scope() as session:
        tok = Token(
            mint=mint,
            symbol="SHARK",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow() - timedelta(minutes=20),
            migrated_at=utcnow() - timedelta(minutes=20),
        )
        tok.research = Research(p_good=0.65, holder_count=80, features_json="{}")
        session.add(tok)
        session.flush()
        session.add(
            Outcome(
                token_id=tok.id,
                t0_mcap=20_000.0,
                max_mcap=110_000.0,
                multiple=5.5,
                last_liq=17_000.0,
            )
        )

    with session_scope() as session:
        asyncio.run(outcomes_mod.refresh_outcomes(session))
        out = (
            session.query(Outcome)
            .join(Token, Token.id == Outcome.token_id)
            .filter(Token.mint == mint)
            .one()
        )
        assert out.label == 1
        assert out.t15m_mcap is not None


def test_young_rh_dexmiss_flood_does_not_starve_real_books(monkeypatch):
    # Live 18:00: one trench poll ingested 64 leftover RH names.
    # Unordered young limit(25) spent every Dex call on last_liq=0
    # infants; BUILDERMAN-class books and Sol t15m waited.
    init_db()
    from launchfinder.models import Outcome as _Outcome
    from launchfinder.models import Research as _Research
    from launchfinder.models import Snapshot as _Snapshot
    from launchfinder.models import Token as _Token
    from launchfinder.scoring.outcomes import YOUNG_RH_DEXMISS

    with session_scope() as session:
        session.query(_Snapshot).delete()
        session.query(_Outcome).delete()
        session.query(_Research).delete()
        session.query(_Token).delete()
    refreshed: list[str] = []

    async def fake_market(mint, chain="sol"):
        refreshed.append(mint)
        if "realbook" in mint or mint.startswith("YoungSol"):
            return {"mcap_usd": 55_000.0, "liquidity_usd": 22_000.0, "volume_h1": 4_000.0, "price_usd": 1.0}
        return {}

    async def fake_coin(mint):
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market)
    monkeypatch.setattr(outcomes_mod.pumpfun, "get_coin", fake_coin)

    real_rh = "0xrealbookyoung000000000000000000000001"
    young_sol = "YoungSolT15mMint111111111111111111111"
    miss_mints = []
    with session_scope() as session:
        for i in range(30):
            mint = f"0xdexmissflood{i:02d}0000000000000000000000"
            t = Token(
                mint=mint,
                symbol=f"MISS{i}",
                source="rh_trenches",
                chain="robinhood",
                first_seen_at=utcnow() - timedelta(minutes=10),
                migrated_at=utcnow() - timedelta(minutes=10),
            )
            t.research = Research(p_good=0.33, holder_count=2, features_json="{}")
            session.add(t)
            session.flush()
            session.add(Outcome(token_id=t.id, t0_mcap=40_000.0, max_mcap=40_000.0, multiple=1.0, last_liq=0.0))
            miss_mints.append(mint)
        book = Token(
            mint=real_rh,
            symbol="BUILDERMAN",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow() - timedelta(minutes=16),
            migrated_at=utcnow() - timedelta(minutes=16),
        )
        book.research = Research(p_good=0.76, holder_count=143, features_json="{}")
        session.add(book)
        session.flush()
        session.add(
            Outcome(
                token_id=book.id,
                t0_mcap=21_640.0,
                max_mcap=21_640.0,
                multiple=1.0,
                last_liq=22_000.0,
            )
        )
        sol = Token(
            mint=young_sol,
            symbol="YNG",
            source="poll",
            chain="sol",
            first_seen_at=utcnow() - timedelta(minutes=16),
            migrated_at=utcnow() - timedelta(minutes=16),
        )
        sol.research = Research(p_good=0.5, features_json="{}")
        session.add(sol)
        session.flush()
        session.add(Outcome(token_id=sol.id, t0_mcap=69_000.0, max_mcap=69_000.0))
        for i in range(60):
            t = Token(
                mint=f"SolOldFlood{i:03d}Mint11",
                symbol=f"S{i}",
                source="poll",
                chain="sol",
                first_seen_at=utcnow() - timedelta(hours=10),
                migrated_at=utcnow() - timedelta(hours=10),
            )
            t.research = Research(p_good=0.3, features_json="{}")
            session.add(t)
            session.flush()
            session.add(Outcome(token_id=t.id, t0_mcap=69_000.0, max_mcap=69_000.0))

    with session_scope() as session:
        asyncio.run(outcomes_mod.refresh_outcomes(session))

    assert real_rh in refreshed
    assert young_sol in refreshed
    miss_hit = [m for m in miss_mints if m in refreshed]
    # Young chairs cap Dex-miss at 8; the 16 reserved RH slots may
    # still pick leftovers at priority 4. The flood must not take every
    # young + reserved chair — some infants stay unvisited.
    assert len(miss_hit) <= YOUNG_RH_DEXMISS + 16
    assert len(miss_hit) < len(miss_mints)


def test_refresh_yields_the_event_loop_between_tokens(monkeypatch):
    # Live 18:40: /health 502'd while refresh held the loop across a
    # 20-launch RH ingest + ~70 Dex ticks. sleep(0) must fire once per
    # pending row so FastAPI can answer /health.
    init_db()
    from launchfinder.models import Outcome as _Outcome
    from launchfinder.models import Research as _Research
    from launchfinder.models import Snapshot as _Snapshot
    from launchfinder.models import Token as _Token

    with session_scope() as session:
        session.query(_Snapshot).delete()
        session.query(_Outcome).delete()
        session.query(_Research).delete()
        session.query(_Token).delete()

    yields = []
    real_sleep = outcomes_mod.asyncio.sleep

    async def counted_sleep(delay=0, *args, **kwargs):
        yields.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(outcomes_mod.asyncio, "sleep", counted_sleep)

    async def fake_market(mint, chain="sol"):
        return {"mcap_usd": 80_000.0, "liquidity_usd": 12_000.0, "price_usd": 0.0001}

    async def fake_coin(mint):
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market)
    monkeypatch.setattr(outcomes_mod.pumpfun, "get_coin", fake_coin)

    with session_scope() as session:
        for i in range(3):
            t = Token(
                mint=f"YieldTokenMint{i:03d}1111111111111111",
                symbol=f"Y{i}",
                source="poll",
                chain="sol",
                first_seen_at=utcnow() - timedelta(minutes=16),
                migrated_at=utcnow() - timedelta(minutes=16),
            )
            t.research = Research(p_good=0.4, features_json="{}")
            session.add(t)
            session.flush()
            session.add(Outcome(token_id=t.id, t0_mcap=69_000.0, max_mcap=69_000.0))

    with session_scope() as session:
        asyncio.run(outcomes_mod.refresh_outcomes(session))

    assert yields.count(0) >= 3


def test_rh_airdrop_class_gets_reserved_second_look(monkeypatch):
    # Live Goldinu: 1148 wallets / last_liq $4k / p=0.92 / 1.68x. Horizons
    # were still None after hours because the random + 6h pool never
    # visited. Reserved chairs + every-visit second-look write 0.48
    # without a one-shot recap. GUH-class ($47k / 105) is not this pool.
    import json

    from launchfinder.models import Outcome as _Outcome
    from launchfinder.models import Research as _Research
    from launchfinder.models import Snapshot as _Snapshot
    from launchfinder.models import Token as _Token
    from launchfinder.scoring.features import extract_features
    from launchfinder.scoring.outcomes import RH_AIRDROP_REFRESH

    init_db()
    with session_scope() as session:
        session.query(_Snapshot).delete()
        session.query(_Outcome).delete()
        session.query(_Research).delete()
        session.query(_Token).delete()

    refreshed: list[str] = []

    async def fake_market(mint, chain="sol"):
        refreshed.append(mint)
        if "goldinuairdrop" in mint:
            # Leftover Dex $30k — dust last_liq must still win.
            return {
                "mcap_usd": 69_000.0,
                "liquidity_usd": 30_367.0,
                "volume_h1": 400.0,
                "price_usd": 1.0,
            }
        return {"mcap_usd": 80_000.0, "liquidity_usd": 12_000.0, "volume_h1": 2_000.0, "price_usd": 1.0}

    async def fake_coin(mint):
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market)
    monkeypatch.setattr(outcomes_mod.pumpfun, "get_coin", fake_coin)

    gold_mint = "0xgoldinuairdropvisit000000000000000001"
    guh_mint = "0xguhstaysliveairdrop0000000000000000002"
    features = extract_features(
        {
            "chain": "robinhood",
            "coin": {"name": "Goldinu", "symbol": "GOLDINU"},
            "twitter": {"followers": 8_000, "age_days": 400, "verified": True, "tweets": 400},
            "twitter_handle": "goldinu",
            "website": "https://example.com",
            "holders": {"holder_count": 1148, "top10_pct": 40},
            "market": {"liquidity_usd": 30_367, "volume_h1": 2_000},
        }
    )
    assert features["rh_airdrop_book"] == 0.0

    with session_scope() as session:
        for i in range(60):
            t = Token(
                mint=f"SolAirdropFlood{i:03d}Mint1",
                symbol=f"S{i}",
                source="poll",
                chain="sol",
                first_seen_at=utcnow() - timedelta(hours=10),
                migrated_at=utcnow() - timedelta(hours=10),
            )
            t.research = Research(p_good=0.3, features_json="{}")
            session.add(t)
            session.flush()
            session.add(Outcome(token_id=t.id, t0_mcap=69_000.0, max_mcap=69_000.0, last_liq=12_000.0))
        gold = Token(
            mint=gold_mint,
            symbol="GOLDINU",
            source="rh_pons",
            chain="robinhood",
            first_seen_at=utcnow() - timedelta(hours=4),
            migrated_at=utcnow() - timedelta(hours=4),
        )
        gold.research = Research(
            p_good=0.92,
            holder_count=1148,
            features_json=json.dumps(features),
            risk_flags_json="[]",
            reasons_json="[]",
        )
        session.add(gold)
        session.flush()
        session.add(
            Outcome(
                token_id=gold.id,
                t0_mcap=40_900.0,
                t15m_mcap=55_000.0,
                t1h_mcap=60_000.0,
                t6h_mcap=None,
                max_mcap=68_800.0,
                multiple=1.68,
                last_liq=4_090.0,
            )
        )
        guh = Token(
            mint=guh_mint,
            symbol="GUH",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow() - timedelta(hours=4),
            migrated_at=utcnow() - timedelta(hours=4),
        )
        guh.research = Research(p_good=0.68, holder_count=105, features_json="{}", risk_flags_json="[]")
        session.add(guh)
        session.flush()
        session.add(
            Outcome(
                token_id=guh.id,
                t0_mcap=20_000.0,
                max_mcap=50_400.0,
                multiple=2.52,
                last_liq=41_000.0,
            )
        )

    with session_scope() as session:
        asyncio.run(outcomes_mod.refresh_outcomes(session))
        gold = session.query(Token).filter(Token.mint == gold_mint).one()
        assert gold.research.p_good <= 0.48
        assert gold.outcome.last_liq == 4_090.0

    assert gold_mint in refreshed
    assert RH_AIRDROP_REFRESH == 8
    # GUH is a real 2x+ book, not airdrop-class. Do not require a visit;
    # just confirm we did not pull it under the paper line.
    with session_scope() as session:
        guh = session.query(Token).filter(Token.mint == guh_mint).one()
        assert guh.research.p_good == 0.68


def test_live_refresh_rejects_sol_80x_dex_wick(monkeypatch):
    # Live 𝕏LIFE: t0 $69k floor, Dex $8.3M / 9 wallets / 120x, then
    # _early_label wrote a win. Honest peak on the live path must
    # keep max at the parked book and not label.
    init_db()
    from launchfinder.models import Outcome as _Outcome
    from launchfinder.models import Research as _Research
    from launchfinder.models import Snapshot as _Snapshot
    from launchfinder.models import Token as _Token
    from launchfinder.scoring.outcomes import MAX_HONEST_MULTIPLE

    with session_scope() as session:
        session.query(_Snapshot).delete()
        session.query(_Outcome).delete()
        session.query(_Research).delete()
        session.query(_Token).delete()

    mint = "XLifeWickMint11111111111111111111111111"

    async def fake_market(m, chain="sol"):
        return {
            "mcap_usd": 8_316_766.0,
            "liquidity_usd": 240_545.0,
            "volume_h1": 50_000.0,
            "price_usd": 1.0,
        }

    async def fake_coin(m):
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market)
    monkeypatch.setattr(outcomes_mod.pumpfun, "get_coin", fake_coin)

    with session_scope() as session:
        t = Token(
            mint=mint,
            symbol="XLIFE",
            source="poll",
            chain="sol",
            first_seen_at=utcnow() - timedelta(minutes=16),
            migrated_at=utcnow() - timedelta(minutes=16),
        )
        t.research = Research(p_good=0.26, holder_count=9, features_json="{}")
        session.add(t)
        session.flush()
        session.add(
            Outcome(
                token_id=t.id,
                t0_mcap=69_000.0,
                max_mcap=69_000.0,
                multiple=1.0,
                last_liq=5_000.0,
            )
        )

    with session_scope() as session:
        asyncio.run(outcomes_mod.refresh_outcomes(session))
        out = (
            session.query(Outcome)
            .join(Token, Token.id == Outcome.token_id)
            .filter(Token.mint == mint)
            .one()
        )
        assert out.max_mcap == 69_000.0
        assert out.multiple <= MAX_HONEST_MULTIPLE
        assert abs(out.multiple - 1.0) < 1e-6
        assert out.label is None


def test_rh_start_high_t0_gets_reserved_second_look(monkeypatch):
    # Live KFC: leftover t0 $146k / p=0.82 / $40k last_liq. Airdrop
    # chairs skipped it. Reserved start-high chairs + every-visit
    # second-look write entry_premium without a one-shot recap.
    import json

    from launchfinder.models import Outcome as _Outcome
    from launchfinder.models import Research as _Research
    from launchfinder.models import Snapshot as _Snapshot
    from launchfinder.models import Token as _Token
    from launchfinder.scoring.features import extract_features
    from launchfinder.scoring.outcomes import RH_START_HIGH_REFRESH

    init_db()
    with session_scope() as session:
        session.query(_Snapshot).delete()
        session.query(_Outcome).delete()
        session.query(_Research).delete()
        session.query(_Token).delete()

    refreshed: list[str] = []

    async def fake_market(mint, chain="sol"):
        refreshed.append(mint)
        return {
            "mcap_usd": 40_826.0,
            "liquidity_usd": 40_826.0,
            "volume_h1": 8_000.0,
            "price_usd": 1.0,
        }

    async def fake_coin(mint):
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market)
    monkeypatch.setattr(outcomes_mod.pumpfun, "get_coin", fake_coin)

    kfc_mint = "0xkfcstarthighrefresh000000000000000001"
    features = extract_features(
        {
            "chain": "robinhood",
            "coin": {"name": "Kentucky Fried Cookie", "symbol": "KFC"},
            "twitter": {"followers": 8_000, "age_days": 400, "verified": True, "tweets": 400},
            "twitter_handle": "kfc",
            "website": "https://example.com",
            "holders": {"holder_count": 1222, "top10_pct": 40},
            "market": {"liquidity_usd": 40_826, "volume_h1": 8_000, "mcap_usd": 40_826},
        }
    )
    assert features["entry_premium"] == 0.0

    with session_scope() as session:
        for i in range(60):
            t = Token(
                mint=f"SolStartHighFlood{i:03d}Mt1",
                symbol=f"S{i}",
                source="poll",
                chain="sol",
                first_seen_at=utcnow() - timedelta(hours=10),
                migrated_at=utcnow() - timedelta(hours=10),
            )
            t.research = Research(p_good=0.3, features_json="{}")
            session.add(t)
            session.flush()
            session.add(Outcome(token_id=t.id, t0_mcap=69_000.0, max_mcap=69_000.0, last_liq=12_000.0))
        kfc = Token(
            mint=kfc_mint,
            symbol="KFC",
            source="rh_pons",
            chain="robinhood",
            first_seen_at=utcnow() - timedelta(hours=4),
            migrated_at=utcnow() - timedelta(hours=4),
        )
        kfc.research = Research(
            p_good=0.82,
            holder_count=1222,
            features_json=json.dumps(features),
            risk_flags_json="[]",
            reasons_json="[]",
        )
        session.add(kfc)
        session.flush()
        session.add(
            Outcome(
                token_id=kfc.id,
                t0_mcap=146_816.0,
                t15m_mcap=146_816.0,
                t1h_mcap=146_816.0,
                max_mcap=146_816.0,
                multiple=1.0,
                last_liq=40_826.0,
            )
        )

    with session_scope() as session:
        asyncio.run(outcomes_mod.refresh_outcomes(session))
        kfc = session.query(Token).filter(Token.mint == kfc_mint).one()
        assert kfc.research.p_good < 0.70

    assert kfc_mint in refreshed
    assert RH_START_HIGH_REFRESH == 8


def test_rh_start_high_stale_last_stays_on_reserved_refresh(monkeypatch):
    # Live CRCL: 5× dropped the under-2× start-high chair. last stayed
    # $159k after the t1h dump while Dex recovered to $1.2M.
    from launchfinder.models import Outcome as _Outcome
    from launchfinder.models import Research as _Research
    from launchfinder.models import Snapshot as _Snapshot
    from launchfinder.models import Token as _Token
    from launchfinder.scoring.outcomes import _rh_start_high_stale_last

    init_db()
    with session_scope() as session:
        session.query(_Snapshot).delete()
        session.query(_Outcome).delete()
        session.query(_Research).delete()
        session.query(_Token).delete()

    refreshed: list[str] = []

    async def fake_market(mint, chain="sol"):
        refreshed.append(mint)
        return {
            "mcap_usd": 1_197_786.0,
            "liquidity_usd": 105_407.0,
            "volume_h1": 204_839.0,
            "price_usd": 0.0012,
        }

    async def fake_coin(mint):
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market)
    monkeypatch.setattr(outcomes_mod.pumpfun, "get_coin", fake_coin)

    crcl_mint = "0x51d1abab4d1e7b50642a25d3f7b2823067ebf9d7"
    with session_scope() as session:
        for i in range(60):
            t = Token(
                mint=f"SolCrclFlood{i:03d}Mt1",
                symbol=f"C{i}",
                source="poll",
                chain="sol",
                first_seen_at=utcnow() - timedelta(hours=10),
                migrated_at=utcnow() - timedelta(hours=10),
            )
            t.research = Research(p_good=0.3, features_json="{}")
            session.add(t)
            session.flush()
            session.add(Outcome(token_id=t.id, t0_mcap=69_000.0, max_mcap=69_000.0, last_liq=12_000.0))
        crcl = Token(
            mint=crcl_mint,
            symbol="CRCL",
            source="rh_dex",
            chain="robinhood",
            first_seen_at=utcnow() - timedelta(hours=12),
            migrated_at=utcnow() - timedelta(hours=12),
        )
        crcl.research = Research(p_good=0.18, holder_count=438, features_json="{}")
        session.add(crcl)
        session.flush()
        session.add(
            Outcome(
                token_id=crcl.id,
                t0_mcap=271_612.0,
                t15m_mcap=217_278.0,
                t1h_mcap=158_599.0,
                max_mcap=1_379_173.0,
                multiple=5.08,
                last_liq=118_923.0,
                last_mcap=158_599.0,
            )
        )
        session.flush()
        assert _rh_start_high_stale_last(crcl.outcome) is True

    with session_scope() as session:
        asyncio.run(outcomes_mod.refresh_outcomes(session))
        crcl = session.query(Token).filter(Token.mint == crcl_mint).one()
        assert abs(crcl.outcome.last_mcap - 1_197_786.0) < 1e-6

    assert crcl_mint in refreshed


def test_rh_start_high_labeled_zero_last_stays_on_post_label(monkeypatch):
    # Live CRCL: early 5× labeled at t1h. last_mcap stayed 0 so the
    # unlabeled start-high pool never wrote the Dex recovery.
    from launchfinder.models import Outcome as _Outcome
    from launchfinder.models import Research as _Research
    from launchfinder.models import Snapshot as _Snapshot
    from launchfinder.models import Token as _Token
    from launchfinder.scoring.outcomes import _rh_start_high_stale_last

    init_db()
    with session_scope() as session:
        session.query(_Snapshot).delete()
        session.query(_Outcome).delete()
        session.query(_Research).delete()
        session.query(_Token).delete()

    refreshed: list[str] = []

    async def fake_market(mint, chain="sol"):
        refreshed.append(mint)
        return {
            "mcap_usd": 1_120_727.0,
            "liquidity_usd": 101_959.0,
            "volume_h1": 231_666.0,
            "price_usd": 0.00112,
        }

    async def fake_coin(mint):
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market)
    monkeypatch.setattr(outcomes_mod.pumpfun, "get_coin", fake_coin)

    crcl_mint = "0x51d1abab4d1e7b50642a25d3f7b2823067ebf9d7"
    with session_scope() as session:
        crcl = Token(
            mint=crcl_mint,
            symbol="CRCL",
            source="rh_dex",
            chain="robinhood",
            first_seen_at=utcnow() - timedelta(hours=12),
            migrated_at=utcnow() - timedelta(hours=12),
        )
        crcl.research = Research(p_good=0.18, holder_count=438, features_json="{}")
        session.add(crcl)
        session.flush()
        session.add(
            Outcome(
                token_id=crcl.id,
                t0_mcap=271_612.0,
                t1h_mcap=158_599.0,
                max_mcap=1_379_173.0,
                multiple=5.08,
                last_liq=118_923.0,
                last_mcap=0.0,
                label=1,
            )
        )
        session.flush()
        assert _rh_start_high_stale_last(crcl.outcome) is True

    with session_scope() as session:
        asyncio.run(outcomes_mod.refresh_outcomes(session))
        crcl = session.query(Token).filter(Token.mint == crcl_mint).one()
        assert abs(crcl.outcome.last_mcap - 1_120_727.0) < 1e-6

    assert crcl_mint in refreshed


def test_rh_floor_lapse_gets_reserved_second_look(monkeypatch):
    # Live DCE: p=0.078 / hp=0.47 / $20k / 30 wallets after the 960
    # floor lapsed. Airdrop chairs want 200+ / $800–$8k; start-high
    # wants leftover t0. Reserved floor-restore chairs visit without
    # a one-shot recap. Claude 0.48 veto and YOLO 2x+ stay out.
    from launchfinder.models import Outcome as _Outcome
    from launchfinder.models import Research as _Research
    from launchfinder.models import Snapshot as _Snapshot
    from launchfinder.models import Token as _Token
    from launchfinder.scoring.outcomes import RH_FLOOR_RESTORE_REFRESH

    init_db()
    with session_scope() as session:
        session.query(_Snapshot).delete()
        session.query(_Outcome).delete()
        session.query(_Research).delete()
        session.query(_Token).delete()

    refreshed: list[str] = []

    async def fake_market(mint, chain="sol"):
        refreshed.append(mint)
        return {
            "mcap_usd": 20_761.0,
            "liquidity_usd": 20_761.0,
            "volume_h1": 4_000.0,
            "price_usd": 1.0,
        }

    async def fake_coin(mint):
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market)
    monkeypatch.setattr(outcomes_mod.pumpfun, "get_coin", fake_coin)

    dce_mint = "0xdcefloorrestore000000000000000000001"
    claude_mint = "0xclaudevetoshelffloor0000000000000001"
    yolo_mint = "0xyolofloor2xblock0000000000000000001"

    with session_scope() as session:
        for i in range(60):
            t = Token(
                mint=f"SolFloorFlood{i:03d}Mint1",
                symbol=f"S{i}",
                source="poll",
                chain="sol",
                first_seen_at=utcnow() - timedelta(hours=10),
                migrated_at=utcnow() - timedelta(hours=10),
            )
            t.research = Research(p_good=0.3, heuristic_p=0.3, features_json="{}")
            session.add(t)
            session.flush()
            session.add(Outcome(token_id=t.id, t0_mcap=69_000.0, max_mcap=69_000.0, last_liq=12_000.0))
        dce = Token(
            mint=dce_mint,
            symbol="DCE",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow() - timedelta(hours=3),
            migrated_at=utcnow() - timedelta(hours=3),
        )
        dce.research = Research(
            p_good=0.0785,
            heuristic_p=0.47,
            holder_count=30,
            features_json="{}",
            risk_flags_json="[]",
        )
        claude = Token(
            mint=claude_mint,
            symbol="Claude",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow() - timedelta(hours=3),
            migrated_at=utcnow() - timedelta(hours=3),
        )
        claude.research = Research(
            p_good=0.48,
            heuristic_p=0.59,
            holder_count=1133,
            features_json="{}",
            risk_flags_json="[]",
        )
        yolo = Token(
            mint=yolo_mint,
            symbol="YOLO",
            source="rh_pons",
            chain="robinhood",
            first_seen_at=utcnow() - timedelta(hours=3),
            migrated_at=utcnow() - timedelta(hours=3),
        )
        yolo.research = Research(
            p_good=0.64,
            heuristic_p=0.80,
            holder_count=1140,
            features_json="{}",
            risk_flags_json="[]",
        )
        session.add_all([dce, claude, yolo])
        session.flush()
        session.add(
            Outcome(
                token_id=dce.id,
                t0_mcap=20_000.0,
                t15m_mcap=20_761.0,
                t1h_mcap=20_761.0,
                max_mcap=20_761.0,
                multiple=1.0,
                last_liq=20_761.0,
            )
        )
        session.add(
            Outcome(
                token_id=claude.id,
                t0_mcap=20_000.0,
                max_mcap=33_800.0,
                multiple=1.69,
                last_liq=34_986.0,
            )
        )
        session.add(
            Outcome(
                token_id=yolo.id,
                t0_mcap=63_516.0,
                max_mcap=178_000.0,
                multiple=2.81,
                last_liq=45_291.0,
            )
        )

    with session_scope() as session:
        asyncio.run(outcomes_mod.refresh_outcomes(session))

    assert dce_mint in refreshed
    assert RH_FLOOR_RESTORE_REFRESH == 8


def test_rh_wallet_map_chairs_include_seven_hour_books(monkeypatch):
    # Live THEINVESTOR: 7h / $35k / GMGN holders, no top_wallets.
    # t15m/t6h already wrote; random 30 can miss it.
    from launchfinder.scoring.outcomes import RH_WALLET_MAP_REFRESH

    init_db()
    refreshed: list[str] = []

    async def fake_market(mint, chain="sol"):
        refreshed.append(mint)
        return {"mcap_usd": 35_000.0, "liquidity_usd": 35_000.0, "price_usd": 0.0001}

    async def fake_coin(mint):
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market)
    monkeypatch.setattr(outcomes_mod.pumpfun, "get_coin", fake_coin)

    with session_scope() as session:
        for i in range(40):
            t = Token(
                mint=f"SolCrowdWallet{i:03d}Mint11",
                symbol=f"SC{i}",
                source="poll",
                chain="sol",
                first_seen_at=utcnow() - timedelta(hours=10),
                migrated_at=utcnow() - timedelta(hours=10),
            )
            t.research = Research(p_good=0.3, features_json="{}")
            session.add(t)
            session.flush()
            session.add(Outcome(token_id=t.id, t0_mcap=69_000.0, max_mcap=69_000.0, last_liq=20_000.0))
        inv = Token(
            mint="0xac292fda653018e459d69db1c88f11565e551e19",
            symbol="INVCHAIR",
            chain="robinhood",
            source="rh_trenches",
            first_seen_at=utcnow() - timedelta(hours=7),
            migrated_at=utcnow() - timedelta(hours=7),
        )
        inv.research = Research(
            p_good=0.92,
            holder_count=98,
            features_json="{}",
            raw_json='{"holders":{"holder_count":98,"source":"gmgn"}}',
        )
        session.add(inv)
        session.flush()
        session.add(
            Outcome(
                token_id=inv.id,
                t0_mcap=20_000.0,
                max_mcap=35_000.0,
                multiple=1.1,
                last_liq=35_529.0,
                t15m_mcap=22_000.0,
                t1h_mcap=24_000.0,
                t6h_mcap=30_000.0,
            )
        )

    with session_scope() as session:
        asyncio.run(outcomes_mod.refresh_outcomes(session))

    assert "0xac292fda653018e459d69db1c88f11565e551e19" in refreshed
    assert RH_WALLET_MAP_REFRESH == 8


def test_rh_lp_open_chairs_rescore_missed_ingest(monkeypatch):
    # Live MEME: a 40-minute fair LP open sat at p=0.24 after t15m
    # already wrote. Young 25 (35m) and wallet-map chairs miss a
    # mapped pool-open. Reserved LP-open chairs must visit it under 2×.
    import json

    from launchfinder.models import Outcome as _Outcome
    from launchfinder.models import Research as _Research
    from launchfinder.models import Snapshot as _Snapshot
    from launchfinder.models import Token as _Token
    from launchfinder.scoring.features import extract_features
    from launchfinder.scoring.outcomes import RH_LP_OPEN_REFRESH

    init_db()
    with session_scope() as session:
        session.query(_Snapshot).delete()
        session.query(_Outcome).delete()
        session.query(_Research).delete()
        session.query(_Token).delete()

    refreshed: list[str] = []

    async def fake_market(mint, chain="sol"):
        refreshed.append(mint)
        return {
            "mcap_usd": 24_000.0,
            "liquidity_usd": 22_110.0,
            "volume_h1": 85_000.0,
            "price_usd": 0.00002,
        }

    async def fake_coin(mint):
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market)
    monkeypatch.setattr(outcomes_mod.pumpfun, "get_coin", fake_coin)

    meme_mint = "0x385f4f8ae47651ce5f58f5265395a669f8281e18"
    features = extract_features(
        {
            "chain": "robinhood",
            "coin": {"name": "A Meme Coin", "symbol": "MEME"},
            "twitter": {"followers": 2, "age_days": 0.02, "verified": True, "tweets": 0},
            "twitter_handle": "amemecoinrh",
            "symbol_flood": 4,
            "holders": {
                "holder_count": 4,
                "top10_pct": 1.2,
                "top_wallets": [
                    {"pct": 98.8, "label": "pool"},
                    {"pct": 1.2, "label": ""},
                ],
            },
            "market": {},
        }
    )
    with session_scope() as session:
        for i in range(60):
            t = Token(
                mint=f"SolLpOpenFlood{i:03d}Mt11",
                symbol=f"S{i}",
                source="poll",
                chain="sol",
                first_seen_at=utcnow() - timedelta(hours=10),
                migrated_at=utcnow() - timedelta(hours=10),
            )
            t.research = Research(p_good=0.3, features_json="{}")
            session.add(t)
            session.flush()
            session.add(Outcome(token_id=t.id, t0_mcap=69_000.0, max_mcap=69_000.0, last_liq=12_000.0))
        meme = Token(
            mint=meme_mint,
            symbol="MEME",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow() - timedelta(minutes=40),
            migrated_at=utcnow() - timedelta(minutes=40),
        )
        meme.research = Research(
            p_good=0.24,
            heuristic_p=0.24,
            holder_count=4,
            features_json=json.dumps(features),
            raw_json=json.dumps(
                {
                    "holders": {
                        "holder_count": 4,
                        "top_wallets": [
                            {"pct": 98.8, "label": "pool"},
                            {"pct": 1.2, "label": ""},
                        ],
                    },
                    "gmgn": {"source": "gmgn", "rug_risk": 0},
                }
            ),
        )
        session.add(meme)
        session.flush()
        session.add(
            Outcome(
                token_id=meme.id,
                t0_mcap=21_997.0,
                t15m_mcap=24_000.0,
                t1h_mcap=24_000.0,
                t6h_mcap=24_000.0,
                max_mcap=24_000.0,
                multiple=1.09,
                last_liq=22_110.0,
            )
        )

    with session_scope() as session:
        asyncio.run(outcomes_mod.refresh_outcomes(session))
        row = session.query(Token).filter(Token.mint == meme_mint).one()
        assert row.research.p_good >= 0.50

    assert meme_mint in refreshed
    assert RH_LP_OPEN_REFRESH == 8
