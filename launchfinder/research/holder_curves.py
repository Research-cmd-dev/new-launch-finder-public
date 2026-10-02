"""Holder concentration curves over the first hour — Learn/shadow only.

SI study: top10 profile (~25–35% printers vs ~92% dud median) separates
better than raw holder count. Curves use TapeBar.holders + Research
top10 snapshots; never open fills; do not grow FEATURE_NAMES.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..models import Research, Token
from .early_book import tape_minute_trajectory

# Printer band from SI cohort (26% / 35%); dud median ~92%.
ORGANIC_TOP10_LO = 15.0
ORGANIC_TOP10_HI = 45.0
CONCENTRATED_TOP10 = 70.0


def _research_top10(research: Research | None) -> float | None:
    if research is None:
        return None
    top10 = float(research.top10_pct or 0.0)
    if top10 > 0:
        return top10
    try:
        raw = json.loads(research.raw_json or "{}")
    except json.JSONDecodeError:
        return None
    holders = raw.get("holders") if isinstance(raw, dict) else {}
    if not isinstance(holders, dict):
        return None
    try:
        v = float(holders.get("top10_pct") or 0.0)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _top10_history(research: Research | None) -> list[dict[str, Any]]:
    """Optional top10 series if research.raw_json.holders.top10_history exists."""
    if research is None:
        return []
    try:
        raw = json.loads(research.raw_json or "{}")
    except json.JSONDecodeError:
        return []
    holders = raw.get("holders") if isinstance(raw, dict) else {}
    if not isinstance(holders, dict):
        return []
    hist = holders.get("top10_history")
    if not isinstance(hist, list):
        return []
    out: list[dict[str, Any]] = []
    for row in hist:
        if not isinstance(row, dict):
            continue
        try:
            pct = float(row.get("top10_pct") or row.get("pct") or 0.0)
        except (TypeError, ValueError):
            continue
        if pct <= 0:
            continue
        out.append({"top10_pct": pct, "at": str(row.get("at") or "")})
    return out


def concentration_class(top10_pct: float | None) -> str:
    if top10_pct is None or top10_pct <= 0:
        return "unknown"
    if ORGANIC_TOP10_LO <= float(top10_pct) <= ORGANIC_TOP10_HI:
        return "organic_band"
    if float(top10_pct) >= CONCENTRATED_TOP10:
        return "concentrated"
    if float(top10_pct) < ORGANIC_TOP10_LO:
        return "very_dispersed"
    return "elevated"


def holder_count_curve(
    session: Session,
    chain: str,
    mint: str,
    *,
    minutes: int = 60,
) -> list[dict[str, Any]]:
    """First-hour holder counts from TapeBar (age_min, holders)."""
    tape = tape_minute_trajectory(session, chain, mint, minutes=minutes)
    return [
        {"age_min": b["age_min"], "holders": int(b.get("holders") or 0), "minute": b.get("minute")}
        for b in tape
        if int(b.get("holders") or 0) > 0
    ]


def _at_age(curve: list[dict[str, Any]], age: float) -> int | None:
    hit = next((c for c in curve if float(c["age_min"]) >= age), None)
    if hit is None:
        return int(curve[-1]["holders"]) if curve else None
    return int(hit["holders"])


def holder_concentration_curve(
    session: Session,
    token: Token,
    research: Research | None = None,
    *,
    minutes: int = 60,
) -> dict[str, Any]:
    """Side-key bundle: holder growth + top10 profile over first hour."""
    if research is None:
        research = (
            session.query(Research).filter(Research.token_id == token.id).one_or_none()
        )
    curve = holder_count_curve(session, token.chain, token.mint, minutes=minutes)
    top10 = _research_top10(research)
    top10_hist = _top10_history(research)
    n0 = int(curve[0]["holders"]) if curve else int(getattr(research, "holder_count", 0) or 0)
    n15 = _at_age(curve, 15.0)
    n60 = _at_age(curve, 60.0) if curve else n0
    growth_15 = None
    if n0 and n15 is not None and n0 > 0:
        growth_15 = float(n15) / float(n0)
    growth_60 = None
    if n0 and n60 is not None and n0 > 0:
        growth_60 = float(n60) / float(n0)
    cls = concentration_class(top10)
    # Printer-like: organic top10 band; growth alone is weak (SI clone counterexample).
    printerish = 1.0 if cls == "organic_band" else 0.0
    if cls == "concentrated":
        printerish = 0.0
    return {
        "curve": curve[:minutes],
        "top10_pct": top10,
        "top10_history": top10_hist,
        "concentration_class": cls,
        "holders_t0": n0 or None,
        "holders_15m": n15,
        "holders_60m": n60,
        "holder_growth_15m": growth_15,
        "holder_growth_60m": growth_60,
        "n_curve_points": len(curve),
        "organic_top10_band": printerish,
        "paper_only": True,
        "side_key": "holder_concentration_curve",
        "note": "top10 profile > raw holder count (SI study); curves from TapeBar",
    }


def append_top10_history(
    holders: dict[str, Any],
    top10_pct: float,
    *,
    now: datetime | None = None,
    max_points: int = 24,
) -> dict[str, Any]:
    """Optional writer for research refresh paths — stores top10 series.

    Safe to call from Learn/shadow instrumentation; does not change gates.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        pct = float(top10_pct or 0.0)
    except (TypeError, ValueError):
        return holders
    if pct <= 0:
        return holders
    hist = holders.get("top10_history") if isinstance(holders.get("top10_history"), list) else []
    cleaned: list[dict[str, Any]] = []
    for row in hist:
        if isinstance(row, dict) and float(row.get("top10_pct") or 0) > 0:
            cleaned.append({"top10_pct": float(row["top10_pct"]), "at": str(row.get("at") or "")})
    last = cleaned[-1] if cleaned else None
    if last and abs(float(last["top10_pct"]) - pct) < 0.05:
        cleaned[-1] = {"top10_pct": pct, "at": now.isoformat()}
    else:
        cleaned.append({"top10_pct": pct, "at": now.isoformat()})
    holders["top10_history"] = cleaned[-max_points:]
    holders["top10_pct"] = pct
    return holders
