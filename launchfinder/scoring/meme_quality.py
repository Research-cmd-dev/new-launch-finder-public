"""Meme-only quality score — Learn / shadow labels, not a short-list pass.

Combines frozen entry features (tape, holders, wallets) with entry discipline.
Hard vetoes (copycat spam, hijack, celebrity/brand, …) zero the score.
Does not add ``meme`` to THESIS_HARD_TAGS.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from .paper_gate import PAPER_CHASE_FRESH_MULT, paper_hard_veto
from .paper_v1 import THESIS_HARD_TAGS, v1_thesis_from_features
from .signal_score import early_wallet_hits, v1_signal_from_features

MEME_QUALITY_LOOKBACK_DAYS = 14
MEME_QUALITY_TOP_DECILE = 0.90
MEME_QUALITY_MIN_CANDIDATES = 10
MEME_QUALITY_FALLBACK_FLOOR = 0.72
MEME_QUALITY_WRITE_FLOOR = 0.55
PAPER_V1_SKIP_MEME_QUALITY = "v1 meme-q"
MEME_MCAP_SWEET_LO = 50_000.0
MEME_MCAP_SWEET_HI = 800_000.0


def v1_meme_quality_stamp(day: str) -> str:
    from .paper_v1 import PAPER_V1_EXIT_REASON_MAX, v1_day_reason

    stamp = f"{PAPER_V1_SKIP_MEME_QUALITY}|{v1_day_reason(day)}"
    if len(stamp) > PAPER_V1_EXIT_REASON_MAX:
        raise ValueError(f"meme quality stamp too long ({len(stamp)}): {stamp!r}")
    return stamp


def _risk_flags_list(raw: str | None) -> list[str]:
    try:
        flags = json.loads((raw or "[]") or "[]")
    except Exception:
        return []
    return [str(x) for x in flags] if isinstance(flags, list) else []


def is_meme_dominant_thesis(thesis: dict[str, Any] | None) -> bool:
    """Meme tag or high name_quality without github/dev/cto hard tags."""
    bits = thesis if isinstance(thesis, dict) else {}
    hard = set(bits.get("hard_tags") or [])
    tags = set(bits.get("tags") or [])
    if hard & THESIS_HARD_TAGS:
        return False
    if "meme" in tags:
        return True
    return float(bits.get("name_quality") or 0.0) >= 0.6


def fomo_wallet_hits(session: Session | None, token_id: int | None) -> int:
    if session is None or not token_id:
        return 0
    from ..models import FomoWalletHit

    return int(
        session.query(FomoWalletHit.id).filter(FomoWalletHit.token_id == int(token_id)).count()
    )


def meme_quality_score(
    features: dict[str, Any] | None,
    *,
    risk_flags: list[str] | None = None,
    chain: str = "sol",
    multiple: float | None = None,
    entry_mcap: float | None = None,
    early_hits: int = 0,
    fomo_hits: int = 0,
    website: str = "",
    twitter: str = "",
    twitter_handle: str = "",
    twitter_followers: float = 0.0,
    twitter_age_days: float = 0.0,
    twitter_verified: bool = False,
) -> dict[str, Any]:
    """0–1 meme pile quality. ``eligible`` false when hard veto fires."""
    feat = features if isinstance(features, dict) else {}
    flags = list(risk_flags or [])
    veto = paper_hard_veto(
        flags,
        chain=chain,
        website=website,
        twitter=twitter,
        twitter_handle=twitter_handle,
        twitter_followers=twitter_followers,
        twitter_age_days=twitter_age_days,
        twitter_verified=twitter_verified,
    )
    if veto:
        return {
            "score": 0.0,
            "eligible": False,
            "hard_veto": veto,
            "parts": {},
            "late": False,
        }
    tape_dump = any(
        needle in " | ".join(f.lower() for f in flags)
        for needle in (
            "first-hour tape is dumping",
            "sellers already dominate",
            "dollar-weighted selling dominates",
        )
    )
    vol = max(0.0, min(1.0, float(feat.get("volume_n") or 0.0)))
    liq = max(0.0, min(1.0, float(feat.get("liquidity_n") or 0.0)))
    organic = max(0.0, min(1.0, float(feat.get("organic_book") or 0.0)))
    buy_p = max(0.0, min(1.0, float(feat.get("buy_pressure") or 0.0)))
    tape = 0.35 * vol + 0.35 * liq + 0.20 * organic + 0.10 * buy_p
    if tape_dump:
        tape *= 0.35

    holder_n = max(0.0, min(1.0, float(feat.get("holder_n") or 0.0)))
    top10_inv = max(0.0, min(1.0, float(feat.get("top10_inv") or 0.0)))
    renounced = max(0.0, min(1.0, float(feat.get("gmgn_renounced") or 0.0)))
    creator_wr = max(0.0, min(1.0, float(feat.get("creator_win_rate") or 0.0)))
    holders = 0.40 * holder_n + 0.35 * top10_inv + 0.15 * renounced + 0.10 * creator_wr

    eh = max(0, int(early_hits or 0))
    fh = max(0, int(fomo_hits or 0))
    wallet_signal = v1_signal_from_features(feat, early_hits=eh)
    wallet = min(
        1.0,
        float(wallet_signal.get("score") or 0.0) * 0.65
        + min(1.0, (eh + fh * 0.5) / 3.0) * 0.35,
    )

    mcap = float(entry_mcap or 0.0)
    if mcap <= 0:
        discipline = 0.45
    elif MEME_MCAP_SWEET_LO <= mcap <= MEME_MCAP_SWEET_HI:
        discipline = 1.0
    elif mcap < MEME_MCAP_SWEET_LO:
        discipline = max(0.35, mcap / MEME_MCAP_SWEET_LO)
    else:
        discipline = max(0.2, 1.0 - (mcap - MEME_MCAP_SWEET_HI) / (MEME_MCAP_SWEET_HI * 4))

    mult = float(multiple or 0.0)
    late = mult >= float(PAPER_CHASE_FRESH_MULT)
    if late:
        discipline *= 0.35
    if mult >= 5.0:
        discipline *= 0.5

    score = 0.30 * tape + 0.25 * holders + 0.25 * wallet + 0.20 * discipline
    score = round(max(0.0, min(1.0, score)), 4)
    return {
        "score": score,
        "eligible": True,
        "hard_veto": "",
        "late": late,
        "parts": {
            "tape": round(tape, 4),
            "holders": round(holders, 4),
            "wallets": round(wallet, 4),
            "discipline": round(discipline, 4),
            "early_wallet_hits": eh,
            "fomo_wallet_hits": fh,
        },
    }


def meme_quality_for_token(
    session: Session | None,
    token: Any,
    *,
    features: dict[str, Any] | None = None,
    thesis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Desk / API payload for one token."""
    from ..chains import normalize_chain

    research = getattr(token, "research", None)
    outcome = getattr(token, "outcome", None)
    feat = features
    if feat is None and research is not None:
        try:
            feat = json.loads(research.features_json or "{}")
        except Exception:
            feat = {}
    th = thesis if thesis is not None else v1_thesis_from_features(feat if isinstance(feat, dict) else {})
    meme_only = is_meme_dominant_thesis(th)
    flags = _risk_flags_list(research.risk_flags_json if research is not None else None)
    chain = normalize_chain(getattr(token, "chain", None) or "sol")
    entry_m = float((outcome.t0_mcap if outcome else 0.0) or 0.0)
    mult = float((outcome.multiple if outcome else 0.0) or 0.0)
    token_id = getattr(token, "id", None)
    mq = meme_quality_score(
        feat if isinstance(feat, dict) else {},
        risk_flags=flags,
        chain=chain,
        multiple=mult,
        entry_mcap=entry_m,
        early_hits=early_wallet_hits(session, token_id),
        fomo_hits=fomo_wallet_hits(session, token_id),
        website=str(getattr(token, "website", "") or ""),
        twitter=str(getattr(token, "twitter", "") or ""),
        twitter_handle=str(research.twitter_handle or "") if research else "",
        twitter_followers=float(research.twitter_followers or 0) if research else 0.0,
        twitter_age_days=float(research.twitter_age_days or 0) if research else 0.0,
        twitter_verified=bool(research.twitter_verified) if research else False,
    )
    return {
        "meme_only": meme_only,
        "thesis_tags": th.get("tags") or [],
        **mq,
    }
