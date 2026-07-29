from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Mapping


FRANKFURT_REGION = "eu-central-1"
DEFAULT_PARAMETER_NAME = "/flowdesk/databento/api-key"
MIB = 1024 * 1024
GIB = 1024 * MIB

_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_PREFIX_PATTERN = re.compile(r"^[a-z0-9][a-z0-9/_-]{0,127}$")


class WorkerConfigError(ValueError):
    """A safe configuration error that never contains secret values."""


@dataclass(frozen=True)
class WorkerConfig:
    region: str
    bucket: str
    prefix: str
    parameter_name: str
    max_files: int
    max_file_bytes: int
    max_job_bytes: int
    multipart_part_bytes: int
    http_connect_timeout_seconds: int
    http_read_timeout_seconds: int
    http_max_retries: int

    @property
    def raw_job_prefix(self) -> str:
        return f"{self.prefix}/raw/databento/jobs"

    @property
    def manifest_prefix(self) -> str:
        return f"{self.prefix}/metadata/databento/jobs"


def _positive_int(
    values: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = values.get(name)
    try:
        parsed = int(raw) if raw is not None else default
    except (TypeError, ValueError) as exc:
        raise WorkerConfigError(f"{name} must be an integer.") from exc
    if isinstance(raw, bool) or not minimum <= parsed <= maximum:
        raise WorkerConfigError(f"{name} must be between {minimum} and {maximum}.")
    return parsed


def load_worker_config(environ: Mapping[str, str]) -> WorkerConfig:
    region = (environ.get("FLOWDESK_AWS_REGION") or environ.get("AWS_REGION") or FRANKFURT_REGION).strip()
    if region != FRANKFURT_REGION:
        raise WorkerConfigError("The first Flowdesk worker release is restricted to eu-central-1.")

    bucket = (environ.get("FLOWDESK_S3_BUCKET") or "").strip()
    if not _BUCKET_PATTERN.fullmatch(bucket) or ".." in bucket:
        raise WorkerConfigError("FLOWDESK_S3_BUCKET must be a valid explicit S3 bucket name.")

    prefix = (environ.get("FLOWDESK_S3_PREFIX") or "flowdesk").strip().strip("/")
    if not _PREFIX_PATTERN.fullmatch(prefix) or "//" in prefix or ".." in prefix.split("/"):
        raise WorkerConfigError("FLOWDESK_S3_PREFIX must be a safe relative S3 prefix.")

    parameter_name = (environ.get("DATABENTO_PARAMETER_NAME") or DEFAULT_PARAMETER_NAME).strip()
    if parameter_name != DEFAULT_PARAMETER_NAME:
        raise WorkerConfigError(
            f"DATABENTO_PARAMETER_NAME must remain {DEFAULT_PARAMETER_NAME} for the least-privilege IAM policy."
        )

    max_files = _positive_int(
        environ, "FLOWDESK_MAX_FILES", 128, minimum=1, maximum=512
    )
    max_file_bytes = _positive_int(
        environ, "FLOWDESK_MAX_FILE_BYTES", 15 * GIB, minimum=1, maximum=100 * GIB
    )
    max_job_bytes = _positive_int(
        environ, "FLOWDESK_MAX_JOB_BYTES", 50 * GIB, minimum=1, maximum=500 * GIB
    )
    if max_job_bytes < max_file_bytes:
        raise WorkerConfigError("FLOWDESK_MAX_JOB_BYTES must be at least FLOWDESK_MAX_FILE_BYTES.")

    multipart_part_bytes = _positive_int(
        environ,
        "FLOWDESK_MULTIPART_PART_BYTES",
        16 * MIB,
        minimum=5 * MIB,
        maximum=128 * MIB,
    )
    if math.ceil(max_file_bytes / multipart_part_bytes) > 10_000:
        raise WorkerConfigError("Multipart size would exceed the S3 limit of 10,000 parts per object.")

    return WorkerConfig(
        region=region,
        bucket=bucket,
        prefix=prefix,
        parameter_name=parameter_name,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_job_bytes=max_job_bytes,
        multipart_part_bytes=multipart_part_bytes,
        http_connect_timeout_seconds=_positive_int(
            environ, "FLOWDESK_HTTP_CONNECT_TIMEOUT_SECONDS", 10, minimum=1, maximum=60
        ),
        http_read_timeout_seconds=_positive_int(
            environ, "FLOWDESK_HTTP_READ_TIMEOUT_SECONDS", 120, minimum=10, maximum=900
        ),
        http_max_retries=_positive_int(
            environ, "FLOWDESK_HTTP_MAX_RETRIES", 3, minimum=0, maximum=10
        ),
    )
