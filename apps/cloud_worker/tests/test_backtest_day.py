from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from apps.cloud_worker import backtest_day
from apps.cloud_worker.backtest_day import BacktestDayError, run_backtest_day
from apps.cloud_worker.config import WorkerConfig


JOB_ID = "GLBX-20260723-4BH5UYFQSY"
BUCKET = "flowdesk-demo-bucket-eu-central-1"
SESSION_DATE = "2026-04-26"
FILENAME = "glbx-mdp3-20260426.mbo.dbn.zst"


def config() -> WorkerConfig:
    return WorkerConfig(
        region="eu-central-1",
        bucket=BUCKET,
        prefix="flowdesk",
        parameter_name="/flowdesk/databento/api-key",
        max_files=128,
        max_file_bytes=1_000_000,
        max_job_bytes=2_000_000,
        multipart_part_bytes=4 * 1024 * 1024,
        http_connect_timeout_seconds=1,
        http_read_timeout_seconds=10,
        http_max_retries=2,
    )


class FakeClientError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeBody:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def read(self, size: int | None = None) -> bytes:
        if size is None or size < 0:
            size = len(self.data) - self.offset
        chunk = self.data[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class FakeS3:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = dict(objects)
        self.puts: list[dict[str, Any]] = []

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        if Key not in self.objects:
            raise FakeClientError("NoSuchKey")
        return {"Body": FakeBody(self.objects[Key])}

    def put_object(self, **parameters: Any) -> dict[str, str]:
        self.puts.append(parameters)
        self.objects[str(parameters["Key"])] = bytes(parameters["Body"])
        return {"ETag": '"result"'}


def manifest(data: bytes) -> dict[str, Any]:
    return {
        "status": "COMPLETED",
        "jobId": JOB_ID,
        "files": [
            {
                "filename": FILENAME,
                "sizeBytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "s3Key": f"flowdesk/raw/databento/jobs/{JOB_ID}/{FILENAME}",
            }
        ],
    }


def test_backtest_day_streams_verifies_runs_and_writes_encrypted_result(tmp_path, monkeypatch) -> None:
    data = b"small-dbn-fixture"
    manifest_key = f"flowdesk/metadata/databento/jobs/{JOB_ID}/manifest.json"
    raw_key = f"flowdesk/raw/databento/jobs/{JOB_ID}/{FILENAME}"
    s3 = FakeS3(
        {
            manifest_key: json.dumps(manifest(data)).encode(),
            raw_key: data,
        }
    )
    observed: dict[str, Any] = {}

    def fake_runner(path: Path, **parameters: Any) -> dict[str, Any]:
        observed["bytes"] = path.read_bytes()
        observed.update(parameters)
        return {
            "sessionDate": SESSION_DATE,
            "sourceFingerprint": hashlib.sha256(data).hexdigest(),
            "fillMode": "realistic",
            "seed": 7,
            "eventsProcessed": 123,
            "topCandidates": [{"strategyName": "MES L3 Momentum"}],
            "realisticExecutionGate": {"passed": False, "reason": "NEGATIVE_EXPECTANCY"},
            "automaticOrderExecution": False,
            "paperPromotionAllowed": False,
            "profitabilityClaim": False,
        }

    monkeypatch.setattr(backtest_day, "run_daily_strategy_backtest", fake_runner)
    scratch = Path("/tmp") / f"flowdesk-{tmp_path.name}"
    result = run_backtest_day(
        config(),
        s3,
        job_id=JOB_ID,
        session_date=SESSION_DATE,
        confirmed_session_date=SESSION_DATE,
        scratch_directory=scratch,
    )

    assert observed["bytes"] == data
    assert observed["session_date"] == SESSION_DATE
    assert observed["data_fingerprint"] == hashlib.sha256(data).hexdigest()
    assert result["eventsProcessed"] == 123
    assert result["automaticOrderExecution"] is False
    assert len(s3.puts) == 1
    written = s3.puts[0]
    assert written["ServerSideEncryption"] == "AES256"
    assert written["IfNoneMatch"] == "*"
    assert written["Metadata"]["source-sha256"] == hashlib.sha256(data).hexdigest()
    assert json.loads(written["Body"])["profitabilityClaim"] is False


def test_backtest_day_requires_exact_date_confirmation() -> None:
    with pytest.raises(BacktestDayError, match="exactly match"):
        run_backtest_day(
            config(),
            FakeS3({}),
            job_id=JOB_ID,
            session_date=SESSION_DATE,
            confirmed_session_date="2026-04-27",
            scratch_directory=Path("/tmp/flowdesk-test"),
        )


def test_backtest_day_accepts_the_private_scratch_volume_root(tmp_path, monkeypatch) -> None:
    temporary_directory = tempfile.TemporaryDirectory
    data = b"small-dbn-fixture"
    manifest_key = f"flowdesk/metadata/databento/jobs/{JOB_ID}/manifest.json"
    raw_key = f"flowdesk/raw/databento/jobs/{JOB_ID}/{FILENAME}"
    s3 = FakeS3(
        {
            manifest_key: json.dumps(manifest(data)).encode(),
            raw_key: data,
        }
    )
    monkeypatch.setattr(
        backtest_day,
        "run_daily_strategy_backtest",
        lambda path, **_: {
            "sessionDate": SESSION_DATE,
            "sourceFingerprint": hashlib.sha256(data).hexdigest(),
            "fillMode": "realistic",
            "seed": 7,
            "eventsProcessed": 1,
            "topCandidates": [],
            "realisticExecutionGate": None,
            "automaticOrderExecution": False,
            "paperPromotionAllowed": False,
            "profitabilityClaim": False,
        },
    )
    scratch = Path("/scratch")
    # Avoid touching the host root in this unit test while still exercising the exact
    # accepted value used by Fargate.
    monkeypatch.setattr(Path, "mkdir", lambda self, **_: None)
    monkeypatch.setattr(
        backtest_day.tempfile,
        "TemporaryDirectory",
        lambda **_: temporary_directory(dir=tmp_path),
    )
    result = run_backtest_day(
        config(),
        s3,
        job_id=JOB_ID,
        session_date=SESSION_DATE,
        confirmed_session_date=SESSION_DATE,
        scratch_directory=scratch,
    )
    assert result["eventsProcessed"] == 1


def test_backtest_day_reports_an_unwritable_scratch_volume_safely(monkeypatch) -> None:
    data = b"small-dbn-fixture"
    manifest_key = f"flowdesk/metadata/databento/jobs/{JOB_ID}/manifest.json"
    raw_key = f"flowdesk/raw/databento/jobs/{JOB_ID}/{FILENAME}"
    s3 = FakeS3(
        {
            manifest_key: json.dumps(manifest(data)).encode(),
            raw_key: data,
        }
    )
    monkeypatch.setattr(
        backtest_day.tempfile,
        "TemporaryDirectory",
        lambda **_: (_ for _ in ()).throw(PermissionError("host details")),
    )

    with pytest.raises(BacktestDayError, match="private scratch volume is not writable"):
        run_backtest_day(
            config(),
            s3,
            job_id=JOB_ID,
            session_date=SESSION_DATE,
            confirmed_session_date=SESSION_DATE,
            scratch_directory=Path("/scratch"),
        )
