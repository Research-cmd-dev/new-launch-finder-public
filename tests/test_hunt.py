from datetime import timedelta

from launchfinder.db import init_db, session_scope
from launchfinder.models import HuntCard, Outcome, Research, Token, utcnow
from launchfinder.scoring.features import FEATURE_NAMES
from launchfinder.scoring.hunt import (
    HIGH_SCORE_REVIEW_FLOOR,
    HUNT_LIVE_LAST_SECONDS,
    HUNT_THIN_WATCH_LIVE_CAP,
    LIVE_MODEL_SIGNAL,
    _hunt_row_lock_contention,
    conviction_from_tape,
    high_score_mints_needing_review,
    hunt_board_rank,
    hunt_eligible,
    hunt_live_value,
    hunt_mints_needing_tick,
    hunt_thin_watch_book,
    list_hunt_mints,
    pick_live_conviction,
    rebuild_hunt_window,
    sol_pair_in_hunt_window,
    this_window_hunt_tape_mints,
    upsert_hunt,
)
from launchfinder.scoring.hunt_tape import (
    apply_hunt_tape_identity,
    apply_hunt_tape_market,
    looks_like_contract,
)
from launchfinder.serialize import still_doing_well


def test_feature_names_untouched():
    assert len(FEATURE_NAMES) == 66


def test_hunt_row_lock_contention_detects_operational_error():
    from sqlalchemy.exc import OperationalError

    assert _hunt_row_lock_contention(OperationalError("stmt", {}, Exception("lock timeout")))
    assert not _hunt_row_lock_contention(ValueError("nope"))


def test_hunt_keeps_2x_climber_drops_sol_leftover_fdv():
    init_db()
    now = utcnow()
    with session_scope() as session:
        good = Token(
            mint="HuntGood11111111111111111111111111111111",
            symbol="CLIMB",
            chain="sol",
            first_seen_at=now - timedelta(hours=2),
            migrated_at=now - timedelta(hours=2),
            source="poll",
        )
        good.research = Research(features_json="{}", p_good=0.2, holder_count=80)
        good.outcome = Outcome(t0_mcap=69_000, last_mcap=180_000, last_liq=20_000, multiple=2.6)
        ghost = Token(
            mint="HuntGhost1111111111111111111111111111111",
            symbol="GHOST",
            chain="sol",
            first_seen_at=now - timedelta(hours=2),
            migrated_at=now - timedelta(hours=2),
            source="poll",
        )
        ghost.research = Research(features_json="{}", p_good=0.4, holder_count=80)
        ghost.outcome = Outcome(t0_mcap=69_000, last_mcap=8_000_000, last_liq=40_000, multiple=116.0)
        session.add_all([good, ghost])
        session.flush()
        assert hunt_eligible(good, good.outcome, good.research, now=now) is True
        assert hunt_eligible(ghost, ghost.outcome, ghost.research, now=now) is False
        assert upsert_hunt(session, good, now=now) is not None
        assert upsert_hunt(session, ghost, now=now) is None
        mints = list_hunt_mints(session, "sol", hours=18, limit=20)
        assert good.mint in mints
        assert ghost.mint not in mints
        assert session.query(HuntCard).filter(HuntCard.mint == good.mint).count() == 1


def test_hunt_list_hides_seed_with_no_last_print():
    # AGE sort showed $69k 1.00× / Live — because last was 0 and the
    # pill treated 0 as missing. Keep the Hunt card so tape can write
    # the first Dex last; do not spend the desk 80 on a seed.
    init_db()
    now = utcnow()
    with session_scope() as session:
        seed = Token(
            mint="HuntSeed11111111111111111111111111111111",
            symbol="HALH",
            chain="sol",
            first_seen_at=now - timedelta(minutes=3),
            migrated_at=now - timedelta(minutes=3),
            source="poll",
        )
        seed.research = Research(features_json="{}", p_good=0.10, holder_count=2)
        seed.outcome = Outcome(t0_mcap=69_000, last_mcap=0, last_liq=0, multiple=0)
        named = Token(
            mint="HuntNamed1111111111111111111111111111111",
            symbol="ZPOOL",
            chain="sol",
            first_seen_at=now - timedelta(minutes=20),
            migrated_at=now - timedelta(minutes=20),
            source="poll",
        )
        named.research = Research(features_json="{}", p_good=0.12, holder_count=80)
        named.outcome = Outcome(t0_mcap=69_000, last_mcap=134_000, last_liq=22_000, multiple=1.94)
        session.add_all([seed, named])
        session.flush()
        assert hunt_eligible(seed, seed.outcome, seed.research, now=now) is True
        assert upsert_hunt(session, seed, now=now) is not None
        assert upsert_hunt(session, named, now=now) is not None
        mints = list_hunt_mints(session, "sol", hours=18, limit=20)
        assert named.mint in mints
        assert seed.mint not in mints
        assert session.query(HuntCard).filter(HuntCard.mint == seed.mint).count() == 1


def test_hunt_drops_sol_pair_that_opened_a_year_ago():
    # Live ARMY: PumpSwap pairCreatedAt 2025-09-02, migrated_at today,
    # Hunt showed 3m / Entry 97. The 18h window is the pair clock.
    from datetime import datetime, timezone

    from launchfinder.scoring.hunt import sol_launch_at

    init_db()
    now = utcnow()
    pair_open = datetime(2025, 9, 2, 17, 10, 4, tzinfo=timezone.utc)
    with session_scope() as session:
        army = Token(
            mint="ArmyOld111111111111111111111111111111111",
            symbol="ARMY",
            chain="sol",
            created_at_chain=pair_open,
            first_seen_at=now - timedelta(minutes=3),
            migrated_at=now - timedelta(minutes=3),
            source="poll",
        )
        army.research = Research(features_json="{}", p_good=0.97, holder_count=1985)
        army.outcome = Outcome(t0_mcap=113_865, last_mcap=115_993, last_liq=37_976, multiple=1.02)
        fresh = Token(
            mint="ArmyFresh1111111111111111111111111111111",
            symbol="NEW",
            chain="sol",
            created_at_chain=now - timedelta(minutes=8),
            first_seen_at=now - timedelta(minutes=3),
            migrated_at=now - timedelta(minutes=3),
            source="poll",
        )
        fresh.research = Research(features_json="{}", p_good=0.8, holder_count=200)
        fresh.outcome = Outcome(t0_mcap=80_000, last_mcap=90_000, last_liq=20_000, multiple=1.1)
        session.add_all([army, fresh])
        session.flush()
        assert sol_launch_at(army) == pair_open
        assert hunt_eligible(army, army.outcome, army.research, now=now) is False
        assert hunt_eligible(fresh, fresh.outcome, fresh.research, now=now) is True
        assert upsert_hunt(session, army, now=now) is None
        assert upsert_hunt(session, fresh, now=now) is not None


def test_hunt_list_drops_sol_leftover_first_seen_today():
    # Live DNR: pair opened 2026-08-16, migrated_at / first_seen today,
    # Hunt still showed Entry 90 because list_hunt_mints ORed first_seen.
    init_db()
    now = utcnow()
    pair_open = now - timedelta(days=28)
    seen = now - timedelta(hours=4)
    since = now - timedelta(hours=18)
    with session_scope() as session:
        leftover = Token(
            mint="DnrLeft111111111111111111111111111111111",
            symbol="DNR",
            chain="sol",
            created_at_chain=pair_open,
            first_seen_at=seen,
            migrated_at=seen,
            source="poll",
        )
        leftover.research = Research(features_json="{}", p_good=0.90, holder_count=400)
        leftover.outcome = Outcome(t0_mcap=64_276, last_mcap=72_356, last_liq=18_000, multiple=1.13)
        fresh = Token(
            mint="DnrFresh11111111111111111111111111111111",
            symbol="NEW",
            chain="sol",
            created_at_chain=now - timedelta(hours=2),
            first_seen_at=now - timedelta(hours=2),
            migrated_at=now - timedelta(hours=2),
            source="poll",
        )
        fresh.research = Research(features_json="{}", p_good=0.92, holder_count=200)
        fresh.outcome = Outcome(t0_mcap=59_000, last_mcap=80_000, last_liq=20_000, multiple=1.36)
        session.add_all([leftover, fresh])
        session.flush()
        session.add(
            HuntCard(
                chain="sol",
                mint=leftover.mint,
                token_id=leftover.id,
                first_seen_at=seen,
                launched_at=seen,
                entry_p=0.90,
                conviction_p=0.90,
                t0_mcap=64_276,
                last_mcap=72_356,
                multiple=1.13,
                last_liq=18_000,
                holders=400,
            )
        )
        session.flush()
        assert sol_pair_in_hunt_window(leftover, since=since) is False
        assert sol_pair_in_hunt_window(fresh, since=since) is True
        assert leftover.mint not in list_hunt_mints(session, "sol", hours=18, limit=20)
        rebuild_hunt_window(session, "sol")
        assert leftover.mint not in list_hunt_mints(session, "sol", hours=18, limit=20)
        assert session.query(HuntCard).filter(HuntCard.mint == leftover.mint).count() == 0
        assert upsert_hunt(session, leftover, now=now) is None
        assert upsert_hunt(session, fresh, now=now) is not None
        assert fresh.mint in list_hunt_mints(session, "sol", hours=18, limit=20)


def test_rh_hunt_keeps_recent_first_seen_when_created_at_is_old():
    # RH leftover chairs stay on migrated/first_seen. Do not apply Sol pair clock.
    init_db()
    now = utcnow()
    with session_scope() as session:
        chair = Token(
            mint="RhChair111111111111111111111111111111111",
            symbol="CHAIR",
            chain="robinhood",
            created_at_chain=now - timedelta(days=28),
            first_seen_at=now - timedelta(hours=3),
            migrated_at=now - timedelta(hours=3),
            source="poll",
        )
        chair.research = Research(features_json="{}", p_good=0.90, holder_count=80)
        chair.outcome = Outcome(t0_mcap=40_000, last_mcap=90_000, last_liq=20_000, multiple=2.25)
        session.add(chair)
        session.flush()
        assert hunt_eligible(chair, chair.outcome, chair.research, now=now) is True
        assert upsert_hunt(session, chair, now=now) is not None
        assert chair.mint in list_hunt_mints(session, "robinhood", hours=24, limit=20)


def test_rh_leftover_window_is_12_to_24h_not_a_sort():
    from launchfinder.scoring.hunt import hunt_board_rank, hunt_leftover_window

    assert hunt_leftover_window("robinhood", 13.6) is True
    assert hunt_leftover_window("robinhood", 12.0) is True
    assert hunt_leftover_window("robinhood", 24.0) is True
    assert hunt_leftover_window("robinhood", 6.0) is False
    assert hunt_leftover_window("robinhood", 24.1) is False
    assert hunt_leftover_window("sol", 13.6) is False
    leftover = hunt_board_rank({"last_mcap": 80_000, "t0_mcap": 40_000, "conviction_p": 0.20, "leftover_window": True, "first_seen_at": "z"})
    climber = hunt_board_rank({"last_mcap": 90_000, "t0_mcap": 40_000, "conviction_p": 0.40, "leftover_window": False, "first_seen_at": "a"})
    assert climber > leftover


def test_rh_hunt_window_keeps_a_13h_book():
    """DAM was 13.6h / Dex $1M when Hunt deleted the card at 12h."""
    from launchfinder.scoring.hunt import HUNT_HOURS_RH, hunt_leftover_window
    from launchfinder.scoring.paper_gate import PAPER_WINDOW_RH

    assert HUNT_HOURS_RH == 24.0
    assert PAPER_WINDOW_RH == 12.0
    assert hunt_leftover_window("robinhood", 13.6) is True
    init_db()
    now = utcnow()
    with session_scope() as session:
        dam = Token(
            mint="0x7c651ee2f3c1c456ac3afc483b834c823649a0e2",
            symbol="DAM",
            chain="robinhood",
            first_seen_at=now - timedelta(hours=13, minutes=36),
            migrated_at=now - timedelta(hours=13, minutes=36),
            source="rh_bitquery",
        )
        dam.research = Research(features_json="{}", p_good=0.01, holder_count=80, scorer="first_sight")
        dam.outcome = Outcome(t0_mcap=40_287, last_mcap=40_287, last_liq=17_299, multiple=1.0)
        old = Token(
            mint="0x39dbed3a2bd333467115de45665cc57f813c4571",
            symbol="PONS",
            chain="robinhood",
            first_seen_at=now - timedelta(hours=25),
            migrated_at=now - timedelta(hours=25),
            source="rh_bitquery",
        )
        old.research = Research(features_json="{}", p_good=0.22, holder_count=80, scorer="legacy")
        old.outcome = Outcome(t0_mcap=410_000_000, last_mcap=410_000_000, last_liq=5_000_000, multiple=1.0)
        session.add_all([dam, old])
        session.flush()
        assert hunt_eligible(dam, dam.outcome, dam.research, now=now) is True
        assert hunt_eligible(old, old.outcome, old.research, now=now) is False
        assert upsert_hunt(session, dam, now=now) is not None
        assert upsert_hunt(session, old, now=now) is None


def test_hunt_live_conviction_does_not_copy_entry_on_a_dump():
    init_db()
    now = utcnow()
    with session_scope() as session:
        token = Token(
            mint="HuntRug111111111111111111111111111111111",
            symbol="ZTHX",
            chain="sol",
            first_seen_at=now - timedelta(minutes=25),
            migrated_at=now - timedelta(minutes=25),
            source="poll",
        )
        token.research = Research(
            features_json="{}",
            risk_flags_json='["Creator was funded by a wallet behind prior rugs","First-hour tape is dumping on real volume"]',
            p_good=0.94,
            heuristic_p=0.92,
            holder_count=1021,
            top10_pct=19.55,
        )
        token.outcome = Outcome(
            t0_mcap=316_000,
            last_mcap=240_000,
            max_mcap=316_000,
            last_liq=46_000,
            multiple=0.76,
        )
        session.add(token)
        session.flush()
        row = upsert_hunt(session, token, now=now)
        assert row is not None
        assert row.entry_p == 0.94
        assert row.conviction_p < 0.40
        assert row.conviction_p != row.entry_p
        assert pick_live_conviction(0.86, 0.95, last_mcap=451_000, t0_mcap=43_000) == 0.95
        assert pick_live_conviction(0.05, 0.95, last_mcap=240_000, t0_mcap=316_000) == 0.05
        assert pick_live_conviction(
            0.28, 0.76, last_mcap=92_848, t0_mcap=63_032, max_mcap=577_557
        ) == 0.28
        nina = hunt_board_rank({"last_mcap": 531_000, "t0_mcap": 43_000, "conviction_p": 0.95, "first_seen_at": "b"})
        recap = hunt_board_rank({"last_mcap": 92_848, "t0_mcap": 63_032, "multiple": 9.16, "conviction_p": 0.28, "first_seen_at": "a"})
        assert nina > recap
        assert conviction_from_tape(
            chain="sol",
            entry_p=0.94,
            multiple=0.76,
            last_mcap=240_000,
            t0_mcap=316_000,
            max_mcap=316_000,
            last_liq=46_000,
            holders=1021,
            top10_pct=19.55,
            flags=[
                "Creator was funded by a wallet behind prior rugs",
                "First-hour tape is dumping on real volume",
            ],
        ) < 0.40


def test_hunt_live_sol_sitter_scores_rh_thin_watch_capped():
    # Live inversion: Sol Hunt mean ~0.13 / 60 of 80 at 0 because Bloom
    # zeros last < 0.85× t0. RH Hunt mean ~0.80 / 44 at 80+ because
    # leftover 5–10× thin-watch books stack to 0.95. Hunt Live is not
    # Bloom. FEATURE_NAMES stays 66. Do not cap NINA / sitting RH Entry.
    from launchfinder.scoring.bloom import promise_score

    assert HUNT_THIN_WATCH_LIVE_CAP == 0.62
    assert hunt_thin_watch_book(0.04, ["Watch preview was already thin"]) is True
    assert hunt_thin_watch_book(0.68, ["X account created very recently"]) is False
    assert hunt_thin_watch_book(0.04, ["Watch preview was already thin"], last_liq=189_822, holders=660) is False
    assert hunt_thin_watch_book(0.68, last_liq=2_000, holders=400) is True
    pain = conviction_from_tape(
        chain="sol",
        entry_p=0.42,
        multiple=0.72,
        last_mcap=96_558,
        t0_mcap=133_287,
        max_mcap=133_287,
        last_liq=24_085,
        holders=12,
        top10_pct=40,
        flags=["X account created very recently", "Supply looks concentrated in top wallets"],
    )
    gs = conviction_from_tape(
        chain="sol",
        entry_p=0.60,
        multiple=0.70,
        last_mcap=25_465,
        t0_mcap=36_132,
        max_mcap=36_132,
        last_liq=12_000,
        holders=80,
        top10_pct=30,
        flags=["Heavy sniper presence at launch (20+)"],
    )
    dust = conviction_from_tape(
        chain="sol",
        entry_p=0.06,
        multiple=0.03,
        last_mcap=2_242,
        t0_mcap=69_000,
        max_mcap=69_000,
        last_liq=2_251,
        holders=0,
        top10_pct=80,
        flags=["Same ticker launched repeatedly in 24h (copycat spam)"],
    )
    dyna = conviction_from_tape(
        chain="robinhood",
        entry_p=0.04,
        multiple=9.46,
        last_mcap=476_791,
        t0_mcap=50_381,
        max_mcap=476_791,
        last_liq=189_822,
        holders=660,
        top10_pct=25,
        flags=["Watch preview was already thin"],
        vol_h1=80_000,
    )
    sat = conviction_from_tape(
        chain="sol",
        entry_p=0.68,
        multiple=2.24,
        last_mcap=98_186,
        t0_mcap=43_843,
        max_mcap=98_186,
        last_liq=24_904,
        holders=397,
        top10_pct=30,
        flags=["X account created very recently"],
        vol_h1=40_000,
    )
    nina = conviction_from_tape(
        chain="sol",
        entry_p=0.92,
        multiple=26.27,
        last_mcap=483_198,
        t0_mcap=43_082,
        max_mcap=1_131_649,
        last_liq=69_092,
        holders=630,
        top10_pct=16.55,
        flags=["Heavy sniper presence at launch (20+)"],
        vol_h1=40_000,
    )
    sitting = conviction_from_tape(
        chain="robinhood",
        entry_p=0.16,
        multiple=1.0,
        last_mcap=25_164,
        t0_mcap=25_164,
        max_mcap=25_164,
        last_liq=25_126,
        holders=107,
        top10_pct=40,
        flags=["Watch preview was already thin"],
    )
    bloom_pain, _ = promise_score(
        chain="sol",
        entry_p=0.42,
        multiple=0.72,
        last_mcap=96_558,
        t0_mcap=133_287,
        max_mcap=133_287,
        last_liq=24_085,
        holders=12,
        top10_pct=40,
        vol_h1=0,
        runner_p=None,
        second_leg=False,
        label=None,
        flags=["X account created very recently"],
    )
    bloom_dyna, _ = promise_score(
        chain="robinhood",
        entry_p=0.04,
        multiple=9.46,
        last_mcap=476_791,
        t0_mcap=50_381,
        max_mcap=476_791,
        last_liq=189_822,
        holders=660,
        top10_pct=25,
        vol_h1=80_000,
        runner_p=None,
        second_leg=False,
        label=None,
        flags=["Watch preview was already thin"],
    )
    assert 0.20 <= pain <= 0.60
    assert 0.20 <= gs <= 0.60
    assert dust == 0.0
    assert dyna > HUNT_THIN_WATCH_LIVE_CAP
    assert sat >= 0.70
    assert nina >= 0.70
    assert sitting < HUNT_THIN_WATCH_LIVE_CAP
    assert bloom_pain == 0.0
    assert bloom_dyna >= 0.90
    assert (
        pick_live_conviction(
            dyna,
            0.95,
            last_mcap=476_791,
            t0_mcap=50_381,
            max_mcap=476_791,
            allow_bloom=False,
        )
        == dyna
    )
    assert (
        pick_live_conviction(
            dyna,
            0.95,
            last_mcap=476_791,
            t0_mcap=50_381,
            max_mcap=476_791,
            allow_bloom=False,
            cap=HUNT_THIN_WATCH_LIVE_CAP,
        )
        == HUNT_THIN_WATCH_LIVE_CAP
    )


def test_high_score_review_picks_unlabeled_90_not_labeled_nina():
    # Hunt-tick chairs order by ATH multiple, so labeled NINA 26× can
    # eat every slot and refresh_outcomes drops them (label is set).
    # A fading unlabeled 90+ (AI MEME under t0) must still get a chair.
    from launchfinder.scoring.outcomes import _high_score_needs_second_look

    assert HIGH_SCORE_REVIEW_FLOOR == 0.90
    init_db()
    now = utcnow()
    stale = now - timedelta(minutes=12)
    with session_scope() as session:
        nina = Token(
            mint="HsNina11111111111111111111111111111111111",
            symbol="NINA",
            chain="sol",
            first_seen_at=now - timedelta(hours=11),
            migrated_at=now - timedelta(hours=11),
            source="poll",
        )
        nina.research = Research(features_json="{}", p_good=0.92, holder_count=630)
        nina.outcome = Outcome(
            t0_mcap=43_082,
            last_mcap=192_068,
            max_mcap=1_131_649,
            last_liq=43_828,
            multiple=26.27,
            label=1,
        )
        fading = Token(
            mint="HsFade11111111111111111111111111111111111",
            symbol="AIMEME",
            chain="sol",
            first_seen_at=now - timedelta(hours=2),
            migrated_at=now - timedelta(hours=2),
            source="poll",
        )
        fading.research = Research(features_json="{}", p_good=0.92, holder_count=145)
        fading.outcome = Outcome(
            t0_mcap=79_492,
            last_mcap=61_995,
            max_mcap=121_119,
            last_liq=19_247,
            multiple=1.52,
        )
        mid = Token(
            mint="HsMid111111111111111111111111111111111111",
            symbol="MID",
            chain="sol",
            first_seen_at=now - timedelta(hours=1),
            migrated_at=now - timedelta(hours=1),
            source="poll",
        )
        mid.research = Research(features_json="{}", p_good=0.70, holder_count=80)
        mid.outcome = Outcome(t0_mcap=40_000, last_mcap=38_000, last_liq=12_000, multiple=0.95)
        session.add_all([nina, fading, mid])
        session.flush()
        assert upsert_hunt(session, nina, now=stale) is not None
        assert upsert_hunt(session, fading, now=stale) is not None
        assert upsert_hunt(session, mid, now=stale) is not None
        session.flush()
        picked = high_score_mints_needing_review(session, limit=8)
        assert fading.mint in picked
        assert nina.mint not in picked
        assert mid.mint not in picked
        ticks = hunt_mints_needing_tick(session, limit=8)
        assert nina.mint in ticks
        assert _high_score_needs_second_look(fading, fading.outcome, set(picked)) is True
        assert _high_score_needs_second_look(nina, nina.outcome, set(picked)) is False
        assert _high_score_needs_second_look(mid, mid.outcome, set()) is False


def test_hunt_tape_climber_writes_last_live_keeps_entry_and_full_chairs():
    """Weak-entry 1.05× book that Dex says is 2.3× — Doing well can see it.

    Entry stays frozen. HuntCard.updated_at stays stale so the 8-minute
    full chairs still visit. FEATURE_NAMES 66. No extra GMGN. No auto-buy.
    """
    assert HUNT_LIVE_LAST_SECONDS == 60.0
    assert len(FEATURE_NAMES) == 66
    init_db()
    now = utcnow()
    stale = now - timedelta(minutes=12)
    with session_scope() as session:
        token = Token(
            mint="TapeClimb1111111111111111111111111111111",
            symbol="CLIMB",
            chain="sol",
            first_seen_at=now - timedelta(hours=2),
            migrated_at=now - timedelta(hours=2),
            source="poll",
        )
        token.research = Research(features_json="{}", p_good=0.40, holder_count=80, top10_pct=22.0)
        token.outcome = Outcome(
            t0_mcap=40_000,
            last_mcap=42_000,
            max_mcap=42_000,
            last_liq=12_000,
            multiple=1.05,
        )
        session.add(token)
        session.flush()
        row = upsert_hunt(session, token, now=stale)
        assert row is not None
        row.updated_at = stale
        session.flush()
        market = {
            "mcap_usd": 95_000,
            "liquidity_usd": 22_000,
            "volume_h1": 80_000,
            "price_usd": 0.001,
        }
        assert apply_hunt_tape_market(session, token, market, now=now) is True
        session.flush()
        session.refresh(token)
        session.refresh(token.outcome)
        session.refresh(token.research)
        hunt = session.query(HuntCard).filter(HuntCard.mint == token.mint).one()
        assert token.research.p_good == 0.40
        assert hunt.entry_p == 0.40
        assert float(token.outcome.last_mcap) == 95_000
        assert float(hunt.last_mcap) == 95_000
        assert hunt.conviction_p != hunt.entry_p
        assert hunt.updated_at == stale
        assert token.mint in hunt_mints_needing_tick(session, limit=8)
        assert token.mint in this_window_hunt_tape_mints(session)["sol"]
        card = {
            "chain": "sol",
            "last_mcap": token.outcome.last_mcap,
            "max_mcap": token.outcome.max_mcap,
            "t0_mcap": token.outcome.t0_mcap,
            "last_liq": token.outcome.last_liq,
            "multiple": token.outcome.multiple,
            "holder_count": 80,
            "top10_pct": 22.0,
            "risk_flags": [],
        }
        assert still_doing_well(card) is True
        nina = Token(
            mint="TapeNina11111111111111111111111111111111",
            symbol="NINA",
            chain="sol",
            first_seen_at=now - timedelta(hours=1),
            migrated_at=now - timedelta(hours=1),
            source="poll",
        )
        nina.research = Research(
            features_json="{}",
            p_good=0.92,
            holder_count=400,
            top10_pct=18.0,
            twitter_handle="ninathemonkey",
        )
        nina.outcome = Outcome(
            t0_mcap=43_082,
            last_mcap=431_029,
            max_mcap=1_131_649,
            last_liq=80_000,
            multiple=10.0,
        )
        session.add(nina)
        session.flush()
        nina_row = upsert_hunt(session, nina, now=stale)
        assert nina_row is not None
        nina_row.updated_at = stale
        assert apply_hunt_tape_market(
            session,
            nina,
            {
                "mcap_usd": 440_000,
                "liquidity_usd": 82_000,
                "volume_h1": 200_000,
                "price_usd": 0.01,
            },
            now=now,
        )
        session.flush()
        session.refresh(nina)
        assert nina.research.p_good == 0.92
        nina_hunt = session.query(HuntCard).filter(HuntCard.mint == nina.mint).one()
        assert nina_hunt.entry_p == 0.92
        from launchfinder.serialize import desk_entry_cap

        assert desk_entry_cap(0.92, {"twitter_handle": "ninathemonkey", "twitter_age_days": 626, "twitter_verified": True}) == 0.92


def test_hunt_tape_skips_rh_ghost_and_does_not_rewrite_entry():
    init_db()
    now = utcnow()
    with session_scope() as session:
        token = Token(
            mint="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            symbol="GHOST",
            chain="robinhood",
            first_seen_at=now - timedelta(hours=3),
            migrated_at=now - timedelta(hours=3),
            source="poll",
        )
        token.research = Research(features_json="{}", p_good=0.86, holder_count=40)
        token.outcome = Outcome(
            t0_mcap=26_746,
            last_mcap=64_006,
            max_mcap=64_006,
            last_liq=25_000,
            multiple=2.39,
        )
        session.add(token)
        session.flush()
        upsert_hunt(session, token, now=now)
        assert apply_hunt_tape_market(
            session,
            token,
            {"mcap_usd": 957_691, "liquidity_usd": 200, "volume_h1": 10, "price_usd": 1.0},
            now=now,
        ) is False
        session.refresh(token)
        assert token.research.p_good == 0.86
        assert float(token.outcome.last_mcap) == 64_006


def test_looks_like_contract_and_fills_blank_rh_ticker():
    assert looks_like_contract("0xc0e66139fedc24dedad728346e3d4") is True
    assert looks_like_contract("PONSHARE") is False
    assert looks_like_contract("") is False
    assert looks_like_contract("7x5ZQFHuB1ybDJyM1111111111111111") is True
    assert looks_like_contract("HALHALH") is False
    assert looks_like_contract("ATOM") is False
    init_db()
    now = utcnow()
    with session_scope() as session:
        token = Token(
            mint="0xc0e66139fedc24dedad728346e3d4aaaaaaaaa",
            symbol="0xc0e66139fedc24dedad728346e3d4",
            name="",
            chain="robinhood",
            first_seen_at=now - timedelta(hours=1),
            migrated_at=now - timedelta(hours=1),
            source="rh_bitquery",
        )
        token.research = Research(features_json="{}", p_good=0.12, holder_count=6)
        token.outcome = Outcome(t0_mcap=20_000, last_mcap=22_000, max_mcap=22_000, last_liq=8_000, multiple=1.1)
        session.add(token)
        session.flush()
        assert apply_hunt_tape_identity(
            token,
            {"name": "Uniswap Share", "symbol": "UNISHARE", "image_url": "https://img.example/u.png"},
        )
        assert token.symbol == "UNISHARE"
        assert token.name == "Uniswap Share"
        assert token.image_url.endswith("u.png")
        named = Token(
            mint="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            symbol="PONSHARE",
            name="Ponshare",
            chain="robinhood",
            first_seen_at=now,
            migrated_at=now,
            source="poll",
        )
        assert apply_hunt_tape_identity(named, {"name": "Other", "symbol": "OTHER"}) is False
        assert named.symbol == "PONSHARE"
        assert apply_hunt_tape_market(
            session,
            token,
            {
                "mcap_usd": 24_000,
                "liquidity_usd": 9_000,
                "volume_h1": 4_000,
                "name": "Uniswap Share",
                "symbol": "UNISHARE",
            },
            now=now,
        )
        session.refresh(token)
        assert token.research.p_good == 0.12
        assert token.symbol == "UNISHARE"


def test_tape_book_is_dead_skips_empty_pool_not_quiet_volume():
    from launchfinder.scoring.hunt_tape import tape_book_is_dead

    assert tape_book_is_dead(200, 10) is True
    assert tape_book_is_dead(0, 0) is True
    assert tape_book_is_dead(80_000, 50) is False
    assert tape_book_is_dead(80_000, 0) is False


def test_hunt_tape_writes_a_quiet_fat_rh_book():
    """vol_h1 < $100 is leftover-LP t0, not a Hunt tape skip."""
    init_db()
    now = utcnow()
    with session_scope() as session:
        token = Token(
            mint="0xquietfatrh000000000000000000000000000001",
            symbol="QUIET",
            chain="robinhood",
            first_seen_at=now - timedelta(hours=2),
            migrated_at=now - timedelta(hours=2),
            source="poll",
        )
        token.research = Research(features_json="{}", p_good=0.12, holder_count=150)
        token.outcome = Outcome(t0_mcap=40_000, last_mcap=42_000, max_mcap=42_000, last_liq=80_000, multiple=1.05)
        session.add(token)
        session.flush()
        upsert_hunt(session, token, now=now)
        assert apply_hunt_tape_market(
            session,
            token,
            {"mcap_usd": 48_000, "liquidity_usd": 82_000, "volume_h1": 50, "price_usd": 0.001},
            now=now,
        ) is True
        session.refresh(token)
        assert float(token.outcome.last_mcap) == 48_000
        assert token.research.p_good == 0.12


def test_this_window_hunt_tape_pins_the_board_before_climbers():
    """An 8×+ runner used to lose the 240 lottery to newest 1.2× climbers."""
    init_db()
    now = utcnow()
    with session_scope() as session:
        runner = Token(
            mint="TapePinRunner111111111111111111111111111",
            symbol="PIN",
            chain="sol",
            first_seen_at=now - timedelta(hours=10),
            migrated_at=now - timedelta(hours=10),
            created_at_chain=now - timedelta(hours=10),
            source="poll",
        )
        runner.research = Research(features_json="{}", p_good=0.40, holder_count=200)
        runner.outcome = Outcome(t0_mcap=40_000, last_mcap=400_000, max_mcap=400_000, last_liq=40_000, multiple=10.0)
        session.add(runner)
        session.flush()
        upsert_hunt(session, runner, now=now)
        for i in range(90):
            tok = Token(
                mint=f"TapePinClimb{i:03d}11111111111111111111111",
                symbol=f"C{i}",
                chain="sol",
                first_seen_at=now - timedelta(minutes=i + 1),
                migrated_at=now - timedelta(minutes=i + 1),
                created_at_chain=now - timedelta(minutes=i + 1),
                source="poll",
            )
            tok.research = Research(features_json="{}", p_good=0.10, holder_count=40)
            tok.outcome = Outcome(t0_mcap=40_000, last_mcap=60_000, max_mcap=60_000, last_liq=20_000, multiple=1.5)
            session.add(tok)
            session.flush()
            upsert_hunt(session, tok, now=now)
        tape = this_window_hunt_tape_mints(session)["sol"]
        board = list_hunt_mints(session, "sol", limit=80)
        assert runner.mint in board
        assert tape[0 : len(board)] == board
        assert runner.mint in tape[:80]


def test_this_window_hunt_tape_keeps_unconfirmed_real_book():
    """DAM: $17k liq / last==t0 lost the 240 lottery to newer 1.0× dust."""
    from launchfinder.scoring.hunt import HUNT_LIVE_LAST_PER_CHAIN, HUNT_THIN_LIQ

    init_db()
    now = utcnow()
    with session_scope() as session:
        dam = Token(
            mint="0x7c651ee2f3c1c456ac3afc483b834c823649a0e2",
            symbol="DAM",
            name="Modam",
            chain="robinhood",
            first_seen_at=now - timedelta(hours=10),
            migrated_at=now - timedelta(hours=10),
            created_at_chain=now - timedelta(hours=10),
            source="rh_bitquery",
        )
        dam.research = Research(features_json="{}", p_good=0.01, holder_count=0, scorer="first_sight")
        dam.outcome = Outcome(t0_mcap=40_287, last_mcap=40_287, max_mcap=40_287, last_liq=17_299, multiple=1.0)
        session.add(dam)
        session.flush()
        card = upsert_hunt(session, dam, now=now - timedelta(hours=10))
        assert card is not None
        card.updated_at = now - timedelta(hours=10)
        session.flush()
        for i in range(HUNT_LIVE_LAST_PER_CHAIN + 20):
            tok = Token(
                mint=f"0x{'%040d' % i}",
                symbol=f"N{i}",
                chain="robinhood",
                first_seen_at=now - timedelta(minutes=i + 1),
                migrated_at=now - timedelta(minutes=i + 1),
                created_at_chain=now - timedelta(minutes=i + 1),
                source="rh_bitquery",
            )
            tok.research = Research(features_json="{}", p_good=0.01, holder_count=2, scorer="first_sight")
            tok.outcome = Outcome(t0_mcap=40_000, last_mcap=40_000, max_mcap=40_000, last_liq=2_000, multiple=1.0)
            session.add(tok)
            session.flush()
            upsert_hunt(session, tok, now=now)
        assert float(dam.outcome.last_liq) >= HUNT_THIN_LIQ
        tape = this_window_hunt_tape_mints(session)["robinhood"]
        assert dam.mint in tape


def test_this_window_hunt_tape_keeps_empty_last_fat_book():
    """ORBIO: t0 snap mcap $0 / $648k liq is not last==t0, so v91 dropped it."""
    from launchfinder.scoring.hunt import HUNT_LIVE_LAST_PER_CHAIN, HUNT_THIN_LIQ, hunt_tape_unconfirmed_clause

    init_db()
    now = utcnow()
    with session_scope() as session:
        orbio = Token(
            mint="0xaa07a0e9209e16ac99708c3ec70159c6ef3128a3",
            symbol="ORBIO",
            name="Orbio.so",
            chain="robinhood",
            first_seen_at=now - timedelta(hours=14),
            migrated_at=now - timedelta(hours=14),
            created_at_chain=now - timedelta(hours=14),
            source="rh_dex",
        )
        orbio.research = Research(features_json="{}", p_good=0.0163, holder_count=80, scorer="first_sight")
        orbio.outcome = Outcome(t0_mcap=40_000, last_mcap=0, max_mcap=0, last_liq=648_383, multiple=0)
        session.add(orbio)
        session.flush()
        card = upsert_hunt(session, orbio, now=now - timedelta(hours=14))
        assert card is not None
        card.last_mcap = 0
        card.last_liq = 648_383
        card.updated_at = now - timedelta(hours=14)
        session.flush()
        dust = Token(
            mint="0xorbiodust000000000000000000000000000001",
            symbol="DUST",
            chain="robinhood",
            first_seen_at=now - timedelta(hours=2),
            migrated_at=now - timedelta(hours=2),
            source="rh_dex",
        )
        dust.research = Research(features_json="{}", p_good=0.01, holder_count=2, scorer="first_sight")
        dust.outcome = Outcome(t0_mcap=40_000, last_mcap=0, max_mcap=0, last_liq=2_000, multiple=0)
        session.add(dust)
        session.flush()
        upsert_hunt(session, dust, now=now)
        for i in range(HUNT_LIVE_LAST_PER_CHAIN + 20):
            tok = Token(
                mint=f"0x{'%040d' % (i + 500)}",
                symbol=f"N{i}",
                chain="robinhood",
                first_seen_at=now - timedelta(minutes=i + 1),
                migrated_at=now - timedelta(minutes=i + 1),
                source="rh_bitquery",
            )
            tok.research = Research(features_json="{}", p_good=0.01, holder_count=2, scorer="first_sight")
            tok.outcome = Outcome(t0_mcap=40_000, last_mcap=40_000, max_mcap=40_000, last_liq=2_000, multiple=1.0)
            session.add(tok)
            session.flush()
            upsert_hunt(session, tok, now=now)
        assert float(orbio.outcome.last_liq) >= HUNT_THIN_LIQ
        tape = this_window_hunt_tape_mints(session)["robinhood"]
        assert orbio.mint in tape
        dust_card = session.query(HuntCard).filter(HuntCard.mint == dust.mint).one()
        assert float(dust_card.last_liq) < HUNT_THIN_LIQ
        assert hunt_tape_unconfirmed_clause.__doc__ and "ORBIO" in hunt_tape_unconfirmed_clause.__doc__


def test_pick_live_conviction_uses_promoted_model_except_on_a_dump():
    """Hunt Live is the trained model on a live book; tape still wins a dump."""
    assert pick_live_conviction(0.86, 0.40, last_mcap=200_000, t0_mcap=80_000, max_mcap=200_000, model_p=0.91) == 0.91
    assert pick_live_conviction(0.05, 0.40, last_mcap=2_000, t0_mcap=80_000, max_mcap=80_000, model_p=0.91) == 0.05
    assert pick_live_conviction(0.28, 0.76, last_mcap=92_848, t0_mcap=63_032, max_mcap=577_557, model_p=0.91) == 0.28
    # RH Live artifact clips 0.01 on every card. Floor is no signal; tape stands.
    assert LIVE_MODEL_SIGNAL == 0.05
    assert pick_live_conviction(0.89, 0.40, last_mcap=188_854, t0_mcap=15_241, max_mcap=284_193, model_p=0.01) == 0.89
    assert pick_live_conviction(0.92, 0.40, last_mcap=823_413, t0_mcap=152_446, max_mcap=1_313_440, model_p=0.5143) == 0.5143
    # No Dex last is missing, not a dump the model must not restore — desk draws —.
    assert pick_live_conviction(0.0, 0.40, last_mcap=0, t0_mcap=69_000, max_mcap=69_000, model_p=0.91) == 0.0
    assert hunt_live_value(0, 0.40) is None
    assert hunt_live_value(69_000, 0.0) == 0.0
    assert hunt_live_value(255_265, 0.5263) == 0.5263
