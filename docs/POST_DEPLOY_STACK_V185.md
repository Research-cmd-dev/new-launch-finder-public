# Post-deploy — stack-v185 (fomo_alerts idle self-heal)

## Knob

`/ws/alerts` loop stamps heartbeat every **60s** while connected (even `recv=0`). Forces reconnect if no frame received for **150s** (under 600s stability budget).

## Verify

```bash
BASE=https://new-launch-finder-production.up.railway.app
curl -sS "$BASE/health" | python3 -c "
import json,sys
d=json.load(sys.stdin)
assert d['image_rev']=='stack-v185'
fa=(d.get('loops') or {}).get('fomo_alerts') or {}
age=float(fa.get('age_s') or 9999)
assert age < 600, (age, fa)
print('ok', fa)
"
```

Dual-deploy: `artifacts/launchfinder-stack-v185.tar.gz`
