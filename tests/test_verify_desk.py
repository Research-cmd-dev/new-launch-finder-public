"""Scorecard predicates for the four desk claims — no live HTTP."""

from datetime import datetime, timedelta, timezone

from scripts.verify_desk import (
    Scorecard,
    _bar_age_s,
    _empty_tape_reason,
    _frozen_entry,
    _leak_line,
    _pattern,
    _pick_named,
)


def test_pattern_hits_two_tick_collapsed_phantom_on_sol():
    two = _pattern(
        {
            "chain": "sol",
            "t0_mcap": 80_000,
            "last_mcap": 82_000,
            "max_mcap": 200_000,
            "risk_flags": [],
        }
    )
    assert "two-tick" in two
    collapsed = _pattern({"chain": "sol", "t0_mcap": 80_000, "last_mcap": 20_000, "max_mcap": 80_000})
    assert "collapsed" in collapsed
    phantom = _pattern({"chain": "sol", "t0_mcap": 600_000, "last_mcap": 10_000, "max_mcap": 600_000})
    assert "phantom" in phantom


def test_nina_held_book_is_not_two_tick():
    nina = _pattern(
        {
            "chain": "sol",
            "t0_mcap": 43_000,
            "last_mcap": 431_000,
            "max_mcap": 112_000,
            "risk_flags": ["sniper"],
        }
    )
    assert nina == []


def test_leak_line_is_the_current_buy_line():
    assert _leak_line({"scorer": "first_sight", "chain": "sol"}) == 0.14
    assert _leak_line({"scorer": "first_sight", "chain": "robinhood"}) == 0.30
    assert _leak_line({"scorer": "legacy", "chain": "sol"}) == 0.90


def test_pick_named_takes_fat_first_sight_and_a_dump():
    fat = {
        "symbol": "FAT",
        "mint": "FatMint",
        "scorer": "first_sight",
        "last_liq": 20_000,
        "holder_count": 80,
        "entry_p": 0.22,
        "t0_mcap": 80_000,
        "last_mcap": 90_000,
    }
    dump = {
        "symbol": "DMP",
        "mint": "DumpMint",
        "scorer": "legacy",
        "last_liq": 2_000,
        "holder_count": 12,
        "entry_p": 0.91,
        "t0_mcap": 80_000,
        "last_mcap": 12_000,
    }
    dust = {
        "symbol": "DST",
        "mint": "DustMint",
        "scorer": "first_sight",
        "last_liq": 1_000,
        "holder_count": 3,
        "entry_p": 0.28,
        "t0_mcap": 8_000,
        "last_mcap": 7_000,
    }
    picked = _pick_named([dust, dump, fat])
    assert [c["symbol"] for c in picked] == ["FAT", "DMP"]


def test_frozen_entry_prefers_ledger_over_faded_p_good():
    p, src = _frozen_entry(
        {
            "entry_p": 0.24,
            "p_good": 0.0699,
            "ledger": {"entry": {"entry_p": 0.0699, "scorer": "first_sight"}},
        }
    )
    assert src == "ledger" and abs(p - 0.0699) < 1e-9
    p, src = _frozen_entry({"entry_p": 0.22, "p_good": 0.22})
    assert src == "token" and abs(p - 0.22) < 1e-9


def test_bar_age_and_scorecard_fail_flag():
    now = datetime.now(timezone.utc)
    fresh = {"bars": [{"t": (now - timedelta(seconds=20)).isoformat(), "mcap": 1}]}
    stale = {"bars": [{"t": (now - timedelta(minutes=10)).isoformat(), "mcap": 1}]}
    assert _bar_age_s(fresh) < 60
    assert _bar_age_s(stale) > 180
    assert _bar_age_s({"bars": []}) is None
    card = Scorecard()
    card.ok("tape", "heartbeat")
    assert not card.failed
    card.fail("paper", "Sol hi is 0.15")
    assert card.failed
    dumped = card.dump()
    assert dumped["rows"][-1]["status"] == "FAIL"
    assert dumped["rows"][-1]["claim"] == "paper"


def test_empty_tape_reason_exempts_dead_not_rob_class():
    assert _empty_tape_reason({"last_mcap": 0, "last_liq": 0}) == "no live last/liq — leftover not taped"
    assert _empty_tape_reason({"last_mcap": 15_143_643, "last_liq": 250_591}) is None
    assert _empty_tape_reason({"last_mcap": 2_202_920, "last_liq": 516_424}) is None
