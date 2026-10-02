"""Pre-warm research for launchpad books ~20% from graduation.

Watch already lists near-completion. This stores coin + socials in
ScanState so migrate research is ready. Does not create a Token,
stamp migrated_at, or take a hunt chair. Stuck-door 0.90 stays.
No extra GMGN HTTP.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..chains import normalize_chain, normalize_mint
from ..models import ScanState, Token, utcnow
from ..social import extract_github, extract_twitter_handle

log = logging.getLogger("launchfinder.prewarm")

# ~20% of the curve left. Do not raise into the stuck-door band.
PREWARM_MIN_PROGRESS = 0.80
PREWARM_MAX_PROGRESS = 0.90
MAX_PER_CYCLE = 2


def prewarm_key(chain: str, mint: str) -> str:
    chain = normalize_chain(chain)
    short = "rh" if chain == "robinhood" else "sol"
    mint = normalize_mint(mint, chain)
    return f"pw:{short}:{mint}"[:64]


def is_prewarm_candidate(row: dict[str, Any], chain: str = "sol") -> bool:
    """True for a live 0.80–0.89 curve book that is not frozen leftover."""
    chain = normalize_chain(chain)
    mint = str(row.get("mint") or row.get("address") or "").strip()
    if not mint:
        return False
    try:
        prog = float(row.get("progress") or row.get("launchpad_progress") or 0.0)
    except (TypeError, ValueError):
        prog = 0.0
    if prog > 1.0:
        prog /= 100.0
    if prog < PREWARM_MIN_PROGRESS or prog >= PREWARM_MAX_PROGRESS:
        return False
    if chain == "sol":
        from ..ingest.pump_poll import is_frozen_trench

        return not is_frozen_trench(row)
    from ..ingest.rh_poll import is_dust_trench, is_frozen_low_progress

    return not is_frozen_low_progress(row) and not is_dust_trench(row)


def load_prewarm(session: Session, mint: str, chain: str = "sol") -> dict[str, Any] | None:
    row = session.query(ScanState).filter(ScanState.key == prewarm_key(chain, mint)).one_or_none()
    if row is None or not row.value:
        return None
    try:
        data = json.loads(row.value)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def save_prewarm(session: Session, payload: dict[str, Any]) -> str:
    chain = normalize_chain(str(payload.get("chain") or "sol"))
    mint = normalize_mint(str(payload.get("mint") or ""), chain)
    key = prewarm_key(chain, mint)
    value = json.dumps(payload, default=str)
    row = session.query(ScanState).filter(ScanState.key == key).one_or_none()
    if row is None:
        session.add(ScanState(key=key, value=value, updated_at=utcnow()))
    else:
        row.value = value
        row.updated_at = utcnow()
    session.flush()
    return key


def mark_prewarm_used(session: Session, mint: str, chain: str = "sol") -> None:
    row = session.query(ScanState).filter(ScanState.key == prewarm_key(chain, mint)).one_or_none()
    if row is not None:
        session.delete(row)
        session.flush()


def merge_prewarm_into_coin(coin: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Fill missing coin fields. Does not overwrite a live migrate payload."""
    out = dict(coin or {})
    stored = payload.get("coin") if isinstance(payload.get("coin"), dict) else {}
    for key in (
        "name",
        "symbol",
        "twitter",
        "website",
        "telegram",
        "creator",
        "description",
        "image_url",
        "pool_address",
        "launchpad",
        "gmgn_row",
    ):
        if stored.get(key) and not out.get(key):
            out[key] = stored[key]
    tw = payload.get("twitter")
    if isinstance(tw, dict) and tw:
        out["prewarm_twitter"] = tw
    gh = payload.get("github")
    if isinstance(gh, dict) and gh:
        out["prewarm_github"] = gh
    if payload.get("preview_p"):
        out["preview_p"] = float(payload.get("preview_p") or 0.0)
        out["preview_reasons"] = list(payload.get("preview_reasons") or [])
        out["preview_flags"] = list(payload.get("preview_flags") or [])
    return out


def annotate_watch(session: Session, rows: list[dict[str, Any]], chain: str) -> list[dict[str, Any]]:
    from ..scoring.preview import attach_watch_scores

    mints = [str(r.get("mint") or "") for r in rows if r.get("mint")]
    if not mints:
        return rows
    prewarms: dict[str, dict[str, Any]] = {}
    for mint in mints:
        data = load_prewarm(session, mint, chain)
        if data:
            prewarms[mint] = data
    return attach_watch_scores(session, rows, chain, prewarms)


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items() if k != "raw"}
    if isinstance(value, list):
        return [_json_safe(v) for v in value[:20]]
    return value


async def build_prewarm_payload(row: dict[str, Any], chain: str) -> dict[str, Any]:
    """Public Pump / PONS metadata + cached socials. No Token. No extra GMGN."""
    from . import github, pumpfun, twitter

    chain = normalize_chain(chain)
    mint = normalize_mint(str(row.get("mint") or row.get("address") or ""), chain)
    coin: dict[str, Any] = {}
    if chain == "sol":
        try:
            coin = await pumpfun.get_coin(mint) or {}
        except Exception:
            coin = {}
    elif chain == "robinhood" and (
        str(row.get("launchpad") or "").lower() == "pons" or row.get("pons") or row.get("factory")
    ):
        from . import pons

        if pons.pons_available():
            try:
                meta = await pons.token_metadata(mint)
            except Exception:
                meta = {}
            if isinstance(meta, dict) and meta:
                coin.update({k: meta.get(k) for k in ("name", "symbol", "twitter", "website", "telegram", "description", "image_url", "pool_address") if meta.get(k)})
                coin["launchpad"] = "pons"
    for key in ("name", "symbol", "twitter", "website", "telegram", "creator", "description", "image_url", "launchpad"):
        if row.get(key) and not coin.get(key):
            coin[key] = row[key]
    coin["mint"] = mint
    coin["chain"] = chain
    handle = extract_twitter_handle(str(coin.get("twitter") or row.get("twitter") or ""))
    tw: dict[str, Any] = {}
    if handle:
        try:
            tw = await twitter.lookup_handle(handle) or {}
        except Exception:
            tw = {}
    blob = " ".join(str(coin.get(k) or row.get(k) or "") for k in ("description", "website", "name"))
    gh_url, gh_ref = extract_github(blob)
    gh: dict[str, Any] = {}
    if gh_ref:
        try:
            gh = await github.lookup_repo(gh_ref) or {}
        except Exception:
            gh = {}
        if gh.get("url"):
            gh_url = str(gh["url"])
    try:
        progress = float(row.get("progress") or 0.0)
    except (TypeError, ValueError):
        progress = 0.0
    return {
        "chain": chain,
        "mint": mint,
        "symbol": coin.get("symbol") or row.get("symbol") or "",
        "progress": progress,
        "coin": _json_safe(coin),
        "twitter": _json_safe(tw),
        "twitter_handle": handle,
        "github": _json_safe(gh),
        "github_url": gh_url,
        "at": utcnow().isoformat(),
        "source": "watch_near_grad",
    }


async def prewarm_watch_cycle(chain: str, *, limit: int = MAX_PER_CYCLE) -> int:
    """Prefetch up to `limit` eligible watch books. Never inserts Token."""
    from ..db import ingest_lock, session_scope

    chain = normalize_chain(chain)
    if chain == "robinhood":
        from ..ingest.pons_poll import refresh_climbing_progress
        from ..ingest.rh_poll import prewarm_source_rows

        try:
            await refresh_climbing_progress(limit=4)
        except Exception:
            log.debug("pons climb refresh skipped", exc_info=True)
        rows = prewarm_source_rows()
    else:
        from ..ingest.pump_poll import watch_preview

        rows = watch_preview()
    picks: list[dict[str, Any]] = []
    async with ingest_lock:
        with session_scope() as session:
            mints = [str(r.get("mint") or "") for r in rows if r.get("mint")]
            known = (
                {m for (m,) in session.query(Token.mint).filter(Token.mint.in_(mints)).all()}
                if mints
                else set()
            )
            for row in rows:
                if not is_prewarm_candidate(row, chain):
                    continue
                mint = normalize_mint(str(row.get("mint") or ""), chain)
                if not mint or mint in known:
                    continue
                if load_prewarm(session, mint, chain):
                    continue
                picks.append(row)
                if len(picks) >= max(1, int(limit)):
                    break
    wrote = 0
    for row in picks:
        try:
            payload = await build_prewarm_payload(row, chain)
        except Exception:
            log.exception("prewarm fetch failed")
            continue
        mint = str(payload.get("mint") or "")
        async with ingest_lock:
            with session_scope() as session:
                if session.query(Token.mint).filter(Token.mint == mint).first():
                    continue
                if load_prewarm(session, mint, chain):
                    continue
                save_prewarm(session, payload)
                wrote += 1
                log.info("prewarmed %s %s p=%.2f", payload.get("symbol"), mint, float(payload.get("progress") or 0.0))
    return wrote
