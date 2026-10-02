"""hunt_tape per-mint defer + boot hunt_card delete batches."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import InvalidRequestError

from launchfinder.db import init_db, session_scope
from launchfinder.models import HuntCard, Outcome, Research, Token, utcnow
from launchfinder.scoring.hunt import (
    _delete_hunt_cards_resilient,
    _hunt_session_defer,
    rebuild_hunt_window,
    upsert_hunt,
)
from launchfinder.scoring.hunt_tape import refresh_hunt_tape


def test_hunt_session_defer_closed_transaction():
    assert _hunt_session_defer(
        InvalidRequestError("Can't operate on closed transaction inside context manager")
    )


def test_delete_hunt_cards_resilient_batches():
    init_db()
    now = utcnow()
    with session_scope() as session:
        cards = []
        for i in range(7):
            token = Token(
                mint=f"DelHunt{i}111111111111111111111111111111111",
                symbol=f"D{i}",
                chain="sol",
                first_seen_at=now - timedelta(days=30),
                source="poll",
            )
            token.research = Research(features_json="{}", p_good=0.1)
            token.outcome = Outcome(t0_mcap=10_000, last_mcap=12_000, multiple=1.2)
            session.add(token)
            session.flush()
            card = HuntCard(
                chain="sol",
                mint=token.mint,
                token_id=token.id,
                first_seen_at=now - timedelta(days=30),
                launched_at=now - timedelta(days=30),
                entry_p=0.1,
                t0_mcap=10_000,
                last_mcap=12_000,
            )
            session.add(card)
            cards.append(card)
        session.flush()
        n = _delete_hunt_cards_resilient(session, cards, batch_size=3)
        assert n == 7
        assert session.query(HuntCard).count() == 0


def test_upsert_hunt_without_nested_savepoint_inside_outer_nested():
    init_db()
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        token = Token(
            mint="NestedHunt111111111111111111111111111111",
            symbol="NH",
            chain="sol",
            first_seen_at=now,
            migrated_at=now,
            source="poll",
        )
        token.research = Research(features_json="{}", p_good=0.25, holder_count=50)
        token.outcome = Outcome(t0_mcap=69_000, last_mcap=80_000, last_liq=20_000, multiple=1.16)
        session.add(token)
        session.flush()
        with session.begin_nested():
            row = upsert_hunt(session, token, now=now, nested_savepoint=False)
        assert row is not None


@pytest.mark.asyncio
async def test_refresh_hunt_tape_defers_one_mint_and_completes(monkeypatch):
    init_db()
    mints = [
        "DeferMint1111111111111111111111111111111",
        "OkMint111111111111111111111111111111111",
    ]
    calls: list[str] = []

    def fake_window(session):
        return {"sol": mints}

    async def fake_markets(*args, **kwargs):
        return {m: {"mcap_usd": 100_000, "liquidity_usd": 50_000, "volume_h1": 1000} for m in mints}

    def fake_apply(session, token, market, now=None, nested_savepoint=True, write_bar=True):
        calls.append(token.mint)
        if token.mint == mints[0]:
            raise InvalidRequestError("Can't operate on closed transaction inside context manager")
        return False

    monkeypatch.setattr(
        "launchfinder.scoring.hunt_tape.this_window_hunt_tape_mints",
        fake_window,
    )
    monkeypatch.setattr(
        "launchfinder.scoring.hunt_tape.historical_hydrate_tape_mints",
        lambda session, chain, limit=24: [],
    )
    monkeypatch.setattr("launchfinder.research.dexscreener.token_markets", fake_markets)
    async def fake_enrich(markets, tokens, chain):
        return markets

    monkeypatch.setattr(
        "launchfinder.research.dexscreener.enrich_markets_with_pool_fallback",
        fake_enrich,
    )
    monkeypatch.setattr("launchfinder.scoring.hunt_tape.apply_hunt_tape_market", fake_apply)

    with session_scope() as session:
        for mint in mints:
            token = Token(
                mint=mint,
                symbol="X",
                chain="sol",
                first_seen_at=utcnow(),
                source="poll",
            )
            token.research = Research(features_json="{}", p_good=0.2)
            token.outcome = Outcome(t0_mcap=50_000, last_mcap=60_000, multiple=1.2)
            session.add(token)
        session.flush()
        wrote = await refresh_hunt_tape(session)
    # Apply deferred the first mint; sibling savepoint still writes leftover bars.
    assert wrote == 2
    assert len(calls) == 2


def test_rebuild_hunt_window_uses_resilient_delete(monkeypatch):
    init_db()
    now = utcnow()
    with session_scope() as session:
        token = Token(
            mint="StaleHunt111111111111111111111111111111",
            symbol="OLD",
            chain="sol",
            first_seen_at=now - timedelta(days=40),
            migrated_at=now - timedelta(days=40),
            source="poll",
        )
        token.research = Research(features_json="{}", p_good=0.1)
        token.outcome = Outcome(t0_mcap=10_000, last_mcap=11_000, multiple=1.1)
        session.add(token)
        session.flush()
        session.add(
            HuntCard(
                chain="sol",
                mint=token.mint,
                token_id=token.id,
                first_seen_at=now - timedelta(days=40),
                launched_at=now - timedelta(days=40),
                entry_p=0.1,
                t0_mcap=10_000,
                last_mcap=11_000,
            )
        )
        session.flush()
        batches: list[int] = []

        def track_delete(session, cards, *, batch_size=5):
            batches.append(len(cards))
            return _delete_hunt_cards_resilient(session, cards, batch_size=batch_size)

        monkeypatch.setattr(
            "launchfinder.scoring.hunt._delete_hunt_cards_resilient",
            track_delete,
        )
        rebuild_hunt_window(session, "sol", limit=10)
        assert batches and batches[0] >= 1


def test_recover_session_after_lock_pending_rollback():
    from sqlalchemy.exc import PendingRollbackError

    from launchfinder.scoring.hunt import _pending_rollback, recover_session_after_lock

    assert _pending_rollback(
        PendingRollbackError("This Session's transaction has been rolled back due to a previous exception during flush.", [])
    )
    init_db()
    with session_scope() as session:
        exc = PendingRollbackError("pending rollback", [])
        assert recover_session_after_lock(session, exc, outer=True) is True
