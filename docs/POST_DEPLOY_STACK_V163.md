# Post-deploy — stack-v163 (thesis gate hydrate-on-read)

Paper-only. Builds on v162 (high-SI shadow + hold-conviction). Entry lines unchanged.

## Root cause (v162 gate still 1/15)

`merge_thesis_feature_dict` only max-merged **stale** `research.features_json`. GitHub/dev/CTO evidence often lives on `Research`/`Token` columns or empty `raw_json` until `hydrate_thesis_raw_from_stored_meta` + `sync_thesis_from_raw` run (worker / `v1_hard_tag_ready`). Production-gate `_thesis_tagged` calls `_v1_thesis` → `_v1_entry_features` **without** that hydrate, so scalars stayed thin and hard tags never derived.

## Fix

`sync_research_thesis_features()`; `_v1_entry_features` hydrates+syncs before max-merge. `repair_thin_entry_thesis` uses the same path for partial entry blobs (not only `thesis_keys_thin`).

```bash
BASE=https://new-launch-finder-production.up.railway.app

curl -sS "$BASE/health" | python3 -c "
import sys,json
h=json.load(sys.stdin)
assert h.get('image_rev')=='stack-v163'
"

curl -sS "$BASE/api/paper/v1/production-gate" | python3 -c "
import sys,json
d=json.load(sys.stdin)
bars={b['key']:b for b in (d.get('bars') or [])}
th=bars.get('thesis_coverage') or {}
print('thesis', th.get('color'), th.get('detail'), th.get('extra'))
"
```

Expect thesis `tagged/n` to rise when column/raw evidence exists on short-list tokens (honest github/dev/cto only).
