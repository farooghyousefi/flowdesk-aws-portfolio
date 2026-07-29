from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from apps.connectors.databento.src.config import (
    DEFAULT_VERIFICATION_LIMIT,
    ConnectorConfig,
    ConnectorError,
    build_verification_request,
    validate_verification_limit,
)
from apps.connectors.databento.src.download import download_range
from apps.connectors.databento.src.estimate import estimate_billable_size, estimate_cost
from apps.connectors.databento.src.verification_context import (
    MboVerificationContext,
    ResolvedContract,
    build_reference_request,
    estimate_with_fallback,
    resolve_contract,
    resolve_verification_window,
)
from apps.connectors.databento.src.verify_estimate import parse_args


class FakeMetadata:
    def __init__(self, costs: dict[int, float] | None = None) -> None:
        self.costs = costs or {1_000: 0.25}
        self.cost_calls: list[dict[str, object]] = []
        self.size_calls: list[dict[str, object]] = []
        self.price_calls: list[dict[str, object]] = []

    def get_cost(self, **kwargs: object) -> float:
        self.cost_calls.append(kwargs)
        return self.costs[int(kwargs["limit"])]

    def get_billable_size(self, **kwargs: object) -> int:
        self.size_calls.append(kwargs)
        return int(kwargs["limit"]) * 400

    def list_unit_prices(self, **kwargs: object) -> list[dict[str, object]]:
        self.price_calls.append(kwargs)
        return [{"mode": "historical", "unit_prices": {"mbp-10": 0.5}}]


class FakeSymbology:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def resolve(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        symbol = str(kwargs["symbols"])
        resolved = "42003239" if kwargs["stype_out"] == "instrument_id" else "MESU6"
        return {
            "result": {symbol: [{"d0": "2026-07-13", "d1": "2026-07-15", "s": resolved}]},
            "partial": [],
            "not_found": [],
        }


class FakeTimeseries:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def get_range(self, **kwargs: object) -> None:
        self.calls.append(kwargs)
        Path(kwargs["path"]).write_bytes(b"dbn")


class FakeClient:
    def __init__(self, costs: dict[int, float] | None = None) -> None:
        self.metadata = FakeMetadata(costs)
        self.symbology = FakeSymbology()
        self.timeseries = FakeTimeseries()


def request(limit: int = 1_000):
    return build_verification_request(
        "2026-07-13T23:59:59.999936957Z",
        "2026-07-14T00:00:01.999936957Z",
        42003239,
        "MESU6",
        limit=limit,
    )


def context() -> MboVerificationContext:
    return MboVerificationContext(
        instrument_id=42003239,
        snapshot_ready_timestamp=100,
        first_natural_f_last_timestamp=120,
        file_start_timestamp=0,
        file_end_timestamp=2_000_000_120,
    )


def test_default_verification_limit_is_one_thousand() -> None:
    assert DEFAULT_VERIFICATION_LIMIT == 1_000
    assert parse_args(["--mbo-file", "x"]).limit == 1_000


def test_hard_verification_limit_is_ten_thousand() -> None:
    assert validate_verification_limit(10_000) == 10_000
    with pytest.raises(ConnectorError, match="hard maximum"):
        validate_verification_limit(10_001)


def test_limit_is_passed_to_cost_and_billable_size() -> None:
    client = FakeClient()
    assert estimate_cost(client, request()) == Decimal("0.25")
    assert estimate_billable_size(client, request()) == 400_000
    assert client.metadata.cost_calls[0]["limit"] == 1_000
    assert client.metadata.size_calls[0]["limit"] == 1_000


def test_limit_and_concrete_instrument_are_passed_to_download(tmp_path: Path) -> None:
    client = FakeClient()
    destination = tmp_path / "reference.dbn.zst"
    download_range(client, request(), destination)
    call = client.timeseries.calls[0]
    assert call["limit"] == 1_000
    assert call["symbols"] == "42003239"
    assert call["stype_in"] == "instrument_id"
    assert destination.read_bytes() == b"dbn"


def test_automatic_window_starts_at_first_natural_state() -> None:
    start, end = resolve_verification_window(context(), None, None)
    assert start == 120
    assert end == 2_000_000_120


def test_start_before_snapshot_is_blocked() -> None:
    with pytest.raises(ConnectorError, match="before SNAPSHOT_READY"):
        resolve_verification_window(
            context(),
            "1970-01-01T00:00:00.000000099Z",
            "1970-01-01T00:00:00.000000150Z",
        )


def test_contract_resolution_binds_continuous_symbol_to_mbo_instrument() -> None:
    client = FakeClient()
    contract = resolve_contract(client, context(), 120, 1_000)
    assert contract == ResolvedContract("MES.v.0", 42003239, "MESU6")
    req = build_reference_request(contract, 120, 1_000, 1_000)
    assert req.symbol == "42003239"
    assert req.stype_in == "instrument_id"


def test_cost_over_one_dollar_triggers_one_hundred_record_fallback() -> None:
    client = FakeClient({1_000: 1.25, 100: 0.12})
    config = ConnectorConfig("db-test-secret", Decimal("1.00"), Decimal("5.00"))
    primary, fallback = estimate_with_fallback(client, request(), config)
    assert primary.estimated_cost_usd == Decimal("1.25")
    assert fallback is not None
    assert fallback.request.limit == 100
    assert fallback.estimated_cost_usd == Decimal("0.12")
    assert [call["limit"] for call in client.metadata.cost_calls] == [1_000, 100]


def test_reference_request_keeps_fixed_point_nanosecond_window() -> None:
    req = request()
    assert req.start_iso == "2026-07-13T23:59:59.999936957Z"
    assert req.end_iso == "2026-07-14T00:00:01.999936957Z"
