from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from apps.connectors.databento.src.config import ConnectorConfig, ConnectorError, build_request
from apps.connectors.databento.src.dbn_reader import DbnSummary
from apps.connectors.databento.src.download import (
    assert_daily_budget,
    output_path,
    record_download_cost,
    require_confirmation,
)
from apps.connectors.databento.src.manifest import build_manifest, write_manifest


def test_download_without_confirm_is_blocked() -> None:
    with pytest.raises(ConnectorError, match="--confirm"):
        require_confirmation(False)


def test_daily_cost_limit_is_enforced(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.json"
    now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    record_download_cost(Decimal("4.75"), path=ledger, now=now)
    config = ConnectorConfig("secret", Decimal("1"), Decimal("5"))
    with pytest.raises(ConnectorError, match="daily cost limit"):
        assert_daily_budget(Decimal("0.26"), config, path=ledger, now=now)


def test_output_path_is_bounded_to_mes_day(tmp_path: Path) -> None:
    request = build_request("2026-07-14T13:30:00Z", "2026-07-14T13:40:00Z")
    result = output_path(request, root=tmp_path)
    assert result.parent == tmp_path / "2026-07-14"
    assert result.name == "MES.v.0_mbo_20260714T133000Z_20260714T134000Z.dbn.zst"


def test_manifest_contains_integrity_and_no_secret(tmp_path: Path) -> None:
    data_file = tmp_path / "sample.dbn.zst"
    data_file.write_bytes(b"dbn-test-data")
    request = build_request("2026-07-14T13:30:00Z", "2026-07-14T13:40:00Z")
    summary = DbnSummary(
        file=str(data_file),
        dataset="GLBX.MDP3",
        schema="mbo",
        record_count=2,
        first_timestamp="2026-07-14T13:30:00.000000001Z",
        last_timestamp="2026-07-14T13:30:00.000000002Z",
        instrument_ids=[123],
        raw_symbols=["MES.v.0"],
        action_counts={"A": 2},
    )
    manifest = build_manifest(
        data_file,
        request,
        Decimal("0.12"),
        summary,
        downloaded_at=datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
    )
    manifest_path = write_manifest(data_file, manifest)
    persisted = json.loads(manifest_path.read_text())
    assert persisted["recordCount"] == 2
    assert persisted["instrumentIds"] == [123]
    assert persisted["rawSymbols"] == ["MES.v.0"]
    assert len(persisted["sha256"]) == 64
    assert "api" not in json.dumps(persisted).lower()
