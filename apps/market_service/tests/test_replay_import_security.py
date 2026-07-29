from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.connectors.databento.src.config import ConnectorError, REPO_ROOT, resolve_data_file
from apps.market_service.importer import external_verification_status, import_file
from apps.market_service.replay import ReplayEngine

DEMO = REPO_ROOT / "data" / "databento" / "raw" / "MES" / "2026-07-13" / "MES.v.0_mbo_20260713T235955Z_20260714T000010Z.dbn.zst"
PARTIAL = REPO_ROOT / "data" / "databento" / "raw" / "MES" / "2026-07-14" / "MES.v.0_mbo_20260714T133000Z_20260714T134000Z.dbn.zst"


@pytest.fixture(scope="module")
def complete_session() -> dict:
    return import_file(str(DEMO))


def test_import_manifest_hash_snapshot_fixed_prices_and_partial_file(complete_session) -> None:
    assert complete_session["completeness"] == "complete"
    assert complete_session["sha256"] == "163f7fffa57c765b63ca107243fc6311858716b5b884221a77ef429ff4cc8771"
    assert complete_session["record_count"] == 15343
    assert complete_session["derived_manifest"]["bars"].endswith(".parquet")
    partial = import_file(str(PARTIAL))
    assert partial["completeness"] == "partial"
    assert partial["record_count"] == 626614


def test_replay_is_deterministic_and_controls_work(complete_session) -> None:
    replay = ReplayEngine()
    first = replay.load(complete_session["id"])
    signature = (first["eventCursor"], first["book"]["bestBid"]["priceFixed"], first["features"]["tradeSummary"]["delta"])
    assert replay.play()["playing"] is True
    assert replay.pause()["playing"] is False
    stepped = replay.step_group()
    assert stepped["eventGroupCursor"] >= first["eventGroupCursor"]
    replay.set_speed("50")
    assert replay.state()["speed"] == "50"
    sought = replay.seek(progress=.9)
    assert sought["progress"] >= .89
    reset = replay.reset()
    reset_signature = (reset["eventCursor"], reset["book"]["bestBid"]["priceFixed"], reset["features"]["tradeSummary"]["delta"])
    assert reset_signature == signature
    another = ReplayEngine().load(complete_session["id"])
    assert (another["eventCursor"], another["book"]["bestBid"]["priceFixed"], another["features"]["tradeSummary"]["delta"]) == signature


def test_path_traversal_and_outside_paths_are_blocked(tmp_path) -> None:
    outside = tmp_path / "secret.dbn.zst"
    outside.write_bytes(b"not dbn")
    with pytest.raises(ConnectorError):
        resolve_data_file(str(outside))


def test_external_verification_requires_matching_passed_report(tmp_path) -> None:
    report_root = tmp_path / "reports"
    report_root.mkdir()
    report = {
        "passed": True,
        "mboFile": str(DEMO),
        "request": {"instrumentId": 42003239},
    }
    (report_root / "verification.json").write_text(json.dumps(report))
    assert external_verification_status(DEMO, 42003239, report_root=report_root) == "externally_verified"
    assert external_verification_status(DEMO, 7, report_root=report_root) == "external_verification_pending"


def test_service_has_no_order_execution_routes() -> None:
    from apps.market_service.service import app

    paths = {route.path for route in app.routes}
    assert not any(path.startswith("/orders") or path.startswith("/broker") for path in paths)
    assert "/live/health" in paths
