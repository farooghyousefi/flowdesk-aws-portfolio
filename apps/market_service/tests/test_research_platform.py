from __future__ import annotations

from datetime import UTC, datetime

import pytest

from apps.connectors.databento.src.dbn_reader import MboEvent
from apps.market_service import storage
from apps.market_service.data_health import derive_data_health
from apps.market_service.event_backtester import EventDrivenBacktester, FillModel, TradeIntent
from apps.market_service.instruments import size_position
from apps.market_service.market_events import NormalizedMarketEvent, normalize_events
from apps.market_service.market_providers import LiveProvider, LiveState, ReplayProvider
from apps.market_service.planner_jobs import create_estimate_job, retry_estimate_job, run_estimate_job
from apps.market_service.research import promote_strategy, reject_strategy, rollback_strategy
from apps.market_service.signal_engine import SignalEngine, SignalPolicy
from apps.market_service.validation import chronological_session_split, evaluate_promotion, purged_walk_forward_windows


@pytest.fixture()
def isolated_storage(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "APP_ROOT", tmp_path / "app")
    monkeypatch.setattr(storage, "JOURNAL_ROOT", tmp_path / "journal")
    monkeypatch.setattr(storage, "DERIVED_ROOT", tmp_path / "derived")
    monkeypatch.setattr(storage, "SQLITE_PATH", tmp_path / "app" / "test.sqlite3")
    monkeypatch.setattr(storage, "DUCKDB_PATH", tmp_path / "app" / "test.duckdb")
    storage.migrate()


def planner_request() -> dict[str, object]:
    return {
        "date": "2026-07-14", "timezone": "Europe/Berlin",
        "replayStart": "15:00", "replayEnd": "16:30", "contextMinutes": 30,
    }


def normalized(*, ts: int, action: str, side: str, price: float, size: int = 1, order_id: int = 1, sequence: int = 1) -> NormalizedMarketEvent:
    return NormalizedMarketEvent(
        event_type={"A": "order_add", "T": "trade"}.get(action, action),
        timestamp=datetime.fromtimestamp(ts / 1_000_000_000, UTC).isoformat(), timestamp_ns=ts,
        receive_timestamp_ns=ts, publisher_id=1, channel_id=1, sequence=sequence,
        stable_tie_breaker=sequence, side=side, price_fixed=round(price * 1_000_000_000),
        size=size, order_id=order_id, instrument_id=42, action=action, flags=128, snapshot=False,
    )


def test_estimate_job_persists_reuses_result_and_retries(isolated_storage) -> None:
    created = create_estimate_job(planner_request())
    reused = create_estimate_job(planner_request())
    assert reused["id"] == created["id"]
    assert reused["reused"] is True
    completed = run_estimate_job(created["id"], estimate_runner=lambda request: {"input": request, "downloadStarted": False})
    assert completed["status"] == "COMPLETED"
    assert storage.get_estimate_job(created["id"])["result"]["downloadStarted"] is False
    assert create_estimate_job(planner_request())["id"] == created["id"]

    failed_job = create_estimate_job({**planner_request(), "contextMinutes": 31})
    failed = run_estimate_job(failed_job["id"], estimate_runner=lambda _: (_ for _ in ()).throw(RuntimeError("metadata unavailable")))
    assert failed["status"] == "FAILED"
    assert failed["error"]["code"] == "RUNTIMEERROR"
    retried = retry_estimate_job(failed_job["id"])
    assert retried["status"] == "PENDING"
    assert retried["retryOf"] == failed_job["id"]


def test_normalized_events_use_timestamp_channel_sequence_and_stable_tie_breaker() -> None:
    def raw(sequence: int, publisher: int, channel: int) -> MboEvent:
        return MboEvent(
            timestamp="2026-07-15T13:00:00.000000000Z", ts_event=1_000, action="T", side="B",
            price=5_000_000_000_000, size=1, order_id=sequence, instrument_id=42,
            sequence=sequence, flags=128, ts_recv=1_001, publisher_id=publisher, channel_id=channel,
        )
    result = normalize_events([raw(3, 2, 1), raw(2, 1, 2), raw(1, 1, 1)])
    assert [(item.publisher_id, item.channel_id, item.sequence) for item in result] == [(1, 1, 1), (1, 2, 2), (2, 1, 3)]
    assert all(item.timestamp_ns == 1_000 for item in result)


def test_event_backtester_uses_latency_queue_and_never_fills_from_future_before_decision() -> None:
    events = [
        normalized(ts=1_000_000_000, action="A", side="B", price=5000.00, size=10, order_id=1),
        normalized(ts=1_000_000_001, action="A", side="A", price=5000.25, size=10, order_id=2),
        normalized(ts=1_100_000_000, action="T", side="A", price=5000.00, size=12, sequence=3),
        normalized(ts=1_200_000_000, action="T", side="B", price=5001.00, size=12, sequence=4),
    ]
    intent = TradeIntent(
        id="limit-1", decision_timestamp_ns=1_000_000_000, direction="long", order_type="LIMIT",
        entry_price=5000.00, stop_price=4999.00, targets=(5001.00,), contracts=2,
    )
    model = FillModel(mode="optimistic", latency_ms=50, limit_fill_probability=1)
    result = EventDrivenBacktester(fill_model=model, seed=9).run(events, [intent])
    assert result["trades"][0]["entryTimestampNs"] == 1_100_000_000
    assert result["trades"][0]["filledContracts"] == 2
    assert result["trades"][0]["exitReason"] == "TARGET"
    assert result["futureLeakageGuard"] is True

    late = TradeIntent(**{**intent.__dict__, "id": "late", "decision_timestamp_ns": 2_000_000_000})
    assert EventDrivenBacktester(fill_model=model).run(events, [late])["trades"] == []


def _thin_queue_limit_scenario() -> tuple[list[NormalizedMarketEvent], TradeIntent, FillModel]:
    events = [
        normalized(ts=1_000_000_000, action="A", side="B", price=5000.00, size=3, order_id=1),
        normalized(ts=1_000_000_001, action="A", side="A", price=5000.25, size=10, order_id=2),
        normalized(ts=1_100_000_000, action="T", side="A", price=5000.00, size=3, sequence=3),
        normalized(ts=1_200_000_000, action="T", side="B", price=5001.00, size=12, sequence=4),
    ]
    intent = TradeIntent(
        id="limit-1", decision_timestamp_ns=1_000_000_000, direction="long", order_type="LIMIT",
        entry_price=5000.00, stop_price=4999.00, targets=(5001.00,), contracts=2,
    )
    model = FillModel(mode="realistic", latency_ms=50, limit_fill_probability=0.5)
    return events, intent, model


def test_event_backtester_same_seed_same_data_same_config_gives_identical_results() -> None:
    events, intent, model = _thin_queue_limit_scenario()
    first = EventDrivenBacktester(fill_model=model, seed=0).run(events, [intent])
    second = EventDrivenBacktester(fill_model=model, seed=0).run(events, [intent])
    assert first["trades"] == second["trades"]
    assert first["metrics"] == second["metrics"]


def test_event_backtester_different_seed_changes_only_the_stochastic_fill_outcome() -> None:
    events, intent, model = _thin_queue_limit_scenario()
    # Same data and config; only the seed differs. The queue-depth roll this scenario
    # exercises is genuinely stochastic (limit_fill_probability=0.5), but must still be
    # fully reproducible per seed, not globally random.
    filled = EventDrivenBacktester(fill_model=model, seed=0).run(events, [intent])
    unfilled = EventDrivenBacktester(fill_model=model, seed=5).run(events, [intent])
    assert len(filled["trades"]) == 1
    assert len(unfilled["trades"]) == 0
    assert filled["fillModelVersion"] == unfilled["fillModelVersion"]


def test_streaming_market_backtest_covers_events_beyond_old_250k_cap_without_buffering_session() -> None:
    decision_ns = 300_000_000
    intent = TradeIntent(
        id="late-full-session-trade",
        decision_timestamp_ns=decision_ns,
        direction="long",
        order_type="MARKET",
        entry_price=5000.25,
        stop_price=4999.25,
        targets=(5001.25,),
        contracts=1,
        exit_level_reference="ENTRY_RELATIVE",
        time_stop_duration_ns=2_000_000_000,
    )

    def full_session_events():
        yield normalized(ts=1, action="A", side="B", price=5000.00, size=10, order_id=1, sequence=1)
        yield normalized(ts=2, action="A", side="A", price=5000.25, size=10, order_id=2, sequence=2)
        for index in range(3, 250_010):
            yield normalized(
                ts=index + 1_000,
                action="N",
                side="N",
                price=0,
                size=0,
                order_id=index,
                sequence=index,
            )
        yield normalized(
            ts=decision_ns + 35_000_000,
            action="N",
            side="N",
            price=0,
            size=0,
            sequence=250_011,
        )
        yield normalized(
            ts=decision_ns + 100_000_000,
            action="T",
            side="B",
            price=5001.25,
            size=1,
            sequence=250_012,
        )

    result = EventDrivenBacktester(
        fill_model=FillModel(mode="optimistic", latency_ms=35, entry_slippage_ticks=0),
    ).run_market_streaming(full_session_events(), [intent])
    assert result["eventsProcessed"] > 250_000
    assert result["eventBufferSize"] == 1
    assert result["streaming"] is True
    assert result["metrics"]["trades"] == 1
    assert result["trades"][0]["exitReason"] == "TARGET"


def test_entry_relative_levels_reanchor_to_actual_fill_but_absolute_market_levels_do_not() -> None:
    events = [
        normalized(ts=1, action="A", side="B", price=5000.75, size=10, order_id=1, sequence=1),
        normalized(ts=2, action="A", side="A", price=5001.00, size=10, order_id=2, sequence=2),
        normalized(ts=40_000_000, action="N", side="N", price=0, size=0, sequence=3),
        normalized(ts=100_000_000, action="T", side="B", price=5003.00, size=1, sequence=4),
    ]
    base = {
        "decision_timestamp_ns": 0,
        "direction": "long",
        "order_type": "MARKET",
        "entry_price": 5000.00,
        "stop_price": 4999.00,
        "targets": (5002.00,),
        "contracts": 1,
    }
    model = FillModel(mode="optimistic", latency_ms=35, entry_slippage_ticks=0)
    relative = EventDrivenBacktester(fill_model=model).run_market_streaming(
        iter(events),
        [TradeIntent(id="relative", **base, exit_level_reference="ENTRY_RELATIVE")],
    )["trades"][0]
    absolute = EventDrivenBacktester(fill_model=model).run_market_streaming(
        iter(events),
        [TradeIntent(id="absolute", **base, exit_level_reference="ABSOLUTE_MARKET_LEVEL")],
    )["trades"][0]

    assert relative["entryPrice"] == 5001.00
    assert relative["stopPrice"] == 5000.00
    assert relative["targets"] == [5003.00]
    assert relative["exitLevelReference"] == "ENTRY_RELATIVE"
    assert absolute["stopPrice"] == 4999.00
    assert absolute["targets"] == [5002.00]
    assert absolute["exitLevelReference"] == "ABSOLUTE_MARKET_LEVEL"
    assert abs(float(absolute["resultR"])) < abs(float(relative["resultR"]))


def test_streaming_market_fill_walks_real_l3_depth_instead_of_inventing_top_level_size() -> None:
    events = [
        normalized(ts=1, action="A", side="B", price=4999.75, size=10, order_id=1, sequence=1),
        normalized(ts=2, action="A", side="A", price=5000.00, size=1, order_id=2, sequence=2),
        normalized(ts=3, action="A", side="A", price=5000.25, size=2, order_id=3, sequence=3),
        normalized(ts=40_000_000, action="N", side="N", price=0, size=0, sequence=4),
        normalized(ts=100_000_000, action="T", side="B", price=5001.00, size=1, sequence=5),
    ]
    intent = TradeIntent(
        id="depth-walk",
        decision_timestamp_ns=0,
        direction="long",
        order_type="MARKET",
        entry_price=5000.00,
        stop_price=4999.00,
        targets=(5001.00,),
        contracts=3,
        exit_level_reference="ABSOLUTE_MARKET_LEVEL",
    )
    trade = EventDrivenBacktester(
        fill_model=FillModel(mode="optimistic", latency_ms=35, entry_slippage_ticks=0),
    ).run_market_streaming(iter(events), [intent])["trades"][0]
    assert trade["filledContracts"] == 3
    assert trade["partialFill"] is False
    assert trade["entryPrice"] == pytest.approx((5000.00 + 5000.25 * 2) / 3, abs=1e-6)
    assert trade["fillDetails"]["depthWalked"] is True


def test_grouped_streaming_gate_keeps_candidate_positions_independent_in_one_event_pass() -> None:
    events = [
        normalized(ts=1, action="A", side="B", price=4999.75, size=10, order_id=1, sequence=1),
        normalized(ts=2, action="A", side="A", price=5000.00, size=10, order_id=2, sequence=2),
        normalized(ts=40_000_000, action="N", side="N", price=0, size=0, sequence=3),
        normalized(ts=100_000_000, action="T", side="B", price=5001.00, size=1, sequence=4),
    ]
    common = {
        "decision_timestamp_ns": 0,
        "direction": "long",
        "order_type": "MARKET",
        "entry_price": 5000.00,
        "stop_price": 4999.00,
        "targets": (5001.00,),
        "contracts": 1,
        "exit_level_reference": "ENTRY_RELATIVE",
    }
    result = EventDrivenBacktester(
        fill_model=FillModel(mode="optimistic", latency_ms=35, entry_slippage_ticks=0),
    ).run_market_streaming_groups(
        iter(events),
        {
            "candidate-1": [TradeIntent(id="one", **common)],
            "candidate-2": [TradeIntent(id="two", **common)],
        },
    )

    assert result["eventsProcessed"] == len(events)
    assert result["eventBufferSize"] == 1
    assert result["groups"]["candidate-1"]["metrics"]["trades"] == 1
    assert result["groups"]["candidate-2"]["metrics"]["trades"] == 1
    assert result["groups"]["candidate-1"]["trades"][0]["intentId"] == "one"
    assert result["groups"]["candidate-2"]["trades"][0]["intentId"] == "two"


def test_simulated_l3_book_preserves_remaining_size_after_partial_cancel_and_fill() -> None:
    from apps.market_service.event_backtester import SimulatedBook

    book = SimulatedBook()
    book.apply(normalized(ts=1, action="A", side="A", price=5000.25, size=10, order_id=9))
    book.apply(normalized(ts=2, action="C", side="A", price=5000.25, size=3, order_id=9))
    assert book.depth_at("A", 5_000_250_000_000) == 7
    assert book.orders[9][2] == 7
    book.apply(normalized(ts=3, action="F", side="A", price=5000.25, size=2, order_id=9))
    assert book.depth_at("A", 5_000_250_000_000) == 5
    assert book.orders[9][2] == 5


def test_position_sizing_obeys_tick_value_drawdown_contract_and_liquidity_limits() -> None:
    sized = size_position(
        symbol="MES", entry_price=5000, stop_price=4998, maximum_risk_usd=100,
        remaining_drawdown_usd=80, maximum_contracts=5, liquidity_contract_limit=2,
        estimated_slippage_ticks=2, round_trip_fees_usd=2.2,
    )
    assert sized["allowed"] is True
    assert sized["contracts"] == 2
    assert sized["riskUsd"] <= 80
    blocked = size_position(
        symbol="MES", entry_price=5000, stop_price=4980, maximum_risk_usd=25,
        remaining_drawdown_usd=25, maximum_contracts=1, liquidity_contract_limit=1,
        estimated_slippage_ticks=4, round_trip_fees_usd=2.2,
    )
    assert blocked["allowed"] is False
    assert blocked["reasonCode"] == "MINIMUM_POSITION_EXCEEDS_RISK"


def test_chronological_validation_purges_embargo_and_blocks_small_samples() -> None:
    sessions = [{"id": f"s-{index}", "start_at": f"2026-07-{index + 1:02d}T13:00:00Z"} for index in range(20)]
    split = chronological_session_split(list(reversed(sessions)))
    flattened = [item for name in ("Development", "Training", "Validation", "Pilot", "Locked Test", "Forward Paper") for item in split[name]]
    assert flattened == [item["id"] for item in sessions]
    windows = purged_walk_forward_windows(flattened, train_size=6, test_size=3, embargo=2)
    assert windows
    assert not set(windows[0]["train"]) & set(windows[0]["test"])
    assert len(windows[0]["embargo"]) == 2
    gate = evaluate_promotion({"trades": 3, "netExpectancyUsd": 25, "profitFactor": 4})
    assert gate["eligible"] is False
    assert "MINIMUM_SESSIONS" in gate["failedReasons"]
    assert gate["profitabilityClaim"] is False


def test_signal_state_machine_debounces_and_risk_guard_overrides() -> None:
    engine = SignalEngine(policy=SignalPolicy(debounce_ms=0, minimum_hold_ms=0, cooldown_ms=1000))
    setup = {
        "state": "trade_ready", "direction": "long", "setupName": "MES test",
        "entryZone": {"min": 5000.0, "max": 5000.5}, "invalidation": 4998.0,
        "targets": [5004.0], "confidence": 85,
        "reasons": [{"code": "DELTA", "state": "fulfilled", "titleKey": "setup.deltaShift"}],
    }
    risk = {"state": "allowed", "plannedRiskUsd": 75, "remainingDrawdown": 1000, "maximumContracts": 3, "reasons": []}
    first = engine.update(timestamp="2026-07-15T13:00:00Z", timestamp_ns=1_000_000_000, setup_decision=setup, risk=risk, features={"topOfBookLiquidityContracts": 5}, completeness="complete", session_id=None, run_id=None, strategy_status="VALIDATED")
    second = engine.update(timestamp="2026-07-15T13:00:00.001Z", timestamp_ns=1_001_000_000, setup_decision=setup, risk=risk, features={"topOfBookLiquidityContracts": 5}, completeness="complete", session_id=None, run_id=None, strategy_status="VALIDATED")
    assert first["status"] == "WAIT"
    assert second["status"] == "LONG"
    assert second["contracts"] >= 1
    blocked = engine.update(timestamp="2026-07-15T13:00:00.002Z", timestamp_ns=1_002_000_000, setup_decision=setup, risk={**risk, "state": "blocked"}, features={}, completeness="complete", session_id=None, run_id=None, strategy_status="VALIDATED")
    assert blocked["status"] == "NO_TRADE"


def test_partial_data_health_never_claims_full_l3(tmp_path) -> None:
    trade_file = tmp_path / "trades.parquet"
    trade_file.write_bytes(b"fixture")
    health = derive_data_health({
        "completeness": "partial", "schema_name": "mbo", "integrity_status": "passed",
        "snapshot_status": "post_snapshot", "sequence_regressions": 0, "sequence_gaps": 0,
        "record_count": 500, "instrument_id": "42", "derived_manifest": {"trades": str(trade_file)},
    })
    assert health["fullL3Claim"] is False
    assert health["bookReconstructionStatus"] == "PARTIAL"
    assert health["signalCapability"] == "REPLAY_ONLY"
    assert health["featureAvailability"]["queueFeatures"] is False


def test_strategy_promotion_requires_gate_and_reject_rollback_are_audited(isolated_storage) -> None:
    strategy_hash = "a" * 64
    storage.save_strategy_version({
        "id": "strategy-a", "name": "MES baseline", "version": "research-v1",
        "strategy_hash": strategy_hash, "status": "RESEARCH_ONLY", "validation_status": "PENDING",
        "config": {}, "data_fingerprints": ["data-a"],
    })
    with pytest.raises(ValueError, match="validation gate"):
        promote_strategy(strategy_hash)
    storage.save_experiment({
        "id": "experiment-a", "name": "eligible", "strategy_name": "MES baseline",
        "strategy_hash": strategy_hash, "parameter_hash": "p" * 64, "dataset_fingerprint": "data-a",
        "split_name": "Forward Paper", "seed": 7, "fill_model_version": "fill-v1:realistic",
        "cost_model_version": "cost-v1", "feature_version": "features-v1", "code_version": "code-v1",
        "status": "COMPLETED", "config": {}, "metrics": {"trades": 120}, "validation": {"eligible": True},
    })
    assert promote_strategy(strategy_hash)["status"] == "ACTIVE"
    assert rollback_strategy(strategy_hash)["validation_status"] == "ROLLBACK"
    assert reject_strategy(strategy_hash)["status"] == "REJECTED"
    events = [item["eventType"] for item in storage.list_audit()]
    assert "MODEL_PROMOTED" in events
    assert "MODEL_ROLLED_BACK" in events
    assert "MODEL_REJECTED" in events


def test_replay_and_live_providers_share_events_and_live_gaps_disable_signals() -> None:
    first = normalized(ts=1_000, action="A", side="B", price=5000, sequence=1)
    snapshot = NormalizedMarketEvent(**{**first.__dict__, "sequence": 2, "stable_tie_breaker": 2, "snapshot": True})
    gap = NormalizedMarketEvent(**{**first.__dict__, "sequence": 4, "stable_tie_breaker": 4})
    assert [item.sequence for item in ReplayProvider([snapshot, first]).events()] == [1, 2]
    live = LiveProvider()
    live.connect()
    assert live.state == LiveState.CONNECTING
    assert live.ingest(first) is True
    assert live.signal_eligible is False
    assert live.ingest(snapshot) is True
    assert live.state == LiveState.LIVE
    assert live.signal_eligible is True
    assert live.ingest(gap) is False
    assert live.state == LiveState.DEGRADED
    assert live.status()["reasonCode"] == "SEQUENCE_GAP"
    assert live.status()["automaticOrderExecution"] is False
