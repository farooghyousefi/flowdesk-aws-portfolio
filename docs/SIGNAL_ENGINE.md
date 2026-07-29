# Signal Engine

## Purpose

The signal engine converts current feature and risk state into an auditable manual decision. Its outputs are `LONG`, `SHORT`, or `NO_TRADE`; none of them routes an order.

## Required Inputs

- complete and eligible market-data state
- active, promoted strategy version
- valid bid/ask and bounded spread
- current microstructure/context feature snapshot
- explicit entry, stop, invalidation, and MES risk sizing
- passing challenge and daily-risk rules

If any required input is unavailable or stale, the result is `NO_TRADE` with structured reason codes. A spread wider than two MES ticks blocks a trade.

## Lifecycle

1. A qualifying decision creates a signal snapshot and `SIGNAL_CREATED` audit event.
2. Replay updates the signal from current and past events only.
3. Data degradation, strategy change, invalidation, cooldown, or risk violation turns it into `NO_TRADE` and records `SIGNAL_INVALIDATED`.
4. The UI localizes reason labels and displays the immutable strategy/model/data references.

## Position Sizing

Sizing uses the centralized MES specification: `0.25` index-point tick and `$5` per point per micro contract. Contract count is capped by stop distance, configured dollars at risk, challenge limits, and instrument limits. Displayed sizing is guidance for a manual order, not an execution instruction.

## Challenge Rules

The guard checks account target, maximum loss, daily stop, per-trade risk, maximum trades, consecutive losses, allowed session/instrument, news and overnight restrictions, consistency, minimum trading days, and configured scaling rules. A failed rule hard-blocks the signal.

## What It Does Not Prove

A signal score is not a probability of profit. Heuristic labels such as absorption, iceberg, exhaustion, or institutional activity describe observed patterns and never identify actor intent with certainty. Promotion requires separate chronological validation and forward paper evidence.
