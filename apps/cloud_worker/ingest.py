from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, Callable, Iterable

import requests

from . import WORKER_VERSION
from .config import MIB, WorkerConfig
from .databento_api import DatabentoApiError, DatabentoBatchClient, validate_download_url


_JOB_ID_PATTERN = re.compile(r"^GLBX-\d{8}-[A-Z0-9]{10}$")
_FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_SHA256_PATTERN = re.compile(r"^sha256:([0-9a-fA-F]{64})$")
_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_SCHEMAS = {"mbo", "trades", "ohlcv-1m"}
_ALLOWED_FILE_SUFFIXES = (".dbn.zst", ".json", ".json.zst")


class IngestError(RuntimeError):
    """A bounded ingest failure safe to include in CloudWatch."""


class _EarlyEof(IngestError):
    pass


@dataclass(frozen=True)
class BatchFile:
    filename: str
    size: int
    sha256: str
    url: str


@dataclass(frozen=True)
class IngestPlan:
    job_id: str
    details: dict[str, Any]
    files: tuple[BatchFile, ...]

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.files)

    def public(self) -> dict[str, Any]:
        return {
            "workerVersion": WORKER_VERSION,
            "jobId": self.job_id,
            "job": self.details,
            "fileCount": len(self.files),
            "totalBytes": self.total_bytes,
            "files": [
                {"filename": item.filename, "sizeBytes": item.size, "sha256": item.sha256}
                for item in self.files
            ],
            "downloadStarted": False,
            "s3WriteStarted": False,
        }


def validate_job_id(job_id: str) -> str:
    normalized = job_id.strip().upper()
    if not _JOB_ID_PATTERN.fullmatch(normalized):
        raise IngestError("The Databento batch job ID has an invalid format.")
    return normalized


def validate_request_fingerprint(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if not _FINGERPRINT_PATTERN.fullmatch(normalized):
        raise IngestError("The Flowdesk request fingerprint must be 64 lowercase hexadecimal characters.")
    return normalized


def _normalized_symbols(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return []


def _public_job_details(job_id: str, details: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "dataset",
        "schema",
        "symbols",
        "stype_in",
        "stype_out",
        "start",
        "end",
        "encoding",
        "compression",
        "split_duration",
        "record_count",
        "actual_size",
        "state",
        "ts_process_done",
        "ts_expiration",
    )
    result = {"id": job_id}
    for key in allowed:
        value = details.get(key)
        if value is not None:
            result[key] = value
    return result


def _validate_job_details(job_id: str, details: dict[str, Any]) -> dict[str, Any]:
    remote_id = str(details.get("id") or job_id).upper()
    if remote_id != job_id:
        raise IngestError("Databento returned details for a different batch job.")
    if str(details.get("state") or "").lower() != "done":
        raise IngestError("The Databento batch job is not in the done state.")
    if str(details.get("dataset") or "") != "GLBX.MDP3":
        raise IngestError("Only the GLBX.MDP3 dataset is allowed.")
    if str(details.get("schema") or "") not in _ALLOWED_SCHEMAS:
        raise IngestError("The Databento batch schema is not allowed for Flowdesk.")
    if str(details.get("encoding") or "").lower() != "dbn":
        raise IngestError("The Databento batch job must use DBN encoding.")
    if str(details.get("compression") or "").lower() != "zstd":
        raise IngestError("The Databento batch job must use Zstandard compression.")
    if str(details.get("delivery") or "download").lower() != "download":
        raise IngestError("The Databento batch job must use download delivery.")
    if str(details.get("stype_in") or "").lower() != "continuous":
        raise IngestError("The Databento batch job must use continuous input symbology.")
    symbols = _normalized_symbols(details.get("symbols"))
    if symbols != ["MES.v.0"]:
        raise IngestError("The first Flowdesk worker release accepts only MES.v.0.")
    return _public_job_details(job_id, details)


def _parse_batch_files(items: Iterable[dict[str, Any]], config: WorkerConfig) -> tuple[BatchFile, ...]:
    raw_items = list(items)
    if not raw_items:
        raise IngestError("Databento returned no files for the completed batch job.")
    if len(raw_items) > config.max_files:
        raise IngestError("The Databento batch job exceeds the configured file-count limit.")

    files: list[BatchFile] = []
    names: set[str] = set()
    for item in raw_items:
        filename = str(item.get("filename") or "")
        if (
            not _FILENAME_PATTERN.fullmatch(filename)
            or PurePosixPath(filename).name != filename
            or not filename.endswith(_ALLOWED_FILE_SUFFIXES)
        ):
            raise IngestError("Databento returned an unsafe or unsupported batch filename.")
        if filename in names:
            raise IngestError("Databento returned duplicate batch filenames.")
        names.add(filename)

        try:
            size = int(item["size"])
        except (KeyError, TypeError, ValueError) as exc:
            raise IngestError("Databento returned an invalid batch file size.") from exc
        if size <= 0 or size > config.max_file_bytes:
            raise IngestError("A Databento batch file exceeds the configured size limit.")

        hash_match = _SHA256_PATTERN.fullmatch(str(item.get("hash") or ""))
        if hash_match is None:
            raise IngestError("A Databento batch file is missing its SHA-256 hash.")
        urls = item.get("urls")
        if not isinstance(urls, dict) or not urls.get("https"):
            raise IngestError("A Databento batch file has no HTTPS download URL.")
        url = validate_download_url(str(urls["https"]))
        files.append(
            BatchFile(
                filename=filename,
                size=size,
                sha256=hash_match.group(1).lower(),
                url=url,
            )
        )

    files.sort(key=lambda item: item.filename)
    total_bytes = sum(item.size for item in files)
    if total_bytes > config.max_job_bytes:
        raise IngestError("The Databento batch job exceeds the configured total-size limit.")
    if not any(item.filename.endswith(".dbn.zst") for item in files):
        raise IngestError("The Databento batch job contains no DBN.ZST market-data file.")
    return tuple(files)


def plan_ingest(
    config: WorkerConfig,
    client: DatabentoBatchClient,
    job_id: str,
) -> IngestPlan:
    normalized_job_id = validate_job_id(job_id)
    try:
        details = client.get_job_details(normalized_job_id)
        files = client.list_files(normalized_job_id)
    except DatabentoApiError as exc:
        raise IngestError(str(exc)) from exc
    return IngestPlan(
        job_id=normalized_job_id,
        details=_validate_job_details(normalized_job_id, details),
        files=_parse_batch_files(files, config),
    )


def _error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    return str(error.get("Code")) if isinstance(error, dict) and error.get("Code") else None


def _head_object(s3_client: Any, *, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        return s3_client.head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        if _error_code(exc) in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise IngestError("S3 object lookup failed.") from exc


def _content_type(filename: str) -> str:
    return "application/json" if filename.endswith(".json") else "application/octet-stream"


def _upload_part(
    s3_client: Any,
    *,
    config: WorkerConfig,
    object_key: str,
    upload_id: str,
    part_number: int,
    payload: bytes,
) -> dict[str, Any]:
    try:
        response = s3_client.upload_part(
            Bucket=config.bucket,
            Key=object_key,
            UploadId=upload_id,
            PartNumber=part_number,
            Body=payload,
        )
    except Exception as exc:
        raise IngestError("S3 multipart upload failed.") from exc
    etag = response.get("ETag")
    if not etag:
        raise IngestError("S3 returned no ETag for an uploaded part.")
    return {"ETag": etag, "PartNumber": part_number}


def _stream_file_to_s3(
    config: WorkerConfig,
    client: DatabentoBatchClient,
    s3_client: Any,
    *,
    job_id: str,
    batch_file: BatchFile,
    object_key: str,
) -> dict[str, Any]:
    existing = _head_object(s3_client, bucket=config.bucket, key=object_key)
    if existing is not None:
        metadata = existing.get("Metadata") or {}
        if (
            int(existing.get("ContentLength") or -1) == batch_file.size
            and metadata.get("sha256") == batch_file.sha256
            and metadata.get("databento-job-id") == job_id
        ):
            return {
                "filename": batch_file.filename,
                "sizeBytes": batch_file.size,
                "sha256": batch_file.sha256,
                "s3Key": object_key,
                "reused": True,
            }
        raise IngestError("An existing S3 object conflicts with the Databento batch manifest.")

    try:
        created = s3_client.create_multipart_upload(
            Bucket=config.bucket,
            Key=object_key,
            ContentType=_content_type(batch_file.filename),
            ServerSideEncryption="AES256",
            Metadata={
                "sha256": batch_file.sha256,
                "databento-job-id": job_id,
                "source": "databento",
            },
        )
        upload_id = str(created["UploadId"])
    except Exception as exc:
        raise IngestError("S3 multipart upload could not be created.") from exc

    parts: list[dict[str, Any]] = []
    digest = hashlib.sha256()
    committed = 0
    retries = 0

    try:
        while committed < batch_file.size:
            response: requests.Response | None = None
            attempt_start = committed
            try:
                response = client.open_file(batch_file.url, offset=committed)
                buffer = bytearray()
                for chunk in response.iter_content(chunk_size=min(MIB, config.multipart_part_bytes)):
                    if not chunk:
                        continue
                    buffer.extend(chunk)
                    if committed + len(buffer) > batch_file.size:
                        raise IngestError("Databento sent more bytes than declared in its batch manifest.")
                    while len(buffer) >= config.multipart_part_bytes:
                        payload = bytes(buffer[: config.multipart_part_bytes])
                        del buffer[: config.multipart_part_bytes]
                        part = _upload_part(
                            s3_client,
                            config=config,
                            object_key=object_key,
                            upload_id=upload_id,
                            part_number=len(parts) + 1,
                            payload=payload,
                        )
                        parts.append(part)
                        digest.update(payload)
                        committed += len(payload)

                if committed + len(buffer) != batch_file.size:
                    raise _EarlyEof("Databento closed the file download before the declared size was reached.")
                if buffer:
                    payload = bytes(buffer)
                    part = _upload_part(
                        s3_client,
                        config=config,
                        object_key=object_key,
                        upload_id=upload_id,
                        part_number=len(parts) + 1,
                        payload=payload,
                    )
                    parts.append(part)
                    digest.update(payload)
                    committed += len(payload)
                retries = 0
            except (requests.RequestException, DatabentoApiError, _EarlyEof) as exc:
                retryable = not isinstance(exc, DatabentoApiError) or exc.retryable
                if not retryable or retries >= config.http_max_retries:
                    raise IngestError("Databento file download failed after safe retries.") from exc
                retries = 0 if committed > attempt_start else retries + 1
                continue
            finally:
                if response is not None:
                    response.close()

        if committed != batch_file.size:
            raise IngestError("The streamed Databento file size did not match its manifest.")
        if digest.hexdigest() != batch_file.sha256:
            raise IngestError("The streamed Databento file failed SHA-256 verification.")
        if not parts:
            raise IngestError("The Databento file produced no S3 multipart parts.")

        s3_client.complete_multipart_upload(
            Bucket=config.bucket,
            Key=object_key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
    except Exception:
        try:
            s3_client.abort_multipart_upload(
                Bucket=config.bucket,
                Key=object_key,
                UploadId=upload_id,
            )
        except Exception as abort_error:
            # Incomplete multipart parts can incur storage cost. A cleanup
            # failure must therefore be visible instead of being discarded.
            raise IngestError("The worker failed and S3 multipart cleanup also failed.") from abort_error
        raise

    remote = _head_object(s3_client, bucket=config.bucket, key=object_key)
    metadata = (remote or {}).get("Metadata") or {}
    if (
        remote is None
        or int(remote.get("ContentLength") or -1) != batch_file.size
        or metadata.get("sha256") != batch_file.sha256
    ):
        raise IngestError("S3 verification after multipart completion failed.")

    return {
        "filename": batch_file.filename,
        "sizeBytes": batch_file.size,
        "sha256": batch_file.sha256,
        "s3Key": object_key,
        "reused": False,
    }


def run_ingest(
    config: WorkerConfig,
    client: DatabentoBatchClient,
    s3_client: Any,
    *,
    job_id: str,
    confirmed_job_id: str,
    request_fingerprint: str | None = None,
    now: datetime | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    normalized_job_id = validate_job_id(job_id)
    if validate_job_id(confirmed_job_id) != normalized_job_id:
        raise IngestError("Ingest confirmation must exactly match the Databento batch job ID.")
    fingerprint = validate_request_fingerprint(request_fingerprint)
    plan = plan_ingest(config, client, normalized_job_id)
    emit = progress or (lambda _: None)
    emit({"event": "JOB_VALIDATED", "jobId": normalized_job_id, "fileCount": len(plan.files)})

    uploaded: list[dict[str, Any]] = []
    for index, batch_file in enumerate(plan.files, start=1):
        emit(
            {
                "event": "FILE_STARTED",
                "jobId": normalized_job_id,
                "file": batch_file.filename,
                "fileNumber": index,
                "fileCount": len(plan.files),
            }
        )
        object_key = f"{config.raw_job_prefix}/{normalized_job_id}/{batch_file.filename}"
        result = _stream_file_to_s3(
            config,
            client,
            s3_client,
            job_id=normalized_job_id,
            batch_file=batch_file,
            object_key=object_key,
        )
        uploaded.append(result)
        emit(
            {
                "event": "FILE_REUSED" if result["reused"] else "FILE_UPLOADED",
                "jobId": normalized_job_id,
                "file": batch_file.filename,
                "fileNumber": index,
                "fileCount": len(plan.files),
            }
        )

    completed_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat().replace("+00:00", "Z")
    manifest = {
        "manifestVersion": 1,
        "workerVersion": WORKER_VERSION,
        "status": "COMPLETED",
        "jobId": normalized_job_id,
        "requestFingerprint": fingerprint,
        "job": plan.details,
        "bucket": config.bucket,
        "files": uploaded,
        "fileCount": len(uploaded),
        "totalBytes": sum(item["sizeBytes"] for item in uploaded),
        "completedAt": completed_at,
        "automaticOrderExecution": False,
    }
    manifest_bytes = (json.dumps(manifest, sort_keys=True, separators=(",", ":"), default=str) + "\n").encode()
    manifest_key = f"{config.manifest_prefix}/{normalized_job_id}/manifest.json"
    try:
        s3_client.put_object(
            Bucket=config.bucket,
            Key=manifest_key,
            Body=manifest_bytes,
            ContentType="application/json",
            ServerSideEncryption="AES256",
            Metadata={"databento-job-id": normalized_job_id, "worker-version": WORKER_VERSION},
        )
    except Exception as exc:
        raise IngestError("The verified Flowdesk ingest manifest could not be written to S3.") from exc

    manifest_head = _head_object(s3_client, bucket=config.bucket, key=manifest_key)
    if manifest_head is None or int(manifest_head.get("ContentLength") or -1) != len(manifest_bytes):
        raise IngestError("The Flowdesk ingest manifest failed its S3 size verification.")

    emit({"event": "JOB_COMPLETED", "jobId": normalized_job_id, "fileCount": len(uploaded)})
    return {**manifest, "manifestKey": manifest_key}
