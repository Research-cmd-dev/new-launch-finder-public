# Post-deploy verification — stack-v151 (historical hydrate rank + dust gate)

Dave deploys. Agent does **not** deploy. Paper-safe only (`armed=false`).

## Preconditions

- `/health` → `image_rev` = `stack-v151`
- Worker logs should **not** flood `hunt tape unpark historical` for RH dust (~4.6k `last_mcap`)
- Canonical VRAX must appear in hydrate / unpark logs when Dex is live

## Command proof checklist

```bash
BASE=https://new-launch-finder-production.up.railway.app
VRAX=0x94641b97010608c3827fb058074889f19868ff33
RESI=resideK2Ejv9apu1op2VBv89poumxCDCs8nzBxQ5q1y

# VRAX (canonical — not copycat 0xf2bf06…): unparked + last_mcap + on RH Hunt
curl -sS "$BASE/api/tokens/$VRAX" | python3 -c "
import sys,json
d=json.load(sys.stdin)
o=d.get('outcome') or {}
print('historical', d.get('is_historical'), 'last_mcap', o.get('last_mcap'), 'pool', (d.get('pool_address') or '')[:18])
assert d.get('is_historical') in (False, 0, None)
assert float(o.get('last_mcap') or 0) > 500_000
"

curl -sS "$BASE/api/hunt?chain=robinhood" | python3 -c "
import sys,json
m='$VRAX'.lower()
items=json.load(sys.stdin).get('items') or []
hit=[i for i in items if (i.get('mint') or '').lower()==m]
print('vrax_on_hunt', bool(hit), 'n', len(items))
assert hit and float(hit[0].get('last_mcap') or 0) > 500_000
"

# RESI: liq-first ~850–950k; must stay unparked after tape (no poll re-park)
curl -sS "$BASE/api/tokens/$RESI" | python3 -c "
import sys,json
d=json.load(sys.stdin)
last=float((d.get('outcome') or {}).get('last_mcap') or 0)
print('last_mcap', last, 'historical', d.get('is_historical'))
assert 800_000 < last < 1_050_000
assert d.get('is_historical') in (False, 0, None)
"

python3 scripts/verify_desk.py --base "$BASE"

curl -sS "$BASE/health" | python3 -c "
import sys,json
h=json.load(sys.stdin)
tape=(h.get('loops') or {}).get('hunt_tape') or {}
print('image_rev', h.get('image_rev'), 'hunt_tape', tape)
assert h.get('image_rev') == 'stack-v151'
assert int(tape.get('age_s') or 9999) < 180
"
```

## Verify claims

| Claim | Evidence |
|-------|----------|
| **2. Minute tape** | VRAX `0x94641…` in `this_window` hydrate set; `last_mcap` from V4 pool Dex |
| **3. Live health** | VRAX on RH Hunt with non-zero last; RESI stays `historical=false` across polls |

Hard stops: `armed=false`, no Hunt floor lowers, no FOMO→buys, no paid-X.
