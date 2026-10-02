"""FOMO trader scorecard — scoring only, no live WSS."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from launchfinder.db import init_db, session_scope
from launchfinder.models import FomoAlertEvent, HuntCard, Outcome, Token, utcnow
from launchfinder.research.fomo_trader_scorecard import (
    build_trader_scorecard,
    is_lead_early,
    trader_identity,
    trader_scorecard_detail,
)


def _add_buy(session, **kw):
    defaults = {
        "event_id": f"ev-{kw.get('trader', 't')}-{kw.get('mint', 'm')}",
        "trader": "alpha",
        "user_id": "u1",
        "token_symbol": "TOK",
        "mint": "Mint1111111111111111111111111111111111111",
        "chain": "sol",
        "alert_type": "buy",
        "usd_value": 3000.0,
        "received_at": utcnow(),
        "event_ts": utcnow(),
    }
    defaults.update(kw)
    session.add(FomoAlertEvent(**defaults))


def test_trader_identity_prefers_user_id():
    ev = FomoAlertEvent(trader="@foo", user_id="uid-9", event_id="x")
    assert trader_identity(ev) == "uid:uid-9"


def test_lead_early_before_hunt_first_seen():
    now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    ev = FomoAlertEvent(
        event_id="e1",
        trader="t",
        mint="m",
        chain="sol",
        alert_type="buy",
        event_ts=now,
        received_at=now,
    )
    hunt_at = now + timedelta(hours=2)
    assert is_lead_early(ev, hunt_at, None) is True
    assert is_lead_early(ev, now - timedelta(hours=1), None) is False


def test_scorecard_ranks_high_lead_trader():
    init_db()
    now = utcnow()
    hunt_later = now + timedelta(hours=4)
    with session_scope() as session:
        tok = Token(mint="MintScore1111111111111111111111111111111", symbol="SC", chain="sol", source="test")
        session.add(tok)
        session.flush()
        session.add(
            HuntCard(
                chain="sol",
                mint=tok.mint,
                token_id=tok.id,
                first_seen_at=hunt_later,
            )
        )
        session.add(
            Outcome(token_id=tok.id, multiple=3.0, t0_mcap=50_000, max_mcap=150_000, label=1)
        )
        wallet = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
        for i in range(5):
            _add_buy(
                session,
                event_id=f"lead-{i}",
                trader="leader",
                user_id="lead-uid",
                trader_wallet=wallet if i == 4 else "",
                mint=tok.mint,
                received_at=now - timedelta(days=1),
                event_ts=now - timedelta(days=1),
            )
        for i in range(5):
            _add_buy(
                session,
                event_id=f"late-{i}",
                trader="laggard",
                user_id="lag-uid",
                mint=tok.mint,
                received_at=hunt_later + timedelta(minutes=i),
                event_ts=hunt_later + timedelta(minutes=i),
                on_hunt=True,
            )
        session.flush()
        card = build_trader_scorecard(session, min_buys=5, days=7)
        assert len(card["traders"]) >= 1
        top = card["traders"][0]
        assert top["trader_key"] == "uid:lead-uid"
        assert top["lead_early"] == 5
        assert top["trader_wallet"] == wallet
        detail = trader_scorecard_detail(session, user_id="lead-uid")
        assert detail is not None
        assert detail["n_buys"] == 5
