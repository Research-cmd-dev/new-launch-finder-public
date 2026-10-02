"""RH Hunt tape Dex hydrate — pool-id fallback and mint case."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from launchfinder.db import init_db, session_scope
from launchfinder.models import HuntCard, Outcome, Research, Token, utcnow
from launchfinder.research import dexscreener
from launchfinder.research.rh_hydrate_alarm import hunt_mcap_hydrate_gaps, rh_hunt_hydrate_gaps
from launchfinder.app import _hunt_board_sync
from launchfinder.scoring.hunt import (
    historical_hydrate_tape_mints,
    list_hunt_mints,
    sync_fat_hunt_board_cards,
    this_window_hunt_tape_mints,
    upsert_hunt,
)
from launchfinder.scoring.hunt_tape import apply_hunt_tape_market, hunt_tape_may_hydrate_historical

VRAX = "0x94641b97010608c3827fb058074889f19868ff33"
VRAX_PAIR = "0x338fce553e60c8b0acccde1956bc378586ef6f2935339ebae5804d429d461e44"
VRAX_COPYCAT = "0xf2bf060000000000000000000000000000000001"


def test_enrich_markets_uses_v4_pool_when_token_lookup_empty(monkeypatch):
    token = Token(mint=VRAX, chain="robinhood", pool_address=VRAX_PAIR)

    async def fake_pair(pool_id, chain="sol"):
        assert pool_id == VRAX_PAIR
        return {
            "mint": VRAX,
            "mcap_usd": 2_500_000.0,
            "liquidity_usd": 140_000.0,
            "volume_h1": 1000.0,
            "pair_address": VRAX_PAIR,
        }

    monkeypatch.setattr(dexscreener, "pair_market", fake_pair)
    markets = {}
    out = asyncio.run(dexscreener.enrich_markets_with_pool_fallback(markets, [token], "robinhood"))
    assert out[VRAX]["mcap_usd"] == 2_500_000.0


def test_upsert_hunt_updates_uppercase_card_mint():
    """VRAX-class: HuntCard 0x…ABC vs Token lowercase — one row, not a duplicate."""
    init_db()
    with session_scope() as session:
        token = Token(
            mint=VRAX,
            symbol="VRAX",
            chain="robinhood",
            source="rh_bitquery",
            first_seen_at=utcnow(),
            migrated_at=utcnow(),
        )
        token.research = Research(p_good=0.5, features_json="{}")
        token.outcome = Outcome(t0_mcap=69_000.0, last_mcap=2_500_000.0, last_liq=140_000.0, multiple=36.0)
        session.add(token)
        session.flush()
        session.add(
            HuntCard(
                chain="robinhood",
                mint=VRAX.upper(),
                token_id=token.id,
                first_seen_at=utcnow(),
                t0_mcap=69_000.0,
                last_mcap=0.0,
            )
        )
        session.flush()
        upsert_hunt(session, token, touch_updated=False)
        card = session.query(HuntCard).filter(HuntCard.chain == "robinhood").one()
        assert card.mint == VRAX
        assert float(card.last_mcap or 0) > 1_000_000


def test_apply_hunt_tape_writes_last_mcap_from_pair_market():
    init_db()
    market = {
        "mint": VRAX,
        "mcap_usd": 2_534_788.0,
        "liquidity_usd": 144_812.0,
        "volume_h1": 500.0,
        "symbol": "VRAX",
        "name": "Commander Vrax",
    }
    with session_scope() as session:
        token = Token(
            mint=VRAX,
            symbol="VRAX",
            name="Commander Vrax",
            chain="robinhood",
            source="rh_bitquery",
            pool_address=VRAX_PAIR,
            first_seen_at=utcnow(),
            migrated_at=utcnow(),
        )
        token.research = Research(p_good=0.5, features_json="{}")
        token.outcome = Outcome(t0_mcap=69_000.0, last_mcap=0.0, last_liq=0.0)
        session.add(token)
        session.flush()
        session.add(
            HuntCard(
                chain="robinhood",
                mint=VRAX.upper(),
                token_id=token.id,
                first_seen_at=utcnow(),
                t0_mcap=69_000.0,
                last_mcap=0.0,
            )
        )
        session.flush()
        ok = apply_hunt_tape_market(session, token, market, now=datetime.now(timezone.utc))
        assert ok is True
        assert float(token.outcome.last_mcap or 0) > 1_000_000
        assert float(token.outcome.last_liq or 0) > 0


def test_rh_hydrate_alarm_flags_zero_last_with_fomo_mcap():
    init_db()
    with session_scope() as session:
        token = Token(
            mint=VRAX,
            symbol="VRAX",
            chain="robinhood",
            source="rh_bitquery",
            first_seen_at=utcnow(),
            migrated_at=utcnow(),
        )
        token.research = Research(p_good=0.5, features_json="{}")
        token.outcome = Outcome(t0_mcap=50_000.0, last_mcap=0.0, last_liq=0.0)
        session.add(token)
        session.flush()
        session.add(
            HuntCard(
                chain="robinhood",
                mint=VRAX,
                token_id=token.id,
                first_seen_at=utcnow(),
                launched_at=utcnow(),
            )
        )
        session.flush()
        alarms = rh_hunt_hydrate_gaps(
            session,
            [{"chain": "robinhood", "mint": VRAX, "mcap_usd": 2_500_000}],
        )
        assert any(a["mint"] == VRAX and a["reason"] == "on_hunt_last_zero_fomo_board" for a in alarms)


def test_historical_hydrate_ranks_vrax_above_rh_dust_and_copycat():
    """Canonical VRAX (V4 pool) must win the cap over ~4.6k dust and copycat ticker."""
    init_db()
    dust_mcap = 4_600.0
    with session_scope() as session:
        for i in range(30):
            mint = f"0xdust{i:04x}{'0' * 32}"
            token = Token(
                mint=mint,
                symbol="DUST",
                chain="robinhood",
                source="rh_bitquery",
                is_historical=True,
                first_seen_at=utcnow(),
                migrated_at=utcnow(),
            )
            token.research = Research(p_good=0.1, features_json="{}", holder_count=3)
            token.outcome = Outcome(t0_mcap=5_000.0, last_mcap=0.0, last_liq=dust_mcap)
            session.add(token)
        copycat = Token(
            mint=VRAX_COPYCAT,
            symbol="VRAX",
            chain="robinhood",
            source="rh_bitquery",
            is_historical=True,
            first_seen_at=utcnow(),
            migrated_at=utcnow(),
        )
        copycat.research = Research(p_good=0.2, features_json="{}", holder_count=80)
        copycat.outcome = Outcome(t0_mcap=48_000.0, last_mcap=0.0, last_liq=48_000.0)
        session.add(copycat)
        vrax = Token(
            mint=VRAX,
            symbol="VRAX",
            chain="robinhood",
            source="rh_bitquery",
            is_historical=True,
            pool_address=VRAX_PAIR,
            first_seen_at=utcnow(),
            migrated_at=utcnow(),
        )
        vrax.research = Research(p_good=0.5, features_json="{}", holder_count=120)
        vrax.outcome = Outcome(t0_mcap=69_000.0, last_mcap=0.0, last_liq=140_000.0)
        session.add(vrax)
        session.flush()
        tape_mints = historical_hydrate_tape_mints(session, "robinhood", limit=20)
        assert tape_mints[0] == VRAX
        assert VRAX in tape_mints
        assert VRAX_COPYCAT not in tape_mints[:5]
        window = this_window_hunt_tape_mints(session)["robinhood"]
        assert VRAX in window


def test_hunt_tape_may_hydrate_blocks_rh_dust_without_v4_book():
    init_db()
    with session_scope() as session:
        token = Token(
            mint="0xdustonly00000000000000000000000000000001",
            symbol="DUST",
            chain="robinhood",
            source="rh_bitquery",
            is_historical=True,
            first_seen_at=utcnow(),
            migrated_at=utcnow(),
        )
        token.research = Research(p_good=0.1, features_json="{}", holder_count=5)
        token.outcome = Outcome(t0_mcap=5_000.0, last_mcap=0.0, last_liq=4_600.0)
        session.add(token)
        session.flush()
        market = {"mcap_usd": 4_600.0, "liquidity_usd": 4_600.0, "volume_h1": 100.0}
        assert (
            hunt_tape_may_hydrate_historical(
                token,
                token.outcome,
                market,
                mcap=4_600.0,
                liq=4_600.0,
            )
            is False
        )


def test_historical_rh_zero_last_in_tape_set_and_apply_unparks():
    init_db()
    market = {
        "mint": VRAX,
        "mcap_usd": 3_100_000.0,
        "liquidity_usd": 180_000.0,
        "volume_h1": 800.0,
        "symbol": "VRAX",
        "name": "Commander Vrax",
        "pair_address": VRAX_PAIR,
    }
    with session_scope() as session:
        token = Token(
            mint=VRAX,
            symbol="",
            chain="robinhood",
            source="rh_bitquery",
            is_historical=True,
            pool_address=VRAX_PAIR,
            first_seen_at=utcnow(),
            migrated_at=utcnow(),
        )
        token.research = Research(p_good=0.5, features_json="{}")
        token.outcome = Outcome(t0_mcap=69_000.0, last_mcap=0.0, last_liq=0.0)
        session.add(token)
        session.flush()
        tape_mints = historical_hydrate_tape_mints(session, "robinhood")
        assert VRAX in tape_mints
        window = this_window_hunt_tape_mints(session)["robinhood"]
        assert VRAX in window
        ok = apply_hunt_tape_market(session, token, market, now=datetime.now(timezone.utc))
        assert ok
        assert token.is_historical is False
        assert float(token.outcome.last_mcap or 0) > 2_000_000
        card = upsert_hunt(session, token, touch_updated=False)
        assert card is not None
        assert float(card.last_mcap or 0) > 2_000_000


def test_historical_vrax_class_hydrates_from_pool_market():
    """Live VRAX: is_historical + rh_bitquery + zero last still takes Dex/pool tape."""
    init_db()
    market = {
        "mint": VRAX,
        "mcap_usd": 3_100_000.0,
        "liquidity_usd": 180_000.0,
        "volume_h1": 800.0,
        "symbol": "VRAX",
        "name": "Commander Vrax",
        "pair_address": VRAX_PAIR,
    }
    with session_scope() as session:
        token = Token(
            mint=VRAX,
            symbol="",
            name="",
            chain="robinhood",
            source="rh_bitquery",
            is_historical=True,
            pool_address=VRAX_PAIR,
            first_seen_at=utcnow(),
            migrated_at=utcnow(),
        )
        token.research = Research(p_good=0.5, features_json="{}")
        token.outcome = Outcome(t0_mcap=69_000.0, last_mcap=0.0, last_liq=0.0)
        session.add(token)
        session.flush()
        session.add(
            HuntCard(
                chain="robinhood",
                mint=VRAX,
                token_id=token.id,
                first_seen_at=utcnow(),
                t0_mcap=69_000.0,
                last_mcap=0.0,
            )
        )
        session.flush()
        ok = apply_hunt_tape_market(session, token, market, now=datetime.now(timezone.utc))
        assert ok is True
        assert token.is_historical is False
        assert float(token.outcome.last_mcap or 0) > 2_000_000
        card = session.query(HuntCard).filter(HuntCard.token_id == token.id).one()
        assert float(card.last_mcap or 0) > 2_000_000


def test_historical_resi_unparks_when_stored_last_below_dex_liq_first():
    """Live RESI ~595k stored vs ~920k Dex — must hydrate, not stay historical."""
    init_db()
    resi = "resideK2Ejv9apu1op2VBv89poumxCDCs8nzBxQ5q1y"
    market = {
        "mint": resi,
        "mcap_usd": 920_000.0,
        "liquidity_usd": 95_000.0,
        "volume_h1": 200_000.0,
        "symbol": "RESI",
        "dex_id": "raydium",
    }
    with session_scope() as session:
        token = Token(
            mint=resi,
            symbol="RESI",
            chain="sol",
            source="websocket",
            is_historical=True,
            first_seen_at=utcnow(),
            migrated_at=utcnow(),
        )
        token.research = Research(p_good=0.5, features_json="{}")
        token.outcome = Outcome(t0_mcap=69_000.0, last_mcap=595_000.0, last_liq=50_000.0, multiple=8.6)
        session.add(token)
        session.flush()
        ok = apply_hunt_tape_market(session, token, market, now=datetime.now(timezone.utc))
        assert ok is True
        assert token.is_historical is False
        assert 850_000 < float(token.outcome.last_mcap or 0) < 980_000


def test_commander_guarantee_survives_unordered_commander_band_crowd():
    """>240 commander-band rows: VRAX must still pin via holders≥1500 SQL (not limit lottery)."""
    init_db()
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        for i in range(260):
            mint = f"0xcrowd{i:04x}{'0' * 32}"
            token = Token(
                mint=mint,
                symbol=f"X{i}",
                chain="robinhood",
                source="rh_bitquery",
                first_seen_at=now - timedelta(hours=3),
                migrated_at=now - timedelta(hours=3),
            )
            token.research = Research(p_good=0.2, features_json="{}", holder_count=80)
            token.outcome = Outcome(
                t0_mcap=300_000.0,
                last_mcap=800_000.0 + i,
                last_liq=25_000.0,
                multiple=2.5,
            )
            session.add(token)
        vrax = Token(
            mint=VRAX,
            symbol="VRAX",
            chain="robinhood",
            source="rh_bitquery",
            pool_address=None,
            first_seen_at=now - timedelta(hours=5),
            migrated_at=now - timedelta(hours=5),
        )
        vrax.research = Research(p_good=0.35, features_json="{}", holder_count=4788)
        vrax.outcome = Outcome(
            t0_mcap=3_200_000.0,
            last_mcap=3_500_000.0,
            last_liq=180_000.0,
            multiple=1.09,
        )
        session.add(vrax)
        session.flush()
        synced = sync_fat_hunt_board_cards(session, "robinhood", limit=32)
        assert VRAX in synced
        assert VRAX in list_hunt_mints(session, "robinhood", limit=200)
    board = _hunt_board_sync("robinhood", 200, 0.0)
    assert any((i.get("mint") or "").lower() == VRAX.lower() for i in board["items"])


def test_list_hunt_mints_syncs_live_vrax_shape_without_pool_address():
    """Production VRAX: pool_address null, ~3.4M, crowded by mega + commander fat — still on RH Hunt."""
    init_db()
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        for i in range(80):
            mint = f"0xmega{i:04x}{'0' * 32}"
            token = Token(
                mint=mint,
                symbol=f"M{i}",
                chain="robinhood",
                source="rh_bitquery",
                first_seen_at=now - timedelta(hours=2),
                migrated_at=now - timedelta(hours=2),
            )
            token.research = Research(p_good=0.5, features_json="{}", holder_count=50)
            token.outcome = Outcome(
                t0_mcap=5_000_000.0,
                last_mcap=20_000_000.0 + i * 500_000.0,
                last_liq=80_000.0,
                multiple=4.0,
            )
            session.add(token)
            session.flush()
            upsert_hunt(session, token, now=now)
        for i in range(40):
            mint = f"0xmid{i:04x}{'0' * 32}"
            token = Token(
                mint=mint,
                symbol=f"C{i}",
                chain="robinhood",
                source="rh_bitquery",
                first_seen_at=now - timedelta(hours=2),
                migrated_at=now - timedelta(hours=2),
            )
            token.research = Research(p_good=0.5, features_json="{}", holder_count=120)
            token.outcome = Outcome(
                t0_mcap=1_000_000.0,
                last_mcap=5_000_000.0,
                last_liq=60_000.0,
                multiple=5.0,
            )
            session.add(token)
            session.flush()
            upsert_hunt(session, token, now=now)
        vrax = Token(
            mint=VRAX,
            symbol="VRAX",
            chain="robinhood",
            source="rh_bitquery",
            pool_address=None,
            first_seen_at=now - timedelta(hours=8),
            migrated_at=now - timedelta(hours=8),
        )
        vrax.research = Research(p_good=0.35, features_json="{}", holder_count=4814)
        vrax.outcome = Outcome(
            t0_mcap=3_200_000.0,
            last_mcap=3_400_000.0,
            last_liq=189_000.0,
            multiple=1.06,
        )
        session.add(vrax)
        session.flush()
    with session_scope() as session:
        assert VRAX in list_hunt_mints(session, "robinhood", limit=200)
    board = _hunt_board_sync("robinhood", 200, 0.0)
    hits = [i for i in board["items"] if (i.get("mint") or "").lower() == VRAX.lower()]
    assert hits and float(hits[0].get("last_mcap") or 0) > 500_000


def test_historical_hydrate_sol_fomo_zero_not_starved_by_fat_scan():
    """AQUA-class: fomo_board $0 last must survive HISTORICAL_HYDRATE_SCAN pre-score cap."""
    from launchfinder.scoring.hunt import HISTORICAL_HYDRATE_SCAN, hunt_eligible

    init_db()
    now = datetime.now(timezone.utc)
    aqua = "AquaFomoStub111111111111111111111111111"
    with session_scope() as session:
        for i in range(HISTORICAL_HYDRATE_SCAN + 12):
            mint = f"FatSolHist{i:04d}1111111111111111111111111"[:44]
            token = Token(
                mint=mint,
                symbol=f"F{i}",
                chain="sol",
                source="poll",
                is_historical=True,
                first_seen_at=now,
            )
            token.research = Research(p_good=0.1, features_json="{}")
            token.outcome = Outcome(
                t0_mcap=100_000.0,
                last_mcap=2_000_000.0 + float(i),
                last_liq=50_000.0,
                multiple=20.0,
            )
            session.add(token)
        aqua_tok = Token(
            mint=aqua,
            symbol="AQUA",
            chain="sol",
            source="fomo_board",
            is_historical=True,
            first_seen_at=now,
            created_at_chain=None,
        )
        aqua_tok.research = Research(p_good=0.0, features_json="{}", risk_flags_json="[]")
        aqua_tok.outcome = Outcome(t0_mcap=0.0, last_mcap=0.0, multiple=0.0)
        session.add(aqua_tok)
        session.flush()
        # v179: zero t0+last fomo_board stubs are hydrate-only, not Hunt-eligible.
        assert hunt_eligible(aqua_tok, aqua_tok.outcome, aqua_tok.research) is False
        mints = historical_hydrate_tape_mints(session, "sol", limit=20)
        assert aqua in mints


def test_historical_resi_outside_hunt_window_still_in_hydrate_set():
    init_db()
    resi = "resideK2Ejv9apu1op2VBv89poumxCDCs8nzBxQ5q1y"
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        token = Token(
            mint=resi,
            symbol="RESI",
            chain="sol",
            source="websocket",
            is_historical=True,
            first_seen_at=now - timedelta(days=5),
            migrated_at=now - timedelta(days=5),
        )
        token.research = Research(p_good=0.5, features_json="{}")
        token.outcome = Outcome(
            t0_mcap=69_000.0,
            last_mcap=577_531.0,
            last_liq=78_629.0,
            multiple=8.0,
        )
        session.add(token)
        session.flush()
        mints = historical_hydrate_tape_mints(session, "sol", limit=20)
        assert resi in mints
        sync_fat_hunt_board_cards(session, "sol", limit=8)


def test_vrax_unpark_appears_on_rh_hunt_board_past_24h_window():
    init_db()
    market = {
        "mint": VRAX,
        "mcap_usd": 3_750_000.0,
        "liquidity_usd": 180_000.0,
        "volume_h1": 800.0,
        "symbol": "VRAX",
        "name": "Commander Vrax",
        "pair_address": VRAX_PAIR,
    }
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        token = Token(
            mint=VRAX,
            symbol="VRAX",
            chain="robinhood",
            source="rh_bitquery",
            is_historical=True,
            pool_address=VRAX_PAIR,
            first_seen_at=now - timedelta(hours=30),
            migrated_at=now - timedelta(hours=30),
        )
        token.research = Research(p_good=0.5, features_json="{}", holder_count=120)
        token.outcome = Outcome(t0_mcap=69_000.0, last_mcap=0.0, last_liq=0.0)
        session.add(token)
        session.flush()
        assert apply_hunt_tape_market(session, token, market, now=now) is True
        card = upsert_hunt(session, token, now=now, touch_updated=False)
        assert card is not None
        assert float(card.last_mcap or 0) > 1_000_000
        mints = list_hunt_mints(session, "robinhood", limit=80)
        assert VRAX in mints
    board = _hunt_board_sync("robinhood", 80, 0.0)
    hits = [i for i in board["items"] if (i.get("mint") or "").lower() == VRAX.lower()]
    assert hits and float(hits[0].get("last_mcap") or 0) > 500_000


def test_historical_resi_class_reanchors_stale_last_to_liq_first_dex():
    init_db()
    resi = "resideK2Ejv9apu1op2VBv89poumxCDCs8nzBxQ5q1y"
    market = {
        "mint": resi,
        "mcap_usd": 920_000.0,
        "liquidity_usd": 95_000.0,
        "volume_h1": 200_000.0,
        "symbol": "RESI",
        "dex_id": "raydium",
    }
    with session_scope() as session:
        token = Token(
            mint=resi,
            symbol="RESI",
            chain="sol",
            source="websocket",
            is_historical=True,
            first_seen_at=utcnow(),
            migrated_at=utcnow(),
        )
        token.research = Research(p_good=0.5, features_json="{}")
        token.outcome = Outcome(t0_mcap=69_000.0, last_mcap=1_720_000.0, last_liq=50_000.0, multiple=24.9)
        session.add(token)
        session.flush()
        ok = apply_hunt_tape_market(session, token, market, now=datetime.now(timezone.utc))
        assert ok is True
        assert token.is_historical is False
        assert 850_000 < float(token.outcome.last_mcap or 0) < 980_000


def test_sol_desk_high_vs_fomo_alarm():
    init_db()
    resi = "resideK2Ejv9apu1op2VBv89poumxCDCs8nzBxQ5q1y"
    with session_scope() as session:
        token = Token(
            mint=resi,
            symbol="RESI",
            chain="sol",
            source="websocket",
            first_seen_at=utcnow(),
            migrated_at=utcnow(),
        )
        token.research = Research(p_good=0.5, features_json="{}")
        token.outcome = Outcome(t0_mcap=69_000.0, last_mcap=1_720_000.0, last_liq=50_000.0)
        session.add(token)
        session.flush()
        session.add(
            HuntCard(
                chain="sol",
                mint=resi,
                token_id=token.id,
                first_seen_at=utcnow(),
                launched_at=utcnow(),
            )
        )
        session.flush()
        alarms = hunt_mcap_hydrate_gaps(
            session,
            [{"chain": "sol", "mint": resi, "mcap_usd": 642_000}],
            chain="sol",
        )
        assert any(a["reason"] == "desk_mcap_high_vs_fomo" for a in alarms)
