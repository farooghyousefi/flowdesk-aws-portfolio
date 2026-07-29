from __future__ import annotations

from pathlib import Path

import pytest

from apps.connectors.databento.src.config import ConnectorError
from apps.connectors.databento.src.dbn_reader import display_price, iter_events, normalize_record, summarize_dbn
from apps.connectors.databento.src.validate import validate_summary


def test_dbn_reader_reads_real_dbn_container(synthetic_dbn: Path) -> None:
    summary = summarize_dbn(synthetic_dbn)
    assert summary.dataset == "GLBX.MDP3"
    assert summary.schema == "mbo"
    assert summary.record_count == 2
    assert summary.instrument_ids == [123]
    assert summary.raw_symbols == ["MES.v.0"]
    assert summary.action_counts == {"A": 2}


def test_dbn_records_expose_required_mbo_fields(synthetic_dbn: Path) -> None:
    events = list(iter_events(synthetic_dbn, limit=1))
    assert len(events) == 1
    assert events[0].action == "A"
    assert events[0].side == "B"
    assert events[0].price == 5_000_000_000_000
    assert events[0].size == 2
    assert events[0].order_id == 10
    assert events[0].instrument_id == 123
    assert events[0].sequence == 1
    assert events[0].flags == 128


def test_invalid_dbn_record_is_rejected() -> None:
    with pytest.raises(ConnectorError, match="missing field"):
        normalize_record({"ts_event": 1})


def test_valid_mes_summary_passes_validation(synthetic_dbn: Path) -> None:
    assert validate_summary(summarize_dbn(synthetic_dbn)) == []


def test_fixed_point_prices_are_rendered_without_float_error() -> None:
    assert display_price(5_123_250_000_000) == "5123.25"
    assert display_price(7_580_000_000_000) == "7580"
