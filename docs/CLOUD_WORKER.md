# Flowdesk Cloud Worker

## Why this worker exists

The Mac remains the Flowdesk user interface and control surface. Large
Databento files must not pass through or remain on the Mac. The cloud worker is
a short-lived Python process that runs inside an ECS/Fargate or AWS Batch task,
copies one already completed Databento Batch job into S3, verifies the transfer,
writes a compact manifest, and exits.

It is not an always-on server and it is not a trading bot.

## AWS identities

The two IAM roles have deliberately different responsibilities:

| Role | Used by | Permissions |
| --- | --- | --- |
| `FlowdeskWorkerTaskRole` | Python code inside the container | Flowdesk S3 prefix and `/flowdesk/databento/api-key` |
| `FlowdeskWorkerExecutionRole` | ECS/Fargate startup agent | Pull the private ECR image and write CloudWatch logs |

The worker uses temporary task-role credentials supplied by ECS. Static AWS
access keys are neither required nor supported in the container.

## Data path

```text
completed Databento Batch job
  -> authenticated HTTPS chunks
  -> bounded in-memory buffer
  -> S3 multipart parts
  -> Databento size and SHA-256 verification
  -> compact S3 manifest
  -> worker exits
```

Raw objects use this layout:

```text
s3://BUCKET/flowdesk/raw/databento/jobs/JOB_ID/FILENAME
s3://BUCKET/flowdesk/metadata/databento/jobs/JOB_ID/manifest.json
```

No complete market-data file is written to local disk. A connection failure
resumes from the last complete multipart part. A failed or mismatched stream
aborts the multipart upload. A retry reuses an existing object only when job
ID, byte size, and SHA-256 all match; otherwise it fails closed.

## Non-negotiable safety behavior

- The Batch client has no submit or purchase method.
- Only jobs already in Databento state `done` are accepted.
- The first release accepts only `GLBX.MDP3`, `MES.v.0`, DBN, and Zstandard.
- Every file needs Databento's declared byte size and `sha256:` hash.
- Unsafe filenames, non-HTTPS URLs, local network addresses, and unexpected
  redirects are blocked.
- The API key is decrypted from Parameter Store at runtime and is never added
  to a manifest, URL log, image, or repository file.
- Progress logs contain one event per job or file, never one event per MBO
  record.
- `automaticOrderExecution` is permanently `false` in the ingest manifest.
- The runtime uses a pinned Alpine-based Python image and contains no Perl
  runtime. Do not deploy an image while its ECR basic scan reports any
  `CRITICAL` or `HIGH` operating-system findings.

Transfer integrity does not by itself qualify a dataset for research. A later
cloud validation stage must still inspect DBN metadata, records, instrument ID,
snapshot state, and book completeness before a session can be used.

## Runtime configuration

| Environment variable | Initial value | Purpose |
| --- | --- | --- |
| `AWS_REGION` | `eu-central-1` | Keeps the first release in Frankfurt |
| `FLOWDESK_S3_BUCKET` | explicit private bucket name | Prevents an implicit or wrong destination |
| `FLOWDESK_S3_PREFIX` | `flowdesk` | Matches the least-privilege S3 policy |
| `DATABENTO_PARAMETER_NAME` | `/flowdesk/databento/api-key` | Matches the least-privilege SSM policy |
| `FLOWDESK_MAX_FILES` | `128` | Accepts the verified daily-split research job while rejecting unexpected fragmentation |
| `FLOWDESK_MAX_FILE_BYTES` | `15 GiB` | Per-file safety limit |
| `FLOWDESK_MAX_JOB_BYTES` | `50 GiB` | Total task safety limit |
| `FLOWDESK_MULTIPART_PART_BYTES` | `16 MiB` | Bounded memory and resumable S3 uploads |

## Commands

Inspection reads metadata only. It downloads no file and writes nothing to S3:

```bash
python -m apps.cloud_worker inspect --job-id GLBX-YYYYMMDD-XXXXXXXXXX
```

Ingest requires the same job ID twice so an incorrectly constructed task
command cannot silently copy a different job:

```bash
python -m apps.cloud_worker ingest \
  --job-id GLBX-YYYYMMDD-XXXXXXXXXX \
  --confirm-job-id GLBX-YYYYMMDD-XXXXXXXXXX \
  --request-fingerprint 64_LOWERCASE_HEX_CHARACTERS
```

Daily research reads exactly one already-ingested UTC day, verifies its S3
object against the ingest manifest, runs the bounded-memory strategy search and
realistic execution gate, and writes an immutable AES-256-encrypted JSON result:

```bash
python -m apps.cloud_worker backtest-day \
  --job-id GLBX-YYYYMMDD-XXXXXXXXXX \
  --date 2026-05-05 \
  --confirm-date 2026-05-05 \
  --fill-mode realistic \
  --seed 7
```

Results use this layout:

```text
s3://BUCKET/flowdesk/research/daily-research-v3/jobs/JOB_ID/sessions/YYYY-MM-DD/SOURCE_HASH.json
```

The research command has no broker integration, always records
`automaticOrderExecution=false`, and cannot promote a candidate to paper
trading while calendar and news coverage are missing.

Daily research v3 preserves compact evidence for every curated candidate and
replays every candidate that produced a signal through the realistic fill model
in one shared, memory-bounded event pass. Candidate positions remain
independent; the historical L3 book is decoded only once for the gate.

## Multi-session campaign

Freeze the data cohorts before inspecting multi-session outcomes:

```bash
npm run campaign:plan -- \
  --job-id GLBX-YYYYMMDD-XXXXXXXXXX \
  --bucket FLOWDESK_BUCKET \
  --profile aws-course \
  --output data/derived/research-campaign.json \
  --publish
```

The immutable plan assigns the earliest 60% of daily files to Development, the
next 20% to Validation, and withholds the final 20% as Locked Test. Strategy
selection uses Development only. The campaign runner permits only Development
or Validation, pins an explicit ECS task definition, verifies the read-only
worker and scratch initializer, reuses existing result objects, and enforces a
maximum concurrency of two tasks. It deliberately has no command that launches
the Locked Test.

Campaign aggregation consumes realistic candidate evidence, rejects parameter
drift or source-fingerprint drift, and never treats a positive historical result
as a profitability claim or paper-trading authorization.

## Fargate scratch volume

The image defaults to the non-root user `10001:10001` and keeps its root
filesystem read-only. A daily backtest needs a writable ephemeral volume at
`/scratch`. Fargate creates that volume as root, so the task definition must run
a short non-essential `scratch-init` container as root to set only that mount to
owner `10001:10001` and mode `0700`. The main `flowdesk-worker` container must
depend on `scratch-init:SUCCESS`; it continues to run as the image's non-root
user. Do not solve the mount ownership problem by running the research worker
itself as root.

Build the local image with:

```bash
npm run worker:build
```

The build emits one ARM64 image manifest without a provenance wrapper so ECR
basic scanning can inspect it. Creating the image does not start Fargate,
purchase Databento data, or write to S3. ECR upload and the first metadata-only
Fargate inspection are separate, explicitly reviewed steps.
