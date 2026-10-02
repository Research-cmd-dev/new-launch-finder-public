"""Same-day paperV1 thesis weight refit from short-list closes.

Paper only. Does not grow FEATURE_NAMES. Does not rewrite Entry p_good.
Weights live in ScanState and nudge ranking when closed sample is dense
enough; until then the default 0.35/0.25/0.25/0.15 blend is used.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from ..models import PaperFill, ScanState, utcnow

log = logging.getLogger("launchfinder.thesis_weights")

WEIGHTS_KEY = "paper_v1:thesis_weights"
POLICY_KEY = "paper_v1:rank_policy"  # signal | live | thesis
DEFAULT_WEIGHTS = {
    "github_auth_n": 0.35,
    "real_project": 0.25,
    "gmgn_cto": 0.25,
    "name_quality": 0.15,
}
MIN_CLOSED = 12
FEATURE_KEYS = tuple(DEFAULT_WEIGHTS.keys())
DEFAULT_RANK_POLICY = "signal"
RANK_POLICIES = frozenset({"signal", "live", "thesis"})
# Auto-flip only when a challenger beats current by this margin on closed n.
POLICY_FLIP_MARGIN = 0.05
POLICY_META_KEY = "paper_v1:rank_policy_meta"


def default_weights() -> dict[str, float]:
    return dict(DEFAULT_WEIGHTS)


def normalize_weights(raw: dict[str, Any] | None) -> dict[str, float]:
    out = default_weights()
    if isinstance(raw, dict):
        for key in FEATURE_KEYS:
            try:
                val = float(raw.get(key))
            except (TypeError, ValueError):
                continue
            if val >= 0:
                out[key] = val
    total = sum(out.values()) or 1.0
    return {k: round(v / total, 4) for k, v in out.items()}


def _scan_row(session: Session, key: str) -> ScanState | None:
    return session.query(ScanState).filter(ScanState.key == key).first()


def load_thesis_weights(session: Session) -> dict[str, float]:
    row = _scan_row(session, WEIGHTS_KEY)
    if row is None or not row.value:
        return default_weights()
    try:
        data = json.loads(row.value)
    except Exception:
        return default_weights()
    return normalize_weights(data.get("weights") if isinstance(data, dict) else data)


def save_thesis_weights(
    session: Session,
    weights: dict[str, float],
    *,
    meta: dict[str, Any] | None = None,
) -> dict[str, float]:
    norm = normalize_weights(weights)
    payload = {"weights": norm, **(meta or {})}
    now = utcnow()
    row = _scan_row(session, WEIGHTS_KEY)
    if row is None:
        session.add(ScanState(key=WEIGHTS_KEY, value=json.dumps(payload), updated_at=now))
    else:
        row.value = json.dumps(payload)
        row.updated_at = now
    session.flush()
    return norm


def load_rank_policy(session: Session) -> str:
    row = _scan_row(session, POLICY_KEY)
    val = (row.value if row else "") or DEFAULT_RANK_POLICY
    return val if val in RANK_POLICIES else DEFAULT_RANK_POLICY


def save_rank_policy(session: Session, policy: str) -> str:
    policy = policy if policy in RANK_POLICIES else DEFAULT_RANK_POLICY
    now = utcnow()
    row = _scan_row(session, POLICY_KEY)
    if row is None:
        session.add(ScanState(key=POLICY_KEY, value=policy, updated_at=now))
    else:
        row.value = policy
        row.updated_at = now
    session.flush()
    return policy


def _hit2x(fill: PaperFill) -> bool:
    entry = float(fill.entry_mcap or 0.0)
    peak = float(fill.max_mcap or 0.0)
    return entry > 0 and peak >= 2.0 * entry


def _features_for_fill(session: Session, fill: PaperFill) -> dict[str, Any]:
    """Entry Decision features for a fill — avoids importing ledger (cycle)."""
    from ..models import Decision

    entry = None
    if fill.token_id:
        entry = (
            session.query(Decision)
            .filter(Decision.token_id == fill.token_id, Decision.kind == "entry")
            .order_by(Decision.id.desc())
            .first()
        )
    row = entry
    if row is None and fill.decision_id:
        row = session.get(Decision, fill.decision_id)
    if row is None:
        return {}
    try:
        data = json.loads(row.features_json or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def refit_thesis_weights(session: Session, *, min_closed: int = MIN_CLOSED) -> dict[str, Any]:
    """Nudge thesis weights from closed paperV1 (+ shadow) outcomes.

    Lift = hit2× rate when the feature is active minus rate when inactive.
    Positive lift raises the weight; negative lowers it. Soft updates so
    a small sample cannot flip the blend overnight.
    """
    from .paper_v1 import PAPER_V1_LINE, PAPER_V1_SHADOW_LINE, v1_peak_promoted_open

    rows = (
        session.query(PaperFill)
        .filter(
            PaperFill.line.in_((PAPER_V1_LINE, PAPER_V1_SHADOW_LINE)),
            PaperFill.status == "closed",
        )
        .order_by(PaperFill.id.desc())
        .limit(200)
        .all()
    )
    rows = [fill for fill in rows if not v1_peak_promoted_open(fill)]
    if len(rows) < int(min_closed):
        return {
            "fitted": False,
            "reason": f"need>={min_closed} closed, have {len(rows)}",
            "n": len(rows),
            "weights": load_thesis_weights(session),
        }
    hits = 0
    active_hits = {k: 0 for k in FEATURE_KEYS}
    active_n = {k: 0 for k in FEATURE_KEYS}
    inactive_hits = {k: 0 for k in FEATURE_KEYS}
    inactive_n = {k: 0 for k in FEATURE_KEYS}
    for fill in rows:
        feat = _features_for_fill(session, fill)
        won = _hit2x(fill)
        if won:
            hits += 1
        for key in FEATURE_KEYS:
            val = float(feat.get(key) or 0.0)
            on = val >= (0.5 if key != "name_quality" else 0.6)
            if on:
                active_n[key] += 1
                if won:
                    active_hits[key] += 1
            else:
                inactive_n[key] += 1
                if won:
                    inactive_hits[key] += 1
    base = hits / max(1, len(rows))
    weights = load_thesis_weights(session)
    lifts: dict[str, float] = {}
    for key in FEATURE_KEYS:
        if active_n[key] < 3 or inactive_n[key] < 3:
            lifts[key] = 0.0
            continue
        rate_on = active_hits[key] / active_n[key]
        rate_off = inactive_hits[key] / inactive_n[key]
        lifts[key] = rate_on - rate_off
        # Soft nudge: ±0.04 max per refit around current weight.
        weights[key] = max(0.05, weights[key] + max(-0.04, min(0.04, lifts[key] * 0.2)))
    norm = save_thesis_weights(
        session,
        weights,
        meta={
            "n_closed": len(rows),
            "base_hit2x": round(base, 4),
            "lifts": {k: round(v, 4) for k, v in lifts.items()},
            "fitted_at": utcnow().isoformat(),
        },
    )
    policy = load_rank_policy(session)
    ab = _score_rank_policies(session, rows)
    flipped = False
    winner = policy
    if ab.get("ready"):
        scores = ab.get("scores") or {}
        current_rate = float(scores.get(policy) or 0.0)
        best_policy = max(scores, key=lambda k: float(scores.get(k) or 0.0))
        best_rate = float(scores.get(best_policy) or 0.0)
        if best_policy != policy and (best_rate - current_rate) >= POLICY_FLIP_MARGIN:
            winner = save_rank_policy(session, best_policy)
            flipped = True
            _save_policy_meta(
                session,
                {
                    "flipped_at": utcnow().isoformat(),
                    "from": policy,
                    "to": winner,
                    "scores": scores,
                    "n": len(rows),
                    "margin": POLICY_FLIP_MARGIN,
                },
            )
        else:
            _save_policy_meta(
                session,
                {
                    "evaluated_at": utcnow().isoformat(),
                    "policy": policy,
                    "scores": scores,
                    "n": len(rows),
                    "flipped": False,
                },
            )
    log.info(
        "thesis weights refit n=%s base_hit2x=%.3f policy=%s flipped=%s weights=%s lifts=%s",
        len(rows),
        base,
        winner,
        flipped,
        norm,
        {k: round(v, 3) for k, v in lifts.items()},
    )
    return {
        "fitted": True,
        "n": len(rows),
        "base_hit2x": round(base, 4),
        "weights": norm,
        "lifts": {k: round(v, 4) for k, v in lifts.items()},
        "policy": winner,
        "policy_prev": policy,
        "policy_flipped": flipped,
        "policy_scores": ab.get("scores") or {},
    }


def _fill_live_p(feat: dict[str, Any], fill: PaperFill) -> float:
    from .paper_v1 import read_live_at_entry

    live, _src = read_live_at_entry(feat)
    if live is not None and live > 0:
        return float(live)
    return float(fill.entry_p or 0.0)


def _score_rank_policies(session: Session, rows: list[PaperFill]) -> dict[str, Any]:
    """Retrospective hit2× if we had ranked by each policy (closed sample)."""
    from .paper_v1 import v1_thesis_from_features

    if len(rows) < MIN_CLOSED:
        return {"ready": False, "scores": {}}
    scored: list[dict[str, Any]] = []
    for fill in rows:
        feat = _features_for_fill(session, fill)
        thesis = v1_thesis_from_features(feat, weights=load_thesis_weights(session))
        scored.append(
            {
                "fill": fill,
                "entry_p": float(fill.entry_p or 0.0),
                "live_p": _fill_live_p(feat, fill),
                "thesis_score": float(thesis.get("score") or 0.0),
                "hit": _hit2x(fill),
            }
        )

    def _rank_key(policy: str):
        if policy == "live":
            return lambda r: (r["live_p"], r["entry_p"], r["thesis_score"])
        if policy == "thesis":
            return lambda r: (r["thesis_score"], r["live_p"], r["entry_p"])
        return lambda r: (r["entry_p"], r["live_p"], r["thesis_score"])

    # Approximate: take top half under each policy as "would pick", score hit2×.
    k = max(6, len(scored) // 2)
    scores: dict[str, float] = {}
    for policy in sorted(RANK_POLICIES):
        ordered = sorted(scored, key=_rank_key(policy), reverse=True)[:k]
        hits = sum(1 for r in ordered if r["hit"])
        scores[policy] = round(hits / max(1, len(ordered)), 4)
    return {"ready": True, "scores": scores, "top_k": k}


def _save_policy_meta(session: Session, meta: dict[str, Any]) -> None:
    now = utcnow()
    row = _scan_row(session, POLICY_META_KEY)
    payload = json.dumps(meta)
    if row is None:
        session.add(ScanState(key=POLICY_META_KEY, value=payload, updated_at=now))
    else:
        row.value = payload
        row.updated_at = now
    session.flush()
