"""Hard copycat gate → paper_v1_shadow learn labels (never opens short list)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from launchfinder.db import init_db, session_scope
from launchfinder.ledger import paper_v1_review, reconsider_copycat_veto_shadow
from launchfinder.models import Decision, Outcome, PaperFill, Research, Token
from launchfinder.scoring.paper_gate import PAPER_SHADOW_VETOES
from launchfinder.scoring.paper_v1 import (
    PAPER_V1_COPYCAT_FOMO_STAMP,
    PAPER_V1_COPYCAT_VETO,
    PAPER_V1_EXIT_REASON_MAX,
    PAPER_V1_LINE,
    PAPER_V1_SHADOW_LINE,
    is_copycat_skip,
    v1_skip_family,
    v1_veto_skip_stamp,
)
from launchfinder.scoring.veto_retro import veto_retro

SI = "DEW9dSN6QpWyNthphCpMmAbZP1Q4cEKR9xQXAri98WDP"


def test_copycat_spam_not_in_shadow_vetoes():
    assert PAPER_V1_COPYCAT_VETO == "copycat spam"
    assert "copycat spam" not in PAPER_SHADOW_VETOES


def test_v1_veto_skip_stamp_fits_exit_reason_column():
    day = "2026-09-30"
    stamp = v1_veto_skip_stamp(PAPER_V1_COPYCAT_VETO, day)
    assert stamp == "v1 veto|copycat|d:2026-09-30"
    assert len(stamp) <= PAPER_V1_EXIT_REASON_MAX
    fomo = v1_veto_skip_stamp(PAPER_V1_COPYCAT_FOMO_STAMP, day)
    assert fomo == "v1 veto|cc-fomo|d:2026-09-30"
    assert len(fomo) <= PAPER_V1_EXIT_REASON_MAX
    assert v1_skip_family(stamp) == "copycat"
    assert v1_skip_family(fomo) == "copycat"
    assert is_copycat_skip(fomo) is True
    assert v1_skip_family("v1 live-cold|d:2026-09-30") == "signal"


def test_fomo_only_copycat_risk_flags_writes_shadow_without_gate():
    init_db()
    now = datetime.now(timezone.utc)
    mint = "FomoOnlyCopycat1111111111111111111111111111"
    with session_scope() as session:
        token = Token(
            mint=mint,
            symbol="FOMOSI",
            chain="sol",
            source="fomo",
            first_seen_at=now - timedelta(hours=4),
            migrated_at=now - timedelta(hours=4),
        )
        token.research = Research(
            p_good=0.38,
            features_json="{}",
            risk_flags_json='["Same ticker launched repeatedly in 24h (copycat spam)"]',
        )
        token.outcome = Outcome(
            t0_mcap=55_000.0,
            last_mcap=1_500_000.0,
            max_mcap=8_000_000.0,
            last_liq=95_000.0,
            multiple=120.0,
        )
        session.add(token)
        session.flush()
        out = reconsider_copycat_veto_shadow(session, "sol", now=now)
        assert out["fomo_shadow"] == 1
        assert out["shadow"] == 1
        shadow = (
            session.query(PaperFill)
            .filter(PaperFill.mint == mint, PaperFill.line == PAPER_V1_SHADOW_LINE)
            .one()
        )
        assert shadow.status == "skipped"
        assert shadow.decision_id is None
        assert shadow.exit_reason.startswith("v1 veto|cc-fomo|d:")
        assert len(shadow.exit_reason) <= PAPER_V1_EXIT_REASON_MAX
        assert v1_skip_family(shadow.exit_reason) == "copycat"
        assert (
            session.query(PaperFill)
            .filter(PaperFill.mint == mint, PaperFill.line == PAPER_V1_LINE)
            .count()
            == 0
        )


def test_si_shaped_copycat_gate_writes_v1_shadow_skipped():
    init_db()
    # Pin midday UTC so gate.at (now-2h) stays on the same book day.
    now = datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)
    with session_scope() as session:
        token = Token(
            mint=SI,
            symbol="SI",
            chain="sol",
            source="websocket",
            first_seen_at=now - timedelta(hours=6),
            migrated_at=now - timedelta(hours=6),
        )
        token.research = Research(
            p_good=0.42,
            features_json="{}",
            risk_flags_json='["Same ticker launched repeatedly in 24h (copycat spam)"]',
        )
        token.outcome = Outcome(
            t0_mcap=69_000.0,
            last_mcap=2_000_000.0,
            max_mcap=9_900_000.0,
            last_liq=120_000.0,
            multiple=143.0,
        )
        session.add(token)
        session.flush()
        gate = Decision(
            token_id=token.id,
            chain="sol",
            mint=SI,
            kind="gate",
            entry_p=0.14,
            entry_mcap=69_000.0,
            liq=25_000.0,
            veto=PAPER_V1_COPYCAT_VETO,
            at=now - timedelta(hours=2),
            source="live",
        )
        session.add(gate)
        session.flush()
        out = reconsider_copycat_veto_shadow(session, "sol", now=now)
        assert out["shadow"] == 1
        shadow = (
            session.query(PaperFill)
            .filter(PaperFill.mint == SI, PaperFill.line == PAPER_V1_SHADOW_LINE)
            .one()
        )
        assert shadow.status == "skipped"
        assert shadow.exit_reason.startswith("v1 veto|copycat|d:")
        assert len(shadow.exit_reason) <= PAPER_V1_EXIT_REASON_MAX
        assert (
            session.query(PaperFill)
            .filter(PaperFill.mint == SI, PaperFill.line == PAPER_V1_LINE)
            .count()
            == 0
        )
        day = now.date().isoformat()
        review = paper_v1_review(session, "sol", day=day, now=now)
        hits = [r for r in review["shadow"] if r["mint"] == SI]
        assert hits and hits[0]["skip_reason"].startswith("v1 veto|copycat")
        assert hits[0]["skip_family"] == "copycat"


def test_veto_retro_keeps_copycat_spam_hard():
    init_db()
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        for i in range(12):
            t = Token(chain="sol", mint=f"copycat{i:04d}", symbol=f"C{i}", source="live")
            session.add(t)
            session.flush()
            session.add(
                Outcome(
                    token_id=t.id,
                    t0_mcap=50_000,
                    max_mcap=400_000,
                    last_mcap=1_000,
                    last_liq=900,
                    label=0,
                )
            )
            session.add(
                Decision(
                    token_id=t.id,
                    chain="sol",
                    mint=t.mint,
                    kind="gate",
                    entry_p=0.2,
                    entry_mcap=50_000,
                    liq=20_000,
                    veto=PAPER_V1_COPYCAT_VETO,
                    at=now,
                    source="live",
                )
            )
    with session_scope() as session:
        out = veto_retro(session, "sol")
        needle = next(n for n in out["needles"] if n["veto"] == PAPER_V1_COPYCAT_VETO)
        assert needle["class"] == "hard"
        assert needle["keep"] is True
        assert PAPER_V1_COPYCAT_VETO in out["verdict"]["keep_hard"]
