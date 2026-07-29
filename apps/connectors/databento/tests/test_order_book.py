from __future__ import annotations

import time

import databento as db

from apps.connectors.databento.src.dbn_reader import (
    F_LAST,
    F_SNAPSHOT,
    MboEvent,
    OrderBook,
    SnapshotStatus,
    reconstruct_book,
)


def event(
    action: str,
    order_id: int,
    *,
    side: str = "B",
    price: int = 5_000_000_000_000,
    size: int = 1,
    sequence: int = 1,
    ts_event: int | None = None,
    flags: int = F_LAST,
    instrument_id: int = 123,
    publisher_id: int = 1,
    channel_id: int = 0,
) -> MboEvent:
    timestamp_value = ts_event or sequence
    return MboEvent(
        timestamp=f"t{timestamp_value}",
        ts_event=timestamp_value,
        action=action,
        side=side,
        price=price,
        size=size,
        order_id=order_id,
        instrument_id=instrument_id,
        sequence=sequence,
        flags=flags,
        ts_recv=timestamp_value,
        publisher_id=publisher_id,
        channel_id=channel_id,
    )


def test_add_best_bid_ask_spread_and_top_ten() -> None:
    events = []
    for index in range(12):
        events.append(event("A", 100 + index, price=(5_000 - index) * 1_000_000_000, sequence=index + 1))
    events.append(event("A", 999, side="A", price=5_001_000_000_000, sequence=20))
    book, snapshot, groups = reconstruct_book(events)
    assert groups == 13
    assert snapshot is not None
    assert snapshot.best_bid.price == 5_000_000_000_000
    assert snapshot.best_ask.price == 5_001_000_000_000
    assert snapshot.spread == 1_000_000_000
    assert len(snapshot.bids) == 10


def test_orders_aggregate_by_price_level() -> None:
    _, snapshot, _ = reconstruct_book(
        [
            event("A", 1, size=2, sequence=1),
            event("A", 2, size=3, sequence=2),
        ]
    )
    assert snapshot is not None
    assert snapshot.best_bid.order_count == 2
    assert snapshot.best_bid.total_size == 5


def test_modify_size_updates_order_and_level() -> None:
    book = OrderBook()
    book.apply(event("A", 1, size=2, sequence=1))
    book.apply(event("M", 1, side="B", price=5_000_000_000_000, size=4, sequence=2))
    assert book.orders[1].size == 4
    assert book.bids[5_000_000_000_000].total_size == 4
    assert book.bids[5_000_000_000_000].order_count == 1
    assert book.orders[1].priority_changed is True


def test_modify_price_moves_order_between_levels() -> None:
    book = OrderBook()
    book.apply(event("A", 1, size=2, sequence=1))
    book.apply(event("M", 1, side="B", price=5_001_000_000_000, size=2, sequence=2))
    assert 5_000_000_000_000 not in book.bids
    assert book.bids[5_001_000_000_000].total_size == 2
    assert book.orders[1].price == 5_001_000_000_000


def test_partial_cancel_updates_order_and_level() -> None:
    book = OrderBook()
    book.apply(event("A", 1, size=5, sequence=1))
    book.apply(event("C", 1, size=2, sequence=2))
    assert book.orders[1].size == 3
    assert book.bids[5_000_000_000_000].total_size == 3
    assert book.bids[5_000_000_000_000].order_count == 1


def test_full_cancel_removes_order_and_empty_level() -> None:
    book = OrderBook()
    book.apply(event("A", 1, size=3, sequence=1))
    book.apply(event("C", 1, size=3, sequence=3))
    assert 1 not in book.orders
    assert 5_000_000_000_000 not in book.bids


def test_trade_does_not_change_resting_book() -> None:
    book = OrderBook()
    book.apply(event("A", 1, size=5, sequence=1))
    book.apply(event("T", 1, size=2, sequence=2))
    assert book.orders[1].size == 5


def test_fill_does_not_change_resting_book() -> None:
    book = OrderBook()
    book.apply(event("A", 1, size=5, sequence=1))
    book.apply(event("F", 1, size=2, sequence=3))
    assert book.orders[1].size == 5


def test_identical_fill_records_are_all_counted_but_do_not_change_book() -> None:
    book = OrderBook()
    fill = event("F", 1, size=2, sequence=1)
    book.apply(fill)
    book.apply(fill)
    assert book.action_counts["F"] == 2
    assert book.duplicate_events == 0
    assert not book.orders


def test_clear_resets_both_sides() -> None:
    book = OrderBook()
    book.apply(event("A", 1, sequence=1))
    book.apply(event("A", 2, side="A", sequence=2))
    book.apply(event("R", 0, side="N", price=0, size=0, sequence=3))
    assert book.orders == {}


def test_duplicate_event_is_ignored() -> None:
    book = OrderBook()
    duplicate = event("A", 1, size=2, sequence=1)
    assert book.apply(duplicate) is True
    assert book.apply(duplicate) is False
    assert book.duplicate_events == 1
    assert len(book.orders) == 1


def test_duplicate_add_is_counted_without_double_adding_level() -> None:
    book = OrderBook()
    book.apply(event("A", 1, size=2, sequence=1))
    book.apply(event("A", 1, size=9, sequence=2))
    assert book.duplicate_adds == 1
    assert book.orders[1].size == 2
    assert book.bids[5_000_000_000_000].total_size == 2
    assert book.bids[5_000_000_000_000].order_count == 1


def test_unknown_order_id_is_counted_without_crash() -> None:
    book = OrderBook()
    book.apply(event("M", 404, sequence=1))
    book.apply(event("C", 405, sequence=2))
    assert book.unknown_order_events == 2
    assert book.orders == {}


def test_incomplete_event_group_is_not_published() -> None:
    _, snapshot, groups = reconstruct_book([event("A", 1, flags=0)])
    assert groups == 0
    assert snapshot is None


def test_f_last_does_not_create_snapshot_per_event_group() -> None:
    book = OrderBook()
    events = [event("A", index, sequence=index + 1) for index in range(100)]
    result_book, snapshot, groups = reconstruct_book(events, book=book)
    assert groups == 100
    assert snapshot is not None
    assert result_book.snapshots_created == 1


def test_snapshot_clear_add_and_f_last_reaches_ready() -> None:
    book = OrderBook()
    book.apply(event("R", 0, side="N", price=0, size=0, sequence=1, flags=F_SNAPSHOT))
    assert book.snapshot_status == SnapshotStatus.SNAPSHOT_LOADING
    book.apply(event("A", 1, sequence=2, flags=F_SNAPSHOT))
    book.apply(event("A", 2, sequence=3, flags=F_SNAPSHOT | F_LAST))
    assert book.saw_snapshot is True
    assert book.snapshot_status == SnapshotStatus.SNAPSHOT_READY
    assert book.is_snapshot_ready is True


def test_empty_snapshot_clear_with_f_last_reaches_ready() -> None:
    book = OrderBook()
    book.apply(
        event(
            "R",
            0,
            side="N",
            price=0,
            size=0,
            sequence=0,
            flags=F_SNAPSHOT | F_LAST,
        )
    )
    assert book.snapshot_status == SnapshotStatus.SNAPSHOT_READY
    assert book.snapshot_sequence_regressions_ignored == 0


def test_unknown_cancel_in_partial_mode_is_counted_without_integrity_warning() -> None:
    book = OrderBook()
    book.apply(event("C", 404, sequence=1))
    assert book.snapshot_status == SnapshotStatus.PRE_SNAPSHOT
    assert book.unknown_cancel_events == 1
    assert book.unknown_order_references_pre_snapshot == 1
    assert book.integrity_warnings == 0


def test_unknown_cancel_after_ready_snapshot_is_integrity_warning() -> None:
    book = OrderBook()
    book.apply(event("R", 0, side="N", price=0, size=0, sequence=1, flags=F_SNAPSHOT))
    book.apply(event("A", 1, sequence=2, flags=F_SNAPSHOT | F_LAST))
    book.apply(event("C", 404, sequence=3))
    assert book.snapshot_status == SnapshotStatus.POST_SNAPSHOT
    assert book.unknown_cancel_events == 1
    assert book.integrity_warnings == 1
    assert book.post_snapshot_integrity_warnings == 1
    assert book.unknown_order_references_after_snapshot == 1


def test_best_bid_is_highest_incremental_level() -> None:
    book = OrderBook()
    book.apply(event("A", 1, price=4_999_000_000_000, sequence=1))
    book.apply(event("A", 2, price=5_000_000_000_000, sequence=2))
    assert book.best_bid().price == 5_000_000_000_000


def test_best_ask_is_lowest_incremental_level() -> None:
    book = OrderBook()
    book.apply(event("A", 1, side="A", price=5_002_000_000_000, sequence=1))
    book.apply(event("A", 2, side="A", price=5_001_000_000_000, sequence=2))
    assert book.best_ask().price == 5_001_000_000_000


def test_oversized_cancel_is_clamped_without_negative_level() -> None:
    book = OrderBook()
    book.apply(event("A", 1, size=2, sequence=1))
    book.apply(event("C", 1, size=5, sequence=2))
    assert book.oversized_cancels == 1
    assert book.negative_level_sizes == 0
    assert not book.orders
    assert not book.bids


def test_invalid_side_and_undefined_price_are_skipped() -> None:
    book = OrderBook()
    book.apply(event("A", 1, side="N", sequence=1))
    book.apply(event("A", 2, price=int(db.UNDEF_PRICE), sequence=2))
    assert book.invalid_sides == 1
    assert book.undefined_prices == 1
    assert not book.orders


def test_out_of_order_sequence_and_second_instrument_are_counted() -> None:
    book = OrderBook()
    book.apply(event("R", 0, side="N", price=0, size=0, sequence=0, flags=F_SNAPSHOT))
    book.apply(event("A", 1, sequence=10, flags=F_SNAPSHOT | F_LAST))
    book.apply(event("T", 0, side="N", price=0, size=0, sequence=5))
    book.apply(event("T", 0, side="N", price=0, size=0, sequence=4))
    book.apply(event("A", 2, sequence=6, instrument_id=456))
    assert book.out_of_order_sequences == 1
    assert book.multiple_instrument_events == 1
    assert set(book.orders) == {1}


def test_snapshot_and_natural_sequence_diagnostics_are_separate() -> None:
    book = OrderBook()
    book.apply(event("R", 0, side="N", price=0, size=0, sequence=0, flags=F_SNAPSHOT))
    book.apply(event("A", 1, sequence=20, flags=F_SNAPSHOT))
    book.apply(event("A", 2, sequence=10, flags=F_SNAPSHOT | F_LAST))
    book.apply(event("T", 0, side="N", price=0, size=0, sequence=100))
    book.apply(event("T", 0, side="N", price=0, size=0, sequence=100, ts_event=101))
    book.apply(event("T", 0, side="N", price=0, size=0, sequence=103))
    book.apply(event("T", 0, side="N", price=0, size=0, sequence=102))

    assert book.snapshot_records_observed == 3
    assert book.snapshot_sequence_regressions_ignored == 1
    assert book.natural_events_after_snapshot == 4
    assert book.repeated_sequence_values == 1
    assert book.natural_sequence_gaps == 1
    assert book.natural_sequence_regressions == 1


def test_natural_sequences_are_keyed_by_publisher_and_channel() -> None:
    book = OrderBook()
    book.apply(event("R", 0, side="N", price=0, size=0, sequence=0, flags=F_SNAPSHOT))
    book.apply(event("A", 1, sequence=10, flags=F_SNAPSHOT | F_LAST))
    book.apply(event("T", 0, side="N", price=0, size=0, sequence=10, channel_id=1))
    book.apply(event("T", 0, side="N", price=0, size=0, sequence=2, channel_id=2))
    assert book.natural_sequence_regressions == 0
    assert book.natural_sequence_gaps == 0


def test_unknown_references_are_split_by_phase_and_fill_never_warns() -> None:
    book = OrderBook()
    book.apply(event("M", 10, sequence=1))
    book.apply(event("R", 0, side="N", price=0, size=0, sequence=0, flags=F_SNAPSHOT))
    book.apply(event("C", 20, sequence=2, flags=F_SNAPSHOT))
    book.apply(event("A", 1, sequence=3, flags=F_SNAPSHOT | F_LAST))
    book.apply(event("F", 30, sequence=4))

    assert book.unknown_order_references_pre_snapshot == 1
    assert book.unknown_order_references_during_snapshot == 1
    assert book.unknown_order_references_after_snapshot == 1
    assert book.unknown_modify_events == 1
    assert book.unknown_cancel_events == 1
    assert book.unknown_fill_events == 1
    assert book.post_snapshot_integrity_warnings == 0


def test_one_hundred_thousand_events_performance_regression() -> None:
    def events():
        for index in range(50_000):
            order_id = index + 1
            price = (5_000 + index % 20) * 1_000_000_000
            yield event("A", order_id, price=price, size=2, sequence=index * 2 + 1)
            yield event("C", order_id, price=price, size=2, sequence=index * 2 + 2)

    started = time.perf_counter()
    book, snapshot, groups = reconstruct_book(events())
    elapsed = time.perf_counter() - started
    assert book.records_processed == 100_000
    assert groups == 100_000
    assert book.snapshots_created == 1
    assert snapshot is not None
    assert not book.orders
    assert elapsed < 8.0
