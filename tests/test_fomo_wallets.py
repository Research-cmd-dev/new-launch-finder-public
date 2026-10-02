from __future__ import annotations

import asyncio
import json

from launchfinder.app import fomo_wallets
from launchfinder.db import SessionLocal
from launchfinder.models import Outcome, Research, Token
from launchfinder.research.fomo_wallets import (
    SKIP_LABELS,
    SKIP_OWNERS,
    accumulate_labeled_maps,
    list_fomo_wallets,
    rebuild_from_runners,
)
from launchfinder.scoring.features import FEATURE_NAMES


def _seed_runner(
    session,
    *,
    mint: str,
    symbol: str,
    chain: str,
    multiple: float,
    holders: list[dict],
) -> tuple[Token, Research, Outcome, float]:
    token = Token(mint=mint, symbol=symbol, name=symbol, chain=chain, source="poll")
    research = Research(
        raw_json=json.dumps({"holders": {"top_wallets": holders}}),
        holder_count=len(holders),
        features_json="{}",
        risk_flags_json="[]",
    )
    token.research = research
    session.add(token)
    session.flush()
    outcome = Outcome(
        token_id=token.id,
        t0_mcap=20_000,
        max_mcap=20_000 * multiple,
        last_mcap=20_000 * multiple,
        last_liq=12_000,
        multiple=multiple,
        label=1,
    )
    session.add(outcome)
    session.flush()
    return token, research, outcome, multiple


def test_rebuild_ranks_repeat_wallets_and_skips_pool_labels() -> None:
    repeat = "SoLFomoWallet111111111111111111111111111"
    once = "SoLOnceWallet222222222222222222222222222"
    db = SessionLocal()
    try:
        runners = [
            _seed_runner(
                db,
                mint="MintA1111111111111111111111111111111111111",
                symbol="WINA",
                chain="sol",
                multiple=12.0,
                holders=[
                    {"owner": repeat, "pct": 3.2, "label": "holder"},
                    {"owner": "SoLCreator333333333333333333333333333333", "pct": 8.0, "label": "creator"},
                    {"owner": "SoLPool444444444444444444444444444444444", "pct": 40.0, "label": "pool"},
                ],
            ),
            _seed_runner(
                db,
                mint="MintB1111111111111111111111111111111111111",
                symbol="WINB",
                chain="sol",
                multiple=8.5,
                holders=[
                    {"owner": repeat, "pct": 1.4, "label": None},
                    {"owner": once, "pct": 2.1, "label": "sniper"},
                ],
            ),
            _seed_runner(
                db,
                mint="MintC1111111111111111111111111111111111111",
                symbol="WINC",
                chain="sol",
                multiple=6.0,
                holders=[{"owner": repeat, "pct": 0.9, "label": "holder"}],
            ),
        ]
        payload = rebuild_from_runners(db, "sol", runners)
        db.commit()
        assert payload["mapped"] == 3
        assert payload["wallets"] == 2
        listed = list_fomo_wallets(db, "sol", rebuild=False)
        assert listed["source"] == "stored_holder_maps"
        assert listed["mapped"] == 3
        assert listed["wallets"] == 2
        items = listed["items"]
        assert items[0]["owner"] == repeat
        assert items[0]["n_tokens"] == 3
        assert items[0]["best_symbol"] == "WINA"
        assert items[0]["best_multiple"] == 12.0
        assert len(items[0]["hits"]) == 3
        assert items[1]["owner"] == once
        assert items[1]["n_tokens"] == 1
        assert items[0]["n_wins"] == 3
        assert items[0]["n_rugs"] == 0
        again = rebuild_from_runners(db, "sol", runners)
        db.commit()
        assert again["mapped"] == 3
        assert again["wallets"] == 2
        listed_again = list_fomo_wallets(db, "sol", rebuild=False)
        assert listed_again["items"][0]["n_tokens"] == 3
        assert len(listed_again["items"][0]["hits"]) == 3
    finally:
        db.close()


def test_accumulate_adds_rug_without_wiping_wins() -> None:
    repeat = "SoLFomoWallet111111111111111111111111111"
    db = SessionLocal()
    try:
        win = _seed_runner(
            db,
            mint="MintWin11111111111111111111111111111111111",
            symbol="WINX",
            chain="sol",
            multiple=9.0,
            holders=[{"owner": repeat, "pct": 2.2, "label": "holder"}],
        )
        rebuild_from_runners(db, "sol", [win])
        rug_token = Token(mint="MintRug11111111111111111111111111111111111", symbol="RUGX", chain="sol", source="poll")
        rug_token.research = Research(
            raw_json=json.dumps({"holders": {"top_wallets": [{"owner": repeat, "pct": 4.0, "label": "holder"}]}}),
            holder_count=1,
            features_json="{}",
            risk_flags_json="[]",
        )
        db.add(rug_token)
        db.flush()
        db.add(
            Outcome(
                token_id=rug_token.id,
                t0_mcap=20_000,
                max_mcap=8_000,
                last_mcap=8_000,
                last_liq=400,
                multiple=0.4,
                label=0,
            )
        )
        db.flush()
        mapped = accumulate_labeled_maps(db, "sol", limit=80)
        db.commit()
        assert mapped >= 1
        listed = list_fomo_wallets(db, "sol", rebuild=False)
        assert listed["wallets"] == 1
        item = listed["items"][0]
        assert item["owner"] == repeat
        assert item["n_wins"] == 1
        assert item["n_rugs"] == 1
        assert item["n_tokens"] == 2
        assert any(h["symbol"] == "WINX" and h["win"] for h in item["hits"])
        assert any(h["symbol"] == "RUGX" and not h["win"] for h in item["hits"])
    finally:
        db.close()


def test_rh_wallet_addresses_are_lowercased() -> None:
    owner = "0xAbCdEf0123456789abcdef0123456789ABCDEF01"
    db = SessionLocal()
    try:
        runners = [
            _seed_runner(
                db,
                mint="0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                symbol="RHWIN",
                chain="robinhood",
                multiple=9.0,
                holders=[{"owner": owner, "pct": 2.5, "label": "holder"}],
            )
        ]
        payload = rebuild_from_runners(db, "robinhood", runners)
        db.commit()
        assert payload["wallets"] == 1
        listed = list_fomo_wallets(db, "robinhood", rebuild=False)
        assert listed["items"][0]["owner"] == owner.lower()
    finally:
        db.close()


def test_skip_labels_cover_infra_wallets() -> None:
    assert {"creator", "pool", "dev", "burn", "dex", "contract", "router", "aggregator"} <= SKIP_LABELS
    assert "0x000000000000000000000000000000000000dead" in SKIP_OWNERS


def test_rh_burn_address_is_excluded_even_without_label() -> None:
    db = SessionLocal()
    try:
        runners = [
            _seed_runner(
                db,
                mint="0xbeefbeefbeefbeefbeefbeefbeefbeefbeefbeef",
                symbol="BURNED",
                chain="robinhood",
                multiple=11.0,
                holders=[
                    {"owner": "0x000000000000000000000000000000000000dEad", "pct": 40.0, "label": ""},
                    {"owner": "0xAbcDef0123456789abcdef0123456789ABCDEF02", "pct": 2.0, "label": "holder"},
                ],
            )
        ]
        rebuild_from_runners(db, "robinhood", runners)
        listed = list_fomo_wallets(db, "robinhood", rebuild=False)
        assert listed["mapped"] == 1
        assert listed["wallets"] == 1
        assert listed["items"][0]["owner"] == "0xabcdef0123456789abcdef0123456789abcdef02"
        assert "dead" not in listed["items"][0]["owner"]
    finally:
        db.close()


def test_fomo_wallets_api_returns_empty_then_rows() -> None:
    empty = asyncio.run(fomo_wallets(chain="sol", limit=100))
    assert empty["items"] == []
    assert empty["mapped"] == 0
    assert empty["source"] == "stored_holder_maps"

    db = SessionLocal()
    try:
        runners = [
            _seed_runner(
                db,
                mint="MintApi11111111111111111111111111111111111",
                symbol="APIW",
                chain="sol",
                multiple=7.2,
                holders=[{"owner": "SoLApiWallet555555555555555555555555555", "pct": 1.1}],
            )
        ]
        rebuild_from_runners(db, "sol", runners)
        db.commit()
    finally:
        db.close()

    filled = asyncio.run(fomo_wallets(chain="sol", limit=10))
    assert filled["wallets"] == 1
    assert filled["items"][0]["best_symbol"] == "APIW"
    assert filled["items"][0]["hits"][0]["mint"].startswith("MintApi")


def test_fomo_ranks_this_window_over_historical() -> None:
    db = SessionLocal()
    try:
        fat = "SoLFatWallet666666666666666666666666666"
        live = "SoLLiveWallet777777777777777777777777777"
        hist = _seed_runner(
            db,
            mint="MintHist111111111111111111111111111111111",
            symbol="USWS",
            chain="sol",
            multiple=40.0,
            holders=[{"owner": fat, "pct": 2.0, "label": "holder"}],
        )
        rebuild_from_runners(db, "sol", [hist])
        hist[0].is_historical = True
        db.flush()
        live_a = _seed_runner(
            db,
            mint="MintLiveA1111111111111111111111111111111",
            symbol="PUGCOIN",
            chain="sol",
            multiple=14.0,
            holders=[{"owner": live, "pct": 1.4, "label": "holder"}],
        )
        live_b = _seed_runner(
            db,
            mint="MintLiveB1111111111111111111111111111111",
            symbol="SAAR",
            chain="sol",
            multiple=11.0,
            holders=[{"owner": live, "pct": 1.1, "label": "holder"}],
        )
        rebuild_from_runners(db, "sol", [live_a, live_b])
        db.commit()
        listed = list_fomo_wallets(db, "sol", rebuild=False)
        assert listed["items"][0]["owner"] == live
        assert listed["items"][0]["n_live_wins"] == 2
        assert listed["items"][0]["best_symbol"] == "PUGCOIN"
        fat_row = next(row for row in listed["items"] if row["owner"] == fat)
        assert fat_row["n_live_wins"] == 0
    finally:
        db.close()


def test_feature_names_stay_sixty_six() -> None:
    assert len(FEATURE_NAMES) == 66
