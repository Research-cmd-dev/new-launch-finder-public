from launchfinder.research.twitter import (
    is_brand_x_account,
    is_claimed_brand_x,
    is_official_brand_handle,
)
from launchfinder.scoring.features import (
    extract_features,
    heuristic_probability,
    is_brand_clone_book,
)
from launchfinder.social import (
    bio_hits_project,
    extract_github,
    extract_twitter_handle,
    is_official_brand_website,
)


def test_hijacked_celebrity_handle_is_penalized():
    base = {
        "coin": {"name": "BEAST", "symbol": "BEAST", "reply_count": 40},
        "twitter_handle": "MrBeast",
        "holders": {"holder_count": 400, "top10_pct": 30, "creator_hold_pct": 5},
        "time_to_migrate_min": 30,
        "market": {"buys_m5": 30, "sells_m5": 10, "liquidity_usd": 25000, "volume_h1": 9000},
    }
    genuine = extract_features({**base, "twitter": {"followers": 35_000_000, "age_days": 4000, "verified": True, "owner_mentions": 3, "hijack": False}})
    hijacked = extract_features({**base, "twitter": {"followers": 35_000_000, "age_days": 4000, "verified": True, "owner_mentions": 0, "hijack": True}})
    assert genuine["x_handle_hijack"] == 0.0
    assert hijacked["x_handle_hijack"] == 1.0
    flags: list[str] = []
    p_hijacked = heuristic_probability(hijacked, [], flags)
    p_genuine = heuristic_probability(genuine, [], [])
    assert p_hijacked < p_genuine
    assert any("hijack" in f.lower() for f in flags)


def test_verified_product_account_is_a_brand():
    # Live @GeminiApp: 573k / 808d / verified. Old 2000d gate missed it.
    assert is_brand_x_account(573_300, 808, True) is True
    assert is_brand_x_account(2_724_906, 6380, True) is True
    assert is_brand_x_account(80_000, 808, True) is False
    assert is_brand_x_account(150_000, 400, False) is False
    assert is_official_brand_website("https://gemini.google.com/app") is True
    assert is_official_brand_website("https://sites.google.com/view/mycoin") is False
    assert is_official_brand_handle("NASA") is True
    assert is_official_brand_handle("launchonquick") is False
    assert is_claimed_brand_x(
        "NASA",
        followers=92_390_162,
        age_days=6843,
        verified=True,
        flags=["Claimed celebrity/brand X — not this launch"],
    ) is True
    assert is_claimed_brand_x("launchonquick", followers=5, age_days=1, verified=True) is False


def test_twitter_handle_from_status_url():
    assert extract_twitter_handle("https://x.com/lana_goat/status/2093777256900342172") == "lana_goat"


def test_twitter_ignores_home():
    assert extract_twitter_handle("https://x.com/home") == ""


def test_github_repo():
    url, ref = extract_github("docs at https://github.com/acme/cool-coin and more")
    assert url == "https://github.com/acme/cool-coin"
    assert ref == "acme/cool-coin"


def test_bio_hits_project_ticker_and_name():
    assert bio_hits_project("building $QUICK on solana", "QUICK", "Quick Launch")
    assert bio_hits_project("just a guy", "QUICK", "Quick") is False
    assert bio_hits_project("NASA official", "NASA", "NASA") is True
