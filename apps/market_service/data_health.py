from __future__ import annotations

from pathlib import Path
from typing import Any


def derive_data_health(session: dict[str, Any]) -> dict[str, Any]:
    manifest = session.get("derived_manifest") or {}
    external = session.get("external_book_verification") or {}

    def exists(name: str) -> bool:
        value = manifest.get(name)
        return bool(value and Path(str(value)).is_file())

    mbo_available = str(session.get("schema_name", "mbo")) == "mbo"
    mbp10_available = external.get("status") == "passed"
    trades_available = exists("trades")
    ohlcv_available = exists("bars")
    complete_l3 = (
        session.get("completeness") == "complete"
        and mbo_available
        and session.get("integrity_status") == "passed"
        and str(session.get("snapshot_status", "")).lower() in {"post_snapshot", "snapshot_ready"}
        and int(session.get("sequence_regressions", 0)) == 0
        and int(session.get("out_of_order_events", session.get("sequence_regressions", 0))) == 0
    )
    if complete_l3:
        capability = "FULL_L3_SIGNAL"
    elif mbp10_available and trades_available:
        capability = "L2_ORDERFLOW_SIGNAL"
    elif ohlcv_available:
        capability = "CHART_CONTEXT_ONLY"
    elif mbo_available and int(session.get("record_count", 0)) > 0:
        capability = "REPLAY_ONLY"
    else:
        capability = "UNUSABLE"
    features = {
        "domReconstruction": complete_l3,
        "queueFeatures": complete_l3,
        "pullingStacking": complete_l3,
        "absorptionCandidates": complete_l3,
        "tradeFlow": trades_available,
        "delta": trades_available,
        "footprint": exists("footprint"),
        "vwap": trades_available,
        "bars": ohlcv_available,
        "liveSignal": False,
        "replaySignal": capability in {"FULL_L3_SIGNAL", "L2_ORDERFLOW_SIGNAL"},
    }
    return {
        "activeSession": False,
        "completeness": session.get("completeness"),
        "mboL3Available": mbo_available,
        "mbp10Available": mbp10_available,
        "tradesAvailable": trades_available,
        "ohlcvAvailable": ohlcv_available,
        "snapshotPosition": session.get("snapshot_status"),
        # For symbol-filtered MBO, venue sequence numbers also advance for
        # messages from other instruments on the same channel. These jumps are
        # informational and are not evidence of missing events for this book.
        "sequenceGaps": int(session.get("sequence_gaps", 0)),
        "sequenceGapSemantics": "VENUE_CHANNEL_JUMPS",
        "outOfOrderEvents": int(session.get("out_of_order_events", session.get("sequence_regressions", 0))),
        "duplicateEvents": int(session.get("duplicate_events", 0)),
        "contractMapping": session.get("contract_mapping_status", "resolved"),
        "instrumentId": session.get("instrument_id"),
        "timeRange": {"start": session.get("start_at"), "end": session.get("end_at")},
        "bookReconstructionStatus": "COMPLETE" if complete_l3 else "PARTIAL" if mbo_available else "UNAVAILABLE",
        "featureAvailability": features,
        "signalCapability": capability,
        "fullL3Claim": complete_l3,
    }

