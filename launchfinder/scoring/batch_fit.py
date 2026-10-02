"""Phase 1: batch fit on frozen decisions, time-split validation, isotonic
calibration, promote only if better than the incumbent.

Training rows are ``decisions`` (kind=entry) joined to forward outcomes.
The label is the desk goal: 2x from the decision's own entry mcap within
the horizon with a live pool at judgement. Backfill / historical rows are
never in the set. Features are the 66 FEATURE_NAMES as frozen at entry —
nothing a later repair wrote.

Promotion copies the fitted weights into ``ModelState`` so ``predict()``
keeps its shape, stores the isotonic map on the artifact, and stops the
online SGD from drifting the promoted weights.
"""

from __future__ import annotations

import bisect
import json
import logging
import time
from datetime import datetime
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from ..chains import normalize_chain
from ..models import ModelArtifact, ModelState, utcnow
from .features import FEATURE_NAMES, feature_vector

log = logging.getLogger("launchfinder.batch_fit")

MIN_ROWS = 400
MIN_VALID = 80
MIN_POSITIVES = 15
VALID_FRACTION = 0.2
L2 = 1.0
NEWTON_ITERS = 30
CAL_FLOOR = 0.01
CAL_CEIL = 0.99
# Candidate must not rank worse than the incumbent by more than this.
PROMOTE_AUC_SLACK = 0.01
PROMOTE_TOP_DECILE_SLACK = 0.02
ARTIFACT_CACHE_SECONDS = 300.0

_artifact_cache: dict[tuple[str, str], tuple[float, list[list[float]] | None]] = {}


def reset_artifact_cache() -> None:
    _artifact_cache.clear()


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------


# Only decisions written at first score carry a feature vector frozen at
# entry. ``seed_t0`` rows copied Research.features_json as it stood at seed
# time — after repairs, with entry_collapse / holder_n / volume_n already
# describing the outcome. The first RH fit on those rows reported AUC 0.94
# and 99.8% hit2x at 90 against a forward board that hits 22%. That is the
# label leaking, not a model.
TRAIN_SOURCES: tuple[str, ...] = ("live",)


def training_rows(session: Session, chain: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Resolved live entry decisions with the 2x label and the frozen feature vector."""
    from ..ledger import resolve_entry_decisions
    from .outcomes import is_rh_leftover_fdv

    now = now or utcnow()
    out: list[dict[str, Any]] = []
    # Evidence-based label: a hit needs a real post-entry print, a seeded
    # max_mcap never counts, silence is a loss (same judge as the board).
    for decision, outcome, res in resolve_entry_decisions(session, chain, now=now, sources=TRAIN_SOURCES, with_features=True):
        if res is None:
            continue
        # Leftover graduation FDV echoed as t0 + last_liq on a 5-wallet book
        # (MORDOR) is not a market; same exclusion the online path used.
        if outcome is not None and is_rh_leftover_fdv(outcome, int(decision.holders or 0), chain=chain):
            continue
        try:
            feats = json.loads(decision.features_json or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(feats, dict) or not feats:
            continue
        out.append(
            {
                "at": decision.at,
                "x": feature_vector(feats),
                "y": 1 if res["hit2x"] else 0,
                "entry_p": float(decision.entry_p or 0.0),
                "source": decision.source,
            }
        )
    out.sort(key=lambda r: r["at"])
    return out


def _metrics_flags(a: ModelArtifact) -> dict[str, Any]:
    try:
        m = json.loads(a.metrics_json or "{}")
    except json.JSONDecodeError:
        m = {}
    return m if isinstance(m, dict) else {}


def demote_unfrozen_fits(session: Session) -> list[dict[str, Any]]:
    """Un-promote artifacts fitted before the live-only rule.

    Clearing ``promoted`` drops their isotonic map from ``predict()`` and
    hands learning back to the online path (``train_pending`` unfreezes).
    The weights they copied into ``ModelState`` stay — the incumbent is
    gone and the online path trains on the same mutable rows anyway; the
    honest replacement is the next live-only fit. Idempotent; runs before
    every fit and once at worker boot.
    """
    rows = session.query(ModelArtifact).filter(ModelArtifact.promoted.is_(True)).all()
    demoted: list[dict[str, Any]] = []
    for art in rows:
        if _metrics_flags(art).get("live_only"):
            continue
        art.promoted = False
        session.add(art)
        demoted.append({"chain": art.chain, "kind": art.kind, "version": art.version})
        log.warning("demoted %s %s model v%s: fitted on unfrozen seed features", art.chain, art.kind, art.version)
    if demoted:
        session.flush()
        reset_artifact_cache()
    return demoted


def time_split(rows: list[dict[str, Any]], *, fraction: float = VALID_FRACTION) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Oldest rows train, newest validate. Never shuffle a tape."""
    if not rows:
        return [], []
    n_valid = max(MIN_VALID, int(len(rows) * fraction))
    n_valid = min(n_valid, max(0, len(rows) - MIN_VALID))
    return rows[: len(rows) - n_valid], rows[len(rows) - n_valid :]


# --------------------------------------------------------------------------
# Fit
# --------------------------------------------------------------------------


def _sigmoid(z: np.ndarray) -> np.ndarray:
    z = np.clip(z, -30.0, 30.0)
    return 1.0 / (1.0 + np.exp(-z))


def fit_logistic(X: np.ndarray, y: np.ndarray, *, l2: float = L2, iters: int = NEWTON_ITERS) -> tuple[np.ndarray, float]:
    """Ridge logistic regression by Newton steps. Bias is not penalised."""
    n, d = X.shape
    Xb = np.hstack([X, np.ones((n, 1))])
    w = np.zeros(d + 1)
    reg = np.full(d + 1, float(l2))
    reg[-1] = 1e-6
    for _ in range(iters):
        p = _sigmoid(Xb @ w)
        g = Xb.T @ (p - y) + reg * w
        s = p * (1.0 - p) + 1e-9
        H = (Xb * s[:, None]).T @ Xb + np.diag(reg)
        try:
            step = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:
            step = np.linalg.lstsq(H, g, rcond=None)[0]
        w = w - step
        if float(np.max(np.abs(step))) < 1e-6:
            break
    return w[:-1], float(w[-1])


def isotonic_fit(scores: np.ndarray, labels: np.ndarray) -> list[list[float]]:
    """Pool-adjacent-violators. Returns [[min_score, calibrated_p], ...] ascending."""
    order = np.argsort(scores, kind="stable")
    s = scores[order]
    y = labels[order].astype(float)
    blocks: list[list[float]] = []  # [min_score, sum_y, n]
    for si, yi in zip(s, y, strict=True):
        blocks.append([float(si), float(yi), 1.0])
        while len(blocks) >= 2 and blocks[-2][1] / blocks[-2][2] > blocks[-1][1] / blocks[-1][2]:
            a = blocks.pop()
            b = blocks.pop()
            blocks.append([b[0], a[1] + b[1], a[2] + b[2]])
    out: list[list[float]] = []
    for lo, sum_y, n in blocks:
        p = min(CAL_CEIL, max(CAL_FLOOR, sum_y / n))
        out.append([round(lo, 6), round(p, 4)])
    return out


def apply_calibration(cal: list[list[float]] | None, p: float) -> float:
    if not cal:
        return float(p)
    keys = [row[0] for row in cal]
    idx = bisect.bisect_right(keys, float(p)) - 1
    if idx < 0:
        return float(cal[0][1])
    return float(cal[idx][1])


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


def auc(scores: np.ndarray, labels: np.ndarray) -> float | None:
    pos = scores[labels == 1]
    neg = scores[labels == 0]
    if len(pos) == 0 or len(neg) == 0:
        return None
    order = np.argsort(np.concatenate([pos, neg]), kind="stable")
    ranks = np.empty(len(order), dtype=float)
    ranks[order] = np.arange(1, len(order) + 1)
    # Average ranks for ties.
    allv = np.concatenate([pos, neg])
    uniq, inv = np.unique(allv, return_inverse=True)
    if len(uniq) < len(allv):
        sums = np.zeros(len(uniq))
        counts = np.zeros(len(uniq))
        np.add.at(sums, inv, ranks)
        np.add.at(counts, inv, 1)
        ranks = (sums / counts)[inv]
    r_pos = ranks[: len(pos)].sum()
    return round(float((r_pos - len(pos) * (len(pos) + 1) / 2.0) / (len(pos) * len(neg))), 4)


def metrics(p: np.ndarray, y: np.ndarray) -> dict[str, Any]:
    n = len(y)
    if n == 0:
        return {"n": 0}
    k = max(1, n // 10)
    order = np.argsort(-p, kind="stable")
    top = y[order[:k]]
    bins = []
    for lo in range(0, 10):
        mask = (p >= lo / 10.0) & (p < (lo + 1) / 10.0 if lo < 9 else p <= 1.0)
        if mask.sum() == 0:
            continue
        bins.append({"bin": f"{lo / 10:.1f}-{(lo + 1) / 10:.1f}", "n": int(mask.sum()), "predicted": round(float(p[mask].mean()), 3), "actual": round(float(y[mask].mean()), 3)})
    actuals = [b["actual"] for b in bins if b["n"] >= 10]
    monotone = all(a <= b + 1e-9 for a, b in zip(actuals, actuals[1:], strict=False)) if len(actuals) >= 2 else None
    hi = p >= 0.9
    return {
        "n": int(n),
        "positives": int(y.sum()),
        "base_rate": round(float(y.mean()), 4),
        "brier": round(float(np.mean((p - y) ** 2)), 4),
        "auc": auc(p, y),
        "precision_top_decile": round(float(top.mean()), 4),
        "hit2x_at_90": round(float(y[hi].mean()), 4) if hi.sum() else None,
        "n_at_90": int(hi.sum()),
        "monotone": monotone,
        "bins": bins,
    }


def _incumbent_raw(model: ModelState, X: np.ndarray) -> np.ndarray:
    weights = json.loads(model.weights_json or "{}")
    w = np.array([float(weights.get(name, 0.0)) for name in FEATURE_NAMES])
    return _sigmoid(X @ w + float(model.bias))


def should_promote(cand: dict[str, Any], inc: dict[str, Any]) -> tuple[bool, str]:
    if not cand.get("n") or cand.get("positives", 0) < MIN_POSITIVES:
        return False, "too few validation positives"
    if inc.get("auc") is not None and cand.get("auc") is not None and cand["auc"] + PROMOTE_AUC_SLACK < inc["auc"]:
        return False, f"auc {cand['auc']} under incumbent {inc['auc']}"
    if cand["precision_top_decile"] + PROMOTE_TOP_DECILE_SLACK < inc.get("precision_top_decile", 0.0):
        return False, f"top-decile {cand['precision_top_decile']} under incumbent {inc.get('precision_top_decile')}"
    if cand["brier"] >= inc.get("brier", 1.0):
        return False, f"brier {cand['brier']} not under incumbent {inc.get('brier')}"
    return True, "better brier, ranking held"


# --------------------------------------------------------------------------
# Cycle
# --------------------------------------------------------------------------


def latest_promoted(session: Session, chain: str, kind: str = "entry") -> ModelArtifact | None:
    return (
        session.query(ModelArtifact)
        .filter(ModelArtifact.chain == normalize_chain(chain), ModelArtifact.kind == kind, ModelArtifact.promoted.is_(True))
        .order_by(ModelArtifact.id.desc())
        .first()
    )


def promoted_calibration(session: Session, chain: str, kind: str = "entry") -> list[list[float]] | None:
    """Isotonic map of the promoted artifact, cached a few minutes per chain."""
    key = (normalize_chain(chain), kind)
    now = time.monotonic()
    hit = _artifact_cache.get(key)
    if hit and now - hit[0] < ARTIFACT_CACHE_SECONDS:
        return hit[1]
    art = latest_promoted(session, chain, kind)
    cal = None
    if art is not None:
        try:
            cal = json.loads(art.calibration_json or "[]") or None
        except json.JSONDecodeError:
            cal = None
    _artifact_cache[key] = (now, cal)
    return cal


def has_promoted(session: Session, chain: str, kind: str = "entry") -> bool:
    return promoted_calibration(session, chain, kind) is not None or latest_promoted(session, chain, kind) is not None


def fit_entry_model(session: Session, chain: str, *, now: datetime | None = None, promote: bool = True) -> dict[str, Any]:
    """One batch cycle. Always writes an artifact row (promoted or not) when a fit ran."""
    from .model import get_or_create_model

    chain = normalize_chain(chain)
    now = now or utcnow()
    demote_unfrozen_fits(session)
    rows = training_rows(session, chain, now=now)
    if len(rows) < MIN_ROWS:
        return {"chain": chain, "fitted": False, "reason": f"{len(rows)} live rows < {MIN_ROWS}"}
    train, valid = time_split(rows)
    if len(valid) < MIN_VALID or sum(r["y"] for r in train) < MIN_POSITIVES:
        return {"chain": chain, "fitted": False, "reason": "not enough validation rows or positives"}
    Xt = np.array([r["x"] for r in train], dtype=float)
    yt = np.array([r["y"] for r in train], dtype=float)
    Xv = np.array([r["x"] for r in valid], dtype=float)
    yv = np.array([r["y"] for r in valid], dtype=float)
    w, b = fit_logistic(Xt, yt)
    raw_v = _sigmoid(Xv @ w + b)
    cal = isotonic_fit(raw_v, yv)
    cal_v = np.array([apply_calibration(cal, float(p)) for p in raw_v])
    cand = metrics(cal_v, yv)
    cand["raw_brier"] = round(float(np.mean((raw_v - yv) ** 2)), 4)
    model = get_or_create_model(session, chain=chain)
    inc = metrics(_incumbent_raw(model, Xv), yv)
    entry_only = metrics(np.array([r["entry_p"] for r in valid], dtype=float), yv)
    ok, why = should_promote(cand, inc) if promote else (False, "promotion disabled")
    prev = latest_promoted(session, chain)
    art = ModelArtifact(
        chain=chain,
        kind="entry",
        created_at=now,
        version=(prev.version + 1) if prev else 1,
        weights_json=json.dumps({name: float(wi) for name, wi in zip(FEATURE_NAMES, w, strict=True)}),
        bias=b,
        calibration_json=json.dumps(cal),
        n_train=len(train),
        n_valid=len(valid),
        metrics_json=json.dumps(
            {
                **cand,
                "valid_from": valid[0]["at"].isoformat() if valid else None,
                "entry_p_baseline": entry_only,
                "promote_reason": why,
                "live_only": True,
            }
        ),
        incumbent_json=json.dumps(inc),
        promoted=bool(ok),
    )
    session.add(art)
    if ok:
        model.weights_json = art.weights_json
        model.bias = b
        model.n_train = len(train) + len(valid)
        model.version += 1
        model.updated_at = now
        session.add(model)
        reset_artifact_cache()
        log.info("promoted %s entry model v%s: %s (brier %.4f vs %.4f)", chain, art.version, why, cand["brier"], inc.get("brier", float("nan")))
    else:
        log.info("kept %s incumbent: %s", chain, why)
    session.flush()
    return {"chain": chain, "fitted": True, "promoted": bool(ok), "reason": why, "candidate": cand, "incumbent": inc, "entry_p_baseline": entry_only, "n_train": len(train), "n_valid": len(valid), "version": art.version}


def artifact_card(session: Session, chain: str, kind: str = "entry", *, limit: int = 10) -> dict[str, Any]:
    chain = normalize_chain(chain)
    rows = (
        session.query(ModelArtifact)
        .filter(ModelArtifact.chain == chain, ModelArtifact.kind == kind)
        .order_by(ModelArtifact.id.desc())
        .limit(limit)
        .all()
    )
    promoted = latest_promoted(session, chain, kind)

    def _row(a: ModelArtifact) -> dict[str, Any]:
        try:
            m = json.loads(a.metrics_json or "{}")
        except json.JSONDecodeError:
            m = {}
        try:
            inc = json.loads(a.incumbent_json or "{}")
        except json.JSONDecodeError:
            inc = {}
        return {
            "id": a.id,
            "version": a.version,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "promoted": bool(a.promoted),
            "n_train": a.n_train,
            "n_valid": a.n_valid,
            "brier": m.get("brier"),
            "auc": m.get("auc"),
            "precision_top_decile": m.get("precision_top_decile"),
            "hit2x_at_90": m.get("hit2x_at_90"),
            "n_at_90": m.get("n_at_90"),
            "monotone": m.get("monotone"),
            "incumbent_brier": inc.get("brier"),
            "incumbent_auc": inc.get("auc"),
            "reason": m.get("promote_reason"),
            "live_only": bool(m.get("live_only")),
            "raw_auc": m.get("raw_auc"),
            "hit5x_top_decile": m.get("hit5x_top_decile"),
            "baseline_auc": (m.get("entry_p_baseline") or {}).get("auc"),
            "baseline_top_decile": (m.get("entry_p_baseline") or {}).get("precision_top_decile"),
            "seed_rows": m.get("seed_rows"),
            "live_rows": m.get("live_rows"),
            # first-sight only: validation-tail flow / hit rate at the desk lines.
            "lines": m.get("lines"),
            "n_features": m.get("n_features"),
            # first-sight only: share of rows whose holder count was a data gap.
            "holders_unknown_valid": m.get("holders_unknown_valid"),
            "holders_unknown_train": m.get("holders_unknown_train"),
            "base_rate": m.get("base_rate"),
            # first-sight only: which 2x label the fit was judged on (1: sight
            # print / t0 re-base; 2: first fillable print) and how much of
            # the validation tail the desk could never have bought.
            "label_version": m.get("label_version") or (1 if kind == "first_sight" else None),
            "unfilled_valid": m.get("unfilled_valid"),
            "filled_later_valid": m.get("filled_later_valid"),
            # live: runner rate inside the 2x head's own top decile vs the slice.
            "runner_base_rate": m.get("runner_base_rate"),
            # live_runner / ran_shadow: flow and hit rate above 0.20, and how the
            # promoted 2x Live head ranks the same runner label.
            "share_ge_020": m.get("share_ge_020"),
            "hit_ge_020": m.get("hit_ge_020"),
            "live_2x_top_decile": (m.get("live_2x_baseline") or {}).get("precision_top_decile"),
            "live_2x_auc": (m.get("live_2x_baseline") or {}).get("auc"),
        }

    return {
        "chain": chain,
        "kind": kind,
        "promoted_version": promoted.version if promoted else None,
        "promoted_at": promoted.created_at.isoformat() if promoted and promoted.created_at else None,
        "calibration": json.loads(promoted.calibration_json or "[]") if promoted else [],
        "artifacts": [_row(a) for a in rows],
        "label": "2x from the decision entry mcap within 24h with a live pool; backfill excluded",
    }
