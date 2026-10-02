# stack-v188 — always fetch FOMO trending (no snap short-circuit)

## Fix

v187 could still return a **green** mirror when the local `fomo_trending_board` snapshot was ≤25m old, **without** calling keyed trending REST — so `capture_age_hours` stayed `None` while FOMO API capture was hours stale (BANDIT case).

**v188:** `_resolve_trending_board` **always** `fetch_trending`. Snapshot rows are used only on timeout / sit-out / error.

## Verify

```bash
pytest tests/test_fomo_trending_mirror.py -q
curl -sS "$BASE/api/fomo-trending" | python3 -c "
import json,sys
d=json.load(sys.stdin)
assert d.get('capture_age_hours') is not None or d.get('board_stale'), d
print('ok', 'stale', d.get('board_stale'), 'age_h', d.get('capture_age_hours'))
"
```

Dual-deploy: `artifacts/launchfinder-stack-v188.tar.gz`
