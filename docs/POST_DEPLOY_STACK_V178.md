# Post-deploy — stack-v178 (paper_sync beat hotfix)

v177 required `loop:paper_sync` but promote/lock exceptions could skip `_beat`.

## Fix

- `_beat("paper_sync")` fires immediately after the first successful `sync_paper_ledger` nested commit.
- `promote_paper_v1_queue` / `lock_paper_v1` wrapped in try/except so later failures cannot starve the beat.

```bash
curl -sS "$BASE/health" | python3 -c "
import sys,json
d=json.load(sys.stdin)
assert d['image_rev']=='stack-v178'
ps=(d.get('loops') or {}).get('paper_sync')
assert ps and ps.get('age_s') is not None, ps
print('paper_sync ok', ps)
"
```
