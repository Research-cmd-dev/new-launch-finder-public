# Post-deploy — stack-v158 (FOMO-only copycat shadow labels)

Extends v157 gate-path copycat shadows: Sol tokens with **copycat spam in Research.risk_flags** (FOMO/desk) get `paper_v1_shadow` skipped even when there is **no gate Decision**. Stamp unchanged: `v1 veto|copycat|d:YYYY-MM-DD` (≤32).

```bash
BASE=https://new-launch-finder-production.up.railway.app
SI=DEW9dSN6QpWyNthphCpMmAbZP1Q4cEKR9xQXAri98WDP
DAY=$(date -u +%F)

curl -sS "$BASE/health" | python3 -c "import sys,json; assert json.load(sys.stdin).get('image_rev')=='stack-v158'"

# After paper-sync: SI-class FOMO copycat should appear in Learn shadow (gate or FOMO-only path).
curl -sS "$BASE/api/paper/v1/review?chain=sol&day=$DAY" | python3 -c "
import sys,json,os
si=os.environ.get('SI','$SI')
d=json.load(sys.stdin)
hit=[r for r in (d.get('shadow') or []) if r.get('mint')==si]
print('si_shadow', bool(hit), 'shadow_n', len(d.get('shadow') or []))
if hit:
    r=hit[0].get('skip_reason','')
    assert r.startswith('v1 veto|copycat|d:'), r
    assert len(r) <= 32
"
```

Paper-only: no `paper_v1` opens; `copycat spam` ∉ `PAPER_SHADOW_VETOES`.
