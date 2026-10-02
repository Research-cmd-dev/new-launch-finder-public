from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..config import GITHUB_API, settings
from ..httputil import get_json
from ..social import age_days, parse_joined


def launch_signal(gh: dict[str, Any], *, launched_at: datetime | None = None) -> dict[str, Any]:
    """Free GitHub snapshot vs launch clock. Not a FEATURE_NAMES row.

    A repo pushed after migrate with real age is a public-dev tell.
    A 1-day empty repo is the opposite. Does not rewrite entry p_good.
    """
    if not gh:
        return {"delta": 0.0, "reasons": [], "line": ""}
    age = float(gh.get("age_days") or 0.0)
    stars = int(gh.get("stars") or 0)
    commits = int(gh.get("commits") or 0)
    pushed = parse_joined(str(gh.get("pushed_at") or ""))
    if age < 2 and stars <= 0 and commits <= 2:
        return {
            "delta": -0.03,
            "reasons": [f"GitHub looks staged at launch ({age:.0f}d)"],
            "line": f"GitHub is brand-new ({age:.0f}d) — treat as staged, not a real repo.",
        }
    launch = launched_at
    if launch is not None and launch.tzinfo is None:
        launch = launch.replace(tzinfo=timezone.utc)
    if pushed is not None and pushed.tzinfo is None:
        pushed = pushed.replace(tzinfo=timezone.utc)
    after = bool(launch and pushed and pushed > launch)
    real = age >= 30 or commits >= 10 or stars >= 3
    if after and real:
        return {
            "delta": 0.03,
            "reasons": ["GitHub pushed after launch"],
            "line": "GitHub updated after migrate.",
        }
    return {"delta": 0.0, "reasons": [], "line": ""}


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    return headers


async def lookup_repo(owner_repo: str) -> dict[str, Any]:
    owner_repo = owner_repo.strip().strip("/")
    if "/" not in owner_repo:
        return await lookup_user(owner_repo)
    data = await get_json(f"{GITHUB_API}/repos/{owner_repo}", headers=_headers())
    if not isinstance(data, dict) or not data.get("full_name"):
        return {}
    created = parse_joined(data.get("created_at"))
    out = {
        "full_name": data.get("full_name"),
        "url": data.get("html_url") or f"https://github.com/{owner_repo}",
        "stars": int(data.get("stargazers_count") or 0),
        "forks": int(data.get("forks_count") or 0),
        "open_issues": int(data.get("open_issues_count") or 0),
        "age_days": age_days(created),
        "description": data.get("description") or "",
        "pushed_at": data.get("pushed_at") or "",
        "is_fork": bool(data.get("fork")),
    }
    # Deep history costs 2 more calls — only with an authenticated budget.
    if settings.github_token and out["full_name"]:
        out.update(await repo_activity(out["full_name"]))
    return out


async def repo_activity(owner_repo: str) -> dict[str, Any]:
    """Commit and contributor volume — the fields that separate a project
    with real history from a repo staged the day of the token launch."""
    commits = await get_json(
        f"{GITHUB_API}/repos/{owner_repo}/commits",
        headers=_headers(),
        params={"per_page": 30},
    )
    contributors = await get_json(
        f"{GITHUB_API}/repos/{owner_repo}/contributors",
        headers=_headers(),
        params={"per_page": 10},
    )
    return {
        "commits": len(commits) if isinstance(commits, list) else 0,
        "contributors": len(contributors) if isinstance(contributors, list) else 0,
    }


async def lookup_user(owner: str) -> dict[str, Any]:
    data = await get_json(f"{GITHUB_API}/users/{owner}", headers=_headers())
    if not isinstance(data, dict) or not data.get("login"):
        return {}
    created = parse_joined(data.get("created_at"))
    return {
        "full_name": data.get("login"),
        "url": data.get("html_url") or f"https://github.com/{owner}",
        "stars": 0,
        "forks": 0,
        "open_issues": 0,
        "age_days": age_days(created),
        "description": data.get("bio") or "",
        "public_repos": int(data.get("public_repos") or 0),
    }


async def search_token(name: str, symbol: str, mint: str) -> dict[str, Any]:
    """Only accept a search hit if the mint or exact ticker is in the repo text."""
    q = f"{symbol} {name} solana in:name,description,readme"
    data = await get_json(
        f"{GITHUB_API}/search/repositories",
        headers=_headers(),
        params={"q": q, "per_page": 5, "sort": "updated"},
    )
    items = (data or {}).get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return {}
    needle = (mint or "").lower()
    ticker = (symbol or "").lower()
    for item in items:
        blob = " ".join(
            [
                str(item.get("full_name") or ""),
                str(item.get("description") or ""),
                str(item.get("html_url") or ""),
            ]
        ).lower()
        if needle and needle in blob:
            return await lookup_repo(item.get("full_name") or "")
        if ticker and len(ticker) >= 3 and ticker in blob and "solana" in blob:
            return await lookup_repo(item.get("full_name") or "")
    return {}
