import asyncio

from launchfinder.research import twitter


def test_fxtwitter_first_official_only_for_big_accounts(monkeypatch):
    calls = {"fx": 0, "official": 0}

    async def fake_fx(handle):
        calls["fx"] += 1
        return {"handle": handle, "followers": 900, "source": "fxtwitter"}

    async def fake_official(handle):
        calls["official"] += 1
        return {"handle": handle, "followers": 950, "source": "twitter_api"}

    monkeypatch.setattr(twitter, "_fxtwitter", fake_fx)
    monkeypatch.setattr(twitter, "_official", fake_official)
    twitter._handle_cache.clear()

    out = asyncio.run(twitter.lookup_handle("smallaccount"))
    assert out["source"] == "fxtwitter"
    assert calls["official"] == 0  # no paid call for a small account

    # cache: second lookup is free
    asyncio.run(twitter.lookup_handle("smallaccount"))
    assert calls["fx"] == 1


def test_official_only_for_celebrity_accounts_fx_already_found(monkeypatch):
    calls = {"official": 0}

    async def fake_fx_big(handle):
        return {"handle": handle, "followers": 3_000_000, "source": "fxtwitter"}

    async def fake_fx_mid(handle):
        return {"handle": handle, "followers": 25_000, "source": "fxtwitter"}

    async def fake_fx_none(handle):
        return {}

    async def fake_official(handle):
        calls["official"] += 1
        return {"handle": handle, "followers": 3_100_000, "source": "twitter_api"}

    monkeypatch.setattr(twitter, "_official", fake_official)
    twitter._handle_cache.clear()

    monkeypatch.setattr(twitter, "_fxtwitter", fake_fx_big)
    out = asyncio.run(twitter.lookup_handle("celebrity"))
    # Paid confirm is off. A 3M account stays on the free profile.
    assert out["source"] == "fxtwitter"
    assert calls["official"] == 0

    twitter._handle_cache.clear()
    monkeypatch.setattr(twitter, "_fxtwitter", fake_fx_mid)
    out = asyncio.run(twitter.lookup_handle("midsize"))
    assert out["source"] == "fxtwitter"
    assert calls["official"] == 0  # 25k is not worth a credit

    twitter._handle_cache.clear()
    monkeypatch.setattr(twitter, "_fxtwitter", fake_fx_none)
    out = asyncio.run(twitter.lookup_handle("unknownhandle"))
    assert out == {}
    assert calls["official"] == 0  # dead handle must not fall through to paid API


def test_mention_count_is_cached(monkeypatch):
    calls = {"n": 0}

    async def fake_get(url, params):
        calls["n"] += 1
        return {"data": [{"tweet_count": 12}]}

    monkeypatch.setattr(twitter, "_x_get", fake_get)
    twitter._mention_cache.clear()
    assert asyncio.run(twitter.mention_count("$CAC")) == 12
    assert asyncio.run(twitter.mention_count("$CAC")) == 12
    assert calls["n"] == 1


def test_official_brand_never_spends_paid_x(monkeypatch):
    calls = {"official": 0, "counts": 0}

    async def fake_fx(handle):
        return {"handle": handle, "followers": 92_390_162, "verified": True, "age_days": 6000, "source": "fxtwitter"}

    async def fake_official(handle):
        calls["official"] += 1
        return {"handle": handle, "followers": 92_390_162, "source": "twitter_api"}

    async def fake_get(url, params):
        calls["counts"] += 1
        return {"data": [{"tweet_count": 400}]}

    monkeypatch.setattr(twitter, "_fxtwitter", fake_fx)
    monkeypatch.setattr(twitter, "_official", fake_official)
    monkeypatch.setattr(twitter, "_x_get", fake_get)
    twitter._handle_cache.clear()
    twitter._mention_cache.clear()

    out = asyncio.run(twitter.lookup_handle("NASA"))
    assert out["source"] == "fxtwitter"
    assert calls["official"] == 0

    twitter._handle_cache.clear()
    out = asyncio.run(twitter.lookup_handle("nasa"))
    assert out["source"] == "fxtwitter"
    assert calls["official"] == 0

    assert asyncio.run(twitter.mention_count("$NASA")) == 0
    assert asyncio.run(twitter.mention_count("$Google")) == 0
    assert calls["counts"] == 0
    assert twitter.should_spend_x_credits("NASA", followers=92_390_162, verified=True) is False
    assert twitter.should_spend_x_credits(
        "launchonquick",
        followers=4_200,
        flags=[],
    )
    assert twitter.should_spend_x_credits(
        "middev",
        followers=8_000,
        flags=["Claimed celebrity/brand X — not this launch"],
    ) is False


def test_account_mentions_skip_official_brand(monkeypatch):
    async def fake_get(url, params):
        raise AssertionError("brand hijack must not spend a counts credit")

    monkeypatch.setattr(twitter, "_x_get", fake_get)
    assert asyncio.run(twitter.account_mentions("NASA", "NASA", mint="NasaMint111")) is None


def test_paid_x_stays_off_even_when_a_bearer_is_set(monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(twitter, "settings", replace(twitter.settings, twitter_bearer="bearer-token"))
    monkeypatch.setattr(twitter, "X_PAID_ENABLED", False)
    assert twitter.x_api_available() is False


def test_paid_x_budget_trips_after_cap(monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(twitter, "settings", replace(twitter.settings, twitter_bearer="bearer-token"))
    monkeypatch.setattr(twitter, "X_PAID_ENABLED", True)
    monkeypatch.setattr(twitter, "X_MONTHLY_CALL_BUDGET", 2)
    monkeypatch.setattr(twitter, "_local_month_key", twitter._month_key())
    monkeypatch.setattr(twitter, "_local_month_calls", 2)
    assert twitter.x_api_available() is False
    monkeypatch.setattr(twitter, "_local_month_calls", 0)
    assert twitter.x_api_available() is True
