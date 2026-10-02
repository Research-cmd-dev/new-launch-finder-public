"""Social velocity — how fast X mentions / shill posts ramp vs organic.

FxTwitter-safe only. Never enables X_PAID_ENABLED. Mentions come from
stored Research baselines (paid path off); follower/tweet ramps from
free FxTwitter snapshots in ScanState / Research. Learn/shadow only.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..models import Research, ScanState, Token
from .live_social import social_key

# Heuristic bands — descriptive, not gates.
SHILL_TWEET_BURST = 25  # tweet delta with thin followers
ORGANIC_FOLLOWER_FLOOR = 80
ORGANIC_FOLLOWER_GROWTH = 1.05


def _aware(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _load_social_scan(session: Session, mint: str) -> dict[str, Any]:
    row = session.query(ScanState).filter(ScanState.key == social_key(mint)).one_or_none()
    if row is None:
        return {}
    try:
        data = json.loads(row.value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def classify_social_ramp(
    *,
    followers_now: int,
    followers_prev: int | None,
    tweets_now: int,
    tweets_prev: int | None,
    mentions_now: int = 0,
    mentions_prev: int | None = None,
    age_min: float | None = None,
) -> dict[str, Any]:
    """Label ramp shape without paid mention fetches."""
    reasons: list[str] = []
    f_now = int(followers_now or 0)
    t_now = int(tweets_now or 0)
    m_now = int(mentions_now or 0)
    f_prev = int(followers_prev) if followers_prev is not None else None
    t_prev = int(tweets_prev) if tweets_prev is not None else None
    m_prev = int(mentions_prev) if mentions_prev is not None else None

    label = "quiet"
    velocity = 0.0  # [-1, 1] descriptive

    if f_prev is None and t_prev is None and m_prev is None:
        return {
            "label": "baseline",
            "velocity": 0.0,
            "organic_score": 0.0,
            "shill_score": 0.0,
            "reasons": ["no prior social snapshot"],
            "paper_only": True,
        }

    tweet_delta = (t_now - t_prev) if t_prev is not None else 0
    follower_delta = (f_now - f_prev) if f_prev is not None else 0
    mention_delta = (m_now - m_prev) if m_prev is not None else 0

    organic = 0.0
    shill = 0.0

    # Organic: followers grow with modest tweet pace.
    if (
        f_prev is not None
        and f_prev >= ORGANIC_FOLLOWER_FLOOR
        and f_now >= int(f_prev * ORGANIC_FOLLOWER_GROWTH)
        and follower_delta >= 40
    ):
        organic += 0.5
        reasons.append(f"followers ramp {f_prev}→{f_now}")
        label = "organic_ramp"
        velocity += 0.35

    if t_prev is not None and t_prev >= 15 and tweet_delta >= 10 and f_now >= ORGANIC_FOLLOWER_FLOOR:
        organic += 0.25
        reasons.append(f"dev posting {t_prev}→{t_now}")
        if label == "quiet":
            label = "organic_ramp"
        velocity += 0.2

    # Shill burst: large tweet/mention spike with thin follower base.
    if tweet_delta >= SHILL_TWEET_BURST and f_now < ORGANIC_FOLLOWER_FLOOR:
        shill += 0.6
        reasons.append(f"tweet burst +{tweet_delta} on thin followers ({f_now})")
        label = "shill_burst"
        velocity -= 0.4

    if m_prev is not None and m_now >= 15 and m_now >= max(2 * max(m_prev, 1), m_prev + 10):
        # Stored mentions only — we do not fetch paid $TICKER counts here.
        if f_now < ORGANIC_FOLLOWER_FLOOR:
            shill += 0.4
            reasons.append(f"mention spike {m_prev}→{m_now}/h thin book")
            label = "shill_burst"
            velocity -= 0.25
        else:
            organic += 0.2
            reasons.append(f"mention accel {m_prev}→{m_now}/h with followers")
            if label == "quiet":
                label = "organic_ramp"
            velocity += 0.15

    if age_min is not None and age_min < 30 and shill >= 0.5:
        reasons.append("early shill shape (<30m)")

    if organic >= 0.5 and shill < 0.3:
        label = "organic_ramp"
    elif shill >= 0.5:
        label = "shill_burst"
    elif organic == 0 and shill == 0 and (tweet_delta or follower_delta or mention_delta):
        label = "flat_noise"

    return {
        "label": label,
        "velocity": round(max(-1.0, min(1.0, velocity)), 3),
        "organic_score": round(min(1.0, organic), 3),
        "shill_score": round(min(1.0, shill), 3),
        "tweet_delta": tweet_delta,
        "follower_delta": follower_delta,
        "mention_delta": mention_delta,
        "reasons": reasons,
        "paper_only": True,
    }


def social_velocity(
    session: Session,
    token: Token,
    research: Research | None = None,
) -> dict[str, Any]:
    """Side-key social velocity bundle for Learn/shadow ranking."""
    if research is None:
        research = (
            session.query(Research).filter(Research.token_id == token.id).one_or_none()
        )
    scan = _load_social_scan(session, token.mint)
    f_now = int(
        (research.twitter_followers if research else 0)
        or scan.get("followers")
        or 0
    )
    t_now = int((research.twitter_tweets if research else 0) or scan.get("tweets") or 0)
    m_now = int((research.x_mentions_1h if research else 0) or scan.get("mentions") or 0)
    prev = scan.get("prev") if isinstance(scan.get("prev"), dict) else {}
    # Prefer explicit prev; else treat current scan as only baseline.
    f_prev = prev.get("followers") if prev else scan.get("followers_prev")
    t_prev = prev.get("tweets") if prev else scan.get("tweets_prev")
    m_prev = prev.get("mentions") if prev else scan.get("mentions_prev")
    if f_prev is None and "followers" in scan and scan.get("delta") is not None:
        # Reconstruct rough prev from stored now - not perfect; ok for shadow.
        try:
            f_prev = int(scan.get("followers_prev")) if scan.get("followers_prev") is not None else None
        except (TypeError, ValueError):
            f_prev = None

    age_min = None
    first = _aware(getattr(token, "first_seen_at", None) or getattr(token, "created_at", None))
    if first:
        age_min = (datetime.now(timezone.utc) - first).total_seconds() / 60.0

    shape = classify_social_ramp(
        followers_now=f_now,
        followers_prev=int(f_prev) if f_prev is not None else None,
        tweets_now=t_now,
        tweets_prev=int(t_prev) if t_prev is not None else None,
        mentions_now=m_now,
        mentions_prev=int(m_prev) if m_prev is not None else None,
        age_min=age_min,
    )
    # SI study: both printers were meme-only / weak social — social is weak separator.
    return {
        **shape,
        "followers": f_now,
        "tweets": t_now,
        "mentions_1h": m_now,
        "handle": (research.twitter_handle if research else "") or "",
        "age_min": round(age_min, 1) if age_min is not None else None,
        "fx_twitter_only": True,
        "x_paid_enabled": False,
        "side_key": "social_velocity",
        "study_note": "SI printers had weak/empty X — social is weak separator; surface only",
        "paper_only": True,
    }
