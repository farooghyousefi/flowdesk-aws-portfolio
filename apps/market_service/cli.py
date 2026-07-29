from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import resource
import socket
import sqlite3
import sys
import time
from pathlib import Path

from apps.connectors.databento.src.config import ENV_FILE, REPO_ROOT
from apps.connectors.databento.src.dbn_reader import OrderBook, iter_events
from apps.connectors.databento.src.validate import validate_file
from .backtest_protocol import conservative_report, create_plan, protocol_status, scan_session
from .importer import import_discovered, import_file
from .planner import (
    MODE_SPECS,
    build_time_window,
    download_ready_job,
    estimate_plan,
    optimize_plan,
    refresh_jobs,
    submit_purchase,
)
from .replay import ReplayEngine
from .storage import DUCKDB_PATH, SQLITE_PATH, ensure_directories, list_dataset_jobs, list_sessions, migrate, session_library

DEMO_FILE = REPO_ROOT / "data" / "databento" / "raw" / "MES" / "2026-07-13" / "MES.v.0_mbo_20260713T235955Z_20260714T000010Z.dbn.zst"
PARTIAL_FILE = REPO_ROOT / "data" / "databento" / "raw" / "MES" / "2026-07-14" / "MES.v.0_mbo_20260714T133000Z_20260714T134000Z.dbn.zst"


def setup_local() -> int:
    ensure_directories()
    migrate()
    sessions = import_discovered()
    print("LOCAL SETUP COMPLETE")
    print(f"SQLite: {SQLITE_PATH}")
    print(f"DuckDB: {DUCKDB_PATH}")
    print(f"Registered sessions: {len(sessions)}")
    return 0


def run_import(file_arg: str | None) -> int:
    sessions = [import_file(file_arg)] if file_arg else import_discovered()
    for session in sessions:
        print(f"IMPORTED {session['id']} {session['contract_symbol']} {session['completeness'].upper()} {session['record_count']} records")
    return 0


def demo() -> int:
    session = import_file(str(DEMO_FILE))
    engine = ReplayEngine()
    state = engine.load(session["id"])
    print("REPLAY DEMO READY")
    print(f"Session: {session['id']} · {session['contract_symbol']} · COMPLETE BOOK")
    print(f"Events: {state['eventCount']} · paused at {state['timestamp']}")
    print("Browser: http://localhost:3000/replay")
    return 0


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


def doctor() -> int:
    checks: list[tuple[str, str, str]] = []
    checks.append(("PASS", "Python", platform.python_version()))
    expected_venv = (REPO_ROOT / ".venv").resolve()
    checks.append(("PASS" if Path(sys.prefix).resolve() == expected_venv else "WARNING", "venv", sys.executable))
    for module in ("databento", "fastapi", "uvicorn", "duckdb", "pyarrow"):
        checks.append(("PASS" if importlib.util.find_spec(module) else "FAIL", module, "installed" if importlib.util.find_spec(module) else "run npm run local:setup"))
    env_mode = oct(ENV_FILE.stat().st_mode & 0o777) if ENV_FILE.exists() else "missing"
    checks.append(("PASS" if env_mode == "0o600" else "FAIL", ".env.local permissions", env_mode))
    key_present = False
    if ENV_FILE.exists():
        key_present = any(line.startswith("DATABENTO_API_KEY=") and line.strip() != "DATABENTO_API_KEY=" for line in ENV_FILE.read_text().splitlines())
    checks.append(("PASS" if key_present else "FAIL", "Databento key", "configured and redacted" if key_present else "missing"))
    checks.append(("PASS" if DEMO_FILE.is_file() else "FAIL", "complete demo DBN", str(DEMO_FILE)))
    checks.append(("PASS" if PARTIAL_FILE.is_file() else "WARNING", "partial demo DBN", str(PARTIAL_FILE)))
    try:
        migrate()
        sqlite3.connect(SQLITE_PATH).execute("SELECT 1").fetchone()
        checks.append(("PASS", "SQLite", str(SQLITE_PATH)))
    except Exception as exc:
        checks.append(("FAIL", "SQLite", type(exc).__name__))
    checks.append(("PASS" if DUCKDB_PATH.exists() else "WARNING", "DuckDB", str(DUCKDB_PATH)))
    for port in (3000, 8787):
        checks.append(("PASS" if _port_available(port) else "WARNING", f"port {port}", "available" if _port_available(port) else "already in use; running service may be expected"))
    sessions = list_sessions()
    checks.append(("PASS" if any(item["completeness"] == "complete" for item in sessions) else "FAIL", "complete session registry", f"{len(sessions)} sessions"))
    checks.append(("PASS" if (REPO_ROOT / "apps" / "web" / ".next").exists() else "WARNING", "frontend build", "present" if (REPO_ROOT / "apps" / "web" / ".next").exists() else "run npm run build"))
    for status, name, detail in checks:
        print(f"{status:<7} {name}: {detail}")
    return 1 if any(status == "FAIL" for status, _, _ in checks) else 0


def benchmark(file_arg: str | None) -> int:
    path = Path(file_arg).resolve() if file_arg else PARTIAL_FILE
    book = OrderBook()
    count = 0
    started = time.perf_counter()
    for event in iter_events(path):
        book.apply(event)
        count += 1
    elapsed = max(time.perf_counter() - started, 0.000001)
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak = rss / (1024 * 1024) if rss > 10_000_000 else rss / 1024
    print("MARKET BENCHMARK")
    print(f"File: {path}")
    print(f"Records: {count}")
    print(f"Elapsed: {elapsed:.3f} s")
    print(f"Records per second: {count / elapsed:,.0f}")
    print(f"Peak RSS: {peak:.1f} MB")
    print(f"Snapshot status: {book.snapshot_status.value}")
    print(f"Book completeness: {'COMPLETE' if book.is_snapshot_ready else 'PARTIAL'}")
    return 0


def _planner_payload(args: argparse.Namespace) -> dict[str, object]:
    return {
        "market": "MES", "dataset": "GLBX.MDP3", "symbol": "MES.v.0",
        "date": args.date, "timezone": args.timezone,
        "replayStart": args.replay_start, "replayEnd": args.replay_end,
        "contextMinutes": args.context_minutes, "days": 1,
    }


def dataset_plan(args: argparse.Namespace) -> int:
    payload = _planner_payload(args)
    window = build_time_window(payload)
    result = {
        "input": payload,
        "berlin": {"start": window["replay_start_local"], "end": window["replay_end_local"]},
        "utc": {"start": window["replay_start_utc"], "end": window["replay_end_utc"]},
        "modes": [
            {"key": spec.key, "label": spec.label, "schemas": spec.schemas, "scope": spec.request_scope}
            for spec in MODE_SPECS.values()
        ],
        "downloadStarted": False,
    }
    print(json.dumps(result, indent=2, default=str))
    return 0


def dataset_estimate(args: argparse.Namespace, *, optimize: bool = False) -> int:
    result = optimize_plan(_planner_payload(args)) if optimize else estimate_plan(_planner_payload(args))
    print(json.dumps(result, indent=2, default=str))
    return 0


def dataset_submit(args: argparse.Namespace) -> int:
    result = submit_purchase(
        args.estimate_id, acknowledged=args.acknowledge, confirmation=args.confirmation,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0


def dataset_download(args: argparse.Namespace) -> int:
    print(json.dumps(download_ready_job(args.job_id), indent=2, default=str))
    return 0


def dataset_validate(args: argparse.Namespace) -> int:
    summary, errors = validate_file(Path(args.file).resolve())
    print(json.dumps({"summary": summary.__dict__, "errors": errors, "valid": not errors}, indent=2, default=str))
    return 1 if errors else 0


def dataset_list() -> int:
    print(json.dumps({"sessions": session_library(), "jobs": list_dataset_jobs()}, indent=2, default=str))
    return 0


def backtest_plan_cli(args: argparse.Namespace) -> int:
    session_ids = args.session_id or [item["id"] for item in list_sessions() if item["completeness"] == "complete"][:1]
    result = create_plan({
        "strategy": args.strategy, "instrument": "MES", "sessionIds": session_ids,
        "mode": args.mode, "startingBalance": args.starting_balance,
        "riskPerTrade": args.risk_per_trade, "maximumTradesPerDay": args.max_trades,
        "requireFullL3": True,
    })
    print(json.dumps(result, indent=2, default=str))
    return 0


def backtest_scan_cli(args: argparse.Namespace) -> int:
    print(json.dumps(scan_session(args.session_id, args.plan_id), indent=2, default=str))
    return 0


def _add_planner_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--date", required=True)
    parser.add_argument("--timezone", default="Europe/Berlin")
    parser.add_argument("--replay-start", default="15:00")
    parser.add_argument("--replay-end", default="16:30")
    parser.add_argument("--context-minutes", type=int, default=30)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local Flowdesk service utilities.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup")
    importer = sub.add_parser("import")
    importer.add_argument("--file")
    sub.add_parser("demo")
    sub.add_parser("doctor")
    benchmarker = sub.add_parser("benchmark")
    benchmarker.add_argument("--file")
    for name in ("dataset-plan", "dataset-estimate", "dataset-optimize"):
        _add_planner_args(sub.add_parser(name))
    submitter = sub.add_parser("dataset-submit")
    submitter.add_argument("--estimate-id", required=True)
    submitter.add_argument("--acknowledge", action="store_true")
    submitter.add_argument("--confirmation", required=True)
    sub.add_parser("dataset-jobs")
    downloader = sub.add_parser("dataset-download")
    downloader.add_argument("--job-id", required=True)
    validator = sub.add_parser("dataset-validate")
    validator.add_argument("--file", required=True)
    dataset_importer = sub.add_parser("dataset-import")
    dataset_importer.add_argument("--file")
    sub.add_parser("dataset-list")
    planner = sub.add_parser("backtest-plan")
    planner.add_argument("--mode", choices=("practice", "pilot", "locked"), default="practice")
    planner.add_argument("--strategy", default="MES Pullback / Retest")
    planner.add_argument("--session-id", action="append")
    planner.add_argument("--starting-balance", type=float, default=50_000)
    planner.add_argument("--risk-per-trade", type=float, default=75)
    planner.add_argument("--max-trades", type=int, default=3)
    scanner = sub.add_parser("backtest-scan")
    scanner.add_argument("--session-id", required=True)
    scanner.add_argument("--plan-id")
    reporter = sub.add_parser("backtest-report")
    reporter.add_argument("--plan-id")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "setup": return setup_local()
    if args.command == "import": return run_import(args.file)
    if args.command == "demo": return demo()
    if args.command == "doctor": return doctor()
    if args.command == "benchmark": return benchmark(args.file)
    if args.command == "dataset-plan": return dataset_plan(args)
    if args.command == "dataset-estimate": return dataset_estimate(args)
    if args.command == "dataset-optimize": return dataset_estimate(args, optimize=True)
    if args.command == "dataset-submit": return dataset_submit(args)
    if args.command == "dataset-jobs": print(json.dumps(refresh_jobs(), indent=2, default=str)); return 0
    if args.command == "dataset-download": return dataset_download(args)
    if args.command == "dataset-validate": return dataset_validate(args)
    if args.command == "dataset-import": return run_import(args.file)
    if args.command == "dataset-list": return dataset_list()
    if args.command == "backtest-plan": return backtest_plan_cli(args)
    if args.command == "backtest-scan": return backtest_scan_cli(args)
    if args.command == "backtest-report": print(json.dumps(conservative_report(args.plan_id), indent=2, default=str)); return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
