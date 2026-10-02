"""FORWARD production-gate progress — Learn operator card.

Read-only composition of existing ledger / scorecard / risk / heartbeat
APIs. Paper only. Does not arm live, open fills, or spend X.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..chains import normalize_chain
from ..image_rev import IMAGE_REV
from ..models import PaperFill, utcnow

THESIS_COVERAGE_BAR = 0.80
SAMPLE_BAR = 30
SAMPLE_AMBER = 10
HIT_COMPARE_MIN = 10
BASELINE_MIN = 5
STALE_LOOP_S = 600
# Keep in sync with scripts/gate_status.py LOOP_FRESH_S (operator CLI).
LOOP_FRESH_S: dict[str, int] = {
    "ingest": 600,
    "hunt_tape": 600,
    "tape_refresh": 900,
    "paper_sync": 600,
    "batch_fit": 7200,
    "fomo_trending": 7200,
    "fomo_alerts": 600,
}
# Worker beats hunt_tape + tape_refresh (+ ingest). Legacy "hunt" is not a loop key.
CRITICAL_TAPE = ("hunt_tape", "tape_refresh")
CRITICAL_PAPER = ("paper_sync",)
HOURLY_LOOPS = frozenset({"batch_fit", "live_fit", "fomo_trending"})


def _aware(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _color(level: str) -> str:
    if level in {"green", "amber", "red"}:
        return level
    return "amber"


def _bar(
    key: str,
    label: str,
    *,
    color: str,
    value: Any,
    target: str,
    detail: str,
    progress: float | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "key": key,
        "label": label,
        "color": _color(color),
        "value": value,
        "target": target,
        "detail": detail,
        "progress": None if progress is None else max(0.0, min(1.0, round(float(progress), 3))),
        "extra": extra or {},
    }
    return out


def _v1_opens(session: Session) -> list[PaperFill]:
    from .paper_v1 import PAPER_V1_LINE

    return (
        session.query(PaperFill)
        .filter(PaperFill.line == PAPER_V1_LINE, PaperFill.status.in_(("open", "closed")))
        .order_by(PaperFill.id.desc())
        .limit(2_000)
        .all()
    )


def _sellable_closed(fills: list[PaperFill]) -> list[PaperFill]:
    out: list[PaperFill] = []
    for fill in fills:
        if fill.status != "closed":
            continue
        entry = float(fill.entry_mcap or 0.0)
        peak = float(fill.max_mcap or 0.0)
        last = float(fill.last_mcap or 0.0)
        if entry > 0 and (peak > 0 or last > 0):
            out.append(fill)
    return out


def _hit2x(fill: PaperFill) -> bool:
    entry = float(fill.entry_mcap or 0.0)
    peak = float(fill.max_mcap or 0.0)
    return entry > 0 and peak >= 2.0 * entry


def _thesis_tagged(session: Session, fill: PaperFill) -> bool:
    from ..ledger import _v1_thesis
    from .paper_v1 import v1_row_has_hard_tag

    thesis = _v1_thesis(session, fill.token_id, fill.decision_id)
    return v1_row_has_hard_tag({"thesis": thesis})


def _wide_hi_baseline(session: Session, chain: str) -> dict[str, Any]:
    from ..ledger import paper_scorecard_view

    view = paper_scorecard_view(session, chain)
    hi = ((view.get("scorecard") or {}).get("closed_by_line") or {}).get("hi") or {}
    n = int(hi.get("n") or 0)
    wins = int(hi.get("wins") or 0)
    return {
        "chain": normalize_chain(chain),
        "n": n,
        "wins": wins,
        "rate": round(wins / n, 4) if n else None,
    }


def _lock_cycle_start(now: datetime) -> datetime:
    from .paper_v1 import PAPER_V1_LOCK_HOUR

    now = _aware(now) or utcnow()
    lock = now.replace(hour=PAPER_V1_LOCK_HOUR, minute=0, second=0, microsecond=0)
    if now < lock:
        lock = lock - timedelta(days=1)
    return lock


def _thesis_bar(session: Session, opens: list[PaperFill]) -> dict[str, Any]:
    from ..ledger import _v1_thesis
    from .paper_v1 import THESIS_HARD_TAGS

    n = len(opens)
    tagged = sum(1 for fill in opens if _thesis_tagged(session, fill))
    meme_legacy = 0
    for fill in opens:
        if _thesis_tagged(session, fill):
            continue
        thesis = _v1_thesis(session, fill.token_id, fill.decision_id)
        hard = set(thesis.get("hard_tags") or [])
        if hard & set(THESIS_HARD_TAGS):
            continue
        if float(thesis.get("name_quality") or 0.0) >= 0.6 or "meme" in (thesis.get("tags") or []):
            meme_legacy += 1
    coverage = round(tagged / n, 4) if n else None
    if n == 0:
        color, detail = "red", "No paperV1 opens yet — thesis coverage has no denominator."
    elif coverage is not None and coverage >= THESIS_COVERAGE_BAR:
        color, detail = "green", f"{tagged}/{n} opens carry ≥1 frozen thesis tag."
    elif coverage is not None and coverage >= 0.40:
        color, detail = "amber", f"{tagged}/{n} tagged — need {THESIS_COVERAGE_BAR:.0%} of opens."
    else:
        legacy_note = (
            f" ({meme_legacy} legacy meme-only frozen — bar unchanged; v165 primes future qualifies)"
            if meme_legacy
            else ""
        )
        color, detail = "red", f"{tagged}/{n} tagged — short-list thin on hard thesis{legacy_note}."
    return _bar(
        "thesis_coverage",
        "Thesis coverage",
        color=color,
        value=coverage,
        target=f"≥{THESIS_COVERAGE_BAR:.0%} of paperV1 opens with a hard thesis tag",
        detail=detail,
        progress=coverage,
        extra={
            "n": n,
            "tagged": tagged,
            "bar": THESIS_COVERAGE_BAR,
            "legacy_meme_only_n": meme_legacy,
        },
    )


def _sample_bar(closed: list[PaperFill]) -> dict[str, Any]:
    n = len(closed)
    if n >= SAMPLE_BAR:
        color, detail = "green", f"{n} closed paperV1 fills with sellable prints."
    elif n >= SAMPLE_AMBER:
        color, detail = "amber", f"{n}/{SAMPLE_BAR} closed sellable fills — keep the paper book running."
    else:
        color, detail = "red", f"{n}/{SAMPLE_BAR} closed sellable fills — sample is still too small."
    return _bar(
        "sample",
        "Closed sample",
        color=color,
        value=n,
        target=f"≥{SAMPLE_BAR} closed paperV1 fills",
        detail=detail,
        progress=n / SAMPLE_BAR if SAMPLE_BAR else None,
        extra={"n": n, "bar": SAMPLE_BAR},
    )


def _hit_bar(session: Session, closed: list[PaperFill]) -> dict[str, Any]:
    from .paper_v1 import v1_peak_promoted_open

    organic = [fill for fill in closed if not v1_peak_promoted_open(fill)]
    n_peak_promoted = len(closed) - len(organic)
    hits_all = sum(1 for fill in closed if _hit2x(fill))
    hits_org = sum(1 for fill in organic if _hit2x(fill))
    sl_n = len(closed)
    sl_n_org = len(organic)
    sl_rate = round(hits_all / sl_n, 4) if sl_n else None
    rate_excl_peak = round(hits_org / sl_n_org, 4) if sl_n_org else None
    baselines = [_wide_hi_baseline(session, "sol"), _wide_hi_baseline(session, "robinhood")]
    base_n = sum(int(b["n"]) for b in baselines)
    base_wins = sum(int(b["wins"]) for b in baselines)
    base_rate = round(base_wins / base_n, 4) if base_n else None
    delta = None
    if rate_excl_peak is not None and base_rate is not None:
        delta = round(rate_excl_peak - base_rate, 4)

    if sl_n_org < HIT_COMPARE_MIN or base_n < BASELINE_MIN:
        color = "amber"
        detail = (
            f"Skill sample {hits_org}/{sl_n_org} hit2× (excl {n_peak_promoted} peak-promote) "
            f"vs wide-hi {base_wins}/{base_n} — need ≥{HIT_COMPARE_MIN} organic closes."
        )
    elif rate_excl_peak is not None and base_rate is not None and rate_excl_peak > base_rate:
        color = "green"
        detail = (
            f"Organic hit2× {rate_excl_peak:.0%} beats wide-hi {base_rate:.0%} (Δ{delta:+.0%}); "
            f"incl peak-promote {hits_all}/{sl_n}."
        )
    else:
        color = "red"
        detail = (
            f"Organic hit2× {rate_excl_peak if rate_excl_peak is not None else '—'} "
            f"does not beat wide-hi {base_rate if base_rate is not None else '—'} "
            f"({n_peak_promoted} peak-promoted excluded)."
        )
    return _bar(
        "hit_quality",
        "Hit2× vs wide-hi",
        color=color,
        value=rate_excl_peak,
        target="Organic short-list hit2× above wide-hi baseline",
        detail=detail,
        progress=None if rate_excl_peak is None else min(1.0, rate_excl_peak / 0.50),
        extra={
            "short_list": {"n": sl_n, "hit2x_n": hits_all, "rate": sl_rate},
            "organic": {"n": sl_n_org, "hit2x_n": hits_org, "rate": rate_excl_peak},
            "n_peak_promoted": n_peak_promoted,
            "rate_excl_peak": rate_excl_peak,
            "baseline": {"n": base_n, "wins": base_wins, "rate": base_rate, "by_chain": baselines},
            "delta": delta,
        },
    )


def _loop_fresh_limit(name: str) -> int:
    return LOOP_FRESH_S.get(name, STALE_LOOP_S)


def _loop_is_fresh(loops: dict[str, Any], name: str) -> bool:
    info = loops.get(name)
    if not info:
        return False
    age = info.get("age_s")
    if age is None:
        return False
    try:
        return int(age) <= _loop_fresh_limit(name)
    except (TypeError, ValueError):
        return False


def _gate_stale_loops(loops: dict[str, Any]) -> list[str]:
    """Same budget keys as scripts/gate_status.loops_fresh."""
    stale: list[str] = []
    for name, limit in LOOP_FRESH_S.items():
        row = loops.get(name)
        if not isinstance(row, dict):
            stale.append(f"{name}:missing")
            continue
        try:
            age = float(row.get("age_s"))
        except (TypeError, ValueError):
            stale.append(f"{name}:no-age")
            continue
        if age > limit:
            stale.append(f"{name}:{int(age)}s")
    return stale


def _tape_loops_ok(loops: dict[str, Any]) -> bool:
    return any(_loop_is_fresh(loops, name) for name in CRITICAL_TAPE)


def _paper_sync_ok(loops: dict[str, Any]) -> tuple[bool, bool]:
    """Paper path ok only when loop:paper_sync beat is fresh (no hunt_tape stand-in)."""
    if _loop_is_fresh(loops, "paper_sync"):
        return True, False
    return False, False


def _stability_bar(session: Session, *, now: datetime) -> dict[str, Any]:
    from ..ledger import heartbeats

    loops = heartbeats(session) or {}
    stale = _gate_stale_loops(loops)
    notes: list[str] = []
    for name, info in loops.items():
        note = str((info or {}).get("note") or "")
        if "helius capped" in note.lower():
            notes.append(f"{name}: helius capped")
    has_tape = _tape_loops_ok(loops)
    has_paper, paper_via_hunt_tape = _paper_sync_ok(loops)
    lock_start = _lock_cycle_start(now)
    cycle_ok = True
    for name in (*CRITICAL_TAPE, *CRITICAL_PAPER, "hunt_tape"):
        info = loops.get(name)
        if not info:
            continue
        at = info.get("at")
        try:
            ts = datetime.fromisoformat(at) if at else None
        except ValueError:
            ts = None
        ts = _aware(ts)
        if ts is not None and ts < lock_start and not _loop_is_fresh(loops, name):
            cycle_ok = False

    if not loops:
        color = "amber"
        detail = f"{IMAGE_REV} · no worker heartbeats in this process (API-only or fresh db)."
    elif stale:
        color = "red"
        detail = f"{IMAGE_REV} · worker loops stale or missing: {', '.join(stale)}."
        cycle_ok = False
    elif notes:
        color = "amber"
        detail = f"{IMAGE_REV} · loops fresh · {'; '.join(notes)}."
    elif has_tape and has_paper:
        color = "green"
        detail = f"{IMAGE_REV} · hunt_tape / tape_refresh + paper_sync fresh this lock cycle."
    elif not has_tape and not has_paper:
        color = "amber"
        detail = (
            f"{IMAGE_REV} · worker heartbeats present but hunt_tape / tape_refresh "
            f"and paperV1 sync paths are missing or stale."
        )
    elif not has_tape:
        color = "amber"
        detail = f"{IMAGE_REV} · paper path ok but hunt_tape / tape_refresh heartbeat missing or stale."
    else:
        color = "amber"
        detail = (
            f"{IMAGE_REV} · tape loops fresh but loop:paper_sync missing or stale."
        )

    return _bar(
        "stability",
        "IMAGE_REV / loops",
        color=color,
        value=IMAGE_REV,
        target="No IMAGE_REV / worker-loop incident for a lock cycle",
        detail=detail,
        progress=1.0 if color == "green" else (0.5 if color == "amber" else 0.15),
        extra={
            "image_rev": IMAGE_REV,
            "stale": stale,
            "notes": notes,
            "lock_cycle_start": lock_start.isoformat(),
            "cycle_ok": cycle_ok and color == "green",
            "has_tape": has_tape,
            "has_paper": has_paper,
            "paper_via_hunt_tape": paper_via_hunt_tape,
            "loop_budget_s": LOOP_FRESH_S,
        },
    )


def _ritual_bar(session: Session, chain: str, *, now: datetime) -> dict[str, Any]:
    from .day_delta import paper_v1_day_delta
    from .paper_v1 import (
        PAPER_V1_SKIP_CAP,
        PAPER_V1_SKIP_NO_THESIS,
        PAPER_V1_SKIP_SCORE_MISS,
    )

    delta = paper_v1_day_delta(session, chain, now=now)
    today = delta.get("today") or {}
    yday = delta.get("yesterday") or {}
    skip_today = today.get("skip_reasons") or {}
    skip_yday = yday.get("skip_reasons") or {}
    used_today = bool(
        today.get("picked_n")
        or today.get("skipped_n")
        or today.get("closed_n")
        or today.get("shadow_n")
        or skip_today
    )
    used_yday = bool(
        yday.get("picked_n")
        or yday.get("skipped_n")
        or yday.get("closed_n")
        or yday.get("shadow_n")
        or skip_yday
    )
    taxonomy = [PAPER_V1_SKIP_CAP, PAPER_V1_SKIP_NO_THESIS, PAPER_V1_SKIP_SCORE_MISS]
    if used_today and (skip_today or today.get("picked_n") or today.get("closed_n")):
        color = "green"
        detail = (
            f"Today {today.get('day')}: picked {today.get('picked_n', 0)}, "
            f"skipped {today.get('skipped_n', 0)}, closed {today.get('closed_n', 0)}."
        )
    elif used_yday:
        color = "amber"
        detail = f"Yesterday had review rows; today is still silent ({today.get('silence') or 'no picks'})."
    else:
        color = "amber"
        detail = "Day-delta is wired; no short-list review rows yet today or yesterday."

    return _bar(
        "ritual",
        "Daily ritual",
        color=color,
        value="used" if used_today else ("yesterday" if used_yday else "idle"),
        target="Daily review used; skip reasons readable",
        detail=detail,
        progress=1.0 if used_today else (0.55 if used_yday else 0.25),
        extra={
            "used_today": used_today,
            "used_yesterday": used_yday,
            "skip_reasons": skip_today,
            "skip_families": today.get("skip_families") or {},
            "skip_reasons_signal": today.get("skip_reasons_signal") or {},
            "skip_taxonomy": taxonomy,
            "today": {
                "day": today.get("day"),
                "picked_n": today.get("picked_n"),
                "skipped_n": today.get("skipped_n"),
                "closed_n": today.get("closed_n"),
                "thesis_coverage": today.get("thesis_coverage"),
                "hit2x_n": today.get("hit2x_n"),
                "silence": today.get("silence"),
            },
            "delta": delta.get("delta"),
            "rank_policy": delta.get("rank_policy"),
            "ritual": delta.get("ritual") or [],
        },
    )


def _fomo_mirror_bar(session: Session) -> dict[str, Any]:
    """Last hourly audit mirror freshness — desk sanity gate (not a buy list)."""
    from ..research.fomo_coverage import AUDIT_EVERY_S, load_fomo_trending_audit

    audit = load_fomo_trending_audit(session) or {}
    stale = bool(audit.get("board_stale"))
    age_h = audit.get("capture_age_hours")
    captured = audit.get("captured_at")
    audit_at = audit.get("at")
    audit_age_s: float | None = None
    if audit_at:
        try:
            ts = _aware(datetime.fromisoformat(str(audit_at).replace("Z", "+00:00")))
            if ts:
                audit_age_s = max(0.0, (utcnow() - ts).total_seconds())
        except ValueError:
            audit_age_s = None
    if not audit:
        color = "amber"
        detail = "No FOMO trending audit stamp yet — run /api/fomo-trending?refresh=true."
        progress = 0.35
        value = "unknown"
    elif stale:
        color = "red"
        detail = (
            f"API capture stale (≈{age_h}h at {captured or '?'}). "
            "App Tokens→Trending may differ — board is not green."
        )
        progress = 0.0
        value = "stale"
    elif audit_age_s is not None and audit_age_s > AUDIT_EVERY_S * 1.5:
        color = "amber"
        detail = f"Last audit {int(audit_age_s // 60)}m ago — refresh trending mirror."
        progress = 0.45
        value = "audit_old"
    else:
        color = "green"
        detail = "Trending REST capture within 15m budget on last audit."
        progress = 1.0
        value = "fresh"
    return _bar(
        "fomo_mirror",
        "FOMO trending mirror",
        color=color,
        value=value,
        target="API capture ≤15m · matches app board",
        detail=detail,
        progress=progress,
        extra={
            "board_stale": stale,
            "capture_age_hours": age_h,
            "captured_at": captured,
            "api_source": audit.get("api_source"),
            "mirror_note": audit.get("mirror_note"),
            "audit_at": audit_at,
        },
    )


def _risk_bar(session: Session) -> dict[str, Any]:
    from ..risk import risk_status

    st = risk_status(session)
    armed = bool(st.get("armed"))
    kill = bool(st.get("kill_switch"))
    if armed:
        color = "red"
        detail = "armed=true — live path is on. Paper-only overnight requires arm off."
        value = "ARMED"
        progress = 0.0
    elif kill:
        color = "amber"
        detail = "armed=false · kill switch ON (short list frozen)."
        value = "kill"
        progress = 0.55
    else:
        color = "green"
        detail = (
            f"armed=false · paper only · max ${float(st.get('max_notional_usd') or 0):.0f} "
            f"/ ${float(st.get('max_per_name_usd') or 0):.0f} per name."
        )
        value = "off"
        progress = 1.0
    return _bar(
        "risk",
        "Risk arm",
        color=color,
        value=value,
        target="armed=false · kill switch + notional caps present",
        detail=detail,
        progress=progress,
        extra={
            "armed": armed,
            "kill_switch": kill,
            "live_allowed": bool(st.get("live_allowed")),
            "max_notional_usd": st.get("max_notional_usd"),
            "max_per_name_usd": st.get("max_per_name_usd"),
            "paper_only": True,
        },
    )


def production_gate_progress(
    session: Session,
    chain: str = "sol",
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Seven FORWARD bars for Learn. Read-only. Never arms live."""
    now = now or utcnow()
    chain = normalize_chain(chain)
    opens = _v1_opens(session)
    closed = _sellable_closed(opens)

    bars = [
        _thesis_bar(session, opens),
        _sample_bar(closed),
        _hit_bar(session, closed),
        _fomo_mirror_bar(session),
        _stability_bar(session, now=now),
        _ritual_bar(session, chain, now=now),
        _risk_bar(session),
    ]
    greens = sum(1 for b in bars if b["color"] == "green")
    reds = sum(1 for b in bars if b["color"] == "red")
    ready = greens == len(bars)
    return {
        "chain": chain,
        "paper_only": True,
        "arm_ui": False,
        "image_rev": IMAGE_REV,
        "generated_at": now.isoformat() if hasattr(now, "isoformat") else str(now),
        "ready": ready,
        "cleared": greens,
        "total": len(bars),
        "summary": (
            "Production gate cleared — still paper until Dave arms."
            if ready
            else f"{greens}/{len(bars)} bars green · {reds} red · paper only."
        ),
        "bars": bars,
        "note": (
            "Operator trust card. No trading surface. armed stays false here. "
            "Bars compose /api/paper/v1, /api/paper/scorecard, /api/paper/v1/day-delta, "
            "/api/risk, /health loops."
        ),
    }


# Imported by Dockerfile lockstep — keep the name stable.
assert THESIS_COVERAGE_BAR == 0.80 and SAMPLE_BAR == 30
