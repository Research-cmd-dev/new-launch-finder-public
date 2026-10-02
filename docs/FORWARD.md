# Launch Finder — forward plan (production path)


> **Public snapshot note:** `.agents/` skills and private Origin deploy credentials are not shipped here. Prefer [README.md](../README.md) hard locks and [HOW_IT_WORKS.md](HOW_IT_WORKS.md).

Last reviewed: 2026-09-29 (`stack-v130` shipping; paper only).
API coverage: **[docs/APIS.md](APIS.md)**.

Desk truth: `.agents/skills/launchfinder-feature-map/SKILL.md`.  
Agent routing: `AGENTS.md` + `.agents/ROLES.md`.

---

## Goal

Ship a system that can safely move from **paper 1–5/day** to **small live
execution** — only after the short list proves it picks better than chance
and improves from its own outcomes.

### Accelerate to production (project vision)

There is a **great need to accelerate improvement** so we reach a
production-grade product as quickly as possible. Production-grade here means
the gate below is earned — not that we flip a live switch early.

**Bias for speed (allowed):**

- Prefer changes that add **honest daily labels**, raise **thesis coverage**,
  or **shorten the refit → next-day short list** cycle.
- Ship small, verifiable slices; observe UTC days; refit from closes.
- Parallelize only when seams do not collide (see sprint rules).
- Optional data unlocks (Bitquery archive, GoldRush spike, loser cohorts)
  only when they compress time-to-evidence on the 1–5.

**Not speed (reject):**

- Auto-buy / arm before the gate; swap keys; “see more buys” via lower Hunt lines.
- Cosmetic desk work that does not improve selection or learning-loop honesty.
- Broad rewrites that stall deploy while the paper book idles.

Paper capital is zero today → explore **policy** hard and fast; do **not**
explore execution until the gate below passes.

**Production means (in order):**

1. Short list has **non-zero frozen thesis** on real candidates (not empty vectors).
2. Every UTC day yields **honest labels** (picked / skipped / outcome).
3. Policy **updates from those labels** without human rewrites.
4. Operator can **see and trust** the loop (Learn + review).
5. Only then: tiny live size, hard kill switch, alerts on.

---

## Live baseline (facts)

| Item | Value |
|---|---|
| Live `IMAGE_REV` | `stack-v131` on main tip (`09b9c18`); `stack-v132` is the paper-safe FOMO-heartbeat + Learn query-bound ship |
| Desk | `/` Desk only · Picks · Learn · Hunt · Calibrate |
| paperV1 | Cap 5/UTC day · midday promote (Live≥0.70 / strong thesis / queued peak≥2×) |
| Thesis | Enrich+repair shipped (v117); runner-scoped repair + Live@entry freeze jobs (v129) |
| Learn | Sanity apply + Daily ritual day-delta + veto-retro + runners-retro |
| `alerts` / auto-buy | Off |
| Tip | `cursor/next-steps-1b70` (product HEAD; formerly stacked on veto-retro) |
| `main` | Empty initial commit — land via bootstrap PR (fast-forward tip → `cursor/bootstrap-main-1b70` → `main`) |
| Paid X | Env `X_PAID_ENABLED` (default off) + `X_MONTHLY_CALL_BUDGET` (default 2000) |

---

## Production gate (must be true before real money)

All of these, on **both** Sol and RH, for a rolling window of resolved
short-list names (not wide Hunt):

| Gate | Bar |
|---|---|
| Thesis coverage | ≥80% of paperV1 opens have ≥1 frozen thesis tag |
| Sample | ≥30 closed paperV1 fills with sellable prints |
| Hit quality | Short-list hit2× **above** random / wide-hi baseline on same window |
| Stability | No IMAGE_REV / worker-loop incident for a full lock cycle |
| Operator ritual | Daily review used; skip reasons readable |
| Risk controls | Kill switch, max notional, no swap without explicit arm |

Until the gate passes: **paper only**.

---

## 1–2 week sprint (do in this order)

Timeboxes are calendar order, not effort estimates. Skip nothing that unblocks
the next row; do not parallelize M1–M3 across agents on the same seams.

### Days 1–2 — M1 unblock thesis (critical path)

| # | Work | Exit |
|---|---|---|
| 1a | **Done (v116 live)**: paperV1 reads entry `features_json` even when fill.decision_id is gate | `thesis_score > 0` on 10/10 queued |
| 1b | **Done (partial)**: tags present on half the book (`meme`); soft path still silent | Need GitHub/CTO/`real_project` for `thesis ≥ 0.25` |
| 1c | **Shipped (v117)**: website GitHub scrape + thesis recompute before Decision; Hunt-tape enrich on awaiting_fill; repair thin Decisions from raw | Soft path can fire when sources exist |
| 1d | **Shipped (v117)**: `repair_thin_entry_thesis` on paper sync (no HTTP) | Existing thin Decisions pick up stored github/cto |

*Next after v117 live proof: M2 midday promote (also shipped) + skip taxonomy + shadow book.*

### Days 3–5 — M2 dense daily labels

1. Midday **promote-from-queue** — **shipped v117** (`promote_paper_v1_queue`).
2. Review skip taxonomy `cap` / `no-thesis` / `score-miss` — **shipped v118**.
3. Shadow book `paper_v1_shadow` near-miss negatives — **shipped v118**.
4. Keep wide gated paper as training ocean — never the success metric.

*Exit:* most UTC days have picked **or** explicit skips with reasons; shadow rows accumulate. Watch Learn `/api/paper/v1/review` `shadow` + `skip_reason`.

### Days 5–8 — M3 same-day self-improve

1. Short-list **thesis weight refit** from paperV1 closes (promote/demote GitHub vs CTO vs meme vs Live margin).
2. A/B rank policies under independent 3/chain/day lanes (`signal` default vs `live` / `thesis`); score only on forward short-list P&L / hit2×.
3. Learn pane: yesterday vs today delta (hit rates + tag attribution).
4. Freeze mutable RH holder columns onto decisions for honest training.

*Exit:* weights move from data; losing policy is demoted without a hand-edit.

### Days 8–10 — M4 production readiness (still paper)

1. UTC-day report — **shipped v121** (`GET /api/paper/v1/report`).
2. Runbook — **shipped** (`docs/RUNBOOK.md`).
3. Risk module stub — **shipped v121** (`GET/POST /api/risk`, arm default off).
4. Pass **Production gate** table above on a real closed sample (observe).

### Days 10–14 — M5 tiny live (only after gate)

1. Arm one chain, min size, paper twin still running.
2. Alerts for fills / dumps / worker stale — not for Hunt flood.
3. Expand size only if short-list forward metrics hold.

---

## Parallel hygiene (do not steal M1–M3 cycles)

### M0 — Repo hygiene

- [x] Tip advanced to oversight + paperV1 v115
- [x] Ship v116 (thesis entry-Decision fix) + both Railway deploys
- [x] Ship v117 (thesis enrich + repair + midday promote + chain filter)
- [x] Ship v118 (skip taxonomy + shadow near-miss book)
- [x] Ship v119 Learn shadow UI
- [x] Ship v120 thesis weight refit + rank policy A/B
- [x] Ship v121 risk stub + day report + runbook
- [x] Ship v122 peak-2× midday promote + runners retro + year-winners template
- [x] Advance tip to v121 HEAD; merge bootstrap so `main` = tip still open
- [x] One deploy checklist: both Railway services + `/health.image_rev` (see `docs/RUNBOOK.md`)
- [x] paperV1 book chain filter

These help humans and agents; they do **not** replace thesis / labels / refit.

---

## What not to do

- Lower wide Hunt lines to “see more buys”
- Auto-buy / swap keys before the production gate
- Optimize for Hunt cosmetics or fill count
- Parallel agents rewriting `desk_lines.py` + `app.py` together
- Spending cycles on Classic / retired tabs
- Jumping to M5 because paper fills “look busy”

---

## Cadence

Every change answers: **better 1–5 selection**, **faster honest learning**,
or **shorter path to the production gate**? If none, it is not the next step.

1. Branch from tip (`cursor/desk-next-phases-114a`) until `main` is real.
2. One writer per seam; verify + IMAGE_REV when shipping UI/runtime.
3. Deploy **both** Railway services; prove with
   `/health.image_rev` + `/api/paper/v1` + `/api/paper/v1/review`.
4. End of each day: note thesis coverage %, picks taken, skip reasons, hit2× on closed short-list.

---

## Incredible-returns template (alpha note)

A 1-year “best coins” shopping list has **little extractable alpha by itself**
(survivorship). Useful form:

1. Confirmed runners in our ledger (`/api/runners`, `/api/year-winners`).
2. Curated first-book north stars (`exemplars.json` — PONS day-1, desk-caught).
3. Extract only **first-print** features (frozen thesis tags, Entry calibration,
   early-wallet overlap) vs same-day misses — never peak multiple as a feature.

### Zero-hour cohort scan (2026-09-29 research)

Scripts: `scripts/scan_zero_hour_alpha.py`, `scripts/scan_zero_hour_multisource.py`.
Artifacts: `/opt/cursor/artifacts/zero_hour_*.json|md`.

| Source | Role |
|---|---|
| GMGN / Gecko day OHLCV | First-day open FDV, high×, vol/open |
| Helius enhanced (Sol) | First-hour unique buyers, 1m/5m density, top10 share |
| Bitquery | **Live** RH Initialize + fresh-window trades only — archive add-on needed for year-old 0–24h (see `docs/APIS.md`) |

Working extractable rules (paper gates, not Decision rewrites):

1. **PONS-shape**: open FDV \< \$100k + day-1 vol/open ≥ 10× + high ≥ 10× (14 hits in cohort).
2. **Helius density (Sol)**: day-1 ≥10× runners median ~145 unique buyers / ~141 in first minute vs thinner/concentrated books (e.g. PAID top10≈0.75).
3. **Leftover reject**: first-seen mcap ≥ \$1M is not zero-hour — score as continuation only.
4. Midday peak-2× promote is **confirmation**, not discovery (median hours→5× ≈ 0 on honest opens).

## Immediate next implementation slice

**Post-v129 operating plan:** **[docs/SELF_IMPROVE_PLAN.md](SELF_IMPROVE_PLAN.md)** (one knob per
cycle, gate read = `scripts/gate_status.py`, log = [docs/CYCLE_LOG.md](CYCLE_LOG.md)).

**Do not wait on new launches.** Full plan: **[docs/OFFLINE_SPRINT.md](OFFLINE_SPRINT.md)** (10 steps).

Live blockers this plan attacks first (2026-09-29):

- Sol runners-retro **0/60** would-pass (Live unknown→0; thesis tags empty).
- RH **12/60** would-pass score but **0** on paperV1 book; thesis-path **0**.
- RH short list already has skips/shadow/closes to mine; Sol short list is silent.

Compressed order:

1. Capture-gap autopsy (runners-retro miss taxonomy).
2. Reconstruct historical Live at entry for retro (read-only).
3. Batch thin-thesis repair → thesis-path > 0.
4. Same-day miss / loser cohort API (desk-era).
5. Skip/shadow attribution → fix Sol paper silence.
6. Thesis weight refit + rank policy from closed short-list (+shadow).
7. Zero-hour scan on **ledger runners** (Gecko/Helius).
8. Helius early-density as Sol shadow score.
9. Freeze RH holders onto Decisions + batch model refit.
10. Ship paper-only shadow gates (leftover reject, etc.); arm still off.

Optional later (do not block): Bitquery archive, GoldRush spike, curated year list
with losers. Observe UTC days in parallel while the offline steps ship.
