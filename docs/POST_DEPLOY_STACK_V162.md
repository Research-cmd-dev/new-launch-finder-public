# Post-deploy — stack-v162 (thesis gate repair + high-SI FOMO shadow)

Paper-only. Builds on v161 hold-conviction. Entry lines unchanged (Sol 0.14 / RH 0.30).

## A) Thesis coverage fix

**Root cause:** `_v1_entry_features` only filled Research thesis keys when entry was **exactly 0**. Opens with thin partial entry (`github_auth_n` 0.2) never picked up hydrated Research (`0.8+`), so production-gate thesis stayed ~1/15 even after v160 repair.

**Fix:** `merge_thesis_feature_dict` max-merges thesis keys; entry Decision patch uses the same max merge after `refresh_stored_thesis_evidence`.

## B) High-SI FOMO Learn shadow

FOMO trending / audit mints with **40×–80×** honest multiple get `paper_v1_shadow` skipped, stamp `v1 si-pr|d:YYYY-MM-DD`. **Copycat spam blocked** (use copycat shadow path). No Hunt floor / entry changes.

```bash
BASE=https://new-launch-finder-production.up.railway.app
DAY=$(date -u +%F)

curl -sS "$BASE/health" | python3 -c "
import sys,json
h=json.load(sys.stdin)
assert h.get('image_rev')=='stack-v162'
assert h.get('armed') in (False, None, 0, 'false')
"

curl -sS "$BASE/api/paper/v1/production-gate" | python3 -c "
import sys,json
d=json.load(sys.stdin)
bars={b['key']:b for b in (d.get('bars') or [])}
for k in ('thesis_coverage','sample','risk'):
    b=bars.get(k) or {}
    print(k, b.get('color'), b.get('value'), (b.get('detail') or '')[:80])
"

curl -sS "$BASE/api/paper/v1/review?chain=sol&day=$DAY" | python3 -c "
import sys,json
d=json.load(sys.stdin)
si=[r for r in (d.get('shadow') or []) if str(r.get('skip_reason','')).startswith('v1 si-pr')]
copy=[r for r in (d.get('shadow') or []) if 'copycat' in str(r.get('skip_reason',''))]
print('shadow_n', len(d.get('shadow') or []), 'si_pr', len(si), 'copycat', len(copy))
"
```

After deploy + paper-sync: thesis `tagged/n` should rise when stored GitHub/CTO evidence exists on opens; high-SI FOMO names appear under shadow with `v1 si-pr` (not `paper_v1` open).
