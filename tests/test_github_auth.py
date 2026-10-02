from datetime import datetime, timedelta, timezone

from launchfinder.research.github import launch_signal
from launchfinder.scoring.features import extract_features, github_authenticity, heuristic_probability


def test_real_project_scores_high():
    gh = {
        "full_name": "realdev/realproject",
        "age_days": 400,
        "commits": 30,
        "contributors": 4,
        "stars": 120,
        "is_fork": False,
    }
    assert github_authenticity(gh) >= 0.6


def test_staged_repo_scores_low():
    same_day_dump = {"full_name": "x/y", "age_days": 0.5, "commits": 1, "contributors": 1, "stars": 0, "is_fork": False}
    forked_copy = {"full_name": "x/z", "age_days": 2, "commits": 25, "contributors": 1, "stars": 1, "is_fork": True}
    assert github_authenticity(same_day_dump) <= 0.15
    assert github_authenticity(forked_copy) <= 0.15
    assert github_authenticity({}) == 0.0


def test_real_project_requires_both_github_and_credible_x():
    gh_real = {"full_name": "x/y", "age_days": 300, "commits": 30, "contributors": 3, "stars": 50, "is_fork": False}
    base = {
        "coin": {"name": "DevCoin", "symbol": "DEV"},
        "github_url": "https://github.com/x/y",
        "github": gh_real,
        "twitter_handle": "devteam",
    }
    both = extract_features({**base, "twitter": {"followers": 4000, "age_days": 200}})
    assert both["real_project"] == 1.0
    reasons: list[str] = []
    heuristic_probability(both, reasons, [])
    assert any("Real dev project" in r for r in reasons)

    hijacked = extract_features({**base, "twitter": {"followers": 4000, "age_days": 200, "hijack": True}})
    assert hijacked["real_project"] == 0.0
    no_x = extract_features({**base, "twitter_handle": "", "twitter": {}})
    assert no_x["real_project"] == 0.0
    fresh_tiny_x = extract_features({**base, "twitter": {"followers": 12, "age_days": 1}})
    assert fresh_tiny_x["real_project"] == 0.0
    staged_gh = extract_features({**base, "github": {"full_name": "x/y", "age_days": 0.4, "commits": 1, "contributors": 1, "stars": 0, "is_fork": False}, "twitter": {"followers": 4000, "age_days": 200}})
    assert staged_gh["real_project"] == 0.0


def test_heuristic_rewards_genuine_and_flags_staged():
    base = {
        "coin": {"name": "DevCoin", "symbol": "DEV", "reply_count": 60},
        "github_url": "https://github.com/x/y",
        "holders": {"holder_count": 300, "top10_pct": 30},
        "market": {"buys_m5": 20, "sells_m5": 10, "liquidity_usd": 20000, "volume_h1": 9000},
    }
    genuine = extract_features({**base, "github": {"full_name": "x/y", "age_days": 300, "commits": 30, "contributors": 3, "stars": 50, "is_fork": False}})
    staged = extract_features({**base, "github": {"full_name": "x/y", "age_days": 0.3, "commits": 1, "contributors": 1, "stars": 0, "is_fork": False}})
    reasons: list[str] = []
    flags: list[str] = []
    p_genuine = heuristic_probability(genuine, reasons, [])
    p_staged = heuristic_probability(staged, [], flags)
    assert p_genuine > p_staged
    assert any("genuine" in r for r in reasons)
    assert any("staged" in f for f in flags)


def test_github_launch_signal_after_migrate_and_staged():
    launched = datetime(2026, 9, 13, 18, 0, tzinfo=timezone.utc)
    after = launch_signal(
        {
            "age_days": 400,
            "stars": 12,
            "commits": 30,
            "pushed_at": (launched + timedelta(hours=3)).isoformat(),
        },
        launched_at=launched,
    )
    assert after["delta"] == 0.03
    assert "after launch" in after["reasons"][0].lower()

    staged = launch_signal({"age_days": 0.4, "stars": 0, "commits": 1, "pushed_at": launched.isoformat()}, launched_at=launched)
    assert staged["delta"] == -0.03
    assert "staged" in staged["line"].lower()

    empty = launch_signal({}, launched_at=launched)
    assert empty["delta"] == 0.0
