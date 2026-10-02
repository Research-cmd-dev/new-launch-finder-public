"""Yesterday → today short-list delta for the Learn daily ritual."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..chains import normalize_chain
from ..ledger import paper_v1_review
from .paper_v1 import (
    PAPER_V1_THESIS_MIN,
    THESIS_COVERAGE_DEF,
    v1_day,
    v1_skip_family,
    v1_thesis_coverage,
)
from .thesis_weights import load_rank_policy, load_thesis_weights


def _opens(review: dict[str, Any]) -> list[dict[str, Any]]:
    """Names the desk took that day: open picks plus closes (review lists closes in both)."""
    seen: set[Any] = set()
    out: list[dict[str, Any]] = []
    for row in list(review.get("picked") or []) + list(review.get("closed") or []):
        key = row.get("id") or row.get("mint")
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _day_stats(review: dict[str, Any]) -> dict[str, Any]:
    closed = list(review.get("closed") or [])
    picked = list(review.get("picked") or [])
    skipped = list(review.get("skipped") or [])
    shadow = list(review.get("shadow") or [])
    queued = list(review.get("queued") or [])
    hit2 = sum(1 for r in closed if r.get("hit2x"))
    hit5 = sum(1 for r in closed if r.get("hit5x"))
    tags: dict[str, int] = {}
    for row in picked + closed:
        for tag in row.get("tags") or []:
            tags[str(tag)] = tags.get(str(tag), 0) + 1
    skips: dict[str, int] = {}
    families: dict[str, int] = {}
    signal_skips: dict[str, int] = {}
    for row in skipped:
        reason = (row.get("skip_reason") or "").strip() or "unspecified"
        skips[reason] = skips.get(reason, 0) + 1
        family = v1_skip_family(reason)
        families[family] = families.get(family, 0) + 1
        if family == "signal":
            signal_skips[reason] = signal_skips.get(reason, 0) + 1
    # One coverage definition everywhere: hard tag on opens (FORWARD gate).
    opens = _opens(review)
    cov = v1_thesis_coverage(opens)
    # Secondary, labelled: queued names already carrying a soft-path thesis.
    soft_ready = sum(
        1
        for row in queued
        if float(row.get("thesis_score") or 0.0) >= PAPER_V1_THESIS_MIN and (row.get("tags") or [])
    )
    return {
        "day": review.get("day"),
        "taken_chain": review.get("taken_chain"),
        "room": review.get("room"),
        "picked_n": len(picked),
        "queued_n": len(review.get("queued") or []),
        "skipped_n": len(skipped),
        "shadow_n": len(shadow),
        "closed_n": len(closed),
        "hit2x_n": hit2,
        "hit5x_n": hit5,
        "hit2x_rate": round(hit2 / len(closed), 4) if closed else None,
        "thesis_coverage": cov["coverage"],
        "thesis_n": cov["n"],
        "thesis_hard_tag_n": cov["hard_tag_n"],
        "thesis_any_tag_coverage": cov["any_tag_coverage"],
        "thesis_coverage_def": THESIS_COVERAGE_DEF,
        "queued_soft_path_ready": soft_ready,
        "silence": review.get("silence"),
        "skip_reasons": skips,
        "skip_families": families,
        "skip_reasons_signal": signal_skips,
        "tag_counts": tags,
    }


def _delta_num(today: Any, yday: Any) -> Any:
    if today is None or yday is None:
        return None
    try:
        return round(float(today) - float(yday), 4)
    except (TypeError, ValueError):
        return None


def paper_v1_day_delta(
    session: Session,
    chain: str = "sol",
    *,
    day: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compare book_day vs prior UTC day — hit rates, skips, thesis coverage."""
    now = now or datetime.now(timezone.utc)
    chain = normalize_chain(chain)
    today = day or v1_day(now)
    try:
        today_dt = datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        today_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yday = v1_day(today_dt - timedelta(days=1))

    today_review = paper_v1_review(session, chain, day=today, now=now)
    yday_review = paper_v1_review(session, chain, day=yday, now=now)
    t = _day_stats(today_review)
    y = _day_stats(yday_review)

    from .miss_cohort import paper_miss_learn

    miss = paper_miss_learn(session, chain, days=14, now=now)
    would = miss.get("would_have") or {}
    signal_bits = ", ".join(
        f"{k}×{v}" for k, v in sorted((t.get("skip_reasons_signal") or {}).items(), key=lambda kv: -kv[1])[:4]
    ) or "none"
    copycat_n = int((t.get("skip_families") or {}).get("copycat") or 0)
    si_n = int((t.get("skip_families") or {}).get("si_printer") or 0)
    ritual = [
        f"Hard-tag coverage on opens today {t['thesis_coverage'] if t['thesis_coverage'] is not None else '—'} "
        f"(n={t['thesis_n']} opens; any-tag {t['thesis_any_tag_coverage'] if t['thesis_any_tag_coverage'] is not None else '—'})",
        f"Taken {t['taken_chain']}/{today_review.get('cap_per_chain') or 3} · room {t['room']}",
        f"Closed hit2× {t['hit2x_n']}/{t['closed_n']} vs yday {y['hit2x_n']}/{y['closed_n']}",
        f"Silence: {t['silence'] or 'none'}",
        f"Rank policy: {load_rank_policy(session)}",
        f"Signal skips {signal_bits} · copycat drown ×{copycat_n} (excluded from miss cohort) · SI ×{si_n}",
        (
            f"Miss autopsies {miss.get('n_runners') or 0} runners / {miss.get('n_duds') or 0} duds "
            f"· would-have {would.get('n_hit_runners') or 0}/{miss.get('n_runners') or 0} "
            f"P={would.get('precision') if would.get('precision') is not None else '—'} "
            f"R={would.get('recall') if would.get('recall') is not None else '—'} "
            f"({would.get('evidence') or 'thin'}; open=false)"
        ),
        (
            f"FOMO trending no-Hunt {((miss.get('fomo_trend_no_hunt') or {}).get('n_joined') or 0)} "
            f"joined / {(miss.get('fomo_trend_no_hunt') or {}).get('n_runners') or 0} later ≥5× "
            f"· duds {(miss.get('fomo_trend_no_hunt') or {}).get('n_duds') or 0} "
            f"· desk-no-hunt {(miss.get('fomo_trend_no_hunt') or {}).get('n_desk_no_hunt') or 0} "
            f"· door miss {(miss.get('fomo_trend_no_hunt') or {}).get('n_door_miss') or 0} "
            f"(open=false)"
        ),
    ]
    return {
        "chain": chain,
        "today": t,
        "yesterday": y,
        "delta": {
            "hit2x_rate": _delta_num(t.get("hit2x_rate"), y.get("hit2x_rate")),
            "thesis_coverage": _delta_num(t.get("thesis_coverage"), y.get("thesis_coverage")),
            "picked_n": _delta_num(t.get("picked_n"), y.get("picked_n")),
            "skipped_n": _delta_num(t.get("skipped_n"), y.get("skipped_n")),
            "closed_n": _delta_num(t.get("closed_n"), y.get("closed_n")),
        },
        "ritual": ritual,
        "paper_misses": {
            "n_runners": miss.get("n_runners"),
            "n_duds": miss.get("n_duds"),
            "n_copycat_suppressed": miss.get("n_copycat_suppressed"),
            "would_have": would,
            "fixture_mint": (miss.get("fixture") or {}).get("mint"),
            "fomo_trend_no_hunt": {
                "n_joined": (miss.get("fomo_trend_no_hunt") or {}).get("n_joined"),
                "n_total": (miss.get("fomo_trend_no_hunt") or {}).get("n_total"),
                "n_runners": (miss.get("fomo_trend_no_hunt") or {}).get("n_runners"),
                "n_duds": (miss.get("fomo_trend_no_hunt") or {}).get("n_duds"),
                "n_desk_no_hunt": (miss.get("fomo_trend_no_hunt") or {}).get("n_desk_no_hunt"),
                "n_door_miss": (miss.get("fomo_trend_no_hunt") or {}).get("n_door_miss"),
                "by_why": (miss.get("fomo_trend_no_hunt") or {}).get("by_why") or {},
                "separators": (miss.get("fomo_trend_no_hunt") or {}).get("separators") or [],
                "open": False,
                "side_key": "fomo_trend_no_hunt",
            },
        },
        "thesis_weights": load_thesis_weights(session),
        "rank_policy": load_rank_policy(session),
        "paper_only": True,
    }
