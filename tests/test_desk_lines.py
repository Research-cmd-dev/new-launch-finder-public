"""Desk lines follow the scorer that wrote the Entry (stack-v76).

Legacy blend: 0.70 / 0.90. First-sight (calibrated, Sol): 0.10 / 0.14 (v86;
0.15 / 0.30 from v80 to v85). A first-sight 0.55 is above its buy line and must fill paper, write
line rows, count on the board and colour Hunt — while a legacy 0.55 on the
same tape must not.
"""

import json
from datetime import timedelta

from launchfinder.db import init_db, session_scope
from launchfinder.desk_lines import (
    FIRST_SIGHT_LINES,
    LEGACY_LINES,
    SCORER_FIRST_SIGHT,
    SCORER_LEGACY,
    desk_lines,
    lines_for_scorer,
    scorer_for,
)
from launchfinder.ledger import (
    DECISION_ENTRY,
    DECISION_GATE,
    DECISION_LINE70,
    DECISION_LINE90,
    PAPER_LINE,
    REPAIR_SCORER_KEY,
    honest_calibration,
    honest_weekly,
    ledger_context,
    paper_ledger_view,
    record_entry_decision,
    repair_decision_scorers,
    seed_decisions_from_t0,
    sync_paper_ledger,
)
from launchfinder.models import Decision, HuntCard, ModelArtifact, Outcome, PaperFill, Research, ScanState, Snapshot, Token, utcnow
from launchfinder.scoring import first_sight as fs
from launchfinder.scoring.hunt import HUNT_THIN_WATCH_LIVE_CAP, conviction_from_tape, hunt_thin_watch_book, upsert_hunt
from launchfinder.scoring.outcomes import _commit_live_score
from launchfinder.serialize import apply_desk_entry_honesty, desk_entry_cap, token_card


def _token(mint: str, *, p: float, scorer: str = SCORER_LEGACY, chain: str = "sol", age_min: float = 6.0, holders: int = 120, flags: str = "[]"):
    now = utcnow()
    tok = Token(
        mint=mint,
        symbol=mint[:4].upper(),
        chain=chain,
        first_seen_at=now - timedelta(minutes=age_min),
        migrated_at=now - timedelta(minutes=age_min),
        created_at_chain=now - timedelta(minutes=age_min + 30),
        source="poll",
    )
    tok.research = Research(features_json='{"holder_n": 0.4}', p_good=p, heuristic_p=0.8, model_p=0.9, holder_count=holders, risk_flags_json=flags, scorer=scorer)
    tok.outcome = Outcome(t0_mcap=69_000, last_mcap=70_000, last_liq=25_000, max_mcap=70_000, multiple=1.01)
    return tok


def _promote_first_sight(session, chain="sol"):
    art = ModelArtifact(
        chain=chain,
        kind=fs.KIND,
        created_at=utcnow(),
        version=1,
        weights_json=json.dumps({"w": {n: 0.0 for n in fs.FEATURES}, "mu": {n: 0.0 for n in fs.FEATURES}, "sd": {n: 1.0 for n in fs.FEATURES}}),
        bias=0.0,
        calibration_json="[]",
        n_train=10,
        n_valid=10,
        metrics_json="{}",
        incumbent_json="{}",
        promoted=True,
    )
    session.add(art)
    session.flush()
    fs.reset_cache()
    return art


def test_lines_follow_the_scorer_not_the_chain():
    assert (LEGACY_LINES.lo, LEGACY_LINES.hi, LEGACY_LINES.thin) == (0.70, 0.90, 0.20)
    assert (FIRST_SIGHT_LINES.lo, FIRST_SIGHT_LINES.hi, FIRST_SIGHT_LINES.thin) == (0.10, 0.14, 0.05)
    assert lines_for_scorer("first_sight") is FIRST_SIGHT_LINES
    assert lines_for_scorer("legacy") is LEGACY_LINES and lines_for_scorer(None) is LEGACY_LINES and lines_for_scorer("garbage") is LEGACY_LINES
    assert scorer_for({"first_sight_p": 0.41}) == SCORER_FIRST_SIGHT
    assert scorer_for({"first_sight_p": None}) == SCORER_LEGACY and scorer_for(None) == SCORER_LEGACY
    assert scorer_for({"first_sight_p": 0.41, "awaiting_fill": True}) == SCORER_LEGACY
    assert FIRST_SIGHT_LINES.hi_label == "14" and FIRST_SIGHT_LINES.lo_label == "10" and LEGACY_LINES.lo_label == "70"
    init_db()
    with session_scope() as session:
        # No promoted first-sight fit: the chain runs legacy lines.
        assert desk_lines(session, "sol") is LEGACY_LINES
        assert desk_lines(session, "robinhood") is LEGACY_LINES
        _promote_first_sight(session)
        assert desk_lines(session, "sol") is FIRST_SIGHT_LINES
        # A Sol fit does not switch RH; RH needs its own promoted artifact,
        # and then runs on RH first-sight lines (0.25 / 0.30).
        assert desk_lines(session, "robinhood") is LEGACY_LINES
        _promote_first_sight(session, chain="robinhood")
        from launchfinder.desk_lines import FIRST_SIGHT_RH_LINES

        assert desk_lines(session, "robinhood") is FIRST_SIGHT_RH_LINES and desk_lines(session, "rh").hi == 0.3


def test_legacy_equivalent_reads_a_first_sight_entry_through_its_lines():
    """Runner tiers (0.55 / 0.65) and Bloom's weak-entry test think in the
    legacy scale. A first-sight Entry maps piecewise through its lines:
    lo -> 0.70, hi -> 0.90. Legacy is the identity."""
    from launchfinder.desk_lines import FIRST_SIGHT_RH_LINES, legacy_equivalent
    from launchfinder.scoring.bloom import promise_score, should_alert

    assert legacy_equivalent(0.55, LEGACY_LINES) == 0.55 and legacy_equivalent(0.93, LEGACY_LINES) == 0.93
    assert legacy_equivalent(0.10, FIRST_SIGHT_LINES) == 0.70 and legacy_equivalent(0.14, FIRST_SIGHT_LINES) == 0.90
    # TIPPED-class Sol ceiling 0.1918 is a buy (90-eq ~91), not a junk 19.
    assert legacy_equivalent(0.1918, FIRST_SIGHT_LINES) == 0.906
    assert legacy_equivalent(0.05, FIRST_SIGHT_LINES) == 0.35 and legacy_equivalent(0.12, FIRST_SIGHT_LINES) == 0.80
    assert legacy_equivalent(1.0, FIRST_SIGHT_LINES) == 1.0 and legacy_equivalent(0.0, FIRST_SIGHT_LINES) == 0.0
    # RH: 0.30 is the high line, so 0.42 reads above the legacy 90.
    assert legacy_equivalent(0.30, FIRST_SIGHT_RH_LINES) == 0.90 and legacy_equivalent(0.42, FIRST_SIGHT_RH_LINES) > 0.90
    assert legacy_equivalent(0.275, FIRST_SIGHT_RH_LINES) == 0.80

    # Bloom: a 2.5x book with a first-sight 0.42 Entry is not a "late
    # bloomer after a weak entry" — no weak-entry bonus, no alert on that ground.
    common = dict(chain="robinhood", multiple=2.5, last_mcap=150_000, t0_mcap=60_000, max_mcap=150_000, last_liq=40_000, holders=300, top10_pct=30.0, vol_h1=50_000, runner_p=None, second_leg=False, label=None, flags=[])
    weak, weak_reasons = promise_score(entry_p=0.42, **common)
    strong, strong_reasons = promise_score(entry_p=0.42, entry_legacy=legacy_equivalent(0.42, FIRST_SIGHT_RH_LINES), **common)
    assert any("Entry score was only" in r for r in weak_reasons) and not any("Entry score was only" in r for r in strong_reasons)
    assert strong <= weak
    assert should_alert(0.42, 0.70, False, 0.58) is True  # read raw, 0.42 < min_promise: "late bloomer"
    assert should_alert(legacy_equivalent(0.42, FIRST_SIGHT_RH_LINES), 0.70, False, 0.58) is False  # read on its lines: a strong entry doing 1.2x is not a bloom


def test_rh_first_sight_cards_are_read_against_rh_lines_everywhere():
    """An RH first-sight 0.27 is between its lines (0.25 / 0.30); the same
    number on Sol is above its buy line (0.10 / 0.14). Entry decisions,
    line rows, the review clause, ledger context and the card all agree."""
    from launchfinder.desk_lines import FIRST_SIGHT_RH_LINES
    from launchfinder.scoring.hunt import high_line_clause

    init_db()
    with session_scope() as session:
        rh = _token("0xrhfs27000000000000000000000000000000001", p=0.27, scorer=SCORER_FIRST_SIGHT, chain="robinhood")
        sol = _token("SolFs2700000000000000000000000000000000001", p=0.27, scorer=SCORER_FIRST_SIGHT, chain="sol")
        leg = _token("0xrhleg9100000000000000000000000000000001", p=0.91, scorer=SCORER_LEGACY, chain="robinhood")
        session.add_all([rh, sol, leg])
        session.flush()
        scored = {"p_good": 0.27, "first_sight_p": 0.27, "legacy_p": 0.6, "risk_flags": []}
        record_entry_decision(session, rh, rh.research, scored, market={"mcap_usd": 71_000, "liquidity_usd": 25_000}, holder_count=120)
        record_entry_decision(session, sol, sol.research, scored, market={"mcap_usd": 71_000, "liquidity_usd": 25_000}, holder_count=120)
        kinds_rh = {d.kind for d in session.query(Decision).filter(Decision.mint == rh.mint).all()}
        kinds_sol = {d.kind for d in session.query(Decision).filter(Decision.mint == sol.mint).all()}
        assert kinds_rh == {DECISION_ENTRY, DECISION_LINE70}
        assert kinds_sol == {DECISION_ENTRY, DECISION_LINE70, DECISION_LINE90}
        ctx = ledger_context(session, rh)
        assert ctx["entry"]["desk_lines"] == FIRST_SIGHT_RH_LINES.as_dict()
        assert [(ln["slot"], ln["threshold"]) for ln in ctx["lines"]] == [("lo", 0.25)]
        for tok in (rh, sol, leg):
            session.add(HuntCard(chain=tok.chain, mint=tok.mint, token_id=tok.id, entry_p=float(tok.research.p_good), scorer=tok.research.scorer, t0_mcap=69_000, last_mcap=70_000))
        session.flush()
        above = {m for (m,) in session.query(HuntCard.mint).filter(high_line_clause()).all()}
        assert above == {sol.mint, leg.mint}
        card = token_card(rh)
        assert card["scorer"] == SCORER_FIRST_SIGHT and card["desk_lines"]["hi"] == 0.3
        assert token_card(sol)["desk_lines"]["hi"] == 0.14


def test_entry_decision_stamps_scorer_and_writes_lines_on_its_own_scale():
    init_db()
    with session_scope() as session:
        tok = _token("LinesFs11111111111111111111111111111111111", p=0.55, scorer=SCORER_FIRST_SIGHT)
        session.add(tok)
        session.flush()
        scored = {"p_good": 0.55, "first_sight_p": 0.55, "legacy_p": 0.93, "heuristic_p": 0.80, "model_p": 0.97, "risk_flags": []}
        row = record_entry_decision(session, tok, tok.research, scored, market={"mcap_usd": 71_000, "liquidity_usd": 25_000}, holder_count=120)
        assert row.scorer == SCORER_FIRST_SIGHT and row.entry_p == 0.55
        kinds = {d.kind: d for d in session.query(Decision).filter(Decision.mint == tok.mint).all()}
        # 0.55 crosses both first-sight lines (0.10 / 0.14) ...
        assert set(kinds) == {DECISION_ENTRY, DECISION_LINE70, DECISION_LINE90}
        assert kinds[DECISION_LINE90].scorer == SCORER_FIRST_SIGHT and kinds[DECISION_LINE90].source == "live"
        # ... while the same number from the legacy blend crosses neither.
        leg = _token("LinesLeg1111111111111111111111111111111111", p=0.55, scorer=SCORER_LEGACY)
        session.add(leg)
        session.flush()
        row2 = record_entry_decision(session, leg, leg.research, {"p_good": 0.55, "first_sight_p": None}, market={"mcap_usd": 71_000}, holder_count=120)
        assert row2.scorer == SCORER_LEGACY
        assert {d.kind for d in session.query(Decision).filter(Decision.mint == leg.mint).all()} == {DECISION_ENTRY}
        ctx = ledger_context(session, tok)
        assert ctx["entry"]["scorer"] == SCORER_FIRST_SIGHT and ctx["entry"]["desk_lines"]["hi"] == 0.14
        assert [(ln["slot"], ln["threshold"], ln["scorer"]) for ln in ctx["lines"]] == [("lo", 0.10, "first_sight"), ("hi", 0.14, "first_sight")]


def test_hunt_card_freezes_scorer_and_uses_its_lines_for_crossings_and_thin_cap():
    init_db()
    with session_scope() as session:
        tok = _token("LinesHunt111111111111111111111111111111111", p=0.52, scorer=SCORER_FIRST_SIGHT)
        session.add(tok)
        session.flush()
        card = upsert_hunt(session, tok)
        assert card.scorer == SCORER_FIRST_SIGHT and card.entry_p == 0.52
        assert sorted(d.kind for d in session.query(Decision).filter(Decision.mint == tok.mint).all()) == [DECISION_LINE70, DECISION_LINE90]
        # The research row is re-labelled later; the card keeps the frozen scorer.
        tok.research.scorer = SCORER_LEGACY
        upsert_hunt(session, tok, touch_updated=False)
        assert card.scorer == SCORER_FIRST_SIGHT
        # Legacy 0.52 on the same tape: no crossing.
        leg = _token("LinesHuntL11111111111111111111111111111111", p=0.52)
        session.add(leg)
        session.flush()
        upsert_hunt(session, leg)
        assert session.query(Decision).filter(Decision.mint == leg.mint).count() == 0
    # No tape: Entry/flag still answers. A fat print is not leftover dust.
    assert hunt_thin_watch_book(0.11) is True
    assert hunt_thin_watch_book(0.11, thin_entry=FIRST_SIGHT_LINES.thin) is False
    assert hunt_thin_watch_book(0.03, thin_entry=FIRST_SIGHT_LINES.thin) is True
    assert hunt_thin_watch_book(0.11, last_liq=60_000, holders=400) is False
    assert hunt_thin_watch_book(0.03, thin_entry=FIRST_SIGHT_LINES.thin, last_liq=60_000, holders=400) is False
    assert hunt_thin_watch_book(0.68, last_liq=2_000, holders=400) is True
    assert hunt_thin_watch_book(0.68, last_liq=60_000, holders=8) is True
    kw = dict(chain="sol", multiple=6.0, last_mcap=420_000, t0_mcap=70_000, max_mcap=420_000, last_liq=60_000, holders=400, top10_pct=20.0, flags=[], vol_h1=80_000)
    legacy_live = conviction_from_tape(entry_p=0.11, **kw)
    fs_live = conviction_from_tape(entry_p=0.11, thin_entry=FIRST_SIGHT_LINES.thin, **kw)
    assert legacy_live > HUNT_THIN_WATCH_LIVE_CAP and fs_live > HUNT_THIN_WATCH_LIVE_CAP


def test_paper_ledger_fills_at_the_scorers_high_line():
    init_db()
    now = utcnow()
    with session_scope() as session:
        _promote_first_sight(session)
        fs_buy = _token("LinesPaperFs111111111111111111111111111111", p=0.56, scorer=SCORER_FIRST_SIGHT)
        fs_low = _token("LinesPaperLo111111111111111111111111111111", p=0.08, scorer=SCORER_FIRST_SIGHT)
        leg_mid = _token("LinesPaperLg111111111111111111111111111111", p=0.60, scorer=SCORER_LEGACY)
        leg_buy = _token("LinesPaperLb111111111111111111111111111111", p=0.92, scorer=SCORER_LEGACY)
        session.add_all([fs_buy, fs_low, leg_mid, leg_buy])
        session.flush()
        for tok in (fs_buy, fs_low, leg_mid, leg_buy):
            upsert_hunt(session, tok)
        out = sync_paper_ledger(session, "sol", now=now)
        assert out["opened"] == 2
        assert {f.mint for f in session.query(PaperFill).all()} == {fs_buy.mint, leg_buy.mint}
        gated = {g.mint for g in session.query(Decision).filter(Decision.kind == DECISION_GATE).all()}
        assert gated == {fs_buy.mint, leg_buy.mint}
        from launchfinder.ledger import paper_ledger_view

        view = paper_ledger_view(session, "sol")
        assert view["lines"]["scorer"] == SCORER_FIRST_SIGHT and "Entry>=0.14" in view["strategy"]
        assert "watch>=0.10" in view["strategy"] and "Live>=0.50" in view["strategy"]
        by_mint = {g.mint: g.scorer for g in session.query(Decision).filter(Decision.kind == DECISION_GATE)}
        assert by_mint[fs_buy.mint] == SCORER_FIRST_SIGHT and by_mint[leg_buy.mint] == SCORER_LEGACY


def test_unknown_holders_do_not_skip_the_live_watch_gate():
    init_db()
    now = utcnow()
    with session_scope() as session:
        _promote_first_sight(session)
        blank = _token("LinesPaperUk111111111111111111111111111111", p=0.12, scorer=SCORER_FIRST_SIGHT, holders=0)
        session.add(blank)
        session.flush()
        upsert_hunt(session, blank)
        assert sync_paper_ledger(session, "sol", now=now)["opened"] == 0
        assert session.query(PaperFill).count() == 0


def test_start_high_writes_a_shadow_fill_and_not_a_buy():
    init_db()
    now = utcnow()
    with session_scope() as session:
        _promote_first_sight(session)
        late = _token(
            "LinesPaperSh111111111111111111111111111111",
            p=0.56,
            scorer=SCORER_FIRST_SIGHT,
            flags='["start-high rug"]',
        )
        session.add(late)
        session.flush()
        upsert_hunt(session, late)
        out = sync_paper_ledger(session, "sol", now=now)
        assert out["opened"] == 0
        fill = session.query(PaperFill).one()
        assert fill.line == "shadow_late" and fill.status == "open"
        from launchfinder.models import Ticket

        assert session.query(Ticket).count() == 0
        gate = session.query(Decision).filter(Decision.kind == DECISION_GATE).one()
        assert gate.scorer == SCORER_FIRST_SIGHT and gate.veto == "start-high"
        view = paper_ledger_view(session, "sol")
        assert view["closed_trades"] == 0
        assert view["scorecard"]["shadow_late"]["open"] == 1
        assert view["scorecard"]["this_window"]["n"] == 0


def test_rh_start_high_opens_a_gated_fill_sol_stays_shadow():
    """New RH start-high / pre-pumped books. Prior rugs stay vetoed. Sol stays shadow."""
    init_db()
    now = utcnow()
    with session_scope() as session:
        _promote_first_sight(session, chain="robinhood")
        late = _token(
            "0x18e600000000000000000000000000000000ff5c",
            p=0.36,
            scorer=SCORER_FIRST_SIGHT,
            chain="robinhood",
            flags='["Already dumped below graduation mcap (start-high rug pattern)"]',
        )
        pre = _token(
            "0x18e600000000000000000000000000000000ff5d",
            p=0.36,
            scorer=SCORER_FIRST_SIGHT,
            chain="robinhood",
            flags='["pre-pumped before the book"]',
        )
        rugs = _token(
            "0x18e600000000000000000000000000000000ff5e",
            p=0.36,
            scorer=SCORER_FIRST_SIGHT,
            chain="robinhood",
            flags='["Creator was funded by a wallet behind prior rugs"]',
        )
        session.add_all([late, pre, rugs])
        session.flush()
        for tok in (late, pre, rugs):
            upsert_hunt(session, tok)
        out = sync_paper_ledger(session, "robinhood", now=now)
        assert out["opened"] == 2
        fills = {f.mint: f for f in session.query(PaperFill).filter(PaperFill.line == PAPER_LINE).all()}
        assert set(fills) == {late.mint, pre.mint}
        assert fills[late.mint].line == "gated90" and fills[late.mint].status == "open"
        assert fills[pre.mint].line == "gated90"
        from launchfinder.models import Ticket

        assert {t.mint for t in session.query(Ticket).all()} == {late.mint, pre.mint}
        gate = session.query(Decision).filter(Decision.kind == DECISION_GATE, Decision.mint == late.mint).one()
        assert gate.scorer == SCORER_FIRST_SIGHT and gate.veto == ""
        rug_gate = session.query(Decision).filter(Decision.kind == DECISION_GATE, Decision.mint == rugs.mint).one()
        assert rug_gate.veto == "prior rugs"
        view = paper_ledger_view(session, "robinhood")
        assert "start-high and pre-pumped still paper" in view["strategy"]
        assert view["scorecard"]["shadow_late"]["n"] == 0
        assert view["open_positions"] == 2


def test_sol_paper_opens_watch_only_when_live_is_healthy():
    """Thin watch still needs Live >= 0.50. Fresh fat watch opens without it."""
    from launchfinder.models import ModelArtifact
    from launchfinder.scoring.batch_fit import reset_artifact_cache
    from launchfinder.scoring.live_fit import LIVE_PAPER_HI

    init_db()
    reset_artifact_cache()
    now = utcnow()
    with session_scope() as session:
        _promote_first_sight(session)
        thin = _token("LinesPaperLv111111111111111111111111111111", p=0.12, scorer=SCORER_FIRST_SIGHT, holders=10)
        fat = _token("LinesPaperFf111111111111111111111111111111", p=0.12, scorer=SCORER_FIRST_SIGHT, holders=120)
        session.add_all([thin, fat])
        session.flush()
        upsert_hunt(session, thin)
        upsert_hunt(session, fat)
        first = sync_paper_ledger(session, "sol", now=now)
        assert first["opened"] == 1
        assert session.query(PaperFill).filter(PaperFill.line == PAPER_LINE).one().mint == fat.mint
        session.add(
            ModelArtifact(
                chain="sol",
                kind="live",
                created_at=now,
                version=1,
                weights_json="{}",
                bias=3.0,
                calibration_json="[]",
                n_train=400,
                n_valid=100,
                metrics_json="{}",
                incumbent_json="{}",
                promoted=True,
            )
        )
        session.flush()
        reset_artifact_cache()
        assert LIVE_PAPER_HI == 0.50
        assert sync_paper_ledger(session, "sol", now=now)["opened"] == 1
        mints = {f.mint for f in session.query(PaperFill).all()}
        assert mints == {thin.mint, fat.mint}


def test_paper_uses_frozen_decision_not_drifted_hunt_entry():
    """NBS / JERRY: Hunt 0.63 cannot fill RH when the ledger Entry is 0.24."""
    init_db()
    now = utcnow()
    with session_scope() as session:
        _promote_first_sight(session, chain="robinhood")
        drifted = _token(
            "0x1bca4677c658d274c1425cb96d20d20a21b33fcf",
            p=0.63,
            scorer=SCORER_FIRST_SIGHT,
            chain="robinhood",
            holders=15,
        )
        honest = _token(
            "0x380a0842617f0276fcdc833e70d61155575c4ba3",
            p=0.63,
            scorer=SCORER_FIRST_SIGHT,
            chain="robinhood",
            holders=80,
        )
        session.add_all([drifted, honest])
        session.flush()
        session.add(
            Decision(
                chain="robinhood",
                mint=drifted.mint,
                token_id=drifted.id,
                kind=DECISION_ENTRY,
                at=now,
                source="live",
                scorer=SCORER_FIRST_SIGHT,
                entry_p=0.24,
                entry_mcap=27_978,
                liq=27_890,
            )
        )
        session.add(
            Decision(
                chain="robinhood",
                mint=honest.mint,
                token_id=honest.id,
                kind=DECISION_ENTRY,
                at=now,
                source="live",
                scorer=SCORER_FIRST_SIGHT,
                entry_p=0.36,
                entry_mcap=30_000,
                liq=28_000,
            )
        )
        session.flush()
        upsert_hunt(session, drifted)
        upsert_hunt(session, honest)
        drift_card = session.query(HuntCard).filter(HuntCard.mint == drifted.mint).one()
        assert drift_card.entry_p == 0.24
        drift_card.entry_p = 0.63
        session.flush()
        out = sync_paper_ledger(session, "robinhood", now=now)
        mints = {f.mint for f in session.query(PaperFill).filter(PaperFill.chain == "robinhood").all()}
        assert drifted.mint not in mints
        assert honest.mint in mints
        assert out["opened"] == 1


def test_rh_paper_opens_watch_only_when_live_is_healthy():
    """RH 0.25–0.30 fills only when Live >= 0.50. No fresh-fat bypass."""
    from launchfinder.models import ModelArtifact
    from launchfinder.scoring.batch_fit import reset_artifact_cache
    from launchfinder.scoring.live_fit import LIVE_PAPER_HI
    from launchfinder.scoring.paper_gate import PAPER_WINDOW_RH

    init_db()
    reset_artifact_cache()
    now = utcnow()
    with session_scope() as session:
        _promote_first_sight(session, chain="robinhood")
        watch = _token(
            "0xrhwatch250000000000000000000000000000001",
            p=0.27,
            scorer=SCORER_FIRST_SIGHT,
            chain="robinhood",
            holders=120,
        )
        hi = _token(
            "0xrhhi300000000000000000000000000000000001",
            p=0.36,
            scorer=SCORER_FIRST_SIGHT,
            chain="robinhood",
            holders=80,
        )
        session.add_all([watch, hi])
        session.flush()
        upsert_hunt(session, watch)
        upsert_hunt(session, hi)
        first = sync_paper_ledger(session, "robinhood", now=now)
        assert first["opened"] == 1
        assert session.query(PaperFill).filter(PaperFill.line == PAPER_LINE).one().mint == hi.mint
        session.add(
            ModelArtifact(
                chain="robinhood",
                kind="live",
                created_at=now,
                version=1,
                weights_json="{}",
                bias=3.0,
                calibration_json="[]",
                n_train=400,
                n_valid=100,
                metrics_json="{}",
                incumbent_json="{}",
                promoted=True,
            )
        )
        session.flush()
        reset_artifact_cache()
        assert LIVE_PAPER_HI == 0.50
        assert PAPER_WINDOW_RH == 12.0
        assert sync_paper_ledger(session, "robinhood", now=now)["opened"] == 1
        mints = {f.mint for f in session.query(PaperFill).filter(PaperFill.chain == "robinhood").all()}
        assert mints == {watch.mint, hi.mint}
        view = paper_ledger_view(session, "robinhood")
        assert "RH watch>=0.25" in view["strategy"] and "no fresh-fat bypass" in view["strategy"]
        assert view["scorecard"]["open"]["watch_n"] == 1


def test_board_reads_each_decision_against_its_own_lines_and_can_split_by_scorer():
    init_db()
    now = utcnow()
    with session_scope() as session:
        rows = [
            ("LinesBoardA111111111111111111111111111111", 0.55, SCORER_FIRST_SIGHT, 200_000),  # fs hi, 2x hit
            ("LinesBoardB111111111111111111111111111111", 0.11, SCORER_FIRST_SIGHT, 50_000),  # fs lo, miss
            ("LinesBoardC111111111111111111111111111111", 0.93, SCORER_LEGACY, 50_000),  # legacy hi, miss
            ("LinesBoardD111111111111111111111111111111", 0.55, SCORER_LEGACY, 200_000),  # legacy under both lines
        ]
        for mint, p, scorer, last in rows:
            tok = _token(mint, p=p, scorer=scorer, age_min=60 * 30)
            tok.outcome.last_mcap = last
            tok.outcome.max_mcap = last
            session.add(tok)
            session.flush()
            session.add(Decision(chain="sol", mint=mint, token_id=tok.id, kind=DECISION_ENTRY, at=now - timedelta(hours=30), source="live", scorer=scorer, entry_p=p, entry_mcap=69_000, liq=25_000))
            session.add(Snapshot(token=tok, kind="live", mcap_usd=last, liquidity_usd=25_000, taken_at=now - timedelta(hours=20)))
        session.flush()
        wk = honest_weekly(session, "sol", weeks=4)
        week = wk["weeks"][-1]
        assert week["scorers"] == {SCORER_FIRST_SIGHT: 2, SCORER_LEGACY: 2}
        # hi cell: fs 0.55 (hit) + legacy 0.93 (miss). The legacy 0.55 is not on any line.
        assert week["lines"]["hi"]["n"] == 2 and week["lines"]["hi"]["hit2x"] == 1
        # lo cell: fs 0.55, fs 0.20, legacy 0.93.
        assert week["lines"]["lo"]["n"] == 3 and week["lines"]["lo"]["hit2x"] == 1
        assert wk["lines"]["scorer"] == SCORER_LEGACY  # no promoted fit in this fixture
        cal_all = honest_calibration(session, "sol")
        assert cal_all["resolved"] == 4 and cal_all["scorers"] == {SCORER_FIRST_SIGHT: 2, SCORER_LEGACY: 2}
        cal_fs = honest_calibration(session, "sol", scorer=SCORER_FIRST_SIGHT)
        assert cal_fs["resolved"] == 2 and cal_fs["scorer"] == SCORER_FIRST_SIGHT
        assert [b["bin"] for b in cal_fs["bins"]] == ["0.1-0.2", "0.5-0.6"]
        cal_leg = honest_calibration(session, "sol", scorer=SCORER_LEGACY)
        assert cal_leg["resolved"] == 2 and [b["bin"] for b in cal_leg["bins"]] == ["0.5-0.6", "0.9-1.0"]


def test_second_look_keeps_the_first_sight_entry_and_refreshes_flags_only():
    research = Research(p_good=0.44, heuristic_p=0.3, model_p=0.2, scorer=SCORER_FIRST_SIGHT, risk_flags_json="[]", reasons_json="[]")
    outcome = Outcome(t0_mcap=69_000, last_mcap=80_000, max_mcap=80_000, multiple=1.16)
    lifted = _commit_live_score(research, outcome, {"heuristic_p": 0.9, "model_p": 0.95, "reasons": ["tape ok"], "risk_flags": ["Dumping on real volume"]}, 0.91)
    assert lifted is False and research.p_good == 0.44 and research.heuristic_p == 0.3
    assert json.loads(research.risk_flags_json) == ["Dumping on real volume"] and json.loads(research.reasons_json) == ["tape ok"]
    legacy = Research(p_good=0.44, heuristic_p=0.3, model_p=0.2, scorer=SCORER_LEGACY, risk_flags_json="[]", reasons_json="[]")
    assert _commit_live_score(legacy, outcome, {"heuristic_p": 0.9, "model_p": 0.95}, 0.91) is True and legacy.p_good == 0.91


def test_first_sight_cards_skip_the_legacy_display_caps_and_carry_their_lines():
    fs_card = {"scorer": "first_sight", "p_good": 0.56, "risk_flags": ["Same ticker launched repeatedly in 24h (copycat spam)", "First-hour tape is dumping on real volume"], "heuristic_p": 0.91}
    assert desk_entry_cap(0.56, fs_card) == 0.56
    out = apply_desk_entry_honesty(dict(fs_card))
    # Entry is the frozen first-sight p, never the legacy heuristic.
    assert out["entry_p"] == 0.56 and out["p_good"] == 0.56
    leg_card = {"scorer": "legacy", "p_good": 0.92, "risk_flags": ["Same ticker launched repeatedly in 24h (copycat spam)", "First-hour tape is dumping on real volume"], "heuristic_p": 0.91}
    assert desk_entry_cap(0.92, leg_card) == 0.48
    init_db()
    with session_scope() as session:
        tok = _token("LinesCard111111111111111111111111111111111", p=0.56, scorer=SCORER_FIRST_SIGHT, flags='["Same ticker launched repeatedly in 24h (copycat spam)"]')
        session.add(tok)
        session.flush()
        card = token_card(tok)
        assert card["scorer"] == "first_sight" and card["entry_p"] == 0.56 and card["desk_lines"]["hi"] == 0.14


def test_repair_stamps_v75_first_sight_rows_and_seeded_line_sources(monkeypatch):
    init_db()
    now = utcnow()
    with session_scope() as session:
        # A v75 live first-sight entry: legacy_p frozen in features, entry_p differs, no scorer.
        fs_tok = _token("LinesRepairFs11111111111111111111111111111", p=0.58)
        fs_tok.research.scorer = SCORER_LEGACY
        session.add(fs_tok)
        session.flush()
        card = upsert_hunt(session, fs_tok)
        session.add(
            Decision(
                chain="sol",
                mint=fs_tok.mint,
                token_id=fs_tok.id,
                kind=DECISION_ENTRY,
                at=now - timedelta(hours=1),
                source="live",
                image_rev="stack-v75",
                entry_p=0.58,
                entry_mcap=70_000,
                liq=25_000,
                holders=120,
                features_json=json.dumps({"holder_n": 0.4, "legacy_p": 0.93}),
            )
        )
        # A v75 live legacy entry: entry_p equals legacy_p — stays legacy.
        leg_tok = _token("LinesRepairLg11111111111111111111111111111", p=0.93)
        session.add(leg_tok)
        session.flush()
        session.add(Decision(chain="sol", mint=leg_tok.mint, token_id=leg_tok.id, kind=DECISION_ENTRY, at=now - timedelta(hours=1), source="live", image_rev="stack-v75", entry_p=0.93, features_json=json.dumps({"legacy_p": 0.93})))
        # A seeded entry whose line rows were stamped live by the v75 seed.
        seed_tok = _token("LinesRepairSd11111111111111111111111111111", p=0.95)
        session.add(seed_tok)
        session.flush()
        session.add(Decision(chain="sol", mint=seed_tok.mint, token_id=seed_tok.id, kind=DECISION_ENTRY, at=now - timedelta(days=2), source="seed_t0", entry_p=0.95))
        session.add(Decision(chain="sol", mint=seed_tok.mint, token_id=seed_tok.id, kind=DECISION_LINE90, at=now - timedelta(days=2), source="live", entry_p=0.95))
        session.flush()
        out = repair_decision_scorers(session)
        assert out == {"source": 1, "scorer": 1, "lines": 2}
        d = {(r.mint, r.kind): r for r in session.query(Decision).all()}
        assert d[(fs_tok.mint, DECISION_ENTRY)].scorer == SCORER_FIRST_SIGHT
        assert d[(fs_tok.mint, DECISION_LINE90)].scorer == SCORER_FIRST_SIGHT and d[(fs_tok.mint, DECISION_LINE90)].at == d[(fs_tok.mint, DECISION_ENTRY)].at
        assert d[(leg_tok.mint, DECISION_ENTRY)].scorer == SCORER_LEGACY and (leg_tok.mint, DECISION_LINE70) not in d
        assert d[(seed_tok.mint, DECISION_LINE90)].source == "seed_t0"
        session.refresh(card)
        assert card.scorer == SCORER_FIRST_SIGHT and fs_tok.research.scorer == SCORER_FIRST_SIGHT
        # Once.
        assert repair_decision_scorers(session) == {"skipped": 1}
        assert session.query(ScanState).filter(ScanState.key == REPAIR_SCORER_KEY).count() == 1


def test_seed_pass_writes_legacy_lines_with_seed_source():
    init_db()
    now = utcnow()
    with session_scope() as session:
        _promote_first_sight(session)  # chain runs first-sight lines now ...
        tok = _token("LinesSeed111111111111111111111111111111111", p=0.92, age_min=60)
        session.add(tok)
        session.flush()
        session.add(Snapshot(token=tok, kind="t0", mcap_usd=70_000, liquidity_usd=20_000, p_good=0.92, taken_at=now - timedelta(minutes=60)))
        session.flush()
        assert seed_decisions_from_t0(session) == 1
        rows = {r.kind: r for r in session.query(Decision).filter(Decision.mint == tok.mint).all()}
        # ... but the t0 snap was a legacy score: legacy lines, seed source, legacy stamp.
        assert set(rows) == {DECISION_ENTRY, DECISION_LINE70, DECISION_LINE90}
        assert all(r.source == "seed_t0" and r.scorer == SCORER_LEGACY for r in rows.values())


def test_line_metrics_report_flow_and_hit_rate_at_both_lines():
    import numpy as np

    now = utcnow()
    valid = [{"at": now - timedelta(days=7)} for _ in range(10)]
    p = np.array([0.05, 0.1, 0.2, 0.31, 0.35, 0.4, 0.52, 0.6, 0.65, 0.66])
    y2 = np.array([0, 0, 0, 1, 0, 1, 1, 1, 0, 1], dtype=float)
    y5 = np.array([0, 0, 0, 0, 0, 1, 1, 0, 0, 1], dtype=float)
    out = fs.line_metrics(p, y2, y5, valid, now=now)
    assert out["lo"]["threshold"] == 0.10 and out["lo"]["n"] == 9 and out["lo"]["hit2x"] == round(5 / 9, 4) and out["lo"]["per_day"] == round(9 / 7, 1)
    assert out["hi"]["threshold"] == 0.14 and out["hi"]["n"] == 8 and out["hi"]["hit2x"] == round(5 / 8, 4) and out["hi"]["hit5x"] == round(3 / 8, 4) and out["hi"]["share"] == 0.8


def test_api_exposes_lines_and_scorer_splits():
    from fastapi.testclient import TestClient

    from launchfinder.app import app

    init_db()
    with session_scope() as session:
        _promote_first_sight(session)
        tok = _token("LinesApi1111111111111111111111111111111111", p=0.53, scorer=SCORER_FIRST_SIGHT)
        session.add(tok)
        session.flush()
        upsert_hunt(session, tok)
    client = TestClient(app)
    weekly = client.get("/api/ledger/weekly?chain=sol").json()
    assert weekly["lines"] == FIRST_SIGHT_LINES.as_dict() and weekly["legacy_lines"] == LEGACY_LINES.as_dict()
    cal = client.get("/api/model/calibration?chain=sol&scorer=first_sight").json()
    assert cal["scorer"] == SCORER_FIRST_SIGHT and cal["lines"]["hi"] == 0.14
    assert client.get("/api/model/calibration?chain=sol&scorer=nope").status_code == 422
    hunt = client.get("/api/hunt?chain=sol").json()["items"]
    row = next(r for r in hunt if r["mint"] == "LinesApi1111111111111111111111111111111111")
    assert row["scorer"] == SCORER_FIRST_SIGHT and row["desk_lines"]["hi"] == 0.14 and row["entry_p"] == 0.53
    art = client.get("/api/model/artifacts?chain=sol&kind=first_sight").json()
    assert art["desk_lines"]["scorer"] == SCORER_FIRST_SIGHT
    dec = client.get("/api/ledger/decisions?chain=sol&kind=line90").json()["items"]
    assert dec and dec[0]["scorer"] == SCORER_FIRST_SIGHT and dec[0]["entry_p"] == 0.53
    paper = client.get("/api/paper?chain=sol&gated=true").json()
    assert paper["lines"]["hi"] == 0.14
