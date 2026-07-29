from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable

import databento as db

from apps.connectors.databento.src.config import ConnectorConfig, load_config, safe_error

from .storage import append_audit, connect, get_data_estimate, utc_now

AUTHORIZATION_STATES = {
    "IDLE", "VALIDATING", "SUBMITTING", "AUTHORIZED", "QUEUED", "DOWNLOADING",
    "IMPORTING", "VALIDATING_IMPORT", "COMPLETED", "EXPIRED", "CANCELLED", "FAILED",
}
ACTIVE_STATES = {"AUTHORIZED", "SUBMITTING", "QUEUED", "DOWNLOADING", "IMPORTING", "VALIDATING_IMPORT"}
TERMINAL_STATES = {"COMPLETED", "EXPIRED", "CANCELLED", "FAILED"}
ALLOWED_TRANSITIONS = {
    "AUTHORIZED": {"SUBMITTING", "CANCELLED", "EXPIRED", "FAILED"},
    "SUBMITTING": {"QUEUED", "FAILED"},
    "QUEUED": {"DOWNLOADING", "COMPLETED", "CANCELLED", "EXPIRED", "FAILED"},
    "DOWNLOADING": {"IMPORTING", "FAILED"},
    "IMPORTING": {"VALIDATING_IMPORT", "FAILED"},
    "VALIDATING_IMPORT": {"COMPLETED", "FAILED"},
}
ENCODING = "dbn"
COMPRESSION = "zstd"
SPLIT_DURATION = "day"


class AuthorizationError(ValueError):
    def __init__(self, code: str, message: str, next_action: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.next_action = next_action
        self.status_code = status_code

    def public(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "nextAction": self.next_action}


def execution_mode() -> str:
    mode = os.environ.get("DATABENTO_BATCH_EXECUTION_MODE", "disabled").strip().lower()
    return mode if mode in {"disabled", "dry_run", "live"} else "disabled"


def _decimal(value: Any, code: str = "INVALID_AMOUNT") -> Decimal:
    try:
        parsed = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, AttributeError) as exc:
        raise AuthorizationError(code, "The authorization amount is invalid.", "Reload the purchase review.") from exc
    if not parsed.is_finite() or parsed < 0:
        raise AuthorizationError(code, "The authorization amount is invalid.", "Reload the purchase review.")
    return parsed


def canonical_amount(value: Any) -> Decimal:
    return _decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def canonical_confirmation_phrase(value: Any) -> str:
    raw_amount = _decimal(value)
    amount = canonical_amount(raw_amount)
    return "DOWNLOAD" if raw_amount < Decimal("0.25") else f"DOWNLOAD ${amount:.2f}"


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _audit(database: Any, event_type: str, payload: dict[str, Any], now: str) -> None:
    database.execute(
        "INSERT INTO audit_events(plan_id, session_id, event_type, payload_json, created_at) VALUES(NULL, NULL, ?, ?, ?)",
        (event_type, json.dumps(payload, default=str), now),
    )


def _audit_rejection(estimate_id: str, idempotency_key: str, error: AuthorizationError) -> None:
    append_audit("DOWNLOAD_AUTHORIZATION_REJECTED", {
        "estimateId": estimate_id,
        "idempotencyKey": idempotency_key,
        "code": error.code,
        "nextAction": error.next_action,
        "secretValuesLogged": False,
    })


def _mark_estimate_expired(estimate_id: str) -> None:
    with connect() as database:
        database.execute("BEGIN IMMEDIATE")
        estimate = database.execute("SELECT status, request_fingerprint, mode FROM data_estimates WHERE id = ?", (estimate_id,)).fetchone()
        if not estimate or estimate["status"] == "EXPIRED":
            return
        if database.execute("SELECT 1 FROM download_authorizations WHERE estimate_id = ?", (estimate_id,)).fetchone():
            return
        now = utc_now()
        database.execute("UPDATE data_estimates SET status = 'EXPIRED' WHERE id = ?", (estimate_id,))
        _audit(database, "DATA_ESTIMATE_EXPIRED", {
            "estimateId": estimate_id, "fingerprint": estimate["request_fingerprint"],
            "mode": estimate["mode"], "status": "EXPIRED",
        }, now)


def _decode_authorization(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"], "estimateId": row["estimate_id"], "idempotencyKey": row["idempotency_key"],
        "fingerprint": row["request_fingerprint"], "mode": row["mode"], "state": row["state"],
        "acceptedTerms": bool(row["accepted_terms"]), "authorizationAmount": float(row["authorization_amount"]),
        "authorizationAmountDisplay": row["displayed_authorization_amount"], "executionMode": row["execution_mode"],
        "error": {"code": row["error_code"], "message": row["error_message"], "retrySafe": bool(row["retry_safe"])} if row["error_code"] else None,
        "recovered": bool(row["recovered"]), "createdAt": row["created_at"], "updatedAt": row["updated_at"],
        "authorizedAt": row["authorized_at"],
    }


def _decode_job(row: Any) -> dict[str, Any]:
    details = json.loads(row["details_json"] or "{}")
    raw_state = str(row["status"])
    state = {
        "SUBMITTED": "QUEUED",
        "DOWNLOADED": "IMPORTING", "IMPORTED": "COMPLETED", "VALIDATED": "COMPLETED",
    }.get(raw_state, raw_state)
    poll_error = details.get("pollError")
    row_error_code = row["error_code"]
    row_error_message = row["error_message"]
    error = None
    if row_error_code:
        error = {"code": row_error_code, "message": row_error_message, "retrySafe": False}
    elif poll_error:
        error = {"code": "REMOTE_STATUS_FAILED", "message": str(poll_error), "retrySafe": True}
    return {
        "id": row["id"], "authorizationId": row["authorization_id"], "estimateId": row["estimate_id"],
        "schema": row["schema_name"], "remoteJobId": row["remote_job_id"], "state": state,
        "rawState": raw_state, "remoteState": details.get("remoteState", raw_state),
        "readyForDownload": raw_state == "READY",
        "progress": float(row["progress"] or 0), "error": error,
        "actualCostUsd": float(row["actual_cost"]) if row["actual_cost"] is not None else None,
        "downloadBytes": row["download_bytes"], "executionMode": details.get("executionMode", "legacy"),
        "recovered": bool(details.get("recovered")), "createdAt": row["created_at"], "updatedAt": row["updated_at"],
        "downloadedAt": row["downloaded_at"], "chargedAt": row["charged_at"],
    }


def _timeline(database: Any, authorization_id: str) -> list[dict[str, Any]]:
    rows = database.execute(
        """SELECT id, event_type, payload_json, created_at FROM audit_events
           WHERE json_extract(payload_json, '$.authorizationId') = ? ORDER BY id""",
        (authorization_id,),
    ).fetchall()
    return [{"id": row["id"], "eventType": row["event_type"], "payload": json.loads(row["payload_json"]), "createdAt": row["created_at"]} for row in rows]


def _authorization_result(database: Any, authorization_id: str, *, idempotent: bool = False) -> dict[str, Any]:
    row = database.execute("SELECT * FROM download_authorizations WHERE id = ?", (authorization_id,)).fetchone()
    jobs = database.execute("SELECT * FROM dataset_jobs WHERE authorization_id = ? ORDER BY created_at", (authorization_id,)).fetchall()
    return {
        "authorization": _decode_authorization(row), "jobs": [_decode_job(job) for job in jobs],
        "timeline": _timeline(database, authorization_id), "idempotentReplay": idempotent,
        "chargeCreated": False, "remoteSubmissionCreated": False,
    }


def _reservation_totals(database: Any, current: datetime) -> tuple[Decimal, Decimal, Decimal]:
    day = current.replace(hour=0, minute=0, second=0, microsecond=0)
    week = day - timedelta(days=day.weekday())
    month = day.replace(day=1)
    rows = database.execute(
        """SELECT l.amount, a.created_at FROM authorization_ledger l
           JOIN download_authorizations a ON a.id = l.authorization_id WHERE l.state = 'RESERVED'"""
    ).fetchall()
    totals = [Decimal("0"), Decimal("0"), Decimal("0")]
    for row in rows:
        created = _parse_utc(row["created_at"])
        amount = _decimal(row["amount"])
        if created >= day:
            totals[0] += amount
        if created >= week:
            totals[1] += amount
        if created >= month:
            totals[2] += amount
    return tuple(totals)  # type: ignore[return-value]


def _validate_budget(database: Any, estimate: Any, amount: Decimal, current: datetime) -> None:
    metadata = json.loads(estimate["metadata_json"] or "{}")
    limits = (
        canonical_amount(metadata.get("dailyLimitUsd", 0)),
        canonical_amount(metadata.get("weeklyLimitUsd", 0)),
        canonical_amount(metadata.get("monthlyLimitUsd", 0)),
    )
    request_limit = canonical_amount(metadata.get("requestLimitUsd", 0))
    if amount > request_limit:
        raise AuthorizationError("REQUEST_LIMIT_EXCEEDED", "The maximum authorization exceeds the request limit.", "Create a smaller estimate.")
    totals = _reservation_totals(database, current)
    for name, used, limit in zip(("daily", "weekly", "monthly"), totals, limits, strict=True):
        if used + amount > limit:
            raise AuthorizationError("BUDGET_EXCEEDED", f"The {name} authorization budget would be exceeded.", "Reduce the request or release another reservation.")


def reconcile_existing_jobs() -> int:
    """Repair legacy job/estimate splits locally. This function never contacts Databento."""
    repaired = 0
    with connect() as database:
        database.execute("BEGIN IMMEDIATE")
        rows = database.execute(
            """SELECT j.*, e.request_fingerprint, e.mode, e.metadata_json
               FROM dataset_jobs j JOIN data_estimates e ON e.id = j.estimate_id
               WHERE j.authorization_id IS NULL AND j.remote_job_id IS NOT NULL"""
        ).fetchall()
        for job in rows:
            now = utc_now()
            authorization_id = str(uuid.uuid4())
            metadata = json.loads(job["metadata_json"] or "{}")
            raw_amount = _decimal(metadata.get("maximumAuthorizedUsd", 0))
            amount = canonical_amount(raw_amount)
            state = "COMPLETED" if job["status"] in {"IMPORTED", "VALIDATED", "COMPLETED"} else "QUEUED"
            phrase = canonical_confirmation_phrase(raw_amount)
            database.execute(
                """INSERT INTO download_authorizations(
                   id, estimate_id, idempotency_key, request_fingerprint, mode, state, accepted_terms,
                   confirmation_phrase, authorization_amount, displayed_authorization_amount,
                   execution_mode, retry_safe, recovered, created_at, updated_at, authorized_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (authorization_id, job["estimate_id"], f"recovered:{job['id']}", job["request_fingerprint"], job["mode"], state,
                 0, phrase, f"{amount:.2f}", f"{amount:.2f}", "recovered", 0, 1, job["created_at"], now, job["created_at"]),
            )
            database.execute(
                "INSERT INTO authorization_ledger(id, authorization_id, estimate_id, amount, state, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), authorization_id, job["estimate_id"], f"{amount:.2f}", "RESERVED", job["created_at"], now),
            )
            details = json.loads(job["details_json"] or "{}")
            details.update({"recovered": True, "executionMode": "recovered", "confirmationRecordAvailable": False})
            database.execute(
                "UPDATE dataset_jobs SET authorization_id = ?, status = ?, details_json = ?, updated_at = ? WHERE id = ?",
                (authorization_id, state, json.dumps(details, default=str), now, job["id"]),
            )
            database.execute(
                "UPDATE data_estimates SET status = ?, job_id = ? WHERE id = ?",
                (state, job["remote_job_id"], job["estimate_id"]),
            )
            common = {"authorizationId": authorization_id, "estimateId": job["estimate_id"], "jobId": job["id"], "recovered": True}
            _audit(database, "DOWNLOAD_AUTHORIZED", {**common, "acceptedTermsRecordAvailable": False}, now)
            _audit(database, "DOWNLOAD_JOB_CREATED", {**common, "remoteJobAlreadyExisted": True}, now)
            repaired += 1
    return repaired


def authorize_download(payload: dict[str, Any], *, mode: str | None = None, now: datetime | None = None, fault_after: str | None = None) -> dict[str, Any]:
    estimate_id = str(payload.get("estimateId") or "")
    idempotency_key = str(payload.get("idempotencyKey") or "").strip()
    current = (now or datetime.now(UTC)).astimezone(UTC)
    runtime_mode = mode or execution_mode()
    persistence_stage = "AUTHORIZATION"
    try:
        if not estimate_id or not idempotency_key:
            raise AuthorizationError("INVALID_REQUEST", "Estimate ID and idempotency key are required.", "Reload the purchase review.")
        with connect() as database:
            database.execute("BEGIN IMMEDIATE")
            replay = database.execute("SELECT id FROM download_authorizations WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if replay:
                return _authorization_result(database, replay["id"], idempotent=True)
            estimate = database.execute("SELECT * FROM data_estimates WHERE id = ?", (estimate_id,)).fetchone()
            if not estimate:
                raise AuthorizationError("ESTIMATE_NOT_FOUND", "The estimate no longer exists.", "Create a fresh estimate.", status_code=404)
            existing = database.execute("SELECT id FROM download_authorizations WHERE estimate_id = ?", (estimate_id,)).fetchone()
            if str(payload.get("fingerprint") or "") != estimate["request_fingerprint"]:
                raise AuthorizationError("ESTIMATE_FINGERPRINT_MISMATCH", "The estimate fingerprint changed.", "Reload and review the current estimate.")
            requested_mode = str(payload.get("mode") or "").strip().lower()
            if requested_mode != estimate["mode"]:
                raise AuthorizationError("MODE_MISMATCH", "The selected data mode changed.", "Reload and review the current estimate.")
            if not bool(payload.get("acceptedTerms")):
                raise AuthorizationError("CONFIRMATION_REQUIRED", "Cost acknowledgement is required.", "Accept the cost terms.")
            metadata = json.loads(estimate["metadata_json"] or "{}")
            raw_amount = _decimal(metadata.get("maximumAuthorizedUsd", 0))
            amount = canonical_amount(raw_amount)
            displayed = canonical_amount(payload.get("displayedAuthorizationAmount"))
            if displayed != amount:
                raise AuthorizationError("AMOUNT_MISMATCH", "The displayed authorization amount is stale.", "Reload the purchase review.")
            phrase = canonical_confirmation_phrase(raw_amount)
            if str(payload.get("confirmationPhrase") or "").strip() != phrase:
                raise AuthorizationError("CONFIRMATION_PHRASE_MISMATCH", "The exact case-sensitive confirmation phrase is required.", "Enter the phrase shown in the dialog exactly.")
            if existing:
                return _authorization_result(database, existing["id"], idempotent=True)
            if current >= _parse_utc(estimate["expires_at"]):
                database.execute("UPDATE data_estimates SET status = 'EXPIRED' WHERE id = ?", (estimate_id,))
                raise AuthorizationError("ESTIMATE_EXPIRED", "The estimate has expired.", "Create a fresh estimate.", status_code=409)
            if bool(estimate["local_reuse"]):
                raise AuthorizationError("DUPLICATE_DATASET", "An identical complete local data set already exists.", "Use the existing local data set.", status_code=409)
            if not bool(estimate["allowed"]):
                raise AuthorizationError("AUTHORIZATION_BLOCKED", "This estimate is not eligible for a paid download.", "Review the estimate warnings or use the local data.")
            if runtime_mode == "disabled":
                raise AuthorizationError("QUEUE_UNAVAILABLE", "Batch submission is disabled on this service.", "Set DATABENTO_BATCH_EXECUTION_MODE=dry_run for QA or live for a controlled order.", status_code=503)
            unlinked_job = database.execute(
                "SELECT id FROM dataset_jobs WHERE estimate_id = ? AND authorization_id IS NULL LIMIT 1", (estimate_id,)
            ).fetchone()
            if unlinked_job:
                raise AuthorizationError("JOB_ALREADY_EXISTS", "A local job already exists for this estimate.", "Reload Data Planner and inspect the existing job.", status_code=409)
            _validate_budget(database, estimate, amount, current)
            timestamp = current.isoformat().replace("+00:00", "Z")
            authorization_id = str(uuid.uuid4())
            _audit(database, "DOWNLOAD_AUTHORIZATION_REQUESTED", {"authorizationId": authorization_id, "estimateId": estimate_id, "idempotencyKey": idempotency_key}, timestamp)
            persistence_stage = "AUTHORIZATION"
            database.execute(
                """INSERT INTO download_authorizations(
                   id, estimate_id, idempotency_key, request_fingerprint, mode, state, accepted_terms,
                   confirmation_phrase, authorization_amount, displayed_authorization_amount, execution_mode,
                   retry_safe, recovered, created_at, updated_at, authorized_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (authorization_id, estimate_id, idempotency_key, estimate["request_fingerprint"], estimate["mode"], "AUTHORIZED", 1,
                 phrase, f"{amount:.2f}", f"{displayed:.2f}", runtime_mode, 0, 0, timestamp, timestamp, timestamp),
            )
            if fault_after == "authorization":
                raise RuntimeError("injected authorization transaction failure")
            persistence_stage = "LEDGER"
            database.execute(
                "INSERT INTO authorization_ledger(id, authorization_id, estimate_id, amount, state, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), authorization_id, estimate_id, f"{amount:.2f}", "RESERVED", timestamp, timestamp),
            )
            _audit(database, "DOWNLOAD_AUTHORIZED", {"authorizationId": authorization_id, "estimateId": estimate_id, "amount": f"{amount:.2f}", "executionMode": runtime_mode}, timestamp)
            job_ids: list[str] = []
            persistence_stage = "JOB"
            for schema in json.loads(estimate["schemas_json"]):
                job_id = str(uuid.uuid4())
                job_ids.append(job_id)
                database.execute(
                    """INSERT INTO dataset_jobs(
                       id, estimate_id, schema_name, status, details_json, created_at, updated_at,
                       authorization_id, progress
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (job_id, estimate_id, schema, "AUTHORIZED", json.dumps({"executionMode": runtime_mode, "remoteSubmitted": False}), timestamp, timestamp, authorization_id, 0),
                )
                _audit(database, "DOWNLOAD_JOB_CREATED", {"authorizationId": authorization_id, "estimateId": estimate_id, "jobId": job_id, "schema": schema}, timestamp)
            database.execute("UPDATE data_estimates SET status = 'AUTHORIZED', job_id = ? WHERE id = ?", (",".join(job_ids), estimate_id))
            if fault_after == "jobs":
                raise RuntimeError("injected job transaction failure")
            return _authorization_result(database, authorization_id)
    except AuthorizationError as exc:
        if exc.code == "ESTIMATE_EXPIRED":
            _mark_estimate_expired(estimate_id)
        _audit_rejection(estimate_id, idempotency_key, exc)
        raise
    except sqlite3.Error as exc:
        codes = {
            "AUTHORIZATION": ("AUTHORIZATION_PERSIST_FAILED", "The authorization could not be saved."),
            "LEDGER": ("LEDGER_RESERVATION_FAILED", "The budget reservation could not be saved."),
            "JOB": ("JOB_CREATION_FAILED", "The local download job could not be saved."),
        }
        code, message = codes[persistence_stage]
        error = AuthorizationError(code, message + " No order was created.", "Check local storage health, then retry with the same idempotency key.", status_code=500)
        try:
            _audit_rejection(estimate_id, idempotency_key, error)
        except sqlite3.Error:
            pass
        raise error from exc


def get_authorization(*, authorization_id: str | None = None, estimate_id: str | None = None) -> dict[str, Any] | None:
    reconcile_existing_jobs()
    with connect() as database:
        if authorization_id:
            row = database.execute("SELECT id FROM download_authorizations WHERE id = ?", (authorization_id,)).fetchone()
        else:
            row = database.execute("SELECT id FROM download_authorizations WHERE estimate_id = ?", (estimate_id,)).fetchone()
        return _authorization_result(database, row["id"]) if row else None


def list_download_jobs() -> list[dict[str, Any]]:
    reconcile_existing_jobs()
    with connect() as database:
        rows = database.execute("SELECT * FROM dataset_jobs ORDER BY created_at DESC").fetchall()
        jobs = []
        for row in rows:
            item = _decode_job(row)
            item["timeline"] = _timeline(database, row["authorization_id"]) if row["authorization_id"] else []
            estimate = database.execute("SELECT mode, raw_symbol, start_utc, end_utc FROM data_estimates WHERE id = ?", (row["estimate_id"],)).fetchone()
            if estimate:
                item.update({"mode": estimate["mode"], "rawSymbol": estimate["raw_symbol"], "requestStartUtc": estimate["start_utc"], "requestEndUtc": estimate["end_utc"]})
            if row["authorization_id"]:
                auth = database.execute(
                    "SELECT authorization_amount, retry_safe FROM download_authorizations WHERE id = ?", (row["authorization_id"],)
                ).fetchone()
                if auth:
                    item["authorizationAmountUsd"] = float(auth["authorization_amount"])
                    item["retrySafe"] = bool(auth["retry_safe"])
            jobs.append(item)
        return jobs


def submit_authorization(authorization_id: str, *, client_factory: Callable[[str], Any] | None = None, config: ConnectorConfig | None = None) -> dict[str, Any]:
    with connect() as database:
        database.execute("BEGIN IMMEDIATE")
        authorization = database.execute("SELECT * FROM download_authorizations WHERE id = ?", (authorization_id,)).fetchone()
        if not authorization:
            raise AuthorizationError("AUTHORIZATION_NOT_FOUND", "Authorization not found.", "Reload Data Planner.", status_code=404)
        if authorization["execution_mode"] != "live":
            return _authorization_result(database, authorization_id, idempotent=True)
        if authorization["state"] != "AUTHORIZED":
            return _authorization_result(database, authorization_id, idempotent=True)
        now = utc_now()
        database.execute("UPDATE download_authorizations SET state = 'SUBMITTING', updated_at = ? WHERE id = ?", (now, authorization_id))
        database.execute("UPDATE dataset_jobs SET status = 'SUBMITTING', updated_at = ? WHERE authorization_id = ?", (now, authorization_id))
        _audit(database, "DOWNLOAD_SUBMITTING", {"authorizationId": authorization_id, "estimateId": authorization["estimate_id"]}, now)
    estimate = get_data_estimate(authorization["estimate_id"])
    active_config = config or load_config()
    client = (client_factory or db.Historical)(active_config.api_key)
    try:
        with connect() as database:
            jobs = database.execute("SELECT * FROM dataset_jobs WHERE authorization_id = ? ORDER BY created_at", (authorization_id,)).fetchall()
        for job in jobs:
            response = client.batch.submit_job(
                dataset=estimate["dataset"], symbols=estimate["instrument_id"], schema=job["schema_name"],
                start=estimate["start_utc"], end=estimate["end_utc"], encoding=ENCODING,
                compression=COMPRESSION, split_duration=SPLIT_DURATION, delivery="download",
                stype_in="instrument_id", stype_out="instrument_id",
            )
            remote_id = str(response.get("id") or response.get("job_id") or "")
            if not remote_id:
                raise RuntimeError("Databento did not return a batch job ID.")
            now = utc_now()
            with connect() as database:
                database.execute("BEGIN IMMEDIATE")
                database.execute(
                    "UPDATE dataset_jobs SET remote_job_id = ?, status = 'QUEUED', details_json = ?, updated_at = ? WHERE id = ?",
                    (remote_id, json.dumps({**response, "executionMode": "live", "remoteSubmitted": True}, default=str), now, job["id"]),
                )
                _audit(database, "DOWNLOAD_REMOTE_JOB_QUEUED", {"authorizationId": authorization_id, "estimateId": authorization["estimate_id"], "jobId": job["id"]}, now)
        with connect() as database:
            database.execute("BEGIN IMMEDIATE")
            now = utc_now()
            database.execute("UPDATE download_authorizations SET state = 'QUEUED', updated_at = ? WHERE id = ?", (now, authorization_id))
            database.execute("UPDATE data_estimates SET status = 'QUEUED' WHERE id = ?", (authorization["estimate_id"],))
            return _authorization_result(database, authorization_id)
    except Exception as exc:
        message = safe_error(exc, (active_config.api_key,))
        error_code = "NETWORK_ERROR" if isinstance(exc, (ConnectionError, OSError, TimeoutError)) else "DATABENTO_REJECTED"
        with connect() as database:
            database.execute("BEGIN IMMEDIATE")
            now = utc_now()
            database.execute(
                "UPDATE download_authorizations SET state = 'FAILED', error_code = ?, error_message = ?, retry_safe = 0, updated_at = ? WHERE id = ?",
                (error_code, message, now, authorization_id),
            )
            database.execute(
                "UPDATE dataset_jobs SET status = 'FAILED', error_code = ?, error_message = ?, updated_at = ? WHERE authorization_id = ? AND remote_job_id IS NULL",
                (error_code, message, now, authorization_id),
            )
            database.execute("UPDATE data_estimates SET status = 'FAILED' WHERE id = ?", (authorization["estimate_id"],))
            _audit(database, "DOWNLOAD_FAILED", {"authorizationId": authorization_id, "estimateId": authorization["estimate_id"], "code": error_code, "retrySafe": False}, now)
        raise AuthorizationError(error_code, message, "Check Databento for an existing job before any retry.", status_code=502) from exc


def mark_queue_failed(authorization_id: str, message: str) -> dict[str, Any]:
    with connect() as database:
        database.execute("BEGIN IMMEDIATE")
        authorization = database.execute("SELECT * FROM download_authorizations WHERE id = ?", (authorization_id,)).fetchone()
        if not authorization:
            raise AuthorizationError("AUTHORIZATION_NOT_FOUND", "Authorization not found.", "Reload Data Planner.", status_code=404)
        now = utc_now()
        database.execute(
            "UPDATE download_authorizations SET state = 'FAILED', error_code = 'QUEUE_UNAVAILABLE', error_message = ?, retry_safe = 1, updated_at = ? WHERE id = ?",
            (message, now, authorization_id),
        )
        database.execute(
            "UPDATE dataset_jobs SET status = 'FAILED', error_code = 'QUEUE_UNAVAILABLE', error_message = ?, updated_at = ? WHERE authorization_id = ? AND remote_job_id IS NULL",
            (message, now, authorization_id),
        )
        database.execute("UPDATE data_estimates SET status = 'FAILED' WHERE id = ?", (authorization["estimate_id"],))
        _audit(database, "DOWNLOAD_QUEUE_FAILED", {
            "authorizationId": authorization_id, "estimateId": authorization["estimate_id"],
            "code": "QUEUE_UNAVAILABLE", "retrySafe": True,
        }, now)
        return _authorization_result(database, authorization_id)


def prepare_queue_retry(authorization_id: str) -> dict[str, Any]:
    with connect() as database:
        database.execute("BEGIN IMMEDIATE")
        authorization = database.execute("SELECT * FROM download_authorizations WHERE id = ?", (authorization_id,)).fetchone()
        if not authorization:
            raise AuthorizationError("AUTHORIZATION_NOT_FOUND", "Authorization not found.", "Reload Data Planner.", status_code=404)
        if authorization["state"] != "FAILED" or not bool(authorization["retry_safe"]) or authorization["error_code"] != "QUEUE_UNAVAILABLE":
            raise AuthorizationError("RETRY_NOT_SAFE", "This authorization cannot be retried automatically.", "Inspect Databento for an existing job before any retry.", status_code=409)
        if database.execute("SELECT 1 FROM dataset_jobs WHERE authorization_id = ? AND remote_job_id IS NOT NULL", (authorization_id,)).fetchone():
            raise AuthorizationError("JOB_ALREADY_EXISTS", "A remote job already exists.", "Refresh the existing job instead of retrying.", status_code=409)
        now = utc_now()
        database.execute(
            "UPDATE download_authorizations SET state = 'AUTHORIZED', error_code = NULL, error_message = NULL, retry_safe = 0, updated_at = ? WHERE id = ?",
            (now, authorization_id),
        )
        database.execute(
            "UPDATE dataset_jobs SET status = 'AUTHORIZED', error_code = NULL, error_message = NULL, updated_at = ? WHERE authorization_id = ?",
            (now, authorization_id),
        )
        database.execute("UPDATE data_estimates SET status = 'AUTHORIZED' WHERE id = ?", (authorization["estimate_id"],))
        _audit(database, "DOWNLOAD_AUTHORIZATION_RETRY_QUEUED", {"authorizationId": authorization_id, "estimateId": authorization["estimate_id"]}, now)
        return _authorization_result(database, authorization_id)


def transition_download_job(
    job_id: str,
    state: str,
    event_type: str,
    *,
    error_code: str | None = None,
    error_message: str | None = None,
    progress: float | None = None,
    actual_cost: Decimal | str | float | None = None,
    download_bytes: int | None = None,
    downloaded_at: str | None = None,
    charged_at: str | None = None,
) -> dict[str, Any]:
    if state not in AUTHORIZATION_STATES:
        raise AuthorizationError("UNKNOWN_AUTHORIZATION_STATE", "Unknown authorization state.", "Refresh the job status.")
    with connect() as database:
        database.execute("BEGIN IMMEDIATE")
        job = database.execute("SELECT * FROM dataset_jobs WHERE id = ?", (job_id,)).fetchone()
        if not job or not job["authorization_id"]:
            raise AuthorizationError("JOB_NOT_FOUND", "Authorized download job not found.", "Reload Data Planner.", status_code=404)
        authorization = database.execute("SELECT * FROM download_authorizations WHERE id = ?", (job["authorization_id"],)).fetchone()
        current = str(authorization["state"])
        if state != current and state not in ALLOWED_TRANSITIONS.get(current, set()):
            raise AuthorizationError("UNKNOWN_AUTHORIZATION_STATE", f"Unsafe state transition {current} to {state}.", "Refresh the job status before retrying.", status_code=409)
        now = utc_now()
        retry_safe = 1 if error_code == "QUEUE_UNAVAILABLE" else 0
        database.execute(
            """UPDATE download_authorizations SET state = ?, error_code = ?, error_message = ?,
               retry_safe = ?, updated_at = ? WHERE id = ?""",
            (state, error_code, error_message, retry_safe, now, job["authorization_id"]),
        )
        fields = ["status = ?", "error_code = ?", "error_message = ?", "updated_at = ?"]
        values: list[Any] = [state, error_code, error_message, now]
        for column, value in (
            ("progress", progress), ("actual_cost", str(actual_cost) if actual_cost is not None else None),
            ("download_bytes", download_bytes), ("downloaded_at", downloaded_at), ("charged_at", charged_at),
        ):
            if value is not None:
                fields.append(f"{column} = ?")
                values.append(value)
        values.append(job_id)
        database.execute(f"UPDATE dataset_jobs SET {', '.join(fields)} WHERE id = ?", values)
        estimate_status = "IMPORTED" if state == "COMPLETED" else state
        estimate_values: list[Any] = [estimate_status]
        estimate_fields = ["status = ?"]
        if download_bytes is not None:
            estimate_fields.append("actual_local_size = ?")
            estimate_values.append(download_bytes)
        if downloaded_at is not None:
            estimate_fields.append("downloaded_at = ?")
            estimate_values.append(downloaded_at)
        estimate_values.append(job["estimate_id"])
        database.execute(f"UPDATE data_estimates SET {', '.join(estimate_fields)} WHERE id = ?", estimate_values)
        _audit(database, event_type, {
            "authorizationId": job["authorization_id"], "estimateId": job["estimate_id"],
            "jobId": job_id, "status": state, "errorCode": error_code,
        }, now)
        return _authorization_result(database, job["authorization_id"])



def prepare_existing_remote_download_retry(job_id: str) -> dict[str, Any]:
    """Re-open a failed local download/import without creating a new remote order.

    This path is intentionally limited to jobs that already have a Databento
    remote ID. It never calls ``submit_job`` and never creates a new ledger
    reservation.
    """
    with connect() as database:
        database.execute("BEGIN IMMEDIATE")
        job = database.execute("SELECT * FROM dataset_jobs WHERE id = ?", (job_id,)).fetchone()
        if not job or not job["authorization_id"]:
            raise AuthorizationError(
                "JOB_NOT_FOUND",
                "Authorized download job not found.",
                "Reload Data Planner.",
                status_code=404,
            )
        if not job["remote_job_id"]:
            raise AuthorizationError(
                "REMOTE_JOB_ID_MISSING",
                "This job has no existing Databento job ID.",
                "Do not create a replacement order. Review the authorization audit.",
                status_code=422,
            )
        authorization = database.execute(
            "SELECT * FROM download_authorizations WHERE id = ?",
            (job["authorization_id"],),
        ).fetchone()
        if not authorization:
            raise AuthorizationError(
                "AUTHORIZATION_NOT_FOUND",
                "Authorization not found.",
                "Reload Data Planner.",
                status_code=404,
            )
        if authorization["state"] != "FAILED":
            return _authorization_result(database, authorization["id"], idempotent=True)
        if job["error_code"] not in {"IMPORT_FAILED", "NETWORK_ERROR", "DOWNLOAD_FAILED"}:
            raise AuthorizationError(
                "RETRY_NOT_SAFE",
                "This failed job cannot be retried automatically.",
                "Inspect the existing Databento job before retrying.",
                status_code=409,
            )
        now = utc_now()
        database.execute(
            """UPDATE download_authorizations
               SET state = 'QUEUED', error_code = NULL, error_message = NULL,
                   retry_safe = 0, updated_at = ?
               WHERE id = ?""",
            (now, authorization["id"]),
        )
        database.execute(
            """UPDATE dataset_jobs
               SET status = 'READY', error_code = NULL, error_message = NULL, updated_at = ?
               WHERE id = ?""",
            (now, job_id),
        )
        database.execute(
            "UPDATE data_estimates SET status = 'QUEUED' WHERE id = ?",
            (job["estimate_id"],),
        )
        _audit(
            database,
            "DOWNLOAD_LOCAL_RETRY_QUEUED",
            {
                "authorizationId": authorization["id"],
                "estimateId": job["estimate_id"],
                "jobId": job_id,
                "remoteJobId": job["remote_job_id"],
                "newRemoteOrderCreated": False,
            },
            now,
        )
        return _authorization_result(database, authorization["id"])

def cancel_authorization_job(job_id: str) -> dict[str, Any]:
    with connect() as database:
        database.execute("BEGIN IMMEDIATE")
        job = database.execute("SELECT * FROM dataset_jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            raise AuthorizationError("JOB_NOT_FOUND", "Download job not found.", "Reload Data Planner.", status_code=404)
        if job["remote_job_id"]:
            raise AuthorizationError("REMOTE_CANCEL_REQUIRED", "This job already exists at Databento and was not cancelled locally.", "Cancel it in Databento, then refresh status.", status_code=409)
        if job["status"] not in {"AUTHORIZED", "FAILED"}:
            raise AuthorizationError("CANCEL_NOT_ALLOWED", "This job cannot be cancelled in its current state.", "Refresh the job status.", status_code=409)
        now = utc_now()
        database.execute("UPDATE dataset_jobs SET status = 'CANCELLED', updated_at = ? WHERE id = ?", (now, job_id))
        database.execute("UPDATE download_authorizations SET state = 'CANCELLED', updated_at = ? WHERE id = ?", (now, job["authorization_id"]))
        database.execute("UPDATE authorization_ledger SET state = 'RELEASED', updated_at = ? WHERE authorization_id = ?", (now, job["authorization_id"]))
        database.execute("UPDATE data_estimates SET status = 'CANCELLED' WHERE id = ?", (job["estimate_id"],))
        _audit(database, "DOWNLOAD_CANCELLED", {"authorizationId": job["authorization_id"], "estimateId": job["estimate_id"], "jobId": job_id}, now)
        return _authorization_result(database, job["authorization_id"])


def purchase_review(estimate_id: str) -> dict[str, Any]:
    from .planner import estimate_public

    item = get_data_estimate(estimate_id)
    if not item:
        raise AuthorizationError("ESTIMATE_NOT_FOUND", "Estimate not found.", "Create a fresh estimate.", status_code=404)
    public = estimate_public(item)
    now = datetime.now(UTC)
    expires = _parse_utc(item["expires_at"])
    expired = now >= expires
    raw_amount = _decimal(public["maximumAuthorizedUsd"])
    amount = canonical_amount(raw_amount)
    existing = get_authorization(estimate_id=estimate_id)
    if expired and not existing:
        _mark_estimate_expired(estimate_id)
        public["status"] = "EXPIRED"
    return {
        "estimate": public, "expired": expired, "expiresAt": item["expires_at"],
        "remainingSeconds": max(0, int((expires - now).total_seconds())),
        "confirmationPhrase": canonical_confirmation_phrase(raw_amount), "confirmationCaseSensitive": True,
        "authorizationAmountDisplay": f"{amount:.2f}", "fingerprint": item["request_fingerprint"],
        "canSubmit": bool(public["allowed"] and not expired and not public["localReuse"] and not existing),
        "existingAuthorization": existing, "executionMode": execution_mode(),
        "chargeCreated": False, "fileSaved": False,
        "nextSafeStep": "Confirm the exact phrase only after reviewing every field." if not expired else "Run a fresh estimate.",
    }
