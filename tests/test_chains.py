from launchfinder.chains import (
    chain_links,
    graduation_mcap,
    normalize_chain,
    normalize_mint,
    profile,
)
from launchfinder.research.dexscreener import _best_pair
from launchfinder.research.gmgn import (
    PUMP_PLATFORMS,
    SOL_PLATFORMS,
    _platforms_for,
    _trench_section,
    _trenches_body,
    is_pons_platform,
    is_sol_gmgn_create_pad,
    trench_to_coin,
)


def test_normalize_chain_aliases():
    assert normalize_chain("RH") == "robinhood"
    assert normalize_chain("solana") == "sol"
    assert normalize_chain("nope") == "sol"
    assert profile("robinhood").dex == "robinhood"
    assert graduation_mcap("robinhood") != graduation_mcap("sol")


def test_evm_mint_lowercased():
    assert normalize_mint("0xABCDef", "robinhood") == "0xabcdef"
    assert normalize_mint("So111pump", "sol") == "So111pump"


def test_rh_links_skip_pumpfun():
    links = chain_links("0xabc", "robinhood")
    assert "pump" not in links
    assert links["gmgn"] == "https://gmgn.ai/robinhood/token/0xabc"
    assert "robinhood" in links["dex"]
    assert "blockscout" in links["explorer"]
    assert links["pons"] == "https://www.ponsfamily.com/launchpad/0xabc"
    assert links["pons_launchpad"] == "https://www.ponsfamily.com/launchpad"
    pair_links = chain_links(
        "0x98096d17e191b3da1d5f99a6d7b3584351b11e18",
        "robinhood",
        pair_address="0x9c89b04303dfa76f3f6fb02c2b77be0e8a00ab8fa00d507119acd54ab3e8640d",
    )
    assert pair_links["dex"].endswith("0x9c89b04303dfa76f3f6fb02c2b77be0e8a00ab8fa00d507119acd54ab3e8640d")


def test_dex_pair_picks_robinhood_only():
    pairs = [
        {"chainId": "base", "liquidity": {"usd": 9_000_000}, "priceUsd": "1"},
        {"chainId": "robinhood", "liquidity": {"usd": 12_000}, "priceUsd": "0.01"},
    ]
    best = _best_pair(pairs, "robinhood")
    assert best["chainId"] == "robinhood"
    assert _best_pair(pairs, "sol")["chainId"] == "base"  # sol may fall back


def test_best_pair_prefers_sol_pumpswap_over_leftover_pumpfun_fdv():
    # Live LAPTOP / NIKE: leftover pump.fun pair printed $17M–$295M
    # (sometimes with fat fake liq) while PumpSwap was ~$2k.
    pairs = [
        {
            "chainId": "solana",
            "dexId": "pumpfun",
            "liquidity": {"usd": 1_467_997},
            "marketCap": 295_307_085,
            "fdv": 295_307_085,
            "volume": {"h1": 0, "m5": 0},
            "pairAddress": "leftover",
        },
        {
            "chainId": "solana",
            "dexId": "pumpswap",
            "liquidity": {"usd": 2_326},
            "marketCap": 2_333,
            "fdv": 2_333,
            "volume": {"h1": 8.95, "m5": 0},
            "pairAddress": "live",
        },
    ]
    assert _best_pair(pairs, "sol")["pairAddress"] == "live"


def test_best_pair_keeps_pumpfun_when_it_is_the_only_book():
    pairs = [
        {
            "chainId": "solana",
            "dexId": "pumpfun",
            "liquidity": {"usd": 18_000},
            "marketCap": 42_000,
            "volume": {"h1": 4_000},
            "pairAddress": "curve",
        }
    ]
    assert _best_pair(pairs, "sol")["pairAddress"] == "curve"


def test_best_pair_resi_class_prefers_deep_liq_over_wash_volume_thin_pool():
    # RESI: Ray ~$86k liq / ~$700k mcap vs thin pool with wash h1 vol / ~$1.7M mcap.
    pairs = [
        {
            "chainId": "solana",
            "dexId": "raydium",
            "liquidity": {"usd": 86_126},
            "marketCap": 702_652,
            "fdv": 702_652,
            "volume": {"h1": 334_000},
            "pairAddress": "deep",
        },
        {
            "chainId": "solana",
            "dexId": "meteora",
            "liquidity": {"usd": 95},
            "marketCap": 1_720_000,
            "fdv": 1_720_000,
            "volume": {"h1": 900_000},
            "pairAddress": "wash",
        },
    ]
    best = _best_pair(pairs, "sol")
    assert best["pairAddress"] == "deep"
    from launchfinder.research.dexscreener import _market_from_pair

    m = _market_from_pair(best, "sol")
    assert 600_000 < m["mcap_usd"] < 800_000


def test_best_pair_prefers_deep_hims_book_over_dust_fdv():
    # BONER: leftover V4 books print $10M+ FDV on $50 liq. The live
    # market is the HIMS pair (~$2.8M liq).
    pairs = [
        {"chainId": "robinhood", "liquidity": {"usd": 50}, "marketCap": 19_000_000, "pairAddress": "dust"},
        {"chainId": "robinhood", "liquidity": {"usd": 2_800_000}, "marketCap": 67_000_000, "pairAddress": "hims"},
        {"chainId": "robinhood", "liquidity": {"usd": 128_000}, "marketCap": 67_000_000, "pairAddress": "weth"},
    ]
    assert _best_pair(pairs, "robinhood")["pairAddress"] == "hims"


def test_trench_coin_carries_chain():
    coin = trench_to_coin(
        {
            "address": "0xAbC0000000000000000000000000000000000001",
            "symbol": "FLAP",
            "name": "Flap",
            "usd_market_cap": 42000,
            "created_timestamp": 1_777_000_000,
            "open_timestamp": 1_777_000_400,
        },
        chain="robinhood",
    )
    assert coin["chain"] == "robinhood"
    assert coin["mint"].startswith("0x")


def test_evm_pool_id_fits_after_clip():
    from launchfinder.db import SessionLocal, init_db
    from launchfinder.ingest.store import upsert_migration

    init_db()
    session = SessionLocal()
    try:
        token = upsert_migration(
            session,
            mint="0x1bfedda482aa6ee16b7d86a873a0a2c712bb1e18",
            source="rh_trenches",
            coin={
                "chain": "robinhood",
                "name": "Long Guy",
                "symbol": "LONGGUY",
                "pool_address": "0xcfc797d042d3d07740cf1abf86723fe6c31429ae271b03d9ffe95c073f757fe2",
            },
        )
        session.flush()
        assert token is not None
        assert len(token.pool_address) <= 128
        assert token.pool_address.startswith("0xcfc797")
    finally:
        session.close()


def test_long_symbol_and_name_clip_instead_of_dataerror():
    # Live 17:09 / 17:14: mint 0x1d7915… skipped DataError every poll.
    # PONS symbols/names can exceed Token.symbol 32 / name 128.
    from launchfinder.db import SessionLocal, init_db
    from launchfinder.ingest.store import upsert_migration

    init_db()
    session = SessionLocal()
    try:
        token = upsert_migration(
            session,
            mint="0xrhlongsymclip0000000000000000000001",
            source="rh_trenches",
            coin={
                "chain": "robinhood",
                "name": "N" * 200,
                "symbol": "S" * 80,
                "creator": "C" * 90,
            },
        )
        session.flush()
        assert token is not None
        assert token.symbol == "S" * 32
        assert token.name == "N" * 128
        assert token.creator == "C" * 64
    finally:
        session.close()


def test_symbol_and_name_strip_before_clip():
    # Live 17:40: KURO ingested as "KURO " and would twin a clean ticker.
    from launchfinder.db import SessionLocal, init_db
    from launchfinder.ingest.store import upsert_migration

    init_db()
    session = SessionLocal()
    try:
        token = upsert_migration(
            session,
            mint="KuroSpaceMint111111111111111111111111",
            source="poll",
            coin={
                "chain": "sol",
                "name": "  Kuro Inu  ",
                "symbol": "KURO ",
                "creator": "  creatorpad  ",
            },
        )
        session.flush()
        assert token is not None
        assert token.symbol == "KURO"
        assert token.name == "Kuro Inu"
        assert token.creator == "creatorpad"
    finally:
        session.close()


def test_rh_trenches_do_not_use_pump_platforms():
    assert _platforms_for("robinhood", None) is None
    assert "launchpad_platform" not in _trench_section(30, None)
    assert _platforms_for("sol", None) == list(SOL_PLATFORMS)
    assert "ray_launchpad" in SOL_PLATFORMS
    assert "pool_ray" not in SOL_PLATFORMS
    assert _trench_section(30, list(SOL_PLATFORMS))["launchpad_platform"] == list(SOL_PLATFORMS)
    body = _trenches_body(60, None)
    assert "new_creation" in body
    assert "completed" in body
    assert "launchpad_platform" not in body["new_creation"]
    assert profile("robinhood").launch_note.startswith("PONS")
    assert is_pons_platform("pons")
    assert is_pons_platform("Pons")
    assert not is_pons_platform("Flap")
    assert is_sol_gmgn_create_pad("ray_launchpad")
    assert not is_sol_gmgn_create_pad("Pump.fun")
    assert not is_sol_gmgn_create_pad("pool_ray")


def test_trench_coin_carries_pons_launchpad():
    coin = trench_to_coin(
        {
            "address": "0xpons000000000000000000000000000000000001",
            "symbol": "PONSX",
            "launchpad_platform": "pons",
            "usd_market_cap": 18000,
        },
        chain="robinhood",
    )
    assert coin["launchpad"] == "pons"
    assert coin["chain"] == "robinhood"


def test_token_markets_batches_and_keeps_requested_base(monkeypatch):
    from launchfinder.research import dexscreener

    calls: list[str] = []

    async def fake_get(url, **_kwargs):
        calls.append(url)
        addrs = url.rsplit("/", 1)[-1].split(",")
        return {
            "pairs": [
                {
                    "chainId": "solana",
                    "dexId": "pumpswap",
                    "baseToken": {"address": addrs[0], "name": "A", "symbol": "A"},
                    "quoteToken": {"address": "So11111111111111111111111111111111111111112", "symbol": "SOL"},
                    "priceUsd": "1",
                    "marketCap": 80_000,
                    "liquidity": {"usd": 15_000},
                    "volume": {"h1": 20_000, "m5": 1_000},
                    "txns": {"h1": {"buys": 10, "sells": 4}, "m5": {"buys": 2, "sells": 1}},
                }
            ]
        }

    monkeypatch.setattr(dexscreener, "get_json", fake_get)
    mints = [f"Mint{i:02d}1111111111111111111111111111111111" for i in range(31)]
    import asyncio

    out = asyncio.run(dexscreener.token_markets(mints, "sol"))
    assert len(calls) == 2
    assert len(calls[0].rsplit("/", 1)[-1].split(",")) == 30
    assert mints[0] in out
    assert out[mints[0]]["mcap_usd"] == 80_000
