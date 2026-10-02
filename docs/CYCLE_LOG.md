# paperV1 self-improve cycle log

One entry per knob. Rules (full text in [SELF_IMPROVE_PLAN.md](SELF_IMPROVE_PLAN.md)):

- **One behaviour knob per cycle.** Invariant repairs ride along; they are listed, not counted.
- **Claim before you ship.** An entry with `verdict: OPEN` is the lock; one open cycle at a time.
- **KEEP only with evidence** from `scripts/gate_status.py`, day-delta and sanity-loop on ≥ 3 resolved book days.
- **Flat = revert.** No evidence is not “wait more”.
- Numbers come from Learn / Sanity / paperV1 / M3 surfaces only. No parallel scorecard.

Entry template:

```
## Cycle N — <knob in one line> — stack-vNNN
claimed: <date> by <who>          verdict: OPEN | KEEP | REVERT | HOLD
baseline: gates x/6 · sol labels/day · rh labels/day · hard-tag cov sol/rh · resolved closes sol/rh
knob diff: <one line from gate_status knobs, or the one behaviour change>
rides along (not knobs): <invariant repairs, each with its test>
predicted move: <metric → direction>
keep if / revert if: <written before deploy>
read 1 (<date>): ...   read 2: ...   read 3: ...
decision: <verdict + gate_status json path>
```

---

## Cycle 0 — baseline, no knob — stack-v129

claimed: 2026-09-29 (planning thread)          verdict: **HOLD** (observe one full lock cycle)

baseline (`gate_status.py --days 7`, 06:55 UTC): **gates 1/6** (risk controls only)

| | sol | robinhood |
|---|---|---|
| labels/day (queued+open+skipped+shadow) | **0** every day since paperV1 shipped (v110, 09-27) | 09-28: 162 · 09-29: 155 (61 queued, 5 open, 32 skipped, 32 shadow, 1 closed); 09-23…09-26 predate paperV1 |
| hard-tag coverage on opens | n/a (0 opens) | **0.00** on 5 opens (any-tag 1.00 — all `meme`) |
| resolved closes | 0 | 0 (1 unresolved early close, PUMPBLOX ride) |
| wide-hi baseline hit2× (observed) | 0.108 @ hi 0.14, n_obs 18,354 | 0.097 @ hi 0.30, n_obs 13,312 |
| cap breaches | — | 09-27 / 09-28 (5-cap regime) · **09-29: 5 > 3** (rows promoted before v124 landed 03:51 UTC) |
| review truncation | — | 09-27 (`taken_chain 5`, 0 rows returned) |
| sanity hard checks | pass / pass | pass / pass (soft warn: copycat-spam watch needle) |

knob snapshot: policy `signal` · weights 0.35/0.25/0.25/0.15 (default) · caps 3+3 · Sol Live 0.50/0.40 ·
RH Entry 0.40/0.30 · immediate 0.70 · peak 2.0× · thesis 0.25/0.70 · leftover $1M · refit n≥12 · flip ≥5 pp

stability notes: `git_sha null`; six IMAGE_REVs (v123→v129) shipped 01:59→06:20 UTC 2026-09-29 —
the 09-29 book straddles regimes and is **excluded** from any v129 baseline. CI default subset is
red on `main` (3 tests use `PaperFill.one()`; paper sync now also writes a `paper_v1` row) — test-only fix rides with v130.

decision: HOLD through the 2026-09-30 book. Evidence: `/opt/cursor/artifacts/gate_status_2026-09-29.json`.
(Superseded note: this entry originally named Sol reconsider as "Cycle 1 / v130". A parallel thread had
already claimed v130 — see Cycles 1–3 below — so Sol reconsider ships as **Cycle 4 / stack-v133** on `main` after v132.)

---

## Cycles 1–3 — sanity-apply one-knob runs (parallel thread, PR #21) — stack-v130

claimed: 2026-09-29 (self-improve-c3 thread)          verdict: **HOLD** ×3 (Δ 0 on every metric)

Recorded here from the PR #21 report so the log has one spine; numbers live in that PR thread.

- Cycle 3 knob: `enrich_thin_thesis_http` — website→GitHub HTTP enrich for thin confirmed runners via
  `enrich_thesis_before_entry`, then Decision thesis patch (`/api/sanity-loop?apply=1&actions=enrich_thin_thesis_http`).
  Live result: scanned 11, website fetches 8/8, skipped no-site 3, **enriched 0, patched 0** → HOLD.
- Cycles 1–2: single-action sanity applies from the same `actions=` filter set
  (`repair_thin_thesis`, `freeze_live_coverage`); both Δ 0 → HOLD.
- Shipped as `stack-v130` (dual deploy, `armed false`, paper only). Code landed on `main` at `f7788a8`
  together with the plan (PR #22).

Lesson carried forward: three HOLDs on thesis-repair knobs say the thin-thesis cohort is thin because the
sites have nothing to find, not because we failed to look. Stop spending cycles there until Sol produces rows.

---

## Cycle 4 — Sol reconsider-on-paper-sync (M1) + invariant repairs (M2) — stack-v133

claimed: 2026-09-29 14:55 UTC (cloud agent, Dave paper-only thread)          verdict: **OPEN**

baseline (`gate_status.py --days 7`, 07:13 UTC, live `stack-v130`): **gates 1/6** (risk controls only) —
`/opt/cursor/artifacts/gate_status_pre_v131.json` (pre-ship; live prod remained **stack-v132** until this PR deploys **stack-v133**).

| | sol | robinhood |
|---|---|---|
| labels/day | **0** every day since v110 (09-27) — no queued / open / skipped / shadow row ever | 09-29 so far: 65 queued · 5 open · 170 skipped · 53 shadow |
| hard-tag coverage on opens | n/a (0 opens) | **0.00** on 5 opens (any-tag 1.00, all `meme`) |
| resolved closes / hit2× | 0 / — | 0 / — (1 unresolved early close, hit2× 1.0 survivor-biased) |
| wide-hi baseline hit2× | 0.108 @ hi 0.14 (n_obs 18,359) | 0.097 @ hi 0.30 (n_obs 13,318) |
| cap breaches | — | 09-27, 09-28, 09-29 all `5 > 3` (v123 5-cap rows straddling v124) |
| review truncation | — | 09-27 (`taken_chain 5`, 0 rows in bounded scan) |
| `/api/model/calibration` | HTTP 500 both chains this run (baseline read from cached prior run) | |

knob diff (the ONE behaviour change): Sol wide gated fills that opened Live-cold now get re-evaluated for
paperV1 on every paper sync for `PAPER_V1_RECONSIDER_HOURS` after the fill; qualify path is the unchanged
`_consider_paper_v1` (Live ≥ 0.50 / thesis soft 0.40 + hard tag, RH untouched, Entry floors untouched),
priced at the **current** sellable print with the 1.8× chase refuse, leftover ≥ $1M reject, 3/chain, 23:00 lock.
Live is frozen at the reconsider moment (`freeze_live_at_entry(src="paper_reconsider")`). If the window ends
with no row, a shadow row is written with skip reason **`v1 live-cold`** so Sol silence becomes a label.

rides along (not knobs), each with a test:
- `paper_v1_review` scan bound by chain + book-day window (no `limit(240)` truncation) → RH 09-27 no longer blind.
- `promote_paper_v1_queue` per-day lease claim (second promoter in the same tick does nothing).
- `running_git_sha()` falls back to the `launchfinder/.git_sha` stamp; `/health` shows `deploy_stamp`.
- One thesis-coverage definition — **hard tag on opens** — in day-delta, `paper_v1_day_report` gate and desk label; any-tag kept as a labelled secondary.
- CI: the 3 `PaperFill.one()` tests filter `line == PAPER_LINE` (paper sync also writes a `paper_v1` row).
- Sanity `paper_this_window` detail relabelled as wide-book EV (it never read the short list).

predicted move (written before deploy):
- Sol labels/day: 0 → **≥ 1 row per Sol book day with any wide fill** (queued/open, or shadow `v1 live-cold` / `v1 late-chase` / `v1 leftover`).
- Sol opens: 0 → **0–3/day**; most reconsiders are expected to end as `v1 live-cold` shadow — that is a KEEP-eligible result because the gate needs *labels*, not opens.
- RH: **Δ 0** on every metric (RH is not in `PAPER_V1_RECONSIDER_CHAINS`). Any RH move is a regression signal.
- stability: cap-breach list stops growing (per-day promote lease); truncation list empty; `git_sha` non-null after the stamped deploy.
- hard-tag coverage on Sol opens: **≥ 0.80** by construction (soft path requires a hard tag). If it reads below, the coverage repair is wrong, not the knob.

keep if / revert if:
- **KEEP** if, over ≥ 3 resolved Sol book days, every day with ≥ 1 wide Sol fill has ≥ 1 paperV1 row (any label), RH shows Δ 0, and no Sol open lacks a frozen `live_at_entry`.
- **REVERT** (drop the worker hook, keep the M2 repairs) if Sol still shows a silent day that had wide fills, or if RH metrics move, or if any Sol open is priced > 1.8× t0.
- **HOLD** if fewer than 3 resolved Sol days exist by 2026-10-03 — do not extend the window, do not touch a second knob.

read 1 (post-deploy, same day): see "post-deploy" below.   read 2: 2026-10-01 book.   read 3: 2026-10-02 book.
decision: —

---

## Cycle 5 — paperV1 hard-tag sync from stored evidence (no HTTP) — stack-v134

claimed: 2026-09-29 15:35 UTC (cloud agent, Dave paper-only thread)          verdict: **OPEN**

baseline (Learn production-gate + desk, live **stack-v133** ~15:20 UTC PT morning, pre-v134 deploy):
**gates ~3/6** on Sol — `hit_quality` green, `ritual` green (`v1 live-cold` shadows firing), `risk` green;
`thesis_coverage` **red 0/15** hard tags on opens; `sample` amber **11/30** closed sellable;
`stability` amber on v133 (missing `paper_sync` heartbeat — fixed on `main` at `aab758e`, deploy in flight).

| | sol | robinhood |
|---|---|---|
| hard-tag coverage on opens | **0.00** (15 opens, score-path picks) | (unchanged — RH not target) |
| labels / reconsider | live-cold shadows working (Cycle 4) | Δ 0 expected |
| wide-hi baseline hit2× | unchanged | unchanged |

knob diff (the ONE behaviour change): before paperV1 qualify / shadow / reconsider and on each paper
sync, **sync entry Decision thesis keys from `Research.raw_json`** (`ensure_entry_thesis_from_stored`)
and batch-repair existing **paper_v1 opens** without hard-tag reads (`repair_paper_v1_open_thesis`).
No HTTP enrich, no floor changes, no Hunt / FOMO / arm / paid X.

rides along (not knobs): PR#26 stability loop honesty already on `main` (`aab758e`).

predicted move:
- Sol `thesis_coverage` on opens: **0/15 → >0** when raw_json already carries GitHub / dev / CTO signal
  (honest stamps only — meme-alone opens stay untagged).
- RH opens: **Δ 0** unless stored raw already had hard-tag evidence on an RH open.
- Qualify / Live / Entry floors: **Δ 0**.

keep if / revert if:
- **KEEP** if hard-tag coverage on Sol opens rises without lowering hit_quality / ritual, and stamped tags
  match `v1_thesis_from_features` thresholds (github ≥0.6 auth, dev real≥0.5, cto≥0.5).
- **REVERT** if any open gains a hard tag without stored raw/github/cto/dev evidence, or RH metrics move.
- **HOLD** if opens stay 0 hard tags because raw is genuinely empty (same lesson as Cycle 3 HTTP HOLD).

read 1: post-v134 deploy.   read 2: —   read 3: —
decision: —
