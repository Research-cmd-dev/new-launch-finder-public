"""Hold conviction after paper buy — exit layer only, not entry.

Scores open paper_v1 positions (0–1) from tape Live health, wallet support,
thesis hard tags, and meme_quality for meme-only books. Hard vetoes collapse
the score. Does not change desk buy lines or qualify paths.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from ..models import ScanState, Token, utcnow
from .live_fit import LIVE_EXIT_GIVEBACK, LIVE_EXIT_P, LIVE_EXIT_RAN
from .meme_quality import fomo_wallet_hits, is_meme_dominant_thesis, meme_quality_for_token
from .paper_gate import paper_live_dump, paper_no_run_exit
from .paper_v1 import v1_row_has_hard_tag
from .signal_score import early_wallet_hits

HOLD_CONVICTION_HIGH = 0.62
HOLD_CONVICTION_LOW = 0.38
HOLD_SHADOW_KEY = "paper_v1_hold_shadow_log"
HOLD_SHADOW_KEEP = 48
# High conviction: need deeper giveback before live-dump fires.
HOLD_HIGH_LIVE_FLOOR = 0.28
HOLD_HIGH_LAST_PEAK = 0.48
# Low conviction: exit sooner on a fading runner.
HOLD_LOW_LIVE_CEIL = 0.42
HOLD_LOW_GIVEBACK = 0.45


def _policy(score: float) -> str:
    if score >= HOLD_CONVICTION_HIGH:
        return "hold"
    if score <= HOLD_CONVICTION_LOW:
        return "cut"
    return "neutral"


def hold_conviction_for_fill(
    session: Session,
    token: Token,
    fill: Any,
    *,
    live_p: float | None,
    thesis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """0–1 hold score + policy label for an open paper_v1 fill."""
    from ..ledger import _v1_entry_features, _v1_thesis

    thesis = thesis if thesis is not None else _v1_thesis(session, fill.token_id, fill.decision_id)
    feat = _v1_entry_features(session, fill.token_id, fill.decision_id)
    live = float(live_p) if live_p is not None else 0.0
    tape = 0.35 * max(0.0, min(1.0, live))
    tape += 0.25 * max(0.0, min(1.0, float(feat.get("buy_pressure") or 0.0)))
    tape += 0.20 * max(0.0, min(1.0, float(feat.get("organic_book") or 0.0)))
    tape += 0.20 * max(0.0, min(1.0, float(feat.get("liquidity_n") or 0.0)))
    tape = min(1.0, tape)

    eh = early_wallet_hits(session, fill.token_id)
    fh = fomo_wallet_hits(session, fill.token_id)
    wallets = min(1.0, (eh + fh * 0.5) / 3.0)

    thesis_boost = 0.0
    if v1_row_has_hard_tag({"thesis": thesis}):
        thesis_boost = min(1.0, 0.55 + float(thesis.get("score") or 0.0) * 0.45)

    meme_score = None
    if is_meme_dominant_thesis(thesis):
        mq = meme_quality_for_token(session, token, features=feat, thesis=thesis)
        if not mq.get("eligible") or float(mq.get("score") or 0.0) <= 0.0:
            return {
                "score": 0.0,
                "policy": "cut",
                "parts": {"tape": 0.0, "wallets": 0.0, "thesis": 0.0, "meme": 0.0},
                "hard_veto": mq.get("hard_veto") or "ineligible",
            }
        meme_score = float(mq.get("score") or 0.0)
        score = 0.35 * tape + 0.25 * wallets + 0.40 * meme_score
    else:
        score = 0.40 * tape + 0.25 * wallets + 0.35 * thesis_boost

    score = round(max(0.0, min(1.0, score)), 4)
    return {
        "score": score,
        "policy": _policy(score),
        "parts": {
            "tape": round(tape, 4),
            "wallets": round(wallets, 4),
            "thesis": round(thesis_boost, 4),
            "meme": round(meme_score, 4) if meme_score is not None else None,
        },
        "hard_veto": "",
    }


def paper_live_dump_with_hold(
    *,
    entry_mcap: float,
    peak_mcap: float,
    last_mcap: float,
    live_p: float | None,
    hold_conviction: float,
) -> bool:
    """Live-dump with hold layer — high conviction resists one weak tick."""
    base = paper_live_dump(
        entry_mcap=entry_mcap,
        peak_mcap=peak_mcap,
        last_mcap=last_mcap,
        live_p=live_p,
    )
    entry = float(entry_mcap or 0.0)
    peak = float(peak_mcap or 0.0)
    last = float(last_mcap or 0.0)
    live = float(live_p) if live_p is not None else None
    if hold_conviction <= HOLD_CONVICTION_LOW and live is not None:
        if (
            live < HOLD_LOW_LIVE_CEIL
            and peak >= LIVE_EXIT_RAN * entry
            and last < HOLD_LOW_GIVEBACK * peak
        ):
            return True
    if not base:
        return False
    if hold_conviction >= HOLD_CONVICTION_HIGH and live is not None:
        if live >= HOLD_HIGH_LIVE_FLOOR and last >= HOLD_HIGH_LAST_PEAK * peak:
            return False
    return base


def paper_no_run_with_hold(
    *,
    entry_mcap: float,
    peak_mcap: float,
    last_mcap: float,
    age_hours: float,
    hold_conviction: float,
) -> bool:
    """No-run exit — low conviction exits sooner; high conviction waits."""
    base = paper_no_run_exit(
        entry_mcap=entry_mcap,
        peak_mcap=peak_mcap,
        last_mcap=last_mcap,
        age_hours=age_hours,
    )
    if hold_conviction >= HOLD_CONVICTION_HIGH and base:
        entry = float(entry_mcap or 0.0)
        peak = float(peak_mcap or 0.0)
        last = float(last_mcap or 0.0)
        if entry > 0 and peak > 0 and last >= 0.42 * peak:
            return False
    if hold_conviction <= HOLD_CONVICTION_LOW:
        entry = float(entry_mcap or 0.0)
        peak = float(peak_mcap or 0.0)
        last = float(last_mcap or 0.0)
        age = float(age_hours or 0.0)
        if (
            age >= 0.75
            and entry > 0
            and peak > 0
            and last > 0
            and peak < LIVE_EXIT_RAN * entry
            and last < 0.55 * peak
        ):
            return True
    return base


def record_hold_shadow(session: Session, entry: dict[str, Any]) -> None:
    """Append Learn-only would-hold vs would-cut shadow (paper_v1 opens)."""
    now = utcnow()
    row = session.query(ScanState).filter(ScanState.key == HOLD_SHADOW_KEY).one_or_none()
    history: list[dict[str, Any]] = []
    if row and row.value:
        try:
            parsed = json.loads(row.value)
            if isinstance(parsed, list):
                history = [x for x in parsed if isinstance(x, dict)]
        except Exception:
            history = []
    stamped = {**entry, "at": now.isoformat()}
    history.insert(0, stamped)
    history = history[:HOLD_SHADOW_KEEP]
    raw = json.dumps(history, default=str)
    if row is None:
        session.add(ScanState(key=HOLD_SHADOW_KEY, value=raw, updated_at=now))
    else:
        row.value = raw
        row.updated_at = now


def load_hold_shadow_log(session: Session, *, limit: int = 20) -> list[dict[str, Any]]:
    row = session.query(ScanState).filter(ScanState.key == HOLD_SHADOW_KEY).one_or_none()
    if row is None or not row.value:
        return []
    try:
        parsed = json.loads(row.value)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [x for x in parsed if isinstance(x, dict)][: max(1, int(limit))]
