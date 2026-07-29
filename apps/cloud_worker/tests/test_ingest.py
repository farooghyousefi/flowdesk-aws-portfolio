from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from apps.cloud_worker.config import WorkerConfig
from apps.cloud_worker.ingest import IngestError, plan_ingest, run_ingest


JOB_ID = "GLBX-20260716-J3TTEVHVW8"
BUCKET = "flowdesk-demo-bucket-eu-central-1"


def config() -> WorkerConfig:
    return WorkerConfig(
        region="eu-central-1",
        bucket=BUCKET,
        prefix="flowdesk",
        parameter_name="/flowdesk/databento/api-key",
        max_files=8,
        max_file_bytes=1_000,
        max_job_bytes=2_000,
        multipart_part_bytes=4,
        http_connect_timeout_seconds=1,
        http_read_timeout_seconds=10,
        http_max_retries=2,
    )


def job_details(**overrides: object) -> dict[str, object]:
    return {
        "id": JOB_ID,
        "state": "done",
        "dataset": "GLBX.MDP3",
        "schema": "mbo",
        "encoding": "dbn",
        "compression": "zstd",
        "delivery": "download",
        "stype_in": "continuous",
        "symbols": ["MES.v.0"],
        "start": "2026-07-14T00:00:00Z",
        "end": "2026-07-15T00:00:00Z",
        **overrides,
    }


def file_item(data: bytes, filename: str = "MES-20260714.mbo.dbn.zst", **overrides: object) -> dict:
    return {
        "filename": filename,
        "size": len(data),
        "hash": f"sha256:{hashlib.sha256(data).hexdigest()}",
        "urls": {"https": f"https://hist.databento.com/download/{filename}?token=secret"},
        **overrides,
    }


@dataclass
class FakeDownloadResponse:
    data: bytes
    closed: bool = False

    def iter_content(self, chunk_size: int) -> Any:
        for start in range(0, len(self.data), max(1, chunk_size)):
            yield self.data[start : start + chunk_size]

    def close(self) -> None:
        self.closed = True


class FakeBatchClient:
    def __init__(
        self,
        data: bytes,
        *,
        details: dict[str, object] | None = None,
        files: list[dict] | None = None,
        first_response_limit: int | None = None,
    ) -> None:
        self.data = data
        self.details = details or job_details()
        self.files = files or [file_item(data)]
        self.first_response_limit = first_response_limit
        self.open_offsets: list[int] = []
        self.metadata_calls = 0

    def get_job_details(self, job_id: str) -> dict[str, Any]:
        self.metadata_calls += 1
        return dict(self.details)

    def list_files(self, job_id: str) -> list[dict[str, Any]]:
        self.metadata_calls += 1
        return list(self.files)

    def open_file(self, url: str, *, offset: int = 0) -> FakeDownloadResponse:
        self.open_offsets.append(offset)
        remaining = self.data[offset:]
        if len(self.open_offsets) == 1 and self.first_response_limit is not None:
            remaining = remaining[: self.first_response_limit]
        return FakeDownloadResponse(remaining)


class FakeClientError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.uploads: dict[str, dict[str, Any]] = {}
        self.aborted: list[str] = []
        self.abort_failure = False
        self._next_upload = 1

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        if Key not in self.objects:
            raise FakeClientError("404")
        item = self.objects[Key]
        return {"ContentLength": len(item["Body"]), "Metadata": dict(item.get("Metadata") or {})}

    def create_multipart_upload(self, *, Bucket: str, Key: str, **parameters: object) -> dict[str, str]:
        upload_id = f"upload-{self._next_upload}"
        self._next_upload += 1
        self.uploads[upload_id] = {"Key": Key, "Parts": {}, **parameters}
        return {"UploadId": upload_id}

    def upload_part(
        self, *, Bucket: str, Key: str, UploadId: str, PartNumber: int, Body: bytes
    ) -> dict[str, str]:
        self.uploads[UploadId]["Parts"][PartNumber] = bytes(Body)
        return {"ETag": f'"etag-{PartNumber}"'}

    def complete_multipart_upload(
        self, *, Bucket: str, Key: str, UploadId: str, MultipartUpload: dict
    ) -> dict:
        upload = self.uploads.pop(UploadId)
        body = b"".join(upload["Parts"][part["PartNumber"]] for part in MultipartUpload["Parts"])
        self.objects[Key] = {"Body": body, "Metadata": upload["Metadata"]}
        return {"ETag": '"complete"'}

    def abort_multipart_upload(self, *, Bucket: str, Key: str, UploadId: str) -> None:
        if self.abort_failure:
            raise RuntimeError("simulated cleanup failure")
        self.aborted.append(UploadId)
        self.uploads.pop(UploadId, None)

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **parameters: object) -> dict:
        self.objects[Key] = {"Body": bytes(Body), "Metadata": parameters.get("Metadata") or {}}
        return {"ETag": '"manifest"'}


def test_inspection_is_read_only_and_excludes_download_urls() -> None:
    data = b"verified-market-data"
    client = FakeBatchClient(data)
    plan = plan_ingest(config(), client, JOB_ID).public()
    rendered = json.dumps(plan)
    assert plan["downloadStarted"] is False
    assert plan["s3WriteStarted"] is False
    assert plan["fileCount"] == 1
    assert "token=secret" not in rendered
    assert client.open_offsets == []


def test_ingest_streams_multipart_verifies_hash_writes_manifest_and_reuses() -> None:
    data = b"verified-market-data"
    client = FakeBatchClient(data)
    s3 = FakeS3()
    events: list[dict] = []
    result = run_ingest(
        config(),
        client,  # type: ignore[arg-type]
        s3,
        job_id=JOB_ID,
        confirmed_job_id=JOB_ID,
        request_fingerprint="a" * 64,
        now=datetime(2026, 7, 18, 18, 0, tzinfo=UTC),
        progress=events.append,
    )

    data_key = f"flowdesk/raw/databento/jobs/{JOB_ID}/MES-20260714.mbo.dbn.zst"
    manifest_key = f"flowdesk/metadata/databento/jobs/{JOB_ID}/manifest.json"
    assert s3.objects[data_key]["Body"] == data
    assert s3.objects[data_key]["Metadata"]["sha256"] == hashlib.sha256(data).hexdigest()
    manifest = json.loads(s3.objects[manifest_key]["Body"])
    assert manifest["status"] == "COMPLETED"
    assert manifest["automaticOrderExecution"] is False
    assert manifest["requestFingerprint"] == "a" * 64
    assert "token=secret" not in json.dumps(manifest)
    assert result["status"] == "COMPLETED"
    assert [event["event"] for event in events] == [
        "JOB_VALIDATED", "FILE_STARTED", "FILE_UPLOADED", "JOB_COMPLETED"
    ]

    first_download_count = len(client.open_offsets)
    repeated_events: list[dict] = []
    repeated = run_ingest(
        config(),
        client,  # type: ignore[arg-type]
        s3,
        job_id=JOB_ID,
        confirmed_job_id=JOB_ID,
        progress=repeated_events.append,
    )
    assert repeated["files"][0]["reused"] is True
    assert len(client.open_offsets) == first_download_count
    assert any(event["event"] == "FILE_REUSED" for event in repeated_events)


def test_interrupted_download_resumes_only_from_committed_part() -> None:
    data = b"abcdefghijkl"
    client = FakeBatchClient(data, first_response_limit=5)
    s3 = FakeS3()
    run_ingest(
        config(),
        client,  # type: ignore[arg-type]
        s3,
        job_id=JOB_ID,
        confirmed_job_id=JOB_ID,
    )
    assert client.open_offsets == [0, 4]
    key = f"flowdesk/raw/databento/jobs/{JOB_ID}/MES-20260714.mbo.dbn.zst"
    assert s3.objects[key]["Body"] == data


def test_hash_mismatch_aborts_multipart_and_writes_no_manifest() -> None:
    actual = b"actual-bytes"
    claimed = b"different-bytes"
    client = FakeBatchClient(actual, files=[file_item(claimed, size=len(actual))])
    s3 = FakeS3()
    with pytest.raises(IngestError, match="SHA-256"):
        run_ingest(
            config(), client, s3,  # type: ignore[arg-type]
            job_id=JOB_ID, confirmed_job_id=JOB_ID,
        )
    assert s3.aborted == ["upload-1"]
    assert not s3.objects


def test_multipart_cleanup_failure_is_reported() -> None:
    actual = b"actual-bytes"
    claimed = b"different-bytes"
    client = FakeBatchClient(actual, files=[file_item(claimed, size=len(actual))])
    s3 = FakeS3()
    s3.abort_failure = True
    with pytest.raises(IngestError, match="cleanup also failed"):
        run_ingest(
            config(), client, s3,  # type: ignore[arg-type]
            job_id=JOB_ID, confirmed_job_id=JOB_ID,
        )


def test_path_traversal_and_non_done_jobs_are_blocked_before_download() -> None:
    data = b"data"
    traversal = FakeBatchClient(data, files=[file_item(data, filename="../market.dbn.zst")])
    with pytest.raises(IngestError, match="unsafe"):
        plan_ingest(config(), traversal, JOB_ID)
    assert traversal.open_offsets == []

    pending = FakeBatchClient(data, details=job_details(state="processing"))
    with pytest.raises(IngestError, match="done state"):
        plan_ingest(config(), pending, JOB_ID)
    assert pending.open_offsets == []


def test_confirmation_and_existing_object_collision_fail_closed() -> None:
    data = b"market-data"
    client = FakeBatchClient(data)
    with pytest.raises(IngestError, match="confirmation"):
        run_ingest(
            config(), client, FakeS3(),  # type: ignore[arg-type]
            job_id=JOB_ID, confirmed_job_id="GLBX-20260716-AAAAAAAAAA",
        )
    assert client.metadata_calls == 0

    s3 = FakeS3()
    key = f"flowdesk/raw/databento/jobs/{JOB_ID}/MES-20260714.mbo.dbn.zst"
    s3.objects[key] = {"Body": b"other", "Metadata": {"sha256": "0" * 64}}
    with pytest.raises(IngestError, match="conflicts"):
        run_ingest(
            config(), client, s3,  # type: ignore[arg-type]
            job_id=JOB_ID, confirmed_job_id=JOB_ID,
        )
    assert s3.objects[key]["Body"] == b"other"
    assert client.open_offsets == []
