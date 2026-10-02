# Post-deploy verification — stack-v154 (VRAX commander pin on RH Hunt)

Dave deploys. Agent does **not** deploy. Paper-safe (`armed=false`).

## What v154 fixes (v153 live)

v153 `sync_fat_hunt_board_cards` prefetched only the **top ~48 rows by absolute mcap**, then kept **16** pins — on prod ~26 RH fat books at **10M–60M** crowded out Commander VRAX (~**3.4M**, `pool_address` null, **~4.8k holders**).

v154: **commander band 250k–10M** scanned separately; **FOMO-audit** mints and **holders ≥ 1500** are **guaranteed** prepends before scored fill + capped mega tier. Still runs inside `list_hunt_mints` → `GET /api/hunt`.

## Curl checklist

```bash
BASE=https://new-launch-finder-production.up.railway.app
VRAX=0x94641b97010608c3827fb058074889f19868ff33
RESI=resideK2Ejv9apu1op2VBv89poumxCDCs8nzBxQ5q1y

curl -sS "$BASE/health" | python3 -c "
import sys,json
h=json.load(sys.stdin)
assert h.get('image_rev')=='stack-v154'
assert h.get('armed') in (False, None, 0)
"

curl -sS "$BASE/api/tokens/$VRAX" | python3 -c "
import sys,json
d=json.load(sys.stdin)
print('historical', d.get('historical'), 'last', d.get('last_mcap'), 'holders', d.get('holder_count'))
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

# RESI: hist=false stable; mcap band tracks live Dex (Sep-30 evening ~520k–720k, not a fake 900k gate)
curl -sS "$BASE/api/tokens/$RESI" | python3 -c "
import sys,json
d=json.load(sys.stdin)
last=float(d.get('last_mcap') or 0)
print('historical', d.get('historical'), 'last', last)
assert d.get('historical') in (False, 0, None)
assert 500_000 < last < 750_000
"

python3 scripts/verify_desk.py --base "$BASE"
```

### RESI Dex reference (manual, not a deploy gate)

```bash
# Raydium liq-first pair — compare to desk last; band above is honest if Dex ~650k
curl -sS "https://api.dexscreener.com/latest/dex/tokens/resideK2Ejv9apu1op2VBv89poumxCDCs8nzBxQ5q1y" | python3 -c "
import sys,json
p=(json.load(sys.stdin).get('pairs') or [])
p=sorted(p, key=lambda x: float((x.get('liquidity') or {}).get('usd') or 0), reverse=True)
print('top_liq_mcap', (p[0].get('marketCap') or p[0].get('fdv')) if p else None)
"
```

Hard stops: no arm, no Hunt floor lowers, no FOMO→buys.
