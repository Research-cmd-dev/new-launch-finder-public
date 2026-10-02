"""Find a public developer when bloom says a migrate is getting interesting.

Sources we already trust (no extra GMGN HTTP):
- stored X handle / Dex / Pump user ``x_username``
- token website + description (one HTML GET)
- creator-wallet prior wins/rugs on ``research``
- FxTwitter profile (free). Paid X mention counts stay gated elsewhere.

Does not write FEATURE_NAMES. Does not rewrite entry ``p_good``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..httputil import client
from ..models import Research, Token
from ..social import (
    extract_github,
    extract_twitter_handle,
    is_official_brand_website,
    is_staged_social_pair,
)
from . import pumpfun, twitter

log = logging.getLogger("launchfinder.dev_dive")

_SKIP_HANDLES = {
    "home", "i", "intent", "share", "search", "hashtag", "pumpfun", "pump_fun",
    "ponshood", "robinhood", "dexscreener", "gmgnai",
}


def handles_from_text(*parts: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if not part:
            continue
        handle = extract_twitter_handle(part)
        key = handle.lower()
        if handle and key not in seen and key not in _SKIP_HANDLES and not twitter.is_official_brand_handle(handle):
            seen.add(key)
            found.append(handle)
        for raw in re.findall(r"(?:https?://)?(?:www\.)?(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})", part, re.I):
            if raw.lower() in seen or raw.lower() in _SKIP_HANDLES or twitter.is_official_brand_handle(raw):
                continue
            seen.add(raw.lower())
            found.append(raw)
        for raw in re.findall(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_]{1,15})", part):
            if raw.lower() in seen or raw.lower() in _SKIP_HANDLES or twitter.is_official_brand_handle(raw):
                continue
            seen.add(raw.lower())
            found.append(raw)
    return found


def _research_flags(research: Research) -> list[str]:
    try:
        loaded = json.loads(research.risk_flags_json or "[]")
    except json.JSONDecodeError:
        return []
    return [str(x) for x in loaded] if isinstance(loaded, list) else []


def _pump_creator_handle(research: Research) -> str:
    try:
        raw = json.loads(research.raw_json or "{}")
    except json.JSONDecodeError:
        raw = {}
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    return extract_twitter_handle(str(user.get("x_username") or user.get("twitter") or ""))


def should_dive_public_dev(token: Token, research: Research) -> bool:
    """True only when a stored Pump creator X is a personal developer.

    The token's listed X is the project account — not the person
    working on it. Website scrapes of NASA.gov were the Dev hunt
    that burned credits on official brand X. Copycat / celebrity
    flags never dive. FEATURE_NAMES stays 66.
    """
    flags = _research_flags(research)
    blob = " ".join(f.lower() for f in flags)
    if "celebrity/brand" in blob or "copycat spam" in blob:
        return False
    pump = _pump_creator_handle(research)
    if not pump:
        return False
    if twitter.is_official_brand_handle(pump) or twitter.is_claimed_brand_x(pump, flags=flags):
        return False
    token_url = str(token.twitter or "")
    website = str(token.website or "")
    if is_staged_social_pair(token_url, website):
        token_h = extract_twitter_handle(token_url)
        if token_h and pump.lower() == token_h.lower():
            return False
    return twitter.should_spend_x_credits(pump, flags=flags)


def handles_from_research(token: Token, research: Research) -> list[str]:
    raw: dict[str, Any] = {}
    try:
        raw = json.loads(research.raw_json or "{}")
    except json.JSONDecodeError:
        raw = {}
    gmgn = raw.get("gmgn") or {}
    user = raw.get("user") or {}
    tw = raw.get("twitter") or {}
    return handles_from_text(
        research.twitter_handle or "",
        token.twitter or "",
        token.website or "",
        token.description or "",
        token.github_url or "",
        str(gmgn.get("twitter_username") or ""),
        str(user.get("x_username") or ""),
        str(user.get("username") or ""),
        str(tw.get("handle") or ""),
        str(tw.get("bio") or ""),
    )


async def _website_blob(url: str) -> str:
    url = (url or "").strip()
    if not url.startswith("http"):
        return ""
    try:
        resp = await client().get(url, timeout=6.0)
        if resp.status_code >= 400:
            return ""
        return (resp.text or "")[:80_000]
    except Exception as exc:
        log.debug("dev website fetch failed %s: %s", url, exc)
        return ""


def score_public_dev(
    profile: dict[str, Any],
    *,
    symbol: str,
    name: str,
    website: str,
) -> dict[str, Any]:
    """Heuristic only — not a FEATURE_NAMES row."""
    handle = str(profile.get("handle") or "").lstrip("@")
    if not handle:
        return {"tier": "none", "delta": 0.0, "reasons": [], "line": ""}
    if profile.get("hijack"):
        return {
            "tier": "hijack",
            "delta": -0.12,
            "reasons": [f"@{handle} looks hijacked (never tweets this ticker)"],
            "line": f"Claimed @{handle} does not look like the real owner.",
        }
    age = float(profile.get("age_days") or 0.0)
    followers = int(profile.get("followers") or 0)
    verified = bool(profile.get("verified"))
    # Official @CocaCola / @GeminiApp / Nike. A 100k+ verified brand
    # is not a pump.fun deployer. Bio matching "Google Gemini" is the
    # product account, not this mint.
    if twitter.is_brand_x_account(followers, age, verified):
        return {
            "tier": "hijack",
            "delta": -0.12,
            "reasons": [f"@{handle} is a celebrity/brand account, not this launch"],
            "line": f"Claimed @{handle} is a famous brand account ({followers:,} followers, {age:.0f}d) — not the deployer.",
        }
    tweets = int(profile.get("tweets") or 0)
    bio = str(profile.get("bio") or "")
    blob = f"{bio} {profile.get('name') or ''} {website}".lower()
    sym = (symbol or "").strip().lower()
    named = [w for w in re.split(r"[^a-z0-9]+", (name or "").lower()) if len(w) >= 4]
    mentions_project = bool(sym and (sym in blob or f"${sym}" in blob))
    mentions_project = mentions_project or any(w in blob for w in named[:3])
    site_host = ""
    if "://" in website:
        try:
            site_host = website.split("://", 1)[1].split("/", 1)[0].lower()
        except IndexError:
            site_host = ""
    site_in_bio = bool(site_host and site_host in blob)

    if age < 7 and followers < 200 and not verified:
        return {
            "tier": "burner",
            "delta": -0.04,
            "reasons": [f"@{handle} is a {age:.0f}d burner"],
            "line": f"@{handle} is brand-new ({age:.0f}d, {followers:,} followers) — treat as a throwaway, not a public dev.",
        }

    reasons: list[str] = []
    delta = 0.0
    tier = "weak"
    if age >= 60 and tweets >= 20 and (followers >= 150 or verified):
        tier = "credible"
        delta += 0.05
        reasons.append(f"@{handle} looks like a real public account ({age:.0f}d, {followers:,} followers)")
    if age >= 365 and (followers >= 800 or verified):
        tier = "strong"
        delta += 0.03
        reasons.append(f"@{handle} is aged ({age:.0f}d)")
    if mentions_project:
        delta += 0.03
        reasons.append(f"@{handle} bio/name matches the ticker")
    if site_in_bio:
        delta += 0.02
        reasons.append("Dev bio links the project site")
    if verified and tier == "weak":
        tier = "credible"
        delta += 0.04
        reasons.append(f"@{handle} is verified")
    if tier == "weak" and handle:
        reasons.append(f"Public X @{handle} — too thin to treat as a proven dev")
        return {
            "tier": "weak",
            "delta": 0.0,
            "reasons": reasons,
            "line": f"Found @{handle} but the account is thin ({age:.0f}d, {followers:,} followers).",
        }
    line = (
        f"Public dev @{handle}: {age:.0f}d account, {followers:,} followers"
        + (", verified" if verified else "")
        + (", bio matches the project" if mentions_project else "")
        + "."
    )
    return {"tier": tier, "delta": round(min(0.10, delta), 4), "reasons": reasons, "line": line, "handle": handle}


def creator_wallet_line(research: Research) -> tuple[float, str]:
    wins = int(research.creator_prior_wins or 0)
    rugs = int(research.creator_prior_rugs or 0)
    launches = int(research.creator_prior_launches or 0)
    if wins <= 0 and rugs <= 0 and launches <= 0:
        return 0.0, ""
    if wins >= 1 and rugs <= wins:
        return 0.04, f"Creator wallet has {wins} prior win(s) / {rugs} rugs across {launches} launches."
    if rugs >= 2 and rugs > wins:
        return -0.05, f"Creator wallet looks serial ({rugs} rugs / {wins} wins / {launches} launches)."
    if launches >= 8 and wins == 0:
        return -0.03, f"Creator has {launches} prior launches and no stored wins."
    return 0.0, f"Creator wallet: {launches} prior, {wins} wins, {rugs} rugs."


async def dive_developer(token: Token, research: Research) -> dict[str, Any]:
    """Best-effort public-dev packet. Safe to call on every near-bloom tick once."""
    if not should_dive_public_dev(token, research):
        return {
            "handle": "",
            "tier": "skip",
            "delta": 0.0,
            "reasons": [],
            "line": "",
            "github": token.github_url or "",
        }
    handle = _pump_creator_handle(research)
    website = token.website or ""
    html = ""
    # Official brand sites (nasa.gov / gemini.google.com) are how
    # @NASA got into the dive. Do not scrape them for handles.
    if website and not is_official_brand_website(website):
        html = await _website_blob(website)
        if html and not token.github_url:
            gh_url, _ = extract_github(html)
            if gh_url:
                token.github_url = gh_url
    if not handle and token.creator and (token.chain or "sol") == "sol":
        try:
            user = await pumpfun.get_user(token.creator)
        except Exception:
            user = {}
        handle = extract_twitter_handle(
            str((user or {}).get("x_username") or (user or {}).get("twitter") or "")
        )
    profile: dict[str, Any] = {}
    flags = _research_flags(research)
    if handle and (
        twitter.is_official_brand_handle(handle) or twitter.is_claimed_brand_x(handle, flags=flags)
    ):
        handle = ""
    if handle:
        try:
            profile = await twitter.lookup_handle(handle)
        except Exception as exc:
            log.debug("dev X lookup failed @%s: %s", handle, exc)
            profile = {"handle": handle}
        # Identify the personal developer on the card. Do not write
        # this handle into twitter_handle — that field is the token's
        # main X. Do not write follower stats either; those can leak
        # into second-look features / paid mention spend. Entry
        # p_good and features_json stay frozen.

    scored = score_public_dev(
        profile or {"handle": handle},
        symbol=token.symbol or "",
        name=token.name or "",
        website=website,
    )
    wallet_delta, wallet_line = creator_wallet_line(research)
    reasons = list(scored.get("reasons") or [])
    if wallet_line:
        reasons.append(wallet_line)
    line = " ".join(part for part in (scored.get("line") or "", wallet_line) if part)
    return {
        "handle": scored.get("handle") or handle,
        "tier": scored.get("tier") or "none",
        "delta": round(float(scored.get("delta") or 0.0) + wallet_delta, 4),
        "reasons": reasons,
        "line": line,
        "github": token.github_url or "",
    }
