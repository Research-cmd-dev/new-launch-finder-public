# Post-deploy — stack-v167 (FOMO desk multiple for `v1 si-pr`)

Paper-only. No `paper_v1` opens, no arm, Hunt floors unchanged.

## Why v166 still showed `si_pr=0`

- Live-cold / meme-q **shadow rows** show `multiple≈1×` because review uses **shadow fill** entry vs last — not the FOMO printer read.
- v166 band check used **`Outcome` only**; many FOMO printers keep flat `Outcome` on shadow mints while **`/api/fomo-trending`** still shows honest multiples (e.g. PAID ~80×, MEME ~49× on_hunt).
- Stored hourly audit had **no `items` list** — `fomo_high_si_candidate_mints` never saw audit mints; Hunt SQL required `Outcome.multiple ≥ 40`.

## v167 fix

- `fomo_desk_metrics_for_chain()` — sync same desk merge as FOMO API (snapshot + `_desk_row`).
- `printer_label_multiple()` — max of Outcome peak, **FOMO desk `multiple`**, Hunt last/t0, frozen **entry** vs peak/mcap (no invented numbers).
- Audit payload persists **`items`** (top 40); candidates also from `top` + `vetoed`.
- RH mint case normalization on token load.

## Live proof (named printers)

```bash
BASE=https://new-launch-finder-production.up.railway.app
DAY=$(date -u +%F)

curl -sS "$BASE/health" | python3 -c "import sys,json; assert json.load(sys.stdin)['image_rev']=='stack-v167'"

# Board multiples (expect PAID ~80× sol, MEME ~49× rh)
curl -sS "$BASE/api/fomo-trending" | python3 -c "
import sys,json
for i in json.load(sys.stdin).get('items') or []:
    s=(i.get('symbol') or '').upper()
    if s in ('PAID','MEME'):
        print(s, i.get('chain'), 'mult', round(float(i.get('multiple') or 0),1), 'mint', i.get('mint'))
"

# After 1–2 paper-sync ticks (~45s each)
curl -sS "$BASE/api/paper/v1/review?chain=sol&day=$DAY" | python3 -c "
import sys,json
d=json.load(sys.stdin)
si=[r for r in (d.get('shadow') or []) if str(r.get('skip_reason','')).startswith('v1 si-pr')]
print('sol si_pr', len(si))
for r in si:
    if (r.get('symbol') or '').upper()=='PAID':
        print('PAID', r.get('skip_reason'), 'peak', r.get('peak_multiple'))
"

curl -sS "$BASE/api/paper/v1/review?chain=robinhood&day=$DAY" | python3 -c "
import sys,json
d=json.load(sys.stdin)
si=[r for r in (d.get('shadow') or []) if str(r.get('skip_reason','')).startswith('v1 si-pr')]
print('rh si_pr', len(si))
for r in si:
    if (r.get('symbol') or '').upper()=='MEME':
        print('MEME', r.get('skip_reason'))
"
```

**Pass:** `sol si_pr >= 1` with **PAID** stamped `v1 si-pr|d:…` (if still on board), or **RH `MEME`** si-pr when on_hunt. Copycat **SI** stays `v1 veto|copycat`, not si-pr.
