"""Production-gate thesis inventory + one-shot repair (paper only)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..image_rev import IMAGE_REV
from ..models import PaperFill, Research, Token
from ..social import extract_github
from .paper_v1 import PAPER_V1_LINE, THESIS_HARD_TAGS, v1_thesis_from_features
from .production_gate import THESIS_COVERAGE_BAR, _thesis_tagged, _v1_opens
from .thesis_enrich import (
    THESIS_KEYS,
    merge_thesis_feature_dict,
    refresh_stored_thesis_evidence,
    repair_paper_v1_open_thesis,
    sync_research_thesis_features,
    _loads,
    _tw_from_research,
)


def _entry_feat(session: Session, token_id: int | None) -> dict[str, Any]:
    from ..ledger import DECISION_ENTRY
    from ..models import Decision

    if not token_id:
        return {}
    entry = (
        session.query(Decision)
        .filter(Decision.token_id == token_id, Decision.kind == DECISION_ENTRY)
        .order_by(Decision.id.desc())
        .first()
    )
    if entry is None:
        return {}
    feat = _loads(entry.features_json, {})
    return feat if isinstance(feat, dict) else {}


def _feat_snapshot(features: dict[str, Any] | None) -> dict[str, float]:
    feat = features if isinstance(features, dict) else {}
    return {k: round(float(feat.get(k) or 0.0), 4) for k in THESIS_KEYS}


def _evidence_snapshot(research: Research | None, token: Token | None) -> dict[str, Any]:
    raw = _loads(research.raw_json, {}) if research else {}
    if not isinstance(raw, dict):
        raw = {}
    gh = raw.get("github") if isinstance(raw.get("github"), dict) else {}
    gmgn = raw.get("gmgn") if isinstance(raw.get("gmgn"), dict) else {}
    return {
        "github_url": (token.github_url or "").strip() if token else "",
        "website": (token.website or "").strip() if token else "",
        "twitter": (token.twitter or "").strip()[:80] if token else "",
        "github_stars_col": int(research.github_stars or 0) if research else 0,
        "github_forks_col": int(research.github_forks or 0) if research else 0,
        "github_age_col": float(research.github_age_days or 0) if research else 0.0,
        "twitter_handle_col": (research.twitter_handle or "").strip() if research else "",
        "raw_github_blob": bool(gh),
        "raw_gmgn_cto": bool(gmgn.get("cto")),
        "offsite_github_hint": bool(
            extract_github(
                " ".join(
                    p
                    for p in (
                        (token.description if token else "") or "",
                        (token.twitter if token else "") or "",
                        (token.telegram if token else "") or "",
                    )
                    if p
                )
            )[1]
        ),
    }


def _classify_row(
    *,
    gate_tagged: bool,
    after_sync: dict[str, Any],
    evidence: dict[str, Any],
) -> str:
    if gate_tagged:
        return "gate_tagged"
    hard = set(after_sync.get("hard_tags") or [])
    if hard & set(THESIS_HARD_TAGS):
        return "hard_after_sync_not_gate"
    has_stored = any(
        [
            evidence.get("github_url"),
            evidence.get("raw_github_blob"),
            evidence.get("github_stars_col", 0) > 0,
            evidence.get("raw_gmgn_cto"),
            evidence.get("twitter_handle_col"),
        ]
    )
    if has_stored:
        return "stored_evidence_below_hard_threshold"
    if float(after_sync.get("name_quality") or 0) >= 0.6:
        return "meme_only_no_stored_hard_evidence"
    return "thin_no_evidence"


def thesis_gate_counts(session: Session) -> tuple[int, int]:
    fills = _v1_opens(session)
    tagged = sum(1 for fill in fills if _thesis_tagged(session, fill))
    return len(fills), tagged


def thesis_gate_inventory(session: Session) -> dict[str, Any]:
    """Per-fill evidence for the production-gate thesis denominator (read-only summary)."""
    from ..ledger import _v1_entry_features, _v1_thesis

    fills = _v1_opens(session)
    rows: list[dict[str, Any]] = []
    class_counts: dict[str, int] = {}
    chains: dict[str, int] = {}

    for fill in fills:
        chains[fill.chain or ""] = chains.get(fill.chain or "", 0) + 1
        token = session.get(Token, fill.token_id) if fill.token_id else None
        research = (
            session.query(Research).filter(Research.token_id == fill.token_id).one_or_none()
            if fill.token_id
            else None
        )
        evidence = _evidence_snapshot(research, token)
        feat_stored_before = _feat_snapshot(_loads(research.features_json, {}) if research else {})
        entry_before = _feat_snapshot(_entry_feat(session, fill.token_id))

        if research is not None and token is not None:
            synced = sync_research_thesis_features(research, token)
        else:
            synced = {}
        feat_after_sync = _feat_snapshot(synced)
        merged = merge_thesis_feature_dict(_entry_feat(session, fill.token_id), synced)
        thesis_merged = v1_thesis_from_features(merged)
        gate_thesis = _v1_thesis(session, fill.token_id, fill.decision_id)
        gate_tagged = _thesis_tagged(session, fill)

        via_entry = _v1_entry_features(session, fill.token_id, fill.decision_id)
        hydrate_called = bool(research is not None)

        klass = _classify_row(
            gate_tagged=gate_tagged,
            after_sync=thesis_merged,
            evidence=evidence,
        )
        class_counts[klass] = class_counts.get(klass, 0) + 1

        rows.append(
            {
                "fill_id": fill.id,
                "token_id": fill.token_id,
                "mint": fill.mint,
                "symbol": (token.symbol or "") if token else "",
                "chain": fill.chain,
                "status": fill.status,
                "gate_tagged": gate_tagged,
                "gate_hard_tags": gate_thesis.get("hard_tags") or [],
                "class": klass,
                "evidence": evidence,
                "entry_features": entry_before,
                "research_features_before_sync": feat_stored_before,
                "research_features_after_sync": feat_after_sync,
                "merged_after_sync": _feat_snapshot(merged),
                "merged_hard_tags": thesis_merged.get("hard_tags") or [],
                "v1_entry_features_path": _feat_snapshot(via_entry),
                "hydrate_sync_on_read": hydrate_called,
            }
        )

    n, tagged = thesis_gate_counts(session)
    return {
        "paper_only": True,
        "image_rev": IMAGE_REV,
        "definition": f"hard tag (github/dev/cto) on {PAPER_V1_LINE} open+closed fills",
        "gate_bar": THESIS_COVERAGE_BAR,
        "n_fills": n,
        "tagged": tagged,
        "coverage": round(tagged / n, 4) if n else None,
        "by_chain": chains,
        "by_class": class_counts,
        "rows": rows,
        "read_path": "_thesis_tagged -> _v1_thesis -> _v1_entry_features -> sync_research_thesis_features + merge",
    }


async def repair_thesis_gate_one_shot(
    session: Session,
    *,
    gmgn_cap: int = 20,
    pump_cap: int = 20,
    free_limit: int = 50,
    http_limit: int = 40,
    website_budget: int = 30,
) -> dict[str, Any]:
    """HTTP + stored-meta thesis repair on the full gate denominator."""
    from .thesis_enrich import (
        enrich_paper_v1_open_free_sources,
        enrich_paper_v1_open_thin_thesis_http,
    )

    n_before, tagged_before = thesis_gate_counts(session)
    fills = _v1_opens(session)
    seen: set[int] = set()
    for fill in fills:
        if not fill.token_id or fill.token_id in seen:
            continue
        seen.add(int(fill.token_id))
        refresh_stored_thesis_evidence(session, int(fill.token_id))

    free = await enrich_paper_v1_open_free_sources(
        session,
        limit=free_limit,
        gmgn_cap=gmgn_cap,
        pump_cap=pump_cap,
    )
    http = await enrich_paper_v1_open_thin_thesis_http(
        session,
        limit=http_limit,
        website_budget=website_budget,
    )
    desk = repair_paper_v1_open_thesis(session, limit=max(50, len(seen) + 5))
    session.flush()

    n_after, tagged_after = thesis_gate_counts(session)
    inv = thesis_gate_inventory(session)
    return {
        "paper_only": True,
        "image_rev": IMAGE_REV,
        "before": {"n_fills": n_before, "tagged": tagged_before},
        "after": {"n_fills": n_after, "tagged": tagged_after},
        "delta_tagged": tagged_after - tagged_before,
        "refresh_tokens": len(seen),
        "free_sources": free,
        "http_enrich": http,
        "open_repair": desk,
        "by_class_after": inv.get("by_class"),
        "rows_after": [
            {
                "fill_id": r["fill_id"],
                "symbol": r["symbol"],
                "mint": r["mint"],
                "gate_tagged": r["gate_tagged"],
                "class": r["class"],
                "merged_hard_tags": r["merged_hard_tags"],
            }
            for r in inv.get("rows") or []
        ],
    }
