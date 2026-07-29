from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import databento as db

from .config import (
    ESTIMATE_MAX_AGE,
    ESTIMATE_ROOT,
    ConnectorConfig,
    ConnectorError,
    HistoricalRequest,
    build_request,
    format_utc,
    load_config,
    safe_error,
)


def request_fingerprint(request: HistoricalRequest) -> str:
    payload = {
        "dataset": request.dataset,
        "schema": request.schema,
        "symbol": request.symbol,
        "stypeIn": request.stype_in,
        "start": request.start_iso,
        "end": request.end_iso,
        "limit": request.limit,
        "instrumentId": request.instrument_id,
        "rawSymbol": request.raw_symbol,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def receipt_path(request: HistoricalRequest, root: Path = ESTIMATE_ROOT) -> Path:
    return root / f"{request_fingerprint(request)}.json"


def estimate_cost(client: Any, request: HistoricalRequest) -> Decimal:
    parameters = {
        "dataset": request.dataset,
        "schema": request.schema,
        "symbols": request.symbol,
        "stype_in": request.stype_in,
        "start": request.start_iso,
        "end": request.end_iso,
    }
    if request.limit is not None:
        parameters["limit"] = request.limit
    value = client.metadata.get_cost(**parameters)
    return Decimal(str(value))


def estimate_billable_size(client: Any, request: HistoricalRequest) -> int:
    parameters = {
        "dataset": request.dataset,
        "schema": request.schema,
        "symbols": request.symbol,
        "stype_in": request.stype_in,
        "start": request.start_iso,
        "end": request.end_iso,
    }
    if request.limit is not None:
        parameters["limit"] = request.limit
    return int(client.metadata.get_billable_size(**parameters))


def relevant_unit_price(client: Any, request: HistoricalRequest) -> Decimal:
    prices = client.metadata.list_unit_prices(dataset=request.dataset)
    for entry in prices:
        if str(entry.get("mode")) != "historical":
            continue
        unit_prices = entry.get("unit_prices") or {}
        if request.schema in unit_prices:
            return Decimal(str(unit_prices[request.schema]))
    raise ConnectorError(
        f"No historical unit price was returned for schema {request.schema}."
    )


def is_request_allowed(cost: Decimal, config: ConnectorConfig) -> bool:
    return cost >= 0 and cost <= config.max_request_cost_usd


def save_receipt(
    request: HistoricalRequest,
    cost: Decimal,
    config: ConnectorConfig,
    *,
    root: Path = ESTIMATE_ROOT,
    now: datetime | None = None,
    billable_bytes: int | None = None,
    unit_price_usd_per_gb: Decimal | None = None,
) -> Path:
    estimated_at = now or datetime.now(UTC)
    payload = {
        "fingerprint": request_fingerprint(request),
        "dataset": request.dataset,
        "schema": request.schema,
        "symbol": request.symbol,
        "stypeIn": request.stype_in,
        "start": request.start_iso,
        "end": request.end_iso,
        "limit": request.limit,
        "instrumentId": request.instrument_id,
        "rawSymbol": request.raw_symbol,
        "estimatedCostUsd": float(cost),
        "configuredMaxRequestCostUsd": float(config.max_request_cost_usd),
        "allowed": is_request_allowed(cost, config),
        "estimatedAt": format_utc(estimated_at),
    }
    if billable_bytes is not None:
        payload["estimatedBillableBytes"] = billable_bytes
    if unit_price_usd_per_gb is not None:
        payload["unitPriceUsdPerGb"] = float(unit_price_usd_per_gb)
    path = receipt_path(request, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def load_receipt(
    request: HistoricalRequest,
    config: ConnectorConfig,
    *,
    root: Path = ESTIMATE_ROOT,
    now: datetime | None = None,
) -> dict[str, Any]:
    path = receipt_path(request, root)
    if not path.is_file():
        raise ConnectorError("A successful cost estimate for this exact request is required.")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConnectorError("The cost estimate receipt is unreadable.") from exc

    expected = request_fingerprint(request)
    if receipt.get("fingerprint") != expected:
        raise ConnectorError("The cost estimate does not match this request.")
    estimated_at = datetime.fromisoformat(str(receipt.get("estimatedAt", "")).replace("Z", "+00:00"))
    current_time = now or datetime.now(UTC)
    if current_time - estimated_at > ESTIMATE_MAX_AGE:
        raise ConnectorError("The cost estimate is older than 30 minutes. Estimate again.")

    cost = Decimal(str(receipt.get("estimatedCostUsd")))
    if not receipt.get("allowed") or not is_request_allowed(cost, config):
        raise ConnectorError("The estimated request cost exceeds the configured request limit.")
    receipt["cost"] = cost
    return receipt


def print_estimate(request: HistoricalRequest, cost: Decimal, config: ConnectorConfig) -> None:
    print("DATABENTO COST ESTIMATE")
    print()
    print(f"Dataset: {request.dataset}")
    print(f"Schema: {request.schema}")
    print(f"Symbol: {request.symbol}")
    print(f"Input symbology: {request.stype_in}")
    print(f"Start: {request.start_iso}")
    print(f"End: {request.end_iso}")
    print(f"Duration: {int(request.duration.total_seconds())} seconds")
    if request.limit is not None:
        print(f"Record limit: {request.limit}")
    print(f"Estimated cost USD: {cost:.6f}")
    print(f"Configured max request cost USD: {config.max_request_cost_usd:.2f}")
    print(f"Allowed: {'YES' if is_request_allowed(cost, config) else 'NO'}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Estimate a safe MES MBO historical request.")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--symbol", default="MES.v.0")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    config: ConnectorConfig | None = None
    try:
        args = parse_args(argv)
        request = build_request(args.start, args.end, args.symbol)
        config = load_config()
        client = db.Historical(config.api_key)
        cost = estimate_cost(client, request)
        save_receipt(request, cost, config)
        print_estimate(request, cost, config)
        return 0
    except Exception as exc:
        secrets = (config.api_key,) if config else ()
        print(f"ERROR: {safe_error(exc, secrets)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
