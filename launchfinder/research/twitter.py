from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from ..config import FXTWITTER_API, env_int, env_str, settings
from ..httputil import client, get_json
from ..social import age_days, parse_joined

log = logging.getLogger("launchfinder.twitter")

# The official X API has tight per-window limits on lower tiers. Space the
# calls out, and when a 429 arrives back off until the reported reset so
# research degrades to FxTwitter instead of hammering a dead window.
_min_gap = 1.2
_last_call = 0.0
_cooldown_until = 0.0
# Paid path is env-gated. Default off — FxTwitter stays free for profiles.
# Set X_PAID_ENABLED=1 and TWITTER_BEARER_TOKEN to spend. Monthly call budget
# (X_MONTHLY_CALL_BUDGET, default 2000 ≈ cautious ~$50–75/mo depending on
# endpoint mix) is tracked in ScanState when a DB session is available.
_X_PAID_ENV = env_str("X_PAID_ENABLED", "0").lower() in {"1", "true", "on", "yes"}
X_PAID_ENABLED = _X_PAID_ENV
X_MONTHLY_CALL_BUDGET = max(0, env_int("X_MONTHLY_CALL_BUDGET", "2000"))
_BUDGET_KEY_PREFIX = "x_paid_calls:"
# Process-local fallback when no session is wired into _x_get.
_local_month_calls = 0
_local_month_key = ""


def _month_key(now: datetime | None = None) -> str:
    stamp = now or datetime.now(timezone.utc)
    return stamp.strftime("%Y-%m")


def paid_x_enabled() -> bool:
    """Runtime read so tests can monkeypatch env / module flag."""
    if not X_PAID_ENABLED:
        return False
    return bool(settings.twitter_bearer)


def x_calls_this_month() -> int:
    return int(_local_month_calls) if _local_month_key == _month_key() else 0


def x_budget_remaining() -> int:
    if X_MONTHLY_CALL_BUDGET <= 0:
        return 0
    return max(0, X_MONTHLY_CALL_BUDGET - x_calls_this_month())


def x_api_available() -> bool:
    if not paid_x_enabled():
        return False
    if time.monotonic() < _cooldown_until:
        return False
    if X_MONTHLY_CALL_BUDGET > 0 and x_calls_this_month() >= X_MONTHLY_CALL_BUDGET:
        return False
    return True


def _record_paid_call() -> None:
    global _local_month_calls, _local_month_key
    key = _month_key()
    if _local_month_key != key:
        _local_month_key = key
        _local_month_calls = 0
    _local_month_calls += 1
    # Best-effort persist for multi-replica honesty.
    try:
        from ..db import session_scope
        from ..models import ScanState, utcnow

        sk = f"{_BUDGET_KEY_PREFIX}{key}"
        with session_scope() as session:
            row = session.query(ScanState).filter(ScanState.key == sk).first()
            now = utcnow()
            if row is None:
                session.add(ScanState(key=sk, value="1", updated_at=now))
            else:
                try:
                    n = int(row.value or "0") + 1
                except ValueError:
                    n = 1
                row.value = str(n)
                row.updated_at = now
                _local_month_calls = max(_local_month_calls, n)
    except Exception:
        log.debug("x paid call counter persist skipped", exc_info=True)


def sync_paid_call_counter_from_db() -> int:
    """Load monthly counter from ScanState into process memory."""
    global _local_month_calls, _local_month_key
    key = _month_key()
    _local_month_key = key
    try:
        from ..db import session_scope
        from ..models import ScanState

        sk = f"{_BUDGET_KEY_PREFIX}{key}"
        with session_scope() as session:
            row = session.query(ScanState).filter(ScanState.key == sk).first()
            _local_month_calls = int(row.value or "0") if row else 0
    except Exception:
        _local_month_calls = 0
    return _local_month_calls


# Official org / celebrity handles that are never a pump deployer.
# Live NASA coin pasted https://x.com/NASA (92M). Stats-based
# is_brand_x_account still covers unknown 100k+ verified brands.
_OFFICIAL_BRAND_HANDLES = frozenset(
    {
        "nasa",
        "nasa_jpl",
        "nasaearth",
        "elonmusk",
        "spacex",
        "tesla",
        "google",
        "openai",
        "anthropicai",
        "claudeai",
        "apple",
        "microsoft",
        "meta",
        "facebook",
        "instagram",
        "nvidia",
        "samsung",
        "nintendo",
        "nintendoamerica",
        "pokemon",
        "nike",
        "adidas",
        "cocacola",
        "rockstargames",
        "rollsroycecars",
        "grok",
        "whitehouse",
        "potus",
        "amazon",
        "netflix",
        "disney",
        "mcdonalds",
        "starbucks",
        "pumpfun",
    }
)


def is_official_brand_handle(handle: str | None) -> bool:
    """True for well-known official accounts (NASA, Google, …)."""
    h = str(handle or "").lstrip("@").lower()
    return bool(h) and h in _OFFICIAL_BRAND_HANDLES


# Live Metapad: @metapadspace stored 3565d (~9.8y), Dex Pair 17y,
# 188 followers, verified, Entry 92. An 8y+ project X on a new
# launch is the bought-aged-handle tell. 90d–2y "not brand-new"
# stays a plus. Official brand X (NASA) is a different veto.
BOUGHT_AGED_TOKEN_X_DAYS = 2920.0


def is_bought_aged_token_x(
    handle: str | None,
    age_days: float,
    *,
    claimed_brand: bool = False,
) -> bool:
    """Project X only — never Dev X. Empty handle = no token X."""
    if claimed_brand or is_official_brand_handle(handle):
        return False
    h = str(handle or "").lstrip("@").strip()
    if not h:
        return False
    return float(age_days or 0) >= BOUGHT_AGED_TOKEN_X_DAYS


def is_brand_x_account(followers: float, age_days: float, verified: bool) -> bool:
    """Official product / celebrity X — not a pump.fun deployer.

    Live Google Gemini claimed @GeminiApp (573k, verified, 808d).
    The old gate required 2000d, so Gemini's own tweets of "Google"
    cleared the hijack check and graduation printed 98%. A verified
    100k+ account tweets its brand name all day; require the mint.
    """
    if float(followers or 0) < 100_000:
        return False
    return bool(verified) or float(age_days or 0) >= 2000


def is_claimed_brand_x(
    handle: str | None,
    *,
    followers: float = 0,
    age_days: float = 0,
    verified: bool = False,
    flags: list[str] | None = None,
) -> bool:
    """Pasted official X, not this mint's Dev account.

    Live NASA: profile URL + 92M followers + celebrity/brand flag,
    but associated_dev_handle still painted the tape green and
    linked Dev @NASA. FEATURE_NAMES stays 66.
    """
    h = str(handle or "").lstrip("@")
    if not h:
        return False
    if is_official_brand_handle(h):
        return True
    blob = " ".join(str(flag).lower() for flag in (flags or []))
    if "celebrity/brand" in blob:
        return True
    return is_brand_x_account(followers, age_days, verified)


async def _x_get(url: str, params: dict[str, Any]) -> Any:
    global _last_call, _cooldown_until
    if not x_api_available():
        return None
    wait = _min_gap - (time.monotonic() - _last_call)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_call = time.monotonic()
    try:
        resp = await client().get(
            url,
            headers={"Authorization": f"Bearer {settings.twitter_bearer}"},
            params=params,
        )
        if resp.status_code == 429:
            reset = float(resp.headers.get("x-rate-limit-reset") or 0)
            pause = max(60.0, reset - time.time()) if reset else 900.0
            _cooldown_until = time.monotonic() + min(pause, 900.0)
            log.warning("X API rate limited; falling back to FxTwitter for %.0fs", pause)
            return None
        resp.raise_for_status()
        _record_paid_call()
        return resp.json()
    except Exception as exc:
        log.debug("X API call failed: %s", exc)
        return None


# Handle-profile cache: copycat waves reuse the same accounts, and a 6h-old
# follower count is fine for scoring.
_handle_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_HANDLE_TTL = 6 * 3600.0


# Official user lookup is only worth a credit for celebrity-sized
# accounts (hijack check starts at 50k). Dead/fake handles that FxTwitter
# cannot resolve used to fall through to a paid 404 — that is the bulk of
# remaining spend. NASA-class brands never need a paid confirm.
_OFFICIAL_FOLLOWER_FLOOR = 100_000
PAID_MENTION_FOLLOWERS = 25_000


def should_spend_x_credits(
    handle: str | None,
    *,
    followers: float = 0,
    age_days: float = 0,
    verified: bool = False,
    flags: list[str] | None = None,
) -> bool:
    """Paid counts / official user lookup.

    Live social no longer pays for ``$TICKER`` counts. Official brand
    X and copycat flags never move the score enough to be worth a
    credit. FEATURE_NAMES stays 66.
    """
    if is_official_brand_handle(handle):
        return False
    if is_claimed_brand_x(
        handle,
        followers=followers,
        age_days=age_days,
        verified=verified,
        flags=flags,
    ):
        return False
    blob = " ".join(str(flag).lower() for flag in (flags or []))
    if "copycat spam" in blob or "celebrity/brand" in blob:
        return False
    return True


async def lookup_handle(handle: str) -> dict[str, Any]:
    """Free FxTwitter first. Paid official API only to confirm a large
    account FxTwitter already found — never as a fallback for missing
    handles. Official brand handles never spend a credit.
    """
    handle = (handle or "").lstrip("@").strip()
    if not handle:
        return {}
    cached = _handle_cache.get(handle.lower())
    if cached and time.monotonic() - cached[0] < _HANDLE_TTL:
        return dict(cached[1])
    fx = await _fxtwitter(handle)
    result = fx
    followers = float((fx or {}).get("followers") or 0)
    if (
        fx
        and x_api_available()
        and followers >= _OFFICIAL_FOLLOWER_FLOOR
        and should_spend_x_credits(
            handle,
            followers=followers,
            age_days=float(fx.get("age_days") or 0),
            verified=bool(fx.get("verified")),
        )
    ):
        official = await _official(handle)
        if official:
            result = official
    if result:
        _handle_cache[handle.lower()] = (time.monotonic(), dict(result))
        if len(_handle_cache) > 4000:
            _handle_cache.clear()
    elif is_official_brand_handle(handle):
        result = {"handle": handle, "source": "brand_skip", "followers": 0}
        _handle_cache[handle.lower()] = (time.monotonic(), dict(result))
    return result or {}


_mention_cache: dict[str, tuple[float, int]] = {}
_MENTION_TTL = 45 * 60.0


async def mention_count(query: str, *, ttl: float | None = None) -> int:
    if not query:
        return 0
    # $NASA / $Google counts are not worth a credit.
    symbol = str(query).lstrip("$").split()[0] if query else ""
    if is_official_brand_handle(symbol):
        return 0
    key = query.lower()
    hold = _MENTION_TTL if ttl is None else max(0.0, float(ttl))
    cached = _mention_cache.get(key)
    if hold > 0 and cached and time.monotonic() - cached[0] < hold:
        return cached[1]
    data = await _x_get(
        "https://api.twitter.com/2/tweets/counts/recent",
        {"query": query, "granularity": "hour"},
    )
    if not isinstance(data, dict):
        return 0
    buckets = data.get("data") or []
    if not buckets:
        n = int((data.get("meta") or {}).get("total_tweet_count") or 0)
    else:
        n = int((buckets[-1] or {}).get("tweet_count") or 0)
    _mention_cache[key] = (time.monotonic(), n)
    if len(_mention_cache) > 2000:
        _mention_cache.clear()
    return n


async def account_mentions(handle: str, symbol: str, mint: str = "") -> int | None:
    """Did this account tweet the ticker (or mint) in the last 7 days?

    None = unknown (API unavailable) — callers must only act on an
    authoritative answer. Brand accounts tweet their own name all day,
    so a mint pins the check to this launch, not Coca-Cola the soda.
    """
    handle = (handle or "").lstrip("@").strip()
    symbol = (symbol or "").strip()
    mint = (mint or "").strip()
    if not handle or not (symbol or mint):
        return None
    if not should_spend_x_credits(handle):
        return None
    if mint:
        query = f"from:{handle} ({mint} OR pump.fun)"
    else:
        query = f"from:{handle} ({symbol} OR ${symbol})"
    data = await _x_get(
        "https://api.twitter.com/2/tweets/counts/recent",
        {"query": query, "granularity": "day"},
    )
    if not isinstance(data, dict):
        return None
    total = (data.get("meta") or {}).get("total_tweet_count")
    if total is None:
        buckets = data.get("data") or []
        total = sum(int((b or {}).get("tweet_count") or 0) for b in buckets)
    return int(total)


async def _official(handle: str) -> dict[str, Any]:
    data = await _x_get(
        f"https://api.twitter.com/2/users/by/username/{handle}",
        {"user.fields": "created_at,description,public_metrics,verified,verified_type"},
    )
    user = (data or {}).get("data") if isinstance(data, dict) else None
    if not isinstance(user, dict):
        return {}
    metrics = user.get("public_metrics") or {}
    created = parse_joined(user.get("created_at"))
    return {
        "handle": user.get("username") or handle,
        "name": user.get("name") or "",
        "followers": int(metrics.get("followers_count") or 0),
        "following": int(metrics.get("following_count") or 0),
        "tweets": int(metrics.get("tweet_count") or 0),
        "verified": bool(user.get("verified")),
        "age_days": age_days(created),
        "bio": user.get("description") or "",
        "source": "twitter_api",
    }


async def _fxtwitter(handle: str) -> dict[str, Any]:
    data = await get_json(f"{FXTWITTER_API}/{handle}")
    user = (data or {}).get("user") if isinstance(data, dict) else None
    if not isinstance(user, dict):
        return {}
    created = parse_joined(user.get("joined"))
    verification = user.get("verification") or {}
    verified = bool(user.get("verified") or verification.get("verified") or verification.get("type"))
    return {
        "handle": user.get("screen_name") or handle,
        "name": user.get("name") or "",
        "followers": int(user.get("followers") or 0),
        "following": int(user.get("following") or 0),
        "tweets": int(user.get("tweets") or 0),
        "verified": verified,
        "age_days": age_days(created),
        "bio": user.get("description") or "",
        "source": "fxtwitter",
        "joined": created.isoformat() if created else "",
        "now": datetime.now(timezone.utc).isoformat(),
    }
