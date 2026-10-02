# Post-deploy — stack-v155 (Commander VRAX on RH Hunt)

Paper-safe. Dave deploys; agent does not.

```bash
BASE=https://new-launch-finder-production.up.railway.app
VRAX=0x94641b97010608c3827fb058074889f19868ff33

curl -sS "$BASE/health" | python3 -c "import sys,json; h=json.load(sys.stdin); assert h.get('image_rev')=='stack-v155'"

curl -sS "$BASE/api/hunt?chain=robinhood&limit=200" | python3 -c "
import sys,json
m='$VRAX'.lower()
items=json.load(sys.stdin).get('items') or []
hit=[i for i in items if (i.get('mint') or '').lower()==m]
print('vrax_on_hunt', bool(hit), 'n', len(items))
assert hit and float(hit[0].get('last_mcap') or 0) > 500_000
"
```

v155 fix: RH commander guarantees use a **dedicated SQL** (`holders≥1500` OR FOMO-audit mint), **ordered by holders**, **no `LIMIT 240` lottery**.
