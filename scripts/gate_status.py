#!/usr/bin/env python3
"""Production-gate dashboard for the paperV1 short list — read-only, API-only.

One honest view of the six gate rows in docs/FORWARD.md, computed the same
way every cycle so KEEP / REVERT decisions in docs/CYCLE_LOG.md cannot drift
between scorecards. No DB, no GMGN, no state mutation on the desk.

Definitions (these are the gate, not the Learn convenience numbers):

  thesis   share of paperV1 *opens* (open+closed, line paper_v1) in the window
           that carry >=1 HARD thesis tag (github / dev / cto). Meme alone is
           not a thesis since stack-v124.
  sample   closed paperV1 rows on *resolved* book days (day < today and no
           row still open / queued). Early closes on an unresolved day are
           survivors (rides / live dumps) and are reported separately.
  hit      short-list hit2x on that resolved sample vs the wide-hi baseline:
           n_observed-weighted hit2x_observed of Entry calibration bins at or
           above the chain's hi line.
  stable   /health image_rev == repo IMAGE_REV, loops fresh, git_sha stamped,
           no book day with taken_chain > cap_per_chain, and (via a local
           state file) the rev unchanged for a full lock cycle.
  ritual   day-delta answers on both chains, no silent chain-day in the
           window, every skipped row carries a reason.
  risk     armed false, kill off, live_allowed false, paper_only, x_api off.

Exit 1 only when the desk cannot be read. Red gates are expected for a
while — the point is to watch them move one knob at a time.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from launchfinder.image_rev import IMAGE_REV  # noqa: E402
from launchfinder.scoring.production_gate import LOOP_FRESH_S  # noqa: E402
from launchfinder.scoring.paper_v1 import (  # noqa: E402
    PAPER_V1_CAP_PER_CHAIN,
    PAPER_V1_LEFTOVER_MCAP,
    PAPER_V1_LOCK_HOUR,
    PAPER_V1_NOW_LIVE,
    PAPER_V1_PROMOTE_MULT,
    PAPER_V1_RH_ENTRY,
    PAPER_V1_RH_ENTRY_THESIS,
    PAPER_V1_SOL_LIVE,
    PAPER_V1_SOL_LIVE_THESIS,
    PAPER_V1_THESIS_MIN,
    PAPER_V1_THESIS_STRONG,
    THESIS_HARD_TAGS,
)
from launchfinder.scoring.thesis_weights import MIN_CLOSED, POLICY_FLIP_MARGIN  # noqa: E402

DEFAULT_BASE = "https://new-launch-finder-production.up.railway.app"
STATE_PATH = Path("/tmp/gate-status-state.json")
CHAINS = ("sol", "robinhood")

THESIS_BAR = 0.80
SAMPLE_BAR = 30
LOCK_CYCLE_H = 24.0
SILENT_TODAY_AFTER_UTC_HOUR = 12


class FetchError(RuntimeError):
    pass


# ----------------------------------------------------------------------------
# Pure helpers (unit-tested; no HTTP)
# ----------------------------------------------------------------------------


def has_hard_tag(tags: Any) -> bool:
    return any(str(t) in THESIS_HARD_TAGS for t in (tags or []))


def window_days(today: str, days: int) -> list[str]:
    """Book days newest-first: today, yesterday, ... (``days`` total)."""
    base = datetime.strptime(today, "%Y-%m-%d").date()
    return [(base - timedelta(days=i)).isoformat() for i in range(max(1, int(days)))]


def _uniq(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = str(row.get("mint") or row.get("id") or id(row))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def summarize_chain(
    chain: str,
    reviews: dict[str, dict[str, Any]],
    *,
    today: str,
    cap_per_chain: int = PAPER_V1_CAP_PER_CHAIN,
    now_hour_utc: int = 23,
) -> dict[str, Any]:
    """Fold per-day reviews into the gate inputs for one chain."""
    opens: list[dict[str, Any]] = []
    resolved_closed: list[dict[str, Any]] = []
    unresolved_closed: list[dict[str, Any]] = []
    cap_breaches: list[dict[str, Any]] = []
    silent_days: list[str] = []
    truncated_days: list[str] = []
    silent_today = False
    unreadable_skips = 0
    skipped_n = 0
    shadow_n = 0
    queued_today = 0
    days_with_rows = 0
    resolved_days: list[str] = []
    for day in sorted(reviews):
        rv = reviews[day] or {}
        picked = _uniq(list(rv.get("picked") or []) + list(rv.get("closed") or []))
        closed = _uniq(list(rv.get("closed") or []))
        skipped = list(rv.get("skipped") or [])
        shadow = list(rv.get("shadow") or [])
        queued = list(rv.get("queued") or [])
        n_rows = len(picked) + len(skipped) + len(queued)
        taken = rv.get("taken_chain")
        if n_rows:
            days_with_rows += 1
        elif taken:
            # The lane spent slots that day but the review returns no rows:
            # the review's bounded scan lost them. Attribution gap, not silence.
            truncated_days.append(day)
        elif day == today:
            silent_today = now_hour_utc >= SILENT_TODAY_AFTER_UTC_HOUR
        else:
            silent_days.append(day)
        opens.extend(picked)
        skipped_n += len(skipped)
        shadow_n += len(shadow)
        if day == today:
            queued_today = len(queued)
        for row in skipped:
            if not (row.get("skip_reason") or "").strip():
                unreadable_skips += 1
        cap = int(rv.get("cap_per_chain") or cap_per_chain)
        if taken is not None and int(taken) > cap:
            cap_breaches.append({"day": day, "taken_chain": int(taken), "cap_per_chain": cap})
        still_open = any((r.get("status") == "open") for r in picked) or bool(queued)
        if day < today and not still_open:
            resolved_days.append(day)
            resolved_closed.extend(closed)
        else:
            unresolved_closed.extend(closed)
    opens = _uniq(opens)
    hard = sum(1 for r in opens if has_hard_tag(r.get("tags")))
    any_tag = sum(1 for r in opens if r.get("tags"))
    res_hits = sum(1 for r in resolved_closed if r.get("hit2x"))
    unres_hits = sum(1 for r in unresolved_closed if r.get("hit2x"))
    return {
        "chain": chain,
        "opens_n": len(opens),
        "hard_tag_n": hard,
        "hard_tag_coverage": round(hard / len(opens), 4) if opens else None,
        "any_tag_coverage": round(any_tag / len(opens), 4) if opens else None,
        "opens_live_missing_n": sum(1 for r in opens if r.get("live_p") in (None, 0, 0.0)),
        "resolved_days": resolved_days,
        "resolved_closed_n": len(resolved_closed),
        "resolved_hit2x_n": res_hits,
        "resolved_hit2x": round(res_hits / len(resolved_closed), 4) if resolved_closed else None,
        "unresolved_closed_n": len(unresolved_closed),
        "unresolved_hit2x": round(unres_hits / len(unresolved_closed), 4) if unresolved_closed else None,
        "skipped_n": skipped_n,
        "shadow_n": shadow_n,
        "queued_today": queued_today,
        "unreadable_skips": unreadable_skips,
        "cap_breaches": cap_breaches,
        "silent_days": silent_days,
        "truncated_days": truncated_days,
        "silent_today": silent_today,
        "days_with_rows": days_with_rows,
        "days_in_window": len(reviews),
    }


def wide_hi_baseline(calibration: dict[str, Any] | None) -> dict[str, Any]:
    """n_observed-weighted hit2x_observed over Entry bins at/above the hi line."""
    cal = calibration or {}
    lines = cal.get("lines") or {}
    try:
        hi = float(lines.get("hi"))
    except (TypeError, ValueError):
        return {"hi": None, "hit2x_observed": None, "n_observed": 0, "hit2x": None, "n": 0}
    n_obs = 0
    hits_obs = 0.0
    n_all = 0
    hits_all = 0.0
    for b in cal.get("bins") or []:
        try:
            lo = float(str(b.get("bin") or "").split("-")[0])
        except ValueError:
            continue
        if lo + 1e-9 < hi:
            continue
        no = int(b.get("n_observed") or 0)
        na = int(b.get("n") or 0)
        n_obs += no
        n_all += na
        hits_obs += float(b.get("hit2x_observed") or 0.0) * no
        hits_all += float(b.get("hit2x") or 0.0) * na
    return {
        "hi": hi,
        "hit2x_observed": round(hits_obs / n_obs, 4) if n_obs else None,
        "n_observed": n_obs,
        "hit2x": round(hits_all / n_all, 4) if n_all else None,
        "n": n_all,
    }


def loops_fresh(health: dict[str, Any]) -> list[str]:
    stale: list[str] = []
    loops = (health or {}).get("loops") or {}
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


def rev_age_hours(state: dict[str, Any] | None, live_rev: str, now: datetime) -> tuple[float | None, dict[str, Any]]:
    """Hours the live rev has been unchanged as seen by this script's state file."""
    st = dict(state or {})
    if st.get("image_rev") != live_rev or not st.get("since"):
        st = {"image_rev": live_rev, "since": now.isoformat()}
        return None, st
    try:
        since = datetime.fromisoformat(str(st["since"]))
    except ValueError:
        st = {"image_rev": live_rev, "since": now.isoformat()}
        return None, st
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return round((now - since).total_seconds() / 3600.0, 2), st


def _gate(name: str, status: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"gate": name, "status": status, "detail": detail, **extra}


def evaluate_gates(
    per_chain: dict[str, dict[str, Any]],
    baselines: dict[str, dict[str, Any]],
    *,
    health: dict[str, Any],
    risk: dict[str, Any],
    delta_ok: dict[str, bool],
    repo_rev: str,
    rev_age_h: float | None,
) -> list[dict[str, Any]]:
    gates: list[dict[str, Any]] = []

    # 1. Thesis coverage (hard tags on opens) — both chains.
    bits = []
    ok = True
    for chain in CHAINS:
        s = per_chain.get(chain) or {}
        cov = s.get("hard_tag_coverage")
        n = s.get("opens_n") or 0
        if cov is None or cov < THESIS_BAR:
            ok = False
        bits.append(f"{chain} hard {cov if cov is not None else '—'} (any {s.get('any_tag_coverage')}) on {n} opens")
    gates.append(_gate("thesis_coverage", "pass" if ok else "fail", "; ".join(bits), bar=THESIS_BAR))

    # 2. Sample — resolved closes per chain.
    bits = []
    ok = True
    for chain in CHAINS:
        s = per_chain.get(chain) or {}
        n = int(s.get("resolved_closed_n") or 0)
        if n < SAMPLE_BAR:
            ok = False
        bits.append(
            f"{chain} resolved {n}/{SAMPLE_BAR} (unresolved early closes {s.get('unresolved_closed_n') or 0}, "
            f"resolved days {len(s.get('resolved_days') or [])})"
        )
    gates.append(_gate("sample", "pass" if ok else "fail", "; ".join(bits), bar=SAMPLE_BAR))

    # 3. Hit quality — resolved hit2x above wide-hi baseline, once the sample exists.
    bits = []
    status = "pass"
    for chain in CHAINS:
        s = per_chain.get(chain) or {}
        b = baselines.get(chain) or {}
        n = int(s.get("resolved_closed_n") or 0)
        hit = s.get("resolved_hit2x")
        base = b.get("hit2x_observed")
        bits.append(
            f"{chain} short-list {hit if hit is not None else '—'} (n={n}) vs wide-hi {base if base is not None else '—'} "
            f"(hi {b.get('hi')}, n_obs {b.get('n_observed')}); survivor-biased unresolved {s.get('unresolved_hit2x')}"
        )
        if n < SAMPLE_BAR or hit is None or base is None:
            if status != "fail":
                status = "warn"
        elif hit <= base:
            status = "fail"
    gates.append(_gate("hit_quality", status, "; ".join(bits)))

    # 4. Stability.
    live_rev = str((health or {}).get("image_rev") or "")
    stale = loops_fresh(health)
    breaches = [b for chain in CHAINS for b in ((per_chain.get(chain) or {}).get("cap_breaches") or [])]
    problems: list[str] = []
    if live_rev != repo_rev:
        problems.append(f"rev live {live_rev} != repo {repo_rev}")
    if stale:
        problems.append("stale loops " + ",".join(stale))
    if breaches:
        problems.append("cap breach " + ",".join(f"{b['day']}:{b['taken_chain']}>{b['cap_per_chain']}" for b in breaches))
    truncated = {c: (per_chain.get(c) or {}).get("truncated_days") or [] for c in CHAINS}
    for chain, days_ in truncated.items():
        if days_:
            problems.append(f"{chain} review lost {len(days_)} spent day(s) {','.join(days_)} (bounded scan) — day-delta yesterday is blind")
    warns: list[str] = []
    if not (health or {}).get("git_sha"):
        warns.append("git_sha null (stamp is image_rev only)")
    if rev_age_h is None:
        warns.append("rev age unknown (first observation)")
    elif rev_age_h < LOCK_CYCLE_H:
        warns.append(f"rev {live_rev} unchanged {rev_age_h}h < {LOCK_CYCLE_H}h lock cycle")
    if problems:
        status = "fail"
    elif warns:
        status = "warn"
    else:
        status = "pass"
    gates.append(_gate("stability", status, "; ".join(problems + warns) or f"rev {live_rev} stable {rev_age_h}h, loops fresh, caps honoured"))

    # 5. Operator ritual.
    problems = []
    warns = []
    for chain in CHAINS:
        s = per_chain.get(chain) or {}
        if not delta_ok.get(chain):
            problems.append(f"{chain} day-delta unreachable")
        if s.get("silent_days"):
            problems.append(f"{chain} silent days {','.join(s['silent_days'])}")
        if s.get("silent_today"):
            warns.append(f"{chain} silent so far today")
        if s.get("unreadable_skips"):
            problems.append(f"{chain} {s['unreadable_skips']} skips without a reason")
    status = "fail" if problems else ("warn" if warns else "pass")
    gates.append(_gate("operator_ritual", status, "; ".join(problems + warns) or "day-delta live, no silent chain-day, skips readable"))

    # 6. Risk controls.
    r = risk or {}
    problems = []
    if r.get("armed"):
        problems.append("ARMED")
    if r.get("live_allowed"):
        problems.append("live_allowed")
    if not r.get("paper_only", True):
        problems.append("paper_only false")
    if (health or {}).get("x_api"):
        problems.append("x_api spend on")
    warns = []
    if r.get("kill_switch"):
        warns.append("kill switch tripped")
    if not r.get("max_notional_usd"):
        warns.append("no max_notional")
    status = "fail" if problems else ("warn" if warns else "pass")
    gates.append(
        _gate(
            "risk_controls",
            status,
            "; ".join(problems + warns)
            or f"armed false, kill off, notional {r.get('max_notional_usd')}/{r.get('max_per_name_usd')}, x_api off",
        )
    )
    return gates


def knob_snapshot(report: dict[str, Any] | None) -> dict[str, Any]:
    """Every tunable a cycle may move — diff this between cycles (one knob only)."""
    rep = report or {}
    return {
        "image_rev_repo": IMAGE_REV,
        "rank_policy": rep.get("rank_policy"),
        "thesis_weights": rep.get("thesis_weights"),
        "cap_per_chain": PAPER_V1_CAP_PER_CHAIN,
        "lock_hour_utc": PAPER_V1_LOCK_HOUR,
        "sol_live_floor": PAPER_V1_SOL_LIVE,
        "sol_live_thesis_floor": PAPER_V1_SOL_LIVE_THESIS,
        "rh_entry_floor": PAPER_V1_RH_ENTRY,
        "rh_entry_thesis_floor": PAPER_V1_RH_ENTRY_THESIS,
        "immediate_live": PAPER_V1_NOW_LIVE,
        "promote_peak_mult": PAPER_V1_PROMOTE_MULT,
        "thesis_min": PAPER_V1_THESIS_MIN,
        "thesis_strong": PAPER_V1_THESIS_STRONG,
        "leftover_mcap": PAPER_V1_LEFTOVER_MCAP,
        "hard_tags": sorted(THESIS_HARD_TAGS),
        "refit_min_closed": MIN_CLOSED,
        "policy_flip_margin": POLICY_FLIP_MARGIN,
    }


# ----------------------------------------------------------------------------
# Live fetch
# ----------------------------------------------------------------------------


def _get(base: str, path: str, *, timeout: int = 90) -> dict[str, Any]:
    url = f"{base.rstrip('/')}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise FetchError(f"{path}: {exc}") from exc


def _safe(base: str, path: str) -> dict[str, Any] | None:
    try:
        return _get(base, path)
    except FetchError as exc:
        print(f"  warn: {exc}", file=sys.stderr)
        return None


def collect(base: str, *, days: int, workers: int, now: datetime) -> dict[str, Any]:
    health = _get(base, "/health")
    today = now.date().isoformat()
    day_list = window_days(today, days)
    jobs: dict[tuple[str, str], str] = {}
    for chain in CHAINS:
        for day in day_list:
            jobs[("review", f"{chain}|{day}")] = f"/api/paper/v1/review?chain={chain}&day={day}"
        jobs[("delta", chain)] = f"/api/paper/v1/day-delta?chain={chain}"
        jobs[("calib", chain)] = f"/api/model/calibration?chain={chain}"
    jobs[("risk", "")] = "/api/risk"
    jobs[("report", "")] = "/api/paper/v1/report"
    results: dict[tuple[str, str], dict[str, Any] | None] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(_safe, base, path): key for key, path in jobs.items()}
        for fut, key in futs.items():
            results[key] = fut.result()
    # Heavy routes (calibration) can 500 under the pool; one sequential retry.
    for key, path in jobs.items():
        if results.get(key) is None:
            results[key] = _safe(base, path)
    reviews: dict[str, dict[str, dict[str, Any]]] = {c: {} for c in CHAINS}
    for (kind, key), val in results.items():
        if kind == "review":
            chain, day = key.split("|", 1)
            reviews[chain][day] = val or {}
    return {
        "now": now.isoformat(),
        "today": today,
        "days": day_list,
        "health": health,
        "risk": results.get(("risk", "")) or (health.get("risk") or {}),
        "report": results.get(("report", "")) or {},
        "reviews": reviews,
        "delta": {c: results.get(("delta", c)) for c in CHAINS},
        "calibration": {c: results.get(("calib", c)) for c in CHAINS},
    }


def build(raw: dict[str, Any], *, state: dict[str, Any] | None, now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    per_chain = {
        c: summarize_chain(c, raw["reviews"][c], today=raw["today"], now_hour_utc=now.hour) for c in CHAINS
    }
    baselines = {c: wide_hi_baseline(raw["calibration"].get(c)) for c in CHAINS}
    live_rev = str((raw["health"] or {}).get("image_rev") or "")
    age_h, new_state = rev_age_hours(state, live_rev, now)
    gates = evaluate_gates(
        per_chain,
        baselines,
        health=raw["health"],
        risk=raw["risk"],
        delta_ok={c: bool(raw["delta"].get(c)) for c in CHAINS},
        repo_rev=IMAGE_REV,
        rev_age_h=age_h,
    )
    out = {
        "generated_at": now.isoformat(),
        "base_days": raw["days"],
        "image_rev_live": live_rev,
        "image_rev_repo": IMAGE_REV,
        "rev_age_hours": age_h,
        "gates": gates,
        "gates_passing": sum(1 for g in gates if g["status"] == "pass"),
        "per_chain": per_chain,
        "baselines": baselines,
        "knobs": knob_snapshot(raw["report"]),
        "ritual": {c: ((raw["delta"].get(c) or {}).get("ritual")) for c in CHAINS},
        "paper_only": True,
    }
    return out, new_state


def render(out: dict[str, Any]) -> str:
    lines = [
        f"paperV1 production gate — {out['generated_at']} — live {out['image_rev_live']} / repo {out['image_rev_repo']}"
        f" — {out['gates_passing']}/6 pass",
        "",
    ]
    for g in out["gates"]:
        mark = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}.get(g["status"], g["status"].upper())
        lines.append(f"[{mark}] {g['gate']:<16} {g['detail']}")
    lines.append("")
    for chain, s in out["per_chain"].items():
        lines.append(
            f"{chain:<10} opens {s['opens_n']} (hard {s['hard_tag_n']}, live missing {s['opens_live_missing_n']}) · "
            f"resolved closes {s['resolved_closed_n']} hit2x {s['resolved_hit2x']} · "
            f"skipped {s['skipped_n']} shadow {s['shadow_n']} queued-today {s['queued_today']} · "
            f"silent {s['silent_days'] or '—'}{' (+today)' if s['silent_today'] else ''} · "
            f"truncated {s['truncated_days'] or '—'}"
        )
    k = out["knobs"]
    lines.append("")
    lines.append(
        f"knobs: policy {k['rank_policy']} · weights {k['thesis_weights']} · caps {k['cap_per_chain']}+{k['cap_per_chain']} · "
        f"Sol live {k['sol_live_floor']}/{k['sol_live_thesis_floor']} · RH entry {k['rh_entry_floor']}/{k['rh_entry_thesis_floor']} · "
        f"immediate {k['immediate_live']} · peak {k['promote_peak_mult']}x · refit n>={k['refit_min_closed']} flip>={k['policy_flip_margin']}"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--days", type=int, default=7, help="book days in the rolling window (default 7)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--json", type=Path, default=None, help="write the full gate JSON here")
    ap.add_argument("--state", type=Path, default=STATE_PATH, help="rev-age state file")
    args = ap.parse_args(argv)

    now = datetime.now(timezone.utc)
    state: dict[str, Any] | None = None
    if args.state and args.state.exists():
        try:
            state = json.loads(args.state.read_text())
        except (OSError, json.JSONDecodeError):
            state = None
    try:
        raw = collect(args.base, days=args.days, workers=args.workers, now=now)
    except FetchError as exc:
        print(f"FAIL fetch: {exc}", file=sys.stderr)
        return 1
    out, new_state = build(raw, state=state, now=now)
    print(render(out))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(out, indent=2, default=str))
        print(f"\njson → {args.json}")
    if args.state:
        try:
            args.state.write_text(json.dumps(new_state))
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
