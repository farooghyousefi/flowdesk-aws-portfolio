# Research Platform / Forschungsplattform

## Scope

The Research Lab evaluates deterministic normalized market events from registered local sessions. It is built for evidence generation and governance, not for broker execution or profitability claims.

## Event And Feature Pipeline

All historical, replay, and future live adapters emit the same normalized event contract. Ordering is deterministic by event timestamp, receive timestamp, sequence, and stable source index. Features are computed incrementally and may use only the current or earlier event.

The microstructure state includes BBO, spread, mid, microprice, queue and depth imbalance, depth slope/concentration, persistent walls, churn, liquidity migration, add/cancel/modify ratios, order lifetime, depletion/replenishment, aggression, trade pace, large clusters, delta momentum, volatility, sweeps, bursts, and a labeled exhaustion heuristic.

Context features include session and anchored VWAP, opening range, one-minute ATR, realized volatility, trend strength, regime, liquidity regime, session phase, time to cash open, and volume nodes. Missing overnight or previous-session history is explicitly unavailable and is never fabricated.

## Event Backtester

- decisions are evaluated in event order with no future reads
- MES tick size, point value, commissions, and slippage are centralized
- fill assumptions are explicit: optimistic runs cannot be promoted
- ambiguous queue position is never invented
- every trade stores decision time, order/fill details, signal score, invalidation, feature/state snapshots, data fingerprint, and strategy/model versions

## Validation Lifecycle

1. Development: strategy construction and diagnostics.
2. Validation: chronological split with a purge window around boundaries.
3. Locked test: untouched out-of-sample evidence.
4. Forward paper: manual observation on newly arriving data.
5. Manual pilot: optional small-size execution outside Flowdesk.

Promotion requires acceptable data quality, a realistic or stressed fill model, enough evidence, and stable validation metrics. Promotion, rejection, invalidation, and rollback append audit events. Activating a strategy does not activate broker trading.

## Persistent Jobs

Estimate jobs and research jobs are stored in `data/app/trading-assistant.sqlite3`. Research jobs support queueing, cancellation, pause, and resume at chunk boundaries. Resume deterministically restarts the source and records checkpoint metadata; book state is not serialized between processes.

## Model Governance

The included model registry is deliberately conservative: a baseline version is registered, versions are auditable, and rollback is explicit. The platform does not silently train on locked data or promote a model based only on in-sample performance.

## Data Providers

- Historical provider: registered immutable local sessions
- Replay provider: deterministic paced playback of normalized events
- Live provider: interface and health state only; signals disable on gaps or delay and require snapshot/resync

Live states are `CONNECTING`, `LIVE`, `DELAYED`, `DEGRADED`, `DISCONNECTED`, and `RESYNCING`. The current project has no enabled live subscription and no broker order route.

## Reproducibility

Keep raw DBN unchanged. A research result is tied to source SHA-256/data fingerprint, split policy, configuration, costs, strategy hash, model version, and code-level feature contract. Before sharing a result, run:

```bash
npm run test:all
npm run build
```

For known restrictions, see [LIMITATIONS](LIMITATIONS.md) and [BACKTEST_PROTOCOL](BACKTEST_PROTOCOL.md).
