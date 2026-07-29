from __future__ import annotations

from pathlib import Path

import databento_dbn as dbn
import pytest


@pytest.fixture
def synthetic_dbn(tmp_path: Path) -> Path:
    metadata = dbn.Metadata(
        dataset="GLBX.MDP3",
        start=1,
        end=5,
        stype_in=dbn.SType.CONTINUOUS,
        stype_out=dbn.SType.INSTRUMENT_ID,
        schema=dbn.Schema.MBO,
        symbols=["MES.v.0"],
    )
    records = [
        dbn.MBOMsg(
            publisher_id=1,
            instrument_id=123,
            ts_event=1,
            order_id=10,
            price=5_000_000_000_000,
            size=2,
            action=dbn.Action.ADD,
            side=dbn.Side.BID,
            ts_recv=1,
            flags=128,
            sequence=1,
        ),
        dbn.MBOMsg(
            publisher_id=1,
            instrument_id=123,
            ts_event=2,
            order_id=11,
            price=5_000_250_000_000,
            size=3,
            action=dbn.Action.ADD,
            side=dbn.Side.ASK,
            ts_recv=2,
            flags=128,
            sequence=2,
        ),
    ]
    path = tmp_path / "MES.v.0_mbo_test.dbn.zst"
    path.write_bytes(metadata.encode() + b"".join(bytes(record) for record in records))
    return path
