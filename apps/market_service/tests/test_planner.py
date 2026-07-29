from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.connectors.databento.src.config import ConnectorConfig, ConnectorError
from apps.market_service import authorization, planner, storage


class MockMetadata:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def list_schemas(self, dataset: str) -> list[str]:
        self.calls.append("list_schemas")
        return ["mbo", "trades", "ohlcv-1m"]

    def get_dataset_condition(self, dataset: str, start_date: str | None = None, end_date: str | None = None) -> list[dict]:
        self.calls.append("get_dataset_condition")
        return [{"condition": "available", "start_date": start_date, "end_date": end_date}]

    def get_dataset_range(self, dataset: str) -> dict:
        self.calls.append("get_dataset_range")
        return {"schema": {schema: {"start": "2019-01-01T00:00:00Z", "end": "2026-07-15T23:59:59Z"} for schema in ("mbo", "trades", "ohlcv-1m")}}

    def list_unit_prices(self, dataset: str) -> list[dict]:
        self.calls.append("list_unit_prices")
        return [{"mode": "historical", "unit_prices": {"mbo": 1.8, "trades": 28, "ohlcv-1m": 70}}]

    def get_record_count(self, **_: object) -> int:
        self.calls.append("get_record_count")
        return 100

    def get_billable_size(self, **_: object) -> int:
        self.calls.append("get_billable_size")
        return 1024

    def get_cost(self, **_: object) -> float:
        self.calls.append("get_cost")
        return 0.01


class MockSymbology:
    def resolve(self, **parameters: object) -> dict:
        symbol = str(parameters["symbols"])
        value = "42003239" if symbol == "MES.v.0" else "MESU6"
        return {"result": {symbol: [{"s": value, "d0": "2026-07-14", "d1": "2026-07-15"}]}}


class MockBatch:
    def __init__(self) -> None:
        self.submissions: list[dict] = []

    def submit_job(self, **parameters: object) -> dict:
        self.submissions.append(dict(parameters))
        return {"id": f"job-{len(self.submissions)}", "state": "queued"}


class MockClient:
    def __init__(self) -> None:
        self.metadata = MockMetadata()
        self.symbology = MockSymbology()
        self.batch = MockBatch()


@pytest.fixture()
def isolated_storage(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "APP_ROOT", tmp_path / "app")
    monkeypatch.setattr(storage, "JOURNAL_ROOT", tmp_path / "journal")
    monkeypatch.setattr(storage, "DERIVED_ROOT", tmp_path / "derived")
    monkeypatch.setattr(storage, "SQLITE_PATH", tmp_path / "app" / "test.sqlite3")
    monkeypatch.setattr(storage, "DUCKDB_PATH", tmp_path / "app" / "test.duckdb")
    storage.migrate()


def request_payload(**overrides: object) -> dict:
    return {
        "date": "2026-07-14", "timezone": "Europe/Berlin",
        "replayStart": "15:00", "replayEnd": "16:30", "contextMinutes": 30,
        **overrides,
    }


def config() -> ConnectorConfig:
    return ConnectorConfig("test-key", Decimal("1"), Decimal("5"), Decimal("15"), Decimal("40"))


def test_timezone_conversion_summer_winter_and_dst_gap() -> None:
    summer = planner.build_time_window(request_payload())
    assert summer["replay_start_utc"].hour == 13
    assert summer["replay_end_utc"].hour == 14
    assert summer["replay_end_utc"].minute == 30
    assert summer["context_start_utc"].hour == 12
    assert summer["context_start_utc"].minute == 30
    winter = planner.build_time_window(request_payload(date="2026-01-14"))
    assert winter["replay_start_utc"].hour == 14
    assert winter["replay_end_utc"].hour == 15
    assert winter["replay_end_utc"].minute == 30
    with pytest.raises(ConnectorError, match="daylight-saving time gap"):
        planner.build_time_window(request_payload(date="2026-03-29", replayStart="02:30", replayEnd="04:00"))


def test_estimate_uses_every_metadata_endpoint_and_never_downloads(isolated_storage) -> None:
    client = MockClient()
    result = planner.estimate_plan(request_payload(), client_factory=lambda _: client, config=config())
    assert result["downloadStarted"] is False
    assert [item["mode"] for item in result["estimates"]] == ["full_l3", "economy", "context"]
    assert result["contract"]["rawSymbol"] == "MESU6"
    for call in (
        "get_dataset_condition", "get_dataset_range", "list_unit_prices",
        "get_record_count", "get_billable_size", "get_cost",
    ):
        assert call in client.metadata.calls
    assert client.batch.submissions == []
    created = datetime.fromisoformat(result["estimates"][0]["createdAt"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(result["estimates"][0]["expiresAt"].replace("Z", "+00:00"))
    assert (expires - created).total_seconds() == 600


def test_fingerprint_changes_with_parameters_and_authorization_is_idempotent(isolated_storage) -> None:
    client = MockClient()
    first = planner.estimate_plan(request_payload(), client_factory=lambda _: client, config=config())
    changed = planner.estimate_plan(request_payload(replayEnd="17:50"), client_factory=lambda _: client, config=config())
    assert first["estimates"][0]["fingerprint"] != changed["estimates"][0]["fingerprint"]
    estimate = first["estimates"][0]
    review = planner.review_purchase(estimate["estimateId"])
    assert review["canSubmit"] is True
    payload = {
        "estimateId": estimate["estimateId"], "fingerprint": estimate["fingerprint"],
        "mode": estimate["mode"], "acceptedTerms": True,
        "confirmationPhrase": review["confirmationPhrase"],
        "displayedAuthorizationAmount": review["authorizationAmountDisplay"],
        "idempotencyKey": "planner-test-key",
    }
    with pytest.raises(authorization.AuthorizationError, match="exact case-sensitive"):
        authorization.authorize_download({**payload, "confirmationPhrase": "DOWNLOAD WRONG"}, mode="dry_run")
    submitted = authorization.authorize_download(payload, mode="dry_run")
    repeated = authorization.authorize_download(payload, mode="dry_run")
    assert submitted["authorization"]["state"] == "AUTHORIZED"
    assert repeated["idempotentReplay"] is True
    assert len(client.batch.submissions) == 0


def test_expired_estimate_is_blocked(isolated_storage) -> None:
    client = MockClient()
    result = planner.estimate_plan(request_payload(), client_factory=lambda _: client, config=config())
    estimate_id = result["estimates"][0]["estimateId"]
    with storage.connect() as database:
        database.execute("UPDATE data_estimates SET expires_at = ? WHERE id = ?", ("2020-01-01T00:00:00Z", estimate_id))
    review = planner.review_purchase(estimate_id)
    assert review["expired"] is True
    assert review["canSubmit"] is False


def test_canonical_request_plan_keeps_end_and_persists_without_metadata(isolated_storage) -> None:
    payload = request_payload(contextMinutes=45)
    preview = planner.preview_request_plan(payload)
    request_plan = preview["requestPlan"]
    assert request_plan == {
        "sessionDate": "2026-07-14", "timezone": "Europe/Berlin",
        "replayStartLocal": "15:00", "replayEndLocal": "16:30", "contextMinutes": 45,
        "replayStartUtc": "2026-07-14T13:00:00Z", "replayEndUtc": "2026-07-14T14:30:00Z",
        "requestStartUtc": "2026-07-14T12:15:00Z", "requestEndUtc": "2026-07-14T14:30:00Z",
    }
    assert preview["metadataRequested"] is False
    assert preview["downloadStarted"] is False
    assert storage.get_planner_state()["requestPlan"] == request_plan


def test_estimate_uses_same_canonical_preview_and_context_never_extends_end(isolated_storage) -> None:
    client = MockClient()
    payload = request_payload(contextMinutes=30)
    expected = planner.build_dataset_request_plan(payload)
    result = planner.estimate_plan(payload, client_factory=lambda _: client, config=config())
    assert result["input"]["replayEndLocal"].endswith("16:30+02:00")
    assert result["input"]["replayEndUtc"] == expected["replayEndUtc"]
    assert result["input"]["requestEndUtc"] == expected["requestEndUtc"]
    economy = next(item for item in result["estimates"] if item["mode"] == "economy")
    full_l3 = next(item for item in result["estimates"] if item["mode"] == "full_l3")
    assert economy["requestStartUtc"] == "2026-07-14T12:30:00Z"
    assert economy["requestEndUtc"] == "2026-07-14T14:30:00Z"
    assert full_l3["requestStartUtc"] == "2026-07-14T00:00:00Z"
    assert full_l3["requestEndUtc"] == "2026-07-14T14:30:00Z"


def test_authorization_cap_includes_effective_reserve_without_contradiction(isolated_storage) -> None:
    client = MockClient()
    client.metadata.get_cost = lambda **_: 0.945618
    result = planner.estimate_plan(request_payload(), client_factory=lambda _: client, config=config())
    estimate = result["estimates"][0]
    assert estimate["rawEstimatedCostUsd"] == pytest.approx(0.945618)
    assert estimate["targetSafetyReserveUsd"] == pytest.approx(0.094562)
    assert estimate["safetyReserveUsd"] == pytest.approx(0.054382)
    assert estimate["maximumAuthorizedUsd"] == pytest.approx(1.0)
    assert estimate["requestLimitUsd"] == pytest.approx(1.0)
    assert estimate["allowed"] is True
    assert estimate["authorizationPolicy"] == "maximum_authorized_includes_effective_reserve_and_is_checked_against_all_budgets"
