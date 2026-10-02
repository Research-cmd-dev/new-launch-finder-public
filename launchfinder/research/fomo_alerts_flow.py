"""Learn aggregates on persisted FOMO /ws/alerts flow."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import settings
from ..models import FomoAlertEvent, utcnow


def _utc_day_start(now: datetime | None = None) -> datetime:
    now = now or utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def recent_flow_events(session: Session, *, limit: int = 100) -> list[dict[str, Any]]:
    since = _utc_day_start()
    rows = (
        session.query(FomoAlertEvent)
        .filter(FomoAlertEvent.received_at >= since)
        .order_by(FomoAlertEvent.received_at.desc())
        .limit(max(1, min(int(limit), 500)))
        .all()
    )
    return [_event_dict(r) for r in rows]


def _event_dict(row: FomoAlertEvent) -> dict[str, Any]:
    return {
        "event_id": row.event_id,
        "trader": row.trader,
        "trader_wallet": row.trader_wallet or "",
        "user_id": row.user_id,
        "symbol": row.token_symbol,
        "mint": row.mint,
        "chain": row.chain,
        "alert_type": row.alert_type,
        "usd_value": row.usd_value,
        "trade_id": row.trade_id,
        "text": row.text_snippet,
        "event_ts": row.event_ts.isoformat() if row.event_ts else None,
        "received_at": row.received_at.isoformat() if row.received_at else None,
        "on_hunt": row.on_hunt,
        "on_paper_v1": row.on_paper_v1,
        "known_token": row.known_token,
        "paper_v1_skipped": row.paper_v1_skipped,
    }


def flow_clusters(session: Session, *, window_min: int | None = None, min_traders: int | None = None) -> list[dict[str, Any]]:
    window_min = int(window_min or settings.fomo_flow_cluster_window_min)
    min_traders = int(min_traders or settings.fomo_flow_cluster_min_traders)
    since = utcnow() - timedelta(minutes=max(5, window_min))
    rows = (
        session.query(FomoAlertEvent)
        .filter(FomoAlertEvent.received_at >= since)
        .order_by(FomoAlertEvent.received_at.desc())
        .all()
    )
    by_mint: dict[tuple[str, str], list[FomoAlertEvent]] = defaultdict(list)
    for row in rows:
        by_mint[(row.chain, row.mint)].append(row)
    out: list[dict[str, Any]] = []
    for (chain, mint), group in by_mint.items():
        traders = {t for t in (r.trader or r.user_id for r in group) if t}
        if len(traders) < min_traders:
            continue
        symbol = next((g.token_symbol for g in group if g.token_symbol), "")
        usd_sum = sum(float(g.usd_value or 0.0) for g in group)
        out.append(
            {
                "chain": chain,
                "mint": mint,
                "symbol": symbol,
                "distinct_traders": len(traders),
                "events": len(group),
                "usd_sum": round(usd_sum, 2),
                "on_hunt": any(g.on_hunt for g in group),
                "on_paper_v1": any(g.on_paper_v1 for g in group),
                "traders": sorted(traders)[:12],
            }
        )
    out.sort(key=lambda x: (-x["distinct_traders"], -x["usd_sum"]))
    return out[:40]


def first_big_buys_on_hunt(session: Session, *, limit: int = 30) -> list[dict[str, Any]]:
    since = _utc_day_start()
    rows = (
        session.query(FomoAlertEvent)
        .filter(
            FomoAlertEvent.received_at >= since,
            FomoAlertEvent.on_hunt.is_(True),
            FomoAlertEvent.alert_type == "buy",
        )
        .order_by(FomoAlertEvent.received_at.asc())
        .all()
    )
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = (row.chain, row.mint)
        if key in seen:
            continue
        seen.add(key)
        out.append(_event_dict(row))
        if len(out) >= limit:
            break
    return out


def near_miss_paper_skips(session: Session, *, limit: int = 40) -> list[dict[str, Any]]:
    since = _utc_day_start()
    rows = (
        session.query(FomoAlertEvent)
        .filter(
            FomoAlertEvent.received_at >= since,
            FomoAlertEvent.on_hunt.is_(True),
            FomoAlertEvent.paper_v1_skipped.is_(True),
        )
        .order_by(FomoAlertEvent.usd_value.desc(), FomoAlertEvent.received_at.desc())
        .limit(max(1, min(int(limit), 200)))
        .all()
    )
    return [_event_dict(r) for r in rows]


def flow_digest(session: Session) -> dict[str, Any]:
    from .fomo_trader_scorecard import top_traders_snippet

    since = _utc_day_start()
    total = session.query(func.count(FomoAlertEvent.id)).filter(FomoAlertEvent.received_at >= since).scalar() or 0
    by_chain = (
        session.query(FomoAlertEvent.chain, func.count(FomoAlertEvent.id))
        .filter(FomoAlertEvent.received_at >= since)
        .group_by(FomoAlertEvent.chain)
        .all()
    )
    return {
        "window": "utc_day",
        "since": since.isoformat(),
        "total_events": int(total),
        "by_chain": {str(c): int(n) for c, n in by_chain},
        "clusters": flow_clusters(session),
        "first_big_buy_on_hunt": first_big_buys_on_hunt(session),
        "near_miss_paper_skips": near_miss_paper_skips(session),
        "recent": recent_flow_events(session, limit=50),
        "top_traders": top_traders_snippet(session, limit=5),
        "note": (
            "Keyed FOMO /ws/alerts social flow — learn only. Compatible with hourly "
            "fomo_trending audit; does not open paper fills."
        ),
    }
