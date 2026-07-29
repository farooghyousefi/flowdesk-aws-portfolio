from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .config import (
    DATASET,
    DEFAULT_SYMBOL,
    FALLBACK_VERIFICATION_LIMIT,
    REFERENCE_SCHEMA,
    SCHEMA,
    ConnectorConfig,
    ConnectorError,
    HistoricalRequest,
    build_verification_request,
    datetime_from_nanoseconds,
    parse_utc_nanoseconds,
)
from .dbn_reader import F_LAST, F_SNAPSHOT, OrderBook, SnapshotStatus, iter_events, open_dbn, store_symbols
from .estimate import estimate_billable_size, estimate_cost, relevant_unit_price

TWO_SECONDS_NS = 2_000_000_000
TEN_MINUTES_NS = 600_000_000_000


@dataclass(frozen=True)
class MboVerificationContext:
    instrument_id: int
    snapshot_ready_timestamp: int
    first_natural_f_last_timestamp: int
    file_start_timestamp: int
    file_end_timestamp: int


@dataclass(frozen=True)
class ResolvedContract:
    input_symbol: str
    instrument_id: int
    raw_symbol: str


@dataclass(frozen=True)
class VerificationEstimate:
    request: HistoricalRequest
    billable_bytes: int
    unit_price_usd_per_gb: Decimal
    estimated_cost_usd: Decimal

    @property
    def billable_mib(self) -> Decimal:
        return Decimal(self.billable_bytes) / Decimal(1024 * 1024)


def _metadata_nanoseconds(value: Any) -> int:
    raw_value = getattr(value, "value", None)
    if raw_value is not None:
        return int(raw_value)
    timestamp = value.to_pydatetime() if hasattr(value, "to_pydatetime") else value
    return int(timestamp.timestamp() * 1_000_000_000)


def inspect_mbo_verification_context(path: Path) -> MboVerificationContext:
    store = open_dbn(path)
    if str(getattr(store, "dataset", "") or "") != DATASET:
        raise ConnectorError("The MBO source uses the wrong dataset.")
    if str(getattr(store, "schema", "") or "") != SCHEMA:
        raise ConnectorError("The verification source is not an MBO file.")
    if DEFAULT_SYMBOL not in store_symbols(store):
        raise ConnectorError(f"The MBO source does not identify {DEFAULT_SYMBOL}.")

    book = OrderBook()
    snapshot_ready: int | None = None
    first_natural_f_last: int | None = None
    instrument_ids: set[int] = set()
    for event in iter_events(path):
        instrument_ids.add(event.instrument_id)
        completed = book.apply(event)
        if (
            snapshot_ready is None
            and completed
            and event.flags & F_SNAPSHOT
            and book.snapshot_status == SnapshotStatus.SNAPSHOT_READY
        ):
            snapshot_ready = event.ts_event
            continue
        if (
            snapshot_ready is not None
            and completed
            and not event.flags & F_SNAPSHOT
            and event.flags & F_LAST
            and book.snapshot_status == SnapshotStatus.POST_SNAPSHOT
        ):
            first_natural_f_last = event.ts_event
            break

    if snapshot_ready is None:
        raise ConnectorError("Verification blocked: the MBO file has no complete snapshot.")
    if first_natural_f_last is None:
        raise ConnectorError("Verification blocked: no natural F_LAST state follows the snapshot.")
    if len(instrument_ids) != 1:
        raise ConnectorError("Verification blocked: the MBO instrument is not unique.")
    instrument_id = next(iter(instrument_ids))
    if instrument_id <= 0:
        raise ConnectorError("Verification blocked: the MBO instrument ID is invalid.")
    return MboVerificationContext(
        instrument_id=instrument_id,
        snapshot_ready_timestamp=snapshot_ready,
        first_natural_f_last_timestamp=first_natural_f_last,
        file_start_timestamp=_metadata_nanoseconds(store.start),
        file_end_timestamp=_metadata_nanoseconds(store.end),
    )


def resolve_verification_window(
    context: MboVerificationContext,
    start: str | None,
    end: str | None,
) -> tuple[int, int]:
    if bool(start) != bool(end):
        raise ConnectorError("Pass --start and --end together.")
    if start is None:
        start_ns = context.first_natural_f_last_timestamp
        end_ns = min(start_ns + TWO_SECONDS_NS, context.file_end_timestamp)
    else:
        start_ns = parse_utc_nanoseconds(start, "Start")
        end_ns = parse_utc_nanoseconds(end or "", "End")

    if start_ns < context.snapshot_ready_timestamp:
        raise ConnectorError("Verification blocked: start is before SNAPSHOT_READY.")
    if start_ns < context.first_natural_f_last_timestamp:
        raise ConnectorError(
            "Verification blocked: start precedes the first natural F_LAST state."
        )
    if end_ns <= start_ns:
        raise ConnectorError("End must be after start.")
    if end_ns - start_ns > TWO_SECONDS_NS:
        raise ConnectorError("Verification windows longer than 2 seconds are blocked.")
    if end_ns > context.file_end_timestamp:
        raise ConnectorError("Verification end exceeds the MBO file range.")
    return start_ns, end_ns


def _unique_resolution(response: dict[str, Any], symbol: str, label: str) -> str:
    if response.get("partial") or response.get("not_found"):
        raise ConnectorError(f"Verification blocked: {label} resolution was incomplete.")
    entries = (response.get("result") or {}).get(symbol) or []
    values = {str(entry.get("s", "")).strip() for entry in entries}
    values.discard("")
    if len(values) != 1:
        raise ConnectorError(f"Verification blocked: {label} did not resolve uniquely.")
    return next(iter(values))


def resolve_contract(
    client: Any,
    context: MboVerificationContext,
    start_ns: int,
    end_ns: int,
) -> ResolvedContract:
    start_date = datetime_from_nanoseconds(start_ns).date()
    end_date = datetime_from_nanoseconds(end_ns).date() + timedelta(days=1)
    continuous = client.symbology.resolve(
        dataset=DATASET,
        symbols=DEFAULT_SYMBOL,
        stype_in="continuous",
        stype_out="instrument_id",
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
    )
    resolved_instrument = int(
        _unique_resolution(continuous, DEFAULT_SYMBOL, "continuous instrument")
    )
    if resolved_instrument != context.instrument_id:
        raise ConnectorError(
            "Verification blocked: resolved instrument does not match the MBO file."
        )

    instrument_symbol = str(resolved_instrument)
    raw = client.symbology.resolve(
        dataset=DATASET,
        symbols=instrument_symbol,
        stype_in="instrument_id",
        stype_out="raw_symbol",
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
    )
    raw_symbol = _unique_resolution(raw, instrument_symbol, "raw symbol")
    return ResolvedContract(DEFAULT_SYMBOL, resolved_instrument, raw_symbol)


def build_reference_request(
    contract: ResolvedContract,
    start_ns: int,
    end_ns: int,
    limit: int,
) -> HistoricalRequest:
    return build_verification_request(
        start_ns,
        end_ns,
        contract.instrument_id,
        contract.raw_symbol,
        limit=limit,
    )


def query_verification_estimate(
    client: Any,
    request: HistoricalRequest,
    *,
    unit_price: Decimal | None = None,
) -> VerificationEstimate:
    price = unit_price if unit_price is not None else relevant_unit_price(client, request)
    billable_bytes = estimate_billable_size(client, request)
    cost = estimate_cost(client, request)
    return VerificationEstimate(request, billable_bytes, price, cost)


def estimate_with_fallback(
    client: Any,
    request: HistoricalRequest,
    config: ConnectorConfig,
) -> tuple[VerificationEstimate, VerificationEstimate | None]:
    primary = query_verification_estimate(client, request)
    fallback: VerificationEstimate | None = None
    if (
        primary.estimated_cost_usd > config.max_request_cost_usd
        and request.limit is not None
        and request.limit > FALLBACK_VERIFICATION_LIMIT
    ):
        fallback_request = replace(request, limit=FALLBACK_VERIFICATION_LIMIT)
        fallback = query_verification_estimate(
            client,
            fallback_request,
            unit_price=primary.unit_price_usd_per_gb,
        )
    return primary, fallback


def has_cost_precision_warning(request: HistoricalRequest) -> bool:
    if request.start_nanoseconds is None or request.end_nanoseconds is None:
        return False
    return (request.end_nanoseconds - request.start_nanoseconds) % TEN_MINUTES_NS != 0
