"""North-star first-book tapes. Not labels. Does not touch FEATURE_NAMES.

PONS at $410M on Sep 15 is a leftover Decision (legacy 0.2167, 24h loss).
The July 13 first day — $2.6k → $272k close, 106×, $565k vol — is the
example to follow. Desk-caught fills (DEX / MeiMei / POOF) stay in the
same basket so loops compare live winners to that shape.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_PATH = Path(__file__).with_name("exemplars.json")


@lru_cache(maxsize=1)
def load_exemplars() -> dict[str, Any]:
    return json.loads(_PATH.read_text())


def historical() -> list[dict[str, Any]]:
    return list(load_exemplars().get("historical") or [])


def desk_caught() -> list[dict[str, Any]]:
    return list(load_exemplars().get("desk_caught") or [])


def incredible() -> list[dict[str, Any]]:
    """Optional curated incredible-return rows (empty until hand-curated)."""
    return list(load_exemplars().get("incredible") or [])


def pons_first_day() -> dict[str, Any]:
    for row in historical():
        if row.get("id") == "pons-first-day":
            return row
    return {}


def follow_lessons() -> list[str]:
    """What the next scoring loop should look for. Not new gates."""
    pons = pons_first_day()
    return (
        list(pons.get("follow") or [])
        + list(load_exemplars().get("do_not") or [])
        + list(load_exemplars().get("alpha_extract") or [])
    )
