"""Pre-graduation watch preview. Not entry p_good. FEATURE_NAMES stays 66.

Watch has no Dex book. This is a social / creator / fill-speed prior,
capped below Hunt, stored at migrate as ``preview_p`` so the model can
learn a side weight (``_preview_p``) without growing the 66-vector.
No extra GMGN HTTP.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..models import Outcome, Token

# Extra features_json key. Not in FEATURE_NAMES. Model weight key matches.
PREVIEW_FEATURE = "preview_p"
PREVIEW_WEIGHT = "_preview_p"
PREVIEW_CAP = 0.60


def _clip(p: float) -> float:
    return max(0.05, min(PREVIEW_CAP, round(p, 4)))


def _progress(row: dict[str, Any]) -> float:
    try:
        prog = float(row.get("progress") or row.get("launchpad_progress") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if prog > 1.0:
        prog /= 100.0
    return max(0.0, min(1.0, prog))


def _age_min(first_seen: Any) -> float:
    if not first_seen:
        return 0.0
    raw = first_seen
    if isinstance(raw, datetime):
        ts = raw
    else:
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return 0.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return max(0.0, (now - ts).total_seconds() / 60.0)


def _name_quality(symbol: str, name: str) -> float:
    sym = (symbol or "").strip()
    if len(sym) <= 1:
        return 0.2
    if any(ch.isdigit() for ch in sym) and len(sym) > 8:
        return 0.25
    blob = f"{name or ''} {sym}".lower()
    if any(bad in blob for bad in ("test", "asdf", "xxx", "untitled")):
        return 0.2
    return 0.7


def creator_preview_books(session: Session, creators: set[str]) -> dict[str, dict[str, int]]:
    """Labeled prior books for watch creators. Stored Token rows only."""
    out: dict[str, dict[str, int]] = {}
    addrs = [c for c in creators if c]
    if not addrs:
        return out
    rows = (
        session.query(Token.creator, Outcome.label)
        .join(Outcome, Outcome.token_id == Token.id)
        .filter(Token.creator.in_(addrs), Outcome.label.is_not(None))
        .all()
    )
    for creator, label in rows:
        key = str(creator or "")
        slot = out.setdefault(key, {"launches": 0, "wins": 0, "rugs": 0})
        slot["launches"] += 1
        if label == 1:
            slot["wins"] += 1
        elif label == 0:
            slot["rugs"] += 1
    return out


def preview_score(
    *,
    symbol: str = "",
    name: str = "",
    twitter: str = "",
    website: str = "",
    telegram: str = "",
    github: str = "",
    twitter_followers: int = 0,
    twitter_age_days: float = 0.0,
    twitter_verified: bool = False,
    github_stars: int = 0,
    progress: float = 0.0,
    age_min: float = 0.0,
    creator_launches: int = 0,
    creator_wins: int = 0,
    creator_rugs: int = 0,
    reply_count: int = 0,
    prewarmed: bool = False,
) -> tuple[float, list[str], list[str]]:
    """Watch-time prior in [0.05, 0.60]. Does not touch FEATURE_NAMES."""
    reasons: list[str] = []
    flags: list[str] = []
    p = 0.22
    if twitter:
        p += 0.06
        reasons.append("Has an X account")
    if twitter_followers >= 200:
        p += 0.06
        reasons.append("X already has followership")
    elif twitter_followers >= 40:
        p += 0.03
    if twitter_age_days >= 90:
        p += 0.05
        reasons.append("X account is not brand-new")
    elif twitter and twitter_age_days > 0 and twitter_age_days < 2:
        p -= 0.06
        flags.append("X account created very recently")
    if twitter_verified:
        p += 0.03
        reasons.append("X is verified")
    if website:
        p += 0.04
        reasons.append("Has a website")
    if telegram:
        p += 0.02
    if github:
        p += 0.05
        reasons.append("Public GitHub linked")
    if github_stars >= 5:
        p += 0.03
    quality = _name_quality(symbol, name)
    if quality < 0.3:
        p -= 0.06
        flags.append("Name looks throwaway")
    elif quality >= 0.7:
        p += 0.02
    if creator_wins >= 1:
        p += 0.08
        reasons.append("Creator has a prior labeled win")
    if creator_rugs >= 2:
        p -= 0.10
        flags.append("Creator has prior rugs")
    elif creator_rugs == 1:
        p -= 0.05
        flags.append("Creator has a prior rug")
    if creator_launches >= 8 and creator_wins == 0:
        p -= 0.06
        flags.append("Serial deployer with no wins")
    if progress >= 0.80 and 0 < age_min < 2:
        p -= 0.10
        flags.append("Curve filled almost instantly")
    elif progress >= 0.80 and age_min >= 20:
        p += 0.04
        reasons.append("Curve filled over time")
    if reply_count >= 20:
        p += 0.03
        reasons.append("Active launch thread")
    if progress >= 0.80:
        p += 0.03
        reasons.append(f"Curve {progress:.0%} — near graduate")
    if prewarmed:
        p += 0.02
        reasons.append("Research card is already warming")
    return _clip(p), reasons[:8], flags[:6]


def merge_preview_feature(features: dict[str, float], preview_p: float) -> dict[str, float]:
    """Stash preview on the frozen vector. FEATURE_NAMES is not appended."""
    out = dict(features)
    p = float(preview_p or 0.0)
    if p > 0:
        out[PREVIEW_FEATURE] = max(0.0, min(PREVIEW_CAP, p))
    return out


def attach_watch_scores(
    session: Session,
    rows: list[dict[str, Any]],
    chain: str,
    prewarms: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Score a watch tape. Does not create Token rows."""
    creators = {str(r.get("creator") or "") for r in rows if r.get("creator")}
    books = creator_preview_books(session, creators)
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        mint = str(item.get("mint") or item.get("address") or "")
        warm = prewarms.get(mint) or {}
        coin = warm.get("coin") if isinstance(warm.get("coin"), dict) else {}
        tw = warm.get("twitter") if isinstance(warm.get("twitter"), dict) else {}
        gh = warm.get("github") if isinstance(warm.get("github"), dict) else {}
        creator = str(item.get("creator") or coin.get("creator") or "")
        stats = books.get(creator) or {}
        p, reasons, flags = preview_score(
            symbol=str(item.get("symbol") or coin.get("symbol") or ""),
            name=str(item.get("name") or coin.get("name") or ""),
            twitter=str(item.get("twitter") or coin.get("twitter") or warm.get("twitter_handle") or ""),
            website=str(item.get("website") or coin.get("website") or ""),
            telegram=str(item.get("telegram") or coin.get("telegram") or ""),
            github=str(warm.get("github_url") or coin.get("github") or ""),
            twitter_followers=int(tw.get("followers") or 0),
            twitter_age_days=float(tw.get("age_days") or 0.0),
            twitter_verified=bool(tw.get("verified")),
            github_stars=int(gh.get("stars") or 0),
            progress=_progress(item) or float(warm.get("progress") or 0.0),
            age_min=_age_min(item.get("first_seen") or item.get("first_seen_at") or item.get("created_at")),
            creator_launches=int(stats.get("launches") or 0),
            creator_wins=int(stats.get("wins") or 0),
            creator_rugs=int(stats.get("rugs") or 0),
            reply_count=int(item.get("reply_count") or coin.get("reply_count") or 0),
            prewarmed=bool(warm),
        )
        item["preview_p"] = p
        item["preview_reasons"] = reasons
        item["preview_flags"] = flags
        item["score"] = round(p * 100.0, 1)
        item["prewarmed"] = bool(warm)
        item["chain"] = chain
        out.append(item)
    return out
