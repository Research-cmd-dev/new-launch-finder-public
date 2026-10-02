from __future__ import annotations

import logging

from .chains import chain_links, normalize_chain
from .config import settings
from .httputil import post_json

log = logging.getLogger("launchfinder.alerts")


def alerts_configured() -> bool:
    return bool(
        (settings.telegram_bot_token and settings.telegram_chat_id)
        or settings.discord_webhook_url
    )


# A high score with one of these flags is a contradiction — the flag is more
# trustworthy than the score, so never ping a human about it.
HARD_STOPS = (
    "honeypot",
    "wash trading",
    "hijack",
    "banned",
    "start-high rug",
    "pre-pumped",
    "copycat spam",
)


def is_hard_stopped(flags: list[str]) -> bool:
    joined = " | ".join(flags).lower()
    return any(stop in joined for stop in HARD_STOPS)


# One alert per token per process: dense early re-scoring must not spam the
# same ping every five minutes. A restart may repeat one alert — acceptable.
_alerted: set[str] = set()


def format_alert(
    *,
    symbol: str,
    name: str,
    mint: str,
    p_good: float,
    mcap_usd: float,
    flags: list[str],
    reasons: list[str] | None = None,
    chain: str = "sol",
) -> str:
    links = chain_links(mint, chain)
    lines = [
        f"🚨 {symbol or mint[:8]} — p(good) {p_good:.0%}",
        f"{name}".strip(),
        f"mcap ${mcap_usd:,.0f}" if mcap_usd else "",
        f"why: {'; '.join(reasons[:3])}" if reasons else "",
        f"flags: {', '.join(flags)}" if flags else "",
        links.get("pons") or links.get("pump") or f"{normalize_chain(chain)} · {mint}",
        links.get("gmgn") or "",
    ]
    return "\n".join(line for line in lines if line)


async def notify_high_score(
    *,
    symbol: str,
    name: str,
    mint: str,
    p_good: float,
    mcap_usd: float,
    flags: list[str],
    reasons: list[str] | None = None,
    chain: str = "sol",
    min_p: float | None = None,
) -> None:
    """``min_p`` is the caller's line for this score's scale (a first-sight
    0.50 is not under ALERT_MIN_P=0.70 — it is the even-odds line)."""
    floor = settings.alert_min_p if min_p is None else float(min_p)
    if p_good < floor or not alerts_configured():
        return
    if mint in _alerted:
        return
    if is_hard_stopped(flags):
        log.info("alert suppressed for %s: hard-stop flag present", symbol or mint)
        return
    _alerted.add(mint)
    if len(_alerted) > 5000:
        _alerted.clear()
    text = format_alert(
        symbol=symbol, name=name, mint=mint, p_good=p_good, mcap_usd=mcap_usd, flags=flags, reasons=reasons, chain=chain
    )
    if settings.telegram_bot_token and settings.telegram_chat_id:
        await post_json(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            {
                "chat_id": settings.telegram_chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
        )
    if settings.discord_webhook_url:
        await post_json(settings.discord_webhook_url, {"content": text})
    log.info("alerted %s p=%.2f", symbol or mint, p_good)


def format_bloom_alert(
    *,
    symbol: str,
    name: str,
    mint: str,
    promise_p: float,
    entry_p: float,
    mcap_usd: float,
    thesis: str,
    reasons: list[str] | None = None,
    flags: list[str] | None = None,
    chain: str = "sol",
) -> str:
    links = chain_links(mint, chain)
    lines = [
        f"🌱 {symbol or mint[:8]} — bloom {promise_p:.0%} (entry {entry_p:.0%})",
        (name or "").strip(),
        f"mcap ${mcap_usd:,.0f}" if mcap_usd else "",
        thesis.strip(),
        f"why now: {'; '.join((reasons or [])[:4])}" if reasons else "",
        f"flags: {', '.join(flags)}" if flags else "",
        links.get("pons") or links.get("pump") or f"{normalize_chain(chain)} · {mint}",
        links.get("gmgn") or "",
    ]
    return "\n".join(line for line in lines if line)


_bloom_alerted: set[str] = set()


async def notify_bloom(
    *,
    symbol: str,
    name: str,
    mint: str,
    promise_p: float,
    entry_p: float,
    mcap_usd: float,
    thesis: str,
    reasons: list[str] | None = None,
    flags: list[str] | None = None,
    chain: str = "sol",
) -> bool:
    """Telegram a late bloomer. Does not use ALERT_MIN_P (that is entry p)."""
    if promise_p < settings.bloom_min_promise or not alerts_configured():
        return False
    if mint in _bloom_alerted:
        return False
    if is_hard_stopped(flags or []):
        log.info("bloom suppressed for %s: hard-stop flag present", symbol or mint)
        return False
    _bloom_alerted.add(mint)
    if len(_bloom_alerted) > 5000:
        _bloom_alerted.clear()
    text = format_bloom_alert(
        symbol=symbol,
        name=name,
        mint=mint,
        promise_p=promise_p,
        entry_p=entry_p,
        mcap_usd=mcap_usd,
        thesis=thesis,
        reasons=reasons,
        flags=flags,
        chain=chain,
    )
    if settings.telegram_bot_token and settings.telegram_chat_id:
        await post_json(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            {
                "chat_id": settings.telegram_chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
        )
    if settings.discord_webhook_url:
        await post_json(settings.discord_webhook_url, {"content": text})
    log.info("bloom alerted %s promise=%.2f entry=%.2f", symbol or mint, promise_p, entry_p)
    return True
