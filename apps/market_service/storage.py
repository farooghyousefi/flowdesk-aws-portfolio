from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from apps.connectors.databento.src.config import REPO_ROOT

APP_ROOT = Path(os.environ.get("FLOWDESK_APP_ROOT", REPO_ROOT / "data" / "app")).expanduser().resolve()
JOURNAL_ROOT = REPO_ROOT / "data" / "journal"
DERIVED_ROOT = REPO_ROOT / "data" / "derived"
SQLITE_PATH = APP_ROOT / "trading-assistant.sqlite3"
DUCKDB_PATH = APP_ROOT / "market.duckdb"
QA_STRATEGY_HASH = "b448776be63828d3434f50007e424dc8ebf73aba1b6d13b9f9ea8a1674ac95a2"

DEFAULT_SETTINGS: dict[str, Any] = {
    "data": {
        "dataset": "GLBX.MDP3",
        "schema": "mbo",
        "symbol": "MES.v.0",
        "instrument": "MES",
        "importDirectory": str(REPO_ROOT / "data" / "databento" / "raw" / "MES"),
        "maxRequestCostUsd": 1.0,
        "maxDailyCostUsd": 5.0,
        "liveEnabled": False,
    },
    "replay": {"defaultSpeed": "1", "uiRefreshRate": 20, "checkpointIntervalSeconds": 5},
    "orderflow": {
        "largeTradeThreshold": 10,
        "imbalanceRatio": 3.0,
        "stackedImbalanceCount": 3,
        "absorptionWindowSeconds": 3,
        "absorptionMinimumObservations": 3,
        "absorptionMinimumElapsedMs": 500,
        "absorptionMinimumAggressiveVolume": 20,
        "absorptionCandidateLimit": 5,
        "replenishmentThreshold": 3,
        "pullingStackingWindowSeconds": 2,
        "heatmapNormalization": "local_percentile",
    },
    "risk": {
        "accountType": "FundedNext Futures Flex 50K",
        "accountSize": 50000,
        "profitTarget": 2500,
        "maximumLossEod": 1500,
        "consistencyTarget": 0.4,
        "maxMiniContracts": 3,
        "maxMicroContracts": 30,
        "maxRiskPerTrade": 75,
        "maxDailyLoss": 150,
        "maxTrades": 3,
        "cooldownMinutes": 20,
        "consecutiveLossLimit": 2,
        "manualDayPnl": 0,
        "manualTotalPnl": 0,
        "openRiskUsd": 0,
        "drawdownMode": "trailing",
        "minimumTradingDays": 5,
        "maximumTradesPerDay": 3,
        "allowedTradingStart": "15:00",
        "allowedTradingEnd": "22:00",
        "newsTradingAllowed": False,
        "overnightHoldingAllowed": False,
        "allowedInstruments": ["MES"],
        "consistencyRule": 0.4,
        "dailyStopAfterLosses": 2,
        "scalingRules": "fixed_contract_cap",
    },
    "ui": {"language": "de"},
    "ai": {"provider": "disabled", "model": "", "enabled": False, "explanationStyle": "concise"},
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _merge_defaults(defaults: dict[str, Any], stored: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(defaults))
    for key, value in stored.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_defaults(merged[key], value)
        else:
            merged[key] = value
    return merged


def ensure_directories() -> None:
    for path in (
        APP_ROOT,
        JOURNAL_ROOT,
        DERIVED_ROOT / "bars",
        DERIVED_ROOT / "footprint",
        DERIVED_ROOT / "orderbook",
        DERIVED_ROOT / "trades",
        DERIVED_ROOT / "features",
        REPO_ROOT / "data" / "databento" / "reference",
        REPO_ROOT / "data" / "databento" / "reports",
    ):
        path.mkdir(parents=True, exist_ok=True)


def connect() -> sqlite3.Connection:
    ensure_directories()
    connection = sqlite3.connect(SQLITE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def migrate() -> None:
    with connect() as database:
        database.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
              id TEXT PRIMARY KEY,
              instrument TEXT NOT NULL,
              symbol TEXT NOT NULL,
              contract_symbol TEXT,
              instrument_id INTEGER NOT NULL,
              start_at TEXT NOT NULL,
              end_at TEXT NOT NULL,
              record_count INTEGER NOT NULL,
              snapshot_status TEXT NOT NULL,
              completeness TEXT NOT NULL,
              file_path TEXT NOT NULL UNIQUE,
              sha256 TEXT NOT NULL,
              imported_at TEXT NOT NULL,
              integrity_status TEXT NOT NULL,
              unknown_pre INTEGER NOT NULL DEFAULT 0,
              unknown_during INTEGER NOT NULL DEFAULT 0,
              unknown_post INTEGER NOT NULL DEFAULT 0,
              sequence_regressions INTEGER NOT NULL DEFAULT 0,
              processing_rate REAL NOT NULL DEFAULT 0,
              peak_rss_mb REAL NOT NULL DEFAULT 0,
              derived_manifest TEXT NOT NULL DEFAULT '{}',
              external_verification TEXT NOT NULL DEFAULT 'external_verification_pending'
            );
            CREATE TABLE IF NOT EXISTS settings (
              id INTEGER PRIMARY KEY CHECK (id = 1),
              payload TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS journal_entries (
              id TEXT PRIMARY KEY,
              session_id TEXT,
              trade_date TEXT NOT NULL,
              session_name TEXT NOT NULL,
              symbol TEXT NOT NULL,
              direction TEXT NOT NULL,
              setup TEXT NOT NULL,
              entry_price REAL NOT NULL,
              stop_price REAL NOT NULL,
              targets_json TEXT NOT NULL,
              exit_price REAL,
              contracts INTEGER NOT NULL,
              risk_usd REAL NOT NULL,
              result_usd REAL,
              result_r REAL,
              screenshot_path TEXT,
              decision_snapshot TEXT NOT NULL,
              risk_snapshot TEXT NOT NULL,
              market_context TEXT NOT NULL,
              notes TEXT NOT NULL,
              emotion TEXT NOT NULL,
              mistake_tags TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS data_estimates (
              id TEXT PRIMARY KEY,
              request_fingerprint TEXT NOT NULL UNIQUE,
              dataset TEXT NOT NULL,
              mode TEXT NOT NULL,
              schemas_json TEXT NOT NULL,
              input_symbol TEXT NOT NULL,
              raw_symbol TEXT NOT NULL,
              instrument_id INTEGER NOT NULL,
              start_utc TEXT NOT NULL,
              end_utc TEXT NOT NULL,
              replay_start TEXT NOT NULL,
              replay_end TEXT NOT NULL,
              timezone TEXT NOT NULL,
              estimated_cost REAL NOT NULL,
              estimated_records INTEGER NOT NULL,
              billable_bytes INTEGER NOT NULL,
              unit_price_json TEXT NOT NULL,
              local_reuse INTEGER NOT NULL DEFAULT 0,
              allowed INTEGER NOT NULL,
              confidence TEXT NOT NULL,
              warnings_json TEXT NOT NULL,
              metadata_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              status TEXT NOT NULL,
              job_id TEXT,
              actual_local_size INTEGER,
              downloaded_at TEXT
            );
            CREATE TABLE IF NOT EXISTS dataset_jobs (
              id TEXT PRIMARY KEY,
              estimate_id TEXT NOT NULL,
              schema_name TEXT NOT NULL,
              remote_job_id TEXT,
              status TEXT NOT NULL,
              details_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(estimate_id, schema_name),
              FOREIGN KEY(estimate_id) REFERENCES data_estimates(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS download_authorizations (
              id TEXT PRIMARY KEY,
              estimate_id TEXT NOT NULL UNIQUE,
              idempotency_key TEXT NOT NULL UNIQUE,
              request_fingerprint TEXT NOT NULL,
              mode TEXT NOT NULL,
              state TEXT NOT NULL,
              accepted_terms INTEGER NOT NULL,
              confirmation_phrase TEXT NOT NULL,
              authorization_amount TEXT NOT NULL,
              displayed_authorization_amount TEXT NOT NULL,
              execution_mode TEXT NOT NULL,
              error_code TEXT,
              error_message TEXT,
              retry_safe INTEGER NOT NULL DEFAULT 0,
              recovered INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              authorized_at TEXT,
              FOREIGN KEY(estimate_id) REFERENCES data_estimates(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS authorization_ledger (
              id TEXT PRIMARY KEY,
              authorization_id TEXT NOT NULL UNIQUE,
              estimate_id TEXT NOT NULL UNIQUE,
              amount TEXT NOT NULL,
              state TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(authorization_id) REFERENCES download_authorizations(id) ON DELETE CASCADE,
              FOREIGN KEY(estimate_id) REFERENCES data_estimates(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS download_authorizations_state_idx
              ON download_authorizations(state, created_at DESC);
            CREATE TABLE IF NOT EXISTS session_splits (
              session_id TEXT PRIMARY KEY,
              split_name TEXT NOT NULL DEFAULT 'Development',
              reason TEXT NOT NULL DEFAULT '',
              locked INTEGER NOT NULL DEFAULT 0,
              viewed_at TEXT,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS backtest_plans (
              id TEXT PRIMARY KEY,
              mode TEXT NOT NULL,
              strategy TEXT NOT NULL,
              config_json TEXT NOT NULL,
              session_ids_json TEXT NOT NULL,
              strategy_hash TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              locked_at TEXT
            );
            CREATE TABLE IF NOT EXISTS audit_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              plan_id TEXT,
              session_id TEXT,
              event_type TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(plan_id) REFERENCES backtest_plans(id) ON DELETE CASCADE,
              FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS blind_trades (
              id TEXT PRIMARY KEY,
              plan_id TEXT NOT NULL,
              session_id TEXT NOT NULL,
              journal_id TEXT,
              opened_at TEXT NOT NULL,
              closed_at TEXT,
              direction TEXT NOT NULL,
              entry REAL NOT NULL,
              stop REAL NOT NULL,
              targets_json TEXT NOT NULL,
              result_r REAL,
              result_usd REAL,
              mae REAL,
              mfe REAL,
              holding_seconds REAL,
              fees_usd REAL NOT NULL DEFAULT 0,
              slippage_usd REAL NOT NULL DEFAULT 0,
              decision_snapshot TEXT NOT NULL,
              risk_snapshot TEXT NOT NULL,
              features_snapshot TEXT NOT NULL,
              status TEXT NOT NULL,
              immutable INTEGER NOT NULL DEFAULT 0,
              FOREIGN KEY(plan_id) REFERENCES backtest_plans(id) ON DELETE CASCADE,
              FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE RESTRICT,
              FOREIGN KEY(journal_id) REFERENCES journal_entries(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS scan_candidates (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              plan_id TEXT,
              session_id TEXT NOT NULL,
              timestamp TEXT NOT NULL,
              timestamp_ns TEXT NOT NULL,
              decision_state TEXT NOT NULL,
              direction TEXT,
              confidence REAL NOT NULL,
              data_quality TEXT NOT NULL,
              reasons_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(plan_id) REFERENCES backtest_plans(id) ON DELETE CASCADE,
              FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS external_book_verifications (
              session_id TEXT PRIMARY KEY,
              mbo_file_hash TEXT NOT NULL,
              status TEXT NOT NULL,
              compared_groups INTEGER,
              bbo_matches INTEGER,
              top10_matches INTEGER,
              mismatches INTEGER,
              report_path TEXT,
              verified_at TEXT,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS planner_state (
              id INTEGER PRIMARY KEY CHECK (id = 1),
              request_plan_json TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS plan_session_assignments (
              plan_id TEXT NOT NULL,
              session_id TEXT NOT NULL,
              split_name TEXT NOT NULL,
              assignment_type TEXT NOT NULL DEFAULT 'primary',
              reused INTEGER NOT NULL DEFAULT 0,
              contaminated INTEGER NOT NULL DEFAULT 0,
              ui_practice_only INTEGER NOT NULL DEFAULT 0,
              source_plan_id TEXT,
              created_at TEXT NOT NULL,
              PRIMARY KEY(plan_id, session_id),
              FOREIGN KEY(plan_id) REFERENCES backtest_plans(id) ON DELETE CASCADE,
              FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE RESTRICT,
              FOREIGN KEY(source_plan_id) REFERENCES backtest_plans(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS backtest_runs (
              id TEXT PRIMARY KEY,
              plan_id TEXT NOT NULL,
              session_id TEXT NOT NULL,
              mode TEXT NOT NULL,
              status TEXT NOT NULL,
              started_at TEXT NOT NULL,
              ended_at TEXT,
              FOREIGN KEY(plan_id) REFERENCES backtest_plans(id) ON DELETE RESTRICT,
              FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE RESTRICT
            );
            CREATE TABLE IF NOT EXISTS application_state (
              id INTEGER PRIMARY KEY CHECK (id = 1),
              active_plan_id TEXT,
              active_run_id TEXT,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(active_plan_id) REFERENCES backtest_plans(id) ON DELETE SET NULL,
              FOREIGN KEY(active_run_id) REFERENCES backtest_runs(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS estimate_jobs (
              id TEXT PRIMARY KEY,
              request_fingerprint TEXT NOT NULL,
              request_json TEXT NOT NULL,
              job_kind TEXT NOT NULL,
              status TEXT NOT NULL,
              progress REAL NOT NULL DEFAULT 0,
              checkpoint_json TEXT NOT NULL DEFAULT '{}',
              result_json TEXT,
              error_code TEXT,
              error_message TEXT,
              retry_of TEXT,
              created_at TEXT NOT NULL,
              started_at TEXT,
              completed_at TEXT,
              expires_at TEXT NOT NULL,
              cancelled_at TEXT,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(retry_of) REFERENCES estimate_jobs(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS estimate_jobs_fingerprint_idx
              ON estimate_jobs(request_fingerprint, created_at DESC);
            CREATE TABLE IF NOT EXISTS range_plans (
              id TEXT PRIMARY KEY,
              request_fingerprint TEXT NOT NULL UNIQUE,
              request_json TEXT NOT NULL,
              estimate_ids_json TEXT NOT NULL DEFAULT '[]',
              summary_json TEXT NOT NULL DEFAULT '{}',
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS range_plans_status_idx
              ON range_plans(status, created_at DESC);
            CREATE TABLE IF NOT EXISTS research_jobs (
              id TEXT PRIMARY KEY,
              experiment_id TEXT,
              session_id TEXT NOT NULL,
              status TEXT NOT NULL,
              progress REAL NOT NULL DEFAULT 0,
              checkpoint_json TEXT NOT NULL DEFAULT '{}',
              config_json TEXT NOT NULL,
              result_json TEXT,
              error_message TEXT,
              created_at TEXT NOT NULL,
              started_at TEXT,
              completed_at TEXT,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE RESTRICT
            );
            CREATE INDEX IF NOT EXISTS research_jobs_session_idx
              ON research_jobs(session_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS experiments (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              strategy_name TEXT NOT NULL,
              strategy_hash TEXT NOT NULL,
              parameter_hash TEXT NOT NULL,
              dataset_fingerprint TEXT NOT NULL,
              split_name TEXT NOT NULL,
              seed INTEGER NOT NULL,
              fill_model_version TEXT NOT NULL,
              cost_model_version TEXT NOT NULL,
              feature_version TEXT NOT NULL,
              code_version TEXT NOT NULL,
              status TEXT NOT NULL,
              config_json TEXT NOT NULL,
              metrics_json TEXT NOT NULL DEFAULT '{}',
              validation_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS strategy_versions (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              version TEXT NOT NULL,
              strategy_hash TEXT NOT NULL UNIQUE,
              status TEXT NOT NULL,
              validation_status TEXT NOT NULL,
              config_json TEXT NOT NULL,
              data_fingerprints_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              promoted_at TEXT,
              rejected_at TEXT
            );
            CREATE TABLE IF NOT EXISTS model_versions (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              version TEXT NOT NULL,
              model_type TEXT NOT NULL,
              status TEXT NOT NULL,
              calibration_json TEXT NOT NULL DEFAULT '{}',
              feature_version TEXT NOT NULL,
              created_at TEXT NOT NULL,
              promoted_at TEXT
            );
            CREATE TABLE IF NOT EXISTS signal_snapshots (
              id TEXT PRIMARY KEY,
              session_id TEXT,
              run_id TEXT,
              timestamp TEXT NOT NULL,
              status TEXT NOT NULL,
              strategy_version TEXT NOT NULL,
              model_version TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              signature TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(session_id, run_id, timestamp, signature),
              FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE SET NULL,
              FOREIGN KEY(run_id) REFERENCES backtest_runs(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS signal_snapshots_time_idx
              ON signal_snapshots(timestamp DESC);
            """
        )
        session_columns = {row["name"] for row in database.execute("PRAGMA table_info(sessions)")}
        session_additions = {
            "dataset_name": "TEXT NOT NULL DEFAULT 'GLBX.MDP3'",
            "schema_name": "TEXT NOT NULL DEFAULT 'mbo'",
            "sequence_gaps": "INTEGER NOT NULL DEFAULT 0",
            "out_of_order_events": "INTEGER NOT NULL DEFAULT 0",
            "duplicate_events": "INTEGER NOT NULL DEFAULT 0",
            "contract_mapping_status": "TEXT NOT NULL DEFAULT 'resolved'",
        }
        for column, definition in session_additions.items():
            if column not in session_columns:
                database.execute(f"ALTER TABLE sessions ADD COLUMN {column} {definition}")
        dataset_job_columns = {row["name"] for row in database.execute("PRAGMA table_info(dataset_jobs)")}
        dataset_job_additions = {
            "authorization_id": "TEXT",
            "progress": "REAL NOT NULL DEFAULT 0",
            "error_code": "TEXT",
            "error_message": "TEXT",
            "actual_cost": "TEXT",
            "download_bytes": "INTEGER",
            "downloaded_at": "TEXT",
            "charged_at": "TEXT",
        }
        for column, definition in dataset_job_additions.items():
            if column not in dataset_job_columns:
                database.execute(f"ALTER TABLE dataset_jobs ADD COLUMN {column} {definition}")
        estimate_job_columns = {row["name"] for row in database.execute("PRAGMA table_info(estimate_jobs)")}
        estimate_job_additions = {
            "progress": "REAL NOT NULL DEFAULT 0",
            "checkpoint_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for column, definition in estimate_job_additions.items():
            if column not in estimate_job_columns:
                database.execute(f"ALTER TABLE estimate_jobs ADD COLUMN {column} {definition}")
        plan_columns = {row["name"] for row in database.execute("PRAGMA table_info(backtest_plans)")}
        if "artifact_kind" not in plan_columns:
            database.execute("ALTER TABLE backtest_plans ADD COLUMN artifact_kind TEXT NOT NULL DEFAULT 'user'")
        if "archived_at" not in plan_columns:
            database.execute("ALTER TABLE backtest_plans ADD COLUMN archived_at TEXT")
        existing = database.execute("SELECT 1 FROM settings WHERE id = 1").fetchone()
        if existing is None:
            database.execute(
                "INSERT INTO settings(id, payload, updated_at) VALUES(1, ?, ?)",
                (json.dumps(DEFAULT_SETTINGS), utc_now()),
            )
        if database.execute("SELECT 1 FROM application_state WHERE id = 1").fetchone() is None:
            database.execute(
                "INSERT INTO application_state(id, active_plan_id, active_run_id, updated_at) VALUES(1, NULL, NULL, ?)",
                (utc_now(),),
            )
        current_settings = json.loads(database.execute("SELECT payload FROM settings WHERE id = 1").fetchone()["payload"])
        merged_settings = _merge_defaults(DEFAULT_SETTINGS, current_settings)
        if merged_settings != current_settings:
            database.execute(
                "UPDATE settings SET payload = ?, updated_at = ? WHERE id = 1",
                (json.dumps(merged_settings), utc_now()),
            )
        _migrate_qa_test_artifacts(database)
        _sync_external_book_verifications(database)


def _migrate_qa_test_artifacts(database: sqlite3.Connection) -> None:
    locked = database.execute(
        """SELECT p.id, p.session_ids_json, p.created_at
           FROM backtest_plans p
           WHERE p.strategy_hash = ? AND p.mode = 'locked'
             AND EXISTS(SELECT 1 FROM audit_events a WHERE a.plan_id = p.id AND a.event_type = 'BLIND_SESSION_STARTED')
             AND EXISTS(SELECT 1 FROM audit_events a WHERE a.plan_id = p.id AND a.event_type = 'CANDIDATE_SCAN_COMPLETED')
             AND NOT EXISTS(SELECT 1 FROM blind_trades t WHERE t.plan_id = p.id)""",
        (QA_STRATEGY_HASH,),
    ).fetchone()
    if not locked:
        return
    rows = database.execute(
        """SELECT p.id FROM backtest_plans p
           WHERE p.strategy_hash = ? AND p.session_ids_json = ?
             AND NOT EXISTS(SELECT 1 FROM blind_trades t WHERE t.plan_id = p.id)""",
        (QA_STRATEGY_HASH, locked["session_ids_json"]),
    ).fetchall()
    if not rows:
        return
    now = utc_now()
    ids = [str(row["id"]) for row in rows]
    for plan_id in ids:
        database.execute(
            "UPDATE backtest_plans SET artifact_kind = 'test_artifact', status = 'ARCHIVED', archived_at = COALESCE(archived_at, ?) WHERE id = ?",
            (now, plan_id),
        )
        already_audited = database.execute(
            "SELECT 1 FROM audit_events WHERE plan_id = ? AND event_type = 'QA_TEST_ARTIFACT_ARCHIVED'",
            (plan_id,),
        ).fetchone()
        if not already_audited:
            database.execute(
                "INSERT INTO audit_events(plan_id, session_id, event_type, payload_json, created_at) VALUES(?, NULL, ?, ?, ?)",
                (
                    plan_id,
                    "QA_TEST_ARTIFACT_ARCHIVED",
                    json.dumps({"reason": "Browser QA artifact identified by exact hash and audit evidence", "dataPreserved": True}),
                    now,
                ),
            )
    placeholders = ",".join("?" for _ in ids)
    database.execute(
        f"UPDATE backtest_runs SET status = 'ARCHIVED', ended_at = COALESCE(ended_at, ?) WHERE plan_id IN ({placeholders}) AND status = 'ACTIVE'",
        (now, *ids),
    )
    state = database.execute("SELECT active_plan_id, active_run_id FROM application_state WHERE id = 1").fetchone()
    if state and (state["active_plan_id"] in ids or state["active_run_id"]):
        active_run = database.execute("SELECT plan_id FROM backtest_runs WHERE id = ?", (state["active_run_id"],)).fetchone() if state["active_run_id"] else None
        if state["active_plan_id"] in ids or (active_run and active_run["plan_id"] in ids):
            database.execute(
                "UPDATE application_state SET active_plan_id = NULL, active_run_id = NULL, updated_at = ? WHERE id = 1",
                (now,),
            )


def _sync_external_book_verifications(database: sqlite3.Connection) -> None:
    now = utc_now()
    sessions = database.execute("SELECT id, file_path, sha256, external_verification FROM sessions").fetchall()
    for session in sessions:
        status = "pending" if "pending" in str(session["external_verification"]) else "not_requested"
        database.execute(
            """INSERT OR IGNORE INTO external_book_verifications(
               session_id, mbo_file_hash, status, updated_at
               ) VALUES(?,?,?,?)""",
            (session["id"], session["sha256"], status, now),
        )
    report_root = REPO_ROOT / "data" / "databento" / "reports" / "book-verification"
    if not report_root.is_dir():
        return
    by_file = {str(Path(row["file_path"]).resolve()): row for row in sessions}
    for report_path in report_root.glob("*.json"):
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            session = by_file.get(str(Path(str(report.get("mboFile") or "")).resolve()))
        except (OSError, json.JSONDecodeError):
            continue
        if not session:
            continue
        metric_matches = report.get("metric_matches") or {}
        top10_values = [int(metric_matches.get(key, 0)) for key in ("top10Prices", "top10Sizes", "top10OrderCounts")]
        verified_at = datetime.fromtimestamp(report_path.stat().st_mtime, UTC).isoformat().replace("+00:00", "Z")
        database.execute(
            """INSERT INTO external_book_verifications(
               session_id, mbo_file_hash, status, compared_groups, bbo_matches, top10_matches,
               mismatches, report_path, verified_at, updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(session_id) DO UPDATE SET
               mbo_file_hash=excluded.mbo_file_hash, status=excluded.status,
               compared_groups=excluded.compared_groups, bbo_matches=excluded.bbo_matches,
               top10_matches=excluded.top10_matches, mismatches=excluded.mismatches,
               report_path=excluded.report_path, verified_at=excluded.verified_at,
               updated_at=excluded.updated_at""",
            (
                session["id"], session["sha256"], "passed" if report.get("passed") else "failed",
                int(report.get("states_compared", 0)), int(metric_matches.get("bbo", 0)),
                min(top10_values) if top10_values else 0, int(report.get("state_mismatches", 0)),
                str(report_path), verified_at, now,
            ),
        )


def _decode_session(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["derived_manifest"] = json.loads(result.get("derived_manifest") or "{}")
    result["external_book_verification"] = get_external_book_verification(result["id"], result["sha256"])
    from .data_health import derive_data_health
    result["data_health"] = derive_data_health(result)
    result["book_reliability"] = "guaranteed" if result["data_health"]["fullL3Claim"] else "not_guaranteed"
    return result


def list_sessions() -> list[dict[str, Any]]:
    migrate()
    with connect() as database:
        rows = database.execute("SELECT * FROM sessions ORDER BY start_at, file_path").fetchall()
    return [_decode_session(row) for row in rows]


def get_session(session_id: str) -> dict[str, Any] | None:
    migrate()
    with connect() as database:
        row = database.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return _decode_session(row) if row else None


def get_external_book_verification(session_id: str, mbo_file_hash: str | None = None) -> dict[str, Any]:
    migrate()
    with connect() as database:
        row = database.execute(
            "SELECT * FROM external_book_verifications WHERE session_id = ?", (session_id,)
        ).fetchone()
    if not row or (mbo_file_hash and row["mbo_file_hash"] != mbo_file_hash):
        return {
            "sessionId": session_id, "mboFileHash": mbo_file_hash or "", "status": "not_requested",
            "comparedGroups": None, "bboMatches": None, "top10Matches": None,
            "mismatches": None, "reportPath": None, "verifiedAt": None,
        }
    return {
        "sessionId": row["session_id"], "mboFileHash": row["mbo_file_hash"], "status": row["status"],
        "comparedGroups": row["compared_groups"], "bboMatches": row["bbo_matches"],
        "top10Matches": row["top10_matches"], "mismatches": row["mismatches"],
        "reportPath": row["report_path"], "verifiedAt": row["verified_at"],
    }


def upsert_session(payload: dict[str, Any]) -> None:
    migrate()
    columns = (
        "id", "instrument", "symbol", "contract_symbol", "instrument_id", "start_at", "end_at",
        "record_count", "snapshot_status", "completeness", "file_path", "sha256", "imported_at",
        "integrity_status", "unknown_pre", "unknown_during", "unknown_post", "sequence_regressions",
        "processing_rate", "peak_rss_mb", "derived_manifest", "external_verification",
        "dataset_name", "schema_name", "sequence_gaps", "out_of_order_events", "duplicate_events",
        "contract_mapping_status",
    )
    defaults = {
        "dataset_name": "GLBX.MDP3", "schema_name": "mbo", "sequence_gaps": 0,
        "out_of_order_events": payload.get("sequence_regressions", 0), "duplicate_events": 0,
        "contract_mapping_status": "resolved",
    }
    values = [
        json.dumps(payload[name]) if name == "derived_manifest" else payload.get(name, defaults.get(name))
        for name in columns
    ]
    placeholders = ",".join("?" for _ in columns)
    update = ",".join(f"{name}=excluded.{name}" for name in columns[1:])
    with connect() as database:
        database.execute(
            f"INSERT INTO sessions({','.join(columns)}) VALUES({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {update}",
            values,
        )


def get_settings() -> dict[str, Any]:
    migrate()
    with connect() as database:
        row = database.execute("SELECT payload FROM settings WHERE id = 1").fetchone()
    stored = json.loads(row["payload"]) if row else {}
    return _merge_defaults(DEFAULT_SETTINGS, stored)


def update_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = get_settings()
    for section, values in payload.items():
        if section not in current or not isinstance(values, dict):
            continue
        current[section].update(values)
    current["data"]["liveEnabled"] = False
    current["ai"]["enabled"] = bool(current["ai"].get("enabled")) and current["ai"].get("provider") != "disabled"
    with connect() as database:
        database.execute(
            "UPDATE settings SET payload = ?, updated_at = ? WHERE id = 1",
            (json.dumps(current), utc_now()),
        )
    return current


JOURNAL_JSON_FIELDS = {"targets", "decision_snapshot", "risk_snapshot", "market_context", "mistake_tags"}


def _decode_journal(row: sqlite3.Row) -> dict[str, Any]:
    raw = dict(row)
    result = {
        "id": raw["id"], "sessionId": raw["session_id"], "date": raw["trade_date"],
        "session": raw["session_name"], "symbol": raw["symbol"], "direction": raw["direction"],
        "setup": raw["setup"], "entry": raw["entry_price"], "stop": raw["stop_price"],
        "targets": json.loads(raw["targets_json"]), "exit": raw["exit_price"], "contracts": raw["contracts"],
        "riskUsd": raw["risk_usd"], "resultUsd": raw["result_usd"], "resultR": raw["result_r"],
        "screenshotPath": raw["screenshot_path"], "decisionSnapshot": json.loads(raw["decision_snapshot"]),
        "riskSnapshot": json.loads(raw["risk_snapshot"]), "marketContext": json.loads(raw["market_context"]),
        "notes": raw["notes"], "emotion": raw["emotion"], "mistakeTags": json.loads(raw["mistake_tags"]),
        "createdAt": raw["created_at"], "updatedAt": raw["updated_at"],
    }
    return result


def list_journal() -> list[dict[str, Any]]:
    migrate()
    with connect() as database:
        rows = database.execute("SELECT * FROM journal_entries ORDER BY trade_date DESC, created_at DESC").fetchall()
    return [_decode_journal(row) for row in rows]


def save_journal(payload: dict[str, Any], entry_id: str) -> dict[str, Any]:
    migrate()
    if journal_is_locked(entry_id):
        raise ValueError("Locked backtest trades are immutable; record a correction in the audit log.")
    now = utc_now()
    existing = next((entry for entry in list_journal() if entry["id"] == entry_id), None)
    created = existing["createdAt"] if existing else now
    values = (
        entry_id, payload.get("sessionId"), payload.get("date", now[:10]), payload.get("session", "Replay"),
        payload.get("symbol", "MES"), payload.get("direction", "LONG"), payload.get("setup", "Manual Review"),
        float(payload.get("entry", 0)), float(payload.get("stop", 0)), json.dumps(payload.get("targets", [])),
        payload.get("exit"), int(payload.get("contracts", 1)), float(payload.get("riskUsd", 0)),
        payload.get("resultUsd"), payload.get("resultR"), payload.get("screenshotPath"),
        json.dumps(payload.get("decisionSnapshot", {})), json.dumps(payload.get("riskSnapshot", {})),
        json.dumps(payload.get("marketContext", {})), str(payload.get("notes", "")),
        str(payload.get("emotion", "neutral")), json.dumps(payload.get("mistakeTags", [])), created, now,
    )
    with connect() as database:
        database.execute(
            """INSERT OR REPLACE INTO journal_entries(
              id, session_id, trade_date, session_name, symbol, direction, setup, entry_price, stop_price,
              targets_json, exit_price, contracts, risk_usd, result_usd, result_r, screenshot_path,
              decision_snapshot, risk_snapshot, market_context, notes, emotion, mistake_tags, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            values,
        )
        row = database.execute("SELECT * FROM journal_entries WHERE id = ?", (entry_id,)).fetchone()
    return _decode_journal(row)


def delete_journal(entry_id: str) -> bool:
    if journal_is_locked(entry_id):
        raise ValueError("Locked backtest trades cannot be deleted.")
    with connect() as database:
        cursor = database.execute("DELETE FROM journal_entries WHERE id = ?", (entry_id,))
    return cursor.rowcount > 0


def journal_csv() -> str:
    output = io.StringIO()
    rows = list_journal()
    fields = ["date", "session", "symbol", "direction", "setup", "entry", "stop", "exit", "contracts", "riskUsd", "resultUsd", "resultR", "notes", "emotion"]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def journal_backup() -> str:
    return json.dumps({"version": 1, "exportedAt": utc_now(), "entries": list_journal()}, indent=2)


def import_journal(entries: list[dict[str, Any]]) -> int:
    count = 0
    for index, entry in enumerate(entries):
        entry_id = str(entry.get("id") or f"import-{datetime.now(UTC).timestamp():.0f}-{index}")
        save_journal(entry, entry_id)
        count += 1
    return count


def save_data_estimate(payload: dict[str, Any]) -> dict[str, Any]:
    columns = (
        "id", "request_fingerprint", "dataset", "mode", "schemas_json", "input_symbol",
        "raw_symbol", "instrument_id", "start_utc", "end_utc", "replay_start", "replay_end",
        "timezone", "estimated_cost", "estimated_records", "billable_bytes", "unit_price_json",
        "local_reuse", "allowed", "confidence", "warnings_json", "metadata_json", "created_at",
        "expires_at", "status", "job_id", "actual_local_size", "downloaded_at",
    )
    values = [payload.get(name) for name in columns]
    placeholders = ",".join("?" for _ in columns)
    with connect() as database:
        database.execute("BEGIN IMMEDIATE")
        existing = database.execute(
            "SELECT id, status FROM data_estimates WHERE request_fingerprint = ?",
            (payload["request_fingerprint"],),
        ).fetchone()
        preserve_lifecycle = bool(existing and (
            existing["status"] in {
                "AUTHORIZED", "SUBMITTING", "SUBMITTED", "QUEUED", "DOWNLOADING",
                "IMPORTING", "VALIDATING_IMPORT", "COMPLETED", "READY", "IMPORTED",
            }
            or database.execute(
                "SELECT 1 FROM dataset_jobs WHERE estimate_id = ? LIMIT 1", (existing["id"],)
            ).fetchone()
            or database.execute(
                "SELECT 1 FROM download_authorizations WHERE estimate_id = ? LIMIT 1", (existing["id"],)
            ).fetchone()
        ))
        protected = {"id"}
        if preserve_lifecycle:
            protected.update({"created_at", "expires_at", "status", "job_id", "actual_local_size", "downloaded_at"})
        updates = ",".join(f"{name}=excluded.{name}" for name in columns if name not in protected)
        database.execute(
            f"INSERT INTO data_estimates({','.join(columns)}) VALUES({placeholders}) "
            f"ON CONFLICT(request_fingerprint) DO UPDATE SET {updates}",
            values,
        )
        row = database.execute(
            "SELECT * FROM data_estimates WHERE request_fingerprint = ?",
            (payload["request_fingerprint"],),
        ).fetchone()
        if not preserve_lifecycle:
            database.execute(
                "INSERT INTO audit_events(plan_id, session_id, event_type, payload_json, created_at) VALUES(NULL, NULL, 'DATA_ESTIMATE_CREATED', ?, ?)",
                (json.dumps({
                    "estimateId": row["id"], "fingerprint": row["request_fingerprint"],
                    "mode": row["mode"], "status": row["status"], "secretValuesLogged": False,
                }), utc_now()),
            )
    return _decode_estimate(row)


def _decode_estimate(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for field in ("schemas_json", "unit_price_json", "warnings_json", "metadata_json"):
        result[field.removesuffix("_json")] = json.loads(result.pop(field) or "{}")
    result["local_reuse"] = bool(result["local_reuse"])
    result["allowed"] = bool(result["allowed"])
    return result


def get_data_estimate(estimate_id: str) -> dict[str, Any] | None:
    migrate()
    with connect() as database:
        row = database.execute("SELECT * FROM data_estimates WHERE id = ?", (estimate_id,)).fetchone()
    return _decode_estimate(row) if row else None


def list_data_estimates(limit: int = 50) -> list[dict[str, Any]]:
    migrate()
    with connect() as database:
        rows = database.execute(
            "SELECT * FROM data_estimates ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 500)),)
        ).fetchall()
    return [_decode_estimate(row) for row in rows]


def update_estimate_status(estimate_id: str, status: str, **values: Any) -> None:
    allowed_fields = {"job_id", "actual_local_size", "downloaded_at"}
    assignments = ["status = ?"]
    parameters: list[Any] = [status]
    for name, value in values.items():
        if name in allowed_fields:
            assignments.append(f"{name} = ?")
            parameters.append(value)
    parameters.append(estimate_id)
    with connect() as database:
        database.execute(
            f"UPDATE data_estimates SET {', '.join(assignments)} WHERE id = ?", parameters
        )


def tracked_costs(now: datetime | None = None) -> dict[str, float | int]:
    current = now or datetime.now(UTC)
    day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=day_start.weekday())
    month_start = day_start.replace(day=1)
    with connect() as database:
        rows = database.execute(
            "SELECT estimated_cost, created_at, downloaded_at, status FROM data_estimates"
        ).fetchall()
        reservations = database.execute(
            """SELECT l.amount, a.created_at FROM authorization_ledger l
               JOIN download_authorizations a ON a.id = l.authorization_id
               WHERE l.state = 'RESERVED'"""
        ).fetchall()
        job_costs = database.execute(
            "SELECT actual_cost, downloaded_at, charged_at FROM dataset_jobs WHERE actual_cost IS NOT NULL"
        ).fetchall()
    totals: dict[str, float | int] = {
        "estimatedToday": 0.0, "authorizedToday": 0.0, "downloadedToday": 0.0,
        "estimatedWeek": 0.0, "authorizedWeek": 0.0, "downloadedWeek": 0.0,
        "estimatedMonth": 0.0, "authorizedMonth": 0.0, "downloadedMonth": 0.0,
        "actualChargedToday": 0.0, "actualChargedWeek": 0.0, "actualChargedMonth": 0.0,
        "avoidedDuplicateRequests": 0,
    }
    for row in rows:
        created = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
        downloaded = datetime.fromisoformat(str(row["downloaded_at"]).replace("Z", "+00:00")) if row["downloaded_at"] else None
        cost = float(row["estimated_cost"])
        if created >= day_start:
            totals["estimatedToday"] += cost
        if created >= week_start:
            totals["estimatedWeek"] += cost
        if created >= month_start:
            totals["estimatedMonth"] += cost
        if row["status"] == "LOCAL_REUSE":
            totals["avoidedDuplicateRequests"] += 1
    for row in reservations:
        created = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
        amount = float(row["amount"])
        if created >= day_start:
            totals["authorizedToday"] += amount
        if created >= week_start:
            totals["authorizedWeek"] += amount
        if created >= month_start:
            totals["authorizedMonth"] += amount
    for row in job_costs:
        amount = float(row["actual_cost"])
        if row["downloaded_at"]:
            downloaded = datetime.fromisoformat(str(row["downloaded_at"]).replace("Z", "+00:00"))
            if downloaded >= day_start:
                totals["downloadedToday"] += amount
            if downloaded >= week_start:
                totals["downloadedWeek"] += amount
            if downloaded >= month_start:
                totals["downloadedMonth"] += amount
        if row["charged_at"]:
            charged = datetime.fromisoformat(str(row["charged_at"]).replace("Z", "+00:00"))
            if charged >= day_start:
                totals["actualChargedToday"] += amount
            if charged >= week_start:
                totals["actualChargedWeek"] += amount
            if charged >= month_start:
                totals["actualChargedMonth"] += amount
    totals["localReusableDatasets"] = len(list_sessions())
    return totals


def save_dataset_job(payload: dict[str, Any]) -> dict[str, Any]:
    now = utc_now()
    with connect() as database:
        database.execute(
            """INSERT INTO dataset_jobs(
                 id, estimate_id, schema_name, remote_job_id, status, details_json, created_at, updated_at,
                 authorization_id, progress, error_code, error_message, actual_cost, download_bytes, downloaded_at, charged_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(estimate_id, schema_name) DO UPDATE SET
                 remote_job_id=excluded.remote_job_id, status=excluded.status,
                 details_json=excluded.details_json, updated_at=excluded.updated_at,
                 authorization_id=COALESCE(excluded.authorization_id, dataset_jobs.authorization_id),
                 progress=excluded.progress, error_code=excluded.error_code,
                 error_message=excluded.error_message, actual_cost=excluded.actual_cost,
                 download_bytes=excluded.download_bytes, downloaded_at=excluded.downloaded_at,
                 charged_at=excluded.charged_at""",
            (
                payload["id"], payload["estimate_id"], payload["schema_name"],
                payload.get("remote_job_id"), payload["status"],
                json.dumps(payload.get("details", {}), default=str), payload.get("created_at", now), now,
                payload.get("authorization_id"), float(payload.get("progress", 0)),
                payload.get("error_code"), payload.get("error_message"), payload.get("actual_cost"),
                payload.get("download_bytes"), payload.get("downloaded_at"),
                payload.get("charged_at"),
            ),
        )
        row = database.execute(
            "SELECT * FROM dataset_jobs WHERE estimate_id = ? AND schema_name = ?",
            (payload["estimate_id"], payload["schema_name"]),
        ).fetchone()
    result = dict(row)
    result["details"] = json.loads(result.pop("details_json") or "{}")
    return result


def list_dataset_jobs() -> list[dict[str, Any]]:
    migrate()
    with connect() as database:
        rows = database.execute("SELECT * FROM dataset_jobs ORDER BY created_at DESC").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["details"] = json.loads(item.pop("details_json") or "{}")
        result.append(item)
    return result


def get_session_split(session_id: str) -> dict[str, Any]:
    migrate()
    with connect() as database:
        row = database.execute("SELECT * FROM session_splits WHERE session_id = ?", (session_id,)).fetchone()
    if row:
        result = dict(row)
        result["locked"] = bool(result["locked"])
        return result
    return {"session_id": session_id, "split_name": "Development", "reason": "", "locked": False, "viewed_at": None}


def set_session_split(session_id: str, split_name: str, reason: str = "", *, lock: bool = False) -> dict[str, Any]:
    allowed = {"Development", "Pilot", "Locked Test", "Forward Paper", "Excluded"}
    if split_name not in allowed:
        raise ValueError("Unsupported session split.")
    current = get_session_split(session_id)
    if current["locked"] and current["split_name"] != split_name:
        raise ValueError("This session split is locked and cannot be moved silently.")
    if split_name == "Excluded" and not reason.strip():
        raise ValueError("Excluded sessions require a reason.")
    now = utc_now()
    with connect() as database:
        database.execute(
            """INSERT INTO session_splits(session_id, split_name, reason, locked, viewed_at, updated_at)
               VALUES(?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET
               split_name=excluded.split_name, reason=excluded.reason,
               locked=MAX(session_splits.locked, excluded.locked), updated_at=excluded.updated_at""",
            (session_id, split_name, reason.strip(), int(lock), current.get("viewed_at"), now),
        )
    return get_session_split(session_id)


def mark_session_viewed(session_id: str) -> dict[str, Any]:
    current = get_session_split(session_id)
    now = utc_now()
    with connect() as database:
        database.execute(
            """INSERT INTO session_splits(session_id, split_name, reason, locked, viewed_at, updated_at)
               VALUES(?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET
               viewed_at=COALESCE(session_splits.viewed_at, excluded.viewed_at), updated_at=excluded.updated_at""",
            (
                session_id, current["split_name"], current["reason"], int(current["locked"]),
                current.get("viewed_at") or now, now,
            ),
        )
    return get_session_split(session_id)


def session_library() -> list[dict[str, Any]]:
    library = []
    for session in list_sessions():
        split = get_session_split(session["id"])
        item = {**session, "split": split}
        item["local_compressed_bytes"] = Path(session["file_path"]).stat().st_size if Path(session["file_path"]).is_file() else 0
        item["data_mode"] = "full_l3" if session["completeness"] == "complete" else "orderflow_partial"
        item["download_status"] = "IMPORTED"
        item["backtest_status"] = "UNASSIGNED" if split["split_name"] == "Development" else split["split_name"].upper().replace(" ", "_")
        library.append(item)
    return library


def save_planner_state(request_plan: dict[str, Any]) -> dict[str, Any]:
    now = utc_now()
    with connect() as database:
        database.execute(
            """INSERT INTO planner_state(id, request_plan_json, updated_at) VALUES(1,?,?)
               ON CONFLICT(id) DO UPDATE SET request_plan_json=excluded.request_plan_json, updated_at=excluded.updated_at""",
            (json.dumps(request_plan), now),
        )
    return {"requestPlan": request_plan, "updatedAt": now}


def get_planner_state() -> dict[str, Any] | None:
    migrate()
    with connect() as database:
        row = database.execute("SELECT request_plan_json, updated_at FROM planner_state WHERE id = 1").fetchone()
    if not row:
        return None
    return {"requestPlan": json.loads(row["request_plan_json"]), "updatedAt": row["updated_at"]}


def save_backtest_plan(payload: dict[str, Any]) -> dict[str, Any]:
    with connect() as database:
        database.execute(
            """INSERT INTO backtest_plans(id, mode, strategy, config_json, session_ids_json,
               strategy_hash, status, created_at, locked_at, artifact_kind, archived_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                payload["id"], payload["mode"], payload["strategy"], json.dumps(payload["config"]),
                json.dumps(payload["session_ids"]), payload["strategy_hash"], payload["status"],
                payload["created_at"], payload.get("locked_at"), payload.get("artifact_kind", "user"),
                payload.get("archived_at"),
            ),
        )
    return get_backtest_plan(payload["id"]) or payload


def get_backtest_plan(plan_id: str) -> dict[str, Any] | None:
    migrate()
    with connect() as database:
        row = database.execute("SELECT * FROM backtest_plans WHERE id = ?", (plan_id,)).fetchone()
    if not row:
        return None
    result = dict(row)
    result["config"] = json.loads(result.pop("config_json"))
    result["session_ids"] = json.loads(result.pop("session_ids_json"))
    result["assignments"] = list_plan_session_assignments(plan_id)
    return result


def list_backtest_plans(*, include_archived: bool = True) -> list[dict[str, Any]]:
    migrate()
    with connect() as database:
        query = "SELECT id FROM backtest_plans" + ("" if include_archived else " WHERE status != 'ARCHIVED'") + " ORDER BY created_at DESC"
        ids = [row["id"] for row in database.execute(query).fetchall()]
    return [plan for plan_id in ids if (plan := get_backtest_plan(plan_id))]


def update_backtest_plan(plan_id: str, **values: Any) -> dict[str, Any]:
    allowed = {"status", "strategy_hash", "session_ids_json", "artifact_kind", "archived_at"}
    updates = {key: value for key, value in values.items() if key in allowed}
    if not updates:
        plan = get_backtest_plan(plan_id)
        if not plan:
            raise ValueError("Backtest plan not found.")
        return plan
    assignments = ", ".join(f"{key} = ?" for key in updates)
    with connect() as database:
        database.execute(f"UPDATE backtest_plans SET {assignments} WHERE id = ?", (*updates.values(), plan_id))
    plan = get_backtest_plan(plan_id)
    if not plan:
        raise ValueError("Backtest plan not found.")
    return plan


def list_plan_session_assignments(plan_id: str) -> list[dict[str, Any]]:
    with connect() as database:
        rows = database.execute(
            "SELECT * FROM plan_session_assignments WHERE plan_id = ? ORDER BY created_at", (plan_id,)
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        for field in ("reused", "contaminated", "ui_practice_only"):
            item[field] = bool(item[field])
        result.append(item)
    return result


def save_plan_session_assignment(
    plan_id: str,
    session_id: str,
    *,
    split_name: str,
    assignment_type: str = "primary",
    reused: bool = False,
    contaminated: bool = False,
    ui_practice_only: bool = False,
    source_plan_id: str | None = None,
) -> dict[str, Any]:
    now = utc_now()
    with connect() as database:
        database.execute(
            """INSERT INTO plan_session_assignments(
               plan_id, session_id, split_name, assignment_type, reused, contaminated,
               ui_practice_only, source_plan_id, created_at
               ) VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(plan_id, session_id) DO UPDATE SET
               split_name=excluded.split_name, assignment_type=excluded.assignment_type,
               reused=excluded.reused, contaminated=excluded.contaminated,
               ui_practice_only=excluded.ui_practice_only, source_plan_id=excluded.source_plan_id""",
            (
                plan_id, session_id, split_name, assignment_type, int(reused), int(contaminated),
                int(ui_practice_only), source_plan_id, now,
            ),
        )
        plan_row = database.execute("SELECT session_ids_json FROM backtest_plans WHERE id = ?", (plan_id,)).fetchone()
        if not plan_row:
            raise ValueError("Backtest plan not found.")
        session_ids = list(dict.fromkeys([*json.loads(plan_row["session_ids_json"]), session_id]))
        database.execute(
            "UPDATE backtest_plans SET session_ids_json = ?, status = 'READY' WHERE id = ?",
            (json.dumps(session_ids), plan_id),
        )
    return next(item for item in list_plan_session_assignments(plan_id) if item["session_id"] == session_id)


def get_application_state() -> dict[str, Any]:
    migrate()
    with connect() as database:
        row = database.execute("SELECT * FROM application_state WHERE id = 1").fetchone()
        run = database.execute("SELECT * FROM backtest_runs WHERE id = ?", (row["active_run_id"],)).fetchone() if row and row["active_run_id"] else None
    return {
        "activePlanId": row["active_plan_id"] if row else None,
        "activeRunId": row["active_run_id"] if row else None,
        "activeRun": dict(run) if run else None,
        "updatedAt": row["updated_at"] if row else None,
    }


def activate_backtest_plan(plan_id: str) -> dict[str, Any]:
    plan = get_backtest_plan(plan_id)
    if not plan or plan["status"] == "ARCHIVED":
        raise ValueError("Only a non-archived plan can become active.")
    now = utc_now()
    with connect() as database:
        state = database.execute("SELECT active_run_id FROM application_state WHERE id = 1").fetchone()
        if state and state["active_run_id"]:
            database.execute(
                "UPDATE backtest_runs SET status = 'PAUSED', ended_at = COALESCE(ended_at, ?) WHERE id = ? AND status = 'ACTIVE'",
                (now, state["active_run_id"]),
            )
        database.execute("UPDATE backtest_plans SET status = 'INACTIVE' WHERE status = 'ACTIVE' AND id != ?", (plan_id,))
        database.execute("UPDATE backtest_plans SET status = CASE WHEN session_ids_json = '[]' THEN 'DRAFT' ELSE 'READY' END WHERE id = ?", (plan_id,))
        database.execute(
            "UPDATE application_state SET active_plan_id = ?, active_run_id = NULL, updated_at = ? WHERE id = 1",
            (plan_id, now),
        )
    return get_backtest_plan(plan_id) or plan


def start_backtest_run(plan_id: str, session_id: str, mode: str, run_id: str) -> dict[str, Any]:
    now = utc_now()
    with connect() as database:
        state = database.execute("SELECT active_plan_id, active_run_id FROM application_state WHERE id = 1").fetchone()
        if not state or state["active_plan_id"] != plan_id:
            raise ValueError("Activate the plan before starting its blind replay.")
        if state["active_run_id"]:
            database.execute(
                "UPDATE backtest_runs SET status = 'PAUSED', ended_at = COALESCE(ended_at, ?) WHERE id = ? AND status = 'ACTIVE'",
                (now, state["active_run_id"]),
            )
        database.execute(
            "INSERT INTO backtest_runs(id, plan_id, session_id, mode, status, started_at, ended_at) VALUES(?,?,?,?,?,?,NULL)",
            (run_id, plan_id, session_id, mode, "ACTIVE", now),
        )
        database.execute(
            "UPDATE application_state SET active_run_id = ?, updated_at = ? WHERE id = 1", (run_id, now)
        )
    return get_application_state()["activeRun"]


def exit_active_backtest_run(*, status: str = "EXITED") -> dict[str, Any] | None:
    now = utc_now()
    with connect() as database:
        state = database.execute("SELECT active_run_id FROM application_state WHERE id = 1").fetchone()
        if not state or not state["active_run_id"]:
            return None
        run = database.execute("SELECT * FROM backtest_runs WHERE id = ?", (state["active_run_id"],)).fetchone()
        database.execute(
            "UPDATE backtest_runs SET status = ?, ended_at = COALESCE(ended_at, ?) WHERE id = ?",
            (status, now, state["active_run_id"]),
        )
        database.execute("UPDATE application_state SET active_run_id = NULL, updated_at = ? WHERE id = 1", (now,))
    return dict(run) if run else None


def derive_application_lock_state() -> dict[str, Any]:
    state = get_application_state()
    active_plan = get_backtest_plan(str(state["activePlanId"])) if state.get("activePlanId") else None
    active_run = state.get("activeRun")
    if active_plan and active_plan["mode"] == "locked":
        if active_run and active_run["status"] == "ACTIVE" and active_run["plan_id"] == active_plan["id"]:
            return {
                "locked": True, "reason": "active_locked_run", "protocolId": active_plan["id"],
                "runId": active_run["id"], "sessionId": active_run["session_id"],
                "strategyHash": active_plan["strategy_hash"],
            }
        return {
            "locked": False, "reason": "locked_protocol_not_running", "protocolId": active_plan["id"],
            "strategyHash": active_plan["strategy_hash"],
        }
    if not active_plan and any(plan["mode"] == "locked" and plan["status"] == "ARCHIVED" for plan in list_backtest_plans()):
        return {"locked": False, "reason": "archived_locked_protocol"}
    return {"locked": False, "reason": "none", "protocolId": active_plan["id"] if active_plan else None}


def append_audit(event_type: str, payload: dict[str, Any], *, plan_id: str | None = None, session_id: str | None = None) -> dict[str, Any]:
    now = utc_now()
    with connect() as database:
        cursor = database.execute(
            "INSERT INTO audit_events(plan_id, session_id, event_type, payload_json, created_at) VALUES(?,?,?,?,?)",
            (plan_id, session_id, event_type, json.dumps(payload), now),
        )
    return {"id": cursor.lastrowid, "planId": plan_id, "sessionId": session_id, "eventType": event_type, "payload": payload, "createdAt": now}


def list_audit(plan_id: str | None = None) -> list[dict[str, Any]]:
    migrate()
    with connect() as database:
        if plan_id:
            rows = database.execute("SELECT * FROM audit_events WHERE plan_id = ? ORDER BY id DESC", (plan_id,)).fetchall()
        else:
            rows = database.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT 200").fetchall()
    return [{"id": row["id"], "planId": row["plan_id"], "sessionId": row["session_id"], "eventType": row["event_type"], "payload": json.loads(row["payload_json"]), "createdAt": row["created_at"]} for row in rows]


def save_scan_candidates(session_id: str, candidates: list[dict[str, Any]], plan_id: str | None = None) -> None:
    now = utc_now()
    with connect() as database:
        database.execute("DELETE FROM scan_candidates WHERE session_id = ? AND plan_id IS ?", (session_id, plan_id))
        database.executemany(
            """INSERT INTO scan_candidates(plan_id, session_id, timestamp, timestamp_ns,
               decision_state, direction, confidence, data_quality, reasons_json, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            [(
                plan_id, session_id, item["timestamp"], item["timestampNs"], item["decision"],
                item.get("direction"), item["confidence"], item["dataQuality"],
                json.dumps(item.get("reasons", [])), now,
            ) for item in candidates],
        )


def list_scan_candidates(session_id: str | None = None) -> list[dict[str, Any]]:
    migrate()
    with connect() as database:
        if session_id:
            rows = database.execute("SELECT * FROM scan_candidates WHERE session_id = ? ORDER BY timestamp_ns", (session_id,)).fetchall()
        else:
            rows = database.execute("SELECT * FROM scan_candidates ORDER BY timestamp_ns LIMIT 500").fetchall()
    return [{"id": row["id"], "planId": row["plan_id"], "sessionId": row["session_id"], "timestamp": row["timestamp"], "timestampNs": row["timestamp_ns"], "decision": row["decision_state"], "direction": row["direction"], "confidence": row["confidence"], "dataQuality": row["data_quality"], "reasons": json.loads(row["reasons_json"])} for row in rows]


def journal_is_locked(entry_id: str) -> bool:
    migrate()
    with connect() as database:
        row = database.execute(
            "SELECT 1 FROM blind_trades WHERE journal_id = ? AND immutable = 1", (entry_id,)
        ).fetchone()
    return row is not None


def list_blind_trades(plan_id: str | None = None) -> list[dict[str, Any]]:
    migrate()
    with connect() as database:
        if plan_id:
            rows = database.execute("SELECT * FROM blind_trades WHERE plan_id = ? ORDER BY opened_at", (plan_id,)).fetchall()
        else:
            rows = database.execute("SELECT * FROM blind_trades ORDER BY opened_at").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["targets"] = json.loads(item.pop("targets_json"))
        item["decisionSnapshot"] = json.loads(item.pop("decision_snapshot"))
        item["riskSnapshot"] = json.loads(item.pop("risk_snapshot"))
        item["featuresSnapshot"] = json.loads(item.pop("features_snapshot"))
        item["immutable"] = bool(item["immutable"])
        result.append(item)
    return result


def save_blind_trade(payload: dict[str, Any]) -> dict[str, Any]:
    now = utc_now()
    with connect() as database:
        database.execute(
            """INSERT INTO blind_trades(
               id, plan_id, session_id, journal_id, opened_at, closed_at, direction,
               entry, stop, targets_json, result_r, result_usd, mae, mfe, holding_seconds,
               fees_usd, slippage_usd, decision_snapshot, risk_snapshot, features_snapshot,
               status, immutable
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                payload["id"], payload["plan_id"], payload["session_id"], payload.get("journal_id"),
                payload.get("opened_at", now), None, payload["direction"], float(payload["entry"]),
                float(payload["stop"]), json.dumps(payload["targets"]), None, None, None, None, None,
                float(payload.get("fees_usd", 0)), 0.0,
                json.dumps(payload["decision_snapshot"]), json.dumps(payload["risk_snapshot"]),
                json.dumps(payload["features_snapshot"]), "OPEN", 0,
            ),
        )
    return next(item for item in list_blind_trades(payload["plan_id"]) if item["id"] == payload["id"])


def close_blind_trade(trade_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    with connect() as database:
        row = database.execute("SELECT immutable, plan_id FROM blind_trades WHERE id = ?", (trade_id,)).fetchone()
        if not row:
            raise ValueError("Blind trade not found.")
        if row["immutable"]:
            raise ValueError("This locked trade is immutable; use an audit correction.")
        database.execute(
            """UPDATE blind_trades SET closed_at=?, result_r=?, result_usd=?, mae=?, mfe=?,
               holding_seconds=?, fees_usd=?, slippage_usd=?, status='CLOSED', immutable=? WHERE id=?""",
            (
                payload.get("closed_at", utc_now()), float(payload["result_r"]), float(payload["result_usd"]),
                float(payload.get("mae", 0)), float(payload.get("mfe", 0)),
                float(payload.get("holding_seconds", 0)), float(payload.get("fees_usd", 0)),
                float(payload.get("slippage_usd", 0)), int(bool(payload.get("immutable", False))), trade_id,
            ),
        )
        plan_id = str(row["plan_id"])
    return next(item for item in list_blind_trades(plan_id) if item["id"] == trade_id)


def _decode_estimate_job(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["request"] = json.loads(item.pop("request_json"))
    item["checkpoint"] = json.loads(item.pop("checkpoint_json", "{}") or "{}")
    item["result"] = json.loads(item.pop("result_json")) if item.get("result_json") else None
    item["progress"] = float(item.get("progress") or 0)
    return item


def save_estimate_job(payload: dict[str, Any]) -> dict[str, Any]:
    now = payload.get("created_at", utc_now())
    with connect() as database:
        database.execute(
            """INSERT INTO estimate_jobs(
               id, request_fingerprint, request_json, job_kind, status, progress, checkpoint_json, result_json,
               error_code, error_message, retry_of, created_at, started_at, completed_at,
               expires_at, cancelled_at, updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                payload["id"], payload["request_fingerprint"], json.dumps(payload["request"], sort_keys=True),
                payload.get("job_kind", "estimate"), payload.get("status", "PENDING"), float(payload.get("progress", 0)),
                json.dumps(payload.get("checkpoint", {}), default=str),
                json.dumps(payload["result"], default=str) if payload.get("result") is not None else None,
                payload.get("error_code"), payload.get("error_message"), payload.get("retry_of"), now,
                payload.get("started_at"), payload.get("completed_at"), payload["expires_at"],
                payload.get("cancelled_at"), now,
            ),
        )
    return get_estimate_job(payload["id"]) or payload


def get_estimate_job(job_id: str) -> dict[str, Any] | None:
    migrate()
    with connect() as database:
        row = database.execute("SELECT * FROM estimate_jobs WHERE id = ?", (job_id,)).fetchone()
    return _decode_estimate_job(row) if row else None


def list_estimate_jobs(limit: int = 50) -> list[dict[str, Any]]:
    migrate()
    with connect() as database:
        rows = database.execute(
            "SELECT * FROM estimate_jobs ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 500)),)
        ).fetchall()
    return [_decode_estimate_job(row) for row in rows]


def find_reusable_estimate_job(request_fingerprint: str, job_kind: str) -> dict[str, Any] | None:
    now = utc_now()
    with connect() as database:
        row = database.execute(
            """SELECT * FROM estimate_jobs
               WHERE request_fingerprint = ? AND job_kind = ?
                 AND status IN ('PENDING','RUNNING','COMPLETED') AND expires_at > ?
               ORDER BY created_at DESC LIMIT 1""",
            (request_fingerprint, job_kind, now),
        ).fetchone()
    return _decode_estimate_job(row) if row else None


def update_estimate_job(job_id: str, status: str, **values: Any) -> dict[str, Any]:
    allowed = {
        "result", "error_code", "error_message", "started_at", "completed_at",
        "expires_at", "cancelled_at", "progress", "checkpoint",
    }
    assignments = ["status = ?", "updated_at = ?"]
    parameters: list[Any] = [status, utc_now()]
    for key, value in values.items():
        if key not in allowed:
            continue
        column = "result_json" if key == "result" else "checkpoint_json" if key == "checkpoint" else key
        assignments.append(f"{column} = ?")
        parameters.append(json.dumps(value, default=str) if key in {"result", "checkpoint"} and value is not None else value)
    parameters.append(job_id)
    with connect() as database:
        database.execute(f"UPDATE estimate_jobs SET {', '.join(assignments)} WHERE id = ?", parameters)
    job = get_estimate_job(job_id)
    if not job:
        raise ValueError("Estimate job not found.")
    return job


def recoverable_estimate_jobs() -> list[dict[str, Any]]:
    return [item for item in list_estimate_jobs(500) if item["status"] in {"PENDING", "RUNNING"}]


def _decode_range_plan(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["request"] = json.loads(item.pop("request_json") or "{}")
    item["estimate_ids"] = json.loads(item.pop("estimate_ids_json") or "[]")
    item["summary"] = json.loads(item.pop("summary_json") or "{}")
    return item


def save_range_plan(payload: dict[str, Any]) -> dict[str, Any]:
    now = payload.get("created_at", utc_now())
    with connect() as database:
        database.execute(
            """INSERT INTO range_plans(
               id, request_fingerprint, request_json, estimate_ids_json, summary_json,
               status, created_at, expires_at, updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(request_fingerprint) DO UPDATE SET
               request_json=excluded.request_json, estimate_ids_json=excluded.estimate_ids_json,
               summary_json=excluded.summary_json, status=excluded.status,
               expires_at=excluded.expires_at, updated_at=excluded.updated_at""",
            (
                payload["id"], payload["request_fingerprint"], json.dumps(payload.get("request", {}), sort_keys=True),
                json.dumps(payload.get("estimate_ids", [])), json.dumps(payload.get("summary", {}), default=str),
                payload.get("status", "ESTIMATING"), now, payload["expires_at"], utc_now(),
            ),
        )
        row = database.execute("SELECT * FROM range_plans WHERE request_fingerprint = ?", (payload["request_fingerprint"],)).fetchone()
    return _decode_range_plan(row)


def get_range_plan(plan_id: str) -> dict[str, Any] | None:
    migrate()
    with connect() as database:
        row = database.execute("SELECT * FROM range_plans WHERE id = ?", (plan_id,)).fetchone()
    return _decode_range_plan(row) if row else None


def get_range_plan_by_fingerprint(request_fingerprint: str) -> dict[str, Any] | None:
    migrate()
    with connect() as database:
        row = database.execute("SELECT * FROM range_plans WHERE request_fingerprint = ?", (request_fingerprint,)).fetchone()
    return _decode_range_plan(row) if row else None


def list_range_plans(limit: int = 10) -> list[dict[str, Any]]:
    migrate()
    with connect() as database:
        rows = database.execute(
            "SELECT * FROM range_plans ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 100)),)
        ).fetchall()
    return [_decode_range_plan(row) for row in rows]


def update_range_plan(plan_id: str, status: str, *, summary: dict[str, Any] | None = None, estimate_ids: list[str] | None = None, expires_at: str | None = None) -> dict[str, Any]:
    assignments = ["status = ?", "updated_at = ?"]
    values: list[Any] = [status, utc_now()]
    if summary is not None:
        assignments.append("summary_json = ?")
        values.append(json.dumps(summary, default=str))
    if estimate_ids is not None:
        assignments.append("estimate_ids_json = ?")
        values.append(json.dumps(estimate_ids))
    if expires_at is not None:
        assignments.append("expires_at = ?")
        values.append(expires_at)
    values.append(plan_id)
    with connect() as database:
        database.execute(f"UPDATE range_plans SET {', '.join(assignments)} WHERE id = ?", values)
    result = get_range_plan(plan_id)
    if not result:
        raise ValueError("Range plan not found.")
    return result


def save_experiment(payload: dict[str, Any]) -> dict[str, Any]:
    now = payload.get("created_at", utc_now())
    with connect() as database:
        database.execute(
            """INSERT INTO experiments(
               id, name, strategy_name, strategy_hash, parameter_hash, dataset_fingerprint,
               split_name, seed, fill_model_version, cost_model_version, feature_version,
               code_version, status, config_json, metrics_json, validation_json, created_at, updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                payload["id"], payload["name"], payload["strategy_name"], payload["strategy_hash"],
                payload["parameter_hash"], payload["dataset_fingerprint"], payload["split_name"],
                int(payload["seed"]), payload["fill_model_version"], payload["cost_model_version"],
                payload["feature_version"], payload["code_version"], payload.get("status", "QUEUED"),
                json.dumps(payload.get("config", {}), sort_keys=True),
                json.dumps(payload.get("metrics", {}), sort_keys=True),
                json.dumps(payload.get("validation", {}), sort_keys=True), now, now,
            ),
        )
    return get_experiment(payload["id"]) or payload


def _decode_experiment(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    for column in ("config_json", "metrics_json", "validation_json"):
        item[column.removesuffix("_json")] = json.loads(item.pop(column) or "{}")
    return item


def get_experiment(experiment_id: str) -> dict[str, Any] | None:
    migrate()
    with connect() as database:
        row = database.execute("SELECT * FROM experiments WHERE id = ?", (experiment_id,)).fetchone()
    return _decode_experiment(row) if row else None


def list_experiments(limit: int = 100) -> list[dict[str, Any]]:
    migrate()
    with connect() as database:
        rows = database.execute(
            "SELECT * FROM experiments ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 500)),)
        ).fetchall()
    return [_decode_experiment(row) for row in rows]


def update_experiment(experiment_id: str, status: str, *, metrics: dict[str, Any] | None = None, validation: dict[str, Any] | None = None) -> dict[str, Any]:
    assignments = ["status = ?", "updated_at = ?"]
    parameters: list[Any] = [status, utc_now()]
    if metrics is not None:
        assignments.append("metrics_json = ?")
        parameters.append(json.dumps(metrics, sort_keys=True))
    if validation is not None:
        assignments.append("validation_json = ?")
        parameters.append(json.dumps(validation, sort_keys=True))
    parameters.append(experiment_id)
    with connect() as database:
        database.execute(f"UPDATE experiments SET {', '.join(assignments)} WHERE id = ?", parameters)
    experiment = get_experiment(experiment_id)
    if not experiment:
        raise ValueError("Experiment not found.")
    return experiment


def save_research_job(payload: dict[str, Any]) -> dict[str, Any]:
    now = payload.get("created_at", utc_now())
    with connect() as database:
        database.execute(
            """INSERT INTO research_jobs(
               id, experiment_id, session_id, status, progress, checkpoint_json, config_json,
               result_json, error_message, created_at, started_at, completed_at, updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                payload["id"], payload.get("experiment_id"), payload["session_id"],
                payload.get("status", "QUEUED"), float(payload.get("progress", 0)),
                json.dumps(payload.get("checkpoint", {})), json.dumps(payload.get("config", {}), sort_keys=True),
                json.dumps(payload["result"], default=str) if payload.get("result") is not None else None,
                payload.get("error_message"), now, payload.get("started_at"), payload.get("completed_at"), now,
            ),
        )
    return get_research_job(payload["id"]) or payload


def _decode_research_job(row: sqlite3.Row, *, include_result: bool = True) -> dict[str, Any]:
    item = dict(row)
    for column in ("checkpoint_json", "config_json"):
        item[column.removesuffix("_json")] = json.loads(item.pop(column) or "{}")
    raw_result = item.pop("result_json")
    item["result"] = json.loads(raw_result) if include_result and raw_result else None
    return item


def get_research_job(job_id: str, *, include_result: bool = True) -> dict[str, Any] | None:
    migrate()
    with connect() as database:
        row = database.execute("SELECT * FROM research_jobs WHERE id = ?", (job_id,)).fetchone()
    return _decode_research_job(row, include_result=include_result) if row else None


def list_research_jobs(limit: int = 100, *, include_result: bool = True) -> list[dict[str, Any]]:
    migrate()
    with connect() as database:
        rows = database.execute(
            "SELECT * FROM research_jobs ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 500)),)
        ).fetchall()
    return [_decode_research_job(row, include_result=include_result) for row in rows]


def update_research_job(job_id: str, status: str, **values: Any) -> dict[str, Any]:
    allowed = {"progress", "checkpoint", "result", "error_message", "started_at", "completed_at"}
    assignments = ["status = ?", "updated_at = ?"]
    parameters: list[Any] = [status, utc_now()]
    for key, value in values.items():
        if key not in allowed:
            continue
        column = {"checkpoint": "checkpoint_json", "result": "result_json"}.get(key, key)
        assignments.append(f"{column} = ?")
        parameters.append(json.dumps(value, default=str) if key in {"checkpoint", "result"} else value)
    parameters.append(job_id)
    with connect() as database:
        database.execute(f"UPDATE research_jobs SET {', '.join(assignments)} WHERE id = ?", parameters)
    job = get_research_job(job_id)
    if not job:
        raise ValueError("Research job not found.")
    return job


def save_strategy_version(payload: dict[str, Any]) -> dict[str, Any]:
    with connect() as database:
        database.execute(
            """INSERT INTO strategy_versions(
               id, name, version, strategy_hash, status, validation_status, config_json,
               data_fingerprints_json, created_at, promoted_at, rejected_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(strategy_hash) DO UPDATE SET
                 status=excluded.status, validation_status=excluded.validation_status,
                 config_json=excluded.config_json, data_fingerprints_json=excluded.data_fingerprints_json,
                 promoted_at=excluded.promoted_at, rejected_at=excluded.rejected_at""",
            (
                payload["id"], payload["name"], payload["version"], payload["strategy_hash"],
                payload["status"], payload["validation_status"], json.dumps(payload.get("config", {}), sort_keys=True),
                json.dumps(payload.get("data_fingerprints", [])), payload.get("created_at", utc_now()),
                payload.get("promoted_at"), payload.get("rejected_at"),
            ),
        )
        row = database.execute("SELECT * FROM strategy_versions WHERE strategy_hash = ?", (payload["strategy_hash"],)).fetchone()
    item = dict(row)
    item["config"] = json.loads(item.pop("config_json"))
    item["data_fingerprints"] = json.loads(item.pop("data_fingerprints_json"))
    return item


def list_strategy_versions() -> list[dict[str, Any]]:
    migrate()
    with connect() as database:
        rows = database.execute("SELECT * FROM strategy_versions ORDER BY created_at DESC").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["config"] = json.loads(item.pop("config_json"))
        item["data_fingerprints"] = json.loads(item.pop("data_fingerprints_json"))
        result.append(item)
    return result


def save_model_version(payload: dict[str, Any]) -> dict[str, Any]:
    with connect() as database:
        database.execute(
            """INSERT INTO model_versions(
               id, name, version, model_type, status, calibration_json, feature_version, created_at, promoted_at
               ) VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET status=excluded.status,
                 calibration_json=excluded.calibration_json, promoted_at=excluded.promoted_at""",
            (
                payload["id"], payload["name"], payload["version"], payload["model_type"], payload["status"],
                json.dumps(payload.get("calibration", {}), sort_keys=True), payload["feature_version"],
                payload.get("created_at", utc_now()), payload.get("promoted_at"),
            ),
        )
        row = database.execute("SELECT * FROM model_versions WHERE id = ?", (payload["id"],)).fetchone()
    item = dict(row)
    item["calibration"] = json.loads(item.pop("calibration_json"))
    return item


def list_model_versions() -> list[dict[str, Any]]:
    migrate()
    with connect() as database:
        rows = database.execute("SELECT * FROM model_versions ORDER BY created_at DESC").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["calibration"] = json.loads(item.pop("calibration_json"))
        result.append(item)
    return result


def save_signal_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    inserted = False
    with connect() as database:
        cursor = database.execute(
            """INSERT OR IGNORE INTO signal_snapshots(
               id, session_id, run_id, timestamp, status, strategy_version, model_version,
               payload_json, signature, created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                payload["id"], payload.get("session_id"), payload.get("run_id"), payload["timestamp"],
                payload["status"], payload["strategy_version"], payload["model_version"],
                json.dumps(payload["payload"], sort_keys=True, default=str), payload["signature"],
                payload.get("created_at", utc_now()),
            ),
        )
        inserted = cursor.rowcount > 0
        row = database.execute("SELECT * FROM signal_snapshots WHERE id = ?", (payload["id"],)).fetchone()
    if inserted:
        append_audit(
            "SIGNAL_INVALIDATED" if payload["status"] == "NO_TRADE" else "SIGNAL_CREATED",
            {
                "status": payload["status"],
                "strategyVersion": payload["strategy_version"],
                "modelVersion": payload["model_version"],
                "signature": payload["signature"][:16],
                "manualExecutionOnly": True,
            },
            session_id=payload.get("session_id"),
        )
    if not row:
        return payload
    item = dict(row)
    item["payload"] = json.loads(item.pop("payload_json"))
    return item


def list_signal_snapshots(limit: int = 200) -> list[dict[str, Any]]:
    migrate()
    with connect() as database:
        rows = database.execute(
            "SELECT * FROM signal_snapshots ORDER BY timestamp DESC LIMIT ?", (max(1, min(limit, 1000)),)
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        result.append(item)
    return result
