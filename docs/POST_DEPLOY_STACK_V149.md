# Post-deploy verification — stack-v149 (VRAX / RESI hydrate)

Dave deploys; agent does **not** deploy. Paper-safe checks only.

## Preconditions

- `/health` → `image_rev` = `stack-v149`
- Worker log: `boot repairs done`, `worker ingest loop up`, `hunt tape cycle N wrote …`

## Command proof plan

```bash
BASE=https://new-launch-finder-production.up.railway.app
VRAX=0x94641b97010608c3827fb058074889f19868ff33
RESI=resideK2Ejv9apu1op2VBv89poumxCDCs8nzBxQ5q1y

# 1) VRAX token last_mcap > 0 (historical unpark + pool path)
curl -sS "$BASE/api/tokens/$VRAX" | python3 -c "
import sys,json
d=json.load(sys.stdin)
o=d.get('outcome') or {}
print('image_rev', d.get('image_rev'))
print('historical', d.get('is_historical'))
print('last_mcap', o.get('last_mcap'))
assert float(o.get('last_mcap') or 0) > 500_000
"

# 2) VRAX on RH Hunt board (last_mcap>0 filter)
curl -sS "$BASE/api/hunt?chain=robinhood" | python3 -c "
import sys,json
d=json.load(sys.stdin)
m='$VRAX'.lower()
hits=[i for i in d.get('items',[]) if (i.get('mint') or '').lower()==m]
print('hunt_n', len(d.get('items',[])), 'vrax_on_board', bool(hits))
if hits:
  print('board_last_mcap', hits[0].get('last_mcap'))
assert hits and float(hits[0].get('last_mcap') or 0) > 500_000
"

# 3) RESI liq-first last (~850k–950k, not ~1.72M wash)
curl -sS "$BASE/api/tokens/$RESI" | python3 -c "
import sys,json
d=json.load(sys.stdin)
o=d.get('outcome') or {}
last=float(o.get('last_mcap') or 0)
print('last_mcap', last)
assert 800_000 < last < 1_050_000
"

# 4) Desk scorecard (verify skill)
python3 scripts/verify_desk.py --base "$BASE"

# 5) hunt_tape heartbeat fresh (age_s well under LOOP_STALE_S)
curl -sS "$BASE/health" | python3 -c "
import sys,json
h=json.load(sys.stdin)
tape=h.get('loops',{}).get('hunt_tape') or {}
print('hunt_tape', tape)
assert int(tape.get('age_s') or 9999) < 180
"
```

## Claims touched

| Claim | What this proves |
|-------|------------------|
| **2. Minute tape** | VRAX/RESI `last_mcap` from live Dex; hunt_tape heartbeat fresh |
| **3. Live health** | Hunt board shows VRAX with non-zero last (Live can compute) |

Hard stops unchanged: `armed=false`, no Hunt floor lowers, no FOMO→buys, no paid-X.
