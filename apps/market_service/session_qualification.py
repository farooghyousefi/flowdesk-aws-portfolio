from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


# One independent research day must contain a meaningful continuous trading window.
# Six hours excludes snapshots and short diagnostics while accepting the user's
# 14.5-hour MES session and regular-session-only datasets.
MIN_INDEPENDENT_SESSION_SECONDS = 6 * 60 * 60


def parse_utc_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def independent_session_rejection_reason(
    dataset: dict[str, Any],
    *,
    minimum_seconds: int = MIN_INDEPENDENT_SESSION_SECONDS,
) -> str | None:
    """Return why a dataset cannot count as an independent Full-L3 day.

    This gate is intentionally stricter than the import/data-health gate. A tiny
    snapshot may be structurally complete and useful for diagnostics, but it is
    not an independent trading session for validation.
    """

    if str(dataset.get("completeness") or "").lower() != "complete":
        return "NOT_COMPLETE"
    if str(dataset.get("integrity_status") or "").lower() != "passed":
        return "INTEGRITY_NOT_PASSED"

    data_health = dataset.get("data_health") if isinstance(dataset.get("data_health"), dict) else {}
    data_mode = str(dataset.get("data_mode") or "").lower()
    if data_mode != "full_l3" and not bool(data_health.get("fullL3Claim")):
        return "NOT_FULL_L3"

    start_at = parse_utc_datetime(dataset.get("start_at"))
    end_at = parse_utc_datetime(dataset.get("end_at"))
    if not start_at or not end_at or end_at <= start_at:
        return "INVALID_TIME_RANGE"

    duration_seconds = (end_at - start_at).total_seconds()
    if duration_seconds < minimum_seconds:
        return "TOO_SHORT_FOR_INDEPENDENT_DAY"

    if int(dataset.get("record_count") or 0) <= 0:
        return "EMPTY_DATASET"
    return None


def qualifying_independent_full_l3_sessions(
    datasets: list[dict[str, Any]],
    *,
    minimum_seconds: int = MIN_INDEPENDENT_SESSION_SECONDS,
) -> list[dict[str, Any]]:
    return [
        item
        for item in datasets
        if independent_session_rejection_reason(item, minimum_seconds=minimum_seconds) is None
    ]


def excluded_session_diagnostics(
    datasets: list[dict[str, Any]],
    *,
    minimum_seconds: int = MIN_INDEPENDENT_SESSION_SECONDS,
) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for item in datasets:
        reason = independent_session_rejection_reason(item, minimum_seconds=minimum_seconds)
        if reason is None:
            continue
        start_at = parse_utc_datetime(item.get("start_at"))
        end_at = parse_utc_datetime(item.get("end_at"))
        duration_seconds = (
            max(0.0, (end_at - start_at).total_seconds())
            if start_at and end_at
            else None
        )
        diagnostics.append(
            {
                "sessionId": item.get("id"),
                "reason": reason,
                "durationSeconds": duration_seconds,
                "recordCount": int(item.get("record_count") or 0),
            }
        )
    return diagnostics
