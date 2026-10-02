import asyncio
from datetime import timedelta

from launchfinder.db import init_db, session_scope
from launchfinder.models import Outcome, Research, Snapshot, Token, utcnow
from launchfinder.scoring import outcomes as outcomes_mod
from launchfinder.scoring.outcomes import _early_scan_due


def test_early_scan_gap_logic():
    init_db()
    with session_scope() as session:
        t = Token(mint="EarlyScanMint1111", symbol="EARL", source="poll", first_seen_at=utcnow())
        session.add(t)
        session.flush()
        now = utcnow()
        assert _early_scan_due(session, t.id, now)  # nothing recorded yet
        session.add(Snapshot(token_id=t.id, kind="early", mcap_usd=80_000.0, taken_at=now - timedelta(minutes=2)))
        session.flush()
        assert not _early_scan_due(session, t.id, now)  # too recent
        session.add(Snapshot(token_id=t.id, kind="early", mcap_usd=80_000.0, taken_at=now - timedelta(minutes=6)))
        # the max() is still the 2-minute-old one
        assert not _early_scan_due(session, t.id, now)


def test_young_token_gets_early_snapshots(monkeypatch):
    init_db()

    async def fake_market(mint, chain="sol"):
        return {"mcap_usd": 95_000.0, "liquidity_usd": 25_000.0, "price_usd": 0.0001, "buys_m5": 12, "sells_m5": 4}

    async def fake_coin(mint):
        return {}

    monkeypatch.setattr(outcomes_mod.dexscreener, "token_market", fake_market)
    monkeypatch.setattr(outcomes_mod.pumpfun, "get_coin", fake_coin)

    with session_scope() as session:
        t = Token(
            mint="EarlyDenseMint111",
            symbol="DENSE",
            source="poll",
            first_seen_at=utcnow() - timedelta(minutes=8),
            migrated_at=utcnow() - timedelta(minutes=8),
        )
        t.research = Research(p_good=0.5, features_json="{}", risk_flags_json="[]", reasons_json="[]")
        session.add(t)
        session.flush()
        session.add(Outcome(token_id=t.id, t0_mcap=69_000.0, max_mcap=69_000.0))

    with session_scope() as session:
        asyncio.run(outcomes_mod.refresh_outcomes(session))

    with session_scope() as session:
        kinds = [s.kind for s in session.query(Snapshot).join(Token, Token.id == Snapshot.token_id).filter(Token.mint == "EarlyDenseMint111").all()]
        assert "early" in kinds
