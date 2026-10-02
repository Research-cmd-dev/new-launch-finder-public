"""Thesis weight refit nudges ranking from closed short-list outcomes."""

from datetime import datetime, timezone
import json

from launchfinder.db import init_db, session_scope
from launchfinder.ledger import DECISION_ENTRY
from launchfinder.models import Decision, PaperFill, Token, utcnow
from launchfinder.scoring.paper_v1 import choose_v1, v1_thesis_from_features
from launchfinder.scoring.thesis_weights import (
    DEFAULT_WEIGHTS,
    load_rank_policy,
    load_thesis_weights,
    normalize_weights,
    refit_thesis_weights,
    save_rank_policy,
    save_thesis_weights,
)


def test_normalize_and_default_weights():
    w = normalize_weights({"github_auth_n": 0.7, "real_project": 0.1, "gmgn_cto": 0.1, "name_quality": 0.1})
    assert abs(sum(w.values()) - 1.0) < 1e-6
    assert w["github_auth_n"] > DEFAULT_WEIGHTS["github_auth_n"]


def test_thesis_score_uses_custom_weights():
    feat = {"github_auth_n": 1.0, "real_project": 0.0, "gmgn_cto": 0.0, "name_quality": 0.0}
    default = v1_thesis_from_features(feat)
    heavy = v1_thesis_from_features(feat, weights={"github_auth_n": 0.7, "real_project": 0.1, "gmgn_cto": 0.1, "name_quality": 0.1})
    assert heavy["score"] > default["score"]


def test_choose_v1_live_policy_prefers_live_margin():
    early = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)
    rows = [
        {"id": "thesis", "chain": "sol", "entry_p": 0.15, "live_p": 0.55, "opened_at": early, "thesis_score": 0.85},
        {"id": "hot", "chain": "sol", "entry_p": 0.20, "live_p": 0.90, "opened_at": early, "thesis_score": 0.10},
    ]
    assert choose_v1(rows, already=0, cap=1, policy="thesis") == ["thesis"]
    assert choose_v1(rows, already=0, cap=1, policy="live") == ["hot"]


def test_refit_noop_until_enough_closes():
    init_db()
    with session_scope() as session:
        out = refit_thesis_weights(session, min_closed=12)
        assert out["fitted"] is False
        assert load_thesis_weights(session)["github_auth_n"] == DEFAULT_WEIGHTS["github_auth_n"]


def test_refit_nudges_weights_from_closed_hits():
    init_db()
    with session_scope() as session:
        now = utcnow()
        for i in range(14):
            tok = Token(
                mint=f"WRefitMint{i:02d}111111111111111111111111",
                symbol=f"WR{i}",
                chain="sol",
                first_seen_at=now,
                migrated_at=now,
                created_at_chain=now,
                source="poll",
            )
            session.add(tok)
            session.flush()
            # Even ids: strong github + hit2x; odd: meme-only + miss.
            strong = i % 2 == 0
            feats = {
                "github_auth_n": 0.9 if strong else 0.0,
                "real_project": 1.0 if strong else 0.0,
                "gmgn_cto": 0.0,
                "name_quality": 0.7,
            }
            session.add(
                Decision(
                    token_id=tok.id,
                    mint=tok.mint,
                    kind=DECISION_ENTRY,
                    chain="sol",
                    at=now,
                    source="live",
                    entry_p=0.2,
                    entry_mcap=70_000,
                    liq=25_000,
                    features_json=json.dumps(feats),
                    image_rev="test",
                )
            )
            session.flush()
            dec = session.query(Decision).filter(Decision.token_id == tok.id).one()
            session.add(
                PaperFill(
                    chain="sol",
                    mint=tok.mint,
                    token_id=tok.id,
                    decision_id=dec.id,
                    line="paper_v1",
                    opened_at=now,
                    closed_at=now,
                    entry_p=0.2,
                    entry_mcap=70_000,
                    entry_liq=25_000,
                    target=2.0,
                    ride=10.0,
                    max_mcap=200_000 if strong else 70_000,
                    min_mcap=70_000,
                    last_mcap=150_000 if strong else 60_000,
                    last_liq=25_000,
                    status="closed",
                    return_pct=100.0 if strong else -20.0,
                    exit_reason="test",
                    image_rev="test",
                    updated_at=now,
                )
            )
        session.flush()
        out = refit_thesis_weights(session, min_closed=12)
        assert out["fitted"] is True
        w = load_thesis_weights(session)
        assert w["github_auth_n"] > DEFAULT_WEIGHTS["github_auth_n"]
        assert save_rank_policy(session, "live") == "live"
        assert load_rank_policy(session) == "live"
        # Reset to signal; refit should score policies and may flip when margin clears.
        save_rank_policy(session, "signal")
        out2 = refit_thesis_weights(session, min_closed=12)
        assert out2["fitted"] is True
        assert "policy_scores" in out2
        assert set(out2["policy_scores"]).issuperset({"signal", "live", "thesis"})
        save_thesis_weights(session, DEFAULT_WEIGHTS)
