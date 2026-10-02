# stack-v187 — FOMO trending mirror staleness (desk sanity gate)

## Problem

Keyed `GET /v2/leaderboard/tokens/trending` can return `source: captured` with `capturedAt` **hours** behind the FOMO app Tokens→Trending UI while `stale: false`. Query params do not force a live board.

## Knob

- **15m capture budget** (`FOMO_TRENDING_CAPTURE_BUDGET_S = 900`).
- When over budget: `board_stale=true`, `source=fomo_api_capture_stale`, **no snapshot refresh**, fail-loud in Learn.

## Surfaces

- `GET /api/fomo-trending` — `board_stale`, `capture_age_hours`, `captured_at`, `mirror_note`, optional `ws_alert_hot_mints` (not FOMO ranks).
- Sanity loop — hard fail `fomo_mirror`; `fomo_door` not scored while stale.
- Production gate — red **FOMO trending mirror** bar from last audit.
- Desk Learn — FOMO trend row **not green** when stale.
- `scripts/check_fomo_trending.py` — exit 1 when stale.

## Verify

```bash
pytest tests/test_fomo_trending_mirror.py tests/test_fomo_trending_resilience.py -q
curl -sS "$BASE/api/fomo-trending" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('source',d.get('source'),'stale',d.get('board_stale'),'age_h',d.get('capture_age_hours'))
"
```

Dual-deploy: `artifacts/launchfinder-stack-v187.tar.gz`

Also ships **stack-v186** trader-wallet on `fomo_alert_events` (same image rev line).
