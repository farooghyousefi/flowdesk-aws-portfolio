# Market Replay

Replay uses the same Rule Engine as live mode.

Rules:

- only closed bars can create confirmed signals
- no look-ahead bars
- replay frames are sorted by bar end time
- each replay frame stores the rule evaluation at that point in time

The web UI includes a Replay panel with pause, step, and speed controls. The current implementation replays in-memory live bars; SQLite-backed replay persistence is represented in the Prisma schema and is the next integration step.
