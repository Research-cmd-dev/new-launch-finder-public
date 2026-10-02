"""Runners vs paperV1: would confirmed 5×+ names have cleared the short list?

Retrospective only — does not open fills or rewrite Decisions. Paper only.
Reconstructs Live-at-entry from HuntCard / LiveSample when possible so Sol
score-path is not always dead on historical rows.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from sqlalchemy.orm import Session

from ..chains import normalize_chain, graduation_mcap
from ..desk_lines import lines_for_scorer
from ..models import Decision, HuntCard, LiveSample, Outcome, PaperFill, Research, Token
from .fomo_trend_no_hunt import FOMO_TREND_NO_HUNT_KEY, FOMO_TREND_NO_HUNT_WHY_KEY
from .miss_cohort import (
    PAPER_MISS_JOIN_KEY,
    is_paper_miss_reason,
    paper_miss_reason,
)
from .paper_v1 import (
    PAPER_V1_LINE,
    PAPER_V1_SHADOW_LINE,
    PAPER_V1_SOL_LIVE,
    PAPER_V1_SOL_LIVE_THESIS,
    PAPER_V1_RH_ENTRY,
    PAPER_V1_RH_ENTRY_THESIS,
    PAPER_V1_THESIS_MIN,
    v1_qualifies,
    v1_thesis_from_features,
    v1_thesis_ok,
)
from .thesis_weights import load_thesis_weights


def _entry_decision(session: Session, token_id: int) -> Decision | None:
    return (
        session.query(Decision)
        .filter(Decision.token_id == token_id, Decision.kind == "entry")
        .order_by(Decision.id.desc())
        .first()
    )


def _entry_features(decision: Decision | None) -> dict[str, Any]:
    if decision is None:
        return {}
    try:
        data = json.loads(decision.features_json or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _live_at_entry(session: Session, token: Token, decision: Decision | None) -> tuple[float | None, str]:
    """Best-effort Live at first sight. Prefer Decision freeze; never invents a pass."""
    from .paper_v1 import read_live_at_entry

    if decision is not None:
        frozen_live, frozen_src = read_live_at_entry(_entry_features(decision))
        if frozen_live is not None and frozen_live > 0:
            return frozen_live, frozen_src or "frozen"
    hunt = (
        session.query(HuntCard)
        .filter(HuntCard.chain == token.chain, HuntCard.mint == token.mint)
        .first()
    )
    if hunt is not None:
        live = float(hunt.conviction_p or 0.0)
        if live > 0:
            return live, "hunt_card"
    if decision is not None:
        sample = (
            session.query(LiveSample)
            .filter(LiveSample.decision_id == decision.id)
            .order_by(LiveSample.id.asc())
            .first()
        )
        if sample is None:
            sample = (
                session.query(LiveSample)
                .filter(LiveSample.token_id == token.id)
                .order_by(LiveSample.id.asc())
                .first()
            )
        if sample is not None:
            try:
                feat = json.loads(sample.features_json or "{}")
            except Exception:
                feat = {}
            if isinstance(feat, dict):
                for key in ("live_p", "conviction_p", "model_p"):
                    if feat.get(key) is not None:
                        try:
                            val = float(feat[key])
                        except (TypeError, ValueError):
                            continue
                        if val > 0:
                            return val, "live_sample"
            # Proxy: post-entry multiple from sample mcap vs entry — not a Live score.
            # Leave unknown rather than invent probability.
    return None, "unknown"


def _v1_path(chain: str, entry_p: float, live_p: float | None, hi: float, thesis: dict[str, Any]) -> str:
    """score | thesis | miss — which short-list path (if any) would clear."""
    if not v1_qualifies(chain, entry_p, live_p, hi=hi, thesis=thesis):
        return "miss"
    thesis_ok = v1_thesis_ok(thesis)
    if normalize_chain(chain) == "robinhood":
        if float(entry_p or 0.0) >= PAPER_V1_RH_ENTRY:
            return "score"
        return "thesis" if thesis_ok else "score"
    if float(entry_p or 0.0) >= float(hi) and float(live_p or 0.0) >= PAPER_V1_SOL_LIVE:
        return "score"
    return "thesis" if thesis_ok else "score"


def _miss_reason(
    chain: str,
    entry_p: float,
    live_p: float | None,
    live_src: str,
    *,
    hi: float,
    thesis: dict[str, Any],
    has_features: bool,
    entry_mcap: float,
) -> str:
    from .paper_v1 import v1_leftover_reject

    if v1_leftover_reject(entry_mcap):
        return "leftover_mcap"
    if not has_features:
        return "thin_features"
    if normalize_chain(chain) == "sol" and live_src == "unknown":
        # Would score-path need Live? If entry clears hi, Live unknown is the blocker.
        if float(entry_p or 0.0) >= float(hi) and not v1_thesis_ok(thesis):
            return "live_unknown"
        if float(entry_p or 0.0) >= float(hi) * 0.75 and not v1_thesis_ok(thesis):
            return "live_unknown"
    if not v1_thesis_ok(thesis):
        tags = thesis.get("tags") or []
        score = float(thesis.get("score") or 0.0)
        if score < PAPER_V1_THESIS_MIN or not tags:
            return "no_thesis_tags"
    if normalize_chain(chain) == "robinhood":
        if float(entry_p or 0.0) < PAPER_V1_RH_ENTRY_THESIS:
            return "entry_below_soft"
        if float(entry_p or 0.0) < PAPER_V1_RH_ENTRY:
            return "entry_below_hi"
    else:
        if float(entry_p or 0.0) < float(hi) * 0.75:
            return "entry_below_soft"
        if float(live_p or 0.0) < PAPER_V1_SOL_LIVE_THESIS:
            return "live_below_soft"
        if float(entry_p or 0.0) < float(hi) or float(live_p or 0.0) < PAPER_V1_SOL_LIVE:
            return "score_miss"
    return "score_miss"


def paper_v1_runners_retro(
    session: Session,
    chain: str = "sol",
    *,
    min_multiple: float = 5.0,
    limit: int = 60,
) -> dict[str, Any]:
    """Confirmed runners with paperV1 would-pass / short-list capture."""
    from .outcomes import (
        DEAD_POOL_LIQ,
        MAX_HONEST_MULTIPLE,
        confirmed_runner_multiple,
        is_bundle_copycat_run,
        is_prepumped_entry,
    )

    chain = normalize_chain(chain)
    hi = float(lines_for_scorer("first_sight", chain).hi)
    weights = load_thesis_weights(session)
    filters = [
        Outcome.multiple >= float(min_multiple),
        Outcome.multiple <= MAX_HONEST_MULTIPLE,
        Token.source != "backfill",
        Token.chain == chain,
        (Outcome.label.is_(None)) | (Outcome.label == 1),
        Outcome.last_liq >= DEAD_POOL_LIQ,
        ~Research.risk_flags_json.like("%start-high rug%"),
        ~Research.risk_flags_json.like("%Pre-pumped%"),
    ]
    if chain == "sol":
        filters.append(Outcome.t0_mcap >= 0.4 * graduation_mcap("sol"))
    if chain == "robinhood":
        filters.append(Token.is_historical.is_(False))
    rows = (
        session.query(Token, Research, Outcome)
        .join(Research, Research.token_id == Token.id)
        .join(Outcome, Outcome.token_id == Token.id)
        .filter(*filters)
        .order_by(Outcome.multiple.desc())
        .limit(800)
        .all()
    )
    above_cap_rows = (
        session.query(Token, Outcome)
        .join(Outcome, Outcome.token_id == Token.id)
        .filter(
            Token.chain == chain,
            Token.source != "backfill",
            Outcome.multiple > MAX_HONEST_MULTIPLE,
            (Outcome.label.is_(None)) | (Outcome.label == 1),
        )
        .order_by(Outcome.multiple.desc())
        .limit(12)
        .all()
    )
    above_honest_cap_note = [
        {
            "mint": token.mint,
            "symbol": token.symbol or "",
            "multiple": round(float(outcome.multiple or 0.0), 1),
            "note": (
                f"Above MAX_HONEST_MULTIPLE={MAX_HONEST_MULTIPLE:.0f} — annotate only; "
                "excluded from retro items. Does not raise the cap."
            ),
        }
        for token, outcome in above_cap_rows
    ]
    items: list[dict[str, Any]] = []
    miss_counts: Counter[str] = Counter()
    live_known = 0
    for token, research, outcome in rows:
        if is_prepumped_entry(session, token, research, outcome):
            continue
        if is_bundle_copycat_run(research):
            continue
        confirmed = confirmed_runner_multiple(session, token, outcome)
        if confirmed < float(min_multiple):
            continue
        decision = _entry_decision(session, token.id)
        feat = _entry_features(decision)
        thesis = v1_thesis_from_features(feat, weights=weights)
        entry_p = float(
            (decision.entry_p if decision is not None else 0.0)
            or research.p_good
            or 0.0
        )
        live_p, live_src = _live_at_entry(session, token, decision)
        if live_src != "unknown":
            live_known += 1
        # Sol score path needs a Live number; unknown stays 0 for qualify.
        live_for_path = live_p if live_p is not None else (None if chain == "robinhood" else 0.0)
        path = _v1_path(chain, entry_p, live_for_path, hi, thesis)
        would_pass = path != "miss"
        entry_mcap = float(
            (decision.entry_mcap if decision is not None else 0.0)
            or (outcome.t0_mcap if outcome else 0.0)
            or 0.0
        )
        miss = ""
        if not would_pass:
            miss = _miss_reason(
                chain,
                entry_p,
                live_p,
                live_src,
                hi=hi,
                thesis=thesis,
                has_features=bool(feat),
                entry_mcap=entry_mcap,
            )
            miss_counts[miss] += 1
        v1_fill = (
            session.query(PaperFill)
            .filter(
                PaperFill.mint == token.mint,
                PaperFill.line.in_((PAPER_V1_LINE, PAPER_V1_SHADOW_LINE)),
            )
            .order_by(PaperFill.id.desc())
            .first()
        )
        skip_label = paper_miss_reason((v1_fill.exit_reason if v1_fill else "") or "")
        paper_miss = bool(
            is_paper_miss_reason((v1_fill.exit_reason if v1_fill else "") or "")
            or feat.get(PAPER_MISS_JOIN_KEY)
        )
        items.append(
            {
                "symbol": token.symbol or "",
                "mint": token.mint,
                "chain": chain,
                "multiple": round(confirmed, 1),
                "entry_p": round(entry_p, 4),
                "live_p": None if live_p is None else round(live_p, 4),
                "live_src": live_src,
                "entry_mcap": round(entry_mcap) if entry_mcap else None,
                "thesis_score": thesis.get("score"),
                "tags": thesis.get("tags") or [],
                "would_pass_v1": would_pass,
                "path": path,
                "miss_reason": miss,
                "has_entry_features": bool(feat),
                "paper_miss_join": paper_miss,
                "paper_miss_reason": skip_label or None,
                "fomo_trend_no_hunt": bool(feat.get(FOMO_TREND_NO_HUNT_KEY)),
                "fomo_no_hunt_why": feat.get(FOMO_TREND_NO_HUNT_WHY_KEY) or None,
                "short_list": None
                if v1_fill is None
                else {
                    "line": v1_fill.line,
                    "status": v1_fill.status,
                    "exit_reason": (v1_fill.exit_reason or "")[:80],
                },
            }
        )
        if len(items) >= int(limit):
            break
    n = len(items)
    would = sum(1 for r in items if r["would_pass_v1"])
    on_list = sum(1 for r in items if r.get("short_list") and r["short_list"].get("line") == PAPER_V1_LINE)
    by_path = {"score": 0, "thesis": 0, "miss": 0}
    for r in items:
        by_path[str(r["path"])] = by_path.get(str(r["path"]), 0) + 1
    thin = sum(1 for r in items if not r["has_entry_features"])
    booked_pass = sum(
        1
        for r in items
        if r["would_pass_v1"] and r.get("short_list") and r["short_list"].get("line") == PAPER_V1_LINE
    )
    return {
        "chain": chain,
        "min_multiple": float(min_multiple),
        "n": n,
        "would_pass_v1": would,
        "would_pass_rate": round(would / n, 3) if n else None,
        "on_paper_v1": on_list,
        "would_pass_not_booked": max(0, would - booked_pass),
        "by_path": by_path,
        "by_miss": dict(miss_counts.most_common()),
        "thin_entry_features": thin,
        "live_reconstructed": live_known,
        "note": (
            "Retrospective on confirmed runners. Live-at-entry prefers Decision "
            "freeze (live_p_at_entry), then HuntCard/LiveSample; unknown Sol Live "
            "still treated as 0 for score-path. Miss taxonomy in by_miss. "
            "Not a label rewrite."
        ),
        "above_honest_cap_note": above_honest_cap_note,
        "items": items,
    }
