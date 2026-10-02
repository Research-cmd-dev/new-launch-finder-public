# Post-deploy — stack-v164 (thesis gate audit + one-shot repair)

Paper-only. v161 hold-conviction + v162 high-SI Learn unchanged.

## Root cause (v163 soak still 1/15)

Hydrate-on-read works, but **~14/15 short-list fills have no honest github/dev/cto evidence** in Token/Research/raw after sync — only `name_quality` / meme scalars. Worker GMGN enrich was capped (1/tick) and HTTP enrich skips tokens with no website/GitHub hint, so the gate denominator never gained hard tags.

## Operator proof + repair

```bash
BASE=https://new-launch-finder-production.up.railway.app

# Per-fill evidence (15 rows) — no HTTP
curl -sS "$BASE/api/paper/v1/thesis-gate-audit" | python3 -m json.tool

# One-shot free-source + HTTP repair (GMGN/pump/website→GitHub), then re-count
curl -sS "$BASE/api/paper/v1/thesis-gate-audit?repair=1" | python3 -m json.tool

curl -sS "$BASE/api/paper/v1/production-gate" | python3 -c "
import sys,json
d=json.load(sys.stdin)
th=next(b for b in d['bars'] if b['key']=='thesis_coverage')
print(th['detail'], th['extra'])
"
```

**Expected:** `by_class` mostly `meme_only_no_stored_hard_evidence` before repair; `tagged` may rise only where GMGN/HTTP discovers real GitHub or CTO (no invented tags).
