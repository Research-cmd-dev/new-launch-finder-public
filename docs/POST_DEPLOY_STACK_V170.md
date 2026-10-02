# Post-deploy — stack-v170 (scrub v168 si-pr flood)

v169 stopped new Hunt stamps; v170 **demotes** existing off-board si-pr rows.

## Behavior

On each `reconsider_high_si_fomo_shadow`:

1. **`demoted_stale`**: shadow rows with active `v1 si-pr|…` but **not** `fomo_desk_si_pr_eligible` → `v1 si-pr-stale|d:<prior book>` and `opened_at` pushed **5d back** (row kept).
2. Review **omits** `v1 si-pr-stale` from the shadow list.
3. On-board FOMO ≥40× non-copycat still **stamp/resurface** `v1 si-pr`.

## Proof

```bash
BASE=https://new-launch-finder-production.up.railway.app
DAY=$(date -u +%F)

curl -sS "$BASE/health" | python3 -c "import sys,json; assert json.load(sys.stdin)['image_rev']=='stack-v170'"

curl -sS "$BASE/api/fomo-trending" | python3 -c "
import sys,json
hi=[i for i in json.load(sys.stdin).get('items',[]) if 40<=float(i.get('multiple') or 0)<=160 and 'copycat' not in str(i.get('bucket',''))]
print('fomo_eligible_n', len(hi))
"

# After one paper-sync: si_pr ≈ fomo_eligible_n (not 80+)
for CH in sol robinhood; do
  curl -sS "$BASE/api/paper/v1/review?chain=$CH&day=$DAY" | python3 -c "
import sys,json
d=json.load(sys.stdin)
si=[r for r in d.get('shadow',[]) if str(r.get('skip_reason','')).startswith('v1 si-pr') and not str(r.get('skip_reason','')).startswith('v1 si-pr-stale')]
print('$CH si_pr', len(si))
"
done

# On-board printer
curl -sS "$BASE/api/paper/v1/si-pr-probe?chain=sol&mint=<fomo_board_mint>"
```

Worker log: `paperV1 high-SI fomo stale demote sol: n=…` on first sync after deploy.
