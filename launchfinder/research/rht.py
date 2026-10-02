"""robinhoodtrenches.com — live tape of tracked fomo.family wallets.

Unofficial, keyless, no wallet. Radar is first FOMO-wallet buy on a
young pool. Token board is who piled in over a window. No extra GMGN.
"""

from __future__ import annotations

from typing import Any

from ..httputil import client

RHT_API = "https://robinhoodtrenches.com"


def parse_rht_rows(payload: Any) -> list[dict[str, Any]]:
    """RH meme mints only. Stocks, drained books, and non-0x rows drop."""
    rows = payload if isinstance(payload, list) else []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("is_stock"):
            continue
        if row.get("drained"):
            continue
        address = str(row.get("token") or row.get("mint") or "").strip()
        if not address.startswith("0x"):
            continue
        mint = address.lower()
        if mint in seen:
            continue
        seen.add(mint)
        out.append(
            {
                "mint": mint,
                "name": str(row.get("name") or ""),
                "symbol": str(row.get("symbol") or ""),
                "network": "robinhood",
                "fomo_buyers": int(row.get("buyers") or row.get("traders") or 0),
            }
        )
    return out


async def fetch_rht_radar(*, minutes: int = 180, limit: int = 40) -> list[dict[str, Any]]:
    resp = await client().get(
        f"{RHT_API}/api/radar",
        params={"minutes": max(15, min(int(minutes), 720)), "limit": max(1, min(int(limit), 60))},
    )
    if resp.status_code >= 400:
        return []
    try:
        payload = resp.json()
    except Exception:
        return []
    return parse_rht_rows(payload)


async def fetch_rht_tokens(*, window: str = "24h", limit: int = 60) -> list[dict[str, Any]]:
    resp = await client().get(
        f"{RHT_API}/api/tokens",
        params={"window": window, "limit": max(1, min(int(limit), 200)), "stocks": "false"},
    )
    if resp.status_code >= 400:
        return []
    try:
        payload = resp.json()
    except Exception:
        return []
    return parse_rht_rows(payload)
