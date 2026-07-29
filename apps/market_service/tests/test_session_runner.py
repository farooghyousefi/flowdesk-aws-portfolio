from __future__ import annotations

from datetime import UTC, datetime

import databento_dbn as dbn
import pytest

from apps.connectors.databento.src.dbn_reader import F_LAST, F_SNAPSHOT
from apps.market_service.session_runner import (
    DailyResearchError,
    _candidate_rank,
    run_daily_strategy_backtest,
)


def test_candidate_ranking_prefers_trade_evidence_over_an_idle_zero_score() -> None:
    idle = {
        "strategyName": "Idle",
        "rankingScore": 0.0,
        "metrics": {"trades": 0},
    }
    tested_loss = {
        "strategyName": "Tested loss",
        "rankingScore": -4.0,
        "metrics": {"trades": 7},
    }

    ranked = sorted((idle, tested_loss), key=_candidate_rank, reverse=True)

    assert ranked[0]["strategyName"] == "Tested loss"


def test_daily_session_runner_reads_dbn_without_database_or_order_side_effects(tmp_path) -> None:
    start = int(datetime(2026, 4, 26, tzinfo=UTC).timestamp() * 1_000_000_000)
    metadata = dbn.Metadata(
        dataset="GLBX.MDP3",
        start=start,
        end=start + 5_000_000_000,
        stype_in=dbn.SType.CONTINUOUS,
        stype_out=dbn.SType.INSTRUMENT_ID,
        schema=dbn.Schema.MBO,
        symbols=["MES.v.0"],
    )

    def message(
        *,
        offset: int,
        order_id: int,
        price: float,
        size: int,
        action: dbn.Action,
        side: dbn.Side,
        sequence: int,
        flags: int = F_LAST,
        instrument_id: int = 123,
    ) -> dbn.MBOMsg:
        timestamp = start + offset
        return dbn.MBOMsg(
            publisher_id=1,
            instrument_id=instrument_id,
            ts_event=timestamp,
            order_id=order_id,
            price=round(price * 1_000_000_000),
            size=size,
            action=action,
            side=side,
            ts_recv=timestamp,
            flags=flags,
            sequence=sequence,
        )

    records = [
        message(
            offset=0,
            order_id=0,
            price=0,
            size=0,
            action=dbn.Action.CLEAR,
            side=dbn.Side.NONE,
            sequence=1,
            flags=F_SNAPSHOT | F_LAST,
        ),
        message(
            offset=1,
            order_id=10,
            price=5000.00,
            size=20,
            action=dbn.Action.ADD,
            side=dbn.Side.BID,
            sequence=2,
        ),
        message(
            offset=2,
            order_id=11,
            price=5000.25,
            size=20,
            action=dbn.Action.ADD,
            side=dbn.Side.ASK,
            sequence=3,
        ),
        message(
            offset=1_000_000_000,
            order_id=0,
            price=5000.25,
            size=5,
            action=dbn.Action.TRADE,
            side=dbn.Side.BID,
            sequence=4,
        ),
        message(
            offset=2_000_000_000,
            order_id=0,
            price=5000.50,
            size=5,
            action=dbn.Action.TRADE,
            side=dbn.Side.BID,
            sequence=5,
        ),
    ]
    path = tmp_path / "session.dbn"
    path.write_bytes(metadata.encode() + b"".join(bytes(record) for record in records))
    progress: list[dict] = []

    result = run_daily_strategy_backtest(
        path,
        session_date="2026-04-26",
        data_fingerprint="a" * 64,
        progress=progress.append,
    )

    assert result["eventsProcessed"] == len(records)
    assert result["candidateCount"] >= 20
    assert result["automaticOrderExecution"] is False
    assert result["paperPromotionAllowed"] is False
    assert result["profitabilityClaim"] is False
    assert progress[0]["event"] == "SESSION_SCAN_STARTED"
    assert progress[-1]["event"] == "REALISTIC_GATE_COMPLETED"


def test_daily_session_runner_rejects_multiple_mapped_instruments(tmp_path) -> None:
    start = int(datetime(2026, 4, 26, tzinfo=UTC).timestamp() * 1_000_000_000)
    metadata = dbn.Metadata(
        dataset="GLBX.MDP3",
        start=start,
        end=start + 1_000_000_000,
        stype_in=dbn.SType.CONTINUOUS,
        stype_out=dbn.SType.INSTRUMENT_ID,
        schema=dbn.Schema.MBO,
        symbols=["MES.v.0"],
    )

    def message(instrument_id: int, sequence: int) -> dbn.MBOMsg:
        return dbn.MBOMsg(
            publisher_id=1,
            instrument_id=instrument_id,
            ts_event=start + sequence,
            order_id=sequence,
            price=5_000_000_000_000,
            size=1,
            action=dbn.Action.ADD,
            side=dbn.Side.BID,
            ts_recv=start + sequence,
            flags=F_LAST,
            sequence=sequence,
        )

    path = tmp_path / "roll-day.dbn"
    path.write_bytes(
        metadata.encode()
        + bytes(message(123, 1))
        + bytes(message(456, 2))
    )

    with pytest.raises(DailyResearchError, match="exactly one mapped"):
        run_daily_strategy_backtest(
            path,
            session_date="2026-04-26",
            data_fingerprint="b" * 64,
        )
