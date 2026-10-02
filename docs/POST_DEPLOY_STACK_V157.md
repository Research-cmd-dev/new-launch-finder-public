# Post-deploy — stack-v157 (copycat shadow exit_reason ≤32)

Hotfix: `PaperFill.exit_reason` is `varchar(32)`. Copycat shadow stamp is now `v1 veto|copycat|d:YYYY-MM-DD`.

```bash
BASE=https://new-launch-finder-production.up.railway.app
SI=DEW9dSN6QpWyNthphCpMmAbZP1Q4cEKR9xQXAri98WDP
DAY=$(date -u +%F)

curl -sS "$BASE/health" | python3 -c "import sys,json; assert json.load(sys.stdin).get('image_rev')=='stack-v157'"

curl -sS "$BASE/api/paper/v1/review?chain=sol&day=$DAY" | python3 -c "
import sys,json
si='$SI'
d=json.load(sys.stdin)
hit=[r for r in (d.get('shadow') or []) if r.get('mint')==si]
print('si_shadow', bool(hit))
if hit:
    r=hit[0].get('skip_reason','')
    assert r.startswith('v1 veto|copycat|d:'), r
    assert len(r) <= 32, len(r)
"
```
