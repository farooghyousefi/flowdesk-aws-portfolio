from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from apps.connectors.databento.src.dbn_reader import OrderBook, iter_events

from .event_backtester import EventDrivenBacktester, FillModel, TradeIntent
from .features import OrderflowFeatures
from .market_events import stream_normalized_events
from .microstructure import MicrostructureFeatures
from .strategy_search import (
    CandidateRuntime,
    curated_strategy_specs,
    segment_name,
    summarize_candidate,
)


ProgressCallback = Callable[[dict[str, Any]], None]
MINIMUM_GATE_TRADES = 5
MINIMUM_GATE_RETENTION = 0.5


class DailyResearchError(ValueError):
    """A deterministic source or research-contract violation."""


def _candidate_rank(candidate: dict[str, Any]) -> tuple[bool, bool, float, int]:
    """Prefer actual evidence over the mathematically higher score of no activity."""
    trades = int(candidate.get("metrics", {}).get("trades") or 0)
    return (
        trades >= MINIMUM_GATE_TRADES,
        trades > 0,
        float(candidate.get("rankingScore") or 0),
        trades,
    )


def _utc_day_bounds(session_date: str) -> tuple[int, int]:
    parsed = date.fromisoformat(session_date)
    start = datetime.combine(parsed, datetime.min.time(), tzinfo=UTC)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1_000_000_000), int(end.timestamp() * 1_000_000_000)


def _candidate_intents(
    candidate: dict[str, Any],
    *,
    data_fingerprint: str,
) -> list[TradeIntent]:
    trades = candidate["trades"]
    duration_ns = int(candidate["parameters"].get("timeStopSeconds", 90)) * 1_000_000_000
    return [
        TradeIntent(
            id=f"session-gate:{candidate['candidateIndex']}:{index}",
            decision_timestamp_ns=int(trade["decisionTimestampNs"]),
            direction=trade["direction"],
            order_type="MARKET",
            entry_price=float(trade["entryPrice"]),
            stop_price=float(trade["stopPrice"]),
            targets=(float(trade["targetPrice"]),),
            contracts=1,
            exit_level_reference="ENTRY_RELATIVE",
            valid_until_ns=int(trade["decisionTimestampNs"]) + 15_000_000_000,
            time_stop_duration_ns=duration_ns,
            signal_timestamp_ns=int(trade["decisionTimestampNs"]),
            market_regime=str(trade.get("regime") or "unknown"),
            feature_snapshot=dict(trade.get("featureSnapshot") or {}),
            data_fingerprint=data_fingerprint,
            strategy_version=f"daily-search:{candidate['strategyName']}",
            model_version="candidate-runtime-v2",
        )
        for index, trade in enumerate(trades)
    ]


def _compact_fast_trade(trade: dict[str, Any]) -> dict[str, Any]:
    return {
        key: trade.get(key)
        for key in (
            "direction",
            "decisionTimestampNs",
            "entryTimestampNs",
            "exitTimestampNs",
            "holdingSeconds",
            "entryPrice",
            "exitPrice",
            "stopPrice",
            "targetPrice",
            "grossUsd",
            "costUsd",
            "netUsd",
            "resultR",
            "exitReason",
            "segment",
            "regime",
        )
    }


def _compact_realistic_trade(trade: dict[str, Any]) -> dict[str, Any]:
    return {
        "intentId": trade.get("intentId"),
        "direction": trade.get("direction"),
        "decisionTimestampNs": trade.get("decisionTimestampNs"),
        "entryTimestampNs": trade.get("entryTimestampNs"),
        "exitTimestampNs": trade.get("exitTimestampNs"),
        "holdingSeconds": round(float(trade.get("holdingNanoseconds") or 0) / 1_000_000_000, 3),
        "entryPrice": trade.get("entryPrice"),
        "exitPrice": trade.get("exitPrice"),
        "stopPrice": trade.get("stopPrice"),
        "targets": trade.get("targets"),
        "grossUsd": trade.get("grossUsd"),
        "costUsd": trade.get("feesUsd"),
        "slippageUsd": trade.get("slippageUsd"),
        "netUsd": trade.get("netUsd"),
        "resultR": trade.get("resultR"),
        "exitReason": trade.get("exitReason"),
        "regime": trade.get("marketRegime"),
    }


def _compact_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        key: candidate.get(key)
        for key in (
            "candidateIndex",
            "family",
            "strategyName",
            "parameters",
            "metrics",
            "segmentMetrics",
            "regimeMetrics",
            "rankingScore",
            "paperEligible",
            "paperFailedReasons",
            "diagnosis",
        )
    } | {
        "tradeEvidence": [
            _compact_fast_trade(trade)
            for trade in candidate.get("trades", [])
        ],
    }


def _gate_candidates(
    candidates: list[dict[str, Any]],
    *,
    path: Path,
    fill_mode: str,
    seed: int,
    data_fingerprint: str,
) -> tuple[list[dict[str, Any]], int, int]:
    eligible = [
        candidate
        for candidate in candidates
        if len(candidate.get("trades", [])) > 0
    ]
    if not eligible:
        return (
            [
                {
                    "candidateIndex": candidate["candidateIndex"],
                    "strategyName": candidate["strategyName"],
                    "evaluated": False,
                    "passed": False,
                    "reason": "INSUFFICIENT_TRADES_FOR_GATE",
                    "fastTrades": len(candidate.get("trades", [])),
                    "realisticTrades": 0,
                    "retention": 0.0,
                    "tradeEvidence": [],
                }
                for candidate in candidates
            ],
            0,
            1,
        )

    grouped = EventDrivenBacktester(
        fill_model=FillModel(mode=fill_mode),
        seed=seed,
    ).run_market_streaming_groups(
        stream_normalized_events(path),
        {
            str(candidate["candidateIndex"]): _candidate_intents(
                candidate,
                data_fingerprint=data_fingerprint,
            )
            for candidate in eligible
        },
    )
    grouped_results = grouped["groups"]
    evidence = []
    for candidate in candidates:
        fast_trades = len(candidate.get("trades", []))
        if fast_trades == 0:
            evidence.append(
                {
                    "candidateIndex": candidate["candidateIndex"],
                    "strategyName": candidate["strategyName"],
                    "evaluated": False,
                    "passed": False,
                    "reason": "INSUFFICIENT_TRADES_FOR_GATE",
                    "fastTrades": fast_trades,
                    "realisticTrades": 0,
                    "retention": 0.0,
                    "tradeEvidence": [],
                }
            )
            continue
        group = grouped_results[str(candidate["candidateIndex"])]
        metrics = group["metrics"]
        realistic_trades = int(metrics["trades"])
        retention = realistic_trades / fast_trades
        expectancy = float(metrics.get("netExpectancyUsd") or 0)
        passed = (
            realistic_trades >= MINIMUM_GATE_TRADES
            and retention >= MINIMUM_GATE_RETENTION
            and expectancy > 0
        )
        if passed:
            reason = None
        elif realistic_trades < MINIMUM_GATE_TRADES or retention < MINIMUM_GATE_RETENTION:
            reason = "TOO_FEW_TRADES_SURVIVE_REALISTIC_FILLS"
        else:
            reason = "NEGATIVE_EXPECTANCY_UNDER_REALISTIC_FILLS"
        evidence.append(
            {
                "candidateIndex": candidate["candidateIndex"],
                "strategyName": candidate["strategyName"],
                "evaluated": True,
                "passed": passed,
                "reason": reason,
                "fastTrades": fast_trades,
                "realisticTrades": realistic_trades,
                "retention": round(retention, 4),
                "metrics": metrics,
                "tradeEvidence": [
                    _compact_realistic_trade(trade)
                    for trade in group["trades"]
                ],
                "streaming": True,
                "fillModelVersion": grouped["fillModelVersion"],
                "seed": seed,
            }
        )
    return evidence, int(grouped["eventsProcessed"]), int(grouped["eventBufferSize"])


def run_daily_strategy_backtest(
    path: Path,
    *,
    session_date: str,
    data_fingerprint: str,
    fill_mode: str = "realistic",
    seed: int = 7,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Run a bounded-memory strategy search and realistic gate for one UTC DBN day.

    This function has no database, network, or order-execution side effects. It reads the
    same local DBN file twice: once for fast candidate discovery and once for the full
    streaming execution gate of the best candidate.
    """
    if fill_mode not in {"optimistic", "realistic", "stressed"}:
        raise ValueError("fill_mode must be optimistic, realistic, or stressed.")
    emit = progress or (lambda _: None)
    start_ns, end_ns = _utc_day_bounds(session_date)
    runtimes = [
        CandidateRuntime(spec=spec, index=index)
        for index, spec in enumerate(curated_strategy_specs(), start=1)
    ]
    pending_runtimes: list[CandidateRuntime] = []
    book = OrderBook()
    features = OrderflowFeatures()
    microstructure = MicrostructureFeatures()
    actions: Counter[str] = Counter()
    instrument_ids: set[int] = set()
    processed = 0
    last_timestamp_ns = 0
    last_trade_price = 0.0
    last_signal_bucket = -1

    emit({"event": "SESSION_SCAN_STARTED", "date": session_date, "candidateCount": len(runtimes)})
    for event in iter_events(path):
        instrument_ids.add(event.instrument_id)
        if len(instrument_ids) > 1:
            raise DailyResearchError(
                "Daily research requires exactly one mapped futures instrument per source file."
            )
        processed += 1
        last_timestamp_ns = event.ts_event
        actions[event.action] += 1
        before_order = book.orders.get(event.order_id)
        features.observe(event, before_order=before_order)
        book.apply(event)
        microstructure.observe(event, book=book, before_order=before_order)

        if (
            pending_runtimes
            and book.is_snapshot_ready
            and start_ns <= event.ts_event < end_ns
        ):
            unresolved: list[CandidateRuntime] = []
            for runtime in pending_runtimes:
                runtime.resolve_pending(event.ts_event, book=book)
                if runtime.pending is not None:
                    unresolved.append(runtime)
            pending_runtimes = unresolved

        if event.action == "T" and book.is_snapshot_ready and start_ns <= event.ts_event < end_ns:
            last_trade_price = event.price / 1_000_000_000
            for runtime in runtimes:
                runtime.on_trade(event.ts_event, last_trade_price, fill_mode=fill_mode)
            signal_bucket = event.ts_event // 1_000_000_000
            if signal_bucket != last_signal_bucket:
                last_signal_bucket = signal_bucket
                feature_contract = features.contract(data_complete=True)
                microstructure_contract = microstructure.contract(book)
                feature_contract["microstructure"] = microstructure_contract
                feature_contract["topOfBookLiquidityContracts"] = microstructure_contract[
                    "orderBook"
                ]["topOfBookLiquidityContracts"]
                feature_contract["externalContext"] = {
                    "calendarCoverage": "missing",
                    "newsCoverage": "missing",
                    "eventRisk": "unknown",
                    "newsRisk": "unknown",
                    "gate": "clear",
                    "gateReasons": [],
                }
                segment = segment_name(event.ts_event, start_ns, end_ns)
                for runtime in runtimes:
                    was_pending = runtime.pending is not None
                    runtime.maybe_open(
                        timestamp_ns=event.ts_event,
                        segment=segment,
                        features=feature_contract,
                        fill_mode=fill_mode,
                    )
                    if not was_pending and runtime.pending is not None:
                        pending_runtimes.append(runtime)
        if processed % 1_000_000 == 0:
            emit(
                {
                    "event": "SESSION_SCAN_PROGRESS",
                    "date": session_date,
                    "eventsProcessed": processed,
                    "lastTimestampNs": last_timestamp_ns,
                }
            )

    if last_trade_price:
        for runtime in runtimes:
            runtime.close_at_end(last_timestamp_ns, last_trade_price, fill_mode=fill_mode)

    summaries = sorted(
        (
            summarize_candidate(
                runtime,
                context_coverage={"calendar": False, "news": False},
            )
            for runtime in runtimes
        ),
        key=_candidate_rank,
        reverse=True,
    )
    top = summaries[:8]
    gate_candidate = next(
        (
            candidate
            for candidate in summaries
            if int(candidate.get("metrics", {}).get("trades") or 0) >= MINIMUM_GATE_TRADES
        ),
        top[0] if top else None,
    )
    emit(
        {
            "event": "SESSION_SCAN_COMPLETED",
            "date": session_date,
            "eventsProcessed": processed,
            "topCandidate": top[0]["strategyName"] if top else None,
        }
    )
    realistic_candidates, gate_events_processed, gate_buffer_size = _gate_candidates(
        summaries,
        path=path,
        fill_mode=fill_mode,
        seed=seed,
        data_fingerprint=data_fingerprint,
    )
    gate = (
        next(
            (
                candidate
                for candidate in realistic_candidates
                if candidate["candidateIndex"] == gate_candidate["candidateIndex"]
            ),
            None,
        )
        if gate_candidate
        else None
    )
    if gate is not None:
        gate = {
            **gate,
            "eventsProcessed": gate_events_processed,
            "eventBufferSize": gate_buffer_size,
        }
    emit(
        {
            "event": "REALISTIC_GATE_COMPLETED",
            "date": session_date,
            "passed": bool(gate and gate["passed"]),
            "reason": gate.get("reason") if gate else "NO_CANDIDATE",
            "eventsProcessed": gate.get("eventsProcessed", 0) if gate else 0,
        }
    )
    return {
        "sessionDate": session_date,
        "sourceFingerprint": data_fingerprint,
        "fillMode": fill_mode,
        "seed": seed,
        "eventsProcessed": processed,
        "instrumentId": next(iter(instrument_ids), None),
        "actionCounts": dict(sorted(actions.items())),
        "candidateCount": len(runtimes),
        "topCandidates": top,
        "candidateEvidence": [_compact_candidate(candidate) for candidate in summaries],
        "realisticCandidateEvidence": realistic_candidates,
        "realisticExecutionGate": gate,
        "contextCoverage": {"calendar": False, "news": False},
        "automaticOrderExecution": False,
        "paperPromotionAllowed": False,
        "profitabilityClaim": False,
    }
