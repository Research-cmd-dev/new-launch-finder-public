from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from launchfinder.research.early_wallets import (
    apply_still_holding,
    extract_rpc_signatures,
    list_early_wallets,
    parse_early_buys,
    record_early_buys,
)
from launchfinder.scoring.features import FEATURE_NAMES


ZCAT = "HcRLc9VDgjLeK154xDawfb1dmVJ98DoSqcwTHGqiDeJR"
POOL = "BTccxxTFi7a9xJTE1exKn38Jgie35s6gNeRxd8DM61Rc"
CREATOR = "5CEbueQnq1Ym2uSSx2xXds3jQAqT1BDnkA59RZobSPAG"
LAUNCH = datetime(2026, 8, 30, 23, 29, 55, tzinfo=timezone.utc)


def test_parse_early_buys_keeps_first_inbound_drops_pool() -> None:
    t0 = int(LAUNCH.timestamp())
    buyer = "EarlyBuyer1111111111111111111111111111111"
    txs = [
        {
            "timestamp": t0 + 40,
            "tokenTransfers": [
                {
                    "mint": ZCAT,
                    "fromUserAccount": POOL,
                    "toUserAccount": buyer,
                    "tokenAmount": 1000,
                }
            ],
            "events": {"swap": {"nativeInput": {"amount": 1_000_000_000}}},
        },
        {
            "timestamp": t0 + 90,
            "tokenTransfers": [
                {
                    "mint": ZCAT,
                    "fromUserAccount": POOL,
                    "toUserAccount": buyer,
                    "tokenAmount": 50,
                }
            ],
        },
        {
            "timestamp": t0 + 20,
            "tokenTransfers": [
                {
                    "mint": ZCAT,
                    "fromUserAccount": buyer,
                    "toUserAccount": POOL,
                    "tokenAmount": 10,
                }
            ],
        },
        {
            "timestamp": t0 + 10,
            "tokenTransfers": [
                {
                    "mint": ZCAT,
                    "fromUserAccount": POOL,
                    "toUserAccount": CREATOR,
                    "tokenAmount": 9_000,
                }
            ],
        },
        {
            "timestamp": t0 + 4000,
            "tokenTransfers": [
                {
                    "mint": ZCAT,
                    "fromUserAccount": POOL,
                    "toUserAccount": "LateBuyer11111111111111111111111111111111",
                    "tokenAmount": 10,
                }
            ],
        },
    ]
    rows = parse_early_buys(
        txs, mint=ZCAT, pool=POOL, creator=CREATOR, launched=LAUNCH, mark_price=0.16
    )
    assert [r["owner"] for r in rows] == [buyer]
    assert rows[0]["age_at_buy_s"] == 40
    assert rows[0]["entry_price"] == 0.001
    assert rows[0]["sol_spent"] == 1.0
    assert rows[0]["token_amount"] == 1000
    assert abs(rows[0]["mark_multiple"] - 160.0) < 0.01


def test_parse_early_buys_reads_top_level_native_input() -> None:
    t0 = int(LAUNCH.timestamp())
    buyer = "EarlyBuyer2222222222222222222222222222222"
    rows = parse_early_buys(
        [
            {
                "timestamp": t0 + 12,
                "nativeInput": {"amount": 2_000_000_000},
                "tokenTransfers": [
                    {
                        "mint": ZCAT,
                        "fromUserAccount": POOL,
                        "toUserAccount": buyer,
                        "tokenAmount": 500,
                    }
                ],
            }
        ],
        mint=ZCAT,
        pool=POOL,
        creator=CREATOR,
        launched=LAUNCH,
        mark_price=0.16,
    )
    assert rows[0]["entry_price"] == 0.004
    assert abs(rows[0]["mark_multiple"] - 40.0) < 0.01


def test_record_early_buys_rolls_up() -> None:
    from launchfinder.db import SessionLocal, init_db
    from launchfinder.models import EarlyWallet, Token

    init_db()
    db = SessionLocal()
    try:
        token = Token(mint=ZCAT, symbol="ZCAT", chain="sol", source="backfill", is_historical=True)
        db.add(token)
        db.flush()
        n = record_early_buys(
            db,
            token,
            [
                {
                    "owner": "EarlyBuyer1111111111111111111111111111111",
                    "first_buy_ts": LAUNCH,
                    "age_at_buy_s": 40,
                    "sol_spent": 1.2,
                    "token_amount": 1000,
                    "entry_price": 0.001,
                    "mark_multiple": 160.0,
                }
            ],
            still_in=set(),
        )
        assert n == 1
        wallet = db.query(EarlyWallet).one()
        assert wallet.n_runners == 1
        assert wallet.n_sized == 1
        assert wallet.n_profitable == 1
        assert wallet.sum_sol_spent == 1.2
        assert wallet.best_symbol == "ZCAT"
        assert wallet.best_mark_multiple == 160.0
        assert wallet.hits[0].still_holding is False
        listed = list_early_wallets(db, "sol")
        assert listed["wallets"] == 1
        assert listed["sized"] == 1
        assert listed["items"][0]["best_symbol"] == "ZCAT"
        assert listed["items"][0]["n_sized"] == 1
        assert listed["source"] == "helius_early_swaps"
    finally:
        db.close()


def test_zcat_seed_is_historical_backfill(monkeypatch) -> None:
    from launchfinder.db import SessionLocal, init_db
    from launchfinder.ingest import zcat_seed
    from launchfinder.models import Token

    init_db()

    async def _market(mint, chain="sol"):
        return {"mcap_usd": 159_000_000, "liquidity_usd": 2_000_000, "price_usd": 0.16, "price_native": 0.0008}

    async def _ingest(session, **kwargs):
        token = Token(
            mint=kwargs["mint"],
            symbol="ZCAT",
            chain="sol",
            source=kwargs["source"],
            is_historical=kwargs["historical"],
        )
        session.add(token)
        session.flush()
        return token

    monkeypatch.setattr(zcat_seed, "token_market", _market)
    monkeypatch.setattr(zcat_seed, "ingest_and_research", _ingest)
    assert asyncio.run(zcat_seed.ensure_zcat_reference()) is True
    db = SessionLocal()
    try:
        row = db.query(Token).filter(Token.mint == ZCAT).one()
        assert row.is_historical is True
        assert row.source == "backfill"
        assert row.symbol == "ZCAT"
    finally:
        db.close()


def test_park_zcat_keeps_historical() -> None:
    from launchfinder.db import SessionLocal, init_db
    from launchfinder.ingest.zcat_seed import park_zcat_reference
    from launchfinder.models import Outcome, Token

    init_db()
    db = SessionLocal()
    try:
        token = Token(mint=ZCAT, symbol="ZCAT", chain="sol", source="poll", is_historical=False)
        db.add(token)
        db.flush()
        db.add(Outcome(token_id=token.id, t0_mcap=69_000.0, max_mcap=180_000_000.0, multiple=2608.0, label=1))
        db.flush()
        assert park_zcat_reference(db) == 1
        row = db.query(Token).filter(Token.mint == ZCAT).one()
        assert row.is_historical is True
        assert row.source == "backfill"
        assert row.outcome.t0_mcap == 0.0
        assert row.outcome.multiple == 0.0
        assert row.outcome.used_for_train is True
        assert park_zcat_reference(db) == 0
    finally:
        db.close()


def test_list_early_wallets_prefers_sized_entries() -> None:
    from launchfinder.db import SessionLocal, init_db
    from launchfinder.models import Token

    init_db()
    db = SessionLocal()
    try:
        token = Token(mint=ZCAT, symbol="ZCAT", chain="sol", source="backfill", is_historical=True)
        db.add(token)
        db.flush()
        record_early_buys(
            db,
            token,
            [
                {
                    "owner": "DustBuyer11111111111111111111111111111111",
                    "first_buy_ts": LAUNCH,
                    "age_at_buy_s": 5,
                    "sol_spent": 0.001,
                    "token_amount": 9_000,
                    "entry_price": 1e-8,
                    "mark_multiple": 2000.0,
                },
                {
                    "owner": "SizedBuyer1111111111111111111111111111111",
                    "first_buy_ts": LAUNCH,
                    "age_at_buy_s": 40,
                    "sol_spent": 1.5,
                    "token_amount": 800,
                    "entry_price": 0.002,
                    "mark_multiple": 80.0,
                },
            ],
        )
        listed = list_early_wallets(db, "sol")
        assert listed["sized"] == 1
        assert listed["sized_all"] == 1
        assert listed["sized_live"] == 0
        assert [w["owner"] for w in listed["items"]] == ["SizedBuyer1111111111111111111111111111111"]
    finally:
        db.close()


def test_apply_still_holding_marks_owners() -> None:
    from launchfinder.db import SessionLocal, init_db
    from launchfinder.models import EarlyWallet, Token

    init_db()
    db = SessionLocal()
    try:
        token = Token(mint=ZCAT, symbol="ZCAT", chain="sol", source="backfill", is_historical=True)
        db.add(token)
        db.flush()
        owner = "HoldBuyer11111111111111111111111111111111"
        record_early_buys(
            db,
            token,
            [{"owner": owner, "age_at_buy_s": 20, "sol_spent": 0.5, "mark_multiple": 12.0}],
        )
        assert db.query(EarlyWallet).one().n_still_in == 0
        assert apply_still_holding(db, token, {owner}) == 1
        wallet = db.query(EarlyWallet).one()
        assert wallet.n_still_in == 1
        assert wallet.hits[0].still_holding is True
    finally:
        db.close()


def test_extract_rpc_signatures_reads_first_hour_page() -> None:
    sigs, token = extract_rpc_signatures(
        {
            "result": {
                "data": [
                    {"signature": "SigOne111", "blockTime": int(LAUNCH.timestamp())},
                    {"signature": "SigTwo222", "blockTime": int(LAUNCH.timestamp()) + 10},
                ],
                "paginationToken": "slot:2",
            }
        }
    )
    assert sigs == ["SigOne111", "SigTwo222"]
    assert token == "slot:2"
    assert extract_rpc_signatures({"error": {"code": -32601}}) == ([], "")


def test_tiny_t0_repair_skips_zcat_backfill() -> None:
    from launchfinder.db import SessionLocal, init_db
    from launchfinder.models import Outcome, Token
    from launchfinder.scoring.outcomes import repair_sol_tiny_t0

    init_db()
    db = SessionLocal()
    try:
        token = Token(mint=ZCAT, symbol="ZCAT", chain="sol", source="backfill", is_historical=True)
        db.add(token)
        db.flush()
        db.add(Outcome(token_id=token.id, t0_mcap=410.0, max_mcap=180_000_000.0, multiple=4.0))
        db.flush()
        assert repair_sol_tiny_t0(db) == 0
        assert db.query(Outcome).one().t0_mcap == 410.0
    finally:
        db.close()


def test_harvest_skips_historical_and_prefers_this_window() -> None:
    from datetime import timedelta

    from launchfinder.db import SessionLocal, init_db
    from launchfinder.models import Outcome, Research, Token
    from launchfinder.research.early_wallets import harvest_candidate_tokens, record_early_buys

    init_db()
    db = SessionLocal()
    now = datetime.now(timezone.utc)
    try:
        zcat = Token(
            mint=ZCAT,
            symbol="ZCAT",
            chain="sol",
            source="backfill",
            is_historical=True,
            pool_address=POOL,
            migrated_at=LAUNCH,
            first_seen_at=LAUNCH,
        )
        zcat.research = Research(raw_json="{}", features_json="{}", risk_flags_json="[]", holder_count=80)
        db.add(zcat)
        db.flush()
        db.add(
            Outcome(
                token_id=zcat.id,
                t0_mcap=69_000,
                max_mcap=80_000_000,
                last_mcap=80_000_000,
                last_liq=200_000,
                multiple=40.0,
                label=1,
            )
        )
        live = Token(
            mint="ThisWindowMint111111111111111111111111111",
            symbol="CAC",
            chain="sol",
            source="poll",
            is_historical=False,
            pool_address="ThisWindowPool11111111111111111111111111",
            migrated_at=now - timedelta(hours=2),
            first_seen_at=now - timedelta(hours=2),
        )
        live.research = Research(raw_json="{}", features_json="{}", risk_flags_json="[]", holder_count=90)
        db.add(live)
        db.flush()
        db.add(
            Outcome(
                token_id=live.id,
                t0_mcap=69_000,
                max_mcap=2_000_000,
                last_mcap=2_000_000,
                last_liq=80_000,
                multiple=28.0,
                label=1,
            )
        )
        db.flush()
        picked = harvest_candidate_tokens(db, limit=2)
        assert [t.symbol for t in picked] == ["CAC"]
        record_early_buys(
            db,
            live,
            [{"owner": "SizedBuyer1111111111111111111111111111111", "sol_spent": 0.5, "mark_multiple": 8.0}],
        )
        assert harvest_candidate_tokens(db, limit=2) == []
    finally:
        db.close()


def test_list_early_ranks_this_window_ahead_of_zcat() -> None:
    from launchfinder.db import SessionLocal, init_db
    from launchfinder.models import Token

    init_db()
    db = SessionLocal()
    try:
        zcat = Token(mint=ZCAT, symbol="ZCAT", chain="sol", source="backfill", is_historical=True)
        db.add(zcat)
        db.flush()
        live = Token(
            mint="ThisWindowMint222222222222222222222222222",
            symbol="PUGCOIN",
            chain="sol",
            source="poll",
            is_historical=False,
        )
        db.add(live)
        db.flush()
        record_early_buys(
            db,
            zcat,
            [
                {
                    "owner": "ZcatOnly111111111111111111111111111111111",
                    "sol_spent": 2.0,
                    "mark_multiple": 900.0,
                }
            ],
        )
        record_early_buys(
            db,
            live,
            [
                {
                    "owner": "LiveEarly11111111111111111111111111111111",
                    "sol_spent": 0.4,
                    "mark_multiple": 14.0,
                }
            ],
        )
        listed = list_early_wallets(db, "sol")
        assert listed["items"][0]["owner"] == "LiveEarly11111111111111111111111111111111"
        assert listed["items"][0]["best_symbol"] == "PUGCOIN"
        assert listed["items"][0]["live"] is True
        assert listed["sized_live"] == 1
        assert listed["sized_all"] == 2
    finally:
        db.close()


def test_harvest_prefers_confirmed_runner_over_newest_unconfirmed() -> None:
    from datetime import timedelta

    from launchfinder.db import SessionLocal, init_db
    from launchfinder.models import Outcome, Research, Snapshot, Token
    from launchfinder.research.early_wallets import harvest_candidate_tokens

    init_db()
    db = SessionLocal()
    now = datetime.now(timezone.utc)
    try:
        confirmed = Token(
            mint="ConfirmedMint111111111111111111111111111",
            symbol="CAC",
            chain="sol",
            source="poll",
            is_historical=False,
            pool_address="ConfirmedPool11111111111111111111111111",
            migrated_at=now - timedelta(hours=6),
            first_seen_at=now - timedelta(hours=6),
        )
        confirmed.research = Research(
            raw_json='{"holders":{"top_wallets":[{"owner":"A","pct":1.2}]}}',
            features_json="{}",
            risk_flags_json="[]",
            holder_count=120,
        )
        db.add(confirmed)
        db.flush()
        db.add(
            Outcome(
                token_id=confirmed.id,
                t0_mcap=69_000,
                max_mcap=69_000 * 12,
                last_mcap=69_000 * 12,
                last_liq=40_000,
                multiple=12.0,
                label=1,
            )
        )
        db.add(Snapshot(token_id=confirmed.id, kind="t0", mcap_usd=69_000, liquidity_usd=20_000))
        db.add(
            Snapshot(
                token_id=confirmed.id,
                kind="t6h",
                mcap_usd=69_000 * 12,
                liquidity_usd=40_000,
            )
        )
        newest = Token(
            mint="NewestMint111111111111111111111111111111",
            symbol="NEWX",
            chain="sol",
            source="poll",
            is_historical=False,
            pool_address="NewestPool1111111111111111111111111111",
            migrated_at=now - timedelta(minutes=20),
            first_seen_at=now - timedelta(minutes=20),
        )
        newest.research = Research(raw_json="{}", features_json="{}", risk_flags_json="[]", holder_count=90)
        db.add(newest)
        db.flush()
        db.add(
            Outcome(
                token_id=newest.id,
                t0_mcap=69_000,
                max_mcap=69_000 * 28,
                last_mcap=69_000 * 28,
                last_liq=20_000,
                multiple=28.0,
                label=1,
            )
        )
        db.flush()
        picked = harvest_candidate_tokens(db, limit=1)
        assert [t.symbol for t in picked] == ["CAC"]
    finally:
        db.close()


def test_harvest_skips_recent_empty_attempt() -> None:
    from datetime import timedelta

    from launchfinder.db import SessionLocal, init_db
    from launchfinder.models import Outcome, Research, Token
    from launchfinder.research.early_wallets import harvest_candidate_tokens, stamp_harvest_attempt

    init_db()
    db = SessionLocal()
    now = datetime.now(timezone.utc)
    try:
        live = Token(
            mint="AttemptMint11111111111111111111111111111",
            symbol="SAAR",
            chain="sol",
            source="poll",
            is_historical=False,
            pool_address="AttemptPool111111111111111111111111111",
            migrated_at=now - timedelta(hours=2),
            first_seen_at=now - timedelta(hours=2),
        )
        live.research = Research(raw_json="{}", features_json="{}", risk_flags_json="[]", holder_count=90)
        db.add(live)
        db.flush()
        db.add(
            Outcome(
                token_id=live.id,
                t0_mcap=69_000,
                max_mcap=2_000_000,
                last_mcap=2_000_000,
                last_liq=80_000,
                multiple=15.0,
                label=1,
            )
        )
        db.flush()
        assert [t.symbol for t in harvest_candidate_tokens(db, limit=1)] == ["SAAR"]
        stamp_harvest_attempt(db, live.id)
        assert harvest_candidate_tokens(db, limit=1) == []
    finally:
        db.close()


def test_feature_names_stay_sixty_six() -> None:
    assert len(FEATURE_NAMES) == 66
