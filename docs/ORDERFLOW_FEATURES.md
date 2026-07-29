# Orderflow Features

The feature engine is deterministic and uses Databento action semantics:

- Only action `T` contributes trade volume; fill action `F` is never counted a second time.
- Aggressive buy/sell volume, delta, cumulative delta, pace, volume/second, average size, and VWAP
- 1m, 5m, and 15m OHLCV bars with buy/sell volume, delta, trade count, cumulative delta, and VWAP
- Footprint by price with bid/ask volume, delta, configurable imbalance, and stacked imbalance
- Pulling, stacking, execution, modify, and cancel accounting; initial snapshot adds are excluded
- Liquidity heatmap from server-side book buckets rendered on Canvas
- Session volume profile with POC and value area
- Session open/high/low, VWAP, and explicit missing previous-session context
- Multi-timeframe trend/range/compression/expansion observations

Bars expose an explicit `completed` flag. Setup minimum-bar and structure rules consume completed bars only. The UI labels the open footprint as a forming 1-minute bar and reports start, elapsed, remaining time, completion, and completed 1m/5m/15m counts; internal replay rendering buckets are not presented as completed market bars.

Absorption and iceberg outputs are labeled heuristic candidates with confidence and merged reason codes. Candidates require configurable observations, elapsed time, and aggressive volume; snapshot adds remain excluded. Results are deduplicated by side and price within the active window, ranked from volume, displacement, replenishment, persistence, and data completeness components, and limited to the configured top count. They do not claim knowledge of hidden intent. Complete-book-dependent setup rules are restricted whenever book reliability is not guaranteed.
