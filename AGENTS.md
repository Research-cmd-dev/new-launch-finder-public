> **Public paper-only snapshot.** Agent skills under `.agents/` and private Origin (`dave-1/new_launch_finder`) are **not** in this tree. Start at [README.md](README.md). Live Railway URLs below (if present) are the private deploy — this public clone does **not** include credentials and may lag Origin.

# Launch Finder — agent guide

This repo is a **research desk** for new Pump.fun (Solana) and Robinhood Chain launches.
It scores, watches, and papers trades. It does **not** auto-buy.

**Canonical branch for product work:** `cursor/desk-next-phases-114a` (current tip is `stack-v110`).
`main` is still empty — do not treat it as the live product tree.
See README.md (CONTRIBUTING.md not in this public snapshot) and `docs/FORWARD.md` for branch policy and next priorities.

Live Sol desk: `https://new-launch-finder-production.up.railway.app/ui`  
Live RH desk: `https://new-launch-finder-production.up.railway.app/ui/rh`  
Health: `GET /health` (`image_rev` must match `launchfinder/image_rev.py`)

**Deploy lag check:** before trusting live numbers, compare `/health.image_rev`
to tip. As of 2026-09-27 evening deploy, live matched tip at `stack-v110`.

---

## Read order (every coding agent)

1. This file (`AGENTS.md`) — goals, hard rules, how to split work
2. `docs/FORWARD.md` — current priorities and tip↔prod status  
   (`docs/OFFLINE_SPRINT.md` — 10 steps that do **not** wait on new launches;  
   `docs/SELF_IMPROVE_PLAN.md` + `docs/CYCLE_LOG.md` — one-knob cycle rules and the gate read `scripts/gate_status.py`)
3. `docs/HOW_IT_WORKS.md` + this README (feature-map skill not shipped publicly) — current desk truth before any score/UI/gate change
4. `scripts/verify_desk.py` + `pytest` (verify skill not shipped publicly) — how to prove a change before calling it done
5. `docs/HOW_IT_WORKS.md` — doors / ingest (stale on chart tabs; feature-map wins on desk UI)
6. README.md agent contract (ROLES.md not shipped publicly) — which Cursor subagent to use for which job
7. README.md (CONTRIBUTING.md not in this public snapshot) — branch / PR policy

---

## Product goal

Thousands of migrations hit Solana and Robinhood every day. The desk must
learn to pick **1–5 buys per day** — the best memes, community takeovers, and
novel projects with real developers/GitHub — and **self-improve on that day’s
data as fast as the honest ledger allows**.

**Urgency:** get to a **production-grade** product as quickly as possible —
a short list that earns trust, a learning loop that compounds daily, and
(only after the gate) small live execution with hard kill controls. Bias
every change toward **faster honest improvement** on the 1–5, not polish,
Hunt cosmetics, or speculative infra. Speed means denser labels and shorter
refit cycles under paper; it does **not** mean skipping the production gate
or turning on auto-buy early.

- **Sensor / training set:** wide ingest, Entry, Live, Hunt (the flood).
- **Action surface:** the short list (`paperV1` today — cap 5/UTC day).
- **Primary success:** recursive learning-loop speed + honesty (frozen
  decisions → outcomes → better next-day short list).
- **Secondary success:** forward P&L of the 1–5 short list — not “how many
  hi-line fills” and not how pretty the board looks.
- **North star:** production-ready short list + operator-trusted loop, reached
  by compressing the paper learning cycle — not by relaxing risk.

Details and priorities: `docs/FORWARD.md`.

---

## Hard rules

- Research only. No `GMGN_PRIVATE_KEY`. No auto-swap. No trading bot framing.
- Do not invent a second copy of a constant — open the named source in the feature map.
- Do not hardcode Entry thresholds (`0.70` / `0.90` / first-sight lines). Use `desk_lines` / `legacy_equivalent`.
- Do not bump `IMAGE_REV` unless behavior or static cache actually changes; when you do, keep the five-file lockstep (see verify skill).
- Do not reverse NINA / RH MEME / GS uncapped fixtures unless the user explicitly asks.
- Prefer small, evidence-backed caps over broad display patches.
- Prefer ledger / frozen decision fields over mutable `Research.p_good` for evaluation.
- Do not optimize for more wide-book buys. Prefer work that improves the
  **1–5 short list**, **shortens the learning loop**, or **compresses time to
  the production gate** (`docs/FORWARD.md` — Accelerate to production).
- Auto-buy stays off until the short list has forward proof (`docs/FORWARD.md`).
  Urgency never overrides that gate.

---

## Repo map (where to look)

| Area | Path |
|---|---|
| FastAPI app + worker loops | `launchfinder/app.py` |
| Ingest doors | `launchfinder/ingest/` |
| Research enrichment | `launchfinder/research/` |
| Scoring / first-sight / hunt / paper | `launchfinder/scoring/` |
| Desk lines (per scorer / chain) | `launchfinder/desk_lines.py` |
| Ledger / decisions | `launchfinder/ledger.py` |
| Test desk UI | `launchfinder/static/desk*.html`, `desk.js`, `desk.css` |
| Image stamp | `launchfinder/image_rev.py` |
| Tests | `tests/` |
| Cloud agent env scripts | `scripts/cloud-agent-install.sh`, `scripts/cloud-agent-start.sh` |

---

## Cursor skills in this repo

Desk-native:

| Skill | When |
|---|---|
| `launchfinder-feature-map` | Before changing scoring, hunt, paper, desk UI, leftovers, IMAGE_REV, deploy |
| `launchfinder-verify` | Before declaring a change done; after score/gate/UI/deploy work |
| `launchfinder-orchestrate` | When splitting a multi-file or multi-board task across subagents |

GMGN pack (read-only market data): `gmgn-market`, `gmgn-token`, `gmgn-track`, `gmgn-holder-analysis`, `gmgn-contract-dd`, `gmgn-kline-pattern`, `gmgn-portfolio`.  
`gmgn-swap` stays human-confirmed only — never auto-run.

---

## How agents should work (best results)

### Parent agent stays thin

- Own the goal, hard constraints, and final PR.
- Read feature-map + verify skills yourself (or resume from them).
- Delegate parallel, independent chunks — do not nest the whole task in one subagent.

### Split along product seams

Good parallel cuts (see README.md agent contract (ROLES.md not shipped publicly)):

1. **Explore** — locate owners of a constant / API / UI surface
2. **Score / ledger** — `scoring/`, `ledger.py`, `desk_lines.py`, related tests
3. **Desk UI** — `static/desk*`, Pair pane, tabs (prove with click-through)
4. **Ingest / research** — doors, GMGN/Helius/Bitquery, enrichment only
5. **Verify** — pytest subset + live `/health` + Hunt/Paper pulls after deploy

Bad cuts: two agents editing `app.py` scoring paths at once; one agent inventing thresholds while another invents different ones.

### Done means verified

A change is done only when the verify skill's bar is met for that change type
(tests, and live API / UI when relevant). Screenshots alone are not enough.

### Prefer the tip branch

Unless the user names another branch, start from `cursor/desk-next-phases-114a`
(or a short-lived `cursor/<task>-1b70` cut from it). Do not rebuild the desk from empty `main`.
