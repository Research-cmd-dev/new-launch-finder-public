# Post-deploy — stack-v160 (desk thesis coverage sync)

Paper-safe: hydrate stored GitHub/dev/CTO evidence onto Research + entry Decisions for paperV1 opens, recent Hunt, and shadow rows. No invented tags; meme not added to hard tags.

```bash
BASE=https://new-launch-finder-production.up.railway.app

curl -sS "$BASE/health" | python3 -c "
import sys,json
h=json.load(sys.stdin)
assert h.get('image_rev')=='stack-v160'
assert h.get('armed') in (False, None, 0, 'false')
print('armed', h.get('armed'), 'paper_only', h.get('paper_only'))
"

curl -sS "$BASE/api/paper/v1/production-gate" | python3 -c "
import sys,json
d=json.load(sys.stdin)
bars={b['key']:b for b in (d.get('bars') or [])}
th=bars.get('thesis_coverage') or {}
print('thesis', th.get('color'), th.get('value'), th.get('detail'))
"

# Local gate dashboard (same thesis definition as FORWARD)
python3 scripts/gate_status.py --base "$BASE" 2>/dev/null | head -40
```

After one paper-sync cycle, thesis bar should tick up when opens had `github_url` / raw_json evidence that was not yet on entry `features_json`.
