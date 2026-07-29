# Data Planner

Data Planner is a five-step local workflow: Scope, Time, Compare, Authorize, Validate.

The canonical request plan is persisted as one structure containing session date, `Europe/Berlin`, visible local replay start/end, context minutes, converted replay UTC start/end, and raw request UTC start/end. The UI, preview endpoint, estimate operation, and reload all read that structure. The default visible window is `15:00-16:30` Berlin.

The server resolves `MES.v.0` to one raw contract and instrument ID for the selected date. Local `Europe/Berlin` times are converted with the IANA timezone database, including summer/winter offsets and blocked ambiguous or missing DST times.

Three modes are estimated from live Databento metadata:

| Mode | Schemas | Raw request | Intended use |
| --- | --- | --- | --- |
| Full L3 Research | `mbo` | UTC midnight to replay end | Complete historical book and L3 research |
| Orderflow Economy | `trades` + `ohlcv-1m` | Context start to replay end | Tape, delta, footprint, VWAP, chart context |
| Chart Context Only | `ohlcv-1m` | UTC midnight to replay end | 1m/5m/15m chart context |

Each estimate records count, billable bytes, unit price, cost, safety reserve, limits, confidence, ten-minute warning, TTL, stable fingerprint, mapping dates, and local reuse. Changing time, mode, schemas, instrument, encoding, compression, or split invalidates the fingerprint.

Context is prepended to replay start and never extends replay end. For `2026-07-14`, `15:00-16:30 Europe/Berlin`, and 30 minutes context, the visible replay is `13:00-14:30 UTC` and the Economy request is `12:30-14:30 UTC`. A form change immediately removes the current cards, marks the estimate stale, and disables review until a fresh estimate succeeds or fails cleanly.

Estimate costs nothing. Estimate does not download market data. Only Confirm Download can create a paid request. A local validated file is reused without another paid request.
