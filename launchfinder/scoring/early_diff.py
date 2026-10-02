"""Early-launch feature-divergence ranker from the SI cohort Learn study.

Paper-safe / Learn-shadow only:
- Does not grow FEATURE_NAMES
- Does not open fills or change production gates
- Serial deployer is a prior, never a hard block
- Surfaces top of ranked list for closer look
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from ..chains import normalize_chain
from ..models import Decision, HuntCard, Research, ScanState, Token, utcnow
from ..research.early_book import early_book_shape, tape_minute_trajectory
from ..research.holder_curves import concentration_class, holder_concentration_curve
from ..research.liq_behavior import liq_behavior, liq_path_from_tape
from ..research.social_velocity import classify_social_ramp, social_velocity
from ..research.wallet_clusters import wallet_clusters

# Bundled next to exemplars.json (launchfinder/data/ is gitignored).
STUDY_DIR = Path(__file__).resolve().parent / "si_cohort"
FINDINGS_PATH = STUDY_DIR / "findings.json"
COHORT_PATH = STUDY_DIR / "cohort.csv"

# Features used for divergence scoring (decision-time separators from study).
# Direction: +1 means higher = more printer-like; -1 means lower = more printer-like.
RANK_FEATURES: list[tuple[str, float]] = [
    ("rising_book_15m", 1.0),
    ("mcap_t0_to_early_ratio", 1.0),
    ("bar_mcap_15m_ratio", 1.0),
    ("dump_on_volume", -1.0),
    ("top10_pct", -1.0),
    ("fomo_late_hot", 1.0),
    ("serial_pattern_break", 1.0),
    ("organic_top10_band", 1.0),
    ("liq_build_score", 1.0),
    ("liq_farm_score", -1.0),
    ("social_organic", 0.3),  # weak separator per study
    ("social_shill", -0.3),
]

TOP_N = 12
# First-hour pass: above dud-median neighborhood. Closer-look survivors only.
FIRST_HOUR_PASS_SCORE = 0.45
# Desk alert when a launch newly crosses this (paper-safe toast, not a trade).
ALERT_THRESHOLD = 0.55
SURVIVOR_HOURS = (2, 3, 4)
ALERT_FEED_KEY = "ediff:alert_feed"
ALERT_PING_PREFIX = "ediff:ping:"


def load_findings() -> dict[str, Any]:
    if not FINDINGS_PATH.exists():
        return {"comparisons": [], "paper_safe": True}
    try:
        return json.loads(FINDINGS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {"comparisons": [], "paper_safe": True}


def load_cohort_rows() -> list[dict[str, Any]]:
    if not COHORT_PATH.exists():
        return []
    with COHORT_PATH.open(newline="") as fh:
        return list(csv.DictReader(fh))


def _f(row: dict[str, Any], key: str) -> float | None:
    raw = row.get(key)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def study_baselines(findings: dict[str, Any] | None = None) -> dict[str, dict[str, float]]:
    """printer_median / dud_median / cliffs_delta per feature from findings."""
    findings = findings or load_findings()
    out: dict[str, dict[str, float]] = {}
    for row in findings.get("comparisons") or []:
        feat = str(row.get("feature") or "")
        if not feat:
            continue
        try:
            out[feat] = {
                "printer_median": float(row.get("printer_median") or 0.0),
                "dud_median": float(row.get("dud_median") or 0.0),
                "cliffs_delta": float(row.get("cliffs_delta") or 0.0),
                "abs_delta": float(row.get("abs_delta") or 0.0),
                "n_printer": float(row.get("n_printer") or 0.0),
                "n_dud": float(row.get("n_dud") or 0.0),
            }
        except (TypeError, ValueError):
            continue
    return out


def _norm_toward_printer(
    value: float | None,
    *,
    printer: float,
    dud: float,
    direction: float,
) -> float | None:
    """Map value to [0,1] where 1 = printer-like, 0 = dud-like."""
    if value is None:
        return None
    span = abs(printer - dud)
    if span < 1e-9:
        return 0.5
    if direction >= 0:
        # higher better
        lo, hi = min(dud, printer), max(dud, printer)
        if printer >= dud:
            t = (float(value) - lo) / (hi - lo + 1e-12)
        else:
            t = (hi - float(value)) / (hi - lo + 1e-12)
    else:
        # lower better (top10, dump_on_volume)
        if printer <= dud:
            t = (dud - float(value)) / (dud - printer + 1e-12)
        else:
            t = (float(value) - dud) / (printer - dud + 1e-12)
    return max(0.0, min(1.0, t))


def divergence_score(
    features: dict[str, float | None],
    baselines: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """Weighted sum of printer-likeness on SI separators."""
    baselines = baselines or study_baselines()
    parts: list[dict[str, Any]] = []
    num = den = 0.0
    for feat, weight in RANK_FEATURES:
        val = features.get(feat)
        base = baselines.get(feat)
        if base is None:
            # Fallback midpoints when study has no row
            defaults = {
                "organic_top10_band": (1.0, 0.0),
                "liq_build_score": (0.7, 0.1),
                "liq_farm_score": (0.0, 0.6),
                "social_organic": (0.3, 0.2),
                "social_shill": (0.1, 0.4),
                "serial_pattern_break": (1.0, 0.0),
            }
            if feat not in defaults or val is None:
                continue
            printer, dud = defaults[feat]
            cliffs = abs(weight)
        else:
            printer = float(base["printer_median"])
            dud = float(base["dud_median"])
            cliffs = float(base.get("abs_delta") or abs(base.get("cliffs_delta") or 0.5))
        n = _norm_toward_printer(val, printer=printer, dud=dud, direction=weight)
        if n is None:
            continue
        w = abs(weight) * max(0.15, cliffs)
        parts.append(
            {
                "feature": feat,
                "value": val,
                "printer_likeness": round(n, 3),
                "weight": round(w, 3),
            }
        )
        num += w * n
        den += w
    score = (num / den) if den > 0 else 0.0
    return {
        "score": round(score, 4),
        "parts": sorted(parts, key=lambda p: -abs(p["weight"] * (p["printer_likeness"] - 0.5))),
        "n_features": len(parts),
    }


def extract_live_features(
    session: Session,
    token: Token,
    research: Research | None = None,
) -> dict[str, Any]:
    """Pull side-key bundles and flatten rank features."""
    if research is None:
        research = (
            session.query(Research).filter(Research.token_id == token.id).one_or_none()
        )
    top10 = float(research.top10_pct or 0.0) if research else None
    if top10 is not None and top10 <= 0:
        top10 = None
    book = early_book_shape(session, token, top10_pct=top10)
    holders = holder_concentration_curve(session, token, research)
    social = social_velocity(session, token, research)
    wallets = wallet_clusters(session, token, research)
    liq = liq_behavior(session, token)

    serial = bool(wallets.get("serial_prior"))
    rising = float(book.get("rising_book_15m") or 0.0)
    dump = float(book.get("dump_on_volume") or 0.0)
    organic_top10 = float(holders.get("organic_top10_band") or 0.0)
    # Serial pattern break: serial prior + rising book + organic top10 (SI Super Intelligence).
    serial_break = 1.0 if (serial and rising >= 1.0 and organic_top10 >= 1.0 and dump < 1.0) else 0.0
    if serial and rising >= 1.0 and dump < 1.0 and top10 is not None and top10 <= 45:
        serial_break = 1.0

    flat = {
        "rising_book_15m": rising,
        "mcap_t0_to_early_ratio": book.get("mcap_t0_to_early_ratio"),
        "bar_mcap_15m_ratio": book.get("bar_mcap_15m_ratio"),
        "dump_on_volume": dump,
        "top10_pct": top10 if top10 is not None else holders.get("top10_pct"),
        "fomo_late_hot": float(book.get("fomo_late_hot") or 0.0),
        "serial_pattern_break": serial_break,
        "organic_top10_band": organic_top10,
        "liq_build_score": float(liq.get("build_score") or 0.0),
        "liq_farm_score": float(liq.get("farm_score") or 0.0),
        "social_organic": float(social.get("organic_score") or 0.0),
        "social_shill": float(social.get("shill_score") or 0.0),
    }
    return {
        "flat": flat,
        "early_book": book,
        "holder_curves": holders,
        "social_velocity": social,
        "wallet_clusters": wallets,
        "liq_behavior": liq,
    }


def score_token(
    session: Session,
    token: Token,
    research: Research | None = None,
    baselines: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    bundle = extract_live_features(session, token, research)
    div = divergence_score(bundle["flat"], baselines)
    return {
        "mint": token.mint,
        "symbol": token.symbol or "",
        "name": token.name or "",
        "chain": token.chain,
        "divergence_score": div["score"],
        "n_features": div["n_features"],
        "top_parts": div["parts"][:6],
        "features": bundle["flat"],
        "bundles": {
            "early_book": {
                k: bundle["early_book"].get(k)
                for k in (
                    "rising_book_15m",
                    "dump_on_volume",
                    "fomo_late_hot",
                    "mcap_t0_to_early_ratio",
                    "liq_t0_to_early_ratio",
                    "bar_mcap_15m_ratio",
                )
            },
            "holders": {
                k: bundle["holder_curves"].get(k)
                for k in (
                    "top10_pct",
                    "concentration_class",
                    "holders_t0",
                    "holders_15m",
                    "holder_growth_15m",
                    "organic_top10_band",
                    "n_curve_points",
                )
            },
            "social": {
                k: bundle["social_velocity"].get(k)
                for k in ("label", "velocity", "organic_score", "shill_score", "followers")
            },
            "wallets": {
                "serial_prior": bundle["wallet_clusters"].get("serial_prior"),
                "serial_hard_block": False,
                "cluster_heat": bundle["wallet_clusters"].get("cluster_heat"),
                "multi_mint_wallets": (bundle["wallet_clusters"].get("early_overlap") or {}).get(
                    "multi_mint_wallets"
                ),
            },
            "liq": {
                k: bundle["liq_behavior"].get(k)
                for k in ("label", "build_score", "farm_score", "liq_ratio", "pull_rate")
            },
        },
        "paper_only": True,
    }


def rank_recent(
    session: Session,
    chain: str = "sol",
    *,
    hours: int = 48,
    limit: int = 80,
    top_n: int = TOP_N,
) -> dict[str, Any]:
    """Rank recent Hunt / Decision names by SI divergence. Read-only."""
    chain = normalize_chain(chain)
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=max(1, int(hours)))
    baselines = study_baselines()

    cards = (
        session.query(HuntCard, Token, Research)
        .join(Token, Token.id == HuntCard.token_id)
        .outerjoin(Research, Research.token_id == Token.id)
        .filter(HuntCard.chain == chain, HuntCard.first_seen_at >= since)
        .order_by(HuntCard.first_seen_at.desc())
        .limit(limit)
        .all()
    )
    if not cards:
        # Fallback: recent entry decisions
        rows = (
            session.query(Decision, Token, Research)
            .join(Token, Token.id == Decision.token_id)
            .outerjoin(Research, Research.token_id == Token.id)
            .filter(
                Decision.chain == chain,
                Decision.kind == "entry",
                Decision.at >= since,
            )
            .order_by(Decision.at.desc())
            .limit(limit)
            .all()
        )
        cards = [(None, t, r) for _, t, r in rows]

    scored: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _card, token, research in cards:
        if token.mint in seen:
            continue
        seen.add(token.mint)
        try:
            scored.append(score_token(session, token, research, baselines))
        except Exception as exc:  # noqa: BLE001 — shadow path must not break Learn
            scored.append(
                {
                    "mint": token.mint,
                    "symbol": token.symbol or "",
                    "divergence_score": 0.0,
                    "error": str(exc)[:120],
                    "paper_only": True,
                }
            )

    scored.sort(key=lambda r: (-float(r.get("divergence_score") or 0.0), r.get("symbol") or ""))
    top = scored[: max(1, int(top_n))]
    findings = load_findings()
    return {
        "paper_only": True,
        "side_key": "early_diff",
        "study": findings.get("study") or "si-meme-copy-printer-vs-dud",
        "serial_policy": findings.get("serial_policy")
        or "PRIOR only — never hard-block.",
        "n_scanned": len(scored),
        "top_n": len(top),
        "hours": hours,
        "separators": [s["signal"] for s in (findings.get("top3_separating_signals") or [])],
        "items": top,
        "note": (
            "Feature-divergence vs SI cohort printers. Closer look only — "
            "not a buy list. No fills. No gate changes. N=2 printers descriptive."
        ),
        "not_safe_to_ship": findings.get("not_safe_to_ship") or [],
    }


def score_cohort_row(row: dict[str, Any], baselines: dict[str, dict[str, float]] | None = None) -> dict[str, Any]:
    """Offline score one CSV cohort row (no DB)."""
    flat = {
        "rising_book_15m": _f(row, "rising_book_15m"),
        "mcap_t0_to_early_ratio": _f(row, "mcap_t0_to_early_ratio"),
        "bar_mcap_15m_ratio": _f(row, "bar_mcap_15m_ratio"),
        "dump_on_volume": _f(row, "dump_on_volume"),
        "top10_pct": _f(row, "top10_pct"),
        "fomo_late_hot": _f(row, "fomo_late_hot"),
        "serial_pattern_break": _f(row, "serial_pattern_break") or 0.0,
        "organic_top10_band": (
            1.0
            if (_f(row, "top10_pct") is not None and 15.0 <= float(_f(row, "top10_pct")) <= 45.0)
            else 0.0
        ),
        "liq_build_score": None,
        "liq_farm_score": None,
        "social_organic": 0.0,
        "social_shill": 0.0,
    }
    # Derive liq scores from available ratios when tape absent.
    liq_r = _f(row, "liq_t0_to_early_ratio")
    dump = flat["dump_on_volume"] or 0.0
    if liq_r is not None and liq_r >= 1.1 and dump < 1:
        flat["liq_build_score"] = min(1.0, 0.3 + 0.4 * (liq_r - 1.0))
        flat["liq_farm_score"] = 0.0
    elif dump >= 1:
        flat["liq_build_score"] = 0.0
        flat["liq_farm_score"] = 0.7
    else:
        flat["liq_build_score"] = 0.2
        flat["liq_farm_score"] = 0.2

    div = divergence_score(flat, baselines)
    return {
        "symbol": row.get("symbol") or "",
        "name": row.get("name") or "",
        "mint": row.get("mint") or "",
        "label": row.get("label") or "",
        "multiple": _f(row, "multiple"),
        "divergence_score": div["score"],
        "top_parts": div["parts"][:5],
        "features": flat,
        "paper_only": True,
    }


def prove_on_cohort() -> dict[str, Any]:
    """Rank SI cohort offline; printers should outrank dud median."""
    baselines = study_baselines()
    rows = load_cohort_rows()
    scored = [score_cohort_row(r, baselines) for r in rows]
    scored.sort(key=lambda r: (-float(r["divergence_score"]), -(r.get("multiple") or 0)))
    printers = [r for r in scored if r["label"] == "printer"]
    duds = [r for r in scored if r["label"] == "dud"]
    adj = [r for r in scored if r["label"] == "adjacent_printer"]
    p_med = _median([r["divergence_score"] for r in printers])
    d_med = _median([r["divergence_score"] for r in duds])
    # Rank positions of printers among all
    rank_by_mint = {r["mint"]: i + 1 for i, r in enumerate(scored)}
    return {
        "paper_only": True,
        "n": len(scored),
        "n_printers": len(printers),
        "n_duds": len(duds),
        "n_adjacent": len(adj),
        "printer_score_median": p_med,
        "dud_score_median": d_med,
        "separation_ok": bool(
            p_med is not None and d_med is not None and p_med > d_med
        ),
        "printers": [
            {
                "symbol": r["symbol"],
                "mint": r["mint"][:12] + "…",
                "score": r["divergence_score"],
                "rank": rank_by_mint.get(r["mint"]),
                "multiple": r["multiple"],
            }
            for r in printers
        ],
        "adjacent": [
            {
                "symbol": r["symbol"],
                "score": r["divergence_score"],
                "rank": rank_by_mint.get(r["mint"]),
                "multiple": r["multiple"],
            }
            for r in adj
        ],
        "top12": [
            {
                "symbol": r["symbol"],
                "label": r["label"],
                "score": r["divergence_score"],
                "multiple": r["multiple"],
            }
            for r in scored[:12]
        ],
        "note": "Offline proof on bundled SI cohort CSV. Descriptive N=2.",
    }


def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    mid = len(s) // 2
    if len(s) % 2:
        return round(s[mid], 4)
    return round((s[mid - 1] + s[mid]) / 2.0, 4)


def passed_first_hour_filter(
    row: dict[str, Any],
    *,
    min_score: float = FIRST_HOUR_PASS_SCORE,
) -> bool:
    """True when first-hour divergence clears the closer-look bar.

    Dump-on-volume without rising book fails even if score is noisy.
    """
    score = float(row.get("divergence_score") or 0.0)
    feat = row.get("features") or {}
    dump = float(feat.get("dump_on_volume") or 0.0)
    rising = float(feat.get("rising_book_15m") or 0.0)
    late_hot = float(feat.get("fomo_late_hot") or 0.0)
    if dump >= 1.0 and rising < 1.0 and late_hot < 1.0:
        return False
    if score >= float(min_score):
        return True
    # Explicit study separators can pass thin-score books
    return rising >= 1.0 or late_hot >= 1.0


def _flat_from_tape_window(
    tape: list[dict[str, Any]],
    *,
    top10_pct: float | None,
    serial_prior: bool = False,
) -> dict[str, float | None]:
    """Rebuild rank features from a truncated minute tape (survivor hours)."""
    if not tape:
        return {
            "rising_book_15m": 0.0,
            "mcap_t0_to_early_ratio": None,
            "bar_mcap_15m_ratio": None,
            "dump_on_volume": 0.0,
            "top10_pct": top10_pct,
            "fomo_late_hot": 0.0,
            "serial_pattern_break": 0.0,
            "organic_top10_band": (
                1.0
                if top10_pct is not None and 15.0 <= float(top10_pct) <= 45.0
                else 0.0
            ),
            "liq_build_score": 0.0,
            "liq_farm_score": 0.0,
            "social_organic": 0.0,
            "social_shill": 0.0,
        }
    first = tape[0]
    at15 = next((b for b in tape if float(b.get("age_min") or 0) >= 14.0), tape[-1])
    m0 = float(first.get("mcap") or 0.0)
    m15 = float(at15.get("mcap") or 0.0)
    l0 = float(first.get("liq") or 0.0)
    l15 = float(at15.get("liq") or 0.0)
    v15 = float(at15.get("vol_h1") or 0.0)
    ratio = (m15 / m0) if m0 > 0 else None
    rising = 0.0
    if ratio is not None and ratio >= 1.5:
        if l0 <= 0 or (l15 / l0 if l0 else 0) >= 1.1 or l15 >= 15_000:
            rising = 1.0
    dump = 1.0 if (ratio is not None and ratio < 0.7 and v15 > 10_000) else 0.0
    late_hot = 1.0 if m0 >= 500_000 and float(first.get("vol_h1") or 0) >= 500_000 else 0.0
    if late_hot and top10_pct is not None and float(top10_pct) > 50:
        late_hot = 0.0
    if late_hot:
        rising = 1.0
    organic = (
        1.0 if top10_pct is not None and 15.0 <= float(top10_pct) <= 45.0 else 0.0
    )
    liq = liq_path_from_tape(tape)
    serial_break = (
        1.0
        if serial_prior and rising >= 1.0 and dump < 1.0 and (organic >= 1.0 or (top10_pct or 99) <= 45)
        else 0.0
    )
    return {
        "rising_book_15m": rising,
        "mcap_t0_to_early_ratio": ratio,
        "bar_mcap_15m_ratio": ratio,
        "dump_on_volume": dump,
        "top10_pct": top10_pct,
        "fomo_late_hot": late_hot,
        "serial_pattern_break": serial_break,
        "organic_top10_band": organic,
        "liq_build_score": float(liq.get("build_score") or 0.0),
        "liq_farm_score": float(liq.get("farm_score") or 0.0),
        "social_organic": 0.0,
        "social_shill": 0.0,
    }


def rescore_at_hour(
    session: Session,
    token: Token,
    hour: int,
    *,
    research: Research | None = None,
    baselines: dict[str, dict[str, float]] | None = None,
    serial_prior: bool = False,
) -> dict[str, Any]:
    """Divergence score using tape truncated to ``hour`` (survivor second pass)."""
    baselines = baselines or study_baselines()
    if research is None:
        research = (
            session.query(Research).filter(Research.token_id == token.id).one_or_none()
        )
    top10 = float(research.top10_pct or 0.0) if research else None
    if top10 is not None and top10 <= 0:
        top10 = None
    tape = tape_minute_trajectory(session, token.chain, token.mint, minutes=int(hour) * 60)
    flat = _flat_from_tape_window(tape, top10_pct=top10, serial_prior=serial_prior)
    div = divergence_score(flat, baselines)
    return {
        "hour": int(hour),
        "divergence_score": div["score"],
        "n_tape_bars": len(tape),
        "features": flat,
        "concentration_class": concentration_class(top10),
        "paper_only": True,
    }


def survivor_hour_rescores(
    session: Session,
    scored_h1: list[dict[str, Any]],
    *,
    hours: tuple[int, ...] = SURVIVOR_HOURS,
    baselines: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """Re-score ONLY first-hour survivors at h2/h3/h4 — not the full universe."""
    baselines = baselines or study_baselines()
    survivors = [r for r in scored_h1 if passed_first_hour_filter(r)]
    items: list[dict[str, Any]] = []
    for row in survivors:
        mint = row.get("mint") or ""
        token = session.query(Token).filter(Token.mint == mint).one_or_none()
        if token is None:
            continue
        research = (
            session.query(Research).filter(Research.token_id == token.id).one_or_none()
        )
        serial = bool(((row.get("bundles") or {}).get("wallets") or {}).get("serial_prior"))
        passes: list[dict[str, Any]] = [
            {
                "hour": 1,
                "divergence_score": float(row.get("divergence_score") or 0.0),
                "passed_filter": True,
            }
        ]
        for h in hours:
            try:
                passes.append(
                    rescore_at_hour(
                        session,
                        token,
                        h,
                        research=research,
                        baselines=baselines,
                        serial_prior=serial,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                passes.append({"hour": h, "error": str(exc)[:100], "divergence_score": None})
        items.append(
            {
                "mint": mint,
                "symbol": row.get("symbol") or "",
                "h1_score": float(row.get("divergence_score") or 0.0),
                "passes": passes,
                "latest_score": next(
                    (p.get("divergence_score") for p in reversed(passes) if p.get("divergence_score") is not None),
                    None,
                ),
                "paper_only": True,
            }
        )
    items.sort(key=lambda r: (-float(r.get("latest_score") or 0.0), r.get("symbol") or ""))
    return {
        "paper_only": True,
        "survivor_only": True,
        "n_h1_scanned": len(scored_h1),
        "n_survivors": len(survivors),
        "n_rescored": len(items),
        "hours": [1, *list(hours)],
        "first_hour_pass_score": FIRST_HOUR_PASS_SCORE,
        "items": items,
        "note": "h2/h3/h4 re-score only for first-hour filter survivors — not full universe.",
    }


def _alert_ping_key(mint: str) -> str:
    return f"{ALERT_PING_PREFIX}{mint}"[:64]


def _load_alert_feed(session: Session) -> list[dict[str, Any]]:
    row = session.query(ScanState).filter(ScanState.key == ALERT_FEED_KEY).one_or_none()
    if row is None:
        return []
    try:
        data = json.loads(row.value or "[]")
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _save_alert_feed(session: Session, feed: list[dict[str, Any]]) -> None:
    payload = json.dumps(feed[:40])
    row = session.query(ScanState).filter(ScanState.key == ALERT_FEED_KEY).one_or_none()
    now = utcnow()
    if row is None:
        # Prefer in-session pending row when autoflush is off.
        for obj in list(session.new) + list(session.dirty):
            if isinstance(obj, ScanState) and obj.key == ALERT_FEED_KEY:
                row = obj
                break
    if row is None:
        session.add(ScanState(key=ALERT_FEED_KEY, value=payload, updated_at=now))
    else:
        row.value = payload
        row.updated_at = now
    session.flush()


def claim_threshold_alert(session: Session, mint: str) -> bool:
    """True once per mint — durable across worker restarts."""
    key = _alert_ping_key(mint)
    pending = {
        str(obj.key)
        for obj in list(session.new) + list(session.dirty)
        if isinstance(obj, ScanState) and obj.key
    }
    if key in pending:
        return False
    if session.query(ScanState).filter(ScanState.key == key).one_or_none() is not None:
        return False
    session.add(ScanState(key=key, value="1", updated_at=utcnow()))
    session.flush()
    return True


def emit_threshold_alerts(
    session: Session,
    scored: list[dict[str, Any]],
    *,
    threshold: float = ALERT_THRESHOLD,
) -> list[dict[str, Any]]:
    """Desk-visible paper-safe alerts when divergence newly crosses threshold.

    Not a trade signal. Not auto-buy. Idempotent via ScanState ping keys.
    """
    feed = _load_alert_feed(session)
    fresh: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc).isoformat()
    for row in scored:
        score = float(row.get("divergence_score") or 0.0)
        if score < float(threshold):
            continue
        mint = str(row.get("mint") or "")
        if not mint:
            continue
        if not claim_threshold_alert(session, mint):
            continue
        alert = {
            "at": now,
            "mint": mint,
            "symbol": row.get("symbol") or "",
            "name": row.get("name") or "",
            "divergence_score": score,
            "threshold": float(threshold),
            "kind": "early_diff_threshold",
            "paper_only": True,
            "not_a_trade_signal": True,
            "message": (
                f"Early-diff closer-look: {row.get('symbol') or mint[:8]} "
                f"crossed {threshold:.2f} (score {score:.2f})"
            ),
        }
        fresh.append(alert)
        feed.insert(0, alert)
    if fresh:
        _save_alert_feed(session, feed)
    return fresh


def list_threshold_alerts(session: Session, *, limit: int = 20) -> list[dict[str, Any]]:
    return _load_alert_feed(session)[: max(1, int(limit))]


def run_early_diff_tick(
    session: Session,
    chain: str = "sol",
    *,
    hours: int = 6,
    limit: int = 60,
) -> dict[str, Any]:
    """Worker tick: rank recent, alert on threshold cross, survivor rescores.

    Paper-safe. Does not open fills or change gates.
    """
    live = rank_recent(session, chain, hours=hours, limit=limit, top_n=TOP_N)
    # Need full scored list for survivors — re-rank with large top_n
    full = rank_recent(session, chain, hours=hours, limit=limit, top_n=limit)
    scored = full.get("items") or []
    new_alerts = emit_threshold_alerts(session, scored)
    survivors = survivor_hour_rescores(session, scored)
    return {
        "paper_only": True,
        "n_scanned": full.get("n_scanned") or len(scored),
        "n_alerts_new": len(new_alerts),
        "alerts_new": new_alerts,
        "alerts": list_threshold_alerts(session),
        "survivors": survivors,
        "top": live.get("items") or [],
        "threshold": ALERT_THRESHOLD,
    }


def early_diff_status(
    session: Session,
    chain: str = "sol",
    *,
    hours: int = 48,
    top_n: int = TOP_N,
    include_cohort_proof: bool = True,
    emit_alerts: bool = True,
) -> dict[str, Any]:
    """Learn API payload: live rank + survivors h2/h3/h4 + alerts + cohort proof."""
    # Score a wider set for survivor filter, then trim top for display.
    wide = rank_recent(session, chain, hours=hours, top_n=max(top_n, 40), limit=80)
    scored = wide.get("items") or []
    top = scored[: max(1, int(top_n))]
    new_alerts: list[dict[str, Any]] = []
    if emit_alerts:
        new_alerts = emit_threshold_alerts(session, scored)
    survivors = survivor_hour_rescores(session, scored)
    out: dict[str, Any] = {
        **wide,
        "items": top,
        "top_n": len(top),
        "workstreams": [
            "feature_divergence_ranker",
            "holder_concentration_curves",
            "social_velocity",
            "wallet_clustering",
            "liquidity_behavior",
            "survivor_h2_h3_h4_rescore",
            "threshold_cross_alerts",
        ],
        "first_hour_pass_score": FIRST_HOUR_PASS_SCORE,
        "alert_threshold": ALERT_THRESHOLD,
        "survivors": survivors,
        "alerts": list_threshold_alerts(session),
        "alerts_new": new_alerts,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    if include_cohort_proof:
        out["cohort_proof"] = prove_on_cohort()
    return out


# Re-export pure helpers for tests
__all__ = [
    "ALERT_THRESHOLD",
    "FIRST_HOUR_PASS_SCORE",
    "classify_social_ramp",
    "divergence_score",
    "early_diff_status",
    "emit_threshold_alerts",
    "extract_live_features",
    "liq_path_from_tape",
    "load_cohort_rows",
    "load_findings",
    "passed_first_hour_filter",
    "prove_on_cohort",
    "rank_recent",
    "rescore_at_hour",
    "run_early_diff_tick",
    "score_cohort_row",
    "score_token",
    "study_baselines",
    "survivor_hour_rescores",
]
