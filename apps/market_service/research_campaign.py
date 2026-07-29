from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any, Iterable

from .strategy_search import metrics_for_trades
from .validation import purged_walk_forward_windows


CAMPAIGN_VERSION = "research-campaign-v1"
DAILY_ENGINE_VERSION = "daily-research-v3"
_DAILY_FILE = re.compile(r"^glbx-mdp3-(\d{4})(\d{2})(\d{2})\.mbo\.dbn\.zst$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
COHORTS = ("Development", "Validation", "Locked Test")


class ResearchCampaignError(ValueError):
    """A deterministic campaign validation error safe to show to the user."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _manifest_sessions(manifest: dict[str, Any], *, job_id: str) -> list[dict[str, Any]]:
    if manifest.get("status") != "COMPLETED":
        raise ResearchCampaignError("The source ingest manifest is not marked COMPLETED.")
    if str(manifest.get("jobId") or "").upper() != job_id.upper():
        raise ResearchCampaignError("The source ingest manifest belongs to a different job.")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ResearchCampaignError("The source ingest manifest is missing its file list.")

    sessions: list[dict[str, Any]] = []
    seen_dates: set[str] = set()
    expected_prefix = f"flowdesk/raw/databento/jobs/{job_id.upper()}/"
    for item in files:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("filename") or "")
        match = _DAILY_FILE.fullmatch(filename)
        if match is None:
            continue
        session_date = "-".join(match.groups())
        if session_date in seen_dates:
            raise ResearchCampaignError("The source manifest contains a duplicate daily session.")
        seen_dates.add(session_date)
        sha256 = str(item.get("sha256") or "")
        key = str(item.get("s3Key") or "")
        size = item.get("sizeBytes")
        if not _SHA256.fullmatch(sha256):
            raise ResearchCampaignError("A daily source file has an invalid SHA-256.")
        if not key.startswith(expected_prefix) or not key.endswith(filename):
            raise ResearchCampaignError("A daily source file has an unexpected S3 key.")
        if not isinstance(size, int) or size < 1:
            raise ResearchCampaignError("A daily source file has an invalid size.")
        sessions.append(
            {
                "sessionDate": session_date,
                "filename": filename,
                "sourceKey": key,
                "sourceBytes": size,
                "sourceFingerprint": sha256,
            }
        )
    sessions.sort(key=lambda row: row["sessionDate"])
    if len(sessions) < 15:
        raise ResearchCampaignError(
            "At least 15 independent daily files are required to freeze campaign cohorts."
        )
    return sessions


def build_campaign_plan(
    manifest: dict[str, Any],
    *,
    job_id: str,
    engine_version: str = DAILY_ENGINE_VERSION,
) -> dict[str, Any]:
    """Freeze chronological cohorts before any multi-session result is inspected."""
    normalized_job_id = job_id.upper()
    sessions = _manifest_sessions(manifest, job_id=normalized_job_id)
    count = len(sessions)
    development_end = round(count * 0.60)
    validation_end = round(count * 0.80)
    for index, session in enumerate(sessions):
        if index < development_end:
            cohort = "Development"
        elif index < validation_end:
            cohort = "Validation"
        else:
            cohort = "Locked Test"
        session["cohort"] = cohort
        session["resultKey"] = (
            f"flowdesk/research/{engine_version}/jobs/{normalized_job_id}/sessions/"
            f"{session['sessionDate']}/{session['sourceFingerprint'][:16]}.json"
        )

    development_validation = [
        row["sessionDate"]
        for row in sessions
        if row["cohort"] in {"Development", "Validation"}
    ]
    windows = purged_walk_forward_windows(
        development_validation,
        train_size=min(20, max(5, len(development_validation) // 2)),
        test_size=min(5, max(2, len(development_validation) // 8)),
        embargo=1,
    )
    source_fingerprint = _fingerprint(
        [
            {
                "sessionDate": row["sessionDate"],
                "sourceFingerprint": row["sourceFingerprint"],
                "sourceBytes": row["sourceBytes"],
            }
            for row in sessions
        ]
    )
    core = {
        "campaignVersion": CAMPAIGN_VERSION,
        "engineVersion": engine_version,
        "jobId": normalized_job_id,
        "sourceManifestFingerprint": source_fingerprint,
        "cohortPolicy": {
            "method": "CHRONOLOGICAL_PRECOMMITMENT",
            "developmentFraction": 0.60,
            "validationFraction": 0.20,
            "lockedTestFraction": 0.20,
            "selectionUsesOnly": ["Development"],
            "confirmationUsesOnly": ["Validation"],
            "lockedTestExecution": "WITHHELD_UNTIL_STRATEGY_FREEZE",
        },
        "sessionCounts": {
            cohort: sum(1 for row in sessions if row["cohort"] == cohort)
            for cohort in COHORTS
        },
        "sessions": sessions,
        "purgedWalkForwardWindows": windows,
        "automaticOrderExecution": False,
        "paperPromotionAllowed": False,
        "profitabilityClaim": False,
    }
    return {**core, "campaignId": _fingerprint(core)[:20]}


def _plan_by_date(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    sessions = plan.get("sessions")
    if not isinstance(sessions, list):
        raise ResearchCampaignError("The campaign plan is missing its sessions.")
    by_date: dict[str, dict[str, Any]] = {}
    for row in sessions:
        if not isinstance(row, dict):
            raise ResearchCampaignError("The campaign plan contains an invalid session.")
        session_date = str(row.get("sessionDate") or "")
        if session_date in by_date:
            raise ResearchCampaignError("The campaign plan contains a duplicate session date.")
        by_date[session_date] = row
    return by_date


def _candidate_specs(result: dict[str, Any]) -> dict[int, dict[str, Any]]:
    evidence = result.get("candidateEvidence")
    if not isinstance(evidence, list):
        raise ResearchCampaignError("A daily result is missing complete candidate evidence.")
    specs: dict[int, dict[str, Any]] = {}
    for candidate in evidence:
        if not isinstance(candidate, dict):
            continue
        index = int(candidate.get("candidateIndex") or 0)
        if index < 1:
            raise ResearchCampaignError("A daily result contains an invalid candidate index.")
        specs[index] = {
            "candidateIndex": index,
            "family": candidate.get("family"),
            "strategyName": candidate.get("strategyName"),
            "parameters": candidate.get("parameters"),
            "specFingerprint": _fingerprint(
                {
                    "family": candidate.get("family"),
                    "strategyName": candidate.get("strategyName"),
                    "parameters": candidate.get("parameters"),
                }
            ),
        }
    return specs


def _realistic_evidence(result: dict[str, Any]) -> dict[int, dict[str, Any]]:
    evidence = result.get("realisticCandidateEvidence")
    if not isinstance(evidence, list):
        raise ResearchCampaignError("A daily result is missing realistic candidate evidence.")
    return {
        int(candidate["candidateIndex"]): candidate
        for candidate in evidence
        if isinstance(candidate, dict) and int(candidate.get("candidateIndex") or 0) > 0
    }


def aggregate_campaign_results(
    plan: dict[str, Any],
    daily_results: Iterable[dict[str, Any]],
    *,
    include_locked_test: bool = False,
) -> dict[str, Any]:
    """Aggregate realistic evidence without using validation or locked data for selection."""
    by_date = _plan_by_date(plan)
    results: dict[str, dict[str, Any]] = {}
    canonical_specs: dict[int, dict[str, Any]] = {}
    trades: dict[int, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    evaluated_sessions: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for result in daily_results:
        session_date = str(result.get("sessionDate") or "")
        planned = by_date.get(session_date)
        if planned is None:
            raise ResearchCampaignError("A daily result is outside the frozen campaign plan.")
        if session_date in results:
            raise ResearchCampaignError("The campaign contains duplicate daily results.")
        cohort = str(planned.get("cohort"))
        if cohort == "Locked Test" and not include_locked_test:
            raise ResearchCampaignError(
                "Locked Test results cannot be aggregated before explicit strategy freeze."
            )
        if result.get("engineVersion") != plan.get("engineVersion"):
            raise ResearchCampaignError("A daily result uses a different engine version.")
        if result.get("sourceFingerprint") != planned.get("sourceFingerprint"):
            raise ResearchCampaignError("A daily result does not match its frozen source file.")
        if result.get("automaticOrderExecution") is not False:
            raise ResearchCampaignError("A daily result does not preserve the no-order invariant.")
        specs = _candidate_specs(result)
        realistic = _realistic_evidence(result)
        for index, spec in specs.items():
            existing = canonical_specs.get(index)
            if existing is not None and existing["specFingerprint"] != spec["specFingerprint"]:
                raise ResearchCampaignError("Candidate parameters changed inside one campaign.")
            canonical_specs[index] = spec
            candidate = realistic.get(index)
            if candidate is None:
                raise ResearchCampaignError("A daily result omits a candidate's realistic status.")
            if candidate.get("evaluated"):
                evaluated_sessions[index][cohort] += 1
            for trade in candidate.get("tradeEvidence") or []:
                trades[index][cohort].append(
                    {**trade, "sessionDate": session_date}
                )
        results[session_date] = result

    candidates = []
    for index, spec in sorted(canonical_specs.items()):
        cohort_metrics: dict[str, Any] = {}
        for cohort in COHORTS:
            ordered_trades = sorted(
                trades[index][cohort],
                key=lambda row: (
                    row["sessionDate"],
                    int(row.get("entryTimestampNs") or 0),
                ),
            )
            metrics = metrics_for_trades(ordered_trades)
            metrics["resultSessions"] = sum(
                1
                for date, result in results.items()
                if by_date[date]["cohort"] == cohort
                and index in _candidate_specs(result)
            )
            metrics["evaluatedSessions"] = evaluated_sessions[index][cohort]
            metrics["tradeSessions"] = len(
                {row["sessionDate"] for row in ordered_trades}
            )
            cohort_metrics[cohort] = metrics

        development = cohort_metrics["Development"]
        development_score = (
            float(development["netExpectancyUsd"])
            * min(1.0, int(development["trades"]) / 100)
            - 0.01 * float(development["maximumDrawdownUsd"])
        )
        validation = cohort_metrics["Validation"]
        if int(validation["resultSessions"]) < plan["sessionCounts"]["Validation"]:
            validation_status = "PENDING"
        elif (
            int(validation["trades"]) >= 20
            and float(validation["netExpectancyUsd"]) > 0
            and float(validation.get("profitFactor") or 0) >= 1.05
        ):
            validation_status = "CONFIRMED"
        else:
            validation_status = "REJECTED"
        candidates.append(
            {
                **spec,
                "cohortMetrics": cohort_metrics,
                "developmentSelectionScore": round(development_score, 6),
                "validationStatus": validation_status,
            }
        )

    candidates.sort(
        key=lambda row: (
            int(row["cohortMetrics"]["Development"]["trades"]) >= 20,
            row["developmentSelectionScore"],
            int(row["cohortMetrics"]["Development"]["trades"]),
        ),
        reverse=True,
    )
    completed_counts = {
        cohort: sum(
            1
            for date in results
            if by_date[date]["cohort"] == cohort
        )
        for cohort in COHORTS
    }
    selected = candidates[0] if candidates else None
    development_complete = (
        completed_counts["Development"] == plan["sessionCounts"]["Development"]
    )
    validation_complete = (
        completed_counts["Validation"] == plan["sessionCounts"]["Validation"]
    )
    if not development_complete:
        status = "DEVELOPMENT_INCOMPLETE"
    elif not validation_complete:
        status = "VALIDATION_PENDING"
    elif selected and selected["validationStatus"] == "CONFIRMED":
        status = "VALIDATION_CONFIRMED_RESEARCH_ONLY"
    else:
        status = "NO_VALIDATED_CANDIDATE"
    return {
        "campaignVersion": plan["campaignVersion"],
        "campaignId": plan["campaignId"],
        "engineVersion": plan["engineVersion"],
        "jobId": plan["jobId"],
        "status": status,
        "completedSessionCounts": completed_counts,
        "plannedSessionCounts": plan["sessionCounts"],
        "selectionUsesOnly": "Development",
        "selectedCandidate": selected,
        "candidates": candidates,
        "lockedTestIncluded": include_locked_test,
        "lockedTestUsedForSelection": False,
        "automaticOrderExecution": False,
        "paperPromotionAllowed": False,
        "profitabilityClaim": False,
    }
