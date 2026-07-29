from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from .config import WorkerConfig


_DATABENTO_KEY_PATTERN = re.compile(r"^db-[A-Za-z0-9_-]{20,200}$")


class AwsClientError(RuntimeError):
    """A redacted AWS integration failure."""


@dataclass(frozen=True)
class AwsClients:
    s3: Any
    ssm: Any


def create_aws_clients(
    config: WorkerConfig,
    *,
    session_factory: Callable[..., Any] | None = None,
) -> AwsClients:
    """Create clients from the Fargate task credentials, never static keys."""

    if session_factory is None:
        import boto3

        session_factory = boto3.Session
    session = session_factory(region_name=config.region)
    return AwsClients(s3=session.client("s3"), ssm=session.client("ssm"))


def load_databento_api_key(config: WorkerConfig, ssm_client: Any) -> str:
    try:
        response = ssm_client.get_parameter(Name=config.parameter_name, WithDecryption=True)
    except Exception as exc:
        raise AwsClientError("The Databento parameter could not be read by the task role.") from exc

    value = str((response.get("Parameter") or {}).get("Value") or "").strip()
    if not _DATABENTO_KEY_PATTERN.fullmatch(value):
        raise AwsClientError("The Databento parameter is empty or has an invalid key format.")
    return value
