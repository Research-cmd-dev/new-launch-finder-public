# Post-deploy — stack-v161 (hold conviction after paper buy)

Exit layer only — desk entry lines unchanged. Paper-only; armed=false.

```bash
BASE=https://new-launch-finder-production.up.railway.app
DAY=$(date -u +%F)

curl -sS "$BASE/health" | python3 -c "
import sys,json
h=json.load(sys.stdin)
assert h.get('image_rev')=='stack-v161'
assert h.get('armed') in (False, None, 0, 'false')
"

curl -sS "$BASE/api/paper/v1/review?chain=sol&day=$DAY" | python3 -c "
import sys,json
d=json.load(sys.stdin)
opens=[r for r in (d.get('picked') or []) if r.get('status')=='open']
print('open_n', len(opens))
if opens:
    hc=opens[0].get('hold_conviction') or {}
    print('hold_sample', hc.get('score'), hc.get('policy'))
shadow=d.get('hold_shadow') or []
print('hold_shadow_log', len(shadow))
if shadow:
    print('last_shadow', shadow[0].get('policy'), shadow[0].get('would_dump_base'), shadow[0].get('would_dump_adj'))
"
```

Open paper_v1 rows expose `hold_conviction` on review + token detail. Shadow log records would-dump base vs conviction-adjusted each mark cycle.
