# Post-deploy — stack-v182 (fomo_trending txn resilience)

## What shipped

- FOMO trending coverage no longer holds a Postgres transaction open across FOMO/Dex HTTP.
- Audit persist + loop heartbeat run under `ingest_lock` with one retry on lock/txn-abort class errors.
- Worker `fomo_trending` loop beats with a short error/defer note instead of a bare `OperationalError`.

## Verify

```bash
BASE=https://new-launch-finder-production.up.railway.app
curl -sS "$BASE/health" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['image_rev'] == 'stack-v182'
ft = (d.get('loops') or {}).get('fomo_trending') or {}
note = str(ft.get('note') or '')
assert 'error OperationalError' not in note
assert (ft.get('age_s') or 9999) < 7200
print('ok', note[:80], 'age_s', ft.get('age_s'))
"
```

Dual-deploy tarball: `artifacts/launchfinder-stack-v182.tar.gz`
