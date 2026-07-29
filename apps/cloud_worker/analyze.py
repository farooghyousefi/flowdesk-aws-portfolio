from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from . import WORKER_VERSION
from .config import WorkerConfig
from .ingest import validate_job_id

_ALLOWED_DATASET = "GLBX.MDP3"
_ALLOWED_SCHEMAS = {"mbo", "trades", "ohlcv-1m"}


class AnalyzeError(RuntimeError):
    """A bounded analyze failure safe to include in CloudWatch."""


def _error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    return str(error.get("Code")) if isinstance(error, dict) and error.get("Code") else None


def _get_object_bytes(s3_client: Any, *, bucket: str, key: str, context: str) -> bytes:
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
        return response["Body"].read()
    except Exception as exc:
        if _error_code(exc) in {"404", "NoSuchKey", "NotFound"}:
            raise AnalyzeError(f"{context} was not found in S3.") from exc
        raise AnalyzeError(f"{context} could not be read from S3.") from exc


def _read_manifest(s3_client: Any, *, bucket: str, manifest_key: str) -> dict[str, Any]:
    raw = _get_object_bytes(
        s3_client, bucket=bucket, key=manifest_key, context="The Flowdesk ingest manifest"
    )
    try:
        manifest = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise AnalyzeError("The Flowdesk ingest manifest is not valid JSON.") from exc
    if not isinstance(manifest, dict):
        raise AnalyzeError("The Flowdesk ingest manifest has an unexpected shape.")
    return manifest


def _find_dbn_file(manifest: dict[str, Any], *, job_id: str, raw_job_prefix: str) -> dict[str, Any]:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise AnalyzeError("The ingest manifest is missing its file list.")
    candidates = [
        item
        for item in files
        if isinstance(item, dict) and str(item.get("filename") or "").endswith(".dbn.zst")
    ]
    if len(candidates) != 1:
        raise AnalyzeError("The ingest manifest must reference exactly one DBN.ZST market-data file.")

    entry = candidates[0]
    expected_prefix = f"{raw_job_prefix}/{job_id}/"
    s3_key = str(entry.get("s3Key") or "")
    filename = str(entry.get("filename") or "")
    if not s3_key.startswith(expected_prefix) or not s3_key.endswith(filename):
        raise AnalyzeError("The ingest manifest references an unexpected S3 key.")
    return entry


@dataclass(frozen=True)
class AnalyzePlan:
    job_id: str
    manifest: dict[str, Any]
    dbn_file: dict[str, Any]
    raw_key: str


def plan_analyze(config: WorkerConfig, s3_client: Any, job_id: str) -> AnalyzePlan:
    normalized_job_id = validate_job_id(job_id)
    manifest_key = f"{config.manifest_prefix}/{normalized_job_id}/manifest.json"
    manifest = _read_manifest(s3_client, bucket=config.bucket, manifest_key=manifest_key)

    if str(manifest.get("jobId") or "").upper() != normalized_job_id:
        raise AnalyzeError("The ingest manifest belongs to a different Databento job.")
    if manifest.get("status") != "COMPLETED":
        raise AnalyzeError("The ingest manifest is not marked COMPLETED.")

    dbn_file = _find_dbn_file(manifest, job_id=normalized_job_id, raw_job_prefix=config.raw_job_prefix)
    return AnalyzePlan(
        job_id=normalized_job_id,
        manifest=manifest,
        dbn_file=dbn_file,
        raw_key=str(dbn_file["s3Key"]),
    )


def _character(value: Any) -> str:
    if hasattr(value, "value"):
        value = value.value
    if isinstance(value, bytes):
        return value.decode("ascii")
    return str(value)


def _summarize(data: bytes) -> dict[str, Any]:
    try:
        import databento as db
    except ImportError as exc:
        raise AnalyzeError("The databento decoding library is not installed in this runtime.") from exc

    try:
        store = db.DBNStore.from_bytes(data)
    except Exception as exc:
        raise AnalyzeError("The DBN payload could not be decoded.") from exc

    dataset = str(getattr(store, "dataset", "") or "")
    if dataset != _ALLOWED_DATASET:
        raise AnalyzeError("The decoded DBN dataset is not GLBX.MDP3.")
    schema = str(getattr(store, "schema", "") or "")
    if schema not in _ALLOWED_SCHEMAS:
        raise AnalyzeError("The decoded DBN schema is not allowed for Flowdesk analysis.")

    record_count = 0
    action_counts: Counter[str] = Counter()
    instrument_ids: set[int] = set()
    first_ts: int | None = None
    last_ts: int | None = None

    for record in store:
        record_count += 1
        ts_event = int(getattr(record, "ts_event"))
        instrument_ids.add(int(getattr(record, "instrument_id")))
        if hasattr(record, "action"):
            action_counts[_character(getattr(record, "action"))] += 1
        first_ts = ts_event if first_ts is None else min(first_ts, ts_event)
        last_ts = ts_event if last_ts is None else max(last_ts, ts_event)

    if record_count == 0:
        raise AnalyzeError("The decoded DBN payload contains no records.")

    return {
        "dataset": dataset,
        "schema": schema,
        "symbols": sorted(str(item) for item in (getattr(store, "symbols", None) or [])),
        "recordCount": record_count,
        "instrumentIds": sorted(instrument_ids),
        "actionCounts": dict(sorted(action_counts.items())),
        "firstTsEvent": first_ts,
        "lastTsEvent": last_ts,
    }


def run_analyze(
    config: WorkerConfig,
    s3_client: Any,
    *,
    job_id: str,
    now: datetime | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    emit = progress or (lambda _: None)
    plan = plan_analyze(config, s3_client, job_id)
    emit({"event": "MANIFEST_VALIDATED", "jobId": plan.job_id})

    expected_size = plan.dbn_file.get("sizeBytes")
    expected_sha256 = str(plan.dbn_file.get("sha256") or "")
    data = _get_object_bytes(
        s3_client, bucket=config.bucket, key=plan.raw_key, context="The raw Databento DBN object"
    )
    if not isinstance(expected_size, int) or len(data) != expected_size:
        raise AnalyzeError("The downloaded DBN object size does not match the ingest manifest.")
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise AnalyzeError("The downloaded DBN object failed SHA-256 verification.")
    emit({"event": "RAW_OBJECT_VERIFIED", "jobId": plan.job_id, "sizeBytes": len(data)})

    stats = _summarize(data)
    emit({"event": "DBN_DECODED", "jobId": plan.job_id, "recordCount": stats["recordCount"]})

    completed_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat().replace("+00:00", "Z")
    result = {
        "workerVersion": WORKER_VERSION,
        "jobId": plan.job_id,
        "sourceKey": plan.raw_key,
        "sourceBytes": len(data),
        "curatedWriteStarted": False,
        "completedAt": completed_at,
        **stats,
    }
    emit({"event": "ANALYSIS_COMPLETED", "jobId": plan.job_id, "recordCount": stats["recordCount"]})
    return result
