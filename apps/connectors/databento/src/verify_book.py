from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Iterator

import databento as db

from .config import (
    DATASET,
    DATA_ROOT,
    DEFAULT_VERIFICATION_LIMIT,
    REFERENCE_SCHEMA,
    ConnectorConfig,
    ConnectorError,
    HistoricalRequest,
    announce_data_file_selection,
    load_config,
    resolve_data_file,
    safe_error,
    validate_verification_limit,
)
from .dbn_reader import (
    F_LAST,
    F_SNAPSHOT,
    BookSnapshot,
    DbnSummary,
    MboEvent,
    OrderBook,
    SnapshotStatus,
    display_price,
    iter_events,
    open_dbn,
    store_symbols,
    timestamp_iso,
)
from .download import (
    assert_daily_budget,
    download_range,
    record_download_cost,
    require_confirmation,
)
from .estimate import load_receipt
from .manifest import build_manifest, write_manifest
from .verification_context import (
    build_reference_request,
    inspect_mbo_verification_context,
    resolve_contract,
    resolve_verification_window,
)

REFERENCE_ROOT = DATA_ROOT / "reference" / REFERENCE_SCHEMA / "MES"
REPORT_ROOT = DATA_ROOT / "reports" / "book-verification"
METRICS = ("bbo", "spread", "top10Prices", "top10Sizes", "top10OrderCounts")


@dataclass(frozen=True)
class BookLevelState:
    price: int
    size: int
    order_count: int


EMPTY_LEVEL = BookLevelState(int(db.UNDEF_PRICE), 0, 0)


@dataclass(frozen=True)
class Top10State:
    timestamp: str
    ts_event: int
    publisher_id: int
    instrument_id: int
    sequence: int
    bids: tuple[BookLevelState, ...]
    asks: tuple[BookLevelState, ...]
    action: str = ""
    side: str = ""

    @property
    def key(self) -> tuple[int, int, int, int]:
        return (self.publisher_id, self.instrument_id, self.sequence, self.ts_event)

    @property
    def spread(self) -> int | None:
        if self.bids[0].price == db.UNDEF_PRICE or self.asks[0].price == db.UNDEF_PRICE:
            return None
        return self.asks[0].price - self.bids[0].price


@dataclass
class VerificationResult:
    instrument_id: int | None = None
    states_compared: int = 0
    state_mismatches: int = 0
    reference_states_before_ready: int = 0
    reference_states_unmatched: int = 0
    mbo_states_without_reference: int = 0
    post_snapshot_integrity_warnings: int = 0
    metric_matches: dict[str, int] = field(
        default_factory=lambda: {name: 0 for name in METRICS}
    )
    metric_mismatches: dict[str, int] = field(
        default_factory=lambda: {name: 0 for name in METRICS}
    )
    first_mismatch: dict[str, Any] | None = None

    @property
    def passed(self) -> bool:
        return (
            self.states_compared > 0
            and self.state_mismatches == 0
            and self.reference_states_unmatched == 0
            and self.post_snapshot_integrity_warnings == 0
        )


def _pad(levels: Iterable[BookLevelState]) -> tuple[BookLevelState, ...]:
    result = list(levels)[:10]
    result.extend([EMPTY_LEVEL] * (10 - len(result)))
    return tuple(result)


def state_from_mbo(event: MboEvent, snapshot: BookSnapshot) -> Top10State:
    return Top10State(
        timestamp=event.timestamp,
        ts_event=event.ts_event,
        publisher_id=event.publisher_id,
        instrument_id=event.instrument_id,
        sequence=event.sequence,
        bids=_pad(
            BookLevelState(level.price, level.total_size, level.order_count)
            for level in snapshot.bids
        ),
        asks=_pad(
            BookLevelState(level.price, level.total_size, level.order_count)
            for level in snapshot.asks
        ),
        action=event.action,
        side=event.side,
    )


def state_from_mbp_record(record: Any) -> Top10State:
    levels = list(record.levels)
    return Top10State(
        timestamp=timestamp_iso(int(record.ts_event)),
        ts_event=int(record.ts_event),
        publisher_id=int(record.publisher_id),
        instrument_id=int(record.instrument_id),
        sequence=int(record.sequence),
        bids=_pad(
            BookLevelState(int(level.bid_px), int(level.bid_sz), int(level.bid_ct))
            for level in levels
        ),
        asks=_pad(
            BookLevelState(int(level.ask_px), int(level.ask_sz), int(level.ask_ct))
            for level in levels
        ),
        action=str(record.action),
        side=str(record.side),
    )


def compare_states(expected: Top10State, actual: Top10State) -> tuple[str, ...]:
    mismatches: list[str] = []
    if expected.bids[0] != actual.bids[0] or expected.asks[0] != actual.asks[0]:
        mismatches.append("bbo")
    if expected.spread != actual.spread:
        mismatches.append("spread")
    if tuple(level.price for level in expected.bids + expected.asks) != tuple(
        level.price for level in actual.bids + actual.asks
    ):
        mismatches.append("top10Prices")
    if tuple(level.size for level in expected.bids + expected.asks) != tuple(
        level.size for level in actual.bids + actual.asks
    ):
        mismatches.append("top10Sizes")
    if tuple(level.order_count for level in expected.bids + expected.asks) != tuple(
        level.order_count for level in actual.bids + actual.asks
    ):
        mismatches.append("top10OrderCounts")
    return tuple(mismatches)


def _level_payload(level: BookLevelState) -> dict[str, Any]:
    return {
        "priceRaw": level.price,
        "price": None if level.price == db.UNDEF_PRICE else display_price(level.price),
        "size": level.size,
        "orderCount": level.order_count,
    }


def state_payload(state: Top10State) -> dict[str, Any]:
    return {
        "timestamp": state.timestamp,
        "tsEvent": state.ts_event,
        "publisherId": state.publisher_id,
        "instrumentId": state.instrument_id,
        "sequence": state.sequence,
        "action": state.action,
        "side": state.side,
        "spreadRaw": state.spread,
        "bids": [_level_payload(level) for level in state.bids],
        "asks": [_level_payload(level) for level in state.asks],
    }


def _event_payload(event: MboEvent) -> dict[str, Any]:
    return {
        "timestamp": event.timestamp,
        "action": event.action,
        "side": event.side,
        "priceRaw": event.price,
        "size": event.size,
        "orderId": event.order_id,
        "instrumentId": event.instrument_id,
        "publisherId": event.publisher_id,
        "channelId": event.channel_id,
        "sequence": event.sequence,
        "flags": event.flags,
    }


def iter_mbp10_states(path: Path, instrument_id: int | None = None) -> Iterator[Top10State]:
    store = open_dbn(path)
    if str(getattr(store, "dataset", "") or "") != DATASET:
        raise ConnectorError("The MBP-10 reference uses the wrong dataset.")
    if str(getattr(store, "schema", "") or "") != REFERENCE_SCHEMA:
        raise ConnectorError("The reference file is not MBP-10 data.")
    for record in store:
        if instrument_id is not None and int(record.instrument_id) != instrument_id:
            raise ConnectorError("The MBP-10 reference contains a different instrument ID.")
        flags = int(record.flags)
        if flags & F_SNAPSHOT or not flags & F_LAST:
            continue
        yield state_from_mbp_record(record)


def _set_missing_reference_mismatch(
    result: VerificationResult,
    expected: Top10State,
    preceding: Iterable[MboEvent] = (),
) -> None:
    if result.first_mismatch is None:
        result.first_mismatch = {
            "timestamp": expected.timestamp,
            "sequence": expected.sequence,
            "action": expected.action,
            "side": expected.side,
            "mismatchFields": ["missingMboState"],
            "mboEventGroup": [],
            "mbp10ReferenceRecord": state_payload(expected),
            "expectedBbo": {
                "bid": _level_payload(expected.bids[0]),
                "ask": _level_payload(expected.asks[0]),
            },
            "actualBbo": None,
            "expectedTop10": state_payload(expected),
            "actualTop10": None,
            "relevantPrecedingMboRecords": [
                _event_payload(item) for item in list(preceding)[-10:]
            ],
        }


def verify_streams(
    mbo_events: Iterable[MboEvent],
    reference_states: Iterable[Top10State],
    *,
    start_ns: int | None = None,
    end_ns: int | None = None,
) -> VerificationResult:
    result = VerificationResult()
    references = iter(reference_states)
    expected = next(references, None)
    book = OrderBook()
    group: list[MboEvent] = []
    preceding: deque[MboEvent] = deque(maxlen=10)
    preceding_before_group: list[MboEvent] = []
    comparison_started = False

    for event in mbo_events:
        if not group:
            preceding_before_group = list(preceding)
        group.append(event)
        if not book.apply(event):
            continue
        if book.snapshot_status != SnapshotStatus.POST_SNAPSHOT:
            preceding.extend(group)
            group = []
            continue
        if start_ns is not None and event.ts_event < start_ns:
            preceding.extend(group)
            group = []
            continue
        if end_ns is not None and event.ts_event >= end_ns:
            break

        actual_key = (event.publisher_id, event.instrument_id, event.sequence, event.ts_event)
        if not comparison_started:
            while expected is not None and expected.key < actual_key:
                result.reference_states_before_ready += 1
                expected = next(references, None)
            comparison_started = True
        else:
            while expected is not None and expected.key < actual_key:
                result.reference_states_unmatched += 1
                _set_missing_reference_mismatch(result, expected, preceding)
                expected = next(references, None)

        if expected is None or actual_key < expected.key:
            result.mbo_states_without_reference += 1
            preceding.extend(group)
            group = []
            continue
        if actual_key > expected.key:
            preceding.extend(group)
            group = []
            continue

        actual = state_from_mbo(event, book.snapshot(10))
        mismatches = compare_states(expected, actual)
        result.states_compared += 1
        for metric in METRICS:
            target = result.metric_mismatches if metric in mismatches else result.metric_matches
            target[metric] += 1
        if mismatches:
            result.state_mismatches += 1
            if result.first_mismatch is None:
                result.first_mismatch = {
                    "timestamp": event.timestamp,
                    "sequence": event.sequence,
                    "action": expected.action,
                    "side": expected.side,
                    "mismatchFields": list(mismatches),
                    "mboEventGroup": [_event_payload(item) for item in group],
                    "mbp10ReferenceRecord": state_payload(expected),
                    "expectedBbo": {
                        "bid": _level_payload(expected.bids[0]),
                        "ask": _level_payload(expected.asks[0]),
                    },
                    "actualBbo": {
                        "bid": _level_payload(actual.bids[0]),
                        "ask": _level_payload(actual.asks[0]),
                    },
                    "expectedTop10": state_payload(expected),
                    "actualTop10": state_payload(actual),
                    "relevantPrecedingMboRecords": [
                        _event_payload(item) for item in preceding_before_group[-10:]
                    ],
                }
        expected = next(references, None)
        preceding.extend(group)
        group = []

    if comparison_started:
        while expected is not None:
            result.reference_states_unmatched += 1
            _set_missing_reference_mismatch(result, expected, preceding)
            expected = next(references, None)
    result.post_snapshot_integrity_warnings = book.post_snapshot_integrity_warnings
    result.instrument_id = book.primary_instrument_id
    return result


def reference_output_path(request: HistoricalRequest, root: Path = REFERENCE_ROOT) -> Path:
    folder = root / request.start.strftime("%Y-%m-%d")
    start_stamp = request.start_iso.replace("-", "").replace(":", "").replace(".", "")
    end_stamp = request.end_iso.replace("-", "").replace(":", "").replace(".", "")
    filename = (
        f"{request.symbol}_{request.schema}_{start_stamp}_{end_stamp}_"
        f"limit{request.limit}.dbn.zst"
    )
    return folder / filename


def _metadata_nanoseconds(value: Any) -> int:
    raw_value = getattr(value, "value", None)
    if raw_value is not None:
        return int(raw_value)
    if hasattr(value, "timestamp"):
        return int(value.timestamp() * 1_000_000_000)
    raise ConnectorError("DBN metadata contains an invalid time range.")


def validate_reference_metadata(path: Path, request: HistoricalRequest) -> None:
    store = open_dbn(path)
    symbols = {str(value) for value in (getattr(store, "symbols", None) or [])}
    checks = {
        "dataset": str(getattr(store, "dataset", "") or "") == request.dataset,
        "schema": str(getattr(store, "schema", "") or "") == REFERENCE_SCHEMA,
        "symbol": request.symbol in symbols,
        "stype_in": str(getattr(store, "stype_in", "") or "") == request.stype_in,
        "start": _metadata_nanoseconds(store.start) == request.start_nanoseconds,
        "end": _metadata_nanoseconds(store.end) == request.end_nanoseconds,
    }
    failed = [name for name, valid in checks.items() if not valid]
    if failed:
        raise ConnectorError("DBN metadata does not match the request: " + ", ".join(failed))


def summarize_reference(path: Path, request: HistoricalRequest) -> DbnSummary:
    store = open_dbn(path)
    symbols = store_symbols(store)
    instrument_ids: set[int] = set()
    action_counts: dict[str, int] = {}
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    count = 0
    for record in store:
        count += 1
        instrument_ids.add(int(record.instrument_id))
        action = str(record.action)
        action_counts[action] = action_counts.get(action, 0) + 1
        timestamp = timestamp_iso(int(record.ts_event))
        first_timestamp = first_timestamp or timestamp
        last_timestamp = timestamp
    if count == 0:
        raise ConnectorError("The MBP-10 reference contains no records.")
    if request.limit is None or count > request.limit:
        raise ConnectorError("The MBP-10 reference exceeds the requested record limit.")
    if instrument_ids != {request.instrument_id}:
        raise ConnectorError("The MBP-10 reference instrument does not match the MBO file.")
    return DbnSummary(
        file=str(path),
        dataset=str(store.dataset),
        schema=str(store.schema),
        record_count=count,
        first_timestamp=first_timestamp,
        last_timestamp=last_timestamp,
        instrument_ids=sorted(instrument_ids),
        raw_symbols=symbols,
        action_counts=action_counts,
    )


def prepare_reference(
    client: Any,
    config: ConnectorConfig,
    request: HistoricalRequest,
    *,
    confirmed: bool,
) -> Path:
    require_confirmation(confirmed)
    destination = reference_output_path(request)
    if destination.is_file():
        return destination

    receipt = load_receipt(request, config)
    estimated_cost = Decimal(str(receipt["cost"]))
    assert_daily_budget(estimated_cost, config)
    download_range(client, request, destination)
    try:
        validate_reference_metadata(destination, request)
        summary = summarize_reference(destination, request)
        manifest = build_manifest(destination, request, estimated_cost, summary)
        write_manifest(destination, manifest)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    record_download_cost(estimated_cost)
    return destination


def report_payload(
    result: VerificationResult,
    mbo_path: Path,
    mbp_path: Path,
    request: HistoricalRequest,
) -> dict[str, Any]:
    payload = asdict(result)
    payload["passed"] = result.passed
    payload["request"] = {
        "dataset": request.dataset,
        "schema": request.schema,
        "symbol": request.symbol,
        "stypeIn": request.stype_in,
        "start": request.start_iso,
        "end": request.end_iso,
        "limit": request.limit,
        "instrumentId": request.instrument_id,
        "resolvedRawSymbol": request.raw_symbol,
    }
    payload["mboFile"] = str(mbo_path)
    payload["mbp10File"] = str(mbp_path)
    payload["comparison"] = "exact fixed-point integer equality; no tolerance"
    return payload


def write_reports(
    result: VerificationResult,
    mbo_path: Path,
    mbp_path: Path,
    request: HistoricalRequest,
    *,
    root: Path = REPORT_ROOT,
    now: datetime | None = None,
) -> tuple[Path, Path]:
    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / f"MES_book_verification_{timestamp}.json"
    text_path = root / f"MES_book_verification_{timestamp}.txt"
    payload = report_payload(result, mbo_path, mbp_path, request)
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    lines = [
        "DATABENTO MBO VS MBP-10 VERIFICATION",
        "",
        f"MBO file: {mbo_path}",
        f"MBP-10 file: {mbp_path}",
        f"Instrument ID: {result.instrument_id or '-'}",
        f"Resolved raw symbol: {request.raw_symbol or '-'}",
        f"Range: {request.start_iso} to {request.end_iso}",
        f"Record limit: {request.limit}",
        "Comparison: exact fixed-point integer equality; no tolerance",
        f"Compared event states: {result.states_compared}",
        f"Exact BBO matches: {result.metric_matches['bbo']}",
        f"Exact top-10 price matches: {result.metric_matches['top10Prices']}",
        f"Exact top-10 size matches: {result.metric_matches['top10Sizes']}",
        f"Exact top-10 count matches: {result.metric_matches['top10OrderCounts']}",
        f"Price mismatches: {result.metric_mismatches['top10Prices']}",
        f"Size mismatches: {result.metric_mismatches['top10Sizes']}",
        f"Count mismatches: {result.metric_mismatches['top10OrderCounts']}",
        f"State mismatches: {result.state_mismatches}",
        f"Unmatched MBP-10 states: {result.reference_states_unmatched}",
        f"MBO states without MBP-10 update: {result.mbo_states_without_reference}",
        f"Post-snapshot integrity warnings: {result.post_snapshot_integrity_warnings}",
        "First mismatch timestamp: "
        + (str(result.first_mismatch["timestamp"]) if result.first_mismatch else "-"),
    ]
    for metric in METRICS:
        lines.append(
            f"{metric}: {result.metric_matches[metric]} exact, "
            f"{result.metric_mismatches[metric]} mismatch"
        )
    lines.append(f"Verification: {'PASSED' if result.passed else 'FAILED'}")
    if result.first_mismatch:
        lines.extend(
            [
                "",
                "FIRST MISMATCH",
                json.dumps(result.first_mismatch, indent=2),
            ]
        )
    text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return text_path, json_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify reconstructed MBO top 10 against MBP-10.")
    mbo = parser.add_mutually_exclusive_group()
    mbo.add_argument("--mbo-file")
    mbo.add_argument("--latest", action="store_true")
    parser.add_argument("--mbp-file")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--limit", type=int, default=DEFAULT_VERIFICATION_LIMIT)
    parser.add_argument("--confirm", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    config: ConnectorConfig | None = None
    try:
        args = parse_args(argv)
        limit = validate_verification_limit(args.limit)
        mbo_path = resolve_data_file(args.mbo_file, latest=args.latest)
        announce_data_file_selection(mbo_path, file_arg=args.mbo_file, latest=args.latest)
        if not args.mbp_file:
            require_confirmation(args.confirm)
        context = inspect_mbo_verification_context(mbo_path)
        start_ns, end_ns = resolve_verification_window(context, args.start, args.end)
        config = load_config()
        client = db.Historical(config.api_key)
        contract = resolve_contract(client, context, start_ns, end_ns)
        request = build_reference_request(contract, start_ns, end_ns, limit)

        if args.mbp_file:
            mbp_path = Path(args.mbp_file).expanduser().resolve()
            if not mbp_path.is_file():
                raise ConnectorError(f"MBP-10 reference does not exist: {mbp_path}")
        else:
            mbp_path = prepare_reference(client, config, request, confirmed=args.confirm)
        validate_reference_metadata(mbp_path, request)

        result = verify_streams(
            iter_events(mbo_path),
            iter_mbp10_states(mbp_path, request.instrument_id),
            start_ns=start_ns,
            end_ns=end_ns,
        )
        text_path, json_path = write_reports(result, mbo_path, mbp_path, request)
        print(text_path.read_text(encoding="utf-8"), end="")
        print(f"Text report: {text_path}")
        print(f"JSON report: {json_path}")
        return 0 if result.passed else 1
    except Exception as exc:
        secrets = (config.api_key,) if config else ()
        print(f"ERROR: {safe_error(exc, secrets)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
