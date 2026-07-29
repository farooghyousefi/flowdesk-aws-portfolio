# Batch Downloads

Full-session purchases use the Databento Batch API with `dbn`, `zstd`, and `split_duration=day`. Submissions are idempotent by estimate and schema.

A tracked job must reach `READY` before download. The local download path is constrained below `data/databento/raw/MES`; traversal is rejected. Files first become `filename.dbn.zst.part` and are checked for nonzero size, dataset, schema, instrument, record count, and SHA-256. A manifest is written without secrets or signed URLs. Only then is the file atomically renamed and imported.

MBO import additionally reconstructs the book, checks snapshot state and unknown references, creates Parquet derivatives, refreshes DuckDB, and registers the session in SQLite. Failed validation never registers a replayable session; the `.part` path is retained for inspection.

```bash
npm run dataset:jobs
npm run dataset:download -- --job-id TRACKED_JOB_ID
npm run dataset:validate -- --file /absolute/path/file.dbn.zst
npm run dataset:import -- --file /absolute/path/file.dbn.zst
```
