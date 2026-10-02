"""Liquidity behavior — depth builds steadily vs pulled on upticks.

SI study: rising book needs mcap+liq up together; dump-on-volume duds
print vol while mcap collapses. Farming often pulls liq as price ticks up.
Learn/shadow only — not a fill gate.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..models import Token
from .early_book import _ratio, tape_minute_trajectory

UPTICK_MCAP = 1.03  # 3% mcap tick up between bars
PULL_LIQ = 0.97  # liq drops while mcap up
BUILD_LIQ = 1.02  # liq rises with mcap


def liq_path_from_tape(tape: list[dict[str, Any]]) -> dict[str, Any]:
    """Score build-vs-pull from minute bars."""
    if len(tape) < 2:
        return {
            "n_bars": len(tape),
            "upticks": 0,
            "pull_on_uptick": 0,
            "build_on_uptick": 0,
            "pull_rate": None,
            "build_rate": None,
            "liq_t0": None,
            "liq_last": None,
            "liq_ratio": None,
            "label": "insufficient",
            "build_score": 0.0,
            "farm_score": 0.0,
        }

    upticks = pull = build = 0
    for i in range(1, len(tape)):
        prev, cur = tape[i - 1], tape[i]
        m0, m1 = float(prev.get("mcap") or 0), float(cur.get("mcap") or 0)
        l0, l1 = float(prev.get("liq") or 0), float(cur.get("liq") or 0)
        if m0 <= 0 or l0 <= 0:
            continue
        if m1 >= m0 * UPTICK_MCAP:
            upticks += 1
            if l1 <= l0 * PULL_LIQ:
                pull += 1
            elif l1 >= l0 * BUILD_LIQ:
                build += 1

    l_first = float(tape[0].get("liq") or 0)
    l_last = float(tape[-1].get("liq") or 0)
    liq_ratio = _ratio(l_last, l_first) if l_first > 0 else None
    pull_rate = (pull / upticks) if upticks else None
    build_rate = (build / upticks) if upticks else None

    build_score = 0.0
    farm_score = 0.0
    label = "flat"
    if liq_ratio is not None and liq_ratio >= 1.1 and (build_rate or 0) >= 0.4:
        label = "building"
        build_score = min(1.0, 0.4 + 0.3 * float(liq_ratio - 1.0) + 0.4 * float(build_rate or 0))
    elif pull_rate is not None and pull_rate >= 0.5 and upticks >= 2:
        label = "pulled_on_uptick"
        farm_score = min(1.0, 0.3 + 0.7 * float(pull_rate))
    elif liq_ratio is not None and liq_ratio < 0.7:
        label = "liq_draining"
        farm_score = min(1.0, 0.5 + 0.5 * (1.0 - float(liq_ratio)))
    elif liq_ratio is not None and liq_ratio >= 1.05:
        label = "steady_build"
        build_score = min(1.0, 0.35 + 0.25 * float(liq_ratio - 1.0))

    return {
        "n_bars": len(tape),
        "upticks": upticks,
        "pull_on_uptick": pull,
        "build_on_uptick": build,
        "pull_rate": round(pull_rate, 3) if pull_rate is not None else None,
        "build_rate": round(build_rate, 3) if build_rate is not None else None,
        "liq_t0": l_first or None,
        "liq_last": l_last or None,
        "liq_ratio": round(liq_ratio, 4) if liq_ratio is not None else None,
        "label": label,
        "build_score": round(build_score, 3),
        "farm_score": round(farm_score, 3),
    }


def liq_behavior(
    session: Session,
    token: Token,
    *,
    minutes: int = 60,
) -> dict[str, Any]:
    """Side-key liquidity path for Learn/shadow."""
    tape = tape_minute_trajectory(session, token.chain, token.mint, minutes=minutes)
    path = liq_path_from_tape(tape)
    return {
        **path,
        "paper_only": True,
        "side_key": "liq_behavior",
        "note": "building = depth rises with mcap; pulled_on_uptick ≈ farming",
    }
