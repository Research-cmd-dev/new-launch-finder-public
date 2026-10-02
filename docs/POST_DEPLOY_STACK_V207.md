# stack-v207 — copycat FALSE-VETO Learn (www)

Paper-only. **Do not arm.** Copycat hard skip stays.

www mint: `GAwhcphCqCv5bKHmCiN4VDdNWfbXJL4npmkc8L3Q9S9H`
(world wide web). Labeled FALSE-VETO — copycat hard skip later ~20–24×
vs paper entry ~$204k / entry_p 0.2712. Evidence row #1.

## What shipped

- Hard veto still blocks paperV1 / gated / ticket paths (`copycat spam`).
- Existing `paper_v1_shadow` `v1 veto|copycat` / `v1 veto|cc-fomo` labels stay.
- Side keys on Decision (not FEATURE_NAMES): `gate_veto=copycat`,
  `would_have`, `fomo_rank`.
- Would-have is true only when **all** of: official FOMO rank/position ≤3,
  Hunt card present, holders ≥26, liq ≥$5k, `v1_thesis_ok` (github/dev/cto).
- miss-cohort / Learn: `copycat_veto`, `evidence_rows[0]=www`,
  `n_copycat_veto_winners`, winner samples tagged `gate_veto=copycat`.
- `COPYCAT_LEARN_ARMED = False`. Risk `armed` unchanged.

## Tests before deploy

```bash
python3 -m pytest -q --tb=short \
  tests/test_copycat_learn.py \
  tests/test_copycat_veto_shadow.py \
  tests/test_paper_miss_learn.py \
  tests/test_paper_v1.py::test_desk_shows_the_side_list \
  tests/test_runners.py::test_health_exposes_image_rev \
  tests/test_rh_board.py::test_test_desk_pages
```

## Dual-deploy verify (after both API + worker SUCCESS)

Do **not** tip-deploy from this note unless the operator asks. After a
deliberate dual-upload of this tree:

```bash
curl -sS https://new-launch-finder-production.up.railway.app/health | python3 -c \
  'import json,sys; h=json.load(sys.stdin); print(h.get("image_rev"), h.get("git_sha"), h.get("armed"))'
# expect: image_rev=stack-v207  armed=false (or omitted/false)

curl -sS 'https://new-launch-finder-production.up.railway.app/api/paper/v1/miss-cohort?chain=sol' | python3 -c '
import json,sys
c=json.load(sys.stdin)
ev=c.get("evidence_rows") or (c.get("copycat_veto") or {}).get("evidence_rows") or []
print("n_copycat_veto_winners", c.get("n_copycat_veto_winners"))
print("n_would_have_copycat", c.get("n_would_have_copycat"))
print("evidence0", (ev[0] if ev else None))
print("armed", (c.get("copycat_veto") or {}).get("armed"))
print("paper_miss copycat_suppressed", (c.get("paper_misses") or {}).get("n_copycat_suppressed"))
'
# evidence0.mint must start GAwhcphC
# evidence0.gate_veto == copycat
# copycat_veto.armed == false
```

Learn pane on `/ui`: "Copycat vetoed winners" and "Evidence #1 · www · FALSE-VETO · copycat".

Confirm a copycat name still has **no** `paper_v1` open row:

```bash
# after worker reconsider: shadow skipped, line paper_v1 count stays 0
```

FOMO rank comes from `GET /api/fomo-trending` official snapshot only — no wallet lookup.
