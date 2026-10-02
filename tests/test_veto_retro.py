"""Veto health retro — historical keep/refuse feedback, not a buy list."""

from __future__ import annotations

from datetime import datetime, timezone

from launchfinder.db import session_scope
from launchfinder.models import Decision, Outcome, PaperFill, Token
from launchfinder.scoring.paper_gate import PAPER_HARD_NEEDLES, PAPER_SHADOW_VETOES
from launchfinder.scoring.veto_retro import WATCH_HELD_RATE, WATCH_MIN_PEAK5, _cls, veto_retro


def test_veto_classes():
    assert _cls("hijack") == "hard"
    assert _cls("late chase") == "soft_shadow"
    assert _cls("start-high") == "soft_shadow"
    assert _cls("two-tick") == "tape"
    assert "hijack" in PAPER_HARD_NEEDLES
    assert "late chase" in PAPER_SHADOW_VETOES


def test_veto_retro_hijack_dust_keeps():
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        for i in range(10):
            t = Token(chain="sol", mint=f"hijackdust{i:04d}", symbol=f"HD{i}", source="live")
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
        tpass = Token(chain="sol", mint="passgate0001", symbol="OK", source="live")
        session.add(tpass)
        session.flush()
        session.add(
            Outcome(
                token_id=tpass.id,
                t0_mcap=60_000,
                max_mcap=180_000,
                last_mcap=120_000,
                last_liq=30_000,
                label=1,
            )
        )
        session.add(
            Decision(
                token_id=tpass.id,
                chain="sol",
                mint=tpass.mint,
                kind="gate",
                entry_p=0.3,
                entry_mcap=60_000,
                liq=25_000,
                veto="",
                at=now,
                source="live",
            )
        )

    with session_scope() as session:
        out = veto_retro(session, "sol")
        assert out["n_gate_pass"] == 1
        assert out["n_gate_veto"] == 10
        assert out["verdict"]["hijack_keep"] is True
        hj = next(n for n in out["needles"] if n["veto"] == "hijack")
        assert hj["hit5_t0"] == 10
        assert hj["peak5_then_dust"] == 10
        assert hj["peak5_held_sellable"] == 0
        assert (hj["peak5_held_sellable_rate"] or 0) < WATCH_HELD_RATE
        assert hj["keep"] is True


def test_veto_retro_soft_shadow_rollup():
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        t = Token(chain="sol", mint="softlate0001", symbol="SL", source="live")
        session.add(t)
        session.flush()
        session.add(
            Outcome(
                token_id=t.id,
                t0_mcap=40_000,
                max_mcap=200_000,
                last_mcap=10_000,
                last_liq=2_000,
                label=0,
            )
        )
        d = Decision(
            token_id=t.id,
            chain="sol",
            mint=t.mint,
            kind="gate",
            entry_p=0.25,
            entry_mcap=120_000,
            liq=40_000,
            veto="late chase",
            at=now,
            source="live",
        )
        session.add(d)
        session.flush()
        session.add(
            PaperFill(
                chain="sol",
                mint=t.mint,
                token_id=t.id,
                decision_id=d.id,
                line="shadow_late",
                opened_at=now,
                entry_p=0.25,
                entry_mcap=120_000,
                entry_liq=40_000,
                target=2.0,
                ride=10.0,
                max_mcap=130_000,
                min_mcap=80_000,
                last_mcap=90_000,
                last_liq=10_000,
                status="closed",
                return_pct=-40.0,
                exit_reason="shadow",
                closed_at=now,
            )
        )

    with session_scope() as session:
        out = veto_retro(session, "sol")
        late = next(n for n in out["needles"] if n["veto"] == "late chase")
        assert late["class"] == "soft_shadow"
        assert late["shadow"]["n"] == 1
        assert late["shadow"]["avg_return_pct"] == -40.0


def test_desk_wires_veto_retro():
    from pathlib import Path

    js = Path("launchfinder/static/desk.js").read_text()
    assert "/api/paper/veto-retro" in js
    assert "Veto health" in js
    assert "learn-veto" in js
