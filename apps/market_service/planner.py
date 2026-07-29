from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import databento as db

from apps.connectors.databento.src.config import (
    DATASET,
    DEFAULT_SYMBOL,
    RAW_ROOT,
    ConnectorConfig,
    ConnectorError,
    load_config,
    safe_error,
)
from apps.connectors.databento.src.dbn_reader import DbnSummary, open_dbn, store_symbols, summarize_dbn, timestamp_iso
from .importer import import_file, sha256_file
from .storage import (
    connect,
    get_data_estimate,
    get_planner_state,
    list_dataset_jobs,
    list_sessions,
    save_data_estimate,
    save_dataset_job,
    save_planner_state,
    tracked_costs,
    update_estimate_status,
    set_session_split,
    utc_now,
)

ESTIMATE_TTL = timedelta(minutes=10)
TEN_MINUTES_SECONDS = 600
ENCODING = "dbn"
COMPRESSION = "zstd"
SPLIT_DURATION = "day"


@dataclass(frozen=True)
class ModeSpec:
    key: str
    label: str
    schemas: tuple[str, ...]
    request_scope: str
    available_features: tuple[str, ...]
    disabled_features: tuple[str, ...]
    suitability: tuple[str, ...]


MODE_SPECS = {
    "full_l3": ModeSpec(
        "full_l3", "Full L3 Research", ("mbo",), "utc_midnight",
        ("Complete DOM", "Queue structure", "Pulling / stacking", "Liquidity heatmap", "L3 candidates"),
        (), ("UI Practice", "Manual Replay", "L3 Research", "Strategy Backtest"),
    ),
    "economy": ModeSpec(
        "economy", "Orderflow Economy", ("trades", "ohlcv-1m"), "visible_with_context",
        ("Tape", "Trades", "Delta", "Footprint", "VWAP", "Volume"),
        ("Complete DOM", "Queue position", "L3 iceberg confirmation", "L3 pulling / stacking", "Full heatmap"),
        ("UI Practice", "Manual Replay", "Strategy Backtest"),
    ),
    "context": ModeSpec(
        "context", "Chart Context Only", ("ohlcv-1m",), "utc_midnight",
        ("1m / 5m / 15m context", "Session high / low", "Chart structure"),
        ("Tape", "Footprint", "DOM", "MBO", "L3 features"),
        ("UI Practice", "Strategy Context"),
    ),
}


def _parse_clock(value: str, name: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise ConnectorError(f"{name} must use HH:MM local time.") from exc


def _local_datetime(day: date, clock: time, timezone: str, name: str) -> datetime:
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ConnectorError(f"Unknown IANA timezone: {timezone}") from exc
    naive = datetime.combine(day, clock)
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    if first.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != naive:
        raise ConnectorError(f"{name} falls into a daylight-saving time gap.")
    if first.utcoffset() != second.utcoffset():
        raise ConnectorError(f"{name} is ambiguous at the daylight-saving time transition.")
    return first


def build_time_window(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        session_date = date.fromisoformat(str(payload.get("sessionDate") or payload.get("date") or ""))
    except ValueError as exc:
        raise ConnectorError("Date must use YYYY-MM-DD.") from exc
    timezone = str(payload.get("timezone") or "Europe/Berlin")
    replay_start = _local_datetime(
        session_date, _parse_clock(str(payload.get("replayStartLocal") or payload.get("replayStart") or "15:00"), "Replay start"), timezone, "Replay start"
    )
    replay_end = _local_datetime(
        session_date, _parse_clock(str(payload.get("replayEndLocal") or payload.get("replayEnd") or "16:30"), "Replay end"), timezone, "Replay end"
    )
    if replay_end <= replay_start:
        raise ConnectorError("Replay end must be after replay start on the selected day.")
    context_minutes = int(payload.get("contextMinutes", 30))
    if context_minutes < 0 or context_minutes > 1440:
        raise ConnectorError("Context must be between 0 and 1440 minutes.")
    return {
        "date": session_date,
        "timezone": timezone,
        "replay_start_local": replay_start,
        "replay_end_local": replay_end,
        "replay_start_utc": replay_start.astimezone(UTC),
        "replay_end_utc": replay_end.astimezone(UTC),
        "context_start_utc": (replay_start - timedelta(minutes=context_minutes)).astimezone(UTC),
        "context_minutes": context_minutes,
    }


def build_dataset_request_plan(payload: dict[str, Any]) -> dict[str, Any]:
    window = build_time_window(payload)
    return {
        "sessionDate": window["date"].isoformat(),
        "timezone": window["timezone"],
        "replayStartLocal": window["replay_start_local"].strftime("%H:%M"),
        "replayEndLocal": window["replay_end_local"].strftime("%H:%M"),
        "contextMinutes": window["context_minutes"],
        "replayStartUtc": _format_utc(window["replay_start_utc"]),
        "replayEndUtc": _format_utc(window["replay_end_utc"]),
        "requestStartUtc": _format_utc(window["context_start_utc"]),
        "requestEndUtc": _format_utc(window["replay_end_utc"]),
    }


def preview_request_plan(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    plan = build_dataset_request_plan(payload)
    if persist:
        save_planner_state(plan)
    return {"requestPlan": plan, "valid": True, "metadataRequested": False, "downloadStarted": False}


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_local(value: datetime) -> str:
    return value.isoformat(timespec="minutes")


def _unique_mapping(response: dict[str, Any], symbol: str, label: str) -> dict[str, str]:
    if response.get("partial") or response.get("not_found"):
        raise ConnectorError(f"Contract resolution is incomplete for {label}.")
    entries = (response.get("result") or {}).get(symbol) or []
    values = {str(entry.get("s", "")).strip() for entry in entries}
    values.discard("")
    if len(values) != 1 or not entries:
        raise ConnectorError("Contract resolution is ambiguous.")
    return {"value": next(iter(values)), "validFrom": str(entries[0].get("d0", "")), "validTo": str(entries[-1].get("d1", ""))}


def resolve_contract(client: Any, session_date: date, symbol: str = DEFAULT_SYMBOL) -> dict[str, Any]:
    end_date = session_date + timedelta(days=1)
    continuous = client.symbology.resolve(
        dataset=DATASET, symbols=symbol, stype_in="continuous", stype_out="instrument_id",
        start_date=session_date.isoformat(), end_date=end_date.isoformat(),
    )
    instrument = _unique_mapping(continuous, symbol, "continuous instrument")
    instrument_id = int(instrument["value"])
    raw = client.symbology.resolve(
        dataset=DATASET, symbols=str(instrument_id), stype_in="instrument_id", stype_out="raw_symbol",
        start_date=session_date.isoformat(), end_date=end_date.isoformat(),
    )
    raw_symbol = _unique_mapping(raw, str(instrument_id), "raw symbol")
    return {
        "inputSymbol": symbol,
        "rawSymbol": raw_symbol["value"],
        "instrumentId": instrument_id,
        "mappingValidFrom": max(instrument["validFrom"], raw_symbol["validFrom"]),
        "mappingValidTo": min(instrument["validTo"], raw_symbol["validTo"]),
    }


def planner_fingerprint(
    *, dataset: str, mode: str, schemas: tuple[str, ...], instrument_id: int,
    start_utc: str, end_utc: str,
) -> str:
    payload = {
        "dataset": dataset, "mode": mode, "schemas": list(schemas),
        "instrumentId": instrument_id, "startUtc": start_utc, "endUtc": end_utc,
        "encoding": ENCODING, "compression": COMPRESSION, "splitDuration": SPLIT_DURATION,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _historical_unit_prices(entries: list[dict[str, Any]]) -> dict[str, Decimal]:
    for entry in entries:
        if str(entry.get("mode")) == "historical":
            return {key: Decimal(str(value)) for key, value in (entry.get("unit_prices") or {}).items()}
    raise ConnectorError("Databento returned no historical unit-price table.")


def _local_reuse(
    mode: str, instrument_id: int, start_utc: datetime, end_utc: datetime,
    fingerprint: str,
) -> dict[str, Any] | None:
    for session in list_sessions():
        manifest_path = Path(f"{session['file_path']}.manifest.json")
        manifest: dict[str, Any] = {}
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text())
            except (OSError, json.JSONDecodeError):
                manifest = {}
        fingerprint_match = manifest.get("requestFingerprint") == fingerprint
        range_match = (
            session["instrument_id"] == instrument_id
            and datetime.fromisoformat(session["start_at"].replace("Z", "+00:00")) <= start_utc
            and datetime.fromisoformat(session["end_at"].replace("Z", "+00:00")) >= end_utc
        )
        quality_match = mode != "full_l3" or session["completeness"] == "complete"
        if quality_match and (fingerprint_match or range_match):
            return {"sessionId": session["id"], "file": session["file_path"], "action": "USE_LOCAL_COPY"}
    return None


def _mode_request_window(spec: ModeSpec, window: dict[str, Any], replay_end_override: datetime | None = None) -> tuple[datetime, datetime]:
    end = replay_end_override or window["replay_end_utc"]
    if spec.request_scope == "visible_with_context":
        return window["context_start_utc"], end
    return datetime.combine(window["replay_start_utc"].date(), time.min, tzinfo=UTC), end


def _metadata_call_parameters(schema: str, contract: dict[str, Any], start: str, end: str) -> dict[str, Any]:
    return {
        "dataset": DATASET,
        "schema": schema,
        "symbols": contract["instrumentId"],
        "stype_in": "instrument_id",
        "start": start,
        "end": end,
    }


def estimate_mode(
    client: Any,
    config: ConnectorConfig,
    payload: dict[str, Any],
    spec: ModeSpec,
    *,
    shared: dict[str, Any] | None = None,
    replay_end_override: datetime | None = None,
) -> dict[str, Any]:
    window = build_time_window(payload)
    contract = (shared or {}).get("contract") or resolve_contract(client, window["date"])
    request_start, request_end = _mode_request_window(spec, window, replay_end_override)
    start_iso, end_iso = _format_utc(request_start), _format_utc(request_end)
    available_schemas = (shared or {}).get("schemas") or client.metadata.list_schemas(DATASET)
    missing_schemas = [schema for schema in spec.schemas if schema not in available_schemas]
    if missing_schemas:
        raise ConnectorError("Databento schemas unavailable: " + ", ".join(missing_schemas))

    conditions = (shared or {}).get("conditions")
    if conditions is None:
        conditions = client.metadata.get_dataset_condition(
            DATASET, start_date=request_start.date().isoformat(), end_date=request_end.date().isoformat()
        )
    dataset_range = (shared or {}).get("range") or client.metadata.get_dataset_range(DATASET)
    unavailable = [item for item in conditions if str(item.get("condition")) != "available"]
    prices = (shared or {}).get("unit_prices")
    if prices is None:
        prices = _historical_unit_prices(client.metadata.list_unit_prices(DATASET))

    records = 0
    billable_bytes = 0
    raw_cost = Decimal("0")
    schema_details = []
    for schema in spec.schemas:
        parameters = _metadata_call_parameters(schema, contract, start_iso, end_iso)
        schema_records = int(client.metadata.get_record_count(**parameters))
        schema_bytes = int(client.metadata.get_billable_size(**parameters))
        schema_cost = Decimal(str(client.metadata.get_cost(**parameters)))
        records += schema_records
        billable_bytes += schema_bytes
        raw_cost += schema_cost
        schema_details.append({
            "schema": schema, "records": schema_records, "billableBytes": schema_bytes,
            "estimatedCostUsd": float(schema_cost), "unitPriceUsdPerGiB": float(prices.get(schema, Decimal("0"))),
        })

    fingerprint = planner_fingerprint(
        dataset=DATASET, mode=spec.key, schemas=spec.schemas, instrument_id=contract["instrumentId"],
        start_utc=start_iso, end_utc=end_iso,
    )
    reuse = _local_reuse(spec.key, contract["instrumentId"], request_start, request_end, fingerprint)
    costs = tracked_costs()
    warnings: list[str] = []
    duration_seconds = int((request_end - request_start).total_seconds())
    exact_blocks = duration_seconds % TEN_MINUTES_SECONDS == 0
    confidence = "HIGH" if exact_blocks and not unavailable else "MEDIUM" if not unavailable else "LOW"
    if not exact_blocks:
        warnings.append("Databento may overestimate cost, size, and records outside exact 10-minute blocks.")
    if unavailable:
        warnings.append("Dataset condition is not fully available for the selected range.")
    schema_ranges = dataset_range.get("schema", {}) if isinstance(dataset_range, dict) else {}
    for schema in spec.schemas:
        schema_range = schema_ranges.get(schema, {})
        if schema_range and end_iso > str(schema_range.get("end", end_iso)):
            warnings.append(f"{schema} is not yet available through the requested end time.")

    estimated_cost = Decimal("0") if reuse else raw_cost
    daily_remaining = max(Decimal("0"), config.max_daily_cost_usd - Decimal(str(costs["downloadedToday"])))
    weekly_remaining = max(Decimal("0"), config.max_weekly_cost_usd - Decimal(str(costs["downloadedWeek"])))
    monthly_remaining = max(Decimal("0"), config.max_monthly_cost_usd - Decimal(str(costs["downloadedMonth"])))
    target_reserve = (raw_cost * Decimal("0.10")).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    reserve_headroom = max(Decimal("0"), config.max_request_cost_usd - raw_cost)
    safety_reserve = Decimal("0") if reuse else min(target_reserve, reserve_headroom)
    maximum_authorized = Decimal("0") if reuse else raw_cost + safety_reserve
    allowed = not warnings and (
        reuse is not None
        or (
            raw_cost <= config.max_request_cost_usd
            and maximum_authorized <= config.max_request_cost_usd
            and maximum_authorized <= daily_remaining
            and maximum_authorized <= weekly_remaining
            and maximum_authorized <= monthly_remaining
        )
    )
    if not reuse and raw_cost > config.max_request_cost_usd:
        warnings.append("Configured request cost limit would be exceeded.")
    if not reuse and maximum_authorized > daily_remaining:
        warnings.append("Local tracked daily budget would be exceeded.")
    if not reuse and maximum_authorized > weekly_remaining:
        warnings.append("Local tracked weekly budget would be exceeded.")
    if not reuse and maximum_authorized > monthly_remaining:
        warnings.append("Local tracked monthly budget would be exceeded.")

    created = datetime.now(UTC)
    request_plan = build_dataset_request_plan(payload)
    storage_payload = {
        "id": str(uuid.uuid4()), "request_fingerprint": fingerprint, "dataset": DATASET,
        "mode": spec.key, "schemas_json": json.dumps(spec.schemas), "input_symbol": DEFAULT_SYMBOL,
        "raw_symbol": contract["rawSymbol"], "instrument_id": contract["instrumentId"],
        "start_utc": start_iso, "end_utc": end_iso,
        "replay_start": _format_local(window["replay_start_local"]),
        "replay_end": _format_local(window["replay_end_local"]), "timezone": window["timezone"],
        "estimated_cost": float(estimated_cost), "estimated_records": records,
        "billable_bytes": billable_bytes, "unit_price_json": json.dumps({k: float(v) for k, v in prices.items() if k in spec.schemas}),
        "local_reuse": int(reuse is not None), "allowed": int(allowed), "confidence": confidence,
        "warnings_json": json.dumps(warnings),
        "metadata_json": json.dumps({
            "rawEstimatedCostUsd": float(raw_cost), "safetyReserveUsd": float(safety_reserve),
            "targetSafetyReserveUsd": float(target_reserve),
            "maximumAuthorizedUsd": float(maximum_authorized), "schemaDetails": schema_details,
            "contract": contract, "conditions": conditions, "datasetRange": dataset_range,
            "availableFeatures": spec.available_features, "disabledFeatures": spec.disabled_features,
            "suitability": spec.suitability, "reuse": reuse, "durationSeconds": duration_seconds,
            "requestPlan": request_plan,
            "requestLimitUsd": float(config.max_request_cost_usd),
            "authorizationPolicy": "maximum_authorized_includes_effective_reserve_and_is_checked_against_all_budgets",
            "dailyLimitUsd": float(config.max_daily_cost_usd), "weeklyLimitUsd": float(config.max_weekly_cost_usd),
            "monthlyLimitUsd": float(config.max_monthly_cost_usd), "dailyRemainingUsd": float(daily_remaining),
            "weeklyRemainingUsd": float(weekly_remaining), "monthlyRemainingUsd": float(monthly_remaining),
        }),
        "created_at": _format_utc(created), "expires_at": _format_utc(created + ESTIMATE_TTL),
        "status": "LOCAL_REUSE" if reuse else "AWAITING_CONFIRMATION" if allowed else "BLOCKED",
        "job_id": None, "actual_local_size": None, "downloaded_at": None,
    }
    return estimate_public(save_data_estimate(storage_payload))


def _shared_metadata(client: Any, window: dict[str, Any]) -> dict[str, Any]:
    contract = resolve_contract(client, window["date"])
    return {
        "contract": contract,
        "schemas": client.metadata.list_schemas(DATASET),
        "conditions": client.metadata.get_dataset_condition(
            DATASET, start_date=window["replay_start_utc"].date().isoformat(), end_date=window["replay_end_utc"].date().isoformat()
        ),
        "range": client.metadata.get_dataset_range(DATASET),
        "unit_prices": _historical_unit_prices(client.metadata.list_unit_prices(DATASET)),
    }


def estimate_plan(
    payload: dict[str, Any],
    *,
    client_factory: Callable[[str], Any] | None = None,
    config: ConnectorConfig | None = None,
) -> dict[str, Any]:
    if str(payload.get("dataset") or DATASET) != DATASET:
        raise ConnectorError(f"Only {DATASET} is supported.")
    if str(payload.get("symbol") or DEFAULT_SYMBOL) != DEFAULT_SYMBOL:
        raise ConnectorError(f"Only {DEFAULT_SYMBOL} is supported.")
    request_plan = build_dataset_request_plan(payload)
    save_planner_state(request_plan)
    active_config = config or load_config()
    factory = client_factory or db.Historical
    client = factory(active_config.api_key)
    window = build_time_window(payload)
    shared = _shared_metadata(client, window)
    estimates = [estimate_mode(client, active_config, payload, spec, shared=shared) for spec in MODE_SPECS.values()]
    return {
        "generatedAt": utc_now(),
        "input": planner_input_public(payload, window),
        "contract": shared["contract"],
        "estimates": estimates,
        "costs": tracked_costs(),
        "downloadStarted": False,
        "message": "Estimate completed. No market data was downloaded.",
    }


def optimize_plan(
    payload: dict[str, Any],
    *,
    client_factory: Callable[[str], Any] | None = None,
    config: ConnectorConfig | None = None,
) -> dict[str, Any]:
    plan = estimate_plan(payload, client_factory=client_factory, config=config)
    alternatives = [{"label": item["label"], "estimate": item} for item in plan["estimates"]]
    window = build_time_window(payload)
    cutoff = _local_datetime(window["date"], time(16, 30), window["timezone"], "Cost optimizer cutoff")
    if window["replay_start_local"] < cutoff < window["replay_end_local"]:
        active_config = config or load_config()
        client = (client_factory or db.Historical)(active_config.api_key)
        shared = _shared_metadata(client, window)
        shortened = estimate_mode(
            client, active_config, payload, MODE_SPECS["full_l3"], shared=shared,
            replay_end_override=cutoff.astimezone(UTC),
        )
        shortened["label"] = "Full L3 until 16:30"
        alternatives.insert(1, {"label": "Full L3 until 16:30", "estimate": shortened})
    return {**plan, "alternatives": alternatives, "downloadStarted": False}


def planner_input_public(payload: dict[str, Any], window: dict[str, Any]) -> dict[str, Any]:
    request_plan = build_dataset_request_plan(payload)
    return {
        "market": str(payload.get("market") or "MES"), "dataset": DATASET, "symbol": DEFAULT_SYMBOL,
        "date": window["date"].isoformat(), "sessionDate": window["date"].isoformat(), "timezone": window["timezone"],
        "replayStartLocal": _format_local(window["replay_start_local"]),
        "replayEndLocal": _format_local(window["replay_end_local"]),
        "replayStartUtc": _format_utc(window["replay_start_utc"]),
        "replayEndUtc": _format_utc(window["replay_end_utc"]),
        "requestStartUtc": request_plan["requestStartUtc"], "requestEndUtc": request_plan["requestEndUtc"],
        "contextMinutes": window["context_minutes"], "days": int(payload.get("days", 1)),
    }


def estimate_public(item: dict[str, Any]) -> dict[str, Any]:
    metadata = item["metadata"]
    spec = MODE_SPECS[item["mode"]]
    return {
        "estimateId": item["id"], "fingerprint": item["request_fingerprint"],
        "mode": item["mode"], "label": spec.label, "dataset": item["dataset"],
        "schemas": item["schemas"], "inputSymbol": item["input_symbol"],
        "rawSymbol": item["raw_symbol"], "instrumentId": item["instrument_id"],
        "requestStartUtc": item["start_utc"], "requestEndUtc": item["end_utc"],
        "replayStartLocal": item["replay_start"], "replayEndLocal": item["replay_end"],
        "timezone": item["timezone"], "estimatedRecords": item["estimated_records"],
        "billableBytes": item["billable_bytes"], "estimatedCostUsd": item["estimated_cost"],
        "rawEstimatedCostUsd": metadata["rawEstimatedCostUsd"],
        "billableMiB": item["billable_bytes"] / (1024 * 1024),
        "billableGiB": item["billable_bytes"] / (1024 * 1024 * 1024),
        "unitPrices": item["unit_price"], "safetyReserveUsd": metadata["safetyReserveUsd"],
        "targetSafetyReserveUsd": metadata.get("targetSafetyReserveUsd", metadata["safetyReserveUsd"]),
        "maximumAuthorizedUsd": metadata["maximumAuthorizedUsd"],
        "authorizationPolicy": metadata.get("authorizationPolicy"),
        "requestLimitUsd": metadata["requestLimitUsd"], "dailyLimitUsd": metadata["dailyLimitUsd"],
        "weeklyLimitUsd": metadata["weeklyLimitUsd"], "monthlyLimitUsd": metadata["monthlyLimitUsd"],
        "dailyRemainingUsd": metadata["dailyRemainingUsd"], "weeklyRemainingUsd": metadata["weeklyRemainingUsd"],
        "monthlyRemainingUsd": metadata["monthlyRemainingUsd"], "allowed": item["allowed"],
        "confidence": item["confidence"], "warnings": item["warnings"], "status": item["status"],
        "localReuse": item["local_reuse"], "reuse": metadata.get("reuse"),
        "availableFeatures": list(metadata["availableFeatures"]), "disabledFeatures": list(metadata["disabledFeatures"]),
        "suitability": list(metadata["suitability"]), "schemaDetails": metadata["schemaDetails"],
        "contract": metadata["contract"], "createdAt": item["created_at"], "expiresAt": item["expires_at"],
        "requestPlan": metadata.get("requestPlan"),
    }


def review_purchase(estimate_id: str) -> dict[str, Any]:
    from .authorization import purchase_review

    return purchase_review(estimate_id)


def submit_purchase(
    estimate_id: str,
    *,
    acknowledged: bool,
    confirmation: str,
    client_factory: Callable[[str], Any] | None = None,
    config: ConnectorConfig | None = None,
) -> dict[str, Any]:
    raise ConnectorError(
        "Legacy synchronous submission is disabled. Use POST /data-planner/estimates/{estimate_id}/authorize."
    )


def refresh_jobs(
    *, client_factory: Callable[[str], Any] | None = None,
    config: ConnectorConfig | None = None,
) -> list[dict[str, Any]]:
    jobs = list_dataset_jobs()
    if not jobs:
        return []
    active_config = config or load_config()
    client = (client_factory or db.Historical)(active_config.api_key)
    refreshed = []
    for job in jobs:
        if not job.get("remote_job_id") or job["status"] in {"IMPORTED", "FAILED"}:
            refreshed.append(job)
            continue
        try:
            details = client.batch.get_job_details(job["remote_job_id"])
            remote_state = str(details.get("state") or details.get("status") or "PROCESSING").upper()
            mapped = {"DONE": "READY", "QUEUED": "QUEUED", "PROCESSING": "PROCESSING", "EXPIRED": "EXPIRED"}.get(remote_state, remote_state)
            actual_cost = next((details.get(key) for key in ("cost_usd", "actual_cost", "cost", "bill_amount") if details.get(key) is not None), job.get("actual_cost"))
            refreshed.append(save_dataset_job({
                **job, "status": mapped, "details": details, "actual_cost": actual_cost,
                "charged_at": utc_now() if actual_cost is not None and not job.get("charged_at") else job.get("charged_at"),
            }))
        except Exception as exc:
            refreshed.append(save_dataset_job({**job, "status": job["status"], "details": {**job["details"], "pollError": safe_error(exc, (active_config.api_key,))}}))
    return refreshed


def _compact_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _summarize_download(path: Path) -> DbnSummary:
    store = open_dbn(path)
    schema = str(getattr(store, "schema", "") or "")
    if schema == "mbo":
        return summarize_dbn(path)
    instrument_ids: set[int] = set()
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    record_count = 0
    for record in store:
        header = getattr(record, "hd", None)
        instrument_id = int(getattr(record, "instrument_id", getattr(header, "instrument_id", 0)))
        ts_event = int(getattr(record, "ts_event", getattr(header, "ts_event", 0)))
        record_count += 1
        instrument_ids.add(instrument_id)
        first_timestamp = first_timestamp or timestamp_iso(ts_event)
        last_timestamp = timestamp_iso(ts_event)
    return DbnSummary(
        file=str(path), dataset=str(getattr(store, "dataset", "") or ""), schema=schema,
        record_count=record_count, first_timestamp=first_timestamp, last_timestamp=last_timestamp,
        instrument_ids=sorted(instrument_ids), raw_symbols=store_symbols(store), action_counts={},
    )


def download_ready_job(
    job_id: str,
    *,
    client_factory: Callable[[str], Any] | None = None,
    config: ConnectorConfig | None = None,
) -> dict[str, Any]:
    """Download an already purchased READY batch job through a validated .part path."""
    job = next(
        (item for item in list_dataset_jobs() if item["id"] == job_id or item.get("remote_job_id") == job_id),
        None,
    )
    if not job:
        raise ConnectorError("Tracked batch job not found.")
    if job["status"] not in {"READY", "DOWNLOADED", "VALIDATED", "IMPORTED"}:
        raise ConnectorError("Batch download is blocked until the tracked job is READY.")
    estimate = get_data_estimate(job["estimate_id"])
    if not estimate:
        raise ConnectorError("Estimate for this batch job was not found.")
    destination = (
        RAW_ROOT / estimate["start_utc"][:10]
        / f"{estimate['input_symbol']}_{job['schema_name']}_{_compact_timestamp(estimate['start_utc'])}_{_compact_timestamp(estimate['end_utc'])}.dbn.zst"
    )
    if not _is_within(destination, RAW_ROOT):
        raise ConnectorError("Unsafe batch output path was blocked.")
    manifest_path = Path(f"{destination}.manifest.json")
    if destination.is_file() and manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("requestFingerprint") == estimate["request_fingerprint"]:
            if job.get("authorization_id"):
                from .authorization import transition_download_job
                transition_download_job(
                    job["id"], "COMPLETED", "IMPORT_VALIDATED", progress=1,
                    download_bytes=destination.stat().st_size,
                )
            else:
                save_dataset_job({**job, "status": "IMPORTED" if job["schema_name"] == "mbo" else "VALIDATED", "details": {**job["details"], "localReuse": True, "file": str(destination)}})
                update_estimate_status(estimate["id"], "LOCAL_REUSE", actual_local_size=destination.stat().st_size)
            return {"reused": True, "file": str(destination), "manifest": str(manifest_path), "downloadStarted": False}
        raise ConnectorError("A different local request already occupies the target path.")

    active_config = config or load_config()
    client = (client_factory or db.Historical)(active_config.api_key)
    staging_root = RAW_ROOT / ".batch-parts"
    staging_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(f"{destination}.part")
    if temporary.exists():
        raise ConnectorError("A retained .part file already exists; inspect it before retrying.")
    if job.get("authorization_id"):
        from .authorization import transition_download_job
        transition_download_job(job["id"], "DOWNLOADING", "DOWNLOAD_STARTED", progress=0)
    else:
        save_dataset_job({**job, "status": "DOWNLOADING", "details": job["details"]})
        update_estimate_status(estimate["id"], "DOWNLOADING")
    lifecycle_stage = "DOWNLOAD"
    try:
        remote_files = client.batch.list_files(job["remote_job_id"])
        paths = client.batch.download(job["remote_job_id"], output_dir=staging_root)
        dbn_paths = [Path(path) for path in paths if str(path).endswith(".dbn.zst")]
        if len(dbn_paths) != 1:
            raise ConnectorError("Expected exactly one DBN.ZST file for the day-split job.")
        source = dbn_paths[0]
        if not source.is_file() or not _is_within(source, staging_root):
            raise ConnectorError("Databento returned an unsafe or missing staged file.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.replace(temporary)
        if temporary.stat().st_size <= 0:
            raise ConnectorError("Databento returned an empty batch file.")
        summary = _summarize_download(temporary)
        if summary.dataset != estimate["dataset"] or summary.schema != job["schema_name"]:
            raise ConnectorError("Downloaded DBN dataset or schema does not match the estimate.")
        if summary.instrument_ids != [int(estimate["instrument_id"])] or summary.record_count <= 0:
            raise ConnectorError("Downloaded DBN instrument or record count is invalid.")
        digest = sha256_file(temporary)
        manifest = {
            "requestFingerprint": estimate["request_fingerprint"], "estimateId": estimate["id"],
            "batchJobId": job["remote_job_id"], "dataset": estimate["dataset"],
            "schema": job["schema_name"], "inputSymbol": estimate["input_symbol"],
            "symbol": estimate["input_symbol"], "rawSymbol": estimate["raw_symbol"],
            "instrumentId": estimate["instrument_id"], "startUtc": estimate["start_utc"],
            "start": estimate["start_utc"], "endUtc": estimate["end_utc"], "end": estimate["end_utc"],
            "visibleReplayStart": estimate["replay_start"], "visibleReplayEnd": estimate["replay_end"],
            "timezone": estimate["timezone"], "estimatedRecords": estimate["estimated_records"],
            "actualRecords": summary.record_count, "recordCount": summary.record_count,
            "estimatedBillableBytes": estimate["billable_bytes"], "estimatedCostUsd": estimate["estimated_cost"],
            "localCompressedBytes": temporary.stat().st_size, "sha256": digest,
            "snapshotExpected": job["schema_name"] == "mbo", "snapshotFound": summary.action_counts.get("R", 0) > 0,
            "bookCompleteness": "pending_import" if job["schema_name"] == "mbo" else "not_applicable",
            "downloadedAt": utc_now(), "validatedAt": utc_now(),
            "databentoSdkVersion": getattr(db, "__version__", "unknown"),
            "remoteFiles": [
                {"filename": Path(str(item.get("filename") or item.get("name") or "unknown")).name, "size": item.get("size")}
                for item in remote_files
            ],
        }
        temporary.replace(destination)
        manifest_tmp = Path(f"{manifest_path}.tmp")
        manifest_tmp.write_text(json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8")
        manifest_tmp.replace(manifest_path)
        downloaded_at = utc_now()
        if job.get("authorization_id"):
            transition_download_job(
                job["id"], "IMPORTING", "DOWNLOAD_COMPLETED", progress=0.75,
                download_bytes=destination.stat().st_size, downloaded_at=downloaded_at,
            )
            transition_download_job(job["id"], "IMPORTING", "IMPORT_STARTED", progress=0.8)
            lifecycle_stage = "IMPORT"
        session = import_file(str(destination)) if job["schema_name"] == "mbo" else None
        if session:
            planned_split = estimate.get("metadata", {}).get("plannedSplit") or {}
            if planned_split.get("splitName"):
                set_session_split(
                    session["id"], str(planned_split["splitName"]),
                    reason=f"Automatic chronological range assignment from {estimate.get('metadata', {}).get('rangePlanId', 'range plan')}",
                    lock=bool(planned_split.get("locked")),
                )
            manifest["plannedSplit"] = planned_split or None
            manifest["bookCompleteness"] = session["completeness"]
            manifest["snapshotFound"] = session["snapshot_status"] == "post_snapshot"
            manifest_tmp.write_text(json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8")
            manifest_tmp.replace(manifest_path)
        status = "IMPORTED" if session else "VALIDATED"
        if job.get("authorization_id"):
            transition_download_job(job["id"], "VALIDATING_IMPORT", "IMPORT_VALIDATED", progress=0.95)
            transition_download_job(job["id"], "COMPLETED", "DOWNLOAD_COMPLETED", progress=1)
        else:
            save_dataset_job({**job, "status": status, "details": {**job["details"], "file": str(destination), "manifest": str(manifest_path)}})
            update_estimate_status(
                estimate["id"], status, actual_local_size=destination.stat().st_size, downloaded_at=downloaded_at
            )
        return {
            "reused": False, "file": str(destination), "manifest": str(manifest_path),
            "records": summary.record_count, "status": status, "session": session,
        }
    except Exception as exc:
        message = safe_error(exc, (active_config.api_key,))
        if job.get("authorization_id"):
            transition_download_job(
                job["id"], "FAILED", "IMPORT_FAILED" if lifecycle_stage == "IMPORT" else "DOWNLOAD_FAILED",
                error_code="IMPORT_FAILED" if lifecycle_stage == "IMPORT" else "NETWORK_ERROR",
                error_message=message,
            )
        else:
            save_dataset_job({**job, "status": "FAILED", "details": {**job["details"], "error": message, "part": str(temporary)}})
            update_estimate_status(estimate["id"], "FAILED")
        raise ConnectorError("Batch download or validation failed: " + message) from exc
