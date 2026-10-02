"""stack-v207: copycat FALSE-VETO Learn grain. Hard skip stays. Paper only."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from launchfinder.db import init_db, session_scope
from launchfinder.desk_lines import SCORER_FIRST_SIGHT
from launchfinder.ingest.fomo_poll import save_trending_snapshot
from launchfinder.ledger import reconsider_copycat_veto_shadow
from launchfinder.models import Decision, HuntCard, Outcome, PaperFill, Research, Token
from launchfinder.risk import defaults
from launchfinder.scoring.copycat_learn import (
    COPYCAT_LEARN_ARMED,
    FOMO_TOP_BAND,
    GATE_VETO_COPYCAT,
    GATE_VETO_KEY,
    WOULD_HAVE_KEY,
    WWW_EVIDENCE,
    WWW_MINT,
    copycat_false_veto_evidence,
    copycat_hard_skip_holds,
    copycat_veto_cohort,
    fomo_board_rank,
    honest_floors_pass,
    in_fomo_top_band,
    merge_www_evidence,
    stamp_copycat_learn_sides,
    would_have_copycat_shadow,
)
from launchfinder.scoring.features import FEATURE_NAMES
from launchfinder.scoring.miss_cohort import miss_cohort
from launchfinder.scoring.paper_gate import PAPER_HARD_NEEDLES, paper_hard_veto
from launchfinder.scoring.paper_v1 import (
    PAPER_V1_COPYCAT_VETO,
    PAPER_V1_LINE,
    PAPER_V1_SHADOW_LINE,
    PAPER_V1_SKIPPED,
)

NOW = datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)
THESIS_OK = {
    "github_auth_n": 0.9,
    "real_project": 1.0,
    "gmgn_cto": 0.0,
    "name_quality": 0.2,
}
THESIS_DICT = {"score": 0.4, "hard_tags": ["github"], "tags": ["github"]}


def test_constants_stay_paper_only_and_hard():
    assert COPYCAT_LEARN_ARMED is False
    assert defaults()["armed"] is False
    assert FOMO_TOP_BAND == 3
    assert GATE_VETO_COPYCAT == "copycat"
    assert WWW_MINT == "GAwhcphCqCv5bKHmCiN4VDdNWfbXJL4npmkc8L3Q9S9H"
    assert GATE_VETO_KEY not in FEATURE_NAMES
    assert WOULD_HAVE_KEY not in FEATURE_NAMES
    assert "fomo_rank" not in FEATURE_NAMES
    assert len(FEATURE_NAMES) == 66
    assert "copycat spam" in PAPER_HARD_NEEDLES
    assert paper_hard_veto(["copycat spam"]) == "copycat spam"
    assert copycat_hard_skip_holds(["Same ticker launched repeatedly in 24h (copycat spam)"])
    ev = copycat_false_veto_evidence()
    assert ev[0]["mint"] == WWW_MINT
    assert ev[0]["gate_veto"] == "copycat"
    assert ev[0]["label"] == "FALSE-VETO"
    assert ev[0]["entry_p"] == 0.2712
    assert ev[0]["entry_mcap"] == 204_000


def test_would_have_requires_fomo_top_hunt_and_floors():
    ok = dict(
        veto="copycat spam",
        on_hunt=True,
        fomo_rank=2,
        holders=40,
        liq=8_000.0,
        thesis=THESIS_DICT,
    )
    assert would_have_copycat_shadow(**ok) is True
    assert would_have_copycat_shadow(**{**ok, "fomo_rank": 3}) is True
    assert would_have_copycat_shadow(**{**ok, "fomo_rank": 8}) is False
    assert would_have_copycat_shadow(**{**ok, "fomo_rank": None}) is False
    assert would_have_copycat_shadow(**{**ok, "on_hunt": False}) is False
    assert would_have_copycat_shadow(**{**ok, "holders": 10}) is False
    assert would_have_copycat_shadow(**{**ok, "liq": 4999.0}) is False
    assert would_have_copycat_shadow(**{**ok, "thesis": {"score": 0.4, "tags": ["meme"]}}) is False
    assert would_have_copycat_shadow(**{**ok, "veto": "start-high"}) is False
    assert in_fomo_top_band(1) and in_fomo_top_band(3) and not in_fomo_top_band(4)
    assert honest_floors_pass(holders=26, liq=5000, thesis=THESIS_DICT)
    assert not honest_floors_pass(holders=25, liq=5000, thesis=THESIS_DICT)


def test_fomo_board_rank_uses_official_rank_or_position():
    items = [
        {"mint": "Aaa1111111111111111111111111111111111111111", "rank": 7, "chain": "sol"},
        {"mint": WWW_MINT, "chain": "sol"},
        {"mint": "Ccc1111111111111111111111111111111111111111", "rank": 1, "chain": "sol"},
    ]
    assert fomo_board_rank(items, WWW_MINT) == 2
    assert fomo_board_rank(items, "Aaa1111111111111111111111111111111111111111") == 7
    assert fomo_board_rank(items, "missing") is None


def test_www_is_always_evidence_row_one():
    later = [
        {
            "mint": "OtherCopycatWinner1111111111111111111111111",
            "symbol": "CLONE",
            "gate_veto": "copycat",
            "multiple": 8.0,
        },
        {
            "mint": WWW_MINT,
            "symbol": "www",
            "would_have": False,
            "multiple": 22.0,
            "gate_veto": "copycat",
        },
    ]
    rows = merge_www_evidence(later)
    assert rows[0]["mint"] == WWW_MINT
    assert rows[0]["id"] == "www-false-veto"
    assert rows[0]["gate_veto"] == "copycat"
    assert rows[0]["label"] == "FALSE-VETO"
    assert rows[0]["multiple"] == 22.0
    assert rows[1]["symbol"] == "CLONE"


def test_stamp_sides_do_not_grow_feature_names():
    feat = stamp_copycat_learn_sides(
        {"holder_n": 0.6},
        veto="copycat spam",
        would_have=True,
        fomo_rank=2,
    )
    assert feat[GATE_VETO_KEY] == "copycat"
    assert feat[WOULD_HAVE_KEY] is True
    assert feat["fomo_rank"] == 2
    assert feat["holder_n"] == 0.6
    assert GATE_VETO_KEY not in FEATURE_NAMES


def _seed_copycat(
    session,
    *,
    mint: str,
    symbol: str,
    rank: int | None,
    hunt: bool,
    thesis: bool,
    holders: int = 80,
    liq: float = 20_000.0,
    entry_p: float = 0.2712,
    entry_mcap: float = 204_000.0,
    multiple: float = 22.0,
):
    token = Token(
        mint=mint,
        symbol=symbol,
        chain="sol",
        source="websocket",
        first_seen_at=NOW - timedelta(hours=6),
        migrated_at=NOW - timedelta(hours=6),
    )
    token.research = Research(
        p_good=entry_p,
        features_json='{"github_auth_n":0.9,"real_project":1.0,"gmgn_cto":0.0,"name_quality":0.2}'
        if thesis
        else '{"name_quality":0.8}',
        risk_flags_json='["Same ticker launched repeatedly in 24h (copycat spam)"]',
        holder_count=holders,
        scorer=SCORER_FIRST_SIGHT,
    )
    peak = entry_mcap * multiple
    token.outcome = Outcome(
        t0_mcap=entry_mcap,
        last_mcap=peak,
        max_mcap=peak,
        last_liq=liq,
        multiple=multiple,
    )
    session.add(token)
    session.flush()
    gate = Decision(
        token_id=token.id,
        chain="sol",
        mint=mint,
        kind="gate",
        entry_p=entry_p,
        entry_mcap=entry_mcap,
        liq=liq,
        holders=holders,
        veto=PAPER_V1_COPYCAT_VETO,
        at=NOW - timedelta(hours=2),
        source="live",
        features_json=token.research.features_json,
    )
    entry = Decision(
        token_id=token.id,
        chain="sol",
        mint=mint,
        kind="entry",
        entry_p=entry_p,
        entry_mcap=entry_mcap,
        liq=liq,
        holders=holders,
        at=NOW - timedelta(hours=5),
        source="live",
        features_json=token.research.features_json,
    )
    session.add_all([gate, entry])
    session.flush()
    if hunt:
        session.add(
            HuntCard(
                chain="sol",
                mint=mint,
                token_id=token.id,
                first_seen_at=NOW - timedelta(hours=6),
                launched_at=NOW - timedelta(hours=6),
                entry_p=entry_p,
                scorer=SCORER_FIRST_SIGHT,
                conviction_p=0.4,
                t0_mcap=entry_mcap,
                last_mcap=peak,
                multiple=multiple,
                last_liq=liq,
                holders=holders,
                updated_at=NOW,
            )
        )
    if rank is not None:
        save_trending_snapshot(
            session,
            [{"mint": mint, "chain": "sol", "symbol": symbol, "rank": rank}],
            now=NOW,
        )
    return token, gate


def test_www_false_veto_shadow_and_miss_cohort_tag():
    init_db()
    with session_scope() as session:
        token, gate = _seed_copycat(
            session,
            mint=WWW_MINT,
            symbol="www",
            rank=2,
            hunt=True,
            thesis=True,
        )
        out = reconsider_copycat_veto_shadow(session, "sol", now=NOW)
        assert out["shadow"] >= 1
        assert out["would_have"] >= 1
        shadow = (
            session.query(PaperFill)
            .filter(PaperFill.mint == WWW_MINT, PaperFill.line == PAPER_V1_SHADOW_LINE)
            .one()
        )
        assert shadow.status == PAPER_V1_SKIPPED
        assert shadow.exit_reason.startswith("v1 veto|copycat|d:")
        assert (
            session.query(PaperFill)
            .filter(PaperFill.mint == WWW_MINT, PaperFill.line == PAPER_V1_LINE)
            .count()
            == 0
        )
        session.refresh(gate)
        feat = __import__("json").loads(gate.features_json)
        assert feat[GATE_VETO_KEY] == "copycat"
        assert feat[WOULD_HAVE_KEY] is True
        assert feat["fomo_rank"] == 2
        assert paper_hard_veto(["copycat spam"]) == "copycat spam"

        cohort = miss_cohort(session, "sol", days=14, now=NOW)
        ev = cohort["evidence_rows"]
        assert ev[0]["mint"] == WWW_MINT
        assert ev[0]["gate_veto"] == "copycat"
        assert ev[0]["label"] == "FALSE-VETO"
        assert cohort["n_copycat_veto_winners"] >= 1
        assert cohort["copycat_veto"]["armed"] is False
        assert cohort["copycat_veto"]["open"] is False
        winners = [
            r
            for day in cohort["by_day"]
            for r in day.get("winner_sample") or []
            if r.get("mint") == WWW_MINT
        ]
        assert winners and winners[0]["gate_veto"] == "copycat"
        pm = cohort["paper_misses"]
        assert pm["copycat_veto"]["evidence_rows"][0]["mint"] == WWW_MINT
        assert pm["n_copycat_suppressed"] >= 1


def test_would_have_false_when_fomo_rank_outside_top_band():
    init_db()
    mint = "CopycatRankEight11111111111111111111111111"
    with session_scope() as session:
        token, gate = _seed_copycat(
            session,
            mint=mint,
            symbol="CLONE",
            rank=8,
            hunt=True,
            thesis=True,
            entry_p=0.22,
            entry_mcap=80_000.0,
            multiple=6.0,
        )
        out = reconsider_copycat_veto_shadow(session, "sol", now=NOW)
        assert out["shadow"] >= 1
        session.refresh(gate)
        feat = __import__("json").loads(gate.features_json)
        assert feat[GATE_VETO_KEY] == "copycat"
        assert feat[WOULD_HAVE_KEY] is False
        assert feat["fomo_rank"] == 8
        assert (
            session.query(PaperFill)
            .filter(PaperFill.mint == mint, PaperFill.line == PAPER_V1_LINE)
            .count()
            == 0
        )
        card = copycat_veto_cohort(session, "sol", days=14, now=NOW, win_mult=2.0)
        assert card["evidence_rows"][0]["mint"] == WWW_MINT
        live = [r for r in card["evidence_rows"] if r["mint"] == mint]
        assert live and live[0]["gate_veto"] == "copycat"
        assert live[0]["would_have"] is False


def test_would_have_false_without_hunt_card():
    init_db()
    mint = "CopycatNoHunt11111111111111111111111111111"
    with session_scope() as session:
        token, gate = _seed_copycat(
            session,
            mint=mint,
            symbol="NOHUNT",
            rank=1,
            hunt=False,
            thesis=True,
            entry_mcap=90_000.0,
            multiple=5.5,
        )
        reconsider_copycat_veto_shadow(session, "sol", now=NOW)
        session.refresh(gate)
        feat = __import__("json").loads(gate.features_json)
        assert feat[GATE_VETO_KEY] == "copycat"
        assert feat[WOULD_HAVE_KEY] is False
        assert feat["fomo_rank"] == 1
