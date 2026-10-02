"""High-SI FOMO Learn shadow — copycat-aware, no paper_v1 opens."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from launchfinder.db import init_db, session_scope
from launchfinder.ledger import paper_v1_review, reconsider_high_si_fomo_shadow, si_pr_fomo_probe
from launchfinder.models import Outcome, PaperFill, Research, Token
from launchfinder.scoring.high_si_learn import (
    ensure_fomo_desk_token,
    fomo_board_stub_unactionable,
    fomo_desk_si_pr_eligible,
    high_si_copycat_blocked,
    is_active_si_pr_shadow_reason,
    printer_label_multiple,
    repair_fomo_board_missing_outcomes,
    si_pr_label_opened_at,
    si_printer_band_ok,
    token_high_si_printer,
    v1_si_pr_stale_stamp,
    v1_si_printer_stamp,
)
from launchfinder.scoring.hunt import historical_hydrate_tape_mints, hunt_eligible, upsert_hunt
from launchfinder.scoring.paper_gate import paper_hard_veto
from launchfinder.scoring.paper_v1 import (
    PAPER_V1_EXIT_REASON_MAX,
    PAPER_V1_LINE,
    PAPER_V1_SHADOW_LINE,
    PAPER_V1_SKIP_LIVE_COLD,
    PAPER_V1_SKIPPED,
    v1_skip_stamp,
)
from launchfinder.scoring.high_si_learn import outcome_peak_multiple

ELON_MINT = "ElonClassMint11111111111111111111111111"
EACC_MINT = "EaccHijackMint1111111111111111111111111"


def test_si_printer_stamp_fits_column():
    s = v1_si_printer_stamp("2026-09-30")
    assert s.startswith("v1 si-pr|d:")
    assert len(s) <= PAPER_V1_EXIT_REASON_MAX


def test_copycat_blocks_high_si_path():
    tok = Token(mint="x", chain="sol", symbol="SI")
    tok.research = Research(
        risk_flags_json='["Same ticker launched repeatedly in 24h (copycat spam)"]',
        features_json="{}",
    )
    assert high_si_copycat_blocked(tok, tok.research) is True


def test_hijack_does_not_block_high_si_path():
    tok = Token(mint="y", chain="sol", symbol="EACC")
    tok.research = Research(
        risk_flags_json='["X account created very recently (hijack)"]',
        features_json="{}",
    )
    assert paper_hard_veto(["X account created very recently (hijack)"]) == "hijack"
    assert high_si_copycat_blocked(tok, tok.research) is False


def test_high_si_printer_allows_above_hunt_cap():
    tok = Token(mint="z", chain="sol", symbol="BIG")
    tok.outcome = Outcome(multiple=143.0)
    assert token_high_si_printer(tok, tok.outcome) is True


def test_si_printer_band_20x_floor_no_upper_cap():
    assert si_printer_band_ok(20.0) is True
    assert si_printer_band_ok(25.0) is True
    assert si_printer_band_ok(1000.0) is True
    assert si_printer_band_ok(143.0) is True
    assert si_printer_band_ok(15.0) is False
    assert si_printer_band_ok(19.9) is False


def test_ensure_fomo_desk_token_creates_zeroed_outcome():
    init_db()
    mint = "NewFomoStub1111111111111111111111111111"
    with session_scope() as session:
        token = ensure_fomo_desk_token(
            session,
            "sol",
            {"mint": mint, "symbol": "STUB", "multiple": 50.0},
        )
        assert token is not None
        assert token.source == "fomo_board"
        assert token.outcome is not None
        assert token.outcome.last_mcap == 0.0
        assert token.outcome.t0_mcap == 0.0


def test_repair_fomo_board_missing_outcome():
    init_db()
    mint = "LegacyFomoStub1111111111111111111111111"
    with session_scope() as session:
        token = Token(
            mint=mint,
            symbol="AQUA",
            chain="sol",
            source="fomo_board",
            is_historical=True,
            first_seen_at=datetime.now(timezone.utc),
        )
        token.research = Research(p_good=0.0, features_json="{}", risk_flags_json="[]")
        session.add(token)
        session.flush()
        assert token.outcome is None
        assert repair_fomo_board_missing_outcomes(session, "sol") == 1
        session.refresh(token)
        assert token.outcome is not None
        assert token.outcome.last_mcap == 0.0


def test_fomo_board_zero_t0_stub_not_hunt_eligible():
    init_db()
    now = datetime.now(timezone.utc)
    mint = "NoHuntFomoStub111111111111111111111111"
    with session_scope() as session:
        token = ensure_fomo_desk_token(
            session,
            "sol",
            {"mint": mint, "symbol": "AQUA", "multiple": 80.0, "status": "leftover"},
        )
        assert token is not None
        oc = token.outcome
        assert fomo_board_stub_unactionable(token, oc)
        assert hunt_eligible(token, oc, token.research, now=now) is False
        assert upsert_hunt(session, token, now=now) is None
        from launchfinder.models import HuntCard

        assert session.query(HuntCard).filter(HuntCard.mint == mint).count() == 0


def test_repaired_fomo_stub_in_historical_hydrate_set():
    init_db()
    mint = "HydrateFomoStub111111111111111111111111"
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        token = Token(
            mint=mint,
            symbol="AQUA",
            chain="sol",
            source="fomo_board",
            is_historical=True,
            first_seen_at=now,
        )
        token.research = Research(p_good=0.0, features_json="{}", risk_flags_json="[]")
        session.add(token)
        session.flush()
        assert repair_fomo_board_missing_outcomes(session, "sol") == 1
        mints = historical_hydrate_tape_mints(session, "sol", limit=12)
        assert mint in mints


def test_fomo_desk_si_pr_eligible_extreme_multiples():
    assert fomo_desk_si_pr_eligible({"status": "leftover", "multiple": 1000.0}) is True
    assert fomo_desk_si_pr_eligible({"status": "caught", "multiple": 20.0}) is True
    assert fomo_desk_si_pr_eligible({"status": "caught", "multiple": 25.0}) is True
    assert fomo_desk_si_pr_eligible({"status": "caught", "multiple": 15.0}) is False
    assert fomo_desk_si_pr_eligible({"multiple": 500.0}) is False


def test_fomo_desk_multiple_when_outcome_flat():
    init_db()
    now = datetime.now(timezone.utc)
    mint = "PaidPrinterMint111111111111111111111111"
    with session_scope() as session:
        token = Token(
            mint=mint,
            symbol="PAID",
            chain="sol",
            source="poll",
            first_seen_at=now - timedelta(days=20),
        )
        token.research = Research(p_good=0.4, features_json="{}", risk_flags_json="[]")
        token.outcome = Outcome(
            t0_mcap=137_000.0,
            last_mcap=137_000.0,
            max_mcap=137_000.0,
            multiple=1.0,
        )
        session.add(token)
        session.flush()
        from launchfinder.models import Decision

        session.add(
            Decision(
                token_id=token.id,
                kind="entry",
                entry_p=0.85,
                entry_mcap=137_000.0,
                liq=50_000.0,
                at=now - timedelta(days=5),
            )
        )
        session.flush()
        fomo_row = {"multiple": 79.8, "last_mcap": 10_600_000.0, "mcap_usd": 10_900_000.0}
        mult = printer_label_multiple(session, token, "sol", token.outcome, fomo_row=fomo_row)
        assert mult >= 75.0
        assert token_high_si_printer(token, token.outcome, label_multiple=mult)


def test_peak_multiple_labels_dumped_fomo_printer():
    oc = Outcome(multiple=12.0, t0_mcap=80_000.0, max_mcap=6_400_000.0)
    assert outcome_peak_multiple(oc) >= 80.0
    tok = Token(mint="p", chain="sol", symbol="EACC")
    tok.outcome = oc
    assert token_high_si_printer(tok, oc, label_multiple=80.0) is True


def test_hunt_only_peak_does_not_stamp_si_pr():
    init_db()
    now = datetime.now(timezone.utc)
    mint = "HuntOnlyMint1111111111111111111111111"
    with session_scope() as session:
        token = Token(
            mint=mint,
            symbol="ROY",
            chain="sol",
            source="poll",
            first_seen_at=now - timedelta(days=2),
        )
        token.research = Research(p_good=0.2, features_json="{}", risk_flags_json="[]")
        token.outcome = Outcome(
            t0_mcap=50_000.0,
            last_mcap=5_000_000.0,
            max_mcap=6_000_000.0,
            multiple=100.0,
        )
        session.add(token)
        session.flush()
        out = reconsider_high_si_fomo_shadow(session, "sol", now=now)
        assert out["shadow"] == 0
        assert (
            session.query(PaperFill)
            .filter(PaperFill.mint == mint, PaperFill.line == PAPER_V1_SHADOW_LINE)
            .count()
            == 0
        )


def test_high_si_fomo_shadow_writes_skipped_no_paper_v1():
    init_db()
    now = datetime.now(timezone.utc)
    mint = ELON_MINT
    with session_scope() as session:
        from launchfinder.ingest.fomo_poll import save_trending_snapshot

        save_trending_snapshot(
            session,
            [{"mint": mint, "chain": "sol", "symbol": "ELON", "mcap_usd": 4_000_000}],
            now,
        )
        token = Token(
            mint=mint,
            symbol="ELON",
            chain="sol",
            source="fomo",
            first_seen_at=now - timedelta(days=2),
        )
        token.research = Research(
            p_good=0.35,
            features_json='{"name_quality":0.75}',
            risk_flags_json="[]",
        )
        token.outcome = Outcome(
            t0_mcap=80_000.0,
            last_mcap=4_000_000.0,
            max_mcap=5_000_000.0,
            last_liq=200_000.0,
            multiple=55.0,
        )
        session.add(token)
        session.flush()
        assert token_high_si_printer(token, token.outcome)
        out = reconsider_high_si_fomo_shadow(session, "sol", now=now)
        assert out["shadow"] == 1
        shadow = (
            session.query(PaperFill)
            .filter(PaperFill.mint == mint, PaperFill.line == PAPER_V1_SHADOW_LINE)
            .one()
        )
        assert shadow.status == "skipped"
        assert shadow.exit_reason.startswith("v1 si-pr|d:")
        assert (
            session.query(PaperFill)
            .filter(PaperFill.mint == mint, PaperFill.line == PAPER_V1_LINE)
            .count()
            == 0
        )


def test_high_si_fomo_shadow_upgrades_existing_live_cold():
    """v166: prior shadow rows must upgrade to si-pr, not block on _v1_has_row."""
    init_db()
    now = datetime.now(timezone.utc)
    mint = EACC_MINT
    day = now.strftime("%Y-%m-%d")
    with session_scope() as session:
        from launchfinder.ingest.fomo_poll import save_trending_snapshot

        save_trending_snapshot(
            session,
            [{"mint": mint, "chain": "sol", "symbol": "EACC", "mcap_usd": 2_000_000}],
            now,
        )
        token = Token(
            mint=mint,
            symbol="EACC",
            chain="sol",
            source="fomo",
            first_seen_at=now - timedelta(days=1),
        )
        token.research = Research(
            p_good=0.30,
            features_json='{"name_quality":0.6}',
            risk_flags_json='["X account created very recently (hijack)"]',
        )
        token.outcome = Outcome(
            t0_mcap=80_000.0,
            last_mcap=1_500_000.0,
            max_mcap=6_400_000.0,
            last_liq=120_000.0,
            multiple=80.0,
        )
        session.add(token)
        session.flush()
        session.add(
            PaperFill(
                chain="sol",
                mint=mint,
                token_id=token.id,
                line=PAPER_V1_SHADOW_LINE,
                opened_at=now - timedelta(hours=2),
                entry_p=0.12,
                entry_mcap=80_000.0,
                entry_liq=50_000.0,
                status=PAPER_V1_SKIPPED,
                exit_reason=v1_skip_stamp(PAPER_V1_SKIP_LIVE_COLD, day),
            )
        )
        session.flush()
        out = reconsider_high_si_fomo_shadow(session, "sol", now=now)
        assert out["shadow"] == 1
        assert out.get("upgraded", 0) == 1
        shadow = (
            session.query(PaperFill)
            .filter(PaperFill.mint == mint, PaperFill.line == PAPER_V1_SHADOW_LINE)
            .one()
        )
        assert shadow.exit_reason.startswith("v1 si-pr|d:")


def test_si_pr_shadow_resurfaces_in_today_review():
    """Review matches ``v1 si-pr|d:<today>`` without rewriting opened_at to now."""
    init_db()
    now = datetime.now(timezone.utc)
    mint = "PaidReviewMint111111111111111111111111"
    day = now.strftime("%Y-%m-%d")
    old_opened = now - timedelta(days=10)
    with session_scope() as session:
        from launchfinder.ingest.fomo_poll import save_trending_snapshot

        save_trending_snapshot(
            session,
            [{"mint": mint, "chain": "sol", "symbol": "PAID", "mcap_usd": 9_000_000}],
            now,
        )
        token = Token(
            mint=mint,
            symbol="PAID",
            chain="sol",
            source="poll",
            first_seen_at=now - timedelta(days=20),
        )
        token.research = Research(p_good=0.4, features_json="{}", risk_flags_json="[]")
        token.outcome = Outcome(
            t0_mcap=120_000.0,
            last_mcap=9_000_000.0,
            max_mcap=9_500_000.0,
            multiple=79.0,
        )
        session.add(token)
        session.flush()
        session.add(
            PaperFill(
                chain="sol",
                mint=mint,
                token_id=token.id,
                line=PAPER_V1_SHADOW_LINE,
                opened_at=old_opened,
                entry_p=0.12,
                entry_mcap=120_000.0,
                entry_liq=40_000.0,
                status=PAPER_V1_SKIPPED,
                exit_reason=v1_skip_stamp(PAPER_V1_SKIP_LIVE_COLD, "2026-09-01"),
            )
        )
        session.flush()
        probe = si_pr_fomo_probe(session, "sol", mint, now=now)
        assert probe.get("stage") == "stamped"
        shadow = (
            session.query(PaperFill)
            .filter(PaperFill.mint == mint, PaperFill.line == PAPER_V1_SHADOW_LINE)
            .one()
        )
        assert shadow.opened_at.replace(tzinfo=timezone.utc) == old_opened.replace(tzinfo=timezone.utc)
        review = paper_v1_review(session, "sol", day=day, now=now)
        si = [
            r
            for r in review.get("shadow") or []
            if str(r.get("skip_reason", "")).startswith("v1 si-pr")
        ]
        assert any(r.get("symbol") == "PAID" for r in si)


def test_hunt_flood_si_pr_demoted_off_today_review():
    init_db()
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    mint = "HuntFloodMint111111111111111111111111"
    with session_scope() as session:
        token = Token(
            mint=mint,
            symbol="ROY",
            chain="sol",
            source="poll",
            first_seen_at=now - timedelta(days=3),
        )
        token.research = Research(p_good=0.2, features_json="{}", risk_flags_json="[]")
        token.outcome = Outcome(multiple=90.0, t0_mcap=50_000.0, max_mcap=5_000_000.0)
        session.add(token)
        session.flush()
        session.add(
            PaperFill(
                chain="sol",
                mint=mint,
                token_id=token.id,
                line=PAPER_V1_SHADOW_LINE,
                opened_at=now,
                entry_p=0.2,
                entry_mcap=50_000.0,
                entry_liq=30_000.0,
                status=PAPER_V1_SKIPPED,
                exit_reason=v1_si_printer_stamp(day),
            )
        )
        session.flush()
        out = reconsider_high_si_fomo_shadow(session, "sol", now=now)
        assert out.get("demoted_stale", 0) >= 1
        shadow = (
            session.query(PaperFill)
            .filter(PaperFill.mint == mint, PaperFill.line == PAPER_V1_SHADOW_LINE)
            .one()
        )
        assert shadow.exit_reason.startswith("v1 si-pr-stale|")
        assert not is_active_si_pr_shadow_reason(shadow.exit_reason)
        review = paper_v1_review(session, "sol", day=day, now=now)
        assert not any(r.get("symbol") == "ROY" for r in review.get("shadow") or [])


def test_si_pr_opened_at_from_frozen_entry_decision():
    init_db()
    now = datetime.now(timezone.utc)
    mint = "FrozenEntryMint11111111111111111111111"
    frozen_at = now - timedelta(days=4)
    with session_scope() as session:
        from launchfinder.models import Decision

        token = Token(mint=mint, symbol="FRZ", chain="sol", first_seen_at=now - timedelta(days=20))
        token.research = Research(p_good=0.3, features_json="{}", risk_flags_json="[]")
        token.outcome = Outcome(t0_mcap=90_000.0, last_mcap=5_000_000.0, max_mcap=6_000_000.0)
        session.add(token)
        session.flush()
        session.add(
            Decision(
                token_id=token.id,
                kind="entry",
                entry_p=0.82,
                entry_mcap=90_000.0,
                liq=40_000.0,
                at=frozen_at,
            )
        )
        session.flush()
        opened = si_pr_label_opened_at(session, token, "sol")
        assert opened.replace(tzinfo=timezone.utc) == frozen_at.replace(tzinfo=timezone.utc)


def test_fomo_desk_si_pr_survives_stale_sweep():
    init_db()
    now = datetime.now(timezone.utc)
    mint = "PaidKeepMint1111111111111111111111111"
    with session_scope() as session:
        from launchfinder.ingest.fomo_poll import save_trending_snapshot

        save_trending_snapshot(
            session,
            [{"mint": mint, "chain": "sol", "symbol": "PAID", "mcap_usd": 8_000_000}],
            now,
        )
        token = Token(
            mint=mint,
            symbol="PAID",
            chain="sol",
            source="poll",
            first_seen_at=now - timedelta(days=5),
        )
        token.research = Research(p_good=0.4, features_json="{}", risk_flags_json="[]")
        token.outcome = Outcome(
            t0_mcap=100_000.0,
            last_mcap=8_000_000.0,
            max_mcap=8_500_000.0,
            multiple=80.0,
        )
        session.add(token)
        session.flush()
        session.add(
            PaperFill(
                chain="sol",
                mint=mint,
                token_id=token.id,
                line=PAPER_V1_SHADOW_LINE,
                opened_at=now - timedelta(days=2),
                entry_p=0.12,
                entry_mcap=100_000.0,
                entry_liq=40_000.0,
                status=PAPER_V1_SKIPPED,
                exit_reason=v1_si_printer_stamp("2026-09-28"),
            )
        )
        session.flush()
        out = reconsider_high_si_fomo_shadow(session, "sol", now=now)
        assert out.get("demoted_stale", 0) == 0
        shadow = (
            session.query(PaperFill)
            .filter(PaperFill.mint == mint, PaperFill.line == PAPER_V1_SHADOW_LINE)
            .one()
        )
        assert is_active_si_pr_shadow_reason(shadow.exit_reason)
        probe = si_pr_fomo_probe(session, "sol", mint, now=now)
        assert probe.get("stage") == "stamped"

