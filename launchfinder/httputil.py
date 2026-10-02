from __future__ import annotations

import logging
from typing import Any, Callable

import httpx

log = logging.getLogger("launchfinder.http")

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=8.0),
            headers={"User-Agent": "new-launch-finder/0.1"},
            follow_redirects=True,
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def get_json(url: str, *, headers: dict[str, str] | None = None, params: dict | None = None) -> Any:
    try:
        resp = await client().get(url, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.debug("GET failed %s: %s", url, exc)
        return None


async def post_json(
    url: str,
    payload: dict,
    *,
    headers: dict[str, str] | None = None,
    params: dict | None = None,
    on_status: Callable[[int, str], None] | None = None,
) -> Any:
    """POST and parse JSON; None on any failure.

    ``on_status`` receives (status_code, body_prefix) for non-2xx replies so
    a caller can trip a breaker on 429 without the exception leaking.
    """
    try:
        resp = await client().post(url, json=payload, headers=headers, params=params)
        if on_status is not None and resp.status_code >= 400:
            try:
                on_status(int(resp.status_code), (resp.text or "")[:120])
            except Exception:
                log.debug("on_status hook failed", exc_info=True)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.debug("POST failed %s: %s", url, exc)
        return None
