# Bitquery Pro — turn on the RH V4 door

The listener is already written. You only set the token on Railway. Do not paste it in git or chat.

Token: [account.bitquery.io](https://account.bitquery.io) → generate after the Pro upgrade ([docs](https://docs.bitquery.io/docs/authorization/how-to-generate/)). An old Personal token can keep the old point cap.

## 1. Put the token on both services

Railway project `new-launch-finder` / env **production** (the canvas with Postgres + the two app boxes).

| Service | Why |
|---|---|
| **new-launch-finder-worker** | Runs `listen_rh_pools` (this is the one that must have it) |
| **new-launch-finder** (API) | So `GET /health` shows `bitquery: true` |

Variable name: `BITQUERY_API_TOKEN`  
(empty stub already exists on the worker)

### Dashboard clicks (easiest)

Stay on **production** (top-left of the canvas).

1. Click the **new-launch-finder-worker** box (the one on the right).
2. Open the **Variables** tab (not Settings).
3. If `BITQUERY_API_TOKEN` is already listed, click it / the pencil and paste your token as the value. If it is missing, click **New Variable**, name `BITQUERY_API_TOKEN`, paste the token.
4. Railway stages the change. Open the **Review changes** / deploy banner at the top and **Deploy**. Wait until that service is online again.
5. Click the canvas background (or the project name) to go back, then click the **new-launch-finder** box (API, left, under Postgres). Repeat steps 2–4 with the **same** name and token.

Optional: after it is saved, the ⋮ menu on the variable → **Seal** so the value cannot be read back.

Do **not** put the token on Postgres.

Dashboard: Variables → set `BITQUERY_API_TOKEN` → Deploy.  
Or locally, with the token in your shell only:

```bash
# worker first
railway variables --set "BITQUERY_API_TOKEN=$BITQUERY_API_TOKEN" \
  --project abedfa79-deea-41e2-979a-be422ca629a2 \
  --environment 05c2a5f7-07a1-471d-884b-36eb9cd9b915 \
  --service 0404949f-5042-4dea-bceb-2e950d6dbf0f

# API (health flag only)
railway variables --set "BITQUERY_API_TOKEN=$BITQUERY_API_TOKEN" \
  --project abedfa79-deea-41e2-979a-be422ca629a2 \
  --environment 05c2a5f7-07a1-471d-884b-36eb9cd9b915 \
  --service 23a8cfe7-40d3-467d-b006-4613dcfe00da
```

Do not `railway up` a new image for this. A variable change is enough.

## 2. Prove it

1. Worker deploy log: `subscribed to Robinhood Uni V4 Initialize`  
   (not `no BITQUERY_API_TOKEN — Robinhood Uni V4 stream disabled`)
2. `GET https://new-launch-finder-production.up.railway.app/health` → `"bitquery": true`
3. A native-ETH / off-quote RH open should ingest as `source=rh_bitquery` before it hits a Dex quote page.

Handshake / `stream error` in worker logs usually means a stale token or a leftover IDE subscription eating the Pro stream-minute cap. Revoke old tokens; check Account → Subscriptions.

## 3. What this uses (and what not to add yet)

One 24/7 GraphQL subscription: Robinhood Uni V4 `Initialize` on PoolManager `0x8366…0951`. That is all RH v4, not only pools.trade — BOOTS / ROUTE / LEGS class.

Pro: ~100k stream-minutes. One always-on stream ≈ 43k/month. Do **not** add a second live stream (Sol DEXTrades, Kafka, gRPC) until this one stays up a week.

HTTP query points (1M/mo) are for later honesty lookups (RWA-class last vs real 1h volume). Not this step.

Code: `launchfinder/ingest/bitquery.py`. Local: same var in `.env` (see `.env.example`).
