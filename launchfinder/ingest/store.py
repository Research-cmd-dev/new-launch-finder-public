from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..alerts import notify_high_score
from ..chains import normalize_chain, normalize_mint, token_chain
from ..models import Token, utcnow
from ..research.pipeline import research_token

log = logging.getLogger("launchfinder.ingest")


def _clip(value: Any, n: int) -> str:
    # Live 17:40: KURO ingested as "KURO " and would twin a clean ticker.
    return str(value or "").strip()[:n]


def upsert_migration(
    session: Session,
    *,
    mint: str,
    source: str,
    coin: dict[str, Any] | None = None,
    signature: str = "",
    pool_address: str = "",
    migrated_at: datetime | None = None,
    historical: bool = False,
) -> Token | None:
    if not mint:
        return None
    coin = coin or {}
    chain = normalize_chain(coin.get("chain") or "sol")
    mint = normalize_mint(mint, chain)
    token = session.query(Token).filter(Token.mint == mint).one_or_none()
    created = False
    if token is None:
        token = Token(mint=mint, chain=chain, first_seen_at=utcnow(), source=source, is_historical=historical)
        session.add(token)
        created = True
    elif not token.chain:
        token.chain = chain
    token.name = _clip(coin.get("name") or token.name, 128)
    token.symbol = _clip(coin.get("symbol") or token.symbol, 32)
    token.creator = _clip(coin.get("creator") or token.creator, 64)
    token.description = coin.get("description") or token.description
    token.image_url = coin.get("image_url") or token.image_url
    token.twitter = coin.get("twitter") or token.twitter
    token.website = coin.get("website") or token.website
    token.telegram = coin.get("telegram") or token.telegram
    token.created_at_chain = coin.get("created_at") or token.created_at_chain
    token.nsfw = bool(coin.get("nsfw") or token.nsfw)
    token.banned = bool(coin.get("banned") or token.banned)
    token.reply_count = int(coin.get("reply_count") or token.reply_count or 0)
    token.pool_address = _clip(pool_address or coin.get("pool_address") or token.pool_address, 128)
    if signature:
        token.signature = _clip(signature, 128)
    if token.migrated_at is None:
        token.migrated_at = migrated_at or coin.get("updated_at") or utcnow()
    session.flush()
    return token if created or token.research is None else token


async def ingest_and_research(
    session: Session,
    *,
    mint: str,
    source: str,
    coin: dict[str, Any] | None = None,
    signature: str = "",
    pool_address: str = "",
    migrated_at: datetime | None = None,
    historical: bool = False,
) -> Token | None:
    token = upsert_migration(
        session,
        mint=mint,
        source=source,
        coin=coin,
        signature=signature,
        pool_address=pool_address,
        migrated_at=migrated_at,
        historical=historical,
    )
    if token is None:
        return None
    if token.research is not None and not historical:
        return token
    from ..research.prewarm import load_prewarm, mark_prewarm_used, merge_prewarm_into_coin

    chain = normalize_chain((coin or {}).get("chain") or token.chain or "sol")
    pre = load_prewarm(session, mint, chain)
    if pre:
        coin = merge_prewarm_into_coin(coin or {}, pre)
    try:
        research = await research_token(session, token, coin=coin)
    except Exception:
        log.exception("research failed for %s", mint)
        return token
    if pre and token.research is not None:
        mark_prewarm_used(session, mint, chain)
    if not historical and not token.is_historical:
        try:
            from ..scoring.hunt import upsert_hunt

            upsert_hunt(session, token)
        except Exception:
            log.exception("hunt upsert failed for %s", mint)
        try:
            latest = token.snapshots[-1] if token.snapshots else None
            # Entry-time pings can't be conviction-checked yet (no trajectory),
            # so they demand an exceptional score; lift / runner / bloom
            # are the later channels. Durable ScanState so a restart
            # does not re-spam.
            from ..desk_lines import SCORER_FIRST_SIGHT, lines_for_scorer

            lines = lines_for_scorer(research.scorer, token_chain(token))
            # Legacy: 0.80, above its 0.70 line. First-sight: its high
            # (even-odds) line — the scale tops out near 0.67.
            ping_floor = lines.hi if lines.scorer == SCORER_FIRST_SIGHT else 0.8
            if research.p_good < ping_floor:
                return token
            from ..scoring.alert_tiers import claim_ping

            if claim_ping(session, "new", token.mint):
                await notify_high_score(
                    symbol=token.symbol,
                    name=token.name,
                    mint=token.mint,
                    p_good=research.p_good,
                    mcap_usd=latest.mcap_usd if latest else 0.0,
                    flags=json.loads(research.risk_flags_json or "[]"),
                    reasons=json.loads(research.reasons_json or "[]"),
                    chain=token.chain or "sol",
                    min_p=ping_floor,
                )
        except Exception:
            log.exception("alert dispatch failed for %s", mint)
    return token
