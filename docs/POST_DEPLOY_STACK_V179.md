# Post-deploy — stack-v179 (honest SI labels + fomo_board stubs off Hunt)

## Changes

1. **SI Learn** — `v1 si-pr` rows keep honest `opened_at` (frozen entry / first decision). Today's Learn review also matches `exit_reason == v1 si-pr|d:<book_day>` so resurface does not rewrite timestamps.
2. **Hunt** — `source=fomo_board` with `t0_mcap=0` and `last_mcap=0` are not Hunt-eligible (hydrate/repair from v175 unchanged).

## Verify

```bash
curl -sS "$BASE/health" | python3 -c "import sys,json; assert json.load(sys.stdin)['image_rev']=='stack-v179'"
# loops.paper_sync still fresh (v178 beat hotfix retained)
curl -sS "$BASE/health" | python3 -c "
import sys,json
d=json.load(sys.stdin)
ps=(d.get('loops') or {}).get('paper_sync')
assert ps, 'missing paper_sync'
print('paper_sync age_s', ps.get('age_s'))
"
```

After deploy: AQUA-class `fomo_board` stubs should not appear on Hunt until hydrate sets a real print.
