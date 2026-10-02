from datetime import timedelta

from launchfinder.config import GRADUATION_MCAP_USD
from launchfinder.models import Outcome
from launchfinder.scoring.outcomes import _early_label, _label, honest_tracked_peak


def test_honest_tracked_peak_rejects_babysol_artifact():
    # Live BABYSOL: t0 $69k, honest 12x, then Dex printed 4871x.
    peak, multiple = honest_tracked_peak(69_000.0, 828_000.0, 336_000_000.0)
    assert peak == 828_000.0
    assert abs(multiple - 12.0) < 1e-6
    # First post-label tick that is already an artifact stays at 0.
    peak, multiple = honest_tracked_peak(69_000.0, 0.0, 336_000_000.0)
    assert peak == 0.0
    assert multiple == 0.0
    # Honest 15x still lands.
    peak, multiple = honest_tracked_peak(69_000.0, 414_000.0, 1_035_000.0)
    assert peak == 1_035_000.0
    assert abs(multiple - 15.0) < 1e-6


def test_repair_sol_tiny_t0_skips_rh_catties():
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Token
    from launchfinder.scoring.outcomes import repair_sol_tiny_t0

    init_db()
    with session_scope() as session:
        sol = Token(mint="SolTinyT0Mint11111111111111111111111111", symbol="ARROW", chain="sol", source="poll")
        rh = Token(mint="0xcattiestiny000000000000000000000000001", symbol="CATTIES", chain="robinhood", source="rh_trenches")
        session.add_all([sol, rh])
        session.flush()
        session.add(Outcome(token_id=sol.id, t0_mcap=410.84, max_mcap=1_888.0, multiple=4.60))
        session.add(Outcome(token_id=rh.id, t0_mcap=3_353.0, max_mcap=3_353.0, multiple=1.0))
        session.flush()
        assert repair_sol_tiny_t0(session) >= 1
        sol_o = session.query(Outcome).filter(Outcome.token_id == sol.id).one()
        rh_o = session.query(Outcome).filter(Outcome.token_id == rh.id).one()
        assert sol_o.t0_mcap == 69_000.0
        assert sol_o.multiple < 0.1
        assert rh_o.t0_mcap == 3_353.0


def test_rh_backfill_uses_chain_floor_not_solana():
    from launchfinder.chains import graduation_mcap

    outcome = Outcome(t0_mcap=20_000.0, max_mcap=20_000.0, last_liq=0.0)
    _label(outcome, {}, historical=True, chain="robinhood")
    assert outcome.t0_mcap == graduation_mcap("robinhood")
    assert outcome.t0_mcap == 40_000.0
    assert outcome.label == 0


def test_rh_watched_24h_keeps_live_t0():
    # Quiet-retire is not a missing entry. FAT-class $20k t0 must survive 24h.
    outcome = Outcome(t0_mcap=20_364.0, max_mcap=20_364.0, last_liq=400.0)
    _label(outcome, {}, historical=False, chain="robinhood")
    assert outcome.t0_mcap == 20_364.0
    assert outcome.label == 0


def test_historical_modest_peak_with_collapse_is_not_a_win():
    # Peaked at 120k then collapsed to 20k before we backfilled it. The old
    # math used ath / mcap-at-ingest (120k / 20k = 6x) and called it a winner;
    # against the graduation baseline it never reached 2x.
    outcome = Outcome(t0_mcap=20_000.0, max_mcap=20_000.0, last_liq=500.0)
    _label(outcome, {"ath_mcap": 120_000.0}, historical=True)
    assert outcome.t0_mcap == GRADUATION_MCAP_USD
    assert outcome.multiple < 2.0
    assert outcome.label == 0


def test_historical_real_winner_still_wins():
    outcome = Outcome(t0_mcap=90_000.0, max_mcap=90_000.0, last_liq=40_000.0)
    _label(outcome, {"ath_mcap": 600_000.0}, historical=True)
    assert outcome.multiple >= 5.0
    assert outcome.label == 1


def test_small_multiples_are_not_wins():
    # 2-4x bounces are not the target; the desk hunts 5-50x runners.
    for ath in (165_000.0, 240_000.0, 300_000.0):
        outcome = Outcome(t0_mcap=69_000.0, max_mcap=69_000.0, last_liq=30_000.0)
        _label(outcome, {"ath_mcap": ath}, historical=True)
        assert outcome.multiple < 5.0
        assert outcome.label == 0


def test_historical_modest_peak_is_not_a_win():
    # Peaked at 110k from a 69k graduation: below 2x, label 0.
    outcome = Outcome(t0_mcap=30_000.0, max_mcap=30_000.0, last_liq=15_000.0)
    _label(outcome, {"ath_mcap": 110_000.0}, historical=True)
    assert outcome.label == 0


def test_live_token_keeps_ingest_entry_price():
    # Live tokens are ingested minutes after migration; their t0 is a real
    # entry price and must not be overwritten by the graduation baseline.
    outcome = Outcome(t0_mcap=95_000.0, max_mcap=520_000.0, last_liq=30_000.0)
    _label(outcome, {"ath_mcap": 520_000.0}, historical=False)
    assert outcome.t0_mcap == 95_000.0
    assert outcome.label == 1


def test_early_label_win_is_immediate():
    outcome = Outcome(t0_mcap=90_000.0, max_mcap=470_000.0, last_liq=25_000.0, multiple=470_000.0 / 90_000.0)
    _early_label(outcome, timedelta(minutes=45))
    assert outcome.label == 1


def test_thin_holder_five_x_is_not_a_win():
    # SHORT-class: 4 wallets printing 5x is a wick, not a runner label.
    pending = Outcome(t0_mcap=40_000.0, max_mcap=220_000.0, last_liq=12_000.0, multiple=5.5)
    _early_label(pending, timedelta(minutes=45), holders=4, chain="robinhood")
    assert pending.label is None
    judged = Outcome(t0_mcap=40_000.0, max_mcap=220_000.0, last_liq=12_000.0)
    _label(judged, {"ath_mcap": 220_000.0}, historical=False, holders=4, chain="robinhood")
    assert judged.multiple >= 5.0
    assert judged.label == 0
    honest = Outcome(t0_mcap=40_000.0, max_mcap=220_000.0, last_liq=12_000.0, multiple=5.5)
    _early_label(honest, timedelta(minutes=45), holders=176, chain="robinhood")
    assert honest.label == 1


def test_early_label_triple_stays_pending():
    # A 3.2x is not a runner win; it stays pending until the horizons decide.
    outcome = Outcome(t0_mcap=90_000.0, max_mcap=290_000.0, last_liq=25_000.0, multiple=290_000.0 / 90_000.0)
    _early_label(outcome, timedelta(minutes=45))
    assert outcome.label is None


def test_early_label_rug_waits_for_six_hours():
    outcome = Outcome(t0_mcap=90_000.0, max_mcap=95_000.0, last_liq=300.0, multiple=95_000.0 / 90_000.0)
    _early_label(outcome, timedelta(hours=2))
    assert outcome.label is None  # too early to call it dead
    _early_label(outcome, timedelta(hours=7))
    assert outcome.label == 0


def test_fake_multiple_with_drained_pool_is_a_loss():
    # $WIND pattern: t0 captured post-collapse made multiple look like 38x,
    # but the pool is empty — nobody could exit. Label 0.
    outcome = Outcome(t0_mcap=8.49, max_mcap=328.0, last_liq=1.17, multiple=38.6)
    _label(outcome, {}, historical=False)
    assert outcome.label == 0


def test_instant_dump_rugs_early():
    # Dead pool and under half of entry: label 0 from the first refresh,
    # no need to wait six hours.
    outcome = Outcome(t0_mcap=69_000.0, max_mcap=69_000.0, last_liq=200.0, multiple=0.1)
    _early_label(outcome, timedelta(minutes=20))
    assert outcome.label == 0


def test_liquidity_blip_still_waits_for_six_hours():
    # Dead pool but price holding: could be a data blip — wait for 6h.
    outcome = Outcome(t0_mcap=69_000.0, max_mcap=80_000.0, last_liq=200.0, multiple=1.16)
    _early_label(outcome, timedelta(minutes=20))
    assert outcome.label is None


def test_early_label_undecided_stays_pending():
    outcome = Outcome(t0_mcap=90_000.0, max_mcap=140_000.0, last_liq=20_000.0, multiple=140_000.0 / 90_000.0)
    _early_label(outcome, timedelta(hours=7))
    assert outcome.label is None


def test_ghost_book_detects_leftover_lp():
    from launchfinder.scoring.outcomes import is_ghost_book

    assert is_ghost_book(105.0, 8.0) is True          # METH t0 leftover pair
    assert is_ghost_book(0.0, 0.0) is True            # FAT/PENIS Dex miss
    assert is_ghost_book(20_000.0, 50.0) is True      # leftover LP, no flow
    assert is_ghost_book(148.67, 1_744.57) is True    # AIAIAI leftover $285k FDV
    assert is_ghost_book(22_502.0, 14_730.0) is False # BRICKED live book
    assert is_ghost_book(35_209.0, 887.0) is False    # MACRODUCK live book


def test_ghost_peak_keeps_dex_miss_climber_strips_leftover_fdv():
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Token
    from launchfinder.scoring.outcomes import repair_rh_ghost_peak

    init_db()
    with session_scope() as session:
        shark = Token(mint="0xsharkpeak00000000000000000000000000001", symbol="SHARK", chain="robinhood")
        leftover = Token(mint="0xstlspeak00000000000000000000000000001", symbol="STL", chain="robinhood")
        session.add_all([shark, leftover])
        session.flush()
        session.add(Outcome(token_id=shark.id, t0_mcap=20_000.0, max_mcap=68_200.0, multiple=3.41, last_liq=17_122.0))
        session.add(Outcome(token_id=leftover.id, t0_mcap=40_000.0, max_mcap=28_431_114.0, multiple=710.78, last_liq=28_431_114.0))
        session.flush()
        repair_rh_ghost_peak(session)
        shark_o = session.query(Outcome).filter(Outcome.token_id == shark.id).one()
        left_o = session.query(Outcome).filter(Outcome.token_id == leftover.id).one()
        assert shark_o.multiple >= 3.4
        assert shark_o.last_liq >= 17_000.0
        assert left_o.max_mcap == 40_000.0
        assert left_o.multiple <= 1.01


def test_rh_ingest_entry_parks_leftover_fdv():
    from launchfinder.scoring.outcomes import rh_ingest_entry

    entry, seed = rh_ingest_entry(285_253.0, 148.67, 1_744.57, 0.0, 40_000.0)
    assert entry == 40_000.0
    assert seed == 40_000.0
    # CATTIES-class live small book is kept.
    entry, seed = rh_ingest_entry(3_353.0, 3_720.0, 63_683.0, 0.0, 40_000.0)
    assert entry == 3_353.0
    assert seed == 3_353.0
    # SHARK-class live book at the floor is kept.
    entry, seed = rh_ingest_entry(42_374.0, 38_413.0, 28_667.0, 0.0, 40_000.0)
    assert entry == 42_374.0
    # Live ROUTE: arrived at $804k with Dex h1 +1053% — t0 is the open.
    entry, seed = rh_ingest_entry(
        804_008.0, 82_910.94, 606_581.94, 0.0, 40_000.0, price_change_pct=1053.0
    )
    assert 65_000.0 < entry < 75_000.0
    assert seed == 804_008.0
    # A modest 1h bounce is not a late catch-up.
    entry, seed = rh_ingest_entry(
        804_008.0, 82_910.94, 606_581.94, 0.0, 40_000.0, price_change_pct=50.0
    )
    assert entry == 804_008.0
    # Dex garbage must not invent a $40k open (live EDOG / UGLY).
    entry, seed = rh_ingest_entry(
        548_888.0, 80_000.0, 200_000.0, 0.0, 40_000.0, price_change_pct=9.955e21
    )
    assert entry == 548_888.0
    entry, seed = rh_ingest_entry(
        548_888.0, 80_000.0, 200_000.0, 0.0, 40_000.0, price_change_pct=18_377.0
    )
    assert entry == 548_888.0


def test_repair_rh_late_dex_t0_this_window_only() -> None:
    import json
    from datetime import timedelta

    from launchfinder.db import SessionLocal, init_db
    from launchfinder.models import Outcome, Research, Snapshot, Token, utcnow
    from launchfinder.scoring.features import FEATURE_NAMES
    from launchfinder.scoring.outcomes import repair_rh_late_dex_t0

    init_db()
    now = utcnow()
    db = SessionLocal()
    try:
        route = Token(
            mint="0x4a72b9702f991b790788f8afa9e7112541f4e8f8",
            symbol="ROUTE",
            chain="robinhood",
            source="rh_fomo",
            created_at_chain=now - timedelta(hours=1, minutes=46),
            migrated_at=now - timedelta(hours=1, minutes=46),
            first_seen_at=now,
        )
        route.research = Research(
            raw_json=json.dumps({"market": {"price_change_h1": 1053.0, "mcap_usd": 804_008.0}}),
            features_json="{}",
            risk_flags_json="[]",
        )
        db.add(route)
        db.flush()
        db.add(
            Outcome(
                token_id=route.id,
                t0_mcap=804_008.0,
                max_mcap=7_110_623.0,
                last_mcap=7_110_623.0,
                last_liq=82_910.0,
                label=1,
            )
        )
        db.add(Snapshot(token_id=route.id, kind="t0", mcap_usd=804_008.0, liquidity_usd=82_910.0, volume_h1=606_581.0))

        leftover = Token(
            mint="0xoldfat0000000000000000000000000000000001",
            symbol="LEGS",
            chain="robinhood",
            source="rh_dex",
            created_at_chain=now - timedelta(days=5),
            migrated_at=now - timedelta(days=5),
            first_seen_at=now,
        )
        leftover.research = Research(
            raw_json=json.dumps({"market": {"price_change_h1": 2000.0, "mcap_usd": 900_000.0}}),
            features_json="{}",
            risk_flags_json="[]",
        )
        db.add(leftover)
        db.flush()
        db.add(Outcome(token_id=leftover.id, t0_mcap=900_000.0, max_mcap=900_000.0, last_liq=80_000.0))

        rug = Token(
            mint="0xthiswindowrug00000000000000000000000001",
            symbol="DUMP",
            chain="robinhood",
            source="rh_fomo",
            created_at_chain=now - timedelta(hours=2),
            migrated_at=now - timedelta(hours=2),
            first_seen_at=now,
        )
        rug.research = Research(
            raw_json=json.dumps({"market": {"price_change_h1": 1053.0, "mcap_usd": 804_008.0}}),
            features_json="{}",
            risk_flags_json="[]",
        )
        db.add(rug)
        db.flush()
        db.add(Outcome(token_id=rug.id, t0_mcap=804_008.0, max_mcap=804_008.0, last_liq=80_000.0, label=0))
        db.commit()

        assert repair_rh_late_dex_t0(db) == 1
        db.commit()
        fixed = db.query(Outcome).filter(Outcome.token_id == route.id).one()
        assert 65_000.0 < fixed.t0_mcap < 75_000.0
        assert fixed.max_mcap == 7_110_623.0
        assert fixed.multiple > 80.0
        old = db.query(Outcome).filter(Outcome.token_id == leftover.id).one()
        assert old.t0_mcap == 900_000.0
        dumped = db.query(Outcome).filter(Outcome.token_id == rug.id).one()
        assert dumped.t0_mcap == 804_008.0
        assert len(FEATURE_NAMES) == 66
    finally:
        db.close()


def test_restore_rh_late_dex_junk_t0_from_snap() -> None:
    import json
    from datetime import timedelta

    from launchfinder.db import SessionLocal, init_db
    from launchfinder.models import Outcome, Research, Snapshot, Token, utcnow
    from launchfinder.scoring.features import FEATURE_NAMES
    from launchfinder.scoring.outcomes import (
        repair_rh_late_dex_t0,
        restore_rh_late_dex_junk_t0,
        rh_implied_open_mcap,
    )

    assert rh_implied_open_mcap(548_888.0, 9.955e21, 40_000.0) is None
    assert rh_implied_open_mcap(548_888.0, 18_377.0, 40_000.0) is None
    assert 65_000.0 < (rh_implied_open_mcap(804_008.0, 1053.0, 40_000.0) or 0) < 75_000.0

    init_db()
    now = utcnow()
    db = SessionLocal()
    try:
        ugly = Token(
            mint="0xuglyjunk00000000000000000000000000000001",
            symbol="UGLY",
            chain="robinhood",
            source="rh_fomo",
            created_at_chain=now - timedelta(hours=3),
            first_seen_at=now - timedelta(hours=1),
        )
        ugly.research = Research(
            raw_json=json.dumps({"market": {"price_change_h1": 18_377.0, "mcap_usd": 548_888.0}}),
            features_json="{}",
            risk_flags_json="[]",
        )
        db.add(ugly)
        db.flush()
        db.add(Outcome(token_id=ugly.id, t0_mcap=40_000.0, max_mcap=548_888.0, last_mcap=548_888.0, last_liq=80_000.0))
        db.add(Snapshot(token_id=ugly.id, kind="t0", mcap_usd=548_888.0, liquidity_usd=80_000.0, volume_h1=200_000.0))

        ghost = Token(
            mint="0xghostleftover00000000000000000000000001",
            symbol="AIAIAI",
            chain="robinhood",
            source="rh_dex",
            created_at_chain=now - timedelta(hours=2),
            first_seen_at=now - timedelta(hours=1),
        )
        ghost.research = Research(
            raw_json=json.dumps({"market": {"price_change_h1": 9.955e21, "mcap_usd": 285_253.0}}),
            features_json="{}",
            risk_flags_json="[]",
        )
        db.add(ghost)
        db.flush()
        db.add(Outcome(token_id=ghost.id, t0_mcap=40_000.0, max_mcap=40_000.0, last_liq=148.0))
        db.add(Snapshot(token_id=ghost.id, kind="t0", mcap_usd=285_253.0, liquidity_usd=148.67, volume_h1=1_744.57))
        db.commit()

        assert restore_rh_late_dex_junk_t0(db) == 1
        db.commit()
        restored = db.query(Outcome).filter(Outcome.token_id == ugly.id).one()
        assert restored.t0_mcap == 548_888.0
        parked = db.query(Outcome).filter(Outcome.token_id == ghost.id).one()
        assert parked.t0_mcap == 40_000.0
        assert repair_rh_late_dex_t0(db) == 0
        assert db.query(Outcome).filter(Outcome.token_id == ugly.id).one().t0_mcap == 548_888.0
        assert len(FEATURE_NAMES) == 66
    finally:
        db.close()


def test_reanchor_ghost_robinhood_meth_class():
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Snapshot, Token
    from launchfinder.scoring.outcomes import reanchor_ghost_robinhood

    init_db()
    with session_scope() as session:
        token = Token(
            mint="0xmethreanchor000000000000000000000000001",
            symbol="METH",
            chain="robinhood",
            source="rh_trenches",
        )
        session.add(token)
        session.flush()
        session.add(Snapshot(token_id=token.id, kind="t0", mcap_usd=34_195.0, liquidity_usd=105.0, volume_h1=8.0))
        session.add(Snapshot(token_id=token.id, kind="t1h", mcap_usd=48_061.0, liquidity_usd=38_144.0, volume_h1=6_515.0))
        outcome = Outcome(token_id=token.id, t0_mcap=34_195.0, max_mcap=48_061.0, multiple=1.41, t1h_mcap=41_938.0)
        session.add(outcome)
        session.flush()
        assert reanchor_ghost_robinhood(session, token, outcome, 41_938.0, 38_144.0, 6_515.0) is True
        # Earliest stored live print wins over the current Dex tick.
        assert outcome.t0_mcap == 48_061.0
        assert outcome.max_mcap == 48_061.0
        assert outcome.multiple == 1.0
        assert outcome.t1h_mcap is None  # horizons recaptured against live pool
        t0 = session.query(Snapshot).filter(Snapshot.token_id == token.id, Snapshot.kind == "t0").one()
        assert t0.mcap_usd == 48_061.0
        assert t0.liquidity_usd == 38_144.0


def test_reanchor_rescores_cashbird_class_empty_cap():
    # Ingest scored 0.48 on last_liq=0 / 24 holders (empty cap). Ghost t0
    # re-anchor finds a $38k book while multiple is still 1.0 — lift off
    # 0.48 so paper/capture see the live book before 2x. Do not lift a
    # name that already ran (CASHBIRD 6.7x).
    import json

    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Research, Snapshot, Token
    from launchfinder.scoring.features import extract_features
    from launchfinder.scoring.model import predict
    from launchfinder.scoring.outcomes import reanchor_ghost_robinhood

    init_db()
    with session_scope() as session:
        empty = extract_features(
            {
                "chain": "robinhood",
                "coin": {"name": "Cashbird", "symbol": "CASHBIRD"},
                "twitter": {"followers": 2_400, "age_days": 200, "verified": False, "tweets": 80},
                "twitter_handle": "cashbird",
                "website": "https://example.com",
                "holders": {"holder_count": 24, "top10_pct": 40},
                "market": {"liquidity_usd": 0, "volume_h1": 0},
                "last_liq": 0,
                "age_min": 5,
            }
        )
        assert empty["rh_empty_book"] == 1.0
        token = Token(
            mint="0xcashbirdrescore00000000000000000000001",
            symbol="CASHBIRD",
            chain="robinhood",
            source="rh_trenches",
        )
        scored = predict(session, empty, chain="robinhood")
        token.research = Research(
            p_good=scored["p_good"],
            heuristic_p=scored["heuristic_p"],
            model_p=scored["model_p"],
            features_json=json.dumps(empty),
            holder_count=24,
        )
        session.add(token)
        session.flush()
        session.add(Snapshot(token_id=token.id, kind="t0", mcap_usd=40_000.0, liquidity_usd=0.0, volume_h1=0.0))
        outcome = Outcome(
            token_id=token.id,
            t0_mcap=40_000.0,
            max_mcap=40_000.0,
            multiple=1.0,
            last_liq=0.0,
        )
        session.add(outcome)
        session.flush()
        assert token.research.p_good <= 0.48
        assert reanchor_ghost_robinhood(session, token, outcome, 41_938.0, 38_144.0, 6_515.0) is True
        assert outcome.last_liq == 38_144.0
        assert outcome.multiple < 2.0
        assert token.research.p_good > 0.48
        stored = json.loads(token.research.features_json)
        assert stored["rh_empty_book"] == 1.0  # at-entry vector stays frozen

        ran = Token(
            mint="0xcashbirdalreadyran00000000000000000001",
            symbol="CASHRAN",
            chain="robinhood",
            source="rh_trenches",
        )
        ran.research = Research(
            p_good=0.48,
            features_json=json.dumps(empty),
            holder_count=24,
        )
        session.add(ran)
        session.flush()
        session.add(Snapshot(token_id=ran.id, kind="t0", mcap_usd=40_000.0, liquidity_usd=0.0, volume_h1=0.0))
        session.add(Snapshot(token_id=ran.id, kind="early", mcap_usd=20_000.0, liquidity_usd=20_000.0, volume_h1=4_000.0))
        session.add(Snapshot(token_id=ran.id, kind="t15m", mcap_usd=50_000.0, liquidity_usd=38_144.0, volume_h1=6_515.0))
        ran_out = Outcome(
            token_id=ran.id,
            t0_mcap=40_000.0,
            max_mcap=50_000.0,
            multiple=1.25,
            last_liq=0.0,
        )
        session.add(ran_out)
        session.flush()
        assert reanchor_ghost_robinhood(session, ran, ran_out, 50_000.0, 38_144.0, 6_515.0) is True
        assert ran_out.multiple >= 2.0
        assert ran.research.p_good == 0.48


def test_reanchor_skips_honest_t0_and_solana():
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Snapshot, Token
    from launchfinder.scoring.outcomes import reanchor_ghost_robinhood

    init_db()
    with session_scope() as session:
        bricked = Token(mint="0xbrickedreanchor00000000000000000000001", symbol="BRICKED", chain="robinhood")
        session.add(bricked)
        session.flush()
        session.add(Snapshot(token_id=bricked.id, kind="t0", mcap_usd=41_774.0, liquidity_usd=30_804.0, volume_h1=14_730.0))
        honest = Outcome(token_id=bricked.id, t0_mcap=41_774.0, max_mcap=101_919.0, multiple=2.44)
        session.add(honest)
        session.flush()
        assert reanchor_ghost_robinhood(session, bricked, honest, 101_919.0, 22_502.0, 8_000.0) is False
        assert honest.t0_mcap == 41_774.0

        sol = Token(mint="SolGhostMint111111111111111111111111111", symbol="SOL", chain="sol")
        session.add(sol)
        session.flush()
        session.add(Snapshot(token_id=sol.id, kind="t0", mcap_usd=69_000.0, liquidity_usd=105.0, volume_h1=8.0))
        sol_out = Outcome(token_id=sol.id, t0_mcap=69_000.0, max_mcap=69_000.0, multiple=1.0)
        session.add(sol_out)
        session.flush()
        assert reanchor_ghost_robinhood(session, sol, sol_out, 80_000.0, 20_000.0, 5_000.0) is False
        assert sol_out.t0_mcap == 69_000.0


def test_reanchor_uses_stored_live_print_when_dex_is_quiet():
    # FAT-class: t0 was a Dex miss, early snaps saw the real pool, current
    # tick is leftover LP again. Do not wait for another live Dex print.
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Snapshot, Token
    from launchfinder.scoring.outcomes import reanchor_ghost_robinhood, repair_rh_ghost_t0

    init_db()
    with session_scope() as session:
        fat = Token(
            mint="0xfatreanchor0000000000000000000000000001",
            symbol="FAT",
            chain="robinhood",
            source="rh_trenches",
            is_historical=True,
        )
        session.add(fat)
        session.flush()
        session.add(Snapshot(token_id=fat.id, kind="t0", mcap_usd=0.0, liquidity_usd=0.0, volume_h1=0.0))
        session.add(Snapshot(token_id=fat.id, kind="early", mcap_usd=20_364.0, liquidity_usd=20_400.0, volume_h1=15_143.0))
        outcome = Outcome(token_id=fat.id, t0_mcap=40_000.0, max_mcap=40_000.0, multiple=1.0)
        session.add(outcome)
        session.flush()
        assert reanchor_ghost_robinhood(session, fat, outcome, 20_269.0, 20_269.0, 40.0) is True
        assert outcome.t0_mcap == 20_364.0
        assert outcome.max_mcap == 20_364.0
        assert outcome.multiple == 1.0

        bag = Token(
            mint="0xbagreanchor0000000000000000000000000001",
            symbol="BAG",
            chain="robinhood",
            source="rh_trenches",
        )
        session.add(bag)
        session.flush()
        session.add(Snapshot(token_id=bag.id, kind="t0", mcap_usd=0.0, liquidity_usd=0.0, volume_h1=0.0))
        session.add(Snapshot(token_id=bag.id, kind="early", mcap_usd=101_484.0, liquidity_usd=70_473.0, volume_h1=32_541.0))
        session.add(Snapshot(token_id=bag.id, kind="t15m", mcap_usd=22_249.0, liquidity_usd=22_318.0, volume_h1=58_375.0))
        bag_out = Outcome(token_id=bag.id, t0_mcap=40_000.0, max_mcap=101_484.0, multiple=2.54)
        session.add(bag_out)
        session.flush()
        assert repair_rh_ghost_t0(session) >= 1
        assert bag_out.t0_mcap == 101_484.0
        assert bag_out.multiple == 1.0  # floor-to-first-print is not a 2.5x run


def test_adopt_rh_floor_when_t0_snap_is_a_live_small_book():
    # CATTIES: live $3.3k t0 snap, outcome parked on the $40k floor because
    # 0.4×-floor treated a real small RH book as a Solana-style collapse.
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Snapshot, Token
    from launchfinder.scoring.outcomes import adopt_rh_floor_when_t0_is_live, repair_rh_ghost_t0

    init_db()
    with session_scope() as session:
        catties = Token(
            mint="0xcattiesfloor000000000000000000000000001",
            symbol="CATTIES",
            chain="robinhood",
            source="rh_trenches",
            is_historical=True,
        )
        session.add(catties)
        session.flush()
        session.add(Snapshot(token_id=catties.id, kind="t0", mcap_usd=3_353.0, liquidity_usd=3_720.0, volume_h1=63_683.0))
        session.add(Snapshot(token_id=catties.id, kind="t6h", mcap_usd=3_344.0, liquidity_usd=3_349.0, volume_h1=0.0))
        outcome = Outcome(token_id=catties.id, t0_mcap=40_000.0, max_mcap=40_000.0, multiple=1.0)
        session.add(outcome)
        session.flush()
        assert adopt_rh_floor_when_t0_is_live(session, catties, outcome) is True
        assert outcome.t0_mcap == 3_353.0
        assert outcome.max_mcap == 3_353.0
        assert outcome.multiple == 1.0

        bricked = Token(
            mint="0xbrickfloor00000000000000000000000000001",
            symbol="BRICKED",
            chain="robinhood",
        )
        session.add(bricked)
        session.flush()
        session.add(Snapshot(token_id=bricked.id, kind="t0", mcap_usd=41_774.0, liquidity_usd=30_804.0, volume_h1=14_730.0))
        honest = Outcome(token_id=bricked.id, t0_mcap=41_774.0, max_mcap=101_919.0, multiple=2.44)
        session.add(honest)
        session.flush()
        assert adopt_rh_floor_when_t0_is_live(session, bricked, honest) is False
        assert honest.t0_mcap == 41_774.0

        # Repair pass picks CATTIES-class after ghost re-anchor does not apply.
        again = Token(
            mint="0xcattiesfloor000000000000000000000000002",
            symbol="KITTY",
            chain="robinhood",
        )
        session.add(again)
        session.flush()
        session.add(Snapshot(token_id=again.id, kind="t0", mcap_usd=8_200.0, liquidity_usd=9_100.0, volume_h1=4_400.0))
        kit = Outcome(token_id=again.id, t0_mcap=40_000.0, max_mcap=40_000.0, multiple=1.0)
        session.add(kit)
        session.flush()
        assert repair_rh_ghost_t0(session) >= 1
        assert kit.t0_mcap == 8_200.0


def test_park_rh_ghost_high_t0_aiaiai_class():
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Snapshot, Token
    from launchfinder.scoring.outcomes import park_rh_ghost_high_t0, repair_rh_ghost_t0

    init_db()
    with session_scope() as session:
        ai = Token(
            mint="0xaiaiaighost000000000000000000000000001",
            symbol="AIAIAI",
            chain="robinhood",
            source="rh_trenches",
        )
        session.add(ai)
        session.flush()
        session.add(Snapshot(token_id=ai.id, kind="t0", mcap_usd=285_253.0, liquidity_usd=148.67, volume_h1=1_744.57))
        outcome = Outcome(token_id=ai.id, t0_mcap=285_253.0, max_mcap=285_253.0, multiple=1.0, last_liq=148.67)
        session.add(outcome)
        session.flush()
        assert park_rh_ghost_high_t0(session, ai, outcome) is True
        assert outcome.t0_mcap == 40_000.0
        assert outcome.max_mcap == 40_000.0
        assert outcome.last_liq == 0.0

        shark = Token(
            mint="0xsharklive00000000000000000000000000001",
            symbol="SHARK",
            chain="robinhood",
            source="rh_trenches",
        )
        session.add(shark)
        session.flush()
        session.add(Snapshot(token_id=shark.id, kind="t0", mcap_usd=42_374.0, liquidity_usd=38_413.0, volume_h1=28_667.0))
        live = Outcome(token_id=shark.id, t0_mcap=42_374.0, max_mcap=67_766.0, multiple=1.60, last_liq=34_287.0)
        session.add(live)
        session.flush()
        assert park_rh_ghost_high_t0(session, shark, live) is False
        assert live.t0_mcap == 42_374.0

        again = Token(
            mint="0xaiaiaighost000000000000000000000000002",
            symbol="AIA2",
            chain="robinhood",
        )
        session.add(again)
        session.flush()
        session.add(Snapshot(token_id=again.id, kind="t0", mcap_usd=199_000.0, liquidity_usd=200.0, volume_h1=80.0))
        parked = Outcome(token_id=again.id, t0_mcap=199_000.0, max_mcap=199_000.0, multiple=1.0, last_liq=200.0)
        session.add(parked)
        session.flush()
        assert repair_rh_ghost_t0(session) >= 1
        assert parked.t0_mcap == 40_000.0


def test_repair_rh_small_book_clears_jjjacket_collapse():
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Research, Token
    from launchfinder.scoring.outcomes import repair_rh_small_book_collapse

    init_db()
    with session_scope() as session:
        jj = Token(
            mint="0xjjjacketrepair000000000000000000000001",
            symbol="JJJACKET",
            chain="robinhood",
            source="rh_trenches",
        )
        jj.research = Research(
            p_good=0.12,
            features_json='{"entry_collapse": 1.0, "liq": 1}',
            holder_count=186,
            risk_flags_json='["Already dumped below graduation mcap (start-high rug pattern)"]',
        )
        session.add(jj)
        session.flush()
        session.add(
            Outcome(
                token_id=jj.id,
                t0_mcap=12_929.0,
                max_mcap=180_899.0,
                last_liq=130_808.0,
                multiple=14.0,
                label=1,
            )
        )
        session.flush()
        repair_rh_small_book_collapse(session)
        research = session.query(Research).filter(Research.token_id == jj.id).one()
        feats = __import__("json").loads(research.features_json)
        assert feats["entry_collapse"] == 0.0
        assert "start-high rug" not in (research.risk_flags_json or "")


def test_rh_leftover_fdv_skips_mordor_keeps_hsh_and_rock():
    from launchfinder.scoring.outcomes import is_rh_leftover_fdv

    mordor = Outcome(t0_mcap=160_918.0, max_mcap=160_918.0, last_liq=160_920.0, multiple=1.0)
    assert is_rh_leftover_fdv(mordor, holders=5, chain="robinhood") is True
    hsh = Outcome(t0_mcap=21_000.0, max_mcap=21_000.0, last_liq=21_000.0, multiple=1.0)
    assert is_rh_leftover_fdv(hsh, holders=29, chain="robinhood") is False
    rock = Outcome(t0_mcap=15_700.0, max_mcap=84_500.0, last_liq=84_000.0, multiple=5.38)
    assert is_rh_leftover_fdv(rock, holders=79, chain="robinhood") is False
    sol = Outcome(t0_mcap=160_918.0, max_mcap=160_918.0, last_liq=160_920.0, multiple=1.0)
    assert is_rh_leftover_fdv(sol, holders=5, chain="sol") is False


def test_repair_rh_leftover_fdv_parks_mordor_keeps_hsh():
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Research, ScanState, Token
    from launchfinder.scoring.outcomes import REPAIR_RH_LEFTOVER_FDV_KEY, repair_rh_leftover_fdv

    init_db()
    with session_scope() as session:
        session.query(ScanState).filter(ScanState.key == REPAIR_RH_LEFTOVER_FDV_KEY).delete()
        mordor = Token(
            mint="0xmordorrepair00000000000000000000000001",
            symbol="MORDOR",
            chain="robinhood",
            source="rh_trenches",
        )
        mordor.research = Research(p_good=0.19, features_json="{}", holder_count=5)
        hsh = Token(
            mint="0xhshrepair00000000000000000000000000001",
            symbol="HSH",
            chain="robinhood",
            source="rh_trenches",
        )
        hsh.research = Research(p_good=0.54, features_json="{}", holder_count=29)
        session.add_all([mordor, hsh])
        session.flush()
        session.add(
            Outcome(
                token_id=mordor.id,
                t0_mcap=160_918.0,
                max_mcap=160_918.0,
                last_liq=160_920.0,
                multiple=1.0,
                label=0,
                used_for_train=True,
            )
        )
        session.add(
            Outcome(
                token_id=hsh.id,
                t0_mcap=21_318.0,
                max_mcap=21_318.0,
                last_liq=21_001.0,
                multiple=1.0,
                label=0,
            )
        )
        session.flush()
        assert repair_rh_leftover_fdv(session) >= 1
        assert session.query(Outcome).filter(Outcome.token_id == mordor.id).one().last_liq == 0.0
        assert session.query(Outcome).filter(Outcome.token_id == hsh.id).one().last_liq == 21_001.0
        assert repair_rh_leftover_fdv(session) == 0


def test_rh_leftover_fdv_24h_label_parks_book_and_skips_train():
    outcome = Outcome(t0_mcap=160_918.0, max_mcap=160_918.0, last_liq=160_920.0)
    _label(outcome, {}, historical=False, holders=5, chain="robinhood")
    assert outcome.label == 0
    assert outcome.last_liq == 0.0
    assert outcome.used_for_train is True
    honest = Outcome(t0_mcap=21_000.0, max_mcap=21_000.0, last_liq=21_000.0)
    _label(honest, {}, historical=False, holders=29, chain="robinhood")
    assert honest.label == 0
    assert honest.last_liq == 21_000.0
    assert not honest.used_for_train


def test_repair_organic_book_lifts_only():
    import json

    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Research, ScanState, Token
    from launchfinder.scoring.features import extract_features
    from launchfinder.scoring.outcomes import REPAIR_ORGANIC_SCORE_KEY, repair_organic_book_scores

    init_db()
    with session_scope() as session:
        session.query(ScanState).filter(ScanState.key == REPAIR_ORGANIC_SCORE_KEY).delete()
        cac = Token(mint="CacRepairMint11111111111111111111111111", symbol="CAC", chain="sol", source="poll")
        cac.research = Research(
            p_good=0.793,
            features_json=json.dumps(
                extract_features(
                    {
                        "coin": {"name": "Cac", "symbol": "CAC"},
                        "holders": {"holder_count": 18, "top10_pct": 100},
                        "creator_stats": {"launches": 4, "wins": 2, "rugs": 0},
                        "market": {"liquidity_usd": 0, "volume_h1": 0},
                    }
                )
            ),
        )
        duck = Token(mint="DuckRepairMint1111111111111111111111111", symbol="MACRODUCK", chain="sol", source="poll")
        duck.research = Research(
            p_good=0.31,
            features_json=json.dumps(
                extract_features(
                    {
                        "coin": {"name": "Macro Duck", "symbol": "MACRODUCK"},
                        "twitter": {"followers": 6500, "age_days": 500, "verified": True},
                        "twitter_handle": "esotericpigeon",
                        "website": "https://example.com",
                        "holders": {"holder_count": 432, "top10_pct": 36, "creator_hold_pct": 5},
                        "creator_stats": {"launches": 8, "wins": 1, "rugs": 0},
                        "time_to_migrate_min": 80,
                        "market": {
                            "buys_m5": 30,
                            "sells_m5": 22,
                            "liquidity_usd": 40_000,
                            "volume_h1": 60_000,
                        },
                        "gmgn": {
                            "source": "gmgn",
                            "smart_degen": 20,
                            "bot_rate": 67,
                            "bundler_vol_pct": 40,
                            "rug_risk": 5,
                        },
                    }
                )
            ),
        )
        session.add_all([cac, duck])
        session.flush()
        repair_organic_book_scores(session)
        session.refresh(cac.research)
        session.refresh(duck.research)
        assert cac.research.p_good == 0.793
        assert duck.research.p_good >= 0.60


def test_repair_organic_wide_lifts_sol_pvp_not_rh():
    import json

    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Research, ScanState, Token
    from launchfinder.scoring.features import extract_features
    from launchfinder.scoring.outcomes import REPAIR_ORGANIC_WIDE_KEY, repair_organic_wide_book_scores

    init_db()
    with session_scope() as session:
        session.query(ScanState).filter(ScanState.key == REPAIR_ORGANIC_WIDE_KEY).delete()
        pvp_feats = extract_features(
            {
                "coin": {"name": "Pvp", "symbol": "PVP"},
                "holders": {"holder_count": 138, "top10_pct": 38},
                "market": {"liquidity_usd": 15_284, "volume_h1": 1_120},
            }
        )
        pvp = Token(mint="PvpWideRepairMint1111111111111111111111", symbol="PVP", chain="sol", source="poll")
        pvp.research = Research(p_good=0.364, heuristic_p=0.29, features_json=json.dumps(pvp_feats))
        rh = Token(mint="0xrhwide0000000000000000000000000000000001", symbol="BB", chain="robinhood", source="rh_trenches")
        rh.research = Research(p_good=0.45, heuristic_p=0.45, features_json=json.dumps(pvp_feats))
        cac = Token(mint="CacWideRepairMint11111111111111111111111", symbol="CAC", chain="sol", source="poll")
        cac.research = Research(
            p_good=0.793,
            features_json=json.dumps(
                extract_features(
                    {
                        "coin": {"name": "Cac", "symbol": "CAC"},
                        "holders": {"holder_count": 18, "top10_pct": 100},
                        "creator_stats": {"launches": 4, "wins": 2, "rugs": 0},
                        "market": {"liquidity_usd": 0, "volume_h1": 0},
                    }
                )
            ),
        )
        session.add_all([pvp, rh, cac])
        session.flush()
        repair_organic_wide_book_scores(session)
        session.refresh(pvp.research)
        session.refresh(rh.research)
        session.refresh(cac.research)
        assert pvp.research.p_good >= 0.60
        assert rh.research.p_good == 0.45
        assert cac.research.p_good == 0.793


def test_repair_rh_thin_book_caps_only_skinny_rh_rows():
    import json

    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Research, ScanState, Token
    from launchfinder.scoring.outcomes import REPAIR_RH_THIN_BOOK_KEY, repair_rh_thin_book_scores

    init_db()
    with session_scope() as session:
        session.query(ScanState).filter(ScanState.key == REPAIR_RH_THIN_BOOK_KEY).delete()
        skinny = Token(mint="0xthin000000000000000000000000000000000001", symbol="JOHNAPPL", chain="robinhood", source="rh_trenches")
        skinny.research = Research(p_good=0.67, heuristic_p=0.67, holder_count=4, features_json="{}", risk_flags_json="[]")
        wide = Token(mint="0xwide000000000000000000000000000000000001", symbol="WOODY", chain="robinhood", source="rh_trenches")
        wide.research = Research(p_good=0.91, heuristic_p=0.91, holder_count=119, features_json="{}", risk_flags_json="[]")
        sol = Token(mint="SolThinMint11111111111111111111111111111", symbol="BEAST", chain="sol", source="poll")
        sol.research = Research(p_good=0.79, heuristic_p=0.79, holder_count=18, features_json="{}", risk_flags_json="[]")
        session.add_all([skinny, wide, sol])
        session.flush()
        repair_rh_thin_book_scores(session)
        session.refresh(skinny.research)
        session.refresh(wide.research)
        session.refresh(sol.research)
        assert skinny.research.p_good == 0.48
        assert "thin" in " ".join(json.loads(skinny.research.risk_flags_json)).lower()
        assert wide.research.p_good == 0.91
        assert sol.research.p_good == 0.79


def test_repair_rh_empty_book_caps_only_dry_rh_rows():
    import json

    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, ScanState, Token
    from launchfinder.scoring.outcomes import REPAIR_RH_EMPTY_SCORE_KEY, repair_rh_empty_book_scores

    init_db()
    with session_scope() as session:
        session.query(ScanState).filter(ScanState.key == REPAIR_RH_EMPTY_SCORE_KEY).delete()
        dry = Token(mint="0xretailsempty000000000000000000000001", symbol="RETAILS", chain="robinhood", source="rh_trenches")
        dry.research = Research(p_good=0.92, heuristic_p=0.92, holder_count=36, features_json="{}", risk_flags_json="[]")
        wet = Token(mint="0xwoodyliqbook00000000000000000000001", symbol="WOODY", chain="robinhood", source="rh_trenches")
        wet.research = Research(p_good=0.91, heuristic_p=0.91, holder_count=119, features_json="{}", risk_flags_json="[]")
        sol = Token(mint="SolDryBookMint1111111111111111111111", symbol="BEAST", chain="sol", source="poll")
        sol.research = Research(p_good=0.79, heuristic_p=0.79, holder_count=80, features_json="{}", risk_flags_json="[]")
        session.add_all([dry, wet, sol])
        session.flush()
        session.add(Outcome(token_id=dry.id, t0_mcap=40_000.0, max_mcap=40_000.0, multiple=1.0, last_liq=0.0))
        session.add(Outcome(token_id=wet.id, t0_mcap=43_332.0, max_mcap=43_452.0, multiple=1.0, last_liq=39_894.0))
        session.add(Outcome(token_id=sol.id, t0_mcap=69_000.0, max_mcap=69_000.0, multiple=1.0, last_liq=0.0))
        session.flush()
        repair_rh_empty_book_scores(session)
        session.refresh(dry.research)
        session.refresh(wet.research)
        session.refresh(sol.research)
        assert dry.research.p_good == 0.48
        assert "liquidity" in " ".join(json.loads(dry.research.risk_flags_json)).lower()
        assert wet.research.p_good == 0.91
        assert sol.research.p_good == 0.79


def test_hydrate_rh_empty_book_lifts_only_under_2x():
    import json

    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, Token
    from launchfinder.scoring.features import extract_features
    from launchfinder.scoring.outcomes import hydrate_rh_empty_book

    init_db()
    empty = extract_features(
        {
            "chain": "robinhood",
            "coin": {"name": "Bb", "symbol": "BB"},
            "holders": {"holder_count": 193, "top10_pct": 33},
            "market": {"liquidity_usd": 0, "volume_h1": 0},
        }
    )
    assert empty["liquidity_n"] == 0.0
    with session_scope() as session:
        climber = Token(
            mint="0xbb00000000000000000000000000000000000001",
            symbol="BB",
            chain="robinhood",
            source="rh_trenches",
        )
        climber.research = Research(
            p_good=0.45,
            heuristic_p=0.45,
            holder_count=193,
            features_json=json.dumps(empty),
            raw_json='{"market": {"liquidity_usd": 0, "volume_h1": 0}}',
        )
        climber.outcome = Outcome(t0_mcap=58_000, max_mcap=173_000, multiple=2.99, last_liq=16_912)
        fresh = Token(
            mint="0xfresh000000000000000000000000000000000001",
            symbol="NEW",
            chain="robinhood",
            source="rh_trenches",
        )
        fresh.research = Research(
            p_good=0.45,
            heuristic_p=0.45,
            holder_count=80,
            features_json=json.dumps(empty),
            raw_json='{"market": {"liquidity_usd": 0}}',
        )
        fresh.outcome = Outcome(t0_mcap=20_000, max_mcap=22_000, multiple=1.1, last_liq=12_000)
        thin = Token(
            mint="0xthinbook00000000000000000000000000000001",
            symbol="THIN",
            chain="robinhood",
            source="rh_trenches",
        )
        thin.research = Research(
            p_good=0.40,
            holder_count=6,
            features_json=json.dumps(empty),
        )
        thin.outcome = Outcome(t0_mcap=20_000, max_mcap=21_000, multiple=1.05, last_liq=9_000)
        session.add_all([climber, fresh, thin])
        session.flush()
        assert hydrate_rh_empty_book(session, climber, climber.outcome)
        assert hydrate_rh_empty_book(session, fresh, fresh.outcome, {"volume_h1": 8_000})
        assert not hydrate_rh_empty_book(session, thin, thin.outcome)
        meme = Token(
            mint="0x385f4f8ae47651ce5f58f5265395a669f8281e18",
            symbol="MEME",
            chain="robinhood",
            source="rh_trenches",
        )
        meme.research = Research(
            p_good=0.15,
            heuristic_p=0.15,
            holder_count=4,
            features_json=json.dumps(empty),
            raw_json=json.dumps(
                {
                    "market": {"liquidity_usd": 0},
                    "holders": {
                        "holder_count": 4,
                        "top_wallets": [
                            {"pct": 98.8, "label": "pool"},
                            {"pct": 1.2, "label": ""},
                        ],
                    },
                    "gmgn": {"holder_count": 80, "source": "gmgn"},
                }
            ),
        )
        meme.outcome = Outcome(t0_mcap=21_997, max_mcap=24_000, multiple=1.09, last_liq=80_000)
        session.add(meme)
        session.flush()
        assert hydrate_rh_empty_book(session, meme, meme.outcome)
        session.flush()
        session.refresh(meme.research)
        meme_feats = json.loads(meme.research.features_json)
        assert meme_feats["liquidity_n"] >= 0.75
        assert meme_feats.get("rh_thin_book") == 0.0
        assert meme.research.holder_count >= 20
        assert meme.research.p_good > 0.15
        session.flush()
        session.refresh(climber.research)
        session.refresh(fresh.research)
        session.refresh(thin.research)
        climbed = json.loads(climber.research.features_json)
        assert climbed["liquidity_n"] >= 0.75
        assert climber.research.p_good == 0.45
        assert json.loads(climber.research.raw_json)["market"]["liquidity_usd"] == 16_912
        assert json.loads(fresh.research.features_json)["liquidity_n"] >= 0.75
        assert fresh.research.p_good > 0.45
        assert thin.research.p_good == 0.40


def test_repair_rh_empty_book_is_one_shot():
    import json

    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, ScanState, Token
    from launchfinder.scoring.features import extract_features
    from launchfinder.scoring.outcomes import REPAIR_RH_EMPTY_BOOK_KEY, repair_rh_empty_book_from_last_liq

    init_db()
    empty = extract_features(
        {
            "chain": "robinhood",
            "coin": {"name": "New", "symbol": "NEW"},
            "holders": {"holder_count": 80, "top10_pct": 30},
            "market": {"liquidity_usd": 0},
        }
    )
    with session_scope() as session:
        session.query(ScanState).filter(ScanState.key == REPAIR_RH_EMPTY_BOOK_KEY).delete()
        token = Token(mint="0xoneshot000000000000000000000000000000001", symbol="NEW", chain="robinhood")
        token.research = Research(p_good=0.40, holder_count=80, features_json=json.dumps(empty), raw_json="{}")
        token.outcome = Outcome(t0_mcap=20_000, max_mcap=21_000, multiple=1.05, last_liq=15_000)
        # volume lives on last snap-equivalent via last_liq-only hydrate;
        # one-shot still patches liquidity_n even when it does not lift.
        session.add(token)
        session.flush()
        repair_rh_empty_book_from_last_liq(session)
        session.refresh(token.research)
        first = token.research.p_good
        assert json.loads(token.research.features_json)["liquidity_n"] >= 0.75
        repair_rh_empty_book_from_last_liq(session)
        session.refresh(token.research)
        assert token.research.p_good == first


def test_retarget_live_pool_follows_deepest_pair():
    from launchfinder.models import Token
    from launchfinder.scoring.outcomes import retarget_live_pool

    first = "0x27ccf0a6d1ee74840220715bcca7d3b01e0d33aa30d0259b47ae1585b3f4c071"
    deep = "0x161610470ba69ac0ae86724efa3a379cfbeb81a5efcea1eacac4a3cddd230b93"
    token = Token(
        mint="0x385f4f8ae47651ce5f58f5265395a669f8281e18",
        symbol="MEME",
        chain="robinhood",
        pool_address=first,
    )
    assert retarget_live_pool(token, {"pair_address": deep, "liquidity_usd": 1_308_471.0}) is True
    assert token.pool_address == deep
    assert retarget_live_pool(token, {"pair_address": deep, "liquidity_usd": 1_308_471.0}) is False
    assert retarget_live_pool(token, {"pair_address": "0xabc", "liquidity_usd": 9_000_000.0}) is False
    assert token.pool_address == deep


def test_record_live_last_writes_mcap_without_raising_80x_ath():
    from datetime import datetime, timedelta, timezone

    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, Snapshot, Token
    from launchfinder.scoring.outcomes import record_live_last

    init_db()
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=16)
    first = "0x27ccf0a6d1ee74840220715bcca7d3b01e0d33aa30d0259b47ae1585b3f4c071"
    deep = "0xc6e298e137f2905398db87e6eae49ede64d231fee37330fa433fec917f4618b6"
    with session_scope() as session:
        token = Token(
            mint="0x385f4f8ae47651ce5f58f5265395a669f8281e18",
            symbol="MEME",
            chain="robinhood",
            source="rh_trenches",
            pool_address=first,
            first_seen_at=start,
            created_at_chain=start,
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
            )
        )
        session.add(
            Snapshot(
                token_id=token.id,
                kind="early",
                taken_at=start + timedelta(hours=1),
                mcap_usd=175_437.0,
                liquidity_usd=102_433.0,
            )
        )
        session.flush()
        token = session.query(Token).filter(Token.mint == token.mint).one()
        outcome = token.outcome
        record_live_last(
            session,
            token,
            outcome,
            {
                "pair_address": deep,
                "mcap_usd": 81_845_931.0,
                "liquidity_usd": 1_308_471.0,
                "volume_h1": 1_055_679.0,
                "price_usd": 0.08184,
            },
            81_845_931.0,
            now,
            start=start,
        )
        assert abs(outcome.last_mcap - 81_845_931.0) < 1e-6
        assert outcome.max_mcap == 1_073_625.0
        assert token.pool_address == deep
        late = [s for s in token.snapshots if s.kind == "late"]
        assert len(late) == 1
        assert abs(late[0].mcap_usd - 81_845_931.0) < 1e-6


def test_rh_doing_well_refresh_picks_smallest_2x_not_sol():
    """ETAC-class mid-tape 90+ must win a chair before NeMo 500×."""
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, Token
    from launchfinder.scoring.outcomes import _rh_doing_well_refresh_rows

    init_db()
    with session_scope() as session:
        fat = Token(mint="0xrhfat000000000000000000000000000000001", symbol="NEMO", chain="robinhood", source="rh_trenches")
        etac = Token(mint="0x3fb6ab8e2b99570f9e5faf783e465e315df0d68b", symbol="ETAC", chain="robinhood", source="rh_trenches")
        small = Token(mint="0xrhsmall0000000000000000000000000000001", symbol="DUST", chain="robinhood", source="rh_trenches")
        sol = Token(mint="SolDoingWell111111111111111111111111111", symbol="SOLX", chain="sol", source="poll")
        flat = Token(mint="0xrhflat00000000000000000000000000000001", symbol="FLAT", chain="robinhood", source="rh_trenches")
        session.add_all([fat, etac, small, sol, flat])
        session.flush()
        session.add(Research(token_id=fat.id, p_good=0.43, features_json="{}"))
        session.add(Research(token_id=etac.id, p_good=0.92, features_json="{}"))
        session.add(Research(token_id=small.id, p_good=0.20, features_json="{}"))
        session.add(Outcome(token_id=fat.id, t0_mcap=44_000.0, last_mcap=22_000_000.0, max_mcap=22_000_000.0, multiple=500.0, last_liq=80_000.0, label=1))
        session.add(Outcome(token_id=etac.id, t0_mcap=16_389.0, last_mcap=223_931.0, max_mcap=223_931.0, multiple=13.7, last_liq=89_000.0, label=1))
        session.add(Outcome(token_id=small.id, t0_mcap=20_000.0, last_mcap=50_000.0, max_mcap=50_000.0, multiple=2.5, last_liq=12_000.0, label=1))
        session.add(Outcome(token_id=sol.id, t0_mcap=69_000.0, last_mcap=180_000.0, max_mcap=180_000.0, multiple=2.6, last_liq=20_000.0, label=1))
        session.add(Outcome(token_id=flat.id, t0_mcap=20_000.0, last_mcap=24_000.0, max_mcap=24_000.0, multiple=1.2, last_liq=10_000.0, label=1))
        session.flush()
        rows = _rh_doing_well_refresh_rows(
            session.query(Outcome, Token).join(Token, Token.id == Outcome.token_id)
        )
        mints = [token.mint for _, token in rows]
        assert etac.mint in mints
        assert fat.mint in mints
        assert sol.mint not in mints
        assert flat.mint not in mints
        assert mints[0] == etac.mint
