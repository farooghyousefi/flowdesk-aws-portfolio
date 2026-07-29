from __future__ import annotations

import pytest

from apps.connectors.databento.src.dbn_reader import F_LAST, F_SNAPSHOT, MboEvent, OrderBook, timestamp_iso
from apps.market_service.signal_engine import SignalEngine, SignalPolicy
from apps.market_service.strategy_search import (
    SIGNAL_TO_FILL_LATENCY_NS,
    CandidateRuntime,
    StrategySpec,
    curated_strategy_specs,
    strategy_direction,
    strategy_setup_decision,
    summarize_candidate,
)


def book_with_quote(
    *, bid_price: float | None, bid_size: int = 5, ask_price: float | None, ask_size: int = 5,
) -> OrderBook:
    """Build a minimal live OrderBook exposing exactly one bid and/or ask level."""
    book = OrderBook()
    next_order_id = 1

    def apply(action: str, side: str, price: float, size: int, flags: int = F_LAST) -> None:
        nonlocal next_order_id
        next_order_id += 1
        book.apply(MboEvent(
            timestamp=timestamp_iso(1), ts_event=1, action=action, side=side,
            price=round(price * 1_000_000_000), size=size, order_id=next_order_id,
            instrument_id=42, sequence=next_order_id, flags=flags, ts_recv=1,
            publisher_id=1, channel_id=1,
        ))

    apply("R", "N", 0, 0, F_SNAPSHOT | F_LAST)
    if bid_price is not None:
        apply("A", "B", bid_price, bid_size)
    if ask_price is not None:
        apply("A", "A", ask_price, ask_size)
    return book


def feature_contract(*, signed: int, momentum: int, imbalance: float, spread: float = 1.0) -> dict:
    return {
        "topOfBookLiquidityContracts": 20,
        "context": {
            "vwap": 4999.875, "regime": "momentum", "trendStrength": 0.8,
            "sessionPhase": "cash_open", "openingRange": {"complete": True, "high": 4999.5, "low": 4998.0},
        },
        "marketStructure": [{"timeframe": "5m", "state": "trend_up"}],
        "absorptionCandidates": [],
        "externalContext": {
            "calendarCoverage": "complete", "newsCoverage": "complete",
            "eventRisk": "clear", "newsRisk": "clear", "gate": "clear", "gateReasons": [],
        },
        "microstructure": {
            "orderBook": {
                "bestBid": "4999.75",
                "bestAsk": "5000.00",
                "midprice": "4999.875",
                "spreadTicks": spread,
                "queueImbalance": imbalance,
                "topOfBookLiquidityContracts": 20,
            },
            "tradeAggression": {
                "signedVolume": signed,
                "deltaMomentum": momentum,
            },
        },
    }


def test_strategy_direction_requires_aggression_momentum_book_alignment_and_spread() -> None:
    parameters = StrategySpec(20, 8, 0.10, 8, 14).parameters()
    assert strategy_direction(feature_contract(signed=30, momentum=12, imbalance=0.2), parameters) == "long"
    assert strategy_direction(feature_contract(signed=-30, momentum=-12, imbalance=-0.2), parameters) == "short"
    assert strategy_direction(feature_contract(signed=30, momentum=-12, imbalance=0.2), parameters) is None
    assert strategy_direction(feature_contract(signed=30, momentum=12, imbalance=0.2, spread=3), parameters) is None


def test_curated_search_contains_distinct_strategy_families() -> None:
    families = {spec.family for spec in curated_strategy_specs()}
    assert families == {
        "MES_L3_MOMENTUM", "MES_PULLBACK_RETEST", "MES_VWAP_MEAN_REVERSION",
        "MES_OPENING_RANGE_BREAKOUT", "MES_ABSORPTION_REVERSAL",
    }
    assert len(curated_strategy_specs()) >= 20


def test_replay_setup_uses_exact_search_parameters() -> None:
    strategy = {
        "strategy_hash": "a" * 64,
        "status": "PAPER_ACTIVE",
        "config": {"family": "MES_L3_MOMENTUM", "parameters": StrategySpec(20, 8, 0.10, 8, 14).parameters()},
    }
    risk = {"state": "allowed", "reasons": []}
    decision = strategy_setup_decision(
        timestamp="2026-07-14T13:00:00Z",
        completeness="complete",
        features=feature_contract(signed=30, momentum=12, imbalance=0.2),
        risk=risk,
        strategy=strategy,
    )
    assert decision["state"] == "trade_ready"
    assert decision["direction"] == "long"
    assert decision["invalidation"] == 4998.0
    assert decision["targets"] == [5003.5]
    assert decision["paperOnly"] is True


def test_candidate_runtime_applies_realistic_costs_and_segments() -> None:
    runtime = CandidateRuntime(spec=StrategySpec(20, 8, 0.10, 4, 8), index=1)
    features = feature_contract(signed=30, momentum=12, imbalance=0.2)
    runtime.maybe_open(timestamp_ns=1_000_000_000, segment="Development", features=features, fill_mode="realistic")
    assert runtime.open_position is None
    assert runtime.pending is not None
    book = book_with_quote(bid_price=4999.75, ask_price=5000.00)
    runtime.resolve_pending(1_000_000_000 + SIGNAL_TO_FILL_LATENCY_NS, book=book)
    assert runtime.open_position is not None
    runtime.on_trade(2_000_000_000, 5002.5, fill_mode="realistic")
    assert len(runtime.trades) == 1
    assert runtime.trades[0]["exitReason"] == "TARGET"
    assert runtime.trades[0]["decisionTimestampNs"] == 1_000_000_000
    assert runtime.trades[0]["entryTimestampNs"] == 1_000_000_000 + SIGNAL_TO_FILL_LATENCY_NS
    summary = summarize_candidate(runtime)
    assert summary["metrics"]["trades"] == 1
    assert summary["segmentMetrics"]["Development"]["netResultUsd"] > 0
    assert summary["paperEligible"] is False


def test_long_entry_never_fills_below_the_executable_ask() -> None:
    runtime = CandidateRuntime(spec=StrategySpec(20, 8, 0.10, 4, 8), index=1)
    runtime.maybe_open(
        timestamp_ns=1_000_000_000, segment="Development",
        features=feature_contract(signed=30, momentum=12, imbalance=0.2), fill_mode="realistic",
    )
    book = book_with_quote(bid_price=4999.75, ask_price=5002.00)
    runtime.resolve_pending(1_000_000_000 + SIGNAL_TO_FILL_LATENCY_NS, book=book)
    assert runtime.open_position is not None
    assert runtime.open_position.direction == "long"
    # Fills at the ask plus one realistic-mode slippage tick, never below the ask itself.
    assert runtime.open_position.entry_price >= 5002.00
    assert runtime.open_position.entry_price == pytest.approx(5002.25)


def test_short_entry_never_fills_above_the_executable_bid() -> None:
    runtime = CandidateRuntime(spec=StrategySpec(20, 8, 0.10, 4, 8), index=1)
    runtime.maybe_open(
        timestamp_ns=1_000_000_000, segment="Development",
        features=feature_contract(signed=-30, momentum=-12, imbalance=-0.2), fill_mode="realistic",
    )
    book = book_with_quote(bid_price=4998.00, ask_price=5000.25)
    runtime.resolve_pending(1_000_000_000 + SIGNAL_TO_FILL_LATENCY_NS, book=book)
    assert runtime.open_position is not None
    assert runtime.open_position.direction == "short"
    # Fills at the bid minus one realistic-mode slippage tick, never above the bid itself.
    assert runtime.open_position.entry_price <= 4998.00
    assert runtime.open_position.entry_price == pytest.approx(4997.75)


def test_resolve_pending_does_nothing_before_the_latency_delay_elapses() -> None:
    runtime = CandidateRuntime(spec=StrategySpec(20, 8, 0.10, 4, 8), index=1)
    runtime.maybe_open(
        timestamp_ns=1_000_000_000, segment="Development",
        features=feature_contract(signed=30, momentum=12, imbalance=0.2), fill_mode="realistic",
    )
    ready_at_ns = 1_000_000_000 + SIGNAL_TO_FILL_LATENCY_NS
    book = book_with_quote(bid_price=4999.75, ask_price=5000.00)
    runtime.resolve_pending(ready_at_ns - 1, book=book)
    assert runtime.open_position is None
    assert runtime.pending is not None


def test_resolve_pending_uses_the_fresh_quote_at_resolution_not_the_signal_time_quote() -> None:
    """The quote may move during the latency delay; resolve_pending must price off
    whatever the book shows when the delay elapses, never a value captured earlier.
    """
    runtime = CandidateRuntime(spec=StrategySpec(20, 8, 0.10, 4, 8), index=1)
    runtime.maybe_open(
        timestamp_ns=1_000_000_000, segment="Development",
        features=feature_contract(signed=30, momentum=12, imbalance=0.2), fill_mode="realistic",
    )
    # A book at signal time would have implied 5000.25; the book has since moved to 5010.00.
    later_book = book_with_quote(bid_price=5009.75, ask_price=5010.00)
    runtime.resolve_pending(1_000_000_000 + SIGNAL_TO_FILL_LATENCY_NS, book=later_book)
    assert runtime.open_position is not None
    assert runtime.open_position.entry_price == pytest.approx(5010.25)


def test_resolve_pending_produces_no_fill_when_a_quote_side_is_missing() -> None:
    runtime = CandidateRuntime(spec=StrategySpec(20, 8, 0.10, 4, 8), index=1)
    runtime.maybe_open(
        timestamp_ns=1_000_000_000, segment="Development",
        features=feature_contract(signed=30, momentum=12, imbalance=0.2), fill_mode="realistic",
    )
    book = book_with_quote(bid_price=4999.75, ask_price=None)
    runtime.resolve_pending(1_000_000_000 + SIGNAL_TO_FILL_LATENCY_NS, book=book)
    assert runtime.open_position is None
    assert runtime.pending is None
    assert runtime.trades == []


def test_resolve_pending_produces_no_fill_on_a_locked_or_crossed_market() -> None:
    runtime = CandidateRuntime(spec=StrategySpec(20, 8, 0.10, 4, 8), index=1)
    runtime.maybe_open(
        timestamp_ns=1_000_000_000, segment="Development",
        features=feature_contract(signed=30, momentum=12, imbalance=0.2), fill_mode="realistic",
    )
    crossed_book = book_with_quote(bid_price=5000.25, ask_price=5000.00)
    runtime.resolve_pending(1_000_000_000 + SIGNAL_TO_FILL_LATENCY_NS, book=crossed_book)
    assert runtime.open_position is None


def test_resolve_pending_adds_conservative_slippage_when_top_of_book_size_has_thinned() -> None:
    spec = StrategySpec(20, 8, 0.10, 4, 8)  # minimum_top_liquidity defaults to 2
    runtime = CandidateRuntime(spec=spec, index=1)
    runtime.maybe_open(
        timestamp_ns=1_000_000_000, segment="Development",
        features=feature_contract(signed=30, momentum=12, imbalance=0.2), fill_mode="realistic",
    )
    thin_book = book_with_quote(bid_price=4999.75, bid_size=5, ask_price=5000.00, ask_size=1)
    runtime.resolve_pending(1_000_000_000 + SIGNAL_TO_FILL_LATENCY_NS, book=thin_book)
    assert runtime.open_position is not None
    # 1 contract available against minimum_top_liquidity=2 leaves a shortfall of 1: the
    # usual 1-tick realistic slippage plus 1 extra conservative tick for the shortfall.
    assert runtime.open_position.entry_price == pytest.approx(5000.50)


def test_paper_active_strategy_can_emit_replay_signal_but_is_labeled_paper_only() -> None:
    engine = SignalEngine(policy=SignalPolicy(debounce_ms=0, minimum_hold_ms=0, cooldown_ms=0))
    setup = {
        "state": "trade_ready",
        "direction": "long",
        "setupName": "MES L3 Momentum",
        "entryZone": {"min": 4999.75, "max": 5000.0},
        "invalidation": 4998.0,
        "targets": [5003.5],
        "confidence": 80,
        "reasons": [],
    }
    risk = {"state": "allowed", "plannedRiskUsd": 75, "remainingDrawdown": 1500, "maximumContracts": 1, "reasons": []}
    first = engine.update(
        timestamp="2026-07-14T13:00:00Z", timestamp_ns=1_000_000_000,
        setup_decision=setup, risk=risk, features=feature_contract(signed=30, momentum=12, imbalance=0.2),
        completeness="complete", session_id=None, run_id=None, strategy_status="PAPER_ONLY",
    )
    second = engine.update(
        timestamp="2026-07-14T13:00:00.001Z", timestamp_ns=1_001_000_000,
        setup_decision=setup, risk=risk, features=feature_contract(signed=30, momentum=12, imbalance=0.2),
        completeness="complete", session_id=None, run_id=None, strategy_status="PAPER_ONLY",
    )
    assert first["status"] == "WAIT"
    assert second["status"] == "LONG"
    assert second["paperSignal"] is True
    assert second["strategyValidationStatus"] == "PAPER_ONLY"


def test_strategy_search_job_scans_once_saves_candidates_and_activates_paper(tmp_path, monkeypatch) -> None:
    from datetime import UTC, datetime, timedelta

    from apps.connectors.databento.src.dbn_reader import F_LAST, F_SNAPSHOT, MboEvent, timestamp_iso
    from apps.market_service import research, storage

    monkeypatch.setattr(storage, "APP_ROOT", tmp_path / "app")
    monkeypatch.setattr(storage, "JOURNAL_ROOT", tmp_path / "journal")
    monkeypatch.setattr(storage, "DERIVED_ROOT", tmp_path / "derived")
    monkeypatch.setattr(storage, "SQLITE_PATH", tmp_path / "app" / "test.sqlite3")
    monkeypatch.setattr(storage, "DUCKDB_PATH", tmp_path / "app" / "test.duckdb")
    storage.migrate()

    start = datetime(2026, 7, 14, 0, 0, tzinfo=UTC)
    start_ns = int(start.timestamp() * 1_000_000_000)
    events: list[MboEvent] = []
    sequence = 1

    def add_event(offset_ns: int, action: str, side: str, price: float, size: int, order_id: int, flags: int = F_LAST) -> None:
        nonlocal sequence
        ts = start_ns + offset_ns
        events.append(MboEvent(
            timestamp=timestamp_iso(ts), ts_event=ts, action=action, side=side,
            price=round(price * 1_000_000_000), size=size, order_id=order_id,
            instrument_id=42, sequence=sequence, flags=flags, ts_recv=ts,
            publisher_id=1, channel_id=1,
        ))
        sequence += 1

    add_event(0, "R", "N", 0, 0, 0, F_SNAPSHOT | F_LAST)
    add_event(1, "A", "B", 4999.75, 100, 1)
    add_event(2, "A", "A", 5000.00, 20, 2)
    price = 5000.0
    for second in range(1, 181):
        long_phase = (second // 10) % 2 == 0
        bid_size, ask_size = (100, 20) if long_phase else (20, 100)
        add_event(second * 1_000_000_000 - 2, "M", "B", 4999.75, bid_size, 1)
        add_event(second * 1_000_000_000 - 1, "M", "A", 5000.00, ask_size, 2)
        price += 0.25 if long_phase else -0.25
        add_event(second * 1_000_000_000, "T", "B" if long_phase else "A", price, 10, 1000 + second)

    data_file = tmp_path / "data" / "fake.dbn.zst"
    data_file.parent.mkdir(parents=True)
    data_file.write_bytes(b"fixture")
    storage.upsert_session({
        "id": "session-search", "instrument": "MES", "symbol": "MES.v.0", "contract_symbol": "MESU6",
        "instrument_id": 42, "start_at": start.isoformat().replace("+00:00", "Z"),
        "end_at": (start + timedelta(seconds=181)).isoformat().replace("+00:00", "Z"),
        "record_count": len(events), "snapshot_status": "post_snapshot", "completeness": "complete",
        "file_path": str(data_file), "sha256": "f" * 64, "imported_at": storage.utc_now(),
        "integrity_status": "passed", "unknown_pre": 0, "unknown_during": 0, "unknown_post": 0,
        "sequence_regressions": 0, "processing_rate": 1, "peak_rss_mb": 1, "derived_manifest": {},
        "external_verification": "pending", "schema_name": "mbo",
    })
    monkeypatch.setattr(research, "iter_events", lambda _: iter(events))
    monkeypatch.setattr(research, "curated_strategy_specs", lambda: [StrategySpec(5, 0, 0.1, 4, 6, cooldown_seconds=5, time_stop_seconds=20)])

    class ContextStub:
        calendar_covered = True
        news_covered = True
        coverage = {"economicCalendar": {"declaredCoverage": True}, "news": {"declaredCoverage": True}}

        def snapshot(self, _timestamp_ns: int) -> dict:
            return {
                "calendarCoverage": "complete", "newsCoverage": "complete",
                "eventRisk": "clear", "newsRisk": "clear", "gate": "clear", "gateReasons": [],
            }

    monkeypatch.setattr(research.HistoricalContextIndex, "load", lambda *_: ContextStub())

    created = research.create_research_job({
        "sessionId": "session-search", "mode": "search", "fillMode": "realistic", "seed": 7,
    })
    completed = research.run_research_job(created["job"]["id"])
    assert completed["status"] == "COMPLETED"
    assert completed["result"]["eventsProcessed"] == len(events)
    assert completed["result"]["topCandidates"]
    strategies = storage.list_strategy_versions()
    assert any(item["config"].get("family") == "MES_L3_MOMENTUM" for item in strategies)
    assert completed["result"]["automaticOrderExecution"] is False


def _fast_eligible_candidate_summary(spec: StrategySpec) -> dict:
    """A deterministic, genuinely paperEligible summarize_candidate() output.

    Built by feeding hand-crafted, all-winning trades through the real summarize_candidate
    logic, rather than depending on a synthetic MBO stream to organically produce a fast-sim
    winner — this isolates the realistic-execution-gate wiring test (below) from the fast
    strategy logic itself, which is exercised separately elsewhere.
    """
    runtime = CandidateRuntime(spec=spec, index=1)
    trade_id = 0
    for segment, count in (("Development", 5), ("Validation", 3), ("Intraday Holdout", 3)):
        for _ in range(count):
            trade_id += 1
            runtime.trades.append({
                "family": spec.family, "strategyName": spec.name, "direction": "long",
                "entryTimestampNs": trade_id * 1_000_000_000, "exitTimestampNs": trade_id * 1_000_000_000 + 5_000_000_000,
                "holdingSeconds": 5.0, "entryPrice": 5000.0, "exitPrice": 5004.0,
                "stopPrice": 4998.0, "targetPrice": 5004.0, "grossUsd": 80.0, "costUsd": 2.2,
                "netUsd": 77.8, "resultR": 1.5, "exitReason": "TARGET", "segment": segment,
                "regime": "momentum", "featureSnapshot": {},
            })
    return summarize_candidate(runtime, context_coverage={"calendar": True, "news": True})


def test_realistic_execution_gate_blocks_promotion_when_it_fails_and_allows_it_when_it_passes(tmp_path, monkeypatch) -> None:
    """A candidate the fast search finds eligible must not become PAPER_ACTIVE unless it
    also clears the realistic execution gate — and must become PAPER_ACTIVE once it does.
    """
    from datetime import UTC, datetime, timedelta

    from apps.connectors.databento.src.dbn_reader import F_LAST, F_SNAPSHOT, MboEvent, timestamp_iso
    from apps.market_service import research, storage

    monkeypatch.setattr(storage, "APP_ROOT", tmp_path / "app")
    monkeypatch.setattr(storage, "JOURNAL_ROOT", tmp_path / "journal")
    monkeypatch.setattr(storage, "DERIVED_ROOT", tmp_path / "derived")
    monkeypatch.setattr(storage, "SQLITE_PATH", tmp_path / "app" / "test.sqlite3")
    monkeypatch.setattr(storage, "DUCKDB_PATH", tmp_path / "app" / "test.duckdb")
    storage.migrate()

    start = datetime(2026, 7, 14, 0, 0, tzinfo=UTC)
    start_ns = int(start.timestamp() * 1_000_000_000)
    events: list[MboEvent] = []
    sequence = 1

    def add_event(offset_ns: int, action: str, side: str, price: float, size: int, order_id: int, flags: int = F_LAST) -> None:
        nonlocal sequence
        ts = start_ns + offset_ns
        events.append(MboEvent(
            timestamp=timestamp_iso(ts), ts_event=ts, action=action, side=side,
            price=round(price * 1_000_000_000), size=size, order_id=order_id,
            instrument_id=42, sequence=sequence, flags=flags, ts_recv=ts,
            publisher_id=1, channel_id=1,
        ))
        sequence += 1

    add_event(0, "R", "N", 0, 0, 0, F_SNAPSHOT | F_LAST)
    add_event(1, "A", "B", 4999.75, 100, 1)
    add_event(2, "A", "A", 5000.00, 20, 2)
    for second in range(1, 21):
        add_event(second * 1_000_000_000, "T", "B", 5000.0, 10, 1000 + second)

    data_file = tmp_path / "data" / "fake.dbn.zst"
    data_file.parent.mkdir(parents=True)
    data_file.write_bytes(b"fixture")
    storage.upsert_session({
        "id": "session-gate", "instrument": "MES", "symbol": "MES.v.0", "contract_symbol": "MESU6",
        "instrument_id": 42, "start_at": start.isoformat().replace("+00:00", "Z"),
        "end_at": (start + timedelta(seconds=21)).isoformat().replace("+00:00", "Z"),
        "record_count": len(events), "snapshot_status": "post_snapshot", "completeness": "complete",
        "file_path": str(data_file), "sha256": "e" * 64, "imported_at": storage.utc_now(),
        "integrity_status": "passed", "unknown_pre": 0, "unknown_during": 0, "unknown_post": 0,
        "sequence_regressions": 0, "processing_rate": 1, "peak_rss_mb": 1, "derived_manifest": {},
        "external_verification": "pending", "schema_name": "mbo",
    })
    spec = StrategySpec(5, 0, 0.1, 4, 6, cooldown_seconds=5, time_stop_seconds=20)
    monkeypatch.setattr(research, "iter_events", lambda _: iter(events))
    monkeypatch.setattr(research, "curated_strategy_specs", lambda: [spec])
    fake_summary = _fast_eligible_candidate_summary(spec)
    assert fake_summary["paperEligible"] is True
    monkeypatch.setattr(research, "summarize_candidate", lambda runtime, **_: fake_summary)

    class ContextStub:
        calendar_covered = True
        news_covered = True
        coverage = {"economicCalendar": {"declaredCoverage": True}, "news": {"declaredCoverage": True}}

        def snapshot(self, _timestamp_ns: int) -> dict:
            return {
                "calendarCoverage": "complete", "newsCoverage": "complete",
                "eventRisk": "clear", "newsRisk": "clear", "gate": "clear", "gateReasons": [],
            }

    monkeypatch.setattr(research.HistoricalContextIndex, "load", lambda *_: ContextStub())

    monkeypatch.setattr(
        research, "_realistic_execution_gate",
        lambda **_: {"passed": False, "reason": "TEST_FORCED_FAILURE", "fastTrades": 11, "realisticTrades": 1, "retention": 0.09},
    )
    created = research.create_research_job({"sessionId": "session-gate", "mode": "search", "fillMode": "realistic", "seed": 7})
    blocked = research.run_research_job(created["job"]["id"])
    blocked_top = blocked["result"]["topCandidates"][0]
    assert blocked_top["validation"]["paperEligible"] is True
    assert blocked_top["validation"]["paperStatus"] == "RESEARCH_ONLY"
    assert blocked_top["validation"]["realisticExecutionGate"]["passed"] is False
    assert blocked["result"]["activePaperStrategyHash"] is None
    assert not any(item["status"] == "PAPER_ACTIVE" for item in storage.list_strategy_versions())

    monkeypatch.setattr(
        research, "_realistic_execution_gate",
        lambda **_: {"passed": True, "reason": None, "fastTrades": 11, "realisticTrades": 9, "retention": 0.82},
    )
    created_2 = research.create_research_job({"sessionId": "session-gate", "mode": "search", "fillMode": "realistic", "seed": 7})
    allowed = research.run_research_job(created_2["job"]["id"])
    allowed_top = allowed["result"]["topCandidates"][0]
    assert allowed_top["validation"]["paperStatus"] == "PAPER_ACTIVE"
    assert allowed_top["validation"]["realisticExecutionGate"]["passed"] is True
    assert allowed["result"]["activePaperStrategyHash"] is not None
    assert any(item["status"] == "PAPER_ACTIVE" for item in storage.list_strategy_versions())


def test_independent_session_evidence_accumulates_across_search_runs_instead_of_resetting(tmp_path, monkeypatch) -> None:
    """A strategy's independentSessions/lockedSessions metrics must reflect all distinct
    qualifying sessions it has ever been evaluated against, not just the current run.

    Promotion (ValidationPolicy.minimum_independent_sessions=30) can only ever become
    reachable if this evidence accumulates across separate research runs sharing the same
    strategy_hash; a per-run constant would make promotion structurally impossible forever,
    even after enough real Databento sessions are purchased.
    """
    from datetime import UTC, datetime, timedelta

    from apps.connectors.databento.src.dbn_reader import F_LAST, F_SNAPSHOT, MboEvent, timestamp_iso
    from apps.market_service import research, storage

    monkeypatch.setattr(storage, "APP_ROOT", tmp_path / "app")
    monkeypatch.setattr(storage, "JOURNAL_ROOT", tmp_path / "journal")
    monkeypatch.setattr(storage, "DERIVED_ROOT", tmp_path / "derived")
    monkeypatch.setattr(storage, "SQLITE_PATH", tmp_path / "app" / "test.sqlite3")
    monkeypatch.setattr(storage, "DUCKDB_PATH", tmp_path / "app" / "test.duckdb")
    storage.migrate()

    def build_events(start_ns: int) -> list[MboEvent]:
        events: list[MboEvent] = []
        sequence = 1

        def add_event(offset_ns: int, action: str, side: str, price: float, size: int, order_id: int, flags: int = F_LAST) -> None:
            nonlocal sequence
            ts = start_ns + offset_ns
            events.append(MboEvent(
                timestamp=timestamp_iso(ts), ts_event=ts, action=action, side=side,
                price=round(price * 1_000_000_000), size=size, order_id=order_id,
                instrument_id=42, sequence=sequence, flags=flags, ts_recv=ts,
                publisher_id=1, channel_id=1,
            ))
            sequence += 1

        add_event(0, "R", "N", 0, 0, 0, F_SNAPSHOT | F_LAST)
        add_event(1, "A", "B", 4999.75, 100, 1)
        add_event(2, "A", "A", 5000.00, 20, 2)
        price = 5000.0
        for second in range(1, 61):
            long_phase = (second // 10) % 2 == 0
            bid_size, ask_size = (100, 20) if long_phase else (20, 100)
            add_event(second * 1_000_000_000 - 2, "M", "B", 4999.75, bid_size, 1)
            add_event(second * 1_000_000_000 - 1, "M", "A", 5000.00, ask_size, 2)
            price += 0.25 if long_phase else -0.25
            add_event(second * 1_000_000_000, "T", "B" if long_phase else "A", price, 10, 1000 + second)
        return events

    monkeypatch.setattr(research, "curated_strategy_specs", lambda: [StrategySpec(5, 0, 0.1, 4, 6, cooldown_seconds=5, time_stop_seconds=20)])

    class ContextStub:
        calendar_covered = True
        news_covered = True
        coverage = {"economicCalendar": {"declaredCoverage": True}, "news": {"declaredCoverage": True}}

        def snapshot(self, _timestamp_ns: int) -> dict:
            return {
                "calendarCoverage": "complete", "newsCoverage": "complete",
                "eventRisk": "clear", "newsRisk": "clear", "gate": "clear", "gateReasons": [],
            }

    monkeypatch.setattr(research.HistoricalContextIndex, "load", lambda *_: ContextStub())

    independent_session_counts = []
    for index, day in enumerate((14, 15)):
        start = datetime(2026, 7, day, 0, 0, tzinfo=UTC)
        start_ns = int(start.timestamp() * 1_000_000_000)
        events = build_events(start_ns)
        session_id = f"session-search-{index}"
        data_file = tmp_path / "data" / f"fake-{index}.dbn.zst"
        data_file.parent.mkdir(parents=True, exist_ok=True)
        data_file.write_bytes(b"fixture")
        # 6h05m qualifies as an independent Full-L3 day per session_qualification's
        # MIN_INDEPENDENT_SESSION_SECONDS gate, even though the fake event stream only
        # covers the first ~60 seconds of it.
        storage.upsert_session({
            "id": session_id, "instrument": "MES", "symbol": "MES.v.0", "contract_symbol": "MESU6",
            "instrument_id": 42, "start_at": start.isoformat().replace("+00:00", "Z"),
            "end_at": (start + timedelta(hours=6, minutes=5)).isoformat().replace("+00:00", "Z"),
            "record_count": len(events), "snapshot_status": "post_snapshot", "completeness": "complete",
            "file_path": str(data_file), "sha256": f"{index}" * 64, "imported_at": storage.utc_now(),
            "integrity_status": "passed", "unknown_pre": 0, "unknown_during": 0, "unknown_post": 0,
            "sequence_regressions": 0, "processing_rate": 1, "peak_rss_mb": 1, "derived_manifest": {},
            "external_verification": "pending", "schema_name": "mbo",
        })
        monkeypatch.setattr(research, "iter_events", lambda _, events=events: iter(events))

        created = research.create_research_job({
            "sessionId": session_id, "mode": "search", "fillMode": "realistic", "seed": 7,
        })
        completed = research.run_research_job(created["job"]["id"])
        assert completed["status"] == "COMPLETED"
        candidate = completed["result"]["topCandidates"][0]
        independent_session_counts.append(candidate["metrics"]["independentSessions"])

    assert independent_session_counts == [1, 2]
    strategy = next(
        item for item in storage.list_strategy_versions()
        if item["config"].get("family") == "MES_L3_MOMENTUM"
    )
    assert sorted(strategy["data_fingerprints"]) == ["0" * 64, "1" * 64]
