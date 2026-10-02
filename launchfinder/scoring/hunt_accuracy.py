"""Hunt last / Live / mcap vs stored snaps. API-only. No extra GMGN.

Used by the 40-minute paper-90 loop. Live RWA: last $958k / Live 95
while the latest snap was $64k and Pair was empty. FEATURE_NAMES 66.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..serialize import UNBACKED_LAST_RATIO, UNBACKED_SNAP_AGE_MIN, is_unbacked_last_card

# Same doors the loop already stares at. Fat last or a 90+ badge.
SCAN_LAST_USD = 150_000.0
SCAN_LIVE = 0.80
SCAN_ENTRY = 0.85
SCAN_MULTIPLE = 5.0


def latest_snap_from_detail(snaps: list[dict[str, Any]] | None) -> dict[str, Any]:
    rows = [row for row in (snaps or []) if float((row or {}).get("mcap_usd") or 0.0) > 0]
    if not rows:
        return {}
    rows = sorted(rows, key=lambda row: str(row.get("taken_at") or ""))
    last = rows[-1]
    taken = last.get("taken_at")
    age = 0.0
    if taken:
        try:
            ts = datetime.fromisoformat(str(taken).replace("Z", "+00:00"))
        except ValueError:
            ts = None
        if ts is not None:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = max(0.0, (datetime.now(timezone.utc) - ts).total_seconds() / 60.0)
    peak = max(float(row.get("mcap_usd") or 0.0) for row in rows)
    return {
        "snap_mcap": float(last.get("mcap_usd") or 0.0),
        "snap_liq": float(last.get("liquidity_usd") or 0.0),
        "snap_max_mcap": peak,
        "snap_age_min": age,
        "volume_h1": last.get("volume_h1"),
        "taken_at": last.get("taken_at"),
        "n_snaps": len(rows),
    }


def _live_model_from_card(card: dict[str, Any]) -> float:
    """Hunt Live-model p. Tape conviction is a leftover 90 badge — not this."""
    raw = card.get("live_model_p")
    if raw is None:
        return 0.0
    live = float(raw or 0.0)
    if live > 1.5:
        live = live / 100.0
    return live


def _should_scan(card: dict[str, Any]) -> bool:
    last = float(card.get("last_mcap") or 0.0)
    t0 = float(card.get("t0_mcap") or 0.0)
    live = _live_model_from_card(card)
    entry = float(card.get("entry_p") or card.get("p_good") or 0.0)
    multiple = float(card.get("multiple") or 0.0)
    if t0 > 0 and last > 0:
        multiple = max(multiple, last / t0)
    return (
        last >= SCAN_LAST_USD
        or live >= SCAN_LIVE
        or entry >= SCAN_ENTRY
        or multiple >= SCAN_MULTIPLE
    )


def hunt_accuracy_issues(card: dict[str, Any], snaps: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Compare one Hunt/detail card to its stored snaps."""
    if not _should_scan(card):
        return []
    tape = latest_snap_from_detail(snaps)
    last = float(card.get("last_mcap") or 0.0)
    t0 = float(card.get("t0_mcap") or 0.0)
    live = _live_model_from_card(card)
    entry = float(card.get("entry_p") or card.get("p_good") or 0.0)
    issues: list[dict[str, Any]] = []
    if not tape:
        if last >= SCAN_LAST_USD:
            issues.append(
                {
                    "kind": "last_without_snaps",
                    "last_mcap": last,
                    "note": "Fat last and no stored snaps",
                }
            )
        return issues
    probe = {
        "last_mcap": last,
        "snap_mcap": tape.get("snap_mcap"),
        "snap_age_min": tape.get("snap_age_min"),
        "stored_max_mcap": float(card.get("stored_max_mcap") or card.get("max_mcap") or 0.0),
    }
    snap = float(tape.get("snap_mcap") or 0.0)
    ratio = (last / snap) if snap > 0 else 0.0
    if is_unbacked_last_card(probe):
        issues.append(
            {
                "kind": "unbacked_last",
                "last_mcap": last,
                "snap_mcap": snap,
                "ratio": round(ratio, 2),
                "snap_age_min": round(float(tape.get("snap_age_min") or 0.0), 1),
                "note": f"Last is {ratio:.1f}× the latest snap (age {float(tape.get('snap_age_min') or 0):.0f}m)",
            }
        )
        if live >= 0.90:
            issues.append(
                {
                    "kind": "live_on_unbacked",
                    "live": live,
                    "entry": entry,
                    "note": f"Live {live:.0%} on an unbacked last",
                }
            )
    vol = tape.get("volume_h1")
    if vol is not None and float(vol or 0.0) <= 0 and live >= 0.90 and last >= SCAN_LAST_USD:
        issues.append(
            {
                "kind": "live_on_dust_vol",
                "live": live,
                "volume_h1": vol,
                "last_mcap": last,
                "note": "Live 90+ with a stored 1h volume of 0",
            }
        )
    if t0 > 0 and last > 0 and last / t0 >= SCAN_MULTIPLE and snap > 0:
        snap_x = snap / t0
        if last / t0 >= 3.0 * max(snap_x, 1.0) and ratio >= UNBACKED_LAST_RATIO:
            if not any(item["kind"] == "unbacked_last" for item in issues):
                issues.append(
                    {
                        "kind": "multiple_vs_snap",
                        "last_x": round(last / t0, 2),
                        "snap_x": round(snap_x, 2),
                        "note": f"Desk {last / t0:.1f}× vs snap-backed {snap_x:.1f}×",
                    }
                )
    return issues


def scan_hunt_accuracy(
    items: list[dict[str, Any]],
    details_by_mint: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Score a Hunt page against /api/tokens snaps. No extra HTTP beyond that."""
    findings: list[dict[str, Any]] = []
    scanned = 0
    for card in items:
        mint = str(card.get("mint") or "")
        if not mint or not _should_scan(card):
            continue
        scanned += 1
        detail = details_by_mint.get(mint) or {}
        issues = hunt_accuracy_issues(card, detail.get("snapshots"))
        if not issues:
            continue
        findings.append(
            {
                "symbol": card.get("symbol") or detail.get("symbol") or "",
                "mint": mint,
                "chain": card.get("chain") or detail.get("chain") or "",
                "entry": float(card.get("entry_p") or 0.0),
                "live": _live_model_from_card(card),
                "last": float(card.get("last_mcap") or 0.0),
                "t0": float(card.get("t0_mcap") or 0.0),
                "issues": issues,
            }
        )
    return {
        "scanned": scanned,
        "flagged": len(findings),
        "unbacked_age_min": UNBACKED_SNAP_AGE_MIN,
        "unbacked_ratio": UNBACKED_LAST_RATIO,
        "findings": findings,
    }
