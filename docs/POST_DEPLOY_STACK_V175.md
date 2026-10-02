# Post-deploy — stack-v175 (FOMO desk stubs get Outcome rows)

v174 hydrate priority could not see AQUA-class rows: `ensure_fomo_desk_token` never
created `Outcome`, and hydrate SQL inner-joins `outcomes`.

## Behavior

- New `fomo_board` tokens get a zeroed `Outcome` at create time.
- `repair_fomo_board_missing_outcomes` backfills existing stubs (si-pr sync + Sol hydrate).
- v174 priority prepend + v173 SI ≥20× unchanged.

## Proof

```bash
BASE=https://new-launch-finder-production.up.railway.app
curl -sS "$BASE/health" | python3 -c "import sys,json; assert json.load(sys.stdin)['image_rev']=='stack-v175'"
```

After hunt_tape cycles, AQUA `AQVcP67EpMyu4cBZZjqMu91cVsWy1aX98JmcZm1FyY9` should join hydrate and pick up Dex `last_mcap`.
