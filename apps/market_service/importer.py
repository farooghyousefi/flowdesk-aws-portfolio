from __future__ import annotations

import hashlib
import json
import resource
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from apps.connectors.databento.src.config import ConnectorError, REPO_ROOT, resolve_data_file
from apps.connectors.databento.src.dbn_reader import (
    F_LAST,
    DbnSummary,
    OrderBook,
    SnapshotStatus,
    iter_events,
    open_dbn,
    store_symbols,
)
from apps.connectors.databento.src.validate import validate_summary
from .contracts import display_price
from .features import OrderflowFeatures
from .storage import DERIVED_ROOT, DUCKDB_PATH, ensure_directories, upsert_session, utc_now

BOOK_VERIFICATION_ROOT = REPO_ROOT / "data" / "databento" / "reports" / "book-verification"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest(path: Path) -> dict[str, Any]:
    manifest_path = Path(f"{path}.manifest.json")
    if not manifest_path.is_file():
        raise ConnectorError(f"Manifest not found for {path.name}.")
    try:
        return json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise ConnectorError(f"Manifest cannot be read for {path.name}.") from exc


def _contract_symbol(instrument_id: int) -> str:
    estimate_root = REPO_ROOT / "data" / "databento" / "estimates"
    for candidate in sorted(estimate_root.glob("*.json"), reverse=True):
        try:
            payload = json.loads(candidate.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("instrumentId") == instrument_id and payload.get("rawSymbol"):
            return str(payload["rawSymbol"])
    return f"MES · {instrument_id}"



def _session_time_range(
    manifest: dict[str, Any],
    *,
    first_event_timestamp: str | None,
    last_event_timestamp: str | None,
) -> tuple[str | None, str | None]:
    """Return the requested receive-time window for a DBN session.

    MBO snapshot rows can carry original matching-engine event timestamps from
    before the requested historical window. Databento MBO requests are indexed
    by ``ts_recv``, and the persisted manifest records that exact request
    window. Use it for session chronology instead of snapshot ``ts_event``.
    """
    requested_start = str(manifest.get("startUtc") or manifest.get("start") or "").strip()
    requested_end = str(manifest.get("endUtc") or manifest.get("end") or "").strip()
    return (requested_start or first_event_timestamp, requested_end or last_event_timestamp)

def external_verification_status(
    path: Path,
    instrument_id: int,
    *,
    report_root: Path = BOOK_VERIFICATION_ROOT,
) -> str:
    for candidate in sorted(report_root.glob("*.json"), reverse=True):
        try:
            report = json.loads(candidate.read_text())
            request = report.get("request", {})
            report_path = Path(str(report.get("mboFile", ""))).resolve()
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if (
            report.get("passed") is True
            and request.get("instrumentId") == instrument_id
            and report_path == path.resolve()
        ):
            return "externally_verified"
    return "external_verification_pending"


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> str | None:
    if not rows:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path, compression="zstd")
    return str(path)


def _refresh_duckdb() -> None:
    try:
        import duckdb
    except ImportError as exc:
        raise ConnectorError("DuckDB is not installed. Run npm run local:setup.") from exc
    connection = duckdb.connect(str(DUCKDB_PATH))
    try:
        for name in ("bars", "trades", "footprint", "orderbook", "features"):
            files = list((DERIVED_ROOT / name).glob("*.parquet"))
            if files:
                pattern = str(DERIVED_ROOT / name / "*.parquet").replace("'", "''")
                connection.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet('{pattern}', union_by_name=true)")
    finally:
        connection.close()


def import_file(file_arg: str) -> dict[str, Any]:
    ensure_directories()
    path = resolve_data_file(file_arg)
    manifest = _manifest(path)
    digest = sha256_file(path)
    if digest != manifest.get("sha256"):
        raise ConnectorError(f"SHA-256 mismatch for {path.name}.")

    store = open_dbn(path)
    dataset = str(getattr(store, "dataset", "") or "")
    schema = str(getattr(store, "schema", "") or "")
    symbols = store_symbols(store)
    action_counts: Counter[str] = Counter()
    instrument_ids: set[int] = set()
    book = OrderBook()
    features = OrderflowFeatures()
    trades: list[dict[str, Any]] = []
    heatmap: list[dict[str, Any]] = []
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    last_heatmap_ns = 0
    record_count = 0
    started = time.perf_counter()

    for event in iter_events(path):
        record_count += 1
        first_timestamp = first_timestamp or event.timestamp
        last_timestamp = event.timestamp
        instrument_ids.add(event.instrument_id)
        action_counts[event.action] += 1
        before_order = book.orders.get(event.order_id)
        features.observe(event, before_order=before_order)
        complete_group = book.apply(event)
        if event.action == "T":
            trades.append({
                "session_id": "pending", "ts_event_ns": event.ts_event, "timestamp": event.timestamp,
                "price_fixed": event.price, "price": display_price(event.price), "size": event.size,
                "aggressor_side": "buy" if event.side == "B" else "sell", "sequence": event.sequence,
            })
        if complete_group and event.ts_event - last_heatmap_ns >= 1_000_000_000:
            snapshot = book.snapshot(10)
            for side, levels in (("bid", snapshot.bids), ("ask", snapshot.asks)):
                for level in levels:
                    heatmap.append({
                        "session_id": "pending", "ts_event_ns": event.ts_event, "side": side,
                        "price_fixed": level.price, "price": display_price(level.price),
                        "size": level.total_size, "order_count": level.order_count,
                    })
            last_heatmap_ns = event.ts_event

    elapsed = max(time.perf_counter() - started, 0.000001)
    ids = sorted(instrument_ids)
    summary = DbnSummary(
        file=str(path), dataset=dataset, schema=schema, record_count=record_count,
        first_timestamp=first_timestamp, last_timestamp=last_timestamp,
        instrument_ids=ids, raw_symbols=symbols, action_counts=dict(action_counts),
    )
    expected_instrument_id: int | None = None
    if manifest.get("instrumentId") not in {None, ""}:
        try:
            expected_instrument_id = int(manifest["instrumentId"])
        except (TypeError, ValueError) as exc:
            raise ConnectorError("Manifest instrumentId must be a positive integer.") from exc
        if expected_instrument_id <= 0:
            raise ConnectorError("Manifest instrumentId must be a positive integer.")

    expected_symbols = {
        str(value).strip()
        for value in (
            manifest.get("inputSymbol"),
            manifest.get("symbol"),
            manifest.get("rawSymbol"),
        )
        if value not in {None, ""} and str(value).strip()
    }
    errors = validate_summary(
        summary,
        expected_symbols=expected_symbols or None,
        expected_instrument_id=expected_instrument_id,
    )
    if errors:
        raise ConnectorError("DBN validation failed: " + "; ".join(errors))
    if record_count != int(manifest.get("recordCount", -1)):
        raise ConnectorError(f"Record count differs from manifest for {path.name}.")
    if len(ids) != 1:
        raise ConnectorError("Import requires exactly one instrument ID.")

    complete = book.is_snapshot_ready and book.saw_snapshot
    session_start, session_end = _session_time_range(
        manifest,
        first_event_timestamp=first_timestamp,
        last_event_timestamp=last_timestamp,
    )
    session_id = hashlib.sha256(f"{path}:{digest}".encode()).hexdigest()[:16]
    for row in trades:
        row["session_id"] = session_id
    for row in heatmap:
        row["session_id"] = session_id

    bars = []
    for row in features.bars_contract():
        bars.append({"session_id": session_id, **row})
    footprint = []
    for (bucket, price), (bid_volume, ask_volume) in features.footprint.items():
        ratio = ask_volume / max(bid_volume, 1)
        inverse = bid_volume / max(ask_volume, 1)
        footprint.append({
            "session_id": session_id, "bar_start_ns": bucket * 60_000_000_000,
            "price_fixed": price, "price": display_price(price), "bid_volume": bid_volume,
            "ask_volume": ask_volume, "delta": ask_volume - bid_volume,
            "imbalance": "buy" if ratio >= features.imbalance_ratio else "sell" if inverse >= features.imbalance_ratio else "none",
        })
    feature_contract = features.contract()
    feature_rows = [{
        "session_id": session_id, "ts_event_ns": features.last_ts,
        "buy_volume": features.buy_volume, "sell_volume": features.sell_volume,
        "delta": features.cumulative_delta, "trade_count": features.trade_count,
        "payload_json": json.dumps(feature_contract, separators=(",", ":")),
    }]
    derived_manifest = {
        "bars": _write_rows(DERIVED_ROOT / "bars" / f"{session_id}.parquet", bars),
        "trades": _write_rows(DERIVED_ROOT / "trades" / f"{session_id}.parquet", trades),
        "footprint": _write_rows(DERIVED_ROOT / "footprint" / f"{session_id}.parquet", footprint),
        "orderbook": _write_rows(DERIVED_ROOT / "orderbook" / f"{session_id}.parquet", heatmap),
        "features": _write_rows(DERIVED_ROOT / "features" / f"{session_id}.parquet", feature_rows),
    }
    _refresh_duckdb()

    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_rss_mb = rss / (1024 * 1024) if rss > 10_000_000 else rss / 1024
    integrity = "passed" if book.post_snapshot_integrity_warnings == 0 and book.negative_level_sizes == 0 else "warning"
    payload = {
        "id": session_id,
        "instrument": "MES",
        "symbol": symbols[0] if symbols else str(manifest.get("symbol", "MES.v.0")),
        "contract_symbol": _contract_symbol(ids[0]),
        "instrument_id": ids[0],
        "start_at": session_start,
        "end_at": session_end,
        "record_count": record_count,
        "snapshot_status": book.snapshot_status.value,
        "completeness": "complete" if complete else "partial",
        "file_path": str(path),
        "sha256": digest,
        "imported_at": utc_now(),
        "integrity_status": integrity,
        "unknown_pre": book.unknown_order_references_pre_snapshot,
        "unknown_during": book.unknown_order_references_during_snapshot,
        "unknown_post": book.unknown_order_references_after_snapshot,
        "sequence_regressions": book.natural_sequence_regressions,
        "sequence_gaps": book.natural_sequence_gaps,
        "out_of_order_events": book.out_of_order_sequences,
        "duplicate_events": book.duplicate_events,
        "processing_rate": round(record_count / elapsed, 2),
        "peak_rss_mb": round(peak_rss_mb, 2),
        "derived_manifest": derived_manifest,
        "external_verification": external_verification_status(path, ids[0]),
        "dataset_name": dataset or "GLBX.MDP3",
        "schema_name": schema or "mbo",
        "contract_mapping_status": "resolved" if symbols and ids else "missing",
    }
    upsert_session(payload)
    return payload


def import_discovered() -> list[dict[str, Any]]:
    root = REPO_ROOT / "data" / "databento" / "raw" / "MES"
    return [import_file(str(path)) for path in sorted(root.glob("*/*.dbn.zst"))]
