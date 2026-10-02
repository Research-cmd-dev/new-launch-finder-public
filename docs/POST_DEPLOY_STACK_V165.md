# Post-deploy — stack-v165 (high-SI Learn + future-open thesis prime)

Paper-only. v164 thesis audit unchanged. Entry Sol 0.14 / RH 0.30.

## A) High-SI FOMO Learn (`v1 si-pr`)

- Copycat spam → copycat shadow only; **hijack/e/acc** may use SI shadow.
- Label band **40×–160×** (143× SI class eligible; hunt honest cap unchanged).
- FOMO audit buckets include `veto_hijack`, `on_desk`, Hunt high-mult desk.
- Surface: FOMO snapshot/audit + Hunt card or multiple ≥ **70×**.

```bash
BASE=https://new-launch-finder-production.up.railway.app
DAY=$(date -u +%F)
curl -sS "$BASE/health" | python3 -c "import sys,json; assert json.load(sys.stdin).get('image_rev')=='stack-v165'"

curl -sS "$BASE/api/paper/v1/review?chain=sol&day=$DAY" | python3 -c "
import sys,json
d=json.load(sys.stdin)
si=[r for r in (d.get('shadow') or []) if str(r.get('skip_reason','')).startswith('v1 si-pr')]
print('si_pr_shadow', len(si))
for r in si[:8]:
    print(r.get('symbol'), r.get('skip_reason','')[:32])
"
```

After paper-sync ticks: expect **>0** `v1 si-pr` on eligible FOMO/hijack printers (copycat still `v1 veto|copycat`).

## B) Future paper_v1 thesis (not historical meme fills)

- `prime_paper_v1_thesis_for_qualify` before qualify/lock.
- Worker: `enrich_paper_v1_qualify_candidates_http` on Sol wide reconsider window (GMGN + website caps).
- Production-gate thesis detail notes `legacy_meme_only_n` — **80% bar not relaxed**.

```bash
curl -sS "$BASE/api/paper/v1/production-gate" | python3 -c "
import sys,json
th=next(b for b in json.load(sys.stdin)['bars'] if b['key']=='thesis_coverage')
print(th['detail'])
print('extra', th.get('extra'))
"
```
