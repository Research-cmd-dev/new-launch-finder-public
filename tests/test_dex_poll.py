import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from launchfinder.db import init_db
from launchfinder.ingest.dex_poll import (
    SOL_DEX_MAX_NEW,
    discover_fresh_quoted,
    discover_fresh_sol_quoted,
    pair_to_coin,
    poll_rh_dex,
    poll_sol_dex,
    sol_dex_row_ok,
    sol_pair_to_coin,
)
from launchfinder.models import Token
from launchfinder.research.dexscreener import (
    BOOST_MARKET_CAP,
    RH_QUOTE_TOKENS,
    SOL_PEPE_MINT,
    SOL_QUOTE_TOKENS,
    _market_from_pair,
    _sol_profile_mints,
    is_dex_pair_id,
    quoted_pairs,
)


BONER_PAIR = "0x9c89b04303dfa76f3f6fb02c2b77be0e8a00ab8fa00d507119acd54ab3e8640d"
BONER_MINT = "0x98096d17e191B3dA1d5f99a6D7b3584351b11E18"
HIMS = "0xCceE82fE024c36fA15E1005edE3E9e4787e23D09"
COST = "0x4ea005168d7f09a7a0ba9d1def21a479950e44c2"
LIVE_USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
DEAD_USDG = "0xf052f9339afaba171724b3229a624db2a30eb115"
HOTDOG = "0x1c1daef0300551adbfbe403e7d567b6c5aff566f"
GME = "0x1b0e319c6a659f002271b69db8a7df2f911c153e"
KEYCAT = "0x45ea1ec5613b20be000ec62a4443575ad530d8d8"


def test_park_boner_reference_hides_from_live_desk():
    from launchfinder.db import session_scope
    from launchfinder.models import utcnow
    from launchfinder.scoring.outcomes import park_boner_reference, restore_rh_ingest_grace

    init_db()
    with session_scope() as session:
        token = Token(
            mint=BONER_MINT.lower(),
            symbol="BONER",
            chain="robinhood",
            source="rh_backfill",
            is_historical=True,
            first_seen_at=utcnow(),
        )
        session.add(token)
        session.flush()
        restore_rh_ingest_grace(session)
        session.refresh(token)
        assert token.is_historical is True  # rh_backfill is not a live ingest
        assert park_boner_reference(session) == 1
        session.refresh(token)
        assert token.source == "backfill"
        assert token.is_historical is True
        assert park_boner_reference(session) == 0


def test_uniswap_v4_pool_id_is_not_a_token_mint():
    assert is_dex_pair_id(BONER_PAIR)
    assert is_dex_pair_id("0x1a812059207d57de177f7dd3669ffa0d5b0e18543a935167ce49b48903b5aef0")
    assert not is_dex_pair_id(BONER_MINT)
    assert not is_dex_pair_id("0xabc")


def test_market_from_pair_reads_base_token_and_hims_quote():
    pair = {
        "chainId": "robinhood",
        "dexId": "uniswap",
        "labels": ["v4"],
        "pairAddress": BONER_PAIR,
        "url": f"https://dexscreener.com/robinhood/{BONER_PAIR}",
        "baseToken": {"address": BONER_MINT, "name": "Boner Coin", "symbol": "BONER"},
        "quoteToken": {"address": HIMS, "name": "Hims & Hers Health • Robinhood Token", "symbol": "HIMS"},
        "priceUsd": "0.067",
        "marketCap": 67_000_000,
        "liquidity": {"usd": 2_800_000},
        "volume": {"h1": 480_000, "h24": 12_000_000, "m5": 10_000},
        "txns": {"m5": {"buys": 10, "sells": 8}, "h1": {"buys": 100, "sells": 90}},
        "priceChange": {"h1": 8.7},
        "pairCreatedAt": 1_787_259_586_000,
        "info": {
            "imageUrl": "https://cdn.example/boner.png",
            "websites": [{"url": "https://app.long.xyz/tokens/0x98096d17e191b3da1d5f99a6d7b3584351b11e18"}],
            "socials": [{"type": "twitter", "url": "https://x.com/bonercoinlong"}],
        },
    }
    market = _market_from_pair(pair, "robinhood")
    assert market["mint"] == BONER_MINT.lower()
    assert market["symbol"] == "BONER"
    assert market["image_url"] == "https://cdn.example/boner.png"
    assert market["quote_symbol"] == "HIMS"
    assert market["pair_address"] == BONER_PAIR
    assert market["liquidity_usd"] == 2_800_000
    assert market["created_at"] is not None
    coin = pair_to_coin(market)
    assert coin["skip_gmgn"] is True
    assert coin["launchpad"] == "long"
    assert coin["pool_address"] == BONER_PAIR


def test_rh_quote_tokens_cover_cost_and_live_usdg():
    quotes = {name: addr.lower() for name, addr in RH_QUOTE_TOKENS}
    assert quotes["COST"] == COST
    assert quotes["USDG"] == LIVE_USDG
    assert DEAD_USDG not in quotes.values()
    assert quotes["HIMS"] == HIMS.lower()
    assert "NVDA" in quotes
    assert "TSLA" in quotes
    assert quotes["GME"] == GME


def test_quoted_pairs_keeps_cost_quoted_hotdog():
    pairs = [
        {
            "chainId": "robinhood",
            "baseToken": {"address": HOTDOG, "symbol": "HOTDOG", "name": "hotdog"},
            "quoteToken": {"address": COST, "symbol": "COST"},
            "liquidity": {"usd": 180_335},
            "marketCap": 5_000_000,
            "pairCreatedAt": 1_788_379_574_000,
        },
        {
            "chainId": "robinhood",
            "baseToken": {"address": COST, "symbol": "COST"},
            "quoteToken": {"address": LIVE_USDG, "symbol": "USDG"},
            "liquidity": {"usd": 35_000},
            "pairCreatedAt": 1_788_400_000_000,
        },
    ]

    async def fake_get(url):
        return {"pairs": pairs}

    import launchfinder.research.dexscreener as dex

    orig = dex.get_json
    dex.get_json = fake_get
    try:
        rows = asyncio.run(quoted_pairs(COST, chain="robinhood"))
    finally:
        dex.get_json = orig
    assert [r["symbol"] for r in rows] == ["HOTDOG"]
    assert rows[0]["mint"] == HOTDOG
    assert rows[0]["quote_mint"] == COST
    assert rows[0]["liquidity_usd"] == 180_335


def test_quoted_pairs_keeps_gme_quoted_keycat():
    # Live 02:41: KEYCAT / Keyboard Cat was a GME book. Boost latest-first
    # caught it; the GME quote page was never polled. Same COST/HOTDOG door.
    pairs = [
        {
            "chainId": "robinhood",
            "baseToken": {"address": KEYCAT, "symbol": "KEYCAT", "name": "Keyboard Cat"},
            "quoteToken": {"address": GME, "symbol": "GME"},
            "liquidity": {"usd": 29_202},
            "marketCap": 146_000,
            "pairCreatedAt": 1_788_380_000_000,
        },
        {
            "chainId": "robinhood",
            "baseToken": {"address": GME, "symbol": "GME"},
            "quoteToken": {"address": LIVE_USDG, "symbol": "USDG"},
            "liquidity": {"usd": 40_000},
            "pairCreatedAt": 1_788_400_000_000,
        },
    ]

    async def fake_get(url):
        return {"pairs": pairs}

    import launchfinder.research.dexscreener as dex

    orig = dex.get_json
    dex.get_json = fake_get
    try:
        rows = asyncio.run(quoted_pairs(GME, chain="robinhood"))
    finally:
        dex.get_json = orig
    assert [r["symbol"] for r in rows] == ["KEYCAT"]
    assert rows[0]["mint"] == KEYCAT
    assert rows[0]["quote_mint"] == GME
    assert rows[0]["liquidity_usd"] == 29_202


def test_discover_fresh_quoted_prefers_fat_cost_book(monkeypatch):
    from launchfinder.ingest import dex_poll

    now = datetime.now(timezone.utc)
    fat = {
        "mint": HOTDOG,
        "symbol": "HOTDOG",
        "liquidity_usd": 180_335,
        "created_at": now - timedelta(hours=5.5),
    }
    dust = {
        "mint": "0x22efe678e91ff29d60ed287cba5d6a4b255b04aa",
        "symbol": "PONIS",
        "liquidity_usd": 1_200,
        "created_at": now - timedelta(minutes=20),
    }

    async def fake_quoted(quote_mint, chain="robinhood"):
        q = quote_mint.lower()
        if q == COST:
            return [fat]
        if q == HIMS.lower():
            return [dust]
        return []

    async def fake_boosted(*, limit=8):
        return []

    monkeypatch.setattr(dex_poll, "quoted_pairs", fake_quoted)
    monkeypatch.setattr(dex_poll, "boosted_rh_markets", fake_boosted)
    rows = asyncio.run(discover_fresh_quoted())
    assert [r["symbol"] for r in rows] == ["HOTDOG", "PONIS"]


def test_discover_fresh_quoted_skips_late_skinny_leftover_satellite(monkeypatch):
    # Live 04:10: PRESS 0x1a74…a81e ingested via the COIN quote page
    # (~$1.2k liq / 3h) then _best_pair stamped the Sep-8 SPY book
    # as t0=$993k. Quote-page late skinny + fat mcap stays out.
    # Fat late books (HOTDOG $180k liq) still ingest. Late skinny
    # under BOOST_LATE_MCAP still ingest. Boost-path LDX native-ETH
    # mid is unchanged. Do not recap LEGS late-ingest.
    from launchfinder.ingest import dex_poll

    now = datetime.now(timezone.utc)
    coin = "0x6330d8c3178a418788df01a47479c0ce7ccf450b"
    press = {
        "mint": "0x1a7402ca1144d9c42ca30080be51bb4fc9baa81e",
        "symbol": "PRESS",
        "quote_symbol": "COIN",
        "liquidity_usd": 1_247,
        "mcap_usd": 993_185,
        "created_at": now - timedelta(hours=3.1),
    }
    hotdog = {
        "mint": HOTDOG,
        "symbol": "HOTDOG",
        "quote_symbol": "COST",
        "liquidity_usd": 180_335,
        "mcap_usd": 5_000_000,
        "created_at": now - timedelta(hours=5.5),
    }
    young_dust = {
        "mint": "0x22efe678e91ff29d60ed287cba5d6a4b255b04aa",
        "symbol": "PONIS",
        "quote_symbol": "HIMS",
        "liquidity_usd": 1_200,
        "mcap_usd": 12_000,
        "created_at": now - timedelta(minutes=20),
    }
    late_thin_real = {
        "mint": "0x3333333333333333333333333333333333333333",
        "symbol": "THINOK",
        "quote_symbol": "HIMS",
        "liquidity_usd": 1_400,
        "mcap_usd": 40_000,
        "created_at": now - timedelta(hours=3.0),
    }

    async def fake_quoted(quote_mint, chain="robinhood"):
        q = quote_mint.lower()
        if q == coin:
            return [press]
        if q == COST:
            return [hotdog]
        if q == HIMS.lower():
            return [young_dust, late_thin_real]
        return []

    async def fake_boosted(*, limit=8):
        return []

    monkeypatch.setattr(dex_poll, "quoted_pairs", fake_quoted)
    monkeypatch.setattr(dex_poll, "boosted_rh_markets", fake_boosted)
    rows = asyncio.run(discover_fresh_quoted())
    assert [r["symbol"] for r in rows] == ["HOTDOG", "THINOK", "PONIS"]
    assert "PRESS" not in {r["symbol"] for r in rows}


def test_discover_fresh_quoted_picks_up_boosted_eth_v4_legs(monkeypatch):
    # Live 02:40: LEGS $2.1M / Uniswap V4 vs native ETH. WETH/USDG
    # token pages never listed it. Dex token-boosts/top did.
    # A 5.6h $2M print must not ingest as a new $40k t0.
    from launchfinder.ingest import dex_poll

    now = datetime.now(timezone.utc)
    legs = {
        "mint": "0x8fcf98e1348d3ddee46cdd15a5c7d9a8d423077d",
        "symbol": "LEGS",
        "name": "legs.fun",
        "quote_symbol": "ETH",
        "liquidity_usd": 137_899,
        "mcap_usd": 2_141_078,
        "created_at": now - timedelta(hours=5.6),
        "pair_address": "0x1a812059207d57de177f7dd3669ffa0d5b0e18543a935167ce49b48903b5aef0",
        "dex_labels": ["v4"],
    }

    async def fake_quoted(quote_mint, chain="robinhood"):
        return []

    async def fake_boosted(*, limit=8):
        return [legs]

    monkeypatch.setattr(dex_poll, "quoted_pairs", fake_quoted)
    monkeypatch.setattr(dex_poll, "boosted_rh_markets", fake_boosted)
    rows = asyncio.run(discover_fresh_quoted())
    assert rows == []

    legs["created_at"] = now - timedelta(minutes=40)
    legs["mcap_usd"] = 48_000
    rows = asyncio.run(discover_fresh_quoted())
    assert [r["symbol"] for r in rows] == ["LEGS"]
    assert rows[0]["quote_symbol"] == "ETH"


def test_discover_fresh_quoted_picks_up_ldx_native_eth_mid_boost(monkeypatch):
    # Live 03:49: LDX 0x62cd…0a5b / native ETH / $315k / 12h sat on
    # token-boosts/latest with no Token row. BOOST_LATE_MCAP ($150k
    # / >2h) dropped it. Native-ETH mid books under $400k / <18h
    # pass. LEGS $2.1M, GI $1.2M GLD, and SEND WETH 248h stay out.
    # Do not recap LEGS late-ingest. Do not raise BOOST_MARKET_CAP.
    from launchfinder.ingest import dex_poll

    now = datetime.now(timezone.utc)
    ldx = {
        "mint": "0x62cd5cd9d354a250affe9efa21c8918b9640a5b0",
        "symbol": "LDX",
        "name": "LDX",
        "quote_symbol": "ETH",
        "quote_mint": "0x0000000000000000000000000000000000000000",
        "liquidity_usd": 51_000,
        "mcap_usd": 315_000,
        "created_at": now - timedelta(hours=12.2),
        "pair_address": "0x" + "ld" * 32,
        "dex_labels": ["v4"],
    }
    legs = {
        "mint": "0x8fcf98e1348d3ddee46cdd15a5c7d9a8d423077d",
        "symbol": "LEGS",
        "name": "legs.fun",
        "quote_symbol": "ETH",
        "quote_mint": "0x0000000000000000000000000000000000000000",
        "liquidity_usd": 137_899,
        "mcap_usd": 2_141_078,
        "created_at": now - timedelta(hours=5.6),
        "pair_address": "0x1a812059207d57de177f7dd3669ffa0d5b0e18543a935167ce49b48903b5aef0",
        "dex_labels": ["v4"],
    }
    gi = {
        "mint": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "symbol": "GI",
        "quote_symbol": "GLD",
        "liquidity_usd": 80_000,
        "mcap_usd": 1_200_000,
        "created_at": now - timedelta(hours=5.0),
        "pair_address": "0x" + "gi" * 32,
        "dex_labels": ["v4"],
    }
    send = {
        "mint": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "symbol": "SEND",
        "quote_symbol": "WETH",
        "liquidity_usd": 90_000,
        "mcap_usd": 616_000,
        "created_at": now - timedelta(hours=23.0),
        "pair_address": "0x" + "se" * 32,
        "dex_labels": ["v4"],
    }
    weth_mid = {
        "mint": "0xcccccccccccccccccccccccccccccccccccccccc",
        "symbol": "WETHMID",
        "quote_symbol": "WETH",
        "liquidity_usd": 40_000,
        "mcap_usd": 315_000,
        "created_at": now - timedelta(hours=12.0),
        "pair_address": "0x" + "we" * 32,
        "dex_labels": ["v4"],
    }
    eth_ceiling = {
        "mint": "0xdddddddddddddddddddddddddddddddddddddddd",
        "symbol": "ETH400",
        "quote_symbol": "ETH",
        "quote_mint": "0x0000000000000000000000000000000000000000",
        "liquidity_usd": 40_000,
        "mcap_usd": 400_000,
        "created_at": now - timedelta(hours=12.0),
        "pair_address": "0x" + "e4" * 32,
        "dex_labels": ["v4"],
    }
    eth_clock = {
        "mint": "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        "symbol": "ETH18",
        "quote_symbol": "ETH",
        "quote_mint": "0x0000000000000000000000000000000000000000",
        "liquidity_usd": 40_000,
        "mcap_usd": 315_000,
        "created_at": now - timedelta(hours=18.0),
        "pair_address": "0x" + "e8" * 32,
        "dex_labels": ["v4"],
    }

    async def fake_quoted(quote_mint, chain="robinhood"):
        return []

    async def fake_boosted(*, limit=8):
        return [ldx, legs, gi, send, weth_mid, eth_ceiling, eth_clock]

    monkeypatch.setattr(dex_poll, "quoted_pairs", fake_quoted)
    monkeypatch.setattr(dex_poll, "boosted_rh_markets", fake_boosted)
    rows = asyncio.run(discover_fresh_quoted())
    assert [r["symbol"] for r in rows] == ["LDX"]
    assert rows[0]["quote_symbol"] == "ETH"
    assert rows[0]["quote_mint"] == "0x0000000000000000000000000000000000000000"


def test_discover_fresh_quoted_picks_up_profiled_clawdhood(monkeypatch):
    # Live 07:15: CLAWDHOOD 0x50ec…db9b / NVDA V4 / $53k liq / 45m
    # was 404. NVDA/USDG/GME quote pages had 0 quote-side launches
    # (30 pairs are the stock as base). Boosts did not list it.
    # token-profiles/latest had it #12. Skip known so the 8-lookup
    # cap reaches the unknown profile. Do not raise BOOST_MARKET_CAP.
    # Do not recap LEGS late-ingest.
    from launchfinder.db import session_scope
    from launchfinder.models import utcnow
    from launchfinder.ingest import dex_poll
    import launchfinder.research.dexscreener as dex

    now = datetime.now(timezone.utc)
    clawd = "0x50ec3b65691a911be049cd0d2d6e639cd1cfdb9b"
    ahead = [f"0x{i:02x}{'cc' * 19}" for i in range(11)]
    clawd_row = {
        "mint": clawd,
        "symbol": "CLAWDHOOD",
        "name": "CLAWDHOOD",
        "quote_symbol": "NVDA",
        "liquidity_usd": 52_878.0,
        "mcap_usd": 365_898.0,
        "created_at": now - timedelta(minutes=45),
        "pair_address": "0x" + "ca" * 32,
        "dex_labels": ["v4"],
    }

    init_db()
    with session_scope() as session:
        for mint in ahead:
            session.add(
                Token(
                    mint=mint,
                    symbol="KNOWN",
                    chain="robinhood",
                    source="rh_dex",
                    first_seen_at=utcnow(),
                )
            )

    async def fake_quoted(quote_mint, chain="robinhood"):
        return []

    async def fake_get(url):
        if "token-profiles/latest" in url:
            return (
                [{"chainId": "robinhood", "tokenAddress": mint} for mint in ahead]
                + [{"chainId": "robinhood", "tokenAddress": clawd}]
            )
        if "token-boosts/" in url:
            return []
        return {"pairs": []}

    async def fake_market(mint, chain="robinhood"):
        mint = (mint or "").lower()
        if mint == clawd:
            return dict(clawd_row)
        return {
            "mint": mint,
            "symbol": "STALE",
            "quote_symbol": "HIMS",
            "liquidity_usd": 12_000,
            "mcap_usd": 220_000,
            "created_at": now - timedelta(hours=10),
            "pair_address": "0x" + "ee" * 32,
            "dex_labels": ["v4"],
        }

    monkeypatch.setattr(dex_poll, "quoted_pairs", fake_quoted)
    monkeypatch.setattr(dex, "get_json", fake_get)
    monkeypatch.setattr(dex, "token_market", fake_market)
    rows = asyncio.run(discover_fresh_quoted())
    assert [r["symbol"] for r in rows] == ["CLAWDHOOD"]
    assert rows[0]["mint"] == clawd
    assert rows[0]["quote_symbol"] == "NVDA"


def test_discover_fresh_quoted_picks_up_latest_boosted_rig_class(monkeypatch):
    # Live 02:33: RIG / Stock Miner Uniswap V4 vs native ETH.
    # token-boosts/top had 12 stale RH boosts that ate the 8-lookup cap.
    # RIG was #0 on latest. Poll latest first so a new native-ETH V4
    # launch is not starved. Do not recap LEGS late-ingest.
    from launchfinder.ingest import dex_poll
    import launchfinder.research.dexscreener as dex

    now = datetime.now(timezone.utc)
    rig_mint = "0x3c31029d4eb1cd8bca6b26e03af647de5dfa943f"
    stale = [f"0x{i:02x}{'aa' * 19}" for i in range(12)]
    rig = {
        "mint": rig_mint,
        "symbol": "RIG",
        "name": "Stock Miner",
        "quote_symbol": "ETH",
        "liquidity_usd": 98_400,
        "mcap_usd": 1_100_000,
        "created_at": now - timedelta(minutes=50),
        "pair_address": "0x" + "cd" * 32,
        "dex_labels": ["v4"],
    }

    async def fake_quoted(quote_mint, chain="robinhood"):
        return []

    async def fake_get(url):
        if "token-boosts/latest" in url:
            return [{"chainId": "robinhood", "tokenAddress": rig_mint}]
        if "token-boosts/top" in url:
            return [{"chainId": "robinhood", "tokenAddress": mint} for mint in stale]
        return {"pairs": []}

    async def fake_market(mint, chain="robinhood"):
        mint = (mint or "").lower()
        if mint == rig_mint:
            return dict(rig)
        return {
            "mint": mint,
            "symbol": "STALE",
            "quote_symbol": "HIMS",
            "liquidity_usd": 12_000,
            "mcap_usd": 220_000,
            "created_at": now - timedelta(hours=10),
            "pair_address": "0x" + "ee" * 32,
            "dex_labels": ["v4"],
        }

    monkeypatch.setattr(dex_poll, "quoted_pairs", fake_quoted)
    monkeypatch.setattr(dex, "get_json", fake_get)
    monkeypatch.setattr(dex, "token_market", fake_market)
    rows = asyncio.run(discover_fresh_quoted())
    assert [r["symbol"] for r in rows] == ["RIG"]
    assert rows[0]["mint"] == rig_mint
    assert rows[0]["quote_symbol"] == "ETH"


def test_boosted_rh_markets_skips_known_mints_so_memeflix_gets_a_lookup(monkeypatch):
    # Live 03:12: MEMEFLIX $17k / 4.7h / ETH sat #10 on latest.
    # Already-ingested KEYCAT / RIG / Stocker ate the 8-lookup cap.
    # Skip known Token rows so the cap hits unknown boosts.
    # Do not raise BOOST_MARKET_CAP. Do not recap LEGS late-ingest.
    from launchfinder.db import session_scope
    from launchfinder.models import utcnow
    import launchfinder.research.dexscreener as dex

    now = datetime.now(timezone.utc)
    memeflix = "0xbb6ec1e12a2d36b0b31d4e155bfab92b2c69b3d4"
    known = [f"0x{i:02x}{'bb' * 19}" for i in range(8)]
    memeflix_row = {
        "mint": memeflix,
        "symbol": "MEMEFLIX",
        "name": "$MEMEFLIX",
        "quote_symbol": "ETH",
        "liquidity_usd": 14_456,
        "mcap_usd": 17_262,
        "created_at": now - timedelta(hours=4.7),
        "pair_address": "0x" + "ab" * 32,
        "dex_labels": ["v4"],
    }

    init_db()
    with session_scope() as session:
        for mint in known:
            session.add(
                Token(
                    mint=mint,
                    symbol="KNOWN",
                    chain="robinhood",
                    source="rh_dex",
                    first_seen_at=utcnow(),
                )
            )

    lookups: list[str] = []

    async def fake_get(url):
        if "token-boosts/latest" in url:
            return [{"chainId": "robinhood", "tokenAddress": mint} for mint in known + [memeflix]]
        if "token-boosts/top" in url:
            return []
        return {"pairs": []}

    async def fake_market(mint, chain="robinhood"):
        mint = (mint or "").lower()
        lookups.append(mint)
        if mint == memeflix:
            return dict(memeflix_row)
        return {
            "mint": mint,
            "symbol": "STALE",
            "quote_symbol": "HIMS",
            "liquidity_usd": 12_000,
            "mcap_usd": 22_000,
            "created_at": now - timedelta(hours=10),
            "pair_address": "0x" + "ee" * 32,
            "dex_labels": ["v4"],
        }

    monkeypatch.setattr(dex, "get_json", fake_get)
    monkeypatch.setattr(dex, "token_market", fake_market)
    rows = asyncio.run(dex.boosted_rh_markets())
    assert lookups == [memeflix]
    assert [r["symbol"] for r in rows] == ["MEMEFLIX"]
    assert rows[0]["mint"] == memeflix
    assert rows[0]["quote_symbol"] == "ETH"


def test_quoted_pairs_keeps_only_quote_side_launches():
    hims = HIMS.lower()
    pairs = [
        {
            "chainId": "robinhood",
            "baseToken": {"address": BONER_MINT, "symbol": "BONER"},
            "quoteToken": {"address": HIMS, "symbol": "HIMS"},
            "liquidity": {"usd": 2_800_000},
            "pairCreatedAt": 1_787_259_586_000,
        },
        {
            "chainId": "robinhood",
            "baseToken": {"address": HIMS, "symbol": "HIMS"},
            "quoteToken": {"address": "0xF052F9339afAbA171724B3229a624Db2a30eB115", "symbol": "USDG"},
            "liquidity": {"usd": 400_000},
            "pairCreatedAt": 1_787_259_586_000,
        },
    ]

    async def fake_get(url):
        return {"pairs": pairs}

    import launchfinder.research.dexscreener as dex

    orig = dex.get_json
    dex.get_json = fake_get
    try:
        rows = asyncio.run(quoted_pairs(HIMS, chain="robinhood"))
    finally:
        dex.get_json = orig
    assert [r["symbol"] for r in rows] == ["BONER"]
    assert rows[0]["quote_mint"] == hims


def test_poll_rh_dex_ingests_fresh_hims_pair(monkeypatch):
    from launchfinder.ingest import dex_poll

    created = datetime.now(timezone.utc) - timedelta(hours=2)
    row = {
        "mint": "0x22efe678e91ff29d60ed287cba5d6a4b255b04aa",
        "name": "PONIS",
        "symbol": "PONIS",
        "quote_symbol": "HIMS",
        "pair_address": "0x" + "ab" * 32,
        "liquidity_usd": 15_000,
        "mcap_usd": 26_000,
        "created_at": created,
        "website": "https://app.long.xyz/tokens/0x22efe678e91ff29d60ed287cba5d6a4b255b04aa",
        "dex_labels": ["v4"],
    }
    ingested: list[tuple[str, str]] = []

    async def fake_discover(*, max_age_hours=24.0):
        return [row]

    async def fake_seed():
        return False

    async def fake_ingest(session, **kw):
        ingested.append((kw["mint"], kw["source"]))
        return Token(mint=kw["mint"], symbol="PONIS", chain="robinhood", source=kw["source"])

    monkeypatch.setattr(dex_poll, "settings", SimpleNamespace(robinhood_enabled=True))
    monkeypatch.setattr(dex_poll, "discover_fresh_quoted", fake_discover)
    monkeypatch.setattr(dex_poll, "ensure_boner_reference", fake_seed)
    monkeypatch.setattr(dex_poll, "ingest_and_research", fake_ingest)

    init_db()
    found = asyncio.run(dex_poll.poll_rh_dex())
    assert ingested == [("0x22efe678e91ff29d60ed287cba5d6a4b255b04aa", "rh_dex")]
    assert found == ["0x22efe678e91ff29d60ed287cba5d6a4b255b04aa"]


def test_get_token_resolves_dex_pair_id():
    import asyncio

    from launchfinder.app import get_token
    from launchfinder.db import SessionLocal
    from launchfinder.models import Research, utcnow

    init_db()
    session = SessionLocal()
    try:
        existing = session.query(Token).filter(Token.mint == BONER_MINT.lower()).one_or_none()
        if existing is None:
            token = Token(
                mint=BONER_MINT.lower(),
                symbol="BONER",
                name="Boner Coin",
                chain="robinhood",
                source="rh_backfill",
                pool_address=BONER_PAIR,
                first_seen_at=utcnow(),
                is_historical=True,
            )
            token.research = Research(p_good=0.4, features_json="{}")
            session.add(token)
        else:
            existing.pool_address = BONER_PAIR
            existing.symbol = "BONER"
            if existing.research is None:
                existing.research = Research(p_good=0.4, features_json="{}")
        session.commit()
    finally:
        session.close()

    card = asyncio.run(get_token(BONER_MINT.lower()))
    assert card["symbol"] == "BONER"
    assert card["links"]["dex"].endswith(BONER_PAIR)

    async def fake_pair(pair_id, chain="sol"):
        assert is_dex_pair_id(pair_id)
        return {"mint": BONER_MINT.lower()}

    import launchfinder.research.dexscreener as dex

    orig = dex.pair_market
    dex.pair_market = fake_pair
    try:
        via_pair = asyncio.run(get_token(BONER_PAIR))
    finally:
        dex.pair_market = orig
    assert via_pair["symbol"] == "BONER"
    assert via_pair["mint"] == BONER_MINT.lower()


WOJAK = "DUe1qheefigxhjboRqKkGWjSTeYivjLo6yq763redsRC"
KEK = "BtFegYSVBRxy1111111111111111111111111111111"
KERMIT = "BmnGRH8N1ZXo1111111111111111111111111111111"
PUMP_MINT = "AeSDNvEriwPcnwMT111111111111111111111111pump"


def test_sol_quote_tokens_cover_pepe_only():
    quotes = {name: addr for name, addr in SOL_QUOTE_TOKENS}
    assert quotes["PEPE"] == SOL_PEPE_MINT
    assert "WSOL" not in quotes
    assert BOOST_MARKET_CAP == 8
    assert SOL_DEX_MAX_NEW == 8


def test_sol_dex_row_ok_keeps_young_pepe_book_skips_wojak_leftover():
    young = {
        "mint": KEK,
        "liquidity_usd": 14_453,
        "mcap_usd": 25_547,
        "dex_id": "raydium",
    }
    leftover = {
        "mint": WOJAK,
        "liquidity_usd": 117_276,
        "mcap_usd": 1_512_385,
        "dex_id": "raydium",
    }
    curve = {
        "mint": KERMIT,
        "liquidity_usd": 18_000,
        "mcap_usd": 40_000,
        "dex_id": "pumpfun",
    }
    pump = {
        "mint": PUMP_MINT,
        "liquidity_usd": 20_000,
        "mcap_usd": 70_000,
        "dex_id": "raydium",
    }
    thin = {
        "mint": KEK,
        "liquidity_usd": 800,
        "mcap_usd": 12_000,
        "dex_id": "raydium",
    }
    young_fat = {
        "mint": WOJAK,
        "liquidity_usd": 80_000,
        "mcap_usd": 400_000,
        "dex_id": "raydium",
    }
    glitter = {
        "mint": "EcSgxsBGoqZNxHgKhon3jSmqjYW57fSgLFrrF8SgTVGR",
        "liquidity_usd": 20_077,
        "mcap_usd": 10_041_595,
        "dex_id": "raydium",
    }
    assert sol_dex_row_ok(young, 0.4)
    assert sol_dex_row_ok(young, 6.6)
    assert not sol_dex_row_ok(leftover, 13.2)
    assert not sol_dex_row_ok(curve, 0.3)
    assert not sol_dex_row_ok(pump, 0.4)
    assert not sol_dex_row_ok(thin, 0.4)
    assert sol_dex_row_ok(young_fat, 0.5)
    assert not sol_dex_row_ok(young_fat, 3.0)
    assert not sol_dex_row_ok(glitter, 0.7)


def test_quoted_pairs_keeps_pepe_quoted_wojak():
    pairs = [
        {
            "chainId": "solana",
            "dexId": "raydium",
            "baseToken": {"address": WOJAK, "symbol": "WOJAK", "name": "Wojak"},
            "quoteToken": {"address": SOL_PEPE_MINT, "symbol": "PEPE"},
            "liquidity": {"usd": 80_537},
            "marketCap": 69_000,
            "pairCreatedAt": 1_789_800_000_000,
        },
        {
            "chainId": "solana",
            "dexId": "raydium",
            "baseToken": {"address": SOL_PEPE_MINT, "symbol": "PEPE"},
            "quoteToken": {"address": "So11111111111111111111111111111111111111112", "symbol": "SOL"},
            "liquidity": {"usd": 2_000_000},
            "pairCreatedAt": 1_700_000_000_000,
        },
        {
            "chainId": "solana",
            "dexId": "pumpfun",
            "baseToken": {"address": KEK, "symbol": "PPPP"},
            "quoteToken": {"address": SOL_PEPE_MINT, "symbol": "PEPE"},
            "liquidity": {"usd": 0},
            "pairCreatedAt": 1_789_900_000_000,
        },
    ]

    async def fake_get(url):
        return {"pairs": pairs}

    import launchfinder.research.dexscreener as dex

    orig = dex.get_json
    dex.get_json = fake_get
    try:
        rows = asyncio.run(quoted_pairs(SOL_PEPE_MINT, chain="sol"))
    finally:
        dex.get_json = orig
    assert [r["symbol"] for r in rows] == ["WOJAK", "PPPP"]
    assert rows[0]["mint"] == WOJAK
    assert rows[0]["quote_mint"] == SOL_PEPE_MINT
    assert rows[0]["liquidity_usd"] == 80_537


def test_sol_profile_mints_skip_pump_and_pepe():
    rows = [
        {"chainId": "solana", "tokenAddress": KEK},
        {"chainId": "solana", "tokenAddress": PUMP_MINT},
        {"chainId": "solana", "tokenAddress": SOL_PEPE_MINT},
        {"chainId": "robinhood", "tokenAddress": HOTDOG},
        {"chainId": "solana", "tokenAddress": WOJAK},
    ]
    assert _sol_profile_mints(rows) == [KEK, WOJAK]


def test_discover_fresh_sol_quoted_keeps_kek_skips_wojak_leftover(monkeypatch):
    from launchfinder.ingest import dex_poll

    now = datetime.now(timezone.utc)
    kek = {
        "mint": KEK,
        "symbol": "KEK",
        "quote_symbol": "PEPE",
        "dex_id": "raydium",
        "liquidity_usd": 14_453,
        "mcap_usd": 25_547,
        "created_at": now - timedelta(hours=0.4),
    }
    wojak = {
        "mint": WOJAK,
        "symbol": "WOJAK",
        "quote_symbol": "PEPE",
        "dex_id": "raydium",
        "liquidity_usd": 117_276,
        "mcap_usd": 1_512_385,
        "created_at": now - timedelta(hours=13.2),
    }
    curve = {
        "mint": KERMIT,
        "symbol": "PPPP",
        "quote_symbol": "PEPE",
        "dex_id": "pumpfun",
        "liquidity_usd": 18_000,
        "mcap_usd": 40_000,
        "created_at": now - timedelta(minutes=20),
    }

    async def fake_quoted(quote_mint, chain="sol"):
        assert chain == "sol"
        assert quote_mint == SOL_PEPE_MINT
        return [kek, wojak, curve]

    async def fake_boosted(*, limit=8):
        assert limit == BOOST_MARKET_CAP
        return []

    monkeypatch.setattr(dex_poll, "quoted_pairs", fake_quoted)
    monkeypatch.setattr(dex_poll, "boosted_sol_markets", fake_boosted)
    rows = asyncio.run(discover_fresh_sol_quoted())
    assert [r["symbol"] for r in rows] == ["KEK"]
    assert "WOJAK" not in {r["symbol"] for r in rows}


def test_discover_fresh_sol_quoted_picks_up_young_boosted_nonpump(monkeypatch):
    from launchfinder.ingest import dex_poll

    now = datetime.now(timezone.utc)
    profiled = {
        "mint": KERMIT,
        "symbol": "KERMIT",
        "quote_symbol": "SOL",
        "dex_id": "raydium",
        "liquidity_usd": 18_552,
        "mcap_usd": 45_977,
        "created_at": now - timedelta(minutes=40),
    }
    late_fat = {
        "mint": WOJAK,
        "symbol": "WOJAK",
        "quote_symbol": "PEPE",
        "dex_id": "raydium",
        "liquidity_usd": 117_276,
        "mcap_usd": 1_512_385,
        "created_at": now - timedelta(hours=5.0),
    }

    async def fake_quoted(quote_mint, chain="sol"):
        return []

    async def fake_boosted(*, limit=8):
        return [profiled, late_fat]

    monkeypatch.setattr(dex_poll, "quoted_pairs", fake_quoted)
    monkeypatch.setattr(dex_poll, "boosted_sol_markets", fake_boosted)
    rows = asyncio.run(discover_fresh_sol_quoted())
    assert [r["symbol"] for r in rows] == ["KERMIT"]
    assert rows[0]["quote_symbol"] == "SOL"


def test_poll_sol_dex_ingests_fresh_pepe_pair(monkeypatch):
    from launchfinder.ingest import dex_poll

    created = datetime.now(timezone.utc) - timedelta(minutes=25)
    row = {
        "mint": KEK,
        "name": "KEK",
        "symbol": "KEK",
        "quote_symbol": "PEPE",
        "pair_address": "BWh4RGBiantA3RZBrw8pZnMstkDCb2thJQ4v8p5x9SyC",
        "liquidity_usd": 14_453,
        "mcap_usd": 25_547,
        "created_at": created,
        "dex_id": "raydium",
    }
    ingested: list[tuple[str, str, bool, str]] = []

    async def fake_discover(*, max_age_hours=None):
        return [row]

    async def fake_ingest(session, **kw):
        coin = kw["coin"]
        ingested.append((kw["mint"], kw["source"], coin.get("skip_gmgn"), coin.get("chain")))
        return Token(mint=kw["mint"], symbol="KEK", chain="sol", source=kw["source"])

    monkeypatch.setattr(dex_poll, "discover_fresh_sol_quoted", fake_discover)
    monkeypatch.setattr(dex_poll, "ingest_and_research", fake_ingest)

    init_db()
    found = asyncio.run(dex_poll.poll_sol_dex())
    assert ingested == [(KEK, "sol_dex", True, "sol")]
    assert found == [KEK]
    coin = sol_pair_to_coin(row)
    assert coin["skip_gmgn"] is True
    assert coin["launchpad"] == "dex"
    assert coin["chain"] == "sol"
