"""Meme quality score — Learn / shadow only, never short-list opens."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from launchfinder.db import init_db, session_scope
from launchfinder.ledger import reconsider_meme_quality_shadow
from launchfinder.models import HuntCard, Outcome, PaperFill, Research, Token
from launchfinder.scoring.meme_quality import (
    is_meme_dominant_thesis,
    meme_quality_score,
    v1_meme_quality_stamp,
)
from launchfinder.scoring.paper_gate import PAPER_SHADOW_VETOES
from launchfinder.scoring.paper_v1 import (
    PAPER_V1_EXIT_REASON_MAX,
    PAPER_V1_LINE,
    PAPER_V1_SHADOW_LINE,
    THESIS_HARD_TAGS,
    v1_thesis_from_features,
)


def test_meme_not_in_hard_tags():
    assert "meme" not in THESIS_HARD_TAGS


def test_meme_dominant_without_hard_tag():
    thesis = v1_thesis_from_features({"name_quality": 0.75, "github_auth_n": 0.1})
    assert is_meme_dominant_thesis(thesis)
    assert not (thesis.get("hard_tags") or [])


def test_hard_veto_zeros_score():
    out = meme_quality_score(
        {"volume_n": 0.8, "liquidity_n": 0.7, "holder_n": 0.6},
        risk_flags=["Same ticker launched repeatedly in 24h (copycat spam)"],
    )
    assert out["eligible"] is False
    assert out["score"] == 0.0
    assert out["hard_veto"] == "copycat spam"


def test_copycat_spam_stays_out_of_shadow_vetoes():
    assert "copycat spam" not in PAPER_SHADOW_VETOES


def test_meme_quality_stamp_fits_column():
    stamp = v1_meme_quality_stamp("2026-09-30")
    assert stamp == "v1 meme-q|d:2026-09-30"
    assert len(stamp) <= PAPER_V1_EXIT_REASON_MAX


def test_top_decile_meme_shadow_no_paper_v1_open():
    init_db()
    now = datetime.now(timezone.utc)
    mint = "MemeQualTopDecile1111111111111111111111"
    with session_scope() as session:
        token = Token(
            mint=mint,
            symbol="MQ",
            chain="sol",
            source="live",
            first_seen_at=now - timedelta(hours=3),
            migrated_at=now - timedelta(hours=3),
        )
        token.research = Research(
            p_good=0.41,
            features_json=(
                '{"name_quality":0.88,"volume_n":0.85,"liquidity_n":0.80,'
                '"organic_book":0.75,"buy_pressure":0.70,"holder_n":0.70,'
                '"top10_inv":0.78,"gmgn_renounced":1.0,"creator_win_rate":0.55,'
                '"fresh_wallet_n":0.5}'
            ),
            risk_flags_json="[]",
        )
        token.outcome = Outcome(
            t0_mcap=120_000.0,
            last_mcap=400_000.0,
            max_mcap=900_000.0,
            last_liq=85_000.0,
            multiple=1.3,
        )
        session.add(token)
        session.flush()
        session.add(
            HuntCard(
                chain="sol",
                mint=mint,
                token_id=token.id,
                first_seen_at=now - timedelta(hours=3),
                entry_p=0.35,
                conviction_p=0.48,
                t0_mcap=120_000.0,
                last_mcap=400_000.0,
                last_liq=85_000.0,
                updated_at=now,
            )
        )
        session.flush()
        # Seed filler hunt cards so top-decile threshold is defined.
        for i in range(12):
            m = f"MemeFill{i:02d}111111111111111111111111111111"
            t = Token(mint=m, symbol=f"M{i}", chain="sol", source="live", first_seen_at=now)
            t.research = Research(
                p_good=0.3,
                features_json='{"name_quality":0.65,"volume_n":0.2,"liquidity_n":0.2}',
                risk_flags_json="[]",
            )
            t.outcome = Outcome(token_id=0, t0_mcap=80_000, last_mcap=90_000, max_mcap=100_000, last_liq=10_000)
            session.add(t)
            session.flush()
            t.outcome.token_id = t.id
            session.add(
                HuntCard(
                    chain="sol",
                    mint=m,
                    token_id=t.id,
                    updated_at=now - timedelta(minutes=i),
                )
            )
        session.flush()
        out = reconsider_meme_quality_shadow(session, "sol", now=now)
        assert out["shadow"] >= 1
        shadow = (
            session.query(PaperFill)
            .filter(PaperFill.mint == mint, PaperFill.line == PAPER_V1_SHADOW_LINE)
            .one_or_none()
        )
        assert shadow is not None
        assert shadow.status == "skipped"
        assert shadow.exit_reason.startswith("v1 meme-q|d:")
        assert (
            session.query(PaperFill)
            .filter(PaperFill.mint == mint, PaperFill.line == PAPER_V1_LINE)
            .count()
            == 0
        )
