from launchfinder.research.gmgn import summarize
from launchfinder.scoring.features import extract_features, heuristic_probability


def _nested_info() -> dict:
    return {
        "holder_count": 500,
        "migration_market_cap": 71234.5,
        "stat": {
            "holder_count": 500,
            "top_10_holder_rate": 0.22,
            "creator_hold_rate": 0.0,
            "bot_degen_rate": 0.72,
            "fresh_wallet_rate": 0.4,
        },
        "dev": {
            "creator_token_status": "creator_close",
            "creator_open_count": 3,
            "ath_token_info": {"ath_mc": "8500000"},
            "dexscr_ad": 0,
            "dexscr_boost_fee": 0,
        },
        "wallet_tags_stat": {"smart_wallets": 5, "renowned_wallets": 1, "sniper_wallets": 30},
        "price": {"buy_volume_1h": 12000.0, "sell_volume_1h": 30000.0},
    }


def _security() -> dict:
    return {
        "rug_ratio": 0.05,
        "is_wash_trading": True,
        "sniper_count": 30,
        "bundler_trader_amount_rate": 0.45,
        "renounced_mint": True,
        "renounced_freeze_account": True,
        "creator_token_status": "creator_close",
    }


def test_summarize_extracts_nested_research_fields():
    out = summarize(_nested_info(), _security(), "MintXYZ")
    assert out["bot_rate"] == 72.0
    assert out["wash_trading"] is True
    assert out["sniper_count"] == 30
    assert out["bundler_vol_pct"] == 45.0
    assert out["dev_sold"] is True
    assert out["creator_ath_mc"] == 8_500_000.0
    assert out["migration_mcap"] == 71234.5
    assert out["smart_degen"] == 5
    assert out["top10_pct"] == 22.0
    assert out["renounced"] is True


def test_bot_wash_and_sell_pressure_are_penalized():
    gmgn = summarize(_nested_info(), _security(), "MintXYZ")
    features = extract_features({"coin": {"name": "Test", "symbol": "TST"}, "gmgn": gmgn})
    assert features["gmgn_bot_rate_n"] > 0.5
    assert features["gmgn_wash"] == 1.0
    assert features["usd_buy_pressure"] < 0.35
    flags: list[str] = []
    p_dirty = heuristic_probability(features, [], flags)
    assert any("wash" in f.lower() for f in flags)
    assert any("bot" in f.lower() for f in flags)

    clean = dict(features)
    clean.update(
        {
            "gmgn_wash": 0.0,
            "gmgn_bot_rate_n": 0.1,
            "gmgn_sniper_count_n": 0.1,
            "gmgn_bundler_vol_n": 0.05,
            "usd_buy_pressure": 0.7,
        }
    )
    p_clean = heuristic_probability(clean, [], [])
    assert p_clean > p_dirty


def test_proven_creator_is_rewarded():
    gmgn = summarize(_nested_info(), _security(), "MintXYZ")
    features = extract_features({"coin": {"name": "Test", "symbol": "TST"}, "gmgn": gmgn})
    assert features["gmgn_creator_ath_n"] > 0.5
    reasons: list[str] = []
    heuristic_probability(features, reasons, [])
    assert any("multi-million" in r for r in reasons)
