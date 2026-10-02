# Launch Finder — deploy / rollback / kill runbook

Paper only until the production gate in [FORWARD.md](FORWARD.md) passes.
Live arm stays **off** (`GET /api/risk` → `armed: false`).

---

## Deploy both services

Project: `new-launch-finder` · environment: `production`

| Service | Role |
|---|---|
| `new-launch-finder` | API / desk |
| `new-launch-finder-worker` | ingest, Hunt tape, paper sync, batch fit |

```bash
# From a clean tip tree with IMAGE_REV bumped (lockstep — see feature-map).
railway up --detach -y \
  --project abedfa79-deea-41e2-979a-be422ca629a2 \
  --environment production \
  --service new-launch-finder

railway up --detach -y \
  --project abedfa79-deea-41e2-979a-be422ca629a2 \
  --environment production \
  --service new-launch-finder-worker
```

Wait for **SUCCESS** on both (not just container start). Then:

```bash
curl -sS https://new-launch-finder-production.up.railway.app/health | jq .image_rev,.risk
python3 scripts/verify_desk.py
```

`image_rev` must equal `launchfinder/image_rev.py`. Risk should show
`armed: false` and `kill_switch: false` unless you tripped it.

---

## Rollback IMAGE_REV

1. Checkout the prior known-good commit / tip SHA.
2. Confirm `IMAGE_REV` in that tree is the previous `stack-vN`.
3. Redeploy **both** services with `railway up` as above.
4. Prove `/health.image_rev` matches the rolled-back rev.
5. Do **not** `railway config migrate`.

Railway dashboard → service → Deployments → Rollback is OK if the prior
build is still listed; still verify `/health.image_rev` after.

---

## Kill paperV1 opens (emergency)

Freeze new short-list opens without a redeploy:

```bash
# Trip kill switch + skip today's queued paperV1 rows
curl -sS -X POST https://new-launch-finder-production.up.railway.app/api/risk \
  -H 'content-type: application/json' \
  -d '{"kill_switch": true, "flush_queue": true, "note": "operator kill"}'
```

Effects:

- No new paperV1 queue / midday promote / lock opens
- Queued rows skipped with reason `v1 kill_switch`
- Live arm forced off (`armed` cannot stay true while kill is on)

Resume (still paper only):

```bash
curl -sS -X POST https://new-launch-finder-production.up.railway.app/api/risk \
  -H 'content-type: application/json' \
  -d '{"kill_switch": false, "note": "resume paper"}'
```

Pause one chain only:

```bash
curl -sS -X POST .../api/risk \
  -H 'content-type: application/json' \
  -d '{"chain_pause": ["robinhood"]}'
```

---

## UTC-day report

```bash
curl -sS 'https://new-launch-finder-production.up.railway.app/api/paper/v1/report'
curl -sS 'https://new-launch-finder-production.up.railway.app/api/paper/v1/report?day=2026-09-27'
curl -sS 'https://new-launch-finder-production.up.railway.app/api/paper/v1/runners-retro?chain=robinhood'
curl -sS 'https://new-launch-finder-production.up.railway.app/api/year-winners?chain=sol'
```

Returns both chains, skip reasons, tag counts, thesis weights, risk, and a
one-line `text` summary for Learn / Telegram when alerts are later armed.

---

## Do not

- Arm live (`armed: true`) before the FORWARD production gate
- Lower Hunt lines to invent fills
- Deploy only one of the two Railway services
- Force-push tip / amend IMAGE_REV history
