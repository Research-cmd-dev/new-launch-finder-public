"""Yesterday → today short-list delta for Learn ritual."""

from datetime import datetime, timedelta, timezone

from launchfinder.db import init_db, session_scope
from launchfinder.ledger import paper_v1_day_report
from launchfinder.models import Decision, Outcome, PaperFill, Research, Token
from launchfinder.scoring.day_delta import _day_stats, paper_v1_day_delta
from launchfinder.scoring.paper_v1 import THESIS_COVERAGE_DEF, v1_row_has_hard_tag, v1_thesis_coverage


def test_day_delta_shape():
    init_db()
    with session_scope() as session:
        out = paper_v1_day_delta(session, "sol")
        assert out["paper_only"] is True
        assert "today" in out and "yesterday" in out and "delta" in out
        assert isinstance(out["ritual"], list) and out["ritual"]
        assert out["rank_policy"] in {"signal", "live", "thesis"}
        assert out["today"]["thesis_coverage_def"] == THESIS_COVERAGE_DEF
        assert "skip_families" in out["today"]
        assert "skip_reasons_signal" in out["today"]
        assert "paper_misses" in out
        assert any("Miss autopsies" in line or "would-have" in line for line in out["ritual"])
        assert any("copycat drown" in line or "Signal skips" in line for line in out["ritual"])
        assert out["paper_misses"]["would_have"]["open"] is False
        assert "fomo_trend_no_hunt" in out["paper_misses"]
        assert out["paper_misses"]["fomo_trend_no_hunt"]["open"] is False
        assert any("FOMO trending no-Hunt" in line for line in out["ritual"])


def test_thesis_coverage_is_hard_tag_on_opens_everywhere():
    """One definition: meme-only opens count as 0, queued/skipped labels do not count at all."""
    assert v1_row_has_hard_tag({"tags": ["meme"]}) is False
    assert v1_row_has_hard_tag({"tags": ["meme", "github"]}) is True
    assert v1_row_has_hard_tag({"thesis": {"hard_tags": ["cto"]}}) is True
    assert v1_row_has_hard_tag({}) is False
    cov = v1_thesis_coverage([{"tags": ["meme"]}, {"tags": ["github"]}, {"tags": []}, {"tags": ["dev"]}])
    assert cov == {
        "coverage": 0.5,
        "hard_tag_n": 2,
        "any_tag_coverage": 0.75,
        "n": 4,
        "definition": THESIS_COVERAGE_DEF,
    }
    assert v1_thesis_coverage([])["coverage"] is None

    review = {
        "day": "2026-09-27",
        "picked": [
            {"id": 1, "tags": ["meme"], "thesis_score": 0.9},
            {"id": 2, "tags": ["github", "dev"], "thesis_score": 0.6},
            {"id": 3, "tags": ["cto"], "thesis_score": 0.3, "hit2x": True},
        ],
        # Review lists closes under picked too; the same row must not count twice.
        "closed": [{"id": 3, "tags": ["cto"], "thesis_score": 0.3, "hit2x": True}],
        # Queued / skipped / shadow labels are not opens: they used to inflate the number.
        "queued": [{"id": 4, "tags": ["github"], "thesis_score": 0.9}] * 5,
        "skipped": [{"id": 9, "tags": ["github"], "skip_reason": "v1 cap"}] * 20,
        "shadow": [],
    }
    stats = _day_stats(review)
    assert stats["thesis_n"] == 3 and stats["thesis_hard_tag_n"] == 2
    assert stats["thesis_coverage"] == round(2 / 3, 4)
    assert stats["thesis_any_tag_coverage"] == 1.0
    assert stats["queued_soft_path_ready"] == 5
    assert stats["hit2x_n"] == 1 and stats["closed_n"] == 1
    assert stats["skip_families"].get("other") == 20
    assert stats["skip_reasons_signal"] == {}


def _row(session, chain, i, *, day, status, tags_json, opened_at):
    tok = Token(
        mint=f"CovMint{chain[:2]}{i:03d}1111111111111111111111111111",
        symbol=f"C{i}",
        chain=chain,
        first_seen_at=opened_at - timedelta(minutes=10),
        migrated_at=opened_at - timedelta(minutes=10),
        created_at_chain=opened_at - timedelta(minutes=40),
        source="poll",
    )
    tok.research = Research(features_json="{}", p_good=0.2, risk_flags_json="[]", scorer="first_sight")
    tok.outcome = Outcome(t0_mcap=69_000, last_mcap=70_000, last_liq=25_000, max_mcap=70_000, multiple=1.01)
    session.add(tok)
    session.flush()
    dec = Decision(
        token_id=tok.id,
        mint=tok.mint,
        kind="entry",
        chain=chain,
        at=opened_at,
        entry_p=0.2,
        entry_mcap=70_000,
        liq=25_000,
        features_json=tags_json,
        image_rev="test",
    )
    session.add(dec)
    session.flush()
    session.add(
        PaperFill(
            chain=chain,
            mint=tok.mint,
            token_id=tok.id,
            decision_id=dec.id,
            line="paper_v1",
            opened_at=opened_at,
            entry_p=0.2,
            entry_mcap=70_000,
            entry_liq=25_000,
            target=2.0,
            ride=10.0,
            max_mcap=70_000,
            min_mcap=70_000,
            last_mcap=70_000,
            last_liq=25_000,
            status=status,
            exit_reason=f"d:{day}" if status == "open" else f"v1 cap|d:{day}",
            image_rev="test",
            updated_at=opened_at,
        )
    )


def test_day_report_gate_uses_hard_tag_on_opens_and_labels_any_tag():
    init_db()
    day = "2026-10-26"
    now = datetime(2026, 10, 26, 16, 0, tzinfo=timezone.utc)
    meme = '{"name_quality": 0.9, "github_auth_n": 0.0, "real_project": 0.0, "gmgn_cto": 0.0}'
    github = '{"name_quality": 0.2, "github_auth_n": 0.9, "real_project": 1.0, "gmgn_cto": 0.0}'
    with session_scope() as session:
        # RH: 2 opens, both meme-only. Sol: 1 open with github. 6 skipped github labels.
        _row(session, "robinhood", 1, day=day, status="open", tags_json=meme, opened_at=now)
        _row(session, "robinhood", 2, day=day, status="open", tags_json=meme, opened_at=now)
        _row(session, "sol", 3, day=day, status="open", tags_json=github, opened_at=now)
        for i in range(10, 16):
            _row(session, "sol", i, day=day, status="skipped", tags_json=github, opened_at=now)
        session.flush()
        out = paper_v1_day_report(session, day=day, now=now)
        gate = out["gate"]
        assert gate["thesis_opens_n"] == 3 and gate["thesis_hard_tag_n"] == 1
        assert gate["thesis_coverage"] == round(1 / 3, 4)
        assert gate["thesis_coverage_def"] == THESIS_COVERAGE_DEF
        # The old number (any tag over every label) is still there, but named for what it is.
        assert gate["thesis_any_tag_all_labels"] == 1.0
        assert "hard-tag cov on opens 33%" in out["text"]
        # Day-delta reads the same definition per chain.
        rh = paper_v1_day_delta(session, "robinhood", day=day, now=now)["today"]
        assert rh["thesis_n"] == 2 and rh["thesis_coverage"] == 0.0 and rh["thesis_any_tag_coverage"] == 1.0
        sol = paper_v1_day_delta(session, "sol", day=day, now=now)["today"]
        assert sol["thesis_n"] == 1 and sol["thesis_coverage"] == 1.0
