# Post-deploy verification — stack-v152 (VRAX on RH Hunt + RESI stable unparked)

Dave deploys. Agent does **not** deploy. Paper-safe only (`armed=false`).

## Preconditions

- `/health` → `image_rev` = `stack-v152`
- `hunt_tape` heartbeat fresh (`age_s` < 180)

## Command proof checklist

```bash
BASE=https://new-launch-finder-production.up.railway.app
VRAX=0x94641b97010608c3827fb058074889f19868ff33
RESI=resideK2Ejv9apu1op2VBv89poumxCDCs8nzBxQ5q1y

# VRAX: unparked + on RH Hunt board (canonical mint only)
curl -sS "$BASE/api/tokens/$VRAX" | python3 -c "
import sys,json
d=json.load(sys.stdin)
o=d.get('outcome') or {}
print('historical', d.get('is_historical'), 'last_mcap', o.get('last_mcap'))
assert d.get('is_historical') in (False, 0, None)
assert float(o.get('last_mcap') or 0) > 500_000
"

curl -sS "$BASE/api/hunt?chain=robinhood&limit=200" | python3 -c "
import sys,json
m='$VRAX'.lower()
items=json.load(sys.stdin).get('items') or []
hit=[i for i in items if (i.get('mint') or '').lower()==m]
print('vrax_on_hunt', bool(hit), 'n', len(items), 'last', hit[0].get('last_mcap') if hit else None)
assert hit and float(hit[0].get('last_mcap') or 0) > 500_000
"

# RESI: unparked + liq-first band; stable across polls (not historical while on Hunt)
curl -sS "$BASE/api/tokens/$RESI" | python3 -c "
import sys,json
d=json.load(sys.stdin)
last=float((d.get('outcome') or {}).get('last_mcap') or 0)
print('last_mcap', last, 'historical', d.get('is_historical'))
assert d.get('is_historical') in (False, 0, None)
assert 800_000 < last < 1_050_000
"

curl -sS "$BASE/api/hunt?chain=sol&limit=200" | python3 -c "
import sys,json
m='$RESI'
items=json.load(sys.stdin).get('items') or []
hit=[i for i in items if (i.get('mint') or '')==m]
print('resi_on_sol_hunt', bool(hit), 'historical_field', hit[0].get('is_historical') if hit else None)
assert hit
"

python3 scripts/verify_desk.py --base "$BASE"

curl -sS "$BASE/health" | python3 -c "
import sys,json
h=json.load(sys.stdin)
assert h.get('image_rev')=='stack-v152'
assert h.get('armed') in (False, None, 0)
tape=(h.get('loops') or {}).get('hunt_tape') or {}
assert int(tape.get('age_s') or 9999) < 180
"
```

## Verify claims

| Claim | Evidence |
|-------|----------|
| **2. Minute tape** | VRAX/RESI `last_mcap` from live Dex; fat pins keep VRAX on RH Hunt past 24h `launched_at` |
| **3. Live health** | VRAX on `/api/hunt?chain=robinhood`; RESI `historical=false` in ~850–950k band |

Hard stops: `armed=false`, no Hunt floor lowers, no FOMO→buys, no paid-X.
