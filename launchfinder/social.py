from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

TWITTER_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})(?:/status/\d+)?",
    re.I,
)
GITHUB_REPO_RE = re.compile(r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", re.I)
GITHUB_USER_RE = re.compile(r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/?$", re.I)
HANDLE_RE = re.compile(r"^@?([A-Za-z0-9_]{1,15})$")
URL_RE = re.compile(r"https?://[^\s)>\"]+", re.I)


def bio_hits_project(bio: str | None, symbol: str = "", name: str = "") -> bool:
    """True when a profile bio mentions this ticker or a real name token.

    Free FxTwitter field. Not a FEATURE_NAMES row.
    """
    blob = str(bio or "").lower()
    if not blob:
        return False
    sym = str(symbol or "").strip().lower()
    if sym and len(sym) >= 2 and (sym in blob or f"${sym}" in blob):
        return True
    named = [w for w in re.split(r"[^a-z0-9]+", str(name or "").lower()) if len(w) >= 4]
    return any(w in blob for w in named[:3])


def extract_twitter_handle(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip()
    match = TWITTER_RE.search(value)
    if match:
        handle = match.group(1)
        if handle.lower() in {"home", "i", "intent", "share", "search", "hashtag"}:
            return ""
        return handle
    if "status/" in value.lower():
        return ""
    match = HANDLE_RE.match(value.lstrip("@"))
    if match and "http" not in value:
        return match.group(1)
    return ""


def extract_github(text: str | None) -> tuple[str, str]:
    """Return (url, owner/repo or owner)."""
    if not text:
        return "", ""
    repo = GITHUB_REPO_RE.search(text)
    if repo:
        owner, name = repo.group(1), repo.group(2)
        if name.lower() in {"issues", "pulls", "pulse", "actions"}:
            return f"https://github.com/{owner}", owner
        return f"https://github.com/{owner}/{name}", f"{owner}/{name}"
    user = GITHUB_USER_RE.search(text)
    if user:
        owner = user.group(1)
        if owner.lower() in {"features", "topics", "marketplace", "explore"}:
            return "", ""
        return f"https://github.com/{owner}", owner
    return "", ""


def extract_urls(text: str | None) -> list[str]:
    if not text:
        return []
    return URL_RE.findall(text)


def parse_joined(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def age_days(dt: datetime | None, now: datetime | None = None) -> float:
    if not dt:
        return 0.0
    now = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (now - dt).total_seconds() / 86400.0)


# Consumer / product sites that pump clones paste as "the website".
# Free hosts (sites.google.com, github.io) stay off this list.
_BRAND_WEBSITE_HOSTS = {
    "google.com",
    "gemini.google.com",
    "deepmind.google",
    "openai.com",
    "chatgpt.com",
    "anthropic.com",
    "coca-cola.com",
    "pokemon.com",
    "nintendo.com",
    "nike.com",
    "adidas.com",
    "apple.com",
    "microsoft.com",
    "meta.com",
    "facebook.com",
    "instagram.com",
    "amazon.com",
    "netflix.com",
    "disney.com",
    "tesla.com",
    "starbucks.com",
    "mcdonalds.com",
}


def is_twitter_status_url(url: str | None) -> bool:
    """True when the 'project X' is one tweet, not a profile."""
    raw = str(url or "").lower()
    if "status/" not in raw:
        return False
    return "x.com" in raw or "twitter.com" in raw


# Third-party "website" that is just a generated mint card.
# Live Diesel: otcdesks.cash/coin/<mint> + a YoungThugDevvor status.
_GENERATED_SITE_HOSTS = {
    "twine.auction",
    "otcdesks.cash",
}
_MINT_PAGE_RE = re.compile(
    r"/(?:coin|token|ca)/([1-9A-HJ-NP-Za-km-z]{32,44})(?:/|$)",
    re.I,
)
_PASSTHROUGH_SITE_HOSTS = {
    "pump.fun",
    "dexscreener.com",
    "gmgn.ai",
    "solscan.io",
    "birdeye.so",
}


def is_staged_launch_website(url: str | None) -> bool:
    """Generated mint page or a tweet pasted as the website.

    Live TWIN: https://twine.auction/a/twin-zwjg plus an AragornSol
    status URL. Live Diesel: otcdesks.cash/coin/<mint>. NINA's
    reddit thread is a real community — not this.
    """
    raw = str(url or "").strip()
    if not raw:
        return False
    if is_twitter_status_url(raw):
        return True
    host = domain(raw)
    if not host:
        return False
    if host in _GENERATED_SITE_HOSTS or host.endswith(".auction"):
        return True
    if host in _PASSTHROUGH_SITE_HOSTS:
        return False
    try:
        path = urlparse(raw if "://" in raw else f"https://{raw}").path or ""
    except Exception:
        return False
    return bool(_MINT_PAGE_RE.search(path))


def is_staged_social_pair(twitter: str | None, website: str | None) -> bool:
    """Borrowed tweet + generated site. Status URL alone is not enough
    (NINA / ニーナ / BILLION also paste a launch tweet)."""
    return is_twitter_status_url(twitter) and is_staged_launch_website(website)


def is_official_brand_website(url: str | None) -> bool:
    """True when the linked site is a famous brand, not this mint.

    Live Google Gemini used https://gemini.google.com/app and the
    heuristic credited "Has a website".
    """
    host = domain(url)
    if not host:
        return False
    return host in _BRAND_WEBSITE_HOSTS


def domain(url: str | None) -> str:
    if not url:
        return ""
    try:
        host = urlparse(url if "://" in url else f"https://{url}").netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""
