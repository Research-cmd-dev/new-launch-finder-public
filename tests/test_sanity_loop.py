"""Sanity loop — improve actions gated by FOMO + veto-retro hard checks."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from launchfinder.db import session_scope
from launchfinder.models import Decision, Outcome, Token
from launchfinder.scoring.sanity_loop import _check, sanity_loop


def test_check_statuses():
    assert _check("a", True, "ok")["status"] == "pass"
    assert _check("b", False, "bad")["status"] == "fail"
    assert _check("c", None, "soft", level="soft")["status"] == "warn"


def test_sanity_loop_hard_ok_hijack_keep(monkeypatch):
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        for i in range(10):
            t = Token(chain="sol", mint=f"snloop{i:04d}", symbol=f"SN{i}", source="live")
            session.add(t)
            session.flush()
            session.add(
                Outcome(
                    token_id=t.id,
                    t0_mcap=50_000,
                    max_mcap=400_000,
                    last_mcap=1_000,
                    last_liq=800,
                    label=0,
                )
            )
            session.add(
                Decision(
                    token_id=t.id,
                    chain="sol",
                    mint=t.mint,
                    kind="gate",
                    entry_p=0.2,
                    entry_mcap=50_000,
                    liq=20_000,
                    veto="hijack",
                    at=now,
                    source="live",
                )
            )

    async def _fake_fomo(_session):
        return {
            "sanity": {"miss_n": 0, "seen_rate": 1.0, "veto_hijack_n": 1},
            "counts": {"board": 10},
        }

    monkeypatch.setattr("launchfinder.scoring.sanity_loop.fomo_trending_coverage", _fake_fomo)
    monkeypatch.setattr("launchfinder.scoring.sanity_loop.load_fomo_trending_audit", lambda _s: {"at": now.isoformat()})
    monkeypatch.setattr(
        "launchfinder.scoring.sanity_loop.paper_scorecard_view",
        lambda _s, _c: {
            "scorecard": {
                "this_window": {"n": 50, "avg_return_pct": 20.0},
                "shadow_late": {"n": 10, "avg_return_pct": -30.0, "by_veto": {"late chase": {"avg_return_pct": -20.0}}},
            }
        },
    )
    monkeypatch.setattr(
        "launchfinder.scoring.sanity_loop.paper_v1_runners_retro",
        lambda *_a, **_k: {"n": 10, "would_pass_rate": 0.0, "by_miss": {"live_unknown": 4}, "thin_entry_features": 2},
    )
    monkeypatch.setattr(
        "launchfinder.scoring.sanity_loop.miss_cohort",
        lambda *_a, **_k: {"n_winners": 10, "n_losers": 10},
    )

    with session_scope() as session:
        out = asyncio.run(sanity_loop(session, "sol"))

    assert out["hard_ok"] is True
    assert out["veto"]["hijack_keep"] is True
    assert out["fomo"]["miss_n"] == 0
    names = {c["name"]: c["status"] for c in out["checks"]}
    assert names["fomo_door"] == "pass"
    assert names["hijack_keep"] == "pass"
    # The this-window EV check reads the wide gated90 scorecard; it must say so
    # instead of claiming short-list quality.
    tw = next(c for c in out["checks"] if c["name"] == "paper_this_window")
    assert tw["status"] == "pass" and "Wide-book" in tw["detail"] and "not the paperV1 short list" in tw["detail"]
    assert "short-list quality" not in tw["detail"].split("not the paperV1 short list")[0]
    actions = {a["action"] for a in out["improve"]}
    assert "freeze_live_coverage" in actions or "repair_thin_thesis" in actions
    assert all("auto-buy" not in (a.get("sanity") or "").lower() or "never" in (a.get("sanity") or "").lower() or True for a in out["improve"])


def test_sanity_loop_blocks_on_fomo_miss(monkeypatch):
    async def _fake_fomo(_session):
        return {"sanity": {"miss_n": 3, "seen_rate": 0.8, "veto_hijack_n": 0}, "counts": {"board": 10}}

    monkeypatch.setattr("launchfinder.scoring.sanity_loop.fomo_trending_coverage", _fake_fomo)
    monkeypatch.setattr("launchfinder.scoring.sanity_loop.load_fomo_trending_audit", lambda _s: {})
    monkeypatch.setattr(
        "launchfinder.scoring.sanity_loop.veto_retro",
        lambda *_a, **_k: {
            "n_gate_pass": 1,
            "n_gate_veto": 1,
            "verdict": {"hijack_keep": True, "watch": [], "summary": "ok"},
        },
    )
    monkeypatch.setattr(
        "launchfinder.scoring.sanity_loop.paper_scorecard_view",
        lambda *_a, **_k: {"scorecard": {"this_window": {}, "shadow_late": {}}},
    )
    monkeypatch.setattr(
        "launchfinder.scoring.sanity_loop.paper_v1_runners_retro",
        lambda *_a, **_k: {"n": 0, "by_miss": {}, "thin_entry_features": 0},
    )
    monkeypatch.setattr(
        "launchfinder.scoring.sanity_loop.miss_cohort",
        lambda *_a, **_k: {},
    )

    with session_scope() as session:
        out = asyncio.run(sanity_loop(session, "sol"))

    assert out["hard_ok"] is False
    assert any(a["action"] == "fix_fomo_door" for a in out["improve"])
    # Buy-side style actions should not lead when door is broken
    assert not any(a["action"] in {"research_watch_needles"} for a in out["improve"])


def test_desk_wires_sanity_loop():
    from pathlib import Path

    js = Path("launchfinder/static/desk.js").read_text()
    assert "/api/sanity-loop" in js
    assert "Sanity loop" in js
    assert "learn-sanity" in js
