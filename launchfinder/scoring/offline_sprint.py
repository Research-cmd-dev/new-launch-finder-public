"""Offline sprint snapshot for Learn — no new launches required."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..chains import normalize_chain
from .miss_cohort import miss_cohort
from .runners_retro import paper_v1_runners_retro
from .thesis_enrich import repair_thin_entry_thesis
from .thesis_weights import load_rank_policy, load_thesis_weights, refit_thesis_weights


def offline_sprint_status(
    session: Session,
    chain: str = "sol",
    *,
    repair: bool = False,
    refit: bool = False,
) -> dict[str, Any]:
    """Aggregate capture gap + miss cohort + optional repair/refit."""
    chain = normalize_chain(chain)
    repaired = {"scanned": 0, "patched": 0}
    if repair:
        repaired = repair_thin_entry_thesis(session, limit=200)
    fitted: dict[str, Any] = {}
    if refit:
        fitted = refit_thesis_weights(session, min_closed=8)
    retro = paper_v1_runners_retro(session, chain, min_multiple=5.0, limit=60)
    cohort = miss_cohort(session, chain, days=14)
    return {
        "chain": chain,
        "paper_only": True,
        "thesis_weights": load_thesis_weights(session),
        "rank_policy": load_rank_policy(session),
        "repair": repaired,
        "refit": fitted,
        "runners_retro": {
            "n": retro.get("n"),
            "would_pass_v1": retro.get("would_pass_v1"),
            "would_pass_rate": retro.get("would_pass_rate"),
            "by_path": retro.get("by_path"),
            "by_miss": retro.get("by_miss"),
            "thin_entry_features": retro.get("thin_entry_features"),
            "live_reconstructed": retro.get("live_reconstructed"),
            "would_pass_not_booked": retro.get("would_pass_not_booked"),
            "note": retro.get("note"),
        },
        "miss_cohort": {
            "n_winners": cohort.get("n_winners"),
            "n_losers": cohort.get("n_losers"),
            "tag_rate_winners": cohort.get("tag_rate_winners"),
            "tag_rate_losers": cohort.get("tag_rate_losers"),
            "paper_skipped": cohort.get("paper_skipped"),
            "paper_shadow": cohort.get("paper_shadow"),
            "paper_misses": {
                "n_runners": (cohort.get("paper_misses") or {}).get("n_runners"),
                "n_duds": (cohort.get("paper_misses") or {}).get("n_duds"),
                "side_key": (cohort.get("paper_misses") or {}).get("side_key"),
                "separators": [
                    r.get("feature")
                    for r in ((cohort.get("paper_misses") or {}).get("separators") or [])[:4]
                ],
            },
            "note": cohort.get("note"),
        },
        "next": [
            "Repair thin thesis when thin_entry_features > 0",
            "Watch by_miss: live_unknown / no_thesis_tags / leftover_mcap",
            "Use miss_cohort tag rates for precision (not shopping lists)",
            "Shared cap: chain-mix reserve ≥1 Sol when both queue at lock",
            "Leftover mcap ≥ $1M rejected from paperV1 queue",
            "Sanity loop: FOMO miss=0 + hijack_keep before any buy-side experiment",
            "Soft/late vetoes stay shadow-only — RH green shadow ≠ buy",
            "Apply enrich/freeze jobs (runner thesis + Live@entry) when hard_ok",
            "HTTP enrich thin runners (website→GitHub) before no-HTTP repair",
            "Daily ritual day-delta + rank-policy A/B flip from closed short-list",
        ],
    }
