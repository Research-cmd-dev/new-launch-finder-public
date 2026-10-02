"""Populate frozen thesis columns before entry Decision write.

paperV1 ranks on ``github_auth_n`` / ``real_project`` / ``gmgn_cto`` /
``name_quality``. Those keys are stamped once onto ``Decision.features_json``
from ``Research.features_json``. Later Bloom / runner enrich never rewrites
the Decision — so missing GitHub / CTO at first sight leaves soft thesis
silent (meme@0.105 only).

This module:
  - scrapes a project website for a GitHub URL when ingest missed it
  - recomputes the four thesis keys into Research before Decision write
  - optionally patches thin entry Decisions from already-stored raw_json
    (no new HTTP) so today's short list can see real tags

Does not grow FEATURE_NAMES. Does not rewrite ``p_good``.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from ..httputil import client
from ..models import Decision, HuntCard, Outcome, Research, Token, utcnow
from ..social import extract_github, is_official_brand_website
from .features import _real_project, github_authenticity

log = logging.getLogger("launchfinder.thesis_enrich")

THESIS_KEYS = ("github_auth_n", "real_project", "gmgn_cto", "name_quality")
# Cap website scrapes per worker cycle so Hunt stay snappy.
_WEBSITE_FETCH_BUDGET = 4
_website_fetches = 0
# paperV1 open short-list HTTP enrich (Cycle 7) — small batch per hunt_tape tick.
PAPER_V1_OPEN_HTTP_ENRICH_LIMIT = 8
PAPER_V1_OPEN_HTTP_WEBSITE_BUDGET = 5
PAPER_V1_FREE_SOURCES_LIMIT = 10
# One token_research per hunt_tape tick (info+security = 2 HTTP calls); avoids ban stampede.
PAPER_V1_GMGN_REFRESH_CAP = 2
PAPER_V1_PUMP_REFRESH_CAP = 2
PAPER_V1_OPEN_GITHUB_LOOKUP_CAP = 2


def reset_website_budget() -> None:
    global _website_fetches
    _website_fetches = 0


def thesis_keys_thin(features: dict[str, Any] | None) -> bool:
    feat = features if isinstance(features, dict) else {}
    auth = float(feat.get("github_auth_n") or 0.0)
    real = float(feat.get("real_project") or 0.0)
    cto = float(feat.get("gmgn_cto") or 0.0)
    return auth < 0.05 and real < 0.05 and cto < 0.05


def _loads(raw: str | None, default: Any) -> Any:
    try:
        data = json.loads(raw or "")
    except Exception:
        return default
    return data if data is not None else default


def _tw_from_research(research: Research, raw: dict[str, Any]) -> dict[str, Any]:
    tw = dict(raw.get("twitter") or {}) if isinstance(raw.get("twitter"), dict) else {}
    if research.twitter_handle and not tw.get("handle"):
        tw["handle"] = research.twitter_handle
    if research.twitter_followers and not tw.get("followers"):
        tw["followers"] = int(research.twitter_followers or 0)
    if research.twitter_age_days and not tw.get("age_days"):
        tw["age_days"] = float(research.twitter_age_days or 0.0)
    if research.twitter_verified and not tw.get("verified"):
        tw["verified"] = True
    return tw


def recompute_thesis_keys(
    features: dict[str, Any],
    *,
    gh: dict[str, Any],
    tw: dict[str, Any],
    gmgn: dict[str, Any],
    twitter_handle: str = "",
) -> dict[str, float]:
    """Return updated thesis key values (does not mutate features)."""
    ctx = {"twitter_handle": twitter_handle or tw.get("handle") or ""}
    return {
        "github_auth_n": float(github_authenticity(gh or {})),
        "real_project": float(_real_project(ctx, tw or {}, gh or {})),
        "gmgn_cto": 1.0 if (gmgn or {}).get("cto") else float(features.get("gmgn_cto") or 0.0),
        "name_quality": float(features.get("name_quality") or 0.0),
    }


def merge_thesis_feature_dict(
    entry_feat: dict[str, Any] | None,
    research_feat: dict[str, Any] | None,
) -> dict[str, Any]:
    """Max-merge thesis keys so thin non-zero entry blobs do not block Research."""
    out = dict(entry_feat) if isinstance(entry_feat, dict) else {}
    res = research_feat if isinstance(research_feat, dict) else {}
    for key in THESIS_KEYS:
        out[key] = max(float(out.get(key) or 0.0), float(res.get(key) or 0.0))
    return out


def prime_paper_v1_thesis_for_qualify(session: Session, token: Token) -> bool:
    """Sync stored meta + patch entry before paperV1 qualify/lock (future opens only).

    Does not rewrite historical short-list rows. No HTTP — GMGN/website runs on
    worker caps via ``enrich_paper_v1_qualify_candidates_http``.
    """
    research = session.query(Research).filter(Research.token_id == token.id).one_or_none()
    if research is None:
        return False
    refresh_stored_thesis_evidence(session, token.id, token=token, research=research)
    ensure_entry_thesis_from_stored(session, token.id)
    return True


def sync_research_thesis_features(research: Research, token: Token | None) -> dict[str, Any]:
    """Hydrate raw_json from columns/meta, sync thesis keys into features_json.

    No HTTP. Idempotent. Used by paperV1 gate reads and repair so production-gate
    sees the same github/dev/cto scalars as ``v1_hard_tag_ready``.
    """
    hydrate_thesis_raw_from_stored_meta(research, token)
    sync_thesis_from_raw(research, token)
    feat = _loads(research.features_json, {})
    return feat if isinstance(feat, dict) else {}


def apply_thesis_keys(features: dict[str, Any], keys: dict[str, float]) -> bool:
    """Write thesis keys into features. True when any value rose."""
    changed = False
    for name, value in keys.items():
        prev = float(features.get(name) or 0.0)
        if value > prev + 1e-9:
            features[name] = value
            changed = True
        elif name not in features:
            features[name] = value
            changed = True
    return changed


async def _website_blob(url: str) -> str:
    url = (url or "").strip()
    if not url.startswith("http"):
        return ""
    try:
        resp = await client().get(url, timeout=5.0)
        if resp.status_code >= 400:
            return ""
        return (resp.text or "")[:80_000]
    except Exception as exc:
        log.debug("thesis website fetch failed %s: %s", url, exc)
        return ""


async def discover_github_url(
    token: Token,
    *,
    allow_website_fetch: bool = True,
    website: str | None = None,
) -> tuple[str, str]:
    """Find (url, owner/repo) from token fields / website HTML."""
    global _website_fetches
    site = (website if website is not None else token.website) or ""
    blob = " ".join(
        part
        for part in (
            token.github_url or "",
            site,
            token.description or "",
        )
        if part
    )
    url, ref = extract_github(blob)
    if ref:
        return url, ref
    site = site.strip()
    if not allow_website_fetch or not site or is_official_brand_website(site):
        return "", ""
    if _website_fetches >= _WEBSITE_FETCH_BUDGET:
        return "", ""
    _website_fetches += 1
    html = await _website_blob(site)
    return extract_github(html)


def thesis_open_has_discoverable_evidence(token: Token, research: Research) -> bool:
    """True when HTTP / GitHub lookup might find honest hard-tag evidence."""
    from ..social import extract_github

    raw = _loads(research.raw_json, {})
    if not isinstance(raw, dict):
        raw = {}
    gh = raw.get("github") if isinstance(raw.get("github"), dict) else {}
    if str(gh.get("full_name") or "").strip():
        return True
    if (token.github_url or "").strip():
        return True
    if (token.website or "").strip():
        return True
    gmgn = raw.get("gmgn") if isinstance(raw.get("gmgn"), dict) else {}
    if str(gmgn.get("website") or "").strip():
        return True
    blob = " ".join(
        part
        for part in (
            token.description or "",
            token.twitter or "",
            token.telegram or "",
            str(raw.get("github_url") or ""),
        )
        if part
    )
    if extract_github(blob)[1]:
        return True
    market = raw.get("market") if isinstance(raw.get("market"), dict) else {}
    if isinstance(market, dict) and extract_github(str(market.get("github") or ""))[1]:
        return True
    return False


def _github_repo_ref_from_stored(
    token: Token, research: Research, gh: dict[str, Any] | None
) -> tuple[str, str]:
    """(url, owner/repo) from raw github blob + token columns — no HTTP."""
    from ..social import extract_github

    blob = dict(gh or {})
    ref = str(blob.get("full_name") or "").strip()
    url = str(blob.get("url") or "").strip()
    if ref:
        return url, ref
    for part in (token.github_url or "",):
        found_url, found_ref = extract_github(str(part or ""))
        if found_ref:
            return found_url, found_ref
    return "", ""


async def _github_lookup_into_research(
    session: Session,
    token: Token,
    research: Research,
    *,
    gh: dict[str, Any] | None = None,
) -> bool:
    """Honest GitHub API enrich when a stored ref exists but auth < 0.6."""
    from ..research import github as github_api

    raw = _loads(research.raw_json, {})
    if not isinstance(raw, dict):
        raw = {}
    gh_blob = dict(gh if gh is not None else (raw.get("github") or {}))
    if not isinstance(gh_blob, dict):
        gh_blob = {}
    if float(github_authenticity(gh_blob)) >= 0.6:
        return False
    url, ref = _github_repo_ref_from_stored(token, research, gh_blob)
    if not ref:
        return False
    try:
        looked = await github_api.lookup_repo(ref)
    except Exception:
        log.exception("thesis github lookup failed %s", ref)
        return False
    if not (looked.get("full_name") or looked.get("url")):
        return False
    raw["github"] = looked
    research.raw_json = json.dumps(raw, default=str)
    if looked.get("url"):
        token.github_url = str(looked["url"])
    research.github_stars = int(looked.get("stars") or research.github_stars or 0)
    research.github_forks = int(looked.get("forks") or research.github_forks or 0)
    research.github_age_days = float(looked.get("age_days") or research.github_age_days or 0.0)
    features = _loads(research.features_json, {})
    if not isinstance(features, dict):
        features = {}
    features["has_github"] = 1.0
    gmgn = dict(raw.get("gmgn") or {}) if isinstance(raw.get("gmgn"), dict) else {}
    tw = _tw_from_research(research, raw)
    handle = str(research.twitter_handle or tw.get("handle") or "")
    keys = recompute_thesis_keys(features, gh=looked, tw=tw, gmgn=gmgn, twitter_handle=handle)
    if float(features.get("name_quality") or 0.0) > 0:
        keys["name_quality"] = float(features["name_quality"])
    apply_thesis_keys(features, keys)
    research.features_json = json.dumps(features, default=str)
    session.add(research)
    session.add(token)
    return True


def _entry_needs_hard_tag(features: dict[str, Any]) -> bool:
    from .paper_v1 import THESIS_HARD_TAGS, v1_thesis_from_features

    hard = set(v1_thesis_from_features(features).get("hard_tags") or [])
    return not bool(hard & set(THESIS_HARD_TAGS))


def _thesis_keys_supply_hard_tag(keys: dict[str, float]) -> bool:
    from .paper_v1 import v1_thesis_from_features

    feat = {k: keys.get(k, 0.0) for k in THESIS_KEYS}
    return bool(v1_thesis_from_features(feat).get("hard_tags"))


def _truthy_flag(value: Any) -> bool:
    if value is True or value == 1:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return False


def _merge_github_blob(gh: dict[str, Any], *, url: str = "", ref: str = "", research: Research | None = None) -> dict[str, Any]:
    out = dict(gh or {})
    if ref:
        out.setdefault("full_name", ref)
    if url:
        out.setdefault("url", url)
    if research is not None:
        if float(research.github_age_days or 0) > 0:
            out.setdefault("age_days", float(research.github_age_days))
        if int(research.github_stars or 0) > 0:
            out.setdefault("stars", int(research.github_stars))
        if int(research.github_forks or 0) > 0:
            out.setdefault("forks", int(research.github_forks))
    if url or ref:
        out.setdefault("source", "url_meta")
    return out


def hydrate_thesis_raw_from_stored_meta(research: Research, token: Token | None = None) -> bool:
    """Backfill raw_json from Token columns + nested research.raw_json blobs.

    No HTTP. Covers GMGN CTO / socials already in raw, pump/Dex github hints,
    and token description links when website is missing.
    """
    from ..social import extract_github

    changed = hydrate_thesis_raw_from_research(research, token)
    raw = _loads(research.raw_json, {})
    if not isinstance(raw, dict):
        raw = {}
    gmgn = dict(raw.get("gmgn") or {}) if isinstance(raw.get("gmgn"), dict) else {}
    if _truthy_flag(gmgn.get("cto")):
        gmgn = {**gmgn, "cto": True}
        raw["gmgn"] = gmgn
        changed = True
    gh = dict(raw.get("github") or {}) if isinstance(raw.get("github"), dict) else {}
    if float(github_authenticity(gh)) < 0.6:
        for blob in (
            str((token.github_url if token else "") or ""),
            str((token.description if token else "") or ""),
            str((token.twitter if token else "") or ""),
            str((token.website if token else "") or ""),
            str((raw.get("github_url") if isinstance(raw.get("github_url"), str) else "") or ""),
        ):
            found_url, ref = extract_github(blob)
            if ref:
                gh = _merge_github_blob(gh, url=found_url, ref=ref, research=research)
                raw["github"] = gh
                changed = True
                break
        market = raw.get("market") if isinstance(raw.get("market"), dict) else {}
        if float(github_authenticity(gh)) < 0.6 and isinstance(market, dict):
            found_url, ref = extract_github(str(market.get("github") or ""))
            if ref:
                gh = _merge_github_blob(gh, url=found_url, ref=ref, research=research)
                raw["github"] = gh
                changed = True
    if token is not None:
        if not (token.website or "").strip():
            site = str(gmgn.get("website") or "")
            market = raw.get("market") if isinstance(raw.get("market"), dict) else {}
            if isinstance(market, dict):
                site = site or str(market.get("website") or "")
            if site:
                token.website = site
                changed = True
        if not (token.twitter or "").strip():
            handle = str(gmgn.get("twitter_username") or "").lstrip("@")
            if handle:
                token.twitter = f"https://x.com/{handle}"
                changed = True
        if not (token.github_url or "").strip() and gh.get("url"):
            token.github_url = str(gh["url"])
            changed = True
    tw = raw.get("twitter")
    if not isinstance(tw, dict) or not tw.get("handle"):
        handle = str(gmgn.get("twitter_username") or (research.twitter_handle if research else "") or "").lstrip("@")
        if handle:
            raw["twitter"] = {
                "handle": handle,
                "followers": int(gmgn.get("twitter_followers") or research.twitter_followers or 0),
                "age_days": float(research.twitter_age_days or 0.0),
                "verified": bool(research.twitter_verified),
                "source": "gmgn_meta",
            }
            changed = True
    if changed:
        research.raw_json = json.dumps(raw, default=str)
    return changed


def hydrate_thesis_raw_from_research(research: Research, token: Token | None = None) -> bool:
    """Backfill ``raw_json`` from persisted Research / Token columns — no HTTP.

    Hunt / ingest often stamp ``github_stars`` / ``github_url`` on the row
    while ``raw_json.github`` stayed empty, so ``sync_thesis_from_raw`` had
    nothing to read for paperV1 opens that qualified on Live alone.
    """
    raw = _loads(research.raw_json, {})
    if not isinstance(raw, dict):
        raw = {}
    changed = False
    gh = dict(raw.get("github") or {}) if isinstance(raw.get("github"), dict) else {}
    if float(github_authenticity(gh)) < 0.6:
        url = str((token.github_url if token else "") or "")
        from ..social import extract_github

        found_url, ref = extract_github(url)
        if ref:
            gh = dict(gh)
            gh.setdefault("full_name", ref)
            if found_url:
                gh.setdefault("url", found_url)
            if float(research.github_age_days or 0) > 0:
                gh.setdefault("age_days", float(research.github_age_days))
            if int(research.github_stars or 0) > 0:
                gh.setdefault("stars", int(research.github_stars))
            if int(research.github_forks or 0) > 0:
                gh.setdefault("forks", int(research.github_forks))
            gh.setdefault("source", "url_meta")
            raw["github"] = gh
            changed = True
    feat = _loads(research.features_json, {})
    if isinstance(feat, dict) and float(feat.get("gmgn_cto") or 0.0) >= 0.5:
        gmgn = dict(raw.get("gmgn") or {}) if isinstance(raw.get("gmgn"), dict) else {}
        if not gmgn.get("cto"):
            gmgn = {**gmgn, "cto": True}
            raw["gmgn"] = gmgn
            changed = True
    if changed:
        research.raw_json = json.dumps(raw, default=str)
    return changed


def sync_thesis_from_raw(research: Research, token: Token | None = None) -> bool:
    """Recompute thesis keys from Research.raw_json into features_json.

    No HTTP. Safe before Decision write and for thin-Decision repair.
    """
    features = _loads(research.features_json, {})
    if not isinstance(features, dict):
        features = {}
    raw = _loads(research.raw_json, {})
    if not isinstance(raw, dict):
        raw = {}
    gh = dict(raw.get("github") or {}) if isinstance(raw.get("github"), dict) else {}
    gmgn = dict(raw.get("gmgn") or {}) if isinstance(raw.get("gmgn"), dict) else {}
    tw = _tw_from_research(research, raw)
    handle = str(research.twitter_handle or tw.get("handle") or "")
    keys = recompute_thesis_keys(features, gh=gh, tw=tw, gmgn=gmgn, twitter_handle=handle)
    # Preserve name_quality already on the vector (default 0.7).
    if float(features.get("name_quality") or 0.0) > 0:
        keys["name_quality"] = float(features["name_quality"])
    if not apply_thesis_keys(features, keys):
        return False
    research.features_json = json.dumps(features, default=str)
    if token is not None and gh.get("url") and not token.github_url:
        token.github_url = str(gh["url"])
    return True


async def enrich_thesis_before_entry(session: Session, token: Token, research: Research) -> bool:
    """Last chance before Decision: pull GitHub when thesis keys are thin.

    Updates Research.features_json (+ raw.github) in place. Caller must
    then ``record_entry_decision``. Returns True when features changed.
    """
    from ..research import github as github_api

    features = _loads(research.features_json, {})
    if not isinstance(features, dict):
        features = {}
    if not thesis_keys_thin(features) and float(features.get("github_auth_n") or 0.0) >= 0.6:
        return sync_thesis_from_raw(research, token)

    raw = _loads(research.raw_json, {})
    if not isinstance(raw, dict):
        raw = {}
    gh = dict(raw.get("github") or {}) if isinstance(raw.get("github"), dict) else {}
    changed = False

    if float(github_authenticity(gh)) < 0.6:
        url, ref = await discover_github_url(token, allow_website_fetch=True)
        if not ref:
            url, ref = _github_repo_ref_from_stored(token, research, gh)
        if ref:
            try:
                looked = await github_api.lookup_repo(ref)
            except Exception:
                log.exception("thesis github lookup failed %s", ref)
                looked = {}
            if looked.get("full_name") or looked.get("url"):
                gh = looked
                raw["github"] = gh
                research.raw_json = json.dumps(raw, default=str)
                if gh.get("url"):
                    token.github_url = str(gh["url"])
                research.github_stars = int(gh.get("stars") or research.github_stars or 0)
                research.github_forks = int(gh.get("forks") or research.github_forks or 0)
                research.github_age_days = float(gh.get("age_days") or research.github_age_days or 0.0)
                features["has_github"] = 1.0
                features["github_stars_n"] = features.get("github_stars_n") or 0.0
                changed = True

    gmgn = dict(raw.get("gmgn") or {}) if isinstance(raw.get("gmgn"), dict) else {}
    tw = _tw_from_research(research, raw)
    handle = str(research.twitter_handle or tw.get("handle") or "")
    keys = recompute_thesis_keys(features, gh=gh, tw=tw, gmgn=gmgn, twitter_handle=handle)
    if float(features.get("name_quality") or 0.0) > 0:
        keys["name_quality"] = float(features["name_quality"])
    if apply_thesis_keys(features, keys):
        changed = True
    if changed:
        research.features_json = json.dumps(features, default=str)
        session.add(research)
    return changed


def repair_thin_entry_thesis(session: Session, *, limit: int = 120) -> dict[str, int]:
    """Patch thin entry Decision thesis keys from stored Research.raw_json.

    No HTTP. Only raises github_auth_n / real_project / gmgn_cto /
    name_quality when the Decision blob is thin and raw has signal.
    Idempotent via ScanState-friendly return counts.
    """
    from ..ledger import DECISION_ENTRY, features_hash

    rows = (
        session.query(Decision, Research, Token)
        .join(Research, Research.token_id == Decision.token_id)
        .join(Token, Token.id == Decision.token_id)
        .filter(Decision.kind == DECISION_ENTRY, Decision.source == "live")
        .order_by(Decision.id.desc())
        .limit(max(1, int(limit)))
        .all()
    )
    scanned = 0
    patched = 0
    for decision, research, token in rows:
        scanned += 1
        feat = _loads(decision.features_json, {})
        if not isinstance(feat, dict):
            feat = {}
        if not _entry_needs_hard_tag(feat):
            continue
        refreshed = sync_research_thesis_features(research, token)
        merged = merge_thesis_feature_dict(feat, refreshed)
        if not _thesis_keys_supply_hard_tag(merged):
            if thesis_keys_thin(merged) and float(merged.get("gmgn_cto") or 0.0) < 0.5:
                if float(merged.get("github_auth_n") or 0.0) < 0.05:
                    continue
        keys = {k: float(merged.get(k) or 0.0) for k in THESIS_KEYS}
        if not apply_thesis_keys(feat, keys):
            continue
        decision.features_json = json.dumps(feat, default=str)
        decision.features_hash = features_hash(decision.features_json)
        patched += 1
    if patched:
        session.flush()
        log.info("thesis repair patched %s of %s entry Decisions", patched, scanned)
    return {"scanned": scanned, "patched": patched}


def v1_hard_tag_ready(
    session: Session,
    token_id: int | None,
    decision_id: int | None = None,
) -> bool:
    """Sync stored thesis; True when a hard tag is present (entry or Research).

    No HTTP — meta in raw_json + Research columns. Wide fills may not have an
    entry Decision yet; still honor stored GitHub / CTO on Research.
    """
    from ..ledger import DECISION_ENTRY, _v1_thesis
    from .paper_v1 import v1_thesis_from_features, v1_thesis_ok

    if not token_id:
        return False
    research = session.query(Research).filter(Research.token_id == token_id).one_or_none()
    token = session.get(Token, token_id)
    if research is None:
        return False
    sync_research_thesis_features(research, token)
    entry = (
        session.query(Decision)
        .filter(Decision.token_id == token_id, Decision.kind == DECISION_ENTRY)
        .order_by(Decision.id.desc())
        .first()
    )
    if entry is not None:
        _patch_decision_from_research(entry, research)
        return v1_thesis_ok(_v1_thesis(session, token_id, decision_id))
    feat = _loads(research.features_json, {})
    return v1_thesis_ok(v1_thesis_from_features(feat if isinstance(feat, dict) else {}))


def refresh_stored_thesis_evidence(
    session: Session,
    token_id: int | None,
    *,
    token: Token | None = None,
    research: Research | None = None,
) -> dict[str, Any]:
    """Hydrate raw + sync Research thesis keys + patch entry Decision (no HTTP).

    Idempotent. Used by paper sync to close the gap where github/dev/cto
    evidence lives on Token / Research columns or raw_json but never reached
    frozen entry features (production-gate thesis bar).
    """
    from ..ledger import _v1_thesis

    if not token_id:
        return {"ok": False, "reason": "no_token"}
    if token is None:
        token = session.get(Token, int(token_id))
    if research is None:
        research = session.query(Research).filter(Research.token_id == int(token_id)).one_or_none()
    if token is None or research is None:
        return {"ok": False, "reason": "missing_row"}

    before = _v1_thesis(session, int(token_id))
    if before.get("hard_tags"):
        return {"ok": True, "skipped": "already_tagged", "gained_hard_tag": False}

    synced = False
    if hydrate_thesis_raw_from_stored_meta(research, token):
        synced = True
    if sync_thesis_from_raw(research, token):
        synced = True
    patched_entry = ensure_entry_thesis_from_stored(session, int(token_id))
    if synced or patched_entry:
        session.add(research)
        session.add(token)
        session.flush()
    after = _v1_thesis(session, int(token_id))
    gained = bool(after.get("hard_tags")) and not bool(before.get("hard_tags"))
    return {
        "ok": True,
        "synced": synced,
        "patched_entry": patched_entry,
        "gained_hard_tag": gained,
    }


def repair_desk_thesis_coverage(
    session: Session,
    *,
    open_limit: int = 150,
    hunt_limit: int = 80,
    shadow_limit: int = 40,
) -> dict[str, int]:
    """Paper-safe thesis sync: paperV1 opens, recent Hunt, paper_v1_shadow."""
    from ..models import PaperFill
    from .paper_v1 import PAPER_V1_LINE, PAPER_V1_SHADOW_LINE

    open_out = repair_paper_v1_open_thesis(session, limit=open_limit)
    hunt_scanned = 0
    hunt_gained = 0
    since_hunt = utcnow() - timedelta(days=7)
    hunt_rows = (
        session.query(HuntCard, Token)
        .join(Token, Token.id == HuntCard.token_id)
        .filter(HuntCard.updated_at >= since_hunt)
        .order_by(HuntCard.updated_at.desc())
        .limit(max(1, int(hunt_limit)) * 3)
        .all()
    )
    seen_hunt: set[int] = set()
    for _card, token in hunt_rows:
        if token.id in seen_hunt:
            continue
        seen_hunt.add(token.id)
        if hunt_scanned >= int(hunt_limit):
            break
        hunt_scanned += 1
        out = refresh_stored_thesis_evidence(session, token.id, token=token)
        if out.get("gained_hard_tag"):
            hunt_gained += 1

    shadow_scanned = 0
    shadow_gained = 0
    since_shadow = utcnow() - timedelta(days=14)
    shadow_rows = (
        session.query(PaperFill, Token)
        .join(Token, Token.id == PaperFill.token_id)
        .filter(
            PaperFill.line == PAPER_V1_SHADOW_LINE,
            PaperFill.opened_at >= since_shadow,
            PaperFill.token_id.isnot(None),
        )
        .order_by(PaperFill.id.desc())
        .limit(max(1, int(shadow_limit)) * 3)
        .all()
    )
    seen_shadow: set[int] = set()
    for _fill, token in shadow_rows:
        if token.id in seen_shadow:
            continue
        seen_shadow.add(token.id)
        if shadow_scanned >= int(shadow_limit):
            break
        shadow_scanned += 1
        out = refresh_stored_thesis_evidence(session, token.id, token=token)
        if out.get("gained_hard_tag"):
            shadow_gained += 1

    if hunt_gained or shadow_gained:
        log.info(
            "desk thesis coverage hunt_scanned=%s hunt_gained=%s shadow_scanned=%s shadow_gained=%s",
            hunt_scanned,
            hunt_gained,
            shadow_scanned,
            shadow_gained,
        )
    return {
        **{k: int(open_out.get(k) or 0) for k in ("scanned", "patched", "gained_hard_tag", "already_tagged", "skipped_empty")},
        "hunt_scanned": hunt_scanned,
        "hunt_gained_hard_tag": hunt_gained,
        "shadow_scanned": shadow_scanned,
        "shadow_gained_hard_tag": shadow_gained,
    }


def ensure_entry_thesis_from_stored(session: Session, token_id: int | None) -> bool:
    """No HTTP. Sync thesis keys from Research.raw_json onto entry Decision.

    Called on paperV1 qualify / reconsider / promote so hard tags reflect
    stored GitHub / CTO / dev evidence, not an empty gate Decision blob.
    """
    from ..ledger import DECISION_ENTRY

    if not token_id:
        return False
    decision = (
        session.query(Decision)
        .filter(Decision.token_id == token_id, Decision.kind == DECISION_ENTRY)
        .order_by(Decision.id.desc())
        .first()
    )
    research = session.query(Research).filter(Research.token_id == token_id).one_or_none()
    token = session.get(Token, token_id)
    if research is None:
        return False
    hydrate_thesis_raw_from_stored_meta(research, token)
    sync_thesis_from_raw(research, token)
    if decision is None:
        # Wide paper fills often have only a gate Decision. Still sync
        # Research so ``_v1_entry_features`` merge can count honest hard tags.
        return False
    return _patch_decision_from_research(decision, research)


def repair_paper_v1_open_thesis(session: Session, *, limit: int = 100) -> dict[str, int]:
    """Patch entry thesis for paperV1 opens that still read thin.

    Targets picked + closed short-list rows only (production-gate denominator).
    No HTTP — same stored raw_json path as ``repair_thin_entry_thesis``.
    """
    from sqlalchemy import func

    from ..ledger import _v1_thesis
    from ..models import PaperFill
    from .paper_v1 import PAPER_V1_LINE

    cap = max(1, int(limit))
    filt = (
        PaperFill.line == PAPER_V1_LINE,
        PaperFill.status.in_(("open", "closed")),
        PaperFill.token_id.isnot(None),
    )
    # Postgres rejects SELECT DISTINCT token_id ... ORDER BY id. One row per
    # token (latest fill id), then newest tokens first — same intent as before.
    latest_fill = (
        session.query(func.max(PaperFill.id).label("fill_id"))
        .filter(*filt)
        .group_by(PaperFill.token_id)
        .subquery()
    )
    token_ids = [
        int(row[0])
        for row in (
            session.query(PaperFill.token_id)
            .join(latest_fill, PaperFill.id == latest_fill.c.fill_id)
            .order_by(PaperFill.id.desc())
            .limit(cap)
            .all()
        )
        if row[0] is not None
    ]
    scanned = 0
    patched = 0
    gained_hard = 0
    skipped_empty = 0
    already_tagged = 0
    for token_id in token_ids:
        scanned += 1
        before = _v1_thesis(session, token_id)
        if before.get("hard_tags"):
            already_tagged += 1
            continue
        out = refresh_stored_thesis_evidence(session, token_id)
        if out.get("synced") or out.get("patched_entry"):
            patched += 1
        if out.get("gained_hard_tag"):
            gained_hard += 1
        elif not _v1_thesis(session, token_id).get("hard_tags"):
            skipped_empty += 1
    if patched:
        session.flush()
    log.info(
        "paperV1 open thesis repair scanned=%s tagged=%s patched=%s hard_tags=%s empty_raw=%s",
        scanned,
        already_tagged,
        patched,
        gained_hard,
        skipped_empty,
    )
    return {
        "scanned": scanned,
        "patched": patched,
        "gained_hard_tag": gained_hard,
        "already_tagged": already_tagged,
        "skipped_empty": skipped_empty,
    }


def audit_paper_v1_open_thesis_gaps(session: Session, *, limit: int = 500) -> dict[str, Any]:
    """Classify why paperV1 opens lack hard tags (offline / operator harness).

    Does not mutate rows. Production SQL shape (Postgres):

    ``SELECT pf.id, pf.token_id, pf.chain FROM paper_fills pf
      WHERE pf.line = 'paper_v1' AND pf.status IN ('open','closed')
      ORDER BY pf.id DESC LIMIT :limit;``

    Then for each token_id compare ``_v1_thesis`` vs ``recompute_thesis_keys`` on
    hydrated raw (see ``docs/PAPER_V1_THESIS_AUDIT.md``).
    """
    from ..ledger import _v1_thesis
    from ..models import PaperFill
    from .paper_v1 import PAPER_V1_LINE, v1_thesis_from_features

    rows = (
        session.query(PaperFill)
        .filter(
            PaperFill.line == PAPER_V1_LINE,
            PaperFill.status.in_(("open", "closed")),
            PaperFill.token_id.isnot(None),
        )
        .order_by(PaperFill.id.desc())
        .limit(max(1, int(limit)))
        .all()
    )
    out: dict[str, Any] = {
        "n": len(rows),
        "gate_hard_tag": 0,
        "raw_would_github": 0,
        "raw_would_dev": 0,
        "raw_would_cto": 0,
        "columns_only_github": 0,
        "meme_only": 0,
        "score_only_empty": 0,
    }
    for fill in rows:
        tid = fill.token_id
        thesis = _v1_thesis(session, tid, fill.decision_id)
        if thesis.get("hard_tags"):
            out["gate_hard_tag"] += 1
            continue
        research = session.query(Research).filter(Research.token_id == tid).one_or_none()
        token = session.get(Token, tid) if tid else None
        if research is None:
            out["score_only_empty"] += 1
            continue
        raw = _loads(research.raw_json, {})
        if not isinstance(raw, dict):
            raw = {}
        gh = dict(raw.get("github") or {}) if isinstance(raw.get("github"), dict) else {}
        gmgn = dict(raw.get("gmgn") or {}) if isinstance(raw.get("gmgn"), dict) else {}
        tw = _tw_from_research(research, raw)
        handle = str(research.twitter_handle or tw.get("handle") or "")
        keys = recompute_thesis_keys({}, gh=gh, tw=tw, gmgn=gmgn, twitter_handle=handle)
        th = v1_thesis_from_features(keys)
        tags = set(th.get("hard_tags") or [])
        if tags:
            if "github" in tags:
                out["raw_would_github"] += 1
            if "dev" in tags:
                out["raw_would_dev"] += 1
            if "cto" in tags:
                out["raw_would_cto"] += 1
            continue
        hydrated = hydrate_thesis_raw_from_stored_meta(research, token)
        if hydrated:
            raw = _loads(research.raw_json, {})
            gh = dict(raw.get("github") or {}) if isinstance(raw.get("github"), dict) else {}
            gmgn = dict(raw.get("gmgn") or {}) if isinstance(raw.get("gmgn"), dict) else {}
            tw = _tw_from_research(research, raw)
            keys = recompute_thesis_keys({}, gh=gh, tw=tw, gmgn=gmgn, twitter_handle=handle)
            if _thesis_keys_supply_hard_tag(keys):
                out["columns_only_github"] += 1
                continue
        feat = _loads(research.features_json, {})
        if isinstance(feat, dict) and float(feat.get("name_quality") or 0) >= 0.6:
            out["meme_only"] += 1
        else:
            out["score_only_empty"] += 1
    return out


def _patch_decision_from_research(decision: Decision, research: Research) -> bool:
    """Copy non-thin thesis keys from Research onto a thin entry Decision."""
    from ..ledger import features_hash

    feat = _loads(decision.features_json, {})
    if not isinstance(feat, dict):
        feat = {}
    if not _entry_needs_hard_tag(feat):
        return False
    refreshed = _loads(research.features_json, {})
    if not isinstance(refreshed, dict):
        return False
    merged = merge_thesis_feature_dict(feat, refreshed)
    if not _thesis_keys_supply_hard_tag(merged):
        return False
    if not apply_thesis_keys(feat, {k: float(merged.get(k) or 0.0) for k in THESIS_KEYS}):
        return False
    decision.features_json = json.dumps(feat, default=str)
    decision.features_hash = features_hash(decision.features_json)
    return True


def _paper_v1_open_fill_rows(session: Session, *, cap: int) -> list[tuple[Any, Token, Research]]:
    from ..models import PaperFill
    from .paper_v1 import PAPER_V1_LINE

    lim = max(1, int(cap))
    rows = (
        session.query(PaperFill, Token, Research)
        .join(Token, Token.id == PaperFill.token_id)
        .join(Research, Research.token_id == Token.id)
        .filter(
            PaperFill.line == PAPER_V1_LINE,
            PaperFill.status.in_(("open", "closed")),
            PaperFill.token_id.isnot(None),
        )
        .order_by(PaperFill.id.desc())
        .limit(lim * 4)
        .all()
    )
    out: list[tuple[PaperFill, Token, Research]] = []
    seen: set[int] = set()
    for fill, token, research in rows:
        if token.id in seen:
            continue
        seen.add(token.id)
        out.append((fill, token, research))
        if len(out) >= lim:
            break
    return out


async def enrich_paper_v1_open_free_sources(
    session: Session,
    *,
    limit: int = PAPER_V1_FREE_SOURCES_LIMIT,
    gmgn_cap: int = PAPER_V1_GMGN_REFRESH_CAP,
    pump_cap: int = PAPER_V1_PUMP_REFRESH_CAP,
) -> dict[str, Any]:
    """GMGN / pump / stored-meta thesis sources for paperV1 opens (no paid X).

    Runs before website HTTP enrich. Caps GMGN ``token_research`` and pump.fun
    ``get_coin`` calls per hunt_tape tick.
    """
    from ..chains import normalize_chain
    from ..ledger import _v1_thesis
    from ..research import gmgn as gmgn_api
    from ..research import pumpfun

    scanned = 0
    sourced_gmgn = 0
    sourced_meta = 0
    sourced_pump = 0
    tagged = 0
    patched = 0
    still_empty = 0
    errors = 0
    gmgn_calls = 0
    gmgn_skipped_cooldown = 0
    pump_calls = 0
    github_lookups = 0
    github_lookup_cap = max(0, int(PAPER_V1_OPEN_GITHUB_LOOKUP_CAP))
    gmgn_budget = max(0, int(gmgn_cap))
    for fill, token, research in _paper_v1_open_fill_rows(session, cap=limit):
        scanned += 1
        before = _v1_thesis(session, token.id, fill.decision_id)
        if before.get("hard_tags"):
            continue
        try:
            if hydrate_thesis_raw_from_stored_meta(research, token):
                sourced_meta += 1
            sync_thesis_from_raw(research, token)
            if ensure_entry_thesis_from_stored(session, token.id):
                patched += 1
            after = _v1_thesis(session, token.id, fill.decision_id)
            if after.get("hard_tags"):
                tagged += 1
                continue
            info = None
            if gmgn_budget > 0 and settings_gmgn_key() and gmgn_calls < gmgn_budget:
                if not gmgn_api.gmgn_deep_available():
                    gmgn_skipped_cooldown += 1
                else:
                    seed = _loads(research.raw_json, {}).get("gmgn")
                    seed = seed if isinstance(seed, dict) else None
                    info = await gmgn_api.token_research(token.mint, seed=seed, chain=token.chain or "sol")
                    gmgn_calls += 1
                    if not gmgn_api.gmgn_deep_available():
                        gmgn_budget = gmgn_calls
                if info:
                    raw = _loads(research.raw_json, {})
                    if not isinstance(raw, dict):
                        raw = {}
                    raw["gmgn"] = {**(raw.get("gmgn") or {}), **info}
                    research.raw_json = json.dumps(raw, default=str)
                    sourced_gmgn += 1
                    hydrate_thesis_raw_from_stored_meta(research, token)
                    sync_thesis_from_raw(research, token)
                    if ensure_entry_thesis_from_stored(session, token.id):
                        patched += 1
                    after = _v1_thesis(session, token.id, fill.decision_id)
                    if after.get("hard_tags"):
                        tagged += 1
                        continue
            if (
                pump_calls < int(pump_cap)
                and normalize_chain(token.chain or "sol") == "sol"
                and not (token.website or "").strip()
            ):
                coin = await pumpfun.get_coin(token.mint)
                pump_calls += 1
                if coin:
                    if coin.get("website") and not (token.website or "").strip():
                        token.website = str(coin.get("website") or "")
                    if coin.get("twitter") and not (token.twitter or "").strip():
                        token.twitter = str(coin.get("twitter") or "")
                    if coin.get("description") and not (token.description or "").strip():
                        token.description = str(coin.get("description") or "")
                    sourced_pump += 1
                    hydrate_thesis_raw_from_stored_meta(research, token)
                    sync_thesis_from_raw(research, token)
                    if ensure_entry_thesis_from_stored(session, token.id):
                        patched += 1
                    after = _v1_thesis(session, token.id, fill.decision_id)
                    if after.get("hard_tags"):
                        tagged += 1
                        continue
            after = _v1_thesis(session, token.id, fill.decision_id)
            if (
                not after.get("hard_tags")
                and github_lookups < github_lookup_cap
                and thesis_open_has_discoverable_evidence(token, research)
            ):
                raw = _loads(research.raw_json, {})
                gh = raw.get("github") if isinstance(raw, dict) and isinstance(raw.get("github"), dict) else {}
                if await _github_lookup_into_research(session, token, research, gh=gh):
                    github_lookups += 1
                    sync_thesis_from_raw(research, token)
                    if ensure_entry_thesis_from_stored(session, token.id):
                        patched += 1
                    after = _v1_thesis(session, token.id, fill.decision_id)
                    if after.get("hard_tags"):
                        tagged += 1
            if not after.get("hard_tags"):
                still_empty += 1
        except Exception:
            log.exception("paperV1 free-source thesis enrich failed for %s", token.mint)
            errors += 1
    if scanned:
        session.flush()
    log.info(
        "paperV1 open thesis free scanned=%s sourced_meta=%s sourced_gmgn=%s sourced_pump=%s "
        "tagged=%s patched=%s still_empty=%s errors=%s gmgn_calls=%s gmgn_skipped_cooldown=%s "
        "pump_calls=%s github_lookups=%s",
        scanned,
        sourced_meta,
        sourced_gmgn,
        sourced_pump,
        tagged,
        patched,
        still_empty,
        errors,
        gmgn_calls,
        gmgn_skipped_cooldown,
        pump_calls,
        github_lookups,
    )
    return {
        "scanned": scanned,
        "sourced_meta": sourced_meta,
        "sourced_gmgn": sourced_gmgn,
        "sourced_pump": sourced_pump,
        "tagged": tagged,
        "patched": patched,
        "still_empty": still_empty,
        "errors": errors,
        "gmgn_calls": gmgn_calls,
        "gmgn_skipped_cooldown": gmgn_skipped_cooldown,
        "pump_calls": pump_calls,
        "github_lookups": github_lookups,
    }


def settings_gmgn_key() -> bool:
    from ..config import settings

    return bool(settings.gmgn_api_key)


async def enrich_paper_v1_open_thin_thesis_http(
    session: Session,
    *,
    limit: int = PAPER_V1_OPEN_HTTP_ENRICH_LIMIT,
    website_budget: int = PAPER_V1_OPEN_HTTP_WEBSITE_BUDGET,
) -> dict[str, Any]:
    """HTTP enrich for paperV1 opens still missing hard thesis tags.

    Targets the production-gate denominator (open + closed ``paper_v1`` rows)
    without hard tags — not unbounded runner history. Reuses
    ``enrich_thesis_before_entry`` (website / GitHub lookup, no paid X).
    """
    from ..ledger import _v1_thesis
    from ..models import PaperFill
    from .paper_v1 import PAPER_V1_LINE

    global _WEBSITE_FETCH_BUDGET, _website_fetches

    cap = max(1, int(limit))
    prev_budget = _WEBSITE_FETCH_BUDGET
    _WEBSITE_FETCH_BUDGET = max(1, int(website_budget))
    _website_fetches = 0
    scanned = 0
    enriched = 0
    tagged = 0
    patched = 0
    skipped = 0
    skipped_no_site = 0
    errors = 0
    try:
        for fill, token, research in _paper_v1_open_fill_rows(session, cap=cap):
            before = _v1_thesis(session, token.id, fill.decision_id)
            if before.get("hard_tags"):
                skipped += 1
                continue
            if not thesis_open_has_discoverable_evidence(token, research):
                skipped_no_site += 1
                continue
            scanned += 1
            try:
                changed = await enrich_thesis_before_entry(session, token, research)
            except Exception:
                log.exception("paperV1 open thesis http enrich failed for %s", token.mint)
                errors += 1
                continue
            if changed:
                enriched += 1
            hydrate_thesis_raw_from_stored_meta(research, token)
            sync_thesis_from_raw(research, token)
            if ensure_entry_thesis_from_stored(session, token.id):
                patched += 1
            after = _v1_thesis(session, token.id, fill.decision_id)
            if after.get("hard_tags") and not before.get("hard_tags"):
                tagged += 1
            if scanned >= cap or _website_fetches >= _WEBSITE_FETCH_BUDGET:
                break
        if scanned or skipped or skipped_no_site:
            session.flush()
        log.info(
            "paperV1 open thesis http scanned=%s enriched=%s tagged=%s patched=%s "
            "skipped=%s no_site=%s errors=%s fetches=%s",
            scanned,
            enriched,
            tagged,
            patched,
            skipped,
            skipped_no_site,
            errors,
            _website_fetches,
        )
        return {
            "scanned": scanned,
            "enriched": enriched,
            "tagged": tagged,
            "patched": patched,
            "skipped": skipped,
            "skipped_no_site": skipped_no_site,
            "errors": errors,
            "website_fetches": _website_fetches,
            "website_budget": _WEBSITE_FETCH_BUDGET,
        }
    finally:
        _WEBSITE_FETCH_BUDGET = prev_budget


async def enrich_paper_v1_qualify_candidates_http(
    session: Session,
    chain: str = "sol",
    *,
    limit: int = 4,
    website_budget: int = 4,
    gmgn_cap: int = 2,
) -> dict[str, Any]:
    """HTTP thesis discovery for wide fills approaching paperV1 qualify (future opens).

    Does not touch existing paper_v1 short-list rows. Caps GMGN + website fetches.
    """
    from datetime import timedelta

    from ..chains import normalize_chain
    from ..ledger import PAPER_LINE, _v1_has_row
    from ..models import PaperFill
    from ..research import gmgn as gmgn_api
    from .paper_v1 import PAPER_V1_RECONSIDER_CHAINS, PAPER_V1_RECONSIDER_HOURS

    global _WEBSITE_FETCH_BUDGET, _website_fetches

    chain = normalize_chain(chain)
    if chain not in PAPER_V1_RECONSIDER_CHAINS:
        return {"scanned": 0, "enriched": 0, "tagged": 0}
    from ..ledger import _v1_thesis

    now = utcnow()
    window = timedelta(hours=float(PAPER_V1_RECONSIDER_HOURS))
    since = now - window - timedelta(hours=1)
    prev_budget = _WEBSITE_FETCH_BUDGET
    _WEBSITE_FETCH_BUDGET = max(1, int(website_budget))
    _website_fetches = 0
    scanned = enriched = tagged = gmgn_calls = 0
    cap = max(1, int(limit))
    gmgn_budget = max(0, int(gmgn_cap))
    fills = (
        session.query(PaperFill)
        .filter(
            PaperFill.chain == chain,
            PaperFill.line == PAPER_LINE,
            PaperFill.status == "open",
            PaperFill.opened_at >= since,
            PaperFill.token_id.isnot(None),
        )
        .order_by(PaperFill.id.desc())
        .limit(cap * 4)
        .all()
    )
    try:
        for fill in fills:
            if _v1_has_row(session, chain, fill.mint):
                continue
            token = session.get(Token, int(fill.token_id))
            if token is None:
                continue
            research = session.query(Research).filter(Research.token_id == token.id).one_or_none()
            if research is None:
                continue
            if (_v1_thesis(session, token.id, fill.decision_id).get("hard_tags")):
                continue
            prime_paper_v1_thesis_for_qualify(session, token)
            if (_v1_thesis(session, token.id, fill.decision_id).get("hard_tags")):
                tagged += 1
                continue
            scanned += 1
            site = (token.website or "").strip()
            gh_hint = (token.github_url or "").strip()
            if site or gh_hint:
                try:
                    if await enrich_thesis_before_entry(session, token, research):
                        enriched += 1
                except Exception:
                    log.exception("qualify thesis http enrich failed %s", token.mint)
            elif gmgn_budget > 0 and gmgn_calls < gmgn_budget and settings_gmgn_key():
                if gmgn_api.gmgn_deep_available():
                    info = await gmgn_api.token_research(token.mint, chain=token.chain or "sol")
                    gmgn_calls += 1
                    if info:
                        raw = _loads(research.raw_json, {})
                        if not isinstance(raw, dict):
                            raw = {}
                        raw["gmgn"] = {**(raw.get("gmgn") or {}), **info}
                        research.raw_json = json.dumps(raw, default=str)
                        hydrate_thesis_raw_from_stored_meta(research, token)
                        sync_thesis_from_raw(research, token)
                        ensure_entry_thesis_from_stored(session, token.id)
                        enriched += 1
            if (_v1_thesis(session, token.id, fill.decision_id).get("hard_tags")):
                tagged += 1
            if scanned >= cap:
                break
        if scanned or enriched or tagged:
            session.flush()
    finally:
        _WEBSITE_FETCH_BUDGET = prev_budget
    return {
        "scanned": scanned,
        "enriched": enriched,
        "tagged": tagged,
        "gmgn_calls": gmgn_calls,
        "website_fetches": _website_fetches,
    }


async def enrich_runners_thin_thesis(
    session: Session,
    chain: str = "sol",
    *,
    limit: int = 12,
    website_budget: int = 8,
) -> dict[str, Any]:
    """HTTP website→GitHub enrich for thin confirmed runners, then Decision patch.

    Uses ``enrich_thesis_before_entry`` (existing seam). Does not rewrite
    ``p_good`` / FEATURE_NAMES. Caps website fetches via ``website_budget``.
    """
    from ..chains import normalize_chain
    from .outcomes import DEAD_POOL_LIQ, MAX_HONEST_MULTIPLE

    global _WEBSITE_FETCH_BUDGET, _website_fetches

    chain = normalize_chain(chain)
    prev_budget = _WEBSITE_FETCH_BUDGET
    _WEBSITE_FETCH_BUDGET = max(1, int(website_budget))
    _website_fetches = 0
    scanned = 0
    enriched = 0
    patched = 0
    skipped_no_site = 0
    seen: set[int] = set()
    try:
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
            .limit(max(1, int(limit)) * 6)
            .all()
        )
        for token, research, _outcome, decision in rows:
            if token.id in seen:
                continue
            if decision is None:
                continue
            seen.add(token.id)
            feat = _loads(decision.features_json, {})
            if not isinstance(feat, dict):
                feat = {}
            if not thesis_keys_thin(feat):
                continue
            scanned += 1
            site = (token.website or "").strip()
            gh_url = (token.github_url or "").strip()
            if not site and not gh_url:
                skipped_no_site += 1
                continue
            changed = await enrich_thesis_before_entry(session, token, research)
            if changed:
                enriched += 1
            if _patch_decision_from_research(decision, research):
                patched += 1
            if scanned >= int(limit) or _website_fetches >= _WEBSITE_FETCH_BUDGET:
                break
        if patched or enriched:
            session.flush()
            log.info(
                "runners HTTP enrich chain=%s scanned=%s enriched=%s patched=%s fetches=%s",
                chain,
                scanned,
                enriched,
                patched,
                _website_fetches,
            )
        return {
            "chain": chain,
            "scanned": scanned,
            "enriched": enriched,
            "patched": patched,
            "skipped_no_site": skipped_no_site,
            "website_fetches": _website_fetches,
            "website_budget": _WEBSITE_FETCH_BUDGET,
        }
    finally:
        _WEBSITE_FETCH_BUDGET = prev_budget
