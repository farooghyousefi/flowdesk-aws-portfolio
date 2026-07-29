from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apps.connectors.databento.src.config import ConnectorConfig
from apps.market_service import authorization, planner, storage
from apps.market_service.tests.test_planner import MockClient, config, request_payload


@pytest.fixture()
def isolated_storage(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "APP_ROOT", tmp_path / "app")
    monkeypatch.setattr(storage, "JOURNAL_ROOT", tmp_path / "journal")
    monkeypatch.setattr(storage, "DERIVED_ROOT", tmp_path / "derived")
    monkeypatch.setattr(storage, "SQLITE_PATH", tmp_path / "app" / "test.sqlite3")
    monkeypatch.setattr(storage, "DUCKDB_PATH", tmp_path / "app" / "test.duckdb")
    storage.migrate()


def make_estimate(client: MockClient | None = None, **request_overrides: object) -> tuple[MockClient, dict, dict]:
    active_client = client or MockClient()
    result = planner.estimate_plan(
        request_payload(**request_overrides), client_factory=lambda _: active_client, config=config()
    )
    estimate = result["estimates"][0]
    return active_client, estimate, planner.review_purchase(estimate["estimateId"])


def payload(estimate: dict, review: dict, *, key: str = "auth-key") -> dict:
    return {
        "estimateId": estimate["estimateId"], "fingerprint": estimate["fingerprint"],
        "mode": estimate["mode"], "acceptedTerms": True,
        "confirmationPhrase": review["confirmationPhrase"],
        "displayedAuthorizationAmount": review["authorizationAmountDisplay"],
        "idempotencyKey": key,
    }


def table_count(name: str) -> int:
    with storage.connect() as database:
        return int(database.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])


def test_canonical_phrase_rounding_and_decimal_separator() -> None:
    assert authorization.canonical_amount("0.843423") == Decimal("0.84")
    assert authorization.canonical_amount("0,845") == Decimal("0.85")
    assert authorization.canonical_confirmation_phrase("0.843423") == "DOWNLOAD $0.84"
    assert authorization.canonical_confirmation_phrase("0.249") == "DOWNLOAD"


def test_dry_run_authorization_is_atomic_audited_and_never_remote(isolated_storage) -> None:
    client, estimate, review = make_estimate()
    result = authorization.authorize_download(
        {**payload(estimate, review), "displayedAuthorizationAmount": review["authorizationAmountDisplay"].replace(".", ",")},
        mode="dry_run",
    )
    assert result["authorization"]["state"] == "AUTHORIZED"
    assert result["authorization"]["executionMode"] == "dry_run"
    assert result["jobs"][0]["state"] == "AUTHORIZED"
    assert [event["eventType"] for event in result["timeline"]] == [
        "DOWNLOAD_AUTHORIZATION_REQUESTED", "DOWNLOAD_AUTHORIZED", "DOWNLOAD_JOB_CREATED",
    ]
    assert table_count("download_authorizations") == 1
    assert table_count("authorization_ledger") == 1
    assert table_count("dataset_jobs") == 1
    assert client.batch.submissions == []


def test_validation_rejections_are_structured_and_leave_no_partial_state(isolated_storage) -> None:
    _, estimate, review = make_estimate()
    cases = [
        ({"acceptedTerms": False}, "CONFIRMATION_REQUIRED"),
        ({"fingerprint": "stale"}, "ESTIMATE_FINGERPRINT_MISMATCH"),
        ({"mode": "economy"}, "MODE_MISMATCH"),
        ({"displayedAuthorizationAmount": "99.00"}, "AMOUNT_MISMATCH"),
        ({"confirmationPhrase": review["confirmationPhrase"].lower()}, "CONFIRMATION_PHRASE_MISMATCH"),
    ]
    for index, (change, expected) in enumerate(cases):
        with pytest.raises(authorization.AuthorizationError) as raised:
            authorization.authorize_download({**payload(estimate, review, key=f"bad-{index}"), **change}, mode="dry_run")
        assert raised.value.code == expected
    assert table_count("download_authorizations") == 0
    assert table_count("authorization_ledger") == 0
    assert table_count("dataset_jobs") == 0
    with storage.connect() as database:
        rejected = database.execute("SELECT COUNT(*) FROM audit_events WHERE event_type = 'DOWNLOAD_AUTHORIZATION_REJECTED'").fetchone()[0]
    assert rejected == len(cases)


def test_disabled_queue_fails_closed_without_creating_a_job(isolated_storage) -> None:
    _, estimate, review = make_estimate()
    with pytest.raises(authorization.AuthorizationError) as raised:
        authorization.authorize_download(payload(estimate, review), mode="disabled")
    assert raised.value.code == "QUEUE_UNAVAILABLE"
    assert raised.value.status_code == 503
    assert table_count("download_authorizations") == 0
    assert table_count("dataset_jobs") == 0


def test_expired_estimate_is_rejected_using_utc(isolated_storage) -> None:
    _, estimate, review = make_estimate()
    with storage.connect() as database:
        database.execute("UPDATE data_estimates SET expires_at = ? WHERE id = ?", ("2020-01-01T00:00:00Z", estimate["estimateId"]))
    with pytest.raises(authorization.AuthorizationError) as raised:
        authorization.authorize_download(payload(estimate, review), mode="dry_run", now=datetime.now(UTC))
    assert raised.value.code == "ESTIMATE_EXPIRED"
    assert table_count("download_authorizations") == 0
    with storage.connect() as database:
        assert database.execute("SELECT status FROM data_estimates WHERE id = ?", (estimate["estimateId"],)).fetchone()[0] == "EXPIRED"
        assert database.execute("SELECT COUNT(*) FROM audit_events WHERE event_type = 'DATA_ESTIMATE_EXPIRED'").fetchone()[0] == 1


def test_idempotency_key_and_estimate_uniqueness_return_the_same_authorization(isolated_storage) -> None:
    _, estimate, review = make_estimate()
    first = authorization.authorize_download(payload(estimate, review), mode="dry_run")
    same_key = authorization.authorize_download(payload(estimate, review), mode="dry_run")
    new_key = authorization.authorize_download(payload(estimate, review, key="second-key"), mode="dry_run")
    assert first["authorization"]["id"] == same_key["authorization"]["id"] == new_key["authorization"]["id"]
    assert same_key["idempotentReplay"] is True
    assert new_key["idempotentReplay"] is True
    assert table_count("dataset_jobs") == 1


def test_transaction_rolls_back_authorization_ledger_jobs_and_audit(isolated_storage) -> None:
    _, estimate, review = make_estimate()
    with pytest.raises(RuntimeError, match="injected"):
        authorization.authorize_download(payload(estimate, review), mode="dry_run", fault_after="jobs")
    assert table_count("download_authorizations") == 0
    assert table_count("authorization_ledger") == 0
    assert table_count("dataset_jobs") == 0
    with storage.connect() as database:
        created = database.execute("SELECT COUNT(*) FROM audit_events WHERE event_type = 'DOWNLOAD_JOB_CREATED'").fetchone()[0]
    assert created == 0


def test_reestimate_same_fingerprint_cannot_reset_authorized_lifecycle(isolated_storage) -> None:
    client, estimate, review = make_estimate()
    authorization.authorize_download(payload(estimate, review), mode="dry_run")
    before = storage.get_data_estimate(estimate["estimateId"])
    repeated = planner.estimate_plan(request_payload(), client_factory=lambda _: client, config=config())
    after = storage.get_data_estimate(estimate["estimateId"])
    assert repeated["estimates"][0]["estimateId"] == estimate["estimateId"]
    assert before["expires_at"] == after["expires_at"]
    assert after["status"] == "AUTHORIZED"
    assert after["job_id"]


def test_legacy_remote_job_is_reconciled_without_remote_call(isolated_storage) -> None:
    client, estimate, _ = make_estimate()
    storage.save_dataset_job({
        "id": "legacy-job", "estimate_id": estimate["estimateId"], "schema_name": "mbo",
        "remote_job_id": "remote-already-exists", "status": "SUBMITTED", "details": {"state": "queued"},
    })
    assert authorization.reconcile_existing_jobs() == 1
    recovered = authorization.get_authorization(estimate_id=estimate["estimateId"])
    assert recovered["authorization"]["state"] == "QUEUED"
    assert recovered["authorization"]["recovered"] is True
    assert recovered["jobs"][0]["remoteJobId"] == "remote-already-exists"
    assert client.batch.submissions == []
    assert authorization.reconcile_existing_jobs() == 0


def test_local_cancel_releases_reservation(isolated_storage) -> None:
    _, estimate, review = make_estimate()
    created = authorization.authorize_download(payload(estimate, review), mode="dry_run")
    cancelled = authorization.cancel_authorization_job(created["jobs"][0]["id"])
    assert cancelled["authorization"]["state"] == "CANCELLED"
    with storage.connect() as database:
        state = database.execute("SELECT state FROM authorization_ledger").fetchone()[0]
    assert state == "RELEASED"
    assert storage.tracked_costs()["authorizedToday"] == 0


def test_live_submission_calls_remote_once_after_local_commit(isolated_storage) -> None:
    client, estimate, review = make_estimate()
    created = authorization.authorize_download(payload(estimate, review), mode="live")
    submitted = authorization.submit_authorization(created["authorization"]["id"], client_factory=lambda _: client, config=config())
    replay = authorization.submit_authorization(created["authorization"]["id"], client_factory=lambda _: client, config=config())
    assert submitted["authorization"]["state"] == "QUEUED"
    assert replay["idempotentReplay"] is True
    assert len(client.batch.submissions) == 1


def test_budget_uses_reserved_authorizations_not_estimate_status(isolated_storage) -> None:
    _, first, first_review = make_estimate()
    authorization.authorize_download(payload(first, first_review), mode="dry_run")
    _, second, second_review = make_estimate(replayEnd="16:40")
    with storage.connect() as database:
        row = database.execute("SELECT metadata_json FROM data_estimates WHERE id = ?", (second["estimateId"],)).fetchone()
        metadata = json.loads(row["metadata_json"])
        metadata.update({"dailyLimitUsd": 0.01, "weeklyLimitUsd": 0.01, "monthlyLimitUsd": 0.01})
        database.execute("UPDATE data_estimates SET metadata_json = ?, allowed = 1 WHERE id = ?", (json.dumps(metadata), second["estimateId"]))
    with pytest.raises(authorization.AuthorizationError) as raised:
        authorization.authorize_download(payload(second, second_review, key="budget-key"), mode="dry_run")
    assert raised.value.code == "BUDGET_EXCEEDED"


def test_queue_failure_is_visible_and_retry_reuses_the_same_rows(isolated_storage) -> None:
    _, estimate, review = make_estimate()
    created = authorization.authorize_download(payload(estimate, review), mode="live")
    authorization_id = created["authorization"]["id"]
    failed = authorization.mark_queue_failed(authorization_id, "queue unavailable")
    assert failed["authorization"]["state"] == "FAILED"
    assert failed["authorization"]["error"]["code"] == "QUEUE_UNAVAILABLE"
    assert failed["authorization"]["error"]["retrySafe"] is True
    retried = authorization.prepare_queue_retry(authorization_id)
    assert retried["authorization"]["state"] == "AUTHORIZED"
    assert table_count("download_authorizations") == 1
    assert table_count("authorization_ledger") == 1
    assert table_count("dataset_jobs") == 1
