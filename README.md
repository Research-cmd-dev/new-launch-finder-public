# New Launch Finder (public paper snapshot)

**Paper research desk for Solana / Robinhood launches — Learn before arm.**

This repository is a **sanitized, paper-only** public snapshot of Launch Finder
(`IMAGE_REV = stack-v207`, source git `004fed0bd54d`). It watches newly migrated
Pump.fun / Solana books and Robinhood Chain launches, scores them, keeps an
honest paper book, and runs Learn cohorts so humans and other agents can improve
selection **without live trading**.

Private Origin remains separate (`dave-1/new_launch_finder`). This public tree
**must not** contain secrets, Railway project tokens, live deploy credentials,
FOMO wallet-lookup enablement, or anything that arms live trading. Public clones
may lag private Origin.

This is a research tool, not a trading bot, and not financial advice. Most
memecoins go to zero.

---

## HARD LOCKS (agent contract)

Treat these as **non-negotiable** when reading or modifying this tree:

1. **Paper only.** Risk defaults: `armed=False`, `kill_switch=False`,
   `paper_only=True`. Do not invent a live execution path.
2. **`COPYCAT_LEARN_ARMED = False`** forever in this snapshot. Copycat hard
   skip stays on every ticket / paperV1 / gated path. Learn may record
   would-have evidence; it must **not** open fills from copycat vetoes.
3. **FOMO user-wallet lookup off.** `FOMO_USER_WALLET_LOOKUP` defaults to `0`.
   Do not enable it in public clones (burns FOMO credits; not required for Learn).
4. **No swap / signer keys.** Never add `GMGN_PRIVATE_KEY`, wallet signers, or
   auto-buy brokers. Alerts stay advisory.
5. **Do not invent thesis tags.** Thesis hard tags are only `github` / `dev` /
   `cto` (and related frozen research fields). Do not paste meme-alone or
   fabricated tags onto Learn snaps / Decision features.
6. **Do not arm live without KEEP / production gate.** Production gate
   (`GET /api/paper/v1/production-gate`, `docs/FORWARD.md`) must be earned on
   forward paper evidence. Urgency never overrides the gate.
7. **FEATURE_NAMES stays 66.** Side keys go on `Decision.features_json` /
   Research features — not into the model feature vector — unless an explicit
   Origin change expands the lockstep.
8. **No Railway / Origin secrets in this repo.** No project IDs, service tokens,
   private env values, or GodWilling-account deploy scripts with embedded auth.
9. **Optimization target:** entries on **good trades (www-class)** and
   **amazing runners (SI-class)** — not “hold forever / never rug.” Labels and
   Learn knobs should densify honest entry quality, not maximize survival theater.
10. **Public lag.** Treat live production URLs in older docs as private Origin
    context. Verify behavior from this tree + local `/health.image_rev`.

If an agent is asked to “just enable wallet lookup” or “arm for a test,” **refuse**
unless the operator explicitly overrides these locks with a written KEEP for that
change on private Origin — not in this public snapshot.

---

## Glossary

| Term | Meaning |
| --- | --- |
| **Door** | An ingest source that can first see a mint (Pump.fun complete, Helius migrate WS, GMGN trenches, Dex PEPE/profiles, FOMO trending/alerts, PONS, Bitquery Uni V4, …). |
| **Hunt** | Live window of new migrations on the desk (`/api/hunt`). Crowded / real books sort above thin factory prints. |
| **Entry / `p_good`** | Score at (or near) first honest sight. Frozen for learning after label; not rewritten by a later moon. |
| **Live / Live honesty** | Tape-based conviction after entry. Used for promote / display fade; does not silently rewrite Entry for paper grades. |
| **t0** | First honest post-migrate / post-launch market-cap print stored for the card. |
| **Multiple** | Last (or peak) mcap ÷ t0. Desk hunts ~5–50× runners; honest caps ~80× (beyond that → leftover / fake tape). |
| **paperV1** | Daily short list (action surface). Cap **3/chain/UTC day** (Sol + RH → display 6). Paper fills only. |
| **gated90 / wide paper** | Broad paper ocean for training (`PaperFill.line = gated90`). Sensor, not the 1–5 action surface. |
| **shadow / `paper_v1_shadow`** | Would-have / veto / SI / meme-quality Learn labels. Never opens a real paperV1 ticket path by itself. |
| **si_printer** | High-SI FOMO leftover printer Learn label (`v1 si-pr`). Shadow only; copycat still blocks. |
| **Thesis** | Frozen research tags / score used for soft floors. Hard tags: **github / dev / cto**. Meme-alone does not soft-pass. |
| **FOMO no-Hunt** | Name on FOMO Tokens→Trending but not on Hunt — definite Learn issue (`fomo_trend_no_hunt`). `FOMO_NO_HUNT_OPEN = False`. |
| **www FALSE-VETO** | Seeded copycat Learn evidence: mint `GAwhcphC…` skipped as copycat, later ~20–24×. Keep hard skip; learn separators. |
| **Production gate** | Bars in `docs/FORWARD.md` / `production_gate.py` before any live arm (thesis coverage, sample, hit quality, stability, ritual, risk). |
| **IMAGE_REV** | Running image stamp (`stack-v207`). `/health.image_rev` must match `launchfinder/image_rev.py`. |
| **API vs worker** | `LAUNCHFINDER_ROLE=api` serves desk/webhooks; `worker` runs poll/tape/bloom; `all` is local combined. |

---

## Architecture

```
                    ┌─────────────────────────────────────────┐
                    │              ingest doors               │
                    │ Pump · Helius WS · GMGN · Dex · FOMO    │
                    │ PONS · Bitquery · webhooks · …          │
                    └───────────────┬─────────────────────────┘
                                    │ store Token + Research
                                    ▼
┌──────────┐   score/features    ┌────────────────────────────┐
│  worker  │ ───────────────────▶│ desk / Hunt / FOMO boards  │
│ poll+tape│                     │ Entry + Live + tape bars   │
└────┬─────┘                     └─────────────┬──────────────┘
     │                                         │
     │ paper sync / reconsider                 │ decision freeze
     ▼                                         ▼
┌──────────────────┐                  ┌─────────────────────┐
│ paper book       │◀─────────────────│ Decision ledger     │
│ paper_v1         │   qualify/skip   │ entry_p, features,  │
│ gated90          │   promote/lock   │ outcomes, evidence  │
│ shadow / si_pr   │                  └──────────┬──────────┘
└────────┬─────────┘                             │
         │ labels / day-delta / miss-cohort       │
         ▼                                       ▼
┌────────────────────────────────────────────────────────────┐
│ Learn: FOMO no-Hunt · copycat FALSE-VETO · SI printers     │
│ sanity-loop · veto-retro · runners-retro · production-gate │
│ → next knob (one change, evidence-backed)                  │
└────────────────────────────────────────────────────────────┘

HTTP: FastAPI `launchfinder.app:app` (+ static desk at `/`, `/ui`, `/rh`, `/ui/rh`)
DB:   SQLite local default · Postgres via DATABASE_URL in real deploys
```

**API + worker split:** set `LAUNCHFINDER_ROLE=api` on the web service and
`worker` on the scanner so `/health` never blocks on Dex. Locally use `all`
(default).

**Ingest → desk/hunt/FOMO → decision → paper book → Learn** is the only happy
path. Cosmetics that do not improve selection or label honesty are out of scope.

---

## See → Decide → Label → Learn → Next knob

1. **See** — A door admits a mint; research fills socials, holders, liq, GMGN
   security (if keyed), GitHub, etc. Hunt cards appear in-window.
2. **Decide** — Entry `p_good` + Live path + thesis + hard vetoes decide
   paperV1 qualify / skip / shadow. Decisions freeze entry fields.
3. **Label** — Outcomes at 15m / 1h / 6h / 24h (and ongoing tape). Good ≈ real
   multiple from t0 with a live pool (desk hunts 5×+; win_multiple default 5).
4. **Learn** — Cohorts and retros (miss, FOMO no-Hunt, copycat would-have, SI
   printers, day-delta, sanity). Side keys only unless FEATURE_NAMES lockstep.
5. **Next knob** — One evidence-backed policy change (floor, separator, enrich
   job). Re-measure next UTC days. Do not arm live as a “knob.”

---

## Decision patterns

### First minutes / ~5m after migrate

- Prefer **holders, volume, liquidity, organic book** over weak social.
- Sol graduates often already have dozens–hundreds of holders; RH trench prints
  can be 2–6 wallets and still be normal (thin sorts under crowded books).
- First sellable book within ~20 minutes can be the honest fill (grace) — do not
  wait hours for a leftover pair and call it graduation.
- Hard veto needles include honeypot, wash, hijack, copycat spam, celebrity/brand,
  prior rugs, pasted tweet, generated website, start-high / pre-pumped (Sol),
  late chase (both chains unless quality bypass).

### Book over weak social

- FOMO no-Hunt separators explicitly prefer holders / liq / vol / organic_book /
  buy_pressure / top10 — **not** invented thesis tags.
- paper-miss cohort: book/holders/wallets beat social.
- Thesis soft floors require a **hard** tag (github / dev / cto). Meme-alone does
  not soft-pass paperV1.

### Desk lines (Entry thresholds)

| Scorer | Chain | Watch (lo) | Buy (hi) |
| --- | --- | --- | --- |
| `first_sight` | Sol | 0.10 | 0.14 |
| `first_sight` | Robinhood | 0.25 | 0.30 |
| `legacy` | both | 0.70 | 0.90 |

Always read a frozen Entry against the **scorer (+ chain)** stamped on the row.
Do not hardcode 0.70/0.90 against first-sight prints.

### paperV1 qualify (action surface)

- Sol: Live ≥ 0.50 (thesis soft floor 0.40 with hard tag).
- RH: Entry ≥ 0.40 (thesis soft 0.30 with hard tag).
- Immediate open / promote: Live ≥ 0.70, strong thesis, or queued peak ≥ 2×.
- Cap 3 per chain per UTC day; lock hour 23:00 UTC.
- Copycat → hard skip (`v1 veto|copycat` / `cc-fomo`); Learn only.

---

## Paper book paths

| Line / path | Role | Opens tickets? |
| --- | --- | --- |
| **`paper_v1`** | Daily short list (1–5 goal; 3+3 lanes) | Paper fills only |
| **`gated90`** | Wide training ocean | Wide paper fills (sensor) |
| **`paper_v1_shadow`** | Skips, vetoes, near-misses, SI, meme-quality | **No** (Learn labels) |
| **`si_printer` (`v1 si-pr`)** | High-SI FOMO leftover printers (≥20× band) | Shadow only |
| **Copycat hard skip** | Blocks paperV1 / gated / tickets | Never opens; would-have side keys only |

Related gates: `paper_gate.py` (chase, fresh-fat, enrich veto), `paper_enrich.py`
(provisional opens), `early_book.py` (`EARLY_BOOK_OPEN = False` — measure only).

---

## Learn cohorts

### FOMO no-Hunt (`fomo_trend_no_hunt`)

- Trigger: FOMO Tokens→Trending and **not** on Hunt (caught/on_desk without hunt,
  or true door miss).
- Write-once side keys + autopsy (≥32). Separators: holders/liq/vol/book.
- **`FOMO_NO_HUNT_OPEN = False`** — not a buy path.

### Copycat FALSE-VETO (www evidence)

- Mint `GAwhcphCqCv5bKHmCiN4VDdNWfbXJL4npmkc8L3Q9S9H` (world wide web).
- Labeled FALSE-VETO: copycat hard skip → later ~20–24× vs paper entry ~$204k /
  entry_p ~0.2712. Evidence row #1 in miss-cohort / Learn.
- Would-have true only when **all** of: FOMO rank ≤3, Hunt card, holders ≥26,
  liq ≥$5k, `v1_thesis_ok`.
- **`COPYCAT_LEARN_ARMED = False`**. Hard skip remains.

### SI runners (`high_si_learn` / `v1 si-pr`)

- FOMO desk leftover / high-multiple printers (min multiple 20×, 14d lookback).
- Shadow labels for Learn; **copycat still blocks** (`high_si_copycat_blocked`).
- Stale SI shadows demote via ledger helpers.

### Also on Learn

- Miss cohort / paper-miss join (≥5× after taxonomy skip).
- Veto retro, runners retro, day-delta, sanity loop + repair jobs.
- Production-gate progress card (read-only).

---

## Optimization target

**Win definition for this desk:** get **entries** on:

1. **Good trades (www-class)** — names the gate wrongly hard-skipped or missed
   that later printed real multiples with honest book; and
2. **Amazing runners (SI-class)** — high-SI FOMO/leftover printers worth studying.

**Not** the target: hold-forever, never-rug, maximize survival of every wide fill,
or “see more buys” by lowering Hunt lines. Paper capital is zero → explore
**policy** hard; do **not** explore execution until the production gate passes.

---

## File map

```
.
├── README.md                 ← you are here (public agent + human contract)
├── AGENTS.md                 ← goals / hard rules (skills paths softened)
├── .env.example              ← env NAMES only; copy to .env locally
├── .gitignore
├── Dockerfile                ← asserts stack-v207 + paper-safe constants at build
├── railway.toml              ← generic Docker healthcheck (no project IDs)
├── requirements.txt
├── docs/
│   ├── HOW_IT_WORKS.md       ← plain-English desk tour
│   ├── FORWARD.md            ← production path + gate
│   ├── APIS.md               ← key coverage (names / roles)
│   ├── BITQUERY.md
│   ├── RUNBOOK.md
│   ├── SELF_IMPROVE_PLAN.md / CYCLE_LOG.md / OFFLINE_SPRINT.md
│   ├── PAPER_V1_THESIS_AUDIT.md
│   ├── POST_DEPLOY_STACK_V207.md  ← this snapshot’s Learn ship notes
│   └── POST_DEPLOY_STACK_V149+    ← historical ship notes (may cite private host)
├── scripts/
│   ├── gate_status.py        ← operator CLI for loop freshness / gate
│   ├── verify_desk.py
│   ├── stamp_deploy.sh       ← writes .git_sha / .deploy_stamp (no account auth)
│   ├── cloud-agent-*.sh      ← local install/start helpers (no secrets)
│   └── check_fomo_trending.py / scan_*.py
├── tests/                    ← pytest suite (paper-safe)
└── launchfinder/
    ├── app.py                ← FastAPI desk + APIs
    ├── worker.py             ← poll / tape / paper sync loops
    ├── config.py             ← Settings from env (FOMO lookup default off)
    ├── risk.py               ← armed=False defaults
    ├── desk_lines.py         ← Entry thresholds per scorer/chain
    ├── ledger.py             ← decisions, paper sync, shadows, SI labels
    ├── image_rev.py          ← IMAGE_REV = stack-v207
    ├── .git_sha / .deploy_stamp
    ├── ingest/               ← doors (pump, dex, fomo, pons, bitquery, ws, …)
    ├── research/             ← GMGN, holders, FOMO API, GitHub, social, …
    ├── scoring/              ← first_sight, hunt, paper_v1, gates, Learn grains
    │   ├── paper_v1.py
    │   ├── paper_gate.py
    │   ├── copycat_learn.py      ← COPYCAT_LEARN_ARMED = False
    │   ├── fomo_trend_no_hunt.py ← FOMO_NO_HUNT_OPEN = False
    │   ├── high_si_learn.py
    │   ├── miss_cohort.py / production_gate.py / …
    │   └── si_cohort/            ← offline SI cohort artifacts
    └── static/               ← classic + test desk UI
```

**Not shipped publicly:** `.agents/` skills, `CONTRIBUTING.md`, private Origin
git remotes, Railway project linkage, live `.env` values.

---

## Key API endpoints for inspection

Base: local `http://127.0.0.1:8080` (or your host).

| Method | Path | Why look |
| --- | --- | --- |
| GET | `/health` | `image_rev`, `git_sha`, integrations, armed |
| GET | `/api/status` | Role + integration flags |
| GET | `/api/risk` | Confirm `armed=false`, paper_only |
| GET | `/api/hunt` | Live Hunt window |
| GET | `/api/tokens`, `/api/tokens/{mint}` | Book + research card |
| GET | `/api/paper`, `/api/paper/v1` | Wide paper + short list |
| GET | `/api/paper/v1/miss-cohort` | www evidence / copycat would-have |
| GET | `/api/paper/v1/si-pr-probe` | SI printer Learn probe |
| GET | `/api/paper/v1/production-gate` | Gate progress (read-only) |
| GET | `/api/paper/v1/day-delta` | Daily ritual delta |
| GET | `/api/paper/veto-retro` | Veto honesty |
| GET | `/api/fomo-trending`, `/api/fomo-trending/audit` | FOMO board + coverage |
| GET | `/api/fomo-alerts/flow`, `/traders` | Flow / trader scorecard (Learn UI) |
| GET | `/api/sanity-loop` | Automated honesty checks |
| GET | `/api/model` | Learned weights |
| GET | `/api/ledger/decisions` | Frozen decisions |
| POST | `/api/scan-now` | Force a poll (local/dev) |
| POST | `/webhooks/helius`, `/webhooks/migrate` | Ingest hooks when hosted |

UI: `/` and `/rh` (classic), `/ui` and `/ui/rh` (test desk).

---

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill YOUR keys; keep FOMO_USER_WALLET_LOOKUP=0
python -m launchfinder
# or: uvicorn launchfinder.app:app --host 0.0.0.0 --port 8080
```

Open `http://127.0.0.1:8080` (Sol) or `/rh` (Robinhood). Keyless mode works with
Pump.fun + DexScreener + public FxTwitter; add keys for holders, trenches, FOMO
keyed WS, Bitquery, GitHub headroom.

### Docker

```bash
docker build -t launchfinder .
docker run --env-file .env -p 8080:8080 launchfinder
```

`Dockerfile` build-asserts `IMAGE_REV == stack-v207`, `COPYCAT_LEARN_ARMED is False`,
risk `armed is False`, and FOMO wallet lookup helper returns False.

### Env vars (names only)

| Name | Role |
| --- | --- |
| `HELIUS_API_KEY` | Sol RPC/WS, holders, migrate speed |
| `SOLANA_RPC_URL` / `SOLANA_WS_URL` | RPC overrides |
| `GMGN_API_KEY` | Security, trenches, CTO / smart money (**no** private key) |
| `FOMO_API_KEY` | Trending / RHT / keyed alerts WS |
| `FOMO_USER_WALLET_LOOKUP` | **Keep `0`** |
| `BITQUERY_API_TOKEN` | RH Uni V4 Initialize stream |
| `GITHUB_TOKEN` | Repo search limits |
| `TWITTER_BEARER_TOKEN` | Unused unless `X_PAID_ENABLED=1` |
| `X_PAID_ENABLED` / `X_MONTHLY_CALL_BUDGET` | Paid X gate |
| `DATABASE_URL` | SQLite default; Postgres in serious deploys |
| `LAUNCHFINDER_ROLE` | `all` \| `api` \| `worker` |
| `ROBINHOOD_ENABLED` | Pause RH spend |
| `TELEGRAM_*` / `DISCORD_WEBHOOK_URL` | Alert plumbing |
| `WIN_MULTIPLE`, `POLL_SECONDS`, `ALERT_MIN_P`, … | Tuning |

**Never set here:** `GMGN_PRIVATE_KEY`, wallet signers, Railway account/project
tokens, any live broker credentials.

### Tests

```bash
python3 -m pytest -q --tb=short
# focused Learn / paper slice (from POST_DEPLOY_STACK_V207):
python3 -m pytest -q --tb=short \
  tests/test_copycat_learn.py \
  tests/test_copycat_veto_shadow.py \
  tests/test_paper_miss_learn.py \
  tests/test_paper_v1.py::test_desk_shows_the_side_list \
  tests/test_runners.py::test_health_exposes_image_rev \
  tests/test_rh_board.py::test_test_desk_pages
```

---

## What NOT to do

- Invent thesis tags or paste social fluff onto frozen Learn snaps.
- Enable `FOMO_USER_WALLET_LOOKUP` “to see more.”
- Arm live / add swap keys / auto-buy before production gate + explicit KEEP on
  **private** Origin (not this public repo).
- Commit `.env`, tokens, Railway project IDs, or private deploy credentials.
- Expand `FEATURE_NAMES` casually or reverse NINA / RH MEME / GS uncapped fixtures
  without an explicit operator ask.
- Optimize for more wide-book buys or Hunt cosmetics over short-list honesty.
- Treat older `docs/POST_DEPLOY_*` production host curls as credentials — they are
  private Origin verify recipes; this public clone has no deploy auth.

---

## Current snapshot note

| Field | Value |
| --- | --- |
| Snapshot | public paper-only export |
| `IMAGE_REV` | `stack-v207` |
| Source git (stamp) | `004fed0bd54d` |
| Deploy stamp | `20261002T045922Z` |
| Learn focus | copycat FALSE-VETO (www) + FOMO no-Hunt + SI printers |
| Live arm | **off** |
| FOMO wallet lookup | **off** |
| Private Origin | `dave-1/new_launch_finder` (not this repo) |
| Intended public remote | `https://github.com/Research-cmd-dev/new-launch-finder-public` |

Based on **stack-v207 paper Learn**. Public clone may lag private Origin. Prefer
this README’s HARD LOCKS over any stale doc that implies live trading.

Further reading: [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md),
[docs/FORWARD.md](docs/FORWARD.md), [docs/APIS.md](docs/APIS.md),
[docs/POST_DEPLOY_STACK_V207.md](docs/POST_DEPLOY_STACK_V207.md), [AGENTS.md](AGENTS.md).
