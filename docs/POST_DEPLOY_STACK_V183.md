# Post-deploy — stack-v183 (fomo_trending chair cadence)

## Knob

Worker `fomo_trending` chair wakes every `fomo_poll_seconds` (default 600s) and rewrites the loop heartbeat (skip path: `audit fresh`). Full Learn audit payload remains hourly (`AUDIT_EVERY_S=3600`). FOMO/Dex HTTP, coverage, audit, and `ingest_lock` waits are hard-capped; sync DB under the lock runs in `asyncio.to_thread` so Postgres flush cannot freeze the worker loop. Heartbeat always fires in `finally` on timeout/error.

## Verify

```bash
BASE=https://new-launch-finder-production.up.railway.app
curl -sS "$BASE/health" | python3 -c "
import json,sys
d=json.load(sys.stdin)
assert d['image_rev']=='stack-v183'
ft=(d.get('loops') or {}).get('fomo_trending') or {}
poll=float(d.get('fomo_poll_seconds') or 600)
age=float(ft.get('age_s') or 9999)
assert age < poll*2.5, (age, poll)
assert 'error OperationalError' not in str(ft.get('note') or '')
print('ok age_s', age, 'poll', poll, 'note', ft.get('note'))
"
curl -sS "$BASE/api/fomo-trending" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('snapshot_age_s', d.get('snapshot_age_s'), 'last_audit', (d.get('last_audit') or {}).get('at'))
"

```

Dual-deploy tarball: `artifacts/launchfinder-stack-v183.tar.gz`
