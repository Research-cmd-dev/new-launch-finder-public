# Post-deploy — stack-v174 (FOMO zero-last hydrate scan priority)

v173 queued `fomo_board` zero-last rows in SQL but Sol still ordered by `last_mcap`
before the 160-row scan cap — fat historicals starved AQUA-class stubs.

## Behavior

- Sol `historical_hydrate_tape_mints`: load **all** in-window `fomo_board` / FOMO-audit
  zero-last priority rows outside the fat `last_mcap` scan; prepend to tape cap.
- `_hunt_eligible_historical_hydrate` (Sol): `last>0` **or** `fomo_board` zero-last.
- SI Learn floor remains **≥20×**, no upper cap (stack-v173).

## Proof

```bash
BASE=https://new-launch-finder-production.up.railway.app
curl -sS "$BASE/health" | python3 -c "import sys,json; assert json.load(sys.stdin)['image_rev']=='stack-v174'"

# AQUA-class after hunt_tape cycles: historical may clear once Dex prints
curl -sS "$BASE/api/paper/v1/si-pr-probe?chain=sol&mint=AQVcP67EpMyu4cBZZjqMu91cVsWy1aX98JmcZm1FyY9"
```
