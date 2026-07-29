from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Callable

from .planner import ESTIMATE_TTL, build_dataset_request_plan, estimate_plan, optimize_plan
from .range_planner import RANGE_REVIEW_TTL, estimate_range_plan, preview_range_plan, range_request_fingerprint
from .storage import (
    find_reusable_estimate_job,
    get_estimate_job,
    save_estimate_job,
    save_planner_state,
    update_estimate_job,
    utc_now,
)


class EstimateJobCancelled(RuntimeError):
    pass


def request_fingerprint(payload: dict[str, Any], job_kind: str = "estimate") -> str:
    if job_kind == "range":
        return range_request_fingerprint(payload)
    plan = build_dataset_request_plan(payload)
    canonical = {
        "jobKind": job_kind,
        "market": str(payload.get("market") or "MES"),
        "dataset": str(payload.get("dataset") or "GLBX.MDP3"),
        "symbol": str(payload.get("symbol") or "MES.v.0"),
        "days": int(payload.get("days", 1)),
        "requestPlan": plan,
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def public_job(job: dict[str, Any], *, reused: bool = False) -> dict[str, Any]:
    return {
        "id": job["id"],
        "requestFingerprint": job["request_fingerprint"],
        "jobKind": job["job_kind"],
        "status": job["status"],
        "request": job["request"],
        "result": job.get("result"),
        "error": (
            {"code": job.get("error_code") or "ESTIMATE_FAILED", "message": job.get("error_message")}
            if job.get("error_message") else None
        ),
        "retryOf": job.get("retry_of"),
        "createdAt": job["created_at"],
        "startedAt": job.get("started_at"),
        "completedAt": job.get("completed_at"),
        "expiresAt": job["expires_at"],
        "cancelledAt": job.get("cancelled_at"),
        "updatedAt": job["updated_at"],
        "progress": float(job.get("progress") or 0),
        "checkpoint": job.get("checkpoint") or {},
        "reused": reused,
    }


def create_estimate_job(
    payload: dict[str, Any], *, job_kind: str = "estimate", force: bool = False, retry_of: str | None = None,
) -> dict[str, Any]:
    if job_kind not in {"estimate", "optimize", "range"}:
        raise ValueError("Unsupported estimate job kind.")
    if job_kind == "range":
        plan = preview_range_plan(payload)
    else:
        plan = build_dataset_request_plan(payload)
        save_planner_state(plan)
    fingerprint = request_fingerprint(payload, job_kind)
    if not force:
        existing = find_reusable_estimate_job(fingerprint, job_kind)
        if existing:
            return public_job(existing, reused=True)
    now = datetime.now(UTC)
    ttl = RANGE_REVIEW_TTL if job_kind == "range" else ESTIMATE_TTL
    job = save_estimate_job({
        "id": str(uuid.uuid4()),
        "request_fingerprint": fingerprint,
        "request": payload,
        "job_kind": job_kind,
        "status": "PENDING",
        "retry_of": retry_of,
        "created_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + ttl).isoformat().replace("+00:00", "Z"),
    })
    return public_job(job)


def run_estimate_job(
    job_id: str,
    *,
    estimate_runner: Callable[[dict[str, Any]], dict[str, Any]] = estimate_plan,
    optimize_runner: Callable[[dict[str, Any]], dict[str, Any]] = optimize_plan,
    range_runner: Callable[..., dict[str, Any]] = estimate_range_plan,
) -> dict[str, Any]:
    job = get_estimate_job(job_id)
    if not job:
        raise ValueError("Estimate job not found.")
    if job["status"] == "CANCELLED":
        return public_job(job)
    if job["status"] == "COMPLETED":
        return public_job(job, reused=True)
    expires = datetime.fromisoformat(job["expires_at"].replace("Z", "+00:00"))
    if job["status"] == "PENDING" and expires <= datetime.now(UTC):
        return public_job(update_estimate_job(job_id, "EXPIRED"))
    update_estimate_job(
        job_id, "RUNNING", progress=0, checkpoint={"phase": "starting"},
        started_at=job.get("started_at") or utc_now(), error_code=None, error_message=None,
    )
    try:
        if job["job_kind"] == "range":
            def report(completed: int, total: int, session_date: str) -> None:
                latest = get_estimate_job(job_id)
                if latest and latest["status"] == "CANCELLED":
                    raise EstimateJobCancelled("Range estimate cancelled.")
                update_estimate_job(
                    job_id, "RUNNING", progress=completed / max(total, 1),
                    checkpoint={"phase": "range_metadata", "completedDays": completed, "totalDays": total, "sessionDate": session_date},
                )
            result = range_runner(job["request"], progress_callback=report)
        else:
            runner = optimize_runner if job["job_kind"] == "optimize" else estimate_runner
            result = runner(job["request"])
        latest = get_estimate_job(job_id)
        if latest and latest["status"] == "CANCELLED":
            return public_job(latest)
        completed = utc_now()
        updated = update_estimate_job(job_id, "COMPLETED", progress=1, checkpoint={"phase": "completed"}, result=result, completed_at=completed)
        return public_job(updated)
    except EstimateJobCancelled:
        latest = get_estimate_job(job_id)
        return public_job(latest) if latest else public_job(update_estimate_job(job_id, "CANCELLED", cancelled_at=utc_now()))
    except Exception as exc:
        failed = update_estimate_job(
            job_id, "FAILED", error_code=type(exc).__name__.upper(),
            error_message=str(exc) or "Metadata estimate failed.", completed_at=utc_now(),
        )
        return public_job(failed)


def retry_estimate_job(job_id: str) -> dict[str, Any]:
    job = get_estimate_job(job_id)
    if not job:
        raise ValueError("Estimate job not found.")
    if job["status"] not in {"FAILED", "EXPIRED", "CANCELLED"}:
        raise ValueError("Only failed, expired, or cancelled estimate jobs can be retried.")
    return create_estimate_job(job["request"], job_kind=job["job_kind"], force=True, retry_of=job_id)


def cancel_estimate_job(job_id: str) -> dict[str, Any]:
    job = get_estimate_job(job_id)
    if not job:
        raise ValueError("Estimate job not found.")
    if job["status"] not in {"PENDING", "RUNNING"}:
        return public_job(job)
    return public_job(update_estimate_job(job_id, "CANCELLED", cancelled_at=utc_now()))


def expire_estimate_jobs() -> None:
    now = datetime.now(UTC)
    from .storage import list_estimate_jobs

    for job in list_estimate_jobs(500):
        expires = datetime.fromisoformat(job["expires_at"].replace("Z", "+00:00"))
        if job["status"] in {"PENDING", "COMPLETED"} and expires <= now:
            update_estimate_job(job["id"], "EXPIRED")
