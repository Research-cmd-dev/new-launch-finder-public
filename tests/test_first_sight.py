import json
import random
from datetime import timedelta

from launchfinder.db import init_db, session_scope
from launchfinder.ledger import DECISION_ENTRY
from launchfinder.models import Decision, ModelArtifact, Outcome, Research, Snapshot, Token, utcnow
from launchfinder.scoring import first_sight as fs


def _facts(**over):
    now = utcnow()
    base = dict(
        mcap=60_000.0,
        liq=12_000.0,
        vol_h1=8_000.0,
        vol_m5=900.0,
        buys_m5=12,
        sells_m5=4,
        at=now,
        created_at=now - timedelta(hours=6),
        migrated_at=now - timedelta(minutes=2),
        holders=40,
        top10_pct=55.0,
        creator_hold_pct=3.0,
        prior_launches=2,
        twitter="https://x.com/a",
        website="",
        telegram="",
        tw_followers=1200,
        tw_age_days=400.0,
        tw_verified=False,
        desc_len=80,
    )
    base.update(over)
    return base


def test_first_sight_features_are_frozen_facts_and_match_between_rows_and_live():
    feats = fs.first_sight_features(**_facts())
    assert list(feats) == list(fs.FEATURES) and len(fs.vector(feats)) == len(fs.FEATURES)
    assert feats["curve_entry"] == 0.0 and feats["instant_fill"] == 0.0 and feats["no_m5"] == 0.0
    assert feats["buy_share_m5"] == 0.75 and feats["has_twitter"] == 1.0 and feats["no_created"] == 0.0
    # A five-minute curve fill with no chain timestamps flags both.
    quick = fs.first_sight_features(**_facts(created_at=None, migrated_at=None, liq=40.0, buys_m5=0, sells_m5=0))
    assert quick["no_created"] == 1.0 and quick["curve_entry"] == 1.0 and quick["no_m5"] == 1.0 and quick["buy_share_m5"] == 0.5
    bundle = fs.first_sight_features(**_facts(created_at=utcnow() - timedelta(seconds=40), migrated_at=utcnow() - timedelta(seconds=10)))
    assert bundle["instant_fill"] == 1.0

    # Training rows and the live path see the same facts the same way.
    now = utcnow()
    tok = Token(mint="FS111", chain="sol", twitter="https://x.com/a", website="", telegram="", description="x" * 80, created_at_chain=now - timedelta(hours=6), migrated_at=now - timedelta(minutes=2))
    research = Research(top10_pct=55.0, creator_hold_pct=3.0, creator_prior_launches=2, twitter_followers=1200, twitter_age_days=400.0, twitter_verified=False, holder_count=40)
    t0 = Snapshot(kind="t0", mcap_usd=60_000, liquidity_usd=12_000, volume_h1=8_000, volume_m5=900, buys_m5=12, sells_m5=4, taken_at=now)
    d = Decision(entry_mcap=60_000, liq=12_000, vol_h1=8_000, holders=40, at=now)
    from_rows = fs.features_from_rows(d, tok, research, t0)
    from_live = fs.features_from_live(
        tok,
        market={"mcap_usd": 60_000, "liquidity_usd": 12_000, "volume_h1": 8_000, "volume_m5": 900, "buys_m5": 12, "sells_m5": 4},
        holder_info={"holder_count": 40, "top10_pct": 55.0, "creator_hold_pct": 3.0},
        creator_stats={"launches": 2, "wins": 1, "rugs": 0},
        twitter={"followers": 1200, "age_days": 400.0, "verified": False},
        twitter_url="https://x.com/a",
        website="",
        telegram="",
        at=now,
    )
    assert from_rows == from_live


def _seed_board(session, *, n: int, days: int = 30, seed: int = 7):
    """Synthetic judged decisions where slow organic fills with a quiet
    t0 tape win and instant fills with a bot burst lose."""
    rng = random.Random(seed)
    now = utcnow()
    for i in range(n):
        at = now - timedelta(days=days) + timedelta(minutes=i * (days * 24 * 60) / n)
        organic = rng.random() < 0.5
        ttm = rng.uniform(12, 50) if organic else rng.uniform(0.1, 0.9)
        buys = rng.randint(0, 3) if organic else rng.randint(20, 60)
        win = rng.random() < (0.55 if organic else 0.05)
        tok = Token(
            mint=f"FSBoard{i:08d}",
            chain="sol",
            symbol=f"F{i}",
            first_seen_at=at,
            created_at_chain=at - timedelta(minutes=ttm + 0.5),
            migrated_at=at - timedelta(minutes=0.5),
            source="poll",
            is_historical=bool(i % 3 == 0),  # retired names count too
        )
        tok.research = Research(features_json="{}", p_good=0.5, holder_count=rng.randint(5, 300), top10_pct=rng.uniform(20, 100), creator_prior_launches=rng.randint(0, 20))
        entry = 60_000.0
        tok.outcome = Outcome(t0_mcap=entry, max_mcap=entry, last_mcap=entry * (2.5 if win else 0.6), last_liq=20_000, label=None)
        tok.snapshots.append(Snapshot(kind="t0", mcap_usd=entry, liquidity_usd=15_000, volume_h1=rng.uniform(1_000, 30_000), volume_m5=buys * 40.0, buys_m5=buys, sells_m5=rng.randint(0, 5), p_good=0.5, taken_at=at))
        session.add(tok)
        session.flush()
        session.add(Decision(chain="sol", mint=tok.mint, token_id=tok.id, kind=DECISION_ENTRY, at=at, source="seed_t0" if i % 2 else "live", entry_p=rng.uniform(0.2, 0.95), entry_mcap=entry, liq=15_000, vol_h1=5_000, holders=tok.research.holder_count))
    session.flush()


def test_research_pipeline_writes_first_sight_as_sol_entry_and_keeps_the_legacy_blend(monkeypatch):
    import asyncio

    from launchfinder.research import pipeline
    from launchfinder.research import dexscreener, github, gmgn, holders, pumpfun, twitter

    init_db()
    fs.reset_cache()
    monkeypatch.setattr(fs, "MIN_ROWS", 300)

    async def _none(*a, **k):
        return None

    async def _empty(*a, **k):
        return {}

    async def _list(*a, **k):
        return []

    async def _zero(*a, **k):
        return 0

    async def _market(mint, chain="sol"):
        return {"mcap_usd": 60_000, "liquidity_usd": 12_000, "volume_h1": 8_000, "volume_m5": 40, "buys_m5": 1, "sells_m5": 0, "price_usd": 0.00006}

    monkeypatch.setattr(pumpfun, "get_coin", _empty)
    monkeypatch.setattr(pumpfun, "creator_coins", _list)
    monkeypatch.setattr(pumpfun, "get_user", _empty)
    monkeypatch.setattr(dexscreener, "token_market", _market)
    monkeypatch.setattr(holders, "holder_stats", _empty)
    monkeypatch.setattr(gmgn, "token_research", _empty)
    monkeypatch.setattr(github, "lookup_repo", _empty)
    monkeypatch.setattr(github, "search_token", _empty)
    monkeypatch.setattr(twitter, "lookup_handle", _empty)
    monkeypatch.setattr(twitter, "mention_count", _zero)
    monkeypatch.setattr(twitter, "account_mentions", _zero)

    def _research(mint: str):
        with session_scope() as session:
            now = utcnow()
            tok = Token(mint=mint, chain="sol", symbol="FSP", name="First Sight", source="poll", first_seen_at=now, created_at_chain=now - timedelta(hours=5), migrated_at=now - timedelta(minutes=1))
            session.add(tok)
            session.flush()
            asyncio.run(pipeline.research_token(session, tok, coin={"name": "First Sight", "symbol": "FSP"}))
            research = tok.research
            entry = session.query(Decision).filter(Decision.mint == mint, Decision.kind == DECISION_ENTRY).one()
            feats = json.loads(research.features_json)
            return research.p_good, research.heuristic_p, research.model_p, entry.entry_p, entry.heuristic_p, feats.get("legacy_p"), research.thesis

    # No promoted first-sight fit: the legacy blend is Entry, and it is recorded as legacy_p too.
    p, hp, mp, ep, ehp, legacy, thesis = _research("FSPipeLegacy11111111111111111111111111111")
    assert ep == p and legacy == p and "Model p(good)" in thesis

    with session_scope() as session:
        _seed_board(session, n=420)
        out = fs.fit_first_sight(session, "sol")
        assert out["promoted"], out
    fs.reset_cache()
    p2, hp2, mp2, ep2, ehp2, legacy2, thesis2 = _research("FSPipeModel111111111111111111111111111111")
    # Entry is now the first-sight probability; the blend it replaced is still on the row.
    assert ep2 == p2 and legacy2 is not None and p2 != legacy2 and ehp2 == hp2 and hp2 > 0
    assert "First-sight p(2×)" in thesis2


def test_fit_first_sight_promotes_on_a_time_split_and_scores_sol_entry(monkeypatch):
    init_db()
    fs.reset_cache()
    monkeypatch.setattr(fs, "MIN_ROWS", 300)
    with session_scope() as session:
        _seed_board(session, n=420)
        assert fs.first_sight_p(session, "sol", fs.first_sight_features(**_facts())) is None  # nothing promoted yet
        out = fs.fit_first_sight(session, "sol")
        assert out["fitted"] and out["promoted"], out
        assert out["candidate"]["auc"] > 0.75 and out["candidate"]["precision_top_decile"] > out["entry_p_baseline"]["precision_top_decile"]
        art = session.query(ModelArtifact).filter(ModelArtifact.kind == fs.KIND, ModelArtifact.promoted.is_(True)).one()
        metrics = json.loads(art.metrics_json)
        assert metrics["live_only"] is True and metrics["frozen_columns"] is True and metrics["seed_rows"] > 0 and metrics["live_rows"] > 0
        assert metrics["train_max_age_sight_min"] == fs.TRAIN_MAX_AGE_SIGHT_MIN == 60.0
        weights = json.loads(art.weights_json)
        assert set(weights) == {"features", "w", "mu", "sd"} and set(weights["w"]) == set(fs.FEATURES) and tuple(weights["features"]) == fs.FEATURES
        assert metrics["n_features"] == len(fs.FEATURES) and metrics["holders_unknown_valid"] == 0.0
        # Slow organic fill, quiet tape: high. Instant fill with a bot burst: low.
        fs.reset_cache()
        slow = fs.first_sight_p(session, "sol", fs.first_sight_features(**_facts(created_at=utcnow() - timedelta(hours=5), buys_m5=1, sells_m5=0, vol_m5=40.0)))
        fast = fs.first_sight_p(session, "sol", fs.first_sight_features(**_facts(created_at=utcnow() - timedelta(seconds=45), migrated_at=utcnow() - timedelta(seconds=15), buys_m5=45, sells_m5=3, vol_m5=1_800.0)))
        assert slow is not None and fast is not None and slow > fast and 0.01 <= fast <= 0.99
        # RH is not scored by this model.
        assert fs.first_sight_p(session, "robinhood", fs.first_sight_features(**_facts())) is None
        # A fresh incumbent is not refit on the same window.
        again = fs.fit_first_sight(session, "sol")
        assert again["fitted"] is False and "incumbent" in again["reason"]
        # The seed-feature demotion leaves it alone: its inputs are frozen columns.
        from launchfinder.scoring.batch_fit import demote_unfrozen_fits

        assert demote_unfrozen_fits(session) == []
        assert session.query(ModelArtifact).filter(ModelArtifact.kind == fs.KIND, ModelArtifact.promoted.is_(True)).count() == 1


def test_unknown_holder_count_is_imputed_not_read_as_an_empty_book(monkeypatch):
    """Live 07:00 (v76): the Helius plan cap left 44% of Sol first sights with
    holders=0 and the model read them as dead books (0.01). Unknown is a
    state of the desk, not of the token: the holder columns sit at the
    training mean and ``holders_unknown`` carries the fact."""
    import numpy as np

    known = fs.first_sight_features(**_facts(holders=40))
    unknown = fs.first_sight_features(**_facts(holders=0))
    assert known["holders_unknown"] == 0.0 and unknown["holders_unknown"] == 1.0
    assert unknown["log_holders"] == 0.0 and unknown["log_mcap_per_holder"] == 0.0

    X = np.array([fs.vector(known), fs.vector(unknown)], dtype=float)
    mu, sd = fs._moments(np.array([fs.vector(fs.first_sight_features(**_facts(holders=h))) for h in (10, 40, 400, 0, 0)], dtype=float), fs.FEATURES)
    # Moments for the holder columns come from known rows only.
    assert abs(mu[fs.FEATURES.index("log_holders")] - np.mean([np.log1p(10), np.log1p(40), np.log1p(400)])) < 1e-9
    Z = fs._standardize(X, mu, sd, fs.FEATURES)
    for col in fs.HOLDER_FEATURES:
        j = fs.FEATURES.index(col)
        assert Z[1, j] == 0.0 and Z[0, j] != 0.0
    # Everything else is standardized as usual on the unknown row.
    assert Z[1, fs.FEATURES.index("log_mcap")] == Z[0, fs.FEATURES.index("log_mcap")]

    # A fitted model gives an unknown-holder book roughly the score of the
    # same book with typical holders — not the floor.
    init_db()
    fs.reset_cache()
    monkeypatch.setattr(fs, "MIN_ROWS", 300)
    with session_scope() as session:
        _seed_board(session, n=420)
        assert fs.fit_first_sight(session, "sol")["promoted"]
        fs.reset_cache()
        slow = dict(created_at=utcnow() - timedelta(hours=5), buys_m5=1, sells_m5=0, vol_m5=40.0)
        p_known = fs.first_sight_p(session, "sol", fs.first_sight_features(**_facts(holders=60, **slow)))
        p_unknown = fs.first_sight_p(session, "sol", fs.first_sight_features(**_facts(holders=0, **slow)))
        p_tiny = fs.first_sight_p(session, "sol", fs.first_sight_features(**_facts(holders=1, **slow)))
        assert p_known is not None and p_unknown is not None and p_tiny is not None
        assert abs(p_unknown - p_known) <= abs(p_tiny - p_known) + 1e-9
        assert p_unknown > 0.01


def test_pre_v77_artifact_without_a_feature_list_still_scores():
    """Artifact v1 (stack-v75) stored 29 weights and no ``features`` key: it
    is scored on exactly those columns, without holder imputation."""
    import numpy as np

    old = tuple(n for n in fs.FEATURES if n != "holders_unknown")
    weights = {"w": {n: 0.0 for n in old}, "mu": {n: 0.0 for n in old}, "sd": {n: 1.0 for n in old}}
    weights["w"]["log_holders"] = 1.0
    assert fs.artifact_features(weights) == old
    feats = fs.first_sight_features(**_facts(holders=0))
    X = np.array([fs.vector(feats, old)], dtype=float)
    assert X.shape == (1, len(old))
    # No imputation on a pre-v77 artifact: holders=0 is read as it was then.
    z = fs._raw(weights, X, 0.0)
    assert abs(float(z[0]) - 0.5) < 1e-9  # log1p(0) = 0 -> z = 0 -> sigmoid 0.5
    X40 = np.array([fs.vector(fs.first_sight_features(**_facts(holders=40)), old)], dtype=float)
    assert float(fs._raw(weights, X40, 0.0)[0]) > 0.5


def _seed_rh_board(session, *, n: int, days: int = 30, seed: int = 11):
    rng = random.Random(seed)
    now = utcnow()
    for i in range(n):
        at = now - timedelta(days=days) + timedelta(minutes=i * (days * 24 * 60) / n)
        organic = rng.random() < 0.5
        buys = rng.randint(0, 3) if organic else rng.randint(20, 60)
        win = rng.random() < (0.5 if organic else 0.05)
        tok = Token(mint=f"0xrhfs{i:08d}", chain="robinhood", symbol=f"R{i}", first_seen_at=at, created_at_chain=at - timedelta(minutes=rng.uniform(1, 45)), source="rh_poll")
        # Mutable on RH: rewritten by the 2-minute refresh, so it must not be a feature.
        tok.research = Research(features_json="{}", p_good=0.5, holder_count=5000 if win else 3, top10_pct=10.0 if win else 99.9)
        entry = 50_000.0
        tok.outcome = Outcome(t0_mcap=entry, max_mcap=entry, last_mcap=entry * (2.5 if win else 0.6), last_liq=20_000, label=None)
        tok.snapshots.append(Snapshot(kind="t0", mcap_usd=entry, liquidity_usd=15_000, volume_h1=rng.uniform(1_000, 30_000), volume_m5=buys * 40.0, buys_m5=buys, sells_m5=rng.randint(0, 5), p_good=0.5, taken_at=at))
        session.add(tok)
        session.flush()
        session.add(Decision(chain="robinhood", mint=tok.mint, token_id=tok.id, kind=DECISION_ENTRY, at=at, source="seed_t0", entry_p=rng.uniform(0.2, 0.95), entry_mcap=entry, liq=15_000, vol_h1=5_000, holders=tok.research.holder_count))
    session.flush()


def test_robinhood_first_sight_fits_on_frozen_columns_only_and_scores_entry_on_rh_lines(monkeypatch):
    """RH holder / concentration columns are rewritten by the Blockscout
    refresh (the seed rows carry the *latest* count): they are not first
    sight and are not features. Live 07:50 (v77 shadow): AUC 0.92 against
    0.53 for the legacy blend, so RH scores Entry with it from v78 — on
    RH lines (0.25 / 0.30), because the frozen-column fit tops out near 0.45."""
    from launchfinder.desk_lines import FIRST_SIGHT_LINES, FIRST_SIGHT_RH_LINES, current_scorer, desk_lines, lines_for_scorer

    rh = fs.chain_features("robinhood")
    assert set(rh).isdisjoint(fs.RH_MUTABLE_FEATURES) and len(rh) == len(fs.FEATURES) - len(fs.RH_MUTABLE_FEATURES)
    assert fs.chain_features("sol") == fs.FEATURES
    assert "robinhood" in fs.FIRST_SIGHT_FIT_CHAINS and "robinhood" in fs.FIRST_SIGHT_CHAINS
    assert lines_for_scorer("first_sight", "rh") is FIRST_SIGHT_RH_LINES and lines_for_scorer("first_sight", "sol") is FIRST_SIGHT_LINES
    assert lines_for_scorer("first_sight") is FIRST_SIGHT_LINES and lines_for_scorer("legacy", "robinhood").hi == 0.9
    assert FIRST_SIGHT_RH_LINES.hi == 0.3 and FIRST_SIGHT_RH_LINES.lo == 0.25

    init_db()
    fs.reset_cache()
    monkeypatch.setattr(fs, "MIN_ROWS", 300)
    with session_scope() as session:
        _seed_rh_board(session, n=420)
        assert current_scorer(session, "robinhood") == "legacy" and desk_lines(session, "rh").hi == 0.9
        out = fs.fit_first_sight(session, "robinhood")
        assert out["fitted"] and out["promoted"], out
        art = session.query(ModelArtifact).filter(ModelArtifact.kind == fs.KIND, ModelArtifact.chain == "robinhood").one()
        weights = json.loads(art.weights_json)
        assert tuple(weights["features"]) == rh and set(weights["w"]) == set(rh)
        metrics = json.loads(art.metrics_json)
        assert metrics["holders_unknown_valid"] is None
        # Validation line metrics are measured on the RH lines.
        assert metrics["lines"]["hi"]["threshold"] == 0.3 and metrics["lines"]["lo"]["threshold"] == 0.25
        fs.reset_cache()
        p = fs.first_sight_p(session, "robinhood", fs.first_sight_features(**_facts()))
        assert p is not None and 0.01 <= p <= 0.99
        # The mutable columns cannot move an RH score.
        p_conc = fs.first_sight_p(session, "robinhood", fs.first_sight_features(**_facts(holders=0, top10_pct=99.9, creator_hold_pct=90.0)))
        assert p_conc == p
        assert current_scorer(session, "robinhood") == "first_sight" and desk_lines(session, "rh") is FIRST_SIGHT_RH_LINES
        # Sol is untouched by the RH artifact.
        assert fs.first_sight_p(session, "sol", fs.first_sight_features(**_facts())) is None


def test_refit_guard_yields_to_a_new_column_set(monkeypatch):
    """The 6h guard stops a same-window refit re-winning on Brier. A fit on
    a different column set is a new model and goes straight to the judge."""
    init_db()
    fs.reset_cache()
    monkeypatch.setattr(fs, "MIN_ROWS", 300)
    with session_scope() as session:
        _seed_board(session, n=420)
        assert fs.fit_first_sight(session, "sol")["promoted"]
        assert fs.fit_first_sight(session, "sol")["fitted"] is False
        # Rewrite the incumbent as a pre-v77 artifact (29 columns, no list).
        art = session.query(ModelArtifact).filter(ModelArtifact.kind == fs.KIND, ModelArtifact.promoted.is_(True)).one()
        w = json.loads(art.weights_json)
        del w["features"]
        for key in ("w", "mu", "sd"):
            w[key].pop("holders_unknown", None)
        art.weights_json = json.dumps(w)
        session.flush()
        again = fs.fit_first_sight(session, "sol")
        assert again["fitted"] is True and again["version"] == 2
        assert again["incumbent"]["source"] == "first_sight v1"


def test_refit_guard_yields_to_a_new_label_and_the_fit_records_it(monkeypatch):
    """v79: the judge moved from the sight print to the first fillable print.
    An incumbent fitted on the old label is a different model — the guard
    must not hold the honest fit back six hours — and every fit says which
    label it was judged on plus how much of its tail was never fillable."""
    init_db()
    fs.reset_cache()
    monkeypatch.setattr(fs, "MIN_ROWS", 300)
    with session_scope() as session:
        _seed_board(session, n=420)
        out = fs.fit_first_sight(session, "sol")
        assert out["promoted"]
        art = session.query(ModelArtifact).filter(ModelArtifact.kind == fs.KIND, ModelArtifact.promoted.is_(True)).one()
        m = json.loads(art.metrics_json)
        assert m["label_version"] == fs.LABEL_VERSION == 3 and "fillable" in m["label"] and "lifetime high" in m["label"]
        # Every board row sat on a $15k book: nothing was unfilled or re-based.
        assert m["unfilled_valid"] == 0.0 and m["filled_later_valid"] == 0.0
        assert fs.fit_first_sight(session, "sol")["fitted"] is False
        # Same columns, older label: not a same-window refit. The incumbent is
        # re-judged on the new label from the same rows, so this candidate
        # ties it exactly; v80 promotes a tie on a label change (an honest
        # card, no hourly refit churn — live RH lost by 0.0001 Brier every
        # cycle on v79) instead of "brier not under incumbent".
        m["label_version"] = 1
        art.metrics_json = json.dumps(m)
        session.flush()
        again = fs.fit_first_sight(session, "sol")
        assert again["fitted"] is True and again["version"] == 2
        assert again["promoted"] is True and "label v3" in again["reason"] and "ranking held" in again["reason"]
        # A pre-v79 artifact with no label_version at all reads as label 1 too.
        art2 = session.query(ModelArtifact).filter(ModelArtifact.kind == fs.KIND, ModelArtifact.promoted.is_(True)).order_by(ModelArtifact.id.desc()).first()
        m2 = json.loads(art2.metrics_json)
        m2.pop("label_version")
        art2.metrics_json = json.dumps(m2)
        session.flush()
        assert fs.fit_first_sight(session, "sol")["fitted"] is True


def test_training_rows_judge_curve_entries_from_the_first_fillable_print():
    """The label the model learns is the paper trade: a curve token that only
    graduated is a loss; one that ran 2x from its pool open is a win; one
    that never got a pool is unfilled."""
    init_db()
    now = utcnow()
    at = now - timedelta(days=2)
    with session_scope() as session:
        ats = []
        for i, (pool_mcap, peak, has_pool) in enumerate(((69_000, 110_000, True), (60_000, 150_000, True), (0, 0, False))):
            at_i = at + timedelta(seconds=i)
            tok = Token(mint=f"FSCurve{i:09d}", chain="sol", symbol=f"C{i}", first_seen_at=at_i, created_at_chain=at_i - timedelta(minutes=3), source="poll")
            tok.research = Research(features_json="{}", p_good=0.5, holder_count=30)
            tok.outcome = Outcome(t0_mcap=69_000, max_mcap=max(69_000, peak), last_mcap=peak or 1_000, last_liq=25_000 if has_pool else 0, label=None)
            tok.snapshots.append(Snapshot(kind="t0", mcap_usd=30_000, liquidity_usd=0, buys_m5=2, sells_m5=0, taken_at=at_i))
            if has_pool:
                tok.snapshots.append(Snapshot(kind="live", mcap_usd=pool_mcap, liquidity_usd=20_000, taken_at=at_i + timedelta(minutes=6)))
                tok.snapshots.append(Snapshot(kind="live", mcap_usd=peak, liquidity_usd=30_000, taken_at=at_i + timedelta(hours=2)))
            session.add(tok)
            session.flush()
            session.add(Decision(chain="sol", mint=tok.mint, token_id=tok.id, kind=DECISION_ENTRY, at=at_i, source="live", entry_p=0.5, entry_mcap=30_000, liq=0.0, holders=30))
            ats.append(at_i)
        session.flush()
        by_at = {r["at"].replace(tzinfo=None): r for r in fs.training_rows(session, "sol", now=now)}
        assert len(by_at) == 3
        grad, ran, never = (by_at[a.replace(tzinfo=None)] for a in ats)
        assert grad["y"] == 0 and grad["filled_later"] is True and grad["unfilled"] is False  # 110k / 69k = 1.6x
        assert ran["y"] == 1 and ran["filled_later"] is True  # 150k / 60k = 2.5x
        assert never["y"] == 0 and never["unfilled"] is True


def test_held_ranking_on_new_label_is_a_tie_not_a_free_pass():
    inc = {"auc": 0.80, "precision_top_decile": 0.30, "brier": 0.0500}
    tie = {"n": 500, "positives": 40, "auc": 0.80, "precision_top_decile": 0.30, "brier": 0.0505}
    assert fs.held_ranking_on_new_label(tie, inc)
    # Brier within 2% relative, ranking within should_promote's slack.
    assert fs.held_ranking_on_new_label({**tie, "brier": 0.0509, "auc": 0.795, "precision_top_decile": 0.285}, inc)
    assert not fs.held_ranking_on_new_label({**tie, "brier": 0.0520}, inc)
    assert not fs.held_ranking_on_new_label({**tie, "auc": 0.78}, inc)
    assert not fs.held_ranking_on_new_label({**tie, "precision_top_decile": 0.27}, inc)
    assert not fs.held_ranking_on_new_label({**tie, "positives": 5}, inc)
    # No incumbent Brier (entry_p baseline only): ranking is enough.
    assert fs.held_ranking_on_new_label(tie, {"auc": 0.70, "precision_top_decile": 0.20})


def test_ranking_held_promotes_on_same_label_brier_slack():
    from types import SimpleNamespace

    from launchfinder.scoring.batch_fit import should_promote

    prev = SimpleNamespace(version=6)
    # RH v7: ranking holds, Brier 0.0001 worse — should_promote fails, slack promotes.
    cand = {"n": 800, "positives": 80, "auc": 0.8027, "precision_top_decile": 0.31, "brier": 0.0253}
    inc = {"auc": 0.8020, "precision_top_decile": 0.30, "brier": 0.0252}
    ok, why = should_promote(cand, inc)
    assert ok is False and "brier" in why
    same = fs.ranking_held_promote_reason(cand, inc, prev=prev, same_label=True, prev_label=3)
    assert same and same.startswith("ranking held (brier")
    label = fs.ranking_held_promote_reason(cand, inc, prev=prev, same_label=False, prev_label=2)
    assert label and label.startswith("label v3:")
    worse = {**cand, "brier": 0.0300}
    assert fs.ranking_held_promote_reason(worse, inc, prev=prev, same_label=True, prev_label=3) is None


def test_training_rows_drop_late_discovery_leftovers():
    init_db()
    now = utcnow()
    at = now - timedelta(days=2)
    with session_scope() as session:
        for i, age_min in enumerate((8.0, 180.0)):
            tok = Token(
                mint=f"FSAge{i:010d}",
                chain="sol",
                symbol=f"A{i}",
                first_seen_at=at,
                created_at_chain=at - timedelta(minutes=age_min),
                migrated_at=at - timedelta(minutes=1),
                source="poll",
            )
            tok.research = Research(features_json="{}", p_good=0.5, holder_count=40)
            tok.outcome = Outcome(t0_mcap=60_000, max_mcap=60_000, last_mcap=40_000, last_liq=20_000, label=None)
            tok.snapshots.append(Snapshot(kind="t0", mcap_usd=60_000, liquidity_usd=15_000, buys_m5=2, sells_m5=0, taken_at=at))
            tok.snapshots.append(Snapshot(kind="live", mcap_usd=40_000, liquidity_usd=20_000, taken_at=at + timedelta(hours=2)))
            session.add(tok)
            session.flush()
            session.add(Decision(chain="sol", mint=tok.mint, token_id=tok.id, kind=DECISION_ENTRY, at=at, source="live", entry_p=0.5, entry_mcap=60_000, liq=15_000, holders=40))
        session.flush()
        rows = fs.training_rows(session, "sol", now=now)
        assert len(rows) == 1
        assert rows[0]["source"] == "live"


def test_refit_guard_yields_to_a_new_age_cut(monkeypatch):
    init_db()
    fs.reset_cache()
    monkeypatch.setattr(fs, "MIN_ROWS", 300)
    with session_scope() as session:
        _seed_board(session, n=420)
        assert fs.fit_first_sight(session, "sol")["promoted"]
        assert fs.fit_first_sight(session, "sol")["fitted"] is False
        art = session.query(ModelArtifact).filter(ModelArtifact.kind == fs.KIND, ModelArtifact.promoted.is_(True)).one()
        m = json.loads(art.metrics_json)
        m["train_max_age_sight_min"] = 0.0
        art.metrics_json = json.dumps(m)
        session.flush()
        again = fs.fit_first_sight(session, "sol")
        assert again["fitted"] is True


def test_first_sight_waits_for_a_fillable_book_then_finalizes_once(monkeypatch):
    from launchfinder.desk_lines import SCORER_FIRST_SIGHT, SCORER_LEGACY
    from launchfinder.scoring.hunt import upsert_hunt

    init_db()
    fs.reset_cache()
    monkeypatch.setattr(fs, "MIN_ROWS", 300)
    with session_scope() as session:
        _seed_board(session, n=420)
        assert fs.fit_first_sight(session, "sol")["promoted"]
        fs.reset_cache()
        now = utcnow()
        tok = Token(
            mint="FSAwait1111111111111111111111111111111111",
            chain="sol",
            symbol="WAIT",
            first_seen_at=now - timedelta(minutes=8),
            created_at_chain=now - timedelta(minutes=20),
            migrated_at=now - timedelta(minutes=8),
            source="poll",
        )
        tok.research = Research(
            features_json='{"awaiting_fill": 1}',
            p_good=0.01,
            heuristic_p=0.40,
            model_p=0.40,
            holder_count=40,
            scorer=SCORER_FIRST_SIGHT,
            risk_flags_json="[]",
        )
        tok.outcome = Outcome(t0_mcap=60_000, last_mcap=60_000, max_mcap=60_000, last_liq=12_000, multiple=1.0)
        session.add(tok)
        session.flush()
        assert fs.book_is_fillable(400) is False and fs.book_is_fillable(12_000) is True
        assert fs.research_awaiting_fill(tok.research) is True
        assert fs.hunt_entry_locked(tok.research, 0.01, SCORER_FIRST_SIGHT) is False
        assert fs.hunt_entry_locked(tok.research, 0.40, SCORER_FIRST_SIGHT) is False
        card = upsert_hunt(session, tok)
        assert card is not None and card.entry_p == 0.01
        assert session.query(Decision).filter(Decision.mint == tok.mint, Decision.kind == DECISION_ENTRY).count() == 0
        market = {"mcap_usd": 72_000, "liquidity_usd": 12_000, "volume_h1": 8_000, "volume_m5": 40, "buys_m5": 1, "sells_m5": 0, "price_usd": 0.00007}
        assert fs.finalize_first_sight_entry(session, tok, market) is True
        assert fs.entry_is_fill_finalized(tok.research) is True
        assert fs.research_awaiting_fill(tok.research) is False
        assert tok.research.p_good > 0.01
        assert tok.research.scorer == SCORER_FIRST_SIGHT
        entry = session.query(Decision).filter(Decision.mint == tok.mint, Decision.kind == DECISION_ENTRY).one()
        assert entry.entry_p == tok.research.p_good
        assert fs.hunt_entry_locked(tok.research, 0.01, SCORER_FIRST_SIGHT) is False
        assert fs.hunt_entry_locked(tok.research, float(entry.entry_p), SCORER_FIRST_SIGHT) is True
        card2 = upsert_hunt(session, tok)
        assert card2.entry_p == entry.entry_p
        assert fs.finalize_first_sight_entry(session, tok, market) is False
        assert session.query(Decision).filter(Decision.mint == tok.mint, Decision.kind == DECISION_ENTRY).count() == 1
        # v80 wall: already fill_finalized at 0.01, no awaiting_fill flag.
        wall = Token(
            mint="FSWall01111111111111111111111111111111111",
            chain="sol",
            symbol="WALL",
            first_seen_at=now - timedelta(minutes=8),
            created_at_chain=now - timedelta(minutes=20),
            migrated_at=now - timedelta(minutes=8),
            source="poll",
        )
        wall.research = Research(
            features_json='{"fill_finalized": 1, "awaiting_fill": 0}',
            p_good=0.01,
            heuristic_p=0.40,
            model_p=0.40,
            holder_count=180,
            scorer=SCORER_FIRST_SIGHT,
            risk_flags_json="[]",
        )
        wall.outcome = Outcome(t0_mcap=50_000, last_mcap=80_000, max_mcap=80_000, last_liq=40_000, multiple=1.6)
        session.add(wall)
        session.flush()
        assert fs.entry_is_fill_finalized(wall.research) is True
        assert fs.research_awaiting_fill(wall.research) is False
        assert fs.finalize_first_sight_entry(session, wall, market) is True
        assert wall.research.p_good > 0.01
        assert SCORER_LEGACY == "legacy"
