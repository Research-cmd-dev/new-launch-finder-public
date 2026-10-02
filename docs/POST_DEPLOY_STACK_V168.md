# Post-deploy — stack-v168 (si-pr resurface + FOMO-first loop)

## Root cause (v167 live `si_pr=0` with honest FOMO multiples)

1. **`paper_v1_review` SQL** only loads shadows with `opened_at` in **[D−1, D+2)**. PAID/MEME shadows were stamped weeks ago (`live-cold` / old book) → **never in today's review**, even if exit_reason changed.
2. v167 band logic could pass, but rows stayed **invisible** to the review API the parent curls.
3. SI-pr stamp used **`v1_day(first_seen)`** via historical `opened_at`, not today's learn day.

## v168 fix

- si-pr write/upgrade sets **`opened_at = now`** and **`v1 si-pr|d:<today>`**.
- **FOMO-board-first** loop (`iter_canonical_fomo_metrics`) before hunt tail.
- **`resolve_desk_token`** / **`ensure_fomo_desk_token`** (RH `0x` casing).
- **`GET /api/paper/v1/si-pr-probe?chain=&mint=`** — stage: `stamped` | `blocked_band` | `not_on_surface` | …

## Live proof (named printers)

```bash
BASE=https://new-launch-finder-production.up.railway.app
DAY=$(date -u +%F)
PAID=98kfF7rmsg1QDUEoCqNE7g7M1FdrTt92TEp2CLzypump
MEME=0x385f4f8ae47651ce5f58f5265395a669f8281e18

curl -sS "$BASE/health" | python3 -c "import sys,json; assert json.load(sys.stdin)['image_rev']=='stack-v168'"

curl -sS "$BASE/api/paper/v1/si-pr-probe?chain=sol&mint=$PAID"
curl -sS "$BASE/api/paper/v1/si-pr-probe?chain=robinhood&mint=$MEME"
# expect stage=stamped, fomo_multiple~80 / ~49 after one paper-sync

curl -sS "$BASE/api/paper/v1/review?chain=sol&day=$DAY" | python3 -c "
import sys,json
si=[r for r in json.load(sys.stdin).get('shadow',[]) if str(r.get('skip_reason','')).startswith('v1 si-pr')]
print('sol si_pr', len(si), [r.get('symbol') for r in si if r.get('symbol')=='PAID'])
"

curl -sS "$BASE/api/paper/v1/review?chain=robinhood&day=$DAY" | python3 -c "
import sys,json
si=[r for r in json.load(sys.stdin).get('shadow',[]) if str(r.get('skip_reason','')).startswith('v1 si-pr')]
print('rh si_pr', len(si), [r.get('symbol') for r in si if r.get('symbol')=='MEME'])
"
```

SI copycat (`DEW9…` ~143×) must remain **`v1 veto|copycat`**, not si-pr.
