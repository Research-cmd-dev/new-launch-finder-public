#!/usr/bin/env python3
"""API-only Hunt last/Live/mcap vs stored snaps. No extra GMGN.

Companion to scripts/verify_desk.py (see launchfinder-verify skill §4 / §7).
Last-vs-snap only — not a substitute for the four-claim scorecard.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from launchfinder.scoring.hunt_accuracy import _should_scan, scan_hunt_accuracy

DEFAULT_BASE = "https://new-launch-finder-production.up.railway.app"
STATE_PATH = Path("/tmp/paper-loop-state.json")


def _get(base: str, path: str) -> dict:
    url = f"{base.rstrip('/')}{path}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.load(resp)


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan Hunt last/Live vs stored snaps")
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--write-state", action="store_true")
    parser.add_argument("--state", default=str(STATE_PATH))
    args = parser.parse_args()
    items: list[dict] = []
    for chain in ("sol", "rh"):
        payload = _get(args.base, f"/api/hunt?chain={chain}")
        items.extend(payload.get("items") or [])
    details: dict[str, dict] = {}
    for card in items:
        mint = str(card.get("mint") or "")
        if not mint or not _should_scan(card):
            continue
        details[mint] = _get(args.base, f"/api/tokens/{mint}")
    report = scan_hunt_accuracy(items, details)
    print(json.dumps(report, indent=2))
    if args.write_state:
        path = Path(args.state)
        try:
            state = json.loads(path.read_text()) if path.exists() else {}
        except json.JSONDecodeError:
            state = {}
        if not isinstance(state, dict):
            state = {}
        state["hunt_accuracy"] = report
        path.write_text(json.dumps(state, indent=2) + "\n")
    return 1 if report.get("flagged") else 0


if __name__ == "__main__":
    raise SystemExit(main())
