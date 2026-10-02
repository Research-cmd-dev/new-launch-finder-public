# Post-deploy — stack-v169 (FOMO-desk-only `v1 si-pr`)

Paper-only. v168 flooded Learn with Hunt-history 40–160× names; v169 stamps only **current FOMO desk** printers.

## Gate

- Row from `fomo_desk_metrics_for_chain` (snapshot + audit merge).
- `status` caught/leftover **or** audit bucket in `HIGH_SI_FOMO_AUDIT_BUCKETS`.
- Honest FOMO desk `multiple` in **40–160×** (not Hunt peak/t0 alone).
- Copycat still blocked (SI → `v1 veto|copycat`).
- Hunt tail loop removed; stale si-pr **not** resurfaced when off board.

## Live proof

```bash
BASE=https://new-launch-finder-production.up.railway.app
DAY=$(date -u +%F)

curl -sS "$BASE/health" | python3 -c "import sys,json; assert json.load(sys.stdin)['image_rev']=='stack-v169'"

# Board printers ≥40× (expect small n)
curl -sS "$BASE/api/fomo-trending" | python3 -c "
import sys,json
hi=[i for i in json.load(sys.stdin).get('items',[]) if 40<=float(i.get('multiple') or 0)<=160]
print('fomo_40_160', len(hi), [(i.get('symbol'), round(float(i.get('multiple') or 0),1)) for i in hi])
"

curl -sS "$BASE/api/paper/v1/review?chain=sol&day=$DAY" | python3 -c "
import sys,json
si=[r for r in json.load(sys.stdin).get('shadow',[]) if str(r.get('skip_reason','')).startswith('v1 si-pr')]
print('sol si_pr', len(si))
"

# Named board printer (e/acc / PARASITE class while on board)
curl -sS "$BASE/api/paper/v1/si-pr-probe?chain=sol&mint=<mint_from_fomo_board>"
```

Expect **si_pr count ~ order of FOMO ≥40 non-copycat** (single digits), not 80+. PAID may age off when rotated off board; probe reports `not_fomo_desk_band` or `stale_si_pr_no_resurface`.
