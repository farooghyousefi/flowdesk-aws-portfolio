from __future__ import annotations

import hashlib

import pytest

from apps.market_service.research_campaign import (
    DAILY_ENGINE_VERSION,
    ResearchCampaignError,
    aggregate_campaign_results,
    build_campaign_plan,
)


JOB_ID = "GLBX-20260723-4BH5UYFQSY"


def source_manifest(count: int = 20) -> dict:
    files = []
    for day in range(1, count + 1):
        session_date = f"2026-05-{day:02d}"
        filename = f"glbx-mdp3-{session_date.replace('-', '')}.mbo.dbn.zst"
        data = f"source-{session_date}".encode()
        files.append(
            {
                "filename": filename,
                "s3Key": f"flowdesk/raw/databento/jobs/{JOB_ID}/{filename}",
                "sizeBytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return {"status": "COMPLETED", "jobId": JOB_ID, "files": files}


def daily_result(
    planned: dict,
    *,
    candidate_one: list[float],
    candidate_two: list[float],
) -> dict:
    def fast(index: int) -> dict:
        return {
            "candidateIndex": index,
            "family": f"FAMILY_{index}",
            "strategyName": f"Candidate {index}",
            "parameters": {"stopTicks": index + 2, "targetTicks": index + 4},
            "metrics": {"trades": 1},
            "segmentMetrics": {},
            "regimeMetrics": {},
            "rankingScore": 0,
            "paperEligible": False,
            "paperFailedReasons": [],
            "diagnosis": [],
            "tradeEvidence": [],
        }

    def realistic(index: int, outcomes: list[float]) -> dict:
        return {
            "candidateIndex": index,
            "strategyName": f"Candidate {index}",
            "evaluated": bool(outcomes),
            "passed": False,
            "reason": "RESEARCH_ONLY",
            "fastTrades": len(outcomes),
            "realisticTrades": len(outcomes),
            "retention": 1.0 if outcomes else 0.0,
            "tradeEvidence": [
                {
                    "direction": "long",
                    "entryTimestampNs": position + 1,
                    "exitTimestampNs": position + 2,
                    "holdingSeconds": 1,
                    "grossUsd": outcome + 2.2,
                    "costUsd": 2.2,
                    "netUsd": outcome,
                    "resultR": outcome / 10,
                }
                for position, outcome in enumerate(outcomes)
            ],
        }

    return {
        "sessionDate": planned["sessionDate"],
        "sourceFingerprint": planned["sourceFingerprint"],
        "engineVersion": DAILY_ENGINE_VERSION,
        "candidateEvidence": [fast(1), fast(2)],
        "realisticCandidateEvidence": [
            realistic(1, candidate_one),
            realistic(2, candidate_two),
        ],
        "automaticOrderExecution": False,
    }


def test_campaign_plan_freezes_chronological_cohorts_before_results() -> None:
    plan = build_campaign_plan(source_manifest(), job_id=JOB_ID)

    assert plan["sessionCounts"] == {
        "Development": 12,
        "Validation": 4,
        "Locked Test": 4,
    }
    assert [row["cohort"] for row in plan["sessions"][:12]] == ["Development"] * 12
    assert [row["cohort"] for row in plan["sessions"][12:16]] == ["Validation"] * 4
    assert [row["cohort"] for row in plan["sessions"][16:]] == ["Locked Test"] * 4
    assert plan["cohortPolicy"]["selectionUsesOnly"] == ["Development"]
    assert plan["cohortPolicy"]["lockedTestExecution"] == "WITHHELD_UNTIL_STRATEGY_FREEZE"
    assert plan["profitabilityClaim"] is False
    assert plan == build_campaign_plan(source_manifest(), job_id=JOB_ID)


def test_campaign_selection_uses_development_not_better_validation_result() -> None:
    plan = build_campaign_plan(source_manifest(), job_id=JOB_ID)
    results = []
    for planned in plan["sessions"][:16]:
        if planned["cohort"] == "Development":
            results.append(
                daily_result(planned, candidate_one=[10.0, 10.0], candidate_two=[-5.0, -5.0])
            )
        else:
            results.append(
                daily_result(planned, candidate_one=[-10.0], candidate_two=[100.0])
            )

    aggregate = aggregate_campaign_results(plan, results)

    assert aggregate["selectedCandidate"]["candidateIndex"] == 1
    assert aggregate["selectionUsesOnly"] == "Development"
    assert aggregate["lockedTestUsedForSelection"] is False
    assert aggregate["profitabilityClaim"] is False


def test_campaign_rejects_locked_test_result_before_explicit_unlock() -> None:
    plan = build_campaign_plan(source_manifest(), job_id=JOB_ID)
    locked = next(row for row in plan["sessions"] if row["cohort"] == "Locked Test")

    with pytest.raises(ResearchCampaignError, match="Locked Test results"):
        aggregate_campaign_results(
            plan,
            [daily_result(locked, candidate_one=[10.0], candidate_two=[5.0])],
        )


def test_campaign_rejects_candidate_parameter_drift() -> None:
    plan = build_campaign_plan(source_manifest(), job_id=JOB_ID)
    first = daily_result(plan["sessions"][0], candidate_one=[10.0], candidate_two=[5.0])
    second = daily_result(plan["sessions"][1], candidate_one=[10.0], candidate_two=[5.0])
    second["candidateEvidence"][0]["parameters"]["stopTicks"] = 999

    with pytest.raises(ResearchCampaignError, match="parameters changed"):
        aggregate_campaign_results(plan, [first, second])
