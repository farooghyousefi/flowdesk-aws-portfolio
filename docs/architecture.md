# Architecture

```text
Databento DBN + manifest
  -> Python DBN reader and MBO validator
  -> incremental L3 order-book reconstructor
  -> deterministic feature and replay engines
  -> FastAPI REST/WebSocket on 127.0.0.1:8787
  -> Next.js workspace on 127.0.0.1:3000
```

## Ownership

- `apps/connectors/databento`: cost controls, DBN decoding, validation, fixed-point MBO reconstruction, and MBP-10 verification
- `apps/market_service`: import, Parquet/DuckDB/SQLite storage, features, decisions, replay, API, doctor, and benchmark
- `apps/web`: local operational UI and visualizations
- `packages/shared-types`: versioned market-event, book, decision, and data-source contracts
- `packages/trading-engine`: provider-neutral historical, replay, and disabled-live adapters

Prices retain integer identity in engines and contracts. Decimal numbers are presentation values only. The server consumes every event but publishes aggregated state revisions at up to 20 frames per second.

## Storage

- Raw truth: DBN/Zstandard plus manifests and hashes
- Batch derivatives: Parquet in `data/derived`
- Analytical views: DuckDB in `data/app/market.duckdb`
- Settings, sessions, journal: SQLite in `data/app/trading-assistant.sqlite3`

No cloud service, broker order route, or public network binding is part of the default architecture.
