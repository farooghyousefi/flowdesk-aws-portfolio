# Session Library

Session Library lists local raw files, contracts, instrument IDs, request windows, records, compressed size, snapshot/completeness, integrity, download state, and backtest split.

Splits are `Development`, `Pilot`, `Locked Test`, `Forward Paper`, and `Excluded`. Exclusion requires a reason. Locked Test assignments cannot be silently moved. Opening a blind session marks it viewed; later movement into a locked test creates a data-snooping audit warning.

Raw data, derived data, journal entries, and purchase history are separate assets. Deleting one must never imply deleting the others. The current UI exposes the registry and split state; destructive raw-file actions remain intentionally unavailable until a separately audited retention policy is configured.

Data Health follows the active replay session by default. An explicit Inspect selection may differ, but both session identities remain visible. External MBP-10 verification is bound to `session_id` and the exact MBO SHA-256: the complete local snapshot currently links to the existing report with 898 compared event groups and zero mismatches, while the partial file remains pending and cannot inherit that result.
