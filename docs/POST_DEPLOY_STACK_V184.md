# Post-deploy — stack-v184 (fomo audit lock fail-fast)

## What changed

- Audit persist **no longer waits on `ingest_lock`** (avoids queueing behind hunt/tape).
- Postgres work uses **8s statement budget** (`apply_report_guards`) and **12s** write wall; worker audit wall **120s** (not 300s).
- Lock class → **one short retry**, then audit-key-only write; else `defer lock defer` note with chair continuing.
- Trending snapshot: **3s** ingest_lock try, skip if busy.
- RH hydrate gap query skipped on lock defer (coverage still completes).

## Verify

```bash
BASE=https://new-launch-finder-production.up.railway.app
curl -sS "$BASE/health" | python3 -c "
import json,sys
d=json.load(sys.stdin)
assert d['image_rev']=='stack-v184'
ft=(d.get('loops') or {}).get('fomo_trending') or {}
print(ft)
assert float(ft.get('age_s') or 9999) < 1500
"
curl -sS "$BASE/api/fomo-trending" | python3 -c "
import json,sys; d=json.load(sys.stdin)
print('last_audit', (d.get('last_audit') or {}).get('at'))
"
```

Dual-deploy: `artifacts/launchfinder-stack-v184.tar.gz`
