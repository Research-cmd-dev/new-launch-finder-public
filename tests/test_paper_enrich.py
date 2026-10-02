"""Thin-facts paper open + enrich veto cancel. Paper only."""

import json
from datetime import timedelta

from launchfinder.db import init_db, session_scope
from launchfinder.ledger import cancel_provisional_paper_on_veto
from launchfinder.models import PaperFill, Ticket, Token, utcnow
from launchfinder.research.holders import SOL_HOLDER_OPEN_PAGES
from launchfinder.research.pipeline import _live_open_fast
from launchfinder.scoring.features import FEATURE_NAMES
from launchfinder.scoring.paper_gate import PAPER_ENRICH_VETO, PAPER_OPEN_VIA_THIN
from launchfinder.scoring.paper_v1 import PAPER_V1_LINE


def test_feature_names_stay_66_and_open_pages_are_capped():
    assert len(FEATURE_NAMES) == 66
    assert SOL_HOLDER_OPEN_PAGES == 1
    assert PAPER_ENRICH_VETO == "enrich veto"


def test_live_open_fast_skips_historical():
    live = Token(mint="a", is_historical=False)
    hist = Token(mint="b", is_historical=True)
    assert _live_open_fast(live, {}) is True
    assert _live_open_fast(hist, {}) is False
    assert _live_open_fast(live, {"historical": True}) is False


def _thin_token(mint: str):
    now = utcnow()
    tok = Token(
        mint=mint,
        symbol=mint[:4].upper(),
        chain="sol",
        first_seen_at=now - timedelta(minutes=6),
        migrated_at=now - timedelta(minutes=6),
        source="poll",
    )
    from launchfinder.desk_lines import SCORER_FIRST_SIGHT
    from launchfinder.models import Outcome, Research

    tok.research = Research(
        features_json=json.dumps({"paper_provisional": True, "name_quality": 0.7, "holder_n": 0.4}),
        p_good=0.20,
        heuristic_p=0.2,
        model_p=0.2,
        holder_count=40,
        risk_flags_json="[]",
        scorer=SCORER_FIRST_SIGHT,
    )
    tok.outcome = Outcome(t0_mcap=57_000, last_mcap=60_000, last_liq=20_000, max_mcap=60_000)
    return tok


def test_cancel_provisional_skips_thin_fill_and_ticket():
    init_db()
    now = utcnow()
    with session_scope() as session:
        tok = _thin_token("ProvCancel1111111111111111111111111111111")
        session.add(tok)
        session.flush()
        fill = PaperFill(
            chain="sol",
            mint=tok.mint,
            token_id=tok.id,
            line="gated90",
            opened_at=now,
            entry_p=0.20,
            entry_mcap=57_000,
            entry_liq=20_000,
            target=2.0,
            ride=10.0,
            max_mcap=57_000,
            min_mcap=57_000,
            last_mcap=57_000,
            last_liq=20_000,
            status="open",
            open_via=PAPER_OPEN_VIA_THIN,
            image_rev="test",
            updated_at=now,
        )
        session.add(fill)
        session.flush()
        ticket = Ticket(
            chain="sol",
            mint=tok.mint,
            token_id=tok.id,
            fill_id=fill.id,
            symbol=tok.symbol,
            entry_p=0.20,
            entry_mcap=57_000,
            liq=20_000,
            status="shadow",
            image_rev="test",
        )
        session.add(ticket)
        session.flush()
        out = cancel_provisional_paper_on_veto(session, tok, "honeypot")
        assert out["cancelled"] == 1 and out["tickets"] == 1
        session.refresh(fill)
        session.refresh(ticket)
        assert fill.status == "skipped"
        assert fill.exit_reason == "honeypot"
        assert ticket.status == "skipped"
        feat = json.loads(tok.research.features_json)
        assert feat.get("paper_provisional") is False
        assert feat.get("paper_enrich_veto") == "honeypot"


def test_cancel_leaves_non_provisional_fills():
    init_db()
    now = utcnow()
    with session_scope() as session:
        tok = _thin_token("ProvKeep111111111111111111111111111111111")
        tok.research.features_json = json.dumps({"name_quality": 0.7})
        session.add(tok)
        session.flush()
        fill = PaperFill(
            chain="sol",
            mint=tok.mint,
            token_id=tok.id,
            line=PAPER_V1_LINE,
            opened_at=now,
            entry_p=0.20,
            entry_mcap=70_000,
            entry_liq=25_000,
            target=2.0,
            ride=10.0,
            max_mcap=70_000,
            min_mcap=70_000,
            last_mcap=70_000,
            last_liq=25_000,
            status="open",
            open_via="immediate",
            image_rev="test",
            updated_at=now,
        )
        session.add(fill)
        session.flush()
        out = cancel_provisional_paper_on_veto(session, tok, "honeypot")
        assert out["cancelled"] == 0
        session.refresh(fill)
        assert fill.status == "open"
