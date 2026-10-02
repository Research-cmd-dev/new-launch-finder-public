"""Parse, filter, and persist FOMO App /ws/alerts (and REST-shaped rows).

Paper-safe learn layer only. WS messages are free once connected (keyed).
Never opens paper fills or arms live execution.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..chains import normalize_chain, normalize_mint
from ..config import settings
from ..models import FomoAlertEvent, HuntCard, PaperFill, Token, utcnow
from . import fomo_api as _fomo_api
from .fomo_api import (
    _SOL_MINT_RE,
    desk_chain_from_fomo,
    fetch_fomo_user_wallet_sync,
    fomo_user_wallet_lookup_enabled,
    note_wallet_lookup_skipped,
)

log = logging.getLogger("launchfinder.fomo_alerts")

FOMO_WS = "wss://api.fomoapi.io/ws/alerts"
KEEP_ALERT_TYPES = frozenset({"buy", "sell", "thesis"})
_SKIP_MSG_TYPES = frozenset({"welcome", "heartbeat", "ping", "pong", "connected", "subscribed"})


def fomo_alerts_ws_url() -> str | None:
    key = (settings.fomo_api_key or "").strip()
    if not key:
        return None
    from urllib.parse import quote

    return f"{FOMO_WS}?key={quote(key)}"


def _parse_ts(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        sec = float(raw)
        if sec > 1e12:
            sec /= 1000.0
        try:
            return datetime.fromtimestamp(sec, tz=timezone.utc)
        except (OSError, ValueError):
            return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        ts = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _wallet_for_chain(raw: str, chain: str) -> str:
    addr = str(raw or "").strip()
    if not addr:
        return ""
    ch = normalize_chain(chain)
    if ch == "robinhood":
        if not addr.lower().startswith("0x"):
            return ""
        return normalize_mint(addr, ch)
    if ch == "sol":
        if addr.lower().startswith("0x"):
            return ""
        if not _SOL_MINT_RE.fullmatch(addr):
            return ""
        return normalize_mint(addr, ch)
    return ""


def parse_trader_wallet_from_row(row: dict[str, Any], chain: str) -> str:
    """Trader on-chain wallet from a raw FOMO alert. Ignores token mint fields."""
    token_addr = str(
        row.get("tokenAddress") or row.get("token_address") or row.get("mint") or ""
    ).strip()
    candidates: list[str] = []
    trader_block = row.get("trader")
    if isinstance(trader_block, dict):
        for key in ("wallet", "walletAddress", "address", "solAddress", "solana", "evm", "evmAddress"):
            val = trader_block.get(key)
            if val:
                candidates.append(str(val).strip())
        wallets = trader_block.get("wallets")
        if isinstance(wallets, dict):
            for key in ("solana", "sol", "evm", "robinhood"):
                val = wallets.get(key)
                if val:
                    candidates.append(str(val).strip())
    elif isinstance(trader_block, str) and trader_block.strip():
        pass
    for key in (
        "traderWallet",
        "trader_wallet",
        "traderWalletAddress",
        "traderAddress",
        "userWallet",
        "user_wallet",
        "walletAddress",
        "solAddress",
        "wallet",
    ):
        val = row.get(key)
        if val and not isinstance(val, dict):
            candidates.append(str(val).strip())
    top_address = str(row.get("address") or "").strip()
    if top_address and top_address != token_addr:
        candidates.append(top_address)
    for raw in candidates:
        if not raw or raw == token_addr:
            continue
        normalized = _wallet_for_chain(raw, chain)
        if normalized:
            return normalized
    return ""


def _row_chain(row: dict[str, Any]) -> str | None:
    address = str(
        row.get("tokenAddress")
        or row.get("token_address")
        or row.get("address")
        or row.get("mint")
        or ""
    ).strip()
    network = row.get("chain") or row.get("chainId") or row.get("network") or row.get("networkId")
    return desk_chain_from_fomo(network, address)


def normalize_fomo_alert_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """One alert dict suitable for filter + persist. None if unusable."""
    if not isinstance(row, dict):
        return None
    event_id = str(row.get("eventId") or row.get("event_id") or row.get("id") or "").strip()
    if not event_id:
        return None
    chain = _row_chain(row)
    if chain is None:
        return None
    address = str(
        row.get("tokenAddress")
        or row.get("token_address")
        or row.get("address")
        or row.get("mint")
        or ""
    ).strip()
    mint = normalize_mint(address, chain)
    if not mint:
        return None
    alert_type = str(row.get("alertType") or row.get("alert_type") or row.get("type") or "").strip().lower()
    if alert_type in _SKIP_MSG_TYPES:
        return None
    try:
        usd = float(row.get("usdValue") or row.get("usd_value") or row.get("usd") or 0.0)
    except (TypeError, ValueError):
        usd = 0.0
    trader = str(row.get("trader") or row.get("handle") or row.get("username") or "").strip()
    user_id = str(row.get("userId") or row.get("user_id") or "").strip()
    symbol = str(row.get("token") or row.get("symbol") or row.get("tokenSymbol") or "").strip()
    trade_id = str(row.get("tradeId") or row.get("trade_id") or "").strip()
    text = str(row.get("text") or row.get("thesis") or row.get("message") or "").strip()
    if len(text) > 500:
        text = text[:500]
    trader_wallet = parse_trader_wallet_from_row(row, chain)
    return {
        "event_id": event_id,
        "user_id": user_id,
        "trader": trader,
        "trader_wallet": trader_wallet,
        "token_symbol": symbol,
        "mint": mint,
        "chain": chain,
        "alert_type": alert_type,
        "usd_value": usd,
        "trade_id": trade_id,
        "text_snippet": text,
        "event_ts": _parse_ts(row.get("ts") or row.get("timestamp") or row.get("createdAt")),
    }


def passes_fomo_alert_filters(row: dict[str, Any], *, min_usd: float | None = None) -> bool:
    floor = float(min_usd if min_usd is not None else settings.fomo_alerts_min_usd)
    if row.get("alert_type") not in KEEP_ALERT_TYPES:
        return False
    try:
        usd = float(row.get("usd_value") or 0.0)
    except (TypeError, ValueError):
        return False
    return usd >= floor


def iter_ws_alert_rows(payload: Any) -> list[dict[str, Any]]:
    """Expand one decoded WS JSON frame into raw alert dicts."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    msg_type = str(payload.get("type") or payload.get("event") or "").strip().lower()
    if msg_type in _SKIP_MSG_TYPES:
        return []
    if msg_type in {"replay", "alerts", "batch"}:
        rows = payload.get("alerts") or payload.get("data") or payload.get("items")
        if isinstance(rows, list):
            return [x for x in rows if isinstance(x, dict)]
    if msg_type in {"alert", "trade", "activity"} or payload.get("eventId") or payload.get("event_id"):
        return [payload]
    for key in ("alerts", "data", "items", "alert"):
        block = payload.get(key)
        if isinstance(block, list):
            return [x for x in block if isinstance(x, dict)]
        if isinstance(block, dict):
            return [block]
    return []


def parse_ws_payload(payload: Any) -> list[dict[str, Any]]:
    """Normalized, chain-filtered alert rows from one WS message."""
    out: list[dict[str, Any]] = []
    for raw in iter_ws_alert_rows(payload):
        norm = normalize_fomo_alert_row(raw)
        if norm is None:
            continue
        if not passes_fomo_alert_filters(norm):
            continue
        out.append(norm)
    return out


def desk_join_flags(session: Session, chain: str, mint: str) -> dict[str, bool]:
    from ..scoring.paper_v1 import PAPER_V1_LINE

    chain = normalize_chain(chain)
    mint_n = normalize_mint(mint, chain)
    token = session.query(Token).filter(Token.mint == mint_n).one_or_none()
    if token is None and chain == "robinhood":
        token = session.query(Token).filter(Token.mint == mint_n.lower()).one_or_none()
    on_hunt = (
        session.query(HuntCard.id)
        .filter(HuntCard.chain == chain, HuntCard.mint == mint_n)
        .first()
        is not None
    )
    paper_v1 = (
        session.query(PaperFill.status)
        .filter(PaperFill.chain == chain, PaperFill.mint == mint_n, PaperFill.line == PAPER_V1_LINE)
        .order_by(PaperFill.id.desc())
        .first()
    )
    paper_status = str(paper_v1[0]) if paper_v1 else ""
    return {
        "known_token": token is not None,
        "on_hunt": on_hunt,
        "on_paper_v1": paper_status in ("open", "queued", "closed"),
        "paper_v1_skipped": paper_status == "skipped",
    }


def persist_fomo_alert(session: Session, row: dict[str, Any]) -> bool:
    """Insert one event. Returns True when a new row was written.

    Keeps ``trader_wallet`` only when already on the WS/normalized payload.
    Does not call keyed ``GET /v2/users/id`` unless ``FOMO_USER_WALLET_LOOKUP``
    is explicitly on. Alerts still persist with user_id / trader for Learn.
    """
    wallet = str(row.get("trader_wallet") or "").strip()
    uid = str(row.get("user_id") or "").strip()
    if not wallet and uid:
        if fomo_user_wallet_lookup_enabled():
            wallet = fetch_fomo_user_wallet_sync(uid, row["chain"])
            if wallet:
                row["trader_wallet"] = wallet
        else:
            note_wallet_lookup_skipped(user_id=uid, chain=str(row.get("chain") or ""))
    if (
        session.query(FomoAlertEvent.id)
        .filter(FomoAlertEvent.event_id == row["event_id"])
        .first()
        is not None
    ):
        return False
    flags = desk_join_flags(session, row["chain"], row["mint"])
    ev = FomoAlertEvent(
        event_id=row["event_id"],
        user_id=row.get("user_id") or "",
        trader=row.get("trader") or "",
        trader_wallet=wallet,
        token_symbol=row.get("token_symbol") or "",
        mint=row["mint"],
        chain=row["chain"],
        alert_type=row.get("alert_type") or "",
        usd_value=float(row.get("usd_value") or 0.0),
        trade_id=row.get("trade_id") or "",
        text_snippet=row.get("text_snippet") or "",
        event_ts=row.get("event_ts"),
        received_at=utcnow(),
        on_hunt=flags["on_hunt"],
        known_token=flags["known_token"],
        on_paper_v1=flags["on_paper_v1"],
        paper_v1_skipped=flags["paper_v1_skipped"],
    )
    session.add(ev)
    try:
        session.flush()
    except IntegrityError:
        session.expunge(ev)
        return False
    return True


def fomo_alerts_heartbeat_note(
    *,
    connected: bool = False,
    received: int = 0,
    inserted: int = 0,
    sit_out: str = "",
    error: str = "",
) -> str:
    if error:
        return f"error {error}"
    if sit_out:
        return sit_out
    bits = [f"ws={'up' if connected else 'down'}", f"recv={received}", f"new={inserted}"]
    if not fomo_user_wallet_lookup_enabled():
        bits.append("wallet_lookup=off")
        if _fomo_api.WALLET_LOOKUP_SKIPS:
            bits.append(f"wallet_skip={_fomo_api.WALLET_LOOKUP_SKIPS}")
    return " · ".join(bits)
