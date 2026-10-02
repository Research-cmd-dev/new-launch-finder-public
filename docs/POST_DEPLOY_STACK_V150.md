# Post-deploy verification — stack-v150 (historical hydrate in tape set)

Dave deploys. Agent does **not** deploy. Paper-safe only.

## Preconditions

- `/health` → `image_rev` = `stack-v150`
- Worker log includes `hunt tape unpark historical …` for VRAX/RESI when hydrate succeeds

## Command proof checklist

```bash
BASE=https://new-launch-finder-production.up.railway.app
VRAX=0x94641b97010608c3827fb058074889f19868ff33
RESI=resideK2Ejv9apu1op2VBv89poumxCDCs8nzBxQ5q1y

# VRAX: unparked + last_mcap + on RH Hunt board
curl -sS "$BASE/api/tokens/$VRAX" | python3 -c "
import sys,json
d=json.load(sys.stdin)
o=d.get('outcome') or {}
print('historical', d.get('is_historical'), 'last_mcap', o.get('last_mcap'))
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

# RESI: liq-first ~850–950k (not ~1.72M wash)
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
tape=(json.load(sys.stdin).get('loops') or {}).get('hunt_tape') or {}
print('hunt_tape', tape)
assert int(tape.get('age_s') or 9999) < 180
"
```

## Verify claims

| Claim | Evidence |
|-------|----------|
| **2. Minute tape** | VRAX/RESI `last_mcap` from live Dex; `hunt_tape` heartbeat fresh |
| **3. Live health** | VRAX on Hunt with non-zero last |

Hard stops: `armed=false`, no Hunt floor lowers, no FOMO→buys, no paid-X.
