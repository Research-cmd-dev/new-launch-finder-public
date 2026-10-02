import asyncio
from datetime import timedelta

from launchfinder.db import init_db, session_scope
from launchfinder.models import Outcome, Research, Token, utcnow
from launchfinder.research.dev_dive import (
    creator_wallet_line,
    handles_from_research,
    handles_from_text,
    score_public_dev,
    should_dive_public_dev,
)
from launchfinder.scoring.bloom import begin_bloom_cycle, consider_bloom
from launchfinder.scoring.features import FEATURE_NAMES


def test_feature_names_stay_sixty_six():
    assert len(FEATURE_NAMES) == 66


def test_handles_from_text_finds_public_x_and_skips_noise():
    found = handles_from_text(
        "https://x.com/realdevxyz",
        "chart on dexscreener.com follow @pumpfun and @RealDevXYZ",
        "https://twitter.com/home",
    )
    lower = [h.lower() for h in found]
    assert "realdevxyz" in lower
    assert "pumpfun" not in lower
    assert "home" not in lower
    brand = handles_from_text("paste https://x.com/NASA and @Google into the coin")
    assert [h.lower() for h in brand] == []


def test_handles_from_research_uses_pump_user_and_stored_handle():
    token = Token(
        mint="DevMint111111111111111111111111111111111",
        symbol="DEVX",
        name="Dev Token",
        twitter="",
        website="https://example.com",
        description="built by @onchainbuilder",
    )
    research = Research(
        twitter_handle="",
        raw_json='{"user": {"x_username": "pumpdev99"}, "gmgn": {"twitter_username": ""}}',
    )
    found = handles_from_research(token, research)
    lower = [h.lower() for h in found]
    assert "onchainbuilder" in lower
    assert "pumpdev99" in lower


def test_score_public_dev_hijack_and_burner():
    hijack = score_public_dev(
        {"handle": "elonmusk", "hijack": True, "followers": 180_000_000, "age_days": 5000},
        symbol="DEVX",
        name="Dev Token",
        website="",
    )
    assert hijack["tier"] == "hijack"
    assert hijack["delta"] < 0

    burner = score_public_dev(
        {"handle": "newguy", "age_days": 2, "followers": 12, "tweets": 1},
        symbol="DEVX",
        name="Dev Token",
        website="",
    )
    assert burner["tier"] == "burner"
    assert burner["delta"] < 0

    brand = score_public_dev(
        {
            "handle": "CocaCola",
            "age_days": 6380,
            "followers": 2_724_906,
            "tweets": 80_000,
            "verified": True,
            "bio": "The official Coca-Cola account",
            "name": "Coca-Cola",
        },
        symbol="COCA COLA",
        name="COCA COLA",
        website="",
    )
    assert brand["tier"] == "hijack"
    assert brand["delta"] < 0
    assert "brand" in brand["line"].lower()

    gemini = score_public_dev(
        {
            "handle": "GeminiApp",
            "age_days": 808,
            "followers": 573_300,
            "tweets": 4_000,
            "verified": True,
            "bio": "The Gemini app turns research into reality",
            "name": "Google Gemini",
        },
        symbol="Google",
        name="Google Gemini",
        website="https://gemini.google.com/app",
    )
    assert gemini["tier"] == "hijack"
    assert gemini["delta"] < 0


def test_score_public_dev_credible_match_caps_delta():
    scored = score_public_dev(
        {
            "handle": "oldbuilder",
            "age_days": 800,
            "followers": 2400,
            "tweets": 400,
            "bio": "Building $DEVX — https://devx.example",
            "name": "Dev Token",
            "verified": False,
        },
        symbol="DEVX",
        name="Dev Token",
        website="https://devx.example",
    )
    assert scored["tier"] == "strong"
    assert 0.05 <= scored["delta"] <= 0.10
    assert "oldbuilder" in scored["line"]


def test_creator_wallet_line_uses_stored_priors_only():
    good = Research(creator_prior_wins=2, creator_prior_rugs=0, creator_prior_launches=3)
    delta, line = creator_wallet_line(good)
    assert delta > 0
    assert "2 prior win" in line

    serial = Research(creator_prior_wins=0, creator_prior_rugs=4, creator_prior_launches=6)
    delta, line = creator_wallet_line(serial)
    assert delta < 0
    assert "serial" in line

    empty = Research()
    assert creator_wallet_line(empty) == (0.0, "")


def _bloom_fixture(session, *, mint: str, leftover: bool = False, p_good: float = 0.18):
    now = utcnow()
    token = Token(
        mint=mint,
        symbol="DEVX",
        name="Dev Token",
        chain="sol",
        twitter="https://x.com/oldbuilder",
        website="https://devx.example",
        first_seen_at=now - timedelta(hours=5),
        migrated_at=now - timedelta(hours=5),
    )
    token.research = Research(
        features_json="{}",
        reasons_json="[]",
        risk_flags_json="[]",
        raw_json='{"user": {"x_username": "oldbuilder"}}',
        p_good=p_good,
        heuristic_p=0.20,
        holder_count=140,
        top10_pct=36.0,
        thesis="entry thesis",
        creator_prior_wins=1,
        creator_prior_rugs=0,
        creator_prior_launches=2,
    )
    if leftover:
        outcome = Outcome(
            token=token,
            t0_mcap=69_000,
            max_mcap=8_000_000,
            last_mcap=8_000_000,
            last_liq=40_000,
            multiple=115.9,
        )
        market = {"liquidity_usd": 40_000, "volume_h1": 80_000, "mcap_usd": 8_000_000}
    else:
        outcome = Outcome(
            token=token,
            t0_mcap=69_000,
            max_mcap=180_000,
            last_mcap=165_000,
            last_liq=32_000,
            multiple=2.39,
        )
        market = {"liquidity_usd": 32_000, "volume_h1": 45_000, "mcap_usd": 165_000}
    session.add_all([token, outcome])
    session.flush()
    return token, outcome, market, now


def test_should_dive_public_dev_reserves_real_profiles():
    profile = Token(
        mint="DevMintQuick11111111111111111111111111",
        symbol="QUICK",
        twitter="https://x.com/launchonquick",
        website="https://launchonquick.example",
    )
    assert should_dive_public_dev(profile, Research(risk_flags_json="[]", twitter_followers=4200)) is False

    nasa = Token(
        mint="DevMintNasa111111111111111111111111111",
        symbol="NASA",
        twitter="https://x.com/NASA",
        website="https://www.nasa.gov",
    )
    assert should_dive_public_dev(
        nasa,
        Research(
            twitter_handle="NASA",
            twitter_followers=92_390_162,
            risk_flags_json='["Claimed celebrity/brand X — not this launch"]',
        ),
    ) is False

    tweet_only = Token(
        mint="DevMintTweet11111111111111111111111111",
        symbol="NINA",
        twitter="https://x.com/someone/status/123",
        website="https://reddit.com/r/something",
    )
    assert should_dive_public_dev(tweet_only, Research(risk_flags_json="[]")) is False

    pump = Token(
        mint="DevMintPump111111111111111111111111111",
        symbol="DEVX",
        twitter="",
        website="",
    )
    assert should_dive_public_dev(
        pump,
        Research(raw_json='{"user": {"x_username": "pumpdev99"}}', risk_flags_json="[]"),
    )

    split = Token(
        mint="DevMintSplit11111111111111111111111111",
        symbol="SPLIT",
        twitter="https://x.com/projectmainx",
        website="https://project.example",
    )
    assert should_dive_public_dev(
        split,
        Research(raw_json='{"user": {"x_username": "realdevwallet"}}', risk_flags_json="[]"),
    )


def test_consider_bloom_folds_dev_dive_into_thesis_not_entry_p(monkeypatch):
    calls = {"n": 0}

    async def _dive(token, research):
        calls["n"] += 1
        assert research.p_good == 0.18
        return {
            "handle": "oldbuilder",
            "tier": "strong",
            "delta": 0.08,
            "reasons": ["@oldbuilder looks like a real public account (800d, 2,400 followers)"],
            "line": "Public dev @oldbuilder: 800d account, 2,400 followers, bio matches the project.",
            "github": "https://github.com/oldbuilder/devx",
        }

    monkeypatch.setattr("launchfinder.scoring.bloom.dive_developer", _dive)
    begin_bloom_cycle()
    init_db()
    with session_scope() as session:
        token, outcome, market, now = _bloom_fixture(session, mint="DevBloom111111111111111111111111111111")
        first = asyncio.run(consider_bloom(session, token, outcome, market, now=now))
        assert first is not None
        assert token.research.p_good == 0.18
        assert token.research.thesis == "entry thesis"
        assert token.research.features_json == "{}"
        assert first["dev_dived"] is True
        assert first["dev_handle"] == "oldbuilder"
        assert first["dev_tier"] == "strong"
        assert first["promise_p"] >= 0.58
        assert "Public dev @oldbuilder" in first["thesis"]
        assert first["entry_p"] == 0.18

        second = asyncio.run(consider_bloom(session, token, outcome, market, now=now))
        assert calls["n"] == 1
        assert second["dev_handle"] == "oldbuilder"
        assert token.research.p_good == 0.18


def test_leftover_fdv_skips_dev_dive(monkeypatch):
    async def _boom(token, research):
        raise AssertionError("leftover books must not hunt a developer")

    monkeypatch.setattr("launchfinder.scoring.bloom.dive_developer", _boom)
    init_db()
    with session_scope() as session:
        token, outcome, market, now = _bloom_fixture(
            session,
            mint="LeftBloom11111111111111111111111111111",
            leftover=True,
        )
        payload = asyncio.run(consider_bloom(session, token, outcome, market, now=now))
        assert payload is not None
        assert payload["promise_p"] == 0.0
        assert payload["leftover_fdv"] is True
        assert payload["dev_dived"] is False
        assert token.research.p_good == 0.18


def test_consider_bloom_folds_github_not_entry_p(monkeypatch):
    async def _no_dive(token, research):
        return {"handle": "", "tier": "none", "delta": 0.0, "reasons": [], "line": "", "github": ""}

    async def _gh(ref):
        assert ref == "oldbuilder/devx"
        return {
            "full_name": "oldbuilder/devx",
            "age_days": 400,
            "stars": 8,
            "commits": 20,
            "pushed_at": (utcnow() - timedelta(hours=1)).isoformat(),
        }

    monkeypatch.setattr("launchfinder.scoring.bloom.dive_developer", _no_dive)
    monkeypatch.setattr("launchfinder.scoring.bloom.lookup_repo", _gh)
    monkeypatch.setattr("launchfinder.research.github.lookup_repo", _gh)
    begin_bloom_cycle()
    init_db()
    with session_scope() as session:
        token, outcome, market, now = _bloom_fixture(
            session,
            mint="GhBloom111111111111111111111111111111111",
        )
        token.github_url = "https://github.com/oldbuilder/devx"
        first = asyncio.run(consider_bloom(session, token, outcome, market, now=now))
        assert first is not None
        assert token.research.p_good == 0.18
        assert token.research.features_json == "{}"
        assert first["gh_refreshed"] is True
        assert first["promise_p"] >= 0.58
        blob = " ".join(
            [str(r) for r in (first.get("reasons") or [])]
            + [str(first.get("gh_line") or ""), str(first.get("thesis") or "")]
        )
        assert "github" in blob.lower()
        second = asyncio.run(consider_bloom(session, token, outcome, market, now=now))
        assert second["gh_refreshed"] is True
        assert token.research.p_good == 0.18


def test_consider_bloom_skips_brand_dev_hunt(monkeypatch):
    async def _boom(token, research):
        raise AssertionError("NASA-class books must not hunt a developer")

    monkeypatch.setattr("launchfinder.scoring.bloom.dive_developer", _boom)
    begin_bloom_cycle()
    init_db()
    with session_scope() as session:
        token, outcome, market, now = _bloom_fixture(
            session,
            mint="NasaBloom11111111111111111111111111111",
        )
        token.symbol = "NASA"
        token.twitter = "https://x.com/NASA"
        token.website = "https://www.nasa.gov"
        token.research.twitter_handle = "NASA"
        token.research.twitter_followers = 92_390_162
        token.research.risk_flags_json = '["Claimed celebrity/brand X — not this launch"]'
        payload = asyncio.run(consider_bloom(session, token, outcome, market, now=now))
        assert payload is not None
        assert payload["dev_dived"] is True
        assert payload["dev_tier"] == "skip"
        assert payload["dev_handle"] == ""
        assert token.research.p_good == 0.18
