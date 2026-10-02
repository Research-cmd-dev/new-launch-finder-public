# Post-deploy — stack-v166 (si-pr shadow upgrade path)

Paper-only. No `paper_v1` opens, no arm, Hunt floors unchanged. Entry Sol 0.14 / RH 0.30.

## Root cause (v165 live: 0 `v1 si-pr`)

- Worker **did** call `reconsider_high_si_fomo_shadow` each paper sync.
- Hijack / surfaces were not the blocker on v165 (copycat-only block; FOMO mint-first).
- **Blocker:** `_write_high_si_fomo_v1_shadow` bailed when *any* `paper_v1_shadow` row existed (`_v1_has_row`). ~197 Sol shadows today are already `v1 live-cold` / `meme-q` / late-chase — SI path never inserted or upgraded.
- **Secondary:** band used stored `outcome.multiple` on dumps; v166 uses `outcome_peak_multiple` (max_mcap/t0).

## v166 fix

- Upgrade eligible non-copycat shadow rows to `v1 si-pr|d:…` (single shadow row).
- Only skip when shadow is already si-pr or copycat veto shadow.
- FOMO audit/snapshot mints merged first; **sol + robinhood** chains.

```bash
BASE=https://new-launch-finder-production.up.railway.app
DAY=$(date -u +%F)
curl -sS "$BASE/health" | python3 -c "import sys,json; assert json.load(sys.stdin).get('image_rev')=='stack-v166'"

curl -sS "$BASE/api/paper/v1/review?chain=sol&day=$DAY" | python3 -c "
import sys,json
d=json.load(sys.stdin)
shadow=d.get('shadow') or []
si=[r for r in shadow if str(r.get('skip_reason','')).startswith('v1 si-pr')]
mix={}
for r in shadow:
    k=str(r.get('skip_reason',''))[:20]
    mix[k]=mix.get(k,0)+1
print('si_pr_shadow', len(si))
print('mix_sample', sorted(mix.items(), key=lambda x:-x[1])[:6])
for r in si[:8]:
    print(r.get('symbol'), r.get('skip_reason','')[:40])
"
```

After deploy + one paper-sync / hunt_tape cycle: expect **si_pr_shadow > 0** on hijack/e/acc-class printers that already had `v1 live-cold`. Copycat stamps stay `v1 veto|copycat`. Thesis gate unchanged (`legacy_meme_only_n`).

```bash
curl -sS "$BASE/api/paper/v1/production-gate" | python3 -c "
import sys,json
th=next(b for b in json.load(sys.stdin)['bars'] if b['key']=='thesis_coverage')
print(th['detail'])
"
```
