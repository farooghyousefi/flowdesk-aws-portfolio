from __future__ import annotations

import json
from datetime import UTC, datetime
from dataclasses import replace
from pathlib import Path

import databento as db
import databento_dbn as dbn

from apps.connectors.databento.src.config import build_request, build_verification_request
from apps.connectors.databento.src.dbn_reader import F_LAST, F_SNAPSHOT, MboEvent
from apps.connectors.databento.src.verify_book import (
    BookLevelState,
    Top10State,
    compare_states,
    iter_mbp10_states,
    summarize_reference,
    verify_streams,
    write_reports,
)


def mbo_event(
    action: str,
    order_id: int,
    *,
    side: str = "B",
    price: int = 100_000_000_000,
    size: int = 1,
    sequence: int = 1,
    ts_event: int = 1,
    flags: int = F_LAST,
) -> MboEvent:
    return MboEvent(
        timestamp=f"t{ts_event}",
        ts_event=ts_event,
        action=action,
        side=side,
        price=price,
        size=size,
        order_id=order_id,
        instrument_id=123,
        sequence=sequence,
        flags=flags,
        ts_recv=ts_event,
        publisher_id=1,
        channel_id=0,
    )


def state(
    *,
    bid_price: int = 100_000_000_000,
    bid_size: int = 3,
    bid_count: int = 2,
    ask_price: int = 101_000_000_000,
    ask_size: int = 3,
    ask_count: int = 1,
    second_bid_price: int = int(db.UNDEF_PRICE),
) -> Top10State:
    empty = BookLevelState(int(db.UNDEF_PRICE), 0, 0)
    bids = [BookLevelState(bid_price, bid_size, bid_count)]
    bids.append(
        empty
        if second_bid_price == db.UNDEF_PRICE
        else BookLevelState(second_bid_price, 1, 1)
    )
    bids.extend([empty] * 8)
    asks = [BookLevelState(ask_price, ask_size, ask_count), *([empty] * 9)]
    return Top10State("t4", 4, 1, 123, 11, tuple(bids), tuple(asks))


def complete_mbo_group() -> list[MboEvent]:
    return [
        mbo_event("R", 0, side="N", price=0, size=0, sequence=0, flags=F_SNAPSHOT),
        mbo_event("A", 1, size=2, sequence=10, ts_event=2, flags=F_SNAPSHOT),
        mbo_event(
            "A",
            2,
            side="A",
            price=101_000_000_000,
            size=3,
            sequence=10,
            ts_event=2,
            flags=F_SNAPSHOT | F_LAST,
        ),
        mbo_event("A", 3, size=1, sequence=11, ts_event=4, flags=0),
        mbo_event("N", 0, side="N", price=0, size=0, sequence=11, ts_event=4),
    ]


def test_fixed_point_top10_match_is_exact() -> None:
    expected = state(second_bid_price=99_000_000_000)
    actual = state(second_bid_price=99_000_000_000)
    assert compare_states(expected, actual) == ()


def test_one_raw_price_unit_has_no_tolerance() -> None:
    expected = state(second_bid_price=99_000_000_000)
    actual = state(second_bid_price=99_000_000_001)
    assert compare_states(expected, actual) == ("top10Prices",)


def test_price_size_and_order_count_mismatches_are_separate() -> None:
    expected = state(second_bid_price=99_000_000_000)
    assert "top10Prices" in compare_states(
        expected,
        state(second_bid_price=99_000_000_001),
    )
    assert "top10Sizes" in compare_states(expected, state(bid_size=4, second_bid_price=99_000_000_000))
    assert "top10OrderCounts" in compare_states(
        expected,
        state(bid_count=3, second_bid_price=99_000_000_000),
    )


def test_verification_compares_only_completed_mbo_event_group() -> None:
    result = verify_streams(complete_mbo_group(), [state()])
    assert result.states_compared == 1
    assert result.state_mismatches == 0
    assert result.passed is True


def test_first_mismatch_contains_group_expected_and_actual() -> None:
    result = verify_streams(complete_mbo_group(), [state(bid_size=4)])
    assert result.passed is False
    assert result.first_mismatch is not None
    assert result.first_mismatch["mismatchFields"] == ["bbo", "top10Sizes"]
    assert len(result.first_mismatch["mboEventGroup"]) == 2
    assert result.first_mismatch["expectedTop10"]["bids"][0]["size"] == 4
    assert result.first_mismatch["actualTop10"]["bids"][0]["size"] == 3
    assert len(result.first_mismatch["relevantPrecedingMboRecords"]) <= 10


def test_verification_matches_event_identity_instead_of_array_index() -> None:
    events = complete_mbo_group()
    events.append(mbo_event("N", 0, side="N", price=0, size=0, sequence=12, ts_event=5))
    expected = replace(
        state(),
        timestamp="t5",
        ts_event=5,
        sequence=12,
        action="N",
        side="N",
    )
    result = verify_streams(events, [expected])
    assert result.mbo_states_without_reference == 1
    assert result.states_compared == 1
    assert result.passed is True


def test_mbp_reader_filters_snapshot_and_non_f_last_records(tmp_path: Path) -> None:
    metadata = dbn.Metadata(
        dataset="GLBX.MDP3",
        start=1,
        end=10,
        stype_in=dbn.SType.CONTINUOUS,
        stype_out=dbn.SType.INSTRUMENT_ID,
        schema=dbn.Schema.MBP_10,
        symbols=["MES.v.0"],
    )
    levels = [
        dbn.BidAskPair(
            bid_px=100_000_000_000 if index == 0 else dbn.UNDEF_PRICE,
            ask_px=101_000_000_000 if index == 0 else dbn.UNDEF_PRICE,
            bid_sz=3 if index == 0 else 0,
            ask_sz=3 if index == 0 else 0,
            bid_ct=2 if index == 0 else 0,
            ask_ct=1 if index == 0 else 0,
        )
        for index in range(10)
    ]

    def record(ts_event: int, flags: int) -> dbn.MBP10Msg:
        return dbn.MBP10Msg(
            publisher_id=1,
            instrument_id=123,
            ts_event=ts_event,
            price=100_000_000_000,
            size=1,
            action=dbn.Action.ADD,
            side=dbn.Side.BID,
            depth=0,
            ts_recv=ts_event,
            flags=flags,
            sequence=11,
            levels=levels,
        )

    path = tmp_path / "reference.dbn.zst"
    records = [record(2, F_SNAPSHOT | F_LAST), record(3, 0), record(4, F_LAST)]
    path.write_bytes(metadata.encode() + b"".join(bytes(item) for item in records))
    states = list(iter_mbp10_states(path))
    assert len(states) == 1
    assert states[0].ts_event == 4

    request = build_verification_request(
        "1970-01-01T00:00:00.000000001Z",
        "1970-01-01T00:00:00.000000010Z",
        123,
        "MESU6",
        limit=3,
    )
    summary = summarize_reference(path, request)
    assert summary.record_count == 3
    assert summary.instrument_ids == [123]


def test_text_and_json_reports_are_written(tmp_path: Path) -> None:
    result = verify_streams(complete_mbo_group(), [state()])
    request = build_request(
        "2026-07-13T23:59:55Z",
        "2026-07-14T00:00:10Z",
        schema="mbp-10",
    )
    text_path, json_path = write_reports(
        result,
        Path("mbo.dbn.zst"),
        Path("mbp-10.dbn.zst"),
        request,
        root=tmp_path,
        now=datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
    )
    report = text_path.read_text(encoding="utf-8")
    assert "DATABENTO MBO VS MBP-10 VERIFICATION" in report
    assert "Instrument ID: 123" in report
    assert "Verification: PASSED" in report
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["passed"] is True
    assert payload["comparison"] == "exact fixed-point integer equality; no tolerance"
    assert "db-test-secret" not in json.dumps(payload)
