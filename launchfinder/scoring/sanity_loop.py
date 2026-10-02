"""Continuous improve + reality sanity — one Learn scorecard.

Pulls FOMO door coverage, veto-retro keep/watch, paper this-window EV, and
soft-shadow moonbags. Emits pass/warn checks and **gated** next actions:
nothing that fails a hard sanity check becomes a buy suggestion.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..chains import normalize_chain
from ..ledger import paper_scorecard_view
from ..research.fomo_coverage import fomo_trending_coverage, load_fomo_trending_audit
from .miss_cohort import miss_cohort
from .runners_retro import paper_v1_runners_retro
from .veto_retro import veto_retro


def _check(name: str, ok: bool | None, detail: str, *, level: str = "hard") -> dict[str, Any]:
    status = "pass" if ok is True else ("warn" if ok is None else "fail")
    return {"name": name, "status": status, "level": level, "detail": detail}


async def sanity_loop(
    session: Session,
    chain: str = "sol",
    *,
    apply: bool = False,
    only_actions: list[str] | None = None,
) -> dict[str, Any]:
    """Async because FOMO coverage may refresh Dex/board I/O.

    When ``apply`` is True and hard checks pass, runs enrich/freeze jobs for
    executable improve actions (never veto weaken / chase / auto-buy).
    ``only_actions`` limits which executable jobs run (one-knob cycles).
    On hard fail, only explicitly requested enrich/freeze knobs may run.
    """
    chain = normalize_chain(chain)
    applied: dict[str, Any] | None = None
    fomo = await fomo_trending_coverage(session)
    audit = load_fomo_trending_audit(session)
    veto = veto_retro(session, chain)
    paper = paper_scorecard_view(session, chain)
    sc = paper.get("scorecard") or {}
    tw = sc.get("this_window") or {}
    shadow = sc.get("shadow_late") or {}
    retro = paper_v1_runners_retro(session, chain, min_multiple=5.0, limit=40)
    cohort = miss_cohort(session, chain, days=14)

    san = fomo.get("sanity") or {}
    board_stale = bool(fomo.get("board_stale") or san.get("board_stale"))
    capture_age_h = fomo.get("capture_age_hours") or san.get("capture_age_hours")
    miss_n = int(san.get("miss_n") or 0)
    seen = san.get("seen_rate")
    hj_keep = bool((veto.get("verdict") or {}).get("hijack_keep"))
    watch = list((veto.get("verdict") or {}).get("watch") or [])
    tw_n = int(tw.get("n") or 0)
    tw_avg = tw.get("avg_return_pct")
    soft_by = shadow.get("by_veto") or {}
    sol_soft_ok = True
    if chain == "sol":
        for key in ("start-high", "pre-pumped", "late chase"):
            avg = (soft_by.get(key) or {}).get("avg_return_pct")
            if avg is not None and float(avg) > 0:
                sol_soft_ok = False

    checks = [
        _check(
            "fomo_mirror",
            False if board_stale else True,
            (
                f"FOMO API trending mirror stale (capture_age≈{capture_age_h}h). "
                "App Tokens→Trending may differ — do not trust board misses until fresh."
            )
            if board_stale
            else "Keyed trending REST capture is within budget (≤15m).",
        ),
        _check(
            "fomo_door",
            None if board_stale else (miss_n == 0),
            (
                f"Board mirror stale — miss={miss_n} not scored as door bug until API capture is fresh."
            )
            if board_stale
            else (
                f"FOMO board miss={miss_n}; seen_rate={seen}. miss>0 = door bug, not a veto lesson."
            ),
        ),
        _check(
            "hijack_keep",
            hj_keep,
            "Sol hijack must keep refuse while peak≥5× mostly dusts (veto-retro).",
        ),
        _check(
            "soft_shadow_sol",
            sol_soft_ok if chain == "sol" else None,
            "Sol soft/late shadow moonbag should stay ≤0% avg — refuse stays shadow-only.",
            level="soft",
        ),
        _check(
            "paper_this_window",
            (tw_avg is not None and tw_n >= 20 and float(tw_avg) > 0) if tw_n else None,
            f"Wide-book (gated90) this-window EV {tw_avg}% on n={tw_n}. "
            "Wide paper line only — not the paperV1 short list; short-list quality is "
            "/api/paper/v1/review and the production gate.",
            level="soft",
        ),
        _check(
            "veto_watch_sample",
            None if watch else True,
            f"Watch needles (n≥40 peak5): {', '.join(watch) or 'none'}. Research only — never auto-buy.",
            level="soft",
        ),
    ]

    hard_fail = [c for c in checks if c["level"] == "hard" and c["status"] == "fail"]
    improve: list[dict[str, Any]] = []

    by_miss = retro.get("by_miss") or {}
    thin = int(retro.get("thin_entry_features") or 0)
    live_unk = int(by_miss.get("live_unknown") or 0)
    if thin > 0 and not hard_fail:
        improve.append(
            {
                "action": "enrich_thin_thesis_http",
                "why": f"runners-retro thin_entry_features={thin} — website/GitHub HTTP then Decision patch",
                "sanity": "allowed — source enrich only, no veto weaken",
            }
        )
        improve.append(
            {
                "action": "repair_thin_thesis",
                "why": f"runners-retro thin_entry_features={thin} — no-HTTP patch if raw already has signal",
                "sanity": "allowed — enrich only, no veto weaken",
            }
        )
    if live_unk > 0 and not hard_fail:
        improve.append(
            {
                "action": "freeze_live_coverage",
                "why": f"by_miss.live_unknown={live_unk} — score-path blind without Live@entry",
                "sanity": "allowed — capture gap, not a veto carve-out",
            }
        )
    if watch and not hard_fail:
        improve.append(
            {
                "action": "research_watch_needles",
                "why": f"veto-retro watch: {', '.join(watch)}",
                "sanity": "shadow/research only — hard veto stays until explicit policy change",
            }
        )
    if board_stale:
        improve.insert(
            0,
            {
                "action": "hold_course",
                "why": (
                    f"FOMO mirror stale (capture_age≈{capture_age_h}h) — wait for fresh API capture "
                    "or use ws_alert_hot_mints on /api/fomo-trending; no door fixes on a stale board."
                ),
                "sanity": "hard fail until mirror fresh — not a veto lesson",
            },
        )
    elif miss_n > 0:
        improve.append(
            {
                "action": "fix_fomo_door",
                "why": f"FOMO miss_n={miss_n}",
                "sanity": "blocks buy-side experiments until door is honest",
            }
        )
    if not hj_keep:
        improve.append(
            {
                "action": "review_hijack_policy",
                "why": "veto-retro hijack_keep=false (held-sellable rate high with enough peak5)",
                "sanity": "still no auto-buy — needs human carve-out design",
            }
        )
    if not improve:
        improve.append(
            {
                "action": "hold_course",
                "why": "Hard sanity green; keep short-list + veto policy; mine Live freeze / thesis repair only",
                "sanity": "no veto weaken, no chase/soft→buy",
            }
        )

    hard_ok = len(hard_fail) == 0
    if apply:
        from .sanity_jobs import EXECUTABLE, apply_sanity_jobs

        suggested = [a["action"] for a in improve if a.get("action") in EXECUTABLE]
        if hard_ok:
            if only_actions is not None:
                # One-knob: allow requested executable even if not in improve text.
                to_run = [a for a in only_actions if a in EXECUTABLE]
            else:
                to_run = suggested
        else:
            # Hard fail: enrich/freeze only when explicitly named — never buy-side.
            to_run = [a for a in (only_actions or []) if a in EXECUTABLE]
        if to_run:
            applied = await apply_sanity_jobs(session, chain, actions=to_run)
            applied["hard_ok"] = hard_ok
            applied["only_actions"] = only_actions
        else:
            applied = {
                "chain": chain,
                "applied": [],
                "skipped": list(EXECUTABLE),
                "reason": "hard_fail" if not hard_ok else ("filtered_empty" if only_actions is not None else "no_executable"),
                "hard_ok": hard_ok,
                "only_actions": only_actions,
            }

    return {
        "chain": chain,
        "at": datetime.now(timezone.utc).isoformat(),
        "paper_only": True,
        "hard_ok": hard_ok,
        "checks": checks,
        "improve": improve,
        "applied": applied,
        "fomo": {
            "seen_rate": seen,
            "miss_n": miss_n,
            "veto_hijack_n": san.get("veto_hijack_n"),
            "board": (fomo.get("counts") or {}).get("board"),
            "last_audit_at": (audit or {}).get("at") if isinstance(audit, dict) else None,
        },
        "veto": {
            "hijack_keep": hj_keep,
            "watch": watch,
            "n_gate_veto": veto.get("n_gate_veto"),
            "n_gate_pass": veto.get("n_gate_pass"),
            "summary": (veto.get("verdict") or {}).get("summary"),
        },
        "paper": {
            "this_window_n": tw_n,
            "this_window_avg_return_pct": tw_avg,
            "shadow_late_n": shadow.get("n"),
            "shadow_late_avg_return_pct": shadow.get("avg_return_pct"),
        },
        "runners": {
            "n": retro.get("n"),
            "would_pass_rate": retro.get("would_pass_rate"),
            "by_miss": by_miss,
            "thin_entry_features": thin,
        },
        "miss_cohort": {
            "n_winners": cohort.get("n_winners"),
            "n_losers": cohort.get("n_losers"),
            "hard_tag_rate_winners": cohort.get("hard_tag_rate_winners"),
            "signal_score_avg_winners": cohort.get("signal_score_avg_winners"),
            "signal_score_avg_losers": cohort.get("signal_score_avg_losers"),
        },
        "rules": [
            "Hard fail (FOMO miss / hijack_keep) blocks buy-side experiments.",
            "Soft/late vetoes stay shadow-only even when RH shadow prints green.",
            "Watch needles need peak5 n≥40; never auto-promote to paperV1.",
            "Improve actions are capture/repair/research — not chase alpha fantasies.",
        ],
    }
