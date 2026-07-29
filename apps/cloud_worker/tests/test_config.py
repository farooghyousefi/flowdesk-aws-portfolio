from __future__ import annotations

import pytest

from apps.cloud_worker.aws_clients import AwsClientError, load_databento_api_key
from apps.cloud_worker.config import GIB, MIB, WorkerConfigError, load_worker_config


def environment(**overrides: str) -> dict[str, str]:
    return {
        "AWS_REGION": "eu-central-1",
        "FLOWDESK_S3_BUCKET": "flowdesk-demo-bucket-eu-central-1",
        **overrides,
    }


def test_worker_config_is_explicit_and_frankfurt_only() -> None:
    config = load_worker_config(environment())
    assert config.region == "eu-central-1"
    assert config.bucket == "flowdesk-demo-bucket-eu-central-1"
    assert config.raw_job_prefix == "flowdesk/raw/databento/jobs"
    assert config.parameter_name == "/flowdesk/databento/api-key"
    assert config.max_files == 128
    assert config.multipart_part_bytes == 16 * MIB
    assert config.max_file_bytes == 15 * GIB

    with pytest.raises(WorkerConfigError, match="eu-central-1"):
        load_worker_config(environment(AWS_REGION="us-east-1"))
    with pytest.raises(WorkerConfigError, match="bucket"):
        load_worker_config({"AWS_REGION": "eu-central-1"})
    with pytest.raises(WorkerConfigError, match="relative S3 prefix"):
        load_worker_config(environment(FLOWDESK_S3_PREFIX="flowdesk/../other"))
    with pytest.raises(WorkerConfigError, match="least-privilege"):
        load_worker_config(environment(DATABENTO_PARAMETER_NAME="/some/other/key"))


def test_worker_config_enforces_multipart_and_total_limits() -> None:
    with pytest.raises(WorkerConfigError, match="at least FLOWDESK_MAX_FILE_BYTES"):
        load_worker_config(
            environment(FLOWDESK_MAX_FILE_BYTES=str(2 * GIB), FLOWDESK_MAX_JOB_BYTES=str(GIB))
        )
    with pytest.raises(WorkerConfigError, match="between"):
        load_worker_config(environment(FLOWDESK_MULTIPART_PART_BYTES=str(MIB)))


class FakeSsm:
    def __init__(self, value: str | None = None, *, failure: bool = False) -> None:
        self.value = value
        self.failure = failure
        self.calls: list[dict] = []

    def get_parameter(self, **parameters: object) -> dict:
        self.calls.append(dict(parameters))
        if self.failure:
            raise RuntimeError("sensitive backend details")
        return {"Parameter": {"Value": self.value}}


def test_ssm_key_is_decrypted_and_never_returned_in_errors() -> None:
    config = load_worker_config(environment())
    key = "db-" + "a" * 32
    ssm = FakeSsm(key)
    assert load_databento_api_key(config, ssm) == key
    assert ssm.calls == [{"Name": "/flowdesk/databento/api-key", "WithDecryption": True}]

    with pytest.raises(AwsClientError) as invalid:
        load_databento_api_key(config, FakeSsm("MOCK_INVALID_SECRET_VALUE"))
    assert "MOCK_INVALID_SECRET_VALUE" not in str(invalid.value)

    with pytest.raises(AwsClientError) as failed:
        load_databento_api_key(config, FakeSsm(failure=True))
    assert "sensitive backend details" not in str(failed.value)
