from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.connectors.databento.src.config import ConnectorConfig, ConnectorError
from apps.market_service import authorization, range_planner, storage


class MockMetadata:
    def __init__(self, cost: float = 0.10) -> None:
        self.cost = cost
        self.calls: list[str] = []

    def list_schemas(self, dataset: str) -> list[str]:
        self.calls.append("list_schemas")
        return ["mbo", "trades", "ohlcv-1m"]

    def get_dataset_condition(self, dataset: str, start_date: str | None = None, end_date: str | None = None) -> list[dict]:
        self.calls.append("get_dataset_condition")
        return [{"condition": "available", "start_date": start_date, "end_date": end_date}]

    def get_dataset_range(self, dataset: str) -> dict:
        self.calls.append("get_dataset_range")
        return {"schema": {"mbo": {"start": "2019-01-01T00:00:00Z", "end": "2026-07-15T23:59:59Z"}}}

    def list_unit_prices(self, dataset: str) -> list[dict]:
        self.calls.append("list_unit_prices")
        return [{"mode": "historical", "unit_prices": {"mbo": 1.8}}]

    def get_record_count(self, **_: object) -> int:
        self.calls.append("get_record_count")
        return 1_000_000

    def get_billable_size(self, **_: object) -> int:
        self.calls.append("get_billable_size")
        return 1024 * 1024 * 100

    def get_cost(self, **_: object) -> float:
        self.calls.append("get_cost")
        return self.cost


class MockSymbology:
    def resolve(self, **parameters: object) -> dict:
        symbol = str(parameters["symbols"])
        value = "42003239" if symbol == "MES.v.0" else "MESU6"
        return {"result": {symbol: [{"s": value, "d0": "2026-01-01", "d1": "2026-12-31"}]}}


class MockBatch:
    def __init__(self) -> None:
        self.submissions: list[dict] = []

    def submit_job(self, **parameters: object) -> dict:
        self.submissions.append(dict(parameters))
        return {"id": f"job-{len(self.submissions)}", "state": "queued"}


class MockClient:
    def __init__(self, cost: float = 0.10) -> None:
        self.metadata = MockMetadata(cost)
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


def config() -> ConnectorConfig:
    return ConnectorConfig("test-key", Decimal("1"), Decimal("125"), Decimal("125"), Decimal("125"))


def payload(**overrides: object) -> dict:
    return {
        "market": "MES", "dataset": "GLBX.MDP3", "symbol": "MES.v.0",
        "startDate": "2026-06-01", "endDate": "2026-06-05",
        "timezone": "Europe/Berlin", "replayStart": "00:00", "replayEnd": "22:00",
        "contextMinutes": 0, "budgetUsd": 125, "includeWeekends": False,
        **overrides,
    }


def table_count(name: str) -> int:
    with storage.connect() as database:
        return int(database.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])


def test_month_preview_creates_chronological_60_20_20_split() -> None:
    preview = range_planner.preview_range_plan(payload(startDate="2026-06-01", endDate="2026-06-30"))
    assert preview["sessionDays"] == 22
    assert preview["splitPlan"]["developmentSessions"] == 13
    assert preview["splitPlan"]["validationSessions"] == 4
    assert preview["splitPlan"]["lockedSessions"] == 5
    assert preview["splitPlan"]["assignments"][0]["splitName"] == "Development"
    assert preview["splitPlan"]["assignments"][-1] == {
        "sessionDate": "2026-06-30", "splitName": "Locked Test", "locked": True,
    }
    assert preview["metadataRequested"] is False
    assert preview["downloadStarted"] is False


def test_range_validation_rejects_future_and_more_than_six_months() -> None:
    current_utc_date = datetime.now(UTC).date().isoformat()
    with pytest.raises(ConnectorError, match="completed historical day"):
        range_planner.preview_range_plan(payload(startDate=current_utc_date, endDate=current_utc_date))
    with pytest.raises(ConnectorError, match="at most 184"):
        range_planner.preview_range_plan(payload(startDate="2025-12-01", endDate="2026-06-30"))


def test_exact_range_estimate_is_metadata_only_and_persists_daily_children(isolated_storage) -> None:
    client = MockClient(cost=0.10)
    progress: list[tuple[int, int, str]] = []
    result = range_planner.estimate_range_plan(
        payload(), client_factory=lambda _: client, config=config(),
        progress_callback=lambda completed, total, day: progress.append((completed, total, day)),
    )
    plan = result["rangePlan"]
    summary = plan["summary"]
    assert result["downloadStarted"] is False
    assert summary["estimatedSessionDays"] == 5
    assert summary["estimatedDaysCompleted"] == 5
    assert summary["downloadDays"] == 5
    assert summary["localReuseDays"] == 0
    assert summary["rawEstimatedCostUsd"] == pytest.approx(0.50)
    assert summary["maximumAuthorizedUsd"] == pytest.approx(0.55)
    assert summary["remainingBudgetAfterUsd"] == pytest.approx(124.45)
    assert summary["allowed"] is True
    assert summary["confirmationPhrase"] == "DOWNLOAD RANGE $0.55"
    assert len(summary["dailyEstimates"]) == 5
    assert table_count("data_estimates") == 5
    assert table_count("range_plans") == 1
    assert client.batch.submissions == []
    assert progress[0] == (0, 5, "2026-06-01")
    assert progress[-1] == (5, 5, "2026-06-05")


def test_range_authorization_is_atomic_idempotent_and_does_not_submit_remote(isolated_storage) -> None:
    client = MockClient(cost=0.10)
    plan = range_planner.estimate_range_plan(payload(), client_factory=lambda _: client, config=config())["rangePlan"]
    summary = plan["summary"]
    request = {
        "rangePlanId": plan["id"], "acceptedTerms": True,
        "confirmationPhrase": summary["confirmationPhrase"],
        "displayedAuthorizationAmount": f"{summary['maximumAuthorizedUsd']:.2f}",
        "idempotencyKey": "month-authorization-test",
    }
    first = range_planner.authorize_range_plan(request, mode="dry_run")
    repeated = range_planner.authorize_range_plan(request, mode="dry_run")
    assert first["executionMode"] == "dry_run"
    assert first["remoteSubmissionCreated"] is False
    assert first["chargeCreated"] is False
    assert len(first["authorizationIds"]) == 5
    assert len(first["jobIds"]) == 5
    assert repeated["idempotentReplay"] is True
    assert repeated["authorizationIds"] == first["authorizationIds"]
    assert table_count("download_authorizations") == 5
    assert table_count("authorization_ledger") == 5
    assert table_count("dataset_jobs") == 5
    assert client.batch.submissions == []


def test_wrong_aggregate_confirmation_creates_no_partial_rows(isolated_storage) -> None:
    client = MockClient(cost=0.10)
    plan = range_planner.estimate_range_plan(payload(), client_factory=lambda _: client, config=config())["rangePlan"]
    with pytest.raises(authorization.AuthorizationError) as raised:
        range_planner.authorize_range_plan({
            "rangePlanId": plan["id"], "acceptedTerms": True,
            "confirmationPhrase": "DOWNLOAD RANGE $999.00",
            "displayedAuthorizationAmount": f"{plan['summary']['maximumAuthorizedUsd']:.2f}",
            "idempotencyKey": "bad-key",
        }, mode="dry_run")
    assert raised.value.code == "CONFIRMATION_PHRASE_MISMATCH"
    assert table_count("download_authorizations") == 0
    assert table_count("authorization_ledger") == 0
    assert table_count("dataset_jobs") == 0


def test_ready_range_job_ids_only_returns_ready_children(isolated_storage) -> None:
    client = MockClient(cost=0.10)
    plan = range_planner.estimate_range_plan(payload(), client_factory=lambda _: client, config=config())["rangePlan"]
    summary = plan["summary"]
    created = range_planner.authorize_range_plan({
        "rangePlanId": plan["id"], "acceptedTerms": True,
        "confirmationPhrase": summary["confirmationPhrase"],
        "displayedAuthorizationAmount": f"{summary['maximumAuthorizedUsd']:.2f}",
        "idempotencyKey": "ready-test",
    }, mode="dry_run")
    with storage.connect() as database:
        database.execute("UPDATE dataset_jobs SET status='READY' WHERE id IN (?,?)", tuple(created["jobIds"][:2]))
    assert range_planner.ready_range_job_ids(plan["id"]) == created["jobIds"][:2]
