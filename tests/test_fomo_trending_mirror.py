"""FOMO trending API mirror staleness — desk sanity gate."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from launchfinder.research import fomo_api
from launchfinder.research.fomo_api import (
    FOMO_TRENDING_CAPTURE_BUDGET_S,
    TrendingFetchResult,
    parse_trending_upstream_meta,
)


def test_parse_upstream_stale_when_capture_two_hours_old():
    now = datetime(2026, 9, 30, 19, 50, tzinfo=timezone.utc)
    payload = {
        "source": "captured",
        "capturedAt": "2026-09-30T17:49:10Z",
        "ageHours": 2,
        "stale": False,
        "tokens": [],
    }
    meta = parse_trending_upstream_meta(payload, now=now)
    assert meta["board_stale"] is True
    assert meta["api_source"] == "captured"
    assert meta["capture_age_hours"] is not None
    assert meta["capture_age_hours"] > FOMO_TRENDING_CAPTURE_BUDGET_S / 3600.0
    assert "stale" in (meta.get("mirror_note") or "").lower()


def test_parse_upstream_fresh_when_capture_recent():
    now = datetime(2026, 9, 30, 19, 50, tzinfo=timezone.utc)
    payload = {
        "source": "captured",
        "capturedAt": "2026-09-30T19:40:00Z",
        "ageHours": 0.17,
        "stale": False,
        "tokens": [],
    }
    meta = parse_trending_upstream_meta(payload, now=now)
    assert meta["board_stale"] is False


def test_fresh_local_snap_still_fetches_and_surfaces_stale_upstream(monkeypatch):
    """BANDIT class: young desk snap must not mask hours-old FOMO API capture."""
    import launchfinder.research.fomo_coverage as cov

    captured = datetime(2026, 9, 30, 17, 49, 10, tzinfo=timezone.utc)
    fetch_calls = 0

    async def fake_fetch(**_kw):
        nonlocal fetch_calls
        fetch_calls += 1
        return TrendingFetchResult(
            rows=[{"mint": "bandit_mint", "chain": "sol", "symbol": "BANDIT"}],
            upstream=parse_trending_upstream_meta(
                {
                    "source": "captured",
                    "capturedAt": captured.isoformat().replace("+00:00", "Z"),
                    "ageHours": 2,
                    "stale": False,
                },
                now=datetime(2026, 9, 30, 19, 50, tzinfo=timezone.utc),
            ),
        )

    monkeypatch.setattr(cov, "fetch_trending", fake_fetch)
    snap = {
        "at": datetime(2026, 9, 30, 19, 45, tzinfo=timezone.utc).isoformat(),
        "items": [{"mint": "stale_snap_only", "chain": "sol", "symbol": "SI"}],
    }

    async def _run():
        rows, meta, persist = await cov._resolve_trending_board(snap)
        assert fetch_calls == 1
        assert rows[0]["symbol"] == "BANDIT"
        assert meta.get("board_stale") is True
        assert meta.get("capture_age_hours") is not None
        assert meta.get("source") == "fomo_api_capture_stale"
        assert persist is None

    asyncio.run(_run())


def test_resolve_trending_board_marks_stale_and_skips_persist(monkeypatch):
    import launchfinder.research.fomo_coverage as cov

    captured = datetime(2026, 9, 30, 17, 49, 10, tzinfo=timezone.utc)

    async def fake_fetch(**_kw):
        return TrendingFetchResult(
            rows=[{"mint": "m1", "chain": "sol", "symbol": "X"}],
            upstream=parse_trending_upstream_meta(
                {
                    "source": "captured",
                    "capturedAt": captured.isoformat().replace("+00:00", "Z"),
                    "ageHours": 2,
                    "stale": False,
                },
                now=datetime(2026, 9, 30, 19, 50, tzinfo=timezone.utc),
            ),
        )

    monkeypatch.setattr(cov, "fetch_trending", fake_fetch)

    async def _run():
        rows, meta, persist = await cov._resolve_trending_board(None)
        assert len(rows) == 1
        assert meta.get("source") == "fomo_api_capture_stale"
        assert meta.get("board_stale") is True
        assert persist is None

    asyncio.run(_run())


def test_graduated_secondary_drives_sanity_when_trending_stale(monkeypatch):
    import launchfinder.research.fomo_coverage as cov
    from launchfinder.db import init_db, SessionLocal

    init_db()
    captured = datetime(2026, 9, 30, 17, 49, 10, tzinfo=timezone.utc)

    async def fake_trending(**_kw):
        return TrendingFetchResult(
            rows=[{"mint": "trend_m", "chain": "sol", "symbol": "OLD"}],
            upstream=parse_trending_upstream_meta(
                {
                    "source": "captured",
                    "capturedAt": captured.isoformat().replace("+00:00", "Z"),
                    "ageHours": 2,
                },
                now=datetime(2026, 9, 30, 19, 50, tzinfo=timezone.utc),
            ),
        )

    async def fake_graduated(**_kw):
        return TrendingFetchResult(
            rows=[{"mint": "bandit_m", "chain": "sol", "symbol": "BANDIT"}],
            upstream={
                "api_source": "live-fomo",
                "board_stale": False,
                "capture_age_hours": 0.05,
                "board_note": "graduated",
            },
        )

    monkeypatch.setattr(cov, "fetch_trending", fake_trending)
    monkeypatch.setattr(cov, "fetch_graduated", fake_graduated)

    async def _empty_gmgn():
        return [], {"api_source": "gmgn", "board_live": False, "skipped": True}

    async def _empty_dex():
        return [], {"api_source": "dexscreener", "board_live": False}

    monkeypatch.setattr(cov, "_fetch_gmgn_trending_secondary", _empty_gmgn)
    monkeypatch.setattr(cov, "_fetch_dexscreener_trending_secondary", _empty_dex)

    async def fake_classify(_u):
        return {}

    monkeypatch.setattr(cov, "_classify_unknown", fake_classify)

    async def _run():
        db = SessionLocal()
        try:
            card = await cov.fomo_trending_coverage(db)
        finally:
            db.close()
        assert card.get("board_stale") is True
        assert card.get("sanity_board") == "secondary_boards"
        assert "graduated" in (card.get("secondary_sanity_sources") or [])
        grad = card.get("graduated_board") or {}
        assert (grad.get("items") or [])[0]["symbol"] == "BANDIT"
        assert card["counts"]["board"] == 1
        assert (card.get("trending_items") or [])[0]["symbol"] == "OLD"

    asyncio.run(_run())


def test_sanity_loop_hard_fails_on_stale_mirror(monkeypatch):
    from launchfinder.db import init_db, session_scope
    from launchfinder.scoring.sanity_loop import sanity_loop

    init_db()

    async def fake_fomo(_session):
        return {
            "board_stale": True,
            "capture_age_hours": 2.0,
            "sanity": {"miss_n": 0, "seen_rate": 1.0, "board_stale": True},
        }

    monkeypatch.setattr("launchfinder.scoring.sanity_loop.fomo_trending_coverage", fake_fomo)
    monkeypatch.setattr("launchfinder.scoring.sanity_loop.load_fomo_trending_audit", lambda _s: {})
    monkeypatch.setattr("launchfinder.scoring.sanity_loop.veto_retro", lambda _s, _c: {"verdict": {"hijack_keep": True, "watch": []}})
    monkeypatch.setattr(
        "launchfinder.scoring.sanity_loop.paper_scorecard_view",
        lambda _s, _c: {"scorecard": {"this_window": {"n": 0}, "shadow_late": {}}},
    )
    monkeypatch.setattr(
        "launchfinder.scoring.sanity_loop.paper_v1_runners_retro",
        lambda *_a, **_k: {"by_miss": {}, "thin_entry_features": 0},
    )
    monkeypatch.setattr("launchfinder.scoring.sanity_loop.miss_cohort", lambda *_a, **_k: {})

    async def _run():
        with session_scope() as session:
            out = await sanity_loop(session, "sol")
            names = [c["name"] for c in out["checks"]]
            assert "fomo_mirror" in names
            mirror = next(c for c in out["checks"] if c["name"] == "fomo_mirror")
            assert mirror["status"] == "fail"
            assert out["hard_ok"] is False

    asyncio.run(_run())


def test_secondary_gmgn_and_dex_exposed_when_stale(monkeypatch):
    import launchfinder.research.fomo_coverage as cov
    from launchfinder.db import init_db, SessionLocal

    init_db()
    captured = datetime(2026, 9, 30, 17, 49, 10, tzinfo=timezone.utc)

    async def fake_trending(**_kw):
        return TrendingFetchResult(
            rows=[{"mint": "fomo_a", "chain": "sol", "symbol": "A"}],
            upstream=parse_trending_upstream_meta(
                {
                    "source": "captured",
                    "capturedAt": captured.isoformat().replace("+00:00", "Z"),
                    "ageHours": 2,
                },
                now=datetime(2026, 9, 30, 19, 50, tzinfo=timezone.utc),
            ),
        )

    async def fake_graduated(**_kw):
        return TrendingFetchResult(rows=[], upstream={"api_source": "live-fomo", "board_live": False})

    async def fake_gmgn():
        return (
            [{"mint": "gmgn_b", "chain": "sol", "symbol": "GB", "rank": 1}],
            {"api_source": "gmgn", "board_live": True, "captured_at": "2026-09-30T19:50:00Z"},
        )

    async def fake_dex():
        return (
            [{"mint": "dex_c", "chain": "sol", "symbol": "DC", "rank": 1}],
            {"api_source": "dexscreener", "board_live": True, "captured_at": "2026-09-30T19:50:00Z"},
        )

    monkeypatch.setattr(cov, "fetch_trending", fake_trending)
    monkeypatch.setattr(cov, "fetch_graduated", fake_graduated)
    monkeypatch.setattr(cov, "_fetch_gmgn_trending_secondary", fake_gmgn)
    monkeypatch.setattr(cov, "_fetch_dexscreener_trending_secondary", fake_dex)

    async def fake_classify(_u):
        return {}

    monkeypatch.setattr(cov, "_classify_unknown", fake_classify)

    async def _run():
        db = SessionLocal()
        try:
            card = await cov.fomo_trending_coverage(db)
        finally:
            db.close()
        gmgn = card.get("gmgn_trending_board") or {}
        dex = card.get("dexscreener_trending_board") or {}
        assert gmgn.get("board_live") is True
        assert (gmgn.get("items") or [])[0]["symbol"] == "GB"
        assert dex.get("board_live") is True
        assert card.get("sanity_board") == "secondary_boards"
        assert "gmgn_trending" in (card.get("secondary_sanity_sources") or [])
        assert card.get("secondary_overlap", {}).get("fomo_trending") == 1

    asyncio.run(_run())


def test_fomo_mirror_bar_still_red_when_only_gmgn_live():
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import ScanState
    from launchfinder.research.fomo_coverage import AUDIT_KEY
    from launchfinder.scoring.production_gate import _fomo_mirror_bar

    init_db()
    with session_scope() as session:
        row = session.query(ScanState).filter(ScanState.key == AUDIT_KEY).one_or_none()
        payload = '{"board_stale": true, "capture_age_hours": 2.0, "api_source": "captured", "gmgn_trending_board": {"board_live": true}}'
        if row is None:
            session.add(ScanState(key=AUDIT_KEY, value=payload))
        else:
            row.value = payload
        session.commit()
        bar = _fomo_mirror_bar(session)
    assert bar["value"] == "stale"
    assert bar["color"] == "red"
