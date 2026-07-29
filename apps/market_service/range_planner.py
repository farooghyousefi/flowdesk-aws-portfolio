from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable

import databento as db

from apps.connectors.databento.src.config import DATASET, DEFAULT_SYMBOL, ConnectorConfig, ConnectorError, load_config
from .authorization import AuthorizationError, canonical_amount, canonical_confirmation_phrase, execution_mode
from .planner import MODE_SPECS, build_dataset_request_plan, build_time_window, estimate_mode
from .storage import (
    append_audit,
    connect,
    get_data_estimate,
    get_range_plan,
    get_range_plan_by_fingerprint,
    list_dataset_jobs,
    list_range_plans,
    save_range_plan,
    tracked_costs,
    update_range_plan,
    utc_now,
)

MAX_RANGE_CALENDAR_DAYS = 184
RANGE_REVIEW_TTL = timedelta(hours=1)


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_date(value: Any, name: str) -> date:
    try:
        return date.fromisoformat(str(value or ""))
    except ValueError as exc:
        raise ConnectorError(f"{name} must use YYYY-MM-DD.") from exc


def _decimal(value: Any, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value).replace(",", "."))
    except Exception as exc:
        raise ConnectorError(f"{name} must be a valid USD amount.") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ConnectorError(f"{name} must be greater than zero.")
    return parsed.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def range_session_dates(payload: dict[str, Any]) -> list[date]:
    start = _parse_date(payload.get("startDate"), "Start date")
    end = _parse_date(payload.get("endDate"), "End date")
    if end < start:
        raise ConnectorError("End date must be on or after start date.")
    if (end - start).days + 1 > MAX_RANGE_CALENDAR_DAYS:
        raise ConnectorError(f"A range may contain at most {MAX_RANGE_CALENDAR_DAYS} calendar days.")
    if end >= datetime.now(UTC).date():
        raise ConnectorError("End date must be a completed historical day.")
    include_weekends = bool(payload.get("includeWeekends", False))
    values: list[date] = []
    cursor = start
    while cursor <= end:
        if include_weekends or cursor.weekday() < 5:
            values.append(cursor)
        cursor += timedelta(days=1)
    if not values:
        raise ConnectorError("The selected range contains no eligible session days.")
    return values


def _daily_payload(payload: dict[str, Any], day: date) -> dict[str, Any]:
    return {
        "market": str(payload.get("market") or "MES"),
        "dataset": str(payload.get("dataset") or DATASET),
        "symbol": str(payload.get("symbol") or DEFAULT_SYMBOL),
        "date": day.isoformat(),
        "timezone": str(payload.get("timezone") or "Europe/Berlin"),
        "replayStart": str(payload.get("replayStart") or "00:00"),
        "replayEnd": str(payload.get("replayEnd") or "22:00"),
        "contextMinutes": int(payload.get("contextMinutes", 0)),
        "days": 1,
    }


def range_request_fingerprint(payload: dict[str, Any]) -> str:
    days = range_session_dates(payload)
    canonical = {
        "dataset": str(payload.get("dataset") or DATASET),
        "symbol": str(payload.get("symbol") or DEFAULT_SYMBOL),
        "startDate": days[0].isoformat(),
        "endDate": days[-1].isoformat(),
        "sessionDates": [item.isoformat() for item in days],
        "timezone": str(payload.get("timezone") or "Europe/Berlin"),
        "replayStart": str(payload.get("replayStart") or "00:00"),
        "replayEnd": str(payload.get("replayEnd") or "22:00"),
        "contextMinutes": int(payload.get("contextMinutes", 0)),
        "budgetUsd": f"{_decimal(payload.get('budgetUsd', 125), 'Budget'):.2f}",
        "schema": "mbo",
        "mode": "full_l3",
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def preview_range_plan(payload: dict[str, Any]) -> dict[str, Any]:
    if str(payload.get("dataset") or DATASET) != DATASET:
        raise ConnectorError(f"Only {DATASET} is supported.")
    if str(payload.get("symbol") or DEFAULT_SYMBOL) != DEFAULT_SYMBOL:
        raise ConnectorError(f"Only {DEFAULT_SYMBOL} is supported.")
    days = range_session_dates(payload)
    first = build_dataset_request_plan(_daily_payload(payload, days[0]))
    last = build_dataset_request_plan(_daily_payload(payload, days[-1]))
    split = split_plan([item.isoformat() for item in days])
    return {
        "valid": True,
        "metadataRequested": False,
        "downloadStarted": False,
        "startDate": days[0].isoformat(),
        "endDate": days[-1].isoformat(),
        "calendarDays": (days[-1] - days[0]).days + 1,
        "sessionDays": len(days),
        "sessionDates": [item.isoformat() for item in days],
        "timezone": first["timezone"],
        "replayStartLocal": first["replayStartLocal"],
        "replayEndLocal": first["replayEndLocal"],
        "firstRequestStartUtc": first["requestStartUtc"],
        "lastRequestEndUtc": last["requestEndUtc"],
        "budgetUsd": float(_decimal(payload.get("budgetUsd", 125), "Budget")),
        "splitPlan": split,
    }


def split_plan(session_dates: list[str]) -> dict[str, Any]:
    count = len(session_dates)
    development = max(1, int(count * 0.60)) if count else 0
    validation = max(1, int(count * 0.20)) if count >= 3 else max(0, count - development)
    if development + validation > count:
        validation = max(0, count - development)
    locked = max(0, count - development - validation)
    if count >= 5 and locked == 0:
        locked = 1
        development = max(1, development - 1)
    rows = []
    for index, session_date in enumerate(session_dates):
        if index < development:
            name = "Development"
        elif index < development + validation:
            name = "Validation"
        else:
            name = "Locked Test"
        rows.append({"sessionDate": session_date, "splitName": name, "locked": name == "Locked Test"})
    return {
        "developmentSessions": sum(1 for row in rows if row["splitName"] == "Development"),
        "validationSessions": sum(1 for row in rows if row["splitName"] == "Validation"),
        "lockedSessions": sum(1 for row in rows if row["splitName"] == "Locked Test"),
        "assignments": rows,
    }


def _update_child_estimate(estimate_id: str, *, range_plan_id: str, budget: Decimal, planned_split: dict[str, Any], expires_at: str) -> dict[str, Any]:
    with connect() as database:
        row = database.execute("SELECT * FROM data_estimates WHERE id = ?", (estimate_id,)).fetchone()
        if not row:
            raise ConnectorError("A daily estimate disappeared while building the range plan.")
        metadata = json.loads(row["metadata_json"] or "{}")
        warnings = json.loads(row["warnings_json"] or "[]")
        budget_warnings = {
            "Configured request cost limit would be exceeded.",
            "Local tracked daily budget would be exceeded.",
            "Local tracked weekly budget would be exceeded.",
            "Local tracked monthly budget would be exceeded.",
        }
        warnings = [item for item in warnings if item not in budget_warnings]
        raw_cost = Decimal(str(metadata.get("rawEstimatedCostUsd", 0)))
        reserve = Decimal(str(metadata.get("safetyReserveUsd", 0)))
        maximum = raw_cost + reserve
        metadata.update({
            "rangePlanId": range_plan_id,
            "rangeBudgetUsd": float(budget),
            "requestLimitUsd": float(max(Decimal("1.00"), maximum)),
            "dailyLimitUsd": float(budget),
            "weeklyLimitUsd": float(budget),
            "monthlyLimitUsd": float(budget),
            "dailyRemainingUsd": float(budget),
            "weeklyRemainingUsd": float(budget),
            "monthlyRemainingUsd": float(budget),
            "plannedSplit": planned_split,
            "rangeAuthorizationPolicy": "one explicit aggregate confirmation; atomic local reservations; sequential remote submission",
        })
        allowed = int((bool(row["local_reuse"]) or maximum <= budget) and not warnings)
        status = row["status"]
        if status not in {"AUTHORIZED", "SUBMITTING", "QUEUED", "DOWNLOADING", "IMPORTING", "VALIDATING_IMPORT", "COMPLETED", "READY", "IMPORTED"}:
            status = "LOCAL_REUSE" if bool(row["local_reuse"]) else "AWAITING_CONFIRMATION" if allowed else "BLOCKED"
        database.execute(
            "UPDATE data_estimates SET metadata_json = ?, warnings_json = ?, allowed = ?, status = ?, expires_at = ? WHERE id = ?",
            (json.dumps(metadata, default=str), json.dumps(warnings), allowed, status, expires_at, estimate_id),
        )
    return get_data_estimate(estimate_id) or {}


def estimate_range_plan(
    payload: dict[str, Any],
    *,
    client_factory: Callable[[str], Any] | None = None,
    config: ConnectorConfig | None = None,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    preview = preview_range_plan(payload)
    budget = _decimal(payload.get("budgetUsd", 125), "Budget")
    fingerprint = range_request_fingerprint(payload)
    existing = get_range_plan_by_fingerprint(fingerprint)
    plan_id = existing["id"] if existing else str(uuid.uuid4())
    now = datetime.now(UTC)
    expires_at = _utc(now + RANGE_REVIEW_TTL)
    plan = save_range_plan({
        "id": plan_id,
        "request_fingerprint": fingerprint,
        "request": payload,
        "estimate_ids": existing.get("estimate_ids", []) if existing else [],
        "summary": {"preview": preview, "phase": "estimating"},
        "status": "ESTIMATING",
        "created_at": existing.get("created_at", _utc(now)) if existing else _utc(now),
        "expires_at": expires_at,
    })
    active_config = config or load_config()
    client = (client_factory or db.Historical)(active_config.api_key)
    assignments = {row["sessionDate"]: row for row in preview["splitPlan"]["assignments"]}
    estimates: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    days = range_session_dates(payload)
    for index, day in enumerate(days, start=1):
        if progress_callback:
            progress_callback(index - 1, len(days), day.isoformat())
        try:
            daily = _daily_payload(payload, day)
            window = build_time_window(daily)
            # Resolve each day independently so contract rolls are represented correctly.
            from .planner import _shared_metadata
            shared = _shared_metadata(client, window)
            public = estimate_mode(client, active_config, daily, MODE_SPECS["full_l3"], shared=shared)
            item = _update_child_estimate(
                public["estimateId"], range_plan_id=plan_id, budget=budget,
                planned_split=assignments[day.isoformat()], expires_at=expires_at,
            )
            from .planner import estimate_public
            estimates.append(estimate_public(item))
        except Exception as exc:
            errors.append({"sessionDate": day.isoformat(), "message": str(exc)})
        if progress_callback:
            progress_callback(index, len(days), day.isoformat())

    total_raw = sum(Decimal(str(item["rawEstimatedCostUsd"])) for item in estimates if not item["localReuse"])
    total_max = sum(canonical_amount(item["maximumAuthorizedUsd"]) for item in estimates if not item["localReuse"])
    local_count = sum(1 for item in estimates if item["localReuse"])
    blocked = [item for item in estimates if not item["localReuse"] and not item["allowed"]]
    buyable = [item for item in estimates if not item["localReuse"] and item["allowed"]]
    current_reserved = Decimal(str(tracked_costs().get("authorizedMonth", 0)))
    remaining_after = budget - current_reserved - total_max
    allowed = not errors and not blocked and total_max > 0 and remaining_after >= 0
    confirmation = f"DOWNLOAD RANGE ${canonical_amount(total_max):.2f}"
    summary = {
        "preview": preview,
        "rangePlanId": plan_id,
        "status": "READY_FOR_REVIEW" if allowed else "LOCAL_ONLY" if local_count == len(days) else "BLOCKED",
        "estimatedSessionDays": len(days),
        "estimatedDaysCompleted": len(estimates),
        "localReuseDays": local_count,
        "downloadDays": len(buyable),
        "blockedDays": len(blocked) + len(errors),
        "estimatedRecords": sum(int(item["estimatedRecords"]) for item in estimates),
        "billableBytes": sum(int(item["billableBytes"]) for item in estimates if not item["localReuse"]),
        "rawEstimatedCostUsd": float(total_raw),
        "maximumAuthorizedUsd": float(total_max),
        "budgetUsd": float(budget),
        "alreadyReservedUsd": float(current_reserved),
        "remainingBudgetAfterUsd": float(remaining_after),
        "allowed": allowed,
        "confirmationPhrase": confirmation,
        "executionMode": execution_mode(),
        "splitPlan": preview["splitPlan"],
        "dailyEstimates": estimates,
        "errors": errors,
        "warnings": [
            "No order has been submitted. This is metadata-only until the aggregate confirmation is entered.",
            "Historical files remain reusable locally after purchase; do not delete the raw DBN and manifest files.",
        ],
    }
    updated = update_range_plan(
        plan_id, summary["status"], summary=summary,
        estimate_ids=[item["estimateId"] for item in estimates], expires_at=expires_at,
    )
    append_audit("RANGE_ESTIMATE_COMPLETED", {
        "rangePlanId": plan_id, "sessionDays": len(days), "downloadDays": len(buyable),
        "localReuseDays": local_count, "maximumAuthorizedUsd": float(total_max),
        "orderSubmitted": False,
    })
    return {"rangePlan": range_plan_public(updated), "downloadStarted": False, "message": "Range estimate completed. No Databento order was submitted."}


def range_plan_public(plan: dict[str, Any]) -> dict[str, Any]:
    summary = dict(plan.get("summary") or {})
    auth_states: list[str] = []
    remote_jobs = 0
    ready_jobs = 0
    completed_jobs = 0
    with connect() as database:
        for estimate_id in plan.get("estimate_ids", []):
            auth = database.execute("SELECT state FROM download_authorizations WHERE estimate_id = ?", (estimate_id,)).fetchone()
            if auth:
                auth_states.append(str(auth["state"]))
            rows = database.execute("SELECT status FROM dataset_jobs WHERE estimate_id = ?", (estimate_id,)).fetchall()
            for row in rows:
                remote_jobs += 1
                if row["status"] == "READY":
                    ready_jobs += 1
                if row["status"] in {"IMPORTED", "VALIDATED", "COMPLETED"}:
                    completed_jobs += 1
    if auth_states:
        if all(state == "COMPLETED" for state in auth_states):
            runtime_status = "COMPLETED"
        elif any(state == "FAILED" for state in auth_states):
            runtime_status = "PARTIAL_FAILURE"
        elif any(state in {"QUEUED", "DOWNLOADING", "IMPORTING", "VALIDATING_IMPORT"} for state in auth_states):
            runtime_status = "IN_PROGRESS"
        else:
            runtime_status = "AUTHORIZED"
    else:
        runtime_status = plan["status"]
    return {
        "id": plan["id"], "requestFingerprint": plan["request_fingerprint"],
        "request": plan["request"], "estimateIds": plan["estimate_ids"],
        "summary": summary, "status": runtime_status,
        "createdAt": plan["created_at"], "expiresAt": plan["expires_at"], "updatedAt": plan["updated_at"],
        "authorizationStates": auth_states, "remoteJobs": remote_jobs,
        "readyJobs": ready_jobs, "completedJobs": completed_jobs,
    }


def get_range_plan_public(plan_id: str) -> dict[str, Any]:
    plan = get_range_plan(plan_id)
    if not plan:
        raise ConnectorError("Range plan not found.")
    return range_plan_public(plan)


def list_range_plans_public(limit: int = 5) -> list[dict[str, Any]]:
    return [range_plan_public(item) for item in list_range_plans(limit)]



def ready_range_job_ids(plan_id: str) -> list[str]:
    plan = get_range_plan(plan_id)
    if not plan:
        raise ConnectorError("Range plan not found.")
    result: list[str] = []
    for estimate_id in plan.get("estimate_ids", []):
        for job in list_dataset_jobs():
            if str(job.get("estimate_id") or "") != str(estimate_id):
                continue
            if str(job.get("status") or "").upper() == "READY":
                result.append(str(job["id"]))
    return result

def authorize_range_plan(payload: dict[str, Any], *, mode: str | None = None) -> dict[str, Any]:
    plan_id = str(payload.get("rangePlanId") or "")
    idempotency_key = str(payload.get("idempotencyKey") or "").strip()
    plan = get_range_plan(plan_id)
    if not plan:
        raise AuthorizationError("RANGE_PLAN_NOT_FOUND", "The range plan no longer exists.", "Create a fresh range estimate.", status_code=404)
    summary = plan.get("summary") or {}
    if not idempotency_key:
        raise AuthorizationError("INVALID_REQUEST", "An idempotency key is required.", "Reload the range review.")
    if not bool(payload.get("acceptedTerms")):
        raise AuthorizationError("CONFIRMATION_REQUIRED", "Cost acknowledgement is required.", "Accept the range cost terms.")
    expected_phrase = str(summary.get("confirmationPhrase") or "")
    if str(payload.get("confirmationPhrase") or "").strip() != expected_phrase:
        raise AuthorizationError("CONFIRMATION_PHRASE_MISMATCH", "The exact range confirmation phrase is required.", "Enter the phrase shown exactly.")
    displayed = canonical_amount(payload.get("displayedAuthorizationAmount"))
    total = canonical_amount(summary.get("maximumAuthorizedUsd", 0))
    if displayed != total:
        raise AuthorizationError("AMOUNT_MISMATCH", "The displayed range amount is stale.", "Create a fresh range estimate.")
    if not bool(summary.get("allowed")):
        raise AuthorizationError("AUTHORIZATION_BLOCKED", "This range plan is not eligible for purchase.", "Resolve blocked days or reduce the range.")
    if datetime.now(UTC) >= datetime.fromisoformat(plan["expires_at"].replace("Z", "+00:00")):
        raise AuthorizationError("ESTIMATE_EXPIRED", "The range estimate has expired.", "Create a fresh range estimate.", status_code=409)
    runtime_mode = mode or execution_mode()
    if runtime_mode == "disabled":
        raise AuthorizationError("QUEUE_UNAVAILABLE", "Batch submission is disabled on this service.", "Use dry_run for QA or live for a controlled purchase.", status_code=503)

    authorization_ids: list[str] = []
    job_ids: list[str] = []
    now = utc_now()
    with connect() as database:
        database.execute("BEGIN IMMEDIATE")
        existing_rows = database.execute(
            "SELECT id FROM download_authorizations WHERE idempotency_key LIKE ? ORDER BY created_at", (f"range:{plan_id}:{idempotency_key}:%",)
        ).fetchall()
        if existing_rows:
            authorization_ids = [str(row["id"]) for row in existing_rows]
        else:
            reservations = Decimal(str(database.execute(
                "SELECT COALESCE(SUM(CAST(amount AS REAL)),0) FROM authorization_ledger WHERE state = 'RESERVED'"
            ).fetchone()[0]))
            budget = Decimal(str(summary.get("budgetUsd", 0)))
            if reservations + Decimal(str(summary.get("maximumAuthorizedUsd", 0))) > budget:
                raise AuthorizationError("BUDGET_EXCEEDED", "The range budget would be exceeded by existing reservations.", "Release reservations or lower the range.")
            for index, estimate_id in enumerate(plan["estimate_ids"]):
                estimate = database.execute("SELECT * FROM data_estimates WHERE id = ?", (estimate_id,)).fetchone()
                if not estimate or bool(estimate["local_reuse"]):
                    continue
                if not bool(estimate["allowed"]):
                    raise AuthorizationError("AUTHORIZATION_BLOCKED", "At least one daily estimate is blocked.", "Re-estimate the range.")
                existing = database.execute("SELECT id FROM download_authorizations WHERE estimate_id = ?", (estimate_id,)).fetchone()
                if existing:
                    authorization_ids.append(str(existing["id"]))
                    continue
                metadata = json.loads(estimate["metadata_json"] or "{}")
                raw_amount = Decimal(str(metadata.get("maximumAuthorizedUsd", 0)))
                amount = canonical_amount(raw_amount)
                authorization_id = str(uuid.uuid4())
                child_key = f"range:{plan_id}:{idempotency_key}:{index}"
                phrase = canonical_confirmation_phrase(raw_amount)
                database.execute(
                    """INSERT INTO download_authorizations(
                       id, estimate_id, idempotency_key, request_fingerprint, mode, state, accepted_terms,
                       confirmation_phrase, authorization_amount, displayed_authorization_amount, execution_mode,
                       retry_safe, recovered, created_at, updated_at, authorized_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (authorization_id, estimate_id, child_key, estimate["request_fingerprint"], estimate["mode"], "AUTHORIZED", 1,
                     phrase, f"{amount:.2f}", f"{amount:.2f}", runtime_mode, 0, 0, now, now, now),
                )
                database.execute(
                    "INSERT INTO authorization_ledger(id, authorization_id, estimate_id, amount, state, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), authorization_id, estimate_id, f"{amount:.2f}", "RESERVED", now, now),
                )
                authorization_ids.append(authorization_id)
                for schema in json.loads(estimate["schemas_json"]):
                    job_id = str(uuid.uuid4())
                    job_ids.append(job_id)
                    database.execute(
                        """INSERT INTO dataset_jobs(
                           id, estimate_id, schema_name, status, details_json, created_at, updated_at,
                           authorization_id, progress
                           ) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (job_id, estimate_id, schema, "AUTHORIZED", json.dumps({"executionMode": runtime_mode, "remoteSubmitted": False, "rangePlanId": plan_id}), now, now, authorization_id, 0),
                    )
                database.execute("UPDATE data_estimates SET status = 'AUTHORIZED', job_id = ? WHERE id = ?", (",".join(job_ids[-1:]), estimate_id))
        updated_summary = {**summary, "authorizationIds": authorization_ids, "authorizedAt": now, "executionMode": runtime_mode}
        database.execute("UPDATE range_plans SET status = 'AUTHORIZED', summary_json = ?, updated_at = ? WHERE id = ?", (json.dumps(updated_summary), now, plan_id))
        database.execute(
            "INSERT INTO audit_events(plan_id, session_id, event_type, payload_json, created_at) VALUES(NULL,NULL,'RANGE_DOWNLOAD_AUTHORIZED',?,?)",
            (json.dumps({"rangePlanId": plan_id, "authorizationIds": authorization_ids, "amount": f"{total:.2f}", "executionMode": runtime_mode, "secretValuesLogged": False}), now),
        )
    return {
        "rangePlan": get_range_plan_public(plan_id),
        "authorizationIds": authorization_ids,
        "jobIds": job_ids,
        "executionMode": runtime_mode,
        "idempotentReplay": not bool(job_ids),
        "remoteSubmissionCreated": False,
        "chargeCreated": False,
    }
