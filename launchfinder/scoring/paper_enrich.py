"""Background enrich for thin-facts paper opens.

Live ingest writes a Decision on migrate + Dex mcap/liq (one DAS page,
no full GMGN / X). This finishes those calls and cancels the paper-only
provisional open when a hard veto appears.

Does not rewrite entry ``p_good``. Does not grow FEATURE_NAMES.
Does not arm. FOMO wallet lookup stays off.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from ..models import PaperFill, Token, utcnow

log = logging.getLogger("launchfinder.paper_enrich")

PAPER_PROVISIONAL_ENRICH_LIMIT = 8


def _loads(raw: str | None, default: Any) -> Any:
    try:
        data = json.loads(raw or "")
    except Exception:
        return default
    return data if data is not None else default


def _provisional_tokens(session: Session, *, limit: int) -> list[Token]:
    from .paper_gate import PAPER_OPEN_VIA_THIN, research_is_provisional
    from .paper_v1 import PAPER_V1_LINE

    fills = (
        session.query(PaperFill)
        .filter(
            PaperFill.status.in_(("open", "queued")),
            PaperFill.line.in_(("gated90", PAPER_V1_LINE)),
        )
        .order_by(PaperFill.id.desc())
        .limit(max(8, int(limit) * 6))
        .all()
    )
    seen: set[int] = set()
    out: list[Token] = []
    for fill in fills:
        if fill.token_id in seen:
            continue
        token = session.get(Token, fill.token_id) if fill.token_id else None
        if token is None or token.research is None:
            continue
        feat = _loads(token.research.features_json, {})
        thin = (fill.open_via or "") == PAPER_OPEN_VIA_THIN
        if not research_is_provisional(feat if isinstance(feat, dict) else {}) and not thin:
            continue
        seen.add(int(fill.token_id))
        out.append(token)
        if len(out) >= int(limit):
            break
    return out


async def enrich_one_provisional(session: Session, token: Token) -> dict[str, Any]:
    """Finish GMGN / X / holders for one thin open. Cancel on hard veto."""
    import asyncio

    from ..chains import token_chain
    from ..ledger import cancel_provisional_paper_on_veto
    from ..research import github, gmgn, holders, twitter
    from ..scoring.features import extract_features
    from ..scoring.model import predict
    from ..scoring.paper_gate import paper_hard_veto, stamp_paper_provisional
    from ..serialize import associated_dev_handle, display_token_x
    from ..social import extract_github, extract_twitter_handle

    research = token.research
    if research is None:
        return {"ok": False, "reason": "no research"}
    chain = token_chain(token)
    raw = _loads(research.raw_json, {})
    if not isinstance(raw, dict):
        raw = {}
    mint = token.mint
    creator = token.creator or ""
    pool = token.pool_address or ""

    async def _gmgn() -> dict[str, Any]:
        try:
            return await gmgn.token_research(mint, chain=chain) or {}
        except Exception:
            log.exception("provisional GMGN enrich failed for %s", mint)
            return {}

    async def _holders() -> dict[str, Any]:
        try:
            return await holders.holder_stats(
                mint,
                creator,
                pool,
                chain=chain,
                max_pages=holders.SOL_HOLDER_TAPE_PAGES,
                annotate=True,
            )
        except Exception:
            log.exception("provisional holder enrich failed for %s", mint)
            return {}

    handle = extract_twitter_handle(token.twitter or research.twitter_handle or "")

    async def _tw() -> dict[str, Any]:
        if not handle:
            return {}
        try:
            return await twitter.lookup_handle(handle) or {}
        except Exception:
            log.exception("provisional X enrich failed for %s", mint)
            return {}

    blob = " ".join([token.description or "", token.website or "", token.github_url or ""])
    _gh_url, gh_ref = extract_github(blob)

    async def _gh() -> dict[str, Any]:
        if not gh_ref:
            return {}
        try:
            return await github.lookup_repo(gh_ref) or {}
        except Exception:
            log.exception("provisional GitHub enrich failed for %s", mint)
            return {}

    gmgn_info, holder_info, tw, gh = await asyncio.gather(_gmgn(), _holders(), _tw(), _gh())
    if gmgn_info:
        raw["gmgn"] = {**(raw.get("gmgn") or {} if isinstance(raw.get("gmgn"), dict) else {}), **gmgn_info}
    if holder_info:
        raw["holders"] = {**(raw.get("holders") or {} if isinstance(raw.get("holders"), dict) else {}), **holder_info}
        research.holder_count = int(holder_info.get("holder_count") or holder_info.get("holder_sample") or research.holder_count or 0)
        research.top10_pct = float(holder_info.get("top10_pct") or research.top10_pct or 0)
        research.creator_hold_pct = float(holder_info.get("creator_hold_pct") or research.creator_hold_pct or 0)
    if tw:
        raw["twitter"] = {**(raw.get("twitter") or {} if isinstance(raw.get("twitter"), dict) else {}), **tw}
        research.twitter_handle = handle or research.twitter_handle
        research.twitter_followers = int(tw.get("followers") or research.twitter_followers or 0)
        research.twitter_age_days = float(tw.get("age_days") or research.twitter_age_days or 0)
        research.twitter_verified = bool(tw.get("verified") or research.twitter_verified)
    if gh:
        raw["github"] = {**(raw.get("github") or {} if isinstance(raw.get("github"), dict) else {}), **gh}
        research.github_stars = int(gh.get("stars") or research.github_stars or 0)
        research.github_age_days = float(gh.get("age_days") or research.github_age_days or 0)
        if gh.get("url") and not token.github_url:
            token.github_url = str(gh.get("url") or "")
    research.raw_json = json.dumps(raw, default=str)

    feat = _loads(research.features_json, {})
    if not isinstance(feat, dict):
        feat = {}
    ctx = {
        "chain": chain,
        "source": token.source or "",
        "coin": {"name": token.name, "symbol": token.symbol, "source": token.source or ""},
        "market": raw.get("market") or {},
        "twitter": raw.get("twitter") or {},
        "twitter_handle": research.twitter_handle or "",
        "twitter_url": token.twitter or "",
        "website": token.website or "",
        "telegram": token.telegram or "",
        "github": raw.get("github") or {},
        "github_url": token.github_url or "",
        "holders": raw.get("holders") or {},
        "gmgn": raw.get("gmgn") or {},
        "creator_stats": raw.get("creator_stats") or {},
        "time_to_migrate_min": float(research.time_to_migrate_min or 0),
        "last_liq": float(token.outcome.last_liq) if token.outcome else 0.0,
        "t0_mcap": float(token.outcome.t0_mcap) if token.outcome else 0.0,
    }
    try:
        scored = predict(session, extract_features(ctx), chain=chain)
        flags = list(scored.get("risk_flags") or [])
        research.risk_flags_json = json.dumps(flags)
    except Exception:
        log.exception("provisional rescore flags failed for %s", mint)
        flags = _loads(research.risk_flags_json, [])
        if not isinstance(flags, list):
            flags = []

    handle_disp = display_token_x(research.twitter_handle, associated_dev_handle(token, research))
    veto = paper_hard_veto(
        flags,
        chain=chain,
        website=token.website or "",
        twitter=token.twitter or "",
        twitter_handle=handle_disp,
        twitter_followers=float(research.twitter_followers or 0),
        twitter_age_days=float(research.twitter_age_days or 0),
        twitter_verified=bool(research.twitter_verified),
    )
    cancelled = {"cancelled": 0, "tickets": 0}
    if veto:
        cancelled = cancel_provisional_paper_on_veto(session, token, veto, now=utcnow())
    feat = stamp_paper_provisional(feat, False)
    if veto:
        feat["paper_enrich_veto"] = veto
    research.features_json = json.dumps(feat)
    try:
        from .thesis_enrich import sync_research_thesis_features

        sync_research_thesis_features(research, token)
    except Exception:
        log.exception("provisional thesis sync failed for %s", mint)
    session.flush()
    return {"ok": True, "veto": veto, "cancelled": int(cancelled.get("cancelled") or 0)}


async def enrich_provisional_opens(session: Session, *, limit: int = PAPER_PROVISIONAL_ENRICH_LIMIT) -> dict[str, int]:
    """Worker tick: finish thin paper opens, cancel on hard veto. Paper only."""
    scanned = cancelled = vetoed = 0
    for token in _provisional_tokens(session, limit=limit):
        scanned += 1
        try:
            out = await enrich_one_provisional(session, token)
        except Exception:
            log.exception("provisional enrich failed for %s", token.mint)
            continue
        if out.get("veto"):
            vetoed += 1
        cancelled += int(out.get("cancelled") or 0)
    if scanned:
        log.info("provisional enrich scanned=%s vetoed=%s cancelled=%s", scanned, vetoed, cancelled)
    return {"scanned": scanned, "vetoed": vetoed, "cancelled": cancelled}
