import json
from datetime import timedelta

import numpy as np

from launchfinder.db import init_db, session_scope
from launchfinder.ledger import DECISION_ENTRY
from launchfinder.models import Decision, LiveSample, ModelArtifact, Outcome, Research, TapeBar, Token, utcnow
from launchfinder.scoring.batch_fit import (
    MIN_ROWS,
    apply_calibration,
    auc,
    demote_unfrozen_fits,
    fit_entry_model,
    fit_logistic,
    has_promoted,
    isotonic_fit,
    latest_promoted,
    metrics,
    promoted_calibration,
    reset_artifact_cache,
    should_promote,
    time_split,
    training_rows,
)
from launchfinder.scoring.features import FEATURE_NAMES
from launchfinder.scoring.live_fit import (
    LIVE_FEATURES,
    RUNNER_KIND,
    _write_live_runner_shadow,
    apply_vendor_candles,
    fit_live_model,
    live_features,
    live_fit_rows,
    live_label,
    live_label_runner,
    live_model_p,
    live_promote_ok,
    print_from_candles,
    sample_live,
    sample_live_history,
    vendor_candidates,
)
from launchfinder.scoring.model import get_or_create_model, predict, train_pending


def test_feature_names_still_66():
    assert len(FEATURE_NAMES) == 66


def test_isotonic_is_monotone_and_clipped():
    scores = np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95])
    labels = np.array([0, 0, 1, 0, 0, 1, 1, 0, 1, 1])
    cal = isotonic_fit(scores, labels)
    ys = [row[1] for row in cal]
    assert ys == sorted(ys)
    assert all(0.01 <= y <= 0.99 for y in ys)
    assert apply_calibration(cal, 0.0) == ys[0]
    assert apply_calibration(cal, 1.0) == ys[-1]
    assert apply_calibration(None, 0.42) == 0.42


def test_logistic_recovers_a_separable_direction():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(600, 3))
    y = (X[:, 0] * 2.0 - X[:, 1] > 0).astype(float)
    w, b = fit_logistic(X, y, l2=0.1)
    assert w[0] > 0 and w[1] < 0 and abs(w[2]) < abs(w[0])
    p = 1 / (1 + np.exp(-(X @ w + b)))
    assert auc(p, y) > 0.95
    m = metrics(p, y)
    assert m["n"] == 600 and m["precision_top_decile"] >= 0.95


def test_time_split_never_shuffles():
    rows = [{"at": i, "x": [0.0], "y": 0} for i in range(1000)]
    train, valid = time_split(rows)
    assert len(valid) == 200 and train[-1]["at"] < valid[0]["at"]


def test_should_promote_requires_better_brier_and_held_ranking():
    inc = {"brier": 0.20, "auc": 0.70, "precision_top_decile": 0.50}
    ok, _ = should_promote({"n": 100, "positives": 30, "brier": 0.15, "auc": 0.71, "precision_top_decile": 0.52}, inc)
    assert ok
    ok, why = should_promote({"n": 100, "positives": 30, "brier": 0.15, "auc": 0.60, "precision_top_decile": 0.52}, inc)
    assert not ok and "auc" in why
    ok, why = should_promote({"n": 100, "positives": 30, "brier": 0.25, "auc": 0.75, "precision_top_decile": 0.60}, inc)
    assert not ok and "brier" in why
    ok, why = should_promote({"n": 100, "positives": 3, "brier": 0.10, "auc": 0.9, "precision_top_decile": 0.9}, inc)
    assert not ok and "positives" in why


def _seed_decisions(session, n: int, *, chain: str = "sol", start_hours: float = 200.0):
    """Synthetic frozen decisions: holder_n drives the 2x outcome; the incumbent
    online model has the sign wrong on it."""
    rng = np.random.default_rng(1)
    now = utcnow()
    for i in range(n):
        holder = float(rng.uniform(0, 1))
        liq = float(rng.uniform(0, 1))
        win = rng.uniform() < (0.05 + 0.6 * holder)
        tok = Token(
            mint=f"Fit{chain[:1]}{i:037d}",
            symbol=f"F{i}",
            chain=chain,
            first_seen_at=now - timedelta(hours=start_hours - i * 0.1),
            migrated_at=now - timedelta(hours=start_hours - i * 0.1),
            source="poll",
        )
        feats = {name: 0.0 for name in FEATURE_NAMES}
        feats["holder_n"] = holder
        feats["liquidity_n"] = liq
        tok.research = Research(features_json=json.dumps(feats), p_good=0.5, holder_count=100)
        tok.outcome = Outcome(
            t0_mcap=69_000,
            max_mcap=200_000 if win else 80_000,
            last_mcap=150_000 if win else 30_000,
            last_liq=25_000,
            t24h_mcap=150_000 if win else 30_000,
            label=1 if win else 0,
        )
        session.add(tok)
        session.flush()
        session.add(
            Decision(
                chain=chain,
                mint=tok.mint,
                token_id=tok.id,
                kind=DECISION_ENTRY,
                at=now - timedelta(hours=start_hours - i * 0.1),
                entry_p=0.5,
                entry_mcap=69_000,
                liq=20_000,
                holders=100,
                features_json=json.dumps(feats),
            )
        )
    session.flush()


def test_fit_entry_model_promotes_over_a_wrong_sign_incumbent_and_freezes_sgd():
    init_db()
    reset_artifact_cache()
    with session_scope() as session:
        model = get_or_create_model(session, chain="sol")
        weights = {name: 0.0 for name in FEATURE_NAMES}
        weights["holder_n"] = -3.0
        model.weights_json = json.dumps(weights)
        model.bias = 0.0
        model.n_train = 5000
        session.flush()
        _seed_decisions(session, MIN_ROWS + 100)
        rows = training_rows(session, "sol")
        assert len(rows) == MIN_ROWS + 100 and 0.2 < sum(r["y"] for r in rows) / len(rows) < 0.6
        out = fit_entry_model(session, "sol")
        assert out["fitted"] and out["promoted"], out
        assert out["candidate"]["auc"] > out["incumbent"]["auc"]
        assert out["candidate"]["brier"] < out["incumbent"]["brier"]
        art = session.query(ModelArtifact).filter(ModelArtifact.promoted.is_(True)).one()
        assert art.kind == "entry" and art.version == 1
        model = get_or_create_model(session, chain="sol")
        assert json.loads(model.weights_json)["holder_n"] > 0
        assert has_promoted(session, "sol")
        cal = promoted_calibration(session, "sol")
        assert cal and [c[1] for c in cal] == sorted(c[1] for c in cal)
        # predict() now returns the calibrated model_p; a strong holder book
        # scores above a weak one.
        strong = {name: 0.0 for name in FEATURE_NAMES}
        strong["holder_n"] = 0.95
        weak = dict(strong)
        weak["holder_n"] = 0.05
        assert predict(session, strong, chain="sol")["model_p"] > predict(session, weak, chain="sol")["model_p"]
        # Online SGD is frozen: a pending label no longer moves the weights.
        before = model.weights_json
        tok = session.query(Token).first()
        tok.outcome.used_for_train = False
        trained = train_pending(session)
        assert trained == 0 and tok.outcome.used_for_train is True
        assert get_or_create_model(session, chain="sol").weights_json == before
        # A second fit that is not better is stored but not promoted.
        model.weights_json = art.weights_json
        model.bias = art.bias
        out2 = fit_entry_model(session, "sol")
        assert out2["fitted"]
        assert session.query(ModelArtifact).count() == 2


def test_fit_entry_model_needs_rows():
    init_db()
    reset_artifact_cache()
    with session_scope() as session:
        out = fit_entry_model(session, "sol")
        assert out["fitted"] is False and "rows" in out["reason"]


def test_training_rows_are_live_only_and_seed_fits_are_demoted():
    """Live RH v4 fit: AUC 0.94 / 99.8% hit2x at 90 on seed_t0 rows whose
    features_json was the repaired, current vector. The board hit 22%."""
    init_db()
    reset_artifact_cache()
    with session_scope() as session:
        _seed_decisions(session, 6)
        rows = session.query(Decision).order_by(Decision.id).all()
        # Two rows came from the t0 backfill; their features know the answer.
        for d in rows[:2]:
            d.source = "seed_t0"
        session.flush()
        trained = training_rows(session, "sol")
        assert len(trained) == 4 and all(r["source"] == "live" for r in trained)
        assert any(r["x"][FEATURE_NAMES.index("holder_n")] > 0 for r in trained)

        # A promoted artifact that predates the rule is unwound; a live-only
        # one on another chain is left alone.
        model = get_or_create_model(session, chain="robinhood")
        weights = {name: 0.0 for name in FEATURE_NAMES}
        weights["holder_n"] = 0.7
        model.weights_json = json.dumps(weights)
        session.add(
            ModelArtifact(
                chain="robinhood",
                kind="entry",
                version=4,
                weights_json=model.weights_json,
                bias=0.0,
                calibration_json="[]",
                n_train=20_696,
                n_valid=5_174,
                metrics_json=json.dumps({"auc": 0.9387, "hit2x_at_90": 0.9978, "promote_reason": "better brier, ranking held"}),
                incumbent_json="{}",
                promoted=True,
            )
        )
        session.add(
            ModelArtifact(
                chain="sol",
                kind="entry",
                version=1,
                weights_json="{}",
                bias=0.0,
                calibration_json="[]",
                n_train=500,
                n_valid=100,
                metrics_json=json.dumps({"auc": 0.7, "live_only": True}),
                incumbent_json="{}",
                promoted=True,
            )
        )
        session.flush()
        reset_artifact_cache()
        assert has_promoted(session, "robinhood")
        demoted = demote_unfrozen_fits(session)
        assert demoted == [{"chain": "robinhood", "kind": "entry", "version": 4}]
        assert not has_promoted(session, "robinhood")
        assert has_promoted(session, "sol")
        assert json.loads(get_or_create_model(session, chain="robinhood").weights_json)["holder_n"] == 0.7
        assert demote_unfrozen_fits(session) == []
        # Online SGD is unfrozen again for RH.
        tok = session.query(Token).first()
        tok.chain = "robinhood"
        tok.outcome.used_for_train = False
        session.flush()
        assert train_pending(session) == 1


def test_live_features_and_label():
    f = live_features(entry_p=0.9, entry_mcap=100_000, entry_liq=20_000, entry_holders=100, mcap=150_000, liq=40_000, vol_h1=30_000, holders=180, peak_before=160_000, trough_before=90_000)
    assert f["log_mult"] > 0 and f["liq_ratio"] > 0 and f["holder_growth"] > 0 and f["drawdown"] < 0
    assert set(LIVE_FEATURES) <= set(f)
    s = LiveSample(at=utcnow() - timedelta(hours=30), mcap_usd=150_000, peak_before=160_000)
    # A lifetime high alone never vouches: it has no timestamp (v80).
    assert live_label(s, Outcome(max_mcap=400_000, last_liq=30_000, label=1)) == 0
    # A sellable print we saw after the sample does.
    assert live_label(s, Outcome(max_mcap=400_000, last_liq=30_000, label=1), peak_after=(400_000.0, 3)) == 1
    # ...and so does the last Dex look on a sellable pool, or the 24h print.
    assert live_label(s, Outcome(max_mcap=400_000, last_mcap=310_000, last_liq=30_000, label=1)) == 1
    assert live_label(s, Outcome(max_mcap=160_000, last_liq=30_000, t24h_mcap=120_000, label=0)) == 0
    assert live_label(s, Outcome(max_mcap=160_000, last_liq=30_000, t24h_mcap=320_000, label=0)) == 1
    # A print on a thin pool is not a sale.
    assert live_label(s, Outcome(max_mcap=400_000, last_mcap=400_000, last_liq=3_000, label=0)) == 0
    assert live_label(s, Outcome(max_mcap=400_000, last_liq=200, label=0), peak_after=(400_000.0, 3)) == 0
    young = LiveSample(at=utcnow() - timedelta(minutes=30), mcap_usd=150_000, peak_before=160_000)
    assert live_label(young, Outcome(max_mcap=400_000, last_liq=30_000), peak_after=(400_000.0, 3)) is None


def test_runner_label_is_five_x_or_held_two_x_at_six_hours():
    """The 2x head pays a wick. The runner head pays a run or a hold."""
    s = LiveSample(at=utcnow() - timedelta(hours=30), mcap_usd=100_000, peak_before=110_000)
    live = Outcome(last_liq=30_000, label=1)
    # A 2x wick that came back is a Live win and not a runner.
    assert live_label(s, live, peak_after=(210_000.0, 4)) == 1
    assert live_label_runner(s, live, peak_after=(210_000.0, 4)) == 0
    # A sellable 5x print we saw is a runner.
    assert live_label_runner(s, live, peak_after=(500_000.0, 4)) == 1
    # A lifetime high alone still never vouches.
    assert live_label_runner(s, Outcome(max_mcap=900_000, last_liq=30_000, label=1)) == 0
    # Still 2x at the tracker's 6h print is a hold, not a wick.
    assert live_label_runner(s, Outcome(last_liq=30_000, t6h_mcap=210_000, label=1), peak_after=(220_000.0, 4)) == 1
    assert live_label_runner(s, Outcome(last_liq=30_000, t6h_mcap=150_000, label=1), peak_after=(220_000.0, 4)) == 0
    # Dead pool at judgement is 0 whatever it printed on the way.
    assert live_label_runner(s, Outcome(last_liq=200, t6h_mcap=300_000, label=0), peak_after=(600_000.0, 4)) == 0
    # Still open: no label.
    young = LiveSample(at=utcnow() - timedelta(minutes=30), mcap_usd=100_000)
    assert live_label_runner(young, Outcome(last_liq=30_000), peak_after=(600_000.0, 4)) is None


def test_live_runner_shadow_is_written_never_promoted_and_reads_on_the_board():
    """Shadow artifact per Live fit; the artifacts endpoint serves it."""
    from fastapi.testclient import TestClient

    from launchfinder.app import app

    init_db()
    reset_artifact_cache()
    width = len(LIVE_FEATURES)

    def row(i, y_run):
        x = [0.1] * width
        x[1] = 0.9 if y_run else -0.4  # log_mult separates the runners
        return {"x": x, "y": 1, "y_run": y_run, "entry_p": 0.3, "source": "tape"}

    train = [row(i, 1 if i % 3 == 0 else 0) for i in range(120)]
    valid = [row(i, 1 if i % 3 == 0 else 0) for i in range(90)]
    with session_scope() as session:
        out = _write_live_runner_shadow(session, "sol", train, valid, utcnow(), mix="path")
        assert out["fitted"] is True and out["promoted"] is False
        assert out["auc"] is not None and out["auc"] > 0.9
        art = session.query(ModelArtifact).filter(ModelArtifact.kind == RUNNER_KIND).one()
        assert art.promoted is False and art.n_train == 120 and art.n_valid == 90
        m = json.loads(art.metrics_json)
        assert m["base_rate"] > 0.3 and m["fit_rows"] == "path" and "6h print" in m["label"]
        assert set(json.loads(art.weights_json)) == set(LIVE_FEATURES)
        # Never Live: the promoted-live lookup does not see it.
        assert latest_promoted(session, "sol", "live") is None
        # Too few runner positives: no artifact, honest reason.
        thin = _write_live_runner_shadow(session, "robinhood", train[:30], valid[:30], utcnow())
        assert thin["fitted"] is False and "runner positives" in thin["reason"]
        assert session.query(ModelArtifact).filter(ModelArtifact.kind == RUNNER_KIND, ModelArtifact.chain == "robinhood").count() == 0
    client = TestClient(app)
    card = client.get("/api/model/artifacts?chain=sol&kind=live_runner").json()
    assert card["shadow"] is True and card["promoted_version"] is None
    assert card["artifacts"][0]["promoted"] is False and card["artifacts"][0]["base_rate"] > 0.3
    assert card["artifacts"][0]["share_ge_020"] is not None
    assert "6h print" in card["label"]
    assert client.get("/api/model/artifacts?chain=sol&kind=ran_shadow").status_code == 200
    assert client.get("/api/model/artifacts?chain=sol&kind=bogus").status_code == 422


def test_sample_live_cuts_one_row_from_bars_and_fits_when_enough():
    init_db()
    reset_artifact_cache()
    now = utcnow()
    with session_scope() as session:
        tok = Token(mint="LiveSamp111111111111111111111111111111111", symbol="LS", chain="sol", first_seen_at=now - timedelta(minutes=20), migrated_at=now - timedelta(minutes=20), source="poll")
        tok.research = Research(features_json="{}", p_good=0.8, holder_count=90)
        tok.outcome = Outcome(t0_mcap=69_000, max_mcap=90_000, last_mcap=90_000, last_liq=30_000)
        session.add(tok)
        session.flush()
        at = now - timedelta(minutes=20)
        session.add(Decision(chain="sol", mint=tok.mint, token_id=tok.id, kind=DECISION_ENTRY, at=at, entry_p=0.8, entry_mcap=69_000, liq=20_000, holders=90, source="live"))
        for m, mcap in ((1, 70_000), (5, 60_000), (10, 80_000), (15, 90_000), (16, 95_000)):
            session.add(TapeBar(chain="sol", mint=tok.mint, token_id=tok.id, minute=at + timedelta(minutes=m), mcap_usd=mcap, liquidity_usd=30_000, volume_h1=12_000, holders=120))
        session.flush()
        assert sample_live(session, "sol", now=now) == 1
        assert sample_live(session, "sol", now=now) == 0
        s = session.query(LiveSample).one()
        assert s.mcap_usd == 90_000 and s.peak_before == 90_000 and s.trough_before == 60_000 and s.minutes_after == 15.0
        feats = json.loads(s.features_json)
        assert feats["holder_growth"] > 0 and feats["drawdown"] < 0
        assert live_model_p(session, "sol", feats) is None
        assert feats["source"] == "tape"
        out = fit_live_model(session, "sol")
        assert out["fitted"] is False


def test_sample_live_falls_back_to_the_t15m_snapshot_when_the_tape_never_reached_the_mint():
    """Live 07:20 (v76): 106 Sol samples in days against ~900 first sights a
    day — the Hunt tape covers 80 cards, the t15m snapshot covers every
    non-backfill token. No bars: sample from that print, measured on the
    two ends we hold; a late snapshot (40 min) still counts, with the true gap."""
    from launchfinder.models import Snapshot
    from launchfinder.scoring.live_fit import live_sample_counts

    init_db()
    reset_artifact_cache()
    now = utcnow()
    with session_scope() as session:
        toks = []
        for i, (age_min, snap_min, snap_mcap) in enumerate(((20, 16, 120_000), (45, 40, 30_000), (20, 5, 80_000), (20, None, 0))):
            at = now - timedelta(minutes=age_min)
            tok = Token(mint=f"LiveSnap{i}1111111111111111111111111111111", symbol=f"S{i}", chain="sol", first_seen_at=at, migrated_at=at, source="poll")
            tok.research = Research(features_json="{}", p_good=0.6, holder_count=50)
            tok.outcome = Outcome(t0_mcap=60_000, max_mcap=60_000, last_mcap=60_000, last_liq=20_000)
            tok.snapshots.append(Snapshot(kind="t0", mcap_usd=60_000, liquidity_usd=15_000, taken_at=at))
            if snap_min is not None:
                tok.snapshots.append(Snapshot(kind="t15m", mcap_usd=snap_mcap, liquidity_usd=18_000, volume_h1=9_000, taken_at=at + timedelta(minutes=snap_min)))
            session.add(tok)
            session.flush()
            session.add(Decision(chain="sol", mint=tok.mint, token_id=tok.id, kind=DECISION_ENTRY, at=at, entry_p=0.6, entry_mcap=60_000, liq=15_000, holders=50, source="live"))
            toks.append(tok)
        session.flush()
        # Rows 0 and 1 sample from their t15m print; row 2's snapshot is too early
        # (5 min is not a t+15 print); row 3 has no print at all.
        assert sample_live(session, "sol", now=now) == 2
        assert sample_live(session, "sol", now=now) == 0
        samples = {s.mint: s for s in session.query(LiveSample).all()}
        s0 = samples[toks[0].mint]
        assert s0.mcap_usd == 120_000 and s0.minutes_after == 16.0 and s0.peak_before == 120_000 and s0.trough_before == 60_000 and s0.holders == 0
        f0 = json.loads(s0.features_json)
        assert f0["source"] == "t15m" and f0["log_mult"] > 0 and f0["runup"] > 0
        s1 = samples[toks[1].mint]
        assert s1.mcap_usd == 30_000 and s1.minutes_after == 40.0 and s1.peak_before == 60_000 and s1.trough_before == 30_000
        assert json.loads(s1.features_json)["drawdown"] < 0
        assert toks[2].mint not in samples and toks[3].mint not in samples
        counts = live_sample_counts(session, "sol")
        assert counts["samples"] == 2 and counts["resolved"] == 0  # both still open (< 24h)


def test_sample_live_keeps_a_holder_count_the_tape_already_wrote():
    """The t+15 bar can store 0 holders. An earlier bar in the window is the count."""
    from launchfinder.models import Snapshot
    from launchfinder.scoring.live_fit import repair_live_sample_holders

    init_db()
    now = utcnow()
    with session_scope() as session:
        at = now - timedelta(minutes=20)
        tok = Token(mint="LiveHold11111111111111111111111111111111", symbol="LH", chain="sol", first_seen_at=at, migrated_at=at, source="poll")
        tok.research = Research(features_json="{}", p_good=0.7, holder_count=400)
        tok.outcome = Outcome(t0_mcap=60_000, max_mcap=70_000, last_mcap=70_000, last_liq=20_000)
        session.add(tok)
        session.flush()
        session.add(Decision(chain="sol", mint=tok.mint, token_id=tok.id, kind=DECISION_ENTRY, at=at, entry_p=0.7, entry_mcap=60_000, liq=15_000, holders=40, source="live"))
        session.add(TapeBar(chain="sol", mint=tok.mint, token_id=tok.id, minute=at + timedelta(minutes=8), mcap_usd=55_000, liquidity_usd=18_000, holders=140))
        session.add(TapeBar(chain="sol", mint=tok.mint, token_id=tok.id, minute=at + timedelta(minutes=15), mcap_usd=70_000, liquidity_usd=20_000, holders=0))
        session.flush()
        assert sample_live(session, "sol", now=now) == 1
        s = session.query(LiveSample).one()
        assert s.holders == 140 and s.mcap_usd == 70_000
        assert json.loads(s.features_json)["source"] == "tape"
        assert json.loads(s.features_json)["holders_n"] > 0
        # A t15m print with bars before it keeps the path and the count.
        # Current research.holder_count (400) is not the sample.
        at2 = now - timedelta(minutes=30)
        tok2 = Token(mint="LiveHold21111111111111111111111111111111", symbol="L2", chain="sol", first_seen_at=at2, migrated_at=at2, source="poll")
        tok2.research = Research(features_json="{}", p_good=0.6, holder_count=400)
        tok2.outcome = Outcome(t0_mcap=50_000, max_mcap=50_000, last_mcap=50_000, last_liq=12_000)
        tok2.snapshots.append(Snapshot(kind="t15m", mcap_usd=80_000, liquidity_usd=16_000, volume_h1=4_000, taken_at=at2 + timedelta(minutes=16)))
        session.add(tok2)
        session.flush()
        session.add(Decision(chain="sol", mint=tok2.mint, token_id=tok2.id, kind=DECISION_ENTRY, at=at2, entry_p=0.6, entry_mcap=50_000, liq=12_000, holders=30, source="live"))
        session.add(TapeBar(chain="sol", mint=tok2.mint, token_id=tok2.id, minute=at2 + timedelta(minutes=6), mcap_usd=40_000, liquidity_usd=14_000, holders=88))
        session.flush()
        assert sample_live(session, "sol", now=now) == 1
        s2 = session.query(LiveSample).filter(LiveSample.mint == tok2.mint).one()
        assert s2.holders == 88 and s2.mcap_usd == 80_000
        assert s2.trough_before == 40_000
        assert json.loads(s2.features_json)["source"] == "t15m"
        # A row already stored at 0 is repaired from the bar, not from research.
        s2.holders = 0
        s2.features_json = json.dumps({"source": "t15m", "holders_n": 0.0})
        session.flush()
        assert repair_live_sample_holders(session, "sol", now=now) >= 1
        session.refresh(s2)
        assert s2.holders == 88
        assert json.loads(s2.features_json)["source"] == "t15m"
        assert json.loads(s2.features_json)["holders_n"] > 0


def test_sample_live_history_reads_seed_t15m_outside_the_hour_window():
    from launchfinder.models import Snapshot

    init_db()
    now = utcnow()
    with session_scope() as session:
        at = now - timedelta(days=3)
        tok = Token(
            mint="LiveHist111111111111111111111111111111111",
            symbol="LH",
            chain="sol",
            first_seen_at=at,
            migrated_at=at,
            source="poll",
            is_historical=True,
        )
        tok.research = Research(features_json="{}", p_good=0.5, holder_count=40)
        tok.outcome = Outcome(t0_mcap=60_000, max_mcap=80_000, last_mcap=70_000, last_liq=20_000, label=0)
        tok.snapshots.append(Snapshot(kind="t0", mcap_usd=60_000, liquidity_usd=15_000, taken_at=at))
        tok.snapshots.append(Snapshot(kind="t15m", mcap_usd=90_000, liquidity_usd=18_000, volume_h1=6_000, taken_at=at + timedelta(minutes=16)))
        session.add(tok)
        session.flush()
        session.add(Decision(chain="sol", mint=tok.mint, token_id=tok.id, kind=DECISION_ENTRY, at=at, entry_p=0.5, entry_mcap=60_000, liq=15_000, holders=40, source="seed_t0"))
        session.flush()
        assert sample_live(session, "sol", now=now) == 0
        assert sample_live_history(session, "sol", now=now) == 1
        s = session.query(LiveSample).one()
        assert s.mcap_usd == 90_000 and json.loads(s.features_json)["source"] == "t15m"
        assert sample_live_history(session, "sol", now=now) == 0


def _kline(entry_at, *, start_px=0.001, path=((0, 1.0), (15, 1.5), (20, 1.4)), vol=800.0):
    rows = []
    for minute, mult in path:
        px = start_px * mult
        rows.append(
            {
                "time": entry_at + timedelta(minutes=minute),
                "open": px,
                "high": px * 1.02,
                "low": px * 0.98,
                "close": px,
                "volume": vol,
            }
        )
    return rows


def test_print_from_candles_scales_mcap_from_entry_and_leaves_holders_blank():
    now = utcnow()
    at = now - timedelta(minutes=20)
    out = print_from_candles(_kline(at, path=((0, 1.0), (10, 0.8), (15, 2.0))), entry_mcap=60_000, entry_at=at)
    assert out is not None
    assert abs(out["mcap"] - 120_000) < 1.0
    assert out["holders"] == 0 and out["liq"] == 0.0
    assert out["peak"] > 120_000  # high wick
    assert out["trough"] < 60_000  # the 0.8 print
    assert out["vol_h1"] > 0
    # A first candle 20 minutes after entry cannot honestly be t0.
    late = print_from_candles(_kline(at, path=((20, 1.0), (25, 2.0))), entry_mcap=60_000, entry_at=at)
    assert late is None


def test_apply_vendor_candles_fills_missing_tape_and_does_not_overwrite_ours():
    from launchfinder.models import Snapshot
    from launchfinder.research.holders import historical_holder_count_at

    init_db()
    now = utcnow()
    with session_scope() as session:
        at = now - timedelta(minutes=40)
        tok = Token(
            mint="VendFill11111111111111111111111111111111",
            symbol="VF",
            chain="sol",
            first_seen_at=at,
            migrated_at=at,
            source="poll",
        )
        tok.research = Research(features_json="{}", p_good=0.55, holder_count=80)
        tok.outcome = Outcome(t0_mcap=60_000, max_mcap=80_000, last_mcap=70_000, last_liq=20_000)
        tok.snapshots.append(Snapshot(kind="t15m", mcap_usd=90_000, liquidity_usd=18_000, volume_h1=4_000, taken_at=at + timedelta(minutes=16)))
        session.add(tok)
        session.flush()
        d = Decision(chain="sol", mint=tok.mint, token_id=tok.id, kind=DECISION_ENTRY, at=at, entry_p=0.55, entry_mcap=60_000, liq=15_000, holders=40, source="live")
        session.add(d)
        session.flush()
        assert historical_holder_count_at(tok.mint, at) is None
        cand = vendor_candidates(session, "sol", now=now, limit=10)
        assert tok.mint in {c.mint for c in cand}
        candles = _kline(at, path=((0, 1.0), (8, 0.7), (15, 1.8), (20, 1.6)))
        assert apply_vendor_candles(session, d, candles, source="gmgn_kline") is True
        s = session.query(LiveSample).one()
        feats = json.loads(s.features_json)
        assert feats["source"] == "t15m+kline"
        assert s.mcap_usd == 90_000  # our t15m print wins
        assert s.liquidity_usd == 18_000  # never invented from kline
        assert s.holders == 0  # Helius current count is not back-dated
        assert s.trough_before < 60_000  # kline path, not the two-point t15m
        assert s.volume_h1 > 0  # kline USD volume, not a holder count
        # Second pass is already a vendor source — skip.
        assert tok.mint not in {c.mint for c in vendor_candidates(session, "sol", now=now, limit=10)}
        # Tape we wrote ourselves is not overwritten.
        tape_at = now - timedelta(minutes=30)
        tok2 = Token(mint="VendTape11111111111111111111111111111111", symbol="VT", chain="sol", first_seen_at=tape_at, migrated_at=tape_at, source="poll")
        tok2.research = Research(features_json="{}", p_good=0.7, holder_count=90)
        session.add(tok2)
        session.flush()
        session.add(Decision(chain="sol", mint=tok2.mint, token_id=tok2.id, kind=DECISION_ENTRY, at=tape_at, entry_p=0.7, entry_mcap=69_000, liq=20_000, holders=90, source="live"))
        session.add(TapeBar(chain="sol", mint=tok2.mint, token_id=tok2.id, minute=tape_at + timedelta(minutes=15), mcap_usd=80_000, liquidity_usd=22_000, volume_h1=9_000, holders=110))
        session.flush()
        assert sample_live(session, "sol", now=now) == 1
        tape_row = session.query(LiveSample).filter(LiveSample.mint == tok2.mint).one()
        d2 = session.query(Decision).filter(Decision.mint == tok2.mint).one()
        assert apply_vendor_candles(session, d2, _kline(tape_at, path=((0, 1.0), (15, 3.0))), source="gmgn_kline") is False
        session.refresh(tape_row)
        assert tape_row.mcap_usd == 80_000 and json.loads(tape_row.features_json)["source"] == "tape"


def test_apply_vendor_candles_enriches_existing_t15m_path():
    from launchfinder.models import Snapshot

    init_db()
    now = utcnow()
    with session_scope() as session:
        at = now - timedelta(days=2)
        tok = Token(mint="VendEnr111111111111111111111111111111111", symbol="VE", chain="sol", first_seen_at=at, migrated_at=at, source="poll", is_historical=True)
        tok.research = Research(features_json="{}", p_good=0.4, holder_count=30)
        tok.outcome = Outcome(t0_mcap=50_000, max_mcap=55_000, last_mcap=40_000, last_liq=12_000, label=0)
        tok.snapshots.append(Snapshot(kind="t15m", mcap_usd=55_000, liquidity_usd=16_000, volume_h1=3_000, taken_at=at + timedelta(minutes=16)))
        session.add(tok)
        session.flush()
        session.add(Decision(chain="sol", mint=tok.mint, token_id=tok.id, kind=DECISION_ENTRY, at=at, entry_p=0.4, entry_mcap=50_000, liq=14_000, holders=30, source="seed_t0"))
        session.flush()
        assert sample_live_history(session, "sol", now=now) == 1
        s = session.query(LiveSample).one()
        assert json.loads(s.features_json)["source"] == "t15m"
        two_point_trough = s.trough_before
        d = session.query(Decision).filter(Decision.mint == tok.mint).one()
        assert apply_vendor_candles(session, d, _kline(at, start_px=0.002, path=((0, 1.0), (6, 0.5), (15, 1.1))), source="gmgn_kline")
        session.refresh(s)
        assert json.loads(s.features_json)["source"] == "t15m+kline"
        assert s.mcap_usd == 55_000 and s.liquidity_usd == 16_000 and s.holders == 0
        assert s.trough_before < two_point_trough
        # A holder count already on the row survives the kline pass.
        s.holders = 88
        session.flush()
        assert apply_vendor_candles(session, d, _kline(at, start_px=0.002, path=((0, 1.0), (6, 0.4), (15, 1.2))), source="gmgn_kline")
        session.refresh(s)
        assert s.holders == 88
        assert json.loads(s.features_json)["source"] == "t15m+kline"


def test_vendor_candidates_prefer_the_newest_decision():
    """GMGN drops old 1m books. Oldest-first spent the hourly budget on empties."""
    init_db()
    now = utcnow()
    with session_scope() as session:
        mints = []
        for i, age_h in enumerate((20 * 24, 2)):
            at = now - timedelta(hours=age_h)
            tok = Token(mint=f"VendAge{i}1111111111111111111111111111111", symbol=f"VA{i}", chain="sol", first_seen_at=at, migrated_at=at, source="poll")
            tok.research = Research(features_json="{}", p_good=0.4, holder_count=20)
            session.add(tok)
            session.flush()
            session.add(Decision(chain="sol", mint=tok.mint, token_id=tok.id, kind=DECISION_ENTRY, at=at, entry_p=0.4, entry_mcap=50_000, liq=12_000, holders=20, source="live"))
            mints.append(tok.mint)
        session.flush()
        picked = vendor_candidates(session, "sol", now=now, limit=1)
        assert [d.mint for d in picked] == [mints[1]]


def test_live_promote_refuses_a_weaker_top_decile_and_drops_two_point_rows():
    ok, why = live_promote_ok(
        {"auc": 0.80, "brier": 0.10, "precision_top_decile": 0.25, "positives": 20},
        {"auc": 0.50, "brier": 0.20},
        {"auc": 0.79, "brier": 0.10, "precision_top_decile": 0.44},
    )
    assert ok is False and why.startswith("top-decile")
    held, why_ok = live_promote_ok(
        {"auc": 0.80, "brier": 0.10, "precision_top_decile": 0.43, "positives": 20},
        {"auc": 0.50, "brier": 0.20},
        {"auc": 0.79, "brier": 0.10, "precision_top_decile": 0.44},
    )
    assert held is True and why_ok.startswith("beats Entry")
    path, mix = live_fit_rows([{"source": "t15m"}] * 400 + [{"source": "tape"}] * 300)
    assert mix == "path" and len(path) == 300
    mixed, mix_all = live_fit_rows([{"source": "tape"}] * 10 + [{"source": "t15m"}] * 10)
    assert mix_all == "all" and len(mixed) == 20


def test_ran_shadow_is_written_and_never_promoted():
    from launchfinder.scoring.first_sight import RAN_KIND, _write_ran_shadow, chain_features

    init_db()
    names = chain_features("sol")
    width = len(names)

    def row(y):
        return {"x": [0.1] * width, "y_ran": y}

    train = [row(1 if i < 20 else 0) for i in range(40)]
    valid = [row(1 if i < 16 else 0) for i in range(20)]
    with session_scope() as session:
        out = _write_ran_shadow(session, "sol", train, valid, names, np.zeros(width), np.ones(width), utcnow())
        art = session.query(ModelArtifact).filter(ModelArtifact.kind == RAN_KIND).one()
        assert art.promoted is False and out["promoted"] is False
        assert out["n_ge_020"] >= 0
