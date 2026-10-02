"""Honest paper scorecard: this-window exits vs leftover-clock 24h.

this-window = live dump + ride (the desk acted on the tape).
leftover-clock = 24h close (the hold timer — leftover drag).
no-run = never printed 1.5× and gave back half of peak after 1h
(CATFLIX / fomocoin / Prism). dead pool is its own bucket. Opens
split by buy line vs watch. FEATURE_NAMES stays 66. Does not rewrite
entry p_good.
"""

from __future__ import annotations

from typing import Any, Iterable

THIS_WINDOW_EXITS = frozenset({"live dump", "ride"})
LEFTOVER_CLOCK_EXITS = frozenset({"24h"})
NO_RUN_EXITS = frozenset({"no run"})


def _attr(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def _bucket(rows: list[Any], *, target: float) -> dict[str, Any]:
    n = len(rows)
    rets = [float(_attr(r, "return_pct") or 0.0) for r in rows]
    wins = 0
    for row in rows:
        entry = float(_attr(row, "entry_mcap") or 0.0)
        peak = float(_attr(row, "max_mcap") or 0.0)
        tgt = float(_attr(row, "target") or target)
        if entry > 0 and peak >= tgt * entry:
            wins += 1
    return {
        "n": n,
        "wins": wins,
        "avg_return_pct": round(sum(rets) / n, 1) if n else None,
        "total_return_pct": round(sum(rets), 1) if n else 0.0,
    }


def _line_slot(entry_p: float, lo: float, hi: float) -> str:
    if entry_p >= hi:
        return "hi"
    if entry_p >= lo:
        return "watch"
    return "under"


def paper_scorecard(fills: Iterable[Any], *, lines: Any, target: float = 2.0) -> dict[str, Any]:
    """Split closed fills by exit and opens by desk line.

    ``lines`` is a DeskLines (or anything with ``lo`` / ``hi`` / ``scorer``).
    this-window avg is the live-dump + ride book. leftover-clock avg is
    the 24h timer. no-run is the earlier grave exit (never 1.5×). Header
    and Board read these separately so leftover drag does not hide the
    tape exits. ``this_window_by_line`` is that acted-on book split by
    buy line vs watch — the paper EV. It is not a training label and it
    does not move the lines.
    """
    lo = float(getattr(lines, "lo", 0.0) or 0.0)
    hi = float(getattr(lines, "hi", 0.0) or 0.0)
    scorer = str(getattr(lines, "scorer", "") or "")
    fills = list(fills)
    closed = [f for f in fills if str(_attr(f, "status") or "") == "closed"]
    opens = [f for f in fills if str(_attr(f, "status") or "") == "open"]

    by_exit: dict[str, list[Any]] = {}
    for fill in closed:
        reason = str(_attr(fill, "exit_reason") or "") or "unknown"
        by_exit.setdefault(reason, []).append(fill)

    this_window = [f for f in closed if str(_attr(f, "exit_reason") or "") in THIS_WINDOW_EXITS]
    leftover = [f for f in closed if str(_attr(f, "exit_reason") or "") in LEFTOVER_CLOCK_EXITS]
    no_run = [f for f in closed if str(_attr(f, "exit_reason") or "") in NO_RUN_EXITS]
    dead = [f for f in closed if str(_attr(f, "exit_reason") or "") == "dead pool"]

    def by_line(rows: list[Any]) -> dict[str, dict[str, Any]]:
        slots: dict[str, list[Any]] = {"hi": [], "watch": [], "under": []}
        for fill in rows:
            slots[_line_slot(float(_attr(fill, "entry_p") or 0.0), lo, hi)].append(fill)
        return {key: _bucket(val, target=target) for key, val in slots.items()}

    open_hi = [f for f in opens if float(_attr(f, "entry_p") or 0.0) >= hi]
    open_watch = [f for f in opens if lo <= float(_attr(f, "entry_p") or 0.0) < hi]

    return {
        "scorer": scorer,
        "lo": lo,
        "hi": hi,
        "this_window": {**_bucket(this_window, target=target), "exits": sorted(THIS_WINDOW_EXITS)},
        "leftover_clock": {**_bucket(leftover, target=target), "exits": sorted(LEFTOVER_CLOCK_EXITS)},
        "no_run": {**_bucket(no_run, target=target), "exits": sorted(NO_RUN_EXITS)},
        "dead_pool": _bucket(dead, target=target),
        "by_exit": {reason: _bucket(rows, target=target) for reason, rows in sorted(by_exit.items())},
        "open": {
            "n": len(opens),
            "hi_n": len(open_hi),
            "watch_n": len(open_watch),
        },
        "closed_by_line": by_line(closed),
        "this_window_by_line": by_line(this_window),
        "open_by_line": by_line(opens),
        "buckets": [
            {
                "bin": "watch",
                "lo": lo,
                "hi": hi,
                **_bucket(
                    [f for f in fills if lo <= float(_attr(f, "entry_p") or 0.0) < hi],
                    target=target,
                ),
            },
            {
                "bin": "hi",
                "lo": hi,
                "hi": 1.0,
                **_bucket(
                    [f for f in fills if float(_attr(f, "entry_p") or 0.0) >= hi],
                    target=target,
                ),
            },
        ],
    }
