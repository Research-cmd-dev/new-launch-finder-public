from datetime import datetime, timedelta, timezone

from launchfinder.ingest.bitquery import (
    argument_map,
    bitquery_ws_url,
    mint_from_initialize,
    should_skip_late_book,
    trades_to_candles,
)
from launchfinder.scoring.features import FEATURE_NAMES


def test_mint_from_initialize_picks_token_vs_native_eth() -> None:
    # Live ROUTE: Uni V4 vs native ETH.
    args = [
        {"Name": "currency0", "Value": {"address": "0x0000000000000000000000000000000000000000"}},
        {"Name": "currency1", "Value": {"address": "0x4a72b9702f991b790788f8afa9e7112541f4e8f8"}},
        {"Name": "id", "Value": {"hex": "220e47dde1a5180cb131d4c720abf66d5c36fbdf3af91522f7b1c7f749770d8f"}},
    ]
    mint, pool = mint_from_initialize(args)
    assert mint == "0x4a72b9702f991b790788f8afa9e7112541f4e8f8"
    assert pool.startswith("0x220e47")
    assert len(FEATURE_NAMES) == 66


def test_mint_from_initialize_skips_quote_quote() -> None:
    hims = "0xccee82fe024c36fa15e1005ede3e9e4787e23d09"
    weth = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
    mint, _ = mint_from_initialize(
        [
            {"Name": "currency0", "Value": {"address": hims}},
            {"Name": "currency1", "Value": {"address": weth}},
        ]
    )
    assert mint == ""


def test_argument_map_reads_either_case() -> None:
    mapped = argument_map([{"name": "currency1", "value": {"address": "0xABC"}}])
    assert mapped["currency1"].lower().endswith("abc")


def test_should_skip_late_fat_leftover() -> None:
    old = datetime.now(timezone.utc) - timedelta(hours=5)
    assert should_skip_late_book({"created_at": old, "mcap_usd": 2_100_000.0, "liquidity_usd": 80_000.0}, old) is True
    now = datetime.now(timezone.utc)
    assert should_skip_late_book(None, now) is False
    assert (
        should_skip_late_book(
            {"created_at": now, "mcap_usd": 40_000.0, "liquidity_usd": 5_000.0},
            now,
        )
        is False
    )


def test_bitquery_ws_url_hides_empty() -> None:
    assert bitquery_ws_url("") == ""
    url = bitquery_ws_url("ory_at_test")
    assert url.startswith("wss://streaming.bitquery.io/graphql?token=")
    assert "ory_at_test" in url


def test_trades_to_candles_buckets_usd_and_never_invents_holders() -> None:
    t0 = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
    rows = [
        {"Block": {"Time": t0.isoformat()}, "Trade": {"PriceInUSD": 0.001, "AmountInUSD": 200}},
        {"Block": {"Time": (t0 + timedelta(seconds=20)).isoformat()}, "Trade": {"PriceInUSD": 0.0012, "AmountInUSD": 300}},
        {"Block": {"Time": (t0 + timedelta(minutes=1, seconds=5)).isoformat()}, "Trade": {"PriceInUSD": 0.0009, "AmountInUSD": 100}},
    ]
    candles = trades_to_candles(rows)
    assert len(candles) == 2
    first = candles[0]
    assert first["open"] == 0.001 and first["close"] == 0.0012 and first["high"] == 0.0012
    assert first["volume"] == 500
    assert "holders" not in first
    assert trades_to_candles([{"Block": {"Time": t0.isoformat()}, "Trade": {"PriceInUSD": 0, "AmountInUSD": 10}}]) == []
