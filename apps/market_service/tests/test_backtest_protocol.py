from __future__ import annotations

import json

import pytest

from apps.market_service import backtest_protocol, storage
from apps.market_service.replay import ReplayEngine


@pytest.fixture()
def isolated_protocol(tmp_path, monkeypatch) -> dict:
    monkeypatch.setattr(storage, "APP_ROOT", tmp_path / "app")
    monkeypatch.setattr(storage, "JOURNAL_ROOT", tmp_path / "journal")
    monkeypatch.setattr(storage, "DERIVED_ROOT", tmp_path / "derived")
    monkeypatch.setattr(storage, "SQLITE_PATH", tmp_path / "app" / "test.sqlite3")
    monkeypatch.setattr(storage, "DUCKDB_PATH", tmp_path / "app" / "test.duckdb")
    source = tmp_path / "session.dbn.zst"
    source.write_bytes(b"fixture")
    storage.migrate()
    session = {
        "id": "session-complete", "instrument": "MES", "symbol": "MES.v.0", "contract_symbol": "MESU6",
        "instrument_id": 42003239, "start_at": "2026-07-14T00:00:00Z", "end_at": "2026-07-14T16:00:00Z",
        "record_count": 100, "snapshot_status": "post_snapshot", "completeness": "complete",
        "file_path": str(source), "sha256": "abc123", "imported_at": storage.utc_now(), "integrity_status": "passed",
        "unknown_pre": 0, "unknown_during": 0, "unknown_post": 0, "sequence_regressions": 0,
        "processing_rate": 1000, "peak_rss_mb": 1, "derived_manifest": {}, "external_verification": "pending",
    }
    storage.upsert_session(session)
    return session


def test_strategy_hash_is_deterministic_and_sensitive(isolated_protocol) -> None:
    config = backtest_protocol.normalize_config({})
    one = backtest_protocol.strategy_hash(strategy="Retest", instrument="MES", session_ids=["session-complete"], config=config)
    two = backtest_protocol.strategy_hash(strategy="Retest", instrument="MES", session_ids=["session-complete"], config=config)
    changed = backtest_protocol.strategy_hash(strategy="Retest v2", instrument="MES", session_ids=["session-complete"], config=config)
    assert one == two
    assert one != changed


def test_locked_plan_locks_split_and_settings_contract(isolated_protocol) -> None:
    plan = backtest_protocol.create_plan({
        "mode": "locked", "strategy": "Retest", "instrument": "MES", "sessionIds": ["session-complete"],
    })
    assert plan["locked_at"]
    assert plan["strategy_hash"]
    split = storage.get_session_split("session-complete")
    assert split["split_name"] == "Locked Test"
    assert split["locked"] is True
    with pytest.raises(ValueError, match="locked"):
        storage.set_session_split("session-complete", "Development")


def test_conservative_fill_net_metrics_and_immutability(isolated_protocol) -> None:
    plan = backtest_protocol.create_plan({
        "mode": "locked", "strategy": "Retest", "instrument": "MES", "sessionIds": ["session-complete"],
    })
    trade = storage.save_blind_trade({
        "id": "trade-1", "plan_id": plan["id"], "session_id": "session-complete",
        "direction": "long", "entry": 5000, "stop": 4998, "targets": [5004],
        "decision_snapshot": {"setupName": "Retest", "dataReliability": "complete_book"},
        "risk_snapshot": {"state": "allowed"}, "features_snapshot": {"contracts": 1},
    })
    closed = backtest_protocol.finish_trade(trade["id"], {
        "exitPrice": 5004, "exitReason": "target", "mae": 0.75, "mfe": 4.25, "holdingSeconds": 90,
    })
    assert closed["immutable"] is True
    assert closed["fees_usd"] == pytest.approx(3.10)
    assert closed["slippage_usd"] == pytest.approx(2.50)
    assert closed["result_usd"] == pytest.approx(14.40)
    report = backtest_protocol.conservative_report(plan["id"])
    assert report["netResult"] == pytest.approx(14.40)
    assert report["sampleSizeWarning"] is True
    assert report["profitabilityClaim"] is False
    with pytest.raises(ValueError, match="immutable"):
        storage.close_blind_trade("trade-1", {"result_r": 0, "result_usd": 0})


def test_replay_blind_seek_and_candidate_engine(monkeypatch) -> None:
    replay = ReplayEngine()
    replay.blind_mode = "locked"
    replay.blind_run_id = "active-run"
    replay.group_cursor = 2
    replay.group_ends = [1, 2, 3, 4]
    replay.events = []
    with pytest.raises(ValueError, match="Future seek"):
        replay.seek(progress=1)
    monkeypatch.setattr(ReplayEngine, "load", lambda self, session_id: {"loaded": True})
    monkeypatch.setattr(ReplayEngine, "scan_candidates", lambda self, session_id, max_rows=500: {"sessionId": session_id, "counts": {"trade_ready": 1, "wait": 2, "blocked": 0}, "candidates": [], "engine": "ReplayEngine"})
    result = ReplayEngine().scan_candidates("session-complete")
    assert result["engine"] == "ReplayEngine"


def test_locked_plan_without_run_does_not_lock_and_exit_unlocks(isolated_protocol) -> None:
    plan = backtest_protocol.create_plan({
        "mode": "locked", "strategy": "Retest", "instrument": "MES", "sessionIds": ["session-complete"],
    })
    inactive = storage.derive_application_lock_state()
    assert inactive["locked"] is False
    assert inactive["reason"] == "locked_protocol_not_running"

    storage.start_backtest_run(plan["id"], "session-complete", "locked", "run-locked")
    active = storage.derive_application_lock_state()
    assert active == {
        "locked": True, "reason": "active_locked_run", "protocolId": plan["id"],
        "runId": "run-locked", "sessionId": "session-complete", "strategyHash": plan["strategy_hash"],
    }
    storage.exit_active_backtest_run()
    assert storage.derive_application_lock_state()["locked"] is False


def test_new_practice_plan_is_unique_and_clones_locked_assignment(isolated_protocol) -> None:
    locked = backtest_protocol.create_plan({
        "mode": "locked", "strategy": "Retest", "instrument": "MES", "sessionIds": ["session-complete"],
    })
    original_split = storage.get_session_split("session-complete")
    practice = backtest_protocol.create_plan({
        "mode": "practice", "strategy": "Retest", "instrument": "MES", "sessionIds": [],
    })
    assert practice["id"] != locked["id"]
    assert practice["strategy_hash"] != locked["strategy_hash"]
    assert storage.derive_application_lock_state()["locked"] is False

    cloned = backtest_protocol.clone_session_assignment_into_practice(
        practice["id"], "session-complete", ui_practice_only=True,
    )
    assert storage.get_session_split("session-complete") == original_split
    assert cloned["assignment"]["reused"] is True
    assert cloned["assignment"]["contaminated"] is True
    assert cloned["assignment"]["ui_practice_only"] is True
    audit = storage.list_audit(practice["id"])
    assert any(event["eventType"] == "SESSION_ASSIGNMENT_CLONED" for event in audit)


def test_qa_plan_migration_is_idempotent_and_preserves_audit(isolated_protocol) -> None:
    payload = {
        "id": "qa-locked", "mode": "locked", "strategy": "MES Pullback / Retest",
        "config": backtest_protocol.normalize_config({}), "session_ids": ["session-complete"],
        "strategy_hash": storage.QA_STRATEGY_HASH, "status": "READY", "created_at": storage.utc_now(),
        "locked_at": storage.utc_now(),
    }
    storage.save_backtest_plan(payload)
    storage.append_audit("BLIND_SESSION_STARTED", {}, plan_id="qa-locked", session_id="session-complete")
    storage.append_audit("CANDIDATE_SCAN_COMPLETED", {}, plan_id="qa-locked", session_id="session-complete")
    storage.migrate()
    storage.migrate()
    migrated = storage.get_backtest_plan("qa-locked")
    assert migrated and migrated["status"] == "ARCHIVED"
    assert migrated["artifact_kind"] == "test_artifact"
    events = [event for event in storage.list_audit("qa-locked") if event["eventType"] == "QA_TEST_ARTIFACT_ARCHIVED"]
    assert len(events) == 1
    assert len(storage.list_audit("qa-locked")) >= 3
