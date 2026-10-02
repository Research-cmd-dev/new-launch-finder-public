# Post-deploy — stack-v180 (hunt_tape txn resilience + boot delete batches)

## Fix

- Per-mint hunt tape: single savepoint per mint (`upsert_hunt` skips inner nested when tape already nested); closed-txn / lock → log+defer, cycle continues.
- `rebuild_hunt_window` drops stale hunt cards via `_delete_hunt_cards_resilient` (small nested batches, per-row retry).
- Worker hunt_tape loop always beats `hunt_tape` and still runs `_sync_paper_ledger` when refresh throws.

## Verify

```bash
curl -sS "$BASE/health" | python3 -c "
import sys,json
d=json.load(sys.stdin)
assert d['image_rev']=='stack-v180'
for k in ('hunt_tape','paper_sync'):
    row=(d.get('loops') or {}).get(k)
    assert row and row.get('age_s') is not None, k
print('loops ok', {k:(d.get('loops') or {}).get(k,{}).get('age_s') for k in ('hunt_tape','paper_sync')})
"
```
