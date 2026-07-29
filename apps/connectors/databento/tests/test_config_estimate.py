from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.connectors.databento.src.config import (
    ENV_FILE,
    REPO_ROOT,
    ConnectorConfig,
    ConnectorError,
    build_request,
    announce_data_file_selection,
    list_data_files,
    load_config,
    resolve_data_file,
    safe_error,
)
from apps.connectors.databento.src.estimate import (
    estimate_cost,
    is_request_allowed,
    load_receipt,
    save_receipt,
)


class FakeMetadata:
    def __init__(self, cost: float) -> None:
        self.cost = cost
        self.calls: list[dict[str, object]] = []

    def get_cost(self, **kwargs: object) -> float:
        self.calls.append(kwargs)
        return self.cost


class FakeClient:
    def __init__(self, cost: float) -> None:
        self.metadata = FakeMetadata(cost)


def config(max_request: str = "1.00", max_daily: str = "5.00") -> ConnectorConfig:
    return ConnectorConfig("db-test-secret", Decimal(max_request), Decimal(max_daily))


def request():
    return build_request("2026-07-14T13:30:00Z", "2026-07-14T13:40:00Z")


def test_missing_api_key_is_blocked() -> None:
    with pytest.raises(ConnectorError, match="not configured"):
        load_config(environ={})


def test_repository_root_and_env_file_resolve_to_project() -> None:
    assert (REPO_ROOT / "package.json").is_file()
    assert ENV_FILE == REPO_ROOT / ".env.local"


def test_api_key_is_redacted_from_errors() -> None:
    key = "db-12345678901234567890123456789"
    rendered = safe_error(ValueError(f"invalid API key, was {key}"), (key,))
    assert key not in rendered
    assert "[REDACTED]" in rendered


def test_cost_estimate_uses_exact_safe_request() -> None:
    client = FakeClient(0.123456)
    cost = estimate_cost(client, request())
    assert cost == Decimal("0.123456")
    assert client.metadata.calls == [
        {
            "dataset": "GLBX.MDP3",
            "schema": "mbo",
            "symbols": "MES.v.0",
            "stype_in": "continuous",
            "start": "2026-07-14T13:30:00Z",
            "end": "2026-07-14T13:40:00Z",
        }
    ]


def test_time_range_over_sixty_minutes_is_blocked() -> None:
    with pytest.raises(ConnectorError, match="longer than 60 minutes"):
        build_request("2026-07-14T13:30:00Z", "2026-07-14T14:30:01Z")


def test_end_before_start_is_blocked() -> None:
    with pytest.raises(ConnectorError, match="after start"):
        build_request("2026-07-14T13:40:00Z", "2026-07-14T13:30:00Z")


@pytest.mark.parametrize("symbol", ["MES*", "MES?", "ALL_SYMBOLS"])
def test_wildcards_and_all_symbols_are_blocked(symbol: str) -> None:
    with pytest.raises(ConnectorError):
        build_request("2026-07-14T13:30:00Z", "2026-07-14T13:40:00Z", symbol)


@pytest.mark.parametrize("symbol", ["MES.v.0,MNQ.v.0", "MES.v.0 MNQ.v.0"])
def test_multiple_symbols_are_blocked(symbol: str) -> None:
    with pytest.raises(ConnectorError, match="Multiple symbols"):
        build_request("2026-07-14T13:30:00Z", "2026-07-14T13:40:00Z", symbol)


def test_cost_over_request_limit_is_blocked() -> None:
    assert is_request_allowed(Decimal("1.01"), config()) is False
    assert is_request_allowed(Decimal("1.00"), config()) is True


def test_receipt_must_match_and_be_fresh(tmp_path) -> None:
    req = request()
    saved_at = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    save_receipt(req, Decimal("0.25"), config(), root=tmp_path, now=saved_at)
    loaded = load_receipt(
        req,
        config(),
        root=tmp_path,
        now=datetime(2026, 7, 15, 12, 20, tzinfo=UTC),
    )
    assert loaded["cost"] == Decimal("0.25")
    with pytest.raises(ConnectorError, match="older than 30 minutes"):
        load_receipt(
            req,
            config(),
            root=tmp_path,
            now=datetime(2026, 7, 15, 12, 31, tzinfo=UTC),
        )


def test_data_file_selection_is_deterministic_and_explicit(tmp_path, capsys) -> None:
    first = tmp_path / "2026-07-13" / "a.dbn.zst"
    latest = tmp_path / "2026-07-14" / "z.dbn.zst"
    first.parent.mkdir(parents=True)
    latest.parent.mkdir(parents=True)
    first.write_bytes(b"a")
    latest.write_bytes(b"z")

    assert list_data_files(tmp_path) == [first.resolve(), latest.resolve()]
    assert resolve_data_file(None, root=tmp_path) == latest.resolve()
    assert resolve_data_file(None, latest=True, root=tmp_path) == latest.resolve()
    assert resolve_data_file(str(first), root=tmp_path) == first.resolve()

    announce_data_file_selection(latest, file_arg=None, latest=False, root=tmp_path)
    output = capsys.readouterr().out
    assert str(first) in output
    assert str(latest) in output
    assert "deterministic path order" in output


def test_file_and_latest_cannot_be_combined(tmp_path) -> None:
    selected = tmp_path / "2026-07-14" / "z.dbn.zst"
    selected.parent.mkdir(parents=True)
    selected.write_bytes(b"z")
    with pytest.raises(ConnectorError, match="either --file or --latest"):
        resolve_data_file(str(selected), latest=True, root=tmp_path)


def test_reference_schema_is_explicitly_supported() -> None:
    req = build_request(
        "2026-07-14T13:30:00Z",
        "2026-07-14T13:40:00Z",
        schema="mbp-10",
    )
    assert req.schema == "mbp-10"
