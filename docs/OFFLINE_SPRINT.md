# Offline acceleration sprint — 10 steps (no new launches required)

Last planned: 2026-09-29. Complements [FORWARD.md](FORWARD.md).
Goal: compress time-to-production-grade evidence using **data we already have**.

Paper only. No auto-buy. No FEATURE_NAMES growth. No Decision rewrites except
thin thesis repair from stored `raw_json`.

---

## Why this plan (live facts, 2026-09-29)

| Fact | Implication |
|---|---|
| RH paperV1: queued 24, skipped 8, shadow 8, closed hit2× ~0.78 (small n) | Short-list loop is alive on RH — mine closes/skips/shadow now |
| Sol paperV1 review empty today while RH is busy | Diagnose Sol short-list silence offline (not “wait for launches”) |
| Sol runners-retro: **0/60** would-pass-v1 (`by_path` all `miss`) | Live at historical entry treated as **0** → score path dead; thesis tags empty → soft path dead |
| RH runners-retro: **12/60** score-path, **0** thesis-path, **0** on paperV1 book | Capture gap: would-pass ≠ booked — promote/queue/cap/repair to audit |
| thin_entry_features: Sol 13 / RH 0 (sample) | Sol thesis repair is a batch job, not a waiting game |
| Ledger runners: ~70 Sol / ~100 RH | Enough for zero-hour + early-wallet offline mining |
| Thesis weights still defaults; `MIN_CLOSED=12` | Pool closed paperV1+shadow (and prior days) to unlock refit |
| Bitquery archive blocked | Do **not** block this sprint on archive; use GMGN/Gecko/Helius |

---

## The 10 steps (do in order)

Each step has: **input → work → exit → unblocks**. Prefer shipping a Learn/API
surface or paper-only gate after every 2–3 steps so the operator sees progress.

### Step 1 — Capture-gap autopsy (read-only, hours)

**Input:** `/api/paper/v1/runners-retro` both chains; `/api/paper/v1/review`; day report.  
**Work:** Export a table: each confirmed ≥5× runner → path (`score`/`thesis`/`miss`),
entry_p, live_p (or “unknown”), thesis tags, thin flag, whether any `paper_v1*`
fill exists, skip/shadow if near-miss. Classify Sol misses into:
`live_unknown`, `entry_below_hi`, `no_thesis_tags`, `thin_features`, `veto`.  
**Exit:** Written artifact + Learn note: top 3 blockers by count (expect
`live_unknown` + `no_thesis_tags` dominate Sol; RH “would-pass but not booked”).  
**Unblocks:** Steps 2–4 prioritization. No code required beyond a script if useful
(`scripts/audit_runners_capture.py`).

### Step 2 — Reconstruct historical Live at entry (offline labels)

**Input:** `snapshots` / `tape_bars` / GMGN kline / Decision timestamp.  
**Work:** For runners-retro Sol rows with Live=0/unknown, compute
`live_p_at_entry` from stored snapshots nearest first-sight (or vendor kline
features already in Live training path). Surface as **retro-only** field — do
not mutate Decision. Re-run would-pass with reconstructed Live.  
**Exit:** Sol would_pass_rate moves off 0% *or* we prove Live reconstruction is
impossible for most rows (then thesis path becomes the only offline lever).  
**Unblocks:** Honest score-path capture stats; stops undercounting paperV1.

### Step 3 — Batch thin-thesis repair on runners + near-misses

**Input:** Tokens with empty/thin entry `features_json` but non-empty `Research.raw_json`
(github/cto/website). Confirmed runners + RH shadow/skips.  
**Work:** Raise/run `repair_thin_entry_thesis` in a one-shot admin/worker job
(limit high enough to clear the 13+ Sol thin set and any RH thin). Optional:
one GitHub scrape pass on names that have website but empty github blob.  
**Exit:** `thin_entry_features` on runners-retro drops sharply; thesis-path
count > 0 on re-audit.  
**Unblocks:** Soft-path paperV1 for culture names; weight refit signal.

### Step 4 — Same-day miss / loser cohort (desk-era only)

**Input:** All UTC days that already have Decisions + Outcomes (not pre-desk year).  
**Work:** New API or script: for each day/chain, winners (Outcome hit2×/5× in
entry-mcap band $10k–$500k) vs losers (same band, multiple <1.5× or labeled
loss) vs paper skips/shadow. Emit tag rates, Entry bins, holder/liq at Decision.  
**Exit:** `/api/paper/v1/miss-cohort` (or artifact JSON) with ≥1 desk-era week.  
**Unblocks:** Any claim that PONS-shape / thesis tags / buyer density have
**precision**, not just recall on survivors.

### Step 5 — Skip + shadow attribution → fix Sol silence

**Input:** RH skips (`no-thesis` / `score-miss` / `cap` / veto) + shadow book;
Sol empty short list vs wide Hunt/gate activity.  
**Work:** Count skip reasons; for Sol, determine whether candidates exist but
fail floors, or ingest/score path never reaches paperV1. Patch the **actual**
bottleneck (enrich, floors, chain day-cap accounting if shared incorrectly).  
**Exit:** Sol paperV1 can show queued/skipped/shadow from **existing** open
names (or an explicit “no eligible inventory” reason in review — never silent).  
**Unblocks:** Daily labels on both chains without waiting for “better” launches.

### Step 6 — Thesis weight refit + rank policy from closed short-list

**Input:** All closed `paper_v1` + `paper_v1_shadow` with sellable prints (pool
days/chains until n≥12).  
**Work:** Run `refit_thesis_weights`; A/B `rank_policy` thesis vs live on
retrospective short-list hit2× (forward-style walk if enough days). Persist
winning policy.  
**Exit:** Non-default weights in day report **or** documented “n still <12 —
shadow closes counted toward floor.”  
**Unblocks:** M3 self-improve without new flow.

### Step 7 — Zero-hour tape on **ledger runners** (not shopping lists)

**Input:** `/api/runners` mints (~70 Sol / ~100 RH).  
**Work:** `scan_zero_hour_multisource.py` pointed at ledger runners; Gecko day +
Helius first-hour Sol. Produce leftover-reject / open-FDV / vol/open distributions
**paired with Step 4 losers** where Decision timestamps exist.  
**Exit:** Updated artifact + 2–3 **paper shadow gate** proposals with estimated
false-positive rate on desk-era losers.  
**Unblocks:** Step 10 gates; stops treating external meme lists as alpha.

### Step 8 — Helius early-wallet density as Sol shadow score

**Input:** Confirmed Sol runners + Sol paper near-misses / shadow; existing
`early_wallets` harvest.  
**Work:** Offline: unique buyers 1m/5m/1h, top10 share vs hit2× among names we
already saw. Propose thresholds (e.g. reject top10≥0.6 & buyers<30) as
**shadow-only** annotate on review — not Entry features yet.  
**Exit:** Table + optional `early_density` field on Sol paper review rows.  
**Unblocks:** Sol-specific promote/reject hypothesis testable on next closes
(and historically on shadow).

### Step 9 — Freeze mutable RH holders onto Decisions + batch model refit

**Input:** RH Decisions with mutable Research holder columns; Outcomes.  
**Work:** Copy point-in-time holder fields onto Decision (or snapshot join) for
training honesty; `POST /api/model/fit` entry/first_sight/live from frozen rows
only.  
**Exit:** Calibration report before/after; no FEATURE_NAMES change.  
**Unblocks:** Stable Entry/Live for RH paper floors; cleaner M3.

### Step 10 — Ship paper-only shadow gates from offline evidence

**Input:** Steps 4–8 results (precision on losers, leftover reject, density).  
**Work:** Implement as paperV1 **veto / skip_reason / shadow annotate** only:
(1) leftover first-seen mcap ≥ $1M → not zero-hour short-list,
(2) optional open-band preference,
(3) optional Sol early-density shadow flag.
Learn pane: “offline gate would have skipped/kept.” Still arm off.  
**Exit:** IMAGE_REV bump + both services deploy; report shows new skip reasons
on **existing** queued/open book where applicable.  
**Unblocks:** Production gate sample quality; operator trust without waiting.

---

## Explicitly deferred (do not block the 10)

| Item | Why later |
|---|---|
| Bitquery archive upgrade | Nice for year-old trade tape; Steps 2/7 cover desk-era |
| GoldRush free-tier spike | Optional RH holders-at-block; after Step 4 if still thin |
| Hand-curated year “incredible” list | Only after loser cohort exists |
| Live arm / swap keys | Production gate unchanged |
| Lowering Hunt lines | Anti-goal |

---

## Cadence while executing

1. After Steps 1–3: re-pull runners-retro — Sol would_pass and thesis-path must move.  
2. After Steps 4–6: day report shows miss-cohort counts + non-default weights (or clear n gap).  
3. After Steps 7–10: paper shadow gates live; still paper-only.  
4. Every change still answers: better 1–5 selection, faster honest learning, or shorter path to the gate.

## Owner seams (avoid collisions)

| Steps | Seam |
|---|---|
| 1–2 | `runners_retro.py`, read-only scripts, Live reconstruct helper |
| 3, 6 | `thesis_enrich.py`, `thesis_weights.py`, paper sync |
| 4–5 | `ledger.py` review/report, paper_v1 skip taxonomy |
| 7–8 | `scripts/scan_zero_hour_*`, `early_wallets.py` |
| 9 | Decision freeze + `batch_fit` / `first_sight` |
| 10 | `paper_v1.py` + Learn UI + IMAGE_REV lockstep |
