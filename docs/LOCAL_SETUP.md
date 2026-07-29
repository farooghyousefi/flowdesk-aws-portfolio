# Local Setup

## Prerequisites

- macOS with Node.js, npm, and Python 3
- Optional Databento key in local `.env.local`
- Existing DBN files and their adjacent manifest files under `data/databento/raw/MES`

The secret file must be ignored by Git and have mode `0600`:

```bash
cd flowdesk-aws
stat -f "%Sp" .env.local
git check-ignore .env.local
chmod 600 .env.local
```

## Install And Import

```bash
npm run local:setup
```

This installs Node packages, creates `.venv`, installs Python packages, creates local data directories, migrates SQLite, verifies DBN manifests and SHA-256 hashes, registers sessions, writes Parquet batches, and refreshes DuckDB views. It does not print secrets or download market data.

## Start

```bash
npm run dev:trading
```

- Frontend: `http://localhost:3000`
- Market service: `http://127.0.0.1:8787`
- Default mode: paused replay with the complete snapshot session

Both services bind to loopback and stop together with `Ctrl+C`.
