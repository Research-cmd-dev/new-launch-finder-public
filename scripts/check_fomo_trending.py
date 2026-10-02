#!/usr/bin/env python3
"""Compare live FOMO trending to the desk. Sol + RH only.

Exit 1 when a this-window fat book is on the board and not on the desk.
Leftover / thin / no-Dex rows are reported, not a fail.
"""

from __future__ import annotations

import json
import sys
import urllib.request

DEFAULT_BASE = "https://new-launch-finder-production.up.railway.app"


def main() -> int:
    base = DEFAULT_BASE if len(sys.argv) < 2 else sys.argv[1]
    with urllib.request.urlopen(f"{base.rstrip('/')}/api/fomo-trending", timeout=60) as resp:
        card = json.load(resp)
    counts = card.get("counts") or {}
    stale = card.get("board_stale")
    age_h = card.get("capture_age_hours")
    print(
        f"fomo-trending source={card.get('source')} stale={stale} capture_age_h={age_h} "
        f"board={counts.get('board')} caught={counts.get('caught')} miss={counts.get('miss')} "
        f"leftover={counts.get('leftover')} thin={counts.get('thin')} no_dex={counts.get('no_dex')}"
    )
    if stale:
        print("FAIL: FOMO API trending mirror is stale vs app — board is not trustworthy.")
        print(card.get("mirror_note") or "")
        return 1
    for row in card.get("items") or []:
        print(
            f"  {row.get('status'):8} {row.get('chain'):10} "
            f"{(row.get('symbol') or '?')[:12]:12} {row.get('mint')} "
            f"desk={row.get('on_desk')} hunt={row.get('on_hunt')}"
        )
    misses = card.get("misses") or []
    if misses:
        print("MISS this-window fat FOMO books:")
        for row in misses:
            print(f"  {row.get('chain')} {row.get('symbol')} {row.get('mint')}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
