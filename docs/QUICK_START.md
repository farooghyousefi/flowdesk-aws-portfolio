# Quick Start

```bash
cd flowdesk-aws
npm run local:setup
npm run dev:trading
```

Open `http://localhost:3000`.

- **Data Planner** compares Full L3, Economy, and Chart Context. It starts with no estimate and never downloads on page load.
- **Backtest** creates Practice, Pilot, or Locked protocols from local complete sessions.
- **Replay** loads the complete `MESU6` demo paused. The partial session remains visibly restricted.

Safe metadata-only example:

```bash
npm run dataset:estimate -- --date 2026-07-14 --replay-start 15:00 --replay-end 16:30
```

Estimate costs nothing. Estimate does not download market data. Only the reviewed confirmation dialog or explicit `dataset:submit` command can create a paid request.

Stop cleanly with `Ctrl+C`.
