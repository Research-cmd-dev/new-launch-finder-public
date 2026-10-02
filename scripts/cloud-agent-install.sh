#!/usr/bin/env bash
set -euo pipefail
mkdir -p data
if [ -f requirements.txt ]; then
  python3 -m pip install --user -r requirements.txt
fi
