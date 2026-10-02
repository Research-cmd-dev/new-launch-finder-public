from launchfinder.models import Token
from launchfinder.research.pipeline import _is_doa_junk


def _t() -> Token:
    return Token(mint="DoaMint111", symbol="X", name="X")


def test_banned_and_bait_names_are_junk():
    assert _is_doa_junk(_t(), {"banned": True}, {})
    assert _is_doa_junk(_t(), {"name": "CLAIM your airdrop", "symbol": "FREE"}, {})
    assert _is_doa_junk(_t(), {"name": "visit http site", "symbol": "URL"}, {})


def test_empty_pool_is_junk():
    assert _is_doa_junk(_t(), {"name": "Cat", "symbol": "CAT"}, {"liquidity_usd": 300, "mcap_usd": 5_000})


def test_entry_collapse_flag_and_penalty():
    from launchfinder.scoring.features import extract_features, heuristic_probability

    ctx = {
        "coin": {"name": "Hendrik Holt", "symbol": "$WIND"},
        "market": {"mcap_usd": 8.49, "liquidity_usd": 1.17},
        "holders": {"holder_count": 20, "top10_pct": 94.6},
    }
    features = extract_features(ctx)
    assert features["entry_collapse"] == 1.0
    flags: list[str] = []
    p = heuristic_probability(features, [], flags)
    assert p < 0.25
    assert any("start-high rug" in f for f in flags)

    healthy = extract_features({**ctx, "market": {"mcap_usd": 90_000, "liquidity_usd": 30_000}})
    assert healthy["entry_collapse"] == 0.0


def test_robinhood_20k_launch_is_not_entry_collapse():
    from launchfinder.scoring.features import extract_features, heuristic_probability

    ctx = {
        "chain": "robinhood",
        "coin": {"name": "Long X", "symbol": "LONGX"},
        "market": {"mcap_usd": 20_560.0, "liquidity_usd": 20_560.0},
    }
    features = extract_features(ctx)
    assert features["entry_collapse"] == 0.0
    flags: list[str] = []
    p = heuristic_probability(features, [], flags)
    assert not any("start-high rug" in f for f in flags)
    assert p > 0.1

    # same print on Solana is still a collapse vs the $69k floor
    sol = extract_features({**ctx, "chain": "sol"})
    assert sol["entry_collapse"] == 1.0


def test_robinhood_13k_live_book_is_not_entry_collapse():
    # JJJACKET / POV: $13k t0 on an $11k live pool is a small launch, not
    # a dump below the $40k floor. 0.4×$40k=$16k used to brand them rugs.
    from launchfinder.scoring.features import extract_features, heuristic_probability

    ctx = {
        "chain": "robinhood",
        "coin": {"name": "JJ Jacket", "symbol": "JJJACKET"},
        "market": {"mcap_usd": 12_929.0, "liquidity_usd": 11_632.0},
        "holders": {"holder_count": 186, "top10_pct": 30},
    }
    features = extract_features(ctx)
    assert features["entry_collapse"] == 0.0
    flags: list[str] = []
    heuristic_probability(features, [], flags)
    assert not any("start-high rug" in f for f in flags)

    drained = extract_features({
        **ctx,
        "market": {"mcap_usd": 5_000.0, "liquidity_usd": 100.0},
    })
    assert drained["entry_collapse"] == 1.0


def test_robinhood_trench_timestamps_are_not_instant_fill():
    from launchfinder.scoring.features import extract_features, heuristic_probability

    ctx = {
        "chain": "robinhood",
        "coin": {"name": "Epic Face", "symbol": "EPICFACE"},
        "market": {"mcap_usd": 20_185.0, "liquidity_usd": 20_000.0},
        "time_to_migrate_min": 0.016,
    }
    features = extract_features(ctx)
    assert features["migrate_speed"] >= 0.15
    flags: list[str] = []
    heuristic_probability(features, [], flags)
    assert not any("instantly" in f.lower() for f in flags)

    sol = extract_features({**ctx, "chain": "sol"})
    assert sol["migrate_speed"] == 0.05


def test_pre_pumped_migration_is_flagged():
    from launchfinder.scoring.features import extract_features, heuristic_probability

    # USWS: $2.1M mcap 40 seconds after migration, 66 holders
    ctx = {
        "coin": {"name": "United States Water Supply", "symbol": "USWS"},
        "market": {"mcap_usd": 2_147_855.0, "liquidity_usd": 123_509.0, "buys_m5": 39, "sells_m5": 14},
        "holders": {"holder_count": 66, "top10_pct": 36.5},
        "time_to_migrate_min": 0.67,
    }
    features = extract_features(ctx)
    assert features["entry_premium"] == 1.0
    assert features["mcap_per_holder_n"] >= 0.5
    flags: list[str] = []
    p = heuristic_probability(features, [], flags)
    assert p < 0.3
    assert any("Pre-pumped" in f for f in flags)
    assert any("Tiny holder base" in f for f in flags)

    # organic entry at graduation scale is untouched
    organic = extract_features({
        "coin": {"name": "Cat", "symbol": "CAT"},
        "market": {"mcap_usd": 95_000.0, "liquidity_usd": 25_000.0},
        "holders": {"holder_count": 400},
        "time_to_migrate_min": 45,
    })
    assert organic["entry_premium"] == 0.0
    assert organic["mcap_per_holder_n"] < 0.1

    # Live KFC: leftover t0 $146k (3.7× RH floor) with a $40k Dex book.
    # Market-only mcap missed the premium; stored t0 must flag it.
    kfc = extract_features(
        {
            "chain": "robinhood",
            "coin": {"name": "Kentucky Fried Cookie", "symbol": "KFC"},
            "market": {"mcap_usd": 40_826.0, "liquidity_usd": 40_826.0, "volume_h1": 8_000},
            "holders": {"holder_count": 1222, "top10_pct": 40},
            "time_to_migrate_min": 12,
            "t0_mcap": 146_816.0,
        }
    )
    assert kfc["entry_premium"] == 1.0
    assert kfc["organic_book"] == 0.0
    honest_rh = extract_features(
        {
            "chain": "robinhood",
            "coin": {"name": "Cat", "symbol": "CAT"},
            "market": {"mcap_usd": 40_000.0, "liquidity_usd": 22_000.0, "volume_h1": 4_000},
            "holders": {"holder_count": 80},
            "time_to_migrate_min": 12,
            "t0_mcap": 40_000.0,
        }
    )
    assert honest_rh["entry_premium"] == 0.0


def test_model_cannot_overrule_hard_rules():
    from launchfinder.db import SessionLocal, init_db
    from launchfinder.scoring.features import FEATURE_NAMES
    from launchfinder.scoring.model import online_update, predict

    init_db()
    session = SessionLocal()
    try:
        # teach the model to love a feature pattern the rules hate
        hot = {name: 0.0 for name in FEATURE_NAMES}
        hot["entry_premium"] = 1.0
        hot["liquidity_n"] = 0.9
        for _ in range(300):
            online_update(session, hot, 1, lr=0.3)
        scored = predict(session, hot)
        # heuristic tanks it; blend must stay within +0.35 of the rules
        assert scored["p_good"] <= scored["heuristic_p"] + 0.351
    finally:
        session.close()


def test_healthy_token_is_not_junk():
    assert not _is_doa_junk(_t(), {"name": "Cat", "symbol": "CAT"}, {"liquidity_usd": 25_000, "mcap_usd": 90_000})
    # no market data yet: give it the benefit of the doubt
    assert not _is_doa_junk(_t(), {"name": "Cat", "symbol": "CAT"}, {})
