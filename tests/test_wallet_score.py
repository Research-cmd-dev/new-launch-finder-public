from __future__ import annotations

import asyncio
import json

from launchfinder.app import scored_wallet, scored_wallets
from launchfinder.db import SessionLocal
from launchfinder.models import EarlyWallet, EarlyWalletHit, FomoWallet, FomoWalletHit, Research, Token
from launchfinder.research.wallet_score import compute_wallet_score, list_scored_wallets, score_wallet
from launchfinder.scoring.features import FEATURE_NAMES


OWNER = "Ax5dAamJPeuaLpFUzs9FdcpoUhHDcxyjPzxCJQidjups"


def test_compute_wallet_score_gold_is_high() -> None:
    score, grade = compute_wallet_score(
        n_sized=2,
        n_early=2,
        n_early_profitable=2,
        n_still_in=1,
        n_fomo_wins=2,
        n_rugs=0,
        alpha_runs=2,
    )
    assert score >= 70
    assert grade == "A"


def test_repeat_this_window_fomo_is_grade_c() -> None:
    score, grade = compute_wallet_score(n_fomo_wins=3)
    assert score >= 30
    assert grade == "C"
    two, two_grade = compute_wallet_score(n_fomo_wins=2)
    assert two < 30
    assert two_grade == "—"


def test_unknown_wallet_scores_zero() -> None:
    db = SessionLocal()
    try:
        card = score_wallet(db, OWNER, "sol")
        assert card["owner"] == OWNER
        assert card["score"] == 0
        assert card["grade"] == "—"
        assert card["hits"] == []
        assert card["gold"] is False
        assert card["source"] == "stored_maps_and_early_swaps"
    finally:
        db.close()


def test_gold_overlap_scores_high() -> None:
    db = SessionLocal()
    try:
        token = Token(mint="MintGold111111111111111111111111111111111", symbol="GOLD", chain="sol", source="poll")
        token.research = Research(raw_json="{}", features_json="{}", risk_flags_json="[]")
        db.add(token)
        token_b = Token(mint="MintGold222222222222222222222222222222222", symbol="SAAR", chain="sol", source="poll")
        token_b.research = Research(raw_json="{}", features_json="{}", risk_flags_json="[]")
        db.add(token_b)
        db.flush()
        early = EarlyWallet(
            chain="sol",
            owner=OWNER,
            n_runners=2,
            n_sized=2,
            n_profitable=2,
            n_still_in=1,
            best_symbol="GOLD",
            best_mark_multiple=12.0,
        )
        db.add(early)
        db.flush()
        db.add(
            EarlyWalletHit(
                wallet_id=early.id,
                token_id=token.id,
                mint=token.mint,
                symbol="GOLD",
                mark_multiple=12.0,
                sol_spent=1.0,
                still_holding=True,
            )
        )
        fomo = FomoWallet(
            chain="sol",
            owner=OWNER,
            n_tokens=2,
            n_wins=2,
            n_rugs=0,
            best_symbol="GOLD",
            best_multiple=12.0,
        )
        db.add(fomo)
        db.flush()
        db.add(
            FomoWalletHit(
                wallet_id=fomo.id,
                token_id=token.id,
                mint=token.mint,
                symbol="GOLD",
                multiple=12.0,
                pct=1.4,
                is_win=True,
            )
        )
        db.add(
            FomoWalletHit(
                wallet_id=fomo.id,
                token_id=token_b.id,
                mint=token_b.mint,
                symbol="SAAR",
                multiple=11.0,
                pct=1.1,
                is_win=True,
            )
        )
        db.commit()
        card = score_wallet(db, OWNER, "sol")
        assert card["gold"] is True
        assert card["n_sized"] == 1
        assert card["n_wins"] == 2
        assert card["score"] >= 50
        assert card["grade"] in {"A", "B"}
        assert card["best_symbol"] == "GOLD"
        listed = list_scored_wallets(db, "sol", limit=20)
        assert listed["gold"] >= 1
        assert listed["items"][0]["owner"] == OWNER
        assert listed["items"][0]["hits"] == []
    finally:
        db.close()


def test_wallets_api_scores_unknown_then_gold() -> None:
    empty = asyncio.run(scored_wallet(OWNER, chain="sol"))
    assert empty["score"] == 0
    assert empty["grade"] == "—"

    db = SessionLocal()
    try:
        token = Token(mint="MintApiGold11111111111111111111111111111", symbol="APIG", chain="sol", source="poll")
        token.research = Research(raw_json="{}", features_json="{}", risk_flags_json="[]")
        db.add(token)
        db.flush()
        early = EarlyWallet(chain="sol", owner=OWNER, n_runners=1, n_sized=1, n_profitable=1, best_symbol="APIG")
        db.add(early)
        db.flush()
        fomo = FomoWallet(chain="sol", owner=OWNER, n_tokens=1, n_wins=1, n_rugs=0, best_symbol="APIG", best_multiple=8.0)
        db.add(fomo)
        db.commit()
    finally:
        db.close()

    card = asyncio.run(scored_wallet(OWNER, chain="sol"))
    assert card["gold"] is True
    assert card["score"] > 0
    listed = asyncio.run(scored_wallets(chain="sol", limit=20))
    assert listed["source"] == "stored_maps_and_early_swaps"
    assert any(row["owner"] == OWNER for row in listed["items"])


def test_historical_zcat_hit_does_not_mint_gold() -> None:
    db = SessionLocal()
    try:
        token = Token(
            mint="HcRLc9VDgjLeK154xDawfb1dmVJ98DoSqcwTHGqiDeJR",
            symbol="ZCAT",
            chain="sol",
            source="backfill",
            is_historical=True,
        )
        token.research = Research(raw_json="{}", features_json="{}", risk_flags_json="[]")
        db.add(token)
        db.flush()
        early = EarlyWallet(
            chain="sol",
            owner=OWNER,
            n_runners=1,
            n_sized=1,
            n_profitable=1,
            best_symbol="ZCAT",
            best_mark_multiple=1400.0,
        )
        db.add(early)
        db.flush()
        db.add(
            EarlyWalletHit(
                wallet_id=early.id,
                token_id=token.id,
                mint=token.mint,
                symbol="ZCAT",
                mark_multiple=1400.0,
                sol_spent=1.2,
            )
        )
        fomo = FomoWallet(chain="sol", owner=OWNER, n_tokens=1, n_wins=1, n_rugs=0, best_symbol="ZCAT", best_multiple=1400.0)
        db.add(fomo)
        db.flush()
        db.add(
            FomoWalletHit(
                wallet_id=fomo.id,
                token_id=token.id,
                mint=token.mint,
                symbol="ZCAT",
                multiple=1400.0,
                pct=2.0,
                is_win=True,
            )
        )
        db.commit()
        card = score_wallet(db, OWNER, "sol")
        assert card["gold"] is False
        assert card["stored_n_sized"] == 1
    finally:
        db.close()


def test_parked_zcat_map_still_lists_without_gold() -> None:
    owner = "ParkedZcat1111111111111111111111111111112"
    db = SessionLocal()
    try:
        token = Token(
            mint="ParkedZcatMint111111111111111111111111111",
            symbol="ZCAT",
            chain="sol",
            source="backfill",
            is_historical=True,
        )
        token.research = Research(raw_json="{}", features_json="{}", risk_flags_json="[]")
        db.add(token)
        db.flush()
        early = EarlyWallet(
            chain="sol",
            owner=owner,
            n_runners=2,
            n_sized=2,
            n_profitable=1,
            best_symbol="ZCAT",
            best_mark_multiple=1240.0,
        )
        db.add(early)
        db.flush()
        db.add(
            EarlyWalletHit(
                wallet_id=early.id,
                token_id=token.id,
                mint=token.mint,
                symbol="ZCAT",
                mark_multiple=1240.0,
                sol_spent=1.2,
            )
        )
        fomo = FomoWallet(
            chain="sol",
            owner=owner,
            n_tokens=1,
            n_wins=1,
            n_rugs=0,
            best_symbol="ZCAT",
            best_multiple=1240.0,
        )
        db.add(fomo)
        db.flush()
        db.add(
            FomoWalletHit(
                wallet_id=fomo.id,
                token_id=token.id,
                mint=token.mint,
                symbol="ZCAT",
                multiple=1240.0,
                pct=2.0,
                is_win=True,
            )
        )
        db.commit()
        card = score_wallet(db, owner, "sol")
        assert card["gold"] is False
        assert card["parked"] is True
        assert card["n_sized"] == 2
        assert card["n_wins"] == 1
        listed = list_scored_wallets(db, "sol", limit=20)
        row = next(item for item in listed["items"] if item["owner"] == owner)
        assert row["gold"] is False
        assert row["live"] is False
        assert row["parked"] is True
        assert row["score"] == 0
        assert row["n_sized"] == 2
        assert row["n_wins"] == 1
        assert row["best_symbol"] == "ZCAT"
        assert row["hits"] == []
        assert listed["parked"] >= 1
    finally:
        db.close()


def test_venue_wallet_stays_off_scored_list() -> None:
    owner = "VenueRouter11111111111111111111111111113"
    db = SessionLocal()
    try:
        db.add(
            FomoWallet(
                chain="sol",
                owner=owner,
                n_tokens=80,
                n_wins=80,
                n_rugs=0,
                best_symbol="USWS",
                best_multiple=12.0,
            )
        )
        db.commit()
        listed = list_scored_wallets(db, "sol", limit=200)
        assert all(item["owner"] != owner for item in listed["items"])
    finally:
        db.close()


def test_rh_sized_map_share_mints_gold() -> None:
    db = SessionLocal()
    try:
        owner = "0x8607473db14c1a45c6aabbccddeeff0011223344"
        fomo = FomoWallet(
            chain="robinhood",
            owner=owner,
            n_tokens=2,
            n_wins=2,
            n_rugs=0,
            best_symbol="ROUTE",
            best_multiple=12.0,
        )
        db.add(fomo)
        db.flush()
        for i, (mint, symbol, pct) in enumerate(
            (
                ("0x4a72b9702f991b790788f8afa9e7112541f4e8f8", "ROUTE", 1.1),
                ("0xf7d77243fbd0413a650000000000000000000001", "O1BOT", 1.6),
            )
        ):
            token = Token(mint=mint, symbol=symbol, chain="robinhood", source="rh_fomo")
            token.research = Research(raw_json="{}", features_json="{}", risk_flags_json="[]")
            db.add(token)
            db.flush()
            db.add(
                FomoWalletHit(
                    wallet_id=fomo.id,
                    token_id=token.id,
                    mint=token.mint,
                    symbol=symbol,
                    multiple=12.0 - i,
                    pct=pct,
                    is_win=True,
                )
            )
        dust = Token(
            mint="0x1111111111111111111111111111111111111111",
            symbol="DUST",
            chain="robinhood",
            source="rh_fomo",
        )
        dust.research = Research(raw_json="{}", features_json="{}", risk_flags_json="[]")
        db.add(dust)
        db.flush()
        once = FomoWallet(
            chain="robinhood",
            owner="0x1111111111111111111111111111111111111111",
            n_tokens=1,
            n_wins=1,
            n_rugs=0,
            best_symbol="DUST",
            best_multiple=8.0,
        )
        db.add(once)
        db.flush()
        db.add(
            FomoWalletHit(
                wallet_id=once.id,
                token_id=dust.id,
                mint=dust.mint,
                symbol="DUST",
                multiple=8.0,
                pct=0.5,
                is_win=True,
            )
        )
        db.commit()
        card = score_wallet(db, owner, "robinhood")
        assert card["gold"] is True
        assert card["n_sized"] == 2
        assert card["score"] > 0
        dust_card = score_wallet(db, "0x1111111111111111111111111111111111111111", "robinhood")
        assert dust_card["gold"] is False
        assert dust_card["n_sized"] == 0
    finally:
        db.close()


def test_feature_names_stay_sixty_six() -> None:
    assert len(FEATURE_NAMES) == 66
