from __future__ import annotations

import hashlib
import json
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from apps.market_service.session_runner import DailyResearchError, run_daily_strategy_backtest

from . import WORKER_VERSION
from .analyze import _read_manifest
from .config import WorkerConfig
from .ingest import validate_job_id


ENGINE_VERSION = "daily-research-v3"
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class BacktestDayError(RuntimeError):
    """A redacted daily-backtest failure safe to include in CloudWatch."""


def _error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    return str(error.get("Code")) if isinstance(error, dict) and error.get("Code") else None


def _daily_file(manifest: dict[str, Any], *, job_id: str, session_date: str) -> dict[str, Any]:
    try:
        datetime.strptime(session_date, "%Y-%m-%d")
    except ValueError as exc:
        raise BacktestDayError("The session date must be a real YYYY-MM-DD date.") from exc
    if not _DATE_PATTERN.fullmatch(session_date):
        raise BacktestDayError("The session date must use YYYY-MM-DD.")
    expected_filename = f"glbx-mdp3-{session_date.replace('-', '')}.mbo.dbn.zst"
    files = manifest.get("files")
    if not isinstance(files, list):
        raise BacktestDayError("The ingest manifest is missing its file list.")
    matches = [
        item
        for item in files
        if isinstance(item, dict) and item.get("filename") == expected_filename
    ]
    if len(matches) != 1:
        raise BacktestDayError("The requested daily DBN file is not uniquely present in the manifest.")
    item = matches[0]
    expected_prefix = f"flowdesk/raw/databento/jobs/{job_id}/"
    key = str(item.get("s3Key") or "")
    if not key.startswith(expected_prefix) or not key.endswith(expected_filename):
        raise BacktestDayError("The daily DBN manifest entry references an unexpected S3 key.")
    return item


def _download_verified(
    s3_client: Any,
    *,
    bucket: str,
    key: str,
    expected_size: int,
    expected_sha256: str,
    destination: Path,
) -> None:
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
        body = response["Body"]
        digest = hashlib.sha256()
        size = 0
        with destination.open("wb") as output:
            while True:
                chunk = body.read(8 * 1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
    except Exception as exc:
        raise BacktestDayError("The daily DBN object could not be streamed from S3.") from exc
    if size != expected_size:
        raise BacktestDayError("The daily DBN object size does not match the ingest manifest.")
    if digest.hexdigest() != expected_sha256:
        raise BacktestDayError("The daily DBN object failed SHA-256 verification.")


def _existing_result(s3_client: Any, *, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
    except Exception as exc:
        if _error_code(exc) in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise BacktestDayError("The existing daily result could not be checked.") from exc
    try:
        return json.loads(response["Body"].read())
    except Exception as exc:
        raise BacktestDayError("The existing daily result is not valid JSON.") from exc


def run_backtest_day(
    config: WorkerConfig,
    s3_client: Any,
    *,
    job_id: str,
    session_date: str,
    confirmed_session_date: str,
    scratch_directory: Path,
    fill_mode: str = "realistic",
    seed: int = 7,
    now: datetime | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    emit = progress or (lambda _: None)
    normalized_job_id = validate_job_id(job_id)
    if confirmed_session_date != session_date:
        raise BacktestDayError("--confirm-date must exactly match --date.")
    manifest_key = f"{config.manifest_prefix}/{normalized_job_id}/manifest.json"
    manifest = _read_manifest(
        s3_client,
        bucket=config.bucket,
        manifest_key=manifest_key,
    )
    if str(manifest.get("jobId") or "").upper() != normalized_job_id:
        raise BacktestDayError("The ingest manifest belongs to a different Databento job.")
    if manifest.get("status") != "COMPLETED":
        raise BacktestDayError("The ingest manifest is not marked COMPLETED.")
    daily_file = _daily_file(
        manifest,
        job_id=normalized_job_id,
        session_date=session_date,
    )
    expected_size = daily_file.get("sizeBytes")
    expected_sha256 = str(daily_file.get("sha256") or "")
    if not isinstance(expected_size, int) or expected_size < 1:
        raise BacktestDayError("The daily DBN manifest entry has an invalid size.")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise BacktestDayError("The daily DBN manifest entry has an invalid SHA-256.")

    result_key = (
        f"{config.prefix}/research/{ENGINE_VERSION}/jobs/{normalized_job_id}/"
        f"sessions/{session_date}/{expected_sha256[:16]}.json"
    )
    existing = _existing_result(s3_client, bucket=config.bucket, key=result_key)
    if existing is not None:
        if (
            existing.get("sourceFingerprint") == expected_sha256
            and existing.get("engineVersion") == ENGINE_VERSION
            and existing.get("fillMode") == fill_mode
            and int(existing.get("seed", -1)) == seed
        ):
            emit({"event": "DAILY_BACKTEST_REUSED", "date": session_date, "resultKey": result_key})
            return {**existing, "reused": True}
        raise BacktestDayError("A conflicting daily research result already exists.")

    scratch_value = str(scratch_directory)
    if not scratch_directory.is_absolute() or not (
        scratch_value in {"/tmp", "/scratch"}
        or scratch_value.startswith("/tmp/")
        or scratch_value.startswith("/scratch/")
    ):
        raise BacktestDayError("The scratch directory must be an absolute path below /tmp or /scratch.")
    emit(
        {
            "event": "DAILY_BACKTEST_PLANNED",
            "date": session_date,
            "sourceBytes": expected_size,
            "automaticOrderExecution": False,
        }
    )
    try:
        scratch_directory.mkdir(parents=True, exist_ok=True)
        temporary_directory = tempfile.TemporaryDirectory(
            prefix="flowdesk-day-",
            dir=scratch_directory,
        )
    except OSError as exc:
        raise BacktestDayError("The private scratch volume is not writable.") from exc
    with temporary_directory as temp_dir:
        local_path = Path(temp_dir) / str(daily_file["filename"])
        _download_verified(
            s3_client,
            bucket=config.bucket,
            key=str(daily_file["s3Key"]),
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            destination=local_path,
        )
        emit(
            {
                "event": "DAILY_SOURCE_VERIFIED",
                "date": session_date,
                "sourceBytes": expected_size,
            }
        )
        try:
            research = run_daily_strategy_backtest(
                local_path,
                session_date=session_date,
                data_fingerprint=expected_sha256,
                fill_mode=fill_mode,
                seed=seed,
                progress=emit,
            )
        except DailyResearchError as exc:
            raise BacktestDayError(
                "The daily source violates the single-instrument research contract."
            ) from exc

    completed_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat().replace("+00:00", "Z")
    result = {
        **research,
        "engineVersion": ENGINE_VERSION,
        "workerVersion": WORKER_VERSION,
        "jobId": normalized_job_id,
        "sourceKey": daily_file["s3Key"],
        "sourceBytes": expected_size,
        "resultKey": result_key,
        "completedAt": completed_at,
        "reused": False,
    }
    encoded = json.dumps(
        result,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    try:
        s3_client.put_object(
            Bucket=config.bucket,
            Key=result_key,
            Body=encoded,
            ContentType="application/json",
            ServerSideEncryption="AES256",
            Metadata={
                "databento-job-id": normalized_job_id,
                "session-date": session_date,
                "source-sha256": expected_sha256,
                "engine-version": ENGINE_VERSION,
                "result-sha256": hashlib.sha256(encoded).hexdigest(),
            },
            IfNoneMatch="*",
        )
    except Exception as exc:
        raise BacktestDayError("The verified daily research result could not be written to S3.") from exc
    emit(
        {
            "event": "DAILY_BACKTEST_COMPLETED",
            "date": session_date,
            "eventsProcessed": result["eventsProcessed"],
            "resultKey": result_key,
            "profitabilityClaim": False,
        }
    )
    return result
