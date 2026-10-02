# Post-deploy — stack-v156 (copycat-veto shadow labels)

Paper-safe. Dave deploys; agent does not arm.

```bash
BASE=https://new-launch-finder-production.up.railway.app
SI=DEW9dSN6QpWyNthphCpMmAbZP1Q4cEKR9xQXAri98WDP
DAY=$(date -u +%F)

curl -sS "$BASE/health" | python3 -c "import sys,json; assert json.load(sys.stdin).get('image_rev')=='stack-v156'"

# After one paper-sync cycle, SI-class copycat gate should appear in Learn shadow (not paper_v1 open).
curl -sS "$BASE/api/paper/v1/review?chain=sol&day=$DAY" | python3 -c "
import sys,json,os
si=os.environ.get('SI','')
d=json.load(sys.stdin)
shadow=d.get('shadow') or []
hit=[r for r in shadow if (r.get('mint') or '')==si or r.get('mint')=='DEW9dSN6QpWyNthphCpMmAbZP1Q4cEKR9xQXAri98WDP']
print('shadow_n', len(shadow), 'si_hit', bool(hit))
if hit:
    assert hit[0].get('skip_reason','').startswith('v1 veto|copycat spam')
"
```

Hard copycat veto stays hard (`copycat spam` ∉ `PAPER_SHADOW_VETOES`). No new paper_v1 opens from this path.
