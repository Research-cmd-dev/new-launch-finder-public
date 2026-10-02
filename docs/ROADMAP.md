# Launch Finder — audit and roadmap

Audit date: 2026-09-17, image `stack-v66`. Numbers below are from the live
API at that time; re-pull them before acting on any phase.

> **Operating plan (current):** see **[docs/FORWARD.md](FORWARD.md)**.  
> **Desk truth (current):** `.agents/skills/launchfinder-feature-map/SKILL.md`.  
> **Goal shift (2026-09-27):** optimize for a **1–5/day short list** + fast
> recursive learning — not maximizing wide-book “buy” marks.  
> **Urgency:** accelerate honest improvement to a **production-grade** desk
> as fast as evidence allows (FORWARD “Accelerate to production”); paper until
> the gate — do not slow-walk the loop or skip risk.  
> Repo tip / live as of late 2026-09-27: **`stack-v110`**. Re-check `/health`
> before acting on live numbers in this file.  
> Several build-status rows below are **stale relative to v85–v110**; trust
> FORWARD + feature-map over the old table when they disagree.

## The goal, restated

At the moment a token becomes buyable (Pump.fun migration, RH LP open) the desk
prints an **Entry** score. A 90+ Entry must mean a real, forward-measured
chance of 2× (and a fair shot at 5×) from *that* market cap. **Live** must
track the tape so a late climber reaches Doing well and can trigger a buy
later. Success is measured by the forward P&L of the gated 90+ line — not by
label accuracy, not by how the board looks.

## What the audit found

### 1. The two scoreboards disagree, and the honest one says Sol 90+ is not working

| Source | Sol 0.9 bin | RH 0.9 bin |
| --- | --- | --- |
| Label calibration (`Research.p_good` vs `Outcome.label`) | 69% "win" (5×) | 27.5% |
| Paper, ungated 0.70 line, real entry mcap (`/api/paper`) | 90+: n=25, **28% hit 2×, 16% hit 5×, median 1.18×, avg −29%**. 70–79: 67% hit 2× | 90+: n=21, 100% hit 2× — but these are post-climb fair-LP books, not launches |
| Gated 90+ Paper (the desk's actual "buy" line) | **0 fills in the current window** from ~1,260 Sol ingests/day | 0 fills |

Why the label board is optimistic:

- `Research.p_good` is not frozen. Twelve `repair_*` passes in
  `scoring/outcomes.py`, the second-look upgrade, stall honesty
  (`live_multiple` and `age_min` are model features) and ATH-dump caps rewrite
  it after entry. Calibration and `evaluate()` read the rewritten value, so
  losers are marked down and winners marked up after the fact.
- `train_pending()` and `_labeled_rows()` include backfill / historical rows.
  Backfill forces `t0 = graduation_mcap` ($69k) and backfill sources are
  selection-biased toward names that already ran. A 27.7% base rate for "5×
  without liquidity collapse" on Pump.fun graduates is not plausible.
- Frozen entry-time fields already exist (`Snapshot(kind='t0').p_good`,
  `HuntCard.entry_p`) but nothing evaluates or trains against them.

### 2. The model is a hand-patched online logistic regression

`predict()` in `scoring/model.py`: 66-feature SGD logistic (lr 0.08, positive
weight 2.5, L2) blended with the heuristic, followed by ~15 sequential
`min`/`max` patches (0.48 caps, 0.60 organic floor, `heuristic + 0.18`
guard). `YOUNG_RH_N_TRAIN = 48000` is a hand-bumped hold with ~270 lines of
"Live HH:MM: n_train … Hold" comments; the RH model is effectively never
trusted. RH "accuracy 0.985" is the 1.3% base rate. Sol calibration is
non-monotone (0.2–0.3 bin wins 45%, above every bin from 0.4 to 0.7). There is
no holdout, no time-split evaluation, no versioned model artifact.

### 3. Nothing is a ledger

`/api/paper` recomputes from mutable `Research` / `Outcome` / `HuntCard` rows
on every call; `repair_entry_prices` rewrites `t0_mcap`. The same trade can
show a different P&L tomorrow. There is no append-only record of "at time T
the desk said Entry 92 at $X mcap with these flags".

### 4. Tape gaps that block Live and any future buy

- Sol holder count is frozen at ingest; RH refreshes from Blockscout every 2
  min. Holder history exists (`research/holders.py`) but only RH feeds it.
- Price history is t0 / t15m / t1h / t6h / t24h plus live snaps; no 1-minute
  series, so drawdown, slippage and "did the level really trade" are inferred.
- Doing well on Sol shows 2 names; on RH it is 40 names dominated by leftover
  FDV books with Entry 0.2 at 50×.

### 5. Engineering

- `app.py` 8.5k lines, `outcomes.py` ~4k lines; 289 "Live HH:MM" comments in
  `app.py` alone. The code is a patch log.
- 3 tests red on `main` since at least `a50ec9d`
  (`tests/test_runners.py`: `test_card_last_mcap_unfreezes_when_only_the_t1h_snap_exists`,
  `test_doing_well_keeps_start_high_after_dex_recovery`,
  `test_doing_well_keeps_labeled_live_5x_when_runners_unconfirm`). Full suite
  takes ~7 min. No CI checks are reported on PRs.
- One worker runs ingest (12s), Hunt tape (60s), holders (2 min), repairs and
  retrain; `/health` exposes booleans, not last-run timestamps. `alerts:false`.

### 6. What is working

Ingest volume and every integration are green. Hunt Live is now chain-honest
(`stack-v66`). The hard vetoes (copycat / brand clone / bundle / funder rug /
staged social) catch the visible scam classes — the Paper-90 loop has found no
new leak for many iterations. The remaining problem is not leaks: the 90+ line
rarely fires, and on Sol it is not predictive when it does.

## Plan

Each phase has an exit test. Do not start the next phase until it passes.

### Phase 0 — Truth first: decision ledger and honest scoreboard

1. Add an append-only `decisions` table: chain, mint, ts, `entry_p`,
   `heuristic_p`, `model_p`, features hash, entry mcap, liq, holders, flags,
   veto state, `image_rev`, model version. Written at first score and whenever
   a card crosses a desk line (0.70 / 0.90) on the Hunt tick. Never updated.
2. Honest evaluation: `/api/model/calibration?basis=entry` and `/api/report`
   computed from decisions (fallback `Snapshot(t0).p_good` / `HuntCard.entry_p`
   for history), excluding backfill / historical, split by chain and by
   ISO week. Metrics: hit 2×, hit 5×, median multiple, share under 0.5×, per
   Entry decile.
3. `/api/paper` reads and writes fills in the ledger instead of recomputing.

Exit test: re-running the report on a past week returns the same numbers, and
we have a true per-chain hit rate for Entry 90+.

### Phase 1 — Fix the training signal

1. Training set = decisions joined to outcomes. Exclude backfill / historical.
   Strip post-entry features (`live_multiple`, `age_min`, anything from
   `repair_*`) from training vectors; they stay display-only.
2. Primary label = **2× within 24h from the decision's entry mcap, with liq ≥
   floor and holders ≥ floor**. 5× is a secondary label. This is the desk's
   goal, and it lifts positives out of the noise floor.
3. Replace online SGD with a periodic batch fit (logistic or gradient boosting
   on the decisions table) with a time-ordered split and isotonic calibration.
   Store the artifact and its forward metrics in `ModelState`; promote a new
   version only if forward Brier and precision@top-decile beat the incumbent.
   Delete `YOUNG_RH_N_TRAIN`, the blend ramp and every score-shaping patch that
   the fit makes unnecessary. Hard scam vetoes stay as vetoes, not as score
   arithmetic.
4. Per-launchpad models where volume allows (Pump.fun, Bonk, RH PONS).

Exit test: forward, ledger-based Sol Entry 90+ is monotone with the deciles
and hits 2× at ≥ 60% over a rolling month with n ≥ 50.

### Phase 2 — Make the tape honest for Live and for later buys

1. Sol holder refresh on Hunt mints every 2 min (Helius token accounts / DAS),
   feeding the existing holder history.
2. Persist 1-minute OHLC + liquidity for Hunt mints for 24h (Dex / Bitquery).
   Use it for drawdown, slippage estimates and confirmed fills.
3. Live becomes a second model trained on tape features at t+N minutes,
   evaluated the same way as Entry. Bloom and Doing well read it. Today Live
   is heuristics only.

Exit test: Live at t+15m predicts 2× from that point better than Entry alone
on the forward ledger.

### Phase 3 — Execution readiness, still manual

1. "Buy ticket" object created when the gated 90+ line fires: entry mcap,
   size suggestion from liquidity, slippage estimate, stop / take levels,
   veto reasons, decision id. Manual confirm in the desk; ticket → outcome
   logged in the ledger. This is the exact interface a future auto-buy calls.
2. Turn alerts on (Telegram / X DM) for tickets only.
3. Shadow mode: run tickets for at least four weeks and report their forward
   P&L from the ledger.

Exit test: ≥ 5 Sol tickets/day, ticket line hits 2× at ≥ 60% and 5× at ≥ 25%
over four forward weeks. RH tickets only at real LP open with Entry ≥ 0.70,
never on post-climb books.

### Phase 4 — Auto-buy

Only after Phase 3 passes. GMGN swap through the existing skill with per-trade
cap, daily cap, kill switch, and the same ledger. Start with a fraction of the
ticket size and scale on forward results.

### Engineering hygiene (in parallel, small PRs)

- Fix the 3 red tests; add a CI check that runs `pytest -q` on every PR.
- Split `app.py`: routes vs board builders vs paper; move the "Live HH:MM"
  narratives into `CHANGELOG.md` and keep code comments to intent.
- Derive `IMAGE_REV` from the git SHA at build time instead of a hand-edited
  constant across five files.
- `/health` exposes last-run timestamps per worker loop, not booleans.
- Re-point the Paper-90 loop from "look for leaks" to "report the forward
  ledger hit rate per Entry decile"; leaks are now caught, hit rate is not.

## What not to do

- No more caps / floors / holds added to `predict()`. Every one of them is a
  symptom of training on the wrong signal.
- No auto-buy before a forward ledger exists. Today the desk cannot prove its
  own hit rate.

## Build status (stack-v75)

stack-v75 is the scoring pass the honest board finally made possible, and it
started by finding the board itself was lying in two ways.

**The judge.** `decision_result` took `max(Outcome.max_mcap, last_mcap)` as the
peak. `rh_ingest_entry` seeds `max_mcap` at the $40k graduation floor for a
curve book, so an RH trenches name printed at $4k that never traded again
read as a 9× "hit" (2,972 of 3,291 hits in the 0.6 bin; 6,375 rows under $5k
at 98%). The peak is now a print we actually recorded after the decision —
snapshot or one-minute bar on a pool with sellable liquidity (≥ $5k, the
paper exit floor), the outcome's last Dex print on a sellable book, or a
`max_mcap` a refresh raised above its seed — looked up in bulk
(`post_decision_evidence`). A decision with no print at all afterwards is
`unobserved`: nobody traded it, so there was no exit; it is a loss, and the
board shows the share per bin (`No tape`) next to the hit rate on the rows a
market did show us. The batch-fit label reads the same judge.

**The join.** `pump_poll` flips Sol to `is_historical` at 18h and the RH
quiet-retire does the same; the board filtered `is_historical`, so every
retired name — the losers — left the board with it. Sol had 40 judged rows
and 855 "open"; 21,610 first-sight scores from the last 30 days (909 of them
90+) were never in the ledger because the seed skipped retired tokens too.
The reader now excludes only backfill sources (the writer already refuses
tokens historical at first sight), and a second seed pass
(`RESEED_RETIRED_KEY`, fresh-at-t0 names only, no feature copy) wrote 59,040
decisions. Honest Sol board after both fixes: 0.0–0.1 hits 3%, everything
from 0.2 to 0.9+ sits at 24–40% with no order — the 90 line at 35%. RH: 70% of
first sights never print again; 90+ hits 17%.

**The model.** With 21.5k judged Sol rows and columns that are frozen at
first sight outside `features_json` — the decision row, the t0 snapshot's
5-minute buys / sells / volume, creation and migration times, ingest-frozen
holder concentration, creator history, socials — `first_sight.py` fits a
ridge logistic on all of them (seed and live) with the newest 7 days held
out. Out of time: AUC 0.80 for 2× / 0.87 for 5×, top decile 50% 2× / 43% 5×
against the old 90 line's 18% / 10%, bottom three deciles near zero, stable
day by day. Its picks are slow organic curve fills seen with no bot burst;
its misses are five-minute fills with forty buys in the window — the desk's
own "curve filled instantly" flag, which the old blend never weighted. The
old Entry has AUC 0.51 among viable names: a dead-vs-viable detector. Where a
first-sight artifact is promoted (Sol only; RH's research holder columns are
rewritten by the 2-minute refresh and are not frozen), the research pipeline
writes it as `p_good`, so Entry, the desk lines, the paper gate and every new
decision carry it; the legacy blend stays on the row as `heuristic_p` /
`model_p` / `legacy_p`. Fits hourly (a promoted incumbent under 6h is not
refit), `POST /api/model/fit?kind=first_sight`, Board card
`First-sight model · scores Entry`. Expect fewer names over 0.70 and almost
none over 0.90 at first: those numbers now mean what they say.

stack-v72–v74 is the desk pass (v74: header "Live" count relabelled "Tracked" so it cannot read as a score), plus one model-integrity fix it surfaced. The
first RH batch fit (v4, 04:02) promoted itself on AUC 0.94 / 99.8% hit-2× at
90 across 2,265 validation rows while the forward board for the same line
reads 22%. The rows were `seed_t0` decisions whose `features_json` was the
*current* research vector (post-repair: `entry_collapse`, `holder_n`,
`volume_n` already describing the outcome), so the fit learned the label.
`training_rows` now takes `source=live` decisions only (frozen at first
sight), every artifact records `live_only`, and `demote_unfrozen_fits` clears
`promoted` on anything fitted before the rule — at worker boot and before
every fit — which also unfreezes online SGD for that chain. The promoted
weights it copied into `ModelState` stay (the incumbent is gone; the online
path trains on the same mutable rows anyway); the honest replacement is the
first live-only fit, which needs 400 resolved live decisions.

Desk (`/ui`, `/ui/rh`): header metrics are the honest board (`90+ → 2×`,
`70+ → 2×` hit/resolved, ledger counts) instead of the online `accuracy`
(`2.141` on RH); status pill goes amber on `helius capped` or a stale tape
loop; the Telegram banner is a pill. Hunt Live pills mark the 0.62 thin-watch
cap and draw a missing Live as a dashed `—`. A tape strip of our own
one-minute bars (t0 / 2× guides, holder line) sits above the Dex iframe so a
fresh pair is never a ghost. The research card has a **Ledger** block:
frozen entry decision vs card-now drift, line crossings, gate verdict, fill,
ticket with Confirm / Skip. New **Tickets** tab (shadow tickets, state, size,
P&L) and **Board** tab (Entry bins, weekly desk lines, batch-fit card with
`Frozen rows`). Paper shows gate vetoes by name. Sort by Live sorts the shown
Live; Entry sort added. Mobile: header wraps, tape → card → chart, tape keeps
its height. Keys: `j`/`k`, `1`–`8`, `/`.

stack-v70/v71 fix the first thing the honest board showed: Sol entries scored
on the bonding curve (`liq` under the $800 dead-pool floor; Pump reports the curve SOL as "liquidity") were judged from that print, so graduation
alone (~$28k → $69k) read as a 2× for every migrating token and the 0.0–0.1
bin showed an 80× "median". `decision_result` now re-bases a curve entry to
the first pool (`t0_mcap`), and a curve entry that never got a pool is a
dead loss. Both the calibration board and the batch-fit label read this.

stack-v68 was the live-fix pass on the stack-v67 deploy: the ledger seed
survives a live decision landing mid-seed (savepoint per row), the Hunt tape
loop commits its Dex prints before the Sol / RH holder HTTP so
`refresh_outcomes` no longer hits `lock_timeout` on `hunt_cards`, the ledger
label caps the multiple at 80× like the desk, and the batch fit drops
leftover-graduation-FDV RH rows the online path already excluded.

stack-v69 makes the Phase 2 Sol holder refresh budget-aware. Verifying v68,
the shared Helius key answered `429 max usage reached` (the plan cap) and
new Sol launches were scoring with 0 holders. The refresh now runs on an
hourly DAS page budget, only for desk-line cards, two pages a card, and
parks itself 30 min on any 429 so ingest-time holder stats keep the credits.
Open item for the operator: the Helius plan needs headroom (or a second key
for the tape) before Phase 2's Live model can see Sol holder growth.

| Phase | Built | Where | Still forward-gated |
|---|---|---|---|
| 0 ledger | `decisions` (entry / line70 / line90 / gate), 30-day `t0` seed plus the v75 retired pass, `honest_calibration` + `honest_weekly` on an evidence-based judge (sellable post-entry print, silence is a loss), persisted `paper_fills`, `/api/paper?gated=true` reads the ledger | `launchfinder/ledger.py`, `/api/model/calibration`, `/api/ledger/*` | Exit test is met by construction (rows never update). Retired tokens stay on the board. The true Sol 90+ hit rate reads off `/api/ledger/weekly` as weeks accrue. |
| 1 training signal | Batch ridge-logistic on frozen entry features, 2×/24h live-pool label, oldest-80 / newest-20 time split, isotonic map, promote only if forward Brier improves and AUC / top-decile hold; promotion copies weights into `ModelState`, applies the map inside `predict()`, and freezes online SGD for that chain. Hourly in the worker, `POST /api/model/fit` on demand. **v75:** the first-sight model (`first_sight.py`) trains on frozen first-sight columns of every judged decision, seed and live, 7-day out-of-time tail, and is Sol's Entry once promoted. | `launchfinder/scoring/batch_fit.py`, `launchfinder/scoring/first_sight.py`, `/api/model/artifacts?kind=first_sight` | Deleting `YOUNG_RH_N_TRAIN` / blend ramp / score patches waits until the forward board shows they are unnecessary. RH first-sight needs frozen holder columns (write the ingest value to the decision row only). Per-launchpad models need volume. |
| 2 tape | Sol Hunt holders from Helius every 300s (desk-line cards, top-Entry first, cap 4, hourly DAS page budget, 429 breaker), one-minute `tape_bars` (48h), `live_samples` at t+15m, Live model fit against an Entry-only baseline on the same slice | `launchfinder/research/holders.py`, `launchfinder/scoring/hunt_tape.py`, `launchfinder/scoring/live_fit.py`, `/api/model/artifacts?kind=live` | Displayed Live stays heuristic until a live artifact is promoted (`live_model_p` is exposed, not wired to the board). Bars start at deploy, so the first live fit needs ≥ 300 resolved samples. |
| 3 tickets | Shadow ticket per gated fill (size from liquidity, slippage, stop / take / ride, reasons, decision id), Telegram / Discord alert when configured (log otherwise), manual confirm / skip, weekly forward P&L | `launchfinder/ledger.py`, `/api/tickets`, `/api/tickets/report`, `POST /api/tickets/{id}/status` | Four forward weeks of ≥ 5 Sol tickets/day at ≥ 60% 2× / ≥ 25% 5× before Phase 4 is discussed. |
| 4 auto-buy | Not built. | — | Gated on Phase 3. |
| hygiene | 3 red tests fixed (suite green, 1018), `/health.loops` heartbeats per worker loop, `/health.ledger` counts, Dockerfile build asserts for the new modules | — | CI runner: the forge is Origin, so no workflow file was added; run `pytest -q tests` before shipping. `app.py` split and git-SHA `IMAGE_REV` remain. |
