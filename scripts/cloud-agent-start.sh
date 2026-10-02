#!/usr/bin/env bash
set -euo pipefail
mkdir -p data
if [ ! -f launchfinder/app.py ]; then
  exit 0
fi
if curl -sf http://127.0.0.1:8080/health >/dev/null; then
  exit 0
fi
export BACKFILL_LIMIT="${BACKFILL_LIMIT:-0}"
export POLL_SECONDS="${POLL_SECONDS:-30}"
export PORT="${PORT:-8080}"
nohup python3 -m uvicorn launchfinder.app:app --host 0.0.0.0 --port "$PORT" \
  > /tmp/launchfinder.log 2>&1 &
i=0
while [ "$i" -lt 30 ]; do
  if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null; then
    exit 0
  fi
  i=$((i + 1))
  sleep 1
done
echo "launchfinder failed to become healthy" >&2
tail -50 /tmp/launchfinder.log >&2 || true
exit 1
