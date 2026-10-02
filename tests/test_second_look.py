import asyncio

from launchfinder.db import init_db, session_scope
from launchfinder.scoring.outcomes import mention_velocity_boost


def test_mention_velocity_boost_rules():
    import pytest

    # accelerating: 4 -> 20 mentions/hr
    assert mention_velocity_boost(0.66, 4, 20) == pytest.approx(0.71)
    # real volume but not accelerating (baseline already high)
    assert mention_velocity_boost(0.66, 30, 35) == 0.66
    # low absolute volume never boosts
    assert mention_velocity_boost(0.66, 0, 6) == 0.66
    # cap at 0.95
    assert mention_velocity_boost(0.93, 2, 50) == pytest.approx(0.95)
from launchfinder.models import Outcome, Research, Token, utcnow
from launchfinder.scoring.features import extract_features
from launchfinder.scoring.outcomes import _second_look

import json


def test_second_look_rescoring_moves_with_market():
    init_db()
    with session_scope() as session:
        token = Token(mint="SecondLookMint111", symbol="SL", name="Second Look", first_seen_at=utcnow())
        features = extract_features(
            {
                "coin": {"name": "Second Look", "symbol": "SL"},
                "market": {"buys_m5": 5, "sells_m5": 20, "liquidity_usd": 900, "volume_h1": 500},
            }
        )
        token.research = Research(
            features_json=json.dumps(features),
            risk_flags_json="[]",
            p_good=0.3,
        )
        outcome = Outcome(token=token, t0_mcap=70_000.0, max_mcap=90_000.0)
        session.add(token)
        session.add(outcome)
        session.flush()

        strong_market = {
            "buys_m5": 80,
            "sells_m5": 20,
            "liquidity_usd": 60_000,
            "volume_h1": 45_000,
            "mcap_usd": 120_000.0,
        }
        weak_market = {
            "buys_m5": 2,
            "sells_m5": 40,
            "liquidity_usd": 300,
            "volume_h1": 100,
            "mcap_usd": 20_000.0,
        }
        p_strong = asyncio.run(_second_look(session, token, outcome, strong_market))
        p_weak = asyncio.run(_second_look(session, token, outcome, weak_market))
        assert 0.0 < p_weak < p_strong < 1.0
        # unlabeled + still under 2x: the live %score tracks the latest tape
        assert token.research.p_good == p_weak
        # at-entry features stay frozen for training
        stored = json.loads(token.research.features_json)
        entry = extract_features(
            {
                "coin": {"name": "Second Look", "symbol": "SL"},
                "market": {"buys_m5": 5, "sells_m5": 20, "liquidity_usd": 900, "volume_h1": 500},
            }
        )
        assert stored["liquidity_n"] == entry["liquidity_n"]


def test_second_look_dex_miss_uses_last_liq():
    # Stored CHAD/PVP-class tape has volume + holders but empty Dex liq.
    # last_liq $15k must refill liquidity_n so the wide-organic floor fires.
    # research.p_good stays the entry score.
    init_db()
    with session_scope() as session:
        features = extract_features(
            {
                "coin": {"name": "Pvp", "symbol": "PVP"},
                "holders": {"holder_count": 138, "top10_pct": 38},
                "market": {"liquidity_usd": 0, "volume_h1": 1_120},
            }
        )
        assert features["organic_book"] == 0.0
        token = Token(mint="PvpDexMissMint111", symbol="PVP", name="Pvp", first_seen_at=utcnow())
        token.research = Research(features_json=json.dumps(features), risk_flags_json="[]", p_good=0.36)
        outcome = Outcome(token=token, t0_mcap=40_000.0, max_mcap=400_000.0, last_liq=15_284)
        session.add_all([token, outcome])
        session.flush()
        p = asyncio.run(
            _second_look(
                session,
                token,
                outcome,
                {"buys_m5": 0, "sells_m5": 0, "liquidity_usd": 0, "volume_h1": 0},
            )
        )
        assert p >= 0.60
        # multiple is already 10x — do not lift a climber onto the paper line
        assert token.research.p_good == 0.36


def test_second_look_does_not_rewrite_labeled_score():
    init_db()
    with session_scope() as session:
        token = Token(mint="SecondLookLabeledMint222", symbol="LAB", first_seen_at=utcnow())
        features = extract_features(
            {
                "coin": {"name": "Labeled", "symbol": "LAB"},
                "market": {"buys_m5": 5, "sells_m5": 20, "liquidity_usd": 900, "volume_h1": 500},
            }
        )
        token.research = Research(features_json=json.dumps(features), risk_flags_json="[]", p_good=0.41)
        outcome = Outcome(token=token, t0_mcap=20_000.0, max_mcap=21_000.0, multiple=1.05, label=0)
        session.add_all([token, outcome])
        session.flush()
        asyncio.run(
            _second_look(
                session,
                token,
                outcome,
                {"buys_m5": 80, "sells_m5": 10, "liquidity_usd": 50_000, "volume_h1": 40_000, "mcap_usd": 80_000},
            )
        )
        assert token.research.p_good == 0.41


def test_second_look_does_not_realert_when_lift_is_blocked(monkeypatch):
    # Live IBUYMEMES: 0.57 -> 0.71 logged every refresh because multiple
    # >= 2 blocked the write but the upgrade path still compared the
    # computed live p to the frozen entry.
    init_db()
    alerts = []

    async def fake_notify(**kwargs):
        alerts.append(kwargs)

    monkeypatch.setattr("launchfinder.alerts.notify_high_score", fake_notify)

    with session_scope() as session:
        token = Token(
            mint="0xibuymemesrealert0000000000000000000001",
            symbol="IBUYMEMES",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        features = extract_features(
            {
                "coin": {"name": "IBUYMEMES", "symbol": "IBUYMEMES"},
                "holders": {"holder_count": 80, "top10_pct": 30},
                "market": {"buys_m5": 10, "sells_m5": 8, "liquidity_usd": 8_000, "volume_h1": 4_000},
            }
        )
        token.research = Research(features_json=json.dumps(features), risk_flags_json="[]", p_good=0.57)
        outcome = Outcome(token=token, t0_mcap=22_000.0, max_mcap=55_000.0, multiple=2.5, last_liq=18_000)
        session.add_all([token, outcome])
        session.flush()
        asyncio.run(
            _second_look(
                session,
                token,
                outcome,
                {
                    "buys_m5": 80,
                    "sells_m5": 10,
                    "liquidity_usd": 40_000,
                    "volume_h1": 30_000,
                    "mcap_usd": 55_000,
                },
            )
        )
        assert token.research.p_good == 0.57
        assert alerts == []


def test_second_look_does_not_pull_down_a_real_2x_book():
    # Live GUH: 2.53x / 105 wallets / $41k wrote 0.68→0.17 after the
    # 480 floor lapsed. Real climbers freeze; airdrop/thin/empty may drop.
    init_db()
    with session_scope() as session:
        features = extract_features(
            {
                "chain": "robinhood",
                "coin": {"name": "Guh", "symbol": "GUH"},
                "holders": {"holder_count": 105, "top10_pct": 22},
                "market": {"liquidity_usd": 47_454, "volume_h1": 8_000},
                "last_liq": 47_454,
            }
        )
        assert features["rh_airdrop_book"] == 0.0
        token = Token(
            mint="0xguhreal2xbook0000000000000000000000001",
            symbol="GUH",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        token.research = Research(features_json=json.dumps(features), risk_flags_json="[]", p_good=0.68)
        outcome = Outcome(
            token=token,
            t0_mcap=20_000.0,
            max_mcap=50_600.0,
            multiple=2.53,
            last_liq=41_089.0,
        )
        session.add_all([token, outcome])
        session.flush()
        asyncio.run(
            _second_look(
                session,
                token,
                outcome,
                {
                    "buys_m5": 2,
                    "sells_m5": 40,
                    "liquidity_usd": 36_643.0,
                    "volume_h1": 2.0,
                    "mcap_usd": 50_600.0,
                },
            )
        )
        assert token.research.p_good == 0.68


def test_second_look_flags_rh_start_high_t0():
    # Live KFC: frozen features said organic / p=0.82 while t0 was $146k.
    init_db()
    with session_scope() as session:
        features = extract_features(
            {
                "chain": "robinhood",
                "coin": {"name": "Kentucky Fried Cookie", "symbol": "KFC"},
                "twitter": {"followers": 8_000, "age_days": 400, "verified": True, "tweets": 400},
                "twitter_handle": "kfc",
                "website": "https://example.com",
                "holders": {"holder_count": 1222, "top10_pct": 40},
                "market": {"liquidity_usd": 40_826, "volume_h1": 8_000, "mcap_usd": 40_826},
            }
        )
        assert features["entry_premium"] == 0.0
        assert features["organic_book"] == 1.0
        token = Token(
            mint="0xkfcstarthighlook000000000000000000001",
            symbol="KFC",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        token.research = Research(features_json=json.dumps(features), risk_flags_json="[]", p_good=0.82)
        outcome = Outcome(
            token=token,
            t0_mcap=146_816.0,
            max_mcap=146_816.0,
            multiple=1.0,
            last_liq=40_826.0,
        )
        session.add_all([token, outcome])
        session.flush()
        asyncio.run(
            _second_look(
                session,
                token,
                outcome,
                {"liquidity_usd": 40_826.0, "volume_h1": 8_000.0, "mcap_usd": 40_826.0},
            )
        )
        assert token.research.p_good < 0.70


def test_second_look_does_not_recap_lp_open_as_thin():
    # Live MEME: frozen features_json said rh_thin_book=1 (4 wallets).
    # t0 last_liq was $22k — under the $50k fat-book line. Second-look
    # used to recap thin and flip p 0.04↔0.33 while hour-one volume
    # went $768→$85k. Re-detect the 98.8% pool rows and lift under 2x.
    init_db()
    with session_scope() as session:
        features = extract_features(
            {
                "chain": "robinhood",
                "coin": {"name": "A Meme Coin", "symbol": "MEME"},
                "twitter": {"followers": 2, "age_days": 0.02, "verified": True, "tweets": 0},
                "twitter_handle": "amemecoinrh",
                "symbol_flood": 4,
                "holders": {
                    "holder_count": 4,
                    "top10_pct": 1.2,
                    "top_wallets": [
                        {"pct": 98.8, "label": "pool"},
                        {"pct": 1.2, "label": ""},
                    ],
                },
                "market": {},
                "gmgn": {"source": "gmgn", "rug_risk": 0},
            }
        )
        # Simulate a pre-learning snapshot that froze the thin cap.
        features["rh_thin_book"] = 1.0
        features.pop("rh_lp_open_book", None)
        token = Token(
            mint="0x385f4f8ae47651ce5f58f5265395a669f8281e18",
            symbol="MEME",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        token.research = Research(
            features_json=json.dumps(features),
            risk_flags_json='["X account created very recently"]',
            p_good=0.15,
            heuristic_p=0.15,
            holder_count=4,
            raw_json=json.dumps(
                {
                    "holders": {
                        "holder_count": 4,
                        "top_wallets": [
                            {"pct": 98.8, "label": "pool"},
                            {"pct": 1.2, "label": ""},
                        ],
                    },
                    "gmgn": {"source": "gmgn", "holder_count": 80, "rug_risk": 0},
                }
            ),
        )
        outcome = Outcome(
            token=token,
            t0_mcap=21_997.0,
            max_mcap=24_000.0,
            multiple=1.09,
            last_liq=22_110.0,
        )
        session.add_all([token, outcome])
        session.flush()
        asyncio.run(
            _second_look(
                session,
                token,
                outcome,
                {
                    "liquidity_usd": 22_110.0,
                    "volume_h1": 768.24,
                    "mcap_usd": 21_997.0,
                },
            )
        )
        assert token.research.p_good >= 0.50
        stored = json.loads(token.research.features_json)
        # At-entry vector stays frozen for training.
        assert stored["rh_thin_book"] == 1.0


def test_second_look_overlays_late_gmgn_on_lp_open():
    # Ingest froze gmgn_present=0. Trench card later sat in raw_json.
    # Clean late GMGN must lift under 2×; honeypot must withhold paper.
    init_db()
    holders = {
        "holder_count": 4,
        "top_wallets": [
            {"pct": 98.8, "label": "pool"},
            {"pct": 1.2, "label": ""},
        ],
    }
    features = extract_features(
        {
            "chain": "robinhood",
            "coin": {"name": "A Meme Coin", "symbol": "MEME"},
            "twitter": {"followers": 2, "age_days": 0.02, "verified": True, "tweets": 0},
            "twitter_handle": "amemecoinrh",
            "symbol_flood": 4,
            "holders": holders,
            "market": {},
        }
    )
    assert features["gmgn_present"] == 0.0
    with session_scope() as session:
        clean = Token(
            mint="0xlatecleanlpopen00000000000000000001",
            symbol="MEME",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        clean.research = Research(
            features_json=json.dumps(features),
            p_good=0.24,
            heuristic_p=0.24,
            holder_count=4,
            raw_json=json.dumps(
                {
                    "holders": holders,
                    "gmgn": {"source": "gmgn", "rug_risk": 0, "insider_pct": 0},
                }
            ),
        )
        clean_out = Outcome(
            token=clean,
            t0_mcap=21_997.0,
            max_mcap=24_000.0,
            multiple=1.09,
            last_liq=22_110.0,
        )
        dirty = Token(
            mint="0xlatehoneypotlpopen0000000000000002",
            symbol="TRAP",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        dirty.research = Research(
            features_json=json.dumps(features),
            p_good=0.63,
            heuristic_p=0.63,
            holder_count=4,
            raw_json=json.dumps(
                {
                    "holders": holders,
                    "gmgn": {"source": "gmgn", "honeypot": True, "rug_risk": 80},
                }
            ),
        )
        dirty_out = Outcome(
            token=dirty,
            t0_mcap=21_997.0,
            max_mcap=24_000.0,
            multiple=1.09,
            last_liq=22_110.0,
        )
        session.add_all([clean, clean_out, dirty, dirty_out])
        session.flush()
        market = {
            "liquidity_usd": 22_110.0,
            "volume_h1": 768.24,
            "mcap_usd": 21_997.0,
        }
        asyncio.run(_second_look(session, clean, clean_out, market))
        asyncio.run(_second_look(session, dirty, dirty_out, market))
        assert clean.research.p_good >= 0.50
        assert dirty.research.p_good < 0.50
        assert json.loads(clean.research.features_json)["gmgn_present"] == 0.0
