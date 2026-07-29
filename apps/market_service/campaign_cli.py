from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .research_campaign import (
    ResearchCampaignError,
    aggregate_campaign_results,
    build_campaign_plan,
)


REGION = "eu-central-1"
ALLOWED_RUN_COHORTS = ("Development", "Validation")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flowdesk-research-campaign",
        description="Plan and run bounded Flowdesk daily research without broker execution.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan")
    plan.add_argument("--job-id", required=True)
    plan.add_argument("--bucket", required=True)
    plan.add_argument("--profile", default="aws-course")
    plan.add_argument("--output", required=True, type=Path)
    plan.add_argument("--publish", action="store_true")

    status = commands.add_parser("status")
    status.add_argument("--plan", required=True, type=Path)
    status.add_argument("--bucket", required=True)
    status.add_argument("--profile", default="aws-course")
    status.add_argument("--output", type=Path)

    run = commands.add_parser("run")
    run.add_argument("--plan", required=True, type=Path)
    run.add_argument("--bucket", required=True)
    run.add_argument("--profile", default="aws-course")
    run.add_argument("--cluster", default="flowdesk")
    run.add_argument("--task-definition", required=True)
    run.add_argument("--subnet-id", required=True)
    run.add_argument("--security-group-id", required=True)
    run.add_argument("--cohort", choices=ALLOWED_RUN_COHORTS, required=True)
    run.add_argument("--concurrency", type=int, default=2)
    run.add_argument("--max-new-tasks", type=int, default=2)
    return parser


def _session(profile: str) -> Any:
    import boto3

    return boto3.Session(profile_name=profile, region_name=REGION)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except Exception as exc:
        raise ResearchCampaignError("The local campaign JSON could not be read.") from exc
    if not isinstance(value, dict):
        raise ResearchCampaignError("The local campaign JSON must contain one object.")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")


def _body_json(response: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(response["Body"].read())
    except Exception as exc:
        raise ResearchCampaignError("An S3 research object is not valid JSON.") from exc
    if not isinstance(value, dict):
        raise ResearchCampaignError("An S3 research object has an unexpected shape.")
    return value


def _not_found(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    error = response.get("Error") if isinstance(response, dict) else None
    return isinstance(error, dict) and error.get("Code") in {
        "404",
        "NoSuchKey",
        "NotFound",
    }


def _put_immutable_json(
    s3: Any,
    *,
    bucket: str,
    key: str,
    value: dict[str, Any],
) -> None:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    try:
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=encoded,
            ContentType="application/json",
            ServerSideEncryption="AES256",
            IfNoneMatch="*",
            Metadata={
                "campaign-id": str(value["campaignId"]),
                "engine-version": str(value["engineVersion"]),
            },
        )
    except Exception as exc:
        try:
            existing = _body_json(s3.get_object(Bucket=bucket, Key=key))
        except Exception:
            raise ResearchCampaignError("The immutable campaign plan could not be written.") from exc
        if existing != value:
            raise ResearchCampaignError("A conflicting immutable campaign plan already exists.") from exc


def _load_available_results(
    s3: Any,
    *,
    bucket: str,
    plan: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    results = []
    missing = []
    for row in plan["sessions"]:
        if row["cohort"] == "Locked Test":
            continue
        try:
            results.append(
                _body_json(
                    s3.get_object(
                        Bucket=bucket,
                        Key=row["resultKey"],
                    )
                )
            )
        except Exception as exc:
            if _not_found(exc):
                missing.append(row["sessionDate"])
                continue
            raise ResearchCampaignError("A daily result could not be checked in S3.") from exc
    return results, missing


def _validate_task_definition(ecs: Any, task_definition: str) -> str:
    response = ecs.describe_task_definition(taskDefinition=task_definition)
    definition = response["taskDefinition"]
    if definition.get("runtimePlatform", {}).get("cpuArchitecture") != "ARM64":
        raise ResearchCampaignError("The task definition must use ARM64.")
    containers = {
        container["name"]: container
        for container in definition.get("containerDefinitions", [])
    }
    worker = containers.get("flowdesk-worker")
    init = containers.get("scratch-init")
    if not worker or not init:
        raise ResearchCampaignError("The task definition is missing the worker or scratch initializer.")
    if "@sha256:" not in str(worker.get("image") or ""):
        raise ResearchCampaignError("The worker image must be pinned by digest.")
    if worker.get("readonlyRootFilesystem") is not True:
        raise ResearchCampaignError("The worker root filesystem must remain read-only.")
    if init.get("essential") is not False or str(init.get("user")) != "0":
        raise ResearchCampaignError("The scratch initializer must be isolated and nonessential.")
    dependencies = worker.get("dependsOn") or []
    if not any(
        row.get("containerName") == "scratch-init" and row.get("condition") == "SUCCESS"
        for row in dependencies
    ):
        raise ResearchCampaignError("The worker must wait for successful scratch initialization.")
    return str(definition["taskDefinitionArn"])


def _active_campaign_tasks(ecs: Any, *, cluster: str, campaign_id: str) -> int:
    arns: list[str] = []
    for status in ("PENDING", "RUNNING"):
        arns.extend(
            ecs.list_tasks(
                cluster=cluster,
                desiredStatus=status,
                family="flowdesk-worker",
            ).get("taskArns", [])
        )
    if not arns:
        return 0
    tasks = ecs.describe_tasks(cluster=cluster, tasks=arns).get("tasks", [])
    return sum(1 for task in tasks if task.get("startedBy") == campaign_id)


def _missing_sessions(
    s3: Any,
    *,
    bucket: str,
    plan: dict[str, Any],
    cohort: str,
) -> list[dict[str, Any]]:
    missing = []
    for row in plan["sessions"]:
        if row["cohort"] != cohort:
            continue
        try:
            s3.head_object(Bucket=bucket, Key=row["resultKey"])
        except Exception as exc:
            if _not_found(exc):
                missing.append(row)
                continue
            raise ResearchCampaignError("A planned result could not be checked in S3.") from exc
    return missing


def _run(args: argparse.Namespace) -> dict[str, Any]:
    if not 1 <= args.concurrency <= 2:
        raise ResearchCampaignError("Concurrency must remain between 1 and 2.")
    if not 1 <= args.max_new_tasks <= 2:
        raise ResearchCampaignError("At most two new tasks may be started at once.")
    plan = _read_json(args.plan)
    session = _session(args.profile)
    s3 = session.client("s3")
    ecs = session.client("ecs")
    task_definition_arn = _validate_task_definition(ecs, args.task_definition)
    active = _active_campaign_tasks(
        ecs,
        cluster=args.cluster,
        campaign_id=plan["campaignId"],
    )
    capacity = max(0, min(args.max_new_tasks, args.concurrency - active))
    missing = _missing_sessions(
        s3,
        bucket=args.bucket,
        plan=plan,
        cohort=args.cohort,
    )
    launched = []
    for row in missing[:capacity]:
        response = ecs.run_task(
            cluster=args.cluster,
            taskDefinition=task_definition_arn,
            launchType="FARGATE",
            count=1,
            startedBy=plan["campaignId"],
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": [args.subnet_id],
                    "securityGroups": [args.security_group_id],
                    "assignPublicIp": "ENABLED",
                }
            },
            overrides={
                "containerOverrides": [
                    {
                        "name": "flowdesk-worker",
                        "command": [
                            "backtest-day",
                            "--job-id",
                            plan["jobId"],
                            "--date",
                            row["sessionDate"],
                            "--confirm-date",
                            row["sessionDate"],
                            "--fill-mode",
                            "realistic",
                            "--seed",
                            "7",
                        ],
                    }
                ]
            },
            tags=[
                {"key": "Project", "value": "Flowdesk"},
                {"key": "Campaign", "value": plan["campaignId"]},
                {"key": "Cohort", "value": args.cohort.replace(" ", "-")},
                {"key": "SessionDate", "value": row["sessionDate"]},
            ],
        )
        failures = response.get("failures") or []
        tasks = response.get("tasks") or []
        if failures or len(tasks) != 1:
            raise ResearchCampaignError("ECS did not accept a planned daily research task.")
        launched.append(
            {
                "sessionDate": row["sessionDate"],
                "cohort": row["cohort"],
                "taskArn": tasks[0]["taskArn"],
            }
        )
    return {
        "campaignId": plan["campaignId"],
        "cohort": args.cohort,
        "activeBeforeLaunch": active,
        "concurrencyLimit": args.concurrency,
        "missingBeforeLaunch": len(missing),
        "launched": launched,
        "lockedTestStarted": False,
        "automaticOrderExecution": False,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "plan":
            session = _session(args.profile)
            s3 = session.client("s3")
            manifest_key = f"flowdesk/metadata/databento/jobs/{args.job_id.upper()}/manifest.json"
            manifest = _body_json(
                s3.get_object(Bucket=args.bucket, Key=manifest_key)
            )
            plan = build_campaign_plan(manifest, job_id=args.job_id)
            _write_json(args.output, plan)
            if args.publish:
                key = (
                    f"flowdesk/research/campaigns/{plan['jobId']}/"
                    f"{plan['campaignId']}/plan.json"
                )
                _put_immutable_json(s3, bucket=args.bucket, key=key, value=plan)
                plan = {**plan, "publishedPlanKey": key}
            print(json.dumps(plan, indent=2, sort_keys=True))
            return 0

        if args.command == "status":
            plan = _read_json(args.plan)
            s3 = _session(args.profile).client("s3")
            results, missing = _load_available_results(
                s3,
                bucket=args.bucket,
                plan=plan,
            )
            aggregate = aggregate_campaign_results(plan, results)
            value = {**aggregate, "missingUnlockedSessions": missing}
            if args.output:
                _write_json(args.output, value)
            print(json.dumps(value, indent=2, sort_keys=True))
            return 0

        print(json.dumps(_run(args), indent=2, sort_keys=True))
        return 0
    except ResearchCampaignError as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
