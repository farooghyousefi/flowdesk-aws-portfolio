# Databento Cost Safety

`.env.local` remains ignored, mode `0600`, and server-only. Default request, daily, weekly, and monthly limits are `$1.00`, `$5.00`, `$15.00`, and `$40.00`.

Estimate costs nothing and downloads no market data. A paid Batch request requires all of the following:

1. A server-side estimate no older than ten minutes.
2. An unchanged request fingerprint.
3. All local limits still passing under a SQLite transaction lock.
4. The active acknowledgement checkbox.
5. Exact text `DOWNLOAD` or `DOWNLOAD $X.XX`.

Page load, estimate, optimization, hover, retry, reload, and double click cannot submit a Batch job. Existing validated files and tracked jobs are reused.

The one-day metadata demo for `2026-07-14` created three estimates and zero jobs. Full L3 was blocked at `$1.010426`; Economy was `$0.230449`; Chart Context was `$0.003505`.

## Completed MBP-10 Check

- Instrument: `42003239` / `MESU6`
- Range: `2026-07-13T23:59:59.999936957Z` to `2026-07-14T00:00:01.999936957Z`
- Limit: 1,000 records
- Estimate: `$0.000171`
- Result: 898 exact event-group comparisons, zero BBO/spread/top-10 price/size/count mismatches, zero post-snapshot warnings
- Status: `EXTERNALLY VERIFIED`

Reports are stored under ignored `data/databento/reports/book-verification`. No further Databento download is needed for local demo operation.

```bash
npm run databento:verify:estimate -- --mbo-file /absolute/path/file.dbn.zst --limit 1000
```

Estimate commands do not download time-series data. Never add `--confirm` unless a new request is explicitly authorized and its fresh estimate is inside the intended budget.
