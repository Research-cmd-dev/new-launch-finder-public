"""Production risk controls stub — paper now, live later.

Defaults are safe: live arm is **off**, kill switch is **off** (not tripped),
chain pauses empty, notional caps are tiny placeholders. Nothing here
executes a swap. paperV1 queue/promote/lock respect kill + chain pause so
an operator can freeze the short list without redeploying.

Persisted in ScanState key ``risk:controls``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from .models import ScanState, utcnow

log = logging.getLogger("launchfinder.risk")

RISK_KEY = "risk:controls"
DEFAULTS: dict[str, Any] = {
    # Live execution arm — must stay false until production gate passes.
    "armed": False,
    # When True: no new paperV1 opens / promotes; would also block live.
    "kill_switch": False,
    # Soft caps for a future live path (USD). Paper ignores size today.
    "max_notional_usd": 200.0,
    "max_per_name_usd": 40.0,
    # Chains paused for paperV1 + future live: ["sol"], ["robinhood"], both.
    "chain_pause": [],
    "note": "",
}


def defaults() -> dict[str, Any]:
    return dict(DEFAULTS)


def _normalize(raw: dict[str, Any] | None) -> dict[str, Any]:
    out = defaults()
    if not isinstance(raw, dict):
        return out
    out["armed"] = bool(raw.get("armed", False))
    out["kill_switch"] = bool(raw.get("kill_switch", False))
    try:
        out["max_notional_usd"] = max(0.0, float(raw.get("max_notional_usd", out["max_notional_usd"])))
    except (TypeError, ValueError):
        pass
    try:
        out["max_per_name_usd"] = max(0.0, float(raw.get("max_per_name_usd", out["max_per_name_usd"])))
    except (TypeError, ValueError):
        pass
    pauses = raw.get("chain_pause") or []
    if isinstance(pauses, str):
        pauses = [pauses]
    clean: list[str] = []
    for item in pauses:
        name = str(item or "").strip().lower()
        if name in ("sol", "robinhood") and name not in clean:
            clean.append(name)
    out["chain_pause"] = clean
    out["note"] = str(raw.get("note") or "")[:240]
    return out


def load_risk(session: Session) -> dict[str, Any]:
    row = session.query(ScanState).filter(ScanState.key == RISK_KEY).first()
    if row is None or not row.value:
        return defaults()
    try:
        data = json.loads(row.value)
    except Exception:
        return defaults()
    return _normalize(data if isinstance(data, dict) else None)


def save_risk(session: Session, patch: dict[str, Any] | None) -> dict[str, Any]:
    """Merge patch into controls. Refuses to arm when kill_switch is on."""
    cur = load_risk(session)
    if isinstance(patch, dict):
        merged = {**cur, **patch}
        # Explicit list replace for chain_pause when provided.
        if "chain_pause" in patch:
            merged["chain_pause"] = patch.get("chain_pause")
        cur = _normalize(merged)
    if cur["kill_switch"]:
        cur["armed"] = False
    now = utcnow()
    row = session.query(ScanState).filter(ScanState.key == RISK_KEY).first()
    payload = json.dumps(cur)
    if row is None:
        session.add(ScanState(key=RISK_KEY, value=payload, updated_at=now))
    else:
        row.value = payload
        row.updated_at = now
    session.flush()
    log.info(
        "risk controls armed=%s kill=%s pause=%s notional=%.0f/name=%.0f",
        cur["armed"],
        cur["kill_switch"],
        cur["chain_pause"],
        cur["max_notional_usd"],
        cur["max_per_name_usd"],
    )
    return cur


def risk_status(session: Session) -> dict[str, Any]:
    """Operator-facing snapshot for /health and /api/risk."""
    ctrl = load_risk(session)
    return {
        "armed": bool(ctrl["armed"]),
        "kill_switch": bool(ctrl["kill_switch"]),
        "max_notional_usd": float(ctrl["max_notional_usd"]),
        "max_per_name_usd": float(ctrl["max_per_name_usd"]),
        "chain_pause": list(ctrl["chain_pause"]),
        "note": ctrl.get("note") or "",
        "live_allowed": bool(ctrl["armed"]) and not bool(ctrl["kill_switch"]),
        "paper_v1_open_allowed": not bool(ctrl["kill_switch"]),
        "paper_only": True,
    }


def paper_v1_blocked(session: Session, chain: str | None = None) -> str:
    """Empty string if paperV1 may open; otherwise a short reason."""
    from .chains import normalize_chain

    ctrl = load_risk(session)
    if ctrl["kill_switch"]:
        return "kill_switch"
    if chain is not None:
        name = normalize_chain(chain)
        if name in (ctrl.get("chain_pause") or []):
            return f"chain_pause:{name}"
    return ""


def live_blocked(session: Session, chain: str | None = None) -> str:
    """Empty string only when arm is on, kill is off, chain not paused."""
    reason = paper_v1_blocked(session, chain)
    if reason:
        return reason
    ctrl = load_risk(session)
    if not ctrl["armed"]:
        return "not_armed"
    return ""


def kill_paper_v1_queue(session: Session, *, reason: str = "kill_switch") -> dict[str, int]:
    """Skip all queued paperV1 rows for today-ish books. Operator emergency."""
    from .models import PaperFill
    from .scoring.paper_v1 import PAPER_V1_LINE, PAPER_V1_QUEUED, PAPER_V1_SKIPPED, v1_day, v1_skip_stamp

    now = utcnow()
    day = v1_day(now)
    rows = (
        session.query(PaperFill)
        .filter(PaperFill.line == PAPER_V1_LINE, PaperFill.status == PAPER_V1_QUEUED)
        .all()
    )
    n = 0
    for fill in rows:
        fill.status = PAPER_V1_SKIPPED
        fill.exit_reason = v1_skip_stamp(reason if reason.startswith("v1 ") else f"v1 {reason}", day)
        fill.updated_at = now
        n += 1
    if n:
        session.flush()
        log.warning("kill_paper_v1_queue skipped %s queued rows reason=%s", n, reason)
    return {"skipped": n}
