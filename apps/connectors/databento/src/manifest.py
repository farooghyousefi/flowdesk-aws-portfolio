from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .config import HistoricalRequest, REPO_ROOT, format_utc
from .dbn_reader import DbnSummary


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(
    file_path: Path,
    request: HistoricalRequest,
    estimated_cost_usd: Decimal,
    summary: DbnSummary,
    *,
    downloaded_at: datetime | None = None,
) -> dict[str, Any]:
    try:
        file_value = str(file_path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        file_value = str(file_path.resolve())
    payload = {
        "dataset": request.dataset,
        "schema": request.schema,
        "symbol": request.symbol,
        "stypeIn": request.stype_in,
        "start": request.start_iso,
        "end": request.end_iso,
        "estimatedCostUsd": float(estimated_cost_usd),
        "downloadedAt": format_utc(downloaded_at or datetime.now(UTC)),
        "file": file_value,
        "sha256": sha256_file(file_path),
        "recordCount": summary.record_count,
        "instrumentIds": summary.instrument_ids,
        "rawSymbols": summary.raw_symbols,
        "dataQuality": "historical-exchange-feed",
    }
    if request.limit is not None:
        payload["recordLimit"] = request.limit
    if request.instrument_id is not None:
        payload["instrumentId"] = request.instrument_id
    if request.raw_symbol is not None:
        payload["resolvedRawSymbol"] = request.raw_symbol
    return payload


def write_manifest(file_path: Path, payload: dict[str, Any]) -> Path:
    manifest_path = Path(f"{file_path}.manifest.json")
    temporary = manifest_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(manifest_path)
    return manifest_path
