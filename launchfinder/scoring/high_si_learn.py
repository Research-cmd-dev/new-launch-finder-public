"""High-SI FOMO printer Learn labels — shadow only, copycat-aware.

Catches ELON-class / BAGSPAY leftover printers on the FOMO board with
honest high multiples. Does not change Hunt floors or open paper_v1.

stack-v204: copycat still blocks this path (``high_si_copycat_blocked``).
No safe un-veto for FOMO SI printers that already carry copycat spam —
they stay on ``v1 veto|cc-fomo`` / ``v1 veto|copycat`` Learn labels.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from sqlalchemy import func

from ..chains import normalize_chain, normalize_mint
from ..models import Decision, HuntCard, Outcome, Research, Token, utcnow
from .paper_v1 import (
    PAPER_V1_COPYCAT_VETO,
    PAPER_V1_EXIT_REASON_MAX,
    research_risk_flags_copycat_spam,
    v1_day_reason,
)

# FOMO-desk SI Learn floor (Dave 2026-09-30). No upper cap — 1000×+ printers qualify.
HIGH_SI_MULTIPLE_MIN = 20.0
HIGH_SI_FOMO_LOOKBACK_DAYS = 14
FOMO_DESK_BOARD_STATUS = frozenset({"caught", "leftover"})
HIGH_SI_FOMO_AUDIT_BUCKETS = frozenset(
    {
        "leftover",
        "veto_other",
        "veto_hijack",
        "on_hunt",
        "on_desk",
        "short_list",
        "wide_paper",
    }
)
PAPER_V1_SKIP_SI_PRINTER = "v1 si-pr"
PAPER_V1_SKIP_SI_PR_STALE = "v1 si-pr-stale"


def v1_si_printer_stamp(day: str) -> str:
    stamp = f"{PAPER_V1_SKIP_SI_PRINTER}|{v1_day_reason(day)}"
    if len(stamp) > PAPER_V1_EXIT_REASON_MAX:
        raise ValueError(f"si-printer stamp too long ({len(stamp)}): {stamp!r}")
    return stamp


def v1_si_pr_stale_stamp(day: str) -> str:
    stamp = f"{PAPER_V1_SKIP_SI_PR_STALE}|{v1_day_reason(day)}"
    if len(stamp) > PAPER_V1_EXIT_REASON_MAX:
        raise ValueError(f"si-pr-stale stamp too long ({len(stamp)}): {stamp!r}")
    return stamp


def is_active_si_pr_shadow_reason(reason: str) -> bool:
    raw = (reason or "").lower()
    return raw.startswith(PAPER_V1_SKIP_SI_PRINTER) and not raw.startswith(PAPER_V1_SKIP_SI_PR_STALE)


def _risk_flags_list(research: Research | None) -> list[str]:
    import json

    if research is None:
        return []
    try:
        flags = json.loads(research.risk_flags_json or "[]")
    except Exception:
        return []
    return [str(x) for x in flags] if isinstance(flags, list) else []


def high_si_copycat_blocked(token: Token, research: Research | None) -> bool:
    """Copycat spam only — hijack/e/acc printers use SI shadow; copycat uses copycat shadow."""
    if research_risk_flags_copycat_spam(research.risk_flags_json if research else None):
        return True
    blob = " | ".join(_risk_flags_list(research)).lower()
    return PAPER_V1_COPYCAT_VETO in blob or "copycat spam" in blob


def fomo_high_si_candidate_mints(session: Session, chain: str) -> set[str]:
    from ..ingest.fomo_poll import load_trending_snapshot
    from ..research.fomo_coverage import load_fomo_trending_audit

    mints: set[str] = set()
    snap = load_trending_snapshot(session)
    for raw in (snap or {}).get("items") or []:
        c = normalize_chain(raw.get("chain") or "sol")
        if c != chain:
            continue
        m = normalize_mint(raw.get("mint") or "", c)
        if m:
            mints.add(m)
    audit = load_fomo_trending_audit(session) or {}
    for key in ("items", "top", "vetoed"):
        for item in audit.get(key) or []:
            if not isinstance(item, dict):
                continue
            if normalize_chain(item.get("chain") or "sol") != chain:
                continue
            status = str(item.get("status") or "")
            bucket = str(item.get("bucket") or "")
            if status in FOMO_DESK_BOARD_STATUS or bucket in HIGH_SI_FOMO_AUDIT_BUCKETS:
                m = normalize_mint(str(item.get("mint") or ""), chain)
                if m:
                    mints.add(m)
    return mints


def _aware_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def fomo_board_stub_unactionable(token: Token, outcome: Outcome | None) -> bool:
    """``fomo_board`` with t0=0 and last=0 — hydrate/coverage only, not Hunt."""
    if (token.source or "") != "fomo_board":
        return False
    t0 = float((outcome.t0_mcap if outcome else 0.0) or 0.0)
    last = float((outcome.last_mcap if outcome else 0.0) or 0.0)
    return t0 <= 0.0 and last <= 0.0


def si_pr_label_opened_at(session: Session, token: Token, chain: str) -> datetime:
    """When the desk first froze an entry view — not resurface ``now``."""
    chain = normalize_chain(chain)
    entry = (
        session.query(Decision)
        .filter(Decision.token_id == token.id, Decision.kind == "entry")
        .order_by(Decision.id.asc())
        .first()
    )
    if entry is not None:
        at = _aware_utc(entry.at)
        if at is not None:
            return at
    hunt = (
        session.query(HuntCard)
        .filter(HuntCard.chain == chain, HuntCard.mint == token.mint)
        .order_by(HuntCard.id.asc())
        .first()
    )
    if hunt is not None:
        for candidate in (hunt.first_seen_at, hunt.updated_at):
            at = _aware_utc(candidate)
            if at is not None:
                return at
    for candidate in (token.migrated_at, token.first_seen_at, token.created_at_chain):
        at = _aware_utc(candidate)
        if at is not None:
            return at
    return utcnow()


def frozen_entry_mcap(session: Session, token: Token, chain: str) -> float:
    """Honest entry anchor for printer multiple — frozen decision, then Hunt t0."""
    from ..models import Decision, HuntCard

    entry = (
        session.query(Decision)
        .filter(Decision.token_id == token.id, Decision.kind == "entry")
        .order_by(Decision.id.desc())
        .first()
    )
    if entry is not None and float(entry.entry_mcap or 0.0) > 0:
        return float(entry.entry_mcap)
    hunt = (
        session.query(HuntCard)
        .filter(HuntCard.chain == chain, HuntCard.mint == token.mint)
        .first()
    )
    if hunt is not None and float(hunt.t0_mcap or 0.0) > 0:
        return float(hunt.t0_mcap)
    oc = token.outcome
    if oc is not None and float(oc.t0_mcap or 0.0) > 0:
        return float(oc.t0_mcap)
    return 0.0


def outcome_peak_multiple(outcome: Outcome | None) -> float:
    """Peak/t0 for printer labels — stored ``outcome.multiple`` can lag on dumps."""
    if outcome is None:
        return 0.0
    stored = float(outcome.multiple or 0.0)
    t0 = float(outcome.t0_mcap or 0.0)
    peak = float(outcome.max_mcap or 0.0)
    if t0 > 0 and peak > 0:
        return max(stored, peak / t0)
    return stored


def si_printer_band_ok(mult: float) -> bool:
    return float(mult or 0.0) >= HIGH_SI_MULTIPLE_MIN


def printer_label_multiple(
    session: Session,
    token: Token,
    chain: str,
    outcome: Outcome | None,
    *,
    fomo_row: dict[str, Any] | None = None,
) -> float:
    """si-pr band uses honest FOMO desk ``multiple`` only (no Hunt peak/t0)."""
    return fomo_desk_printer_multiple(fomo_row)


def token_high_si_printer(
    token: Token,
    outcome: Outcome | None,
    *,
    label_multiple: float | None = None,
) -> bool:
    mult = (
        float(label_multiple)
        if label_multiple is not None
        else outcome_peak_multiple(outcome)
    )
    if mult <= 0 and outcome is None:
        return False
    return si_printer_band_ok(mult)


def fomo_desk_on_board(fomo_row: dict[str, Any] | None) -> bool:
    """True when row is from FOMO snapshot/audit desk merge (not Hunt-only)."""
    if not fomo_row:
        return False
    status = str(fomo_row.get("status") or "")
    bucket = str(fomo_row.get("bucket") or "")
    if status in FOMO_DESK_BOARD_STATUS:
        return True
    if bucket in HIGH_SI_FOMO_AUDIT_BUCKETS:
        return True
    return False


def fomo_desk_si_pr_eligible(fomo_row: dict[str, Any] | None) -> bool:
    """FOMO desk printer ≥20× on board — required before si-pr stamp/resurface."""
    if not fomo_desk_on_board(fomo_row):
        return False
    return si_printer_band_ok(fomo_desk_printer_multiple(fomo_row))


def token_on_high_si_surface(
    session: Session,
    token: Token,
    chain: str,
    fomo_mints: set[str],
    *,
    outcome: Outcome | None = None,
    fomo_metrics: dict[str, dict[str, Any]] | None = None,
) -> bool:
    """Alias for si-pr gate — FOMO desk row in band only."""
    row = fomo_metric_row(fomo_metrics, chain, token.mint or "")
    return fomo_desk_si_pr_eligible(row)


def fomo_metric_row(
    fomo_metrics: dict[str, dict[str, Any]] | None,
    chain: str,
    mint: str,
) -> dict[str, Any] | None:
    if not fomo_metrics or not mint:
        return None
    row = fomo_metrics.get(mint)
    if row is None and chain == "robinhood":
        row = fomo_metrics.get(mint.lower())
    return row


def iter_canonical_fomo_metrics(
    fomo_metrics: dict[str, dict[str, Any]] | None,
) -> list[tuple[str, dict[str, Any]]]:
    """One row per board mint (skip RH lowercase alias keys)."""
    seen: set[str] = set()
    out: list[tuple[str, dict[str, Any]]] = []
    for key, row in (fomo_metrics or {}).items():
        chain = normalize_chain((row or {}).get("chain") or "sol")
        mint = normalize_mint(str((row or {}).get("mint") or key), chain)
        if not mint or mint in seen:
            continue
        seen.add(mint)
        out.append((mint, dict(row or {}, mint=mint, chain=chain)))
    return out


def resolve_desk_token(session: Session, chain: str, mint: str) -> Token | None:
    chain = normalize_chain(chain)
    mint = normalize_mint(mint, chain)
    tok = (
        session.query(Token)
        .filter(Token.chain == chain, Token.mint == mint)
        .one_or_none()
    )
    if tok is not None:
        return tok
    if chain == "robinhood":
        tok = (
            session.query(Token)
            .filter(Token.chain == chain, func.lower(Token.mint) == mint.lower())
            .one_or_none()
        )
        if tok is not None:
            return tok
    tok = session.query(Token).filter(Token.mint == mint).one_or_none()
    if tok is not None and normalize_chain(tok.chain or "") == chain:
        return tok
    return None


def zeroed_fomo_board_outcome(token_id: int) -> Outcome:
    """Placeholder outcome so hydrate/Hunt joins work before first Dex print."""
    return Outcome(
        token_id=token_id,
        t0_mcap=0.0,
        max_mcap=0.0,
        last_liq=0.0,
        last_mcap=0.0,
        multiple=0.0,
    )


def ensure_fomo_desk_outcome(session: Session, token: Token) -> Outcome:
    if token.outcome is not None:
        return token.outcome
    existing = (
        session.query(Outcome).filter(Outcome.token_id == token.id).one_or_none()
    )
    if existing is not None:
        token.outcome = existing
        return existing
    oc = zeroed_fomo_board_outcome(token.id)
    token.outcome = oc
    session.add(oc)
    session.flush()
    return oc


def repair_fomo_board_missing_outcomes(
    session: Session, chain: str | None = None
) -> int:
    """One-shot repair: fomo_board tokens without an Outcome row (AQUA-class)."""
    q = (
        session.query(Token)
        .outerjoin(Outcome, Outcome.token_id == Token.id)
        .filter(Token.source == "fomo_board")
        .filter(Outcome.id.is_(None))
    )
    if chain is not None:
        q = q.filter(Token.chain == normalize_chain(chain))
    repaired = 0
    for token in q.all():
        ensure_fomo_desk_outcome(session, token)
        repaired += 1
    return repaired


def ensure_fomo_desk_token(
    session: Session, chain: str, fomo_row: dict[str, Any]
) -> Token | None:
    """Link a FOMO board row to a Token row without opening paper_v1."""
    chain = normalize_chain(chain)
    mint = normalize_mint(str(fomo_row.get("mint") or ""), chain)
    if not mint:
        return None
    existing = resolve_desk_token(session, chain, mint)
    if existing is not None:
        ensure_fomo_desk_outcome(session, existing)
        return existing
    symbol = str(fomo_row.get("symbol") or "")[:32]
    token = Token(
        mint=mint,
        chain=chain,
        symbol=symbol,
        name=symbol,
        source="fomo_board",
        first_seen_at=utcnow(),
    )
    token.research = Research(
        p_good=0.0,
        features_json="{}",
        risk_flags_json="[]",
    )
    session.add(token)
    session.flush()
    ensure_fomo_desk_outcome(session, token)
    return token


def fomo_desk_printer_multiple(fomo_row: dict[str, Any] | None) -> float:
    if not fomo_row:
        return 0.0
    return float(fomo_row.get("multiple") or 0.0)
