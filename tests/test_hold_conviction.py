"""Hold conviction — post-buy exit layer; entry lines unchanged."""

from __future__ import annotations

from launchfinder.desk_lines import lines_for_scorer
from launchfinder.scoring.hold_conviction import (
    HOLD_CONVICTION_HIGH,
    paper_live_dump_with_hold,
    paper_no_run_with_hold,
)
from launchfinder.scoring.meme_quality import meme_quality_score
from launchfinder.scoring.paper_gate import PAPER_SHADOW_VETOES, paper_live_dump


def test_sol_entry_line_unchanged():
    lines = lines_for_scorer("first_sight", "sol")
    assert round(lines.hi, 2) == 0.14


def test_rh_entry_line_unchanged():
    lines = lines_for_scorer("first_sight", "robinhood")
    assert round(lines.hi, 2) == 0.30


def test_copycat_spam_stays_hard_for_hold_meme_path():
    assert "copycat spam" not in PAPER_SHADOW_VETOES
    out = meme_quality_score(
        {"name_quality": 0.8, "volume_n": 0.7},
        risk_flags=["copycat spam"],
    )
    assert out["eligible"] is False
    assert out["score"] == 0.0


def test_high_hold_suppresses_borderline_live_dump():
    entry = 100_000.0
    peak = 200_000.0
    last = 99_000.0
    live = 0.30
    base = paper_live_dump(entry_mcap=entry, peak_mcap=peak, last_mcap=last, live_p=live)
    assert base is True
    adj = paper_live_dump_with_hold(
        entry_mcap=entry,
        peak_mcap=peak,
        last_mcap=last,
        live_p=live,
        hold_conviction=HOLD_CONVICTION_HIGH,
    )
    assert adj is False


def test_low_hold_tightens_live_dump():
    entry = 100_000.0
    peak = 160_000.0
    last = 70_000.0
    live = 0.38
    base = paper_live_dump(entry_mcap=entry, peak_mcap=peak, last_mcap=last, live_p=live)
    assert base is False
    adj = paper_live_dump_with_hold(
        entry_mcap=entry,
        peak_mcap=peak,
        last_mcap=last,
        live_p=live,
        hold_conviction=0.2,
    )
    assert adj is True


def test_low_hold_tightens_no_run():
    assert paper_no_run_with_hold(
        entry_mcap=100_000,
        peak_mcap=120_000,
        last_mcap=60_000,
        age_hours=0.8,
        hold_conviction=0.2,
    )
