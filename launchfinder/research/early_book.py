"""Early book trajectory from Snapshots / TapeBars — Learn/shadow only.

Promotes SI-cohort shapes (rising_book_15m, dump_on_volume, mcap/liq ratios)
into reusable helpers. Does not grow FEATURE_NAMES. Does not open fills.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..models import Snapshot, TapeBar, Token

# FOMO catch already-hot book (Super Inu first sight ~$1.7M / $2.1M vol).
FOMO_LATE_HOT_MCAP = 500_000.0
FOMO_LATE_HOT_VOL = 500_000.0
RISING_MCAP_MIN = 1.5
RISING_LIQ_MIN = 1.1


def _aware(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _ratio(a: float, b: float) -> float | None:
    if a is None or b is None:
        return None
    if float(b) <= 0:
        return None
    return float(a) / float(b)


def snap_pair_trajectory(
    session: Session,
    token_id: int,
) -> dict[str, Any]:
    """Best-effort t0 → early live → t15m from Snapshot rows."""
    rows = (
        session.query(Snapshot)
        .filter(Snapshot.token_id == token_id)
        .order_by(Snapshot.taken_at.asc())
        .limit(40)
        .all()
    )
    by_kind: dict[str, Snapshot] = {}
    for row in rows:
        kind = (row.kind or "live").lower()
        if kind not in by_kind:
            by_kind[kind] = row
    t0 = by_kind.get("t0")
    t15 = by_kind.get("t15m")
    # First live after t0 as "early"
    early = None
    t0_at = _aware(t0.taken_at) if t0 else None
    for row in rows:
        if (row.kind or "").lower() == "t0":
            continue
        at = _aware(row.taken_at)
        if t0_at and at and at <= t0_at:
            continue
        if t0_at and at and (at - t0_at) <= timedelta(minutes=20):
            early = row
            break
    if early is None:
        for row in rows:
            if (row.kind or "").lower() == "live":
                early = row
                break

    def _pack(s: Snapshot | None) -> dict[str, float]:
        if s is None:
            return {}
        return {
            "mcap": float(s.mcap_usd or 0.0),
            "liq": float(s.liquidity_usd or 0.0),
            "vol_h1": float(s.volume_h1 or 0.0),
            "at": (_aware(s.taken_at).isoformat() if _aware(s.taken_at) else ""),
        }

    t0p, ep, t15p = _pack(t0), _pack(early), _pack(t15)
    mcap_ratio = _ratio(ep.get("mcap", 0), t0p.get("mcap", 0)) if t0p and ep else None
    liq_ratio = _ratio(ep.get("liq", 0), t0p.get("liq", 0)) if t0p and ep and t0p.get("liq", 0) > 0 else None
    dump = 0.0
    if mcap_ratio is not None and ep.get("vol_h1", 0) > 10_000 and mcap_ratio < 0.7:
        dump = 1.0
    rising = 0.0
    if mcap_ratio is not None and mcap_ratio >= RISING_MCAP_MIN:
        if liq_ratio is None or liq_ratio >= RISING_LIQ_MIN or ep.get("liq", 0) >= 15_000:
            rising = 1.0
    return {
        "t0": t0p,
        "early": ep,
        "t15m": t15p,
        "mcap_t0_to_early_ratio": mcap_ratio,
        "liq_t0_to_early_ratio": liq_ratio,
        "dump_on_volume": dump,
        "rising_book_15m": rising,
        "has_honest_early_traj": 1.0 if (t0p and ep and mcap_ratio is not None) else 0.0,
    }


def tape_minute_trajectory(
    session: Session,
    chain: str,
    mint: str,
    *,
    minutes: int = 60,
) -> list[dict[str, Any]]:
    """First ``minutes`` of TapeBar prints after first bar."""
    bars = (
        session.query(TapeBar)
        .filter(TapeBar.chain == chain, TapeBar.mint == mint)
        .order_by(TapeBar.minute.asc())
        .limit(max(1, int(minutes)) + 5)
        .all()
    )
    if not bars:
        return []
    t0 = _aware(bars[0].minute)
    out: list[dict[str, Any]] = []
    for bar in bars:
        at = _aware(bar.minute)
        if t0 is None or at is None:
            continue
        age_m = (at - t0).total_seconds() / 60.0
        if age_m > float(minutes):
            break
        out.append(
            {
                "age_min": round(age_m, 2),
                "minute": at.isoformat(),
                "mcap": float(bar.mcap_usd or 0.0),
                "liq": float(bar.liquidity_usd or 0.0),
                "vol_h1": float(bar.volume_h1 or 0.0),
                "holders": int(bar.holders or 0),
            }
        )
    return out


def fomo_late_hot(*, mcap: float, vol_h1: float, top10_pct: float | None = None) -> float:
    """Already-organic mega-book at first sight (Super Inu FOMO catch)."""
    if float(mcap or 0) < FOMO_LATE_HOT_MCAP:
        return 0.0
    if float(vol_h1 or 0) < FOMO_LATE_HOT_VOL:
        return 0.0
    if top10_pct is not None and float(top10_pct) > 50:
        return 0.0
    return 1.0


def early_book_shape(
    session: Session,
    token: Token,
    *,
    top10_pct: float | None = None,
    entry_mcap: float | None = None,
    entry_vol_h1: float | None = None,
) -> dict[str, Any]:
    """Side-key bundle for Learn/shadow — never a fill gate."""
    traj = snap_pair_trajectory(session, token.id)
    tape = tape_minute_trajectory(session, token.chain, token.mint, minutes=60)
    mcap0 = float(entry_mcap or 0.0) or float((traj.get("t0") or {}).get("mcap") or 0.0)
    vol0 = float(entry_vol_h1 or 0.0) or float((traj.get("t0") or {}).get("vol_h1") or 0.0)
    late_hot = fomo_late_hot(mcap=mcap0, vol_h1=vol0, top10_pct=top10_pct)
    rising = float(traj.get("rising_book_15m") or 0.0)
    if late_hot and not rising:
        rising = 1.0  # FOMO late-hot counts as rising-book equivalent for ranking
    # Tape 15m ratio when snaps thin
    bar_ratio = None
    if len(tape) >= 2:
        first = tape[0]["mcap"]
        at15 = next((b for b in tape if b["age_min"] >= 14.0), tape[-1])
        bar_ratio = _ratio(at15["mcap"], first)
        if rising < 1 and bar_ratio is not None and bar_ratio >= RISING_MCAP_MIN:
            rising = 1.0
    return {
        "mcap_t0_to_early_ratio": traj.get("mcap_t0_to_early_ratio"),
        "liq_t0_to_early_ratio": traj.get("liq_t0_to_early_ratio"),
        "dump_on_volume": float(traj.get("dump_on_volume") or 0.0),
        "rising_book_15m": rising,
        "fomo_late_hot": late_hot,
        "has_honest_early_traj": float(traj.get("has_honest_early_traj") or 0.0),
        "bar_mcap_15m_ratio": bar_ratio,
        "top10_at_entry": top10_pct,
        "n_tape_bars_60m": len(tape),
        "paper_only": True,
        "side_key": "early_book_shape",
    }
