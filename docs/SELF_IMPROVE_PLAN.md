# Self-improve loop — plan after stack-v129 / PR #7

Written 2026-09-29 (live `stack-v129`, paper only, `armed: false`, `x_api: false`).
Companion: [FORWARD.md](FORWARD.md) (gate table), [OFFLINE_SPRINT.md](OFFLINE_SPRINT.md)
(data work), [CYCLE_LOG.md](CYCLE_LOG.md) (one entry per knob), `scripts/gate_status.py`
(the single gate read).

Mission is locked: research desk, paperV1 3+3 action surface, hard-tag thesis,
no auto-buy. Only tunables move, **one per cycle**, and a knob is KEPT only when
the gate read moved in the predicted direction on resolved book days.

---

## 0. Where we actually are (verified 2026-09-29 06:40–06:55 UTC)

Everything below was pulled from the live API or read from tip code, not from memory.

| Fact | Evidence | So what |
|---|---|---|
| `main` == tip `03bba7a` (v129). PR #7 still shows *open* on the forge but the ref already fast-forwarded. | `git ls-remote`, `origin pr view 7` | Plan on `main` = product. Do not touch the merge. |
| Live `/health`: `image_rev stack-v129`, `armed false`, `paper_only true`, `x_api false`, **`git_sha null`**, all loops fresh. | `/health` | Stamp is `image_rev` only; no commit-level drift check. |
| **Sol paperV1 is silent, not skipping**: 0 queued / skipped / shadow rows on every book day since paperV1 shipped (v110, 09-27 evening) through 09-29. Meanwhile Sol Hunt has 76/80 cards at Entry ≥ 0.14, **7 with Live ≥ 0.50**, 16 in the soft band; the wide gated Sol book opened 39 / closed 50 today. | `/api/paper/v1/review?chain=sol`, `/api/hunt?chain=sol`, `/api/paper?gated=true&chain=sol` | Inventory exists. The short list never sees it. |
| Root cause (code): `_consider_paper_v1` runs **once**, when the wide gated fill opens at the first liquid print — the moment Sol Live is usually unknown. If neither `v1_qualifies` nor `v1_near_qualify` fires, **no row is written**. | `ledger.py` `sync_paper_ledger` → `_consider_paper_v1` / `_consider_paper_v1_shadow` | Silence is a capture bug at the qualify *moment*, not a floor problem. Fix the moment, not the floor. |
| RH 09-29 book: **5 opens vs cap 3**, all `E 0.42 · L — · thesis 0.105 [meme]` via score path; queue of 61 is a wall of 0.4202 clones (BAGSPAY×2, CASHCAT×2); one hard-tag name queued (CAVO, github 0.33). | `/api/paper/v1/review?chain=robinhood` | RH lane is alive but thin and clone-heavy; Live is never present at RH qualify. |
| The 5 > 3 is a **regime artifact**, not a race: rows created 23:08–23:50 on 09-28 (v123), promoted 00:00–03:51 on 09-29 under the shared 5-cap; v124 (3+3) landed 03:51. Six IMAGE_REVs (v123→v129) shipped 01:59→06:20 UTC today. | `git log --date=iso`, review `opened_at` | The stability gate has never had one full lock cycle. |
| **Three different “thesis coverage” numbers**: report 38% (any tag incl. meme, all buckets), day-delta 1.5% (score ≥ 0.25 ∧ tags, picked+queued), gate-honest **hard tag on opens 0%** (RH) / n/a (Sol). | `/api/paper/v1/report`, `/api/paper/v1/day-delta`, `gate_status.py` | Only the last one is the gate. The 38% must not be cited. |
| **0 resolved closes** on both chains. RH rolling `closed 11 · hit2× 0.818 · +209%` is early-close survivorship (rides / live dumps close first; losers ride the 24h clock). | review `metrics` vs `gate_status` resolved-day split | Not gate evidence. |
| Wide-hi baseline (filled, observed): **Sol 0.108** @ hi 0.14 (n_obs 18,354), **RH 0.097** @ hi 0.30 (n_obs 13,312). | `/api/model/calibration` | Short-list hit2× must beat ≈0.10–0.11 on resolved n ≥ 30. |
| `paper_v1_review` scans `limit(240)` rows across **both chains before** filtering chain/day. RH writes ~130 rows/day, so 09-27 returns `taken_chain 5` and **zero rows**. | `ledger.py` `paper_v1_review` | Day-delta “yesterday” is blind on busy days. Ritual reads a hole. |
| Refit: weights still default, `MIN_CLOSED = 12`, policy `signal`; A/B auto-flip at ≥5 pp scores *top-half* of a closed sample the incumbent policy selected. | `thesis_weights.py` | A second, silent knob-mover with selection bias. Must be governed. |
| Miss-cohort: hard-tag rate among winners 4% (Sol) / 1.3% (RH); `signal_score` mean winners < losers on both chains (0.632 vs 0.645; 0.414 vs 0.432). | `/api/sanity-loop` | Default `signal` ranking has no demonstrated lift. Thesis path is structurally rare. |
| Sanity loop: hard checks green both chains; improve actions Sol `repair_thin_thesis` (9) + `freeze_live_coverage` (22 `live_unknown`); RH `research_watch_needles` (copycat spam). The `paper_this_window` soft check reads the **wide** book (n=913 / 166). | `/api/sanity-loop` | Green light to ship a knob; but the soft check is not a short-list read. |
| Paid X: `X_PAID_ENABLED` off, bearer path gated, budget 2000/mo. | `research/twitter.py`, `/health.x_api` | No spend. Keep it that way until hard-tag coverage is the proven bottleneck. |
| **CI default subset is red on `main` at v129**: `test_desk_lines.py::test_{sol,rh}_paper_opens_watch_only_when_live_is_healthy` and `test_ledger.py::test_paper_closes_a_run_on_live_dump` call `session.query(PaperFill).one()`; paper sync now writes `gated90 open` **and** `paper_v1 queued` for the same name. | `pytest` on a clean `main` worktree | Test-only fix (filter `line == "gated90"`). Until then “pytest green” claims are not true for the CI subset. |

Gate read today (`scripts/gate_status.py --days 7`): **1/6 pass** (risk controls). Thesis FAIL,
sample FAIL, hit WARN (no sample), stability FAIL (cap breach, review truncation, `git_sha`
null, rev age unknown), ritual FAIL (Sol silent every day; the 7-day window also predates
paperV1 on 09-23…09-26, which reads as “silent” on both chains — pre-feature, not a regression).

---

## 1. Immediate moves (today / this week), in order

PR #7 is effectively landed. The parallel thread owns the merge and the *first*
Sanity → day-delta → one-knob cycle. Nothing here edits the same seams until
that cycle is written into `CYCLE_LOG.md`.

### M0 — today, zero knobs

1. **Adopt one gate read.** `python3 scripts/gate_status.py --days 7 --json /opt/cursor/artifacts/gate_status_<day>.json`
   is the only number set that decides KEEP / REVERT. Run it now (done — cycle 0 in
   `CYCLE_LOG.md`) and every morning after the 23:00 UTC lock has aged.
2. **Freeze the rev.** No IMAGE_REV bump until `stack-v129` has lived one full lock
   cycle (through the 09-30 book) unless a *hard* sanity check fails. Six revs in
   five hours is why the stability gate cannot be read.
3. **Docs truth after PR #7** (owner: the bootstrap thread; do not collide). `AGENTS.md`,
   `CONTRIBUTING.md`, `FORWARD.md` and the feature-map still say `main` is empty and
   tip is `cursor/desk-next-phases-114a`. If that thread does not update them in its
   cycle, one docs-only PR does it — no code.
4. **Claim the cycle before shipping.** Whoever opens the next `CYCLE_LOG.md` entry
   owns `stack-v130`. Two threads shipping two knobs is the failure mode this plan
   exists to prevent.

### M1 — cycle 1, the ONE knob: make Sol leave a label every day

**Knob:** *when* paperV1 qualify is evaluated — not the floors.

Implementation sketch (seam: `ledger.py` paper sync + `paper_v1.py`; tests in
`tests/test_paper_v1.py` / `tests/test_ledger.py`):

- On each paper sync, for this-window **open wide gated fills** with no
  `paper_v1` / `paper_v1_shadow` row for that mint, re-run `_consider_paper_v1`
  with the *current* Hunt Live (`_paper_live_p`, then `HuntCard.conviction_p`)
  and `buy_mcap` = the current sellable print.
- Keep the existing guards: leftover reject (≥ $1M first-seen), late-chase
  (`PAPER_CHASE_MULT` 1.8× t0 → refuse), hard-tag soft path, 3/chain cap, 23:00 lock.
- If it still does not qualify or near-qualify, write a **shadow row with a new
  skip reason `v1 live-cold`** so the day has a label instead of a hole.
- Freeze Live at that moment (`freeze_live_at_entry(src="paper_reconsider")`) so
  the score path is measurable later.

**Predicted move:** Sol `queued + shadow` rows/day > 0; Sol day-delta `silence` → none;
RH counts unchanged; no open above 1.8× t0.
**KEEP if:** ≥ 3 consecutive Sol book days with rows, hard sanity green, no cap breach.
**REVERT if:** any chased open, any RH change, or Sol still silent after 3 days
(then the next knob is Live *coverage*, not a lower Live floor).

### M2 — invariant repairs bundled with v130 (not knobs; each with a test)

These restore the mission’s own definitions. They do not tune selection, so they
ride with the cycle-1 rev instead of spending cycles.

| Repair | Where | Proof |
|---|---|---|
| Review scan bounded by chain **and** book-day window, not `limit(240)` across chains | `ledger.py` `paper_v1_review` | `/api/paper/v1/review?chain=robinhood&day=<yesterday>` returns the rows `taken_chain` counts |
| Per-day claim on `promote_paper_v1_queue` (the lock has one; promote does not) | `ledger.py` | Two overlapping workers cannot double-open during a deploy |
| `git_sha` stamped (`ARG GIT_SHA` → `ENV GIT_COMMIT` in `Dockerfile`; pass from `railway up` wrapper / runbook) | `Dockerfile`, `image_rev.py` `running_git_sha` | `/health.git_sha` non-null; `gate_status` stability warn clears |
| One thesis-coverage definition: **hard tag on opens** everywhere (report `gate`, day-delta, Learn Daily ritual); keep any-tag as a labelled secondary | `ledger.py` day report, `day_delta.py`, `desk.js` | The three numbers collapse to one |
| Sanity `paper_this_window` labelled as *wide-book* EV, or pointed at the short list | `sanity_loop.py` | No “short-list quality” claim from wide data |
| Three CI-subset tests filter `PaperFill.line == "gated90"` instead of `.one()` | `tests/test_desk_lines.py`, `tests/test_ledger.py` | `.github/workflows/pytest.yml` subset green on `main` |

### M3 — the ranked knob backlog (pick ONE per cycle, by evidence)

1. **Refit guardrail:** `MIN_CLOSED` 12 → 30 *resolved* closes; score the rank-policy
   A/B on resolved book days with top-3-per-lane (what the desk actually takes), not
   top-half; keep the 5 pp margin but require n ≥ 30. Until then the auto-flip is
   frozen (see §2).
2. **RH hard-tag scarcity:** RH queue has 1 hard-tag name in 61. Test policy `thesis`
   in the RH lane only, as an A/B *lane* with its own labels — never an auto flip.
3. **Sol early-density shadow flag** (OFFLINE_SPRINT step 8) — annotate only.
4. **Leftover / open-band preference** (OFFLINE_SPRINT step 10) — shadow first.

### M4 — this week, zero knobs: the daily ritual

~07:00 UTC, ten minutes: `gate_status` → `day-delta` both chains → `sanity-loop`
both chains → one ≤ 10-line `CYCLE_LOG.md` entry → KEEP / REVERT / HOLD. If the
ritual was skipped, the cycle does not advance.

---

## 2. Operating model — the one-knob cycle

### What is locked vs what moves

| Locked (mission) | Movable (tunables — the knob list = `gate_status` `knobs`) |
|---|---|
| Research only; paper only; `armed false`; no swap keys | `rank_policy`; `thesis_weights` (via refit only) |
| 3 + 3 per UTC day; 23:00 lock; forward only | Sol Live floors 0.50 / 0.40; RH Entry floors 0.40 / 0.30 |
| Hard tags = github / dev / cto; meme alone is not thesis | Immediate line 0.70; promote peak 2×; thesis min 0.25 / strong 0.70 |
| Frozen Entry; `FEATURE_NAMES` 66; judge from first fillable print | Leftover mcap $1M; `MIN_CLOSED`; flip margin; **when** qualify runs |
| Six-gate definition in `gate_status.py` | Enrichment coverage (GitHub scrape, CTO refresh) |

### Cadence

| Clock | Who | What |
|---|---|---|
| Hourly | worker (automatic) | sanity jobs (repair / freeze), midday promote, refit attempt — already shipped |
| 23:00 UTC | worker | lock the day; the book is now a label set |
| +24 h | — | the book is *resolved* (every pick closed or 24h-clocked) |
| 07:00 UTC daily | operator (10 min) | `gate_status` + day-delta + sanity → `CYCLE_LOG.md` line |
| Per cycle (≈ 3 resolved book days ≈ 4 calendar days) | operator + one agent | choose the next knob from §1 M3 by evidence; ship `stack-v(N+1)` with tests, lockstep, both services; record the baseline **before** deploy |

A knob shipped at hour *h* gets its first honest read after the next lock **plus**
24 h. Reading it earlier is reading survivors.

### Metrics (all from existing surfaces — no parallel scorecard)

| # | Metric | Source | Gate |
|---|---|---|---|
| 1 | Labels/day per chain (`queued + open + skipped + shadow`) | day-delta / review | Ritual: > 0 on **both** chains |
| 2 | Hard-tag coverage on paperV1 opens | `gate_status` | ≥ 0.80 per chain |
| 3 | Resolved closes | `gate_status` | ≥ 30 per chain |
| 4 | Resolved hit2× vs wide-hi baseline (Sol 0.108 / RH 0.097 today) | `gate_status` | above baseline at n ≥ 30 |
| 5 | Sanity hard checks (FOMO door, hijack keep) | `/api/sanity-loop` | green, both chains |
| 6 | Knob diff between cycles | `gate_status` `knobs` | exactly one line |

Secondary (watch, never optimise): wide-book this-window EV, `shadow_late` avg,
skip-reason mix, queue clone rate (same `entry_p` to 4 dp), Live-missing share of opens.

### Hard gates — a knob may not ship when

- any sanity **hard** check fails on either chain;
- the last resolved book day has `taken_chain > cap_per_chain` (post-v124);
- the live rev is younger than one lock cycle (24 h) — unless the ship is a revert;
- the review is truncated for the chain being changed (yesterday unreadable);
- working tree `IMAGE_REV` ≠ `/health.image_rev` (stamp drift unexplained);
- `CYCLE_LOG.md` has an open cycle owned by someone else.

### Promote / keep / revert

| Verdict | Rule |
|---|---|
| **KEEP** | Target metric moved in the predicted direction on ≥ 3 resolved book days **and** no gate regressed **and** the knob diff is the only diff. Cite `gate_status` JSON path + both day-deltas + both sanity reads. |
| **REVERT** | Any hard sanity fail, cap breach, unpredicted wide-book change, or target metric **flat** after 3 resolved days. Flat is “no evidence”, and no evidence means revert — not “wait more”. Revert = previous rev via `docs/RUNBOOK.md`, both services, `/health` proof, log line. |
| **HOLD** | Sample too thin to read. The next knob must be a *label-density* move (capture / shadow / reason), never a floor loosening. |

### Governing the automatic knob-movers

The refit’s rank-policy auto-flip and weight nudge are knob-movers that run
without a human. Rules until M3-1 ships:

- A flip on `n < 30` **consumes** that cycle’s knob, is logged as such, and is
  manually reverted with `save_rank_policy` in the same ritual.
- Weight nudges are capped at ±0.04 per refit already; they are reported, not
  acted on, until `MIN_CLOSED` is resolved-30.
- `sanity-loop?apply=1` runs only enrich / freeze jobs (`EXECUTABLE`); that stays.

---

## 3. Top risks and failure modes

| Risk | How it shows up | Mitigation (mapped) |
|---|---|---|
| **Stamp drift** | `git_sha null`; six revs in five hours; `/health` shows a rev while Railway still deploys; two workers during overlap; review `limit(240)` truncation makes “yesterday” a hole | M2 repairs; rev-age warn in `gate_status`; rule: rev ≥ 24 h before a knob; runbook “wait for SUCCESS” |
| **Sol silence** | zero rows of any kind for a week while Hunt shows 7 names with Live ≥ 0.50 | M1 reconsider at paper sync with chase guard; `v1 live-cold` shadow reason; measure labels, not opens. The tempting fix — lower Live 0.50 — is an anti-goal |
| **Thin thesis** | hard-tag rate 4% / 1.3% among winners; RH opens are 0.42 memes; one github name in 61 queued | Enrichment coverage first (`thesis_enrich` on RH websites, GitHub scrape breadth, CTO refresh); if after 3 cycles hard-tag-on-opens cannot approach 0.80, the gate definition is re-examined **in writing with the miss-cohort precision table** — never quietly relaxed |
| **Self-deception** | 0.818 hit2× on 11 early closes; A/B scored on the incumbent’s own picks (top-half, not top-3); three coverage numbers; sanity “short-list quality” read from the wide book; the 09-29 RH book mixes v123/v124 regimes | Resolved-day rule; `gate_status` as the single read; M2 unify coverage; exclude 09-29 from the v129 baseline; A/B on resolved days per lane (M3-1) |
| **Knob churn / two writers** | parallel threads each ship “one” knob; deploy stacks change caps mid-book (the 5 > 3) | `CYCLE_LOG.md` is the lock; one open cycle at a time; no rev inside a live book unless revert |
| **Paid X spend** | someone sets `X_PAID_ENABLED=1` with a bearer present | `gate_status` risk gate fails on `x_api true`; X stays off until hard-tag coverage is the proven bottleneck; X never feeds a hard tag |
| **Data-plan ceilings** | `helius_capped`, Bitquery realtime-only | Not blocking the loop; keep on OFFLINE_SPRINT deferred list |

---

## 4. What NOT to do

- Do not set `armed: true`, enable `X_PAID_ENABLED`, or add any swap / signer key.
- Do not lower Sol Live 0.50 / 0.40, RH Entry 0.40 / 0.30, or any hi line to make Sol talk.
- Do not ship more than one behaviour knob per rev, or any rev younger than a lock cycle without a hard-fail reason.
- Do not cite the rolling `metrics.hit2x` (survivor-biased) or the report’s 38% “thesis_cov” as gate evidence.
- Do not count meme-only as thesis; do not add meme back to `THESIS_HARD_TAGS` to raise coverage.
- Do not build another scorecard, table or dashboard; extend `gate_status.py` / Learn only, and map every metric to Learn / Sanity / paperV1 / M3.
- Do not let the refit auto-flip move `rank_policy` on n < 30 without logging and reverting it.
- Do not chase: no short-list open above 1.8× t0 through the reconsider path.
- Do not touch the bootstrap merge, force-push, or edit the merge thread’s seams while its cycle is open.
- Do not spend cycles on Bitquery archive, Hunt cosmetics, Classic tabs, or a curated “incredible” list.

---

## 5. Stretch — only after gates move

- `GET /api/paper/v1/gate` + a Learn **Gate** card rendering the same six rows as `gate_status.py` (same code path, once definitions have held for ≥ 2 cycles).
- RH holder freeze onto Decisions + batch refit (OFFLINE_SPRINT step 9).
- Helius early-wallet density as a Sol shadow score (step 8).
- Daily ritual text to Telegram from `report.text` — still paper, still `alerts` earned not assumed.
- Paid X on the 1–5 only, ≤ 2000 calls/month, when hard-tag coverage is the measured bottleneck.
- `chain_pause` as the per-chain revert for a bad knob instead of a redeploy.
- M5 tiny live: only after **6/6** gates on a rolling window on **both** chains.

---

## 6. Recommended next five actions

1. **Land this plan and run the gate read daily.** `scripts/gate_status.py --days 7 --json …` at 07:00 UTC; paste the six lines into `CYCLE_LOG.md`. Cycle 0 (baseline) is already recorded.
2. **Hold `stack-v129` through the 09-30 book.** No IMAGE_REV for ≥ 24 h; let the merge thread finish its cycle; confirm on 09-30 that RH opens ≤ 3 and that 09-29 was the last 5-cap book.
3. **Cycle 1 = `stack-v130`: Sol reconsider-on-paper-sync** (M1) with chase guard and `v1 live-cold` shadow reason, bundled with the M2 invariant repairs (review scan bound, promote claim, `git_sha`, unified coverage). Tests + lockstep + both services; baseline in `CYCLE_LOG.md` before deploy.
4. **Read after 3 resolved Sol book days.** KEEP if Sol labels/day > 0 and hard sanity stays green; REVERT otherwise and make the next knob Live *coverage*.
5. **Cycle 2 = refit guardrail** (`MIN_CLOSED` resolved-30, A/B on resolved days per lane), then RH hard-tag scarcity as the next evidence-picked knob.

---

## 7. Checklist

**Every morning (07:00 UTC, 10 min)**

- [ ] `python3 scripts/gate_status.py --days 7 --json /opt/cursor/artifacts/gate_status_$(date -u +%F).json`
- [ ] `GET /api/paper/v1/day-delta?chain=sol` and `?chain=robinhood` — silence must be `none` on both
- [ ] `GET /api/sanity-loop?chain=sol` and `?chain=robinhood` — `hard_ok true`
- [ ] `/health.image_rev` == repo `IMAGE_REV`; `armed false`; `x_api false`
- [ ] One ≤ 10-line entry in `docs/CYCLE_LOG.md`: gates x/6, per-chain labels, knob diff, verdict

**Before shipping a knob**

- [ ] `CYCLE_LOG.md` has no open cycle owned by someone else; claim it
- [ ] Live rev ≥ 24 h old, or this is a revert
- [ ] Sanity hard checks green both chains; last resolved day has no cap breach
- [ ] Exactly one line differs in `gate_status` `knobs` (or one behaviour change named in the entry)
- [ ] Predicted move + KEEP / REVERT criteria written **before** deploy
- [ ] Tests for the change; IMAGE_REV five-file lockstep; both Railway services SUCCESS; `/health` proof

**Reading a knob**

- [ ] ≥ 3 resolved book days since the lock after deploy
- [ ] Target metric moved as predicted; no gate regressed; wide book unchanged
- [ ] KEEP / REVERT / HOLD written with the `gate_status` JSON path
