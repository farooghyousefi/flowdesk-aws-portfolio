from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .analyze import AnalyzeError, run_analyze
from .aws_clients import AwsClientError, create_aws_clients, load_databento_api_key
from .backtest_day import BacktestDayError, run_backtest_day
from .config import WorkerConfigError, load_worker_config
from .databento_api import DatabentoApiError, DatabentoBatchClient
from .ingest import IngestError, plan_ingest, run_ingest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="flowdesk-worker",
        description="Run one bounded Flowdesk cloud data job and exit.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    inspect_command = commands.add_parser(
        "inspect",
        help="Validate a completed Databento Batch job without downloading it or writing to S3.",
    )
    inspect_command.add_argument("--job-id", required=True)

    ingest_command = commands.add_parser(
        "ingest",
        help="Stream a completed Databento Batch job directly to verified S3 multipart objects.",
    )
    ingest_command.add_argument("--job-id", required=True)
    ingest_command.add_argument(
        "--confirm-job-id",
        required=True,
        help="Must exactly match --job-id; prevents an accidental task invocation.",
    )
    ingest_command.add_argument(
        "--request-fingerprint",
        help="Optional immutable Flowdesk estimate fingerprint for the final manifest.",
    )

    analyze_command = commands.add_parser(
        "analyze",
        help=(
            "Decode an already-ingested DBN file directly from S3 and report decode "
            "statistics. Read-only; writes nothing to S3."
        ),
    )
    analyze_command.add_argument("--job-id", required=True)

    backtest_command = commands.add_parser(
        "backtest-day",
        help=(
            "Run one read-only daily strategy backtest from an already-ingested S3 DBN "
            "object and save the research result to S3."
        ),
    )
    backtest_command.add_argument("--job-id", required=True)
    backtest_command.add_argument("--date", required=True)
    backtest_command.add_argument(
        "--confirm-date",
        required=True,
        help="Must exactly match --date; prevents an accidental session invocation.",
    )
    backtest_command.add_argument(
        "--fill-mode",
        choices=("optimistic", "realistic", "stressed"),
        default="realistic",
    )
    backtest_command.add_argument("--seed", type=int, default=7)
    return parser.parse_args(argv)


def _write_json(payload: dict[str, Any], *, stream: Any = sys.stdout) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str), file=stream, flush=True)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        config = load_worker_config(os.environ)
        aws = create_aws_clients(config)

        if args.command == "analyze":
            result = run_analyze(config, aws.s3, job_id=args.job_id, progress=_write_json)
            _write_json({"event": "RESULT", **result})
            return 0

        if args.command == "backtest-day":
            result = run_backtest_day(
                config,
                aws.s3,
                job_id=args.job_id,
                session_date=args.date,
                confirmed_session_date=args.confirm_date,
                scratch_directory=Path(
                    os.environ.get("FLOWDESK_SCRATCH_DIRECTORY") or "/tmp/flowdesk"
                ),
                fill_mode=args.fill_mode,
                seed=args.seed,
                progress=_write_json,
            )
            gate = result.get("realisticExecutionGate") or {}
            _write_json(
                {
                    "event": "RESULT",
                    "jobId": result["jobId"],
                    "sessionDate": result["sessionDate"],
                    "eventsProcessed": result["eventsProcessed"],
                    "topCandidate": (
                        result["topCandidates"][0]["strategyName"]
                        if result["topCandidates"]
                        else None
                    ),
                    "realisticGatePassed": bool(gate.get("passed")),
                    "realisticGateReason": gate.get("reason"),
                    "resultKey": result["resultKey"],
                    "profitabilityClaim": False,
                    "automaticOrderExecution": False,
                }
            )
            return 0

        api_key = load_databento_api_key(config, aws.ssm)
        client = DatabentoBatchClient(
            api_key,
            connect_timeout_seconds=config.http_connect_timeout_seconds,
            read_timeout_seconds=config.http_read_timeout_seconds,
        )

        if args.command == "inspect":
            _write_json({"event": "INSPECTION_COMPLETED", **plan_ingest(config, client, args.job_id).public()})
            return 0

        result = run_ingest(
            config,
            client,
            aws.s3,
            job_id=args.job_id,
            confirmed_job_id=args.confirm_job_id,
            request_fingerprint=args.request_fingerprint,
            progress=_write_json,
        )
        _write_json({"event": "RESULT", **result})
        return 0
    except (
        WorkerConfigError,
        AwsClientError,
        DatabentoApiError,
        IngestError,
        AnalyzeError,
        BacktestDayError,
    ) as exc:
        _write_json(
            {"event": "ERROR", "errorCode": type(exc).__name__, "message": str(exc)},
            stream=sys.stderr,
        )
        return 2
    except Exception:
        # Do not serialize raw exceptions: HTTP exceptions can contain signed
        # URLs and authentication context.
        _write_json(
            {
                "event": "ERROR",
                "errorCode": "UNEXPECTED_WORKER_ERROR",
                "message": "The Flowdesk worker failed unexpectedly; inspect the redacted task metrics.",
            },
            stream=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
