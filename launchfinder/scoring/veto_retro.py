"""Historical veto health — keep hard vetoes honest, always improving.

Reads gate Decisions × Outcomes (+ shadow_late fills). Does not open buys,
rewrite Decisions, or weaken needles. Surfaces dust vs held-sellable rates so
Learn can see when a veto still earns its keep (hijack peaks then dusts).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..chains import normalize_chain
from ..models import Decision, Outcome, PaperFill, Token
from .paper_gate import PAPER_HARD_NEEDLES, PAPER_SHADOW_VETOES

# Among peak≥5× vetoed names, held-sellable share above this → watch for carve-out.
WATCH_HELD_RATE = 0.15
# Ignore tiny peak≥5× samples (RH hijack n=4 must not flip policy).
WATCH_MIN_PEAK5 = 40
# Soft/late shadow avg moonbag return above this → soft veto still OK as shadow-only.
SOFT_SHADOW_OK_MAX_AVG = 0.0  # Sol should stay ≤0; RH late chase can be + but still not a buy


def _cls(veto: str) -> str:
    v = (veto or "").strip().lower()
    if v in PAPER_SHADOW_VETOES or v in {"prepumped"}:
        return "soft_shadow"
    if v in {"two-tick", "phantom t0", "tape dump or under hold", "no liquid book in grace", "outside window"}:
        return "tape"
    hard = set(PAPER_HARD_NEEDLES) | {
        "official brand website",
        "staged social",
        "celebrity/brand x",
        "bought aged x",
    }
    if v in hard or "hijack" in v or "celebrity" in v or "brand" in v:
        return "hard"
    return "other"


def veto_retro(session: Session, chain: str = "sol") -> dict[str, Any]:
    """Full-history gate veto scorecard for one chain."""
    chain = normalize_chain(chain)
    gates = (
        session.query(Decision, Outcome, Token.symbol, Token.mint)
        .outerjoin(Outcome, Outcome.token_id == Decision.token_id)
        .outerjoin(Token, Token.id == Decision.token_id)
        .filter(Decision.chain == chain, Decision.kind == "gate")
        .order_by(Decision.id.desc())
        .limit(8_000)
        .all()
    )

    n_pass = 0
    n_veto = 0
    by: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "n": 0,
            "with_outcome": 0,
            "hit2_t0": 0,
            "hit5_t0": 0,
            "peak5_then_dust": 0,
            "peak5_held_sellable": 0,
            "sum_peak_t0": 0.0,
            "sum_last_t0": 0.0,
            "n_peak_t0": 0,
            "hit2_gate": 0,
            "hit5_gate": 0,
            "dust_gate": 0,
            "sum_peak_gate": 0.0,
            "sum_last_gate": 0.0,
            "n_gate_entry": 0,
            "examples_held": [],
        }
    )

    for gate, outcome, symbol, mint in gates:
        veto = (gate.veto or "").strip()
        if not veto:
            n_pass += 1
            continue
        n_veto += 1
        row = by[veto]
        row["n"] += 1
        if outcome is None:
            continue
        t0 = float(outcome.t0_mcap or 0.0)
        mx = float(outcome.max_mcap or 0.0)
        last = float(outcome.last_mcap or 0.0)
        liq = float(outcome.last_liq or 0.0)
        if t0 <= 0:
            continue
        row["with_outcome"] += 1
        peak_x = mx / t0
        last_x = last / t0 if last > 0 else 0.0
        row["sum_peak_t0"] += peak_x
        row["sum_last_t0"] += last_x
        row["n_peak_t0"] += 1
        if mx >= 2 * t0:
            row["hit2_t0"] += 1
        if mx >= 5 * t0:
            row["hit5_t0"] += 1
            if last > 0 and last < 0.5 * t0:
                row["peak5_then_dust"] += 1
            if last >= t0 and liq >= 5000:
                row["peak5_held_sellable"] += 1
                if len(row["examples_held"]) < 6:
                    row["examples_held"].append(
                        {
                            "symbol": symbol or "",
                            "mint": gate.mint or mint or "",
                            "peak_x": round(peak_x, 1),
                            "last_x": round(last_x, 2),
                            "last_liq": round(liq),
                        }
                    )
        entry = float(gate.entry_mcap or 0.0)
        if entry > 0:
            row["n_gate_entry"] += 1
            row["sum_peak_gate"] += mx / entry
            row["sum_last_gate"] += (last / entry) if last > 0 else 0.0
            if mx >= 2 * entry:
                row["hit2_gate"] += 1
            if mx >= 5 * entry:
                row["hit5_gate"] += 1
            if last > 0 and last < 0.5 * entry:
                row["dust_gate"] += 1

    shadow_rows = (
        session.query(PaperFill, Decision.veto)
        .outerjoin(Decision, Decision.id == PaperFill.decision_id)
        .filter(PaperFill.chain == chain, PaperFill.line == "shadow_late", PaperFill.status == "closed")
        .order_by(PaperFill.id.desc())
        .limit(4_000)
        .all()
    )
    shadow_by: dict[str, dict[str, Any]] = defaultdict(lambda: {"n": 0, "wins": 0, "sum_ret": 0.0})
    for fill, veto in shadow_rows:
        key = (veto or "").strip() or "?"
        s = shadow_by[key]
        s["n"] += 1
        s["sum_ret"] += float(fill.return_pct or 0.0)
        entry = float(fill.entry_mcap or 0.0)
        if entry > 0 and float(fill.max_mcap or 0.0) >= 2.0 * entry:
            s["wins"] += 1

    needles: list[dict[str, Any]] = []
    watch: list[str] = []
    keep_hard: list[str] = []
    for veto, row in sorted(by.items(), key=lambda kv: -kv[1]["n"]):
        n5 = int(row["hit5_t0"])
        held = int(row["peak5_held_sellable"])
        dust5 = int(row["peak5_then_dust"])
        held_rate = (held / n5) if n5 else None
        dust_rate = (dust5 / n5) if n5 else None
        n_peak = int(row["n_peak_t0"]) or 0
        klass = _cls(veto)
        sh = shadow_by.get(veto)
        shadow = None
        if sh and sh["n"]:
            shadow = {
                "n": sh["n"],
                "wins": sh["wins"],
                "avg_return_pct": round(sh["sum_ret"] / sh["n"], 1),
            }
        keep = True
        note = ""
        if klass == "hard":
            enough = n5 >= WATCH_MIN_PEAK5
            if enough and held_rate is not None and held_rate >= WATCH_HELD_RATE:
                watch.append(veto)
                note = (
                    f"Watch: {held}/{n5} peak≥5× still held sellable "
                    f"({held_rate:.0%}) — review carve-out, do not auto-buy."
                )
                keep = True  # still keep until explicit policy change
            else:
                keep_hard.append(veto)
                if not enough and n5:
                    note = (
                        f"Keep (thin peak5 n={n5}<{WATCH_MIN_PEAK5}): "
                        f"held sellable {held}/{n5}; need more sample before watch."
                    )
                else:
                    note = (
                        f"Keep: peak≥5× then dust {dust5}/{n5}"
                        + (f" ({dust_rate:.0%})" if dust_rate is not None else "")
                        + f"; held sellable {held}/{n5}."
                    )
        elif klass == "soft_shadow":
            avg = shadow["avg_return_pct"] if shadow else None
            if avg is not None and avg > SOFT_SHADOW_OK_MAX_AVG and chain == "sol":
                note = f"Soft shadow avg moonbag {avg}% — unexpected on Sol; re-check."
                watch.append(veto)
            elif avg is not None:
                note = (
                    f"Soft/late → shadow only (avg moonbag {avg}%). "
                    "Not a buy list even when RH shadow prints green."
                )
            else:
                note = "Soft/late → shadow only. Not a buy."
        else:
            note = "Tape/book fail — no fill."

        needles.append(
            {
                "veto": veto,
                "class": klass,
                "n": int(row["n"]),
                "with_outcome": int(row["with_outcome"]),
                "hit2_t0": int(row["hit2_t0"]),
                "hit5_t0": n5,
                "peak5_then_dust": dust5,
                "peak5_held_sellable": held,
                "peak5_dust_rate": round(dust_rate, 4) if dust_rate is not None else None,
                "peak5_held_sellable_rate": round(held_rate, 4) if held_rate is not None else None,
                "avg_peak_t0": round(row["sum_peak_t0"] / n_peak, 2) if n_peak else None,
                "avg_last_t0": round(row["sum_last_t0"] / n_peak, 2) if n_peak else None,
                "hit2_from_gate": int(row["hit2_gate"]),
                "hit5_from_gate": int(row["hit5_gate"]),
                "dust_from_gate": int(row["dust_gate"]),
                "avg_peak_from_gate": (
                    round(row["sum_peak_gate"] / row["n_gate_entry"], 2) if row["n_gate_entry"] else None
                ),
                "avg_last_from_gate": (
                    round(row["sum_last_gate"] / row["n_gate_entry"], 2) if row["n_gate_entry"] else None
                ),
                "shadow": shadow,
                "keep": keep,
                "note": note,
                "examples_held": row["examples_held"],
            }
        )

    soft_shadow_sol_ok = True
    if chain == "sol":
        for n in needles:
            if n["class"] != "soft_shadow":
                continue
            sh = n.get("shadow") or {}
            if sh.get("avg_return_pct") is not None and sh["avg_return_pct"] > SOFT_SHADOW_OK_MAX_AVG:
                soft_shadow_sol_ok = False

    hijack = next((n for n in needles if n["veto"] == "hijack"), None)
    return {
        "chain": chain,
        "at": datetime.now(timezone.utc).isoformat(),
        "n_gate_pass": n_pass,
        "n_gate_veto": n_veto,
        "watch_held_rate": WATCH_HELD_RATE,
        "needles": needles,
        "verdict": {
            "keep_hard": keep_hard,
            "watch": watch,
            "soft_shadow_sol_ok": soft_shadow_sol_ok if chain == "sol" else None,
            "hijack_keep": bool(
                hijack is None
                or int(hijack.get("hit5_t0") or 0) < WATCH_MIN_PEAK5
                or (hijack.get("peak5_held_sellable_rate") or 0) < WATCH_HELD_RATE
            ),
            "summary": (
                "Hard vetoes stay. Sol hijack peaks then dusts — keep refuse. "
                "Soft/late (pre-pumped / start-high / late chase) stay shadow-only. "
                "Watch only fires with peak5 n≥"
                f"{WATCH_MIN_PEAK5}; never auto-promotes a hard veto into a buy."
            ),
        },
        "policy": {
            "hard_needles": list(PAPER_HARD_NEEDLES),
            "soft_shadow": sorted(PAPER_SHADOW_VETOES),
            "rh_skips_hard": ["start-high", "pre-pumped", "prepumped"],
            "watch_held_rate": WATCH_HELD_RATE,
            "watch_min_peak5": WATCH_MIN_PEAK5,
        },
    }
