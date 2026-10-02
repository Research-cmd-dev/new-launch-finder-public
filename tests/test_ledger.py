from datetime import timedelta

from launchfinder.db import init_db, session_scope
from launchfinder.ledger import (
    DECISION_ENTRY,
    DECISION_GATE,
    Evidence,
    DECISION_LINE70,
    DECISION_LINE90,
    PAPER_LINE,
    decision_result,
    honest_calibration,
    honest_weekly,
    ledger_context,
    list_tickets,
    moonbag_return,
    paper_ledger_view,
    record_entry_decision,
    record_line_decisions,
    seed_decisions_from_t0,
    set_ticket_status,
    sync_paper_ledger,
    ticket_report,
    ticket_size_usd,
    ticket_slippage_pct,
)
from launchfinder.models import Decision, HuntCard, Outcome, PaperFill, Research, ScanState, Snapshot, Ticket, Token, utcnow
from launchfinder.scoring.hunt import upsert_hunt


def _token(mint: str, *, p: float, chain: str = "sol", age_min: float = 5.0, source: str = "poll", holders: int = 120, flags: str = "[]"):
    now = utcnow()
    tok = Token(
        mint=mint,
        symbol=mint[:4].upper(),
        chain=chain,
        first_seen_at=now - timedelta(minutes=age_min),
        migrated_at=now - timedelta(minutes=age_min),
        created_at_chain=now - timedelta(minutes=age_min + 30),
        source=source,
    )
    tok.research = Research(features_json='{"holder_n": 0.4}', p_good=p, heuristic_p=p, model_p=p, holder_count=holders, risk_flags_json=flags)
    tok.outcome = Outcome(t0_mcap=69_000, last_mcap=70_000, last_liq=25_000, max_mcap=70_000, multiple=1.01)
    return tok


def test_entry_decision_written_once_and_never_rewritten():
    init_db()
    with session_scope() as session:
        tok = _token("LedgerEntry1111111111111111111111111111111", p=0.92)
        session.add(tok)
        session.flush()
        scored = {"p_good": 0.92, "heuristic_p": 0.80, "model_p": 0.97, "risk_flags": ["X account created very recently"], "model_version": 7}
        row = record_entry_decision(session, tok, tok.research, scored, market={"mcap_usd": 71_000, "liquidity_usd": 25_000, "volume_h1": 9_000}, holder_count=120)
        assert row is not None and row.kind == DECISION_ENTRY
        assert row.entry_p == 0.92 and row.entry_mcap == 71_000 and row.model_version == 7
        assert row.features_hash and row.image_rev
        # Repairs later move research.p_good; the decision does not move.
        tok.research.p_good = 0.48
        again = record_entry_decision(session, tok, tok.research, {"p_good": 0.48}, market={}, holder_count=1)
        assert again is None
        kinds = {d.kind for d in session.query(Decision).filter(Decision.mint == tok.mint).all()}
        assert kinds == {DECISION_ENTRY, DECISION_LINE70, DECISION_LINE90}
        stored = session.query(Decision).filter(Decision.mint == tok.mint, Decision.kind == DECISION_ENTRY).one()
        assert stored.entry_p == 0.92


def test_backfill_and_historical_are_not_decisions():
    init_db()
    with session_scope() as session:
        back = _token("LedgerBack11111111111111111111111111111111", p=0.95, source="backfill")
        hist = _token("LedgerHist11111111111111111111111111111111", p=0.95)
        hist.is_historical = True
        session.add_all([back, hist])
        session.flush()
        assert record_entry_decision(session, back, back.research, {"p_good": 0.95}, market={}, holder_count=5) is None
        assert record_entry_decision(session, hist, hist.research, {"p_good": 0.95}, market={}, holder_count=5) is None
        assert session.query(Decision).count() == 0


def test_upsert_hunt_records_line_crossings_once():
    init_db()
    with session_scope() as session:
        tok = _token("LedgerLine11111111111111111111111111111111", p=0.91)
        session.add(tok)
        session.flush()
        upsert_hunt(session, tok)
        upsert_hunt(session, tok, touch_updated=False)
        rows = session.query(Decision).filter(Decision.mint == tok.mint).all()
        assert sorted(d.kind for d in rows) == [DECISION_LINE70, DECISION_LINE90]
        low = _token("LedgerLow111111111111111111111111111111111", p=0.55)
        session.add(low)
        session.flush()
        upsert_hunt(session, low)
        assert session.query(Decision).filter(Decision.mint == low.mint).count() == 0


def test_decision_result_uses_decision_entry_not_repaired_t0():
    d = Decision(entry_p=0.9, entry_mcap=100_000, liq=25_000, at=utcnow() - timedelta(hours=30))
    o = Outcome(t0_mcap=20_000, max_mcap=250_000, last_mcap=150_000, last_liq=30_000, t24h_mcap=150_000, label=1)
    res = decision_result(d, o, evidence=Evidence(250_000.0, 3))
    assert res is not None and res["hit2x"] is True and res["hit5x"] is False
    assert res["multiple"] == 2.5 and res["exit_multiple"] == 1.5
    # v80: the outcome's lifetime high never vouches on its own. Live v79 Sol
    # board: GYAT sight $146k, every later print $147k-$154k, judged 3.3x
    # from a max_mcap set months before the decision; San / KWIF / titcoin
    # hit the 80x cap on a 1.0x tape. Without a print we saw, the peak is
    # the last sellable look.
    flat = decision_result(d, o)
    assert flat["multiple"] == 1.5 and flat["hit2x"] is False and flat["dead"] is False
    ancient = Outcome(t0_mcap=146_298, max_mcap=488_000, last_mcap=147_512, last_liq=80_738, label=0)
    old = Decision(entry_p=0.34, entry_mcap=146_298, liq=80_254, at=utcnow() - timedelta(hours=30))
    res_old = decision_result(old, ancient, evidence=Evidence(154_219.0, 12))
    assert res_old["hit2x"] is False and res_old["multiple"] == round(154_219 / 146_298, 3)
    # Same wick on a drained pool is a loss.
    dead = Outcome(t0_mcap=20_000, max_mcap=250_000, last_mcap=1_000, last_liq=200, label=0)
    res2 = decision_result(d, dead, evidence=Evidence(250_000.0, 3))
    assert res2["hit2x"] is False and res2["dead"] is True and res2["under_half"] is True
    # Still open inside the horizon and unlabeled.
    young = Decision(entry_p=0.9, entry_mcap=100_000, at=utcnow() - timedelta(hours=2))
    assert decision_result(young, Outcome(t0_mcap=100_000, max_mcap=120_000, last_liq=30_000)) is None


def test_decision_result_rebases_a_bonding_curve_entry_to_the_first_pool():
    """Live v69 board: TOKPAID-style entries scored at $28k on the curve with
    liq 0 'hit 2x' on the $69k graduation print alone. Nobody bought $28k."""
    curve = Decision(entry_p=0.54, entry_mcap=27_962, liq=0.0, at=utcnow() - timedelta(hours=30))
    filled = Evidence(120_000.0, 3, 69_000.0, curve.at + timedelta(minutes=10))
    graduated = Outcome(t0_mcap=69_000, max_mcap=120_000, last_mcap=90_000, last_liq=30_000, t24h_mcap=90_000, label=0)
    res = decision_result(curve, graduated, evidence=filled)
    assert res["entry_rebased"] is True
    assert res["multiple"] == round(120_000 / 69_000, 3) and res["hit2x"] is False
    assert res["exit_multiple"] == round(90_000 / 69_000, 3)
    # A real 2x from the pool still counts.
    ran = Outcome(t0_mcap=69_000, max_mcap=150_000, last_mcap=140_000, last_liq=30_000, t24h_mcap=140_000, label=1)
    assert decision_result(curve, ran, evidence=Evidence(150_000.0, 3, 69_000.0, curve.at + timedelta(minutes=10)))["hit2x"] is True
    # Never got a pool: no exit existed, so it is a dead loss, not an 80x wick.
    stuck = Outcome(t0_mcap=0, max_mcap=2_500_000, last_mcap=2_000, last_liq=0, label=0)
    res3 = decision_result(curve, stuck)
    assert res3["dead"] is True and res3["hit2x"] is False and res3["entry_rebased"] is False
    # Pump reports the curve's SOL as "liquidity": TIC scored at $29 with liq $13
    # is still a curve entry, not an 80x on graduation.
    dust = Decision(entry_p=0.05, entry_mcap=29, liq=13.0, at=utcnow() - timedelta(hours=30))
    res5 = decision_result(dust, graduated, evidence=Evidence(120_000.0, 3, 69_000.0, dust.at + timedelta(minutes=10)))
    assert res5["entry_rebased"] is True and res5["multiple"] == round(120_000 / 69_000, 3) and res5["hit2x"] is False
    # A pooled entry (liq > 0) is judged from its own print, never re-based to t0.
    pooled = Decision(entry_p=0.9, entry_mcap=100_000, liq=25_000, at=utcnow() - timedelta(hours=30))
    res4 = decision_result(pooled, Outcome(t0_mcap=20_000, max_mcap=250_000, last_mcap=150_000, last_liq=30_000, t24h_mcap=150_000, label=1), evidence=Evidence(250_000.0, 3))
    assert res4["entry_rebased"] is False and res4["multiple"] == 2.5


def test_honest_calibration_and_weekly_exclude_backfill_and_use_frozen_p():
    init_db()
    now = utcnow()
    with session_scope() as session:
        win = _token("LedgerWin111111111111111111111111111111111", p=0.93, age_min=60 * 30)
        win.outcome = Outcome(t0_mcap=69_000, max_mcap=300_000, last_mcap=200_000, last_liq=40_000, t24h_mcap=200_000, multiple=4.3, label=0)
        lose = _token("LedgerLose11111111111111111111111111111111", p=0.91, age_min=60 * 30)
        lose.outcome = Outcome(t0_mcap=69_000, max_mcap=80_000, last_mcap=3_000, last_liq=300, t24h_mcap=3_000, multiple=1.1, label=0)
        back = _token("LedgerBack22222222222222222222222222222222", p=0.95, age_min=60 * 30, source="backfill")
        session.add_all([win, lose, back])
        session.flush()
        for tok in (win, lose, back):
            session.add(
                Decision(
                    chain="sol",
                    mint=tok.mint,
                    token_id=tok.id,
                    kind=DECISION_ENTRY,
                    at=now - timedelta(hours=30),
                    entry_p=tok.research.p_good,
                    entry_mcap=69_000,
                    liq=30_000,
                )
            )
        session.flush()
        # Repair moves the live score; the honest board does not follow it.
        win.research.p_good = 0.30
        cal = honest_calibration(session, "sol")
        assert cal["basis"] == "entry" and cal["resolved"] == 2
        top = [b for b in cal["bins"] if b["bin"] == "0.9-1.0"][0]
        assert top["n"] == 2 and top["hit2x"] == 0.5 and top["hit5x"] == 0.0 and top["dead"] == 0.5
        wk = honest_weekly(session, "sol", weeks=4)
        cell = wk["weeks"][-1]["lines"]["hi"]
        assert cell["n"] == 2 and cell["resolved"] == 2 and cell["hit2x_rate"] == 0.5
        assert wk["weeks"][-1]["scorers"] == {"legacy": 2}
        assert wk["lines"]["scorer"] == "legacy" and wk["lines"]["hi"] == 0.9


def test_seed_from_t0_snaps_runs_once_and_skips_backfill():
    init_db()
    now = utcnow()
    with session_scope() as session:
        live = _token("LedgerSeed11111111111111111111111111111111", p=0.40, age_min=600)
        live.research.p_good = 0.12  # later fade; the t0 snap is the frozen score
        live.snapshots.append(Snapshot(kind="t0", mcap_usd=72_000, liquidity_usd=20_000, volume_h1=5_000, p_good=0.40, taken_at=now - timedelta(minutes=600)))
        back = _token("LedgerSeed22222222222222222222222222222222", p=0.90, age_min=600, source="backfill")
        back.snapshots.append(Snapshot(kind="t0", mcap_usd=69_000, liquidity_usd=20_000, volume_h1=5_000, p_good=0.90, taken_at=now - timedelta(minutes=600)))
        session.add_all([live, back])
        session.flush()
        assert seed_decisions_from_t0(session) == 1
        row = session.query(Decision).filter(Decision.mint == live.mint, Decision.kind == DECISION_ENTRY).one()
        assert row.source == "seed_t0" and row.entry_p == 0.40 and row.entry_mcap == 72_000
        assert session.query(Decision).filter(Decision.mint == back.mint).count() == 0
        assert session.query(ScanState).filter(ScanState.key == "ledger:seeded").count() == 1
        assert seed_decisions_from_t0(session) == 0


def test_judge_needs_a_real_sellable_print_and_counts_silence_as_a_loss():
    """Live v74 board: RH ghost books seeded Outcome.max_mcap at the $40k
    graduation floor while the decision printed $4k on the curve, so a token
    that never traded again read as a 9x. 2,972 of 3,291 "hits" in the 0.6
    bin were that. The peak must be a print we recorded on a sellable pool."""
    d = Decision(entry_p=0.65, entry_mcap=4_254, liq=9_400, at=utcnow() - timedelta(hours=30))
    ghost = Outcome(t0_mcap=40_000, max_mcap=40_000, last_mcap=0, last_liq=9_400, t24h_mcap=40_000, label=0)
    res = decision_result(d, ghost, evidence=(0.0, 0))
    assert res["unobserved"] is True and res["dead"] is True and res["hit2x"] is False and res["multiple"] == 0.0
    # The same seed with one real later print at $5k: observed, but the $40k seed still never counts.
    seen = Outcome(t0_mcap=40_000, max_mcap=40_000, last_mcap=5_000, last_liq=9_400, label=0)
    res = decision_result(d, seen, evidence=(5_000.0, 1))
    assert res["unobserved"] is False and res["hit2x"] is False and res["multiple"] == round(5_000 / 4_254, 3)
    # A refresh that raised max_mcap above its seed is a real ATH.
    ran = Outcome(t0_mcap=40_000, max_mcap=60_000, last_mcap=20_000, last_liq=9_400, label=0)
    assert decision_result(d, ran, evidence=(20_000.0, 3))["hit2x"] is True
    # A 2x print on a $2k pool is a wick nobody sells into: the evidence
    # helper already zeroes it, and the outcome's own last print needs a
    # sellable book too.
    thin = Outcome(t0_mcap=40_000, max_mcap=40_000, last_mcap=12_000, last_liq=2_000, label=0)
    res = decision_result(d, thin, evidence=(0.0, 4))
    assert res["unobserved"] is False and res["hit2x"] is False and res["multiple"] == 0.0
    # Evidence is the honest peak even when the outcome row lags.
    assert decision_result(d, thin, evidence=(9_000.0, 4))["hit2x"] is True
    # Without evidence passed, the outcome's own Dex print still resolves the row.
    plain = Outcome(t0_mcap=4_000, max_mcap=4_000, last_mcap=9_000, last_liq=30_000, label=0)
    assert decision_result(d, plain)["hit2x"] is True


def test_post_decision_evidence_reads_only_later_sellable_prints():
    from launchfinder.ledger import post_decision_evidence

    init_db()
    now = utcnow()
    with session_scope() as session:
        tok = _token("LedgerEvid11111111111111111111111111111111", p=0.5, age_min=60 * 30)
        at = now - timedelta(hours=30)
        tok.snapshots.extend(
            [
                Snapshot(kind="t0", mcap_usd=50_000, liquidity_usd=20_000, taken_at=at - timedelta(seconds=1)),
                Snapshot(kind="live", mcap_usd=500_000, liquidity_usd=1_000, taken_at=at + timedelta(minutes=5)),  # thin wick
                Snapshot(kind="live", mcap_usd=120_000, liquidity_usd=22_000, taken_at=at + timedelta(minutes=9)),
            ]
        )
        session.add(tok)
        session.flush()
        d = Decision(chain="sol", mint=tok.mint, token_id=tok.id, kind=DECISION_ENTRY, at=at, entry_p=0.5, entry_mcap=50_000, liq=20_000)
        session.add(d)
        session.flush()
        ev = post_decision_evidence(session, [d])
        assert (ev[d.id].peak, ev[d.id].n) == (120_000.0, 2)
        # Pooled at sight: the fill print is informational, the judge uses the sight mcap.
        assert ev[d.id].fill_mcap == 120_000.0 and ev[d.id].fill_at == at + timedelta(minutes=9)
        res = decision_result(d, Outcome(t0_mcap=50_000, max_mcap=50_000, last_mcap=0, last_liq=22_000), evidence=ev[d.id])
        assert res["hit2x"] is True and res["multiple"] == 2.4 and res["unobserved"] is False and res["entry_rebased"] is False


def test_judge_measures_a_curve_or_thin_entry_from_the_first_fillable_print():
    """Live v78 board: 2,373 Sol curve 'wins' judged from the curve print, 432
    still 2x once measured from the first sellable pool print — the median
    first print sat 16.8x above the curve number. Graduation is not a trade
    anyone took; the paper desk fills at the first liquid print, so the
    judge must too. The old 0.50 line read 59% on the curve label, 23% here."""
    from launchfinder.ledger import Evidence, post_decision_evidence

    init_db()
    now = utcnow()
    at = now - timedelta(hours=30)
    with session_scope() as session:
        # Scored at $4k on the curve; pool opens at $69k; peak $110k; that is 1.6x from the fill, not 27x.
        grad = _token("LedgerFill11111111111111111111111111111111", p=0.6, age_min=60 * 30)
        grad.snapshots.extend(
            [
                Snapshot(kind="t0", mcap_usd=4_000, liquidity_usd=0, taken_at=at),
                Snapshot(kind="live", mcap_usd=40_000, liquidity_usd=60, taken_at=at + timedelta(minutes=3)),  # still on the curve
                Snapshot(kind="live", mcap_usd=69_000, liquidity_usd=18_000, taken_at=at + timedelta(minutes=6)),  # pool open: the fill
                Snapshot(kind="live", mcap_usd=110_000, liquidity_usd=25_000, taken_at=at + timedelta(minutes=30)),
            ]
        )
        # Scored at $30k on the curve; pool opens at $60k and runs to $150k: a real 2.5x from the fill.
        run = _token("LedgerFill22222222222222222222222222222222", p=0.6, age_min=60 * 30)
        run.snapshots.extend(
            [
                Snapshot(kind="t0", mcap_usd=30_000, liquidity_usd=0, taken_at=at),
                Snapshot(kind="live", mcap_usd=60_000, liquidity_usd=20_000, taken_at=at + timedelta(minutes=8)),
                Snapshot(kind="live", mcap_usd=150_000, liquidity_usd=40_000, taken_at=at + timedelta(hours=3)),
            ]
        )
        # Thin book at sight ($2k liq) that never reached a sellable pool inside 24h, then a pool days later.
        thin = _token("LedgerFill33333333333333333333333333333333", p=0.6, age_min=60 * 30)
        thin.snapshots.extend(
            [
                Snapshot(kind="t0", mcap_usd=20_000, liquidity_usd=2_000, taken_at=at),
                Snapshot(kind="live", mcap_usd=50_000, liquidity_usd=3_000, taken_at=at + timedelta(hours=2)),
                Snapshot(kind="live", mcap_usd=90_000, liquidity_usd=30_000, taken_at=at + timedelta(hours=26)),  # past the horizon
            ]
        )
        session.add_all([grad, run, thin])
        session.flush()
        rows = []
        for tok, liq in ((grad, 0.0), (run, 0.0), (thin, 2_000.0)):
            d = Decision(chain="sol", mint=tok.mint, token_id=tok.id, kind=DECISION_ENTRY, at=at, entry_p=0.6, entry_mcap=tok.snapshots[0].mcap_usd, liq=liq)
            session.add(d)
            rows.append(d)
        session.flush()
        ev = post_decision_evidence(session, rows)
        g = ev[rows[0].id]
        assert g.fill_mcap == 69_000.0 and g.fill_at == at + timedelta(minutes=6) and g.peak == 110_000.0 and g.n == 3
        res = decision_result(rows[0], Outcome(t0_mcap=69_000, max_mcap=2_000_000, last_mcap=100_000, last_liq=25_000, label=0), evidence=g)
        assert res["entry_rebased"] is True and res["fill_mcap"] == 69_000 and res["fill_multiple"] == 17.25 and res["fill_lag_min"] == 6.0
        # Peak is the sellable $110k print over the $69k fill; the seed's 2M ATH (before any fill) does not vouch.
        assert res["multiple"] == round(110_000 / 69_000, 3) and res["hit2x"] is False and res["unfilled"] is False
        r = ev[rows[1].id]
        res2 = decision_result(rows[1], Outcome(t0_mcap=60_000, max_mcap=150_000, last_mcap=140_000, last_liq=40_000, label=1), evidence=r)
        assert res2["fill_mcap"] == 60_000 and res2["multiple"] == 2.5 and res2["hit2x"] is True and res2["fill_multiple"] == 2.0
        t = ev.get(rows[2].id)
        # One print inside the horizon (the t0 sits in the grace), not sellable; the day-later pool is outside it.
        assert t is not None and t.n == 1 and t.fill_mcap == 0.0 and t.peak == 0.0
        res3 = decision_result(rows[2], Outcome(t0_mcap=20_000, max_mcap=90_000, last_mcap=90_000, last_liq=30_000, label=0), evidence=t)
        assert res3["unfilled"] is True and res3["dead"] is True and res3["hit2x"] is False and res3["multiple"] == 0.0
        # A decision fillable at sight is judged from its own print, whatever came later.
        pooled = Decision(entry_p=0.6, entry_mcap=50_000, liq=20_000, at=at)
        res4 = decision_result(pooled, Outcome(t0_mcap=50_000, max_mcap=50_000, last_mcap=120_000, last_liq=22_000, label=0), evidence=Evidence(120_000.0, 2, 120_000.0, at + timedelta(minutes=9)))
        assert res4["entry_rebased"] is False and res4["multiple"] == 2.4 and res4["hit2x"] is True
        # Old-style (peak, n) tuples still read; without a fill the curve entry falls back to the t0 proxy.
        curve = Decision(entry_p=0.6, entry_mcap=4_000, liq=0.0, at=at)
        res5 = decision_result(curve, Outcome(t0_mcap=69_000, max_mcap=69_000, last_mcap=100_000, last_liq=25_000, label=0), evidence=(100_000.0, 3))
        assert res5["unfilled"] is True and res5["hit2x"] is False
        res6 = decision_result(curve, Outcome(t0_mcap=69_000, max_mcap=69_000, last_mcap=150_000, last_liq=25_000, label=0))
        assert res6["entry_rebased"] is True and res6["fill_mcap"] == 69_000 and res6["hit2x"] is True


def test_board_keeps_retired_tokens_and_reseeds_them():
    """Live v74: pump_poll flips Sol to is_historical at 18h and the RH quiet
    retire does the same; the board's join dropped them — 40 survivors judged,
    21,610 first-sight scores invisible. Retired is not backfill."""
    from launchfinder.ledger import RESEED_RETIRED_KEY, entry_decisions_with_outcomes

    init_db()
    now = utcnow()
    with session_scope() as session:
        retired = _token("LedgerRet111111111111111111111111111111111", p=0.91, age_min=60 * 30)
        retired.is_historical = True
        retired.outcome = Outcome(t0_mcap=69_000, max_mcap=69_000, last_mcap=2_000, last_liq=300, label=0)
        session.add(retired)
        session.flush()
        session.add(Decision(chain="sol", mint=retired.mint, token_id=retired.id, kind=DECISION_ENTRY, at=now - timedelta(hours=30), entry_p=0.91, entry_mcap=69_000, liq=20_000))
        session.flush()
        rows = entry_decisions_with_outcomes(session, "sol")
        assert [d.mint for d, _o, _ in rows] == [retired.mint]
        cal = honest_calibration(session, "sol")
        assert cal["resolved"] == 1 and cal["bins"][0]["hit2x"] == 0.0 and cal["bins"][0]["dead"] == 1.0
        assert "unobserved" in cal and cal["bins"][0]["unobserved"] == 0.0 and cal["bins"][0]["n_observed"] == 1

        # Retired pass of the seed: fresh-at-t0 names only, no feature copy, own key.
        fresh = _token("LedgerRet222222222222222222222222222222222", p=0.85, age_min=60 * 20)
        fresh.is_historical = True
        fresh.snapshots.append(Snapshot(kind="t0", mcap_usd=70_000, liquidity_usd=20_000, volume_h1=5_000, p_good=0.85, taken_at=now - timedelta(hours=20)))
        old = _token("LedgerRet333333333333333333333333333333333", p=0.80, age_min=60 * 20)
        old.is_historical = True
        old.created_at_chain = now - timedelta(days=5)  # historical at first sight: not a launch
        old.migrated_at = now - timedelta(days=5)
        old.snapshots.append(Snapshot(kind="t0", mcap_usd=70_000, liquidity_usd=20_000, volume_h1=5_000, p_good=0.80, taken_at=now - timedelta(hours=20)))
        live = _token("LedgerRet444444444444444444444444444444444", p=0.70, age_min=60 * 20)
        live.snapshots.append(Snapshot(kind="t0", mcap_usd=70_000, liquidity_usd=20_000, volume_h1=5_000, p_good=0.70, taken_at=now - timedelta(hours=20)))
        session.add_all([fresh, old, live])
        session.flush()
        assert seed_decisions_from_t0(session, retired=True, key=RESEED_RETIRED_KEY) == 1
        row = session.query(Decision).filter(Decision.mint == fresh.mint, Decision.kind == DECISION_ENTRY).one()
        assert row.source == "seed_t0" and row.entry_p == 0.85 and row.features_json == "{}"
        assert session.query(Decision).filter(Decision.mint.in_([old.mint, live.mint])).count() == 0
        assert seed_decisions_from_t0(session, retired=True, key=RESEED_RETIRED_KEY) == 0
        # The ordinary pass still picks up the live one and skips the retired ones.
        assert seed_decisions_from_t0(session) == 1


def test_seed_survives_a_live_decision_landing_mid_seed(monkeypatch):
    """Live v67: the API wrote TOKPAID's entry between the `known` load and
    its page; one UniqueViolation aborted the whole seed. Now one row."""
    import launchfinder.ledger as ledger_mod

    init_db()
    now = utcnow()
    with session_scope() as session:
        first = _token("LedgerRaceA111111111111111111111111111111", p=0.40, age_min=600)
        first.snapshots.append(Snapshot(kind="t0", mcap_usd=72_000, liquidity_usd=20_000, volume_h1=5_000, p_good=0.40, taken_at=now - timedelta(minutes=600)))
        raced = _token("LedgerRaceB111111111111111111111111111111", p=0.55, age_min=500)
        raced.snapshots.append(Snapshot(kind="t0", mcap_usd=80_000, liquidity_usd=20_000, volume_h1=5_000, p_good=0.55, taken_at=now - timedelta(minutes=500)))
        third = _token("LedgerRaceC111111111111111111111111111111", p=0.60, age_min=400)
        third.snapshots.append(Snapshot(kind="t0", mcap_usd=90_000, liquidity_usd=20_000, volume_h1=5_000, p_good=0.60, taken_at=now - timedelta(minutes=400)))
        session.add_all([first, raced, third])
        session.flush()
        session.commit()

        real_seed_one = ledger_mod._seed_one

        def racing_seed_one(sess, row, *, known, seen, **kw):
            # Another process records B's live entry after `known` was loaded.
            if row[6] == raced.mint and (sess.query(Decision).filter(Decision.mint == raced.mint).count() == 0):
                with sess.begin_nested():
                    sess.add(Decision(chain="sol", mint=raced.mint, token_id=raced.id, kind=DECISION_ENTRY, at=now, source="live", entry_p=0.55, entry_mcap=80_000))
                    sess.flush()
            return real_seed_one(sess, row, known=known, seen=seen, **kw)

        monkeypatch.setattr(ledger_mod, "_seed_one", racing_seed_one)
        assert seed_decisions_from_t0(session) == 2
        rows = {d.mint: d for d in session.query(Decision).filter(Decision.kind == DECISION_ENTRY).all()}
        assert rows[first.mint].source == "seed_t0" and rows[third.mint].source == "seed_t0"
        # The seed never wrote B (in prod the live row, committed by the API
        # process, stays; here it shared the savepoint and rolled back with it).
        assert raced.mint not in rows or rows[raced.mint].source == "live"
        assert session.query(ScanState).filter(ScanState.key == "ledger:seeded").count() == 1


def test_paper_ledger_opens_once_records_veto_and_closes_moonbag():
    init_db()
    now = utcnow()
    with session_scope() as session:
        buy = _token("LedgerBuy111111111111111111111111111111111", p=0.92, age_min=6)
        vetoed = _token("LedgerVeto11111111111111111111111111111111", p=0.94, age_min=6, flags='["Same ticker launched repeatedly in 24h (copycat spam)"]')
        thin = _token("LedgerThin11111111111111111111111111111111", p=0.91, age_min=6)
        thin.outcome.last_liq = 900  # no liquid book yet, inside grace: wait
        session.add_all([buy, vetoed, thin])
        session.flush()
        for tok in (buy, vetoed, thin):
            upsert_hunt(session, tok)
        out = sync_paper_ledger(session, "sol", now=now)
        assert out["opened"] == 1
        fills = session.query(PaperFill).filter(PaperFill.line == PAPER_LINE).all()
        assert len(fills) == 1 and fills[0].mint == buy.mint and fills[0].status == "open"
        assert fills[0].entry_mcap == 70_000 and fills[0].entry_liq == 25_000
        gate = session.query(Decision).filter(Decision.kind == DECISION_GATE).all()
        assert {g.mint: g.veto for g in gate} == {buy.mint: "", vetoed.mint: "copycat spam"}
        # Second sync: nothing re-opens, the vetoed name stays vetoed, thin still waits.
        out2 = sync_paper_ledger(session, "sol", now=now + timedelta(minutes=1))
        assert out2["opened"] == 0 and session.query(PaperFill).filter(PaperFill.line == PAPER_LINE).count() == 1
        ticket = session.query(Ticket).one()
        assert ticket.mint == buy.mint and ticket.status == "shadow" and ticket.size_usd == ticket_size_usd(25_000) == 200.0
        # Tape: 2.4x liquid peak, then 24h passes on a 1.6x print.
        buy.outcome.last_mcap = 168_000
        buy.outcome.last_liq = 30_000
        upsert_hunt(session, buy, touch_updated=False)
        sync_paper_ledger(session, "sol", now=now + timedelta(minutes=30))
        fill = session.query(PaperFill).filter(PaperFill.line == PAPER_LINE).one()
        assert fill.status == "open" and fill.max_mcap == 168_000
        buy.outcome.last_mcap = 112_000
        upsert_hunt(session, buy, touch_updated=False)
        sync_paper_ledger(session, "sol", now=now + timedelta(hours=25))
        fill = session.query(PaperFill).filter(PaperFill.line == PAPER_LINE).one()
        assert fill.status == "closed" and fill.exit_reason == "24h"
        expected = moonbag_return(entry=70_000, peak=168_000, exit_mcap=112_000, target=2.0, ride=10.0, dead=False)
        assert fill.return_pct == round(expected * 100.0, 1)
        assert round(expected, 3) == round(0.5 * 1.0 + 0.5 * 0.6, 3)
        view = paper_ledger_view(session, "sol")
        assert view["ledger"] is True and view["closed_trades"] == 1 and view["wins"] == 1
        assert view["scorecard"]["leftover_clock"]["n"] == 1
        assert view["scorecard"]["this_window"]["n"] == 0
        assert view["closed"][0]["symbol"] == "LEDG" and view["vetoed"][0]["veto"] == "copycat spam"
        # Desk names the veto and shows the book it was judged on.
        assert view["vetoed"][0]["symbol"] == "LEDG" and view["vetoed"][0]["entry_mcap"] == 70_000
        rep = ticket_report(session, "sol", weeks=2)
        assert rep["weeks"][-1]["closed"] == 1 and rep["weeks"][-1]["hit2x_rate"] == 1.0
        tickets = list_tickets(session, "sol")
        assert tickets[0]["fill_status"] == "closed" and tickets[0]["peak_multiple"] == 2.4
        confirmed = set_ticket_status(session, ticket.id, "confirmed")
        assert confirmed["status"] == "confirmed" and confirmed["confirmed_at"]
        # Research card context: frozen entry, gate, fill and ticket — read-only.
        n_before = session.query(Decision).count()
        ctx = ledger_context(session, buy, now=now + timedelta(hours=25))
        # No first-score decision in this fixture (upsert_hunt only writes lines).
        assert ctx["entry"] is None
        assert [ln["line"] for ln in ctx["lines"]] == [DECISION_LINE70, DECISION_LINE90]
        assert ctx["gate"]["verdict"] == "fill" and ctx["gate"]["veto"] == ""
        assert ctx["fill"]["status"] == "closed" and ctx["fill"]["peak_multiple"] == 2.4 and ctx["fill"]["exit_reason"] == "24h"
        assert ctx["ticket"]["id"] == ticket.id and ctx["ticket"]["status"] == "confirmed" and ctx["ticket"]["fill_status"] == "closed"
        vetoed_ctx = ledger_context(session, vetoed, now=now)
        assert vetoed_ctx["gate"]["verdict"] == "veto" and vetoed_ctx["gate"]["veto"] == "copycat spam"
        assert vetoed_ctx["fill"] is None and vetoed_ctx["ticket"] is None
        assert session.query(Decision).count() == n_before  # card reads write nothing


def test_dead_pool_closes_fill_as_loss():
    init_db()
    now = utcnow()
    with session_scope() as session:
        buy = _token("LedgerDead11111111111111111111111111111111", p=0.92, age_min=6)
        session.add(buy)
        session.flush()
        upsert_hunt(session, buy)
        sync_paper_ledger(session, "sol", now=now)
        buy.outcome.last_mcap = 2_000
        buy.outcome.last_liq = 300
        session.query(HuntCard).filter(HuntCard.mint == buy.mint).update({"last_mcap": 2_000, "last_liq": 300})
        sync_paper_ledger(session, "sol", now=now + timedelta(hours=1))
        fill = session.query(PaperFill).filter(PaperFill.line == PAPER_LINE).one()
        assert fill.status == "closed" and fill.exit_reason == "dead pool" and fill.return_pct == -85.0


def test_paper_closes_a_run_on_live_dump():
    """KPORT-class: 7.5× peak, now 0.14×, Live dead — do not sit open until 24h."""
    init_db()
    now = utcnow()
    with session_scope() as session:
        buy = _token("LedgerDump11111111111111111111111111111111", p=0.92, age_min=6)
        session.add(buy)
        session.flush()
        upsert_hunt(session, buy)
        sync_paper_ledger(session, "sol", now=now)
        buy.outcome.last_mcap = 525_000
        buy.outcome.max_mcap = 525_000
        buy.outcome.last_liq = 80_000
        session.query(HuntCard).filter(HuntCard.mint == buy.mint).update(
            {"last_mcap": 525_000, "last_liq": 80_000, "conviction_p": 0.90}
        )
        sync_paper_ledger(session, "sol", now=now + timedelta(minutes=30))
        fill = session.query(PaperFill).filter(PaperFill.line == PAPER_LINE).one()
        assert fill.status == "open" and fill.max_mcap == 525_000
        buy.outcome.last_mcap = 9_800
        buy.outcome.last_liq = 40_000
        session.query(HuntCard).filter(HuntCard.mint == buy.mint).update(
            {"last_mcap": 9_800, "last_liq": 40_000, "conviction_p": 0.20}
        )
        sync_paper_ledger(session, "sol", now=now + timedelta(hours=2))
        fill = session.query(PaperFill).filter(PaperFill.line == PAPER_LINE).one()
        assert fill.status == "closed" and fill.exit_reason == "live dump"


def test_paper_live_dumps_after_the_hunt_card_leaves():
    """OAK-class: the run dumped after Hunt dropped the card. Do not wait for 24h."""
    init_db()
    now = utcnow()
    with session_scope() as session:
        buy = _token("0x96c3a917e3e9ae4decdcd66e4439ae4be1481fba", p=0.92, chain="robinhood", age_min=6)
        session.add(buy)
        session.flush()
        upsert_hunt(session, buy)
        sync_paper_ledger(session, "robinhood", now=now)
        buy.outcome.last_mcap = 210_000
        buy.outcome.max_mcap = 210_000
        buy.outcome.last_liq = 40_000
        session.query(HuntCard).filter(HuntCard.mint == buy.mint).update(
            {"last_mcap": 210_000, "last_liq": 40_000, "conviction_p": 0.80}
        )
        sync_paper_ledger(session, "robinhood", now=now + timedelta(hours=2))
        fill = session.query(PaperFill).filter(PaperFill.line == "gated90").one()
        assert fill.status == "open" and fill.max_mcap == 210_000
        session.query(HuntCard).filter(HuntCard.mint == buy.mint).delete()
        buy.outcome.last_mcap = 3_939
        buy.outcome.last_liq = 4_098
        sync_paper_ledger(session, "robinhood", now=now + timedelta(hours=14))
        fill = session.query(PaperFill).filter(PaperFill.line == "gated90").one()
        assert fill.status == "closed" and fill.exit_reason == "live dump"
        assert session.query(HuntCard).count() == 0


def test_paper_live_dump_ignores_a_stale_high_conviction():
    """Hunt card still says 0.90 after the book is dust. Tape Live is 0."""
    init_db()
    now = utcnow()
    with session_scope() as session:
        buy = _token("0x96c3a917e3e9ae4decdcd66e4439ae4be1481fba", p=0.92, chain="robinhood", age_min=6)
        buy.outcome.t0_mcap = 25_712
        buy.outcome.last_mcap = 25_712
        session.add(buy)
        session.flush()
        upsert_hunt(session, buy)
        sync_paper_ledger(session, "robinhood", now=now)
        buy.outcome.last_mcap = 75_903
        buy.outcome.max_mcap = 75_903
        buy.outcome.last_liq = 40_000
        session.query(HuntCard).filter(HuntCard.mint == buy.mint).update(
            {"last_mcap": 75_903, "last_liq": 40_000, "conviction_p": 0.90}
        )
        sync_paper_ledger(session, "robinhood", now=now + timedelta(hours=2))
        fill = session.query(PaperFill).filter(PaperFill.line == "gated90").one()
        assert fill.status == "open"
        buy.outcome.last_mcap = 3_939
        buy.outcome.last_liq = 4_098
        session.query(HuntCard).filter(HuntCard.mint == buy.mint).update(
            {"last_mcap": 3_939, "last_liq": 4_098, "conviction_p": 0.90}
        )
        sync_paper_ledger(session, "robinhood", now=now + timedelta(hours=14))
        fill = session.query(PaperFill).filter(PaperFill.line == "gated90").one()
        assert fill.status == "closed" and fill.exit_reason == "live dump"


def test_paper_off_hunt_hold_stays_open():
    """A runner still near its peak does not dump just because the card left Hunt."""
    init_db()
    now = utcnow()
    with session_scope() as session:
        buy = _token("LedgerHold11111111111111111111111111111111", p=0.92, age_min=6)
        session.add(buy)
        session.flush()
        upsert_hunt(session, buy)
        sync_paper_ledger(session, "sol", now=now)
        buy.outcome.last_mcap = 200_000
        buy.outcome.max_mcap = 210_000
        buy.outcome.last_liq = 40_000
        session.query(HuntCard).filter(HuntCard.mint == buy.mint).update(
            {"last_mcap": 200_000, "last_liq": 40_000, "conviction_p": 0.70}
        )
        sync_paper_ledger(session, "sol", now=now + timedelta(hours=2))
        session.query(HuntCard).filter(HuntCard.mint == buy.mint).delete()
        sync_paper_ledger(session, "sol", now=now + timedelta(hours=14))
        fill = session.query(PaperFill).filter(PaperFill.line == PAPER_LINE).one()
        assert fill.status == "open"


def test_paper_closes_no_run_grave_before_24h():
    """CATFLIX-class: never 1.5×, last already 0.01× — do not sit open until 24h."""
    init_db()
    now = utcnow()
    with session_scope() as session:
        buy = _token("LedgerNoRun1111111111111111111111111111111", p=0.92, age_min=6)
        session.add(buy)
        session.flush()
        upsert_hunt(session, buy)
        sync_paper_ledger(session, "sol", now=now)
        buy.outcome.last_mcap = 1_650
        buy.outcome.last_liq = 12_000
        session.query(HuntCard).filter(HuntCard.mint == buy.mint).update(
            {"last_mcap": 1_650, "last_liq": 12_000, "conviction_p": 0.20}
        )
        sync_paper_ledger(session, "sol", now=now + timedelta(minutes=30))
        fill = session.query(PaperFill).filter(PaperFill.line == PAPER_LINE).one()
        assert fill.status == "open"
        sync_paper_ledger(session, "sol", now=now + timedelta(hours=1, minutes=5))
        fill = session.query(PaperFill).filter(PaperFill.line == PAPER_LINE).one()
        assert fill.status == "closed" and fill.exit_reason == "no run"
        assert fill.return_pct < 0
        view = paper_ledger_view(session, "sol")
        assert view["scorecard"]["no_run"]["n"] == 1
        assert view["scorecard"]["this_window"]["n"] == 0
        assert view["scorecard"]["leftover_clock"]["n"] == 0


def test_paper_no_run_leaves_alive_book_and_dump_owns_runners():
    """A 1.2× book at 2h stays open; a 7.5× dump is still live dump."""
    init_db()
    now = utcnow()
    with session_scope() as session:
        alive = _token("LedgerAlive1111111111111111111111111111111", p=0.92, age_min=6)
        session.add(alive)
        session.flush()
        upsert_hunt(session, alive)
        sync_paper_ledger(session, "sol", now=now)
        alive.outcome.last_mcap = 84_000
        alive.outcome.max_mcap = 84_000
        alive.outcome.last_liq = 20_000
        session.query(HuntCard).filter(HuntCard.mint == alive.mint).update(
            {"last_mcap": 84_000, "last_liq": 20_000, "conviction_p": 0.40}
        )
        sync_paper_ledger(session, "sol", now=now + timedelta(hours=2))
        fill = session.query(PaperFill).filter(PaperFill.line == PAPER_LINE).one()
        assert fill.status == "open" and fill.exit_reason in (None, "")


def test_hunt_board_marks_a_capped_live_and_token_detail_carries_the_ledger():
    """RH desk was a wall of 62s with nothing saying they were capped."""
    import asyncio

    from launchfinder.app import _hunt_board_sync, get_token
    from launchfinder.scoring.hunt import HUNT_THIN_WATCH_LIVE_CAP

    init_db()
    with session_scope() as session:
        fat = _token("0xthinwatchleftover0000000000000000000000001", p=0.04, chain="robinhood", age_min=40, holders=660, flags='["Watch preview was already thin"]')
        fat.outcome.t0_mcap = 50_381
        fat.outcome.last_mcap = 476_791
        fat.outcome.max_mcap = 476_791
        fat.outcome.last_liq = 189_822
        fat.outcome.multiple = 9.46
        dust = _token("0xthindustleftover0000000000000000000000002", p=0.04, chain="robinhood", age_min=40, holders=8, flags='["Watch preview was already thin"]')
        dust.outcome.t0_mcap = 20_000
        dust.outcome.last_mcap = 8_000
        dust.outcome.max_mcap = 20_000
        dust.outcome.last_liq = 2_000
        dust.outcome.multiple = 0.4
        real = _token("LedgerReal11111111111111111111111111111111", p=0.68, age_min=40, holders=397)
        real.outcome.t0_mcap = 43_843
        real.outcome.last_mcap = 98_186
        real.outcome.max_mcap = 98_186
        real.outcome.multiple = 2.24
        session.add_all([fat, dust, real])
        session.flush()
        upsert_hunt(session, fat)
        upsert_hunt(session, dust)
        upsert_hunt(session, real)
        record_entry_decision(
            session,
            real,
            real.research,
            {"p_good": 0.68, "heuristic_p": 0.68, "model_p": 0.68, "risk_flags": []},
            market={"mcap_usd": 43_843, "liquidity_usd": 25_000},
            holder_count=397,
        )
        fat_mint, dust_mint, real_mint = fat.mint, dust.mint, real.mint
    rh = {c["mint"]: c for c in _hunt_board_sync("robinhood", 80, 12.0)["items"]}
    sol = {c["mint"]: c for c in _hunt_board_sync("sol", 80, 18.0)["items"]}
    assert rh[fat_mint]["live_cap"] is None and rh[fat_mint]["conviction_p"] > HUNT_THIN_WATCH_LIVE_CAP
    assert rh[dust_mint]["live_cap"] == HUNT_THIN_WATCH_LIVE_CAP
    assert rh[dust_mint]["conviction_p"] <= HUNT_THIN_WATCH_LIVE_CAP
    assert sol[real_mint]["live_cap"] is None and sol[real_mint]["conviction_p"] > HUNT_THIN_WATCH_LIVE_CAP
    card = asyncio.run(get_token(real_mint))
    assert card["ledger"]["entry"]["entry_p"] == 0.68 and card["ledger"]["entry"]["source"] == "live"
    assert card["ledger"]["gate"] is None and card["ledger"]["fill"] is None
    bare = asyncio.run(get_token(fat_mint))
    assert bare["ledger"] == {"entry": None, "lines": [], "gate": None, "fill": None, "ticket": None}


def test_api_gated_paper_reads_the_ledger_and_health_shows_loops():
    import asyncio

    from launchfinder.app import health, paper_ledger
    from launchfinder.ledger import beat

    init_db()
    with session_scope() as session:
        beat(session, "hunt_tape", note="cycle 3")
    out = asyncio.run(paper_ledger(min_p=0.9, target=2.0, ride=10.0, strategy="moonbag", gated=True))
    assert out["ledger"] is True and out["gated"] is True and "vetoed" in out
    legacy = asyncio.run(paper_ledger(min_p=0.9, target=2.0, ride=10.0, strategy="moonbag", gated=True, ledger=False))
    assert "ledger" not in legacy
    h = asyncio.run(health())
    assert h["tickets"] is True
    assert "hunt_tape" in h["loops"] and h["loops"]["hunt_tape"]["age_s"] is not None
    assert set(h["ledger"]) == {"decisions", "paper_fills", "tickets"}


def test_ticket_sizing_is_a_slice_of_the_pool():
    assert ticket_size_usd(0) == 0.0
    assert ticket_size_usd(1_000) == 20.0
    assert ticket_size_usd(25_000) == 200.0
    assert ticket_size_usd(10_000) == 100.0
    assert ticket_size_usd(1_000_000) == 200.0
    assert 0 < ticket_slippage_pct(200.0, 25_000) < 2.0


def test_moonbag_return_paths():
    assert moonbag_return(entry=100, peak=250, exit_mcap=120, target=2, ride=10, dead=False) == 0.5 * 1.0 + 0.5 * 0.2
    assert moonbag_return(entry=100, peak=1_200, exit_mcap=900, target=2, ride=10, dead=False) == 0.5 * 1.0 + 0.5 * 9.0
    assert moonbag_return(entry=100, peak=150, exit_mcap=40, target=2, ride=10, dead=False) == -0.6
    assert moonbag_return(entry=100, peak=300, exit_mcap=0, target=2, ride=10, dead=True) == 0.5 * 1.0 + 0.5 * -0.85


def test_honest_calibration_defaults_to_a_bounded_window():
    from launchfinder.db import apply_report_guards
    from launchfinder.ledger import CALIBRATION_DEFAULT_DAYS, REPORT_DECISION_CAP

    init_db()
    now = utcnow()
    with session_scope() as session:
        apply_report_guards(session)
        fresh = _token("CalFresh11111111111111111111111111111111", p=0.91, age_min=60 * 30)
        old = _token("CalOld2222222222222222222222222222222222", p=0.92, age_min=60 * 30)
        fresh.outcome = Outcome(t0_mcap=69_000, max_mcap=200_000, last_mcap=180_000, last_liq=40_000, t24h_mcap=180_000, multiple=2.6, label=1)
        old.outcome = Outcome(t0_mcap=69_000, max_mcap=200_000, last_mcap=180_000, last_liq=40_000, t24h_mcap=180_000, multiple=2.6, label=1)
        session.add_all([fresh, old])
        session.flush()
        session.add(
            Decision(
                chain="sol",
                mint=fresh.mint,
                token_id=fresh.id,
                kind=DECISION_ENTRY,
                at=now - timedelta(hours=30),
                entry_p=0.91,
                entry_mcap=69_000,
                liq=30_000,
            )
        )
        session.add(
            Decision(
                chain="sol",
                mint=old.mint,
                token_id=old.id,
                kind=DECISION_ENTRY,
                at=now - timedelta(days=CALIBRATION_DEFAULT_DAYS + 10),
                entry_p=0.92,
                entry_mcap=69_000,
                liq=30_000,
            )
        )
        session.flush()
        cal = honest_calibration(session, "sol")
        assert cal["window_days"] == CALIBRATION_DEFAULT_DAYS
        assert cal["decision_cap"] == REPORT_DECISION_CAP
        assert cal["resolved"] == 1
        wide = honest_calibration(session, "sol", since=now - timedelta(days=60), window_days=60)
        assert wide["resolved"] == 2
        wk = honest_weekly(session, "sol", weeks=8)
        assert wk["decision_cap"] == REPORT_DECISION_CAP
        assert "truncated" in wk
