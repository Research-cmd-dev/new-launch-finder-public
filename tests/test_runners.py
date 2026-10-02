import asyncio
import json
from datetime import timedelta

from launchfinder.app import runners
from launchfinder.config import GRADUATION_MCAP_USD
from launchfinder.db import init_db, session_scope
from launchfinder.models import Outcome, Research, ScanState, Snapshot, Token, utcnow
from launchfinder.scoring.outcomes import REPAIR_T0_KEY, repair_entry_prices


def test_repair_reanchors_collapsed_entries():
    init_db()
    with session_scope() as session:
        t = Token(mint="RepairMint1111", symbol="RPR", source="poll", first_seen_at=utcnow())
        t.research = Research(p_good=0.3, features_json="{}")
        session.add(t)
        session.flush()
        # the $WIND pathology: t0 captured mid-collapse
        session.add(Outcome(token_id=t.id, t0_mcap=8.49, max_mcap=328.0, multiple=38.6, label=1))
    with session_scope() as session:
        repair_entry_prices(session)
        bad = session.query(Outcome).join(Token, Token.id == Outcome.token_id).filter(Token.mint == "RepairMint1111").one()
        assert bad.t0_mcap == GRADUATION_MCAP_USD
        assert bad.multiple < 0.01
        assert bad.label is None  # re-judged with honest numbers
        assert session.query(ScanState).filter(ScanState.key == REPAIR_T0_KEY).one_or_none() is not None
        # idempotent
        repair_entry_prices(session)


def test_corrupt_mcap_repair_rebuilds_from_snapshots():
    from launchfinder.models import Snapshot
    from launchfinder.scoring.outcomes import repair_corrupt_mcaps, sane_mcap

    from datetime import timedelta

    assert sane_mcap(240_786_340_000.0) == 0.0
    assert sane_mcap(150_000.0) == 150_000.0
    # liquidity-aware: $440M mcap on $20k liquidity is a wrong-pair artifact
    assert sane_mcap(440_000_000.0, 20_000.0) == 0.0
    assert sane_mcap(150_000.0, 20_000.0) == 150_000.0
    # time-aware: a $250M print 15 minutes after migration is manipulated
    # even with a plausible-looking liquidity figure (NTDA case)
    assert sane_mcap(253_000_000.0, 1_400_000.0, timedelta(minutes=15)) == 0.0
    assert sane_mcap(253_000_000.0, 1_400_000.0, timedelta(days=2)) == 253_000_000.0
    assert sane_mcap(5_000_000.0, 500_000.0, timedelta(minutes=15)) == 5_000_000.0
    init_db()
    with session_scope() as session:
        t = Token(mint="CorruptMint111", symbol="CRPT", source="poll", first_seen_at=utcnow())
        t.research = Research(p_good=0.4, features_json="{}")
        session.add(t)
        session.flush()
        # mature snapshot carries the honest reading; early flash print must be ignored
        session.add(Snapshot(token_id=t.id, kind="t6h", mcap_usd=180_000.0, liquidity_usd=20_000.0))
        session.add(Snapshot(token_id=t.id, kind="t15m", mcap_usd=253_000_000.0, liquidity_usd=1_400_000.0))
        session.add(Outcome(token_id=t.id, t0_mcap=69_000.0, max_mcap=1.66e13, multiple=240_786_340.7, label=1))
    with session_scope() as session:
        repair_corrupt_mcaps(session)
        fixed = session.query(Outcome).join(Token, Token.id == Outcome.token_id).filter(Token.mint == "CorruptMint111").one()
        assert fixed.max_mcap == 180_000.0
        assert 2.0 < fixed.multiple < 3.0
        assert fixed.label is None


def test_runner_scoreboard_and_capture_rate():
    init_db()
    with session_scope() as session:
        for i, (mult, p) in enumerate([(15.0, 0.8), (22.0, 0.3)]):
            t = Token(mint=f"RunnerMint{i}111", symbol=f"R{i}", source="poll", first_seen_at=utcnow())
            t.research = Research(p_good=p, features_json="{}")
            session.add(t)
            session.flush()
            session.add(Outcome(token_id=t.id, t0_mcap=69_000.0, max_mcap=69_000.0 * mult, multiple=mult, label=1, last_liq=25_000.0))
            session.add(Snapshot(
                token_id=t.id,
                kind="t6h",
                mcap_usd=69_000.0 * mult,
                liquidity_usd=25_000.0,
            ))
    board = asyncio.run(runners(min_multiple=10.0))
    assert board["runners"] >= 2
    mine = [r for r in board["items"] if r["mint"].startswith("RunnerMint")]
    assert {r["flagged_at_entry"] for r in mine} == {True, False}
    assert board["capture_rate"] is not None


def test_rh_capture_uses_paper_threshold():
    # Live ROCK/SSB/OOOF all entered ~p=0.51. Solana's 0.60 flag line
    # reported capture 0.0 on three honest 5x names the /rh desk bought.
    init_db()
    with session_scope() as session:
        t = Token(
            mint="0xrockcapture000000000000000000000000001",
            symbol="ROCK",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        t.research = Research(p_good=0.5084, holder_count=79, features_json="{}", risk_flags_json="[]")
        session.add(t)
        session.flush()
        session.add(
            Outcome(
                token_id=t.id,
                t0_mcap=32_561.0,
                max_mcap=175_082.0,
                multiple=5.38,
                label=1,
                last_liq=82_369.0,
            )
        )
        session.add(Snapshot(token_id=t.id, kind="early", mcap_usd=175_082.0, liquidity_usd=82_369.0))
    board = asyncio.run(runners(min_multiple=5.0, chain="robinhood"))
    rock = next(r for r in board["items"] if r["mint"] == "0xrockcapture000000000000000000000000001")
    assert rock["flagged_at_entry"] is True
    assert rock["entry_p"] == 0.508
    assert board["capture_rate"] == 1.0


def test_historical_rh_leftover_is_not_a_runner():
    # Live AIAIAI: quiet-retired leftover, then a late Dex post snap at 55.6×.
    # RH runners are the live desk; Solana still shows historical majors.
    init_db()
    with session_scope() as session:
        leftover = Token(
            mint="0xaiaiaihistboard0000000000000000000001",
            symbol="AIAIAI",
            source="rh_trenches",
            chain="robinhood",
            is_historical=True,
            first_seen_at=utcnow(),
        )
        leftover.research = Research(p_good=0.53, holder_count=24, features_json="{}", risk_flags_json="[]")
        session.add(leftover)
        session.flush()
        session.add(
            Outcome(
                token_id=leftover.id,
                t0_mcap=32_226.0,
                max_mcap=1_791_708.0,
                multiple=55.6,
                label=1,
                last_liq=425_154.0,
            )
        )
        session.add(Snapshot(token_id=leftover.id, kind="post", mcap_usd=1_791_708.0, liquidity_usd=425_154.0))

        live = Token(
            mint="0xbelieveboard000000000000000000000001",
            symbol="BELIEVE",
            source="rh_trenches",
            chain="robinhood",
            is_historical=False,
            first_seen_at=utcnow(),
        )
        live.research = Research(p_good=0.78, holder_count=112, features_json="{}", risk_flags_json="[]")
        session.add(live)
        session.flush()
        session.add(
            Outcome(
                token_id=live.id,
                t0_mcap=20_000.0,
                max_mcap=570_000.0,
                multiple=28.5,
                label=1,
                last_liq=80_000.0,
            )
        )
        session.add(Snapshot(token_id=live.id, kind="t6h", mcap_usd=570_000.0, liquidity_usd=80_000.0))

        old_sol = Token(
            mint="SolHistBoardMint11111111111111111111",
            symbol="CAC",
            source="poll",
            chain="sol",
            is_historical=True,
            first_seen_at=utcnow(),
        )
        old_sol.research = Research(p_good=0.79, holder_count=400, features_json="{}", risk_flags_json="[]")
        session.add(old_sol)
        session.flush()
        session.add(
            Outcome(
                token_id=old_sol.id,
                t0_mcap=69_000.0,
                max_mcap=69_000.0 * 65.2,
                multiple=65.2,
                label=1,
                last_liq=200_000.0,
            )
        )
        session.add(Snapshot(token_id=old_sol.id, kind="t6h", mcap_usd=69_000.0 * 65.2, liquidity_usd=200_000.0))
    rh = asyncio.run(runners(min_multiple=5.0, chain="robinhood"))
    sol = asyncio.run(runners(min_multiple=5.0, chain="sol"))
    assert "0xaiaiaihistboard0000000000000000000001" not in {r["mint"] for r in rh["items"]}
    assert "0xbelieveboard000000000000000000000001" in {r["mint"] for r in rh["items"]}
    assert "SolHistBoardMint11111111111111111111" in {r["mint"] for r in sol["items"]}


def test_five_x_confirmed_runner_is_on_the_default_board():
    init_db()
    with session_scope() as session:
        t = Token(mint="FiveXMint1111111", symbol="FIVEX", source="poll", first_seen_at=utcnow())
        t.research = Research(p_good=0.7, features_json="{}")
        session.add(t)
        session.flush()
        session.add(Outcome(token_id=t.id, t0_mcap=69_000.0, max_mcap=69_000.0 * 6.2, multiple=6.2, label=1, last_liq=25_000.0))
        session.add(Snapshot(token_id=t.id, kind="t6h", mcap_usd=69_000.0 * 6.2, liquidity_usd=25_000.0))
    board = asyncio.run(runners())
    assert board["min_multiple"] == 5.0
    assert any(r["mint"] == "FiveXMint1111111" for r in board["items"])


def test_flash_print_without_liquid_peak_is_not_a_runner():
    init_db()
    with session_scope() as session:
        t = Token(mint="FlashPrintMint1", symbol="FLASH", source="poll", first_seen_at=utcnow())
        t.research = Research(p_good=0.2, features_json="{}")
        session.add(t)
        session.flush()
        session.add(Outcome(token_id=t.id, t0_mcap=69_000.0, max_mcap=69_000.0 * 5000.0, multiple=5000.0, label=1))
        session.add(Snapshot(token_id=t.id, kind="t15m", mcap_usd=69_000.0 * 5000.0, liquidity_usd=800.0))
    board = asyncio.run(runners(min_multiple=10.0))
    assert not any(r["mint"] == "FlashPrintMint1" for r in board["items"])


def test_mature_liquid_flash_print_above_80x_is_not_a_runner():
    from datetime import timedelta

    init_db()
    with session_scope() as session:
        seen = utcnow() - timedelta(days=2)
        t = Token(mint="MatureFlashMint1", symbol="USMS", source="poll", first_seen_at=seen, migrated_at=seen)
        t.research = Research(p_good=0.4, features_json="{}")
        session.add(t)
        session.flush()
        session.add(Outcome(token_id=t.id, t0_mcap=69_000.0, max_mcap=69_000.0 * 2000.0, multiple=2000.0, label=1))
        session.add(Snapshot(token_id=t.id, kind="t6h", mcap_usd=69_000.0 * 2000.0, liquidity_usd=80_000.0, taken_at=utcnow()))
    board = asyncio.run(runners(min_multiple=10.0))
    assert not any(r["mint"] == "MatureFlashMint1" for r in board["items"])


def test_prepumped_entry_is_not_a_runner():
    init_db()
    with session_scope() as session:
        flagged = Token(mint="PrePumpFlagMint1", symbol="USWS", source="poll", first_seen_at=utcnow())
        flagged.research = Research(
            p_good=0.25,
            features_json='{"entry_premium": 1.0}',
            risk_flags_json='["Pre-pumped through migration at a huge premium (bundle-owned price)"]',
        )
        session.add(flagged)
        session.flush()
        session.add(Outcome(token_id=flagged.id, t0_mcap=69_000.0, max_mcap=69_000.0 * 15.0, multiple=15.0, label=1))
        session.add(Snapshot(token_id=flagged.id, kind="t6h", mcap_usd=69_000.0 * 15.0, liquidity_usd=25_000.0))

        # no flag stored (researched before the feature shipped) but t0 is $2.1M
        late = Token(mint="PrePumpT0Mint111", symbol="USMS", source="poll", first_seen_at=utcnow())
        late.research = Research(p_good=0.2, features_json="{}", risk_flags_json="[]")
        session.add(late)
        session.flush()
        session.add(Outcome(token_id=late.id, t0_mcap=2_147_855.0, max_mcap=2_147_855.0 * 12.0, multiple=12.0, label=1))
        session.add(Snapshot(token_id=late.id, kind="t6h", mcap_usd=2_147_855.0 * 12.0, liquidity_usd=80_000.0))

        # the live pathology: outcome t0 re-anchored at $69k, first snap already $5M
        anchored = Token(mint="PrePumpSnapMint1", symbol="USWS", source="poll", first_seen_at=utcnow())
        anchored.research = Research(p_good=0.54, features_json="{}", risk_flags_json="[]")
        session.add(anchored)
        session.flush()
        session.add(Outcome(token_id=anchored.id, t0_mcap=69_000.0, max_mcap=5_250_000.0, multiple=76.0, label=1))
        session.add(Snapshot(token_id=anchored.id, kind="t0", mcap_usd=5_037_000.0, liquidity_usd=0.0))
        session.add(Snapshot(token_id=anchored.id, kind="t6h", mcap_usd=5_258_000.0, liquidity_usd=195_000.0))

        # a real runner that is already 10x by t15m must stay on the board
        honest = Token(mint="HonestRunnerMint1", symbol="FTFS", source="poll", first_seen_at=utcnow())
        honest.research = Research(p_good=0.7, features_json="{}", risk_flags_json="[]")
        session.add(honest)
        session.flush()
        session.add(Outcome(token_id=honest.id, t0_mcap=69_000.0, max_mcap=69_000.0 * 15.0, multiple=15.0, label=1, last_liq=50_000.0))
        session.add(Snapshot(token_id=honest.id, kind="t0", mcap_usd=80_000.0, liquidity_usd=20_000.0))
        session.add(Snapshot(token_id=honest.id, kind="t15m", mcap_usd=69_000.0 * 12.0, liquidity_usd=40_000.0))
        session.add(Snapshot(token_id=honest.id, kind="t6h", mcap_usd=69_000.0 * 15.0, liquidity_usd=50_000.0))
    board = asyncio.run(runners(min_multiple=10.0))
    mints = {r["mint"] for r in board["items"]}
    assert "PrePumpFlagMint1" not in mints
    assert "PrePumpT0Mint111" not in mints
    assert "PrePumpSnapMint1" not in mints
    assert "HonestRunnerMint1" in mints


def test_runners_sql_window_does_not_let_70x_tape_bury_pappy():
    """Pappy 33.6x dropped off the live board while AXE (raw 56x, confirmed 5.9x)
    stayed. SQL took the top 250 by outcome.multiple, and 70x–1M× artifacts
    (WOFI/ARROW/USMS) filled it. Cap the window at MAX_HONEST_MULTIPLE."""
    init_db()
    with session_scope() as session:
        for i in range(260):
            t = Token(mint=f"Fake70xMint{i:04d}", symbol=f"F{i}", source="poll", first_seen_at=utcnow())
            t.research = Research(p_good=0.2, features_json="{}", risk_flags_json="[]")
            session.add(t)
            session.flush()
            session.add(Outcome(token_id=t.id, t0_mcap=69_000.0, max_mcap=69_000.0 * 200.0, multiple=200.0, label=1, last_liq=40_000.0))
        pappy = Token(mint="PappyHonestMint11", symbol="Pappy", source="gmgn_trenches", first_seen_at=utcnow())
        pappy.research = Research(p_good=0.30, holder_count=13, features_json="{}", risk_flags_json="[]")
        session.add(pappy)
        session.flush()
        session.add(Outcome(token_id=pappy.id, t0_mcap=69_000.0, max_mcap=2_320_966.0, multiple=33.64, label=1, last_liq=1_950.0))
        session.add(Snapshot(token_id=pappy.id, kind="t15m", mcap_usd=2_320_966.0, liquidity_usd=129_450.0))
    board = asyncio.run(runners(min_multiple=5.0))
    mints = {r["mint"] for r in board["items"]}
    assert "PappyHonestMint11" in mints
    assert not any(m.startswith("Fake70xMint") for m in mints)


def test_runners_drop_instant_fill_copycats_not_pappy():
    # Live GPRO 71x: instant curve + same-ticker flood, heuristic 0.02.
    # Pappy 33x is a thin lottery ticket — no flood/instant pair.
    from launchfinder.scoring.outcomes import is_bundle_copycat_run

    gpro_flags = (
        '["Bonding curve filled almost instantly (bundle risk)",'
        ' "Same ticker launched repeatedly in 24h (copycat spam)"]'
    )
    init_db()
    with session_scope() as session:
        gpro = Token(mint="GproCopycatMint11", symbol="GPRO", source="poll", first_seen_at=utcnow())
        gpro.research = Research(p_good=0.37, holder_count=258, features_json="{}", risk_flags_json=gpro_flags)
        session.add(gpro)
        session.flush()
        session.add(Outcome(token_id=gpro.id, t0_mcap=34_475.0, max_mcap=2_473_349.0, multiple=71.7, label=1, last_liq=197_466.0))
        session.add(Snapshot(token_id=gpro.id, kind="post", mcap_usd=2_473_349.0, liquidity_usd=197_466.0))
        pappy = Token(mint="PappyKeepMint1111", symbol="Pappy", source="poll", first_seen_at=utcnow())
        pappy.research = Research(
            p_good=0.30,
            holder_count=13,
            features_json="{}",
            risk_flags_json='["Many top wallets look brand-new (bundle/sybil risk)"]',
        )
        session.add(pappy)
        session.flush()
        session.add(Outcome(token_id=pappy.id, t0_mcap=69_000.0, max_mcap=2_320_966.0, multiple=33.6, label=1, last_liq=1_950.0))
        session.add(Snapshot(token_id=pappy.id, kind="t15m", mcap_usd=2_320_966.0, liquidity_usd=129_450.0))
        cac = Token(mint="CacKeepMint111111", symbol="CAC", source="poll", first_seen_at=utcnow())
        cac.research = Research(p_good=0.79, holder_count=18, features_json="{}", risk_flags_json="[]")
        session.add(cac)
        session.flush()
        session.add(Outcome(token_id=cac.id, t0_mcap=69_000.0, max_mcap=4_340_000.0, multiple=62.9, label=1, last_liq=80_000.0))
        session.add(Snapshot(token_id=cac.id, kind="post", mcap_usd=4_340_000.0, liquidity_usd=80_000.0))
        assert is_bundle_copycat_run(gpro.research)
        assert not is_bundle_copycat_run(pappy.research)
    board = asyncio.run(runners(min_multiple=5.0))
    mints = {r["mint"] for r in board["items"]}
    assert "GproCopycatMint11" not in mints
    assert "PappyKeepMint1111" in mints
    assert "CacKeepMint111111" in mints


def test_runners_sql_window_does_not_let_unconfirmed_20x_bury_pvp():
    # Live PVP: t0 $40k / $15k liq, post $332k / $47k liq, 8.2x, 138 holders.
    # SQL took the top 250 by raw multiple. Unconfirmed 10–80x tape (no
    # liquid snaps / 411-SOL leftovers) filled it, so an honest 8x never
    # reached confirmed_runner_multiple.
    init_db()
    with session_scope() as session:
        for i in range(300):
            t = Token(mint=f"Fake20xMint{i:04d}", symbol=f"U{i}", source="poll", first_seen_at=utcnow())
            t.research = Research(p_good=0.2, features_json="{}", risk_flags_json="[]")
            session.add(t)
            session.flush()
            session.add(
                Outcome(
                    token_id=t.id,
                    t0_mcap=69_000.0,
                    max_mcap=69_000.0 * 20.0,
                    multiple=20.0,
                    label=1,
                    last_liq=40_000.0,
                )
            )
        for i in range(20):
            t = Token(mint=f"TinyT0Mint{i:04d}", symbol=f"T{i}", source="poll", first_seen_at=utcnow())
            t.research = Research(p_good=0.1, features_json="{}", risk_flags_json="[]")
            session.add(t)
            session.flush()
            session.add(
                Outcome(
                    token_id=t.id,
                    t0_mcap=411.0,
                    max_mcap=15_000.0,
                    multiple=36.5,
                    label=1,
                    last_liq=4_000.0,
                )
            )
        pvp = Token(mint="PvpHonestMint1111", symbol="PVP", source="poll", first_seen_at=utcnow())
        pvp.research = Research(p_good=0.09, holder_count=138, features_json="{}", risk_flags_json="[]")
        session.add(pvp)
        session.flush()
        session.add(
            Outcome(
                token_id=pvp.id,
                t0_mcap=40_486.0,
                max_mcap=332_666.0,
                multiple=8.22,
                label=1,
                last_liq=47_549.0,
            )
        )
        session.add(Snapshot(token_id=pvp.id, kind="t0", mcap_usd=40_486.0, liquidity_usd=15_284.0))
        session.add(Snapshot(token_id=pvp.id, kind="early", mcap_usd=223_106.0, liquidity_usd=38_541.0))
        session.add(Snapshot(token_id=pvp.id, kind="post", mcap_usd=332_666.0, liquidity_usd=47_549.0))
    board = asyncio.run(runners(min_multiple=5.0))
    mints = {r["mint"] for r in board["items"]}
    assert "PvpHonestMint1111" in mints
    assert not any(m.startswith("Fake20xMint") for m in mints)
    assert not any(m.startswith("TinyT0Mint") for m in mints)


def test_thin_holder_print_is_not_a_confirmed_runner():
    from launchfinder.scoring.outcomes import confirmed_runner_multiple, is_thin_holder_print

    assert is_thin_holder_print(4) is True
    assert is_thin_holder_print(10, "robinhood") is True
    assert is_thin_holder_print(4, "sol") is True
    assert is_thin_holder_print(5, "sol") is True  # USMS-class 5-wallet wick
    assert is_thin_holder_print(8, "sol") is False
    assert is_thin_holder_print(13, "sol") is False  # Pappy-class Helius sample
    assert is_thin_holder_print(0) is False
    assert is_thin_holder_print(None, "robinhood") is False
    assert is_thin_holder_print(0, "sol") is False  # Helius-miss unknown
    assert is_thin_holder_print(0, "robinhood") is True  # HIMSTER 0-wallet twin
    assert is_thin_holder_print(20) is False
    assert is_thin_holder_print(176) is False

    init_db()
    with session_scope() as session:
        thin = Token(mint="ThinPrintMint1111", symbol="SHORT", source="rh_trenches", chain="robinhood", first_seen_at=utcnow())
        thin.research = Research(p_good=0.16, holder_count=4, features_json="{}", risk_flags_json="[]")
        session.add(thin)
        session.flush()
        session.add(Outcome(token_id=thin.id, t0_mcap=40_000.0, max_mcap=40_000.0 * 6.2, multiple=6.2, label=1, last_liq=12_000.0))
        session.add(Snapshot(token_id=thin.id, kind="t6h", mcap_usd=40_000.0 * 6.2, liquidity_usd=12_000.0, taken_at=utcnow()))

        real = Token(mint="RealRhRunnerMint1", symbol="BRICKED", source="rh_trenches", chain="robinhood", first_seen_at=utcnow())
        real.research = Research(p_good=0.54, holder_count=176, features_json="{}", risk_flags_json="[]")
        session.add(real)
        session.flush()
        session.add(Outcome(token_id=real.id, t0_mcap=40_000.0, max_mcap=40_000.0 * 6.2, multiple=6.2, label=1, last_liq=20_000.0))
        session.add(Snapshot(token_id=real.id, kind="t6h", mcap_usd=40_000.0 * 6.2, liquidity_usd=20_000.0, taken_at=utcnow()))
        session.flush()
        assert confirmed_runner_multiple(session, thin, thin.outcome) == 0.0
        assert confirmed_runner_multiple(session, real, real.outcome) >= 5.0
    board = asyncio.run(runners(min_multiple=5.0, chain="robinhood"))
    mints = {r["mint"] for r in board["items"]}
    assert "ThinPrintMint1111" not in mints
    assert "RealRhRunnerMint1" in mints


def test_sol_two_wallet_print_is_not_a_confirmed_runner():
    # Live ASTEROID: 2 holders, $1.78M print, outcome t0 at the $69k floor.
    from launchfinder.app import runners as runners_ep
    from launchfinder.scoring.outcomes import confirmed_runner_multiple

    init_db()
    with session_scope() as session:
        ast = Token(mint="AsteroidWickMint1", symbol="ASTEROID", source="poll", chain="sol", first_seen_at=utcnow())
        ast.research = Research(p_good=0.11, holder_count=2, features_json="{}", risk_flags_json="[]")
        session.add(ast)
        session.flush()
        session.add(Outcome(token_id=ast.id, t0_mcap=69_000.0, max_mcap=2_001_389.0, multiple=29.0, label=1, last_liq=139_000.0))
        session.add(Snapshot(token_id=ast.id, kind="t0", mcap_usd=4_000.0, liquidity_usd=0.0))
        session.add(Snapshot(token_id=ast.id, kind="t15m", mcap_usd=1_783_565.0, liquidity_usd=131_729.0))
        session.flush()
        assert confirmed_runner_multiple(session, ast, ast.outcome) == 0.0
    board = asyncio.run(runners_ep(min_multiple=5.0))
    assert "AsteroidWickMint1" not in {r["mint"] for r in board["items"]}


def test_sol_five_wallet_print_is_not_a_confirmed_runner():
    # Live USMS: 5 holders, t0 $251k, peak $1.38M / $118k liq. That is a
    # concentrated wick at the old 5-wallet floor, not a Pappy-class sample.
    from launchfinder.app import runners as runners_ep
    from launchfinder.scoring.outcomes import confirmed_runner_multiple

    init_db()
    with session_scope() as session:
        usms = Token(mint="UsmsFiveWallet111", symbol="USMS", source="poll", chain="sol", first_seen_at=utcnow())
        usms.research = Research(p_good=0.77, holder_count=5, features_json="{}", risk_flags_json="[]")
        session.add(usms)
        session.flush()
        session.add(
            Outcome(
                token_id=usms.id,
                t0_mcap=251_229.0,
                max_mcap=1_388_709.0,
                multiple=5.53,
                label=1,
                last_liq=118_776.0,
            )
        )
        session.add(Snapshot(token_id=usms.id, kind="t0", mcap_usd=251_229.0, liquidity_usd=61_464.0))
        session.add(Snapshot(token_id=usms.id, kind="t15m", mcap_usd=1_231_080.0, liquidity_usd=109_113.0))
        session.flush()
        assert confirmed_runner_multiple(session, usms, usms.outcome) == 0.0
    board = asyncio.run(runners_ep(min_multiple=5.0))
    assert "UsmsFiveWallet111" not in {r["mint"] for r in board["items"]}


def test_sol_live_t0_snap_is_the_entry_not_the_graduation_floor():
    # Live FRUG: t0 snap $264k / $52k liq, peak $538k. From the floor that
    # looks like 7.8x; from the live book it is ~2x and not a runner.
    from launchfinder.scoring.outcomes import confirmed_runner_multiple

    init_db()
    with session_scope() as session:
        frug = Token(mint="FrugPrepumpedMint1", symbol="FRUG", source="poll", chain="sol", first_seen_at=utcnow())
        frug.research = Research(p_good=0.75, holder_count=1387, features_json="{}", risk_flags_json="[]")
        session.add(frug)
        session.flush()
        session.add(Outcome(token_id=frug.id, t0_mcap=69_000.0, max_mcap=1_314_660.0, multiple=19.05, label=1, last_liq=63_000.0))
        session.add(Snapshot(token_id=frug.id, kind="t0", mcap_usd=264_524.0, liquidity_usd=52_700.0))
        session.add(Snapshot(token_id=frug.id, kind="t6h", mcap_usd=538_508.0, liquidity_usd=75_930.0))
        session.flush()
        confirmed = confirmed_runner_multiple(session, frug, frug.outcome)
        assert confirmed < 5.0
        assert abs(confirmed - 538_508.0 / 264_524.0) < 0.05


def test_rh_inter_scan_dex_peak_confirms_ssb_class_runner():
    # Live SSB: t0 $22.8k / $12.8k liq, early snap $113k (4.95x), Dex tick
    # $132k written to max_mcap but never snapshotted. Last liq $30k, 205
    # holders. Without the inter-scan fill the board stays empty while the
    # win label already fired. Leftover FDV 10x above the last book stays 0.
    from launchfinder.scoring.outcomes import confirmed_runner_multiple

    init_db()
    with session_scope() as session:
        ssb = Token(
            mint="0x4c52ac723f2799e3a513ca460fb88562a465cccc",
            symbol="SSB",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        ssb.research = Research(p_good=0.51, holder_count=205, features_json="{}", risk_flags_json="[]")
        session.add(ssb)
        session.flush()
        session.add(
            Outcome(
                token_id=ssb.id,
                t0_mcap=22_840.0,
                max_mcap=132_452.0,
                multiple=5.80,
                label=1,
                last_liq=30_909.0,
            )
        )
        session.add(Snapshot(token_id=ssb.id, kind="t0", mcap_usd=22_840.0, liquidity_usd=12_834.0))
        session.add(Snapshot(token_id=ssb.id, kind="early", mcap_usd=113_027.0, liquidity_usd=28_550.0))

        leftover = Token(
            mint="0xleftoverfdv0000000000000000000000000001",
            symbol="STLSPCXMTGIN",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        leftover.research = Research(p_good=0.2, holder_count=80, features_json="{}", risk_flags_json="[]")
        session.add(leftover)
        session.flush()
        session.add(
            Outcome(
                token_id=leftover.id,
                t0_mcap=40_000.0,
                max_mcap=400_000.0,
                multiple=10.0,
                label=1,
                last_liq=15_000.0,
            )
        )
        session.add(Snapshot(token_id=leftover.id, kind="early", mcap_usd=25_000.0, liquidity_usd=8_000.0))

        thin = Token(
            mint="0xthinssb000000000000000000000000000000001",
            symbol="THINSSB",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        thin.research = Research(p_good=0.16, holder_count=4, features_json="{}", risk_flags_json="[]")
        session.add(thin)
        session.flush()
        session.add(
            Outcome(
                token_id=thin.id,
                t0_mcap=22_840.0,
                max_mcap=132_452.0,
                multiple=5.80,
                label=1,
                last_liq=30_909.0,
            )
        )
        session.add(Snapshot(token_id=thin.id, kind="early", mcap_usd=113_027.0, liquidity_usd=28_550.0))
        session.flush()

        confirmed = confirmed_runner_multiple(session, ssb, ssb.outcome)
        assert confirmed >= 5.0
        assert abs(confirmed - 132_452.0 / 22_840.0) < 0.05
        # Book later dumped under the $5k runner-liq line. Peak snaps still
        # prove the 5.8x — do not un-confirm (live last_liq $4,049).
        ssb.outcome.last_liq = 4_049.0
        session.flush()
        dumped = confirmed_runner_multiple(session, ssb, ssb.outcome)
        assert dumped >= 5.0
        assert abs(dumped - 132_452.0 / 22_840.0) < 0.05
        assert confirmed_runner_multiple(session, leftover, leftover.outcome) < 5.0
        assert confirmed_runner_multiple(session, thin, thin.outcome) == 0.0

    board = asyncio.run(runners(min_multiple=5.0, chain="robinhood"))
    mints = {r["mint"] for r in board["items"]}
    assert "0x4c52ac723f2799e3a513ca460fb88562a465cccc" in mints
    assert "0xleftoverfdv0000000000000000000000000001" not in mints
    assert "0xthinssb000000000000000000000000000000001" not in mints


def test_sol_inter_scan_dex_peak_confirms_maxi_class_runner():
    # Live Maxi: t0 $29k (0 liq), early $96k / $25k liq (3.3x), Dex $159k
    # written to max_mcap (5.45x) / last_liq $33k, 154 holders. Slack 1.35
    # rejected 159/96 = 1.66. Leftover 16x FDV above the last book stays out.
    from launchfinder.scoring.outcomes import confirmed_runner_multiple

    init_db()
    with session_scope() as session:
        maxi = Token(
            mint="MaxiHonestMint111",
            symbol="Maxi",
            source="gmgn_trenches",
            chain="sol",
            first_seen_at=utcnow(),
        )
        maxi.research = Research(p_good=0.21, holder_count=154, features_json="{}", risk_flags_json="[]")
        session.add(maxi)
        session.flush()
        session.add(
            Outcome(
                token_id=maxi.id,
                t0_mcap=29_169.6,
                max_mcap=159_026.0,
                multiple=5.45,
                label=1,
                last_liq=33_396.0,
            )
        )
        session.add(Snapshot(token_id=maxi.id, kind="t0", mcap_usd=29_169.6, liquidity_usd=0.0))
        session.add(Snapshot(token_id=maxi.id, kind="early", mcap_usd=96_039.0, liquidity_usd=25_360.0))

        leftover = Token(
            mint="SolLeftoverFdv111",
            symbol="FAKE16X",
            source="poll",
            chain="sol",
            first_seen_at=utcnow(),
        )
        leftover.research = Research(p_good=0.2, holder_count=80, features_json="{}", risk_flags_json="[]")
        session.add(leftover)
        session.flush()
        session.add(
            Outcome(
                token_id=leftover.id,
                t0_mcap=69_000.0,
                max_mcap=400_000.0,
                multiple=5.8,
                label=1,
                last_liq=15_000.0,
            )
        )
        session.add(Snapshot(token_id=leftover.id, kind="early", mcap_usd=25_000.0, liquidity_usd=8_000.0))
        session.flush()

        confirmed = confirmed_runner_multiple(session, maxi, maxi.outcome)
        assert confirmed >= 5.0
        assert abs(confirmed - 159_026.0 / 29_169.6) < 0.05
        assert confirmed_runner_multiple(session, leftover, leftover.outcome) < 5.0

    board = asyncio.run(runners(min_multiple=5.0))
    mints = {r["mint"] for r in board["items"]}
    assert "MaxiHonestMint111" in mints
    assert "SolLeftoverFdv111" not in mints


def test_rh_inter_scan_dex_peak_confirms_pov_class_runner():
    # Live POV: t0 $13k / $12k liq, t1h $45k / $30k liq (3.47x), Dex $121k
    # = 9.23x / last_liq $55k / 62 holders. Slack 2.0 rejected 121/45 = 2.66.
    # Leftover 16x FDV above the last book stays out.
    from launchfinder.scoring.outcomes import confirmed_runner_multiple

    init_db()
    with session_scope() as session:
        pov = Token(
            mint="0xpovhonest000000000000000000000000000001",
            symbol="POV",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        pov.research = Research(p_good=0.36, holder_count=62, features_json="{}", risk_flags_json="[]")
        session.add(pov)
        session.flush()
        session.add(
            Outcome(
                token_id=pov.id,
                t0_mcap=13_134.0,
                max_mcap=121_227.0,
                multiple=9.23,
                label=1,
                last_liq=54_798.0,
            )
        )
        session.add(Snapshot(token_id=pov.id, kind="t0", mcap_usd=13_134.0, liquidity_usd=12_611.0))
        session.add(Snapshot(token_id=pov.id, kind="t1h", mcap_usd=45_569.0, liquidity_usd=30_256.0))

        leftover = Token(
            mint="0xrhleftover16x00000000000000000000000001",
            symbol="FAKE16X",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        leftover.research = Research(p_good=0.2, holder_count=80, features_json="{}", risk_flags_json="[]")
        session.add(leftover)
        session.flush()
        session.add(
            Outcome(
                token_id=leftover.id,
                t0_mcap=40_000.0,
                max_mcap=400_000.0,
                multiple=10.0,
                label=1,
                last_liq=15_000.0,
            )
        )
        session.add(Snapshot(token_id=leftover.id, kind="early", mcap_usd=25_000.0, liquidity_usd=8_000.0))
        session.flush()

        confirmed = confirmed_runner_multiple(session, pov, pov.outcome)
        assert confirmed >= 5.0
        assert abs(confirmed - 121_227.0 / 13_134.0) < 0.05
        assert confirmed_runner_multiple(session, leftover, leftover.outcome) < 5.0

    board = asyncio.run(runners(min_multiple=5.0, chain="robinhood"))
    mints = {r["mint"] for r in board["items"]}
    assert "0xpovhonest000000000000000000000000000001" in mints
    assert "0xrhleftover16x00000000000000000000000001" not in mints


def test_approaching_board_splits_thin_prints():
    from launchfinder.app import approaching

    init_db()
    with session_scope() as session:
        brick = Token(mint="0xbrick0000000000000000000000000000000001", symbol="BRICKED", source="rh_trenches", chain="robinhood", first_seen_at=utcnow())
        brick.research = Research(p_good=0.54, holder_count=176, features_json="{}", risk_flags_json="[]")
        session.add(brick)
        session.flush()
        session.add(Outcome(token_id=brick.id, t0_mcap=41_774.0, max_mcap=101_919.0, multiple=2.44, last_liq=18_000.0))

        short = Token(mint="0xshort0000000000000000000000000000000001", symbol="SHORT", source="rh_trenches", chain="robinhood", first_seen_at=utcnow())
        short.research = Research(p_good=0.16, holder_count=4, features_json="{}", risk_flags_json="[]")
        session.add(short)
        session.flush()
        session.add(Outcome(token_id=short.id, t0_mcap=40_000.0, max_mcap=156_940.0, multiple=3.92, last_liq=9_000.0))
    board = asyncio.run(approaching(min_multiple=2.0, chain="robinhood"))
    honest = {r["symbol"] for r in board["items"]}
    prints = {r["symbol"] for r in board["thin"]}
    assert "BRICKED" in honest
    assert "SHORT" in prints
    assert "SHORT" not in honest
    assert board["approaching"] >= 1
    assert board["thin_prints"] >= 1


def test_approaching_drops_one_wallet_prints():
    # Live 10:21: BEAVER 1w / 3.68× / $58k sat RH thin prints.
    # A 1–2 wallet book is not a 5–50x climb. SHORT 4w thin stays.
    # Unknown (0) holders stay.
    from launchfinder.app import approaching

    init_db()
    with session_scope() as session:
        beaver = Token(
            mint="0xrhbeaveronewalletapp000000000000001",
            symbol="BVR1W",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        beaver.research = Research(p_good=0.05, holder_count=1, features_json="{}", risk_flags_json="[]")
        session.add(beaver)
        session.flush()
        session.add(Outcome(token_id=beaver.id, t0_mcap=15_800.0, max_mcap=58_117.0, multiple=3.68, last_liq=58_117.39))

        short = Token(
            mint="0xrhshortfourwalletapp000000000000001",
            symbol="SH4W",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        short.research = Research(p_good=0.16, holder_count=4, features_json="{}", risk_flags_json="[]")
        session.add(short)
        session.flush()
        session.add(Outcome(token_id=short.id, t0_mcap=40_000.0, max_mcap=156_940.0, multiple=3.92, last_liq=9_000.0))

        ghost = Token(
            mint="0xrhunknownholdersapp0000000000000001",
            symbol="UNK0W",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        ghost.research = Research(p_good=0.40, holder_count=0, features_json="{}", risk_flags_json="[]")
        session.add(ghost)
        session.flush()
        session.add(Outcome(token_id=ghost.id, t0_mcap=22_000.0, max_mcap=48_000.0, multiple=2.18, last_liq=16_000.0))
    board = asyncio.run(approaching(min_multiple=2.0, chain="robinhood"))
    honest = {r["symbol"] for r in board["items"]}
    prints = {r["symbol"] for r in board["thin"]}
    assert "BVR1W" not in honest
    assert "BVR1W" not in prints
    assert "SH4W" in prints
    assert "UNK0W" in prints or "UNK0W" in honest


def test_approaching_drops_aged_thin_three_x():
    # Live 10:40: CLOUDING 19w / 3.49× / $79k sat RH thin prints
    # after 2h. A thin 3×+ that already sat is hunt tape, not a
    # 5–50x approach. This-window thin 2.5× stays. Honest fat 3×
    # (007-class) stays.
    from launchfinder.app import approaching

    init_db()
    now = utcnow()
    with session_scope() as session:
        aged = Token(
            mint="0xrhcloudingagedthin3x000000000000001",
            symbol="CLD3X",
            source="rh_trenches",
            chain="robinhood",
            created_at_chain=now - timedelta(hours=2, minutes=10),
            first_seen_at=now - timedelta(hours=2, minutes=10),
        )
        aged.research = Research(p_good=0.03, holder_count=19, features_json="{}", risk_flags_json="[]")
        session.add(aged)
        session.flush()
        session.add(Outcome(token_id=aged.id, t0_mcap=22_772.0, max_mcap=79_442.0, multiple=3.49, last_liq=79_515.36))

        young = Token(
            mint="0xrhthiswindowthin25x0000000000000001",
            symbol="THN25",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=now,
        )
        young.research = Research(p_good=0.16, holder_count=4, features_json="{}", risk_flags_json="[]")
        session.add(young)
        session.flush()
        session.add(Outcome(token_id=young.id, t0_mcap=40_000.0, max_mcap=100_000.0, multiple=2.50, last_liq=9_000.0))

        honest = Token(
            mint="0xrh007fatthreexkeep0000000000000001",
            symbol="HON3X",
            source="rh_trenches",
            chain="robinhood",
            created_at_chain=now - timedelta(hours=5),
            first_seen_at=now - timedelta(hours=5),
        )
        honest.research = Research(p_good=0.92, holder_count=578, features_json="{}", risk_flags_json="[]")
        session.add(honest)
        session.flush()
        session.add(Outcome(token_id=honest.id, t0_mcap=55_591.0, max_mcap=170_631.0, multiple=3.07, last_liq=17_923.63))
    board = asyncio.run(approaching(min_multiple=2.0, chain="robinhood"))
    honest_syms = {r["symbol"] for r in board["items"]}
    prints = {r["symbol"] for r in board["thin"]}
    assert "CLD3X" not in honest_syms
    assert "CLD3X" not in prints
    assert "THN25" in prints
    assert "HON3X" in honest_syms


def test_sol_approaching_splits_sub_15_holder_prints():
    # WWR/WOFI-class: 7-8 wallets printing 4.5x is not an honest 5x climb.
    # Runner floor is 8 wallets on Solana (Pappy 13 still counts; USMS 5 does not).
    from launchfinder.app import approaching
    from launchfinder.scoring.outcomes import is_thin_approaching_print, is_thin_holder_print

    assert is_thin_holder_print(7, "sol") is True
    assert is_thin_approaching_print(7, "sol") is True
    assert is_thin_approaching_print(12, "sol") is True
    assert is_thin_approaching_print(32, "sol") is False
    assert is_thin_approaching_print(0, "sol") is False
    assert is_thin_approaching_print(0, "robinhood") is True
    assert is_thin_approaching_print(None, "robinhood") is False

    init_db()
    with session_scope() as session:
        wwr = Token(mint="WwrThinMint111111", symbol="WWR", source="poll", chain="sol", first_seen_at=utcnow())
        wwr.research = Research(p_good=0.66, holder_count=7, features_json="{}", risk_flags_json="[]")
        session.add(wwr)
        session.flush()
        session.add(Outcome(token_id=wwr.id, t0_mcap=69_000.0, max_mcap=69_000.0 * 4.54, multiple=4.54, last_liq=1_885.0))

        bunny = Token(mint="BunnyClimbMint111", symbol="Bunny", source="poll", chain="sol", first_seen_at=utcnow())
        bunny.research = Research(p_good=0.90, holder_count=607, features_json="{}", risk_flags_json="[]")
        session.add(bunny)
        session.flush()
        session.add(Outcome(token_id=bunny.id, t0_mcap=69_000.0, max_mcap=69_000.0 * 4.98, multiple=4.98, last_liq=3_300.0))
    board = asyncio.run(approaching(min_multiple=2.0, chain="sol"))
    honest = {r["symbol"] for r in board["items"]}
    prints = {r["symbol"] for r in board["thin"]}
    assert "Bunny" in honest
    assert "WWR" in prints
    assert "WWR" not in honest


def test_sol_tiny_t0_is_not_an_approaching_climb():
    # Live ARROW/RST/WWR: t0=$410.84 (SOL graduation stored as USD) and
    # max~$1.8k looks like 4.5x. From the $69k floor it is 0.03x.
    from launchfinder.app import approaching
    from launchfinder.scoring.outcomes import repair_sol_tiny_t0

    init_db()
    with session_scope() as session:
        arrow = Token(mint="ArrowSolUnitMint1", symbol="ARROW", source="poll", chain="sol", first_seen_at=utcnow())
        arrow.research = Research(p_good=0.22, holder_count=57, features_json="{}", risk_flags_json="[]")
        session.add(arrow)
        session.flush()
        session.add(Outcome(token_id=arrow.id, t0_mcap=410.84, max_mcap=1_888.0, multiple=4.60, last_liq=1_797.0))
        bunny = Token(mint="BunnyHonestMint11", symbol="Bunny", source="poll", chain="sol", first_seen_at=utcnow())
        bunny.research = Research(p_good=0.90, holder_count=607, features_json="{}", risk_flags_json="[]")
        session.add(bunny)
        session.flush()
        session.add(Outcome(token_id=bunny.id, t0_mcap=28_314.0, max_mcap=141_099.0, multiple=4.98, last_liq=3_455.0))
        session.flush()
        assert repair_sol_tiny_t0(session) >= 1
        fixed = session.query(Outcome).filter(Outcome.token_id == arrow.id).one()
        assert fixed.t0_mcap == 69_000.0
        assert fixed.multiple < 0.1
    board = asyncio.run(approaching(min_multiple=2.0, chain="sol"))
    mints = {r["mint"] for r in board["items"]} | {r["mint"] for r in board["thin"]}
    assert "ArrowSolUnitMint1" not in mints
    assert "BunnyHonestMint11" in {r["mint"] for r in board["items"]}


def test_approaching_drops_flash_print_multiples():
    from launchfinder.app import approaching

    init_db()
    with session_scope() as session:
        fake = Token(mint="ArrowFlashMint111", symbol="ARROW", source="poll", first_seen_at=utcnow())
        fake.research = Research(p_good=0.84, holder_count=37, features_json="{}", risk_flags_json="[]")
        session.add(fake)
        session.flush()
        session.add(Outcome(token_id=fake.id, t0_mcap=69_000.0, max_mcap=69_000.0 * 827908.0, multiple=827908.0, last_liq=40_000.0))
        real = Token(mint="HonestClimbMint11", symbol="CLIMB", source="poll", first_seen_at=utcnow())
        real.research = Research(p_good=0.6, holder_count=80, features_json="{}", risk_flags_json="[]")
        session.add(real)
        session.flush()
        session.add(Outcome(token_id=real.id, t0_mcap=69_000.0, max_mcap=69_000.0 * 3.2, multiple=3.2, last_liq=22_000.0))
    board = asyncio.run(approaching(min_multiple=2.0, chain="sol"))
    mints = {r["mint"] for r in board["items"]}
    assert "ArrowFlashMint111" not in mints
    assert "HonestClimbMint11" in mints


def test_approaching_ignores_stale_climbs():
    from datetime import timedelta

    from launchfinder.app import approaching

    init_db()
    with session_scope() as session:
        old = Token(
            mint="StaleClimbMint111",
            symbol="CUCK",
            source="poll",
            first_seen_at=utcnow() - timedelta(days=5),
        )
        old.research = Research(p_good=0.7, holder_count=1400, features_json="{}", risk_flags_json="[]")
        session.add(old)
        session.flush()
        session.add(Outcome(token_id=old.id, t0_mcap=69_000.0, max_mcap=69_000.0 * 12.0, multiple=12.0, last_liq=40_000.0))
        fresh = Token(mint="FreshClimbMint111", symbol="BRENT", source="poll", first_seen_at=utcnow())
        fresh.research = Research(p_good=0.67, holder_count=200, features_json="{}", risk_flags_json="[]")
        session.add(fresh)
        session.flush()
        session.add(Outcome(token_id=fresh.id, t0_mcap=69_000.0, max_mcap=69_000.0 * 2.8, multiple=2.8, last_liq=44_000.0))
    board = asyncio.run(approaching(min_multiple=2.0, chain="sol"))
    mints = {r["mint"] for r in board["items"]}
    assert "StaleClimbMint111" not in mints
    assert "FreshClimbMint111" in mints
    assert board["window_hours"] == 48.0


def test_approaching_drops_zero_liq_ghosts():
    # Live 06:23 Sol approaching: stonkape 4.15× / Bros 2.16× with
    # last_liq $0 sat next to ANGRYCATS $30k. Mid-book $400 (PSYOPED)
    # stays; a missing pool is not a climb.
    from launchfinder.app import approaching

    init_db()
    with session_scope() as session:
        ghost = Token(
            mint="StonkZeroLiqMint1111111111111111111",
            symbol="STONKAPE",
            source="poll",
            chain="sol",
            first_seen_at=utcnow(),
        )
        ghost.research = Research(p_good=0.13, holder_count=60, features_json="{}", risk_flags_json="[]")
        session.add(ghost)
        session.flush()
        session.add(Outcome(token_id=ghost.id, t0_mcap=69_000.0, max_mcap=69_000.0 * 4.15, multiple=4.15, last_liq=0.0))
        mid = Token(
            mint="MidLiqStillOkMint11111111111111111",
            symbol="PSYKEEP",
            source="poll",
            chain="sol",
            first_seen_at=utcnow(),
        )
        mid.research = Research(p_good=0.15, holder_count=347, features_json="{}", risk_flags_json="[]")
        session.add(mid)
        session.flush()
        session.add(Outcome(token_id=mid.id, t0_mcap=35_336.0, max_mcap=173_500.0, multiple=4.91, last_liq=400.0))
        real = Token(
            mint="AngryCatsRealLiqMint1111111111111",
            symbol="ANGRYKEEP",
            source="poll",
            chain="sol",
            first_seen_at=utcnow(),
        )
        real.research = Research(p_good=0.60, holder_count=93, features_json="{}", risk_flags_json="[]")
        session.add(real)
        session.flush()
        session.add(Outcome(token_id=real.id, t0_mcap=69_000.0, max_mcap=69_000.0 * 3.04, multiple=3.04, last_liq=30_812.0))
    board = asyncio.run(approaching(min_multiple=2.0, chain="sol"))
    mints = {r["mint"] for r in board["items"]} | {r["mint"] for r in board["thin"]}
    assert "StonkZeroLiqMint1111111111111111111" not in mints
    assert "MidLiqStillOkMint11111111111111111" in {r["mint"] for r in board["items"]}
    assert "AngryCatsRealLiqMint1111111111111" in {r["mint"] for r in board["items"]}


def test_approaching_drops_8h_skinny_dumps():
    # Live 08:20: ☉ 13.8h / 3.70× / $3.2k sat Sol approaching after
    # hunt buried the skinny dump at #187. Live 11:11: TriplePONS
    # 4.9h / 2.40× / $4.2k sat #7 under the 8h clock. PSYOPED $400
    # this-window stays. Fat 8h+ climbs stay. Do not restore ☉ hunt.
    from launchfinder.app import approaching

    init_db()
    with session_scope() as session:
        dumped = Token(
            mint="SunSkinnyEightHDumpMint11111111111",
            symbol="SUNDUMP",
            source="poll",
            chain="sol",
            created_at_chain=utcnow() - timedelta(hours=13, minutes=46),
            first_seen_at=utcnow() - timedelta(hours=13, minutes=46),
        )
        dumped.research = Research(p_good=0.92, holder_count=762, features_json="{}", risk_flags_json="[]")
        session.add(dumped)
        session.flush()
        session.add(
            Outcome(
                token_id=dumped.id,
                t0_mcap=38_854.0,
                max_mcap=143_762.0,
                multiple=3.70,
                last_liq=3_165.0,
            )
        )
        mid = Token(
            mint="PsyopedMidLiqThisWindowMint1111111",
            symbol="PSYKEEP8",
            source="poll",
            chain="sol",
            created_at_chain=utcnow() - timedelta(minutes=40),
            first_seen_at=utcnow() - timedelta(minutes=40),
        )
        mid.research = Research(p_good=0.15, holder_count=347, features_json="{}", risk_flags_json="[]")
        session.add(mid)
        session.flush()
        session.add(Outcome(token_id=mid.id, t0_mcap=35_336.0, max_mcap=173_500.0, multiple=4.91, last_liq=400.0))
        fat_old = Token(
            mint="MvcFatEightHClimbMint1111111111111",
            symbol="MVC8H",
            source="poll",
            chain="sol",
            created_at_chain=utcnow() - timedelta(hours=11, minutes=20),
            first_seen_at=utcnow() - timedelta(hours=11, minutes=20),
        )
        fat_old.research = Research(p_good=0.24, holder_count=192, features_json="{}", risk_flags_json="[]")
        session.add(fat_old)
        session.flush()
        session.add(Outcome(token_id=fat_old.id, t0_mcap=40_854.0, max_mcap=144_802.0, multiple=3.54, last_liq=26_791.0))
        early = Token(
            mint="TriplePonsFourHSkinnyMint111111111",
            symbol="TP4H",
            source="poll",
            chain="sol",
            created_at_chain=utcnow() - timedelta(hours=4, minutes=55),
            first_seen_at=utcnow() - timedelta(hours=4, minutes=55),
        )
        early.research = Research(p_good=0.03, holder_count=51, features_json="{}", risk_flags_json="[]")
        session.add(early)
        session.flush()
        session.add(Outcome(token_id=early.id, t0_mcap=49_350.0, max_mcap=118_443.0, multiple=2.40, last_liq=4_275.87))
    board = asyncio.run(approaching(min_multiple=2.0, chain="sol"))
    mints = {r["mint"] for r in board["items"]} | {r["mint"] for r in board["thin"]}
    assert "SunSkinnyEightHDumpMint11111111111" not in mints
    assert "TriplePonsFourHSkinnyMint111111111" not in mints
    assert "PsyopedMidLiqThisWindowMint1111111" in {r["mint"] for r in board["items"]}
    assert "MvcFatEightHClimbMint1111111111111" in {r["mint"] for r in board["items"]}


def test_approaching_drops_stale_sub_three_x():
    # Live 11:21: MONK 17.7h / 2.69× sat Sol approaching + conviction
    # B. MEMECITY 12.8h / 2.27× and RICHDEBT 9h / 2.31× sat with it.
    # An 8h+ book still under 3× is leftover tape. This-window 2×
    # (BEN) stays. Honest 3×+ (007) stays.
    from launchfinder.app import approaching

    init_db()
    with session_scope() as session:
        stale = Token(
            mint="MonkStaleSubThreeXMint11111111111",
            symbol="MONK8H",
            source="poll",
            chain="sol",
            created_at_chain=utcnow() - timedelta(hours=17, minutes=40),
            first_seen_at=utcnow() - timedelta(hours=17, minutes=40),
        )
        stale.research = Research(p_good=0.84, holder_count=102, features_json="{}", risk_flags_json="[]")
        session.add(stale)
        session.flush()
        session.add(Outcome(token_id=stale.id, t0_mcap=131_700.0, max_mcap=354_271.0, multiple=2.69, last_liq=19_658.04))

        young = Token(
            mint="BenThisWindowTwoXMint11111111111",
            symbol="BEN2X",
            source="poll",
            chain="sol",
            created_at_chain=utcnow() - timedelta(hours=2, minutes=40),
            first_seen_at=utcnow() - timedelta(hours=2, minutes=40),
        )
        young.research = Research(p_good=0.28, holder_count=345, features_json="{}", risk_flags_json="[]")
        session.add(young)
        session.flush()
        session.add(Outcome(token_id=young.id, t0_mcap=51_515.0, max_mcap=193_338.0, multiple=2.43, last_liq=42_281.84))

        honest = Token(
            mint="OhOhSevenFatThreeXMint1111111111",
            symbol="HON3X8",
            source="rh_pons",
            chain="robinhood",
            created_at_chain=utcnow() - timedelta(hours=5, minutes=40),
            first_seen_at=utcnow() - timedelta(hours=5, minutes=40),
        )
        honest.research = Research(p_good=0.92, holder_count=578, features_json="{}", risk_flags_json="[]")
        session.add(honest)
        session.flush()
        session.add(Outcome(token_id=honest.id, t0_mcap=55_591.0, max_mcap=170_631.0, multiple=3.07, last_liq=19_071.36))

        aged_rh = Token(
            mint="0xrhhastalesubthreex000000000000001",
            symbol="HA8H",
            source="rh_trenches",
            chain="robinhood",
            created_at_chain=utcnow() - timedelta(hours=12, minutes=20),
            first_seen_at=utcnow() - timedelta(hours=12, minutes=20),
        )
        aged_rh.research = Research(p_good=0.12, holder_count=32, features_json="{}", risk_flags_json="[]")
        session.add(aged_rh)
        session.flush()
        session.add(Outcome(token_id=aged_rh.id, t0_mcap=34_652.0, max_mcap=87_459.0, multiple=2.52, last_liq=65_701.26))
    sol = asyncio.run(approaching(min_multiple=2.0, chain="sol"))
    sol_mints = {r["mint"] for r in sol["items"]} | {r["mint"] for r in sol["thin"]}
    assert "MonkStaleSubThreeXMint11111111111" not in sol_mints
    assert "BenThisWindowTwoXMint11111111111" in sol_mints
    rh = asyncio.run(approaching(min_multiple=2.0, chain="robinhood"))
    rh_mints = {r["mint"] for r in rh["items"]} | {r["mint"] for r in rh["thin"]}
    assert "OhOhSevenFatThreeXMint1111111111" in rh_mints
    assert "0xrhhastalesubthreex000000000000001" not in rh_mints


def test_approaching_includes_mid_liq_climbs():
    """Live 2-5x names with last_liq in the old (0, 800) gap were dropped
    from approaching while still on the 4h table (PSYOPED/SQUISHY)."""
    from launchfinder.app import approaching

    init_db()
    with session_scope() as session:
        t = Token(mint="MidLiqClimbMint11", symbol="PSYOPED", source="poll", first_seen_at=utcnow())
        t.research = Research(p_good=0.15, holder_count=347, features_json="{}", risk_flags_json="[]")
        session.add(t)
        session.flush()
        session.add(Outcome(token_id=t.id, t0_mcap=35_336.0, max_mcap=173_500.0, multiple=4.91, last_liq=400.0))
    with session_scope() as session:
        # 200+ older 70x rows used to fill the SQL window so 2.4x never appeared.
        for i in range(30):
            t = Token(mint=f"BigPrintMint{i:03d}111", symbol=f"BIG{i}", source="poll", first_seen_at=utcnow())
            t.research = Research(p_good=0.4, holder_count=100, features_json="{}", risk_flags_json="[]")
            session.add(t)
            session.flush()
            session.add(Outcome(token_id=t.id, t0_mcap=69_000.0, max_mcap=69_000.0 * 20.0, multiple=20.0, last_liq=20_000.0))
    board = asyncio.run(approaching(min_multiple=2.0, chain="sol"))
    assert any(r["mint"] == "MidLiqClimbMint11" for r in board["items"])
    assert all(r["multiple"] < 5 for r in board["items"])


def test_approaching_excludes_names_already_past_the_win_bar():
    from launchfinder.app import approaching

    init_db()
    with session_scope() as session:
        ran = Token(mint="AlreadyFiveXMint1", symbol="CUCK", source="poll", first_seen_at=utcnow())
        ran.research = Research(p_good=0.7, holder_count=1400, features_json="{}", risk_flags_json="[]")
        session.add(ran)
        session.flush()
        session.add(Outcome(token_id=ran.id, t0_mcap=69_000.0, max_mcap=69_000.0 * 12.0, multiple=12.0, last_liq=40_000.0))
        climb = Token(mint="StillClimbingMint", symbol="BRENT", source="poll", first_seen_at=utcnow())
        climb.research = Research(p_good=0.67, holder_count=200, features_json="{}", risk_flags_json="[]")
        session.add(climb)
        session.flush()
        session.add(Outcome(token_id=climb.id, t0_mcap=69_000.0, max_mcap=69_000.0 * 2.8, multiple=2.8, last_liq=44_000.0))
    board = asyncio.run(approaching(min_multiple=2.0, chain="sol"))
    mints = {r["mint"] for r in board["items"]}
    assert "AlreadyFiveXMint1" not in mints
    assert "StillClimbingMint" in mints


def test_approaching_drops_dumped_wick_after_live_snap():
    # Live FOMODOG: t0 $50k -> max $196k (3.87x) then the book dumped to $3.4k.
    from datetime import timedelta

    from launchfinder.app import approaching

    init_db()
    with session_scope() as session:
        wick = Token(
            mint="0xfomodogwick000000000000000000000000001",
            symbol="FOMODOG",
            source="rh_pons",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        wick.research = Research(p_good=0.61, holder_count=1131, features_json="{}", risk_flags_json="[]")
        session.add(wick)
        session.flush()
        session.add(
            Outcome(
                token_id=wick.id,
                t0_mcap=50_802.0,
                max_mcap=196_834.0,
                multiple=3.87,
                last_liq=3_468.0,
            )
        )
        now = utcnow()
        session.add(Snapshot(token_id=wick.id, kind="t0", mcap_usd=50_802.0, liquidity_usd=22_723.0, volume_h1=10_141.0, taken_at=now - timedelta(minutes=40)))
        session.add(Snapshot(token_id=wick.id, kind="t15m", mcap_usd=194_783.0, liquidity_usd=47_849.0, volume_h1=26_741.0, taken_at=now - timedelta(minutes=20)))
        session.add(Snapshot(token_id=wick.id, kind="early", mcap_usd=3_438.0, liquidity_usd=3_468.0, volume_h1=49_082.0, taken_at=now))

        climb = Token(
            mint="0xboostersstillup00000000000000000000001",
            symbol="BOOSTER",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        climb.research = Research(p_good=0.68, holder_count=112, features_json="{}", risk_flags_json="[]")
        session.add(climb)
        session.flush()
        session.add(
            Outcome(
                token_id=climb.id,
                t0_mcap=20_000.0,
                max_mcap=51_200.0,
                multiple=2.56,
                last_liq=35_000.0,
            )
        )
        session.add(Snapshot(token_id=climb.id, kind="t0", mcap_usd=20_000.0, liquidity_usd=18_000.0, volume_h1=8_000.0))
        session.add(Snapshot(token_id=climb.id, kind="early", mcap_usd=51_200.0, liquidity_usd=35_000.0, volume_h1=12_000.0))
    board = asyncio.run(approaching(min_multiple=2.0, chain="robinhood"))
    mints = {r["mint"] for r in board["items"]} | {r["mint"] for r in board["thin"]}
    assert "0xfomodogwick000000000000000000000000001" not in mints
    assert "0xboostersstillup00000000000000000000001" in {r["mint"] for r in board["items"]}


def test_approaching_prefers_dex_last_over_stale_climb_snap():
    # Live discat: last $3.5k / t0 $136k / last_liq $3.6k after a Dex
    # rewrite, but the last non-ghost snap was still $274k so
    # approaching_live_multiple printed 2.01× and the dump sat on the
    # climb list. Prefer outcome.last_mcap. A fat this-window climb
    # whose last still matches the book stays.
    from launchfinder.app import approaching

    init_db()
    now = utcnow()
    dump = "BygwL3nfUy2gwUkLyWCeVT6rWVKSbDRaSek2symtpump"
    climb = "2RX8rVoMSCQkDMKeDLNBRc2mXDoTevx77T8gTQ4KkdPU"
    with session_scope() as session:
        discat = Token(
            mint=dump,
            symbol="discat",
            source="poll",
            chain="sol",
            created_at_chain=now - timedelta(hours=1, minutes=20),
            first_seen_at=now - timedelta(hours=1, minutes=19),
        )
        discat.research = Research(p_good=0.44, holder_count=25, features_json="{}", risk_flags_json="[]")
        session.add(discat)
        session.flush()
        session.add(
            Outcome(
                token_id=discat.id,
                t0_mcap=136_249.57,
                max_mcap=530_387.0,
                last_mcap=3_528.0,
                multiple=3.89,
                last_liq=3_576.78,
            )
        )
        session.add(
            Snapshot(
                token_id=discat.id,
                kind="early",
                mcap_usd=274_000.0,
                liquidity_usd=45_000.0,
                volume_h1=20_000.0,
                taken_at=now - timedelta(minutes=40),
            )
        )
        cyber = Token(
            mint=climb,
            symbol="CYBER",
            source="poll",
            chain="sol",
            created_at_chain=now - timedelta(hours=2),
            first_seen_at=now - timedelta(hours=2),
        )
        cyber.research = Research(p_good=0.82, holder_count=180, features_json="{}", risk_flags_json="[]")
        session.add(cyber)
        session.flush()
        session.add(
            Outcome(
                token_id=cyber.id,
                t0_mcap=49_886.0,
                max_mcap=211_900.0,
                last_mcap=128_941.0,
                multiple=4.25,
                last_liq=30_174.28,
            )
        )
        session.add(
            Snapshot(
                token_id=cyber.id,
                kind="early",
                mcap_usd=128_941.0,
                liquidity_usd=30_174.28,
                volume_h1=15_000.0,
                taken_at=now,
            )
        )
    board = asyncio.run(approaching(min_multiple=2.0, hours=48.0, chain="sol"))
    mints = {row["mint"] for row in board["items"]} | {row["mint"] for row in board["thin"]}
    assert dump not in mints
    assert climb in {row["mint"] for row in board["items"]}


def test_approaching_keeps_fat_this_window_when_live_snap_dumps():
    # Live 21:13: TikZ 2.30× / $18k / 0.3h and ONBOARDING 4.89× /
    # $29k sat hunt while approaching used a dumped live snap
    # under 2×. Fat this-window 2×+ stays. FOMODOG $3.4k skinny
    # still drops. 007 15.6h stays off.
    from launchfinder.app import approaching

    init_db()
    with session_scope() as session:
        now = utcnow()
        tikz = Token(
            mint="SolTikZFatThisWindowClimbMint11111",
            symbol="TIKZ2X",
            source="poll",
            chain="sol",
            created_at_chain=now - timedelta(minutes=20),
            first_seen_at=now - timedelta(minutes=18),
        )
        tikz.research = Research(p_good=0.92, holder_count=252, features_json="{}", risk_flags_json="[]")
        session.add(tikz)
        session.flush()
        session.add(Outcome(token_id=tikz.id, t0_mcap=42_095.0, max_mcap=96_851.0, multiple=2.30, last_liq=17_968.0))
        session.add(Snapshot(token_id=tikz.id, kind="t0", mcap_usd=42_095.0, liquidity_usd=16_600.0, volume_h1=8_000.0, taken_at=now - timedelta(minutes=18)))
        session.add(Snapshot(token_id=tikz.id, kind="early", mcap_usd=71_000.0, liquidity_usd=18_000.0, volume_h1=12_000.0, taken_at=now - timedelta(minutes=10)))
        session.add(Snapshot(token_id=tikz.id, kind="t15m", mcap_usd=58_000.0, liquidity_usd=17_968.0, volume_h1=9_000.0, taken_at=now))

        aged = Token(
            mint="0x007agedfatthreexoffstrip00000000001",
            symbol="SEVEN8",
            source="rh_pons",
            chain="robinhood",
            created_at_chain=now - timedelta(hours=15, minutes=36),
            first_seen_at=now - timedelta(hours=15, minutes=36),
        )
        aged.research = Research(p_good=0.92, holder_count=578, features_json="{}", risk_flags_json="[]")
        session.add(aged)
        session.flush()
        session.add(Outcome(token_id=aged.id, t0_mcap=55_591.0, max_mcap=170_631.0, multiple=3.07, last_liq=16_918.0))
        session.add(Snapshot(token_id=aged.id, kind="t0", mcap_usd=55_591.0, liquidity_usd=18_000.0, volume_h1=8_000.0, taken_at=now - timedelta(hours=15)))
        session.add(Snapshot(token_id=aged.id, kind="early", mcap_usd=80_000.0, liquidity_usd=16_918.0, volume_h1=6_000.0, taken_at=now))

        skinny = Token(
            mint="0xfomodogskinnywickstaysdropped00001",
            symbol="FOMOWK",
            source="rh_pons",
            chain="robinhood",
            created_at_chain=now - timedelta(minutes=40),
            first_seen_at=now - timedelta(minutes=40),
        )
        skinny.research = Research(p_good=0.61, holder_count=1131, features_json="{}", risk_flags_json="[]")
        session.add(skinny)
        session.flush()
        session.add(Outcome(token_id=skinny.id, t0_mcap=50_802.0, max_mcap=196_834.0, multiple=3.87, last_liq=3_468.0))
        session.add(Snapshot(token_id=skinny.id, kind="t0", mcap_usd=50_802.0, liquidity_usd=22_723.0, volume_h1=10_141.0, taken_at=now - timedelta(minutes=40)))
        session.add(Snapshot(token_id=skinny.id, kind="early", mcap_usd=3_438.0, liquidity_usd=3_468.0, volume_h1=49_082.0, taken_at=now))
    sol = asyncio.run(approaching(min_multiple=2.0, chain="sol"))
    assert any(r["mint"] == "SolTikZFatThisWindowClimbMint11111" for r in sol["items"])
    rh = asyncio.run(approaching(min_multiple=2.0, chain="robinhood"))
    rh_mints = {r["mint"] for r in rh["items"]} | {r["mint"] for r in rh["thin"]}
    assert "0x007agedfatthreexoffstrip00000000001" not in rh_mints
    assert "0xfomodogskinnywickstaysdropped00001" not in rh_mints


def test_rugs_and_start_high_patterns_are_not_runners():
    init_db()
    with session_scope() as session:
        # a rug with a big paper multiple: excluded by label 0
        rug = Token(mint="RugRunnerMint111", symbol="RUGX", source="poll", first_seen_at=utcnow())
        rug.research = Research(p_good=0.5, features_json="{}", risk_flags_json="[]")
        session.add(rug)
        session.flush()
        session.add(Outcome(token_id=rug.id, t0_mcap=69_000.0, max_mcap=900_000.0, multiple=13.0, label=0, last_liq=100.0))
        # start-high pattern flagged at research: excluded by flag
        sh = Token(mint="StartHighMint1111", symbol="SHR", source="poll", first_seen_at=utcnow())
        sh.research = Research(
            p_good=0.4,
            features_json="{}",
            risk_flags_json='["Already dumped below graduation mcap (start-high rug pattern)"]',
        )
        session.add(sh)
        session.flush()
        session.add(Outcome(token_id=sh.id, t0_mcap=69_000.0, max_mcap=1_000_000.0, multiple=14.5, label=None, last_liq=50_000.0))
        # unlabeled with a dead pool: excluded by liquidity rule
        dead = Token(mint="DeadPoolMint11111", symbol="DEAD", source="poll", first_seen_at=utcnow())
        dead.research = Research(p_good=0.6, features_json="{}", risk_flags_json="[]")
        session.add(dead)
        session.flush()
        session.add(Outcome(token_id=dead.id, t0_mcap=69_000.0, max_mcap=800_000.0, multiple=11.6, label=None, last_liq=200.0))
    board = asyncio.run(runners(min_multiple=10.0))
    mints = {r["mint"] for r in board["items"]}
    assert "RugRunnerMint111" not in mints
    assert "StartHighMint1111" not in mints
    assert "DeadPoolMint11111" not in mints


def test_doing_well_lists_runners_and_climbs_not_flats():
    from launchfinder.app import doing_well

    init_db()
    now = utcnow()
    with session_scope() as session:
        flat = Token(
            mint="0xhotdeskflat000000000000000000000000001",
            symbol="HOTFLAT",
            chain="robinhood",
            source="rh_trenches",
            first_seen_at=now,
            is_historical=False,
        )
        flat.research = Research(p_good=0.72, holder_count=80, features_json="{}", risk_flags_json="[]")
        session.add(flat)
        session.flush()
        session.add(Outcome(token_id=flat.id, t0_mcap=40_000.0, max_mcap=41_000.0, multiple=1.02, last_liq=40_000.0))

        climb = Token(
            mint="0xhotdeskclimb0000000000000000000000001",
            symbol="HOTCLIMB",
            chain="robinhood",
            source="rh_trenches",
            first_seen_at=now - timedelta(hours=2),
            is_historical=False,
        )
        climb.research = Research(p_good=0.61, holder_count=90, features_json="{}", risk_flags_json="[]")
        session.add(climb)
        session.flush()
        session.add(
            Outcome(
                token_id=climb.id,
                t0_mcap=40_000.0,
                max_mcap=100_000.0,
                multiple=2.50,
                last_liq=50_000.0,
            )
        )
        session.add(
            Snapshot(
                token_id=climb.id,
                kind="post",
                mcap_usd=100_000.0,
                liquidity_usd=50_000.0,
                volume_h1=20_000.0,
                taken_at=now,
            )
        )

        won = Token(
            mint="0xhotdeskwon000000000000000000000000001",
            symbol="HOTWIN",
            chain="robinhood",
            source="rh_trenches",
            first_seen_at=now - timedelta(hours=6),
            is_historical=False,
        )
        won.research = Research(p_good=0.78, holder_count=200, features_json="{}", risk_flags_json="[]")
        session.add(won)
        session.flush()
        session.add(
            Outcome(
                token_id=won.id,
                t0_mcap=32_000.0,
                max_mcap=176_000.0,
                multiple=5.50,
                label=1,
                last_liq=80_000.0,
            )
        )
        session.add(
            Snapshot(
                token_id=won.id,
                kind="early",
                mcap_usd=176_000.0,
                liquidity_usd=80_000.0,
                taken_at=now - timedelta(hours=5),
            )
        )
    board = asyncio.run(doing_well(chain="robinhood"))
    symbols = [row["symbol"] for row in board["items"]]
    assert "HOTWIN" in symbols
    assert "HOTCLIMB" in symbols
    assert "HOTFLAT" not in symbols
    assert symbols.index("HOTWIN") < symbols.index("HOTCLIMB")


def test_still_doing_well_drops_dumped_ath_keeps_held_run():
    from launchfinder.serialize import live_doing_well_multiple, still_doing_well

    believe = {
        "last_mcap": 88_928.0,
        "max_mcap": 702_855.0,
        "t0_mcap": 24_697.0,
        "multiple": 28.46,
    }
    assert still_doing_well(believe) is False
    assert live_doing_well_multiple(believe) < 4.0

    held = {
        "last_mcap": 500_000.0,
        "max_mcap": 702_855.0,
        "t0_mcap": 24_697.0,
        "multiple": 28.46,
    }
    assert still_doing_well(held) is True
    assert abs(live_doing_well_multiple(held) - 500_000.0 / 24_697.0) < 1e-6

    pokemon = {
        "chain": "sol",
        "last_mcap": 2_118_295.0,
        "max_mcap": 2_118_295.0,
        "t0_mcap": 847_093.0,
        "last_liq": 121_331.0,
        "multiple": 2.50,
        "holder_count": 32,
        "top10_pct": 95.4,
        "twitter_handle": "pokemon",
        "twitter_followers": 8_013_757,
        "twitter_age_days": 6116.0,
        "risk_flags": [
            "Same ticker launched repeatedly in 24h (copycat spam)",
            "Pre-pumped through migration at a huge premium (bundle-owned price)",
            "Tiny holder base for its market cap",
        ],
    }
    assert still_doing_well(pokemon) is False

    cac = {
        "last_mcap": 2_031.0,
        "max_mcap": 4_883_427.0,
        "t0_mcap": 69_000.0,
        "multiple": 70.77,
    }
    assert still_doing_well(cac) is False

    no_later_print = {
        "last_mcap": 0.0,
        "max_mcap": 176_000.0,
        "t0_mcap": 32_000.0,
        "multiple": 5.5,
    }
    assert still_doing_well(no_later_print) is True

    # Live MEME: still many multiples from t0 even after a 40%+ ATH
    # fade (chart 0.16 → 0.085). BELIEVE 3.6× / 13% of ATH stays off.
    meme = {
        "last_mcap": 50_000_000.0,
        "max_mcap": 160_000_000.0,
        "t0_mcap": 21_997.0,
        "multiple": 48.81,
    }
    assert still_doing_well(meme) is True
    assert live_doing_well_multiple(meme) > 2000.0
    # RH MEME: copycat ticker + 4 holders, but 10×+ on fat liq stays.
    meme_copycat = {
        "last_mcap": 74_328_802.0,
        "max_mcap": 1_073_625.0,
        "t0_mcap": 21_997.0,
        "last_liq": 1_172_006.0,
        "multiple": 48.81,
        "holder_count": 4,
        "risk_flags": [
            "X account created very recently",
            "Same ticker launched repeatedly in 24h (copycat spam)",
        ],
    }
    assert still_doing_well(meme_copycat) is True
    faded_small = {
        "last_mcap": 60_000.0,
        "max_mcap": 200_000.0,
        "t0_mcap": 20_000.0,
        "multiple": 10.0,
    }
    assert still_doing_well(faded_small) is False

    # Live CRCL: labeled at t1h so the card last is the dump snap.
    crcl = {
        "last_mcap": 158_599.0,
        "max_mcap": 1_379_173.0,
        "t0_mcap": 271_612.0,
        "last_liq": 118_923.0,
        "multiple": 5.08,
    }
    assert still_doing_well(crcl) is True
    assert live_doing_well_multiple(crcl) >= 4.0

    # Live LAPTOP: leftover Pump FDV $295M / 4279× vs frozen $69k t0.
    # Dex PumpSwap is $2.3k rugged. The 10× held-run is RH MEME only.
    laptop = {
        "chain": "sol",
        "last_mcap": 295_307_085.0,
        "max_mcap": 295_307_085.0,
        "t0_mcap": 69_000.0,
        "last_liq": 1_467_997.0,
        "multiple": 4279.8,
        "holder_count": 57,
        "top10_pct": 100.0,
        "risk_flags": [
            "tiny holder base",
            "prepumped — already well above launch mcap",
            "bundle-owned price",
        ],
    }
    assert still_doing_well(laptop) is False
    # Same numbers without chain must not trip the Sol leftover filter.
    meme_no_chain = {
        "last_mcap": 295_307_085.0,
        "max_mcap": 295_307_085.0,
        "t0_mcap": 69_000.0,
        "last_liq": 1_467_997.0,
        "multiple": 4279.8,
    }
    assert still_doing_well(meme_no_chain) is True

    # Live ICEMAN: Dex $1.7M / t0 $3.7M after a dump. leftover last_liq
    # $111k made the RH CRCL unfreeze re-show ATH $13M. Sol does not.
    iceman = {
        "chain": "sol",
        "last_mcap": 1_724_736.0,
        "max_mcap": 13_239_594.0,
        "t0_mcap": 3_728_614.0,
        "last_liq": 110_956.0,
        "multiple": 3.55,
    }
    assert still_doing_well(iceman) is False
    assert live_doing_well_multiple(iceman) < 1.0

    # Live fih / KAT: historical gmgn_trenches catch-up, frozen last.
    # Do not leftover-sort 2×+. RH MEME held-run has no chain key.
    fih = {
        "chain": "sol",
        "historical": True,
        "last_mcap": 2_118_014.0,
        "max_mcap": 6_691_431.0,
        "t0_mcap": 162_089.0,
        "last_liq": 124_934.0,
        "multiple": 41.3,
    }
    assert still_doing_well(fih) is False


def test_card_last_mcap_prefers_live_outcome_over_hour_one_snap():
    from launchfinder.serialize import token_card

    init_db()
    now = utcnow()
    with session_scope() as session:
        token = Token(
            mint="0xmemelastprint00000000000000000000000001",
            symbol="MEME",
            chain="robinhood",
            source="rh_trenches",
            pool_address="0xc6e298e137f2905398db87e6eae49ede64d231fee37330fa433fec917f4618b6",
            first_seen_at=now - timedelta(hours=16),
            created_at_chain=now - timedelta(hours=16),
        )
        token.research = Research(p_good=0.03, holder_count=4, features_json="{}")
        session.add(token)
        session.flush()
        session.add(
            Outcome(
                token_id=token.id,
                t0_mcap=21_997.0,
                max_mcap=1_073_625.0,
                multiple=48.81,
                last_liq=1_172_006.0,
                last_mcap=81_845_931.0,
            )
        )
        session.add(
            Snapshot(
                token_id=token.id,
                kind="early",
                taken_at=now - timedelta(hours=15),
                mcap_usd=175_437.0,
                liquidity_usd=102_433.0,
            )
        )
        session.flush()
        session.refresh(token)
        _ = token.snapshots
        card = token_card(token)
        assert abs(card["last_mcap"] - 81_845_931.0) < 1e-6
        assert abs(card["max_mcap"] - 81_845_931.0) < 1e-6
        assert card["links"]["dex"].endswith(
            "0xc6e298e137f2905398db87e6eae49ede64d231fee37330fa433fec917f4618b6"
        )


def test_card_last_mcap_unfreezes_hour_one_dump_on_a_fat_start_high():
    # Live CRCL: t1h last $159k / ATH $1.38M / last_liq $119k.
    # Doing well saw 0.58× and dropped a 5× Dex recovery.
    from launchfinder.serialize import still_doing_well, token_card

    init_db()
    now = utcnow()
    with session_scope() as session:
        token = Token(
            mint="0x51d1abab4d1e7b50642a25d3f7b2823067ebf9d7",
            symbol="CRCL",
            chain="robinhood",
            source="rh_dex",
            first_seen_at=now - timedelta(hours=12),
            created_at_chain=now - timedelta(hours=12),
        )
        token.research = Research(p_good=0.18, holder_count=438, features_json="{}")
        session.add(token)
        session.flush()
        session.add(
            Outcome(
                token_id=token.id,
                t0_mcap=271_612.0,
                max_mcap=1_379_173.0,
                multiple=5.08,
                last_liq=118_923.0,
                last_mcap=158_599.0,
            )
        )
        session.flush()
        session.refresh(token)
        card = token_card(token)
        assert abs(card["last_mcap"] - 1_379_173.0) < 1e-6
        assert still_doing_well(card) is True


def test_card_last_mcap_does_not_unfreeze_sol_iceman_dump():
    # Live ICEMAN: Dex $1.7M / leftover ATH $13M / last_liq $111k.
    # RH CRCL unfreeze would re-show ATH and keep it on Doing well.
    from launchfinder.serialize import still_doing_well, token_card

    init_db()
    now = utcnow()
    with session_scope() as session:
        token = Token(
            mint="8tdAyS1pW9p7GQXtE5h8rVSsfBLJi3ek4vrNzCAhpump",
            symbol="ICEMAN",
            chain="sol",
            source="poll",
            first_seen_at=now - timedelta(hours=12),
        )
        token.research = Research(p_good=0.69, holder_count=200, features_json="{}")
        session.add(token)
        session.flush()
        session.add(
            Outcome(
                token_id=token.id,
                t0_mcap=3_728_614.0,
                max_mcap=13_239_594.0,
                multiple=3.55,
                last_liq=110_956.0,
                last_mcap=1_724_736.0,
            )
        )
        session.flush()
        session.refresh(token)
        card = token_card(token)
        assert abs(card["last_mcap"] - 1_724_736.0) < 1e-6
        assert still_doing_well(card) is False


def test_card_last_mcap_unfreezes_when_only_the_t1h_snap_exists():
    # Live CRCL: label=1 at t1h left outcome.last_mcap 0. Detail
    # loaded snaps and showed $159k instead of ATH $1.38M.
    from launchfinder.serialize import still_doing_well, token_card

    init_db()
    now = utcnow()
    with session_scope() as session:
        token = Token(
            mint="0x51d1abab4d1e7b50642a25d3f7b2823067ebf9d7",
            symbol="CRCL",
            chain="robinhood",
            source="rh_dex",
            first_seen_at=now - timedelta(hours=12),
            created_at_chain=now - timedelta(hours=12),
        )
        token.research = Research(p_good=0.18, holder_count=438, features_json="{}")
        session.add(token)
        session.flush()
        session.add(
            Outcome(
                token_id=token.id,
                t0_mcap=271_612.0,
                t1h_mcap=158_599.0,
                max_mcap=1_379_173.0,
                multiple=5.08,
                last_liq=118_923.0,
                last_mcap=0.0,
                label=1,
            )
        )
        session.add(
            Snapshot(
                token_id=token.id,
                kind="t1h",
                taken_at=now - timedelta(hours=11),
                mcap_usd=158_599.0,
                liquidity_usd=45_000.0,
            )
        )
        session.flush()
        session.refresh(token)
        _ = token.snapshots
        card = token_card(token)
        assert abs(card["last_mcap"] - 1_379_173.0) < 1e-6
        assert still_doing_well(card) is True


def test_doing_well_hides_dumped_runners_keeps_held():
    from launchfinder.app import doing_well

    init_db()
    now = utcnow()
    with session_scope() as session:
        dumped = Token(
            mint="0xhotdeskdumped00000000000000000000001",
            symbol="HOTDUMP",
            chain="robinhood",
            source="rh_trenches",
            first_seen_at=now - timedelta(hours=8),
            is_historical=False,
        )
        dumped.research = Research(p_good=0.78, holder_count=200, features_json="{}", risk_flags_json="[]")
        session.add(dumped)
        session.flush()
        session.add(
            Outcome(
                token_id=dumped.id,
                t0_mcap=25_000.0,
                max_mcap=700_000.0,
                multiple=28.0,
                label=1,
                last_liq=80_000.0,
            )
        )
        session.add(
            Snapshot(
                token_id=dumped.id,
                kind="t15m",
                mcap_usd=700_000.0,
                liquidity_usd=200_000.0,
                taken_at=now - timedelta(hours=6),
            )
        )
        session.add(
            Snapshot(
                token_id=dumped.id,
                kind="post",
                mcap_usd=90_000.0,
                liquidity_usd=60_000.0,
                taken_at=now,
            )
        )

        held = Token(
            mint="0xhotdeskheld0000000000000000000000001",
            symbol="HOTHELD",
            chain="robinhood",
            source="rh_trenches",
            first_seen_at=now - timedelta(hours=6),
            is_historical=False,
        )
        held.research = Research(p_good=0.70, holder_count=180, features_json="{}", risk_flags_json="[]")
        session.add(held)
        session.flush()
        session.add(
            Outcome(
                token_id=held.id,
                t0_mcap=32_000.0,
                max_mcap=176_000.0,
                multiple=5.50,
                label=1,
                last_liq=90_000.0,
            )
        )
        session.add(
            Snapshot(
                token_id=held.id,
                kind="post",
                mcap_usd=140_000.0,
                liquidity_usd=90_000.0,
                taken_at=now,
            )
        )
    board = asyncio.run(doing_well(chain="robinhood"))
    symbols = [row["symbol"] for row in board["items"]]
    assert "HOTDUMP" not in symbols
    assert "HOTHELD" in symbols
    held_row = next(row for row in board["items"] if row["symbol"] == "HOTHELD")
    assert abs(held_row["multiple"] - 140_000.0 / 32_000.0) < 1e-6


def test_doing_well_keeps_thin_many_multiple_after_ath_fade():
    # Live MEME: 4 holders so runners skip it. last $74M / t0 $22k
    # on $1.2M liq is still a successful launch after a 40%+ fade.
    from launchfinder.app import doing_well

    init_db()
    now = utcnow()
    with session_scope() as session:
        token = Token(
            mint="0x385f4f8ae47651ce5f58f5265395a669f8281e18",
            symbol="MEME",
            name="A Meme Coin",
            chain="robinhood",
            source="rh_trenches",
            first_seen_at=now - timedelta(hours=16),
            created_at_chain=now - timedelta(hours=16),
            is_historical=False,
        )
        token.research = Research(
            p_good=0.03,
            holder_count=4,
            features_json="{}",
            risk_flags_json='["X account created very recently", "Same ticker launched repeatedly in 24h (copycat spam)"]',
        )
        session.add(token)
        session.flush()
        session.add(
            Outcome(
                token_id=token.id,
                t0_mcap=21_997.0,
                max_mcap=1_073_625.0,
                multiple=48.81,
                last_liq=1_172_006.0,
                last_mcap=74_328_802.0,
            )
        )
    from launchfinder.serialize import still_doing_well

    board = asyncio.run(doing_well(chain="robinhood"))
    hit = [row for row in board["items"] if row["mint"] == "0x385f4f8ae47651ce5f58f5265395a669f8281e18"]
    assert hit
    assert hit[0]["multiple"] > 3000.0
    assert still_doing_well(hit[0]) is True


def test_doing_well_drops_sol_leftover_fdv_even_when_liq_is_fat():
    # Live LAPTOP 76cJ…: Dex mcap ~$2.3k after a rug, leftover Pump FDV
    # ~$295M, 57 holders / 100% top-10, t0 frozen at the $69k floor.
    from launchfinder.app import doing_well
    from launchfinder.serialize import (
        apply_held_ingest_miss_note,
        is_sol_leftover_fdv_doing_well,
        still_doing_well,
        token_card,
    )

    init_db()
    now = utcnow()
    with session_scope() as session:
        token = Token(
            mint="76cJTCcyZ6zVXUM4TkAWoCJ953MDnEzs3mpaAkbypump",
            symbol="LAPTOP",
            name="Laptop",
            chain="sol",
            source="poll",
            first_seen_at=now - timedelta(hours=12),
        )
        token.research = Research(
            p_good=0.25,
            holder_count=57,
            top10_pct=100.0,
            features_json=json.dumps(
                {
                    "t0_mcap": 69_000.0,
                    "holder_count": 57,
                    "top10_pct": 100.0,
                    "creator_pct": 79.0,
                }
            ),
            risk_flags_json=json.dumps(
                [
                    "t0 miss — book already ran",
                    "tiny holder base",
                    "prepumped — already well above launch mcap",
                    "bundle-owned price",
                ]
            ),
        )
        session.add(token)
        session.flush()
        session.add(
            Outcome(
                token_id=token.id,
                t0_mcap=69_000.0,
                max_mcap=295_307_085.0,
                last_mcap=295_307_085.0,
                last_liq=1_467_997.0,
                multiple=4279.8,
                label=None,
            )
        )
        session.add(
            Snapshot(
                token_id=token.id,
                kind="t0",
                taken_at=now - timedelta(hours=12),
                mcap_usd=69_000.0,
                liquidity_usd=20_000.0,
            )
        )
        session.add(
            Snapshot(
                token_id=token.id,
                kind="now",
                taken_at=now - timedelta(minutes=2),
                mcap_usd=295_307_085.0,
                liquidity_usd=1_467_997.0,
            )
        )
        session.flush()
        session.refresh(token)
        _ = token.snapshots
        card = token_card(token)
        assert is_sol_leftover_fdv_doing_well(card) is True
        assert still_doing_well(card) is False
        stamped = apply_held_ingest_miss_note(
            {
                "chain": "sol",
                "t0_mcap": 69_000.0,
                "last_mcap": 295_307_085.0,
                "last_liq": 1_467_997.0,
                "p_good": 0.25,
                "risk_flags": [],
            }
        )
        assert not any("t0 miss" in str(flag) for flag in (stamped.get("risk_flags") or []))
        assert abs(card["last_mcap"] - 295_307_085.0) < 1e-6

    board = asyncio.run(doing_well())
    mints = {item["mint"] for item in board["items"]}
    assert "76cJTCcyZ6zVXUM4TkAWoCJ953MDnEzs3mpaAkbypump" not in mints


def test_doing_well_drops_sol_leftover_fdv_without_holder_flags():
    # WOFI-class leftover: 7000× vs $69k t0 with no tiny-holder flag yet.
    from launchfinder.serialize import is_sol_leftover_fdv_doing_well, still_doing_well

    card = {
        "mint": "wofileftoverfdv0000000000000000000000001",
        "chain": "sol",
        "t0_mcap": 69_000.0,
        "last_mcap": 493_000_000.0,
        "max_mcap": 493_000_000.0,
        "last_liq": 2_000_000.0,
        "multiple": 7148.0,
        "label": None,
        "risk_flags": [],
        "holder_count": 200,
        "top10_pct": 40.0,
    }
    assert is_sol_leftover_fdv_doing_well(card) is True
    assert still_doing_well(card) is False


def test_doing_well_keeps_start_high_second_leg_when_last_froze():
    # Live CRCL: runners skip start-high. last $159k / t0 $272k is
    # 0.58× so the 10× last path missed ATH $1.38M / $119k liq.
    from launchfinder.app import doing_well

    init_db()
    now = utcnow()
    with session_scope() as session:
        token = Token(
            mint="0x51d1abab4d1e7b50642a25d3f7b2823067ebf9d7",
            symbol="CRCL",
            name="Just a Circle",
            chain="robinhood",
            source="rh_dex",
            first_seen_at=now - timedelta(hours=12),
            created_at_chain=now - timedelta(hours=12),
            is_historical=False,
        )
        token.research = Research(p_good=0.18, holder_count=438, features_json="{}")
        session.add(token)
        session.flush()
        session.add(
            Outcome(
                token_id=token.id,
                t0_mcap=271_612.0,
                max_mcap=1_379_173.0,
                multiple=5.08,
                last_liq=118_923.0,
                last_mcap=158_599.0,
            )
        )
    board = asyncio.run(doing_well(chain="robinhood"))
    hit = [row for row in board["items"] if row["mint"] == "0x51d1abab4d1e7b50642a25d3f7b2823067ebf9d7"]
    assert hit
    assert hit[0]["multiple"] >= 4.0


def test_doing_well_keeps_start_high_when_last_mcap_never_wrote():
    from launchfinder.app import doing_well

    init_db()
    now = utcnow()
    with session_scope() as session:
        token = Token(
            mint="0x51d1abab4d1e7b50642a25d3f7b2823067ebf9d7",
            symbol="CRCL",
            name="Just a Circle",
            chain="robinhood",
            source="rh_dex",
            first_seen_at=now - timedelta(hours=12),
            created_at_chain=now - timedelta(hours=12),
            is_historical=False,
        )
        token.research = Research(p_good=0.18, holder_count=438, features_json="{}")
        session.add(token)
        session.flush()
        session.add(
            Outcome(
                token_id=token.id,
                t0_mcap=271_612.0,
                max_mcap=1_379_173.0,
                multiple=5.08,
                last_liq=118_923.0,
                last_mcap=0.0,
                label=1,
            )
        )
    board = asyncio.run(doing_well(chain="robinhood"))
    hit = [row for row in board["items"] if row["mint"] == "0x51d1abab4d1e7b50642a25d3f7b2823067ebf9d7"]
    assert hit
    assert hit[0]["multiple"] >= 4.0


def test_doing_well_keeps_start_high_after_dex_recovery():
    # Live CRCL after post-label refresh: last $980k / 3.6× / 71% of
    # ATH. Not 10× last, not a dump unfreeze, and Pre-pumped skips
    # runners/approaching.
    from launchfinder.app import doing_well

    init_db()
    now = utcnow()
    with session_scope() as session:
        token = Token(
            mint="0x51d1abab4d1e7b50642a25d3f7b2823067ebf9d7",
            symbol="CRCL",
            name="Just a Circle",
            chain="robinhood",
            source="rh_dex",
            first_seen_at=now - timedelta(hours=12),
            created_at_chain=now - timedelta(hours=12),
            is_historical=False,
        )
        token.research = Research(
            p_good=0.18,
            holder_count=438,
            features_json="{}",
            risk_flags_json='["Pre-pumped through migration at a huge premium (bundle-owned price)"]',
        )
        session.add(token)
        session.flush()
        session.add(
            Outcome(
                token_id=token.id,
                t0_mcap=271_612.0,
                max_mcap=1_379_173.0,
                multiple=5.08,
                last_liq=95_328.0,
                last_mcap=979_678.0,
                label=1,
            )
        )
    board = asyncio.run(doing_well(chain="robinhood"))
    hit = [row for row in board["items"] if row["mint"] == "0x51d1abab4d1e7b50642a25d3f7b2823067ebf9d7"]
    assert hit
    assert hit[0]["multiple"] >= 3.0


def test_doing_well_drops_labeled_rug():
    from launchfinder.app import doing_well

    init_db()
    now = utcnow()
    with session_scope() as session:
        token = Token(
            mint="0xa3d33571b52a719638labeledrugdoingwell01",
            symbol="P",
            chain="robinhood",
            source="rh_fomo",
            first_seen_at=now - timedelta(hours=10),
            is_historical=False,
        )
        token.research = Research(
            p_good=0.14,
            holder_count=40,
            features_json="{}",
            risk_flags_json='["Pre-pumped through migration at a huge premium (bundle-owned price)"]',
        )
        session.add(token)
        session.flush()
        session.add(
            Outcome(
                token_id=token.id,
                t0_mcap=40_000.0,
                max_mcap=1_114_989.0,
                last_mcap=1_114_989.0,
                last_liq=1_098_319.0,
                multiple=27.87,
                label=0,
            )
        )
    board = asyncio.run(doing_well(chain="robinhood"))
    assert all(row["symbol"] != "P" for row in board["items"])


def test_held_ingest_miss_note_keeps_meme_p():
    # Live MEME: 2.8% ingest / 3898× / $1.3M liq. Do not lift paper p.
    from launchfinder.serialize import token_card

    init_db()
    now = utcnow()
    with session_scope() as session:
        token = Token(
            mint="0x385f4f8ae47651ce5f58f5265395a669f8281e18",
            symbol="MEME",
            name="A Meme Coin",
            chain="robinhood",
            source="rh_trenches",
            first_seen_at=now - timedelta(hours=17),
            created_at_chain=now - timedelta(hours=17),
        )
        token.research = Research(
            p_good=0.0279,
            heuristic_p=0.15,
            model_p=0.0063,
            holder_count=4,
            features_json="{}",
            risk_flags_json='["X account created very recently", "Same ticker launched repeatedly in 24h (copycat spam)"]',
        )
        session.add(token)
        session.flush()
        session.add(
            Outcome(
                token_id=token.id,
                t0_mcap=21_997.0,
                max_mcap=85_764_017.0,
                multiple=48.81,
                last_liq=1_338_114.0,
                last_mcap=85_764_017.0,
            )
        )
        session.flush()
        session.refresh(token)
        card = token_card(token)
        assert abs(card["p_good"] - 0.0279) < 1e-6
        assert abs(card["score"] - 2.8) < 1e-6
        assert card["ingest_miss"] is True
        assert any("t0 miss" in str(flag) for flag in card["risk_flags"])


def test_doing_well_keeps_labeled_live_5x_when_runners_unconfirm():
    # Live Laptop GsewXp: last $214k / t0 $40.8k / 5.2× / $40k liq /
    # label=1 / 95% ATH. The t0 snap already printed the live book so
    # confirmed_runner_multiple is ~1× and /api/runners skips it. The
    # 10× / $50k held door also misses ($40k liq). Doing well should
    # still list a labeled this-window 5× live book. XMRCAT-class 3×
    # and a dust 5× stay off. Do not leftover-sort 2×+.
    from launchfinder.app import doing_well, runners

    init_db()
    now = utcnow()
    laptop = "GsewXpKnn1ei7YfwfcBgdF7B7giXjfLemVjnRPyupump"
    xmrcat = "CHztigebeDZwDCg3xmrcat3xnotdoingwellxxxx"
    dust = "DustFiveXMint0000000000000000000000001"
    with session_scope() as session:
        token = Token(
            mint=laptop,
            symbol="Laptop",
            name="Slightly Used Laptop",
            chain="sol",
            source="poll",
            first_seen_at=now - timedelta(hours=2),
            created_at_chain=now - timedelta(hours=2),
            is_historical=False,
        )
        token.research = Research(
            p_good=0.87,
            holder_count=344,
            top10_pct=43.64,
            features_json="{}",
            risk_flags_json=json.dumps(
                [
                    "Community takeover (original dev left)",
                    "Same ticker launched repeatedly in 24h (copycat spam)",
                    "Creator was funded by a wallet behind prior rugs",
                ]
            ),
        )
        session.add(token)
        session.flush()
        session.add(
            Outcome(
                token_id=token.id,
                t0_mcap=40_834.0,
                max_mcap=225_806.0,
                last_mcap=214_082.0,
                last_liq=40_205.0,
                multiple=5.53,
                label=1,
            )
        )
        session.add(
            Snapshot(
                token_id=token.id,
                kind="t0",
                taken_at=now - timedelta(hours=2),
                mcap_usd=214_082.0,
                liquidity_usd=40_205.0,
            )
        )
        fade = Token(
            mint=xmrcat,
            symbol="XMRCAT",
            chain="sol",
            source="poll",
            first_seen_at=now - timedelta(hours=3),
            is_historical=False,
        )
        fade.research = Research(
            p_good=0.40,
            holder_count=180,
            features_json="{}",
            risk_flags_json="[]",
        )
        session.add(fade)
        session.flush()
        session.add(
            Outcome(
                token_id=fade.id,
                t0_mcap=42_698.0,
                max_mcap=245_214.0,
                last_mcap=140_638.0,
                last_liq=34_455.0,
                multiple=5.74,
                label=1,
            )
        )
        thin = Token(
            mint=dust,
            symbol="DUST5",
            chain="sol",
            source="poll",
            first_seen_at=now - timedelta(hours=1),
            is_historical=False,
        )
        thin.research = Research(p_good=0.55, holder_count=90, features_json="{}", risk_flags_json="[]")
        session.add(thin)
        session.flush()
        session.add(
            Outcome(
                token_id=thin.id,
                t0_mcap=40_000.0,
                max_mcap=220_000.0,
                last_mcap=210_000.0,
                last_liq=5_000.0,
                multiple=5.25,
                label=1,
            )
        )
    run = asyncio.run(runners(min_multiple=5.0))
    assert laptop not in {row["mint"] for row in run["items"]}
    board = asyncio.run(doing_well())
    mints = {item["mint"] for item in board["items"]}
    assert laptop in mints
    assert xmrcat not in mints
    assert dust not in mints
    hit = next(item for item in board["items"] if item["mint"] == laptop)
    assert hit["multiple"] > 5.0
    assert hit["multiple"] < 6.0


def test_health_exposes_image_rev():
    from launchfinder.app import health
    from launchfinder.image_rev import IMAGE_REV

    card = asyncio.run(health())
    assert card["image_rev"] == IMAGE_REV
    assert IMAGE_REV == "stack-v207"
    assert "git_sha" in card
    assert card["hunt_tape"] is True
    assert float(card["hunt_last_seconds"]) == 60.0
    assert "alerts" in card
    assert card["hunt"] is True
    assert card["fomo_trending"] is True
    assert card["role"] in {"all", "api", "worker"}
    assert card["wallet_score"] is True
    assert card["prewarm"] is True
    assert card["bloom"] is True
    assert card["live_social"] is True
    assert card["paper"] is True
    assert "bitquery" in card
