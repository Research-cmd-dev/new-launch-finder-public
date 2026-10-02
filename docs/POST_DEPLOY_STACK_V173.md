# Post-deploy — stack-v173 (SI Learn FOMO desk ≥20×, no upper cap)

v172’s 40× floor missed live printers (~30× Saw, ~25× ARTHUR); v173 restores **≥20×**.
Also fixes **AQUA-class** Sol `fomo_board` stubs parked historical with $0 last before Dex hydrate.

## Behavior

- **Floor:** `HIGH_SI_MULTIPLE_MIN = 20` on FOMO board `multiple` only.
- **No upper cap:** 1000×+ still eligible.
- **FOMO stubs:** `_reclassify_old_majors` skips unknown-age park for `source=fomo_board` while `last_mcap==0`; Sol historical hydrate queue includes those zeros for a Dex tape attempt.
- **Unchanged:** FOMO-desk-only, copycat block, v170 stale demote.

## Proof

```bash
BASE=https://new-launch-finder-production.up.railway.app
DAY=$(date -u +%F)

curl -sS "$BASE/health" | python3 -c "import sys,json; assert json.load(sys.stdin)['image_rev']=='stack-v173'"

curl -sS "$BASE/api/fomo-trending" | python3 -c "
import sys,json
hi=[i for i in json.load(sys.stdin).get('items',[]) if float(i.get('multiple') or 0)>=20 and 'copycat' not in str(i.get('bucket',''))]
print('fomo_eligible_n', len(hi))
"

for CH in sol robinhood; do
  curl -sS "$BASE/api/paper/v1/review?chain=$CH&day=$DAY" | python3 -c "
import sys,json
d=json.load(sys.stdin)
si=[r for r in d.get('shadow',[]) if str(r.get('skip_reason','')).startswith('v1 si-pr') and not str(r.get('skip_reason','')).startswith('v1 si-pr-stale')]
print('$CH si_pr', len(si))
"
done

curl -sS "$BASE/api/paper/v1/si-pr-probe?chain=sol&mint=<fomo_board_mint>"
```
