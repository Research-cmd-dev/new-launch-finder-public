"""Execute gated sanity improve actions — enrich/freeze only, never buy.

Called from worker paper sync and ``GET /api/sanity-loop?apply=1``.
Hard-veto / chase actions are never executed here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from ..chains import normalize_chain
from ..models import Decision, HuntCard, LiveSample, Outcome, Research, Token
from .paper_v1 import LIVE_AT_ENTRY_KEY, merge_live_at_entry
from .thesis_enrich import (
    enrich_runners_thin_thesis,
    repair_thin_entry_thesis,
    sync_thesis_from_raw,
    thesis_keys_thin,
    THESIS_KEYS,
    apply_thesis_keys,
)

log = logging.getLogger("launchfinder.sanity_jobs")

# Only these improve actions may mutate state.
EXECUTABLE = frozenset(
    {"repair_thin_thesis", "freeze_live_coverage", "enrich_thin_thesis_http"}
)


def _loads(raw: str | None, default: Any) -> Any:
    try:
        data = json.loads(raw or "")
    except Exception:
        return default
    return data if data is not None else default


def repair_runners_thin_thesis(
    session: Session,
    chain: str = "sol",
    *,
    limit: int = 80,
) -> dict[str, int]:
    """Patch thin entry Decisions on confirmed runners (not just recent ids)."""
    from ..ledger import features_hash
    from .outcomes import DEAD_POOL_LIQ, MAX_HONEST_MULTIPLE

    chain = normalize_chain(chain)
    rows = (
        session.query(Token, Research, Outcome, Decision)
        .join(Research, Research.token_id == Token.id)
        .join(Outcome, Outcome.token_id == Token.id)
        .outerjoin(
            Decision,
            (Decision.token_id == Token.id) & (Decision.kind == "entry"),
        )
        .filter(
            Token.chain == chain,
            Token.source != "backfill",
            Outcome.multiple >= 5.0,
            Outcome.multiple <= MAX_HONEST_MULTIPLE,
            Outcome.last_liq >= DEAD_POOL_LIQ,
            (Outcome.label.is_(None)) | (Outcome.label == 1),
        )
        .order_by(Outcome.multiple.desc())
        .limit(max(1, int(limit)) * 4)
        .all()
    )
    scanned = 0
    patched = 0
    seen_tokens: set[int] = set()
    for token, research, _outcome, decision in rows:
        if token.id in seen_tokens:
            continue
        if decision is None:
            continue
        seen_tokens.add(token.id)
        feat = _loads(decision.features_json, {})
        if not isinstance(feat, dict):
            feat = {}
        if not thesis_keys_thin(feat):
            continue
        scanned += 1
        sync_thesis_from_raw(research, token)
        refreshed = _loads(research.features_json, {})
        if not isinstance(refreshed, dict):
            continue
        keys = {k: float(refreshed.get(k) or 0.0) for k in THESIS_KEYS}
        if thesis_keys_thin(keys) and float(keys.get("github_auth_n") or 0.0) < 0.05:
            continue
        if not apply_thesis_keys(feat, keys):
            continue
        decision.features_json = json.dumps(feat, default=str)
        decision.features_hash = features_hash(decision.features_json)
        patched += 1
        if patched >= int(limit):
            break
    if patched:
        session.flush()
        log.info("runners thesis repair chain=%s patched=%s scanned=%s", chain, patched, scanned)
    return {"scanned": scanned, "patched": patched, "chain": chain}


def freeze_live_coverage_batch(
    session: Session,
    chain: str = "sol",
    *,
    limit: int = 120,
) -> dict[str, int]:
    """Write-once Live@entry onto Decisions from HuntCard / LiveSample.

    Never overwrites an existing freeze. Never invents a Live score.
    """
    from ..ledger import features_hash
    from ..models import utcnow

    chain = normalize_chain(chain)
    rows = (
        session.query(Decision, Token)
        .join(Token, Token.id == Decision.token_id)
        .filter(Decision.kind == "entry", Decision.source == "live", Token.chain == chain)
        .order_by(Decision.id.desc())
        .limit(max(1, int(limit)) * 3)
        .all()
    )
    scanned = 0
    frozen = 0
    for decision, token in rows:
        scanned += 1
        feat = _loads(decision.features_json, {})
        if not isinstance(feat, dict):
            feat = {}
        if feat.get(LIVE_AT_ENTRY_KEY) is not None:
            continue
        live_p: float | None = None
        src = ""
        hunt = (
            session.query(HuntCard)
            .filter(HuntCard.chain == token.chain, HuntCard.mint == token.mint)
            .first()
        )
        if hunt is not None and float(hunt.conviction_p or 0.0) > 0:
            live_p = float(hunt.conviction_p)
            src = "hunt_card"
        if live_p is None:
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
                sfeat = _loads(sample.features_json, {})
                if isinstance(sfeat, dict):
                    for key in ("live_p", "conviction_p", "model_p"):
                        if sfeat.get(key) is None:
                            continue
                        try:
                            val = float(sfeat[key])
                        except (TypeError, ValueError):
                            continue
                        if val > 0:
                            live_p = val
                            src = "live_sample"
                            break
        if live_p is None:
            continue
        merged = merge_live_at_entry(feat, live_p, src=src or "retro_freeze", at=utcnow())
        if merged is None:
            continue
        decision.features_json = json.dumps(merged, default=str)
        decision.features_hash = features_hash(decision.features_json)
        frozen += 1
        if frozen >= int(limit):
            break
    if frozen:
        session.flush()
        log.info("live freeze batch chain=%s frozen=%s scanned=%s", chain, frozen, scanned)
    return {"scanned": scanned, "frozen": frozen, "chain": chain}


async def apply_sanity_jobs(
    session: Session,
    chain: str = "sol",
    *,
    actions: list[str] | None = None,
) -> dict[str, Any]:
    """Run executable improve actions. Skips research/veto/chase suggestions."""
    chain = normalize_chain(chain)
    wanted = set(actions or list(EXECUTABLE)) & EXECUTABLE
    out: dict[str, Any] = {"chain": chain, "applied": [], "skipped": []}
    for name in ("repair_thin_thesis", "freeze_live_coverage", "enrich_thin_thesis_http"):
        if name not in wanted:
            out["skipped"].append(name)
    if "repair_thin_thesis" in wanted:
        recent = repair_thin_entry_thesis(session, limit=200)
        runners = repair_runners_thin_thesis(session, chain, limit=80)
        out["repair_thin_thesis"] = {"recent": recent, "runners": runners}
        out["applied"].append("repair_thin_thesis")
    if "freeze_live_coverage" in wanted:
        out["freeze_live_coverage"] = freeze_live_coverage_batch(session, chain, limit=120)
        out["applied"].append("freeze_live_coverage")
    if "enrich_thin_thesis_http" in wanted:
        out["enrich_thin_thesis_http"] = await enrich_runners_thin_thesis(
            session, chain, limit=12, website_budget=8
        )
        out["applied"].append("enrich_thin_thesis_http")
    return out


def apply_sanity_jobs_sync(
    session: Session,
    chain: str = "sol",
    *,
    actions: list[str] | None = None,
) -> dict[str, Any]:
    """Sync wrapper for worker paper sync (may call HTTP enrich)."""
    return asyncio.run(apply_sanity_jobs(session, chain, actions=actions))
