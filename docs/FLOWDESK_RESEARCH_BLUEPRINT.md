# Flowdesk Research Blueprint v1

This document is the binding scope for the Flowdesk MES research system. It prevents the application from silently reverting to a single baseline strategy or treating one high-event-count session as sufficient evidence.

## Operating constraints

- Instrument: MES futures.
- Execution: manual orders only. Flowdesk does not place broker orders.
- Research may output `LONG`, `SHORT`, `WAIT`, or `NO_TRADE`.
- `LONG` and `SHORT` are permitted only for replay/paper use until every validation and context gate passes.
- No result is a guarantee of profitability.
- No paid market-data request is created by the research engine or this update.

## Required evidence layers

### 1. Market structure

The feature contract must include, where the historical coverage permits:

- cash-session opening range, based on 09:30–10:00 America/New_York;
- overnight high and low;
- VWAP and distance from VWAP;
- 1-minute, 5-minute, and 15-minute structure state;
- session phase and trend/regime context;
- previous-day high, low, close, volume profile, POC, and value area after multi-day context data is available.

### 2. Full L3 order flow and microstructure

The research pass consumes the Databento MBO stream and maintains a reconstructed order book. Strategy candidates may use:

- aggressive signed volume and delta momentum;
- queue imbalance and top-of-book liquidity;
- spread and book completeness;
- sweeps, replenishment, exhaustion, and absorption candidates;
- trade velocity and price response to aggressive volume.

### 3. Economic calendar, point in time

Economic events are keyed by `scheduled_at` and `published_at`. The system must never reveal an actual value before `published_at`.

High-impact USD events create a default signal block from ten minutes before until five minutes after the scheduled release. The gate is explicit in the signal evidence.

### 4. News, point in time

News becomes available only at `published_at`. High-relevance breaking news creates a default two-minute signal block. Headlines, relevance, sentiment, and provider are retained for auditability.

Historical calendar and news coverage must span the complete research interval. A coverage declaration without imported rows is not accepted as complete.

### 5. Regime detection

Strategies are evaluated by regime, not only in aggregate. Initial supported classifications include momentum, mean reversion, and chop, plus session phase. A strategy that earns only in one narrow regime is diagnosed as regime-dependent rather than promoted as a general edge.

## Initial strategy families

The bounded strategy search evaluates economically distinct candidates from one event-stream pass:

1. MES L3 Momentum
2. MES Pullback / Retest
3. MES VWAP Mean Reversion
4. MES Opening Range Breakout
5. MES Absorption Reversal

The search is deliberately bounded and auditable. It is not an unrestricted optimizer. Each candidate uses the same event stream, fill model, fees, and chronological split.

## Failure diagnosis

A rejected candidate must state one or more reasons, including:

- no trades or too few trades;
- negative net expectancy;
- trading costs consume the gross edge;
- excessive drawdown;
- instability across chronological segments;
- dependence on a narrow market regime;
- missing economic-calendar coverage;
- missing news coverage;
- too few independent sessions.

This distinction matters: a strategy can be structurally weak, overfit, cost-sensitive, or simply mismatched to the observed regime.

## Data and validation target

The initial binding target is six months and at least 100 independent complete L3 sessions:

- 60 development sessions;
- 20 chronological validation sessions;
- 20 untouched locked-test sessions.

The partitions must be chronological. Locked data must not influence parameter selection. After the initial split, research should use walk-forward evaluation so that performance is measured across changing market conditions.

A large event count from one day is still one independent day. It is useful for engineering and candidate generation, but insufficient for a validated trading signal.

## Signal contract

A signal record must contain:

- state: `LONG`, `SHORT`, `WAIT`, or `NO_TRADE`;
- strategy family and validated version;
- entry, stop, target, MES contract count, and dollar risk when directional;
- market regime and market-structure evidence;
- L3/order-flow evidence;
- economic-event and news risk;
- supporting and opposing evidence;
- invalidation criteria;
- confidence and data-quality status;
- validation status and source-data fingerprint.

`WAIT` is the required state when a setup may be forming but confirmation, context, or validation is insufficient. `NO_TRADE` is required when a hard risk, data-quality, event, or validation gate blocks execution.

## Current limitations after this update

- The current local library contains only approximately one usable complete L3 day plus a tiny snapshot. It does not satisfy the six-month target.
- Historical economic-calendar and news files are not bundled. They must be supplied with accurate point-in-time timestamps and declared coverage.
- Previous-day levels and multi-day profiles become reliable only after multiple consecutive sessions are imported.
- Directional signals remain replay/paper/manual-only until the validation gates pass.
