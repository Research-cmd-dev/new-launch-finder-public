from types import SimpleNamespace

from launchfinder.desk_lines import FIRST_SIGHT_LINES, FIRST_SIGHT_RH_LINES
from launchfinder.scoring.paper_scorecard import paper_scorecard


def _fill(*, status, exit_reason="", entry_p=0.14, return_pct=0.0, entry_mcap=70_000, max_mcap=70_000, target=2.0):
    return SimpleNamespace(
        status=status,
        exit_reason=exit_reason,
        entry_p=entry_p,
        return_pct=return_pct,
        entry_mcap=entry_mcap,
        max_mcap=max_mcap,
        target=target,
    )


def test_scorecard_splits_this_window_from_leftover_clock():
    fills = [
        _fill(status="closed", exit_reason="live dump", entry_p=0.1465, return_pct=38.7, max_mcap=105_000),
        _fill(status="closed", exit_reason="ride", entry_p=0.1667, return_pct=500.0, max_mcap=868_000),
        _fill(status="closed", exit_reason="24h", entry_p=0.12, return_pct=-89.6, max_mcap=70_000),
        _fill(status="closed", exit_reason="24h", entry_p=0.1918, return_pct=-94.2, max_mcap=80_000),
        _fill(status="closed", exit_reason="no run", entry_p=0.1928, return_pct=-99.0, max_mcap=164_986),
        _fill(status="closed", exit_reason="dead pool", entry_p=0.14, return_pct=-85.0, max_mcap=70_000),
        _fill(status="open", entry_p=0.1465, max_mcap=180_000),
        _fill(status="open", entry_p=0.12, max_mcap=90_000),
    ]
    card = paper_scorecard(fills, lines=FIRST_SIGHT_LINES, target=2.0)
    assert card["this_window"]["n"] == 2
    assert card["this_window"]["avg_return_pct"] == 269.4
    assert card["this_window"]["wins"] == 1
    assert card["leftover_clock"]["n"] == 2
    assert card["leftover_clock"]["avg_return_pct"] == -91.9
    assert card["no_run"]["n"] == 1
    assert card["no_run"]["avg_return_pct"] == -99.0
    assert card["dead_pool"]["n"] == 1
    assert card["open"] == {"n": 2, "hi_n": 1, "watch_n": 1}
    assert card["closed_by_line"]["hi"]["n"] == 5
    assert card["closed_by_line"]["watch"]["n"] == 1
    assert card["this_window_by_line"]["hi"]["n"] == 2
    assert card["this_window_by_line"]["hi"]["avg_return_pct"] == 269.4
    assert card["this_window_by_line"]["watch"]["n"] == 0
    assert [b["bin"] for b in card["buckets"]] == ["watch", "hi"]


def test_scorecard_rh_watch_bucket_uses_025_030():
    fills = [
        _fill(status="open", entry_p=0.2894),
        _fill(status="open", entry_p=0.36),
        _fill(status="closed", exit_reason="live dump", entry_p=0.27, return_pct=-11.5, max_mcap=125_000),
    ]
    card = paper_scorecard(fills, lines=FIRST_SIGHT_RH_LINES, target=2.0)
    assert card["lo"] == 0.25 and card["hi"] == 0.30
    assert card["open"]["watch_n"] == 1 and card["open"]["hi_n"] == 1
    assert card["this_window"]["n"] == 1
    assert card["this_window_by_line"]["watch"]["n"] == 1
    assert card["this_window_by_line"]["watch"]["avg_return_pct"] == -11.5
    assert card["this_window_by_line"]["hi"]["n"] == 0
    assert card["leftover_clock"]["n"] == 0
    assert card["leftover_clock"]["avg_return_pct"] is None
