"""Cohort-mined short-list signal from frozen entry features (+ wallet hits).

Does not grow FEATURE_NAMES. Paper ranking overlay — Entry p_good untouched.
Weights come from winner/loser miss-cohort separations (buy pressure, holders,
liquidity, creator prior, concentration, early-wallet overlap).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

# Frozen FEATURE_NAMES columns only — side keys like live_p_at_entry ignored.
SIGNAL_KEYS = (
    "buy_pressure",
    "holder_n",
    "liquidity_n",
    "volume_n",
    "creator_win_rate",
    "fresh_wallet_n",
    "top10_inv",
    "organic_book",
)

# Equal blend until a denser closed sample refits (offline).
DEFAULT_SIGNAL_WEIGHTS: dict[str, float] = {
    "buy_pressure": 0.18,
    "holder_n": 0.16,
    "liquidity_n": 0.14,
    "volume_n": 0.12,
    "creator_win_rate": 0.12,
    "fresh_wallet_n": 0.10,
    "top10_inv": 0.10,
    "organic_book": 0.08,
}

# Early-wallet overlap bonus (not a FEATURE_NAMES column).
EARLY_WALLET_BONUS = 0.08
EARLY_WALLET_CAP = 3


def normalize_signal_weights(raw: dict[str, Any] | None = None) -> dict[str, float]:
    out = dict(DEFAULT_SIGNAL_WEIGHTS)
    if isinstance(raw, dict):
        for key in SIGNAL_KEYS:
            try:
                val = float(raw.get(key))
            except (TypeError, ValueError):
                continue
            if val >= 0:
                out[key] = val
    total = sum(out.values()) or 1.0
    return {k: round(v / total, 4) for k, v in out.items()}


def early_wallet_hits(session: Session | None, token_id: int | None) -> int:
    if session is None or not token_id:
        return 0
    from ..models import EarlyWalletHit

    return int(
        session.query(EarlyWalletHit.id)
        .filter(EarlyWalletHit.token_id == int(token_id))
        .count()
    )


def v1_signal_from_features(
    features: dict[str, Any] | None,
    *,
    weights: dict[str, float] | None = None,
    early_hits: int = 0,
) -> dict[str, Any]:
    """0–1ish short-list signal from frozen features. Paper ranking only."""
    feat = features if isinstance(features, dict) else {}
    w = normalize_signal_weights(weights)
    parts: dict[str, float] = {}
    score = 0.0
    for key in SIGNAL_KEYS:
        try:
            val = float(feat.get(key) or 0.0)
        except (TypeError, ValueError):
            val = 0.0
        val = max(0.0, min(1.0, val))
        parts[key] = round(val, 4)
        score += float(w[key]) * val
    hits = max(0, int(early_hits or 0))
    bonus = EARLY_WALLET_BONUS * min(hits, EARLY_WALLET_CAP) / float(EARLY_WALLET_CAP)
    score = max(0.0, min(1.0, score + bonus))
    return {
        "score": round(score, 4),
        "parts": parts,
        "early_wallet_hits": hits,
        "early_wallet_bonus": round(bonus, 4),
        "weights": w,
    }


def feature_means(rows: list[dict[str, Any]], keys: tuple[str, ...] = SIGNAL_KEYS) -> dict[str, float]:
    """Mean of selected feature keys across feature dicts."""
    acc: dict[str, list[float]] = {k: [] for k in keys}
    for feat in rows:
        if not isinstance(feat, dict):
            continue
        for key in keys:
            try:
                acc[key].append(float(feat.get(key) or 0.0))
            except (TypeError, ValueError):
                continue
    return {
        k: round(sum(v) / len(v), 4) if v else 0.0
        for k, v in acc.items()
    }
