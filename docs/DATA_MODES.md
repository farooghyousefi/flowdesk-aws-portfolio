# Data Modes

## Full L3 Research

Full L3 uses MBO from UTC midnight so the natural start-of-day snapshot can initialize the order book. It is more expensive but required for complete historical DOM, queue structure, pulling/stacking, heatmap, and L3 candidate analysis. The UI only labels it complete after snapshot and integrity validation.

## Orderflow Economy

Economy uses trades plus one-minute bars around the visible replay window. It supports tape, aggressive volume, delta, footprint, VWAP, volume, and chart structure. It cannot provide complete DOM, queue position, L3 iceberg confirmation, or complete pulling/stacking conclusions.

## Chart Context Only

Chart Context uses one-minute bars. It supports resampled 5m/15m structure and session context. It has no tape, footprint, DOM, MBO, or L3 evidence.
