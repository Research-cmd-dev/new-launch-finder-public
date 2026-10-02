"""Pump holder-rewards board, as a side flag. Not a feature.

https://pump.fun/holder-rewards is a public page (no frontend-api route).
We read it on a timer and attach a match onto a Solana card we already
track: dollars paid to holders, and whether the pair is a meme rather
than SOL or a stable. FEATURE_NAMES stays 66. Entry p_good is untouched.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

log = logging.getLogger("launchfinder.holder_rewards")

BOARD_URL = "https://pump.fun/holder-rewards"
REFRESH_SECONDS = 20 * 60

# Wrapped SOL and the stables a pump pair uses as the quote. Anything else
# is a custom meme (or stock) pair: buys route through that token.
_STABLE_QUOTES = frozenset(
    {
        "So11111111111111111111111111111111111111112",
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    }
)
_STABLE_SYMBOLS = frozenset({"SOL", "WSOL", "USDC", "USDT", "USD1"})

_MINT = re.compile(r'\\"mint\\":\\"([1-9A-HJ-NP-Za-km-z]{32,44})\\"')
_QUOTE = re.compile(r'\\"quoteMint\\":\\"([1-9A-HJ-NP-Za-km-z]{32,44})\\"')
_PAID = re.compile(r'\\"distributedUsd\\":([0-9]+(?:\.[0-9]+)?)')
_ASSET = re.compile(
    r'\\"([1-9A-HJ-NP-Za-km-z]{32,44})\\":\{\\"mint\\":\\"[1-9A-HJ-NP-Za-km-z]{32,44}\\",\\"symbol\\":\\"([^\\"]*)\\"'
)

_lock = threading.Lock()
_board: dict[str, dict[str, Any]] = {}
_at = 0.0


def parse_board(html: str) -> dict[str, dict[str, Any]]:
    """Mint -> payout row. The page embeds the list as escaped JSON."""
    if not html:
        return {}
    symbols = {mint: sym for mint, sym in _ASSET.findall(html)}
    out: dict[str, dict[str, Any]] = {}
    hits = list(_MINT.finditer(html))
    for i, hit in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else hit.end() + 4000
        chunk = html[hit.end() : end]
        paid = _PAID.search(chunk)
        quote = _QUOTE.search(chunk)
        if paid is None or quote is None:
            continue
        dollars = float(paid.group(1))
        if dollars <= 0:
            continue
        quote_mint = quote.group(1)
        symbol = symbols.get(quote_mint, "")
        meme = quote_mint not in _STABLE_QUOTES and symbol.upper() not in _STABLE_SYMBOLS
        mint = hit.group(1)
        prev = out.get(mint)
        if prev is not None and float(prev["distributed_usd"]) >= dollars:
            continue
        out[mint] = {
            "distributed_usd": round(dollars, 2),
            "quote_mint": quote_mint,
            "quote_symbol": symbol,
            "meme_pair": meme,
        }
    return out


def lookup(mint: str) -> dict[str, Any] | None:
    """Cached row for this mint. Does not fetch."""
    if not mint:
        return None
    with _lock:
        row = _board.get(mint)
    return dict(row) if row else None


def refresh() -> int:
    """Replace the cache from the public page. Returns how many coins paid out."""
    import httpx

    global _board, _at
    try:
        resp = httpx.get(
            BOARD_URL,
            timeout=25.0,
            headers={"User-Agent": "new-launch-finder/0.1"},
            follow_redirects=True,
        )
        resp.raise_for_status()
    except Exception as exc:
        log.warning("holder rewards board failed: %s", exc)
        return 0
    parsed = parse_board(resp.text)
    if not parsed:
        log.warning("holder rewards board parsed empty")
        return 0
    with _lock:
        _board = parsed
        _at = time.monotonic()
    log.info("holder rewards board %s coins", len(parsed))
    return len(parsed)


def cache_age() -> float:
    with _lock:
        if _at <= 0:
            return -1.0
        return time.monotonic() - _at


def start_holder_rewards() -> None:
    """Refresh the public board on a timer. One task per process."""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _loop() -> None:
        # Let worker boot + first hunt_tape beat before a 25s pump.fun GET.
        await asyncio.sleep(45.0)
        while True:
            try:
                await asyncio.to_thread(refresh)
            except Exception:
                log.exception("holder rewards refresh failed")
            await asyncio.sleep(REFRESH_SECONDS)

    loop.create_task(_loop())


def set_board_for_tests(rows: dict[str, dict[str, Any]] | None) -> None:
    global _board, _at
    with _lock:
        _board = dict(rows or {})
        _at = time.monotonic()
