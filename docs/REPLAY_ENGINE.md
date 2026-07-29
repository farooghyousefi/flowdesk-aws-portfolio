# Replay Engine

The replay engine loads registered DBN sessions and keeps original event order. State becomes externally consistent at `F_LAST` event-group boundaries.

Controls include play, pause, reset/session restart, event-group step, trade step, seek, first-trade jump, high-volume jump, and speeds `0.25x`, `0.5x`, `1x`, `2x`, `5x`, `10x`, `50x`, and `MAX`.

Seeking resets and deterministically rebuilds state from the same source. The complete demo primes through the natural snapshot and pauses at the first post-snapshot group. The partial demo primes at the first trade so chart and tape are visible while book reliability remains explicitly restricted.

```bash
npm run replay:demo
```

The command imports the complete session if necessary and prints `http://localhost:3000/replay`.
