# Troubleshooting

Start with:

```bash
npm run local:doctor
curl -fsS http://127.0.0.1:8787/health | jq
npm run replay:demo
```

| Symptom | Likely cause | Check | Solution |
|---|---|---|---|
| Port occupied | Another local process is running | `lsof -nP -iTCP:3000 -iTCP:8787 -sTCP:LISTEN` | Stop the old process or let `dev:trading` choose its reported frontend port |
| Python venv missing | Setup was not run | `test -x .venv/bin/python` | `npm run local:setup` |
| Databento key missing | `.env.local` absent or empty | `npm run local:doctor` | Add the key to `.env.local`; never put it in browser settings |
| File not found | Wrong raw-data path | `find data/databento/raw -name '*.dbn.zst'` | Use the absolute registered path |
| Corrupt DBN or hash failure | File differs from manifest | `npm run data:import -- --file /absolute/path/file.dbn.zst` | Restore the original DBN and matching manifest; do not rewrite raw data |
| Backend does not start | Python dependency or SQLite issue | `.venv/bin/python -m apps.market_service.service` | Rerun setup, then inspect the first local traceback |
| Frontend does not start | Node install/build issue | `npm run build` | `npm install`, then rebuild |
| Replay appears stopped | It is paused or at end | `curl -fsS http://127.0.0.1:8787/replay/state | jq '.playing,.progress'` | Reset or move the seek control, then play |
| High CPU | `MAX` replay or large seek | Inspect selected speed in Replay | Pause and use `1x` to `10x` |
| High RAM | Large partial session is loaded | `ps -o pid,rss,command -ax | grep market_service` | Return to complete demo and restart the service |
| Partial Book warning | File lacks a complete starting snapshot | Open Data Health | Use it for chart/tape only; do not trust L3/BBO rules |
| No live data | Live adapter is intentionally disabled | `curl -fsS http://127.0.0.1:8787/live/health` | Continue in Replay; live access is not part of this MVP |
| MBP-10 cost blocked | Estimate stale or above limit | `npm run databento:verify:estimate -- --mbo-file ... --limit 1000` | Do not download until explicitly authorized and inside budget |
| SQLite locked | Another service/import owns the DB | `lsof data/app/trading-assistant.sqlite3` | Stop duplicate local services and retry |
| WebSocket disconnected | Market service stopped | `curl -fsS http://127.0.0.1:8787/health` | Restart with `npm run dev:trading` |

## Data Planner And Backtest

## Estimate blocked

- Confirm the date is in the available Databento range.
- Check that replay times form exact ten-minute blocks.
- Review request, daily, weekly, and monthly remaining limits.
- Full L3 above `$1.00` is blocked by default; use the optimizer or deliberately change the server-side limit only after review.

## Estimate expired

Estimates expire after ten minutes. Run a fresh estimate; never reuse the old confirmation phrase.

## Retained `.part`

A failed download keeps its `.part` location in the job details. Inspect or remove it deliberately before retrying. The app will not overwrite it.

## Locked settings

Finish or leave the active Locked protocol before changing orderflow or risk settings. Direct API updates return HTTP `423` while Locked mode is active.

## Services

Run `npm run local:doctor`. Ports `3000` and `8787` must be available before `npm run dev:trading`.
