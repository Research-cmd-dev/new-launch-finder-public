"""GMGN rank + DexScreener secondary board parsing (stack-v191)."""

from __future__ import annotations

from launchfinder.research import gmgn
from launchfinder.research.dexscreener import _sol_dex_trending_rows_from_api


def test_gmgn_rank_list_nested_data_rank():
    payload = {
        "code": 0,
        "data": {
            "rank": [
                {"address": "Mint1111111111111111111111111111111111", "symbol": "A", "market_cap": 1000},
            ],
        },
    }
    rows, trace = gmgn._gmgn_rank_list_from_payload(payload)
    assert len(rows) == 1
    assert trace


def test_gmgn_rank_list_double_nested():
    payload = {
        "data": {
            "data": {
                "rank": [{"address": "Mint2222222222222222222222222222222222", "symbol": "B"}],
            },
        },
    }
    rows, _ = gmgn._gmgn_rank_list_from_payload(payload)
    assert len(rows) == 1


def test_sol_dex_trending_rows_accepts_boost_shape():
    api_rows = [
        {
            "chainId": "solana",
            "tokenAddress": "59PXVfJ28HLYpdYLz8rt8ziE9EWbK4mS8xvq38NUQ1Be",
            "description": "REPO · open source markets",
        },
        {"chainId": "ethereum", "tokenAddress": "0xabc"},
    ]
    out = _sol_dex_trending_rows_from_api(api_rows, cap=10)
    assert len(out) == 1
    assert out[0]["mint"] == "59PXVfJ28HLYpdYLz8rt8ziE9EWbK4mS8xvq38NUQ1Be"
    assert out[0]["symbol"].startswith("REPO")


def test_pack_secondary_board_surfaces_empty_reason():
    from launchfinder.research.fomo_coverage import _pack_secondary_board

    pack = _pack_secondary_board(
        board_kind="gmgn_trending",
        default_note="note",
        upstream={"empty_reason": "no_rank_list_in_payload", "api_source": "gmgn", "board_live": False},
        desk_items=[],
    )
    assert pack["empty_reason"] == "no_rank_list_in_payload"
    assert pack["items"] == []
