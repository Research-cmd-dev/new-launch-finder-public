from launchfinder.scoring.features import (
    FEATURE_NAMES,
    ath_dump_honesty_cap,
    extract_features,
    heuristic_probability,
    is_organic_book,
    social_ticket_cap,
    stall_honesty_cap,
)


def test_bundle_and_concentration_are_penalized():
    features = extract_features(
        {
            "coin": {"name": "claim airdrop", "symbol": "CLAIM99999", "nsfw": False, "reply_count": 1},
            "twitter": {"followers": 2, "age_days": 0.1, "verified": False},
            "twitter_handle": "newcoinxyz",
            "holders": {"holder_count": 12, "top10_pct": 88, "creator_hold_pct": 40},
            "creator_stats": {"launches": 12, "wins": 0, "rugs": 8},
            "time_to_migrate_min": 0.4,
            "market": {"buys_m5": 1, "sells_m5": 20, "liquidity_usd": 200, "volume_h1": 10},
        }
    )
    reasons, flags = [], []
    p = heuristic_probability(features, reasons, flags)
    assert p < 0.35
    assert any("bundle" in f.lower() or "instant" in f.lower() for f in flags)


def test_symbol_flood_is_penalized():
    base = {
        "coin": {"name": "Rocket", "symbol": "RKT", "reply_count": 50},
        "holders": {"holder_count": 300, "top10_pct": 30, "creator_hold_pct": 5},
        "time_to_migrate_min": 45,
        "market": {"buys_m5": 20, "sells_m5": 10, "liquidity_usd": 20000, "volume_h1": 8000},
    }
    clean = extract_features(base)
    flooded = extract_features({**base, "symbol_flood": 4})
    assert flooded["symbol_flood_n"] == 1.0
    assert clean["symbol_flood_n"] == 0.0
    flags: list[str] = []
    p_flooded = heuristic_probability(flooded, [], flags)
    p_clean = heuristic_probability(clean, [], [])
    assert p_flooded < p_clean
    assert any("copycat" in f.lower() for f in flags)


def test_launch_count_without_rugs_is_not_serial():
    few = extract_features(
        {
            "coin": {"name": "Volume", "symbol": "VOL"},
            "creator_stats": {"launches": 8, "wins": 0, "rugs": 0},
            "holders": {"holder_count": 80, "top10_pct": 25},
            "market": {"liquidity_usd": 20_000, "volume_h1": 8_000},
        }
    )
    factory = extract_features(
        {
            "coin": {"name": "Factory", "symbol": "FAC"},
            "creator_stats": {"launches": 20, "wins": 0, "rugs": 0},
            "holders": {"holder_count": 80, "top10_pct": 25},
            "market": {"liquidity_usd": 20_000, "volume_h1": 8_000},
        }
    )
    rugged = extract_features(
        {
            "coin": {"name": "Rugmill", "symbol": "RUG"},
            "creator_stats": {"launches": 8, "wins": 0, "rugs": 3},
            "holders": {"holder_count": 12, "top10_pct": 90},
            "market": {"liquidity_usd": 200, "volume_h1": 10},
        }
    )
    assert few["creator_serial_penalty"] == 0.0
    assert factory["creator_serial_penalty"] == 0.25
    assert rugged["creator_serial_penalty"] >= 0.9


def test_organic_book_lifts_live_runners_not_lottery_tickets():
    # MACRODUCK-class: live book, some bots, "serial" launch count.
    runner = extract_features(
        {
            "coin": {"name": "Macro Duck", "symbol": "MACRODUCK"},
            "twitter": {"followers": 6500, "age_days": 500, "verified": True},
            "twitter_handle": "esotericpigeon",
            "website": "https://example.com",
            "holders": {"holder_count": 432, "top10_pct": 36, "creator_hold_pct": 5},
            "creator_stats": {"launches": 8, "wins": 1, "rugs": 0},
            "time_to_migrate_min": 80,
            "market": {"buys_m5": 30, "sells_m5": 22, "liquidity_usd": 40_000, "volume_h1": 60_000},
            "gmgn": {
                "source": "gmgn",
                "smart_degen": 20,
                "bot_rate": 67,
                "bundler_vol_pct": 40,
                "rug_risk": 5,
            },
        }
    )
    lottery = extract_features(
        {
            "coin": {"name": "Pappy", "symbol": "Pappy"},
            "holders": {"holder_count": 13, "top10_pct": 100, "creator_hold_pct": 40},
            "creator_stats": {"launches": 3, "wins": 0, "rugs": 0},
            "time_to_migrate_min": 400,
            "market": {"liquidity_usd": 0, "volume_h1": 0},
            "gmgn": {"source": "gmgn", "rug_risk": 37},
        }
    )
    pov = extract_features(
        {
            "chain": "robinhood",
            "coin": {"name": "Pov", "symbol": "POV"},
            "holders": {"holder_count": 62, "top10_pct": 20, "creator_hold_pct": 2},
            "creator_stats": {"launches": 8, "wins": 0, "rugs": 0},
            "market": {"liquidity_usd": 0, "volume_h1": 0},
            "gmgn": {"source": "gmgn", "smart_degen": 8, "rug_risk": 4},
        }
    )
    assert runner["organic_book"] == 1.0
    assert lottery["organic_book"] == 0.0
    reasons, flags = [], []
    p_runner = heuristic_probability(runner, reasons, flags)
    p_lottery = heuristic_probability(lottery, [], [])
    p_pov = heuristic_probability(pov, [], [])
    assert p_runner >= 0.6
    assert p_lottery < 0.45
    assert p_pov >= 0.5
    assert any("organic" in r.lower() for r in reasons)
    assert not any("smart-money wallets marked yet" in f.lower() for f in flags)

    thin = extract_features(
        {
            "coin": {"name": "Dust", "symbol": "DUST"},
            "holders": {"holder_count": 30, "top10_pct": 40},
            "market": {"liquidity_usd": 200, "volume_h1": 80},
        }
    )
    assert thin["organic_book"] == 0.0

    # Live PVP: 138 wallets / $15k liq / $1.1k hour-one vol. Classic
    # organic required vol_n 0.74 (~$3.5k). Wide path catches it.
    pvp = extract_features(
        {
            "coin": {"name": "Pvp", "symbol": "PVP"},
            "holders": {"holder_count": 138, "top10_pct": 38, "creator_hold_pct": 4},
            "market": {"liquidity_usd": 15_284, "volume_h1": 1_120},
        }
    )
    assert pvp["organic_book"] == 1.0
    assert pvp["liquidity_n"] >= 0.75
    assert pvp["volume_n"] >= 0.64
    assert pvp["holder_n"] >= 0.64
    solly = extract_features(
        {
            "coin": {"name": "Solly", "symbol": "SOLLY"},
            "holders": {"holder_count": 12, "top10_pct": 100},
            "market": {"liquidity_usd": 8_000, "volume_h1": 2_000},
        }
    )
    assert solly["organic_book"] == 0.0


def test_last_liq_fills_empty_dex_snapshot():
    # Live CHAD: Dex market empty, worker last_liq is a $14k book.
    empty = extract_features(
        {
            "coin": {"name": "Chad", "symbol": "CHAD"},
            "holders": {"holder_count": 175},
            "market": {"liquidity_usd": 0, "volume_h1": 0},
        }
    )
    filled = extract_features(
        {
            "coin": {"name": "Chad", "symbol": "CHAD"},
            "holders": {"holder_count": 175},
            "market": {"liquidity_usd": 0, "volume_h1": 0},
            "last_liq": 14_448,
        }
    )
    dust = extract_features(
        {
            "coin": {"name": "Dust", "symbol": "DUST"},
            "holders": {"holder_count": 40},
            "market": {"liquidity_usd": 0},
            "last_liq": 200,
        }
    )
    assert empty["liquidity_n"] == 0.0
    assert filled["liquidity_n"] >= 0.75
    assert dust["liquidity_n"] == 0.0


def test_proven_creator_tight_book_is_not_floored():
    # CAC/BEAST-class: 18–34 holders, 100% top10, but the creator already
    # printed winners. Heuristic must not collapse to the 0.02 floor.
    features = extract_features(
        {
            "coin": {"name": "Beast", "symbol": "BEAST"},
            "holders": {"holder_count": 18, "top10_pct": 100, "creator_hold_pct": 8},
            "creator_stats": {"launches": 4, "wins": 2, "rugs": 0},
            "market": {"liquidity_usd": 0, "volume_h1": 0},
        }
    )
    reasons: list[str] = []
    p = heuristic_probability(features, reasons, [])
    assert p >= 0.28
    assert p < 0.55
    assert any("prior winners" in r.lower() or "proven creator" in r.lower() for r in reasons)
    assert heuristic_probability({"top10_inv": 0.0}, [], []) == 0.50


def test_organic_profile_scores_higher():
    features = extract_features(
        {
            "coin": {"name": "River", "symbol": "RVR", "reply_count": 180},
            "twitter": {"followers": 12000, "age_days": 900, "verified": True},
            "twitter_handle": "river",
            "website": "https://example.com",
            "telegram": "https://t.me/river",
            "github_url": "https://github.com/river/docs",
            "github": {"stars": 40, "age_days": 200},
            "holders": {"holder_count": 900, "top10_pct": 22, "creator_hold_pct": 4},
            "creator_stats": {"launches": 2, "wins": 1, "rugs": 0},
            "time_to_migrate_min": 80,
            "market": {"buys_m5": 40, "sells_m5": 10, "liquidity_usd": 28000, "volume_h1": 15000},
        }
    )
    p = heuristic_probability(features, [], [])
    assert p > 0.6


def test_rh_pons_and_available_tape_lift_the_score():
    base = {
        "chain": "robinhood",
        "coin": {"name": "Jacket", "symbol": "JJJACKET"},
        "holders": {"holder_count": 80, "top10_pct": 22, "creator_hold_pct": 3},
        "creator_stats": {"launches": 2, "wins": 0, "rugs": 0},
        "market": {"liquidity_usd": 6_000, "volume_h1": 4_000, "buys_m5": 18, "sells_m5": 10},
        "gmgn": {"source": "gmgn", "rug_risk": 4, "swaps_1h": 220, "hot_level": 3, "volume_1h": 8_000},
    }
    dex = extract_features({**base, "source": "rh_dex", "gmgn": {**base["gmgn"], "launchpad": "dex"}})
    pons = extract_features({**base, "source": "rh_pons", "gmgn": {**base["gmgn"], "launchpad": "pons"}})
    mentioned = extract_features({**base, "source": "rh_pons", "x_mentions": 20, "gmgn": {**base["gmgn"], "launchpad": "pons"}})
    assert pons["launchpad_pons"] == 1.0
    assert dex["launchpad_pons"] == 0.0
    assert pons["gmgn_activity_n"] > 0.4
    assert "launchpad_pons" in FEATURE_NAMES
    reasons: list[str] = []
    p_pons = heuristic_probability(pons, reasons, [])
    p_dex = heuristic_probability(dex, [], [])
    p_mentions = heuristic_probability(mentioned, [], [])
    assert p_pons > p_dex
    assert p_mentions > p_pons
    assert any("pons" in r.lower() for r in reasons)


def test_score_uses_the_gmgn_book_when_dex_is_empty():
    # Live RH often has a trench card and no Dex book yet. Scoring only
    # Dex left POV-class names looking like lottery tickets.
    card = {
        "chain": "robinhood",
        "coin": {"name": "Pov", "symbol": "POV"},
        "holders": {"holder_count": 2, "top10_pct": 100},
        "market": {},
        "gmgn": {
            "source": "gmgn",
            "holder_count": 88,
            "top10_pct": 22,
            "dev_hold_pct": 3,
            "liquidity": 6_000,
            "volume_1h": 4_200,
            "swaps_1h": 140,
            "rug_risk": 4,
            "renounced": True,
            "open_source": True,
            "net_buy_24h": 8_000,
            "twitter_username": "povcoin",
            "twitter_followers": 4_000,
        },
    }
    filled = extract_features(card)
    blind = extract_features(
        {
            "chain": "robinhood",
            "coin": {"name": "Pov", "symbol": "POV"},
            "holders": {"holder_count": 2, "top10_pct": 100},
            "market": {},
            "gmgn": {"source": "gmgn", "rug_risk": 4},
        }
    )
    assert filled["holder_n"] > blind["holder_n"]
    assert filled["liquidity_n"] >= 0.75
    assert filled["volume_n"] >= 0.74
    assert filled["has_twitter"] == 1.0
    assert filled["gmgn_renounced"] == 1.0
    reasons: list[str] = []
    p_filled = heuristic_probability(filled, reasons, [])
    p_blind = heuristic_probability(blind, [], [])
    assert p_filled > p_blind
    assert p_filled >= 0.5
    assert any("renounced" in r.lower() or "organic" in r.lower() for r in reasons)


def test_rat_volume_is_penalized():
    base = {
        "coin": {"name": "Rat", "symbol": "RAT"},
        "holders": {"holder_count": 80, "top10_pct": 25},
        "market": {"liquidity_usd": 8_000, "volume_h1": 4_000},
        "gmgn": {"source": "gmgn", "rug_risk": 5, "rat_vol_pct": 0},
    }
    clean = extract_features(base)
    dirty = extract_features({**base, "gmgn": {**base["gmgn"], "rat_vol_pct": 45}})
    flags: list[str] = []
    assert dirty["gmgn_rat_n"] >= 0.4
    assert heuristic_probability(dirty, [], flags) < heuristic_probability(clean, [], [])
    assert any("rat" in f.lower() for f in flags)


def test_rh_thin_book_cannot_clear_the_paper_line():
    # Live JOHNAPPL / LTRL / Clanker: 2–6 wallets, 62–67%, paper bought them.
    thin = extract_features(
        {
            "chain": "robinhood",
            "coin": {"name": "John", "symbol": "JOHNAPPL"},
            "twitter": {"followers": 12_000, "age_days": 400, "verified": True},
            "twitter_handle": "john",
            "website": "https://example.com",
            "holders": {"holder_count": 4, "top10_pct": 100},
            "market": {"liquidity_usd": 200, "volume_h1": 50},
            "gmgn": {"source": "gmgn", "rug_risk": 2, "open_source": True, "renounced": True},
        }
    )
    wide = extract_features(
        {
            "chain": "robinhood",
            "coin": {"name": "Jacket", "symbol": "JJJACKET"},
            "holders": {"holder_count": 80, "top10_pct": 22},
            "market": {"liquidity_usd": 8_000, "volume_h1": 4_000},
            "gmgn": {"source": "gmgn", "rug_risk": 4},
        }
    )
    sol_tight = extract_features(
        {
            "chain": "sol",
            "coin": {"name": "Beast", "symbol": "BEAST"},
            "holders": {"holder_count": 18, "top10_pct": 100},
            "creator_stats": {"launches": 4, "wins": 2, "rugs": 0},
            "market": {},
        }
    )
    assert thin["rh_thin_book"] == 1.0
    assert wide["rh_thin_book"] == 0.0
    assert sol_tight["rh_thin_book"] == 0.0
    flags: list[str] = []
    p_thin = heuristic_probability(thin, [], flags)
    p_wide = heuristic_probability(wide, [], [])
    p_sol = heuristic_probability(sol_tight, [], [])
    assert p_thin <= 0.48
    assert p_wide >= 0.50
    assert p_sol >= 0.28
    assert any("thin" in f.lower() for f in flags)


def test_rh_lp_open_book_is_not_a_thin_wick():
    # Live MEME: 4 Blockscout rows / 98.8% pool / clean GMGN / verified
    # new X / copycat ticker. That is a fair LP open, not SHORT/PLUMBED.
    # Stay under the 0.50 paper line. FEATURE_NAMES stays 66.
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
    fat = extract_features(
        {
            "chain": "robinhood",
            "coin": {"name": "ThinFat", "symbol": "TFAT"},
            "holders": {"holder_count": 4, "top10_pct": 100},
            "last_liq": 80_000,
            "market": {},
            "gmgn": {"source": "gmgn", "rug_risk": 2},
        }
    )
    assert meme["rh_thin_book"] == 0.0
    assert meme["rh_lp_open_book"] == 1.0
    assert fat["rh_thin_book"] == 0.0
    assert len(FEATURE_NAMES) == 66
    reasons: list[str] = []
    flags: list[str] = []
    p_meme = heuristic_probability(meme, reasons, flags)
    # Ingest window was ~6 minutes under 2x. Paper reads 0.50.
    # Copycat + brand-new verified X must not bury a fair LP open.
    assert p_meme >= 0.50
    assert p_meme < 0.70
    assert any("lp open" in r.lower() for r in reasons)
    assert not any("copycat" in f.lower() for f in flags)
    assert not any("very recently" in f.lower() for f in flags)


def test_rh_lp_open_clears_paper_before_gmgn_lands():
    # Live MEME paper window was ~6 minutes. If token_research is
    # late, copycat −0.10 + new X −0.08 used to leave p at ~0.24.
    # Blockscout 98.8% pool is enough to catch; honeypot GMGN is
    # not. SHORT/PLUMBED stay thin-capped. FEATURE_NAMES stays 66.
    from launchfinder.scoring.features import rh_lp_open_clears_paper

    ctx = {
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
    late = extract_features(ctx)
    dirty = extract_features(
        {
            **ctx,
            "gmgn": {"source": "gmgn", "honeypot": True, "rug_risk": 80},
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
    assert late["rh_lp_open_book"] == 1.0
    assert late["gmgn_present"] == 0.0
    assert rh_lp_open_clears_paper(late)
    assert not rh_lp_open_clears_paper(dirty)
    assert not rh_lp_open_clears_paper(wick)
    flags: list[str] = []
    reasons: list[str] = []
    p_late = heuristic_probability(late, reasons, flags)
    p_dirty = heuristic_probability(dirty, [], [])
    p_wick = heuristic_probability(wick, [], [])
    assert p_late >= 0.50
    assert p_late < 0.70
    assert p_dirty < 0.50
    assert p_wick <= 0.48
    assert not any("copycat" in f.lower() for f in flags)
    assert not any("very recently" in f.lower() for f in flags)
    assert any("lp open" in r.lower() for r in reasons)
    assert len(FEATURE_NAMES) == 66


def test_rh_late_dex_book_softens_start_high_on_a_real_tape():
    # Live CRCL: rh_dex t0 $272k / 438w / $316k vol / $52k liq.
    # entry_premium stays — paper must not buy a pumped print —
    # but −0.25 hid a decent launch at 0.18. FEATURE_NAMES stays 66.
    from launchfinder.scoring.features import FEATURE_NAMES, is_rh_late_dex_book

    crcl = extract_features(
        {
            "chain": "robinhood",
            "source": "rh_dex",
            "coin": {"name": "Just a Circle", "symbol": "CRCL"},
            "holders": {"holder_count": 438, "top10_pct": 35.52},
            "market": {
                "liquidity_usd": 51_776.0,
                "volume_h1": 315_999.0,
                "mcap_usd": 271_612.0,
            },
            "t0_mcap": 271_612.0,
        }
    )
    wick = extract_features(
        {
            "chain": "robinhood",
            "coin": {"name": "United States Water Supply", "symbol": "USWS"},
            "market": {
                "mcap_usd": 2_147_855.0,
                "liquidity_usd": 123_509.0,
                "buys_m5": 39,
                "sells_m5": 14,
            },
            "holders": {"holder_count": 66, "top10_pct": 36.5},
            "time_to_migrate_min": 0.67,
        }
    )
    assert crcl["entry_premium"] == 1.0
    assert is_rh_late_dex_book(crcl)
    assert wick["entry_premium"] == 1.0
    assert not is_rh_late_dex_book(wick)
    assert len(FEATURE_NAMES) == 66
    flags: list[str] = []
    p_crcl = heuristic_probability(crcl, [], flags)
    p_wick = heuristic_probability(wick, [], [])
    assert p_crcl > p_wick
    assert p_crcl < 0.50
    assert any("late dex" in f.lower() for f in flags)


def test_rh_empty_book_cannot_clear_the_paper_line():
    # Live RETAILS: 36 holders / last_liq $0 / p=0.92. Thin-book only
    # fires under 20 wallets, so leftover FDV still cleared 0.50.
    empty = extract_features(
        {
            "chain": "robinhood",
            "coin": {"name": "Retails", "symbol": "RETAILS"},
            "twitter": {"followers": 8_000, "age_days": 200, "verified": True},
            "twitter_handle": "retails",
            "website": "https://example.com",
            "holders": {"holder_count": 36, "top10_pct": 40},
            "market": {"liquidity_usd": 0, "volume_h1": 0},
            "last_liq": 0,
            "age_min": 40,
            "gmgn": {"source": "gmgn", "rug_risk": 2, "open_source": True, "renounced": True},
        }
    )
    live = extract_features(
        {
            "chain": "robinhood",
            "coin": {"name": "Woody", "symbol": "WOODY"},
            "holders": {"holder_count": 119, "top10_pct": 22},
            "market": {"liquidity_usd": 39_000, "volume_h1": 8_000},
            "gmgn": {"source": "gmgn", "rug_risk": 4},
        }
    )
    sol_dry = extract_features(
        {
            "chain": "sol",
            "coin": {"name": "Beast", "symbol": "BEAST"},
            "holders": {"holder_count": 80, "top10_pct": 30},
            "creator_stats": {"launches": 4, "wins": 2, "rugs": 0},
            "market": {},
        }
    )
    assert empty["rh_empty_book"] == 1.0
    assert live["rh_empty_book"] == 0.0
    assert sol_dry["rh_empty_book"] == 0.0
    flags: list[str] = []
    p_empty = heuristic_probability(empty, [], flags)
    p_live = heuristic_probability(live, [], [])
    p_sol = heuristic_probability(sol_dry, [], [])
    assert p_empty <= 0.48
    assert p_live >= 0.50
    assert p_sol >= 0.28
    assert any("liquidity" in f.lower() for f in flags)

    # Live WOLVERINE: 44 holders / $0 liq / 5 minutes / p=0.65. The 30m
    # grace is for POV-class Dex-miss infants, not a leftover holder card.
    young_leftover = extract_features(
        {
            "chain": "robinhood",
            "coin": {"name": "Wolverine", "symbol": "WOLVERINE"},
            "twitter": {"followers": 8_000, "age_days": 200, "verified": True, "tweets": 400},
            "twitter_handle": "wolverine",
            "holders": {"holder_count": 44, "top10_pct": 40},
            "market": {"liquidity_usd": 0, "volume_h1": 0},
            "last_liq": 0,
            "age_min": 5,
        }
    )
    pov = extract_features(
        {
            "chain": "robinhood",
            "coin": {"name": "Pov", "symbol": "POV"},
            "holders": {"holder_count": 8, "top10_pct": 40},
            "market": {"liquidity_usd": 0, "volume_h1": 0},
            "last_liq": 0,
            "age_min": 5,
        }
    )
    assert young_leftover["rh_empty_book"] == 1.0
    assert pov["rh_empty_book"] == 0.0
    assert heuristic_probability(young_leftover, [], []) <= 0.48

    # Live STACKS: 104 holders / Dex $0 / last_liq $0 / p=0.87 because
    # GMGN leftover liquidity (~$35k, no hour-one volume) counted as a pool.
    stacks = extract_features(
        {
            "chain": "robinhood",
            "coin": {"name": "Stacks", "symbol": "STACKS"},
            "twitter": {"followers": 12_000, "age_days": 400, "verified": True, "tweets": 800},
            "twitter_handle": "stacks",
            "website": "https://example.com",
            "holders": {"holder_count": 104, "top10_pct": 14},
            "market": {"liquidity_usd": 0, "volume_h1": 0},
            "gmgn": {
                "source": "gmgn",
                "liquidity": 35_000,
                "rug_risk": 2,
                "open_source": True,
                "smart_degen": 8,
                "net_buy_24h": 40_000,
            },
            "last_liq": 0,
            "age_min": 5,
        }
    )
    trench_live = extract_features(
        {
            "chain": "robinhood",
            "coin": {"name": "PovBook", "symbol": "POVBOOK"},
            "holders": {"holder_count": 88, "top10_pct": 22},
            "market": {"liquidity_usd": 0, "volume_h1": 0},
            "gmgn": {"source": "gmgn", "liquidity": 6_000, "volume_1h": 4_200, "rug_risk": 4},
            "last_liq": 0,
            "age_min": 5,
        }
    )
    assert stacks["rh_empty_book"] == 1.0
    assert trench_live["rh_empty_book"] == 0.0
    assert heuristic_probability(stacks, [], []) <= 0.48
    assert heuristic_probability(trench_live, [], []) >= 0.45
    # Infant Dex-miss still gets the grace even if GMGN prints leftover FDV.
    pov_gmgn = extract_features(
        {
            "chain": "robinhood",
            "coin": {"name": "Pov2", "symbol": "POV2"},
            "holders": {"holder_count": 8, "top10_pct": 40},
            "market": {"liquidity_usd": 0, "volume_h1": 0},
            "gmgn": {"source": "gmgn", "liquidity": 35_000},
            "last_liq": 0,
            "age_min": 5,
        }
    )
    assert pov_gmgn["rh_empty_book"] == 0.0


def test_rh_airdrop_book_cannot_clear_the_paper_line():
    # Live COIN/FDC: 1093/1127 PONS wallets on a $3.5k book. Empty/thin
    # miss; re-anchor lifted 0.48→0.60 and paper bought. GUH 105/$47k
    # and CASHBIRD 24/$76k stay live.
    from launchfinder.db import init_db, session_scope
    from launchfinder.scoring.features import is_rh_airdrop_book, is_sol_airdrop_tape
    from launchfinder.scoring.model import predict

    coin = extract_features(
        {
            "chain": "robinhood",
            "coin": {"name": "Coin", "symbol": "COIN"},
            "twitter": {"followers": 8_000, "age_days": 200, "verified": True, "tweets": 400},
            "twitter_handle": "coin",
            "website": "https://example.com",
            "holders": {"holder_count": 1093, "top10_pct": 40},
            "market": {"liquidity_usd": 3_601, "volume_h1": 800},
            "last_liq": 3_601,
        }
    )
    guh = extract_features(
        {
            "chain": "robinhood",
            "coin": {"name": "Guh", "symbol": "GUH"},
            "holders": {"holder_count": 105, "top10_pct": 22},
            "market": {"liquidity_usd": 47_454, "volume_h1": 8_000},
            "last_liq": 47_454,
        }
    )
    cashbird = extract_features(
        {
            "chain": "robinhood",
            "coin": {"name": "Cashbird", "symbol": "CASHBIRD"},
            "holders": {"holder_count": 24, "top10_pct": 30},
            "market": {"liquidity_usd": 76_677, "volume_h1": 12_000},
            "last_liq": 76_677,
        }
    )
    sol = extract_features(
        {
            "chain": "sol",
            "coin": {"name": "Dust", "symbol": "DUST"},
            "holders": {"holder_count": 1100, "top10_pct": 40},
            "market": {"liquidity_usd": 3_500, "volume_h1": 800},
        }
    )
    assert coin["rh_airdrop_book"] == 1.0
    assert guh["rh_airdrop_book"] == 0.0
    assert cashbird["rh_airdrop_book"] == 0.0
    assert sol["rh_airdrop_book"] == 0.0
    assert is_rh_airdrop_book("robinhood", 1127, 3_473.0) is True
    assert is_rh_airdrop_book("robinhood", 105, 47_454.0) is False
    # Live Goldinu: parked last_liq $4k / 1148 wallets, Dex leftover $30k.
    assert is_rh_airdrop_book("robinhood", 1148, 4_090.0, liq=30_367.0) is True
    assert is_rh_airdrop_book("robinhood", 1148, 30_367.0, liq=4_090.0) is True
    assert is_rh_airdrop_book("robinhood", 1214, 49_079.0, liq=49_079.0) is False
    # Sol hunt sort: Loria/Cheyenne-class tape. Not a score cap.
    assert is_sol_airdrop_tape("sol", 2105, 18_435.0) is True
    assert is_sol_airdrop_tape("sol", 4998, 47_109.0) is True
    # Live 11:40 / 12:21: Syoma 356 / $10k then $14.8k / $41.6 sat #12.
    # TRUMP1 $65/holder stays with fat books.
    assert is_sol_airdrop_tape("sol", 356, 10_943.0) is True
    assert is_sol_airdrop_tape("sol", 356, 14_809.0) is True
    assert is_sol_airdrop_tape("sol", 539, 34_875.0) is False
    assert is_sol_airdrop_tape("sol", 55, 219_329.0) is False
    assert is_sol_airdrop_tape("robinhood", 2105, 18_435.0) is False
    # Live 12:38: Dark Arena 291w / $3.0k sat approaching.
    # PUMPLESS 30w / $46k stays.
    assert is_sol_airdrop_tape("sol", 291, 3_014.0) is True
    assert is_sol_airdrop_tape("sol", 30, 45_933.0) is False
    from launchfinder.scoring.features import prefer_rh_dust_liq

    assert prefer_rh_dust_liq(4_090.0, 30_367.0) == 4_090.0
    assert prefer_rh_dust_liq(0.0, 30_367.0) == 30_367.0
    assert prefer_rh_dust_liq(4_090.0, 0.0) == 4_090.0
    assert prefer_rh_dust_liq(47_454.0, 41_000.0) == 41_000.0
    init_db()
    with session_scope() as session:
        scored = predict(session, coin, chain="robinhood")
        assert scored["p_good"] <= 0.48


def test_stored_tape_and_dead_handle_move_heuristic_not_feature_vector():
    # Dex already returns volume_h24 + priceChange.h1; X lookup already
    # stores tweet_count. Score them without resizing FEATURE_NAMES.
    assert "twitter_tweets_n" not in FEATURE_NAMES
    assert "vol_persist_n" not in FEATURE_NAMES
    assert "price_change_h1_n" not in FEATURE_NAMES
    assert "age_min" not in FEATURE_NAMES
    assert "live_multiple" not in FEATURE_NAMES
    assert len(FEATURE_NAMES) == 66

    base = {
        "coin": {"name": "Tape", "symbol": "TAPE"},
        "twitter": {"followers": 80, "age_days": 40, "verified": False, "tweets": 400},
        "twitter_handle": "tapecoin",
        "holders": {"holder_count": 90, "top10_pct": 32},
        "market": {
            "liquidity_usd": 18_000,
            "volume_h1": 6_000,
            "volume_h24": 6_000,
            "price_change_h1": 5.0,
            "buys_m5": 20,
            "sells_m5": 12,
        },
    }
    persist = extract_features(
        {**base, "market": {**base["market"], "volume_h24": 80_000, "price_change_h1": 40.0}}
    )
    dump = extract_features(
        {**base, "market": {**base["market"], "volume_h24": 6_000, "price_change_h1": -55.0}}
    )
    dead = extract_features(
        {**base, "twitter": {"followers": 80, "age_days": 40, "verified": False, "tweets": 0}}
    )
    live_x = extract_features(base)
    assert persist["vol_persist_n"] > 0.5
    assert dump["price_change_h1_n"] < 0.22
    assert dead["twitter_tweets_n"] == 0.0
    assert live_x["twitter_tweets_n"] > 0.2
    p_persist = heuristic_probability(persist, [], [])
    p_dump = heuristic_probability(dump, [], [])
    p_dead = heuristic_probability(dead, [], [])
    p_live = heuristic_probability(live_x, [], [])
    assert p_persist > p_dump
    assert p_dead < p_live


def test_rh_bot_dump_caps_under_paper():
    # Live LIVOCAT: bot wallets + first-hour dump scored 0.53 and
    # paper-bought. Combined veto is 0.48. Solana dump stays uncapped.
    from launchfinder.scoring.features import FEATURE_NAMES, extract_features, heuristic_probability, is_rh_bot_dump_book

    assert "rh_bot_dump" not in FEATURE_NAMES
    ctx = {
        "chain": "robinhood",
        "coin": {"name": "Livo Cat", "symbol": "LIVOCAT"},
        "holders": {"holder_count": 69, "top10_pct": 40},
        "market": {
            "liquidity_usd": 6_513.0,
            "volume_h1": 8_000.0,
            "volume_h24": 8_000.0,
            "price_change_h1": -55.0,
        },
        "gmgn": {"source": "gmgn", "bot_rate": 82},
        "last_liq": 6_513.0,
    }
    feats = extract_features(ctx)
    assert feats["rh_bot_dump"] == 1.0
    assert is_rh_bot_dump_book("robinhood", feats) is True
    assert is_rh_bot_dump_book("sol", feats) is False
    flags: list[str] = []
    p = heuristic_probability(feats, [], flags)
    assert p <= 0.48
    assert any("Bot wallets dominate" in f for f in flags)
    assert any("dumping on real volume" in f for f in flags)

    sol = extract_features({**ctx, "chain": "sol"})
    assert sol["rh_bot_dump"] == 0.0


def test_stall_honesty_caps_flat_high_p_not_2x():
    # Live 13:50: TEAL 0.87 / 75m / 1.0x; XAI 0.92 / 4.5h / 1.0x.
    # YOLO 2.81 and a 20-minute infant stay put.
    assert stall_honesty_cap(0.87, age_min=20, multiple=1.0) == 0.87
    assert abs(stall_honesty_cap(0.87, age_min=40, multiple=1.0) - 0.75) < 1e-6
    assert stall_honesty_cap(0.87, age_min=75, multiple=1.0) == 0.48
    assert stall_honesty_cap(0.92, age_min=270, multiple=1.0) == 0.25
    assert stall_honesty_cap(0.64, age_min=200, multiple=2.81) == 0.64
    assert stall_honesty_cap(0.48, age_min=400, multiple=1.35) == 0.48
    assert stall_honesty_cap(0.89, age_min=280, multiple=1.43) == 0.48


def test_ath_dump_honesty_caps_believe_not_held_5x():
    # Live BELIEVE: 28× from $25k to $703k, last print $89k (13% of ATH).
    # Keep confirmed-5× membership; only the live score fades.
    assert ath_dump_honesty_cap(0.78, max_mcap=702_855, last_mcap=88_928, multiple=28.46) == 0.35
    assert ath_dump_honesty_cap(0.78, max_mcap=702_855, last_mcap=500_000, multiple=28.46) == 0.78
    assert ath_dump_honesty_cap(0.78, max_mcap=70_000, last_mcap=10_000, multiple=1.53) == 0.78
    assert ath_dump_honesty_cap(0.78, max_mcap=702_855, last_mcap=0, multiple=28.46) == 0.78
    assert ath_dump_honesty_cap(0.64, max_mcap=200_000, last_mcap=40_000, multiple=5.2) == 0.48


def test_social_ticket_without_tape_stays_under_sol_paper() -> None:
    # Live paper 0.70: HOLMES / GINGER / PSYOP at $69k, p=0.92, 1×.
    social = extract_features(
        {
            "coin": {"name": "Holmes", "symbol": "HOLMES", "reply_count": 80},
            "twitter": {"followers": 12_000, "age_days": 800, "verified": True, "tweets": 400},
            "twitter_handle": "holmes",
            "website": "https://example.com",
            "github_url": "https://github.com/holmes/docs",
            "github": {"stars": 20, "age_days": 200},
            "holders": {"holder_count": 400, "top10_pct": 28, "creator_hold_pct": 4},
            "creator_stats": {"launches": 2, "wins": 1, "rugs": 0},
            "time_to_migrate_min": 40,
            "market": {"liquidity_usd": 0, "volume_h1": 0},
            "gmgn": {"source": "gmgn", "rug_risk": 2, "insider_pct": 0, "sniper_pct": 0},
        }
    )
    live = extract_features(
        {
            "coin": {"name": "Pug", "symbol": "PUGCOIN", "reply_count": 80},
            "twitter": {"followers": 12_000, "age_days": 800, "verified": True, "tweets": 400},
            "twitter_handle": "pugcoin",
            "website": "https://example.com",
            "holders": {"holder_count": 247, "top10_pct": 36, "creator_hold_pct": 4},
            "creator_stats": {"launches": 1, "wins": 0, "rugs": 0},
            "time_to_migrate_min": 40,
            "market": {"liquidity_usd": 40_000, "volume_h1": 20_000, "buys_m5": 30, "sells_m5": 12},
            "gmgn": {"source": "gmgn", "rug_risk": 2},
        }
    )
    flags: list[str] = []
    p_social = heuristic_probability(social, [], flags)
    p_live = heuristic_probability(live, [], [])
    assert social["organic_book"] == 0.0
    assert p_social <= 0.68
    assert any("live tape" in f.lower() for f in flags)
    assert p_live >= 0.70
    assert social_ticket_cap(0.92, social) == 0.68
    assert social_ticket_cap(0.92, live) == 0.92
    assert len(FEATURE_NAMES) == 66


def test_organic_mid_path_catches_seventy_wallet_tape() -> None:
    # Live fih / BONZI / MACRODUCK: 70+ wallets, $5k+ pool, ~$1k vol.
    mid = extract_features(
        {
            "coin": {"name": "Bonzi", "symbol": "BONZI"},
            "holders": {"holder_count": 80, "top10_pct": 40, "creator_hold_pct": 5},
            "market": {"liquidity_usd": 8_000, "volume_h1": 1_200},
        }
    )
    lottery = extract_features(
        {
            "coin": {"name": "Pappy", "symbol": "Pappy"},
            "holders": {"holder_count": 13, "top10_pct": 100},
            "market": {"liquidity_usd": 0, "volume_h1": 0},
        }
    )
    assert is_organic_book(mid) is True
    assert mid["organic_book"] == 1.0
    assert lottery["organic_book"] == 0.0


def test_google_gemini_brand_clone_cannot_clear_graduation():
    # Live Google / Google Gemini: official @GeminiApp (573k, verified,
    # 808d) + gemini.google.com + copycat flood + 836-wallet organic
    # book. Hijack used ticker mentions, Gemini tweets "Google", and
    # heuristic printed 92% / model 99%. Cap at the 0.48 paper veto.
    from launchfinder.db import init_db, session_scope
    from launchfinder.scoring.features import is_brand_clone_book
    from launchfinder.scoring.model import predict
    from launchfinder.serialize import brand_clone_entry_cap, is_brand_clone_card

    google = extract_features(
        {
            "chain": "sol",
            "coin": {"name": "Google Gemini", "symbol": "Google", "reply_count": 40},
            "twitter": {
                "followers": 573_300,
                "age_days": 808,
                "verified": True,
                "tweets": 4000,
                "hijack": False,
                "owner_mentions": 14,
            },
            "twitter_handle": "GeminiApp",
            "website": "https://gemini.google.com/app",
            "symbol_flood": 2,
            "holders": {"holder_count": 836, "top10_pct": 0.48, "creator_hold_pct": 0},
            "creator_stats": {"launches": 1, "wins": 1, "rugs": 1},
            "time_to_migrate_min": 45,
            "market": {
                "buys_m5": 30,
                "sells_m5": 28,
                "liquidity_usd": 132_000,
                "volume_h1": 65_000,
            },
            "gmgn": {
                "source": "gmgn",
                "rug_risk": 0,
                "insider_pct": 0,
                "sniper_pct": 0,
                "dev_sold": True,
                "creator_ath_mc": 4_000_000,
                "burned": True,
                "renounced": True,
            },
        }
    )
    assert google["x_handle_hijack"] == 1.0
    assert google["symbol_flood_n"] >= 0.5
    assert is_brand_clone_book(google) is True
    flags: list[str] = []
    p = heuristic_probability(google, [], flags)
    assert p <= 0.48
    assert any("brand" in f.lower() or "hijack" in f.lower() for f in flags)

    init_db()
    with session_scope() as session:
        scored = predict(session, google, chain="sol")
    assert scored["p_good"] <= 0.48
    assert scored["heuristic_p"] <= 0.48

    card = {
        "p_good": 0.9821,
        "entry_p": 0.9821,
        "twitter_followers": 573_300,
        "website": "https://gemini.google.com/app",
        "risk_flags": ["Same ticker launched repeatedly in 24h (copycat spam)"],
    }
    assert is_brand_clone_card(card) is True
    assert brand_clone_entry_cap(0.9821, card) == 0.48
    meme = {
        "p_good": 0.03,
        "twitter_followers": 2,
        "risk_flags": ["Same ticker launched repeatedly in 24h (copycat spam)"],
    }
    assert is_brand_clone_card(meme) is False
    assert brand_clone_entry_cap(0.03, meme) == 0.03


def test_jubjub_copycat_dump_cannot_clear_graduation():
    # Live JubJub: copycat flood + dw selling + first-hour dump on a
    # 2200-wallet $97k book. Heuristic 92 / model 99. Cap at 0.48.
    # RH MEME copycat-only (no dump) must stay uncapped.
    from launchfinder.db import init_db, session_scope
    from launchfinder.scoring.features import is_copycat_dump_book
    from launchfinder.scoring.model import predict
    from launchfinder.serialize import desk_entry_cap, is_copycat_dump_card

    jub = extract_features(
        {
            "chain": "sol",
            "coin": {"name": "JubJub Bird", "symbol": "JubJub", "reply_count": 20},
            "twitter": {
                "followers": 3,
                "age_days": 67,
                "verified": False,
                "tweets": 10,
                "hijack": False,
            },
            "twitter_handle": "JubJubBird",
            "symbol_flood": 4,
            "holders": {"holder_count": 2208, "top10_pct": 0.077, "creator_hold_pct": 0},
            "time_to_migrate_min": 40,
            "market": {
                "buys_m5": 10,
                "sells_m5": 40,
                "liquidity_usd": 97_000,
                "volume_h1": 80_000,
                "price_change_h1": -40.0,
            },
            "gmgn": {
                "source": "gmgn",
                "rug_risk": 0,
                "insider_pct": 0,
                "sniper_pct": 0,
                "dev_sold": False,
                "buy_vol_1h": 20_000,
                "sell_vol_1h": 80_000,
            },
        }
    )
    assert jub["symbol_flood_n"] >= 0.5
    assert is_copycat_dump_book(jub) is True
    flags: list[str] = []
    p = heuristic_probability(jub, [], flags)
    assert p <= 0.48
    assert any("copycat" in f.lower() for f in flags)
    assert any("dump" in f.lower() or "selling" in f.lower() for f in flags)

    init_db()
    with session_scope() as session:
        scored = predict(session, jub, chain="sol")
    assert scored["p_good"] <= 0.48
    assert scored["heuristic_p"] <= 0.48

    card = {
        "p_good": 0.99,
        "entry_p": 0.99,
        "twitter_followers": 3,
        "risk_flags": [
            "Same ticker launched repeatedly in 24h (copycat spam)",
            "First-hour tape is dumping on real volume",
            "Dollar-weighted selling dominates hour one",
        ],
    }
    assert is_copycat_dump_card(card) is True
    assert desk_entry_cap(0.99, card) == 0.48
    meme = {
        "p_good": 0.63,
        "entry_p": 0.63,
        "twitter_followers": 2,
        "risk_flags": ["Same ticker launched repeatedly in 24h (copycat spam)"],
    }
    assert is_copycat_dump_card(meme) is False
    # Copycat flood itself is the 0.48 launch cap (live OpenAI 90).
    assert desk_entry_cap(0.63, meme) == 0.48


def test_openai_copycat_flood_cannot_clear_graduation():
    # Live OpenAI C3Wme…: copycat-only, no official X, 2395 holders,
    # two-tick chart. Heuristic 75 / model 93 / blend 90.
    from launchfinder.db import init_db, session_scope
    from launchfinder.scoring.features import is_copycat_flood_book
    from launchfinder.scoring.model import predict
    from launchfinder.serialize import desk_entry_cap

    openai = extract_features(
        {
            "chain": "sol",
            "coin": {"name": "OpenAI", "symbol": "OpenAI", "reply_count": 10},
            "symbol_flood": 4,
            "holders": {"holder_count": 2395, "top10_pct": 8.4, "creator_hold_pct": 0},
            "time_to_migrate_min": 30,
            "market": {
                "buys_m5": 40,
                "sells_m5": 30,
                "liquidity_usd": 29_000,
                "volume_h1": 16_000,
                "price_change_h1": 4.0,
            },
            "gmgn": {
                "source": "gmgn",
                "rug_risk": 0,
                "insider_pct": 0,
                "sniper_pct": 0,
                "buy_vol_1h": 10_000,
                "sell_vol_1h": 6_000,
                "renounced": True,
                "burned": True,
            },
        }
    )
    assert openai["symbol_flood_n"] >= 0.5
    assert is_copycat_flood_book(openai) is True
    flags: list[str] = []
    p = heuristic_probability(openai, [], flags)
    assert p <= 0.48
    assert any("copycat" in f.lower() for f in flags)

    init_db()
    with session_scope() as session:
        scored = predict(session, openai, chain="sol")
    assert scored["p_good"] <= 0.48
    assert scored["heuristic_p"] <= 0.48
    assert desk_entry_cap(0.9011, {"risk_flags": ["Same ticker launched repeatedly in 24h (copycat spam)"]}) == 0.48


def test_funicorn_phantom_t0_cannot_clear_graduation():
    # Live FUNICORN: organic-looking 1164-wallet book froze Entry 92.
    # Dex t0 $2.77M / last $1.8k. First-hour dump + social-without-tape.
    # Time-to-migrate looked human so entry_premium never set.
    from launchfinder.serialize import desk_entry_cap, is_phantom_t0_card

    funicorn = {
        "p_good": 0.92,
        "entry_p": 0.92,
        "t0_mcap": 2_698_994,
        "last_mcap": 1_801,
        "max_mcap": 2_698_994,
        "last_liq": 1_815,
        "holder_count": 1164,
        "top10_pct": 0.49,
        "risk_flags": [
            "X account created very recently",
            "First-hour tape is dumping on real volume",
            "Social card without a live tape",
            "Social card without a live tape — under paper",
        ],
    }
    assert is_phantom_t0_card(funicorn) is True
    assert desk_entry_cap(0.92, funicorn) == 0.48
    # Flags alone (in case t0/last are missing on a thin card).
    assert desk_entry_cap(0.96, {"risk_flags": ["Social card without a live tape"]}) == 0.48
    assert desk_entry_cap(0.92, {"risk_flags": ["First-hour tape is dumping on real volume"]}) == 0.48
    nina = {
        "p_good": 0.92,
        "entry_p": 0.92,
        "t0_mcap": 43_082,
        "last_mcap": 431_029,
        "max_mcap": 1_131_649,
        "risk_flags": [
            "Bonding curve filled almost instantly (bundle risk)",
            "Heavy sniper presence at launch (20+)",
            "Large share of volume is bundler bots",
            "Dumped off ATH (now 39% of peak)",
        ],
    }
    assert is_phantom_t0_card(nina) is False
    assert desk_entry_cap(0.92, nina) == 0.92


def test_pumpball_funder_rug_cannot_clear_graduation():
    # Live Pumpball: funder_rug_n=1.0, 333 wallets, organic book.
    # Heuristic 92 / learned 2% / blend 92. Only flag is the funder.
    from launchfinder.db import init_db, session_scope
    from launchfinder.scoring.features import is_funder_rug_book
    from launchfinder.scoring.model import predict
    from launchfinder.serialize import desk_entry_cap

    pumpball = extract_features(
        {
            "chain": "sol",
            "coin": {"name": "Pumpball", "symbol": "Pumpball", "reply_count": 10},
            "funder_stats": {"rugs": 3, "wins": 0},
            "holders": {"holder_count": 333, "top10_pct": 22.36, "creator_hold_pct": 0},
            "time_to_migrate_min": 40,
            "twitter": {
                "followers": 3393,
                "age_days": 948,
                "verified": True,
                "tweets": 200,
            },
            "website": "https://pumpball.fun/",
            "market": {
                "buys_m5": 20,
                "sells_m5": 10,
                "liquidity_usd": 16_370,
                "volume_h1": 1_375,
            },
            "gmgn": {
                "source": "gmgn",
                "rug_risk": 0,
                "insider_pct": 0,
                "sniper_pct": 0,
                "renounced": True,
                "burned": True,
            },
        }
    )
    assert pumpball["funder_rug_n"] >= 0.6
    assert is_funder_rug_book(pumpball) is True
    flags: list[str] = []
    p = heuristic_probability(pumpball, [], flags)
    assert p <= 0.48
    assert any("prior rugs" in f.lower() for f in flags)

    init_db()
    with session_scope() as session:
        scored = predict(session, pumpball, chain="sol")
    assert scored["p_good"] <= 0.48
    assert scored["heuristic_p"] <= 0.48
    assert desk_entry_cap(0.92, {"risk_flags": ["Creator was funded by a wallet behind prior rugs"]}) == 0.48
    nina = {
        "p_good": 0.92,
        "entry_p": 0.92,
        "t0_mcap": 43_082,
        "last_mcap": 431_029,
        "risk_flags": ["Heavy sniper presence at launch (20+)"],
    }
    assert desk_entry_cap(0.92, nina) == 0.92


def test_kat_collapsed_book_cannot_keep_graduation_entry():
    # Live [KAT]: t0 $37k / last $3.2k / Entry 92 / snipers only.
    from launchfinder.serialize import desk_entry_cap, is_collapsed_book_card

    kat = {
        "p_good": 0.92,
        "entry_p": 0.92,
        "t0_mcap": 37_339,
        "last_mcap": 3_179,
        "max_mcap": 51_796,
        "last_liq": 17_618,
        "holder_count": 305,
        "top10_pct": 24.67,
        "risk_flags": ["Heavy sniper presence at launch (20+)"],
    }
    assert is_collapsed_book_card(kat) is True
    assert desk_entry_cap(0.92, kat) == 0.48
    poor = {
        "p_good": 0.92,
        "t0_mcap": 55_042,
        "last_mcap": 12_472,
        "risk_flags": [
            "Heavy sniper presence at launch (20+)",
            "Community takeover (original dev left)",
        ],
    }
    assert is_collapsed_book_card(poor) is True
    assert desk_entry_cap(0.92, poor) == 0.48
    nina = {
        "p_good": 0.92,
        "t0_mcap": 43_082,
        "last_mcap": 410_979,
        "risk_flags": ["Heavy sniper presence at launch (20+)"],
    }
    assert is_collapsed_book_card(nina) is False
    assert desk_entry_cap(0.92, nina) == 0.92
    los = {
        "p_good": 0.92,
        "entry_p": 0.92,
        "t0_mcap": 35_193,
        "last_mcap": 12_622,
        "max_mcap": 35_193,
        "last_liq": 7_720,
        "holder_count": 372,
        "risk_flags": [
            "Heavy sniper presence at launch (20+)",
            "Community takeover (original dev left)",
        ],
    }
    assert is_collapsed_book_card(los) is True
    assert desk_entry_cap(0.92, los) == 0.48
    ai_meme = {
        "p_good": 0.92,
        "t0_mcap": 79_492,
        "last_mcap": 61_995,
        "risk_flags": ["Heavy sniper presence at launch (20+)"],
    }
    assert is_collapsed_book_card(ai_meme) is False
    assert desk_entry_cap(0.92, ai_meme) == 0.92
    # Live 16:00 ETAC AM1SMh: t0 $393k / last $21k / Entry 95.5.
    etac = {
        "p_good": 0.955,
        "entry_p": 0.955,
        "t0_mcap": 392_756,
        "last_mcap": 21_209,
        "max_mcap": 486_603,
        "last_liq": 21_262,
        "holder_count": 2183,
        "risk_flags": ["Score faded — still under 2x after sitting"],
    }
    assert is_collapsed_book_card(etac) is True
    assert desk_entry_cap(0.955, etac) == 0.48
    # GS 0.70× t0 at $25.5k is a fade, not leftover dust.
    gs = {
        "p_good": 0.92,
        "t0_mcap": 36_132,
        "last_mcap": 25_465,
        "max_mcap": 130_750,
        "risk_flags": ["Heavy sniper presence at launch (20+)"],
    }
    assert is_collapsed_book_card(gs) is False
    assert desk_entry_cap(0.92, gs) == 0.92
    # Live Peg: t0 $36k / last $17.5k (0.49×) / Entry 90. Under the
    # $25k floor but 0.49× missed the old 0.40× ratio.
    peg = {
        "p_good": 0.90,
        "entry_p": 0.90,
        "t0_mcap": 36_024,
        "last_mcap": 17_523,
        "max_mcap": 89_305,
        "risk_flags": [
            "Heavy sniper presence at launch (20+)",
            "Community takeover (original dev left)",
        ],
    }
    assert is_collapsed_book_card(peg) is True
    assert desk_entry_cap(0.90, peg) == 0.48


def test_twin_staged_social_and_two_tick_cannot_clear_graduation():
    # Live TWIN: https://x.com/AragornSol/status/… + twine.auction/a/…
    # Organic book froze Entry 92. Dex 1m is two candles; 2× wick
    # given back to t0. NINA also pastes a tweet but the site is reddit.
    from launchfinder.db import init_db, session_scope
    from launchfinder.scoring.features import is_staged_social_book
    from launchfinder.scoring.model import predict
    from launchfinder.serialize import desk_entry_cap, is_staged_social_card, is_two_tick_card

    twin = extract_features(
        {
            "chain": "sol",
            "coin": {
                "name": "TWIN",
                "symbol": "TWIN",
                "reply_count": 0,
                "twitter": "https://x.com/AragornSol/status/2099235852543315992",
            },
            "twitter_url": "https://x.com/AragornSol/status/2099235852543315992",
            "twitter_handle": "AragornSol",
            "twitter": {
                "followers": 162,
                "age_days": 1676,
                "verified": True,
                "tweets": 200,
            },
            "website": "https://twine.auction/a/twin-zwjg",
            "holders": {"holder_count": 248, "top10_pct": 24.71, "creator_hold_pct": 0},
            "time_to_migrate_min": 20,
            "market": {
                "buys_m5": 12,
                "sells_m5": 10,
                "liquidity_usd": 15_411,
                "volume_h1": 31_760,
            },
            "gmgn": {
                "source": "gmgn",
                "rug_risk": 0,
                "insider_pct": 0,
                "sniper_pct": 0,
                "renounced": True,
                "burned": True,
                "smart_degen": 8,
            },
        }
    )
    assert twin["staged_social"] == 1.0
    assert is_staged_social_book(twin) is True
    flags: list[str] = []
    p = heuristic_probability(twin, [], flags)
    assert p <= 0.48
    assert any("pasted tweet" in f.lower() for f in flags)

    init_db()
    with session_scope() as session:
        scored = predict(session, twin, chain="sol")
    assert scored["p_good"] <= 0.48
    assert scored["heuristic_p"] <= 0.48

    tape = {
        "chain": "sol",
        "p_good": 0.92,
        "entry_p": 0.92,
        "t0_mcap": 41_154,
        "last_mcap": 40_090,
        "max_mcap": 84_648,
        "last_liq": 15_325,
        "holder_count": 248,
        "twitter": "https://x.com/AragornSol/status/2099235852543315992",
        "website": "https://twine.auction/a/twin-zwjg",
        "risk_flags": [],
    }
    assert is_two_tick_card(tape) is True
    assert is_staged_social_card(tape) is True
    assert desk_entry_cap(0.92, tape) == 0.48
    nina = {
        "chain": "sol",
        "p_good": 0.92,
        "t0_mcap": 43_082,
        "last_mcap": 431_029,
        "max_mcap": 1_131_649,
        "twitter": "https://x.com/Bymotionn/status/2099168136456991013",
        "website": "https://www.reddit.com/r/ninathemonkey/",
        "risk_flags": ["Heavy sniper presence at launch (20+)"],
    }
    assert is_two_tick_card(nina) is False
    assert is_staged_social_card(nina) is False
    assert desk_entry_cap(0.92, nina) == 0.92
    pisscoin = {
        "chain": "sol",
        "t0_mcap": 34_774,
        "last_mcap": 33_362,
        "max_mcap": 34_774,
        "twitter": "https://x.com/FartingDevSol/status/2099203679656005647",
        "website": "",
        "risk_flags": ["Heavy sniper presence at launch (20+)"],
    }
    assert is_two_tick_card(pisscoin) is False
    assert is_staged_social_card(pisscoin) is False
    assert desk_entry_cap(0.92, pisscoin) == 0.92
    nina_jp = {
        "chain": "sol",
        "p_good": 0.92,
        "t0_mcap": 53_644,
        "last_mcap": 57_312,
        "max_mcap": 140_234,
        "twitter": "https://x.com/Bingopnl/status/2099131003616870498",
        "website": "https://www.reddit.com/r/ninathemonkey/",
        "risk_flags": ["Heavy sniper presence at launch (20+)"],
    }
    assert is_two_tick_card(nina_jp) is False
    assert is_staged_social_card(nina_jp) is False
    assert desk_entry_cap(0.92, nina_jp) == 0.92
    # Live HUGE: 9.3× wick given back to t0 / 11% ATH / Entry 92.
    # Sniper skip is for ニーナ-sized holds, not a 5×+ dump-to-open.
    huge = {
        "chain": "sol",
        "p_good": 0.92,
        "entry_p": 0.92,
        "t0_mcap": 44_046,
        "last_mcap": 45_740,
        "max_mcap": 409_304,
        "last_liq": 20_000,
        "holder_count": 371,
        "risk_flags": [
            "Heavy sniper presence at launch (20+)",
            "Dumped off ATH (now 11% of peak)",
        ],
    }
    assert is_two_tick_card(huge) is True
    assert desk_entry_cap(0.92, huge) == 0.48
    # GS 3.6× ATH / last 0.91× t0 stays a sniper hold, not HUGE.
    gs = {
        "chain": "sol",
        "p_good": 0.92,
        "t0_mcap": 36_132,
        "last_mcap": 33_021,
        "max_mcap": 130_750,
        "risk_flags": ["Heavy sniper presence at launch (20+)"],
    }
    assert is_two_tick_card(gs) is False
    assert desk_entry_cap(0.92, gs) == 0.92
    from launchfinder.serialize import apply_desk_entry_honesty

    shown = apply_desk_entry_honesty(dict(tape, p_good=0.92, entry_p=None))
    assert shown["entry_p"] == 0.48
    assert shown["p_good"] == 0.48
    assert any("pasted tweet" in str(flag).lower() for flag in shown["risk_flags"])
    nina_shown = apply_desk_entry_honesty(dict(nina, entry_p=None))
    assert nina_shown["p_good"] == 0.92
    assert nina_shown["entry_p"] == 0.92
    nina_faded = apply_desk_entry_honesty(
        dict(nina, p_good=0.55, heuristic_p=0.92, entry_p=None)
    )
    assert nina_faded["entry_p"] == 0.92
    assert nina_faded["p_good"] == 0.55
    diesel = {
        "chain": "sol",
        "p_good": 0.92,
        "entry_p": 0.92,
        "t0_mcap": 43_288,
        "last_mcap": 18_059,
        "max_mcap": 43_288,
        "twitter": "https://x.com/YoungThugDevvor/status/2099263978467725392",
        "website": "https://otcdesks.cash/coin/CJ7oyJJ4pYLV9Phkkyp2yhUZ87QUqS6GdsWuV4ytpump",
        "risk_flags": ["Heavy sniper presence at launch (20+)"],
    }
    assert is_staged_social_card(diesel) is True
    assert desk_entry_cap(0.92, diesel) == 0.48
    diesel_feats = extract_features(
        {
            "chain": "sol",
            "coin": {
                "name": "Diesel",
                "symbol": "Diesel",
                "twitter": "https://x.com/YoungThugDevvor/status/2099263978467725392",
            },
            "twitter_url": "https://x.com/YoungThugDevvor/status/2099263978467725392",
            "website": "https://otcdesks.cash/coin/CJ7oyJJ4pYLV9Phkkyp2yhUZ87QUqS6GdsWuV4ytpump",
        }
    )
    assert diesel_feats["staged_social"] == 1.0
    assert is_staged_social_book(diesel_feats) is True


def test_metapad_bought_aged_token_x_cannot_clear_graduation():
    # Live Metapad: @metapadspace 3565d / Dex Pair 17y / Entry 92.
    # 8y+ project X on a new launch is a bought handle. NINA 626d,
    # MEME brand-new X, and Dev-X-only cards stay open.
    from launchfinder.serialize import (
        apply_desk_entry_honesty,
        desk_entry_cap,
        is_bought_aged_token_x_card,
    )

    metapad = {
        "symbol": "Metapad",
        "twitter_handle": "metapadspace",
        "twitter_age_days": 3565.3,
        "twitter_followers": 188,
        "twitter_verified": True,
        "claimed_brand_x": False,
        "p_good": 0.92,
        "entry_p": 0.92,
        "t0_mcap": 80_000,
        "last_mcap": 109_000,
        "last_liq": 31_000,
        "holder_count": 298,
    }
    assert is_bought_aged_token_x_card(metapad) is True
    assert desk_entry_cap(0.92, metapad) == 0.48
    shown = apply_desk_entry_honesty(dict(metapad))
    assert shown["entry_p"] == 0.48
    assert any("bought aged" in str(flag).lower() for flag in shown["risk_flags"])

    nina = {
        "symbol": "NINA",
        "twitter_handle": "ninathemonkey",
        "twitter_age_days": 626,
        "twitter_followers": 1162,
        "twitter_verified": True,
        "p_good": 0.92,
        "entry_p": 0.92,
        "t0_mcap": 43_082,
        "last_mcap": 431_029,
        "max_mcap": 1_131_649,
        "risk_flags": ["Heavy sniper presence at launch (20+)"],
    }
    assert is_bought_aged_token_x_card(nina) is False
    assert desk_entry_cap(0.92, nina) == 0.92

    meme = {
        "symbol": "MEME",
        "twitter_handle": "amemecoinrh",
        "twitter_age_days": 0.02,
        "twitter_followers": 0,
        "twitter_verified": True,
        "p_good": 0.92,
        "entry_p": 0.92,
    }
    assert is_bought_aged_token_x_card(meme) is False
    assert desk_entry_cap(0.92, meme) == 0.92

    two_year = {
        "twitter_handle": "realproject",
        "twitter_age_days": 730,
        "p_good": 0.92,
        "entry_p": 0.92,
    }
    assert is_bought_aged_token_x_card(two_year) is False
    assert desk_entry_cap(0.92, two_year) == 0.92

    dev_only = {
        "twitter_handle": "",
        "dev_handle": "forestmansol555",
        "twitter_age_days": 5239,
        "p_good": 0.92,
        "entry_p": 0.92,
    }
    assert is_bought_aged_token_x_card(dev_only) is False
    assert desk_entry_cap(0.92, dev_only) == 0.92

    nasa = {
        "twitter_handle": "NASA",
        "twitter_age_days": 6843,
        "twitter_followers": 92_390_162,
        "twitter_verified": True,
        "claimed_brand_x": True,
        "p_good": 0.33,
        "entry_p": 0.33,
    }
    assert is_bought_aged_token_x_card(nasa) is False


def test_rwa_unbacked_last_cannot_keep_flash_mcap_or_live_95():
    # Live RWA: last $958k / 36× / Live 95, latest snap $64k / 6h,
    # Pair empty. Entry 86 at ingest stays. MEME last $82M over an
    # 80×-capped max $1.07M is a later pair — keep last. NINA and
    # sitting RH last ≈ snap stay open.
    from launchfinder.scoring.hunt import conviction_from_tape
    from launchfinder.scoring.hunt_accuracy import hunt_accuracy_issues, scan_hunt_accuracy
    from launchfinder.serialize import (
        apply_unbacked_last_honesty,
        desk_entry_cap,
        is_unbacked_last_card,
    )

    rwa = {
        "symbol": "RWA",
        "chain": "robinhood",
        "p_good": 0.86,
        "entry_p": 0.86,
        "t0_mcap": 26_746,
        "last_mcap": 957_691,
        "max_mcap": 957_691,
        "stored_max_mcap": 957_691,
        "last_liq": 99_964,
        "holder_count": 197,
        "top10_pct": 17.49,
        "snap_mcap": 64_006,
        "snap_liq": 25_723,
        "snap_max_mcap": 64_006,
        "snap_age_min": 360.0,
        "volume_h1": 112_974,
        "risk_flags": ["X account created very recently"],
    }
    assert is_unbacked_last_card(rwa) is True
    assert desk_entry_cap(0.86, rwa) == 0.86
    shown = apply_unbacked_last_honesty(dict(rwa))
    assert abs(shown["last_mcap"] - 64_006) < 1e-6
    assert abs(shown["max_mcap"] - 64_006) < 1e-6
    assert shown["last_mcap"] / shown["t0_mcap"] < 3.0
    assert any("unbacked" in str(flag).lower() for flag in shown["risk_flags"])
    live = conviction_from_tape(
        chain="robinhood",
        entry_p=0.86,
        multiple=shown["last_mcap"] / shown["t0_mcap"],
        last_mcap=shown["last_mcap"],
        t0_mcap=shown["t0_mcap"],
        max_mcap=shown["max_mcap"],
        last_liq=shown["last_liq"],
        holders=197,
        top10_pct=17.49,
        flags=shown["risk_flags"],
        vol_h1=112_974,
    )
    assert live < 0.90
    from launchfinder.scoring.hunt import pick_live_conviction

    # Stored bloom 95 was scored on the $958k flash — do not restore it.
    assert (
        pick_live_conviction(
            live,
            0.95,
            last_mcap=shown["last_mcap"],
            t0_mcap=shown["t0_mcap"],
            max_mcap=shown["max_mcap"],
            allow_bloom=False,
        )
        == live
    )

    meme = {
        "symbol": "MEME",
        "last_mcap": 81_845_931,
        "snap_mcap": 175_437,
        "snap_age_min": 900.0,
        "stored_max_mcap": 1_073_625,
        "max_mcap": 81_845_931,
        "t0_mcap": 21_997,
        "entry_p": 0.03,
    }
    assert is_unbacked_last_card(meme) is False
    assert apply_unbacked_last_honesty(dict(meme))["last_mcap"] == 81_845_931

    fresh = dict(rwa, snap_age_min=10.0)
    assert is_unbacked_last_card(fresh) is False

    nina = {
        "symbol": "NINA",
        "p_good": 0.92,
        "entry_p": 0.92,
        "t0_mcap": 43_082,
        "last_mcap": 431_029,
        "max_mcap": 1_131_649,
        "stored_max_mcap": 1_131_649,
        "snap_mcap": 431_029,
        "snap_age_min": 20.0,
        "risk_flags": ["Heavy sniper presence at launch (20+)"],
    }
    assert is_unbacked_last_card(nina) is False
    assert desk_entry_cap(0.92, nina) == 0.92

    sitting = {
        "symbol": "ATHGIRL",
        "chain": "robinhood",
        "entry_p": 0.70,
        "t0_mcap": 22_000,
        "last_mcap": 22_400,
        "max_mcap": 22_400,
        "stored_max_mcap": 22_400,
        "snap_mcap": 22_400,
        "snap_age_min": 180.0,
    }
    assert is_unbacked_last_card(sitting) is False

    haven = {
        "symbol": "Haven",
        "entry_p": 0.89,
        "t0_mcap": 42_667,
        "last_mcap": 74_526,
        "max_mcap": 95_664,
        "stored_max_mcap": 95_664,
        "snap_mcap": 95_664,
        "snap_age_min": 360.0,
    }
    assert is_unbacked_last_card(haven) is False

    rwa_snaps = [
        {
            "kind": "early",
            "taken_at": "2026-09-15T06:30:52+00:00",
            "mcap_usd": 64_006,
            "volume_h1": 112_974,
            "liquidity_usd": 25_723,
        }
    ]
    flagged = hunt_accuracy_issues(
        {
            "last_mcap": 957_691,
            "max_mcap": 957_691,
            "t0_mcap": 26_746,
            "conviction_p": 0.95,
            "live_model_p": 0.95,
            "entry_p": 0.86,
            "multiple": 35.8,
        },
        rwa_snaps,
    )
    kinds = {item["kind"] for item in flagged}
    assert "unbacked_last" in kinds
    assert "live_on_unbacked" in kinds
    leftover_tape = hunt_accuracy_issues(
        {
            "last_mcap": 957_691,
            "max_mcap": 957_691,
            "t0_mcap": 26_746,
            "conviction_p": 0.95,
            "live_model_p": 0.01,
            "entry_p": 0.86,
            "multiple": 35.8,
        },
        [
            {
                "taken_at": "2026-09-15T06:30:52+00:00",
                "mcap_usd": 64_006,
                "volume_h1": 0,
                "liquidity_usd": 25_723,
            }
        ],
    )
    leftover_kinds = {item["kind"] for item in leftover_tape}
    assert "unbacked_last" in leftover_kinds
    assert "live_on_unbacked" not in leftover_kinds
    assert "live_on_dust_vol" not in leftover_kinds
    nina_issues = hunt_accuracy_issues(
        {
            "last_mcap": 431_029,
            "max_mcap": 1_131_649,
            "t0_mcap": 43_082,
            "conviction_p": 0.95,
            "live_model_p": 0.95,
            "entry_p": 0.92,
            "multiple": 10.0,
        },
        [
            {
                "taken_at": "2026-09-15T12:00:00+00:00",
                "mcap_usd": 431_029,
                "volume_h1": 80_000,
                "liquidity_usd": 40_000,
            }
        ],
    )
    assert nina_issues == []
    report = scan_hunt_accuracy(
        [
            {
                "mint": "0xd47d87ddb7ee8a9f180bef09cd11370f382ffd5d",
                "symbol": "RWA",
                "last_mcap": 957_691,
                "max_mcap": 957_691,
                "t0_mcap": 26_746,
                "conviction_p": 0.95,
                "live_model_p": 0.95,
                "entry_p": 0.86,
                "multiple": 35.8,
            }
        ],
        {
            "0xd47d87ddb7ee8a9f180bef09cd11370f382ffd5d": {"snapshots": rwa_snaps},
        },
    )
    assert report["flagged"] == 1

    from datetime import timedelta

    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, Snapshot, Token, utcnow
    from launchfinder.scoring.outcomes import revert_unbacked_last
    from launchfinder.serialize import token_card

    init_db()
    now = utcnow()
    with session_scope() as session:
        token = Token(
            mint="0xd47d87ddb7ee8a9f180bef09cd11370f382ffd5d",
            symbol="RWA",
            name="Real World ASSets",
            chain="robinhood",
            source="rh_dex",
            first_seen_at=now - timedelta(hours=6),
        )
        token.research = Research(
            p_good=0.86,
            heuristic_p=0.86,
            holder_count=197,
            top10_pct=17.49,
            features_json="{}",
            risk_flags_json='["X account created very recently"]',
        )
        session.add(token)
        session.flush()
        session.add(
            Outcome(
                token_id=token.id,
                t0_mcap=26_746.0,
                last_mcap=957_691.0,
                max_mcap=957_691.0,
                last_liq=99_964.0,
                multiple=35.8,
            )
        )
        session.add(
            Snapshot(
                token_id=token.id,
                kind="early",
                taken_at=now - timedelta(hours=6),
                mcap_usd=64_006.0,
                volume_h1=112_974.0,
                liquidity_usd=25_723.0,
            )
        )
        session.flush()
        session.refresh(token)
        _ = token.snapshots
        card = token_card(token)
        assert abs(card["last_mcap"] - 64_006.0) < 1e-6
        assert card["last_mcap"] / card["t0_mcap"] < 3.0
        assert any("unbacked" in str(flag).lower() for flag in card["risk_flags"])
        assert desk_entry_cap(float(card.get("entry_p") or card["p_good"]), card) == 0.86
        outcome = token.outcome
        assert revert_unbacked_last(session, token, outcome) is True
        assert abs(outcome.last_mcap - 64_006.0) < 1e-6
        assert outcome.multiple < 3.0

