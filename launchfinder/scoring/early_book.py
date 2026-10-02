"""One paperV1 shadow knob: early runner-book (stack-v204).

Frozen first-sight / paper_miss_snap book+holder features only.
Would-have table for Learn. Does **not** open paper_v1.

Does not lower ``PAPER_V1_SOL_LIVE`` / ``LIVE_PAPER_HI`` (0.50).
Does not add a meme-alone soft path.
Evidence on the live miss cohort (29 runners / ~225 duds) is thin —
precision is measured; real opens stay off (``EARLY_BOOK_OPEN = False``).
"""

from __future__ import annotations

from typing import Any

from .paper_gate import PAPER_FRESH_HOLDERS, PAPER_FRESH_LIQ
from .paper_v1 import PAPER_V1_LEFTOVER_MCAP, PAPER_V1_SOL_LIVE

# Side keys on Decision.features_json — not FEATURE_NAMES.
EARLY_BOOK_KEY = "paper_early_book"
EARLY_BOOK_WHY_KEY = "paper_early_book_why"
EARLY_BOOK_OPEN = False

# Live miss-cohort separators (2026-10-02, 14d): top1_inv +0.085, volume_n
# +0.055, migrate_speed +0.032. fresh_wallet_n / liquidity_n deltas are
# negative — do not require high fresh-wallet or extra liq.
EARLY_BOOK_TOP1_INV_MIN = 0.55
EARLY_BOOK_VOLUME_N_MIN = 0.80
EARLY_BOOK_ORGANIC_MIN = 1.0
EARLY_BOOK_PRECISION_THIN = 0.35
EARLY_BOOK_MIN_RUNNERS = 40

# Named floors reused from the wide fresh-fat bypass — not new numbers.
EARLY_BOOK_LIQ_MIN = PAPER_FRESH_LIQ
EARLY_BOOK_HOLDERS_MIN = PAPER_FRESH_HOLDERS


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def early_runner_book(
    snap: dict[str, Any] | None,
    *,
    t0_mcap: float = 0.0,
) -> dict[str, Any]:
    """True when a frozen early snap looks like the 7cYaQc runner book.

    Book/holders only. Live 0.50 is not read. Meme/thesis is not read.
    Fail-closed on missing keys so sparse duds do not inflate recall.
    """
    bits = snap if isinstance(snap, dict) else {}
    why: list[str] = []
    if float(t0_mcap or 0.0) >= PAPER_V1_LEFTOVER_MCAP:
        return {"ok": False, "why": ["leftover"], "knob": "early_runner_book"}
    top1 = _num(bits.get("top1_inv"))
    if top1 is None and _num(bits.get("top1_pct")) is not None:
        # Inverse of freeze_paper_miss_features: top1_pct = (1 - top1_inv) * 50
        top1 = 1.0 - float(bits["top1_pct"]) / 50.0
    volume = _num(bits.get("volume_n"))
    organic = _num(bits.get("organic_book"))
    liq = _num(bits.get("liq"))
    holders = _num(bits.get("holders"))
    if top1 is None or top1 < EARLY_BOOK_TOP1_INV_MIN:
        return {"ok": False, "why": ["top1_inv"], "knob": "early_runner_book"}
    why.append("top1_inv")
    if volume is None or volume < EARLY_BOOK_VOLUME_N_MIN:
        return {"ok": False, "why": ["volume_n"], "knob": "early_runner_book"}
    why.append("volume_n")
    if organic is None or organic < EARLY_BOOK_ORGANIC_MIN:
        return {"ok": False, "why": ["organic_book"], "knob": "early_runner_book"}
    why.append("organic_book")
    if liq is None or liq < EARLY_BOOK_LIQ_MIN:
        return {"ok": False, "why": ["liq"], "knob": "early_runner_book"}
    why.append("liq")
    if holders is None or holders < EARLY_BOOK_HOLDERS_MIN:
        return {"ok": False, "why": ["holders"], "knob": "early_runner_book"}
    why.append("holders")
    return {
        "ok": True,
        "why": why,
        "knob": "early_runner_book",
        "live_floor": PAPER_V1_SOL_LIVE,
        "open": EARLY_BOOK_OPEN,
    }


def stamp_early_book_side_key(
    features: dict[str, Any] | None,
    snap: dict[str, Any] | None = None,
    *,
    t0_mcap: float = 0.0,
) -> dict[str, Any] | None:
    """Write-once ``paper_early_book`` when the frozen snap matches.

    Does not grow FEATURE_NAMES. Returns None when already stamped or no hit.
    """
    feat = dict(features) if isinstance(features, dict) else {}
    if feat.get(EARLY_BOOK_KEY):
        return None
    hit = early_runner_book(snap if isinstance(snap, dict) else feat, t0_mcap=t0_mcap)
    if not hit.get("ok"):
        return None
    feat[EARLY_BOOK_KEY] = True
    feat[EARLY_BOOK_WHY_KEY] = list(hit.get("why") or [])
    return feat


def has_early_book(features: dict[str, Any] | None) -> bool:
    feat = features if isinstance(features, dict) else {}
    return bool(feat.get(EARLY_BOOK_KEY))


def would_have_early_book(
    winners: list[dict[str, Any]],
    duds: list[dict[str, Any]],
) -> dict[str, Any]:
    """Precision / recall of the early-book knob vs miss-cohort snaps.

    Shadow / would-have only. ``open`` stays false while evidence is thin
    or ``EARLY_BOOK_OPEN`` is false.
    """
    win_hits = [s for s in winners if early_runner_book(s).get("ok")]
    dud_hits = [s for s in duds if early_runner_book(s).get("ok")]
    tp = len(win_hits)
    fp = len(dud_hits)
    fn = max(0, len(winners) - tp)
    precision = round(tp / (tp + fp), 4) if (tp + fp) else None
    recall = round(tp / (tp + fn), 4) if (tp + fn) else None
    thin = (
        len(winners) < EARLY_BOOK_MIN_RUNNERS
        or precision is None
        or float(precision) < EARLY_BOOK_PRECISION_THIN
    )
    return {
        "knob": "early_runner_book",
        "paper_only": True,
        "open": False,
        "live_floor_unchanged": PAPER_V1_SOL_LIVE,
        "meme_alone": False,
        "n_runners": len(winners),
        "n_duds": len(duds),
        "n_hit_runners": tp,
        "n_hit_duds": fp,
        "precision": precision,
        "recall": recall,
        "evidence": "thin" if thin else "enough",
        "thresholds": {
            "top1_inv": EARLY_BOOK_TOP1_INV_MIN,
            "volume_n": EARLY_BOOK_VOLUME_N_MIN,
            "organic_book": EARLY_BOOK_ORGANIC_MIN,
            "liq": EARLY_BOOK_LIQ_MIN,
            "holders": EARLY_BOOK_HOLDERS_MIN,
        },
        "note": (
            "Would-have-opened shadow. Does not open paperV1. Live 0.50 "
            "and meme-alone stay unchanged. Thin evidence → table only."
        ),
    }
