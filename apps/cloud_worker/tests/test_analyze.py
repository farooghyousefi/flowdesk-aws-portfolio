from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import databento_dbn as dbn
import pytest

from apps.cloud_worker.analyze import AnalyzeError, run_analyze
from apps.cloud_worker.config import WorkerConfig


JOB_ID = "GLBX-20260722-B88RVQ5VXU"
BUCKET = "flowdesk-demo-bucket-eu-central-1"
FILENAME = "glbx-mdp3-20260716.mbo.dbn.zst"


def config() -> WorkerConfig:
    return WorkerConfig(
        region="eu-central-1",
        bucket=BUCKET,
        prefix="flowdesk",
        parameter_name="/flowdesk/databento/api-key",
        max_files=8,
        max_file_bytes=1_000_000,
        max_job_bytes=2_000_000,
        multipart_part_bytes=4 * 1024 * 1024,
        http_connect_timeout_seconds=1,
        http_read_timeout_seconds=10,
        http_max_retries=2,
    )


def synthetic_dbn_bytes() -> bytes:
    metadata = dbn.Metadata(
        dataset="GLBX.MDP3",
        start=1,
        end=5,
        stype_in=dbn.SType.CONTINUOUS,
        stype_out=dbn.SType.INSTRUMENT_ID,
        schema=dbn.Schema.MBO,
        symbols=["MES.v.0"],
    )
    records = [
        dbn.MBOMsg(
            publisher_id=1,
            instrument_id=123,
            ts_event=1,
            order_id=10,
            price=5_000_000_000_000,
            size=2,
            action=dbn.Action.ADD,
            side=dbn.Side.BID,
            ts_recv=1,
            flags=128,
            sequence=1,
        ),
        dbn.MBOMsg(
            publisher_id=1,
            instrument_id=123,
            ts_event=2,
            order_id=11,
            price=5_000_250_000_000,
            size=3,
            action=dbn.Action.ADD,
            side=dbn.Side.ASK,
            ts_recv=2,
            flags=128,
            sequence=2,
        ),
        dbn.MBOMsg(
            publisher_id=1,
            instrument_id=123,
            ts_event=3,
            order_id=10,
            price=5_000_000_000_000,
            size=2,
            action=dbn.Action.CANCEL,
            side=dbn.Side.BID,
            ts_recv=3,
            flags=128,
            sequence=3,
        ),
    ]
    return metadata.encode() + b"".join(bytes(record) for record in records)


def manifest_for(data: bytes, *, job_id: str = JOB_ID, status: str = "COMPLETED") -> dict[str, Any]:
    return {
        "manifestVersion": 1,
        "status": status,
        "jobId": job_id,
        "bucket": BUCKET,
        "files": [
            {
                "filename": FILENAME,
                "sizeBytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "s3Key": f"flowdesk/raw/databento/jobs/{job_id}/{FILENAME}",
                "reused": False,
            }
        ],
    }


class FakeClientError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeBody:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeS3:
    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects = dict(objects or {})
        self.requested_keys: list[str] = []

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        self.requested_keys.append(Key)
        if Key not in self.objects:
            raise FakeClientError("NoSuchKey")
        return {"Body": FakeBody(self.objects[Key])}


def manifest_key(job_id: str = JOB_ID) -> str:
    return f"flowdesk/metadata/databento/jobs/{job_id}/manifest.json"


def raw_key(job_id: str = JOB_ID) -> str:
    return f"flowdesk/raw/databento/jobs/{job_id}/{FILENAME}"


def test_analyze_decodes_dbn_from_s3_and_writes_nothing() -> None:
    data = synthetic_dbn_bytes()
    s3 = FakeS3(
        {
            manifest_key(): json.dumps(manifest_for(data)).encode(),
            raw_key(): data,
        }
    )
    events: list[dict] = []
    result = run_analyze(
        config(),
        s3,  # type: ignore[arg-type]
        job_id=JOB_ID,
        now=datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
        progress=events.append,
    )

    assert result["jobId"] == JOB_ID
    assert result["dataset"] == "GLBX.MDP3"
    assert result["schema"] == "mbo"
    assert result["recordCount"] == 3
    assert result["instrumentIds"] == [123]
    assert result["actionCounts"] == {"A": 2, "C": 1}
    assert result["curatedWriteStarted"] is False
    assert result["sourceKey"] == raw_key()
    assert [event["event"] for event in events] == [
        "MANIFEST_VALIDATED",
        "RAW_OBJECT_VERIFIED",
        "DBN_DECODED",
        "ANALYSIS_COMPLETED",
    ]
    assert s3.requested_keys == [manifest_key(), raw_key()]


def test_analyze_rejects_incomplete_manifest() -> None:
    data = synthetic_dbn_bytes()
    s3 = FakeS3(
        {
            manifest_key(): json.dumps(manifest_for(data, status="FAILED")).encode(),
            raw_key(): data,
        }
    )
    with pytest.raises(AnalyzeError, match="COMPLETED"):
        run_analyze(config(), s3, job_id=JOB_ID)  # type: ignore[arg-type]


def test_analyze_rejects_tampered_raw_bytes() -> None:
    data = synthetic_dbn_bytes()
    tampered = data + b"\x00"
    s3 = FakeS3(
        {
            manifest_key(): json.dumps(manifest_for(data)).encode(),
            raw_key(): tampered,
        }
    )
    with pytest.raises(AnalyzeError, match="size does not match"):
        run_analyze(config(), s3, job_id=JOB_ID)  # type: ignore[arg-type]


def test_analyze_rejects_manifest_for_wrong_job() -> None:
    data = synthetic_dbn_bytes()
    s3 = FakeS3(
        {
            manifest_key(): json.dumps(manifest_for(data, job_id="GLBX-20260101-AAAAAAAAAA")).encode(),
        }
    )
    with pytest.raises(AnalyzeError, match="different Databento job"):
        run_analyze(config(), s3, job_id=JOB_ID)  # type: ignore[arg-type]


def test_analyze_rejects_missing_manifest() -> None:
    s3 = FakeS3()
    with pytest.raises(AnalyzeError, match="not found"):
        run_analyze(config(), s3, job_id=JOB_ID)  # type: ignore[arg-type]
