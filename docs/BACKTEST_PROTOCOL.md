# Backtest Protocol

Backtest Plan selects strategy, instrument, complete local sessions, Practice/Pilot/Locked mode, starting balance, risk, daily trade limit, commission, exchange/clearing cost, entry/exit/stop slippage, fill rule, and maximum position.

The lifecycle deliberately separates protocol definition, active protocol, plan-to-session assignment, blind replay run, and global application settings. A Locked definition or a `Locked Test` assignment does not lock the application. The single lock contract becomes true only while an active Locked protocol has an active Locked blind replay run for its assigned session. Exiting that run preserves plans, assignments, audit events, and completed trades while making Settings and Risk editable again.

New Practice, Pilot, and Locked plans always receive a new protocol ID and a protocol-scoped strategy hash. A locked session assignment is never moved silently. `Clone session assignment into Practice` creates a second assignment to the same local raw file, preserves the original split, writes an audit event, and marks the Practice assignment `reused`, `contaminated`, and optionally `ui_practice_only`.

The exact Browser-QA artifact family identified by strategy hash `b448776be63828d3434f50007e424dc8ebf73aba1b6d13b9f9ea8a1674ac95a2` is migrated idempotently to `test_artifact` and `ARCHIVED`. It remains visible under Archived/Test Plans with its original session split and audit trail. No hash-prefix or broad mode migration is used.

The Candidate Scan calls the same deterministic `ReplayEngine` and `setup_decision` used by interactive replay. It logs `trade_ready`, `wait`, and `blocked` counts and timestamps. It does not label candidates profitable.

Each stored timestamp can be opened through an audited Candidate Jump. This dedicated path rebuilds state exactly to the candidate event group and does not expose a later event; ordinary future seek remains locked.

Conservative results use actual planned entry/exit movement minus round-trip fees and configured slippage. Limit fills require trade-through or remain unresolved when queue evidence is absent. Reports include trades, wins/losses/breakeven, win rate, average win/loss R, expectancy R/USD, profit factor, drawdown, consecutive losses, MAE, MFE, holding time, fees, slippage, net result, side, time, setup, and data-quality breakdowns.

The required labels are descriptive: `Insufficient sample`, `Negative expectancy`, `Positive observed expectancy`, and `Requires out-of-sample validation`. The application never claims guaranteed profitability.

Targets are 10 Practice trades, 30 Pilot trades, at least 100 Locked trades, and 20 Forward Paper trading days.

Before this repair, the local SQLite database was copied to `data/backups/trading-assistant-pre-master-repair-20260715-190837.sqlite3`; `PRAGMA integrity_check` returned `ok`.
