from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import databento as db

from .config import (
    DATA_ROOT,
    RAW_ROOT,
    ConnectorConfig,
    ConnectorError,
    HistoricalRequest,
    build_request,
    load_config,
    safe_error,
)
from .estimate import load_receipt
from .manifest import build_manifest, write_manifest
from .validate import validate_file

LEDGER_PATH = DATA_ROOT / "cost-ledger.json"


def require_confirmation(confirmed: bool) -> None:
    if not confirmed:
        raise ConnectorError("Download blocked: pass --confirm after reviewing the cost estimate.")


def _today(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).date().isoformat()


def read_daily_spend(path: Path = LEDGER_PATH, *, now: datetime | None = None) -> Decimal:
    if not path.is_file():
        return Decimal("0")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConnectorError("The local Databento cost ledger is unreadable.") from exc
    if payload.get("date") != _today(now):
        return Decimal("0")
    return Decimal(str(payload.get("estimatedDownloadedCostUsd", 0)))


def assert_daily_budget(
    estimated_cost: Decimal,
    config: ConnectorConfig,
    *,
    path: Path = LEDGER_PATH,
    now: datetime | None = None,
) -> None:
    if read_daily_spend(path, now=now) + estimated_cost > config.max_daily_cost_usd:
        raise ConnectorError("Download blocked: configured daily cost limit would be exceeded.")


def record_download_cost(
    estimated_cost: Decimal,
    *,
    path: Path = LEDGER_PATH,
    now: datetime | None = None,
) -> None:
    timestamp = now or datetime.now(UTC)
    total = read_daily_spend(path, now=timestamp) + estimated_cost
    payload = {
        "date": _today(timestamp),
        "estimatedDownloadedCostUsd": float(total),
        "updatedAt": timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def output_path(request: HistoricalRequest, root: Path = RAW_ROOT) -> Path:
    folder = root / request.start.strftime("%Y-%m-%d")
    filename = (
        f"{request.symbol}_{request.schema}_"
        f"{request.start.strftime('%Y%m%dT%H%M%SZ')}_"
        f"{request.end.strftime('%Y%m%dT%H%M%SZ')}.dbn.zst"
    )
    return folder / filename


def download_range(client: Any, request: HistoricalRequest, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(f"{destination}.part")
    if destination.exists():
        raise ConnectorError(f"Download target already exists: {destination}")
    try:
        parameters: dict[str, Any] = {
            "dataset": request.dataset,
            "schema": request.schema,
            "symbols": request.symbol,
            "stype_in": request.stype_in,
            "start": request.start_iso,
            "end": request.end_iso,
            "path": temporary,
        }
        if request.limit is not None:
            parameters["limit"] = request.limit
        client.timeseries.get_range(**parameters)
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise ConnectorError("Databento returned an empty download file.")
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download one confirmed MES MBO historical request.")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--symbol", default="MES.v.0")
    parser.add_argument("--confirm", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    config: ConnectorConfig | None = None
    try:
        args = parse_args(argv)
        request = build_request(args.start, args.end, args.symbol)
        require_confirmation(args.confirm)
        config = load_config()
        receipt = load_receipt(request, config)
        estimated_cost = Decimal(str(receipt["cost"]))
        assert_daily_budget(estimated_cost, config)

        destination = output_path(request)
        client = db.Historical(config.api_key)
        download_range(client, request, destination)
        summary, errors = validate_file(destination)
        if errors:
            destination.unlink(missing_ok=True)
            raise ConnectorError("Downloaded DBN file failed validation: " + "; ".join(errors))

        manifest = build_manifest(destination, request, estimated_cost, summary)
        manifest_path = write_manifest(destination, manifest)
        record_download_cost(estimated_cost)
        print("DATABENTO TEST DOWNLOAD")
        print()
        print(f"File: {destination}")
        print(f"Manifest: {manifest_path}")
        print(f"Records: {summary.record_count}")
        print(f"Estimated cost USD: {estimated_cost:.6f}")
        return 0
    except Exception as exc:
        secrets = (config.api_key,) if config else ()
        print(f"ERROR: {safe_error(exc, secrets)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
