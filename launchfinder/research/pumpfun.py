from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..config import PUMP_API
from ..httputil import get_json


def _ts(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        n = float(value)
        if n > 1e12:
            n /= 1000.0
        return datetime.fromtimestamp(n, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def normalize_coin(raw: dict[str, Any]) -> dict[str, Any]:
    created = _ts(raw.get("created_timestamp"))
    updated = _ts(raw.get("updated_at") or raw.get("last_trade_timestamp"))
    mcap = float(raw.get("usd_market_cap") or raw.get("market_cap_usd") or 0.0)
    return {
        "mint": raw.get("mint") or "",
        "name": raw.get("name") or "",
        "symbol": raw.get("symbol") or "",
        "description": raw.get("description") or "",
        "image_url": raw.get("image_uri") or "",
        "twitter": raw.get("twitter") or "",
        "website": raw.get("website") or "",
        "telegram": raw.get("telegram") or "",
        "creator": raw.get("creator") or "",
        "username": raw.get("username") or "",
        "complete": bool(raw.get("complete")),
        "nsfw": bool(raw.get("nsfw")),
        "banned": bool(raw.get("is_banned")),
        "reply_count": int(raw.get("reply_count") or 0),
        "created_at": created,
        "updated_at": updated,
        "mcap_usd": mcap,
        "ath_mcap": float(raw.get("ath_market_cap") or 0.0),
        "pool_address": raw.get("pump_swap_pool") or raw.get("pool_address") or raw.get("raydium_pool") or "",
        "raw": raw,
    }


async def list_coins(*, complete: bool | None = None, sort: str = "created_timestamp", offset: int = 0, limit: int = 50) -> list[dict[str, Any]]:
    params = f"offset={offset}&limit={limit}&sort={sort}&order=DESC&includeNsfw=false"
    if complete is True:
        params += "&complete=true"
    elif complete is False:
        params += "&complete=false"
    data = await get_json(f"{PUMP_API}/coins?{params}")
    if not isinstance(data, list):
        return []
    return [normalize_coin(x) for x in data if isinstance(x, dict) and x.get("mint")]


async def get_coin(mint: str) -> dict[str, Any] | None:
    data = await get_json(f"{PUMP_API}/coins/{mint}")
    if not isinstance(data, dict) or not data.get("mint"):
        return None
    return normalize_coin(data)


async def creator_coins(creator: str, limit: int = 20) -> list[dict[str, Any]]:
    if not creator:
        return []
    data = await get_json(f"{PUMP_API}/coins?creator={creator}&offset=0&limit={limit}&includeNsfw=false")
    if not isinstance(data, list):
        return []
    return [normalize_coin(x) for x in data if isinstance(x, dict) and x.get("mint")]


async def get_user(address: str) -> dict[str, Any]:
    data = await get_json(f"{PUMP_API}/users/{address}")
    return data if isinstance(data, dict) else {}
