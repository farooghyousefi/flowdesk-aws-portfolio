#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

command -v node >/dev/null || { echo "FAIL Node.js fehlt."; exit 1; }
command -v npm >/dev/null || { echo "FAIL npm fehlt."; exit 1; }
command -v python3 >/dev/null || { echo "FAIL Python 3 fehlt."; exit 1; }

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install --disable-pip-version-check -q -r apps/connectors/databento/requirements.txt
npm install --silent
.venv/bin/python -m apps.market_service.cli setup
