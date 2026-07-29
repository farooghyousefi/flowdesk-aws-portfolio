from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[4]
ENV_FILE = REPO_ROOT / ".env.local"
DATA_ROOT = REPO_ROOT / "data" / "databento"
RAW_ROOT = DATA_ROOT / "raw" / "MES"
ESTIMATE_ROOT = DATA_ROOT / "estimates"

DATASET = "GLBX.MDP3"
SCHEMA = "mbo"
REFERENCE_SCHEMA = "mbp-10"
DEFAULT_SYMBOL = "MES.v.0"
STYPE_IN = "continuous"
MAX_DURATION = timedelta(minutes=60)
MAX_VERIFICATION_DURATION = timedelta(seconds=2)
ESTIMATE_MAX_AGE = timedelta(minutes=30)
DEFAULT_VERIFICATION_LIMIT = 1_000
HARD_VERIFICATION_LIMIT = 10_000
FALLBACK_VERIFICATION_LIMIT = 100

_KEY_PATTERN = re.compile(r"db-[A-Za-z0-9_-]{20,}")
_NANOSECOND_ISO_PATTERN = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T(?P<time>\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d{1,9}))?(?P<timezone>Z|[+-]\d{2}:\d{2})$"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class ConnectorError(ValueError):
    """Expected connector failure that is safe to show after redaction."""


@dataclass(frozen=True)
class ConnectorConfig:
    api_key: str
    max_request_cost_usd: Decimal
    max_daily_cost_usd: Decimal
    max_weekly_cost_usd: Decimal = Decimal("15.00")
    max_monthly_cost_usd: Decimal = Decimal("40.00")
    download_confirmation_required: bool = True


@dataclass(frozen=True)
class HistoricalRequest:
    start: datetime
    end: datetime
    symbol: str = DEFAULT_SYMBOL
    dataset: str = DATASET
    schema: str = SCHEMA
    stype_in: str = STYPE_IN
    limit: int | None = None
    instrument_id: int | None = None
    raw_symbol: str | None = None
    start_nanoseconds: int | None = None
    end_nanoseconds: int | None = None

    @property
    def duration(self) -> timedelta:
        if self.start_nanoseconds is not None and self.end_nanoseconds is not None:
            return timedelta(microseconds=(self.end_nanoseconds - self.start_nanoseconds) / 1_000)
        return self.end - self.start

    @property
    def start_iso(self) -> str:
        if self.start_nanoseconds is not None:
            return format_utc_nanoseconds(self.start_nanoseconds)
        return format_utc(self.start)

    @property
    def end_iso(self) -> str:
        if self.end_nanoseconds is not None:
            return format_utc_nanoseconds(self.end_nanoseconds)
        return format_utc(self.end)


def _positive_decimal(value: str | None, default: str, name: str) -> Decimal:
    try:
        parsed = Decimal((value or default).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ConnectorError(f"{name} must be a valid USD amount.") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ConnectorError(f"{name} must be greater than zero.")
    return parsed


def load_config(
    *,
    env_file: Path = ENV_FILE,
    environ: Mapping[str, str] | None = None,
) -> ConnectorConfig:
    if environ is None:
        load_dotenv(env_file, override=False)
        values: Mapping[str, str] = os.environ
    else:
        values = environ

    api_key = values.get("DATABENTO_API_KEY", "").strip()
    if not api_key:
        raise ConnectorError("DATABENTO_API_KEY is not configured in .env.local.")

    return ConnectorConfig(
        api_key=api_key,
        max_request_cost_usd=_positive_decimal(
            values.get("DATABENTO_MAX_REQUEST_COST_USD"),
            "1.00",
            "DATABENTO_MAX_REQUEST_COST_USD",
        ),
        max_daily_cost_usd=_positive_decimal(
            values.get("DATABENTO_MAX_DAILY_COST_USD"),
            "5.00",
            "DATABENTO_MAX_DAILY_COST_USD",
        ),
        max_weekly_cost_usd=_positive_decimal(
            values.get("DATABENTO_MAX_WEEKLY_COST_USD"),
            "15.00",
            "DATABENTO_MAX_WEEKLY_COST_USD",
        ),
        max_monthly_cost_usd=_positive_decimal(
            values.get("DATABENTO_MAX_MONTHLY_COST_USD"),
            "40.00",
            "DATABENTO_MAX_MONTHLY_COST_USD",
        ),
        download_confirmation_required=(
            values.get("DATABENTO_DOWNLOAD_CONFIRMATION_REQUIRED", "true").strip().lower()
            not in {"0", "false", "no", "off"}
        ),
    )


def parse_utc(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConnectorError(f"{field_name} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise ConnectorError(f"{field_name} must include a timezone.")
    return parsed.astimezone(UTC)


def format_utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc_nanoseconds(value: str | int, field_name: str) -> int:
    if isinstance(value, int):
        if value < 0:
            raise ConnectorError(f"{field_name} must be on or after the UNIX epoch.")
        return value
    match = _NANOSECOND_ISO_PATTERN.fullmatch(value.strip())
    if match is None:
        raise ConnectorError(f"{field_name} must be an ISO-8601 timestamp with a timezone.")
    base = parse_utc(
        f"{match.group('date')}T{match.group('time')}{match.group('timezone')}",
        field_name,
    )
    delta = base - _EPOCH
    seconds = delta.days * 86_400 + delta.seconds
    fraction = (match.group("fraction") or "").ljust(9, "0")
    return seconds * 1_000_000_000 + int(fraction or "0")


def datetime_from_nanoseconds(value: int) -> datetime:
    seconds, nanoseconds = divmod(value, 1_000_000_000)
    return _EPOCH + timedelta(seconds=seconds, microseconds=nanoseconds // 1_000)


def format_utc_nanoseconds(value: int) -> str:
    seconds, nanoseconds = divmod(value, 1_000_000_000)
    base = (_EPOCH + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{nanoseconds:09d}Z"


def validate_verification_limit(limit: int) -> int:
    if isinstance(limit, bool) or limit < 1:
        raise ConnectorError("Record limit must be at least 1.")
    if limit > HARD_VERIFICATION_LIMIT:
        raise ConnectorError(
            f"Record limit exceeds the hard maximum of {HARD_VERIFICATION_LIMIT}."
        )
    return limit


def validate_symbol(symbol: str) -> str:
    normalized = symbol.strip()
    if not normalized:
        raise ConnectorError("Symbol must not be empty.")
    if normalized.upper() == "ALL_SYMBOLS":
        raise ConnectorError("ALL_SYMBOLS is blocked.")
    if any(character in normalized for character in ("*", "?", "[", "]")):
        raise ConnectorError("Wildcards are blocked.")
    if "," in normalized or any(character.isspace() for character in normalized):
        raise ConnectorError("Multiple symbols are blocked.")
    if normalized != DEFAULT_SYMBOL:
        raise ConnectorError(f"Only {DEFAULT_SYMBOL} is allowed.")
    return normalized


def build_request(
    start: str,
    end: str,
    symbol: str = DEFAULT_SYMBOL,
    *,
    schema: str = SCHEMA,
) -> HistoricalRequest:
    if schema not in {SCHEMA, REFERENCE_SCHEMA}:
        raise ConnectorError(f"Unsupported Databento schema: {schema}")
    request = HistoricalRequest(
        start=parse_utc(start, "Start"),
        end=parse_utc(end, "End"),
        symbol=validate_symbol(symbol),
        schema=schema,
    )
    if request.end <= request.start:
        raise ConnectorError("End must be after start.")
    if request.duration > MAX_DURATION:
        raise ConnectorError("Requests longer than 60 minutes are blocked.")
    return request


def build_verification_request(
    start: str | int,
    end: str | int,
    instrument_id: int,
    raw_symbol: str,
    *,
    limit: int = DEFAULT_VERIFICATION_LIMIT,
) -> HistoricalRequest:
    validated_limit = validate_verification_limit(limit)
    if instrument_id <= 0:
        raise ConnectorError("A positive concrete instrument ID is required.")
    normalized_raw_symbol = raw_symbol.strip()
    if not normalized_raw_symbol:
        raise ConnectorError("A uniquely resolved raw symbol is required.")
    start_ns = parse_utc_nanoseconds(start, "Start")
    end_ns = parse_utc_nanoseconds(end, "End")
    if end_ns <= start_ns:
        raise ConnectorError("End must be after start.")
    if end_ns - start_ns > int(MAX_VERIFICATION_DURATION.total_seconds() * 1_000_000_000):
        raise ConnectorError("Verification windows longer than 2 seconds are blocked.")
    return HistoricalRequest(
        start=datetime_from_nanoseconds(start_ns),
        end=datetime_from_nanoseconds(end_ns),
        symbol=str(instrument_id),
        schema=REFERENCE_SCHEMA,
        stype_in="instrument_id",
        limit=validated_limit,
        instrument_id=instrument_id,
        raw_symbol=normalized_raw_symbol,
        start_nanoseconds=start_ns,
        end_nanoseconds=end_ns,
    )


def safe_error(error: BaseException, secrets: tuple[str, ...] = ()) -> str:
    message = str(error)
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return _KEY_PATTERN.sub("[REDACTED]", message)


def list_data_files(root: Path = RAW_ROOT) -> list[Path]:
    resolved_root = root.resolve()
    return sorted(
        (path.resolve() for path in resolved_root.glob("*/*.dbn.zst")),
        key=lambda path: str(path.relative_to(resolved_root)),
    )


def resolve_data_file(
    file_arg: str | None,
    *,
    latest: bool = False,
    root: Path = RAW_ROOT,
) -> Path:
    raw_root = root.resolve()
    if file_arg and latest:
        raise ConnectorError("Use either --file or --latest, not both.")
    if file_arg:
        path = Path(file_arg)
        if not path.is_absolute():
            path = REPO_ROOT / path
        path = path.resolve()
    else:
        candidates = list_data_files(root)
        if not candidates:
            raise ConnectorError("No Databento DBN file was found. Pass --file explicitly.")
        path = candidates[-1]

    if not path.is_relative_to(raw_root):
        raise ConnectorError("The DBN file must be inside data/databento/raw/MES.")
    if not path.is_file():
        raise ConnectorError(f"DBN file does not exist: {path}")
    if not path.name.endswith(".dbn.zst"):
        raise ConnectorError("Expected a .dbn.zst file.")
    return path


def announce_data_file_selection(
    path: Path,
    *,
    file_arg: str | None,
    latest: bool,
    root: Path = RAW_ROOT,
) -> None:
    if file_arg:
        print(f"Selected file: {path}")
        return
    print("Available Databento MBO files:")
    for candidate in list_data_files(root):
        print(f"- {candidate}")
    label = "Latest selected" if latest else "Automatically selected (deterministic path order)"
    print(f"{label}: {path}")
