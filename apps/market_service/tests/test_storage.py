from __future__ import annotations

import json

from apps.market_service import storage


def test_journal_crud_export_and_import(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "APP_ROOT", tmp_path)
    monkeypatch.setattr(storage, "JOURNAL_ROOT", tmp_path / "journal")
    monkeypatch.setattr(storage, "SQLITE_PATH", tmp_path / "app.sqlite3")
    storage.migrate()
    payload = {
        "date": "2026-07-15", "session": "Replay", "symbol": "MES", "direction": "LONG",
        "setup": "Retest", "entry": 5000, "stop": 4998, "targets": [5004], "contracts": 1,
        "riskUsd": 10, "resultUsd": 20, "resultR": 2, "notes": "test", "emotion": "neutral",
    }
    created = storage.save_journal(payload, "entry-1")
    assert created["resultR"] == 2
    updated = storage.save_journal({**payload, "notes": "updated"}, "entry-1")
    assert updated["notes"] == "updated"
    assert "entry-1" in storage.journal_backup()
    assert "date,session,symbol" in storage.journal_csv()
    assert storage.import_journal([{**payload, "id": "entry-2"}]) == 1
    assert len(storage.list_journal()) == 2
    assert storage.delete_journal("entry-1")
    assert len(storage.list_journal()) == 1


def test_settings_never_enable_live_implicitly(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "APP_ROOT", tmp_path)
    monkeypatch.setattr(storage, "JOURNAL_ROOT", tmp_path / "journal")
    monkeypatch.setattr(storage, "SQLITE_PATH", tmp_path / "app.sqlite3")
    storage.migrate()
    result = storage.update_settings({"data": {"liveEnabled": True}, "ai": {"provider": "disabled", "enabled": True}})
    assert result["data"]["liveEnabled"] is False
    assert result["ai"]["enabled"] is False


def test_external_book_verification_is_bound_to_session_and_hash(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "APP_ROOT", tmp_path)
    monkeypatch.setattr(storage, "JOURNAL_ROOT", tmp_path / "journal")
    monkeypatch.setattr(storage, "SQLITE_PATH", tmp_path / "app.sqlite3")
    storage.migrate()
    for session_id, completeness, sha in (("complete", "complete", "hash-complete"), ("partial", "partial", "hash-partial")):
        source = tmp_path / f"{session_id}.dbn.zst"
        source.write_bytes(session_id.encode())
        storage.upsert_session({
            "id": session_id, "instrument": "MES", "symbol": "MES.v.0", "contract_symbol": "MESU6",
            "instrument_id": 42, "start_at": "2026-07-14T00:00:00Z", "end_at": "2026-07-14T00:10:00Z",
            "record_count": 10, "snapshot_status": "post_snapshot" if completeness == "complete" else "missing",
            "completeness": completeness, "file_path": str(source), "sha256": sha,
            "imported_at": storage.utc_now(), "integrity_status": "passed", "unknown_pre": 0,
            "unknown_during": 0, "unknown_post": 0, "sequence_regressions": 0,
            "processing_rate": 1, "peak_rss_mb": 1, "derived_manifest": {},
            "external_verification": "external_verification_pending",
        })
    with storage.connect() as database:
        database.execute(
            """UPDATE external_book_verifications SET status='passed', compared_groups=898,
               bbo_matches=898, top10_matches=898, mismatches=0 WHERE session_id='complete'"""
        )
    sessions = {item["id"]: item for item in storage.list_sessions()}
    assert sessions["complete"]["external_book_verification"]["status"] == "passed"
    assert sessions["complete"]["external_book_verification"]["comparedGroups"] == 898
    assert sessions["partial"]["external_book_verification"]["status"] == "pending"
    assert sessions["partial"]["external_book_verification"]["comparedGroups"] is None
