"""Decision ledger: what the desk said, when, and what happened next.

Phase 0 of docs/ROADMAP.md. Everything here is append-only or
open-once/close-once. Nothing rewrites ``research.p_good``. No GMGN HTTP.
No orders.
"""

from __future__ import annotations

import hashlib
import json
import logging
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, NamedTuple

from sqlalchemy import and_, case, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .chains import normalize_chain, token_chain
from .desk_lines import LEGACY_LINES, SCORER_FIRST_SIGHT, SCORER_LEGACY, DeskLines, desk_lines, lines_for_scorer, scorer_for
from .image_rev import IMAGE_REV
from .models import Decision, HuntCard, Outcome, PaperFill, Research, ScanState, Snapshot, Ticket, Token, utcnow

log = logging.getLogger("launchfinder.ledger")

DECISION_ENTRY = "entry"
DECISION_LINE70 = "line70"
DECISION_LINE90 = "line90"
DECISION_GATE = "gate"

# Legacy-blend lines. Per-scorer lines live in desk_lines.py; these two
# names stay for the seed pass and the RH readers that still run legacy.
LINE_70 = LEGACY_LINES.lo
LINE_90 = LEGACY_LINES.hi

# Forward outcome horizon for the honest scoreboard and the 2x label.
RESOLVE_HOURS = 24.0
HIT_TARGET = 2.0
HIT_STRETCH = 5.0
# A print under this liquidity at judgement is not an exit anyone took.
LEDGER_DEAD_LIQ = 800.0
LEDGER_MAX_MULTIPLE = 80.0
# Seeded history: at-entry t0 snaps for this many days back.
SEED_DAYS = 30
SEED_KEY = "ledger:seeded"
RESEED_RETIRED_KEY = "ledger:seeded_retired:v1"
# Fresh-at-t0 test for the retired pass (pump_poll retires Sol at 18h).
SEED_FRESH = timedelta(hours=24)
SEED_JUST_FLIPPED = timedelta(minutes=30)

# Gated paper line (desk Paper tab). Shadow is the late-veto moonbag
# (pre-pumped, start-high, late chase). It never joins this line.
PAPER_LINE = "gated90"
PAPER_SHADOW_LINE = "shadow_late"
GATE_SCORER_KEY = "ledger:gate_scorer:v105"
PAPER_TARGET = 2.0
PAPER_RIDE = 10.0
PAPER_HOLD_HOURS = 24.0
PAPER_DEAD_EXIT = -0.85
PAPER_SELLABLE_LIQ = 5_000.0

# Shadow ticket sizing: a slice of the pool that would not move it.
TICKET_LIQ_FRACTION = 0.01
TICKET_MIN_USD = 20.0
TICKET_MAX_USD = 200.0
TICKET_STOP_MULT = 0.5

# Learn / Calibrate report bounds. Training fits stay unbounded.
CALIBRATION_DEFAULT_DAYS = 21
REPORT_DECISION_CAP = 8_000
EVIDENCE_CHUNK = 400
PAPER_V1_SUMMARY_CAP = 2_000
PAPER_V1_DAY_LOOKBACK_DAYS = 3


def _aware(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def features_hash(features_json: str) -> str:
    return hashlib.sha1((features_json or "{}").encode("utf-8")).hexdigest()[:40]


def _loads(raw: str | None, default):
    try:
        out = json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return default
    return out if isinstance(out, type(default)) else default


# --------------------------------------------------------------------------
# Writers
# --------------------------------------------------------------------------


def record_decision(
    session: Session,
    token: Token,
    *,
    kind: str,
    entry_p: float,
    heuristic_p: float = 0.0,
    model_p: float = 0.0,
    features_json: str = "{}",
    entry_mcap: float = 0.0,
    liq: float = 0.0,
    vol_h1: float = 0.0,
    holders: int = 0,
    flags: Iterable[str] | None = None,
    veto: str = "",
    at: datetime | None = None,
    source: str = "live",
    model_version: int = 0,
    scorer: str = SCORER_LEGACY,
    known: set[tuple[str, str, str]] | None = None,
) -> Decision | None:
    """Insert one (chain, mint, kind) row. Returns None when it already exists.

    ``known`` is an optional preloaded (chain, mint, kind) set for bulk
    seeding so each row does not cost an existence query. ``scorer`` is
    the model that wrote ``entry_p``; the desk lines read against it.
    """
    if token is None or not token.mint:
        return None
    chain = normalize_chain(token.chain or "sol")
    key = (chain, token.mint, kind)
    if known is not None:
        if key in known:
            return None
        known.add(key)
    else:
        exists = (
            session.query(Decision.id)
            .filter(Decision.chain == chain, Decision.mint == token.mint, Decision.kind == kind)
            .first()
        )
        if exists:
            return None
    row = Decision(
        chain=chain,
        mint=token.mint,
        token_id=token.id,
        kind=kind,
        at=_aware(at) or utcnow(),
        source=source,
        entry_p=float(entry_p or 0.0),
        heuristic_p=float(heuristic_p or 0.0),
        model_p=float(model_p or 0.0),
        features_json=features_json or "{}",
        features_hash=features_hash(features_json or "{}"),
        entry_mcap=float(entry_mcap or 0.0),
        liq=float(liq or 0.0),
        vol_h1=float(vol_h1 or 0.0),
        holders=int(holders or 0),
        flags_json=json.dumps([str(f) for f in (flags or [])]),
        veto=(veto or "")[:64],
        image_rev=IMAGE_REV,
        model_version=int(model_version or 0),
        scorer=str(scorer or SCORER_LEGACY)[:16],
    )
    session.add(row)
    session.flush()
    return row


def record_entry_decision(
    session: Session,
    token: Token,
    research: Research,
    scored: dict[str, Any],
    *,
    market: dict[str, Any] | None,
    holder_count: int,
    at: datetime | None = None,
) -> Decision | None:
    """First score for a live launch. Backfill / historical rows are not decisions."""
    if token.is_historical or (token.source or "") in ("backfill", "rh_backfill"):
        return None
    market = market or {}
    mcap = float(market.get("mcap_usd") or 0.0)
    if mcap <= 0 and token.outcome is not None:
        mcap = float(token.outcome.t0_mcap or 0.0)
    scorer = scorer_for(scored)
    row = record_decision(
        session,
        token,
        kind=DECISION_ENTRY,
        entry_p=float(scored.get("p_good") or research.p_good or 0.0),
        heuristic_p=float(scored.get("heuristic_p") or 0.0),
        model_p=float(scored.get("model_p") or 0.0),
        features_json=research.features_json or "{}",
        entry_mcap=mcap,
        liq=float(market.get("liquidity_usd") or 0.0),
        vol_h1=float(market.get("volume_h1") or 0.0),
        holders=int(holder_count or 0),
        flags=scored.get("risk_flags") or [],
        at=at,
        model_version=int(scored.get("model_version") or 0),
        scorer=scorer,
    )
    if row is not None:
        record_line_decisions(
            session,
            token,
            row.entry_p,
            entry_mcap=mcap,
            liq=row.liq,
            holders=row.holders,
            flags=_loads(row.flags_json, []),
            at=row.at,
            lines=lines_for_scorer(scorer, row.chain),
        )
    return row


def record_line_decisions(
    session: Session,
    token: Token,
    entry_p: float,
    *,
    entry_mcap: float,
    liq: float,
    holders: int,
    flags: Iterable[str] | None,
    at: datetime | None = None,
    known: set[tuple[str, str, str]] | None = None,
    lines: DeskLines | None = None,
    source: str = "live",
) -> list[Decision]:
    """Desk-line crossings. Entry is frozen so these happen once per mint.

    ``lines`` are the scorer's lines for this Entry (``line70`` is the low
    line, ``line90`` the high one — the kind names are storage slots, the
    thresholds live on the scorer). Default: the lines in force on the chain.
    """
    out: list[Decision] = []
    p = float(entry_p or 0.0)
    if lines is None:
        lines = desk_lines(session, token.chain or "sol")
    for line, kind in ((lines.lo, DECISION_LINE70), (lines.hi, DECISION_LINE90)):
        if p < line:
            continue
        row = record_decision(
            session,
            token,
            kind=kind,
            entry_p=p,
            entry_mcap=entry_mcap,
            liq=liq,
            holders=holders,
            flags=flags,
            at=at,
            source=source,
            scorer=lines.scorer,
            known=known,
        )
        if row is not None:
            out.append(row)
    return out


def seed_decisions_from_t0(
    session: Session,
    *,
    days: int = SEED_DAYS,
    batch: int = 2000,
    retired: bool = False,
    key: str = SEED_KEY,
) -> int:
    """One-time history: the at-entry ``t0`` snapshot is the best frozen
    record we have. Marked ``source='seed_t0'`` so reports can split it out.

    ``retired=True`` is the second pass (``RESEED_RETIRED_KEY``): tokens the
    desk had already flipped ``is_historical`` when the first seed ran. Those
    are the losers — Sol retires at 18h, RH when quiet — and the first pass
    skipped 21,610 Sol first-sight scores (909 of them 90+) against 998 it
    kept. Only names that were fresh at their t0 qualify (created within
    ``SEED_FRESH`` of the print, or just migrated), which is the same test
    the live writer applies through ``is_historical`` at first sight.
    """
    if session.query(ScanState.id).filter(ScanState.key == key).first():
        return 0
    cutoff = utcnow() - timedelta(days=days)

    # Columns only: Research.raw_json is tens of KB per row and 40k live
    # tokens would not fit a worker replica if loaded as ORM objects.
    def _page(after_id: int):
        return (
            session.query(
                Snapshot.id,
                Snapshot.taken_at,
                Snapshot.mcap_usd,
                Snapshot.liquidity_usd,
                Snapshot.volume_h1,
                Snapshot.p_good,
                Token.id,
                Token.mint,
                Token.chain,
                Research.p_good,
                Research.heuristic_p,
                Research.model_p,
                Research.features_json,
                Research.holder_count,
                Research.risk_flags_json,
                Outcome.t0_mcap,
                Token.created_at_chain,
                Token.migrated_at,
            )
            .join(Token, Token.id == Snapshot.token_id)
            .join(Research, Research.token_id == Token.id)
            .outerjoin(Outcome, Outcome.token_id == Token.id)
            .filter(
                Snapshot.kind == "t0",
                Snapshot.taken_at >= cutoff,
                Snapshot.id > after_id,
                Token.is_historical.is_(retired),
                Token.source.notin_(("backfill", "rh_backfill")),
            )
            .order_by(Snapshot.id.asc())
            .limit(batch)
            .all()
        )

    known: set[tuple[str, str, str]] = set(
        (c, m, k) for c, m, k in session.query(Decision.chain, Decision.mint, Decision.kind).all()
    )
    seen: set[tuple[str, str]] = set()
    wrote = 0
    raced = 0
    stale = 0
    last_id = 0
    while True:
        page = _page(last_id)
        if not page:
            break
        for row in page:
            last_id = int(row[0])
            if retired and not _fresh_at_t0(row[1], row[-2], row[-1]):
                stale += 1
                continue
            # Live v67: the API scored TOKPAID between the `known` load and
            # this page; one duplicate killed the whole seed. Savepoint per
            # row so a race costs one row, not the batch.
            try:
                with session.begin_nested():
                    _seed_one(session, row[1:-2], known=known, seen=seen, keep_features=not retired)
            except IntegrityError:
                raced += 1
        wrote = len(seen)
        session.commit()
    if raced:
        log.info("ledger seed skipped %s rows already recorded live", raced)
    if stale:
        log.info("ledger seed skipped %s retired rows that were not fresh at t0", stale)
    session.add(ScanState(key=key, value=json.dumps({"n": wrote, "stale": stale, "at": utcnow().isoformat()}), updated_at=utcnow()))
    session.commit()
    log.info("ledger seeded %s entry decisions from %st0 snaps", wrote, "retired " if retired else "")
    return wrote


REPAIR_SCORER_KEY = "ledger:repair_scorer:v1"


def repair_decision_scorers(session: Session) -> dict[str, int]:
    """One-time (stack-v76) stamp of ``scorer`` / ``source`` on rows written
    before the columns existed.

    * Seeded line rows were stamped ``source='live'`` (the seed passed no
      source through ``record_line_decisions``). Any line row whose entry
      row is ``seed_t0`` is a seed row.
    * v75 wrote first-sight Entries without a scorer stamp. A live entry
      decision whose frozen features carry ``legacy_p`` and whose
      ``entry_p`` differs from it was scored by the first-sight model; its
      line rows (recorded against 0.70 / 0.90) inherit the stamp.
    Idempotent; keyed in ScanState.
    """
    if session.query(ScanState.id).filter(ScanState.key == REPAIR_SCORER_KEY).first():
        return {"skipped": 1}
    from sqlalchemy.orm import load_only

    fixed_source = 0
    fixed_scorer = 0
    seeded = {
        (c, m)
        for c, m in session.query(Decision.chain, Decision.mint).filter(Decision.kind == DECISION_ENTRY, Decision.source == "seed_t0").all()
    }
    line_rows = (
        session.query(Decision)
        .options(load_only(Decision.id, Decision.chain, Decision.mint, Decision.source, Decision.scorer))
        .filter(Decision.kind.in_((DECISION_LINE70, DECISION_LINE90)))
        .all()
    )
    for row in line_rows:
        if (row.chain, row.mint) in seeded and row.source != "seed_t0":
            row.source = "seed_t0"
            fixed_source += 1
    # Only v75 live rows can be unstamped first-sight Entries; the frozen
    # vector is a few KB each and there are hundreds, not tens of thousands.
    first_sight_keys: set[tuple[str, str]] = set()
    restamped: list[Decision] = []
    for entry in (
        session.query(Decision)
        .options(
            load_only(
                Decision.id,
                Decision.chain,
                Decision.mint,
                Decision.token_id,
                Decision.at,
                Decision.entry_p,
                Decision.entry_mcap,
                Decision.liq,
                Decision.holders,
                Decision.flags_json,
                Decision.scorer,
                Decision.features_json,
            )
        )
        .filter(Decision.kind == DECISION_ENTRY, Decision.source == "live", Decision.image_rev == "stack-v75")
        .all()
    ):
        if (entry.scorer or SCORER_LEGACY) != SCORER_LEGACY:
            first_sight_keys.add((entry.chain, entry.mint))
            continue
        legacy_p = _loads(entry.features_json, {}).get("legacy_p")
        if legacy_p is None:
            continue
        try:
            if abs(float(entry.entry_p or 0.0) - float(legacy_p)) < 1e-6:
                continue
        except (TypeError, ValueError):
            continue
        entry.scorer = "first_sight"
        first_sight_keys.add((entry.chain, entry.mint))
        restamped.append(entry)
        fixed_scorer += 1
    for row in line_rows:
        if (row.chain, row.mint) in first_sight_keys and (row.scorer or SCORER_LEGACY) != "first_sight":
            row.scorer = "first_sight"
    if restamped:
        ids = [e.token_id for e in restamped]
        for r in session.query(Research).options(load_only(Research.id, Research.token_id, Research.scorer)).filter(Research.token_id.in_(ids)).all():
            r.scorer = "first_sight"
        for h in session.query(HuntCard).options(load_only(HuntCard.id, HuntCard.token_id, HuntCard.scorer)).filter(HuntCard.token_id.in_(ids)).all():
            h.scorer = "first_sight"
    session.flush()
    # v75 judged those Entries against 0.70 / 0.90, so none crossed. Write
    # the 0.30 / 0.50 crossings they did make, dated at the decision.
    lines_added = 0
    for entry in restamped:
        stub = Token(id=entry.token_id, mint=entry.mint, chain=entry.chain)
        lines_added += len(
            record_line_decisions(
                session,
                stub,
                float(entry.entry_p or 0.0),
                entry_mcap=float(entry.entry_mcap or 0.0),
                liq=float(entry.liq or 0.0),
                holders=int(entry.holders or 0),
                flags=_loads(entry.flags_json, []),
                at=entry.at,
                lines=lines_for_scorer("first_sight", entry.chain),
            )
        )
    session.add(ScanState(key=REPAIR_SCORER_KEY, value=json.dumps({"source": fixed_source, "scorer": fixed_scorer, "lines": lines_added, "at": utcnow().isoformat()}), updated_at=utcnow()))
    session.commit()
    if fixed_source or fixed_scorer:
        log.info("ledger repair: %s seeded line rows re-sourced, %s first-sight entries stamped, %s line rows added", fixed_source, fixed_scorer, lines_added)
    return {"source": fixed_source, "scorer": fixed_scorer, "lines": lines_added}


def repair_gate_scorers(session: Session) -> dict[str, int]:
    """Stamp gate rows with the entry decision's scorer.

    ``record_decision`` used to default gates to legacy, so a first-sight
    fill at 0.16 was stored as a legacy gate. Idempotent. Does not rewrite
    entry_p.
    """
    if session.query(ScanState.id).filter(ScanState.key == GATE_SCORER_KEY).first():
        return {"skipped": 1}
    entries = {
        (chain, mint): scorer
        for chain, mint, scorer in session.query(Decision.chain, Decision.mint, Decision.scorer).filter(Decision.kind == DECISION_ENTRY).all()
    }
    fixed = 0
    for gate in session.query(Decision).filter(Decision.kind == DECISION_GATE).all():
        want = entries.get((gate.chain, gate.mint))
        if want and gate.scorer != want:
            gate.scorer = want
            fixed += 1
    session.add(ScanState(key=GATE_SCORER_KEY, value=json.dumps({"gates": fixed, "at": utcnow().isoformat()}), updated_at=utcnow()))
    session.commit()
    return {"gates": fixed}


def _fresh_at_t0(taken_at: datetime | None, created_at_chain: datetime | None, migrated_at: datetime | None) -> bool:
    """Was this a launch when we first scored it? Mirrors the live writer's
    first-sight ``is_historical`` test: fresh creation or a just-flipped pool."""
    at = _aware(taken_at)
    if at is None:
        return False
    created = _aware(created_at_chain)
    if created is not None and timedelta(0) <= at - created <= SEED_FRESH:
        return True
    migrated = _aware(migrated_at)
    if migrated is not None and abs((at - migrated).total_seconds()) <= SEED_JUST_FLIPPED.total_seconds():
        return True
    return False


def _seed_one(
    session: Session,
    row: tuple,
    *,
    known: set[tuple[str, str, str]],
    seen: set[tuple[str, str]],
    keep_features: bool = True,
) -> None:
    (
        taken_at,
        snap_mcap,
        snap_liq,
        snap_vol,
        snap_p,
        token_id,
        mint,
        token_chain_raw,
        research_p,
        heuristic_p,
        model_p,
        features_json,
        holder_count,
        risk_flags_json,
        t0_mcap,
    ) = row
    chain = normalize_chain(token_chain_raw or "sol")
    key = (chain, mint)
    if key in seen or (chain, mint, DECISION_ENTRY) in known:
        return
    mcap = float(snap_mcap or 0.0)
    if mcap <= 0:
        mcap = float(t0_mcap or 0.0)
    p = float(snap_p or 0.0)
    if p <= 0:
        p = float(research_p or 0.0)
    stub = Token(id=token_id, mint=mint, chain=chain)
    flags = _loads(risk_flags_json, [])
    decision = record_decision(
        session,
        stub,
        kind=DECISION_ENTRY,
        entry_p=p,
        heuristic_p=float(heuristic_p or 0.0),
        model_p=float(model_p or 0.0),
        # The research vector is the *current* one (repaired), never trained
        # on for seed rows; the retired pass does not copy 50k of them.
        features_json=(features_json or "{}") if keep_features else "{}",
        entry_mcap=mcap,
        liq=float(snap_liq or 0.0),
        vol_h1=float(snap_vol or 0.0),
        holders=int(holder_count or 0),
        flags=flags,
        at=taken_at,
        source="seed_t0",
        known=known,
    )
    if decision is None:
        return
    seen.add(key)
    # The t0 snap was scored by the legacy blend: its lines, its source.
    record_line_decisions(
        session,
        stub,
        p,
        entry_mcap=mcap,
        liq=decision.liq,
        holders=decision.holders,
        flags=flags,
        at=taken_at,
        known=known,
        lines=LEGACY_LINES,
        source="seed_t0",
    )


# --------------------------------------------------------------------------
# Forward outcome of a decision
# --------------------------------------------------------------------------


# A peak only counts if the pool could absorb a ticket at that print. Same
# floor as the paper ledger's fill and exit. Without it the judge rewards a
# $5k pool that wicks 2x on one buy — a print nobody sells into.
LEDGER_SELLABLE_LIQ = PAPER_SELLABLE_LIQ

# A snapshot inside this window of the decision is the decision's own print.
EVIDENCE_GRACE = timedelta(seconds=30)


class Evidence(NamedTuple):
    """Our own prints after one decision, inside the horizon.

    ``peak``: highest mcap printed on a sellable pool (liq >= the floor).
    ``n``: count of any print at all (silence detector).
    ``fill_mcap`` / ``fill_at``: the first sellable print — the price a
    buyer who acted on the decision could actually have paid. 0 / None when
    no pool ever reached the floor inside the horizon.

    ``None`` passed to the judge means the caller did not look, so only the
    outcome's own Dex print can vouch.
    """

    peak: float = 0.0
    n: int = 0
    fill_mcap: float = 0.0
    fill_at: datetime | None = None


NO_EVIDENCE = Evidence()


def _as_evidence(ev: Evidence | tuple | None) -> Evidence | None:
    if ev is None:
        return None
    if isinstance(ev, Evidence):
        return ev
    peak, n, *rest = ev
    return Evidence(float(peak or 0.0), int(n or 0), float(rest[0]) if rest else 0.0, rest[1] if len(rest) > 1 else None)


def _shift(col, delta: timedelta, postgres: bool):
    """``col + delta`` on both dialects (SQLite cannot add an interval to a column)."""
    if postgres:
        return col + delta
    secs = int(delta.total_seconds())
    return func.datetime(col, f"{'+' if secs >= 0 else '-'}{abs(secs)} seconds")


def post_decision_evidence(session: Session, decisions: Iterable[Decision]) -> dict[int, Evidence]:
    """Peak, count and first fillable print of our own prints after each decision.

    Snapshots and one-minute tape bars are things a market actually showed
    us. ``Outcome.max_mcap`` is not: RH ghost books are seeded at the $40k
    graduation floor and Sol at the migration constant, so a $4k curve
    print that never traded again read as a 9x from the seed alone (live
    v74 board: 2,972 of 3,291 "hits" in the 0.6 bin, 6,375 rows under $5k at
    98%). Prints past the horizon do not count either.

    The fill is the earliest sellable print (a ``row_number`` window per
    decision, so it stays one grouped query per table). Four queries per 5k
    decisions; no per-row lookups.
    """
    from .models import TapeBar

    rows = [d for d in decisions if d is not None and d.id is not None]
    out: dict[int, Evidence] = {}
    if not rows:
        return out
    ids = [d.id for d in rows]
    try:
        postgres = session.get_bind().dialect.name == "postgresql"
    except Exception:
        postgres = False
    after = _shift(Decision.at, EVIDENCE_GRACE, postgres)
    until = _shift(Decision.at, timedelta(hours=RESOLVE_HOURS), postgres)
    fills: dict[int, tuple[datetime, float]] = {}
    for i in range(0, len(ids), EVIDENCE_CHUNK):
        chunk = ids[i : i + EVIDENCE_CHUNK]
        for model, tcol in ((Snapshot, Snapshot.taken_at), (TapeBar, TapeBar.minute)):
            sellable = func.max(case((model.liquidity_usd >= LEDGER_SELLABLE_LIQ, model.mcap_usd), else_=0.0))
            agg = (
                session.query(Decision.id, sellable, func.count(model.id))
                .join(model, model.token_id == Decision.token_id)
                .filter(Decision.id.in_(chunk), model.mcap_usd > 0, tcol > after, tcol <= until)
                .group_by(Decision.id)
                .all()
            )
            for did, peak, n in agg:
                p0, n0 = out.get(did, NO_EVIDENCE)[:2]
                out[did] = Evidence(max(p0, float(peak or 0.0)), n0 + int(n or 0))
            rn = func.row_number().over(partition_by=Decision.id, order_by=tcol.asc()).label("rn")
            sub = (
                session.query(Decision.id.label("did"), tcol.label("t"), model.mcap_usd.label("mc"), rn)
                .join(model, model.token_id == Decision.token_id)
                .filter(Decision.id.in_(chunk), model.mcap_usd > 0, model.liquidity_usd >= LEDGER_SELLABLE_LIQ, tcol > after, tcol <= until)
                .subquery()
            )
            for did, t, mc in session.query(sub.c.did, sub.c.t, sub.c.mc).filter(sub.c.rn == 1).all():
                t = _aware(t)
                if t is not None and (did not in fills or t < fills[did][0]):
                    fills[did] = (t, float(mc or 0.0))
    for did, (t, mc) in fills.items():
        peak, n = out.get(did, NO_EVIDENCE)[:2]
        out[did] = Evidence(peak, n, mc, t)
    return out


def decision_result(
    decision: Decision,
    outcome: Outcome | None,
    *,
    now: datetime | None = None,
    evidence: Evidence | None = None,
) -> dict[str, Any] | None:
    """Resolved forward result for one decision, or None while still open.

    Resolved when the outcome is labeled or the decision is older than the
    horizon. A dead pool at judgement is a loss regardless of the wick.

    The multiple is measured from the price a buyer could have paid, the
    same way the paper desk fills: a decision scored on a sellable pool
    (liq >= ``LEDGER_SELLABLE_LIQ``) is judged from its own entry mcap; one
    scored on the bonding curve or a thin book is judged from the **first
    sellable print** after it (``evidence.fill_mcap``), and a decision that
    never got such a print inside the horizon is ``unfilled`` — a loss,
    because the desk could not act on it. Live v78 board: 2,373 Sol curve
    "wins" judged from the curve print, 432 still 2x from the pool open —
    the median first pool print sat 16.8x above the curve number, so the
    "win" was graduation itself, which no buyer captures. The old 0.50 line
    read 59% 2x on that; 23% once judged from the fill.

    The peak must be a print somebody could have sold into: a post-decision
    snapshot or tape bar on a sellable pool (``evidence``) or the outcome's
    last Dex print. ``Outcome.max_mcap`` never vouches: it is a lifetime
    high with no timestamp, and on the v79 Sol board it was crediting
    year-old tokens' ATHs to decisions taken on a flat tape (GYAT: sight
    $146k, every later print $147k-$154k, judged 3.3x; San / KWIF /
    titcoin hit the 80x cap on a 1.0x tape). Rows older than a week at
    sight read 42% 2x that way and 1.9% from their prints; 64% of the Sol
    positives were that artifact, and the first-sight model learned
    "older and bigger = winner" from it. The paper ledger already marks a
    fill only from prints it saw; the judge now measures the same way. A
    decision with no print at all after entry is ``unobserved`` — nobody
    traded it, so there was no exit; it is a loss, reported separately so
    the board can show how much of a bin is silence rather than a dump.

    Without ``evidence`` (the caller did not look) a curve entry falls back
    to the migration t0 as its fill proxy.
    """
    if outcome is None:
        return None
    now = now or utcnow()
    at = _aware(decision.at) or now
    age_h = (now - at).total_seconds() / 3600.0
    if outcome.label is None and age_h < RESOLVE_HOURS:
        return None
    ev = _as_evidence(evidence)
    sight_mcap = float(decision.entry_mcap or 0.0)
    sight_liq = float(decision.liq or 0.0)
    entry = sight_mcap
    rebased = False
    filled = False
    unfilled = False
    fill_lag_min: float | None = None
    curve_entry = sight_liq < LEDGER_DEAD_LIQ
    fillable_at_sight = sight_liq >= LEDGER_SELLABLE_LIQ
    t0 = float(outcome.t0_mcap or 0.0)
    if not fillable_at_sight:
        if ev is not None:
            if ev.fill_mcap > 0:
                entry = ev.fill_mcap
                rebased = True
                filled = True
                if ev.fill_at is not None:
                    fill_lag_min = round((_aware(ev.fill_at) - at).total_seconds() / 60.0, 1)
            else:
                unfilled = True
        elif curve_entry and t0 > 0:
            # Scored on the bonding curve (no pool, or Pump's $3-$90 curve SOL
            # reported as "liquidity"). Nobody buys a $29 print; the first
            # buyable one is the migration t0. Live v69 board: every pre-
            # migration Sol entry "hit 2x" on graduation FDV alone.
            entry = t0
            rebased = True
    if entry <= 0 and not unfilled:
        return None
    last_mcap = float(outcome.last_mcap or 0.0)
    last_liq = float(outcome.last_liq or 0.0)
    ev_peak, ev_n = (ev.peak, ev.n) if ev is not None else (0.0, 0)
    observed = last_mcap > 0 or ev_n > 0
    # Outcome prints carry one liquidity figure (the last look); they vouch
    # for a peak only when that pool could take a ticket.
    sellable_book = last_liq >= LEDGER_SELLABLE_LIQ
    peak = max(ev_peak, last_mcap if sellable_book else 0.0)
    if unfilled:
        # No fill inside the horizon: the outcome's later look is not a trade we had.
        peak = 0.0
    # Same honesty cap as the desk: a 1150x leftover-FDV wick is a Dex artifact.
    multiple = min(peak / entry, LEDGER_MAX_MULTIPLE) if peak > 0 and entry > 0 else 0.0
    dead = 0 < last_liq < LEDGER_DEAD_LIQ or (last_liq <= 0 and last_mcap <= 0)
    # Never got a pool inside the horizon: no exit existed either.
    never_pooled = curve_entry and t0 <= 0 and last_liq < LEDGER_DEAD_LIQ
    dead = dead or never_pooled or unfilled or not observed
    exit_mcap = float(outcome.t24h_mcap or last_mcap or 0.0) if observed else 0.0
    exit_mult = exit_mcap / entry if exit_mcap > 0 and entry > 0 else 0.0
    return {
        "multiple": round(multiple, 3),
        "exit_multiple": round(exit_mult, 3),
        "hit2x": bool(multiple >= HIT_TARGET and not dead),
        "hit5x": bool(multiple >= HIT_STRETCH and not dead),
        "dead": bool(dead),
        "unobserved": not observed,
        "unfilled": unfilled,
        "under_half": bool(exit_mult > 0 and exit_mult < 0.5) or dead,
        "label": outcome.label,
        "entry_rebased": rebased,
        # Price the judge measured from, and how far above the sight print
        # the first fillable one sat (the graduation jump nobody captures).
        "fill_mcap": round(entry) if entry > 0 else 0,
        "fill_multiple": round(entry / sight_mcap, 2) if filled and sight_mcap > 0 else None,
        "fill_lag_min": fill_lag_min,
    }


def resolve_entry_decisions(
    session: Session,
    chain: str,
    *,
    now: datetime | None = None,
    since: datetime | None = None,
    sources: tuple[str, ...] | None = None,
    with_features: bool = False,
    limit: int | None = None,
) -> list[tuple[Decision, Outcome | None, dict[str, Any] | None]]:
    """Entry decisions with their forward result, evidence looked up in bulk.

    ``limit`` is for Learn/report routes only. Training fits omit it.
    """
    now = now or utcnow()
    rows = entry_decisions_with_outcomes(
        session, chain, since=since, sources=sources, with_features=with_features, limit=limit
    )
    ev = post_decision_evidence(session, (d for d, _o, _ in rows))
    return [(d, o, decision_result(d, o, now=now, evidence=ev.get(d.id, NO_EVIDENCE))) for d, o, _ in rows]


def _decile(p: float) -> int:
    return min(9, max(0, int(float(p or 0.0) * 10.0)))


def _iso_week(ts: datetime) -> str:
    y, w, _ = _aware(ts).isocalendar()
    return f"{y}-W{w:02d}"


def entry_decisions_with_outcomes(
    session: Session,
    chain: str,
    *,
    since: datetime | None = None,
    sources: tuple[str, ...] | None = None,
    with_features: bool = False,
    limit: int | None = None,
) -> list[tuple[Decision, Outcome | None, None]]:
    """Entry decisions + forward outcome. Backfill / historical never wrote
    decisions, so no Token join is needed. ``features_json`` is only loaded
    for training — 40k rows of it is not a report.

    ``limit`` takes the newest N (then returns chronological). Training
    callers omit it so the fit still sees the full honest set.
    """
    from sqlalchemy.orm import load_only

    chain = normalize_chain(chain)
    cols = [Decision.id, Decision.chain, Decision.mint, Decision.token_id, Decision.kind, Decision.at, Decision.source, Decision.scorer, Decision.entry_p, Decision.entry_mcap, Decision.liq, Decision.holders]
    if with_features:
        cols.append(Decision.features_json)
    q = (
        session.query(Decision, Outcome)
        .options(
            load_only(*cols),
        )
        .join(Token, Token.id == Decision.token_id)
        .outerjoin(Outcome, Outcome.token_id == Decision.token_id)
        .filter(Decision.chain == chain, Decision.kind == DECISION_ENTRY)
        # Belt and braces on source: the writers refuse backfill, the reader
        # filters too. ``is_historical`` is NOT a filter here: the desk flips
        # it on retirement (Sol at 18h, RH when quiet), which is exactly the
        # losers. Live v74 Sol board: 40 survivors judged, 21,610 retired
        # first-sight scores invisible. The writer already refused tokens
        # that were historical at first sight.
        .filter(Token.source.notin_(("backfill", "rh_backfill")))
    )
    if since is not None:
        q = q.filter(Decision.at >= since)
    if sources:
        q = q.filter(Decision.source.in_(sources))
    if limit:
        rows = [(d, o, None) for d, o in q.order_by(Decision.at.desc()).limit(int(limit)).all()]
        rows.reverse()
        return rows
    return [(d, o, None) for d, o in q.order_by(Decision.at.asc()).all()]


def honest_calibration(
    session: Session,
    chain: str,
    *,
    since: datetime | None = None,
    scorer: str | None = None,
    limit: int | None = REPORT_DECISION_CAP,
    window_days: int | None = None,
) -> dict[str, Any]:
    """Reliability by Entry decile from frozen decisions. Excludes backfill.

    ``scorer`` narrows the bins to one Entry scale (``legacy`` or
    ``first_sight``); a 0.4 first-sight print and a 0.4 legacy print are
    not the same claim, so the default mixes them only when asked (None).
    ``scorers`` in the result counts resolved rows per scale either way.

    Default window is ``CALIBRATION_DEFAULT_DAYS`` (honest recent bins).
    Training fits do not use this path.
    """
    now = utcnow()
    used_days = window_days
    if since is None:
        used_days = CALIBRATION_DEFAULT_DAYS if window_days is None else int(window_days)
        if used_days > 0:
            since = now - timedelta(days=used_days)
    rows = resolve_entry_decisions(session, chain, now=now, since=since, limit=limit)
    bins: dict[int, list[tuple[float, dict]]] = {}
    open_n = 0
    seed_n = 0
    unobserved_n = 0
    unfilled_n = 0
    by_scorer: dict[str, int] = {}
    open_by_scorer: dict[str, int] = {}
    for decision, _outcome, res in rows:
        sc = str(decision.scorer or SCORER_LEGACY)
        if res is None:
            open_by_scorer[sc] = open_by_scorer.get(sc, 0) + 1
            if scorer is None or sc == scorer:
                open_n += 1
            continue
        by_scorer[sc] = by_scorer.get(sc, 0) + 1
        if scorer is not None and sc != scorer:
            continue
        if decision.source == "seed_t0":
            seed_n += 1
        if res.get("unobserved"):
            unobserved_n += 1
        if res.get("unfilled"):
            unfilled_n += 1
        bins.setdefault(_decile(decision.entry_p), []).append((float(decision.entry_p), res))
    out = []
    for idx in sorted(bins):
        members = bins[idx]
        n = len(members)
        mults = [r["multiple"] for _, r in members]
        observed = [r for _, r in members if not r.get("unobserved")]
        out.append(
            {
                "bin": f"{idx / 10:.1f}-{(idx + 1) / 10:.1f}",
                "n": n,
                "predicted": round(sum(p for p, _ in members) / n, 3),
                "hit2x": round(sum(r["hit2x"] for _, r in members) / n, 3),
                "hit5x": round(sum(r["hit5x"] for _, r in members) / n, 3),
                "median_multiple": round(statistics.median(mults), 2) if mults else 0.0,
                "under_half": round(sum(r["under_half"] for _, r in members) / n, 3),
                "dead": round(sum(r["dead"] for _, r in members) / n, 3),
                # Share of the bin that never printed again after entry. A
                # loss for a buyer, but silence, not a dump.
                "unobserved": round(sum(bool(r.get("unobserved")) for _, r in members) / n, 3),
                # Share of the bin no buyer could fill: scored on the curve or
                # a thin book and never printed on a sellable pool inside the
                # horizon. A loss for the desk (it could not act), not a dump.
                "unfilled": round(sum(bool(r.get("unfilled")) for _, r in members) / n, 3),
                # Share judged from a later fill print rather than the sight print.
                "filled_later": round(sum(bool(r.get("fill_multiple")) for _, r in members) / n, 3),
                # Same hit rate on the rows a market actually showed us.
                "hit2x_observed": round(sum(r["hit2x"] for r in observed) / len(observed), 3) if observed else None,
                "n_observed": len(observed),
            }
        )
    current = desk_lines(session, chain)
    return {
        "chain": normalize_chain(chain),
        "basis": "entry",
        "scorer": scorer,
        "scorers": by_scorer,
        "open_by_scorer": open_by_scorer,
        "lines": current.as_dict(),
        "resolved": sum(b["n"] for b in out),
        "open": open_n,
        "seeded": seed_n,
        "unobserved": unobserved_n,
        "unfilled": unfilled_n,
        "horizon_hours": RESOLVE_HOURS,
        "window_days": used_days,
        "since": since.isoformat() if since else None,
        "decision_cap": limit,
        "truncated": bool(limit) and len(rows) >= int(limit),
        "bins": out,
        "note": "Frozen at-entry decisions vs forward 24h tape. Backfill excluded. Judged from the price a buyer could pay: the sight print on a sellable pool, else the first sellable print after it (the paper fill). A hit needs a later sellable print we actually saw (snapshot, tape bar or last Dex look) and a live pool at judgement; the outcome's lifetime high never counts. No fill inside the horizon is a loss (unfilled), no print at all is a loss (unobserved).",
    }


_LINE_SLOTS = ("lo", "hi")


def _empty_line_cells() -> dict[str, dict[str, Any]]:
    return {slot: {"n": 0, "resolved": 0, "hit2x": 0, "hit5x": 0, "mults": []} for slot in _LINE_SLOTS}


def honest_weekly(session: Session, chain: str, *, weeks: int = 8) -> dict[str, Any]:
    """Per ISO week: how the desk lines did, forward only.

    Cells are keyed ``lo`` / ``hi``. Each decision is read against the lines
    of the scorer that wrote it (legacy 0.70/0.90, first-sight 0.30/0.50), so
    a week that straddles the switch reports both under one honest line;
    ``scorers`` on the week says how many rows came from each.
    """
    now = utcnow()
    since = now - timedelta(weeks=weeks)
    rows = resolve_entry_decisions(session, chain, now=now, since=since, limit=REPORT_DECISION_CAP)
    table: dict[str, dict[str, Any]] = {}
    for decision, _outcome, res in rows:
        wk = _iso_week(decision.at)
        slot = table.setdefault(wk, {"week": wk, "decisions": 0, "scorers": {}, "lines": _empty_line_cells()})
        slot["decisions"] += 1
        sc = str(decision.scorer or SCORER_LEGACY)
        slot["scorers"][sc] = slot["scorers"].get(sc, 0) + 1
        dl = lines_for_scorer(sc, chain)
        for name, ln in (("lo", dl.lo), ("hi", dl.hi)):
            if float(decision.entry_p) < ln:
                continue
            cell = slot["lines"][name]
            cell["n"] += 1
            if res is None:
                continue
            cell["resolved"] += 1
            cell["hit2x"] += int(res["hit2x"])
            cell["hit5x"] += int(res["hit5x"])
            cell["mults"].append(res["multiple"])
    weeks_out = []
    for wk in sorted(table):
        slot = table[wk]
        for cell in slot["lines"].values():
            r = cell["resolved"]
            cell["hit2x_rate"] = round(cell["hit2x"] / r, 3) if r else None
            cell["hit5x_rate"] = round(cell["hit5x"] / r, 3) if r else None
            cell["median_multiple"] = round(statistics.median(cell["mults"]), 2) if cell["mults"] else None
            cell.pop("mults", None)
        weeks_out.append(slot)
    current = desk_lines(session, chain)
    return {
        "chain": normalize_chain(chain),
        "weeks": weeks_out,
        "horizon_hours": RESOLVE_HOURS,
        "decision_cap": REPORT_DECISION_CAP,
        "truncated": len(rows) >= REPORT_DECISION_CAP,
        "lines": current.as_dict(),
        "legacy_lines": LEGACY_LINES.as_dict(),
    }


# --------------------------------------------------------------------------
# Paper fills (persisted gated-90 line)
# --------------------------------------------------------------------------


def _launched_at(token: Token) -> datetime | None:
    from .scoring.hunt import sol_launch_at

    if normalize_chain(token.chain or "sol") == "sol":
        return _aware(sol_launch_at(token) or token.migrated_at or token.first_seen_at)
    return _aware(token.migrated_at or token.first_seen_at)


def _fill_candidates(session: Session, chain: str, *, min_p: float | None = None) -> list[tuple[HuntCard, Token]]:
    """Hunt cards at or above the high line of the scorer that froze them.

    ``min_p`` overrides the line for every card (tests / manual runs).
    """
    chain = normalize_chain(chain)
    filled = {
        m for (m,) in session.query(PaperFill.mint).filter(PaperFill.chain == chain, PaperFill.line == PAPER_LINE).all()
    }
    gated = {
        m for (m,) in session.query(Decision.mint).filter(Decision.chain == chain, Decision.kind == DECISION_GATE).all()
    }
    current = desk_lines(session, chain)
    # Watch-line first-sight cards are candidates for the Live gate
    # (Sol 0.10 / RH 0.25). Buy-on-sight is ``hi`` (0.14 Sol / 0.30 RH).
    # RH watch needs Live>=0.50; no fresh-fat bypass. Sol watch can
    # also fill a fresh fat book.
    floor = min_p if min_p is not None else min(LEGACY_LINES.hi, current.lo)
    rows = (
        session.query(HuntCard, Token)
        .join(Token, Token.id == HuntCard.token_id)
        .options(selectinload(Token.research), selectinload(Token.outcome))
        .filter(HuntCard.chain == chain, HuntCard.entry_p >= floor)
        .all()
    )
    from .scoring.first_sight import frozen_decision_entry

    out = []
    for h, t in rows:
        if t.mint in filled or t.mint in gated:
            continue
        frozen = frozen_decision_entry(session, t.id)
        entry = float(frozen[0] if frozen is not None else (h.entry_p or 0.0))
        scorer = (frozen[1] if frozen and frozen[1] else None) or h.scorer
        if min_p is not None:
            if entry >= min_p:
                out.append((h, t))
            continue
        lines = lines_for_scorer(scorer, chain)
        if entry >= lines.hi:
            out.append((h, t))
        elif (
            chain in ("sol", "robinhood")
            and (scorer or "") == SCORER_FIRST_SIGHT
            and entry >= lines.lo
        ):
            out.append((h, t))
    return out


def _gate_verdict(token: Token, hunt: HuntCard) -> tuple[str, str, float, float, list[str]]:
    """(verdict, veto, buy_mcap, liq, flags) for a 90+ card on its current book."""
    from .scoring.paper_gate import (
        PAPER_FIRST_BOOK_GRACE_MIN,
        PAPER_LATE_OK_KEY,
        paper_chase_multiple,
        paper_chase_quality,
        paper_fill_verdict,
        paper_hard_veto,
        paper_is_chase,
        paper_window_hours,
        stamp_paper_late_ok,
    )
    from .serialize import associated_dev_handle, display_token_x

    research = token.research
    outcome = token.outcome
    flags = [str(f) for f in _loads(research.risk_flags_json if research else "[]", [])]
    if research is None or outcome is None:
        return "fail", "no research", 0.0, 0.0, flags
    launched = _launched_at(token)
    now = utcnow()
    if launched is None:
        return "fail", "no launch time", 0.0, 0.0, flags
    age_min = (now - launched).total_seconds() / 60.0
    if age_min > paper_window_hours(token.chain or "sol") * 60.0:
        return "fail", "outside window", 0.0, 0.0, flags
    # First-book grace runs from the pool open (migration), not the
    # bonding-curve create stamp Sol launch age uses.
    pool_open = _aware(token.migrated_at or token.first_seen_at) or launched
    pool_age_min = (now - pool_open).total_seconds() / 60.0
    t0 = float(hunt.t0_mcap or outcome.t0_mcap or 0.0)
    last = float(hunt.last_mcap or outcome.last_mcap or 0.0)
    liq = float(hunt.last_liq or outcome.last_liq or 0.0)
    handle = display_token_x(research.twitter_handle, associated_dev_handle(token, research))
    veto = paper_hard_veto(
        flags,
        chain=token.chain or "sol",
        website=token.website or "",
        twitter=token.twitter or "",
        twitter_handle=handle,
        twitter_followers=float(research.twitter_followers or 0),
        twitter_age_days=float(research.twitter_age_days or 0),
        twitter_verified=bool(research.twitter_verified),
    )
    if veto:
        return "fail", veto, last or t0, liq, flags
    peak = float(outcome.max_mcap or last or 0.0)
    feat = _loads(research.features_json, {})
    if not isinstance(feat, dict):
        feat = {}
    live_hint = float(hunt.conviction_p or 0.0) or None
    verdict = paper_fill_verdict(
        entry_mcap=t0,
        last_mcap=last,
        last_liq=liq,
        max_mcap=peak,
        chain=token.chain or "sol",
        flags=flags,
        website=token.website or "",
        twitter=token.twitter or "",
        twitter_handle=handle,
        twitter_followers=float(research.twitter_followers or 0),
        twitter_age_days=float(research.twitter_age_days or 0),
        twitter_verified=bool(research.twitter_verified),
        features=feat,
        live_p=live_hint,
    )
    if verdict != "pass":
        # No liquid book yet. Keep looking only inside the first-book grace;
        # a pool that appears hours later is not a graduation fill.
        if liq < PAPER_SELLABLE_LIQ and pool_age_min <= PAPER_FIRST_BOOK_GRACE_MIN:
            return "wait", "", last or t0, liq, flags
        if liq < PAPER_SELLABLE_LIQ:
            return "fail", "no liquid book in grace", last or t0, liq, flags
        reason = verdict if verdict != "fail" else "tape dump or under hold"
        return "fail", reason, last or t0, liq, flags
    if paper_is_chase(t0, last or t0) and not feat.get(PAPER_LATE_OK_KEY):
        quality = paper_chase_quality(
            features=feat,
            live_p=live_hint,
            flags=flags,
            chain=token.chain or "sol",
            website=token.website or "",
            twitter=token.twitter or "",
            twitter_handle=handle,
            twitter_followers=float(research.twitter_followers or 0),
            twitter_age_days=float(research.twitter_age_days or 0),
            twitter_verified=bool(research.twitter_verified),
            entry_mcap=t0,
            multiple=paper_chase_multiple(t0, last or t0),
        )
        if quality["ok"]:
            research.features_json = json.dumps(
                stamp_paper_late_ok(
                    feat,
                    why=quality["why"],
                    multiple=paper_chase_multiple(t0, last or t0),
                )
            )
    return "pass", "", last or t0, liq, flags


def _paper_live_p(session: Session, chain: str, hunt: HuntCard, token: Token) -> float | None:
    """Promoted Live-model health for a Hunt card, or None if none is fitted."""
    from .scoring.live_fit import live_features_for_token, live_model_p

    research = token.research
    outcome = token.outcome
    return live_model_p(
        session,
        chain,
        live_features_for_token(
            session,
            token,
            entry_p=float(hunt.entry_p or 0.0),
            t0_mcap=float(hunt.t0_mcap or (outcome.t0_mcap if outcome else 0.0) or 0.0),
            last_mcap=float(hunt.last_mcap or (outcome.last_mcap if outcome else 0.0) or 0.0),
            last_liq=float(hunt.last_liq or (outcome.last_liq if outcome else 0.0) or 0.0),
            holders=int((research.holder_count if research else 0) or hunt.holders or 0),
            peak=float((outcome.max_mcap if outcome else 0.0) or hunt.last_mcap or 0.0),
            trough=float(hunt.last_mcap or (outcome.last_mcap if outcome else 0.0) or 0.0),
        ),
    )


def _paper_tape_live(
    session: Session,
    chain: str,
    token: Token,
    fill: PaperFill,
    *,
    last: float,
    liq: float,
    peak: float,
) -> float | None:
    """Hunt Live for a paper exit. Tape wins a dump; the model does not.

    OAK stayed open because the exit read the Live model (still ≥ 0.35)
    while the tape was already dust. Same rules on the board and after
    the card is gone. A missing last stays missing.
    """
    from .scoring.hunt import conviction_from_tape, pick_live_conviction
    from .scoring.live_fit import live_features_for_token, live_model_p

    outcome = token.outcome
    research = token.research
    entry_p = float(fill.entry_p or 0.0)
    t0 = float((outcome.t0_mcap if outcome else 0.0) or fill.entry_mcap or 0.0)
    last_m = float(last or 0.0)
    if last_m <= 0 or t0 <= 0:
        return None
    holders = int((research.holder_count if research else 0) or 0)
    peak_m = max(float(peak or 0.0), last_m)
    flags: list[str] = []
    if research is not None and research.risk_flags_json:
        try:
            parsed = json.loads(research.risk_flags_json)
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            flags = [str(flag) for flag in parsed]
    scorer = (research.scorer if research is not None else None) or SCORER_LEGACY
    lines = lines_for_scorer(scorer, chain)
    tape = conviction_from_tape(
        chain=chain,
        entry_p=entry_p,
        multiple=last_m / t0,
        last_mcap=last_m,
        t0_mcap=t0,
        max_mcap=peak_m,
        last_liq=float(liq or 0.0),
        holders=holders,
        top10_pct=float((research.top10_pct if research else 0.0) or 0.0),
        flags=flags,
        label=(outcome.label if outcome is not None else None),
        thin_entry=lines.thin,
    )
    model = live_model_p(
        session,
        chain,
        live_features_for_token(
            session,
            token,
            entry_p=entry_p,
            t0_mcap=t0,
            last_mcap=last_m,
            last_liq=float(liq or 0.0),
            holders=holders,
            peak=peak_m,
            trough=last_m,
        ),
    )
    return pick_live_conviction(
        tape,
        allow_bloom=False,
        last_mcap=last_m,
        t0_mcap=t0,
        max_mcap=peak_m,
        model_p=model,
    )


def _shadow_vetoes() -> frozenset[str]:
    from .scoring.paper_gate import PAPER_SHADOW_VETOES

    return PAPER_SHADOW_VETOES


def _write_shadow_fill(
    session: Session,
    token: Token,
    gate: Decision,
    *,
    now: datetime,
    evidence: Evidence,
    buy_mcap: float | None = None,
    liq: float | None = None,
) -> PaperFill | None:
    """One shadow_late fill per mint. Closed from the judge when the 24h result exists.

    The peak is a post-decision sellable print (``evidence`` / ``decision_result``).
    ``Outcome.max_mcap`` does not vouch. No ticket.
    """
    chain = normalize_chain(gate.chain or token.chain or "sol")
    exists = (
        session.query(PaperFill.id)
        .filter(PaperFill.chain == chain, PaperFill.mint == token.mint, PaperFill.line == PAPER_SHADOW_LINE)
        .first()
    )
    if exists:
        return None
    entry = float(gate.entry_mcap if buy_mcap is None else buy_mcap)
    if entry <= 0:
        return None
    liq_v = float(gate.liq if liq is None else liq)
    outcome = token.outcome
    res = decision_result(gate, outcome, now=now, evidence=evidence)
    peak = max(entry, float(getattr(evidence, "peak", 0.0) or 0.0))
    last = entry
    status = "open"
    reason = ""
    closed_at = None
    return_pct = None
    if res is not None:
        peak = max(peak, float(res.get("multiple") or 0.0) * entry)
        exit_m = float(res.get("exit_multiple") or 0.0) * entry
        last = exit_m if exit_m > 0 else last
        dead = bool(res.get("dead"))
        ret = moonbag_return(
            entry=entry,
            peak=peak,
            exit_mcap=last,
            target=PAPER_TARGET,
            ride=PAPER_RIDE,
            dead=dead,
        )
        status = "closed"
        reason = "ride" if peak >= PAPER_RIDE * entry else ("dead pool" if dead else "24h")
        closed_at = now
        return_pct = round(ret * 100.0, 1)
    elif outcome is not None and float(outcome.last_mcap or 0.0) > 0:
        last = float(outcome.last_mcap)
        if float(outcome.last_liq or 0.0) >= PAPER_SELLABLE_LIQ:
            peak = max(peak, last)
    fill = PaperFill(
        chain=chain,
        mint=token.mint,
        token_id=token.id,
        decision_id=gate.id,
        line=PAPER_SHADOW_LINE,
        opened_at=_aware(gate.at) or now,
        entry_p=float(gate.entry_p or 0.0),
        entry_mcap=entry,
        entry_liq=liq_v,
        target=PAPER_TARGET,
        ride=PAPER_RIDE,
        max_mcap=peak,
        min_mcap=min(entry, last) if last > 0 else entry,
        last_mcap=last,
        last_liq=liq_v,
        status=status,
        closed_at=closed_at,
        exit_mcap=last if status == "closed" else 0.0,
        exit_reason=reason,
        return_pct=return_pct,
        image_rev=IMAGE_REV,
        updated_at=now,
    )
    session.add(fill)
    session.flush()
    return fill


def backfill_shadow_late(session: Session, chain: str, *, now: datetime | None = None) -> int:
    """Open shadow fills for late vetoes already on the gate ledger."""
    now = now or utcnow()
    chain = normalize_chain(chain)
    have = {
        mint
        for (mint,) in session.query(PaperFill.mint).filter(PaperFill.chain == chain, PaperFill.line == PAPER_SHADOW_LINE).all()
    }
    gates = (
        session.query(Decision)
        .filter(Decision.chain == chain, Decision.kind == DECISION_GATE, Decision.veto.in_(tuple(_shadow_vetoes())))
        .all()
    )
    pending = [gate for gate in gates if gate.mint not in have]
    if not pending:
        return 0
    tokens = {
        token.id: token
        for token in session.query(Token).options(selectinload(Token.outcome)).filter(Token.id.in_([g.token_id for g in pending])).all()
    }
    evidence = post_decision_evidence(session, pending)
    n = 0
    for gate in pending:
        token = tokens.get(gate.token_id)
        if token is None:
            continue
        if _write_shadow_fill(session, token, gate, now=now, evidence=evidence.get(gate.id, NO_EVIDENCE)) is not None:
            n += 1
    if n:
        log.info("shadow late opened %s fills on %s", n, chain)
    return n


def _v1_locked(session: Session, day: str) -> bool:
    key = f"paper_v1:locked:{day}"
    return session.query(ScanState.id).filter(ScanState.key == key).first() is not None


def _v1_book_day(fill: PaperFill) -> str:
    from .scoring.paper_v1 import book_day_from_reason, v1_day

    stamped = book_day_from_reason(fill.exit_reason or "")
    if stamped:
        return stamped
    opened = _aware(fill.opened_at)
    return v1_day(opened) if opened else ""


def _v1_opened_since(day: str, extra_days: int = PAPER_V1_DAY_LOOKBACK_DAYS) -> datetime:
    """Lower bound for paperV1 day-scoped pulls. Honest for today's cap."""
    base = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return base - timedelta(days=max(1, int(extra_days)))


def _v1_taken(session: Session, day: str) -> int:
    """Open or closed paperV1 rows already spending that UTC day's total."""
    from .scoring.paper_v1 import PAPER_V1_LINE

    rows = (
        session.query(PaperFill)
        .filter(
            PaperFill.line == PAPER_V1_LINE,
            PaperFill.status.in_(("open", "closed")),
            PaperFill.opened_at >= _v1_opened_since(day),
        )
        .all()
    )
    return sum(1 for fill in rows if _v1_book_day(fill) == day)


def _v1_taken_chain(session: Session, day: str, chain: str) -> int:
    """Open/closed paperV1 rows for one chain on that UTC day (per-chain lane)."""
    from .scoring.paper_v1 import PAPER_V1_LINE

    chain = normalize_chain(chain)
    rows = (
        session.query(PaperFill)
        .filter(
            PaperFill.line == PAPER_V1_LINE,
            PaperFill.chain == chain,
            PaperFill.status.in_(("open", "closed")),
            PaperFill.opened_at >= _v1_opened_since(day),
        )
        .all()
    )
    return sum(1 for fill in rows if _v1_book_day(fill) == day)


def _v1_taken_by_chain(session: Session, day: str) -> dict[str, int]:
    """Cap spend broken out by chain (independent 3/day lanes)."""
    from .scoring.paper_v1 import PAPER_V1_LINE

    rows = (
        session.query(PaperFill)
        .filter(
            PaperFill.line == PAPER_V1_LINE,
            PaperFill.status.in_(("open", "closed", "queued")),
            PaperFill.opened_at >= _v1_opened_since(day),
        )
        .all()
    )
    out = {"sol": 0, "robinhood": 0, "queued_sol": 0, "queued_robinhood": 0}
    for fill in rows:
        if _v1_book_day(fill) != day:
            continue
        chain = normalize_chain(fill.chain or "sol")
        if fill.status == "queued":
            key = f"queued_{chain}"
            out[key] = out.get(key, 0) + 1
        else:
            out[chain] = out.get(chain, 0) + 1
    return out


def freeze_live_at_entry(
    session: Session,
    token_id: int | None,
    live_p: float | None,
    *,
    src: str = "live_model",
    now: datetime | None = None,
) -> bool:
    """Stamp Live once onto the entry Decision.features_json (side keys).

    Does not grow FEATURE_NAMES. Never overwrites an existing freeze.
    Returns True when a stamp was written.
    """
    from .scoring.paper_v1 import merge_live_at_entry

    if not token_id or live_p is None:
        return False
    entry = (
        session.query(Decision)
        .filter(Decision.token_id == int(token_id), Decision.kind == DECISION_ENTRY)
        .order_by(Decision.id.desc())
        .first()
    )
    if entry is None:
        return False
    try:
        feat = json.loads(entry.features_json or "{}")
    except Exception:
        feat = {}
    if not isinstance(feat, dict):
        feat = {}
    merged = merge_live_at_entry(feat, live_p, src=src, at=now or utcnow())
    if merged is None:
        return False
    entry.features_json = json.dumps(merged)
    entry.features_hash = features_hash(entry.features_json)
    session.flush()
    return True


def _stamp_entry_paper_miss(
    session: Session,
    token_id: int | None,
    *,
    reason: str,
    now: datetime | None = None,
) -> bool:
    """Write-once paper-miss join + early feature freeze. Side keys only."""
    from .scoring.early_book import stamp_early_book_side_key
    from .scoring.miss_cohort import (
        freeze_paper_miss_features,
        is_paper_miss_reason,
        stamp_paper_miss_join,
    )

    if not token_id or not is_paper_miss_reason(reason):
        return False
    entry = (
        session.query(Decision)
        .filter(Decision.token_id == int(token_id), Decision.kind == DECISION_ENTRY)
        .order_by(Decision.id.desc())
        .first()
    )
    if entry is None:
        return False
    try:
        feat = json.loads(entry.features_json or "{}")
    except Exception:
        feat = {}
    if not isinstance(feat, dict):
        feat = {}
    token = session.get(Token, int(token_id))
    research = token.research if token is not None else None
    snap = freeze_paper_miss_features(feat, decision=entry, research=research)
    t0 = float(entry.entry_mcap or 0.0)
    changed = False
    merged = stamp_paper_miss_join(feat, reason=reason, snap=snap, at=now or utcnow())
    if merged is not None:
        feat = merged
        changed = True
    early = stamp_early_book_side_key(feat, snap, t0_mcap=t0)
    if early is not None:
        feat = early
        changed = True
    if not changed:
        return False
    entry.features_json = json.dumps(feat)
    entry.features_hash = features_hash(entry.features_json)
    session.flush()
    return True


def _stamp_entry_late_ok(
    session: Session,
    token_id: int | None,
    *,
    why: list[str],
    multiple: float,
) -> bool:
    """Stamp late-but-good onto the entry Decision. Side keys only."""
    from .scoring.paper_gate import PAPER_LATE_OK_KEY, stamp_paper_late_ok

    if not token_id:
        return False
    entry = (
        session.query(Decision)
        .filter(Decision.token_id == int(token_id), Decision.kind == DECISION_ENTRY)
        .order_by(Decision.id.desc())
        .first()
    )
    if entry is None:
        return False
    try:
        feat = json.loads(entry.features_json or "{}")
    except Exception:
        feat = {}
    if not isinstance(feat, dict):
        feat = {}
    if feat.get(PAPER_LATE_OK_KEY):
        return False
    entry.features_json = json.dumps(stamp_paper_late_ok(feat, why=why, multiple=multiple))
    entry.features_hash = features_hash(entry.features_json)
    session.flush()
    return True


def cancel_provisional_paper_on_veto(
    session: Session,
    token: Token,
    veto: str,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Close paper-only thin opens when background enrich hard-vetoes.

    No live orders. Skipped rows do not spend the paperV1 day cap and do
    not enter this-window EV. Non-provisional fills stay put.
    """
    from .scoring.paper_gate import (
        PAPER_ENRICH_VETO,
        PAPER_OPEN_VIA_THIN,
        research_is_provisional,
        stamp_paper_provisional,
    )
    from .scoring.paper_v1 import PAPER_V1_LINE

    now = now or utcnow()
    research = token.research
    feat: dict[str, Any] = {}
    if research is not None:
        feat = _loads(research.features_json, {})
        if not isinstance(feat, dict):
            feat = {}
    provisional = research_is_provisional(feat)
    fills = (
        session.query(PaperFill)
        .filter(
            PaperFill.token_id == token.id,
            PaperFill.status.in_(("open", "queued")),
            PaperFill.line.in_((PAPER_LINE, PAPER_V1_LINE)),
        )
        .all()
    )
    cancelled = 0
    tickets = 0
    reason = (PAPER_ENRICH_VETO if not veto else str(veto).strip()[:32]) or PAPER_ENRICH_VETO
    if len(reason) > 32:
        reason = PAPER_ENRICH_VETO
    for fill in fills:
        thin_fill = (fill.open_via or "") == PAPER_OPEN_VIA_THIN
        if not provisional and not thin_fill:
            continue
        fill.status = "skipped"
        fill.exit_reason = reason
        fill.closed_at = now
        fill.updated_at = now
        cancelled += 1
        ticket = session.query(Ticket).filter(Ticket.fill_id == fill.id).first()
        if ticket is not None and ticket.status == "shadow":
            ticket.status = "skipped"
            tickets += 1
    if research is not None:
        feat = stamp_paper_provisional(feat, False)
        feat["paper_enrich_veto"] = str(veto or "")[:64]
        research.features_json = json.dumps(feat)
    if cancelled:
        session.flush()
        log.info(
            "provisional paper cancelled %s fills (%s tickets) for %s veto=%s",
            cancelled,
            tickets,
            token.mint,
            veto,
        )
    return {"cancelled": cancelled, "tickets": tickets}


def _v1_signal(session: Session, token_id: int | None, decision_id: int | None = None) -> dict[str, Any]:
    """Cohort signal for paperV1 ranking (frozen features + early wallets)."""
    from .scoring.signal_score import early_wallet_hits, v1_signal_from_features

    feat = _v1_entry_features(session, token_id, decision_id)
    return v1_signal_from_features(feat, early_hits=early_wallet_hits(session, token_id))


def _v1_thesis(session: Session, token_id: int | None, decision_id: int | None = None) -> dict[str, Any]:
    """Thesis score using current refit weights when available."""
    from .scoring.paper_v1 import v1_thesis_from_features
    from .scoring.thesis_weights import load_thesis_weights

    return v1_thesis_from_features(
        _v1_entry_features(session, token_id, decision_id),
        weights=load_thesis_weights(session),
    )


def _v1_entry_features(session: Session, token_id: int | None, decision_id: int | None = None) -> dict[str, Any]:
    """Frozen entry features for paperV1 ranking — never live Research.

    Wide fills pass a gate Decision id whose ``features_json`` is empty.
    Always prefer the entry Decision for the token so thesis columns
    (github_auth_n / real_project / gmgn_cto / name_quality) are readable.
    """
    def _parse(row: Decision | None) -> dict[str, Any]:
        if row is None:
            return {}
        try:
            data = json.loads(row.features_json or "{}")
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    entry = None
    if token_id:
        entry = (
            session.query(Decision)
            .filter(Decision.token_id == token_id, Decision.kind == DECISION_ENTRY)
            .order_by(Decision.id.desc())
            .first()
        )
    entry_feat = _parse(entry)
    research = (
        session.query(Research).filter(Research.token_id == token_id).one_or_none() if token_id else None
    )
    if research is not None:
        from .scoring.thesis_enrich import merge_thesis_feature_dict, sync_research_thesis_features

        token = session.get(Token, int(token_id)) if token_id else None
        res_feat = sync_research_thesis_features(research, token)
        merged = merge_thesis_feature_dict(entry_feat, res_feat)
        if merged:
            return merged
    if entry_feat:
        return entry_feat
    # Fall back only when no entry blob exists (rare); never trust an empty gate.
    if decision_id:
        row = session.get(Decision, decision_id)
        if row is not None and row.kind == DECISION_ENTRY:
            return _parse(row)
    return {}


def _consider_paper_v1(
    session: Session,
    chain: str,
    token: Token,
    *,
    entry: float,
    live: float | None,
    hi: float,
    buy_mcap: float,
    liq: float,
    decision_id: int | None,
    now: datetime,
    late_ok: bool = False,
) -> PaperFill | None:
    """Queue a side-list row for a name that just passed the wide gate.

    No ticket. The wide fill is a different line. A Live score at or above
    0.70 takes a slot now, before the 23:00 lock, while the day is still open.
    ``late_ok`` skips leftover-of-the-print: leftover stays a first-seen
    ($1M t0) reject, not a quality name that ran past $1M last.
    """
    from .scoring.paper_v1 import (
        PAPER_V1_CAP_PER_CHAIN,
        PAPER_V1_LINE,
        PAPER_V1_OPEN_VIA_THIN,
        PAPER_V1_QUEUED,
        lock_due,
        next_v1_day,
        v1_day,
        v1_day_reason,
        v1_immediate,
        v1_leftover_reject,
        v1_qualifies,
        v1_queue_reason,
        v1_score_path,
    )

    from .risk import paper_v1_blocked
    from .scoring.thesis_enrich import v1_hard_tag_ready

    if paper_v1_blocked(session, chain):
        return None
    try:
        from .scoring.thesis_enrich import prime_paper_v1_thesis_for_qualify

        prime_paper_v1_thesis_for_qualify(session, token)
    except Exception:
        log.exception("paperV1 thesis prime failed for %s", token.mint)
    thin = False
    try:
        ready = v1_hard_tag_ready(session, token.id, decision_id)
    except Exception:
        log.exception("paperV1 thesis sync failed for %s", token.mint)
        ready = False
    if not ready:
        # Score path on migrate + Dex / Live — queue only. Lock and midday
        # promote still require a hard tag after background enrich.
        if not v1_score_path(chain, entry, live, hi=hi):
            return None
        thin = True
    # Freeze Live at first paper sight so Sol score-path is measurable later.
    try:
        freeze_live_at_entry(session, token.id, live, src="paper_qualify", now=now)
    except Exception:
        log.exception("live_at_entry freeze failed for %s", token.mint)
    thesis = _v1_thesis(session, token.id, decision_id)
    leftover_mcap = float(buy_mcap or 0.0)
    if late_ok:
        leftover_mcap = float(
            (token.outcome.t0_mcap if token.outcome is not None else 0.0) or leftover_mcap
        )
    if v1_leftover_reject(leftover_mcap):
        # Zero-hour short list only — leftovers stay on the wide training ocean.
        # Quality late-ok uses first-seen t0 so a big run is not leftover.
        return None
    if not thin and not v1_qualifies(chain, entry, live, hi=hi, thesis=thesis):
        return None
    if (
        session.query(PaperFill.id)
        .filter(PaperFill.chain == chain, PaperFill.mint == token.mint, PaperFill.line == PAPER_V1_LINE)
        .first()
    ):
        return None
    today = v1_day(now)
    locked = _v1_locked(session, today)
    day = next_v1_day(today) if locked else today
    # Live >= 0.70 or strong thesis takes a per-chain slot before 23:00.
    # Thin-facts score-path never immediate-opens — enrich may still veto.
    immediate = (
        not thin
        and not locked
        and not lock_due(today, now)
        and v1_immediate(live, thesis)
        and _v1_taken_chain(session, today, chain) < PAPER_V1_CAP_PER_CHAIN
    )
    row = PaperFill(
        chain=chain,
        mint=token.mint,
        token_id=token.id,
        decision_id=decision_id,
        line=PAPER_V1_LINE,
        opened_at=now,
        entry_p=entry,
        entry_mcap=buy_mcap,
        entry_liq=liq,
        target=PAPER_TARGET,
        ride=PAPER_RIDE,
        max_mcap=buy_mcap,
        min_mcap=buy_mcap,
        last_mcap=buy_mcap,
        last_liq=liq,
        status="open" if immediate else PAPER_V1_QUEUED,
        exit_reason=v1_day_reason(day) if immediate else v1_queue_reason(live, day),
        open_via=PAPER_V1_OPEN_VIA_THIN if thin else None,
        image_rev=IMAGE_REV,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def _consider_paper_v1_shadow(
    session: Session,
    chain: str,
    token: Token,
    *,
    entry: float,
    live: float | None,
    hi: float,
    buy_mcap: float,
    liq: float,
    decision_id: int | None,
    now: datetime,
) -> PaperFill | None:
    """Negative label: almost cleared the short list but did not queue.

    Separate line so the 5/day cap and wide book stay untouched. Paper only.
    """
    from .scoring.paper_v1 import (
        PAPER_V1_LINE,
        PAPER_V1_SHADOW_LINE,
        PAPER_V1_SKIPPED,
        v1_day,
        v1_miss_reason,
        v1_near_qualify,
        v1_skip_stamp,
    )
    from .scoring.thesis_enrich import ensure_entry_thesis_from_stored

    try:
        ensure_entry_thesis_from_stored(session, token.id)
    except Exception:
        log.exception("paperV1 shadow thesis sync failed for %s", token.mint)
    thesis = _v1_thesis(session, token.id, decision_id)
    if not v1_near_qualify(chain, entry, live, hi=hi, thesis=thesis):
        return None
    if (
        session.query(PaperFill.id)
        .filter(
            PaperFill.chain == chain,
            PaperFill.mint == token.mint,
            PaperFill.line.in_((PAPER_V1_LINE, PAPER_V1_SHADOW_LINE)),
        )
        .first()
    ):
        return None
    day = v1_day(now)
    reason = v1_miss_reason(chain, entry, live, hi=hi, thesis=thesis)
    row = PaperFill(
        chain=chain,
        mint=token.mint,
        token_id=token.id,
        decision_id=decision_id,
        line=PAPER_V1_SHADOW_LINE,
        opened_at=now,
        entry_p=entry,
        entry_mcap=buy_mcap,
        entry_liq=liq,
        target=PAPER_TARGET,
        ride=PAPER_RIDE,
        max_mcap=buy_mcap,
        min_mcap=buy_mcap,
        last_mcap=buy_mcap,
        last_liq=liq,
        status=PAPER_V1_SKIPPED,
        exit_reason=v1_skip_stamp(reason, day),
        image_rev=IMAGE_REV,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    try:
        _stamp_entry_paper_miss(session, token.id, reason=reason, now=now)
    except Exception:
        log.exception("paper-miss stamp failed for %s", token.mint)
    return row


def _v1_has_row(session: Session, chain: str, mint: str) -> bool:
    from .scoring.paper_v1 import PAPER_V1_LINE, PAPER_V1_SHADOW_LINE

    return (
        session.query(PaperFill.id)
        .filter(
            PaperFill.chain == chain,
            PaperFill.mint == mint,
            PaperFill.line.in_((PAPER_V1_LINE, PAPER_V1_SHADOW_LINE)),
        )
        .first()
        is not None
    )


def _write_v1_shadow_reason(
    session: Session,
    fill: PaperFill,
    *,
    reason: str,
    day: str,
    buy_mcap: float,
    liq: float,
    now: datetime,
) -> PaperFill:
    """Shadow row carrying an explicit skip reason for a wide fill's mint."""
    from .scoring.paper_v1 import PAPER_V1_SHADOW_LINE, PAPER_V1_SKIPPED, v1_skip_stamp

    row = PaperFill(
        chain=fill.chain,
        mint=fill.mint,
        token_id=fill.token_id,
        decision_id=fill.decision_id,
        line=PAPER_V1_SHADOW_LINE,
        opened_at=now,
        entry_p=float(fill.entry_p or 0.0),
        entry_mcap=buy_mcap,
        entry_liq=liq,
        target=PAPER_TARGET,
        ride=PAPER_RIDE,
        max_mcap=buy_mcap,
        min_mcap=buy_mcap,
        last_mcap=buy_mcap,
        last_liq=liq,
        status=PAPER_V1_SKIPPED,
        exit_reason=v1_skip_stamp(reason, day),
        image_rev=IMAGE_REV,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    try:
        _stamp_entry_paper_miss(session, fill.token_id, reason=reason, now=now)
    except Exception:
        log.exception("paper-miss stamp failed for %s", fill.mint)
    return row


def reconsider_paper_v1(session: Session, chain: str, *, now: datetime | None = None) -> dict[str, int]:
    """Re-run paperV1 qualify for wide fills that opened Live-cold. Paper only.

    Cycle-4 knob (SELF_IMPROVE_PLAN §M1): *when* qualify is evaluated, not
    the floors. ``_consider_paper_v1`` runs once per wide fill, at the first
    liquid print, when Sol Live is still cold — so Sol never wrote a row of
    any kind. On every paper sync, for ``PAPER_V1_RECONSIDER_HOURS`` after
    the fill, this re-runs the unchanged qualify with the current Hunt Live
    and the current sellable print. Guards stay: leftover (≥ $1M) reject,
    late chase (≥ ``PAPER_CHASE_MULT`` × t0) refuse unless quality
    bypasses the veto entirely, hard-tag soft path,
    3/chain cap and the 23:00 lock all live inside ``_consider_paper_v1``.

    When the window ends without a row the day still gets a label: a
    near-miss shadow if it nearly qualified, else ``v1 live-cold``.
    Live is frozen at the reconsider moment with ``src="paper_reconsider"``;
    ``freeze_live_at_entry`` never overwrites an earlier stamp, so a Live
    frozen at the wide fill stays the entry read and the queued row's
    ``q:<live>`` carries the reconsider-time value.

    Only chains in ``PAPER_V1_RECONSIDER_CHAINS`` (Sol) are touched; RH
    qualifies on Entry and needs no second look. Wide fills that already
    have a ``paper_v1`` or ``paper_v1_shadow`` row are left alone.
    """
    from .scoring.first_sight import frozen_decision_entry
    from .scoring.paper_gate import (
        paper_chase_multiple,
        paper_chase_quality,
        paper_is_chase,
    )
    from .scoring.paper_v1 import (
        PAPER_V1_RECONSIDER_CHAINS,
        PAPER_V1_RECONSIDER_HOURS,
        PAPER_V1_SKIP_LATE_CHASE,
        PAPER_V1_SKIP_LEFTOVER,
        PAPER_V1_SKIP_LIVE_COLD,
        v1_day,
        v1_leftover_reject,
    )
    from .risk import paper_v1_blocked

    out = {"scanned": 0, "queued": 0, "opened": 0, "shadow": 0, "waiting": 0}
    chain = normalize_chain(chain)
    if chain not in PAPER_V1_RECONSIDER_CHAINS:
        return out
    now = now or utcnow()
    if paper_v1_blocked(session, chain):
        # Kill switch / chain pause: no rows of any kind, same as the lock.
        return out
    window = timedelta(hours=PAPER_V1_RECONSIDER_HOURS)
    # Today's book plus anything whose window ended within the last hour
    # (so the expiry tick is never scanned out from under it). Older
    # unlabelled fills predate the knob and are not backfilled.
    day_start = _aware(now).replace(hour=0, minute=0, second=0, microsecond=0)
    since = min(day_start, now - window - timedelta(hours=1))
    fills = (
        session.query(PaperFill)
        .filter(
            PaperFill.chain == chain,
            PaperFill.line == PAPER_LINE,
            PaperFill.opened_at >= since,
        )
        .order_by(PaperFill.id.asc())
        .all()
    )
    for fill in fills:
        if _v1_has_row(session, chain, fill.mint):
            continue
        out["scanned"] += 1
        token = session.get(Token, fill.token_id) if fill.token_id else None
        if token is None:
            continue
        opened = _aware(fill.opened_at) or now
        expired = fill.status != "open" or (now - opened) > window
        hunt = session.query(HuntCard).filter(HuntCard.chain == chain, HuntCard.mint == fill.mint).first()
        outcome = token.outcome
        t0 = float((hunt.t0_mcap if hunt else 0.0) or (outcome.t0_mcap if outcome else 0.0) or fill.entry_mcap or 0.0)
        last = float((hunt.last_mcap if hunt else 0.0) or (outcome.last_mcap if outcome else 0.0) or fill.last_mcap or 0.0)
        liq = float((hunt.last_liq if hunt else 0.0) or (outcome.last_liq if outcome else 0.0) or fill.last_liq or 0.0)
        sellable = last > 0 and liq >= PAPER_SELLABLE_LIQ
        book_day = v1_day(opened)
        try:
            live: float | None = None
            if hunt is not None:
                live = _paper_live_p(session, chain, hunt, token)
                if live is None and float(hunt.conviction_p or 0.0) > 0:
                    live = float(hunt.conviction_p or 0.0)
            refuse = ""
            late_ok = False
            late_why: list[str] = []
            if sellable and paper_is_chase(t0, last):
                feat = _v1_entry_features(session, token.id, fill.decision_id)
                quality = paper_chase_quality(
                    features=feat,
                    live_p=live,
                    chain=chain,
                    entry_mcap=t0,
                    multiple=paper_chase_multiple(t0, last),
                )
                if quality["ok"]:
                    late_ok = True
                    late_why = list(quality["why"] or [])
                else:
                    refuse = PAPER_V1_SKIP_LATE_CHASE
            elif v1_leftover_reject(t0) or (sellable and v1_leftover_reject(last)):
                refuse = PAPER_V1_SKIP_LEFTOVER
            if refuse:
                # Would never open at this print regardless of Live: label now.
                _write_v1_shadow_reason(
                    session, fill, reason=refuse, day=book_day, buy_mcap=last or t0, liq=liq, now=now
                )
                out["shadow"] += 1
                continue
            if not sellable and not expired:
                out["waiting"] += 1
                continue
            try:
                freeze_live_at_entry(session, token.id, live, src="paper_reconsider", now=now)
            except Exception:
                log.exception("live_at_entry freeze failed for %s", token.mint)
            if late_ok:
                try:
                    _stamp_entry_late_ok(
                        session,
                        token.id,
                        why=late_why,
                        multiple=paper_chase_multiple(t0, last),
                    )
                except Exception:
                    log.exception("late-ok stamp failed for %s", token.mint)
            frozen = frozen_decision_entry(session, token.id)
            entry = float(fill.entry_p or (frozen[0] if frozen is not None else 0.0) or 0.0)
            scorer = (frozen[1] if frozen and frozen[1] else None) or (hunt.scorer if hunt else None)
            hi = float(lines_for_scorer(scorer, chain).hi)
            buy_mcap = last if sellable else float(fill.last_mcap or fill.entry_mcap or 0.0)
            row = None
            if fill.status == "open" and sellable:
                row = _consider_paper_v1(
                    session,
                    chain,
                    token,
                    entry=entry,
                    live=live,
                    hi=hi,
                    buy_mcap=buy_mcap,
                    liq=liq,
                    decision_id=fill.decision_id,
                    now=now,
                    late_ok=late_ok,
                )
            if row is not None:
                if late_ok and not row.open_via:
                    from .scoring.paper_gate import PAPER_OPEN_VIA_LATE_OK

                    row.open_via = PAPER_OPEN_VIA_LATE_OK
                out["opened" if row.status == "open" else "queued"] += 1
                continue
            if not expired:
                out["waiting"] += 1
                continue
            shadow = _consider_paper_v1_shadow(
                session,
                chain,
                token,
                entry=entry,
                live=live,
                hi=hi,
                buy_mcap=buy_mcap,
                liq=liq,
                decision_id=fill.decision_id,
                now=now,
            )
            if shadow is None:
                _write_v1_shadow_reason(
                    session,
                    fill,
                    reason=PAPER_V1_SKIP_LIVE_COLD,
                    day=book_day,
                    buy_mcap=buy_mcap,
                    liq=liq,
                    now=now,
                )
            out["shadow"] += 1
        except Exception:
            log.exception("paperV1 reconsider failed for %s", fill.mint)
    if out["queued"] or out["opened"] or out["shadow"]:
        log.info(
            "paperV1 reconsider %s: scanned %s queued %s opened %s shadow %s waiting %s",
            chain,
            out["scanned"],
            out["queued"],
            out["opened"],
            out["shadow"],
            out["waiting"],
        )
    return out


def _copycat_shadow_entry_fields(
    session: Session,
    token: Token,
    chain: str,
    *,
    gate: Decision | None,
) -> tuple[datetime, float, float, float]:
    """opened_at, entry_p, entry_mcap, entry_liq for copycat shadow rows."""
    outcome = token.outcome
    research = token.research
    if gate is not None:
        opened = _aware(gate.at) or utcnow()
        entry_p = float(gate.entry_p or 0.0)
        buy_mcap = float(gate.entry_mcap or (outcome.t0_mcap if outcome else 0.0) or 0.0)
        liq = float(gate.liq or (outcome.last_liq if outcome else 0.0) or 0.0)
        return opened, entry_p, buy_mcap, liq
    opened = _aware(token.first_seen_at) or _aware(token.migrated_at) or utcnow()
    entry_dec = (
        session.query(Decision)
        .filter(Decision.token_id == token.id, Decision.kind == DECISION_ENTRY)
        .order_by(Decision.id.desc())
        .first()
    )
    hunt = (
        session.query(HuntCard)
        .filter(HuntCard.chain == chain, HuntCard.mint == token.mint)
        .first()
    )
    entry_p = float(
        (entry_dec.entry_p if entry_dec is not None else 0.0)
        or (research.p_good if research and research.p_good is not None else 0.0)
        or (hunt.conviction_p if hunt is not None else 0.0)
        or 0.0
    )
    buy_mcap = float(
        (outcome.t0_mcap if outcome else 0.0)
        or (entry_dec.entry_mcap if entry_dec is not None else 0.0)
        or (hunt.last_mcap if hunt is not None else 0.0)
        or 0.0
    )
    liq = float(
        (outcome.last_liq if outcome else 0.0)
        or (entry_dec.liq if entry_dec is not None else 0.0)
        or (hunt.last_liq if hunt is not None else 0.0)
        or 0.0
    )
    return opened, entry_p, buy_mcap, liq


def _annotate_copycat_learn_sides(
    session: Session,
    token: Token,
    *,
    gate: Decision | None = None,
    kind: str = "gate",
) -> dict[str, Any]:
    """Stamp gate_veto / would_have / fomo_rank. Never opens. Never lifts the skip."""
    from .scoring.copycat_learn import (
        GATE_VETO_COPYCAT,
        fomo_rank_from_snapshot,
        is_copycat_veto,
        stamp_copycat_learn_sides,
        would_have_copycat_shadow,
    )
    from .scoring.hunt import token_has_hunt_card
    from .scoring.paper_v1 import PAPER_V1_COPYCAT_VETO, v1_thesis_from_features

    out = {"stamped": 0, "would_have": False, "fomo_rank": None, "on_hunt": False}
    veto = (gate.veto if gate is not None else None) or PAPER_V1_COPYCAT_VETO
    if kind == "fomo" and not is_copycat_veto(veto):
        veto = GATE_VETO_COPYCAT
    if not is_copycat_veto(veto):
        return out
    chain = normalize_chain((gate.chain if gate is not None else None) or token.chain or "sol")
    fomo_rank = fomo_rank_from_snapshot(session, token.mint, chain=chain)
    on_hunt = token_has_hunt_card(session, token)
    research = token.research
    entry = (
        session.query(Decision)
        .filter(Decision.token_id == token.id, Decision.kind == DECISION_ENTRY)
        .order_by(Decision.id.desc())
        .first()
    )
    feat: dict[str, Any] = {}
    for src in (research, entry, gate):
        raw = getattr(src, "features_json", None) if src is not None else None
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if isinstance(data, dict):
            feat.update(data)
    holders = int(
        (gate.holders if gate is not None else 0)
        or (entry.holders if entry is not None else 0)
        or (research.holder_count if research is not None else 0)
        or 0
    )
    hunt = (
        session.query(HuntCard)
        .filter(HuntCard.chain == chain, HuntCard.mint == token.mint)
        .first()
    )
    if holders <= 0 and hunt is not None:
        holders = int(hunt.holders or 0)
    outcome = token.outcome
    liq = float(
        (gate.liq if gate is not None else 0.0)
        or (entry.liq if entry is not None else 0.0)
        or (outcome.last_liq if outcome else 0.0)
        or (hunt.last_liq if hunt is not None else 0.0)
        or 0.0
    )
    thesis = v1_thesis_from_features(feat)
    would = would_have_copycat_shadow(
        veto=veto,
        on_hunt=on_hunt,
        fomo_rank=fomo_rank,
        holders=holders,
        liq=liq,
        thesis=thesis,
    )
    targets: list[Any] = [d for d in (gate, entry) if d is not None]
    if not targets and research is not None:
        targets = [research]
    for row in targets:
        try:
            cur = json.loads(row.features_json or "{}")
        except Exception:
            cur = {}
        if not isinstance(cur, dict):
            cur = {}
        merged = stamp_copycat_learn_sides(
            cur, veto=veto, would_have=would, fomo_rank=fomo_rank
        )
        if merged is None:
            continue
        row.features_json = json.dumps(merged, default=str)
        if hasattr(row, "features_hash"):
            row.features_hash = features_hash(row.features_json)
        out["stamped"] += 1
    out["would_have"] = bool(would)
    out["fomo_rank"] = fomo_rank
    out["on_hunt"] = bool(on_hunt)
    if out["stamped"]:
        session.flush()
    return out


def _write_copycat_veto_v1_shadow(
    session: Session,
    token: Token,
    *,
    gate: Decision | None = None,
    now: datetime,
    kind: str = "gate",
) -> PaperFill | None:
    """Learn-only shadow for hard copycat (gate veto or FOMO/desk flags). Never opens paper_v1."""
    from .scoring.paper_v1 import (
        PAPER_V1_COPYCAT_FOMO_STAMP,
        PAPER_V1_COPYCAT_VETO,
        PAPER_V1_SHADOW_LINE,
        PAPER_V1_SKIPPED,
        research_risk_flags_copycat_spam,
        v1_day,
        v1_veto_skip_stamp,
    )

    chain = normalize_chain((gate.chain if gate is not None else None) or token.chain or "sol")
    if _v1_has_row(session, chain, token.mint):
        _annotate_copycat_learn_sides(session, token, gate=gate, kind=kind)
        return None
    if gate is None:
        research = token.research
        if not research_risk_flags_copycat_spam(
            research.risk_flags_json if research is not None else None
        ):
            return None
    opened, entry_p, buy_mcap, liq = _copycat_shadow_entry_fields(session, token, chain, gate=gate)
    day = v1_day(opened)
    outcome = token.outcome
    peak = float((outcome.max_mcap if outcome else 0.0) or buy_mcap)
    detail = PAPER_V1_COPYCAT_FOMO_STAMP if kind == "fomo" else PAPER_V1_COPYCAT_VETO
    row = PaperFill(
        chain=chain,
        mint=token.mint,
        token_id=token.id,
        decision_id=gate.id if gate is not None else None,
        line=PAPER_V1_SHADOW_LINE,
        opened_at=opened,
        entry_p=entry_p,
        entry_mcap=buy_mcap,
        entry_liq=liq,
        target=PAPER_TARGET,
        ride=PAPER_RIDE,
        max_mcap=peak,
        min_mcap=buy_mcap,
        last_mcap=float((outcome.last_mcap if outcome else 0.0) or buy_mcap),
        last_liq=liq,
        status=PAPER_V1_SKIPPED,
        exit_reason=v1_veto_skip_stamp(detail, day),
        image_rev=IMAGE_REV,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    _annotate_copycat_learn_sides(session, token, gate=gate, kind=kind)
    return row


def _copycat_fomo_shadow_mints(session: Session, chain: str) -> set[str]:
    """FOMO trending board + last audit — Sol desk names for copycat flag pass."""
    from .chains import normalize_mint
    from .ingest.fomo_poll import load_trending_snapshot
    from .research.fomo_coverage import load_fomo_trending_audit

    mints: set[str] = set()
    snap = load_trending_snapshot(session)
    for raw in (snap or {}).get("items") or []:
        c = normalize_chain(raw.get("chain") or "sol")
        if c != chain:
            continue
        m = normalize_mint(raw.get("mint") or "", c)
        if m:
            mints.add(m)
    audit = load_fomo_trending_audit(session)
    for item in (audit or {}).get("items") or []:
        if not isinstance(item, dict):
            continue
        if normalize_chain(item.get("chain") or "sol") != chain:
            continue
        if str(item.get("status") or "") != "caught":
            continue
        m = normalize_mint(str(item.get("mint") or ""), chain)
        if m:
            mints.add(m)
    return mints


def _reconsider_copycat_veto_shadow_fomo(
    session: Session,
    chain: str,
    *,
    since: datetime,
    now: datetime,
) -> dict[str, int]:
    """FOMO/desk copycat flags without a gate Decision (e.g. Super Inu class)."""
    from .scoring.paper_v1 import PAPER_V1_COPYCAT_VETO_LOOKBACK_DAYS, research_risk_flags_copycat_spam

    out = {"fomo_scanned": 0, "fomo_shadow": 0, "would_have": 0, "annotated": 0}
    fomo_mints = _copycat_fomo_shadow_mints(session, chain)
    flagged = (
        session.query(Token)
        .join(Research, Research.token_id == Token.id)
        .options(selectinload(Token.outcome), selectinload(Token.research))
        .filter(
            Token.chain == chain,
            Token.first_seen_at >= since,
            Research.risk_flags_json.ilike("%copycat spam%"),
        )
        .order_by(Token.id.desc())
        .limit(600)
        .all()
    )
    by_mint: dict[str, Token] = {t.mint: t for t in flagged if t.mint}
    if fomo_mints:
        extra = (
            session.query(Token)
            .options(selectinload(Token.outcome), selectinload(Token.research))
            .filter(Token.chain == chain, Token.mint.in_(list(fomo_mints)))
            .all()
        )
        for t in extra:
            if t.mint and t.mint not in by_mint:
                by_mint[t.mint] = t
    for token in by_mint.values():
        research = token.research
        if not research_risk_flags_copycat_spam(
            research.risk_flags_json if research is not None else None
        ):
            continue
        out["fomo_scanned"] += 1
        try:
            with session.begin_nested():
                if _write_copycat_veto_v1_shadow(session, token, gate=None, now=now, kind="fomo") is not None:
                    out["fomo_shadow"] += 1
                sides = _annotate_copycat_learn_sides(session, token, gate=None, kind="fomo")
                out["annotated"] += int(sides.get("stamped") or 0)
                if sides.get("would_have"):
                    out["would_have"] += 1
        except Exception:
            log.exception(
                "paperV1 copycat-veto shadow fomo write failed chain=%s mint=%s",
                chain,
                token.mint,
            )
    if out["fomo_scanned"] or out["fomo_shadow"]:
        log.info(
            "paperV1 copycat-veto shadow fomo %s: scanned %s wrote %s (lookback %sd)",
            chain,
            out["fomo_scanned"],
            out["fomo_shadow"],
            int(PAPER_V1_COPYCAT_VETO_LOOKBACK_DAYS),
        )
    return out


def reconsider_copycat_veto_shadow(
    session: Session, chain: str, *, now: datetime | None = None
) -> dict[str, int]:
    """Label hard copycat gate vetoes on paper_v1_shadow for Learn review.

    Separate from ``reconsider_paper_v1`` — never calls ``_consider_paper_v1``.
    Does not add copycat spam to ``PAPER_SHADOW_VETOES`` (hard veto stays hard).
    """
    from .scoring.paper_v1 import PAPER_V1_COPYCAT_VETO, PAPER_V1_COPYCAT_VETO_LOOKBACK_DAYS
    from .risk import paper_v1_blocked

    out = {
        "scanned": 0,
        "shadow": 0,
        "fomo_scanned": 0,
        "fomo_shadow": 0,
        "would_have": 0,
        "annotated": 0,
    }
    chain = normalize_chain(chain)
    if chain != "sol":
        return out
    now = now or utcnow()
    if paper_v1_blocked(session, chain):
        return out
    since = now - timedelta(days=max(1, int(PAPER_V1_COPYCAT_VETO_LOOKBACK_DAYS)))
    gates = (
        session.query(Decision)
        .filter(
            Decision.chain == chain,
            Decision.kind == DECISION_GATE,
            Decision.veto == PAPER_V1_COPYCAT_VETO,
            Decision.at >= since,
        )
        .order_by(Decision.id.desc())
        .limit(600)
        .all()
    )
    if gates:
        tokens = {
            t.id: t
            for t in session.query(Token)
            .options(selectinload(Token.outcome), selectinload(Token.research))
            .filter(Token.id.in_([g.token_id for g in gates if g.token_id]))
            .all()
        }
        for gate in gates:
            out["scanned"] += 1
            token = tokens.get(gate.token_id)
            if token is None:
                continue
            try:
                with session.begin_nested():
                    wrote = _write_copycat_veto_v1_shadow(session, token, gate=gate, now=now)
                    if wrote is not None:
                        out["shadow"] += 1
                    sides = _annotate_copycat_learn_sides(session, token, gate=gate, kind="gate")
                    out["annotated"] += int(sides.get("stamped") or 0)
                    if sides.get("would_have"):
                        out["would_have"] += 1
            except Exception:
                log.exception(
                    "paperV1 copycat-veto shadow write failed chain=%s mint=%s",
                    chain,
                    gate.mint,
                )
        if out["shadow"]:
            log.info(
                "paperV1 copycat-veto shadow %s: scanned %s wrote %s",
                chain,
                out["scanned"],
                out["shadow"],
            )
    fomo = _reconsider_copycat_veto_shadow_fomo(session, chain, since=since, now=now)
    out["fomo_scanned"] = fomo["fomo_scanned"]
    out["fomo_shadow"] = fomo["fomo_shadow"]
    out["shadow"] += out["fomo_shadow"]
    out["would_have"] += int(fomo.get("would_have") or 0)
    out["annotated"] += int(fomo.get("annotated") or 0)
    return out


def _v1_has_paper_v1_short_list_row(session: Session, chain: str, mint: str) -> bool:
    from .scoring.paper_v1 import PAPER_V1_LINE

    return (
        session.query(PaperFill.id)
        .filter(PaperFill.chain == chain, PaperFill.mint == mint, PaperFill.line == PAPER_V1_LINE)
        .first()
        is not None
    )


def _v1_shadow_row(session: Session, chain: str, mint: str) -> PaperFill | None:
    from .scoring.paper_v1 import PAPER_V1_SHADOW_LINE

    row = (
        session.query(PaperFill)
        .filter(PaperFill.chain == chain, PaperFill.mint == mint, PaperFill.line == PAPER_V1_SHADOW_LINE)
        .order_by(PaperFill.id.desc())
        .first()
    )
    if row is not None or normalize_chain(chain) != "robinhood":
        return row
    return (
        session.query(PaperFill)
        .filter(
            PaperFill.chain == chain,
            PaperFill.line == PAPER_V1_SHADOW_LINE,
            func.lower(PaperFill.mint) == (mint or "").lower(),
        )
        .order_by(PaperFill.id.desc())
        .first()
    )


def _si_pr_shadow_blocked_reason(shadow: PaperFill | None) -> str:
    """Why an existing shadow row cannot be stamped/upgraded to si-pr."""
    if shadow is None:
        return ""
    from .scoring.high_si_learn import is_active_si_pr_shadow_reason

    reason = str(shadow.exit_reason or "")
    if is_active_si_pr_shadow_reason(reason):
        return "already_si_pr"
    if "copycat" in reason:
        return "copycat_shadow"
    return ""


def _si_pr_shadow_entry_fields(
    session: Session, token: Token, chain: str
) -> tuple[datetime, float, float, float]:
    from .scoring.high_si_learn import frozen_entry_mcap, si_pr_label_opened_at

    opened = si_pr_label_opened_at(session, token, chain)
    _opened_hist, entry_p, buy_mcap, liq = _copycat_shadow_entry_fields(
        session, token, chain, gate=None
    )
    frozen_mcap = frozen_entry_mcap(session, token, chain)
    if frozen_mcap > 0:
        buy_mcap = frozen_mcap
    return opened, entry_p, buy_mcap, liq


def _write_high_si_fomo_v1_shadow(
    session: Session,
    token: Token,
    *,
    now: datetime,
    multiple: float,
) -> PaperFill | None:
    """Learn-only shadow for high-SI FOMO printers (not copycat). Never opens paper_v1."""
    from .scoring.high_si_learn import v1_si_printer_stamp
    from .scoring.paper_v1 import PAPER_V1_LINE, PAPER_V1_SHADOW_LINE, PAPER_V1_SKIPPED, v1_day

    chain = normalize_chain(token.chain or "sol")
    if _v1_has_paper_v1_short_list_row(session, chain, token.mint):
        return None
    opened, entry_p, buy_mcap, liq = _si_pr_shadow_entry_fields(session, token, chain)
    # Book day on exit_reason; review also matches ``v1 si-pr|d:<day>`` (honest opened_at).
    day = v1_day(now)
    outcome = token.outcome
    peak = float((outcome.max_mcap if outcome else 0.0) or buy_mcap)
    if buy_mcap > 0 and float(multiple or 0.0) > 0:
        peak = max(peak, buy_mcap * float(multiple))
    last_m = float((outcome.last_mcap if outcome else 0.0) or buy_mcap)
    existing = _v1_shadow_row(session, chain, token.mint)
    blocked = _si_pr_shadow_blocked_reason(existing)
    if blocked == "copycat_shadow":
        return None
    stamp = v1_si_printer_stamp(day)
    if blocked == "already_si_pr" and existing is not None:
        existing.exit_reason = stamp
        existing.updated_at = now
        existing.image_rev = IMAGE_REV
        session.flush()
        return existing
    if existing is not None:
        existing.exit_reason = stamp
        existing.max_mcap = max(float(existing.max_mcap or 0.0), peak)
        existing.last_mcap = last_m or float(existing.last_mcap or buy_mcap)
        existing.updated_at = now
        existing.image_rev = IMAGE_REV
        session.flush()
        log.info(
            "paperV1 high-SI fomo shadow upgrade %s %s mult=%.1f",
            chain,
            token.mint[:8],
            float(multiple or 0.0),
        )
        return existing
    row = PaperFill(
        chain=chain,
        mint=token.mint,
        token_id=token.id,
        decision_id=None,
        line=PAPER_V1_SHADOW_LINE,
        opened_at=opened,
        entry_p=entry_p,
        entry_mcap=buy_mcap,
        entry_liq=liq,
        target=PAPER_TARGET,
        ride=PAPER_RIDE,
        max_mcap=peak,
        min_mcap=buy_mcap,
        last_mcap=last_m,
        last_liq=liq,
        status=PAPER_V1_SKIPPED,
        exit_reason=stamp,
        image_rev=IMAGE_REV,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    log.info(
        "paperV1 high-SI fomo shadow %s %s mult=%.1f",
        chain,
        token.mint[:8],
        float(multiple or 0.0),
    )
    return row


def _attempt_si_pr_fomo_token(
    session: Session,
    chain: str,
    token: Token,
    *,
    fomo_row: dict[str, Any] | None,
    fomo_mints: set[str],
    fomo_metrics: dict[str, dict[str, Any]],
    now: datetime,
    out: dict[str, int],
) -> dict[str, Any] | None:
    """Try one token; return probe detail when ``out`` is the probe dict."""
    from .scoring.high_si_learn import (
        fomo_desk_printer_multiple,
        fomo_desk_si_pr_eligible,
        fomo_metric_row,
        high_si_copycat_blocked,
        printer_label_multiple,
        token_high_si_printer,
    )

    probe: dict[str, Any] = {
        "mint": token.mint,
        "symbol": token.symbol,
        "token_id": token.id,
    }
    row = fomo_row or fomo_metric_row(fomo_metrics, chain, token.mint or "")
    probe["fomo_multiple"] = fomo_desk_printer_multiple(row)
    probe["fomo_desk"] = fomo_desk_si_pr_eligible(row)
    prior = _v1_shadow_row(session, chain, token.mint)
    prior_reason = str(prior.exit_reason or "") if prior else ""
    if not fomo_desk_si_pr_eligible(row):
        if prior_reason.lower().startswith("v1 si-pr"):
            probe["stage"] = "stale_si_pr_no_resurface"
        else:
            out["blocked_not_fomo_desk"] = out.get("blocked_not_fomo_desk", 0) + 1
            probe["stage"] = "not_fomo_desk_band"
        return probe
    out["scanned"] += 1
    if _v1_has_paper_v1_short_list_row(session, chain, token.mint):
        out["blocked_short_list"] += 1
        probe["stage"] = "blocked_short_list"
        return probe
    research = token.research
    if high_si_copycat_blocked(token, research):
        out["blocked_copycat"] += 1
        probe["stage"] = "blocked_copycat"
        return probe
    label_mult = printer_label_multiple(
        session, token, chain, token.outcome, fomo_row=row
    )
    probe["label_multiple"] = round(label_mult, 3)
    if not token_high_si_printer(token, token.outcome, label_multiple=label_mult):
        out["blocked_band"] += 1
        probe["stage"] = "blocked_band"
        return probe
    probe["prior_shadow"] = prior_reason[:48] if prior_reason else ""
    try:
        with session.begin_nested():
            row_fill = _write_high_si_fomo_v1_shadow(
                session,
                token,
                now=now,
                multiple=label_mult,
            )
    except Exception:
        log.exception(
            "paperV1 high-SI fomo shadow write failed chain=%s mint=%s",
            chain,
            token.mint,
        )
        probe["stage"] = "write_error"
        return probe
    if row_fill is None:
        probe["stage"] = "write_none"
        return probe
    out["shadow"] += 1
    if prior is not None and not prior_reason.lower().startswith("v1 si-pr"):
        out["upgraded"] += 1
    probe["stage"] = "stamped"
    probe["exit_reason"] = row_fill.exit_reason
    probe["opened_at"] = _aware(row_fill.opened_at).isoformat() if row_fill.opened_at else None
    return probe


def si_pr_fomo_probe(
    session: Session, chain: str, mint: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """Per-mint si-pr path debugger (PAID / MEME spot checks)."""
    from .chains import normalize_mint
    from .research.fomo_coverage import fomo_desk_metrics_for_chain
    from .scoring.high_si_learn import (
        ensure_fomo_desk_token,
        fomo_metric_row,
        iter_canonical_fomo_metrics,
        resolve_desk_token,
    )
    from .risk import paper_v1_blocked

    chain = normalize_chain(chain)
    now = now or utcnow()
    mint_n = normalize_mint(mint, chain)
    out: dict[str, Any] = {
        "chain": chain,
        "mint": mint_n,
        "blocked_global": paper_v1_blocked(session, chain),
    }
    if out["blocked_global"]:
        out["stage"] = "paper_v1_blocked"
        return out
    fomo_metrics = fomo_desk_metrics_for_chain(session, chain)
    fomo_mints = {m for m, _ in iter_canonical_fomo_metrics(fomo_metrics)}
    row = fomo_metric_row(fomo_metrics, chain, mint_n)
    from .scoring.high_si_learn import fomo_desk_si_pr_eligible

    out["on_fomo_board"] = row is not None
    out["fomo_desk_eligible"] = fomo_desk_si_pr_eligible(row)
    out["fomo_multiple"] = float((row or {}).get("multiple") or 0.0)
    token = resolve_desk_token(session, chain, mint_n)
    if token is None and row:
        token = ensure_fomo_desk_token(session, chain, row)
    if token is None:
        out["stage"] = "token_missing"
        return out
    token = (
        session.query(Token)
        .options(selectinload(Token.outcome), selectinload(Token.research))
        .filter(Token.id == token.id)
        .one()
    )
    counters = {
        "scanned": 0,
        "shadow": 0,
        "upgraded": 0,
        "blocked_copycat": 0,
        "blocked_band": 0,
        "blocked_short_list": 0,
    }
    detail = _attempt_si_pr_fomo_token(
        session,
        chain,
        token,
        fomo_row=row,
        fomo_mints=fomo_mints,
        fomo_metrics=fomo_metrics,
        now=now,
        out=counters,
    )
    out.update(detail or {})
    out["counters"] = counters
    return out


def _demote_stale_si_pr_shadows(
    session: Session,
    chain: str,
    fomo_metrics: dict[str, dict[str, Any]],
    *,
    now: datetime,
) -> int:
    """Hunt-flood si-pr off today's review — keep row, stamp si-pr-stale + old opened_at."""
    from .scoring.high_si_learn import (
        fomo_desk_si_pr_eligible,
        fomo_metric_row,
        is_active_si_pr_shadow_reason,
        v1_si_pr_stale_stamp,
    )
    from .scoring.paper_v1 import PAPER_V1_SHADOW_LINE, book_day_from_reason, v1_day

    chain = normalize_chain(chain)
    demoted = 0
    book_today = v1_day(now)
    rows = (
        session.query(PaperFill)
        .filter(PaperFill.chain == chain, PaperFill.line == PAPER_V1_SHADOW_LINE)
        .order_by(PaperFill.id.desc())
        .limit(500)
        .all()
    )
    for fill in rows:
        if not is_active_si_pr_shadow_reason(fill.exit_reason or ""):
            continue
        row = fomo_metric_row(fomo_metrics, chain, fill.mint or "")
        if fomo_desk_si_pr_eligible(row):
            continue
        prior_book = book_day_from_reason(fill.exit_reason or "") or book_today
        fill.exit_reason = v1_si_pr_stale_stamp(prior_book)
        fill.updated_at = now
        fill.image_rev = IMAGE_REV
        demoted += 1
    if demoted:
        session.flush()
        log.info(
            "paperV1 high-SI fomo stale demote %s: n=%s",
            chain,
            demoted,
        )
    return demoted


def reconsider_high_si_fomo_shadow(
    session: Session, chain: str, *, now: datetime | None = None
) -> dict[str, int]:
    """Label high-multiple FOMO/leftover printers on paper_v1_shadow (Learn)."""
    from .research.fomo_coverage import fomo_desk_metrics_for_chain
    from .scoring.high_si_learn import (
        ensure_fomo_desk_token,
        fomo_desk_si_pr_eligible,
        iter_canonical_fomo_metrics,
        repair_fomo_board_missing_outcomes,
        resolve_desk_token,
    )
    from .risk import paper_v1_blocked

    out = {
        "scanned": 0,
        "shadow": 0,
        "upgraded": 0,
        "blocked_copycat": 0,
        "blocked_band": 0,
        "blocked_short_list": 0,
        "blocked_no_token": 0,
        "blocked_not_fomo_desk": 0,
        "demoted_stale": 0,
    }
    chain = normalize_chain(chain)
    if chain not in {"sol", "robinhood"}:
        return out
    now = now or utcnow()
    if paper_v1_blocked(session, chain):
        return out
    fomo_metrics = fomo_desk_metrics_for_chain(session, chain)
    out["demoted_stale"] = _demote_stale_si_pr_shadows(
        session, chain, fomo_metrics, now=now
    )
    repair_fomo_board_missing_outcomes(session, chain)
    fomo_mints = {m for m, _ in iter_canonical_fomo_metrics(fomo_metrics)}
    seen_token_ids: set[int] = set()
    for mint, fomo_row in iter_canonical_fomo_metrics(fomo_metrics):
        if not fomo_desk_si_pr_eligible(fomo_row):
            continue
        token = resolve_desk_token(session, chain, mint) or ensure_fomo_desk_token(
            session, chain, fomo_row
        )
        if token is None:
            out["blocked_no_token"] += 1
            continue
        if token.id in seen_token_ids:
            continue
        seen_token_ids.add(token.id)
        token = (
            session.query(Token)
            .options(selectinload(Token.outcome), selectinload(Token.research))
            .filter(Token.id == token.id)
            .one()
        )
        _attempt_si_pr_fomo_token(
            session,
            chain,
            token,
            fomo_row=fomo_row,
            fomo_mints=fomo_mints,
            fomo_metrics=fomo_metrics,
            now=now,
            out=out,
        )
    if out["shadow"]:
        log.info(
            "paperV1 high-SI fomo shadow %s: scanned=%s wrote=%s upgraded=%s",
            chain,
            out["scanned"],
            out["shadow"],
            out["upgraded"],
        )
    return out


def _write_meme_quality_v1_shadow(
    session: Session,
    token: Token,
    *,
    now: datetime,
) -> PaperFill | None:
    """Top-decile meme-only Learn shadow. Never opens paper_v1."""
    from .scoring.meme_quality import (
        is_meme_dominant_thesis,
        meme_quality_for_token,
        v1_meme_quality_stamp,
    )
    from .scoring.paper_v1 import PAPER_V1_SHADOW_LINE, PAPER_V1_SKIPPED, v1_day

    chain = normalize_chain(token.chain or "sol")
    if _v1_has_row(session, chain, token.mint):
        return None
    mq = meme_quality_for_token(session, token)
    if not mq.get("meme_only") or not mq.get("eligible"):
        return None
    opened, entry_p, buy_mcap, liq = _copycat_shadow_entry_fields(session, token, chain, gate=None)
    day = v1_day(opened)
    outcome = token.outcome
    peak = float((outcome.max_mcap if outcome else 0.0) or buy_mcap)
    row = PaperFill(
        chain=chain,
        mint=token.mint,
        token_id=token.id,
        decision_id=None,
        line=PAPER_V1_SHADOW_LINE,
        opened_at=opened,
        entry_p=entry_p,
        entry_mcap=buy_mcap,
        entry_liq=liq,
        target=PAPER_TARGET,
        ride=PAPER_RIDE,
        max_mcap=peak,
        min_mcap=buy_mcap,
        last_mcap=float((outcome.last_mcap if outcome else 0.0) or buy_mcap),
        last_liq=liq,
        status=PAPER_V1_SKIPPED,
        exit_reason=v1_meme_quality_stamp(day),
        image_rev=IMAGE_REV,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    try:
        from .scoring.meme_quality import PAPER_V1_SKIP_MEME_QUALITY

        _stamp_entry_paper_miss(
            session, token.id, reason=PAPER_V1_SKIP_MEME_QUALITY, now=now
        )
    except Exception:
        log.exception("paper-miss stamp failed for %s", token.mint)
    return row


def reconsider_meme_quality_shadow(
    session: Session, chain: str, *, now: datetime | None = None
) -> dict[str, int]:
    """Label top-decile meme-only names on paper_v1_shadow (Learn). Paper-safe."""
    from .scoring.meme_quality import (
        MEME_QUALITY_FALLBACK_FLOOR,
        MEME_QUALITY_LOOKBACK_DAYS,
        MEME_QUALITY_MIN_CANDIDATES,
        MEME_QUALITY_TOP_DECILE,
        MEME_QUALITY_WRITE_FLOOR,
        meme_quality_for_token,
    )
    from .risk import paper_v1_blocked

    out = {"scanned": 0, "shadow": 0}
    chain = normalize_chain(chain)
    if chain not in {"sol", "robinhood"}:
        return out
    now = now or utcnow()
    if paper_v1_blocked(session, chain):
        return out
    since = now - timedelta(days=max(1, int(MEME_QUALITY_LOOKBACK_DAYS)))
    hunt_rows = (
        session.query(HuntCard, Token)
        .join(Token, Token.id == HuntCard.token_id)
        .options(selectinload(Token.outcome), selectinload(Token.research))
        .filter(HuntCard.chain == chain, HuntCard.updated_at >= since)
        .order_by(HuntCard.updated_at.desc())
        .limit(500 if chain == "sol" else 120)
        .all()
    )
    if not hunt_rows:
        return out
    scored: list[tuple[float, Token]] = []
    for _card, token in hunt_rows:
        out["scanned"] += 1
        mq = meme_quality_for_token(session, token)
        if not mq.get("meme_only") or not mq.get("eligible"):
            continue
        score = float(mq.get("score") or 0.0)
        if score < MEME_QUALITY_WRITE_FLOOR:
            continue
        scored.append((score, token))
    if not scored:
        return out
    scores = sorted(s for s, _ in scored)
    if len(scores) >= MEME_QUALITY_MIN_CANDIDATES:
        idx = max(0, int(len(scores) * MEME_QUALITY_TOP_DECILE) - 1)
        cutoff = scores[idx]
    else:
        cutoff = MEME_QUALITY_FALLBACK_FLOOR
    for score, token in scored:
        if score < cutoff:
            continue
        try:
            with session.begin_nested():
                if _write_meme_quality_v1_shadow(session, token, now=now) is not None:
                    out["shadow"] += 1
        except Exception:
            log.exception(
                "paperV1 meme-quality shadow write failed chain=%s mint=%s",
                chain,
                token.mint,
            )
    if out["shadow"]:
        log.info(
            "paperV1 meme-quality shadow %s: scanned %s wrote %s cutoff %.3f",
            chain,
            out["scanned"],
            out["shadow"],
            cutoff,
        )
    return out


def meme_quality_review_samples(
    session: Session, chain: str, *, limit: int = 20
) -> list[dict[str, Any]]:
    """Top meme-only scores on recent Hunt board (Learn visibility)."""
    from .scoring.meme_quality import meme_quality_for_token

    chain = normalize_chain(chain)
    since = utcnow() - timedelta(days=3)
    hunt_rows = (
        session.query(HuntCard, Token)
        .join(Token, Token.id == HuntCard.token_id)
        .options(selectinload(Token.outcome), selectinload(Token.research))
        .filter(HuntCard.chain == chain, HuntCard.updated_at >= since)
        .order_by(HuntCard.updated_at.desc())
        .limit(200)
        .all()
    )
    rows: list[dict[str, Any]] = []
    for _card, token in hunt_rows:
        mq = meme_quality_for_token(session, token)
        if not mq.get("meme_only"):
            continue
        rows.append(
            {
                "mint": token.mint,
                "symbol": token.symbol or "",
                "score": mq.get("score"),
                "eligible": mq.get("eligible"),
                "hard_veto": mq.get("hard_veto") or "",
                "late": mq.get("late"),
                "parts": mq.get("parts") or {},
            }
        )
    rows.sort(key=lambda r: (-float(r.get("score") or 0.0), str(r.get("symbol") or "")))
    return rows[:limit]


# Midday promote runs every paper sync (~60 s). Two workers overlapping
# during a deploy each saw 3 slots of room inside their own uncommitted
# transaction and both opened — the 5 > 3 breaches. A short lease serialises
# them; a single worker's own lease is always stale by its next cycle.
PAPER_V1_PROMOTE_LEASE_S = 30.0


def _claim_v1_promote_lease(session: Session, day: str, *, now: datetime) -> bool:
    """Per-day promote lease. False while another promoter holds it.

    ``SELECT … FOR UPDATE`` on the lease row makes the second worker wait
    for the first transaction to commit, so it then reads a fresh stamp
    (and the first worker's opens) instead of racing on stale room.
    """
    key = f"paper_v1:promote_lease:{day}"
    row = session.query(ScanState).filter(ScanState.key == key).with_for_update().first()
    if row is None:
        try:
            with session.begin_nested():
                session.add(ScanState(key=key, value=now.isoformat(), updated_at=now))
                session.flush()
            return True
        except IntegrityError:
            return False
    held = _aware(row.updated_at)
    if held is not None and (now - held).total_seconds() < PAPER_V1_PROMOTE_LEASE_S:
        return False
    row.value = now.isoformat()
    row.updated_at = now
    session.flush()
    return True


def _claim_v1_lock(session: Session, day: str, *, now: datetime) -> bool:
    """Take the day's lock before opening anyone. False if another worker won."""
    if _v1_locked(session, day):
        return False
    try:
        with session.begin_nested():
            session.add(ScanState(key=f"paper_v1:locked:{day}", value=day, updated_at=now))
            session.flush()
        return True
    except IntegrityError:
        return False


def _refresh_v1_queued_peak(session: Session, fill: PaperFill, *, now: datetime) -> float:
    """Stamp sellable Hunt/Outcome peak onto a queued paperV1 row. Returns peak×."""
    from .scoring.paper_v1 import v1_peak_multiple

    token = session.get(Token, fill.token_id) if fill.token_id else None
    hunt = (
        session.query(HuntCard)
        .filter(HuntCard.chain == fill.chain, HuntCard.mint == fill.mint)
        .first()
    )
    outcome = token.outcome if token is not None else None
    last = float((hunt.last_mcap if hunt else 0.0) or (outcome.last_mcap if outcome else 0.0) or 0.0)
    liq = float((hunt.last_liq if hunt else 0.0) or (outcome.last_liq if outcome else 0.0) or 0.0)
    if last > 0:
        fill.last_mcap = last
        fill.last_liq = liq
        if liq >= PAPER_SELLABLE_LIQ:
            fill.max_mcap = max(float(fill.max_mcap or 0.0), last)
        fill.min_mcap = min(float(fill.min_mcap or last), last) if fill.min_mcap else last
        fill.updated_at = now
    return v1_peak_multiple(fill.entry_mcap, fill.max_mcap)


def promote_paper_v1_queue(session: Session, *, now: datetime | None = None) -> dict[str, int]:
    """Open queued paperV1 rows that clear immediate or already printed 2×.

    Midday path — do not wait for the 23:00 lock when slots remain.
    Immediate: Live≥0.70 or strong thesis. Peak path: sellable max ≥ 2× entry
    (catches queued winners like KURATE before lock). Independent 3/day
    per chain. Paper only.
    """
    from .scoring.paper_v1 import (
        PAPER_V1_CAP_PER_CHAIN,
        PAPER_V1_LINE,
        PAPER_V1_QUEUED,
        choose_v1,
        lock_due,
        v1_day,
        v1_day_reason,
        v1_immediate,
        v1_parse_queue,
        v1_peak_ok,
    )
    from .scoring.thesis_enrich import v1_hard_tag_ready
    from .scoring.thesis_weights import load_rank_policy

    from .risk import paper_v1_blocked

    now = now or utcnow()
    today = v1_day(now)
    if _v1_locked(session, today) or lock_due(today, now):
        return {"opened": 0, "peak": 0}
    if paper_v1_blocked(session):
        return {"opened": 0, "peak": 0}
    if not _claim_v1_promote_lease(session, today, now=now):
        return {"opened": 0, "peak": 0, "leased": 1}
    queued = (
        session.query(PaperFill)
        .filter(PaperFill.line == PAPER_V1_LINE, PaperFill.status == PAPER_V1_QUEUED)
        .order_by(PaperFill.id.asc())
        .all()
    )
    # (fill, thesis_score, signal_score, peak_x, via)
    candidates: list[tuple[PaperFill, float, float, float, str]] = []
    for fill in queued:
        if paper_v1_blocked(session, fill.chain):
            continue
        live, day = v1_parse_queue(fill.exit_reason or "")
        book = day or _v1_book_day(fill)
        if book != today:
            continue
        peak_x = _refresh_v1_queued_peak(session, fill, now=now)
        signal = _v1_signal(session, fill.token_id, fill.decision_id)
        s_score = float(signal.get("score") or 0.0)
        if live is not None:
            try:
                freeze_live_at_entry(session, fill.token_id, live, src="paper_queue", now=now)
            except Exception:
                log.exception("live_at_entry freeze failed for queued %s", fill.mint)
        try:
            if not v1_hard_tag_ready(session, fill.token_id, fill.decision_id):
                continue
        except Exception:
            log.exception("paperV1 promote thesis check failed for %s", fill.mint)
            continue
        thesis = _v1_thesis(session, fill.token_id, fill.decision_id)
        t_score = float(thesis.get("score") or 0.0)
        if v1_immediate(live, thesis):
            candidates.append((fill, t_score, s_score, peak_x, "immediate"))
        elif v1_peak_ok(fill.entry_mcap, fill.max_mcap):
            candidates.append((fill, t_score, s_score, peak_x, "peak"))
    candidates.sort(key=lambda item: (0 if item[4] == "immediate" else 1, -item[3], -item[2], item[0].id))
    policy = load_rank_policy(session)
    opened = 0
    peak_n = 0
    for chain_name in ("sol", "robinhood"):
        room = PAPER_V1_CAP_PER_CHAIN - _v1_taken_chain(session, today, chain_name)
        if room <= 0:
            continue
        payload = []
        for i, (fill, t_score, s_score, peak_x, via) in enumerate(candidates):
            if normalize_chain(fill.chain or "") != chain_name:
                continue
            payload.append(
                {
                    "id": i,
                    "chain": fill.chain,
                    "entry_p": float(fill.entry_p or 0.0),
                    "live_p": float(v1_parse_queue(fill.exit_reason or "")[0] or 0.0),
                    "opened_at": _aware(fill.opened_at) or now,
                    "thesis_score": t_score,
                    "signal_score": s_score,
                    "via": via,
                    "fill": fill,
                    "peak_x": peak_x,
                }
            )
        chosen_ids = set(
            choose_v1(
                payload,
                already=PAPER_V1_CAP_PER_CHAIN - room,
                cap=PAPER_V1_CAP_PER_CHAIN,
                policy=policy,
                min_per_chain=0,
            )
        )
        for row in payload:
            if row["id"] not in chosen_ids:
                continue
            fill = row["fill"]
            via = str(row["via"] or "")
            fill.status = "open"
            fill.exit_reason = v1_day_reason(today)
            fill.open_via = via
            if via == "peak":
                from .scoring.paper_v1 import apply_v1_peak_promote_entry

                apply_v1_peak_promote_entry(fill)
                peak_n += 1
            fill.updated_at = now
            opened += 1
    if opened:
        session.flush()
        log.info("paperV1 midday promote opened %s (peak %s)", opened, peak_n)
    return {"opened": opened, "peak": peak_n}


def lock_paper_v1(session: Session, *, now: datetime | None = None) -> dict[str, int]:
    """Lock every due UTC day once, across both chains.

    Call this after both chains have queued. Chosen rows become open.
    The rest of that day are skipped. A day with an empty queue still locks
    at 23:00 so a later name waits for the next day.

    The ScanState lock is claimed first so two workers cannot each open five
    names for the same UTC day.
    """
    from .scoring.paper_v1 import (
        PAPER_V1_CAP_PER_CHAIN,
        PAPER_V1_LINE,
        PAPER_V1_QUEUED,
        PAPER_V1_SKIPPED,
        choose_v1,
        lock_due,
        v1_day,
        v1_day_reason,
        v1_parse_queue,
        v1_skip_stamp,
        PAPER_V1_SKIP_CAP,
        PAPER_V1_SKIP_NO_THESIS,
    )
    from .scoring.thesis_enrich import v1_hard_tag_ready
    from .scoring.thesis_weights import load_rank_policy

    from .risk import paper_v1_blocked

    now = now or utcnow()
    # Kill switch: do not open or skip — leave queue until flush or resume.
    if paper_v1_blocked(session):
        return {"opened": 0, "skipped": 0}
    queued = (
        session.query(PaperFill)
        .filter(PaperFill.line == PAPER_V1_LINE, PaperFill.status == PAPER_V1_QUEUED)
        .all()
    )
    by_day: dict[str, list[PaperFill]] = {}
    opened = 0
    skipped = 0
    for fill in queued:
        _live, day = v1_parse_queue(fill.exit_reason or "")
        if not day:
            day = v1_day(_aware(fill.opened_at) or now)
        if not lock_due(day, now):
            continue
        if paper_v1_blocked(session, fill.chain):
            continue
        if _v1_locked(session, day):
            fill.status = PAPER_V1_SKIPPED
            fill.exit_reason = v1_skip_stamp(PAPER_V1_SKIP_CAP, day)
            fill.updated_at = now
            skipped += 1
            continue
        by_day.setdefault(day, []).append(fill)
    today = v1_day(now)
    if lock_due(today, now) and not _v1_locked(session, today):
        by_day.setdefault(today, [])
    policy = load_rank_policy(session)
    for day in sorted(by_day):
        items = by_day[day]
        if not _claim_v1_lock(session, day, now=now):
            for fill in items:
                fill.status = PAPER_V1_SKIPPED
                fill.exit_reason = v1_skip_stamp(PAPER_V1_SKIP_CAP, day)
                fill.updated_at = now
                skipped += 1
            continue
        chosen: set[int] = set()
        # Independent 3/day lanes — Sol and RH each pick from their own queue.
        for chain_name in ("sol", "robinhood"):
            chain_items = [f for f in items if normalize_chain(f.chain or "") == chain_name]
            if not chain_items:
                continue
            payload = []
            for fill in chain_items:
                live, _stamped = v1_parse_queue(fill.exit_reason or "")
                thesis = _v1_thesis(session, fill.token_id, fill.decision_id)
                signal = _v1_signal(session, fill.token_id, fill.decision_id)
                if live is not None:
                    try:
                        freeze_live_at_entry(session, fill.token_id, live, src="paper_lock", now=now)
                    except Exception:
                        log.exception("live_at_entry freeze failed at lock for %s", fill.mint)
                payload.append(
                    {
                        "id": fill.id,
                        "chain": fill.chain,
                        "entry_p": float(fill.entry_p or 0.0),
                        "live_p": 0.0 if live is None else live,
                        "opened_at": _aware(fill.opened_at) or now,
                        "thesis_score": float(thesis.get("score") or 0.0),
                        "signal_score": float(signal.get("score") or 0.0),
                        "thesis": thesis,
                        "signal": signal,
                    }
                )
            already = _v1_taken_chain(session, day, chain_name)
            chosen |= set(
                choose_v1(
                    payload,
                    already=already,
                    cap=PAPER_V1_CAP_PER_CHAIN,
                    policy=policy,
                    min_per_chain=0,
                )
            )
        for fill in items:
            fill.updated_at = now
            if fill.id in chosen:
                try:
                    tok = session.get(Token, fill.token_id) if fill.token_id else None
                    if tok is not None:
                        from .scoring.thesis_enrich import prime_paper_v1_thesis_for_qualify

                        prime_paper_v1_thesis_for_qualify(session, tok)
                    ready = v1_hard_tag_ready(session, fill.token_id, fill.decision_id)
                except Exception:
                    log.exception("paperV1 lock thesis check failed for %s", fill.mint)
                    ready = False
                if ready:
                    fill.status = "open"
                    fill.exit_reason = v1_day_reason(day)
                    opened += 1
                else:
                    fill.status = PAPER_V1_SKIPPED
                    fill.exit_reason = v1_skip_stamp(PAPER_V1_SKIP_NO_THESIS, day)
                    try:
                        _stamp_entry_paper_miss(
                            session, fill.token_id, reason=PAPER_V1_SKIP_NO_THESIS, now=now
                        )
                    except Exception:
                        log.exception("paper-miss stamp failed for %s", fill.mint)
                    skipped += 1
            else:
                fill.status = PAPER_V1_SKIPPED
                fill.exit_reason = v1_skip_stamp(PAPER_V1_SKIP_CAP, day)
                skipped += 1
    if opened or skipped or by_day:
        session.flush()
        if opened or skipped:
            log.info("paperV1 locked %s open %s skipped", opened, skipped)
    return {"opened": opened, "skipped": skipped}


def paper_v1_summary(session: Session, chain: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Side-list counts. Independent 3/day lanes per chain (6 total)."""
    from .scoring.paper_v1 import (
        PAPER_V1_CAP,
        PAPER_V1_CAP_PER_CHAIN,
        PAPER_V1_LINE,
        PAPER_V1_LOCK_HOUR,
        PAPER_V1_QUEUED,
        PAPER_V1_SKIPPED,
        v1_day,
        v1_parse_queue,
    )

    now = now or utcnow()
    chain = normalize_chain(chain)
    today = v1_day(now)
    rows = (
        session.query(PaperFill, Token.symbol)
        .join(Token, Token.id == PaperFill.token_id)
        .filter(PaperFill.line == PAPER_V1_LINE)
        .order_by(PaperFill.id.desc())
        .limit(PAPER_V1_SUMMARY_CAP)
        .all()
    )
    mine = [(fill, symbol or "") for fill, symbol in rows if fill.chain == chain]

    def _n(status: str) -> int:
        return sum(1 for fill, _symbol in mine if fill.status == status)

    closed = [fill for fill, _symbol in mine if fill.status == "closed"]
    rets = [float(fill.return_pct or 0.0) for fill in closed]
    wins = 0
    for fill in closed:
        entry = float(fill.entry_mcap or 0.0)
        if entry > 0 and float(fill.max_mcap or 0.0) >= float(fill.target or PAPER_TARGET) * entry:
            wins += 1
    today_names = []
    queued_today = 0
    for fill, symbol in rows:
        live, stamped = v1_parse_queue(fill.exit_reason or "")
        book = stamped or _v1_book_day(fill)
        if book != today:
            continue
        if fill.status == PAPER_V1_QUEUED:
            queued_today += 1
        if fill.status in ("open", "closed", PAPER_V1_QUEUED, PAPER_V1_SKIPPED):
            thesis = _v1_thesis(session, fill.token_id, fill.decision_id)
            today_names.append(
                {
                    "symbol": symbol or "",
                    "mint": fill.mint,
                    "chain": fill.chain,
                    "status": fill.status,
                    "entry_p": round(float(fill.entry_p or 0.0), 4),
                    "live_p": None if live is None and fill.status != PAPER_V1_QUEUED else live,
                    "thesis": thesis,
                    "thesis_score": thesis.get("score"),
                    "tags": thesis.get("tags") or [],
                }
            )
    today_names.sort(key=lambda r: (-float(r.get("thesis_score") or 0.0), str(r.get("symbol") or "")))
    hit2 = 0
    hit5 = 0
    for fill in closed:
        entry = float(fill.entry_mcap or 0.0)
        if entry <= 0:
            continue
        peak = float(fill.max_mcap or 0.0)
        if peak >= 2.0 * entry:
            hit2 += 1
        if peak >= 5.0 * entry:
            hit5 += 1
    n_closed = len(closed)
    by_chain = _v1_taken_by_chain(session, today)
    chain_taken = _v1_taken_chain(session, today, chain)
    return {
        "line": PAPER_V1_LINE,
        "cap": PAPER_V1_CAP,
        "cap_per_chain": PAPER_V1_CAP_PER_CHAIN,
        "lock_hour_utc": PAPER_V1_LOCK_HOUR,
        "paper_only": True,
        "rule": (
            f"Paper short list ({PAPER_V1_CAP_PER_CHAIN}/chain/day, {PAPER_V1_CAP} total). "
            "Score path: Sol buy line + Live>=0.50; RH Entry>=0.40. "
            "Thesis soft path needs hard tags (GitHub/dev/CTO) — meme alone does not pass. "
            "Rank by cohort signal, then Live/Entry, then thesis. Locks 23:00 UTC. "
            "Live>=0.70, strong thesis, or queued sellable peak>=2× takes a slot before lock. "
            "Live frozen on Decision at first paper sight. No ticket, no auto-buy."
        ),
        "queued": _n(PAPER_V1_QUEUED),
        "open": _n("open"),
        "closed": _n("closed"),
        "skipped": _n(PAPER_V1_SKIPPED),
        "wins": wins,
        "avg_return_pct": round(sum(rets) / len(rets), 1) if rets else None,
        "hit2x": None if not n_closed else round(hit2 / n_closed, 3),
        "hit5x": None if not n_closed else round(hit5 / n_closed, 3),
        "hit2x_n": hit2,
        "hit5x_n": hit5,
        "today": {
            "day": today,
            "taken": _v1_taken(session, today),
            "taken_chain": chain_taken,
            "room_chain": max(0, PAPER_V1_CAP_PER_CHAIN - chain_taken),
            "queued": queued_today,
            "locked": _v1_locked(session, today),
            "by_chain": by_chain,
            "names": today_names[:12],
        },
    }


def _v1_review_scan_window(book_day: str) -> tuple[datetime, datetime]:
    """[D-1d, D+2d) — every row that can carry book day D by stamp."""
    year, month, dom = (int(part) for part in book_day.split("-"))
    start = datetime(year, month, dom, tzinfo=timezone.utc)
    return start - timedelta(days=1), start + timedelta(days=2)


def paper_v1_review(
    session: Session,
    chain: str = "sol",
    *,
    day: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Daily short-list review: picked / skipped / queued / why / early outcome."""
    from .desk_lines import lines_for_scorer
    from .scoring.paper_v1 import (
        PAPER_V1_CAP,
        PAPER_V1_CAP_PER_CHAIN,
        PAPER_V1_LINE,
        PAPER_V1_QUEUED,
        PAPER_V1_SKIPPED,
        PAPER_V1_SHADOW_LINE,
        close_reason_label,
        v1_day,
        v1_parse_queue,
        v1_skip_family,
        v1_why,
    )
    from .scoring.hold_conviction import load_hold_shadow_log
    from .scoring.thesis_weights import load_rank_policy, load_thesis_weights

    now = now or utcnow()
    chain = normalize_chain(chain)
    book_day = day or v1_day(now)

    summary = paper_v1_summary(session, chain, now=now)
    hi = float(lines_for_scorer("first_sight", chain).hi)
    # Bound the scan by chain and a book-day window, not a global row count:
    # an RH day that wrote 160 labels used to push the previous day (and all
    # of Sol) out of a 240-row tail, so day-delta's "yesterday" read empty.
    # A row is stamped for its book day but may be written the day before
    # (queued after the 23:00 lock) or after (reconsider across midnight).
    lo, hi_day = _v1_review_scan_window(book_day)
    from .scoring.high_si_learn import PAPER_V1_SKIP_SI_PRINTER

    si_pr_book_stamp = f"{PAPER_V1_SKIP_SI_PRINTER}|d:{book_day}"
    rows = (
        session.query(PaperFill, Token)
        .join(Token, Token.id == PaperFill.token_id)
        .filter(
            PaperFill.chain == chain,
            PaperFill.line.in_((PAPER_V1_LINE, PAPER_V1_SHADOW_LINE)),
            or_(
                and_(PaperFill.opened_at >= lo, PaperFill.opened_at < hi_day),
                PaperFill.exit_reason == si_pr_book_stamp,
            ),
        )
        .order_by(PaperFill.id.desc())
        .all()
    )
    buckets: dict[str, list[dict[str, Any]]] = {
        "picked": [],
        "queued": [],
        "skipped": [],
        "closed": [],
        "shadow": [],
    }
    for fill, token in rows:
        if fill.chain != chain:
            continue
        live, stamped = v1_parse_queue(fill.exit_reason or "")
        book = stamped or _v1_book_day(fill)
        if book != book_day:
            continue
        thesis = _v1_thesis(session, fill.token_id, fill.decision_id)
        entry_m = float(fill.entry_mcap or 0.0)
        last_m = float(fill.last_mcap or 0.0)
        peak = float(fill.max_mcap or 0.0)
        multiple = round(last_m / entry_m, 3) if entry_m > 0 else None
        peak_x = round(peak / entry_m, 3) if entry_m > 0 else None
        skip_label = close_reason_label(fill.exit_reason or "")
        item = {
            "id": fill.id,
            "mint": fill.mint,
            "symbol": token.symbol or "",
            "name": token.name or "",
            "chain": fill.chain,
            "status": fill.status,
            "line": fill.line,
            "book_day": book,
            "entry_p": round(float(fill.entry_p or 0.0), 4),
            "live_p": live,
            "thesis": thesis,
            "thesis_score": thesis.get("score"),
            "tags": thesis.get("tags") or [],
            "why": v1_why(thesis, entry_p=float(fill.entry_p or 0.0), live_p=live, hi=hi, chain=fill.chain),
            "skip_reason": skip_label if fill.status == PAPER_V1_SKIPPED else "",
            "skip_family": v1_skip_family(skip_label),
            "multiple": multiple,
            "peak_multiple": peak_x,
            "return_pct": None if fill.return_pct is None else round(float(fill.return_pct), 1),
            "exit_reason": skip_label,
            "opened_at": fill.opened_at.isoformat() if fill.opened_at else None,
            "closed_at": fill.closed_at.isoformat() if fill.closed_at else None,
            "hit2x": bool(entry_m > 0 and peak >= 2.0 * entry_m),
            "hit5x": bool(entry_m > 0 and peak >= 5.0 * entry_m),
            "image_rev": fill.image_rev or "",
            "shadow": fill.line == PAPER_V1_SHADOW_LINE,
        }
        if fill.line == PAPER_V1_LINE and fill.status == "open":
            from .scoring.hold_conviction import hold_conviction_for_fill

            item["hold_conviction"] = hold_conviction_for_fill(
                session, token, fill, live_p=live
            )
        if fill.line == PAPER_V1_SHADOW_LINE:
            if skip_label.startswith("v1 si-pr-stale"):
                continue
            buckets["shadow"].append(item)
            buckets["skipped"].append(item)
            continue
        if fill.status == "open":
            buckets["picked"].append(item)
        elif fill.status == PAPER_V1_QUEUED:
            buckets["queued"].append(item)
        elif fill.status == PAPER_V1_SKIPPED:
            buckets["skipped"].append(item)
        elif fill.status == "closed":
            buckets["closed"].append(item)
            buckets["picked"].append(item)
    for key in buckets:
        buckets[key].sort(key=lambda r: (-float(r.get("thesis_score") or 0.0), str(r.get("symbol") or "")))
    by_chain = _v1_taken_by_chain(session, book_day)
    taken = _v1_taken(session, book_day)
    taken_chain = _v1_taken_chain(session, book_day, chain)
    room = max(0, PAPER_V1_CAP_PER_CHAIN - taken_chain)
    silence = None
    if not buckets["picked"] and not buckets["queued"] and not buckets["skipped"] and not buckets["shadow"]:
        if taken_chain >= PAPER_V1_CAP_PER_CHAIN:
            silence = (
                f"This chain's {PAPER_V1_CAP_PER_CHAIN}/day lane is full "
                f"({taken_chain} taken). Other chain has its own independent lane."
            )
        else:
            silence = (
                "No paperV1 rows for this chain today — inventory empty or "
                "leftover/Live/floor filters blocked qualify (not a shared-cap silence)."
            )
    return {
        "chain": chain,
        "day": book_day,
        "cap": PAPER_V1_CAP,
        "cap_per_chain": PAPER_V1_CAP_PER_CHAIN,
        "paper_only": True,
        "locked": _v1_locked(session, book_day),
        "taken": taken,
        "taken_chain": taken_chain,
        "room": room,
        "by_chain": by_chain,
        "silence": silence,
        "summary": summary,
        "metrics": {
            "hit2x": summary.get("hit2x"),
            "hit5x": summary.get("hit5x"),
            "hit2x_n": summary.get("hit2x_n"),
            "hit5x_n": summary.get("hit5x_n"),
            "closed": summary.get("closed"),
            "avg_return_pct": summary.get("avg_return_pct"),
            "wins": summary.get("wins"),
        },
        "picked": buckets["picked"],
        "queued": buckets["queued"],
        "skipped": buckets["skipped"],
        "shadow": buckets["shadow"],
        "closed": buckets["closed"],
        "rule": summary.get("rule"),
        "thesis_weights": load_thesis_weights(session),
        "rank_policy": load_rank_policy(session),
        "meme_quality": meme_quality_review_samples(session, chain, limit=20),
        "hold_shadow": load_hold_shadow_log(session, limit=24),
    }


def paper_v1_day_report(
    session: Session,
    *,
    day: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """UTC-day export for Learn / Telegram: both chains, skips, shadow, gate.

    Paper only. Safe to call every hour; does not mutate state.
    """
    from .risk import risk_status
    from .scoring.paper_v1 import PAPER_V1_THESIS_MIN, THESIS_COVERAGE_DEF, v1_day, v1_thesis_coverage
    from .scoring.thesis_weights import load_rank_policy, load_thesis_weights

    now = now or utcnow()
    book_day = day or v1_day(now)
    chains = ("sol", "robinhood")
    by_chain: dict[str, Any] = {}
    totals = {
        "picked": 0,
        "queued": 0,
        "skipped": 0,
        "shadow": 0,
        "closed": 0,
        "hit2x_n": 0,
        "thesis_tagged": 0,
        "soft_path_ready": 0,
        "names": 0,
    }
    skip_reasons: dict[str, int] = {}
    tag_counts: dict[str, int] = {}
    opens: list[dict[str, Any]] = []
    for chain in chains:
        review = paper_v1_review(session, chain, day=book_day, now=now)
        seen_open: set[Any] = set()
        for row in list(review.get("picked") or []) + list(review.get("closed") or []):
            if row.get("id") in seen_open:
                continue
            seen_open.add(row.get("id"))
            opens.append(row)
        by_chain[chain] = {
            "picked": len(review.get("picked") or []),
            "queued": len(review.get("queued") or []),
            "skipped": len(review.get("skipped") or []),
            "shadow": len(review.get("shadow") or []),
            "closed": len(review.get("closed") or []),
            "metrics": review.get("metrics") or {},
            "taken": review.get("taken"),
            "locked": review.get("locked"),
        }
        for key in ("picked", "queued", "skipped", "shadow", "closed"):
            totals[key] += by_chain[chain][key]
        totals["hit2x_n"] += int((review.get("metrics") or {}).get("hit2x_n") or 0)
        for bucket in ("picked", "queued", "skipped", "shadow", "closed"):
            for row in review.get(bucket) or []:
                totals["names"] += 1
                tags = row.get("tags") or []
                if tags:
                    totals["thesis_tagged"] += 1
                score = float(row.get("thesis_score") or 0.0)
                if score >= PAPER_V1_THESIS_MIN and tags:
                    totals["soft_path_ready"] += 1
                for tag in tags:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
                reason = (row.get("skip_reason") or "").strip()
                if reason:
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
    cov = v1_thesis_coverage(opens)
    gate = {
        # The gate number: hard tag on opens (same definition as day-delta
        # and scripts/gate_status.py). Any-tag over every label of the day
        # stays visible, labelled, so nobody reads 100% meme as coverage.
        "thesis_coverage": cov["coverage"],
        "thesis_coverage_def": THESIS_COVERAGE_DEF,
        "thesis_opens_n": cov["n"],
        "thesis_hard_tag_n": cov["hard_tag_n"],
        "thesis_any_tag_on_opens": cov["any_tag_coverage"],
        "thesis_any_tag_all_labels": (
            round(totals["thesis_tagged"] / totals["names"], 3) if totals["names"] else None
        ),
        "soft_path_ready": totals["soft_path_ready"],
        "sample_closed": totals["closed"],
        "hit2x_n": totals["hit2x_n"],
        "production_gate": {
            "thesis_coverage_bar": 0.80,
            "thesis_coverage_def": THESIS_COVERAGE_DEF,
            "sample_bar": 30,
            "note": "Paper only until gate bars clear on short-list closes.",
        },
    }
    return {
        "day": book_day,
        "paper_only": True,
        "generated_at": now.isoformat(),
        "chains": by_chain,
        "totals": totals,
        "skip_reasons": skip_reasons,
        "tag_counts": tag_counts,
        "thesis_weights": load_thesis_weights(session),
        "rank_policy": load_rank_policy(session),
        "risk": risk_status(session),
        "gate": gate,
        "text": _day_report_text(book_day, totals, skip_reasons, tag_counts, gate),
    }


def _day_report_text(
    day: str,
    totals: dict[str, Any],
    skip_reasons: dict[str, int],
    tag_counts: dict[str, int],
    gate: dict[str, Any],
) -> str:
    skips = ", ".join(f"{k}×{v}" for k, v in sorted(skip_reasons.items(), key=lambda kv: -kv[1])[:6]) or "—"
    tags = ", ".join(f"{k}×{v}" for k, v in sorted(tag_counts.items(), key=lambda kv: -kv[1])[:6]) or "—"
    cov = gate.get("thesis_coverage")
    cov_s = "—" if cov is None else f"{cov:.0%}"
    return (
        f"paperV1 {day} · picked {totals.get('picked', 0)} · queued {totals.get('queued', 0)} · "
        f"skipped {totals.get('skipped', 0)} · shadow {totals.get('shadow', 0)} · "
        f"closed {totals.get('closed', 0)} hit2× {totals.get('hit2x_n', 0)} · "
        f"hard-tag cov on opens {cov_s} (n={gate.get('thesis_opens_n') or 0}) · "
        f"tags [{tags}] · skips [{skips}] · paper only"
    )


def paper_v1_book(session: Session, chain: str = "sol", *, now: datetime | None = None) -> dict[str, Any]:
    """First-class short-list view for the desk. Paper fills only."""
    from .scoring.paper_v1 import (
        PAPER_V1_CAP,
        PAPER_V1_CAP_PER_CHAIN,
        PAPER_V1_LINE,
        PAPER_V1_QUEUED,
        PAPER_V1_SKIPPED,
        close_reason_label,
        v1_day,
        v1_parse_queue,
    )
    from .scoring.thesis_weights import load_rank_policy, load_thesis_weights

    now = now or utcnow()
    chain = normalize_chain(chain)
    today = v1_day(now)
    summary = paper_v1_summary(session, chain, now=now)
    rows = (
        session.query(PaperFill, Token)
        .join(Token, Token.id == PaperFill.token_id)
        .filter(PaperFill.line == PAPER_V1_LINE, PaperFill.chain == chain)
        .order_by(PaperFill.id.desc())
        .limit(80)
        .all()
    )
    items: list[dict[str, Any]] = []
    for fill, token in rows:
        live, stamped = v1_parse_queue(fill.exit_reason or "")
        book = stamped or _v1_book_day(fill)
        # Today’s book, plus anything still open/queued.
        if book != today and fill.status not in ("open", PAPER_V1_QUEUED):
            continue
        thesis = _v1_thesis(session, fill.token_id, fill.decision_id)
        items.append(
            {
                "id": fill.id,
                "mint": fill.mint,
                "symbol": token.symbol or "",
                "name": token.name or "",
                "chain": fill.chain,
                "status": fill.status,
                "book_day": book,
                "entry_p": round(float(fill.entry_p or 0.0), 4),
                "live_p": live,
                "entry_mcap": float(fill.entry_mcap or 0.0),
                "last_mcap": float(fill.last_mcap or 0.0),
                "multiple": (
                    round(float(fill.last_mcap or 0.0) / float(fill.entry_mcap), 3)
                    if float(fill.entry_mcap or 0.0) > 0
                    else None
                ),
                "return_pct": None if fill.return_pct is None else round(float(fill.return_pct), 1),
                "exit_reason": close_reason_label(fill.exit_reason or ""),
                "opened_at": fill.opened_at.isoformat() if fill.opened_at else None,
                "closed_at": fill.closed_at.isoformat() if fill.closed_at else None,
                "thesis": thesis,
                "thesis_score": thesis.get("score"),
                "tags": thesis.get("tags") or [],
                "image_rev": fill.image_rev or "",
                "paper_only": True,
            }
        )
    items.sort(
        key=lambda r: (
            0 if r["status"] == "open" else 1 if r["status"] == PAPER_V1_QUEUED else 2 if r["status"] == "closed" else 3,
            -float(r.get("thesis_score") or 0.0),
        )
    )
    return {
        "chain": chain,
        "day": today,
        "cap": PAPER_V1_CAP,
        "cap_per_chain": PAPER_V1_CAP_PER_CHAIN,
        "paper_only": True,
        "summary": summary,
        "items": items,
        "thesis_weights": load_thesis_weights(session),
        "rank_policy": load_rank_policy(session),
    }


def open_paper_fills(session: Session, chain: str, *, now: datetime | None = None) -> list[PaperFill]:
    """Buy every this-window 90+ that clears the gate on its current book. Once."""
    now = now or utcnow()
    chain = normalize_chain(chain)
    opened: list[PaperFill] = []
    from .scoring.live_fit import LIVE_PAPER_HI
    from .scoring.paper_gate import paper_is_fresh_fat

    from .scoring.first_sight import frozen_decision_entry

    for hunt, token in _fill_candidates(session, chain):
        frozen = frozen_decision_entry(session, token.id)
        entry = float(frozen[0] if frozen is not None else (hunt.entry_p or 0.0))
        scorer = (frozen[1] if frozen and frozen[1] else None) or hunt.scorer
        lines = lines_for_scorer(scorer, chain)
        if entry < lines.hi:
            live = _paper_live_p(session, chain, hunt, token)
            if live is None or live < LIVE_PAPER_HI:
                research = token.research
                outcome = token.outcome
                fresh = (
                    chain == "sol"
                    and (scorer or "") == SCORER_FIRST_SIGHT
                    and paper_is_fresh_fat(
                        entry_mcap=float(hunt.t0_mcap or (outcome.t0_mcap if outcome else 0.0) or 0.0),
                        last_mcap=float(hunt.last_mcap or (outcome.last_mcap if outcome else 0.0) or 0.0),
                        last_liq=float(hunt.last_liq or (outcome.last_liq if outcome else 0.0) or 0.0),
                        holders=int((research.holder_count if research else 0) or hunt.holders or 0),
                    )
                )
                if not fresh:
                    continue
        verdict, veto, buy_mcap, liq, flags = _gate_verdict(token, hunt)
        if verdict == "wait":
            continue
        research = token.research
        holders = int((research.holder_count if research else 0) or hunt.holders or 0)
        decision = record_decision(
            session,
            token,
            kind=DECISION_GATE,
            entry_p=entry,
            entry_mcap=buy_mcap,
            liq=liq,
            holders=holders,
            flags=flags,
            veto=veto if verdict == "fail" else "",
            at=now,
            scorer=scorer or SCORER_LEGACY,
        )
        if verdict == "fail" and veto in _shadow_vetoes():
            gate = decision or (
                session.query(Decision)
                .filter(Decision.chain == chain, Decision.mint == token.mint, Decision.kind == DECISION_GATE)
                .first()
            )
            if gate is not None:
                _write_shadow_fill(session, token, gate, now=now, evidence=NO_EVIDENCE, buy_mcap=buy_mcap, liq=liq)
        if verdict != "pass" or buy_mcap <= 0:
            continue
        from .scoring.paper_gate import (
            PAPER_LATE_OK_KEY,
            PAPER_OPEN_VIA_LATE_OK,
            PAPER_OPEN_VIA_THIN,
            PAPER_PROVISIONAL_KEY,
        )

        feat = _loads(research.features_json if research else "{}", {})
        if not isinstance(feat, dict):
            feat = {}
        open_via = None
        if feat.get(PAPER_LATE_OK_KEY):
            open_via = PAPER_OPEN_VIA_LATE_OK
        elif feat.get(PAPER_PROVISIONAL_KEY):
            open_via = PAPER_OPEN_VIA_THIN
        fill = PaperFill(
            chain=chain,
            mint=token.mint,
            token_id=token.id,
            decision_id=decision.id if decision else None,
            line=PAPER_LINE,
            opened_at=now,
            entry_p=entry,
            entry_mcap=buy_mcap,
            entry_liq=liq,
            target=PAPER_TARGET,
            ride=PAPER_RIDE,
            max_mcap=buy_mcap,
            min_mcap=buy_mcap,
            last_mcap=buy_mcap,
            last_liq=liq,
            status="open",
            open_via=open_via,
            image_rev=IMAGE_REV,
            updated_at=now,
        )
        session.add(fill)
        session.flush()
        opened.append(fill)
        try:
            open_ticket(session, fill, token, flags=flags, now=now)
        except Exception:
            log.exception("ticket open failed for %s", token.mint)
        try:
            live_p = _paper_live_p(session, chain, hunt, token)
            live_src = "live_model"
            if live_p is None and hunt is not None and float(hunt.conviction_p or 0.0) > 0:
                # Hunt tape conviction is better than unknown for Sol score-path.
                live_p = float(hunt.conviction_p or 0.0)
                live_src = "hunt_card"
            try:
                freeze_live_at_entry(session, token.id, live_p, src=live_src, now=now)
            except Exception:
                log.exception("live_at_entry freeze failed for %s", token.mint)
            v1 = _consider_paper_v1(
                session,
                chain,
                token,
                entry=entry,
                live=live_p,
                hi=lines.hi,
                buy_mcap=buy_mcap,
                liq=liq,
                decision_id=decision.id if decision else None,
                now=now,
                late_ok=open_via == PAPER_OPEN_VIA_LATE_OK,
            )
            if v1 is None:
                _consider_paper_v1_shadow(
                    session,
                    chain,
                    token,
                    entry=entry,
                    live=live_p,
                    hi=lines.hi,
                    buy_mcap=buy_mcap,
                    liq=liq,
                    decision_id=decision.id if decision else None,
                    now=now,
                )
        except Exception:
            log.exception("paperV1 queue failed for %s", token.mint)
    if opened:
        log.info("paper ledger opened %s fills on %s", len(opened), chain)
    return opened


def moonbag_return(*, entry: float, peak: float, exit_mcap: float, target: float, ride: float, dead: bool) -> float:
    """Half out at target, rest rides to ride-multiple or the exit print."""
    if entry <= 0:
        return PAPER_DEAD_EXIT
    exit_ret = PAPER_DEAD_EXIT if dead or exit_mcap <= 0 else (exit_mcap / entry) - 1.0
    if peak >= target * entry:
        ride_ret = (ride - 1.0) if peak >= ride * entry else exit_ret
        return 0.5 * (target - 1.0) + 0.5 * ride_ret
    return exit_ret


def mark_paper_fills(session: Session, chain: str, *, now: datetime | None = None) -> int:
    """Mark open fills to the current Hunt/outcome print; close at the horizon or a dead pool."""
    now = now or utcnow()
    chain = normalize_chain(chain)
    rows = (
        session.query(PaperFill, Token)
        .join(Token, Token.id == PaperFill.token_id)
        .options(selectinload(Token.outcome))
        .filter(PaperFill.chain == chain, PaperFill.status == "open")
        .all()
    )
    if not rows:
        return 0
    hunts = {
        h.mint: h
        for h in session.query(HuntCard).filter(HuntCard.chain == chain, HuntCard.mint.in_([t.mint for _, t in rows])).all()
    }
    touched = 0
    for fill, token in rows:
        outcome = token.outcome
        hunt = hunts.get(token.mint)
        last = float((hunt.last_mcap if hunt else 0.0) or (outcome.last_mcap if outcome else 0.0) or 0.0)
        liq = float((hunt.last_liq if hunt else 0.0) or (outcome.last_liq if outcome else 0.0) or 0.0)
        if last > 0:
            fill.last_mcap = last
            fill.last_liq = liq
            # Only a sellable print counts toward the peak we could exit at.
            if liq >= PAPER_SELLABLE_LIQ:
                fill.max_mcap = max(float(fill.max_mcap or 0.0), last)
            fill.min_mcap = min(float(fill.min_mcap or last), last) if fill.min_mcap else last
        fill.updated_at = now
        opened = _aware(fill.opened_at) or now
        age_h = (now - opened).total_seconds() / 3600.0
        dead = 0 < liq < LEDGER_DEAD_LIQ
        rode_out = float(fill.max_mcap or 0.0) >= float(fill.ride or PAPER_RIDE) * float(fill.entry_mcap or 0.0)
        from .scoring.paper_v1 import PAPER_V1_LINE

        live_dump = False
        hold = None
        if not dead and not rode_out:
            from .scoring.paper_gate import paper_live_dump

            # Tape Live, not the raw model. A stale Hunt conviction_p
            # (0.90 while last is dust) must not hold the fill.
            live = _paper_tape_live(
                session,
                chain,
                token,
                fill,
                last=last or float(fill.last_mcap or 0.0),
                liq=liq,
                peak=float(fill.max_mcap or 0.0),
            )
            if live is None and hunt is not None:
                live = float(hunt.conviction_p or 0.0)
            from .scoring.hold_conviction import (
                hold_conviction_for_fill,
                paper_live_dump_with_hold,
                paper_no_run_with_hold,
                record_hold_shadow,
            )

            entry_m = float(fill.entry_mcap or 0.0)
            peak_m = float(fill.max_mcap or 0.0)
            last_m = last or float(fill.last_mcap or 0.0)
            hold = None
            if fill.line == PAPER_V1_LINE:
                hold = hold_conviction_for_fill(session, token, fill, live_p=live)
                base_dump = paper_live_dump(
                    entry_mcap=entry_m,
                    peak_mcap=peak_m,
                    last_mcap=last_m,
                    live_p=live,
                )
                live_dump = paper_live_dump_with_hold(
                    entry_mcap=entry_m,
                    peak_mcap=peak_m,
                    last_mcap=last_m,
                    live_p=live,
                    hold_conviction=float(hold.get("score") or 0.0),
                )
                record_hold_shadow(
                    session,
                    {
                        "mint": fill.mint,
                        "chain": fill.chain,
                        "hold": hold.get("score"),
                        "policy": hold.get("policy"),
                        "live_p": live,
                        "would_dump_base": base_dump,
                        "would_dump_adj": live_dump,
                    },
                )
            else:
                live_dump = paper_live_dump(
                    entry_mcap=entry_m,
                    peak_mcap=peak_m,
                    last_mcap=last_m,
                    live_p=live,
                )
        from .scoring.paper_gate import paper_no_run_exit

        if fill.line == PAPER_V1_LINE and hold is not None:
            no_run = (
                not dead
                and not rode_out
                and not live_dump
                and paper_no_run_with_hold(
                    entry_mcap=float(fill.entry_mcap or 0.0),
                    peak_mcap=float(fill.max_mcap or 0.0),
                    last_mcap=last or float(fill.last_mcap or 0.0),
                    age_hours=age_h,
                    hold_conviction=float(hold.get("score") or 0.0),
                )
            )
        else:
            no_run = (
                not dead
                and not rode_out
                and not live_dump
                and paper_no_run_exit(
                    entry_mcap=float(fill.entry_mcap or 0.0),
                    peak_mcap=float(fill.max_mcap or 0.0),
                    last_mcap=last or float(fill.last_mcap or 0.0),
                    age_hours=age_h,
                )
            )
        if age_h >= PAPER_HOLD_HOURS or dead or rode_out or live_dump or no_run:
            exit_mcap = float(fill.last_mcap or 0.0)
            ret = moonbag_return(
                entry=float(fill.entry_mcap or 0.0),
                peak=float(fill.max_mcap or 0.0),
                exit_mcap=exit_mcap,
                target=float(fill.target or PAPER_TARGET),
                ride=float(fill.ride or PAPER_RIDE),
                dead=dead,
            )
            fill.status = "closed"
            fill.closed_at = now
            fill.exit_mcap = exit_mcap
            reason = (
                "ride"
                if rode_out
                else (
                    "dead pool"
                    if dead
                    else ("live dump" if live_dump else ("no run" if no_run else "24h"))
                )
            )
            from .scoring.paper_v1 import PAPER_V1_LINE, book_day_from_reason, v1_close_reason, v1_day

            if fill.line == PAPER_V1_LINE:
                day = book_day_from_reason(fill.exit_reason or "") or v1_day(_aware(fill.opened_at) or now)
                fill.exit_reason = v1_close_reason(reason, day)
            else:
                fill.exit_reason = reason
            fill.return_pct = round(ret * 100.0, 1)
        touched += 1
    session.flush()
    return touched


def sync_paper_ledger(session: Session, chain: str, *, now: datetime | None = None) -> dict[str, int]:
    now = now or utcnow()
    opened = open_paper_fills(session, chain, now=now)
    shadow = backfill_shadow_late(session, chain, now=now)
    marked = mark_paper_fills(session, chain, now=now)
    return {"opened": len(opened), "marked": marked, "shadow": shadow}


def _fill_row(fill: PaperFill, symbol: str) -> dict[str, Any]:
    entry = float(fill.entry_mcap or 0.0)
    last = float(fill.last_mcap or 0.0)
    return {
        "symbol": symbol,
        "mint": fill.mint,
        "p_good": round(float(fill.entry_p or 0.0), 4),
        "entry_mcap": round(entry),
        "multiple": round((last / entry) if entry > 0 and last > 0 else 0.0, 2),
        "peak_multiple": round((float(fill.max_mcap or 0.0) / entry) if entry > 0 else 0.0, 2),
        "opened_at": _aware(fill.opened_at).isoformat() if fill.opened_at else None,
        "closed_at": _aware(fill.closed_at).isoformat() if fill.closed_at else None,
        "exit_reason": fill.exit_reason or "",
        "return_pct": fill.return_pct,
        "image_rev": fill.image_rev,
    }


def _shadow_late_summary(session: Session, chain: str) -> dict[str, Any]:
    """Moonbag result of the late vetoes. Not part of the gated book."""
    rows = (
        session.query(PaperFill, Decision.veto)
        .outerjoin(Decision, Decision.id == PaperFill.decision_id)
        .filter(PaperFill.chain == chain, PaperFill.line == PAPER_SHADOW_LINE)
        .all()
    )
    closed = [(fill, veto or "") for fill, veto in rows if fill.status == "closed"]
    rets = [float(fill.return_pct or 0.0) for fill, _ in closed]
    wins = 0
    for fill, _ in closed:
        entry = float(fill.entry_mcap or 0.0)
        if entry > 0 and float(fill.max_mcap or 0.0) >= PAPER_TARGET * entry:
            wins += 1
    by: dict[str, list[float]] = {}
    by_wins: dict[str, int] = {}
    for fill, veto in closed:
        key = veto or "?"
        by.setdefault(key, []).append(float(fill.return_pct or 0.0))
        entry = float(fill.entry_mcap or 0.0)
        if entry > 0 and float(fill.max_mcap or 0.0) >= PAPER_TARGET * entry:
            by_wins[key] = by_wins.get(key, 0) + 1
    n = len(closed)
    return {
        "n": n,
        "open": sum(1 for fill, _ in rows if fill.status == "open"),
        "wins": wins,
        "avg_return_pct": round(sum(rets) / n, 1) if n else None,
        "total_return_pct": round(sum(rets), 1) if n else 0.0,
        "by_veto": {
            key: {
                "n": len(vals),
                "wins": by_wins.get(key, 0),
                "avg_return_pct": round(sum(vals) / len(vals), 1) if vals else None,
            }
            for key, vals in sorted(by.items())
        },
    }


def paper_ledger_view(session: Session, chain: str, *, target: float = PAPER_TARGET, ride: float = PAPER_RIDE) -> dict[str, Any]:
    """Same shape as /api/paper, read from persisted fills."""
    chain = normalize_chain(chain)
    rows = (
        session.query(PaperFill, Token.symbol)
        .join(Token, Token.id == PaperFill.token_id)
        .filter(PaperFill.chain == chain, PaperFill.line == PAPER_LINE)
        .order_by(PaperFill.opened_at.desc())
        .all()
    )
    closed = [_fill_row(f, s) for f, s in rows if f.status == "closed"]
    open_pos = [_fill_row(f, s) for f, s in rows if f.status == "open"]
    wins = sum(1 for f, _ in rows if f.status == "closed" and float(f.max_mcap or 0.0) >= float(f.target or target) * float(f.entry_mcap or 0.0))
    total = sum(float(f.return_pct or 0.0) for f, _ in rows if f.status == "closed")
    gates = (
        session.query(Decision, Token.symbol)
        .join(Token, Token.id == Decision.token_id)
        .filter(Decision.chain == chain, Decision.kind == DECISION_GATE, Decision.veto != "")
        .order_by(Decision.at.desc())
        .limit(20)
        .all()
    )
    current = desk_lines(session, chain)
    from .scoring.live_fit import LIVE_PAPER_HI

    watch = ""
    if current.scorer == SCORER_FIRST_SIGHT:
        from .scoring.paper_gate import PAPER_CHASE_MULT, PAPER_WINDOW_RH

        if chain == "sol":
            watch = (
                f", or Sol watch>={current.lo:.2f} when Live>={LIVE_PAPER_HI:.2f} "
                f"or a fresh fat book with a known holder count; "
                f"skip two-tick and late chase>={PAPER_CHASE_MULT:g}x "
                f"(quality bypass)"
            )
        elif chain == "robinhood":
            watch = (
                f", or RH watch>={current.lo:.2f} when Live>={LIVE_PAPER_HI:.2f} "
                f"and age<{PAPER_WINDOW_RH:g}h; skip two-tick and late chase>={PAPER_CHASE_MULT:g}x "
                f"(quality bypass); "
                f"start-high and pre-pumped still paper; no fresh-fat bypass"
            )
    from .scoring.paper_scorecard import paper_scorecard

    scorecard = paper_scorecard([f for f, _ in rows], lines=current, target=target)
    scorecard["shadow_late"] = _shadow_late_summary(session, chain)
    scorecard["paper_v1"] = paper_v1_summary(session, chain)
    return {
        "strategy": (
            f"ledger: buy this-window Entry>={current.hi:.2f} ({current.scorer} line) at the first liquid print if no veto"
            f"{watch}; sell half at {target:g}x, ride half to {ride:g}x or 24h; "
            f"exit a run on live dump; exit a never-1.5x grave after 1h when last < 50% of peak"
        ),
        "mode": "moonbag",
        "gated": True,
        "ledger": True,
        "lines": current.as_dict(),
        "closed_trades": len(closed),
        "open_positions": len(open_pos),
        "pending": 0,
        "wins": wins,
        "unfilled_2x_wicks": 0,
        "target": target,
        "ride": ride,
        "avg_return_pct": round(total / len(closed), 1) if closed else None,
        "total_return_pct": round(total, 1),
        "scorecard": scorecard,
        "closed": closed[:50],
        "open": open_pos[:50],
        "waiting": [],
        "vetoed": [
            {
                "mint": g.mint,
                "symbol": symbol or "",
                "p_good": round(float(g.entry_p or 0.0), 4),
                "entry_mcap": round(float(g.entry_mcap or 0.0)),
                "liq": round(float(g.liq or 0.0)),
                "veto": g.veto,
                "at": _aware(g.at).isoformat(),
            }
            for g, symbol in gates
        ],
    }


def paper_scorecard_view(session: Session, chain: str, *, target: float = PAPER_TARGET) -> dict[str, Any]:
    """GET /api/paper/scorecard — this-window vs leftover-clock, no row dump."""
    view = paper_ledger_view(session, chain, target=target)
    return {
        "chain": normalize_chain(chain),
        "ledger": True,
        "lines": view["lines"],
        "strategy": view["strategy"],
        "closed_trades": view["closed_trades"],
        "open_positions": view["open_positions"],
        "wins": view["wins"],
        "avg_return_pct": view["avg_return_pct"],
        "total_return_pct": view["total_return_pct"],
        "scorecard": view["scorecard"],
    }


# --------------------------------------------------------------------------
# Tickets (Phase 3, shadow only)
# --------------------------------------------------------------------------


def ticket_size_usd(liq: float) -> float:
    if liq <= 0:
        return 0.0
    return round(max(TICKET_MIN_USD, min(TICKET_MAX_USD, liq * TICKET_LIQ_FRACTION)), 2)


def ticket_slippage_pct(size_usd: float, liq: float) -> float:
    """Constant-product estimate: quote side holds ~half the pool."""
    if liq <= 0 or size_usd <= 0:
        return 0.0
    quote = liq / 2.0
    return round(size_usd / (quote + size_usd) * 100.0, 2)


def open_ticket(session: Session, fill: PaperFill, token: Token, *, flags: list[str] | None = None, now: datetime | None = None) -> Ticket | None:
    now = now or utcnow()
    chain = normalize_chain(token.chain or "sol")
    if session.query(Ticket.id).filter(Ticket.chain == chain, Ticket.mint == token.mint).first():
        return None
    research = token.research
    reasons = _loads(research.reasons_json if research else "[]", [])[:4]
    size = ticket_size_usd(float(fill.entry_liq or 0.0))
    ticket = Ticket(
        chain=chain,
        mint=token.mint,
        token_id=token.id,
        fill_id=fill.id,
        decision_id=fill.decision_id,
        created_at=now,
        symbol=(token.symbol or "")[:32],
        entry_p=float(fill.entry_p or 0.0),
        entry_mcap=float(fill.entry_mcap or 0.0),
        liq=float(fill.entry_liq or 0.0),
        size_usd=size,
        slippage_pct=ticket_slippage_pct(size, float(fill.entry_liq or 0.0)),
        stop_mult=TICKET_STOP_MULT,
        take_mult=float(fill.target or PAPER_TARGET),
        ride_mult=float(fill.ride or PAPER_RIDE),
        reasons_json=json.dumps([str(r) for r in reasons]),
        flags_json=json.dumps([str(f) for f in (flags or [])]),
        status="shadow",
        image_rev=IMAGE_REV,
    )
    session.add(ticket)
    session.flush()
    return ticket


def format_ticket(ticket: Ticket) -> str:
    from .chains import chain_links

    links = chain_links(ticket.mint, ticket.chain)
    reasons = _loads(ticket.reasons_json, [])
    lines = [
        f"🎫 TICKET {ticket.symbol or ticket.mint[:8]} — Entry {float(ticket.entry_p or 0):.0%} ({ticket.chain})",
        f"buy ~${float(ticket.size_usd or 0):,.0f} at mcap ${float(ticket.entry_mcap or 0):,.0f} · liq ${float(ticket.liq or 0):,.0f} · est. slippage {float(ticket.slippage_pct or 0):.1f}%",
        f"stop {float(ticket.stop_mult or 0):.2f}x · take half {float(ticket.take_mult or 0):g}x · ride {float(ticket.ride_mult or 0):g}x / 24h",
        f"why: {'; '.join(str(r) for r in reasons[:3])}" if reasons else "",
        "shadow ticket — nothing is executed",
        links.get("pons") or links.get("pump") or ticket.mint,
        links.get("gmgn") or "",
    ]
    return "\n".join(line for line in lines if line)


async def alert_new_tickets(session: Session, *, limit: int = 10) -> int:
    """Send unalerted shadow tickets to Telegram / Discord when configured.

    Marks them alerted either way so a restart never re-pings.
    """
    from .alerts import alerts_configured
    from .config import settings
    from .httputil import post_json

    rows = session.query(Ticket).filter(Ticket.alerted.is_(False)).order_by(Ticket.created_at.asc()).limit(limit).all()
    sent = 0
    for ticket in rows:
        text = format_ticket(ticket)
        if alerts_configured():
            try:
                if settings.telegram_bot_token and settings.telegram_chat_id:
                    await post_json(
                        f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                        {"chat_id": settings.telegram_chat_id, "text": text, "disable_web_page_preview": True},
                    )
                if settings.discord_webhook_url:
                    await post_json(settings.discord_webhook_url, {"content": text})
                sent += 1
            except Exception:
                log.exception("ticket alert failed for %s", ticket.mint)
        else:
            log.info("ticket (alerts off): %s", text.replace("\n", " | "))
        ticket.alerted = True
    session.flush()
    return sent


def ticket_view(ticket: Ticket, fill: PaperFill | None) -> dict[str, Any]:
    entry = float(ticket.entry_mcap or 0.0)
    last = float((fill.last_mcap if fill else 0.0) or 0.0)
    return {
        "id": ticket.id,
        "chain": ticket.chain,
        "mint": ticket.mint,
        "symbol": ticket.symbol,
        "created_at": _aware(ticket.created_at).isoformat() if ticket.created_at else None,
        "entry_p": round(float(ticket.entry_p or 0.0), 4),
        "entry_mcap": round(entry),
        "liq": round(float(ticket.liq or 0.0)),
        "size_usd": ticket.size_usd,
        "slippage_pct": ticket.slippage_pct,
        "stop_mult": ticket.stop_mult,
        "take_mult": ticket.take_mult,
        "ride_mult": ticket.ride_mult,
        "reasons": _loads(ticket.reasons_json, []),
        "flags": _loads(ticket.flags_json, []),
        "status": ticket.status,
        "confirmed_at": _aware(ticket.confirmed_at).isoformat() if ticket.confirmed_at else None,
        "alerted": bool(ticket.alerted),
        "fill_status": fill.status if fill else None,
        "multiple": round(last / entry, 2) if entry > 0 and last > 0 else None,
        "peak_multiple": round(float(fill.max_mcap or 0.0) / entry, 2) if fill and entry > 0 else None,
        "return_pct": fill.return_pct if fill else None,
        "image_rev": ticket.image_rev,
    }


def list_tickets(session: Session, chain: str | None = None, *, limit: int = 100) -> list[dict[str, Any]]:
    q = session.query(Ticket, PaperFill).outerjoin(PaperFill, PaperFill.id == Ticket.fill_id)
    if chain:
        q = q.filter(Ticket.chain == normalize_chain(chain))
    rows = q.order_by(Ticket.created_at.desc()).limit(limit).all()
    return [ticket_view(t, f) for t, f in rows]


def set_ticket_status(session: Session, ticket_id: int, status: str) -> dict[str, Any] | None:
    """Manual desk action. ``confirmed`` records the human said yes — nothing executes."""
    if status not in ("shadow", "confirmed", "skipped"):
        return None
    row = session.query(Ticket, PaperFill).outerjoin(PaperFill, PaperFill.id == Ticket.fill_id).filter(Ticket.id == ticket_id).first()
    if row is None:
        return None
    ticket, fill = row
    ticket.status = status
    ticket.confirmed_at = utcnow() if status == "confirmed" else None
    session.flush()
    return ticket_view(ticket, fill)


def ticket_report(session: Session, chain: str, *, weeks: int = 8) -> dict[str, Any]:
    """Forward P&L of the ticket line by ISO week from closed fills."""
    chain = normalize_chain(chain)
    since = utcnow() - timedelta(weeks=weeks)
    rows = (
        session.query(Ticket, PaperFill)
        .join(PaperFill, PaperFill.id == Ticket.fill_id)
        .filter(Ticket.chain == chain, Ticket.created_at >= since)
        .order_by(Ticket.created_at.asc())
        .all()
    )
    table: dict[str, dict[str, Any]] = {}
    for ticket, fill in rows:
        wk = _iso_week(ticket.created_at)
        slot = table.setdefault(wk, {"week": wk, "tickets": 0, "closed": 0, "hit2x": 0, "hit5x": 0, "returns": [], "size_usd": 0.0})
        slot["tickets"] += 1
        slot["size_usd"] += float(ticket.size_usd or 0.0)
        if fill.status != "closed":
            continue
        entry = float(fill.entry_mcap or 0.0)
        peak = float(fill.max_mcap or 0.0)
        slot["closed"] += 1
        slot["hit2x"] += int(entry > 0 and peak >= 2.0 * entry)
        slot["hit5x"] += int(entry > 0 and peak >= 5.0 * entry)
        slot["returns"].append(float(fill.return_pct or 0.0))
    out = []
    for wk in sorted(table):
        s = table[wk]
        c = s["closed"]
        out.append(
            {
                "week": wk,
                "tickets": s["tickets"],
                "closed": c,
                "hit2x_rate": round(s["hit2x"] / c, 3) if c else None,
                "hit5x_rate": round(s["hit5x"] / c, 3) if c else None,
                "avg_return_pct": round(sum(s["returns"]) / c, 1) if c else None,
                "size_usd": round(s["size_usd"], 2),
            }
        )
    days = max(1.0, min(weeks * 7.0, (utcnow() - since).total_seconds() / 86400.0))
    return {
        "chain": chain,
        "weeks": out,
        "tickets_per_day": round(len(rows) / days, 2),
        "targets": {"tickets_per_day": 5, "hit2x_rate": 0.60, "hit5x_rate": 0.25, "forward_weeks": 4},
    }


# --------------------------------------------------------------------------
# Loop heartbeats for /health
# --------------------------------------------------------------------------


def beat(session: Session, name: str, *, note: str = "") -> None:
    key = f"loop:{name}"
    row = session.query(ScanState).filter(ScanState.key == key).one_or_none()
    now = utcnow()
    if row is None:
        session.add(ScanState(key=key, value=note or "", updated_at=now))
    else:
        row.value = note or ""
        row.updated_at = now
    session.flush()


def heartbeats(session: Session) -> dict[str, Any]:
    now = utcnow()
    out: dict[str, Any] = {}
    for row in session.query(ScanState).filter(ScanState.key.like("loop:%")).all():
        ts = _aware(row.updated_at)
        out[row.key.split(":", 1)[1]] = {
            "at": ts.isoformat() if ts else None,
            "age_s": round((now - ts).total_seconds()) if ts else None,
            "note": row.value or "",
        }
    return out


def ledger_counts(session: Session) -> dict[str, int]:
    return {
        "decisions": int(session.query(func.count(Decision.id)).scalar() or 0),
        "paper_fills": int(session.query(func.count(PaperFill.id)).scalar() or 0),
        "tickets": int(session.query(func.count(Ticket.id)).scalar() or 0),
    }


# --------------------------------------------------------------------------
# Desk card context
# --------------------------------------------------------------------------


def ledger_context(session: Session, token: Token, *, now: datetime | None = None) -> dict[str, Any]:
    """What the ledger holds for one mint, for the research card.

    Frozen entry decision (vs the mutable research row), desk-line
    crossings, the gate verdict, the persisted paper fill and its ticket.
    Read-only; nothing here writes.
    """
    now = now or utcnow()
    chain = token_chain(token)
    rows = (
        session.query(Decision)
        .filter(Decision.chain == chain, Decision.mint == token.mint)
        .order_by(Decision.at.asc())
        .all()
    )
    by_kind: dict[str, Decision] = {}
    for row in rows:
        by_kind.setdefault(row.kind, row)
    entry = by_kind.get(DECISION_ENTRY)
    gate = by_kind.get(DECISION_GATE)
    fill = (
        session.query(PaperFill)
        .filter(PaperFill.chain == chain, PaperFill.mint == token.mint, PaperFill.line == PAPER_LINE)
        .order_by(PaperFill.opened_at.desc())
        .first()
    )
    ticket = (
        session.query(Ticket)
        .filter(Ticket.chain == chain, Ticket.mint == token.mint)
        .order_by(Ticket.created_at.desc())
        .first()
    )
    out: dict[str, Any] = {"entry": None, "lines": [], "gate": None, "fill": None, "ticket": None}
    if entry is not None:
        ev = post_decision_evidence(session, [entry]).get(entry.id, NO_EVIDENCE)
        res = decision_result(entry, token.outcome, now=now, evidence=ev)
        out["entry"] = {
            "entry_p": round(float(entry.entry_p or 0.0), 4),
            "entry_mcap": round(float(entry.entry_mcap or 0.0)),
            "liq": round(float(entry.liq or 0.0)),
            "holders": int(entry.holders or 0),
            "at": _aware(entry.at).isoformat() if entry.at else None,
            "source": entry.source,
            "scorer": entry.scorer or SCORER_LEGACY,
            "desk_lines": lines_for_scorer(entry.scorer, chain).as_dict(),
            "image_rev": entry.image_rev,
            "result": res,
        }
    for kind, slot in ((DECISION_LINE70, "lo"), (DECISION_LINE90, "hi")):
        row = by_kind.get(kind)
        if row is not None:
            dl = lines_for_scorer(row.scorer, chain)
            out["lines"].append(
                {
                    "line": kind,
                    "slot": slot,
                    "threshold": dl.lo if slot == "lo" else dl.hi,
                    "scorer": dl.scorer,
                    "at": _aware(row.at).isoformat() if row.at else None,
                    "entry_p": round(float(row.entry_p or 0.0), 4),
                }
            )
    if gate is not None:
        out["gate"] = {
            "veto": gate.veto or "",
            "verdict": "veto" if gate.veto else "fill",
            "entry_p": round(float(gate.entry_p or 0.0), 4),
            "entry_mcap": round(float(gate.entry_mcap or 0.0)),
            "liq": round(float(gate.liq or 0.0)),
            "at": _aware(gate.at).isoformat() if gate.at else None,
        }
    if fill is not None:
        entry_m = float(fill.entry_mcap or 0.0)
        out["fill"] = {
            "status": fill.status,
            "line": fill.line,
            "entry_p": round(float(fill.entry_p or 0.0), 4),
            "entry_mcap": round(entry_m),
            "last_mcap": round(float(fill.last_mcap or 0.0)),
            "max_mcap": round(float(fill.max_mcap or 0.0)),
            "multiple": round(float(fill.last_mcap or 0.0) / entry_m, 2) if entry_m > 0 else None,
            "peak_multiple": round(float(fill.max_mcap or 0.0) / entry_m, 2) if entry_m > 0 else None,
            "return_pct": fill.return_pct,
            "exit_reason": fill.exit_reason or "",
            "opened_at": _aware(fill.opened_at).isoformat() if fill.opened_at else None,
            "closed_at": _aware(fill.closed_at).isoformat() if fill.closed_at else None,
        }
    if ticket is not None:
        out["ticket"] = ticket_view(ticket, fill if fill is not None and fill.id == ticket.fill_id else None)
    return out


def tape_series(session: Session, token: Token, *, hours: float = 48.0, now: datetime | None = None) -> dict[str, Any]:
    """One-minute bars plus the stored snapshots for the desk's own tape strip."""
    from .models import Snapshot, TapeBar

    now = now or utcnow()
    chain = token_chain(token)
    since = now - timedelta(hours=hours)
    bars = (
        session.query(TapeBar.minute, TapeBar.mcap_usd, TapeBar.liquidity_usd, TapeBar.holders)
        .filter(TapeBar.chain == chain, TapeBar.mint == token.mint, TapeBar.minute >= since)
        .order_by(TapeBar.minute.asc())
        .all()
    )
    snaps = (
        session.query(Snapshot.taken_at, Snapshot.mcap_usd, Snapshot.liquidity_usd, Snapshot.kind)
        .filter(Snapshot.token_id == token.id)
        .order_by(Snapshot.taken_at.asc())
        .all()
    )
    outcome = token.outcome
    return {
        "mint": token.mint,
        "chain": chain,
        "t0_mcap": float(outcome.t0_mcap or 0.0) if outcome else 0.0,
        "last_mcap": float(outcome.last_mcap or 0.0) if outcome else 0.0,
        "max_mcap": float(outcome.max_mcap or 0.0) if outcome else 0.0,
        "bars": [
            {"t": _aware(m).isoformat(), "mcap": float(mc or 0.0), "liq": float(lq or 0.0), "holders": int(h or 0)}
            for m, mc, lq, h in bars
            if m is not None
        ],
        "snaps": [
            {"t": _aware(ta).isoformat(), "mcap": float(mc or 0.0), "liq": float(lq or 0.0), "kind": kind or ""}
            for ta, mc, lq, kind in snaps
            if ta is not None and float(mc or 0.0) > 0
        ],
    }
