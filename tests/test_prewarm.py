from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from launchfinder.db import SessionLocal, init_db
from launchfinder.models import ScanState, Token
from launchfinder.research.prewarm import (
    PREWARM_MAX_PROGRESS,
    annotate_watch,
    is_prewarm_candidate,
    load_prewarm,
    mark_prewarm_used,
    merge_prewarm_into_coin,
    prewarm_key,
    save_prewarm,
)
from launchfinder.scoring.features import FEATURE_NAMES


NOW = datetime(2026, 9, 8, 14, 30, tzinfo=timezone.utc)


def test_stuck_door_and_low_progress_are_not_prewarmed() -> None:
    assert PREWARM_MAX_PROGRESS == 0.90
    fresh = (NOW - timedelta(minutes=20)).isoformat()
    assert is_prewarm_candidate(
        {"mint": "MintWarm111111111111111111111111111111111", "progress": 0.84, "first_seen": fresh},
        "sol",
    )
    assert is_prewarm_candidate(
        {"mint": "MintWarm111111111111111111111111111111111", "progress": 0.80, "first_seen": fresh},
        "sol",
    )
    assert not is_prewarm_candidate(
        {"mint": "MintDoor111111111111111111111111111111111", "progress": 0.90, "first_seen": fresh},
        "sol",
    )
    assert not is_prewarm_candidate(
        {"mint": "MintLow1111111111111111111111111111111111", "progress": 0.79, "first_seen": fresh},
        "sol",
    )
    old = (NOW - timedelta(hours=20)).isoformat()
    # Frozen leftover: 8h+ and still under 0.40. Do not spend research.
    assert not is_prewarm_candidate(
        {"mint": "MintFrozen1111111111111111111111111111111", "progress": 0.30, "first_seen": old},
        "sol",
    )


def test_frozen_low_progress_sol_is_excluded() -> None:
    old = (NOW - timedelta(hours=20)).isoformat()
    assert not is_prewarm_candidate(
        {"mint": "MintIce1111111111111111111111111111111111", "progress": 0.30, "first_seen": old},
        "sol",
    )


def test_prewarm_roundtrip_does_not_insert_token() -> None:
    init_db()
    db = SessionLocal()
    try:
        mint = "MintPw11111111111111111111111111111111111"
        save_prewarm(
            db,
            {
                "chain": "sol",
                "mint": mint,
                "symbol": "WARM",
                "progress": 0.84,
                "coin": {"symbol": "WARM", "twitter": "https://x.com/warmdev", "creator": "Creator111"},
                "twitter": {"followers": 1200, "verified": False},
                "github": {"stars": 4, "url": "https://github.com/warm/repo"},
            },
        )
        db.commit()
        assert db.query(Token).count() == 0
        loaded = load_prewarm(db, mint, "sol")
        assert loaded is not None
        assert loaded["symbol"] == "WARM"
        assert loaded["twitter"]["followers"] == 1200
        merged = merge_prewarm_into_coin({"symbol": "WARM"}, loaded)
        assert merged["twitter"] == "https://x.com/warmdev"
        assert merged["prewarm_twitter"]["followers"] == 1200
        assert merged["prewarm_github"]["stars"] == 4
        live = merge_prewarm_into_coin({"twitter": "https://x.com/live"}, loaded)
        assert live["twitter"] == "https://x.com/live"
        watch = annotate_watch(
            db,
            [{"mint": mint, "symbol": "WARM", "progress": 0.84}],
            "sol",
        )
        assert watch[0]["prewarmed"] is True
        mark_prewarm_used(db, mint, "sol")
        db.commit()
        assert load_prewarm(db, mint, "sol") is None
        assert db.query(Token).count() == 0
    finally:
        db.close()


def test_prewarm_key_fits_scanstate() -> None:
    rh = prewarm_key("robinhood", "0xd270d4e1ec6e6e0d28c0ecb8be966ec75997ffff")
    sol = prewarm_key("sol", "GVZRhKRFE6ZNFrjBGm1111111111111111111111")
    assert len(rh) <= 64
    assert len(sol) <= 64
    assert rh.startswith("pw:rh:")
    assert sol.startswith("pw:sol:")


def test_prewarm_watch_cycle_skips_existing_token(monkeypatch) -> None:
    from launchfinder.ingest import pump_poll
    from launchfinder.research import prewarm as pw

    init_db()
    db = SessionLocal()
    try:
        mint = "MintLive111111111111111111111111111111111"
        db.add(Token(mint=mint, chain="sol", symbol="LIVE", source="poll"))
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(
        pump_poll,
        "watch_preview",
        lambda: [{"mint": mint, "symbol": "LIVE", "progress": 0.85, "first_seen": NOW.isoformat()}],
    )

    async def boom(*_a, **_k):
        raise AssertionError("must not fetch when the mint is already a hunt token")

    monkeypatch.setattr(pw, "build_prewarm_payload", boom)
    n = asyncio.run(pw.prewarm_watch_cycle("sol", limit=2))
    assert n == 0


def test_rh_zero_mcap_climb_is_a_candidate() -> None:
    fresh = (NOW - timedelta(minutes=12)).isoformat()
    climb = {
        "mint": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "symbol": "CLIMB",
        "progress": 0.84,
        "mcap_usd": 0.0,
        "first_seen": fresh,
        "launchpad": "pons",
    }
    assert is_prewarm_candidate(climb, "robinhood")
    assert not is_prewarm_candidate({**climb, "progress": 0.90}, "robinhood")


def test_rh_prewarm_source_keeps_zero_mcap_pons_off_hunt_strip() -> None:
    from launchfinder.ingest import pons_poll, rh_poll

    climb_mint = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    lock_mint = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    rh_poll._watch_preview = [
        {
            "mint": lock_mint,
            "symbol": "lock",
            "progress": 0.16,
            "mcap_usd": 4449.0,
            "first_seen": NOW.isoformat(),
        }
    ]
    pons_poll._climbing = [
        {
            "mint": climb_mint,
            "symbol": "CLIMB",
            "progress": 0.84,
            "mcap_usd": 0.0,
            "first_seen": NOW.isoformat(),
            "launchpad": "pons",
        }
    ]
    try:
        source = rh_poll.prewarm_source_rows()
        assert any(r.get("symbol") == "CLIMB" for r in source)
        desk = rh_poll.watch_preview()
        assert all(r.get("symbol") != "CLIMB" for r in desk)
        assert is_prewarm_candidate(next(r for r in source if r.get("symbol") == "CLIMB"), "robinhood")
    finally:
        rh_poll._watch_preview = []
        pons_poll._climbing = []


def test_rh_prewarm_cycle_writes_scanstate_not_token(monkeypatch) -> None:
    from launchfinder.ingest import pons_poll, rh_poll
    from launchfinder.research import prewarm as pw

    init_db()
    mint = "0xcccccccccccccccccccccccccccccccccccccccc"
    row = {
        "mint": mint,
        "symbol": "RHWARM",
        "progress": 0.83,
        "mcap_usd": 0.0,
        "twitter": "https://x.com/rhwarm",
        "first_seen": NOW.isoformat(),
        "launchpad": "pons",
    }
    async def _no_refresh(**_k):
        return 0

    monkeypatch.setattr(pons_poll, "refresh_climbing_progress", _no_refresh)
    monkeypatch.setattr(rh_poll, "prewarm_source_rows", lambda: [row])

    async def fake_payload(src, chain):
        assert chain == "robinhood"
        return {
            "chain": "robinhood",
            "mint": mint,
            "symbol": "RHWARM",
            "progress": 0.83,
            "coin": {"symbol": "RHWARM", "twitter": "https://x.com/rhwarm", "chain": "robinhood"},
            "twitter": {"followers": 80},
            "github": {},
        }

    monkeypatch.setattr(pw, "build_prewarm_payload", fake_payload)
    n = asyncio.run(pw.prewarm_watch_cycle("robinhood", limit=2))
    assert n == 1
    db = SessionLocal()
    try:
        assert db.query(Token).count() == 0
        loaded = load_prewarm(db, mint, "robinhood")
        assert loaded is not None
        assert loaded["symbol"] == "RHWARM"
        assert loaded["chain"] == "robinhood"
    finally:
        db.close()


def test_feature_names_stay_sixty_six() -> None:
    assert len(FEATURE_NAMES) == 66
    assert PREWARM_MAX_PROGRESS == 0.90
