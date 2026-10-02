"""Incredible-return templates: confirmed runners + first-sight features.

Alpha lives only in features visible at first print — not in knowing the
symbol later ran. This module builds a review template from our ledger
(confirmed runners), merges curated exemplars, and summarizes extractable
patterns. Not training labels. Does not rewrite Decisions.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy.orm import Session

from ..chains import normalize_chain
from .exemplars import desk_caught, historical, load_exemplars
from .runners_retro import paper_v1_runners_retro

# Honest horizon: our DB does not hold a clean 365-day Sol/RH first-book
# archive. Template = confirmed runners in ledger + curated north stars.
ALPHA_EXTRACT = [
    "Use first-sight / entry Decision features only — never peak multiple as a feature.",
    "Compare tag rates (github / dev / cto / meme) on winners vs same-window misses.",
    "Check Entry calibration: did high entry_p names actually clear 5× more often?",
    "Early-wallet / FOMO overlap on winners is a research signal, not a gate.",
    "Survivorship: a 1y winner list without the losers of that day invents alpha.",
    "Do not buy ticker rhyme or 'similar name' — that is not extractable edge.",
]


def curated_incredible() -> list[dict[str, Any]]:
    """Static north stars from exemplars.json (PONS first day + desk catches)."""
    rows: list[dict[str, Any]] = []
    for row in historical():
        rows.append(
            {
                "id": row.get("id"),
                "symbol": row.get("symbol"),
                "mint": row.get("mint"),
                "chain": row.get("chain"),
                "source": "historical_exemplar",
                "multiple": (row.get("first_day") or {}).get("multiple_high")
                or (row.get("milestones") or {}).get("multiple_open_to_ath"),
                "why": row.get("why"),
                "follow": row.get("follow") or [],
            }
        )
    for row in desk_caught():
        rows.append(
            {
                "id": row.get("id"),
                "symbol": row.get("symbol"),
                "mint": row.get("mint"),
                "chain": row.get("chain"),
                "source": "desk_caught",
                "multiple": row.get("peak_multiple"),
                "entry_p": row.get("entry_p"),
                "return_pct": row.get("return_pct"),
                "exit": row.get("exit"),
            }
        )
    extra = list(load_exemplars().get("incredible") or [])
    for row in extra:
        if isinstance(row, dict):
            rows.append({**row, "source": row.get("source") or "incredible"})
    return rows


def year_winners_template(
    session: Session,
    chain: str = "sol",
    *,
    min_multiple: float = 5.0,
    limit: int = 40,
) -> dict[str, Any]:
    """Live template: curated exemplars + ledger runners with thesis capture."""
    chain = normalize_chain(chain)
    retro = paper_v1_runners_retro(session, chain, min_multiple=min_multiple, limit=limit)
    tag_hits: Counter[str] = Counter()
    path_hits: Counter[str] = Counter()
    for row in retro.get("items") or []:
        path_hits[str(row.get("path") or "miss")] += 1
        for tag in row.get("tags") or []:
            tag_hits[str(tag)] += 1
    curated = [r for r in curated_incredible() if normalize_chain(str(r.get("chain") or chain)) == chain]
    return {
        "chain": chain,
        "horizon_note": (
            "Not a full 1-year external scrape — confirmed runners in our ledger "
            "plus curated exemplars (PONS first day, desk-caught). Full-year "
            "external lists add survivorship; extract alpha only from first-print features."
        ),
        "alpha_extract": ALPHA_EXTRACT,
        "curated": curated,
        "ledger_runners": {
            "n": retro.get("n"),
            "would_pass_v1": retro.get("would_pass_v1"),
            "would_pass_rate": retro.get("would_pass_rate"),
            "by_path": retro.get("by_path"),
            "tag_counts": dict(tag_hits),
            "path_counts": dict(path_hits),
            "thin_entry_features": retro.get("thin_entry_features"),
            "items": (retro.get("items") or [])[:limit],
        },
        "how_to_use": (
            "Treat this as a feature template, not a shopping list. Rank new "
            "names by frozen thesis + Entry/Live the same way; score whether "
            "winner-shaped tags appear before 2×, not after."
        ),
    }
