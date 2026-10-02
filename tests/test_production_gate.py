"""FORWARD production-gate progress — Learn card. Paper only, arm stays off."""

from datetime import datetime, timezone
from pathlib import Path

from launchfinder.db import init_db, session_scope
from launchfinder.image_rev import IMAGE_REV
from launchfinder.models import Decision, PaperFill, ScanState, Token, utcnow
from launchfinder.risk import save_risk
from launchfinder.scoring.paper_v1 import PAPER_V1_LINE, v1_skip_stamp
from launchfinder.scoring.production_gate import (
    SAMPLE_BAR,
    THESIS_COVERAGE_BAR,
    production_gate_progress,
)


def test_empty_book_is_honest_and_unarmed():
    init_db()
    with session_scope() as session:
        out = production_gate_progress(session, "sol")
        assert out["paper_only"] is True
        assert out["arm_ui"] is False
        assert out["ready"] is False
        assert out["total"] == 7
        keys = [b["key"] for b in out["bars"]]
        assert keys == [
            "thesis_coverage",
            "sample",
            "hit_quality",
            "fomo_mirror",
            "stability",
            "ritual",
            "risk",
        ]
        by = {b["key"]: b for b in out["bars"]}
        assert by["thesis_coverage"]["color"] == "red"
        assert by["sample"]["color"] == "red"
        assert by["sample"]["value"] == 0
        assert by["risk"]["color"] == "green"
        assert by["risk"]["extra"]["armed"] is False
        assert by["risk"]["extra"]["live_allowed"] is False
        assert THESIS_COVERAGE_BAR == 0.80
        assert SAMPLE_BAR == 30


def test_progress_turns_green_on_evidence():
    init_db()
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    with session_scope() as session:
        for i in range(32):
            mint = f"GateMint{i:02d}1111111111111111111111111"
            tok = Token(
                mint=mint,
                symbol=f"G{i:02d}",
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
                    kind="entry",
                    chain="sol",
                    at=now,
                    entry_p=0.20,
                    entry_mcap=70_000,
                    liq=25_000,
                    features_json='{"github_auth_n":0.9,"real_project":1.0,"gmgn_cto":0.0,"name_quality":0.2}',
                    image_rev="test",
                )
            )
            session.flush()
            dec = session.query(Decision).filter(Decision.token_id == tok.id).one()
            peak = 160_000 if i < 20 else 80_000
            session.add(
                PaperFill(
                    chain="sol",
                    mint=tok.mint,
                    token_id=tok.id,
                    decision_id=dec.id,
                    line=PAPER_V1_LINE,
                    opened_at=now,
                    closed_at=now,
                    entry_p=0.20,
                    entry_mcap=70_000,
                    entry_liq=25_000,
                    target=2.0,
                    ride=10.0,
                    max_mcap=peak,
                    min_mcap=70_000,
                    last_mcap=peak,
                    last_liq=25_000,
                    status="closed",
                    exit_reason="live dump",
                    return_pct=100.0 if i < 20 else 10.0,
                    image_rev="test",
                    updated_at=now,
                )
            )
        # Wide-hi baseline: a few gated90 hi-line closes that lose more often.
        for i in range(8):
            mint = f"WideMint{i:02d}1111111111111111111111111"
            tok = Token(
                mint=mint,
                symbol=f"W{i:02d}",
                chain="sol",
                first_seen_at=now,
                migrated_at=now,
                created_at_chain=now,
                source="poll",
            )
            session.add(tok)
            session.flush()
            session.add(
                PaperFill(
                    chain="sol",
                    mint=tok.mint,
                    token_id=tok.id,
                    line="gated90",
                    opened_at=now,
                    closed_at=now,
                    entry_p=0.95,
                    entry_mcap=70_000,
                    entry_liq=25_000,
                    target=2.0,
                    ride=10.0,
                    max_mcap=80_000 if i else 160_000,
                    min_mcap=70_000,
                    last_mcap=80_000 if i else 160_000,
                    last_liq=25_000,
                    status="closed",
                    exit_reason="live dump",
                    return_pct=-20.0 if i else 80.0,
                    image_rev="test",
                    updated_at=now,
                )
            )
        session.add(
            PaperFill(
                chain="sol",
                mint="SkipMint111111111111111111111111111",
                token_id=session.query(Token).first().id,
                line=PAPER_V1_LINE,
                opened_at=now,
                entry_p=0.12,
                entry_mcap=70_000,
                status="skipped",
                exit_reason=v1_skip_stamp("v1 no-thesis", "2026-09-29"),
                image_rev="test",
                updated_at=now,
            )
        )
        from launchfinder.models import utcnow as _now

        beat = _now()
        session.add(ScanState(key="loop:hunt_tape", value="ok", updated_at=beat))
        session.add(ScanState(key="loop:tape_refresh", value="ok", updated_at=beat))
        session.add(ScanState(key="loop:ingest", value="ok", updated_at=beat))
        session.add(ScanState(key="loop:batch_fit", value="ok", updated_at=beat))
        session.add(ScanState(key="loop:fomo_trending", value="ok", updated_at=beat))
        session.add(ScanState(key="loop:fomo_alerts", value="ws=up", updated_at=beat))
        session.add(ScanState(key="loop:paper_sync", value="ok", updated_at=beat))
        import json

        session.add(
            ScanState(
                key="fomo_trending_audit",
                value=json.dumps(
                    {
                        "at": beat.isoformat(),
                        "board_stale": False,
                        "capture_age_hours": 0.1,
                        "captured_at": beat.isoformat(),
                        "api_source": "captured",
                    }
                ),
                updated_at=beat,
            )
        )
        session.flush()
        out = production_gate_progress(session, "sol", now=now)
        by = {b["key"]: b for b in out["bars"]}
        assert by["thesis_coverage"]["color"] == "green"
        assert by["thesis_coverage"]["value"] >= 0.80
        assert by["sample"]["color"] == "green"
        assert by["sample"]["value"] >= 30
        assert by["hit_quality"]["color"] == "green"
        assert by["hit_quality"]["extra"]["delta"] > 0
        assert by["fomo_mirror"]["color"] == "green"
        assert by["stability"]["color"] == "green"
        assert by["ritual"]["color"] == "green"
        assert by["risk"]["color"] == "green"
        assert out["ready"] is True
        assert out["paper_only"] is True
        assert out["arm_ui"] is False


def test_hit_bar_rate_excl_peak_ignores_peak_promoted_opens():
    from launchfinder.scoring.production_gate import _hit_bar
    from launchfinder.scoring.paper_v1 import PAPER_V1_LINE, PAPER_V1_OPEN_VIA_PEAK

    init_db()
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    with session_scope() as session:
        specs = [
            ("PeakPromote1111111111111111111111111111", 50_000, 120_000, PAPER_V1_OPEN_VIA_PEAK),
            ("OrganicWin11111111111111111111111111111", 50_000, 110_000, None),
            ("OrganicLoss1111111111111111111111111111", 50_000, 80_000, None),
        ]
        closed = []
        for mint, entry, peak, via in specs:
            tok = Token(
                mint=mint[:44],
                symbol="T",
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
                    kind="entry",
                    chain="sol",
                    at=now,
                    entry_p=0.20,
                    entry_mcap=entry,
                    liq=25_000,
                    features_json='{"github_auth_n":0.9}',
                    image_rev="test",
                )
            )
            session.flush()
            dec = session.query(Decision).filter(Decision.token_id == tok.id).one()
            closed.append(
                PaperFill(
                    chain="sol",
                    mint=tok.mint,
                    token_id=tok.id,
                    decision_id=dec.id,
                    line=PAPER_V1_LINE,
                    opened_at=now,
                    closed_at=now,
                    entry_p=0.20,
                    entry_mcap=entry,
                    entry_liq=25_000,
                    max_mcap=peak,
                    min_mcap=entry,
                    last_mcap=peak,
                    last_liq=25_000,
                    status="closed",
                    exit_reason="test",
                    open_via=via,
                    image_rev="test",
                    updated_at=now,
                )
            )
        session.add_all(closed)
        session.flush()
        bar = _hit_bar(session, closed)
        assert bar["extra"]["n_peak_promoted"] == 1
        assert bar["extra"]["rate_excl_peak"] == 0.5
        assert bar["value"] == 0.5
        assert bar["extra"]["short_list"]["rate"] == round(2 / 3, 4)


def test_armed_true_is_red_and_not_ready():
    init_db()
    with session_scope() as session:
        save_risk(session, {"armed": True})
        out = production_gate_progress(session, "sol")
        risk = next(b for b in out["bars"] if b["key"] == "risk")
        assert risk["color"] == "red"
        assert risk["extra"]["armed"] is True
        assert out["ready"] is False
        assert out["arm_ui"] is False


def test_production_gate_route_is_read_only():
    from fastapi.testclient import TestClient

    from launchfinder.app import app

    init_db()
    client = TestClient(app)
    res = client.get("/api/paper/v1/production-gate?chain=sol")
    assert res.status_code == 200
    body = res.json()
    assert body["paper_only"] is True
    assert body["arm_ui"] is False
    assert body["ready"] is False
    assert len(body["bars"]) == 7


def test_stability_requires_paper_sync_not_hunt_tape_stand_in():
    init_db()
    clock = utcnow()

    def _stamp(session, key: str, at):
        row = session.query(ScanState).filter(ScanState.key == key).one_or_none()
        if row is None:
            session.add(ScanState(key=key, value="ok", updated_at=at))
        else:
            row.value = "ok"
            row.updated_at = at

    with session_scope() as session:
        session.query(ScanState).filter(ScanState.key == "loop:paper_sync").delete()
        for key in (
            "loop:hunt_tape",
            "loop:tape_refresh",
            "loop:ingest",
            "loop:batch_fit",
            "loop:fomo_trending",
            "loop:fomo_alerts",
        ):
            _stamp(session, key, clock)
        session.flush()
        out = production_gate_progress(session, "sol", now=clock)
        st = next(b for b in out["bars"] if b["key"] == "stability")
        assert st["extra"]["has_tape"] is True
        assert st["extra"]["has_paper"] is False
        assert st["color"] != "green"

    with session_scope() as session:
        for key in (
            "loop:hunt_tape",
            "loop:tape_refresh",
            "loop:ingest",
            "loop:batch_fit",
            "loop:fomo_trending",
            "loop:fomo_alerts",
            "loop:paper_sync",
        ):
            _stamp(session, key, clock)
        session.flush()
        out = production_gate_progress(session, "sol", now=clock)
        st = next(b for b in out["bars"] if b["key"] == "stability")
        assert st["color"] == "green"
        assert st["extra"]["has_paper"] is True
        assert st["extra"]["paper_via_hunt_tape"] is False


def test_hourly_fomo_loop_is_not_a_10min_stability_fail():
    from datetime import timedelta

    from launchfinder.scoring.production_gate import HOURLY_LOOPS

    assert "fomo_trending" in HOURLY_LOOPS
    init_db()
    clock = utcnow()

    def _stamp(session, key: str, value: str, at):
        row = session.query(ScanState).filter(ScanState.key == key).one_or_none()
        if row is None:
            session.add(ScanState(key=key, value=value, updated_at=at))
        else:
            row.value = value
            row.updated_at = at

    with session_scope() as session:
        _stamp(session, "loop:hunt_tape", "ok", clock)
        _stamp(session, "loop:tape_refresh", "ok", clock)
        _stamp(session, "loop:ingest", "ok", clock)
        _stamp(session, "loop:batch_fit", "ok", clock)
        _stamp(
            session,
            "loop:fomo_trending",
            "sit-out 402 · seen=None · miss=0 · hijack=0",
            clock - timedelta(seconds=1800),
        )
        _stamp(session, "loop:fomo_alerts", "ws=up · recv=0 · new=0", clock)
        session.flush()
        out = production_gate_progress(session, "sol", now=clock)
        by = {b["key"]: b for b in out["bars"]}
        stale = by["stability"]["extra"].get("stale") or []
        assert not any(s.startswith("fomo_trending:") for s in stale)
        assert by["stability"]["color"] == "green"
        session.query(ScanState).filter(
            ScanState.key.like("loop:%")
        ).delete(synchronize_session=False)


def test_desk_wires_the_gate_card():
    js = Path("launchfinder/static/desk.js").read_text()
    html = Path("launchfinder/static/desk.html").read_text()
    rh = Path("launchfinder/static/desk-rh.html").read_text()
    css = Path("launchfinder/static/desk.css").read_text()
    assert "/api/paper/v1/production-gate" in js
    assert "Production gate" in js
    assert "renderGateCard" in js
    assert "gate-card" in css
    assert IMAGE_REV in html and IMAGE_REV in rh
    assert "learn-gate" in js
