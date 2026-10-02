"""FOMO /ws/alerts trader scorecard — Learn only.

Ranks traders by lead quality from ``fomo_alert_events``. Does not arm live,
lower Hunt floors, or open paper fills.

Composite rank (``rank_score``) for traders with ``n_buys >= min_buys``:

    rank_score = 2.0 * lead_rate
               + 1.5 * hit2x_rate
               - 1.0 * dump_rate
               + 0.5 * hunt_overlap
               - 1.0 * cluster_penalty

``hit2x_rate`` / ``dump_rate`` use only buys with a resolved Outcome multiple;
when ``n_with_outcome < 3`` the hit/dump terms are zero (honest nulls in API).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..config import settings
from ..models import FomoAlertEvent, HuntCard, Outcome, PaperFill, Token, utcnow
from ..scoring.paper_v1 import PAPER_V1_LINE

# Transparent weights — change only with a deliberate cycle knob.
W_LEAD = 2.0
W_HIT2X = 1.5
W_DUMP = 1.0
W_HUNT_OVERLAP = 0.5
W_CLUSTER = 1.0
HIT2X_MULT = 2.0
DUMP_MULT_MAX = 1.0
MIN_OUTCOMES_FOR_HIT_TERM = 3


def _aware(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def score_window_since(now: datetime | None = None, days: int | None = None) -> datetime:
    """Rolling window: last N UTC days including today (floor at UTC midnight N days back)."""
    now = _aware(now or utcnow()) or utcnow()
    d = int(days if days is not None else getattr(settings, "fomo_trader_score_window_days", 7))
    d = max(1, min(d, 90))
    start = (now - timedelta(days=d)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start


def trader_identity(event: FomoAlertEvent) -> str:
    uid = (event.user_id or "").strip()
    if uid:
        return f"uid:{uid}"
    handle = (event.trader or "").strip()
    if handle:
        return f"handle:{handle.lstrip('@').lower()}"
    return ""


def _display_trader(event: FomoAlertEvent) -> str:
    return (event.trader or "").strip() or (event.user_id or "").strip()


def _best_trader_wallet(events: list[FomoAlertEvent]) -> str:
    for ev in reversed(events):
        w = (getattr(ev, "trader_wallet", None) or "").strip()
        if w:
            return w
    return ""


def _event_time(event: FomoAlertEvent) -> datetime:
    return _aware(event.event_ts) or _aware(event.received_at) or utcnow()


def _load_hunt_first_seen(session: Session, keys: set[tuple[str, str]]) -> dict[tuple[str, str], datetime]:
    if not keys:
        return {}
    out: dict[tuple[str, str], datetime] = {}
    chains = {c for c, _ in keys}
    for chain in chains:
        mints = [m for c, m in keys if c == chain]
        if not mints:
            continue
        rows = (
            session.query(HuntCard.mint, HuntCard.first_seen_at)
            .filter(HuntCard.chain == chain, HuntCard.mint.in_(mints))
            .all()
        )
        for mint, at in rows:
            if at:
                out[(chain, mint)] = _aware(at) or at
    return out


def _load_paper_v1_opened(session: Session, keys: set[tuple[str, str]]) -> dict[tuple[str, str], datetime]:
    if not keys:
        return {}
    out: dict[tuple[str, str], datetime] = {}
    chains = {c for c, _ in keys}
    for chain in chains:
        mints = [m for c, m in keys if c == chain]
        rows = (
            session.query(PaperFill.mint, PaperFill.opened_at)
            .filter(
                PaperFill.chain == chain,
                PaperFill.mint.in_(mints),
                PaperFill.line == PAPER_V1_LINE,
                PaperFill.status.in_(("open", "queued", "closed")),
            )
            .all()
        )
        for mint, at in rows:
            if at and (chain, mint) not in out:
                out[(chain, mint)] = _aware(at) or at
            elif at:
                prev = out.get((chain, mint))
                at_a = _aware(at) or at
                if prev is None or at_a < prev:
                    out[(chain, mint)] = at_a
    return out


def _load_outcome_multiple(session: Session, keys: set[tuple[str, str]]) -> dict[tuple[str, str], float | None]:
    if not keys:
        return {}
    out: dict[tuple[str, str], float | None] = {}
    chains = {c for c, _ in keys}
    for chain in chains:
        mints = [m for c, m in keys if c == chain]
        rows = (
            session.query(Token.mint, Outcome.multiple, Outcome.label)
            .join(Outcome, Outcome.token_id == Token.id)
            .filter(Token.chain == chain, Token.mint.in_(mints))
            .all()
        )
        for mint, mult, _label in rows:
            try:
                m = float(mult or 0.0)
            except (TypeError, ValueError):
                m = 0.0
            out[(chain, mint)] = m if m > 0 else None
    return out


def _cluster_penalty_index(buys: list[FomoAlertEvent], min_traders: int) -> dict[str, float]:
    """Per-trader mean penalty when their buy landed in a crowded mint-hour."""
    hour_traders: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    event_penalty: dict[int, float] = {}
    for ev in buys:
        tid = trader_identity(ev)
        if not tid:
            continue
        ts = _event_time(ev)
        hour = ts.strftime("%Y-%m-%dT%H")
        bucket = (ev.chain, ev.mint, hour)
        hour_traders[bucket].add(tid)
    for ev in buys:
        ts = _event_time(ev)
        hour = ts.strftime("%Y-%m-%dT%H")
        bucket = (ev.chain, ev.mint, hour)
        n = len(hour_traders[bucket])
        event_penalty[ev.id] = 1.0 if n >= min_traders else 0.0
    by_trader: dict[str, list[float]] = defaultdict(list)
    for ev in buys:
        tid = trader_identity(ev)
        if tid:
            by_trader[tid].append(event_penalty.get(ev.id, 0.0))
    return {t: (sum(v) / len(v) if v else 0.0) for t, v in by_trader.items()}


def is_lead_early(
    event: FomoAlertEvent,
    hunt_at: datetime | None,
    paper_at: datetime | None,
) -> bool:
    ts = _event_time(event)
    if hunt_at and hunt_at > ts:
        return True
    if paper_at and paper_at > ts:
        return True
    return False


def _buy_rows(session: Session, since: datetime, min_usd: float) -> list[FomoAlertEvent]:
    return (
        session.query(FomoAlertEvent)
        .filter(
            FomoAlertEvent.received_at >= since,
            FomoAlertEvent.alert_type == "buy",
            FomoAlertEvent.usd_value >= min_usd,
        )
        .order_by(FomoAlertEvent.received_at.asc())
        .all()
    )


def _score_one_trader(
    tid: str,
    events: list[FomoAlertEvent],
    hunt_at: dict[tuple[str, str], datetime],
    paper_at: dict[tuple[str, str], datetime],
    outcome_mult: dict[tuple[str, str], float | None],
    cluster_penalty: float,
) -> dict[str, Any]:
    n_buys = len(events)
    lead_early = 0
    on_hunt = 0
    on_paper = 0
    skipped = 0
    hit2x = 0
    dump_proxy = 0
    n_outcome = 0
    samples: list[dict[str, Any]] = []

    for ev in events:
        key = (ev.chain, ev.mint)
        if is_lead_early(ev, hunt_at.get(key), paper_at.get(key)):
            lead_early += 1
        if ev.on_hunt:
            on_hunt += 1
        if ev.on_paper_v1:
            on_paper += 1
        if ev.paper_v1_skipped:
            skipped += 1
        mult = outcome_mult.get(key)
        if mult is not None and mult > 0:
            n_outcome += 1
            if mult >= HIT2X_MULT:
                hit2x += 1
            if mult < DUMP_MULT_MAX:
                dump_proxy += 1
        samples.append(
            {
                "event_id": ev.event_id,
                "mint": ev.mint,
                "chain": ev.chain,
                "symbol": ev.token_symbol,
                "usd_value": ev.usd_value,
                "event_ts": (_aware(ev.event_ts) or _aware(ev.received_at)).isoformat()
                if (ev.event_ts or ev.received_at)
                else None,
                "lead_early": is_lead_early(ev, hunt_at.get(key), paper_at.get(key)),
                "outcome_multiple": mult,
            }
        )

    lead_rate = lead_early / n_buys if n_buys else 0.0
    hunt_overlap = on_hunt / n_buys if n_buys else 0.0
    paper_overlap = on_paper / n_buys if n_buys else 0.0
    skip_overlap = skipped / n_buys if n_buys else 0.0
    hit2x_rate = hit2x / n_outcome if n_outcome else None
    dump_rate = dump_proxy / n_outcome if n_outcome else None

    hit_term = hit2x_rate if n_outcome >= MIN_OUTCOMES_FOR_HIT_TERM and hit2x_rate is not None else 0.0
    dump_term = dump_rate if n_outcome >= MIN_OUTCOMES_FOR_HIT_TERM and dump_rate is not None else 0.0

    rank_score = (
        W_LEAD * lead_rate
        + W_HIT2X * hit_term
        - W_DUMP * dump_term
        + W_HUNT_OVERLAP * hunt_overlap
        - W_CLUSTER * cluster_penalty
    )

    display = _display_trader(events[0])
    user_id = (events[0].user_id or "").strip()
    trader_wallet = _best_trader_wallet(events)
    return {
        "trader_key": tid,
        "trader": display,
        "user_id": user_id or None,
        "trader_wallet": trader_wallet or None,
        "n_buys": n_buys,
        "lead_early": lead_early,
        "lead_rate": round(lead_rate, 4),
        "n_with_outcome": n_outcome,
        "hit2x": hit2x,
        "hit2x_rate": round(hit2x_rate, 4) if hit2x_rate is not None else None,
        "dump_proxy": dump_proxy,
        "dump_rate": round(dump_rate, 4) if dump_rate is not None else None,
        "desk_overlap": {
            "on_hunt": round(hunt_overlap, 4),
            "on_paper_v1": round(paper_overlap, 4),
            "paper_v1_skipped": round(skip_overlap, 4),
        },
        "cluster_penalty": round(cluster_penalty, 4),
        "rank_score": round(rank_score, 4),
        "samples": samples[-20:],
    }


def build_trader_scorecard(
    session: Session,
    *,
    days: int | None = None,
    min_buys: int | None = None,
    min_usd: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    since = score_window_since(now, days)
    floor_usd = float(min_usd if min_usd is not None else settings.fomo_alerts_min_usd)
    min_n = int(min_buys if min_buys is not None else getattr(settings, "fomo_trader_min_buys", 5))
    cluster_min = int(getattr(settings, "fomo_trader_cluster_min_traders", 5))

    buys = _buy_rows(session, since, floor_usd)
    keys = {(b.chain, b.mint) for b in buys}
    hunt_at = _load_hunt_first_seen(session, keys)
    paper_at = _load_paper_v1_opened(session, keys)
    outcome_mult = _load_outcome_multiple(session, keys)
    cluster_by_trader = _cluster_penalty_index(buys, cluster_min)

    by_trader: dict[str, list[FomoAlertEvent]] = defaultdict(list)
    for ev in buys:
        tid = trader_identity(ev)
        if tid:
            by_trader[tid].append(ev)

    ranked: list[dict[str, Any]] = []
    below_floor: list[dict[str, Any]] = []
    for tid, events in by_trader.items():
        row = _score_one_trader(
            tid,
            events,
            hunt_at,
            paper_at,
            outcome_mult,
            cluster_by_trader.get(tid, 0.0),
        )
        if row["n_buys"] >= min_n:
            row["ranked"] = True
            ranked.append(row)
        else:
            row["ranked"] = False
            below_floor.append(row)

    ranked.sort(key=lambda r: (-r["rank_score"], -r["lead_early"], -r["n_buys"]))
    for i, row in enumerate(ranked, start=1):
        row["rank"] = i

    return {
        "window_days": int(days or getattr(settings, "fomo_trader_score_window_days", 7)),
        "since": since.isoformat(),
        "min_buys": min_n,
        "min_usd": floor_usd,
        "formula": {
            "rank_score": (
                f"{W_LEAD}*lead_rate + {W_HIT2X}*hit2x_rate - {W_DUMP}*dump_rate "
                f"+ {W_HUNT_OVERLAP}*hunt_overlap - {W_CLUSTER}*cluster_penalty"
            ),
            "hit2x_multiple": HIT2X_MULT,
            "dump_proxy_below_multiple": DUMP_MULT_MAX,
            "min_outcomes_for_hit_term": MIN_OUTCOMES_FOR_HIT_TERM,
            "cluster_min_traders_per_mint_hour": cluster_min,
        },
        "traders": ranked,
        "below_min_buys": below_floor,
        "note": "Learn-only FOMO trader scorecard. Does not arm or open fills.",
    }


def top_traders_snippet(session: Session, *, limit: int = 5) -> list[dict[str, Any]]:
    card = build_trader_scorecard(session)
    out: list[dict[str, Any]] = []
    for row in card["traders"][: max(1, min(int(limit), 20))]:
        out.append(
            {
                "rank": row.get("rank"),
                "trader": row.get("trader"),
                "trader_wallet": row.get("trader_wallet"),
                "user_id": row.get("user_id"),
                "n_buys": row.get("n_buys"),
                "lead_rate": row.get("lead_rate"),
                "rank_score": row.get("rank_score"),
            }
        )
    return out


def trader_scorecard_detail(
    session: Session,
    *,
    trader: str = "",
    user_id: str = "",
    days: int | None = None,
) -> dict[str, Any] | None:
    card = build_trader_scorecard(session, days=days, min_buys=1)
    handle = trader.strip().lstrip("@").lower()
    uid = user_id.strip()
    for pool in (card["traders"], card["below_min_buys"]):
        for row in pool:
            if uid and (row.get("user_id") or "") == uid:
                return row
            if handle and (row.get("trader") or "").strip().lstrip("@").lower() == handle:
                return row
            key = row.get("trader_key") or ""
            if uid and key == f"uid:{uid}":
                return row
            if handle and key == f"handle:{handle}":
                return row
    return None
