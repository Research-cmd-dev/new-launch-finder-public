"""FOMO user profile wallet REST parse — no live API key in CI."""

from __future__ import annotations

from launchfinder.research.fomo_api import (
    _parse_user_profile_wallets,
    fetch_fomo_user_wallet_sync,
    fomo_user_wallet_lookup_enabled,
    reset_wallet_lookup_skip_state,
)


def test_parse_user_profile_wallets_sol_and_rh():
    sol = _parse_user_profile_wallets(
        {"wallets": {"solana": "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU", "evm": "0xabc"}},
        "sol",
    )
    assert sol == "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
    rh = _parse_user_profile_wallets(
        {"wallets": {"solana": "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU", "evm": "0xa3602804e096cb73bd8344afc1ff3f3390b899c5"}},
        "robinhood",
    )
    assert rh == "0xa3602804e096cb73bd8344afc1ff3f3390b899c5"


def test_fetch_user_wallet_sync_disabled_by_default(monkeypatch):
    import launchfinder.research.fomo_api as api

    reset_wallet_lookup_skip_state()
    api._FOMO_USER_WALLET_CACHE.clear()
    calls = 0

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            nonlocal calls
            calls += 1
            raise AssertionError(f"disabled lookup must not HTTP {url}")

    monkeypatch.delenv("FOMO_USER_WALLET_LOOKUP", raising=False)
    monkeypatch.setattr(api, "_auth_headers", lambda: {"authorization": "Bearer test"})
    monkeypatch.setattr(api.httpx, "Client", _Client)
    assert fomo_user_wallet_lookup_enabled() is False
    assert fetch_fomo_user_wallet_sync("uid-42", "sol") == ""
    assert calls == 0
    assert api.WALLET_LOOKUP_SKIPS == 1


def test_fetch_user_wallet_sync_uses_cache(monkeypatch):
    import launchfinder.research.fomo_api as api

    monkeypatch.setenv("FOMO_USER_WALLET_LOOKUP", "1")
    api._FOMO_USER_WALLET_CACHE.clear()
    calls = 0

    class _Resp:
        status_code = 200

        def json(self):
            return {"wallets": {"solana": "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"}}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            nonlocal calls
            calls += 1
            assert "users/id" in url
            return _Resp()

    monkeypatch.setattr(api, "_auth_headers", lambda: {"authorization": "Bearer test"})
    monkeypatch.setattr(api.httpx, "Client", _Client)
    w1 = fetch_fomo_user_wallet_sync("uid-42", "sol")
    w2 = fetch_fomo_user_wallet_sync("uid-42", "sol")
    assert w1 == w2 == "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
    assert calls == 1
