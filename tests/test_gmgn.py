from launchfinder.research.gmgn import _ban_pause_seconds, parse_kline_rows, summarize, summarize_trench_row, trench_to_coin
from launchfinder.research.holders import holders_from_gmgn
from launchfinder.scoring.features import extract_features, heuristic_probability


def test_banned_account_sits_out_at_least_ten_minutes():
    import time

    from launchfinder.config import settings
    from launchfinder.research import gmgn as gmgn_mod

    assert _ban_pause_seconds({"error": "RATE_LIMIT_BANNED", "reset_at": 0}) >= 900
    # a 60s reset_at must not win — poking every minute extends the ban
    soon = time.time() + 60
    assert _ban_pause_seconds({"error": "RATE_LIMIT_BANNED", "reset_at": soon}) >= 900
    assert 60 <= _ban_pause_seconds({"error": "RATE_LIMIT_EXCEEDED", "reset_at": soon}) <= 120
    old_key = settings.gmgn_api_key
    object.__setattr__(settings, "gmgn_api_key", "test-key")
    try:
        now = time.time()
        gmgn_mod._cooldown_until = now + 100
        gmgn_mod._deep_until = now + 700
        assert gmgn_mod.gmgn_available() is False
        gmgn_mod._cooldown_until = now - 1
        assert gmgn_mod.gmgn_available() is True
        assert gmgn_mod.gmgn_deep_available() is False
        gmgn_mod._deep_until = now - 1
        assert gmgn_mod.gmgn_deep_available() is True
    finally:
        object.__setattr__(settings, "gmgn_api_key", old_key)
        gmgn_mod._cooldown_until = 0.0
        gmgn_mod._deep_until = 0.0


def test_summarize_normalizes_ratio_fields():
    out = summarize(
        {"holder_count": 420, "honeypot": False},
        {
            "is_honeypot": 0,
            "is_bundled": "true",
            "insider_percent": 0.31,
            "sniper_percent": 18,
            "rug_ratio": 0.62,
        },
        "Mint111",
    )
    assert out["source"] == "gmgn"
    assert out["url"].endswith("Mint111")
    assert out["honeypot"] is False
    assert out["bundled"] is True
    assert out["insider_pct"] == 31.0
    assert out["sniper_pct"] == 18.0
    assert out["rug_risk"] == 62.0
    assert out["holder_count"] == 420


def test_gmgn_honeypot_cuts_score():
    clean = extract_features(
        {
            "coin": {"name": "River", "symbol": "RVR", "reply_count": 180},
            "twitter": {"followers": 12000, "age_days": 900, "verified": True},
            "twitter_handle": "river",
            "website": "https://example.com",
            "holders": {"holder_count": 900, "top10_pct": 22, "creator_hold_pct": 4},
            "creator_stats": {"launches": 2, "wins": 1, "rugs": 0},
            "time_to_migrate_min": 80,
            "market": {"buys_m5": 40, "sells_m5": 10, "liquidity_usd": 28000, "volume_h1": 15000},
            "gmgn": {"source": "gmgn", "honeypot": False, "bundled": False, "insider_pct": 4, "sniper_pct": 6, "rug_risk": 8},
        }
    )
    dirty = dict(clean)
    dirty["gmgn_honeypot"] = 1.0
    dirty["gmgn_bundled"] = 1.0
    dirty["gmgn_rug_n"] = 0.7
    p_clean = heuristic_probability(clean, [], [])
    p_dirty = heuristic_probability(dirty, [], [])
    assert p_clean > 0.6
    assert p_dirty < p_clean - 0.2


def test_trench_row_becomes_fresh_coin():
    row = {
        "address": "Abcdefghijklmnopqrstuvwxyz123456789pump",
        "symbol": "GROVE",
        "name": "Grove",
        "usd_market_cap": 82000,
        "created_timestamp": 1_777_000_000,
        "open_timestamp": 1_777_000_400,
        "rug_ratio": 0.08,
        "smart_degen_count": 12,
        "renowned_count": 3,
        "bundler_rate": 0.04,
        "is_honeypot": 0,
        "holder_count": 510,
        "top_10_holder_rate": 0.22,
    }
    coin = trench_to_coin(row)
    assert coin["mint"].endswith("pump")
    assert coin["complete"] is True
    snap = summarize_trench_row(row)
    assert snap["from_trenches"] is True
    assert snap["smart_degen"] == 12
    assert snap["renowned"] == 3
    assert snap["rug_risk"] == 8.0
    features = extract_features(
        {
            "coin": coin,
            "gmgn": snap,
            "holders": {"holder_count": 510, "top10_pct": 22},
            "creator_stats": {"launches": 1, "wins": 0, "rugs": 0},
            "time_to_migrate_min": 40,
            "market": {"buys_m5": 20, "sells_m5": 8, "liquidity_usd": 18000, "volume_h1": 9000},
        }
    )
    assert features["gmgn_smart_n"] > 0
    reasons, flags = [], []
    p = heuristic_probability(features, reasons, flags)
    assert any("smart-money" in r.lower() for r in reasons)
    assert p > 0.3


def test_rh_trench_reads_market_cap_and_iso_created():
    # Live RH completed rows often omit usd_market_cap / created_timestamp.
    # Solana's 18h freshness gate then dropped them (mcap=0, created=None).
    from launchfinder.research.gmgn import _ts

    iso = "2026-08-31T20:00:00Z"
    assert _ts(iso) is not None
    coin = trench_to_coin(
        {
            "address": "0xAbC00000000000000000000000000000000000aa",
            "symbol": "BONDS",
            "market_cap": 28_400,
            "created_at": iso,
            "open_time": "2026-08-31T20:05:00Z",
        },
        chain="robinhood",
    )
    assert coin["mcap_usd"] == 28_400
    assert coin["created_at"] is not None
    assert coin["updated_at"] is not None


def test_trench_card_fields_are_mapped():
    # Live RH trenches already ship tax / OG / socials / lock / TX / N+.
    # We used to keep only the first 40 raw keys and show rug/insider/sniper.
    row = {
        "address": "0xcard000000000000000000000000000000000001",
        "symbol": "CARD",
        "og": True,
        "buy_tax": 0.01,
        "sell_tax": 0.11,
        "launchpad_progress": 0.49,
        "lock_percent": 0.8,
        "burn_status": "yes",
        "dev_token_burn_ratio": 0.03,
        "swaps_1h": 142,
        "volume_1h": 3300,
        "net_buy_24h": 146,
        "x_user_follower": 18400,
        "twitter_username": "Memecompass",
        "website": "https://example.com",
        "telegram": "https://t.me/card",
        "suspected_insider_hold_rate": 0.18,
        "creator_balance_rate": 0.05,
        "top_10_holder_rate": 0.22,
        "creator_token_status": "creator_hold",
        "fund_from_address": "0xfund",
        "creator_created_open_count": 4,
        "cto_flag": False,
        "image_dup": 3,
        "hot_level": 2,
        "launchpad_platform": "pons",
        "is_open_source": "yes",
        "holder_count": 88,
    }
    snap = summarize_trench_row(row, chain="robinhood")
    assert snap["og"] is True
    assert snap["buy_tax_pct"] == 1.0
    assert snap["sell_tax_pct"] == 11.0
    assert snap["progress"] == 0.49
    assert snap["lock_pct"] == 80.0
    assert snap["burned"] is True
    assert snap["swaps_1h"] == 142
    assert snap["volume_1h"] == 3300
    assert snap["net_buy_24h"] == 146
    assert snap["twitter_username"] == "Memecompass"
    assert snap["twitter_followers"] == 18400
    assert snap["insider_pct"] == 18.0
    assert snap["dev_hold_pct"] == 5.0
    assert snap["dev_sold"] is False
    assert snap["creator_status"] == "creator_hold"
    assert snap["fund_from"] == "0xfund"
    assert snap["creator_open_count"] == 4
    assert snap["image_dup"] == 3
    assert snap["launchpad"] == "pons"
    assert snap["open_source"] is True
    coin = trench_to_coin(row, chain="robinhood")
    assert coin["twitter"] == "https://x.com/Memecompass"
    assert coin["website"] == "https://example.com"
    assert coin["telegram"] == "https://t.me/card"


def test_status_url_is_not_a_handle_or_follower_count():
    snap = summarize_trench_row(
        {
            "address": "0xstatus0000000000000000000000000000000001",
            "twitter": "https://x.com/i/status/2094619266943242465",
            "twitter_username": "i/status/2094619266943242465",
            "x_user_follower": 241_527_587,
        },
        chain="robinhood",
    )
    assert snap["twitter_username"] == ""
    assert snap["twitter_followers"] == 0
    named = summarize_trench_row(
        {
            "address": "0xstatus0000000000000000000000000000000002",
            "twitter": "https://x.com/Gap2026_Dev/status/2094616276937449821",
            "twitter_username": "Gap2026_Dev/status/2094616276937449821",
            "x_user_follower": 166,
        },
        chain="robinhood",
    )
    assert named["twitter_username"] == "Gap2026_Dev"
    assert named["twitter_followers"] == 0


def test_high_tax_and_dup_logo_are_penalized():
    from launchfinder.scoring.features import extract_features, heuristic_probability

    dirty = extract_features(
        {
            "coin": {"name": "Taxed", "symbol": "TAX"},
            "gmgn": {
                "source": "gmgn",
                "buy_tax_pct": 11,
                "sell_tax_pct": 11,
                "image_dup": 3,
                "og": False,
            },
        }
    )
    clean = extract_features(
        {
            "coin": {"name": "Clean", "symbol": "CLN"},
            "gmgn": {
                "source": "gmgn",
                "buy_tax_pct": 0,
                "sell_tax_pct": 0,
                "og": True,
                "lock_pct": 80,
                "burned": True,
            },
        }
    )
    assert dirty["gmgn_tax_n"] >= 0.1
    assert dirty["gmgn_image_dup"] >= 0.5
    assert clean["gmgn_og"] == 1.0
    flags: list[str] = []
    reasons: list[str] = []
    p_dirty = heuristic_probability(dirty, [], flags)
    p_clean = heuristic_probability(clean, reasons, [])
    assert any("tax" in f.lower() for f in flags)
    assert any("logo" in f.lower() for f in flags)
    assert any("og" in r.lower() for r in reasons)
    assert p_clean > p_dirty


def test_refresh_known_merges_trench_card(monkeypatch):
    import json

    from launchfinder.db import SessionLocal, init_db
    from launchfinder.ingest.rh_poll import _refresh_known_from_trench
    from launchfinder.ingest.store import upsert_migration
    from launchfinder.models import Research

    init_db()
    session = SessionLocal()
    try:
        token = upsert_migration(
            session,
            mint="0xrefresh00000000000000000000000000000001",
            source="rh_dex",
            coin={"chain": "robinhood", "name": "Ponis", "symbol": "PONIS"},
        )
        token.research = Research(token=token, raw_json=json.dumps({"gmgn": {}}), holder_count=0)
        session.add(token.research)
        session.flush()
        _refresh_known_from_trench(
            session,
            {
                "mint": token.mint,
                "twitter": "https://x.com/ponis",
                "gmgn_row": {
                    "address": token.mint,
                    "og": True,
                    "buy_tax": 0.02,
                    "holder_count": 64,
                    "top_10_holder_rate": 0.31,
                    "twitter_username": "ponis",
                    "x_user_follower": 900,
                },
            },
        )
        session.flush()
        raw = json.loads(token.research.raw_json)
        assert raw["gmgn"]["og"] is True
        assert raw["gmgn"]["buy_tax_pct"] == 2.0
        assert token.research.holder_count == 64
        assert token.twitter == "https://x.com/ponis"
        assert token.research.twitter_handle == "ponis"
        assert token.research.twitter_followers == 900
    finally:
        session.close()


def test_holders_from_gmgn_fills_empty_helius():
    assert holders_from_gmgn({}) == {}
    assert holders_from_gmgn({"holder_count": 0, "top10_pct": 0}) == {}
    out = holders_from_gmgn({"holder_count": 180, "top10_pct": 28.0, "dev_hold_pct": 4.5, "fresh_wallet_pct": 12})
    assert out["holder_count"] == 180
    assert out["top10_pct"] == 28.0
    assert out["creator_hold_pct"] == 4.5
    assert out["source"] == "gmgn"
    features = extract_features(
        {
            "chain": "robinhood",
            "coin": {"name": "Magpie", "symbol": "MAGPIE"},
            "holders": out,
            "market": {"mcap_usd": 40_000, "liquidity_usd": 20_000},
        }
    )
    assert features["holder_n"] > 0
    assert features["top10_inv"] > 0.6


def test_parse_kline_rows_reads_usd_ohlcv_and_drops_empty():
    from datetime import datetime, timezone

    rows = parse_kline_rows(
        {
            "list": [
                {"time": 1_726_574_400_000, "open": "0.001", "high": "0.0012", "low": "0.0009", "close": "0.0011", "volume": "400"},
                {"time": 1_726_574_460_000, "close": "0"},
            ]
        }
    )
    assert len(rows) == 1
    assert rows[0]["close"] == 0.0011 and rows[0]["volume"] == 400.0
    assert isinstance(rows[0]["time"], datetime) and rows[0]["time"].tzinfo == timezone.utc
    assert parse_kline_rows({"list": []}) == []
