"""This-window hunt index.

Desk tape reads these rows. Do not leftover-sort 2×+. FEATURE_NAMES stays 66.
Sol leftover-FDV / old majors require chain == \"sol\" explicitly.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session

from ..chains import normalize_chain, normalize_mint
from ..desk_lines import FIRST_SIGHT_LINES, FIRST_SIGHT_RH_LINES, LEGACY_LINES, SCORER_FIRST_SIGHT, SCORER_LEGACY, lines_for_scorer
from ..models import HuntCard, Outcome, Research, Token, utcnow
from .bloom import leftover_fdv, promise_score

log = logging.getLogger("launchfinder.hunt")

def _hunt_row_lock_contention(exc: BaseException) -> bool:
    """True when Postgres could not take a hunt_cards row lock in time."""
    from sqlalchemy.exc import DBAPIError, OperationalError

    if isinstance(exc, OperationalError):
        return True
    if isinstance(exc, DBAPIError):
        orig = getattr(exc, "orig", None)
        if orig is not None:
            name = type(orig).__name__
            if "Lock" in name or "lock" in str(orig).lower():
                return True
            mod = type(orig).__module__ or ""
            if mod.startswith("psycopg") and name in {
                "LockNotAvailable",
                "QueryCanceled",
                "DeadlockDetected",
            }:
                return True
    return False


def _pending_rollback(exc: BaseException) -> bool:
    """True when the session cannot take more SQL until rollback."""
    from sqlalchemy.exc import InvalidRequestError, PendingRollbackError

    if isinstance(exc, PendingRollbackError):
        return True
    if isinstance(exc, InvalidRequestError):
        msg = str(exc).lower().replace(" ", "")
        if "pendingrollback" in msg or "this session is in 'pending rollback'" in str(exc).lower():
            return True
        if "rolled back due to a previous exception" in str(exc).lower():
            return True
    return False


def _hunt_session_defer(exc: BaseException) -> bool:
    """Row lock, deadlock, or aborted nested txn — defer mint, keep cycle."""
    from sqlalchemy.exc import InvalidRequestError

    if _hunt_row_lock_contention(exc) or _pending_rollback(exc):
        return True
    if isinstance(exc, InvalidRequestError):
        msg = str(exc).lower()
        if "closed transaction" in msg or "inactive transaction" in msg:
            return True
        if "no further sql" in msg and "rollback" in msg:
            return True
    return False


def recover_session_after_lock(
    session: Session, exc: BaseException, *, outer: bool = False
) -> bool:
    """Clear lock / PendingRollback so the cycle can continue. True if recovered.

    Nested savepoint locks are already aborted by ``begin_nested`` — do not
    ``session.rollback()`` those (that would wipe sibling tape bars).
    A failed outer ``commit()`` or PendingRollback must rollback or the
    next SQL dies.
    """
    if not (_hunt_row_lock_contention(exc) or _pending_rollback(exc) or _hunt_session_defer(exc)):
        return False
    if outer or _pending_rollback(exc):
        try:
            session.rollback()
        except Exception:
            log.exception("session rollback after lock / PendingRollback failed")
    return True


def token_has_hunt_card(session: Session, token: Token) -> bool:
    """True when the token already has a Hunt card (board-visible leftover)."""
    chain = normalize_chain(token.chain or "sol")
    mint = normalize_mint(token.mint, chain)
    if not mint:
        return False
    row = (
        session.query(HuntCard.id)
        .filter(HuntCard.chain == chain, HuntCard.mint == mint)
        .first()
    )
    if row is not None:
        return True
    if getattr(token, "id", None) is None:
        return False
    return (
        session.query(HuntCard.id)
        .filter(HuntCard.chain == chain, HuntCard.token_id == token.id)
        .first()
        is not None
    )


def _delete_hunt_cards_resilient(
    session: Session, cards: list[HuntCard], *, batch_size: int = 5
) -> int:
    """Drop hunt rows in small savepoints so boot rebuild cannot deadlock tape."""
    if not cards:
        return 0
    ids = [int(c.id) for c in cards if c.id is not None]
    if not ids:
        return 0
    deleted = 0

    def _delete_ids(chunk: list[int]) -> int:
        with session.begin_nested():
            n = (
                session.query(HuntCard)
                .filter(HuntCard.id.in_(chunk))
                .delete(synchronize_session=False)
            )
            return int(n or 0)

    for i in range(0, len(ids), batch_size):
        chunk = ids[i : i + batch_size]
        try:
            deleted += _delete_ids(chunk)
        except Exception as exc:
            if not _hunt_session_defer(exc):
                raise
            log.info("hunt card delete defer batch n=%s", len(chunk))
            for hid in chunk:
                try:
                    deleted += _delete_ids([hid])
                except Exception as exc2:
                    if _hunt_session_defer(exc2):
                        log.info("hunt card delete skip id=%s", hid)
                        continue
                    raise
    return deleted


HUNT_HOURS_SOL = 18.0
# 12h dropped DAM at 13.6h while Dex was $1M. PONS day-2 ran again.
# Paper window stays 12h. FEATURE_NAMES stays 66.
HUNT_HOURS_RH = 24.0
# Dedicated last+Live loop. Full refresh_outcomes chairs stay as-is.
HUNT_LIVE_LAST_SECONDS = 60.0
HUNT_LIVE_LAST_PER_CHAIN = 240
# Hunt Live ≠ Bloom promise. Bloom zeros last < 0.85× t0 (a dump is
# not a bloom) and stacks leftover 5× books to 0.95. Hunt must still
# paint a fat Sol sitter, and must not treat a thin-watch RH leftover
# as a 95. Cap matches "watch preview was already thin" / Entry < 0.20.
HUNT_THIN_WATCH_LIVE_CAP = 0.62
HUNT_THIN_ENTRY = 0.20
# Live 62 is a thin *tape*, not a thin Entry. A first-sight 0.01 on a
# $100k book is not leftover dust. Cap when the current print is skinny
# or the known holder count is a handful. Missing holders (0) is a data
# gap — do not treat it as a 0-wallet book.
HUNT_THIN_LIQ = 8_000.0
# Calibrated Live artifacts clip at 0.01. RH v2 writes that floor on
# every Hunt card (COLOSSEUM tape 0.89 → Live 0.01). Floor = no signal;
# use tape. Sol fat books sit at 0.51 and keep the model.
LIVE_MODEL_SIGNAL = 0.05


def hunt_hours(chain: str) -> float:
    return HUNT_HOURS_RH if normalize_chain(chain) == "robinhood" else HUNT_HOURS_SOL


HUNT_FAT_PIN_LAST_RH = 250_000.0
HUNT_FAT_PIN_LIQ_RH = 20_000.0
HUNT_FAT_PIN_LAST_SOL = 100_000.0
# Commander-class RH books (VRAX ~3–4M): never lose the pin lottery to 10M–60M leftovers.
HUNT_FAT_COMMANDER_MAX_RH = 10_000_000.0
HUNT_FAT_SYNC_COMMANDER_CAP = 32
HUNT_FAT_SYNC_MEGA_CAP = 8
HUNT_FAT_COMMANDER_HOLDER_GUARANTEE = 1_500


def hunt_fat_live_board_pin(token: Token, outcome: Outcome | None, *, now: datetime | None = None) -> bool:
    """Keep Dex-hydrated fat books on Hunt past the 24h/18h card lottery (VRAX / RESI).

    RH: fat last+liq plus V4 pool id, uniswap_v4 launchpad, or holder book — pool_address
    is often still null after unpark (live VRAX), so do not require the 64-char id alone.
    """
    if outcome is None or token.source == "backfill":
        return False
    from ..research.dexscreener import is_dex_pair_id
    from .outcomes import DEAD_POOL_LIQ, is_sol_old_leftover_major

    chain = normalize_chain(token.chain or "sol")
    last = float(outcome.last_mcap or 0.0)
    liq = float(outcome.last_liq or 0.0)
    if chain == "robinhood":
        if last < HUNT_FAT_PIN_LAST_RH or liq < HUNT_FAT_PIN_LIQ_RH:
            return False
        pool = str(token.pool_address or "")
        if is_dex_pair_id(pool):
            return True
        launchpad = str(getattr(token, "launchpad", "") or "").strip().lower()
        if launchpad in ("uniswap_v4", "uniswap-v4", "univ4"):
            return True
        holders = int((token.research.holder_count if token.research else 0) or 0)
        if holders < 40:
            return False
        # PONS-class day-2 leftover FDV without a V4 pool id on the row.
        if last > HUNT_FAT_COMMANDER_MAX_RH and not is_dex_pair_id(pool):
            return False
        return True
    if last < HUNT_FAT_PIN_LAST_SOL or liq < DEAD_POOL_LIQ:
        return False
    multiple = float(outcome.multiple or 0.0)
    t0 = float(outcome.t0_mcap or 0.0)
    if multiple <= 0 and t0 > 0 and last > 0:
        multiple = last / t0
    if is_sol_old_leftover_major(
        max(last, float(outcome.max_mcap or 0.0)),
        sol_launch_at(token) if chain == "sol" else (token.migrated_at or token.first_seen_at),
        now=now,
        multiple=multiple,
    ):
        return False
    return True


def hunt_leftover_window(chain: str, age_hours: float) -> bool:
    """RH Hunt 12–24h is leftover watch, not this-window paper.

    Sol Hunt and paper are both 18h — no leftover chip.
    Do not leftover-sort on this flag. Sort stays 2× then Live.
    """
    from .paper_gate import PAPER_WINDOW_RH

    if normalize_chain(chain) != "robinhood":
        return False
    age = float(age_hours or 0.0)
    return PAPER_WINDOW_RH <= age <= hunt_hours(chain)


def sol_launch_at(token: Token) -> datetime | None:
    """When the Sol pair actually opened, not when we first ingested it.

    Live ARMY: PumpSwap pairCreatedAt 2025-09-02, Hunt showed 3m because
    migrated_at was set the hour Dex finally printed. Desk age and the
    18h window must use the pair clock. RH leftover chairs stay on
    migrated/first_seen. FEATURE_NAMES stays 66.
    """
    stamps: list[datetime] = []
    for raw in (token.created_at_chain, token.migrated_at, token.first_seen_at):
        if raw is None:
            continue
        ts = raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        if ts.year < 2021:
            continue
        stamps.append(ts)
    return min(stamps) if stamps else None


def _aware(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def sol_pair_in_hunt_window(token: Token | None, *, since: datetime) -> bool:
    """Sol Hunt window is the pair clock. first_seen today is not enough.

    Live DNR: created_at 2026-08-16, migrated_at / first_seen today,
    Hunt still listed Entry 90 because list_hunt_mints ORed first_seen.
    RH leftover chairs stay on migrated/first_seen. FEATURE_NAMES stays 66.
    """
    launched = _aware(sol_launch_at(token) if token is not None else None)
    since = _aware(since)
    return launched is not None and since is not None and launched >= since


def latest_snap_tapes(session: Session, token_ids: list[int]) -> dict[int, dict]:
    """One latest-snap row per token. Hunt must not selectinload all snaps."""
    from sqlalchemy import func

    from ..models import Snapshot

    ids = [int(i) for i in token_ids if i]
    if not ids:
        return {}
    latest = (
        session.query(Snapshot.token_id, func.max(Snapshot.taken_at).label("taken_at"))
        .filter(Snapshot.token_id.in_(ids), Snapshot.mcap_usd > 0)
        .group_by(Snapshot.token_id)
        .subquery()
    )
    rows = (
        session.query(Snapshot)
        .join(
            latest,
            and_(Snapshot.token_id == latest.c.token_id, Snapshot.taken_at == latest.c.taken_at),
        )
        .all()
    )
    peaks = {
        int(tid): float(peak or 0.0)
        for tid, peak in session.query(Snapshot.token_id, func.max(Snapshot.mcap_usd))
        .filter(Snapshot.token_id.in_(ids))
        .group_by(Snapshot.token_id)
        .all()
    }
    now = datetime.now(timezone.utc)
    out: dict[int, dict] = {}
    for snap in rows:
        taken = snap.taken_at
        if taken is not None and taken.tzinfo is None:
            taken = taken.replace(tzinfo=timezone.utc)
        age = max(0.0, (now - taken).total_seconds() / 60.0) if taken is not None else 0.0
        out[int(snap.token_id)] = {
            "snap_mcap": float(snap.mcap_usd or 0.0),
            "snap_liq": float(snap.liquidity_usd or 0.0),
            "snap_max_mcap": float(peaks.get(int(snap.token_id)) or snap.mcap_usd or 0.0),
            "snap_age_min": age,
            "volume_h1": float(snap.volume_h1 or 0.0),
        }
    return out


def hunt_thin_watch_book(
    entry_p: float = 0.0,
    flags: list[str] | None = None,
    *,
    thin_entry: float = HUNT_THIN_ENTRY,
    last_liq: float | None = None,
    holders: int | None = None,
) -> bool:
    """Hunt Live 62 cap: the *current book* is thin, not the Entry.

    A first-sight 0.01 or a pre-LP "Watch preview was already thin" flag
    must not cap a live $80k / 150-holder book (the RH 62 wall). Cap
    leftover dust: liq under ``HUNT_THIN_LIQ``, or a known handful of
    wallets. ``entry_p`` / ``thin_entry`` / the Watch flag are ignored
    when a tape print is supplied. With no tape (tests that only pass
    Entry) the old Entry/flag rule still answers.
    """
    from .outcomes import RH_HUNT_THIN_HOLDERS

    del flags
    if last_liq is not None or holders is not None:
        liq = float(last_liq or 0.0)
        n = int(holders or 0)
        if liq <= 0:
            return True
        if liq < HUNT_THIN_LIQ:
            return True
        if n > 0 and n <= RH_HUNT_THIN_HOLDERS:
            return True
        return False
    if float(entry_p or 0.0) < float(thin_entry):
        return True
    return False


def conviction_from_tape(
    *,
    chain: str,
    entry_p: float,
    multiple: float,
    last_mcap: float,
    t0_mcap: float,
    max_mcap: float,
    last_liq: float,
    holders: int,
    top10_pct: float,
    flags: list[str] | None = None,
    label: int | None = None,
    vol_h1: float | None = None,
    holder_prev: int | None = None,
    holders_age_min: float | None = None,
    thin_entry: float = HUNT_THIN_ENTRY,
) -> float:
    """Live hunt score from the tape. Never copies entry p(good).

    ``vol_h1`` is the latest stored 1h volume when Hunt has a snap.
    Missing (None) stays 0 — do not treat an unloaded Hunt card as dust.
    Hunt mode does not inherit Bloom's under-0.85× zero. ``thin_entry``
    is the frozen scorer's thin line (see desk_lines).
    """
    flags = [str(f) for f in (flags or [])]
    live, _ = promise_score(
        chain=normalize_chain(chain),
        entry_p=float(entry_p or 0.0),
        multiple=float(multiple or 0.0),
        last_mcap=float(last_mcap or 0.0),
        t0_mcap=float(t0_mcap or 0.0),
        max_mcap=float(max_mcap or 0.0),
        last_liq=float(last_liq or 0.0),
        holders=int(holders or 0),
        top10_pct=float(top10_pct or 0.0),
        vol_h1=float(vol_h1 or 0.0),
        runner_p=None,
        second_leg=False,
        label=label,
        flags=flags,
        holder_prev=holder_prev,
        holders_age_min=holders_age_min,
        mode="hunt",
    )
    if hunt_thin_watch_book(
        entry_p,
        flags,
        thin_entry=thin_entry,
        last_liq=float(last_liq or 0.0),
        holders=int(holders or 0),
    ):
        live = min(live, HUNT_THIN_WATCH_LIVE_CAP)
    return live


def pick_live_conviction(
    tape: float,
    bloom_p: float = 0.0,
    *,
    last_mcap: float = 0.0,
    t0_mcap: float = 0.0,
    max_mcap: float = 0.0,
    allow_bloom: bool = True,
    cap: float | None = None,
    model_p: float | None = None,
) -> float:
    """One live number. Promoted Live model is health on a live book.

    Tape still wins on a dump / recap / under-0.85× so a trained 0.91
    cannot restore Live on a dead print. A model at the 0.01 clip is
    no signal (RH Hunt wall) — use tape. Bloom is the fallback when
    no Live artifact is promoted. Thin-watch leftover stays capped.
    """
    tape = float(tape or 0.0)
    bloom_p = float(bloom_p or 0.0)
    under = t0_mcap > 0 and last_mcap > 0 and last_mcap / t0_mcap < 0.85
    peak = max(float(max_mcap or 0.0), float(last_mcap or 0.0))
    recap = (
        peak > 0
        and last_mcap > 0
        and t0_mcap > 0
        and last_mcap / peak < 0.40
        and last_mcap / t0_mcap < 2.0
    )
    # tape ≤ 0.40 is a dump only on a last print. A seed with no Dex
    # last is "no live print yet", not a rug the model must not restore.
    dump = last_mcap > 0 and (under or recap or tape <= 0.40)
    model = None if model_p is None else float(model_p)
    if last_mcap <= 0:
        out = 0.0
    elif dump:
        out = tape
    elif model is not None and model > LIVE_MODEL_SIGNAL:
        out = model
    elif not allow_bloom:
        out = tape
    else:
        out = bloom_p if bloom_p > tape else tape
    if cap is not None:
        out = min(out, float(cap))
    return out


def live_held_multiple(last_mcap: float, t0_mcap: float, fallback: float = 0.0) -> float:
    """Current last/t0. Do not use a dumped ATH wick as the live multiple."""
    last_mcap = float(last_mcap or 0.0)
    t0_mcap = float(t0_mcap or 0.0)
    if t0_mcap > 0 and last_mcap > 0:
        return last_mcap / t0_mcap
    return float(fallback or 0.0)


def hunt_live_value(last_mcap: float, live: float) -> float | None:
    """Hunt Live is a print. No last → missing (desk draws —). Zero is a dead book."""
    if float(last_mcap or 0.0) <= 0:
        return None
    return float(live)


def hunt_board_rank(card: dict) -> tuple:
    """Pin names still holding 2× now. ATH recaps do not take the top."""
    live = live_held_multiple(
        float(card.get("last_mcap") or 0.0),
        float(card.get("t0_mcap") or 0.0),
        float(card.get("multiple") or 0.0),
    )
    return (
        live >= 2.0,
        float(card.get("conviction_p") or 0.0),
        str(card.get("first_seen_at") or ""),
    )


def bloom_promises(session: Session, mints: list[str]) -> dict[str, float]:
    from ..models import ScanState
    from .bloom import bloom_key

    if not mints:
        return {}
    keys = [bloom_key(mint) for mint in mints]
    out: dict[str, float] = {}
    for row in session.query(ScanState).filter(ScanState.key.in_(keys)).all():
        try:
            data = json.loads(row.value or "{}")
        except json.JSONDecodeError:
            continue
        mint = str(data.get("mint") or "")
        if not mint and str(row.key or "").startswith("bloom:"):
            mint = str(row.key).split(":", 1)[-1]
        if mint:
            out[mint] = float(data.get("promise_p") or 0.0)
    return out


def _age_hours(token: Token, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    if normalize_chain(token.chain or "sol") == "sol":
        launched = sol_launch_at(token)
    else:
        launched = token.migrated_at or token.first_seen_at or token.created_at_chain
    if launched is None:
        return 0.0
    if launched.tzinfo is None:
        launched = launched.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max(0.0, (now - launched).total_seconds() / 3600.0)


def _sol_fomo_board_zero_last(token: Token, outcome: Outcome | None) -> bool:
    if normalize_chain(token.chain or "sol") != "sol":
        return False
    if (token.source or "") != "fomo_board":
        return False
    return float((outcome.last_mcap if outcome else 0.0) or 0.0) <= 0.0


def _historical_hydrate_in_window_filter(since: datetime):
    return or_(
        Token.migrated_at >= since,
        Token.first_seen_at >= since,
        and_(
            Token.chain == "sol",
            Outcome.last_mcap >= HUNT_FAT_PIN_LAST_SOL,
        ),
    )


def _sol_zero_last_hydrate_priority(
    token: Token,
    outcome: Outcome,
    fomo_rank: dict[str, float],
    chain: str,
) -> bool:
    if float(outcome.last_mcap or 0.0) > 0:
        return False
    mint = normalize_mint(token.mint, chain)
    if _sol_fomo_board_zero_last(token, outcome):
        return True
    return fomo_rank.get(mint, 0.0) > 0.0


def _historical_hydrate_base_query(session: Session, chain: str, since: datetime):
    return (
        session.query(Token, Outcome, Research)
        .join(Outcome, Outcome.token_id == Token.id)
        .join(Research, Research.token_id == Token.id)
        .filter(Token.chain == chain)
        .filter(Token.is_historical.is_(True))
        .filter(Token.source != "backfill")
        .filter(_historical_hydrate_in_window_filter(since))
    )


def _hunt_eligible_historical_hydrate(
    token: Token,
    outcome: Outcome | None,
    research: Research | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Keep / recreate Hunt rows for live books stuck historical (VRAX / RESI)."""
    if research is None or outcome is None:
        return False
    chain = normalize_chain(token.chain or "sol")
    if _age_hours(token, now) > hunt_hours(chain):
        return False
    last = float(outcome.last_mcap or 0.0)
    if chain == "robinhood":
        return last <= 0
    if last > 0:
        return True
    from .high_si_learn import fomo_board_stub_unactionable

    if fomo_board_stub_unactionable(token, outcome):
        return False
    return False


HISTORICAL_HYDRATE_SCAN = 160
HISTORICAL_HYDRATE_TAPE_CAP = 20


def _fomo_hydrate_rank(session: Session, chain: str) -> dict[str, float]:
    """Mint -> priority from last FOMO trending audit (misses / top board)."""
    from ..research.fomo_coverage import load_fomo_trending_audit

    ranks: dict[str, float] = {}
    audit = load_fomo_trending_audit(session) or {}
    for block in ("misses", "top", "vetoed"):
        for item in audit.get(block) or []:
            if not isinstance(item, dict):
                continue
            if normalize_chain(item.get("chain") or "") != chain:
                continue
            mint = normalize_mint(str(item.get("mint") or ""), chain)
            if mint:
                ranks[mint] = max(ranks.get(mint, 0.0), 2_000_000.0)
    return ranks


def _historical_hydrate_score(
    token: Token,
    outcome: Outcome,
    research: Research,
    chain: str,
    fomo_rank: dict[str, float],
) -> float:
    from ..research.dexscreener import is_dex_pair_id

    mint = normalize_mint(token.mint, chain)
    score = float(fomo_rank.get(mint, 0.0))
    last = float(outcome.last_mcap or 0.0)
    liq = float(outcome.last_liq or 0.0)
    holders = int(research.holder_count or 0)
    if chain == "robinhood":
        if last > 0:
            return -1.0
        pool = str(token.pool_address or "")
        if is_dex_pair_id(pool):
            score += 900_000.0
        if token.source == "rh_bitquery":
            score += 80_000.0
        score += liq
        score += holders * 250.0
        # Copycat VRAX (~48k) without a V4 book — deprioritize vs canonical pool row.
        sym = (token.symbol or "").strip().upper()
        if sym == "VRAX" and not is_dex_pair_id(pool):
            score -= 600_000.0
    else:
        if last <= 0:
            if _sol_fomo_board_zero_last(token, outcome) or fomo_rank.get(mint, 0.0) > 0:
                score += 800_000.0
            else:
                return -1.0
        else:
            score += min(last, 4_000_000.0)
            score += liq * 0.5
    return score


def historical_hydrate_tape_mints(session: Session, chain: str, *, limit: int = HISTORICAL_HYDRATE_TAPE_CAP) -> list[str]:
    """Historical tokens that need a Dex tape write before they can re-enter Hunt.

    Ranked: FOMO board / Uni V4 pool / live liq — not first_seen_at dust lottery.
    """
    chain = normalize_chain(chain)
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hunt_hours(chain))
    if chain == "sol":
        from .high_si_learn import repair_fomo_board_missing_outcomes

        repair_fomo_board_missing_outcomes(session, chain)
    fomo_rank = _fomo_hydrate_rank(session, chain)
    base = _historical_hydrate_base_query(session, chain, since)
    priority_mints_ordered: list[str] = []
    if chain == "robinhood":
        q = base.filter(Outcome.last_mcap <= 0)
        pool_rank = case((Token.pool_address.isnot(None), 1), else_=0)
        q = q.order_by(
            pool_rank.desc(),
            Outcome.last_liq.desc(),
            Research.holder_count.desc(),
            Token.first_seen_at.desc(),
        )
        rows = q.limit(HISTORICAL_HYDRATE_SCAN).all()
    else:
        priority_rows = [
            row
            for row in base.filter(Outcome.last_mcap <= 0).all()
            if _sol_zero_last_hydrate_priority(row[0], row[1], fomo_rank, chain)
        ]
        priority_ids = {row[0].id for row in priority_rows}
        fat_rows = (
            base.filter(Outcome.last_mcap > 0)
            .order_by(
                Outcome.last_mcap.desc(),
                Outcome.last_liq.desc(),
                Token.first_seen_at.desc(),
            )
            .limit(HISTORICAL_HYDRATE_SCAN)
            .all()
        )
        rows = priority_rows + [row for row in fat_rows if row[0].id not in priority_ids]
        priority_scored: list[tuple[float, str]] = []
        for token, outcome, research in priority_rows:
            score = _historical_hydrate_score(token, outcome, research, chain, fomo_rank)
            if score < 0:
                continue
            priority_scored.append((score, normalize_mint(token.mint, chain)))
        priority_scored.sort(key=lambda pair: pair[0], reverse=True)
        priority_mints_ordered = [mint for _score, mint in priority_scored if mint]
    ranked: list[tuple[float, str]] = []
    for token, outcome, research in rows:
        score = _historical_hydrate_score(token, outcome, research, chain, fomo_rank)
        if score < 0:
            continue
        ranked.append((score, normalize_mint(token.mint, chain)))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    cap = max(1, int(limit))
    out: list[str] = []
    seen: set[str] = set()
    for mint in priority_mints_ordered:
        if not mint or mint in seen:
            continue
        seen.add(mint)
        out.append(mint)
        if len(out) >= cap:
            return out
    for _score, mint in ranked:
        if not mint or mint in seen:
            continue
        seen.add(mint)
        out.append(mint)
        if len(out) >= cap:
            break
    return out


def historical_hydrate_refresh_rows(
    session: Session, *, limit_per_chain: int = 8
) -> list[tuple[Outcome, Token]]:
    """Labeled or unlabeled historical rows for tape_refresh chairs."""
    now = datetime.now(timezone.utc)
    out: list[tuple[Outcome, Token]] = []
    for chain in ("robinhood", "sol"):
        since = now - timedelta(hours=hunt_hours(chain))
        q = (
            session.query(Outcome, Token)
            .join(Token, Token.id == Outcome.token_id)
            .join(Research, Research.token_id == Token.id)
            .filter(Token.chain == chain)
            .filter(Token.is_historical.is_(True))
            .filter(Token.source != "backfill")
            .filter(
                or_(
                    Token.migrated_at >= since,
                    Token.first_seen_at >= since,
                    and_(
                        Token.chain == "sol",
                        Outcome.last_mcap >= HUNT_FAT_PIN_LAST_SOL,
                    ),
                )
            )
        )
        if chain == "robinhood":
            q = q.filter(Outcome.last_mcap <= 0)
        else:
            q = q.filter(
                or_(
                    Outcome.last_mcap > 0,
                    Token.source == "fomo_board",
                )
            )
        mints = historical_hydrate_tape_mints(session, chain, limit=limit_per_chain)
        if not mints:
            continue
        q = q.filter(Token.mint.in_(mints))
        fetched = q.all()
        order = {m: i for i, m in enumerate(mints)}
        fetched.sort(key=lambda pair: order.get(normalize_mint(pair[1].mint, chain), 999))
        out.extend(fetched)
    return out


def hunt_eligible(token: Token, outcome: Outcome | None, research: Research | None, *, now: datetime | None = None) -> bool:
    if token is None or token.source == "backfill":
        return False
    if token.is_historical:
        return _hunt_eligible_historical_hydrate(token, outcome, research, now=now)
    if research is None:
        return False
    from .high_si_learn import fomo_board_stub_unactionable

    if fomo_board_stub_unactionable(token, outcome):
        return False
    chain = normalize_chain(token.chain or "sol")
    age_h = _age_hours(token, now)
    pin = hunt_fat_live_board_pin(token, outcome, now=now)
    if age_h > hunt_hours(chain) and not pin:
        return False
    t0 = float((outcome.t0_mcap if outcome else 0.0) or 0.0)
    last = float((outcome.last_mcap if outcome else 0.0) or 0.0)
    multiple = float((outcome.multiple if outcome else 0.0) or 0.0)
    if multiple <= 0 and t0 > 0 and last > 0:
        multiple = last / t0
    liq = float((outcome.last_liq if outcome else 0.0) or 0.0)
    from .outcomes import DEAD_POOL_LIQ, is_sol_old_leftover_major

    if 0 < liq < DEAD_POOL_LIQ:
        return False
    if chain == "sol" and leftover_fdv("sol", t0, last):
        return False
    if chain == "sol" and is_sol_old_leftover_major(
        max(last, float((outcome.max_mcap if outcome else 0.0) or 0.0)),
        sol_launch_at(token),
        now=now,
        multiple=multiple,
    ):
        return False
    return True


def upsert_hunt(
    session: Session,
    token: Token,
    *,
    conviction_p: float | None = None,
    now: datetime | None = None,
    touch_updated: bool = True,
    nested_savepoint: bool = True,
) -> HuntCard | None:
    """Add or refresh a this-window card. Drops leftovers. Never rewrites entry p_good.

    ``touch_updated=False`` is the 60s Hunt tape: last/Live move, but
    the 8-minute full Dex chairs still see the card as due.
    """
    now = now or utcnow()
    chain = normalize_chain(token.chain or "sol")
    outcome = token.outcome
    research = token.research
    mint_norm = normalize_mint(token.mint, chain)
    row = (
        session.query(HuntCard)
        .filter(HuntCard.chain == chain, HuntCard.token_id == token.id)
        .one_or_none()
    )
    if row is None:
        row = (
            session.query(HuntCard)
            .filter(HuntCard.chain == chain, HuntCard.mint == mint_norm)
            .one_or_none()
        )
    if row is None and chain == "robinhood":
        from sqlalchemy import func

        row = (
            session.query(HuntCard)
            .filter(HuntCard.chain == chain, func.lower(HuntCard.mint) == mint_norm)
            .one_or_none()
        )
    if row is not None and row.mint != mint_norm:
        row.mint = mint_norm
        row.token_id = token.id
    if not hunt_eligible(token, outcome, research, now=now):
        if row is not None:
            session.delete(row)
            session.flush()
        return None
    t0 = float((outcome.t0_mcap if outcome else 0.0) or 0.0)
    last = float((outcome.last_mcap if outcome else 0.0) or 0.0)
    multiple = float((outcome.multiple if outcome else 0.0) or 0.0)
    if multiple <= 0 and t0 > 0 and last > 0:
        multiple = last / t0
    stored_entry = float(row.entry_p) if row is not None and row.entry_p else 0.0
    raw_entry = float((research.p_good if research else 0.0) or 0.0)
    # Keep the first frozen entry once it was a fillable book. A pre-book
    # 0.01 first-sight stamp is not frozen — finalize_first_sight_entry
    # rewrites research.p_good on the first sellable print. Prefer the
    # ledger Decision when HuntCard.entry_p has drifted (NBS / JERRY).
    from .first_sight import frozen_decision_entry, hunt_entry_locked

    frozen = frozen_decision_entry(session, getattr(token, "id", None))
    if frozen is not None:
        stored_entry = frozen[0]
    locked = hunt_entry_locked(research, stored_entry, (frozen[1] if frozen else None) or (row.scorer if row is not None else None))
    if locked or frozen is not None:
        entry_p = stored_entry
    else:
        entry_p = raw_entry if raw_entry > 0 else stored_entry
    scorer = (frozen[1] if frozen and frozen[1] else None) or (row.scorer if row is not None and locked and row.scorer else None) or ((research.scorer if research else None) or SCORER_LEGACY)
    lines = lines_for_scorer(scorer, chain)
    try:
        flags = json.loads((research.risk_flags_json if research else "[]") or "[]")
    except json.JSONDecodeError:
        flags = []
    from ..research.holders import holder_tape

    tape_h = holder_tape(research)
    last_liq = float((outcome.last_liq if outcome else 0.0) or 0.0)
    holders = int(tape_h.get("n") or (research.holder_count if research else 0) or 0)
    peak = float((outcome.max_mcap if outcome else 0.0) or last or 0.0)
    tape = conviction_from_tape(
        chain=chain,
        entry_p=entry_p,
        multiple=multiple,
        last_mcap=last,
        t0_mcap=t0,
        max_mcap=peak,
        last_liq=last_liq,
        holders=holders,
        top10_pct=float((research.top10_pct if research else 0.0) or 0.0),
        flags=[str(f) for f in flags],
        label=int(outcome.label) if outcome is not None and outcome.label is not None else None,
        holder_prev=tape_h.get("prev"),
        holders_age_min=tape_h.get("age_min"),
        thin_entry=lines.thin,
    )
    from .live_fit import live_features_for_token, live_model_p

    feats = live_features_for_token(
        session,
        token,
        entry_p=entry_p,
        t0_mcap=t0,
        last_mcap=last,
        last_liq=last_liq,
        holders=holders,
        peak=peak,
        trough=last,
    )
    model_p = live_model_p(session, chain, feats)
    thin = hunt_thin_watch_book(entry_p, flags, thin_entry=lines.thin, last_liq=last_liq, holders=holders)
    bloom = float(conviction_p) if conviction_p is not None else 0.0
    live = pick_live_conviction(
        tape,
        bloom,
        last_mcap=last,
        t0_mcap=t0,
        max_mcap=peak,
        allow_bloom=conviction_p is not None and not thin,
        cap=HUNT_THIN_WATCH_LIVE_CAP if thin else None,
        model_p=model_p,
    )
    if conviction_p is None:
        from ..research.live_social import fold_live_social

        live, _ = fold_live_social(session, token.mint, live)
    live = max(0.0, min(0.95, float(live)))

    def _persist_hunt_row() -> None:
        nonlocal row
        if row is None:
            row = HuntCard(chain=chain, mint=token.mint, token_id=token.id)
            session.add(row)
        row.first_seen_at = token.first_seen_at or now
        if chain == "sol":
            row.launched_at = sol_launch_at(token) or token.migrated_at or token.first_seen_at
        else:
            row.launched_at = token.migrated_at or token.first_seen_at
        row.entry_p = entry_p
        row.scorer = scorer
        row.conviction_p = live
        row.t0_mcap = t0
        row.last_mcap = last
        row.multiple = multiple
        row.last_liq = float((outcome.last_liq if outcome else 0.0) or 0.0)
        row.holders = int((research.holder_count if research else 0) or 0)
        if touch_updated or row.updated_at is None:
            row.updated_at = now
        session.flush()
        if entry_p >= lines.lo:
            from ..ledger import record_line_decisions

            try:
                line_flags = json.loads((research.risk_flags_json if research else "[]") or "[]")
            except json.JSONDecodeError:
                line_flags = []
            record_line_decisions(
                session,
                token,
                entry_p,
                entry_mcap=last or t0,
                liq=row.last_liq,
                holders=row.holders,
                flags=[str(f) for f in line_flags],
                at=now,
                lines=lines,
            )
            session.flush()

    try:
        if nested_savepoint:
            with session.begin_nested():
                _persist_hunt_row()
        else:
            _persist_hunt_row()
    except Exception as exc:
        if _hunt_session_defer(exc):
            log.info("hunt upsert deferred %s (%s)", token.mint[:16], type(exc).__name__)
            return None
        raise
    return row


def hunt_tape_climber_clause():
    """SQL: stored last is already 1.2×–8× t0."""
    return and_(
        HuntCard.t0_mcap > 0,
        HuntCard.last_mcap >= 1.2 * HuntCard.t0_mcap,
        HuntCard.last_mcap < 8.0 * HuntCard.t0_mcap,
    )


def hunt_tape_unconfirmed_clause():
    """SQL: real pool whose stored last has not left t0, or has no last yet.

    Live DAM (0x7c65…a0e2): $17k liq / last==t0 $40k, Entry 0.01. One Hunt
    tape bar, then the newest flood evicted it from the 240. last stayed
    t0 so it never became a climber; Dex ran to $1M unseen.

    Live ORBIO (0xAa07…28A3): t0 snap mcap $0 / $648k liq. last<=0 is not
    last==t0, so v91 still dropped it. Doing well later printed $57M and
    Hunt never wrote a bar. Fat empty-last sits with the unconfirmed
    set, oldest-updated first. Thin last=0 dust stays out.
    FEATURE_NAMES stays 66. No extra GMGN. Do not leftover-sort.
    """
    sitting = and_(
        HuntCard.t0_mcap > 0,
        HuntCard.last_mcap > 0,
        HuntCard.last_mcap < 1.2 * HuntCard.t0_mcap,
    )
    empty_last = HuntCard.last_mcap <= 0
    return and_(
        HuntCard.last_liq >= HUNT_THIN_LIQ,
        or_(sitting, empty_last),
    )


def this_window_hunt_tape_mints(
    session: Session, *, limit_per_chain: int = HUNT_LIVE_LAST_PER_CHAIN
) -> dict[str, list[str]]:
    """This-window Hunt mints for the 60s last+Live loop.

    Climbers (1.2×–8× stored last) go first so a weak-entry book that
    is actually running wins a Dex batch before quiet leftovers.
    Unconfirmed real books (last still at t0, liq ≥ $8k) go next,
    oldest-updated first — otherwise a 0.01 / 1.0× stamp loses the 240
    lottery to newer dust and never becomes a climber (DAM $40k → $1M).
    Leftover chairs stay. FEATURE_NAMES stays 66. No extra GMGN.
    """
    now = datetime.now(timezone.utc)
    out: dict[str, list[str]] = {}
    climber = hunt_tape_climber_clause()
    unconfirmed = hunt_tape_unconfirmed_clause()
    held = case(
        (climber, 0),
        (unconfirmed, 1),
        else_=2,
    )
    stale_first = case((unconfirmed, HuntCard.updated_at), else_=None)
    for chain in ("sol", "robinhood"):
        # The 80 the desk sees first — 8×+ runners used to lose the 240
        # lottery to newest 1.2× climbers and freeze for hours.
        # Tape path must not upsert fat leftovers — that contends with
        # refresh_outcomes / API Hunt sync (ROB / TYPING LockNotAvailable).
        board = list_hunt_mints(session, chain, limit=80, sync_fat=False)
        hydrate = historical_hydrate_tape_mints(session, chain, limit=20)
        seen: set[str] = set()
        merged_board: list[str] = []
        for raw in hydrate + [str(m) for m in board if m]:
            key = normalize_mint(raw, chain)
            if not key or key in seen:
                continue
            seen.add(key)
            merged_board.append(key)
        board = merged_board
        seen = set(board)
        since = now - timedelta(hours=hunt_hours(chain))
        rows = (
            session.query(HuntCard.mint)
            .filter(HuntCard.chain == chain)
            .filter((HuntCard.launched_at >= since) | (HuntCard.first_seen_at >= since))
            .order_by(
                held.asc(),
                stale_first.asc().nulls_last(),
                HuntCard.launched_at.desc(),
                HuntCard.first_seen_at.desc(),
            )
            .limit(limit_per_chain)
            .all()
        )
        rest = [str(mint) for (mint,) in rows if mint and str(mint) not in seen]
        out[chain] = [str(m) for m in board if m] + rest
        out[chain] = out[chain][: int(limit_per_chain)]
    return out


def hunt_mints_needing_tick(session: Session, *, limit: int = 8) -> list[str]:
    """This-window hunt mints due a Dex tick. Extra chairs — leftover chairs stay."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=8)
    rows = (
        session.query(HuntCard.mint)
        .filter(HuntCard.updated_at < cutoff)
        .order_by(HuntCard.multiple.desc(), HuntCard.first_seen_at.desc())
        .limit(limit)
        .all()
    )
    return [str(mint) for (mint,) in rows if mint]


HIGH_SCORE_REVIEW_FLOOR = LEGACY_LINES.hi
HIGH_SCORE_REVIEW_GAP = timedelta(minutes=8)


def high_line_clause():
    """SQL: Hunt card at or above the high line of the scorer that froze it,
    on the chain it was frozen on (first-sight lines differ per chain)."""
    return or_(
        and_(HuntCard.scorer == SCORER_FIRST_SIGHT, HuntCard.chain == "robinhood", HuntCard.entry_p >= FIRST_SIGHT_RH_LINES.hi),
        and_(HuntCard.scorer == SCORER_FIRST_SIGHT, HuntCard.chain != "robinhood", HuntCard.entry_p >= FIRST_SIGHT_LINES.hi),
        and_(or_(HuntCard.scorer.is_(None), HuntCard.scorer != SCORER_FIRST_SIGHT), HuntCard.entry_p >= LEGACY_LINES.hi),
    )


def high_score_mints_needing_review(session: Session, *, limit: int = 8) -> list[str]:
    """Unlabeled this-window 90+ due a Dex + second-look tick.

    Hunt tick orders by ATH multiple and can spend all 8 chairs on
    labeled NINA-class 26× cards that refresh_outcomes then drops
    (label is not None). Fresh 90+ that dump into dust / two-tick /
    collapsed starve, so Hunt keeps showing Entry 92. Extra chairs —
    leftover chairs stay. FEATURE_NAMES stays 66. No extra GMGN.
    """
    cutoff = datetime.now(timezone.utc) - HIGH_SCORE_REVIEW_GAP
    held = case(
        (and_(HuntCard.t0_mcap > 0, HuntCard.last_mcap > 0), HuntCard.last_mcap / HuntCard.t0_mcap),
        else_=1.0,
    )
    rows = (
        session.query(HuntCard.mint)
        .join(Token, Token.id == HuntCard.token_id)
        .join(Outcome, Outcome.token_id == Token.id)
        .filter(high_line_clause())
        .filter(Outcome.label.is_(None))
        .filter(Token.is_historical.is_(False))
        .filter(Token.source != "backfill")
        .filter(HuntCard.updated_at < cutoff)
        .order_by(held.asc(), HuntCard.launched_at.desc())
        .limit(limit)
        .all()
    )
    return [str(mint) for (mint,) in rows if mint]


def _fat_board_sync_score(
    token: Token,
    outcome: Outcome,
    research: Research,
    chain: str,
    fomo_rank: dict[str, float],
) -> float:
    """Rank commander-band RH pins: FOMO audit + holders + liq, not raw mcap."""
    mint = normalize_mint(token.mint, chain)
    score = float(fomo_rank.get(mint, 0.0))
    last = float(outcome.last_mcap or 0.0)
    liq = float(outcome.last_liq or 0.0)
    holders = int(research.holder_count or 0)
    if chain == "robinhood":
        if HUNT_FAT_PIN_LAST_RH <= last <= HUNT_FAT_COMMANDER_MAX_RH:
            score += 600_000.0
        from ..research.dexscreener import is_dex_pair_id

        if is_dex_pair_id(str(token.pool_address or "")):
            score += 400_000.0
        if token.source == "rh_bitquery":
            score += 50_000.0
        score += min(liq, 500_000.0) * 0.25
        score += holders * 120.0
    else:
        score += min(last, 2_000_000.0) * 0.1
        score += liq * 0.2
    return score


def _rh_commander_band_query(base):
    return base.filter(
        Outcome.last_mcap >= HUNT_FAT_PIN_LAST_RH,
        Outcome.last_mcap <= HUNT_FAT_COMMANDER_MAX_RH,
        Outcome.last_liq >= HUNT_FAT_PIN_LIQ_RH,
    )


def _fomo_mint_match_clause(fomo_rank: dict[str, float]):
    mints = [normalize_mint(m, "robinhood") for m in fomo_rank if m]
    if not mints:
        return None
    lowered = [m.lower() for m in mints]
    return func.lower(Token.mint).in_(lowered)


def sync_fat_hunt_board_cards(session: Session, chain: str, *, limit: int = 16) -> list[str]:
    """Production Hunt board path: upsert fat live tokens before the 80-card lottery.

    Called from ``list_hunt_mints`` (``GET /api/hunt``). Commander band 250k–10M RH
    is pinned on FOMO/holders — not the top-N absolute mcap prefetch that dropped VRAX.
    """
    chain = normalize_chain(chain)
    now = datetime.now(timezone.utc)
    fomo_rank = _fomo_hydrate_rank(session, chain)
    base = (
        session.query(Token, Outcome, Research)
        .join(Outcome, Outcome.token_id == Token.id)
        .join(Research, Research.token_id == Token.id)
        .filter(Token.chain == chain)
        .filter(Token.is_historical.is_(False))
        .filter(Token.source != "backfill")
    )
    commander_cap = max(HUNT_FAT_SYNC_COMMANDER_CAP, int(limit))
    mega_cap = HUNT_FAT_SYNC_MEGA_CAP
    out: list[str] = []
    seen: set[str] = set()

    def _consume(rows: list, *, score_fn: bool) -> None:
        scored: list[tuple[float, str]] = []
        for token, outcome, research in rows:
            token.research = research
            token.outcome = outcome
            if not hunt_fat_live_board_pin(token, outcome, now=now):
                continue
            upsert_hunt(session, token, now=now, touch_updated=True)
            mint = normalize_mint(token.mint, chain)
            if score_fn:
                scored.append(
                    (
                        _fat_board_sync_score(token, outcome, research, chain, fomo_rank),
                        mint,
                    )
                )
            else:
                scored.append((float(outcome.last_mcap or 0.0), mint))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        for _score, mint in scored:
            if mint in seen:
                continue
            seen.add(mint)
            out.append(mint)

    if chain == "robinhood":
        fomo_clause = _fomo_mint_match_clause(fomo_rank)
        guarantee_or = [Research.holder_count >= HUNT_FAT_COMMANDER_HOLDER_GUARANTEE]
        if fomo_clause is not None:
            guarantee_or.append(fomo_clause)
        guaranteed_rows = (
            _rh_commander_band_query(base)
            .filter(or_(*guarantee_or))
            .order_by(Research.holder_count.desc(), Outcome.last_mcap.desc())
            .all()
        )
        scored_q = _rh_commander_band_query(base).filter(
            Research.holder_count < HUNT_FAT_COMMANDER_HOLDER_GUARANTEE,
        )
        if fomo_clause is not None:
            scored_q = scored_q.filter(~fomo_clause)
        scored_rows = scored_q.order_by(
            Research.holder_count.desc(),
            Outcome.last_mcap.desc(),
        ).limit(128).all()

        def _pin_row(token: Token, outcome: Outcome, research: Research) -> str | None:
            token.research = research
            token.outcome = outcome
            if not hunt_fat_live_board_pin(token, outcome, now=now):
                return None
            upsert_hunt(session, token, now=now, touch_updated=True)
            return normalize_mint(token.mint, chain)

        for token, outcome, research in guaranteed_rows:
            mint = _pin_row(token, outcome, research)
            if not mint or mint in seen:
                continue
            seen.add(mint)
            out.append(mint)
        scored: list[tuple[float, str]] = []
        for token, outcome, research in scored_rows:
            mint = _pin_row(token, outcome, research)
            if not mint or mint in seen:
                continue
            scored.append(
                (_fat_board_sync_score(token, outcome, research, chain, fomo_rank), mint)
            )
        scored.sort(key=lambda pair: pair[0], reverse=True)
        for _score, mint in scored:
            if mint in seen:
                continue
            if len(out) >= commander_cap:
                break
            seen.add(mint)
            out.append(mint)
        mega_rows = (
            base.filter(
                Outcome.last_mcap > HUNT_FAT_COMMANDER_MAX_RH,
                Outcome.last_liq >= HUNT_FAT_PIN_LIQ_RH,
            )
            .order_by(Outcome.last_mcap.desc())
            .limit(64)
            .all()
        )
        mega_scored: list[tuple[float, str]] = []
        for token, outcome, research in mega_rows:
            token.research = research
            token.outcome = outcome
            if not hunt_fat_live_board_pin(token, outcome, now=now):
                continue
            upsert_hunt(session, token, now=now, touch_updated=True)
            mint = normalize_mint(token.mint, chain)
            if mint in seen:
                continue
            mega_scored.append((float(outcome.last_mcap or 0.0), mint))
        mega_scored.sort(key=lambda pair: pair[0], reverse=True)
        for _last, mint in mega_scored[:mega_cap]:
            if mint in seen:
                continue
            seen.add(mint)
            out.append(mint)
    else:
        from .outcomes import DEAD_POOL_LIQ

        rows = (
            base.filter(
                Outcome.last_mcap >= HUNT_FAT_PIN_LAST_SOL,
                Outcome.last_liq >= DEAD_POOL_LIQ,
            )
            .order_by(Outcome.last_mcap.desc())
            .limit(max(48, int(limit) * 3))
            .all()
        )
        _consume(rows, score_fn=True)
        while len(out) > commander_cap:
            out.pop()
    return out


def _hunt_fat_pin_mints(
    session: Session,
    chain: str,
    since: datetime,
    *,
    limit: int = 12,
) -> list[str]:
    """Hunt board pins for hydrated fat books whose launched_at left the window."""
    chain = normalize_chain(chain)
    rows = (
        session.query(HuntCard, Token, Outcome)
        .join(Token, Token.id == HuntCard.token_id)
        .join(Outcome, Outcome.token_id == Token.id)
        .filter(HuntCard.chain == chain)
        .filter(Token.is_historical.is_(False))
        .filter(Token.source != "backfill")
        .all()
    )
    pinned: list[tuple[float, str]] = []
    for card, token, outcome in rows:
        if not hunt_fat_live_board_pin(token, outcome):
            continue
        launched = card.launched_at or card.first_seen_at or token.migrated_at or token.first_seen_at
        if launched is not None:
            if launched.tzinfo is None:
                launched = launched.replace(tzinfo=timezone.utc)
            if launched >= since:
                continue
        last = float(outcome.last_mcap or card.last_mcap or 0.0)
        pinned.append((last, normalize_mint(card.mint, chain)))
    pinned.sort(key=lambda pair: pair[0], reverse=True)
    out: list[str] = []
    seen: set[str] = set()
    for _last, mint in pinned:
        if mint in seen:
            continue
        seen.add(mint)
        out.append(mint)
        if len(out) >= max(1, int(limit)):
            break
    return out


def list_hunt_mints(
    session: Session,
    chain: str,
    *,
    hours: float | None = None,
    limit: int = 80,
    sync_fat: bool = True,
) -> list[str]:
    chain = normalize_chain(chain)
    window = float(hours) if hours and hours > 0 else hunt_hours(chain)
    since = datetime.now(timezone.utc) - timedelta(hours=window)
    fat_sync = (
        sync_fat_hunt_board_cards(session, chain, limit=HUNT_FAT_SYNC_COMMANDER_CAP)
        if sync_fat
        else []
    )
    fetch_n = max(int(limit) * 4, 80)
    rows = (
        session.query(HuntCard)
        .filter(HuntCard.chain == chain)
        .filter((HuntCard.launched_at >= since) | (HuntCard.first_seen_at >= since))
        .order_by(
            and_(HuntCard.t0_mcap > 0, HuntCard.last_mcap >= 2.0 * HuntCard.t0_mcap).desc(),
            HuntCard.conviction_p.desc(),
            HuntCard.first_seen_at.desc(),
        )
        .limit(fetch_n)
        .all()
    )
    if chain != "sol":
        live = [row.mint for row in rows if float(row.last_mcap or 0.0) > 0]
        pinned = _hunt_fat_pin_mints(session, chain, since, limit=12)
        out: list[str] = []
        seen: set[str] = set()
        for raw in fat_sync + pinned + live:
            key = normalize_mint(raw, chain)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(key)
        return out[: int(limit)]
    tokens = {}
    if rows:
        tokens = {
            token.mint: token
            for token in session.query(Token).filter(Token.mint.in_([row.mint for row in rows])).all()
        }
    pinned = _hunt_fat_pin_mints(session, chain, since, limit=12)
    keep: list[str] = []
    seen: set[str] = set()
    for raw in fat_sync + pinned:
        key = normalize_mint(raw, chain)
        if key and key not in seen:
            seen.add(key)
            keep.append(key)
    for row in rows:
        # Keep no-last Hunt cards in the table so hunt_tape can write the
        # first print. The desk 80 is a tape — AGE sort is not a $69k seed wall.
        if float(row.last_mcap or 0.0) <= 0:
            continue
        if sol_pair_in_hunt_window(tokens.get(row.mint), since=since):
            key = normalize_mint(row.mint, chain)
            if key not in seen:
                seen.add(key)
                keep.append(row.mint)
        if len(keep) >= limit:
            break
    return keep


def rebuild_hunt_window(session: Session, chain: str, *, limit: int = 400) -> int:
    """Boot/repair: index recent live tokens. Does not leftover-sort 2×+."""
    chain = normalize_chain(chain)
    since = datetime.now(timezone.utc) - timedelta(hours=hunt_hours(chain))
    tokens = (
        session.query(Token)
        .filter(Token.chain == chain)
        .filter(Token.is_historical.is_(False))
        .filter(Token.source != "backfill")
        .filter((Token.migrated_at >= since) | (Token.first_seen_at >= since))
        .order_by(Token.first_seen_at.desc())
        .limit(limit)
        .all()
    )
    wrote = 0
    for token in tokens:
        if upsert_hunt(session, token) is not None:
            wrote += 1
    stale = (
        session.query(HuntCard)
        .filter(HuntCard.chain == chain)
        .filter(HuntCard.first_seen_at < since)
        .filter((HuntCard.launched_at.is_(None)) | (HuntCard.launched_at < since))
        .all()
    )
    dropped = list(stale)
    if chain == "sol":
        cards = session.query(HuntCard).filter(HuntCard.chain == chain).all()
        tokens = {}
        if cards:
            tokens = {
                token.mint: token
                for token in session.query(Token).filter(Token.mint.in_([card.mint for card in cards])).all()
            }
        seen = {row.mint for row in dropped}
        for card in cards:
            if card.mint in seen:
                continue
            if not sol_pair_in_hunt_window(tokens.get(card.mint), since=since):
                dropped.append(card)
    dropped_n = _delete_hunt_cards_resilient(session, dropped)
    log.info("hunt rebuild %s wrote=%s dropped=%s", chain, wrote, dropped_n)
    return wrote
