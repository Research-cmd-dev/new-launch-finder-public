# Launch Finder — API / key coverage

Last checked: 2026-09-27 (live `stack-v111` deploy target; paper trading only).

## What we have (production)

| Integration | Worker | API service | Role for 1–5 short list |
|---|---|---|---|
| `HELIUS_API_KEY` | yes | yes | Sol RPC/WS, holders, migrate speed |
| `SOLANA_RPC_URL` / `SOLANA_WS_URL` | yes | (falls back to Helius) | Same |
| `GMGN_API_KEY` | yes | yes | Security, trenches, **CTO flag**, smart money |
| `BITQUERY_API_TOKEN` | yes | yes | RH Uni V4 Initialize door (**realtime plan** — see below) |
| `FOMO_API_KEY` | yes | yes | FOMO trending / RHT doors + keyed `/ws/alerts` social flow (WS messages free once connected; REST keyed alerts still cost credits). Learn: `GET /api/fomo-alerts/flow`, `GET /api/fomo-alerts/traders` (ranked scorecard — does not arm or open fills). Desk **Learn** tab → **FOMO traders** row renders the scorecard (paper-only UI). |
| `GITHUB_TOKEN` | yes | yes | Repo age/commits/contributors → thesis |
| `TWITTER_BEARER_TOKEN` | yes | yes | Present; paid X path off (`x_api: false`) — FxTwitter used |
| `TELEGRAM_*` / `DISCORD_WEBHOOK_URL` | worker | — | Alert plumbing; `alerts: false` until short list proves |
| Postgres `DATABASE_URL` | yes | yes | Ledger / paperV1 |

Public / keyless still used: Pump.fun API, DexScreener, FxTwitter, Blockscout (RH).

## Bitquery plan vs product (important)

Bitquery **has** deep historical cubes (DEX trades, OHLCV, holders, etc. —
Sol `DEXTradeByTokens` archive from mid-2024; EVM with archive add-on). Our
current `BITQUERY_API_TOKEN` does **not**:

| Endpoint | Behavior on our token |
|---|---|
| `streaming.bitquery.io` | Realtime window only — fixed past windows ≈**>6–12h** return empty; live RH Initialize WS works |
| `graphql.bitquery.io` | **403** `plan only allows "realtime"` on `archive:Solana:DEXTradeByTokens` / robinhood / Trading |

So: use Bitquery for **live** RH door + optional first-hour candles on fresh
opens. Year-old runner 0–24h backfill needs an **archive add-on** (or Data
Lake). Until then: GMGN/Gecko day OHLCV + Helius first-hour Sol swaps.

## Optional — not required to start short-list learning

| Key / API | Why consider later |
|---|---|
| Bitquery **archive** add-on | Honest 0–24h DEXTradeByTokens / OHLCV for last year’s runners |
| `OPENAI_API_KEY` | LLM thesis write-up on the 1–5 only (reserved in `.env.example`) |
| Paid X mentions | Enrich short-list social only; not needed for GitHub/CTO ranking |
| Second Helius key / higher plan | Sol holder refresh headroom when `helius_capped` |
| Extra market data (Birdeye, etc.) | Not wired; GMGN + Dex cover current thesis |

## Explicitly do **not** add yet

- Any swap / trading private key (`GMGN_PRIVATE_KEY`, wallet signers)
- Auto-buy brokers — paperV1 only until forward proof

## Health checks

- `GET /health` → `helius`, `gmgn`, `bitquery`, `fomo`, `ws`, `github`, `x_api`, `alerts`, `image_rev`
- `GET /api/status` → `integrations.*` (includes `github`)
