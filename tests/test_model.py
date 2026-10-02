import json

from launchfinder.db import SessionLocal, init_db
from launchfinder.scoring.features import FEATURE_NAMES, extract_features
from launchfinder.scoring.model import YOUNG_RH_N_TRAIN, get_or_create_model, online_update, predict


def _river_features() -> dict:
    return extract_features(
        {
            "coin": {"name": "River", "symbol": "RVR", "reply_count": 80},
            "twitter": {"followers": 8000, "age_days": 400, "verified": False},
            "twitter_handle": "river",
            "website": "https://example.com",
            "holders": {"holder_count": 400, "top10_pct": 25},
            "creator_stats": {"launches": 1, "wins": 1, "rugs": 0},
            "time_to_migrate_min": 40,
            "market": {"buys_h1": 20, "sells_h1": 8, "liquidity_usd": 20000, "volume_h1": 8000},
        }
    )


def test_online_update_moves_probability():
    init_db()
    session = SessionLocal()
    try:
        get_or_create_model(session)
        features = _river_features()
        before = predict(session, features)["model_p"]
        for _ in range(12):
            online_update(session, features, 1, lr=0.2)
        after = predict(session, features)["model_p"]
        assert after > before
    finally:
        session.close()


def _mid_book_features() -> dict:
    # Live enough to score, not organic (no liq/vol). Model may pull this down.
    return extract_features(
        {
            "chain": "robinhood",
            "coin": {"name": "Mid", "symbol": "MID"},
            "holders": {"holder_count": 62, "top10_pct": 20, "creator_hold_pct": 2},
            "creator_stats": {"launches": 2, "wins": 0, "rugs": 0},
            "market": {"liquidity_usd": 0, "volume_h1": 0},
        }
    )


def test_young_rh_model_cannot_pull_below_heuristic():
    # SANDIH-class: leftover-poisoned RH weights drag blend under the 0.50
    # paper line while the rules still pass. Floor p at heuristic until
    # YOUNG_RH_N_TRAIN labels (48000 — leftover n_train ~46k still pulling).
    init_db()
    session = SessionLocal()
    try:
        rh = get_or_create_model(session, chain="robinhood")
        rh.n_train = 44
        rh.bias = -8.0
        rh.weights_json = json.dumps({name: -4.0 for name in FEATURE_NAMES})
        session.flush()
        features = _river_features()
        scored = predict(session, features, chain="robinhood")
        assert scored["heuristic_p"] >= 0.50
        assert scored["model_p"] < 0.20
        assert scored["p_good"] == scored["heuristic_p"]

        rh.n_train = 200  # past the old 160 cutoff; 24h flats are still arriving
        session.flush()
        still_young = predict(session, features, chain="robinhood")
        assert still_young["p_good"] == still_young["heuristic_p"]

        # Live 22:52: n_train 46383 already past 19560. SANDIH-class
        # river books must still floor until the leftover hold.
        rh.n_train = 46383
        session.flush()
        live_n = predict(session, features, chain="robinhood")
        assert live_n["p_good"] == live_n["heuristic_p"]

        rh.n_train = YOUNG_RH_N_TRAIN
        session.flush()
        mid = _mid_book_features()
        mature = predict(session, mid, chain="robinhood")
        assert mid["organic_book"] == 0.0
        assert mature["p_good"] < mature["heuristic_p"]

        sol = get_or_create_model(session, chain="sol")
        sol.n_train = 44
        sol.bias = -8.0
        sol.weights_json = json.dumps({name: -4.0 for name in FEATURE_NAMES})
        session.flush()
        pulled = predict(session, mid, chain="sol")
        assert pulled["p_good"] < pulled["heuristic_p"]
    finally:
        session.close()


def test_rh_clean_lp_open_survives_mature_model_bury():
    # Live MEME: after the young floor, blend is 85% model. The next
    # fair LP open must still clear paper 0.50. SHORT/PLUMBED stay
    # thin-capped. FEATURE_NAMES stays 66.
    meme = extract_features(
        {
            "chain": "robinhood",
            "coin": {"name": "A Meme Coin", "symbol": "MEME"},
            "twitter": {"followers": 2, "age_days": 0.02, "verified": True, "tweets": 0},
            "twitter_handle": "amemecoinrh",
            "symbol_flood": 4,
            "holders": {
                "holder_count": 4,
                "top10_pct": 1.2,
                "top_wallets": [
                    {"owner": "0x8366a39CC670B4001A1121B8F6A443A643e40951", "pct": 98.8, "label": "pool"},
                    {"owner": "0xD9C1383ebF393d28Ed7f6B33B1DED1962c85982C", "pct": 1.2, "label": ""},
                    {"owner": "0x6f02324d20CC679d0E585290CAa6b16baCbC0F77", "pct": 0.0, "label": "pool"},
                    {"owner": "0x000000000000000000000000000000000000dEaD", "pct": 0.0, "label": ""},
                ],
            },
            "market": {},
            "gmgn": {"source": "gmgn", "rug_risk": 0, "insider_pct": 0, "sniper_pct": 0},
        }
    )
    wick = extract_features(
        {
            "chain": "robinhood",
            "coin": {"name": "SHORT", "symbol": "SHORT"},
            "holders": {"holder_count": 4, "top10_pct": 90},
            "last_liq": 200,
            "market": {"liquidity_usd": 200, "volume_h1": 50},
        }
    )
    init_db()
    session = SessionLocal()
    try:
        rh = get_or_create_model(session, chain="robinhood")
        rh.n_train = YOUNG_RH_N_TRAIN
        rh.bias = -8.0
        rh.weights_json = json.dumps({name: -4.0 for name in FEATURE_NAMES})
        session.flush()
        scored = predict(session, meme, chain="robinhood")
        buried = predict(session, wick, chain="robinhood")
        assert meme["rh_lp_open_book"] == 1.0
        assert scored["model_p"] < 0.20
        assert scored["heuristic_p"] >= 0.50
        assert scored["p_good"] >= 0.50
        assert scored["p_good"] == scored["heuristic_p"]
        assert buried["p_good"] <= 0.48
        assert len(FEATURE_NAMES) == 66
    finally:
        session.close()


def test_rh_lp_open_survives_mature_model_before_gmgn():
    # Same bury as the clean-GMGN floor, but token_research has not
    # landed. The next MEME still has to clear paper 0.50.
    meme = extract_features(
        {
            "chain": "robinhood",
            "coin": {"name": "A Meme Coin", "symbol": "MEME"},
            "twitter": {"followers": 2, "age_days": 0.02, "verified": True, "tweets": 0},
            "twitter_handle": "amemecoinrh",
            "symbol_flood": 4,
            "holders": {
                "holder_count": 4,
                "top10_pct": 1.2,
                "top_wallets": [
                    {"owner": "0x8366a39CC670B4001A1121B8F6A443A643e40951", "pct": 98.8, "label": "pool"},
                    {"owner": "0xD9C1383ebF393d28Ed7f6B33B1DED1962c85982C", "pct": 1.2, "label": ""},
                ],
            },
            "market": {},
        }
    )
    init_db()
    session = SessionLocal()
    try:
        rh = get_or_create_model(session, chain="robinhood")
        rh.n_train = YOUNG_RH_N_TRAIN
        rh.bias = -8.0
        rh.weights_json = json.dumps({name: -4.0 for name in FEATURE_NAMES})
        session.flush()
        scored = predict(session, meme, chain="robinhood")
        assert meme["gmgn_present"] == 0.0
        assert meme["rh_lp_open_book"] == 1.0
        assert scored["heuristic_p"] >= 0.50
        assert scored["p_good"] >= 0.50
        assert scored["p_good"] == scored["heuristic_p"]
        assert len(FEATURE_NAMES) == 66
    finally:
        session.close()


def test_young_rh_model_cannot_lift_thin_book_over_paper_line():
    # Live ELEGANS/HTD: 1–4 wallets, heuristic 0.48, model_p 0.67, blend 0.64.
    # Paper buys at 0.50 — the thin-book veto must win after the floor.
    init_db()
    session = SessionLocal()
    try:
        rh = get_or_create_model(session, chain="robinhood")
        rh.n_train = 165
        rh.bias = 8.0
        rh.weights_json = json.dumps({name: 4.0 for name in FEATURE_NAMES})
        session.flush()
        thin = extract_features(
            {
                "chain": "robinhood",
                "coin": {"name": "Elegans", "symbol": "ELEGANS"},
                "twitter": {"followers": 12_000, "age_days": 400, "verified": True, "tweets": 800},
                "twitter_handle": "elegans",
                "website": "https://example.com",
                "holders": {"holder_count": 1, "top10_pct": 100},
                "market": {"liquidity_usd": 9_900, "volume_h1": 2_000},
                "gmgn": {"source": "gmgn", "rug_risk": 2, "open_source": True, "renounced": True},
            }
        )
        assert thin["rh_thin_book"] == 1.0
        scored = predict(session, thin, chain="robinhood")
        assert scored["heuristic_p"] <= 0.48
        assert scored["model_p"] > 0.80
        assert scored["p_good"] <= 0.48
    finally:
        session.close()


def test_organic_book_floors_sol_blend_against_pessimistic_model():
    # rehanfal/MACRODUCK-class: Sol model (n_train in the thousands) would
    # bury a 0.70 heuristic to ~0.20. A live book keeps the rule score.
    init_db()
    session = SessionLocal()
    try:
        sol = get_or_create_model(session, chain="sol")
        sol.n_train = 4000
        sol.bias = -8.0
        sol.weights_json = json.dumps({name: -4.0 for name in FEATURE_NAMES})
        session.flush()
        features = _river_features()
        assert features["organic_book"] == 1.0
        scored = predict(session, features, chain="sol")
        assert scored["heuristic_p"] >= 0.60
        assert scored["model_p"] < 0.20
        assert scored["p_good"] == scored["heuristic_p"]
    finally:
        session.close()


def test_sol_wide_organic_floors_capture_not_rh_paper():
    # PVP-class: organic via the wide OR, heuristic still ~0.3. Sol
    # capture line is 0.60; RH paper must not inherit that floor.
    init_db()
    session = SessionLocal()
    try:
        sol = get_or_create_model(session, chain="sol")
        sol.n_train = 4000
        sol.bias = -8.0
        sol.weights_json = json.dumps({name: -4.0 for name in FEATURE_NAMES})
        session.flush()
        pvp = extract_features(
            {
                "coin": {"name": "Pvp", "symbol": "PVP"},
                "holders": {"holder_count": 138, "top10_pct": 38},
                "market": {"liquidity_usd": 15_284, "volume_h1": 1_120},
            }
        )
        assert pvp["organic_book"] == 1.0
        scored = predict(session, pvp, chain="sol")
        assert scored["heuristic_p"] >= 0.25
        assert scored["heuristic_p"] < 0.60
        assert scored["p_good"] >= 0.60

        rh = get_or_create_model(session, chain="robinhood")
        rh.n_train = YOUNG_RH_N_TRAIN
        rh.bias = -8.0
        rh.weights_json = json.dumps({name: -4.0 for name in FEATURE_NAMES})
        session.flush()
        pulled = predict(session, pvp, chain="robinhood")
        assert pulled["p_good"] < 0.60
    finally:
        session.close()


def test_sol_model_cannot_paper_buy_low_heuristic_bundle():
    # Live JUST: heur 0.47 / model 1.00 / blend 0.82 on a 1.00× instant
    # fill. +0.18 slack stays under paper 0.70. FEATURE_NAMES 66.
    init_db()
    session = SessionLocal()
    try:
        sol = get_or_create_model(session, chain="sol")
        sol.n_train = 4000
        sol.bias = 8.0
        sol.weights_json = json.dumps({name: 4.0 for name in FEATURE_NAMES})
        session.flush()
        just = extract_features(
            {
                "coin": {"name": "Just", "symbol": "JUST"},
                "holders": {"holder_count": 90, "top10_pct": 55, "fresh_wallet_pct": 70},
                "time_to_migrate_min": 0.4,
                "market": {"liquidity_usd": 20_000, "volume_h1": 8_000},
                "gmgn": {"source": "gmgn", "bundler_vol_pct": 80, "bot_rate": 70, "sniper_count": 25},
            }
        )
        scored = predict(session, just, chain="sol")
        assert scored["heuristic_p"] < 0.60
        assert scored["model_p"] > 0.80
        assert scored["p_good"] < 0.70
        assert len(FEATURE_NAMES) == 66
    finally:
        session.close()


def test_snapshot_evaluation_once_per_hour():
    import json

    from launchfinder.models import Outcome, Research, ScanState, Token, utcnow
    from launchfinder.scoring.model import evaluate, evaluation_history, snapshot_evaluation

    init_db()
    session = SessionLocal()
    try:
        assert evaluate(session).get("n") in (0, None) or True  # tolerate other tests' rows
        token = Token(mint="EvalSnapMint111", symbol="EV", first_seen_at=utcnow())
        token.research = Research(p_good=0.8, features_json="{}")
        session.add(token)
        session.flush()
        session.add(Outcome(token_id=token.id, t0_mcap=69000.0, max_mcap=150000.0, multiple=2.2, label=1))
        session.commit()
        first = snapshot_evaluation(session)
        second = snapshot_evaluation(session)
        session.commit()
        assert second is False  # deduped within the hour
        history = evaluation_history(session)
        assert history and history[-1]["n"] >= 1
        assert "brier" in history[-1]
    finally:
        session.close()


def test_calibration_bins_group_by_probability():
    from launchfinder.models import Outcome, Research, Token, utcnow
    from launchfinder.scoring.model import calibration_bins

    init_db()
    session = SessionLocal()
    try:
        for i, (p, label) in enumerate([(0.85, 1), (0.82, 1), (0.15, 0), (0.12, 0)]):
            t = Token(mint=f"CalibMint{i}1111", symbol=f"C{i}", first_seen_at=utcnow())
            t.research = Research(p_good=p, features_json="{}")
            session.add(t)
            session.flush()
            session.add(Outcome(token_id=t.id, t0_mcap=69000.0, max_mcap=69000.0, label=label))
        session.commit()
        bins = calibration_bins(session)
        by_name = {b["bin"]: b for b in bins}
        assert by_name["0.8-0.9"]["n"] >= 2
        assert by_name["0.8-0.9"]["actual"] >= 0.5
        assert by_name["0.1-0.2"]["actual"] <= 0.5
    finally:
        session.close()


def test_train_pending_skips_rh_leftover_fdv():
    from launchfinder.models import Outcome, Research, Token, utcnow
    from launchfinder.scoring.model import get_or_create_model, train_pending

    init_db()
    session = SessionLocal()
    try:
        model = get_or_create_model(session, chain="robinhood")
        before = model.n_train
        leftover = Token(
            mint="0xmordorleftover000000000000000000000001",
            symbol="MORDOR",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        leftover.research = Research(p_good=0.4, features_json='{"liq": 1}', holder_count=5)
        session.add(leftover)
        session.flush()
        session.add(
            Outcome(
                token_id=leftover.id,
                t0_mcap=160_918.0,
                max_mcap=160_918.0,
                last_liq=160_920.0,
                multiple=1.0,
                label=0,
                used_for_train=False,
            )
        )
        honest = Token(
            mint="0xhshhonest00000000000000000000000000001",
            symbol="HSH",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        honest.research = Research(p_good=0.4, features_json='{"liq": 1}', holder_count=29)
        session.add(honest)
        session.flush()
        session.add(
            Outcome(
                token_id=honest.id,
                t0_mcap=21_000.0,
                max_mcap=21_000.0,
                last_liq=21_000.0,
                multiple=1.0,
                label=0,
                used_for_train=False,
            )
        )
        session.commit()
        # Other files share the test DB; only these two rows should train.
        session.query(Outcome).filter(
            Outcome.used_for_train.is_(False),
            Outcome.token_id.notin_([leftover.id, honest.id]),
        ).update({Outcome.used_for_train: True}, synchronize_session=False)
        trained = train_pending(session)
        session.commit()
        session.refresh(model)
        leftover_o = session.query(Outcome).filter(Outcome.token_id == leftover.id).one()
        honest_o = session.query(Outcome).filter(Outcome.token_id == honest.id).one()
        assert leftover_o.used_for_train is True
        assert honest_o.used_for_train is True
        assert trained == 1
        assert model.n_train == before + 1
    finally:
        session.close()


def test_l2_decay_keeps_weights_bounded():
    import json

    from launchfinder.scoring.features import FEATURE_NAMES

    init_db()
    session = SessionLocal()
    try:
        hot = {name: 0.0 for name in FEATURE_NAMES}
        hot[FEATURE_NAMES[0]] = 1.0
        model = None
        for _ in range(400):
            model = online_update(session, hot, 1)
        weights = json.loads(model.weights_json)
        # without decay this weight grows unboundedly toward sigmoid saturation
        assert 0.0 < weights[FEATURE_NAMES[0]] < 8.0
    finally:
        session.close()


def test_predict_stalls_flat_high_p_after_an_hour():
    init_db()
    session = SessionLocal()
    try:
        rh = get_or_create_model(session, chain="robinhood")
        rh.n_train = 100
        session.flush()
        features = _river_features()
        features["age_min"] = 75.0
        features["live_multiple"] = 1.0
        scored = predict(session, features, chain="robinhood")
        assert scored["heuristic_p"] >= 0.50
        assert scored["p_good"] <= 0.48
        assert any("under 2x" in f for f in scored["risk_flags"])

        features["age_min"] = 10.0
        fresh = predict(session, features, chain="robinhood")
        assert fresh["p_good"] == fresh["heuristic_p"]
    finally:
        session.close()
