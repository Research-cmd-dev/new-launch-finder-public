# stack-v186 — FOMO trader wallet (Learn)

## What shipped

- `fomo_alert_events.trader_wallet` — parsed from WS alert payloads when FOMO exposes wallet fields; optional keyed REST `GET /v2/users/id/{userId}` when `userId` is present and WS row has no wallet (cached, paper-safe Learn only).
- `/api/fomo-alerts/recent` and `/api/fomo-alerts/traders` include `trader_wallet` (best-known per trader on scorecard).

## Field discovery (fomoapi.io)

| Source | Trader wallet |
|--------|----------------|
| `/ws/alerts` feed (documented) | Often **absent** — `trader` handle + `userId` only |
| Same stream (observed variants) | `traderWallet`, nested `trader.wallet` / `trader.wallets.solana|evm` |
| `/ws/trades` | `trader.wallet` (different stream; not alerts path) |
| REST `GET /v2/users/id/{userId}` | `wallets.solana`, `wallets.evm` |

Parser does **not** treat top-level `address` as trader wallet when it matches `tokenAddress`.

## Post-deploy

- `GET /health` → `image_rev` == `stack-v186`
- `GET /api/fomo-alerts/recent` → rows may show `trader_wallet` when ingest captured or REST resolved
- No paper fill / arm changes
