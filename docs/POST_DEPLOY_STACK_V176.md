# Post-deploy — stack-v176 (honest hit bar / peak promote via)

Midday `promote_paper_v1_queue` peak path now stamps `open_via=peak`, re-bases
`entry_mcap` at the promote print, and production-gate hit quality colors on
**organic** hit2× (`rate_excl_peak`).

## Proof

```bash
BASE=https://new-launch-finder-production.up.railway.app
curl -sS "$BASE/health" | python3 -c "import sys,json; assert json.load(sys.stdin)['image_rev']=='stack-v176'"

curl -sS "$BASE/api/paper/v1/production-gate?chain=sol" | python3 -c "
import sys,json
b=next(x for x in json.load(sys.stdin)['bars'] if x['key']=='hit_quality')
e=b['extra']
assert 'rate_excl_peak' in e and 'n_peak_promoted' in e
print('rate_excl_peak', e.get('rate_excl_peak'), 'n_peak', e.get('n_peak_promoted'), 'color', b['color'])
"
```
