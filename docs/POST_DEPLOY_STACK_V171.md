# Post-deploy — stack-v171 (SI Learn FOMO desk ≥20×, no upper cap)

v170 demotes off-board si-pr; v171 widens the honest FOMO-desk printer band.

## Behavior

- **Floor:** `HIGH_SI_MULTIPLE_MIN = 20` on FOMO board `multiple` only (unchanged FOMO-desk-only gate from v169).
- **No upper cap:** 160×+ and 1000×+ printers still get `v1 si-pr` when on-board and non-copycat.
- **Unchanged:** copycat → `v1 veto|copycat`; Hunt-only peaks do not stamp; stale demote (`v1 si-pr-stale`) from v170.

## Proof

```bash
BASE=https://new-launch-finder-production.up.railway.app
DAY=$(date -u +%F)

curl -sS "$BASE/health" | python3 -c "import sys,json; assert json.load(sys.stdin)['image_rev']=='stack-v171'"

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
