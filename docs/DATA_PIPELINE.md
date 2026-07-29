# Data Pipeline

`npm run data:import -- --file /absolute/path/file.dbn.zst` performs a deterministic local import:

1. Restrict the path to the configured Databento raw-data root.
2. Load the adjacent manifest and compare SHA-256.
3. Validate DBN dataset, schema, record count, timestamps, and unique instrument.
4. Reconstruct the MBO book in original file order.
5. Detect snapshot state and classify the session as complete or partial.
6. Calculate bars, trades, footprint, book buckets, and feature payloads in one pass.
7. Write large derivatives as Zstandard-compressed Parquet.
8. Register metadata in SQLite and refresh DuckDB views.

Raw DBN files are never modified. Session metadata includes file, hash, records, instrument, time range, snapshot status, completeness, unknown references by phase, sequence regressions, processing rate, memory, and external verification.

`F_LAST` closes a consistent event group. Natural file order is preserved; sequence values are diagnostics and never used to reorder records.
