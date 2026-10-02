"""Phase 2: a Live model trained on the tape ~15 minutes after entry.

Samples are cut from one-minute ``tape_bars`` once an entry decision is
old enough, stored compactly in ``live_samples`` so history outlives the
48h bar window. The label is forward from the sample: did the book reach
2x the t+15 mcap with a live pool. The exit test in docs/ROADMAP.md is
whether this beats Entry alone on the same validation slice.

Once a Live artifact is promoted, Hunt Live is that number (tape heuristic
still wins on a dump/recap). Sol paper can open on the first-sight watch
line when Live is healthy. v86 buy-on-sight is first-sight hi (0.14).

Runner head (stack-v107): every Live fit also writes a ``live_runner``
artifact on the same rows with the runner label (sellable peak >= 5x the
t+15 print, or still >= 2x at the 6h print). It is shadow only: never
promoted, never read by paper or the card. The 2x head's own top decile
is scored against that label too (``hit5x_top_decile``) so the Board can
say whether the runners are already inside the 2x ranking.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from ..chains import normalize_chain
from ..models import Decision, LiveSample, ModelArtifact, Outcome, Snapshot, TapeBar, utcnow
from .batch_fit import (
    MIN_POSITIVES,
    MIN_VALID,
    PROMOTE_AUC_SLACK,
    PROMOTE_TOP_DECILE_SLACK,
    apply_calibration,
    fit_logistic,
    isotonic_fit,
    latest_promoted,
    metrics,
    reset_artifact_cache,
    time_split,
)

log = logging.getLogger("launchfinder.live_fit")

SAMPLE_AT_MIN = 15.0
# Decisions this old are eligible. The upper edge is wide because the t15m
# snapshot (the fallback print when the Hunt tape never reached the mint)
# can land late; ``minutes_after`` on the sample records the true gap.
SAMPLE_WINDOW_MIN = (12.0, 60.0)
# Fallback print: the t15m snapshot, if it was taken inside this gap.
SNAP_FALLBACK_MIN = (10.0, 60.0)
LIVE_MIN_ROWS = 300
LIVE_DEAD_LIQ = 800.0
LIVE_HIT = 2.0
# Runner head. Same rows and inputs as the 2x Live fit, different question:
# did the book run (sellable peak >= 5x the t+15 print) or hold (still
# >= 2x at the tracker's 6h print, so a wick that came straight back does
# not count). Shadow only — written as its own artifact, never promoted,
# never read by paper or the Hunt card. Entry p_good is untouched.
RUNNER_HIT = 5.0
RUNNER_HOLD_MULT = LIVE_HIT
RUNNER_KIND = "live_runner"
# Sol paper: first-sight hi (0.14) is buy-on-sight. Watch-line cards
# (Entry >= 0.10) may fill when the promoted Live model is this high.
# 0.70 never fired (live Hunt clusters ~0.53); 0.50 is the reachable bar.
LIVE_PAPER_HI = 0.50
# Close an open fill when Live dies after a real run (peak >= 1.5×).
LIVE_EXIT_P = 0.35
LIVE_EXIT_GIVEBACK = 0.50
LIVE_EXIT_RAN = 1.5
# Vendor kline / Bitquery trades need a close near entry to scale price → mcap
# from the frozen decision. A later first print cannot honestly back-solve t0.
VENDOR_ANCHOR_SLACK_MIN = 5.0
VENDOR_FILL_PER_CYCLE = 20
VENDOR_SOURCES = ("gmgn_kline", "bitquery_hist", "t15m+kline")
HISTORY_LOOKBACK_HOURS = 24.0 * 21.0

LIVE_FEATURES = [
    "entry_p",
    "log_mult",
    "drawdown",
    "runup",
    "liq_ratio",
    "holder_growth",
    "liq_n",
    "vol_n",
    "mcap_n",
    "holders_n",
]
# Two-point t15m snapshots have no path and no holders. Once a real path
# (tape, kline, or Bitquery) is large enough to fit on its own, those
# snapshots leave the training set. They still get sampled.
LIVE_PATH_SOURCES = frozenset({"tape", "t15m+kline", "bitquery_hist", "gmgn_kline"})


def live_fit_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """Drop two-point t15m rows once the path set can train alone."""
    path = [row for row in rows if str(row.get("source") or "tape") in LIVE_PATH_SOURCES]
    if len(path) >= LIVE_MIN_ROWS:
        return path, "path"
    return list(rows), "all"


def live_promote_ok(cand: dict[str, Any], baseline: dict[str, Any], prev: dict[str, Any] | None) -> tuple[bool, str]:
    """Promote only when Live beats Entry and does not give back rank.

    Top-decile uses the same slack as the entry fit. AUC slack alone let
    Robinhood Live promote while its top decile fell from ~44% to ~29%.
    """
    if int(cand.get("positives") or 0) < MIN_POSITIVES:
        return False, "too few validation positives"
    if not ((cand.get("auc") or 0.0) > (baseline.get("auc") or 0.0) and float(cand.get("brier") or 1.0) < float(baseline.get("brier") or 1.0)):
        return False, f"does not beat Entry alone (auc {cand.get('auc')} vs {baseline.get('auc')})"
    if prev:
        if (cand.get("auc") or 0.0) + PROMOTE_AUC_SLACK < (prev.get("auc") or 0.0):
            return False, "does not beat the promoted live model"
        if float(cand.get("brier") or 1.0) > float(prev.get("brier") or 1.0):
            return False, "does not beat the promoted live model"
        prev_top = float(prev.get("precision_top_decile") or 0.0)
        if float(cand.get("precision_top_decile") or 0.0) + PROMOTE_TOP_DECILE_SLACK < prev_top:
            return False, f"top-decile {cand.get('precision_top_decile')} under promoted {prev_top}"
    return True, "beats Entry alone and the incumbent"


def _aware(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _log_norm(value: float, scale: float) -> float:
    if value <= 0:
        return 0.0
    return min(1.0, math.log1p(value) / math.log1p(scale))


def _log_ratio(num: float, den: float, *, clip: float = 2.0) -> float:
    if num <= 0 or den <= 0:
        return 0.0
    return max(-clip, min(clip, math.log(num / den))) / clip


def live_features(
    *,
    entry_p: float,
    entry_mcap: float,
    entry_liq: float,
    entry_holders: int,
    mcap: float,
    liq: float,
    vol_h1: float,
    holders: int,
    peak_before: float,
    trough_before: float,
) -> dict[str, float]:
    mult = mcap / entry_mcap if entry_mcap > 0 and mcap > 0 else 0.0
    return {
        "entry_p": float(entry_p or 0.0),
        "log_mult": _log_ratio(mcap, entry_mcap),
        "drawdown": (trough_before / entry_mcap - 1.0) if entry_mcap > 0 and trough_before > 0 else 0.0,
        "runup": _log_ratio(peak_before, entry_mcap),
        "liq_ratio": _log_ratio(liq, entry_liq),
        "holder_growth": _log_ratio(float(holders), float(entry_holders)),
        "liq_n": _log_norm(liq, 80_000),
        "vol_n": _log_norm(vol_h1, 50_000),
        "mcap_n": _log_norm(mcap, 500_000),
        "holders_n": _log_norm(float(holders), 2_000),
        "_mult": mult,
    }


def live_vector(features: dict[str, float]) -> list[float]:
    return [float(features.get(name, 0.0) or 0.0) for name in LIVE_FEATURES]


def print_from_candles(
    candles: list[dict[str, Any]],
    *,
    entry_mcap: float,
    entry_at: datetime,
    sample_at: datetime | None = None,
) -> dict[str, Any] | None:
    """Scale time-correct OHLCV to mcap using the frozen entry print.

    GMGN kline and Bitquery trades carry USD price + volume, not holders
    and not pool liquidity. A candle more than ``VENDOR_ANCHOR_SLACK_MIN``
    from entry cannot be the scale — that would invent t0.
    """
    if float(entry_mcap or 0.0) <= 0:
        return None
    parsed: list[dict[str, Any]] = []
    for row in candles or []:
        if not isinstance(row, dict):
            continue
        raw_ts = row.get("time")
        if isinstance(raw_ts, datetime):
            ts = _aware(raw_ts)
        else:
            ts = None
        try:
            close = float(row.get("close") or 0.0)
        except (TypeError, ValueError):
            close = 0.0
        if ts is None or close <= 0:
            continue
        try:
            high = float(row.get("high") or close)
            low = float(row.get("low") or close)
            vol = float(row.get("volume") or 0.0)
        except (TypeError, ValueError):
            high, low, vol = close, close, 0.0
        parsed.append({"time": ts, "close": close, "high": high if high > 0 else close, "low": low if low > 0 else close, "volume": max(0.0, vol)})
    if not parsed:
        return None
    parsed.sort(key=lambda c: c["time"])
    entry_at = _aware(entry_at)
    if entry_at is None:
        return None
    slack = timedelta(minutes=VENDOR_ANCHOR_SLACK_MIN)
    near = [c for c in parsed if abs((c["time"] - entry_at).total_seconds()) <= slack.total_seconds()]
    if not near:
        return None
    anchor = min(near, key=lambda c: abs((c["time"] - entry_at).total_seconds()))
    px0 = float(anchor["close"])
    if px0 <= 0:
        return None
    scale = float(entry_mcap) / px0
    target = _aware(sample_at) or (entry_at + timedelta(minutes=SAMPLE_AT_MIN))
    pick = next((c for c in parsed if c["time"] >= target), None)
    if pick is None:
        after = [c for c in parsed if c["time"] >= entry_at]
        if not after:
            return None
        pick = after[-1]
    window = [c for c in parsed if entry_at <= c["time"] <= pick["time"]] or [pick]
    closes = [float(c["close"]) * scale for c in window]
    highs = [float(c["high"]) * scale for c in window]
    lows = [float(c["low"]) * scale for c in window if float(c["low"]) > 0]
    vol_lo = pick["time"] - timedelta(hours=1)
    vol_h1 = sum(float(c["volume"]) for c in parsed if vol_lo < c["time"] <= pick["time"])
    return {
        "mcap": float(pick["close"]) * scale,
        "liq": 0.0,
        "vol_h1": float(vol_h1),
        "holders": 0,
        "peak": max(highs) if highs else max(closes),
        "trough": min(lows) if lows else min(closes),
        "at": pick["time"],
        "prints": closes,
    }


# --------------------------------------------------------------------------
# Sampling
# --------------------------------------------------------------------------


def _observed_holders(bars: list) -> int:
    """Last non-zero holder count on bars we already wrote.

    A later minute can store 0 because the holder refresh had not landed
    yet. That is a missing print, not a book with no wallets. Do not read
    the current ``Research.holder_count`` — that is now, not t+15.
    """
    last = 0
    for bar in bars or []:
        n = int(getattr(bar, "holders", 0) or 0)
        if n > 0:
            last = n
    return last


def _restamp_sample_holders(sample: LiveSample, holders: int, decision: Decision | None) -> None:
    """Write an observed holder count back onto a sample and its features.

    Keeps the stored source. Does not move mcap, liquidity, or entry p.
    """
    src = _sample_source(sample) or "t15m"
    entry_liq = float(decision.liq or 0.0) if decision is not None else 0.0
    entry_holders = int(decision.holders or 0) if decision is not None else 0
    feats = live_features(
        entry_p=float(sample.entry_p or 0.0),
        entry_mcap=float(sample.entry_mcap or 0.0),
        entry_liq=entry_liq,
        entry_holders=entry_holders,
        mcap=float(sample.mcap_usd or 0.0),
        liq=float(sample.liquidity_usd or 0.0),
        vol_h1=float(sample.volume_h1 or 0.0),
        holders=int(holders),
        peak_before=float(sample.peak_before or 0.0),
        trough_before=float(sample.trough_before or 0.0),
    )
    sample.holders = int(holders)
    sample.features_json = json.dumps({**{k: v for k, v in feats.items() if not k.startswith("_")}, "source": src})


def _t15m_snapshot(session: Session, token_id: int, at: datetime) -> Snapshot | None:
    """The outcome tracker's t15m print for this token, if it landed 10–60
    minutes after the decision with a real mcap. Most first sights never
    reach the Hunt tape (80-card board, batch cap), but every non-backfill
    token gets this snapshot, so the Live model is not starved of rows."""
    lo, hi = SNAP_FALLBACK_MIN
    return (
        session.query(Snapshot)
        .filter(
            Snapshot.token_id == token_id,
            Snapshot.kind == "t15m",
            Snapshot.mcap_usd > 0,
            Snapshot.taken_at >= at + timedelta(minutes=lo),
            Snapshot.taken_at <= at + timedelta(minutes=hi),
        )
        .order_by(Snapshot.taken_at.asc())
        .first()
    )


def sample_live(
    session: Session,
    chain: str,
    *,
    now: datetime | None = None,
    limit: int = 200,
    sources: tuple[str, ...] = ("live",),
    lookback_hours: float | None = None,
) -> int:
    """Cut a t+15 sample for entry decisions that just crossed the window.

    ``lookback_hours`` (history backfill) walks older decisions and accepts
    ``seed_t0`` so the 12k/22k t15m archive can train Live this week.
    """
    chain = normalize_chain(chain)
    now = now or utcnow()
    lo, hi = SAMPLE_WINDOW_MIN
    if lookback_hours is not None:
        oldest = now - timedelta(hours=float(lookback_hours))
        newest = now - timedelta(minutes=lo)
    else:
        oldest = now - timedelta(minutes=hi)
        newest = now - timedelta(minutes=lo)
    have_q = session.query(LiveSample.mint).filter(LiveSample.chain == chain)
    if lookback_hours is None:
        have_q = have_q.filter(LiveSample.at >= oldest - timedelta(hours=1))
    have = {m for (m,) in have_q.all()}
    decisions = (
        session.query(Decision)
        .filter(Decision.chain == chain, Decision.kind == "entry", Decision.source.in_(sources))
        .filter(Decision.at >= oldest, Decision.at <= newest)
        .order_by(Decision.at.asc())
        .limit(limit)
        .all()
    )
    wrote = 0
    for d in decisions:
        if d.mint in have or float(d.entry_mcap or 0.0) <= 0:
            continue
        at = _aware(d.at)
        bars = (
            session.query(TapeBar)
            .filter(TapeBar.chain == chain, TapeBar.mint == d.mint, TapeBar.minute >= at, TapeBar.minute <= at + timedelta(minutes=hi))
            .order_by(TapeBar.minute.asc())
            .all()
        )
        target = at + timedelta(minutes=SAMPLE_AT_MIN)
        pick = next((b for b in bars if _aware(b.minute) >= target and float(b.mcap_usd or 0.0) > 0), None)
        if pick is not None:
            pick_minute = _aware(pick.minute)
            before = [b for b in bars if _aware(b.minute) <= pick_minute and float(b.mcap_usd or 0.0) > 0]
            prints = [float(b.mcap_usd) for b in before]
            # The t+15 bar can be 0 holders while an earlier bar in the
            # same window already recorded the book.
            holders = int(pick.holders or 0) or _observed_holders(before)
            print_ = {"mcap": float(pick.mcap_usd or 0.0), "liq": float(pick.liquidity_usd or 0.0), "vol_h1": float(pick.volume_h1 or 0.0), "holders": holders, "source": "tape"}
        else:
            snap = _t15m_snapshot(session, d.token_id, at)
            if snap is None:
                continue
            pick_minute = _aware(snap.taken_at)
            # Bars before the snapshot still count for run-up / drawdown
            # and for a holder count we wrote. No bars: the two ends only.
            prior = [b for b in bars if _aware(b.minute) is not None and _aware(b.minute) <= pick_minute and float(b.mcap_usd or 0.0) > 0]
            prints = [float(d.entry_mcap), *[float(b.mcap_usd) for b in prior], float(snap.mcap_usd)]
            print_ = {"mcap": float(snap.mcap_usd or 0.0), "liq": float(snap.liquidity_usd or 0.0), "vol_h1": float(snap.volume_h1 or 0.0), "holders": _observed_holders(prior), "source": "t15m"}
        if _add_live_sample(session, d, chain, pick_minute, print_, prints):
            have.add(d.mint)
            wrote += 1
    session.flush()
    return wrote


def _add_live_sample(
    session: Session,
    d: Decision,
    chain: str,
    pick_minute: datetime | None,
    print_: dict[str, Any],
    prints: list[float],
) -> bool:
    if pick_minute is None or float(print_.get("mcap") or 0.0) <= 0:
        return False
    at = _aware(d.at)
    if at is None:
        return False
    usable = [float(p) for p in prints if p and p > 0]
    if not usable:
        usable = [float(print_["mcap"])]
    peak = max(usable)
    trough = min(usable)
    holders = int(print_.get("holders") or 0)
    feats = live_features(
        entry_p=float(d.entry_p or 0.0),
        entry_mcap=float(d.entry_mcap or 0.0),
        entry_liq=float(d.liq or 0.0),
        entry_holders=int(d.holders or 0),
        mcap=float(print_["mcap"]),
        liq=float(print_.get("liq") or 0.0),
        vol_h1=float(print_.get("vol_h1") or 0.0),
        holders=holders,
        peak_before=peak,
        trough_before=trough,
    )
    session.add(
        LiveSample(
            chain=chain,
            mint=d.mint,
            token_id=d.token_id,
            decision_id=d.id,
            at=pick_minute,
            minutes_after=round((pick_minute - at).total_seconds() / 60.0, 1),
            entry_p=float(d.entry_p or 0.0),
            entry_mcap=float(d.entry_mcap or 0.0),
            mcap_usd=float(print_["mcap"]),
            liquidity_usd=float(print_.get("liq") or 0.0),
            volume_h1=float(print_.get("vol_h1") or 0.0),
            holders=holders,
            peak_before=peak,
            trough_before=trough,
            features_json=json.dumps({**{k: v for k, v in feats.items() if not k.startswith("_")}, "source": print_.get("source") or "tape"}),
        )
    )
    return True


def _sample_source(sample: LiveSample | None) -> str:
    if sample is None:
        return ""
    try:
        return str(json.loads(sample.features_json or "{}").get("source") or "")
    except json.JSONDecodeError:
        return ""


def vendor_candidates(
    session: Session,
    chain: str,
    *,
    now: datetime | None = None,
    limit: int = VENDOR_FILL_PER_CYCLE,
) -> list[Decision]:
    """Decisions in the 21d window that still need a vendor path.

    Tape-sourced samples are ours — leave them. ``t15m`` rows can be
    enriched (2-point path → kline path). Already-vendor rows are done.
    """
    chain = normalize_chain(chain)
    now = now or utcnow()
    oldest = now - timedelta(hours=HISTORY_LOOKBACK_HOURS)
    newest = now - timedelta(minutes=SAMPLE_WINDOW_MIN[0])
    decisions = (
        session.query(Decision)
        .filter(Decision.chain == chain, Decision.kind == "entry", Decision.source.in_(("live", "seed_t0")))
        .filter(Decision.at >= oldest, Decision.at <= newest, Decision.entry_mcap > 0)
        .order_by(Decision.at.desc())
        .all()
    )
    have = {
        s.mint: _sample_source(s)
        for s in session.query(LiveSample).filter(LiveSample.chain == chain).all()
    }
    out: list[Decision] = []
    for d in decisions:
        src = have.get(d.mint, "")
        if src == "tape" or src in VENDOR_SOURCES:
            continue
        out.append(d)
        if len(out) >= limit:
            break
    return out


def apply_vendor_candles(
    session: Session,
    decision: Decision,
    candles: list[dict[str, Any]],
    *,
    source: str = "gmgn_kline",
) -> bool:
    """Write or enrich a LiveSample from time-correct candles.

    Never stamps holders (Helius DAS is now-only). Liquidity comes from
    our t15m snapshot when we have one, never from the vendor book.
    Does not rewrite a tape-sourced sample or ``research.p_good``.
    """
    from ..research.holders import historical_holder_count_at

    chain = normalize_chain(decision.chain)
    at = _aware(decision.at)
    if at is None or float(decision.entry_mcap or 0.0) <= 0:
        return False
    existing = (
        session.query(LiveSample)
        .filter(LiveSample.chain == chain, LiveSample.mint == decision.mint)
        .one_or_none()
    )
    if _sample_source(existing) == "tape":
        return False
    vendor = print_from_candles(candles, entry_mcap=float(decision.entry_mcap), entry_at=at)
    if vendor is None:
        return False
    # Honest no-op: DAS is a current snapshot and must not land here.
    # A count already stored from a tape bar stays. Zero does not wipe it.
    looked_up = historical_holder_count_at(decision.mint, at) or 0
    holders = int(looked_up or 0)
    if holders <= 0 and existing is not None and int(existing.holders or 0) > 0:
        holders = int(existing.holders or 0)
    snap = _t15m_snapshot(session, decision.token_id, at)
    if existing is not None:
        peak = max(float(existing.peak_before or 0.0), float(vendor["peak"]), float(existing.mcap_usd or 0.0), float(existing.entry_mcap or 0.0))
        troughs = [x for x in (float(existing.trough_before or 0.0), float(vendor["trough"]), float(existing.mcap_usd or 0.0)) if x > 0]
        trough = min(troughs) if troughs else float(vendor["trough"])
        existing.volume_h1 = float(vendor["vol_h1"] or existing.volume_h1 or 0.0)
        existing.peak_before = peak
        existing.trough_before = trough
        existing.holders = holders
        feats = live_features(
            entry_p=float(existing.entry_p or decision.entry_p or 0.0),
            entry_mcap=float(existing.entry_mcap or decision.entry_mcap or 0.0),
            entry_liq=float(decision.liq or 0.0),
            entry_holders=int(decision.holders or 0),
            mcap=float(existing.mcap_usd or 0.0),
            liq=float(existing.liquidity_usd or 0.0),
            vol_h1=float(existing.volume_h1 or 0.0),
            holders=holders,
            peak_before=peak,
            trough_before=trough,
        )
        existing.features_json = json.dumps({**{k: v for k, v in feats.items() if not k.startswith("_")}, "source": "t15m+kline"})
        session.flush()
        return True
    mcap = float(snap.mcap_usd) if snap is not None and float(snap.mcap_usd or 0.0) > 0 else float(vendor["mcap"])
    liq = float(snap.liquidity_usd or 0.0) if snap is not None else 0.0
    pick_minute = _aware(snap.taken_at) if snap is not None else vendor["at"]
    prints = [float(decision.entry_mcap), mcap, float(vendor["peak"]), float(vendor["trough"]), *vendor.get("prints", [])]
    src = "t15m+kline" if snap is not None else source
    ok = _add_live_sample(
        session,
        decision,
        chain,
        pick_minute,
        {"mcap": mcap, "liq": liq, "vol_h1": float(vendor["vol_h1"]), "holders": holders, "source": src},
        prints,
    )
    if ok:
        session.flush()
    return ok


async def fill_vendor_live_history(
    session: Session,
    chain: str,
    *,
    now: datetime | None = None,
    limit: int = VENDOR_FILL_PER_CYCLE,
) -> int:
    """GMGN kline first, Bitquery trades if the kline book is empty.

    Helius is not a historical holder warehouse — samples keep holders=0.
    Budget is ``VENDOR_FILL_PER_CYCLE`` per chain so trenches stay fed.
    """
    from ..ingest.bitquery import historical_trade_candles
    from ..research import gmgn as gmgn_mod

    chain = normalize_chain(chain)
    now = now or utcnow()
    wrote = 0
    tried = 0
    for d in vendor_candidates(session, chain, now=now, limit=limit):
        at = _aware(d.at)
        if at is None:
            continue
        tried += 1
        until = at + timedelta(minutes=SAMPLE_WINDOW_MIN[1])
        candles: list[dict[str, Any]] = []
        source = "gmgn_kline"
        if gmgn_mod.gmgn_available():
            try:
                candles = await gmgn_mod.token_kline(d.mint, chain=chain, from_ts=at, to_ts=until)
            except Exception:
                log.debug("gmgn kline failed %s", d.mint[:12], exc_info=True)
                candles = []
        if not candles:
            try:
                candles = await historical_trade_candles(d.mint, chain=chain, from_ts=at, to_ts=until)
            except Exception:
                log.debug("bitquery hist failed %s", d.mint[:12], exc_info=True)
                candles = []
            source = "bitquery_hist"
        if apply_vendor_candles(session, d, candles, source=source):
            wrote += 1
    if wrote:
        session.flush()
    log.info("vendor live history %s wrote %s of %s", chain, wrote, tried)
    return wrote


def repair_live_sample_holders(session: Session, chain: str, *, now: datetime | None = None, limit: int = 200) -> int:
    """Fill holders=0 samples from tape bars still inside the 48h window.

    Does not read the live holder count. Does not rewrite a sample that
    already has wallets. Bars older than the tape retention are gone, so
    those rows stay 0.
    """
    chain = normalize_chain(chain)
    now = now or utcnow()
    cutoff = now - timedelta(hours=48)
    rows = (
        session.query(LiveSample)
        .filter(LiveSample.chain == chain, LiveSample.holders == 0, LiveSample.at >= cutoff)
        .order_by(LiveSample.at.desc())
        .limit(int(limit))
        .all()
    )
    wrote = 0
    for sample in rows:
        at = _aware(sample.at)
        if at is None:
            continue
        span = float(sample.minutes_after or SAMPLE_WINDOW_MIN[1])
        start = at - timedelta(minutes=max(span, SAMPLE_AT_MIN))
        bars = (
            session.query(TapeBar)
            .filter(TapeBar.chain == chain, TapeBar.mint == sample.mint, TapeBar.minute >= start, TapeBar.minute <= at)
            .order_by(TapeBar.minute.asc())
            .all()
        )
        holders = _observed_holders(bars)
        if holders <= 0:
            continue
        decision = session.get(Decision, sample.decision_id) if sample.decision_id else None
        _restamp_sample_holders(sample, holders, decision)
        wrote += 1
    if wrote:
        session.flush()
    return wrote


def sample_live_history(session: Session, chain: str, *, now: datetime | None = None, limit: int = 2500) -> int:
    """Backfill LiveSamples from the t15m archive (seed + live decisions)."""
    return sample_live(
        session,
        chain,
        now=now,
        limit=limit,
        sources=("live", "seed_t0"),
        lookback_hours=HISTORY_LOOKBACK_HOURS,
    )


# --------------------------------------------------------------------------
# Labels + fit
# --------------------------------------------------------------------------


def post_sample_peaks(session: Session, samples: list[LiveSample]) -> dict[int, tuple[float, int]]:
    """Highest sellable print (and print count) after each sample, inside the
    horizon — snapshots and tape bars only, the same evidence the entry
    judge uses. One grouped query per table per 5k samples."""
    from ..ledger import LEDGER_SELLABLE_LIQ, RESOLVE_HOURS, _shift

    out: dict[int, tuple[float, int]] = {}
    ids = [s.id for s in samples if s.id is not None]
    if not ids:
        return out
    try:
        postgres = session.get_bind().dialect.name == "postgresql"
    except Exception:
        postgres = False
    until = _shift(LiveSample.at, timedelta(hours=RESOLVE_HOURS), postgres)
    for i in range(0, len(ids), 5000):
        chunk = ids[i : i + 5000]
        for model, tcol in ((Snapshot, Snapshot.taken_at), (TapeBar, TapeBar.minute)):
            sellable = func.max(case((model.liquidity_usd >= LEDGER_SELLABLE_LIQ, model.mcap_usd), else_=0.0))
            agg = (
                session.query(LiveSample.id, sellable, func.count(model.id))
                .join(model, model.token_id == LiveSample.token_id)
                .filter(LiveSample.id.in_(chunk), model.mcap_usd > 0, tcol > LiveSample.at, tcol <= until)
                .group_by(LiveSample.id)
                .all()
            )
            for sid, peak, n in agg:
                p0, n0 = out.get(sid, (0.0, 0))
                out[sid] = (max(p0, float(peak or 0.0)), n0 + int(n or 0))
    return out


def _judged_peak(
    sample: LiveSample,
    outcome: Outcome | None,
    *,
    now: datetime | None = None,
    peak_after: tuple[float, int] | None = None,
) -> tuple[float, float, bool] | None:
    """(base, sellable peak, dead) for a judged sample; None while open.

    The peak is a print we saw after the sample — a snapshot or tape bar on
    a sellable pool (``peak_after``) or the outcome's last Dex look on one.
    ``outcome.max_mcap`` is a lifetime high with no timestamp and never
    vouches (v80: the entry judge dropped it for the same reason — it was
    crediting year-old ATHs to flat tapes). Without ``peak_after`` the
    caller did not look; only the last look and the tracker prints count.
    """
    from ..ledger import LEDGER_SELLABLE_LIQ, RESOLVE_HOURS

    if outcome is None:
        return None
    now = now or utcnow()
    at = _aware(sample.at)
    if outcome.label is None and (now - at).total_seconds() / 3600.0 < RESOLVE_HOURS:
        return None
    base = float(sample.mcap_usd or 0.0)
    if base <= 0:
        return None
    last_liq = float(outcome.last_liq or 0.0)
    dead = 0 < last_liq < LIVE_DEAD_LIQ
    peak = float(peak_after[0]) if peak_after else 0.0
    if last_liq >= LEDGER_SELLABLE_LIQ:
        peak = max(peak, float(outcome.last_mcap or 0.0))
    return base, peak, dead


def live_label(sample: LiveSample, outcome: Outcome | None, *, now: datetime | None = None, peak_after: tuple[float, int] | None = None) -> int | None:
    """Forward 2x from the t+15 print, live pool at judgement. None while open."""
    judged = _judged_peak(sample, outcome, now=now, peak_after=peak_after)
    if judged is None:
        return None
    base, peak, dead = judged
    if dead:
        return 0
    t24 = float(outcome.t24h_mcap or 0.0)
    if peak >= LIVE_HIT * base or t24 >= LIVE_HIT * base:
        return 1
    return 0


def live_label_runner(sample: LiveSample, outcome: Outcome | None, *, now: datetime | None = None, peak_after: tuple[float, int] | None = None) -> int | None:
    """Did it run: sellable peak >= 5x the t+15 print, or still >= 2x at the
    tracker's 6h print. Same evidence and gates as ``live_label``; a dead
    pool at judgement is 0 whatever it printed on the way."""
    judged = _judged_peak(sample, outcome, now=now, peak_after=peak_after)
    if judged is None:
        return None
    base, peak, dead = judged
    if dead:
        return 0
    t6 = float(outcome.t6h_mcap or 0.0)
    if peak >= RUNNER_HIT * base or t6 >= RUNNER_HOLD_MULT * base:
        return 1
    return 0


def live_rows(session: Session, chain: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    chain = normalize_chain(chain)
    now = now or utcnow()
    rows = (
        session.query(LiveSample, Outcome)
        .outerjoin(Outcome, Outcome.token_id == LiveSample.token_id)
        .filter(LiveSample.chain == chain)
        .order_by(LiveSample.at.asc())
        .all()
    )
    peaks = post_sample_peaks(session, [s for s, _o in rows])
    out = []
    for s, o in rows:
        peak_after = peaks.get(s.id, (0.0, 0))
        y = live_label(s, o, now=now, peak_after=peak_after)
        if y is None:
            continue
        try:
            feats = json.loads(s.features_json or "{}")
        except json.JSONDecodeError:
            continue
        y_run = live_label_runner(s, o, now=now, peak_after=peak_after)
        out.append(
            {
                "at": s.at,
                "x": live_vector(feats),
                "y": y,
                "y_run": int(y_run or 0),
                "entry_p": float(s.entry_p or 0.0),
                "source": str(feats.get("source") or "tape"),
            }
        )
    return out


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def fit_live_model(session: Session, chain: str, *, now: datetime | None = None, promote: bool = True) -> dict[str, Any]:
    chain = normalize_chain(chain)
    now = now or utcnow()
    rows, mix = live_fit_rows(live_rows(session, chain, now=now))
    if len(rows) < LIVE_MIN_ROWS:
        return {"chain": chain, "fitted": False, "reason": f"{len(rows)} live rows < {LIVE_MIN_ROWS}"}
    train, valid = time_split(rows)
    if len(valid) < MIN_VALID or sum(r["y"] for r in train) < MIN_POSITIVES:
        return {"chain": chain, "fitted": False, "reason": "not enough validation rows or positives"}
    Xt = np.array([r["x"] for r in train], dtype=float)
    yt = np.array([r["y"] for r in train], dtype=float)
    Xv = np.array([r["x"] for r in valid], dtype=float)
    yv = np.array([r["y"] for r in valid], dtype=float)
    w, b = fit_logistic(Xt, yt)
    raw_v = _sigmoid(Xv @ w + b)
    cal = isotonic_fit(raw_v, yv)
    cal_v = np.array([apply_calibration(cal, float(p)) for p in raw_v])
    cand = metrics(cal_v, yv)
    # Does the 2x head already find the runners? Runner rate inside its own
    # top decile against the slice base, on the same validation rows.
    yrun_v = np.array([int(r.get("y_run") or 0) for r in valid], dtype=float)
    top = np.argsort(-cal_v, kind="stable")[: max(1, len(cal_v) // 10)]
    cand["hit5x_top_decile"] = round(float(yrun_v[top].mean()), 4) if len(top) else None
    cand["runner_base_rate"] = round(float(yrun_v.mean()), 4) if len(yrun_v) else None
    # Baseline: Entry alone, isotonic-calibrated on the same slice so the
    # comparison is about information, not scale.
    ev = np.array([r["entry_p"] for r in valid], dtype=float)
    base_cal = isotonic_fit(ev, yv)
    baseline = metrics(np.array([apply_calibration(base_cal, float(p)) for p in ev]), yv)
    prev = latest_promoted(session, chain, "live")
    prev_metrics = {}
    if prev is not None:
        try:
            pw = json.loads(prev.weights_json or "{}")
            pcal = json.loads(prev.calibration_json or "[]")
            pv = _sigmoid(Xv @ np.array([float(pw.get(n, 0.0)) for n in LIVE_FEATURES]) + float(prev.bias))
            prev_metrics = metrics(np.array([apply_calibration(pcal, float(p)) for p in pv]), yv)
        except (json.JSONDecodeError, ValueError):
            prev_metrics = {}
    ok, why = live_promote_ok(cand, baseline, prev_metrics or None) if promote else (False, "promotion disabled")
    art = ModelArtifact(
        chain=chain,
        kind="live",
        created_at=now,
        version=(prev.version + 1) if prev else 1,
        weights_json=json.dumps({n: float(wi) for n, wi in zip(LIVE_FEATURES, w, strict=True)}),
        bias=b,
        calibration_json=json.dumps(cal),
        n_train=len(train),
        n_valid=len(valid),
        metrics_json=json.dumps({**cand, "entry_p_baseline": baseline, "previous_live": prev_metrics, "promote_reason": why, "live_only": True, "fit_rows": mix}),
        incumbent_json=json.dumps(baseline),
        promoted=bool(ok),
    )
    session.add(art)
    session.flush()
    if ok:
        reset_artifact_cache()
        log.info("promoted %s live model v%s: %s", chain, art.version, why)
    runner = _write_live_runner_shadow(session, chain, train, valid, now, mix=mix)
    return {
        "chain": chain,
        "fitted": True,
        "promoted": bool(ok),
        "reason": why,
        "candidate": cand,
        "entry_p_baseline": baseline,
        "n_train": len(train),
        "n_valid": len(valid),
        "version": art.version,
        "runner": runner,
    }


def _write_live_runner_shadow(
    session: Session,
    chain: str,
    train: list[dict[str, Any]],
    valid: list[dict[str, Any]],
    now: datetime,
    *,
    mix: str = "all",
) -> dict[str, Any] | None:
    """Fit the runner head on the same Live rows. Never promote it.

    Baselines are the 2x Live head and Entry alone, each isotonic-calibrated
    against the runner label on the same slice, so the card says whether a
    runner question needs its own model or the 2x head already answers it.
    """
    if not train or not valid or "y_run" not in train[0]:
        return None
    yt = np.array([int(r.get("y_run") or 0) for r in train], dtype=float)
    yv = np.array([int(r.get("y_run") or 0) for r in valid], dtype=float)
    if float(yt.sum()) < MIN_POSITIVES or float(yv.sum()) < MIN_POSITIVES:
        return {"fitted": False, "reason": f"runner positives {int(yt.sum())} train / {int(yv.sum())} valid < {MIN_POSITIVES}"}
    Xt = np.array([r["x"] for r in train], dtype=float)
    Xv = np.array([r["x"] for r in valid], dtype=float)
    w, b = fit_logistic(Xt, yt)
    raw_v = _sigmoid(Xv @ w + b)
    cal = isotonic_fit(raw_v, yv)
    cal_v = np.array([apply_calibration(cal, float(p)) for p in raw_v])
    cand = metrics(cal_v, yv)
    cand["base_rate"] = round(float(yv.mean()), 4)
    n_hi = int((cal_v >= 0.20).sum())
    cand["n_ge_020"] = n_hi
    cand["share_ge_020"] = round(n_hi / len(cal_v), 4) if len(cal_v) else 0.0
    cand["hit_ge_020"] = round(float(yv[cal_v >= 0.20].mean()), 4) if n_hi else None
    ev = np.array([r["entry_p"] for r in valid], dtype=float)
    entry_base = metrics(np.array([apply_calibration(isotonic_fit(ev, yv), float(p)) for p in ev]), yv)
    two_x = {}
    live_art = latest_promoted(session, chain, "live")
    if live_art is not None:
        try:
            lw = json.loads(live_art.weights_json or "{}")
            lv = _sigmoid(Xv @ np.array([float(lw.get(n, 0.0)) for n in LIVE_FEATURES]) + float(live_art.bias))
            two_x = metrics(np.array([apply_calibration(isotonic_fit(lv, yv), float(p)) for p in lv]), yv)
        except (json.JSONDecodeError, ValueError):
            two_x = {}
    prev = (
        session.query(ModelArtifact)
        .filter(ModelArtifact.chain == chain, ModelArtifact.kind == RUNNER_KIND)
        .order_by(ModelArtifact.id.desc())
        .first()
    )
    art = ModelArtifact(
        chain=chain,
        kind=RUNNER_KIND,
        created_at=now,
        version=(prev.version + 1) if prev else 1,
        weights_json=json.dumps({n: float(wi) for n, wi in zip(LIVE_FEATURES, w, strict=True)}),
        bias=float(b),
        calibration_json=json.dumps(cal),
        n_train=len(train),
        n_valid=len(valid),
        metrics_json=json.dumps(
            {
                **cand,
                "entry_p_baseline": entry_base,
                "live_2x_baseline": two_x,
                "promote_reason": "shadow only — never becomes Live or a paper gate",
                "label": f"sellable peak >= {RUNNER_HIT:g}x the t+15 print, or still >= {RUNNER_HOLD_MULT:g}x at the 6h print; dead pool = 0",
                "live_only": True,
                "fit_rows": mix,
            }
        ),
        incumbent_json=json.dumps(two_x or entry_base),
        promoted=False,
    )
    session.add(art)
    session.flush()
    log.info(
        "live runner shadow %s v%s auc %s top-decile %s base %s (2x head %s, entry %s)",
        chain,
        art.version,
        cand.get("auc"),
        cand.get("precision_top_decile"),
        cand["base_rate"],
        two_x.get("precision_top_decile"),
        entry_base.get("precision_top_decile"),
    )
    return {
        "fitted": True,
        "promoted": False,
        "version": art.version,
        "auc": cand.get("auc"),
        "precision_top_decile": cand.get("precision_top_decile"),
        "base_rate": cand["base_rate"],
        "share_ge_020": cand["share_ge_020"],
        "live_2x_top_decile": two_x.get("precision_top_decile"),
        "entry_top_decile": entry_base.get("precision_top_decile"),
    }


def live_features_for_token(
    session: Session,
    token: Any,
    *,
    entry_p: float,
    t0_mcap: float,
    last_mcap: float,
    last_liq: float,
    holders: int,
    peak: float,
    trough: float,
    vol_h1: float = 0.0,
) -> dict[str, float]:
    """Live features from the frozen entry print vs the current tape."""
    from ..ledger import DECISION_ENTRY

    research = getattr(token, "research", None)
    dec = None
    token_id = getattr(token, "id", None)
    if token_id:
        dec = (
            session.query(Decision)
            .filter(Decision.token_id == token_id, Decision.kind == DECISION_ENTRY)
            .order_by(Decision.at.desc())
            .first()
        )
    entry_liq = float((dec.liq if dec is not None else 0.0) or 0.0)
    entry_holders = int((dec.holders if dec is not None else 0) or 0)
    if entry_liq <= 0:
        entry_liq = float(last_liq or 0.0)
    if entry_holders <= 0:
        entry_holders = int((research.holder_count if research is not None else 0) or holders or 0)
    return live_features(
        entry_p=float(entry_p or 0.0),
        entry_mcap=float(t0_mcap or 0.0),
        entry_liq=entry_liq,
        entry_holders=entry_holders,
        mcap=float(last_mcap or 0.0),
        liq=float(last_liq or 0.0),
        vol_h1=float(vol_h1 or 0.0),
        holders=int(holders or 0),
        peak_before=float(peak or last_mcap or 0.0),
        trough_before=float(trough or last_mcap or 0.0),
    )


def live_model_p(session: Session, chain: str, features: dict[str, float]) -> float | None:
    """Calibrated Live-model probability from a promoted artifact, else None."""
    art = latest_promoted(session, chain, "live")
    if art is None:
        return None
    try:
        weights = json.loads(art.weights_json or "{}")
        cal = json.loads(art.calibration_json or "[]")
    except json.JSONDecodeError:
        return None
    z = float(art.bias) + sum(float(weights.get(n, 0.0)) * float(features.get(n, 0.0) or 0.0) for n in LIVE_FEATURES)
    raw = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
    return round(apply_calibration(cal, raw), 4)


def live_sample_counts(session: Session, chain: str) -> dict[str, int]:
    chain = normalize_chain(chain)
    total = session.query(LiveSample).filter(LiveSample.chain == chain).count()
    rows = live_rows(session, chain)
    by_source: dict[str, int] = {}
    for r in rows:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    return {
        "samples": int(total),
        "resolved": len(rows),
        "positives": int(sum(r["y"] for r in rows)),
        "runners": int(sum(int(r.get("y_run") or 0) for r in rows)),
        "resolved_by_source": by_source,
    }
