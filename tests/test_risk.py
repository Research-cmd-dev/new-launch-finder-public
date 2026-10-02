"""Risk stub + UTC-day report — paper only, arm default off."""

from datetime import datetime, timezone
import json

from launchfinder.db import init_db, session_scope
from launchfinder.ledger import (
    DECISION_ENTRY,
    _consider_paper_v1,
    paper_v1_day_report,
    promote_paper_v1_queue,
)
from launchfinder.models import Decision, PaperFill, Research, Token, utcnow
from launchfinder.risk import (
    kill_paper_v1_queue,
    live_blocked,
    load_risk,
    paper_v1_blocked,
    risk_status,
    save_risk,
)
from launchfinder.scoring.paper_v1 import v1_queue_reason


def test_risk_defaults_safe():
    init_db()
    with session_scope() as session:
        st = risk_status(session)
        assert st["armed"] is False
        assert st["kill_switch"] is False
        assert st["live_allowed"] is False
        assert st["paper_v1_open_allowed"] is True
        assert live_blocked(session) == "not_armed"
        assert paper_v1_blocked(session) == ""


def test_kill_switch_blocks_paper_and_disarms():
    init_db()
    with session_scope() as session:
        save_risk(session, {"armed": True, "kill_switch": True, "note": "test"})
        st = load_risk(session)
        assert st["kill_switch"] is True
        assert st["armed"] is False  # forced off
        assert paper_v1_blocked(session) == "kill_switch"
        assert live_blocked(session) == "kill_switch"


def test_chain_pause_blocks_one_chain():
    init_db()
    with session_scope() as session:
        save_risk(session, {"chain_pause": ["robinhood"]})
        assert paper_v1_blocked(session, "sol") == ""
        assert paper_v1_blocked(session, "robinhood") == "chain_pause:robinhood"


def test_kill_flushes_queued_paper_v1():
    init_db()
    with session_scope() as session:
        now = datetime(2026, 9, 27, 15, 0, tzinfo=timezone.utc)
        day = "2026-09-27"
        tok = Token(
            mint="RiskKillMint11111111111111111111111111",
            symbol="RKILL",
            chain="sol",
            first_seen_at=now,
            migrated_at=now,
            created_at_chain=now,
            source="poll",
        )
        session.add(tok)
        session.flush()
        session.add(
            Decision(
                token_id=tok.id,
                mint=tok.mint,
                kind=DECISION_ENTRY,
                chain="sol",
                at=now,
                entry_p=0.2,
                entry_mcap=70_000,
                liq=25_000,
                features_json='{"name_quality":0.7}',
                image_rev="test",
            )
        )
        session.flush()
        dec = session.query(Decision).filter(Decision.token_id == tok.id).one()
        session.add(
            PaperFill(
                chain="sol",
                mint=tok.mint,
                token_id=tok.id,
                decision_id=dec.id,
                line="paper_v1",
                opened_at=now,
                entry_p=0.2,
                entry_mcap=70_000,
                entry_liq=25_000,
                target=2.0,
                ride=10.0,
                max_mcap=70_000,
                min_mcap=70_000,
                last_mcap=70_000,
                last_liq=25_000,
                status="queued",
                exit_reason=v1_queue_reason(0.8, day),
                image_rev="test",
                updated_at=now,
            )
        )
        session.flush()
        save_risk(session, {"kill_switch": True})
        out = kill_paper_v1_queue(session, reason="kill_switch")
        assert out["skipped"] == 1
        fill = session.query(PaperFill).one()
        assert fill.status == "skipped"
        assert "kill_switch" in (fill.exit_reason or "")


def test_day_report_includes_risk_and_text():
    init_db()
    with session_scope() as session:
        now = datetime(2026, 9, 27, 16, 0, tzinfo=timezone.utc)
        out = paper_v1_day_report(session, day="2026-09-27", now=now)
        assert out["day"] == "2026-09-27"
        assert out["paper_only"] is True
        assert "text" in out and "paperV1" in out["text"]
        assert out["risk"]["armed"] is False
        assert "gate" in out
        assert set(out["chains"]) == {"sol", "robinhood"}


def test_desk_js_loads_report_and_risk():
    from pathlib import Path

    js = Path("launchfinder/static/desk.js").read_text()
    assert "/api/paper/v1/report" in js
    assert "reportData" in js
    assert "kill switch" in js
