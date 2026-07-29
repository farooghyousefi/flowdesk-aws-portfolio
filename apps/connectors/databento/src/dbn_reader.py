from __future__ import annotations

import argparse
import json
import resource
import sys
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import databento as db
from sortedcontainers import SortedDict

from .config import (
    ConnectorError,
    announce_data_file_selection,
    resolve_data_file,
    safe_error,
)

FIXED_PRICE_SCALE = Decimal("1000000000")
F_LAST = 128
F_TOB = 64
F_SNAPSHOT = 32


def _field(record: Any, name: str) -> Any:
    if isinstance(record, dict):
        if name not in record:
            raise ConnectorError(f"DBN record is missing field: {name}")
        return record[name]
    if hasattr(record, name):
        return getattr(record, name)
    try:
        return record[name]
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise ConnectorError(f"DBN record is missing field: {name}") from exc


def _character(value: Any) -> str:
    if hasattr(value, "value"):
        value = value.value
    if isinstance(value, bytes):
        return value.decode("ascii")
    return str(value)


def timestamp_iso(nanoseconds: int) -> str:
    seconds, remainder = divmod(int(nanoseconds), 1_000_000_000)
    base = datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{remainder:09d}Z"


def display_price(raw_price: int) -> str:
    value = Decimal(int(raw_price)) / FIXED_PRICE_SCALE
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


@dataclass(frozen=True)
class MboEvent:
    timestamp: str
    ts_event: int
    action: str
    side: str
    price: int
    size: int
    order_id: int
    instrument_id: int
    sequence: int
    flags: int
    ts_recv: int = 0
    publisher_id: int = 0
    channel_id: int = 0


def normalize_record(record: Any) -> MboEvent:
    ts_event = int(_field(record, "ts_event"))
    return MboEvent(
        timestamp=timestamp_iso(ts_event),
        ts_event=ts_event,
        action=_character(_field(record, "action")),
        side=_character(_field(record, "side")),
        price=int(_field(record, "price")),
        size=int(_field(record, "size")),
        order_id=int(_field(record, "order_id")),
        instrument_id=int(_field(record, "instrument_id")),
        sequence=int(_field(record, "sequence")),
        flags=int(_field(record, "flags")),
        ts_recv=int(_field(record, "ts_recv")),
        publisher_id=int(_field(record, "publisher_id")),
        channel_id=int(_field(record, "channel_id")),
    )


def open_dbn(path: Path) -> Any:
    try:
        return db.DBNStore.from_file(path)
    except Exception as exc:
        raise ConnectorError(f"File cannot be read as DBN: {path.name}") from exc


def iter_events(path: Path, limit: int | None = None) -> Iterator[MboEvent]:
    store = open_dbn(path)
    for index, record in enumerate(store):
        if limit is not None and index >= limit:
            break
        yield normalize_record(record)


def store_symbols(store: Any) -> list[str]:
    symbols = [str(value) for value in (getattr(store, "symbols", None) or [])]
    if symbols:
        return sorted(set(symbols))
    mappings = getattr(store, "mappings", None) or {}
    return sorted(str(value) for value in mappings.keys())


@dataclass(frozen=True)
class DbnSummary:
    file: str
    dataset: str
    schema: str
    record_count: int
    first_timestamp: str | None
    last_timestamp: str | None
    instrument_ids: list[int]
    raw_symbols: list[str]
    action_counts: dict[str, int]


def summarize_dbn(path: Path) -> DbnSummary:
    store = open_dbn(path)
    schema = str(getattr(store, "schema", "") or "")
    dataset = str(getattr(store, "dataset", "") or "")
    symbols = store_symbols(store)
    instrument_ids: set[int] = set()
    action_counts: Counter[str] = Counter()
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    record_count = 0

    for record in store:
        event = normalize_record(record)
        record_count += 1
        instrument_ids.add(event.instrument_id)
        action_counts[event.action] += 1
        first_timestamp = first_timestamp or event.timestamp
        last_timestamp = event.timestamp

    return DbnSummary(
        file=str(path),
        dataset=dataset,
        schema=schema,
        record_count=record_count,
        first_timestamp=first_timestamp,
        last_timestamp=last_timestamp,
        instrument_ids=sorted(instrument_ids),
        raw_symbols=symbols,
        action_counts=dict(action_counts),
    )


@dataclass
class Order:
    side: str
    price: int
    size: int
    ts_event: int
    priority_changed: bool = False


@dataclass
class PriceLevel:
    price: int
    order_count: int = 0
    total_size: int = 0
    order_ids: set[int] = field(default_factory=set, repr=False)


@dataclass(frozen=True)
class BookSnapshot:
    best_bid: PriceLevel | None
    best_ask: PriceLevel | None
    spread: int | None
    bids: list[PriceLevel]
    asks: list[PriceLevel]


class SnapshotStatus(str, Enum):
    PRE_SNAPSHOT = "PRE_SNAPSHOT"
    SNAPSHOT_LOADING = "SNAPSHOT_LOADING"
    SNAPSHOT_READY = "SNAPSHOT_READY"
    POST_SNAPSHOT = "POST_SNAPSHOT"


class OrderBook:
    def __init__(self, *, dedupe_window_size: int = 50_000) -> None:
        self.orders_by_id: dict[int, Order] = {}
        self.bids: SortedDict[int, PriceLevel] = SortedDict()
        self.asks: SortedDict[int, PriceLevel] = SortedDict()
        self._order_ids_by_side: dict[str, set[int]] = {"B": set(), "A": set()}
        self._seen_events: set[tuple[Any, ...]] = set()
        self._event_window: deque[tuple[Any, ...]] = deque()
        self._dedupe_window_size = dedupe_window_size
        self._snapshot_last_sequence: dict[tuple[int, int], int] = {}
        self._natural_last_sequence: dict[tuple[int, int], int] = {}

        self.records_processed = 0
        self.duplicate_events = 0
        self.duplicate_adds = 0
        self.unknown_order_events = 0
        self.unknown_cancel_events = 0
        self.unknown_modify_events = 0
        self.unknown_fill_events = 0
        self.unknown_order_references_pre_snapshot = 0
        self.unknown_order_references_during_snapshot = 0
        self.unknown_order_references_after_snapshot = 0
        self.oversized_cancels = 0
        self.negative_level_sizes = 0
        self.invalid_sides = 0
        self.undefined_prices = 0
        self.invalid_sizes = 0
        self.snapshot_records_observed = 0
        self.snapshot_sequence_regressions_ignored = 0
        self.natural_events_after_snapshot = 0
        self.natural_sequence_gaps = 0
        self.natural_sequence_regressions = 0
        self.repeated_sequence_values = 0
        self.multiple_instrument_events = 0
        self.integrity_warnings = 0
        self.post_snapshot_integrity_warnings = 0
        self.priority_resets = 0
        self.snapshots_created = 0
        self.action_counts: Counter[str] = Counter()
        self.instrument_ids: set[int] = set()
        self.primary_instrument_id: int | None = None
        self.snapshot_status = SnapshotStatus.PRE_SNAPSHOT
        self.saw_snapshot = False
        self.is_consistent = True

    @property
    def orders(self) -> dict[int, Order]:
        return self.orders_by_id

    @property
    def is_snapshot_ready(self) -> bool:
        return self.snapshot_status in {
            SnapshotStatus.SNAPSHOT_READY,
            SnapshotStatus.POST_SNAPSHOT,
        }

    @property
    def out_of_order_sequences(self) -> int:
        return self.natural_sequence_regressions

    def _remember_event(self, identity: tuple[Any, ...]) -> bool:
        if identity in self._seen_events:
            self.duplicate_events += 1
            return False
        if len(self._event_window) >= self._dedupe_window_size:
            expired = self._event_window.popleft()
            self._seen_events.discard(expired)
        self._event_window.append(identity)
        self._seen_events.add(identity)
        return True

    def _warn_if_complete(self) -> None:
        if self.is_snapshot_ready:
            self.integrity_warnings += 1
            if self.snapshot_status == SnapshotStatus.POST_SNAPSHOT:
                self.post_snapshot_integrity_warnings += 1

    def _accept_instrument(self, instrument_id: int) -> bool:
        self.instrument_ids.add(instrument_id)
        if self.primary_instrument_id is None:
            self.primary_instrument_id = instrument_id
            return True
        if instrument_id == self.primary_instrument_id:
            return True
        self.multiple_instrument_events += 1
        self._warn_if_complete()
        return False

    def _observe_phase_and_sequence(self, event: MboEvent) -> None:
        sequence_key = (event.publisher_id, event.channel_id)
        if event.flags & F_SNAPSHOT:
            self.saw_snapshot = True
            self.snapshot_records_observed += 1
            if event.sequence > 0:
                previous = self._snapshot_last_sequence.get(sequence_key)
                if previous is not None and event.sequence < previous:
                    self.snapshot_sequence_regressions_ignored += 1
                if previous is None or event.sequence > previous:
                    self._snapshot_last_sequence[sequence_key] = event.sequence
            if event.action == "R":
                self.snapshot_status = SnapshotStatus.SNAPSHOT_LOADING
            elif not self.is_snapshot_ready:
                self.snapshot_status = SnapshotStatus.SNAPSHOT_LOADING
            return

        if self.snapshot_status == SnapshotStatus.SNAPSHOT_READY:
            self.snapshot_status = SnapshotStatus.POST_SNAPSHOT
        elif self.snapshot_status == SnapshotStatus.SNAPSHOT_LOADING:
            self.integrity_warnings += 1
            return

        if self.snapshot_status != SnapshotStatus.POST_SNAPSHOT:
            return

        self.natural_events_after_snapshot += 1
        if event.sequence <= 0:
            return
        previous = self._natural_last_sequence.get(sequence_key)
        if previous is None:
            self._natural_last_sequence[sequence_key] = event.sequence
        elif event.sequence < previous:
            self.natural_sequence_regressions += 1
            self._warn_if_complete()
        elif event.sequence == previous:
            self.repeated_sequence_values += 1
        else:
            if event.sequence > previous + 1:
                self.natural_sequence_gaps += 1
            self._natural_last_sequence[sequence_key] = event.sequence

    def _finish_event_group(self, event: MboEvent) -> bool:
        is_last = bool(event.flags & F_LAST)
        self.is_consistent = is_last
        if not is_last:
            return False
        if (
            self.snapshot_status == SnapshotStatus.SNAPSHOT_LOADING
            and event.flags & F_SNAPSHOT
        ):
            self.snapshot_status = SnapshotStatus.SNAPSHOT_READY
        return True

    def _levels_for_side(self, side: str) -> SortedDict[int, PriceLevel] | None:
        if side == "B":
            return self.bids
        if side == "A":
            return self.asks
        self.invalid_sides += 1
        self._warn_if_complete()
        return None

    def _valid_price(self, price: int) -> bool:
        if price == db.UNDEF_PRICE:
            self.undefined_prices += 1
            self._warn_if_complete()
            return False
        return True

    def _add_to_level(self, order_id: int, order: Order) -> None:
        levels = self._levels_for_side(order.side)
        if levels is None:
            return
        level = levels.get(order.price)
        if level is None:
            level = PriceLevel(price=order.price)
            levels[order.price] = level
        level.total_size += order.size
        level.order_count += 1
        level.order_ids.add(order_id)
        self._order_ids_by_side[order.side].add(order_id)

    def _remove_from_level(self, order_id: int, order: Order) -> None:
        levels = self._levels_for_side(order.side)
        if levels is None:
            return
        level = levels.get(order.price)
        if level is None:
            self.negative_level_sizes += 1
            self._warn_if_complete()
            self._order_ids_by_side[order.side].discard(order_id)
            return
        level.total_size -= order.size
        level.order_count -= 1
        level.order_ids.discard(order_id)
        self._order_ids_by_side[order.side].discard(order_id)
        if level.total_size < 0 or level.order_count < 0:
            self.negative_level_sizes += 1
            self._warn_if_complete()
            levels.pop(order.price, None)
        elif level.total_size == 0 or level.order_count == 0:
            levels.pop(order.price, None)

    def _clear(self) -> None:
        self.orders_by_id.clear()
        self.bids.clear()
        self.asks.clear()
        self._order_ids_by_side["B"].clear()
        self._order_ids_by_side["A"].clear()

    def _clear_side(self, side: str) -> None:
        if self._levels_for_side(side) is None:
            return
        for order_id in tuple(self._order_ids_by_side[side]):
            order = self.orders_by_id.pop(order_id, None)
            if order is not None:
                self._remove_from_level(order_id, order)

    def _apply_add(self, event: MboEvent) -> None:
        if event.flags & F_TOB:
            self._clear_side(event.side)
            if not self._valid_price(event.price):
                return
        if self._levels_for_side(event.side) is None or not self._valid_price(event.price):
            return
        if event.size <= 0:
            self.invalid_sizes += 1
            self._warn_if_complete()
            return
        if event.order_id in self.orders_by_id:
            self.duplicate_adds += 1
            self._warn_if_complete()
            return
        order = Order(event.side, event.price, event.size, event.ts_event)
        self.orders_by_id[event.order_id] = order
        self._add_to_level(event.order_id, order)

    def _unknown_order(self, action: str) -> None:
        self.unknown_order_events += 1
        if action == "C":
            self.unknown_cancel_events += 1
        elif action == "M":
            self.unknown_modify_events += 1
        elif action == "F":
            self.unknown_fill_events += 1

        if self.snapshot_status == SnapshotStatus.PRE_SNAPSHOT:
            self.unknown_order_references_pre_snapshot += 1
        elif self.snapshot_status == SnapshotStatus.SNAPSHOT_LOADING:
            self.unknown_order_references_during_snapshot += 1
        elif self.snapshot_status == SnapshotStatus.POST_SNAPSHOT:
            self.unknown_order_references_after_snapshot += 1

        if action in {"C", "M"} and self.snapshot_status == SnapshotStatus.POST_SNAPSHOT:
            self._warn_if_complete()

    def _apply_cancel(self, event: MboEvent) -> None:
        existing = self.orders_by_id.get(event.order_id)
        if existing is None:
            self._unknown_order("C")
            return
        if event.size <= 0:
            self.invalid_sizes += 1
            self._warn_if_complete()
            return
        cancel_size = event.size
        if cancel_size > existing.size:
            self.oversized_cancels += 1
            self._warn_if_complete()
            cancel_size = existing.size

        levels = self._levels_for_side(existing.side)
        level = levels.get(existing.price) if levels is not None else None
        if level is None:
            self.negative_level_sizes += 1
            self._warn_if_complete()
            self.orders_by_id.pop(event.order_id, None)
            self._order_ids_by_side[existing.side].discard(event.order_id)
            return

        existing.size -= cancel_size
        level.total_size -= cancel_size
        if existing.size == 0:
            self.orders_by_id.pop(event.order_id, None)
            self._order_ids_by_side[existing.side].discard(event.order_id)
            level.order_count -= 1
            level.order_ids.discard(event.order_id)
        if level.total_size < 0 or level.order_count < 0:
            self.negative_level_sizes += 1
            self._warn_if_complete()
            levels.pop(existing.price, None)
        elif level.total_size == 0 or level.order_count == 0:
            levels.pop(existing.price, None)

    def _apply_modify(self, event: MboEvent) -> None:
        existing = self.orders_by_id.get(event.order_id)
        if existing is None:
            self._unknown_order("M")
            return
        if self._levels_for_side(event.side) is None or not self._valid_price(event.price):
            return
        if event.size <= 0:
            self.invalid_sizes += 1
            self._warn_if_complete()
            return

        priority_changed = existing.price != event.price or existing.side != event.side or event.size > existing.size
        self._remove_from_level(event.order_id, existing)
        if priority_changed:
            self.priority_resets += 1
        updated = Order(event.side, event.price, event.size, event.ts_event, priority_changed)
        self.orders_by_id[event.order_id] = updated
        self._add_to_level(event.order_id, updated)

    def apply(self, event: MboEvent) -> bool:
        self.records_processed += 1
        self.is_consistent = False
        self.action_counts[event.action] += 1
        identity = (
            event.instrument_id,
            event.publisher_id,
            event.channel_id,
            event.sequence,
            event.ts_event,
            event.ts_recv,
            event.action,
            event.side,
            event.price,
            event.size,
            event.order_id,
            event.flags,
        )
        if event.action not in {"T", "F", "N"} and not self._remember_event(identity):
            self.is_consistent = bool(event.flags & F_LAST)
            return False
        self._observe_phase_and_sequence(event)
        if not self._accept_instrument(event.instrument_id):
            return self._finish_event_group(event)

        if event.action == "F":
            if event.order_id and event.order_id not in self.orders_by_id:
                self._unknown_order("F")
        elif event.action in {"T", "N"}:
            pass
        elif event.action == "R":
            self._clear()
        elif event.action == "A":
            self._apply_add(event)
        elif event.action == "M":
            self._apply_modify(event)
        elif event.action == "C":
            self._apply_cancel(event)
        return self._finish_event_group(event)

    @staticmethod
    def _copy_level(level: PriceLevel) -> PriceLevel:
        return PriceLevel(level.price, level.order_count, level.total_size)

    def best_bid(self) -> PriceLevel | None:
        return self._copy_level(self.bids.peekitem(-1)[1]) if self.bids else None

    def best_ask(self) -> PriceLevel | None:
        return self._copy_level(self.asks.peekitem(0)[1]) if self.asks else None

    def snapshot(self, depth: int = 10) -> BookSnapshot:
        if depth < 1:
            raise ConnectorError("Snapshot depth must be at least one.")
        self.snapshots_created += 1
        bid_items = self.bids.items()[-depth:]
        ask_items = self.asks.items()[:depth]
        bids = [self._copy_level(level) for _, level in reversed(bid_items)]
        asks = [self._copy_level(level) for _, level in ask_items]
        best_bid = bids[0] if bids else None
        best_ask = asks[0] if asks else None
        spread = best_ask.price - best_bid.price if best_bid and best_ask else None
        return BookSnapshot(best_bid, best_ask, spread, bids, asks)


def reconstruct_book(
    events: Iterable[MboEvent],
    *,
    book: OrderBook | None = None,
    progress_every: int | None = None,
    progress_callback: Callable[[int], None] | None = None,
) -> tuple[OrderBook, BookSnapshot | None, int]:
    active_book = book or OrderBook()
    complete_groups = 0
    for event in events:
        if active_book.apply(event):
            complete_groups += 1
        if (
            progress_every
            and progress_callback
            and active_book.records_processed % progress_every == 0
        ):
            progress_callback(active_book.records_processed)
    final_snapshot = (
        active_book.snapshot()
        if complete_groups > 0 and active_book.is_consistent
        else None
    )
    return active_book, final_snapshot, complete_groups


def manifest_record_count(path: Path) -> int | None:
    manifest_path = Path(f"{path}.manifest.json")
    if not manifest_path.is_file():
        return None
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8")).get("recordCount")
        return int(value) if value is not None else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def peak_rss_mib() -> float:
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    bytes_value = raw if sys.platform == "darwin" else raw * 1024
    return bytes_value / (1024 * 1024)


def _print_levels(title: str, levels: list[PriceLevel]) -> None:
    print(title)
    print("Price | Orders | Total size")
    for level in levels:
        print(f"{display_price(level.price)} | {level.order_count} | {level.total_size}")
    if not levels:
        print("-")


def print_book(path: Path) -> None:
    total_records = manifest_record_count(path)
    rss_before = peak_rss_mib()
    started = time.perf_counter()

    def report_progress(processed: int) -> None:
        total = f" / {total_records:,}" if total_records is not None else ""
        print(f"Processed: {processed:,}{total}", flush=True)

    book, snapshot, complete_groups = reconstruct_book(
        iter_events(path),
        progress_every=100_000,
        progress_callback=report_progress,
    )
    elapsed = time.perf_counter() - started
    rate = book.records_processed / elapsed if elapsed else 0.0
    rss_after = peak_rss_mib()
    if snapshot is None:
        raise ConnectorError("No complete event group with F_LAST was found.")
    print("DATABENTO ORDER BOOK TEST")
    print()
    print(f"File: {path}")
    print(f"Records processed: {book.records_processed}")
    print(f"Complete event groups: {complete_groups}")
    print(f"Open orders: {len(book.orders_by_id)}")
    print(f"Bid levels: {len(book.bids)}")
    print(f"Ask levels: {len(book.asks)}")
    print(f"Best bid (observed): {display_price(snapshot.best_bid.price) if snapshot.best_bid else '-'}")
    print(f"Best ask (observed): {display_price(snapshot.best_ask.price) if snapshot.best_ask else '-'}")
    print(f"Spread: {display_price(snapshot.spread) if snapshot.spread is not None else '-'}")
    print(f"Elapsed seconds: {elapsed:.3f}")
    print(f"Records per second: {rate:,.0f}")
    print(f"Peak RSS MiB: {rss_after:.1f}")
    print(f"Peak RSS increase MiB: {max(0.0, rss_after - rss_before):.1f}")
    print(f"Snapshot status: {book.snapshot_status.value}")
    print(f"Book completeness: {'COMPLETE' if book.is_snapshot_ready else 'PARTIAL'}")
    print(f"BBO reliability: {'GUARANTEED' if book.is_snapshot_ready else 'NOT GUARANTEED'}")
    if not book.is_snapshot_ready:
        print("Reason: Request did not include an initial MBO snapshot.")
    print(f"Snapshot records observed: {book.snapshot_records_observed}")
    print(
        "Snapshot sequence regressions ignored: "
        f"{book.snapshot_sequence_regressions_ignored}"
    )
    print(f"Natural events after snapshot: {book.natural_events_after_snapshot}")
    print(f"Natural sequence gaps: {book.natural_sequence_gaps}")
    print(f"Natural sequence regressions: {book.natural_sequence_regressions}")
    print(f"Repeated sequence values: {book.repeated_sequence_values}")
    print(f"Unknown order references: {book.unknown_order_events}")
    print(f"Unknown references before snapshot: {book.unknown_order_references_pre_snapshot}")
    print(f"Unknown references during snapshot: {book.unknown_order_references_during_snapshot}")
    print(f"Unknown references after snapshot: {book.unknown_order_references_after_snapshot}")
    print(f"Unknown cancel references: {book.unknown_cancel_events}")
    print(f"Unknown modify references: {book.unknown_modify_events}")
    print(f"Unknown fill references: {book.unknown_fill_events}")
    print(f"Duplicate adds: {book.duplicate_adds}")
    print(f"Duplicate events skipped: {book.duplicate_events}")
    print(f"Oversized cancels: {book.oversized_cancels}")
    print(f"Multiple-instrument events skipped: {book.multiple_instrument_events}")
    print(f"Integrity warnings: {book.integrity_warnings}")
    print(
        "Post-snapshot integrity: "
        f"{'FAILED' if book.post_snapshot_integrity_warnings else 'PASSED'}"
    )
    print(f"Event counts: {dict(sorted(book.action_counts.items()))}")
    print()
    _print_levels("Top 10 Bid Levels", snapshot.bids)
    print()
    _print_levels("Top 10 Ask Levels", snapshot.asks)


def print_preview(path: Path, limit: int) -> None:
    print("DATABENTO EVENT PREVIEW")
    print()
    print(f"File: {path}")
    print(f"Limit: {limit}")
    print("Timestamp | Action | Side | Price | Size | Order ID | Instrument ID | Sequence | Flags")
    for event in iter_events(path, limit=limit):
        print(
            f"{event.timestamp} | {event.action} | {event.side} | {display_price(event.price)} | "
            f"{event.size} | {event.order_id} | {event.instrument_id} | {event.sequence} | {event.flags}"
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read Databento MBO DBN files safely.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    preview = subparsers.add_parser("preview")
    preview_files = preview.add_mutually_exclusive_group()
    preview_files.add_argument("--file")
    preview_files.add_argument("--latest", action="store_true")
    preview.add_argument("--limit", type=int, default=100)
    book = subparsers.add_parser("book")
    book_files = book.add_mutually_exclusive_group()
    book_files.add_argument("--file")
    book_files.add_argument("--latest", action="store_true")
    book.add_argument("--snapshot", choices=["final"], default="final")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        path = resolve_data_file(args.file, latest=args.latest)
        announce_data_file_selection(path, file_arg=args.file, latest=args.latest)
        if args.command == "preview":
            if args.limit < 1 or args.limit > 100:
                raise ConnectorError("Preview limit must be between 1 and 100.")
            print_preview(path, args.limit)
        else:
            print_book(path)
        return 0
    except Exception as exc:
        print(f"ERROR: {safe_error(exc)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
