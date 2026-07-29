# Data Planner Authorization Repair

## Root cause

The estimate lifecycle was overwritten by `save_data_estimate`: an upsert on the
request fingerprint reused the estimate id but reset authorization status, job id,
and expiry. A repeated estimate therefore made an already submitted job look like a
fresh authorization.

The previous submit path also crossed three independent transactions: it committed
`SUBMITTING`, called Databento synchronously, then persisted the job and estimate in
separate transactions. It had no durable idempotency key, authorization record, or
cost reservation. A timeout or reload could leave the UI and database unable to
distinguish a rejected request from a remotely accepted one.

The production database contains one pre-repair Databento job with remote id
`GLBX-20260716-J3TTEVHVW8`. Its local cost, charge, download, and byte fields are
empty. This proves submission, but does not prove whether Databento charged it.
Resolve that job in Databento before enabling live execution.

## Repair

- Authorization, budget reservation, dataset job, estimate link, and audit event are
  committed together under `BEGIN IMMEDIATE`.
- A client idempotency key and a unique estimate authorization prevent duplicate
  jobs. Reusing the same estimate returns the existing result.
- Estimates with an authorization or job preserve their lifecycle during re-estimate.
- The state machine is `IDLE -> VALIDATING -> SUBMITTING -> AUTHORIZED -> QUEUED ->
  DOWNLOADING -> IMPORTING -> VALIDATING_IMPORT -> COMPLETED`, with explicit
  `EXPIRED`, `CANCELLED`, and `FAILED` terminal paths.
- The server owns amount rounding, confirmation phrase, fingerprint, terms, budgets,
  UTC expiry, and queue checks. The browser only presents those values.
- `DATABENTO_BATCH_EXECUTION_MODE=disabled` is the default. `dry_run` exercises the
  complete local transaction. Only `live` may call Databento, and only after the
  local authorization transaction commits.
- Authorized, downloaded, and actually charged costs are separate ledger values.

## Controlled live test

1. Inspect remote job `GLBX-20260716-J3TTEVHVW8` in Databento and cancel or resolve
   it there. Do not infer its state from the local database.
2. Stop the fail-closed local dev processes and start a supervised session:
   `DATABENTO_BATCH_EXECUTION_MODE=live npm run dev:trading`.
3. Create one new estimate with a new request fingerprint and verify that it is below
   the request, daily, weekly, and monthly limits.
4. Open its review dialog, accept the terms, enter the exact server phrase, and click
   Authorize once. Do not retry an unknown outcome; reload first and inspect the
   persistent job card.
5. Confirm exactly one authorization, one reservation, one local job, one remote id,
   and the expected audit sequence. Confirm Databento independently.
6. Restart immediately with `DATABENTO_BATCH_EXECUTION_MODE=disabled`.

The automated end-to-end test uses `dry_run`; it submits two concurrent identical
requests and asserts one authorization, one reservation, one job, no remote id, no
download, and no actual charge.
