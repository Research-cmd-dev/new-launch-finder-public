# Post-deploy verification — stack-v153 (VRAX on RH Hunt + RESI hydrate)

Dave deploys. Agent does **not** deploy. Paper-safe (`armed=false`).

## What v153 fixes (vs v152 live)

- Live VRAX had `pool_address=null` → v152 fat pin never matched; now RH fat pin uses holder book (≥40) / last+liq without 64-char pool id.
- RH Hunt lottery only loaded 80 cards by conviction — VRAX ~1.07× lost; `sync_fat_hunt_board_cards` runs inside `list_hunt_mints` before the board query.
- RESI outside 18h window never entered historical hydrate set; fat Sol historical (last≥100k) now included.
- Hunt board sync matches mints case-insensitively.

## Curl checklist

```bash
BASE=https://new-launch-finder-production.up.railway.app
VRAX=0x94641b97010608c3827fb058074889f19868ff33
RESI=resideK2Ejv9apu1op2VBv89poumxCDCs8nzBxQ5q1y

curl -sS "$BASE/health" | python3 -c "
import sys,json
h=json.load(sys.stdin)
assert h.get('image_rev')=='stack-v153'
assert h.get('armed') in (False, None, 0)
"

curl -sS "$BASE/api/tokens/$VRAX" | python3 -c "
import sys,json
d=json.load(sys.stdin)
print('historical', d.get('historical'), 'last', d.get('last_mcap'))
assert d.get('historical') in (False, 0, None)
assert float(d.get('last_mcap') or 0) > 500_000
"

curl -sS "$BASE/api/hunt?chain=robinhood&limit=200" | python3 -c "
import sys,json
m='$VRAX'.lower()
items=json.load(sys.stdin).get('items') or []
hit=[i for i in items if (i.get('mint') or '').lower()==m]
print('vrax_on_hunt', bool(hit), 'n', len(items))
assert hit and float(hit[0].get('last_mcap') or 0) > 500_000
"

curl -sS "$BASE/api/tokens/$RESI" | python3 -c "
import sys,json
d=json.load(sys.stdin)
print('historical', d.get('historical'), 'last', d.get('last_mcap'))
assert d.get('historical') in (False, 0, None)
assert 800_000 < float(d.get('last_mcap') or 0) < 1_050_000
"

python3 scripts/verify_desk.py --base "$BASE"
```

Hard stops: no arm, no Hunt floor lowers, no FOMO→buys.
