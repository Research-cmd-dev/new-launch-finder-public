"""Periodic X scan for Bloom / Doing well — live score adder only.

Does not write entry p_good. Does not add FEATURE_NAMES. No extra GMGN.
Handle lookup is FxTwitter-first and free. This loop does **not**
call paid ``$TICKER`` tweet counts — those were the credit drain.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Outcome, Research, ScanState, Token, utcnow
from ..scoring.bloom import bloom_scam_book, leftover_fdv, list_blooms
from ..social import extract_twitter_handle
from .twitter import should_spend_x_credits

log = logging.getLogger("launchfinder.live_social")

KEY_PREFIX = "livesocial:"
TICK_KEY = "livesocial:tick"
# FxTwitter-only now (no paid $TICKER counts). Widen the chair so
# mid-size Bloom / Doing-well books actually get a follower/tweet
# snapshot. Brand / copycat chairs still skip.
MAX_PER_CYCLE = 8
MAX_DELTA = 0.08
DOING_WELL_HOURS = 72.0
DOING_WELL_LIQ = 3_000.0
# Mid-size real accounts only. Tiny handles are noise; 80k+ is
# usually a hijacked brand we already refuse to spend on.
LIVE_MENTION_MIN_FOLLOWERS = 200
LIVE_MENTION_MAX_FOLLOWERS = 80_000


def social_key(mint: str) -> str:
    return f"{KEY_PREFIX}{mint}"[:64]


def compute_social_delta(
    *,
    mentions_now: int = 0,
    mentions_prev: int | None = None,
    followers_now: int = 0,
    followers_prev: int | None = None,
    tweets_now: int = 0,
    tweets_prev: int | None = None,
    bio_now: str = "",
    bio_prev: str | None = None,
    symbol: str = "",
    name: str = "",
) -> tuple[float, list[str]]:
    """Change-only adder. First snapshot is a baseline (delta 0).

    Tweet-count / bio come from free FxTwitter. Mention rules stay
    for stored research baselines only — this loop does not fetch them.
    """
    from ..social import bio_hits_project

    reasons: list[str] = []
    delta = 0.0
    now_m = int(mentions_now or 0)
    now_f = int(followers_now or 0)
    now_t = int(tweets_now or 0)
    if mentions_prev is None and followers_prev is None and tweets_prev is None:
        return 0.0, ["X baseline stored — next scan scores the change"]
    prev_m = int(mentions_prev or 0)
    prev_f = int(followers_prev or 0)
    prev_t = int(tweets_prev or 0)
    if prev_m > 0 and now_m >= 8 and now_m >= 2 * prev_m:
        delta += 0.05
        reasons.append(f"X mentions doubled ({prev_m} → {now_m}/h)")
    elif prev_m >= 0 and now_m >= prev_m + 10 and now_m >= 15:
        delta += 0.04
        reasons.append(f"X mentions accelerating ({prev_m} → {now_m}/h)")
    elif prev_m >= 12 and now_m <= int(0.4 * prev_m):
        delta -= 0.05
        reasons.append(f"X mentions faded ({prev_m} → {now_m}/h)")
    if prev_t >= 15 and now_t >= prev_t + 10:
        delta += 0.04
        reasons.append(f"Dev X tweets up ({prev_t:,} → {now_t:,})")
    elif prev_t >= 40 and now_t >= int(prev_t * 1.08) and now_t >= prev_t + 5:
        delta += 0.03
        reasons.append(f"Dev X still posting ({prev_t:,} → {now_t:,})")
    if bio_prev is not None and bio_hits_project(bio_now, symbol, name) and not bio_hits_project(
        bio_prev, symbol, name
    ):
        delta += 0.02
        reasons.append("Dev X bio now matches the ticker")
    if prev_f >= 80 and now_f >= int(prev_f * 1.05) and now_f >= prev_f + 40:
        delta += 0.03
        reasons.append(f"Dev X followers up ({prev_f:,} → {now_f:,})")
    elif prev_f >= 200 and now_f > 0 and now_f <= int(prev_f * 0.97) and now_f <= prev_f - 30:
        delta -= 0.02
        reasons.append(f"Dev X followers slipped ({prev_f:,} → {now_f:,})")
    delta = max(-MAX_DELTA, min(MAX_DELTA, delta))
    return round(delta, 4), reasons


def live_social_delta(session: Session, mint: str) -> tuple[float, list[str]]:
    row = session.query(ScanState).filter(ScanState.key == social_key(mint)).one_or_none()
    if row is None:
        return 0.0, []
    try:
        data = json.loads(row.value or "{}")
    except json.JSONDecodeError:
        return 0.0, []
    if not isinstance(data, dict):
        return 0.0, []
    try:
        delta = float(data.get("delta") or 0.0)
    except (TypeError, ValueError):
        delta = 0.0
    reasons = [str(r) for r in (data.get("reasons") or []) if r]
    return max(-MAX_DELTA, min(MAX_DELTA, delta)), reasons


def fold_live_social(session: Session, mint: str, p: float) -> tuple[float, list[str]]:
    """Add stored X drift to a live promise. Never revives a hard-zero."""
    if p <= 0:
        return p, []
    delta, reasons = live_social_delta(session, mint)
    if not delta:
        return p, reasons
    return max(0.0, min(0.95, round(float(p) + delta, 4))), reasons


def _due(session: Session) -> bool:
    row = session.query(ScanState).filter(ScanState.key == TICK_KEY).one_or_none()
    if row is None or row.updated_at is None:
        return True
    stamp = row.updated_at
    if stamp.tzinfo is None:
        from datetime import timezone

        stamp = stamp.replace(tzinfo=timezone.utc)
    wait = float(settings.live_social_seconds or 720.0)
    return (utcnow() - stamp).total_seconds() >= wait


def _mark_tick(session: Session) -> None:
    row = session.query(ScanState).filter(ScanState.key == TICK_KEY).one_or_none()
    if row is None:
        row = ScanState(key=TICK_KEY, value="{}")
        session.add(row)
    row.updated_at = utcnow()


def _candidate_mints(session: Session) -> list[str]:
    seen: list[str] = []
    have: set[str] = set()
    for chain in ("sol", "robinhood"):
        for row in list_blooms(session, chain, limit=40):
            mint = str(row.get("mint") or "")
            if mint and mint not in have:
                have.add(mint)
                seen.append(mint)
        cutoff = utcnow() - timedelta(hours=DOING_WELL_HOURS)
        for (mint,) in (
            session.query(Token.mint)
            .join(Outcome, Outcome.token_id == Token.id)
            .filter(
                Token.chain == chain,
                Token.is_historical.is_(False),
                Token.source != "backfill",
                Token.first_seen_at >= cutoff,
                Outcome.t0_mcap > 0,
                Outcome.last_mcap >= 2.0 * Outcome.t0_mcap,
                Outcome.last_liq >= DOING_WELL_LIQ,
                or_(Outcome.label.is_(None), Outcome.label == 1),
            )
            .order_by(Outcome.multiple.desc())
            .limit(40)
            .all()
        ):
            if mint and mint not in have:
                have.add(mint)
                seen.append(mint)
    return seen


def _research_flags(research: Research | None) -> list[str]:
    if research is None:
        return []
    try:
        loaded = json.loads(research.risk_flags_json or "[]")
    except json.JSONDecodeError:
        return []
    return [str(f) for f in loaded] if isinstance(loaded, list) else []


def worth_live_x_spend(
    handle: str,
    *,
    followers: float = 0,
    flags: list[str] | None = None,
) -> bool:
    """True only for a real mid-size token X, not a brand hijack.

    Gates the free FxTwitter follower refresh. Paid ``$TICKER``
    counts are not used on this loop.
    """
    h = str(handle or "").lstrip("@").strip()
    if not h:
        return False
    blob = " ".join(str(f).lower() for f in (flags or []))
    if "celebrity/brand" in blob or "copycat spam" in blob:
        return False
    n = float(followers or 0)
    if n < LIVE_MENTION_MIN_FOLLOWERS or n > LIVE_MENTION_MAX_FOLLOWERS:
        return False
    return should_spend_x_credits(h, followers=n, flags=flags)


def _skip_token(token: Token) -> bool:
    if token is None or token.is_historical or token.source == "backfill":
        return True
    if not (token.symbol or "").strip():
        return True
    research = token.research
    outcome = token.outcome
    if outcome is not None and outcome.label == 0:
        return True
    flags: list[str] = []
    if research is not None:
        try:
            flags = [str(f) for f in json.loads(research.risk_flags_json or "[]")]
        except json.JSONDecodeError:
            flags = []
    if bloom_scam_book(flags):
        return True
    t0 = float((outcome.t0_mcap if outcome else 0.0) or 0.0)
    last = float((outcome.last_mcap if outcome else 0.0) or 0.0)
    if leftover_fdv((token.chain or "sol").strip().lower() or "sol", t0, last):
        return True
    if t0 > 0 and last > 0 and last / t0 < 0.85:
        return True
    return False


def _stale(session: Session, mint: str) -> bool:
    row = session.query(ScanState).filter(ScanState.key == social_key(mint)).one_or_none()
    if row is None or row.updated_at is None:
        return True
    stamp = row.updated_at
    if stamp.tzinfo is None:
        from datetime import timezone

        stamp = stamp.replace(tzinfo=timezone.utc)
    wait = float(settings.live_social_seconds or 720.0)
    return (utcnow() - stamp).total_seconds() >= wait


def _load_prev(session: Session, mint: str) -> dict[str, Any]:
    row = session.query(ScanState).filter(ScanState.key == social_key(mint)).one_or_none()
    if row is None:
        return {}
    try:
        data = json.loads(row.value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


async def refresh_live_social(session: Session) -> int:
    """Rescan Bloom / Doing well X metrics. No-op until the interval elapses."""
    if not _due(session):
        return 0
    from . import twitter

    wrote = 0
    for mint in _candidate_mints(session):
        if wrote >= MAX_PER_CYCLE:
            break
        if not _stale(session, mint):
            continue
        token = session.query(Token).filter(Token.mint == mint).one_or_none()
        if token is None or _skip_token(token):
            continue
        symbol = (token.symbol or "").strip()
        research: Research | None = token.research
        handle = ""
        if research is not None:
            handle = extract_twitter_handle(research.twitter_handle or "")
        if not handle:
            handle = extract_twitter_handle(token.twitter or "")
        flags = _research_flags(research)
        followers = int((research.twitter_followers if research else 0) or 0)
        stored_followers = followers
        # Brand / copycat chairs never occupy a slot. FxTwitter
        # follower refresh is free; do not call mention_count.
        if not handle or not should_spend_x_credits(handle, followers=followers, flags=flags):
            continue
        profile: dict[str, Any] = {}
        if handle and stored_followers < 100_000:
            profile = await twitter.lookup_handle(handle)
            followers = int(profile.get("followers") or stored_followers or 0)
        if not worth_live_x_spend(handle, followers=followers, flags=flags):
            continue
        tweets = int(profile.get("tweets") or (research.twitter_tweets if research else 0) or 0)
        bio = str(profile.get("bio") or "")
        prev = _load_prev(session, mint)
        prev_f = prev.get("followers")
        first = "followers" not in prev and "tweets" not in prev
        delta, reasons = compute_social_delta(
            mentions_now=0,
            mentions_prev=None,
            followers_now=followers,
            followers_prev=None if first else int(prev_f or 0),
            tweets_now=tweets,
            tweets_prev=None if first else int(prev.get("tweets") or 0),
            bio_now=bio,
            bio_prev=None if first else str(prev.get("bio") or ""),
            symbol=symbol,
            name=token.name or "",
        )
        if research is not None:
            if followers:
                research.twitter_followers = followers
            if tweets:
                research.twitter_tweets = tweets
        payload = {
            "mint": mint,
            "symbol": symbol,
            "handle": handle,
            "mentions": 0,
            "followers": followers,
            "tweets": tweets,
            "bio": bio,
            "delta": delta,
            "reasons": reasons,
            "updated_at": utcnow().isoformat(),
        }
        row = session.query(ScanState).filter(ScanState.key == social_key(mint)).one_or_none()
        if row is None:
            row = ScanState(key=social_key(mint))
            session.add(row)
        row.value = json.dumps(payload)
        row.updated_at = utcnow()
        wrote += 1
        await asyncio.sleep(0)
    _mark_tick(session)
    if wrote:
        log.info("live social scanned %s bloom/doing-well books", wrote)
    return wrote
