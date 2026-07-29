from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import UTC, datetime
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from apps.connectors.databento.src.dbn_reader import OrderBook, iter_events

from .event_backtester import BacktestInterrupted, EventDrivenBacktester, FillModel, TradeIntent
from .features import OrderflowFeatures
from .market_events import NormalizedMarketEvent, stream_normalized_events
from .microstructure import MicrostructureFeatures
from .research_context import HistoricalContextIndex, sync_context_files
from .session_qualification import (
    excluded_session_diagnostics,
    parse_utc_datetime,
    qualifying_independent_full_l3_sessions,
)
from .storage import (
    append_audit,
    get_experiment,
    get_research_job,
    get_session,
    get_session_split,
    list_experiments,
    list_model_versions,
    list_research_jobs,
    list_signal_snapshots,
    list_strategy_versions,
    save_experiment,
    save_model_version,
    save_research_job,
    save_strategy_version,
    session_library,
    update_experiment,
    update_research_job,
    utc_now,
)
from .strategy_search import (
    CandidateRuntime,
    curated_strategy_specs,
    metrics_for_trades,
    parse_timestamp_ns,
    segment_name,
    summarize_candidate,
)
from .validation import evaluate_promotion


FEATURE_VERSION = "market-structure-microstructure-context-v2"
FILL_MODEL_VERSION = "fill-v1"
COST_MODEL_VERSION = "mes-cost-v1"
CODE_VERSION = "flowdesk-research-v3"
BASELINE_MODEL_ID = "rules-baseline-v1"


def _accumulate_session_fingerprints(existing_strategy: dict[str, Any] | None, session_fingerprint: str) -> list[str]:
    """Merge a new session's fingerprint into a strategy's accumulated evidence.

    A strategy is only promotion-eligible once it has been evaluated against many
    independent sessions (see ValidationPolicy). That evidence must accumulate across
    separate research runs sharing the same strategy_hash, not reset on every run.
    """
    prior = list((existing_strategy or {}).get("data_fingerprints") or [])
    return list(dict.fromkeys([*prior, session_fingerprint]))


def _independent_session_metrics(data_fingerprints: list[str]) -> dict[str, int]:
    """Count distinct qualifying independent/locked sessions backing a strategy's evidence.

    This must reflect actual accumulated sessions, not a per-run constant: hardcoding
    independentSessions to 1 would make MINIMUM_SESSIONS structurally unreachable in
    evaluate_promotion() forever, even after enough real Databento sessions are purchased.
    """
    by_sha256 = {item["sha256"]: item for item in session_library()}
    evaluated = [by_sha256[fingerprint] for fingerprint in data_fingerprints if fingerprint in by_sha256]
    qualifying = qualifying_independent_full_l3_sessions(evaluated)
    locked = [item for item in qualifying if item["split"]["split_name"] == "Locked Test"]
    return {"independentSessions": len(qualifying), "lockedSessions": len(locked)}


MINIMUM_GATE_TRADES = 5
MINIMUM_GATE_TRADE_RETENTION = 0.5


def _realistic_execution_gate(
    *, candidate: dict[str, Any], events: Any, fill_mode: str,
    seed: int, session_fingerprint: str,
) -> dict[str, Any]:
    """Re-verify the fast search's top candidate through the realistic, latency-aware
    EventDrivenBacktester before it may become PAPER_ACTIVE.

    CandidateRuntime (strategy_search.py) is a deliberately cheap, single-pass
    approximation of execution. This gate independently re-simulates the exact same trade
    decisions (direction, timing, stop, target) through the same engine and cost model
    used for baseline research, and only passes if a meaningful majority of those trades
    still fill, and remain net profitable, once full latency and queue-depth-derived fills
    are applied.

    The event source is consumed as a chronological stream. The gate therefore covers the
    full relevant session without retaining millions of MBO events in RAM. Candidate stops
    and targets are explicitly ENTRY_RELATIVE and are re-anchored to the realistic fill.
    """
    fast_trades = candidate["trades"]
    if len(fast_trades) < MINIMUM_GATE_TRADES:
        return {
            "passed": False, "reason": "INSUFFICIENT_TRADES_FOR_GATE",
            "fastTrades": len(fast_trades), "realisticTrades": 0, "retention": 0.0,
        }
    time_stop_seconds = int(candidate["parameters"].get("timeStopSeconds", 90))
    intents = [
        TradeIntent(
            id=f"gate:{candidate['strategyName']}:{index}",
            decision_timestamp_ns=int(trade.get("decisionTimestampNs", trade["entryTimestampNs"])),
            direction=trade["direction"],
            order_type="MARKET",
            entry_price=float(trade["entryPrice"]),
            stop_price=float(trade["stopPrice"]),
            targets=(float(trade["targetPrice"]),),
            contracts=1,
            exit_level_reference="ENTRY_RELATIVE",
            valid_until_ns=int(trade.get("decisionTimestampNs", trade["entryTimestampNs"])) + 15_000_000_000,
            time_stop_duration_ns=time_stop_seconds * 1_000_000_000,
            data_fingerprint=session_fingerprint,
            strategy_version=f"search-gate:{candidate['strategyName']}",
            model_version="candidate-runtime-v1",
        )
        for index, trade in enumerate(fast_trades)
    ]
    backtest = EventDrivenBacktester(
        fill_model=FillModel(mode=fill_mode), seed=seed,
    ).run_market_streaming(events, intents)
    metrics = backtest["metrics"]
    realistic_trades = int(metrics["trades"])
    retention = realistic_trades / len(fast_trades)
    net_expectancy = float(metrics.get("netExpectancyUsd") or 0)
    passed = realistic_trades >= MINIMUM_GATE_TRADES and retention >= MINIMUM_GATE_TRADE_RETENTION and net_expectancy > 0
    if passed:
        reason = None
    elif realistic_trades < MINIMUM_GATE_TRADES or retention < MINIMUM_GATE_TRADE_RETENTION:
        reason = "TOO_FEW_TRADES_SURVIVE_REALISTIC_FILLS"
    else:
        reason = "NEGATIVE_EXPECTANCY_UNDER_REALISTIC_FILLS"
    return {
        "passed": passed,
        "reason": reason,
        "fastTrades": len(fast_trades),
        "realisticTrades": realistic_trades,
        "retention": round(retention, 4),
        "realisticNetExpectancyUsd": metrics.get("netExpectancyUsd"),
        "realisticProfitFactor": metrics.get("profitFactor"),
        "fillModelVersion": backtest["fillModelVersion"],
        "seed": seed,
        "streaming": True,
        "eventsProcessed": backtest["eventsProcessed"],
        "eventBufferSize": backtest["eventBufferSize"],
    }


class ResearchJobControl:
    """Thread-safe cooperative control for long-running research work."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status: str | None = None

    def request(self, status: str) -> None:
        with self._lock:
            self._status = status

    def status(self) -> str | None:
        with self._lock:
            return self._status


ControlCheck = Callable[[], str | None]


def _control_status(job_id: str, control_check: ControlCheck | None) -> str | None:
    if control_check is not None:
        requested = control_check()
        if requested:
            return requested
    current = get_research_job(job_id, include_result=False)
    if current and current["status"] in {"PAUSED", "CANCELLED"}:
        return str(current["status"])
    return None


def _finish_interruption(
    job_id: str,
    experiment_id: str,
    status: str,
    *,
    processed: int,
    last_timestamp_ns: int,
) -> dict[str, Any]:
    checkpoint = {
        "eventsProcessed": processed,
        "lastTimestampNs": str(last_timestamp_ns),
        "phase": "interrupted" if status == "INTERRUPTED" else status.lower(),
    }
    current = get_research_job(job_id, include_result=False)
    if not current:
        raise ValueError("Research job not found during interruption.")
    if status == "PAUSED":
        current = update_research_job(job_id, "PAUSED", checkpoint=checkpoint)
        update_experiment(experiment_id, "PAUSED")
    elif status == "CANCELLED":
        current = update_research_job(job_id, "CANCELLED", checkpoint=checkpoint, completed_at=current.get("completed_at") or utc_now())
        update_experiment(experiment_id, "CANCELLED")
    else:
        current = update_research_job(job_id, current["status"], checkpoint=checkpoint)
    append_audit(
        "RESEARCH_JOB_INTERRUPTED",
        {"jobId": job_id, "status": status, "checkpoint": checkpoint},
        session_id=current["session_id"],
    )
    return current


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _default_config(payload: dict[str, Any]) -> dict[str, Any]:
    fill_mode = str(payload.get("fillMode") or "realistic").lower()
    if fill_mode not in {"optimistic", "realistic", "stressed"}:
        raise ValueError("Fill mode must be optimistic, realistic, or stressed.")
    mode = str(payload.get("mode") or "search").lower()
    if mode not in {"search", "baseline"}:
        raise ValueError("Research mode must be search or baseline.")
    strategy = str(payload.get("strategy") or ("MES L3 Strategy Search" if mode == "search" else "MES Orderflow Baseline"))
    return {
        "mode": mode,
        "strategy": strategy,
        "parameters": {
            "deltaThreshold": int(payload.get("deltaThreshold", 20)),
            "stopTicks": int(payload.get("stopTicks", 8)),
            "targetTicks": int(payload.get("targetTicks", 16)),
            "candidateCooldownSeconds": int(payload.get("candidateCooldownSeconds", 30)),
        },
        "fillMode": fill_mode,
        "seed": int(payload.get("seed", 7)),
        "maximumBacktestEvents": int(payload.get("maximumBacktestEvents", 250_000)),
        "chunkSize": int(payload.get("chunkSize", 25_000)),
    }


def create_research_job(payload: dict[str, Any]) -> dict[str, Any]:
    session_id = str(payload.get("sessionId") or "")
    session = get_session(session_id)
    if not session:
        raise ValueError("Research session not found.")
    if session["integrity_status"] not in {"passed", "warning"}:
        raise ValueError("Research requires a session that passed integrity validation.")
    config = _default_config(payload)
    parameters = config["parameters"]
    parameter_hash = _hash(parameters)
    strategy_hash = _hash({
        "strategy": config["strategy"], "parameters": parameters,
        "featureVersion": FEATURE_VERSION, "fillModelVersion": FILL_MODEL_VERSION,
        "costModelVersion": COST_MODEL_VERSION, "codeVersion": CODE_VERSION,
    })
    experiment_id = str(uuid.uuid4())
    experiment = save_experiment({
        "id": experiment_id,
        "name": str(payload.get("name") or f"{config['strategy']} · {session['start_at'][:10]}"),
        "strategy_name": config["strategy"],
        "strategy_hash": strategy_hash,
        "parameter_hash": parameter_hash,
        "dataset_fingerprint": session["sha256"],
        "split_name": get_session_split(session_id)["split_name"],
        "seed": config["seed"],
        "fill_model_version": f"{FILL_MODEL_VERSION}:{config['fillMode']}",
        "cost_model_version": COST_MODEL_VERSION,
        "feature_version": FEATURE_VERSION,
        "code_version": CODE_VERSION,
        "status": "QUEUED",
        "config": config,
    })
    job = save_research_job({
        "id": str(uuid.uuid4()), "experiment_id": experiment_id, "session_id": session_id,
        "status": "QUEUED", "progress": 0, "config": config,
    })
    if config["mode"] == "baseline":
        save_strategy_version({
            "id": f"strategy-{strategy_hash[:16]}", "name": config["strategy"], "version": "research-v1",
            "strategy_hash": strategy_hash, "status": "RESEARCH_ONLY", "validation_status": "PENDING",
            "config": config, "data_fingerprints": [session["sha256"]],
        })
    save_model_version({
        "id": BASELINE_MODEL_ID, "name": "Rule-based baseline", "version": "v1",
        "model_type": "deterministic_rules", "status": "BASELINE", "feature_version": FEATURE_VERSION,
        "calibration": {"status": "not_calibrated", "probabilityClaim": False},
    })
    append_audit("RESEARCH_JOB_CREATED", {
        "jobId": job["id"], "experimentId": experiment_id, "strategyHash": strategy_hash,
        "datasetFingerprint": session["sha256"], "fillMode": config["fillMode"],
    }, session_id=session_id)
    return {"job": job, "experiment": experiment}



def run_research_job(job_id: str, *, control_check: ControlCheck | None = None) -> dict[str, Any]:
    job = get_research_job(job_id, include_result=False)
    if not job:
        raise ValueError("Research job not found.")
    if str(job.get("config", {}).get("mode") or "baseline") == "search":
        return _run_strategy_search_job(job_id, control_check=control_check)
    return _run_baseline_research_job(job_id, control_check=control_check)


def _run_strategy_search_job(job_id: str, *, control_check: ControlCheck | None = None) -> dict[str, Any]:
    job = get_research_job(job_id)
    if not job:
        raise ValueError("Research job not found.")
    if job["status"] == "COMPLETED":
        return job
    if job["status"] in {"CANCELLED", "PAUSED"}:
        return job
    session = get_session(job["session_id"])
    experiment = get_experiment(str(job["experiment_id"]))
    if not session or not experiment:
        return update_research_job(job_id, "FAILED", error_message="Research dependencies are missing.", completed_at=utc_now())

    update_research_job(job_id, "RUNNING", progress=0, started_at=job.get("started_at") or utc_now())
    update_experiment(experiment["id"], "RUNNING")
    append_audit("STRATEGY_SEARCH_STARTED", {"jobId": job_id, "candidateCount": len(curated_strategy_specs())}, session_id=session["id"])

    config = job["config"]
    fill_mode = str(config.get("fillMode") or "realistic")
    total = max(1, int(session["record_count"]))
    chunk_size = max(10_000, int(config.get("chunkSize", 25_000)))
    start_ns = parse_timestamp_ns(session["start_at"])
    end_ns = parse_timestamp_ns(session["end_at"])
    context_index = HistoricalContextIndex.load(session["start_at"], session["end_at"])
    context_flags = {"calendar": context_index.calendar_covered, "news": context_index.news_covered}
    runtimes = [CandidateRuntime(spec=spec, index=index) for index, spec in enumerate(curated_strategy_specs(), start=1)]
    book = OrderBook()
    features = OrderflowFeatures()
    microstructure = MicrostructureFeatures()
    actions: Counter[str] = Counter()
    processed = 0
    last_timestamp_ns = 0
    last_trade_price = 0.0
    last_signal_bucket = -1

    try:
        for event in iter_events(Path(session["file_path"])):
            processed += 1
            last_timestamp_ns = event.ts_event
            if processed % 2_048 == 0:
                requested = _control_status(job_id, control_check)
                if requested:
                    return _finish_interruption(
                        job_id, experiment["id"], requested,
                        processed=processed, last_timestamp_ns=last_timestamp_ns,
                    )
            actions[event.action] += 1
            before_order = book.orders.get(event.order_id)
            features.observe(event, before_order=before_order)
            book.apply(event)
            microstructure.observe(event, book=book, before_order=before_order)
            if book.is_snapshot_ready and start_ns <= event.ts_event <= end_ns:
                for runtime in runtimes:
                    runtime.resolve_pending(event.ts_event, book=book)
            if event.action == "T" and book.is_snapshot_ready and start_ns <= event.ts_event <= end_ns:
                last_trade_price = event.price / 1_000_000_000
                for runtime in runtimes:
                    runtime.on_trade(event.ts_event, last_trade_price, fill_mode=fill_mode)
                signal_bucket = event.ts_event // 1_000_000_000
                if signal_bucket != last_signal_bucket:
                    last_signal_bucket = signal_bucket
                    contract = microstructure.contract(book)
                    feature_contract = features.contract(data_complete=True)
                    feature_contract["microstructure"] = contract
                    feature_contract["topOfBookLiquidityContracts"] = contract["orderBook"]["topOfBookLiquidityContracts"]
                    feature_contract["externalContext"] = context_index.snapshot(event.ts_event)
                    segment = segment_name(event.ts_event, start_ns, end_ns)
                    for runtime in runtimes:
                        runtime.maybe_open(
                            timestamp_ns=event.ts_event,
                            segment=segment,
                            features=feature_contract,
                            fill_mode=fill_mode,
                        )
            if processed % chunk_size == 0:
                update_research_job(
                    job_id, "RUNNING", progress=min(0.97, processed / total * 0.97),
                    checkpoint={
                        "eventsProcessed": processed,
                        "lastTimestampNs": str(last_timestamp_ns),
                        "phase": "strategy_search",
                        "candidateCount": len(runtimes),
                    },
                )

        if last_trade_price:
            for runtime in runtimes:
                runtime.close_at_end(last_timestamp_ns, last_trade_price, fill_mode=fill_mode)

        update_research_job(
            job_id, "RUNNING", progress=0.985,
            checkpoint={
                "eventsProcessed": processed,
                "lastTimestampNs": str(last_timestamp_ns),
                "phase": "ranking",
                "candidateCount": len(runtimes),
            },
        )
        summaries = sorted((summarize_candidate(runtime, context_coverage=context_flags) for runtime in runtimes), key=lambda item: item["rankingScore"], reverse=True)
        top = summaries[:8]
        top_five = top[:5]
        positive_neighbors = sum(
            1 for item in top_five
            if float(item["segmentMetrics"]["Validation"]["netExpectancyUsd"]) > 0
        )
        parameter_stability = positive_neighbors / max(len(top_five), 1)
        # CandidateRuntime's fast search is a deliberately cheap approximation (see
        # strategy_search.py). No candidate may become PAPER_ACTIVE on the fast engine's
        # word alone: the top-ranked, fast-eligible candidate must also clear the
        # realistic, latency-aware EventDrivenBacktester before it is trusted.
        execution_gate = (
            _realistic_execution_gate(
                candidate=top[0],
                events=stream_normalized_events(Path(session["file_path"])),
                fill_mode=fill_mode,
                seed=int(config.get("seed", 7)), session_fingerprint=session["sha256"],
            )
            if top and top[0]["paperEligible"]
            else None
        )
        active_candidate = top[0] if execution_gate and execution_gate["passed"] else None

        existing_strategy_versions = list_strategy_versions()
        existing_by_hash = {item["strategy_hash"]: item for item in existing_strategy_versions}
        for strategy in existing_strategy_versions:
            if strategy["status"] == "PAPER_ACTIVE":
                save_strategy_version({
                    **strategy,
                    "status": "RESEARCH_ONLY",
                    "validation_status": "PAPER_RETIRED",
                    "promoted_at": None,
                })

        saved_candidates: list[dict[str, Any]] = []
        for rank, candidate in enumerate(top, start=1):
            parameters = candidate["parameters"]
            candidate_name = (
                f"{candidate['strategyName']} · SV{parameters['signedVolumeThreshold']} "
                f"DM{parameters['deltaMomentumThreshold']} QI{parameters['queueImbalanceThreshold']} "
                f"S{parameters['stopTicks']} T{parameters['targetTicks']}"
            )
            parameter_hash = _hash(parameters)
            strategy_hash = _hash({
                "strategy": candidate["strategyName"],
                "parameters": parameters,
                "featureVersion": FEATURE_VERSION,
                "fillModelVersion": FILL_MODEL_VERSION,
                "costModelVersion": COST_MODEL_VERSION,
                "codeVersion": CODE_VERSION,
            })
            data_fingerprints = _accumulate_session_fingerprints(existing_by_hash.get(strategy_hash), session["sha256"])
            session_metrics = _independent_session_metrics(data_fingerprints)
            aggregate_metrics = dict(candidate["metrics"])
            segment_metrics = candidate["segmentMetrics"]
            positive_segments = sum(1 for row in segment_metrics.values() if float(row["netExpectancyUsd"]) > 0)
            aggregate_metrics.update({
                **session_metrics,
                "maximumDrawdownR": round(float(aggregate_metrics["maximumDrawdownUsd"]) / 75, 3),
                "costDegradation": 0.2 if fill_mode == "realistic" else 0.35 if fill_mode == "stressed" else 0.0,
                "parameterStability": round(parameter_stability, 4),
                "regimeStability": round(float(candidate["metrics"].get("regimeStability", positive_segments / 3)), 4),
                "lockedDataUntouched": True,
            })
            formal_validation = evaluate_promotion(aggregate_metrics)
            is_active = bool(active_candidate and active_candidate["candidateIndex"] == candidate["candidateIndex"])
            is_top_ranked = bool(top and candidate["candidateIndex"] == top[0]["candidateIndex"])
            validation = {
                **formal_validation,
                "paperEligible": bool(candidate["paperEligible"]),
                "paperStatus": "PAPER_ACTIVE" if is_active else "RESEARCH_ONLY",
                "paperFailedReasons": candidate["paperFailedReasons"],
                "diagnosis": candidate["diagnosis"],
                "segmentMetrics": segment_metrics,
                "regimeMetrics": candidate["regimeMetrics"],
                "contextCoverage": {"calendar": context_index.calendar_covered, "news": context_index.news_covered},
                "rankingScore": candidate["rankingScore"],
                "chronologicalSplit": "50% development / 25% validation / 25% intraday holdout",
                "realisticExecutionGate": execution_gate if is_top_ranked else None,
            }
            candidate_config = {
                "mode": "search-candidate",
                "family": candidate["family"],
                "strategy": candidate["strategyName"],
                "parameters": parameters,
                "fillMode": fill_mode,
                "seed": int(config.get("seed", 7)),
                "rank": rank,
                "paperOnly": True,
            }
            child_id = str(uuid.uuid4())
            save_experiment({
                "id": child_id,
                "name": f"{candidate_name} · {session['start_at'][:10]}",
                "strategy_name": candidate_name,
                "strategy_hash": strategy_hash,
                "parameter_hash": parameter_hash,
                "dataset_fingerprint": session["sha256"],
                "split_name": "Development",
                "seed": int(config.get("seed", 7)),
                "fill_model_version": f"{FILL_MODEL_VERSION}:{fill_mode}",
                "cost_model_version": COST_MODEL_VERSION,
                "feature_version": FEATURE_VERSION,
                "code_version": CODE_VERSION,
                "status": "COMPLETED",
                "config": candidate_config,
                "metrics": aggregate_metrics,
                "validation": validation,
            })
            save_strategy_version({
                "id": f"strategy-{strategy_hash[:16]}",
                "name": candidate_name,
                "version": "paper-v1",
                "strategy_hash": strategy_hash,
                "status": "PAPER_ACTIVE" if is_active else "RESEARCH_ONLY",
                "validation_status": "PAPER_ONLY" if is_active else "RESEARCH_ONLY",
                "config": candidate_config,
                "data_fingerprints": data_fingerprints,
            })
            saved_candidates.append({
                "rank": rank,
                "experimentId": child_id,
                "strategyHash": strategy_hash,
                "strategyName": candidate_name,
                "parameters": parameters,
                "metrics": aggregate_metrics,
                "validation": validation,
                "diagnosis": candidate["diagnosis"],
                "activeForReplay": is_active,
            })

        best = saved_candidates[0] if saved_candidates else None
        active_saved = next((item for item in saved_candidates if item["activeForReplay"]), None)
        parent_metrics = dict(best["metrics"] if best else metrics_for_trades([]))
        parent_validation = {
            "eligible": False,
            "status": "PAPER_ACTIVE" if active_candidate else "RESEARCH_ONLY",
            "failedReasons": ["MINIMUM_SESSIONS", "MINIMUM_LOCKED_SESSIONS"],
            "candidateCount": len(runtimes),
            "paperActive": bool(active_candidate),
            "profitabilityClaim": False,
        }
        update_experiment(experiment["id"], "COMPLETED", metrics=parent_metrics, validation=parent_validation)
        result = {
            "sessionId": session["id"],
            "datasetFingerprint": session["sha256"],
            "eventsProcessed": processed,
            "actionCounts": dict(actions),
            "candidateCount": len(runtimes),
            "topCandidates": saved_candidates,
            "activePaperStrategyHash": active_saved["strategyHash"] if active_saved else None,
            "chronologicalSplit": "50% development / 25% validation / 25% intraday holdout",
            "independentSessionValidation": False,
            "realisticExecutionGate": execution_gate,
            "realisticExecutionGateEventsAvailable": (
                int(execution_gate.get("eventsProcessed", 0)) if execution_gate else 0
            ),
            "realisticExecutionGateEventsTruncated": False,
            "realisticExecutionGateStreaming": bool(execution_gate),
            "contextCoverage": context_index.coverage,
            "strategyFamilies": sorted({runtime.spec.family for runtime in runtimes}),
            "automaticOrderExecution": False,
            "profitabilityClaim": False,
        }
        completed = update_research_job(
            job_id, "COMPLETED", progress=1, result=result,
            checkpoint={
                "eventsProcessed": processed,
                "lastTimestampNs": str(last_timestamp_ns),
                "phase": "completed",
                "candidateCount": len(runtimes),
                "paperActive": bool(active_candidate),
            }, completed_at=utc_now(),
        )
        append_audit("STRATEGY_SEARCH_COMPLETED", {
            "jobId": job_id,
            "eventsProcessed": processed,
            "candidateCount": len(runtimes),
            "paperActive": bool(active_candidate),
            "activeStrategyHash": result["activePaperStrategyHash"],
            "automaticOrderExecution": False,
            "profitabilityClaim": False,
        }, session_id=session["id"])
        return completed
    except Exception as exc:
        update_experiment(experiment["id"], "FAILED", validation={"error": str(exc)})
        append_audit("STRATEGY_SEARCH_FAILED", {"jobId": job_id, "errorType": type(exc).__name__}, session_id=session["id"])
        return update_research_job(job_id, "FAILED", error_message=str(exc), completed_at=utc_now())


def _run_baseline_research_job(job_id: str, *, control_check: ControlCheck | None = None) -> dict[str, Any]:
    job = get_research_job(job_id)
    if not job:
        raise ValueError("Research job not found.")
    if job["status"] == "COMPLETED":
        return job
    if job["status"] in {"CANCELLED", "PAUSED"}:
        return job
    session = get_session(job["session_id"])
    experiment = get_experiment(str(job["experiment_id"]))
    if not session or not experiment:
        return update_research_job(job_id, "FAILED", error_message="Research dependencies are missing.", completed_at=utc_now())
    update_research_job(job_id, "RUNNING", progress=0, started_at=job.get("started_at") or utc_now())
    update_experiment(experiment["id"], "RUNNING")
    append_audit("RESEARCH_JOB_STARTED", {"jobId": job_id, "resumeSafe": True}, session_id=session["id"])

    config = job["config"]
    parameters = config["parameters"]
    chunk_size = max(1_000, int(config["chunkSize"]))
    total = max(1, int(session["record_count"]))
    book = OrderBook()
    features = OrderflowFeatures()
    microstructure = MicrostructureFeatures()
    actions: Counter[str] = Counter()
    intents: list[TradeIntent] = []
    last_candidate_ns = 0
    processed = 0
    last_timestamp_ns = 0
    try:
        for index, event in enumerate(iter_events(Path(session["file_path"]))):
            processed += 1
            last_timestamp_ns = event.ts_event
            if processed % 2_048 == 0:
                requested = control_check() if control_check is not None else None
                if requested:
                    return _finish_interruption(
                        job_id, experiment["id"], requested,
                        processed=processed, last_timestamp_ns=last_timestamp_ns,
                    )
            actions[event.action] += 1
            before_order = book.orders.get(event.order_id)
            features.observe(event, before_order=before_order)
            book.apply(event)
            microstructure.observe(event, book=book, before_order=before_order)
            if event.action == "T" and book.is_snapshot_ready:
                cooldown_ns = int(parameters["candidateCooldownSeconds"]) * 1_000_000_000
                directional_delta = features.cumulative_delta
                if abs(directional_delta) >= int(parameters["deltaThreshold"]) and event.ts_event - last_candidate_ns >= cooldown_ns:
                    direction = "long" if directional_delta > 0 else "short"
                    entry = event.price / 1_000_000_000
                    sign = 1 if direction == "long" else -1
                    stop = entry - sign * int(parameters["stopTicks"]) * 0.25
                    target = entry + sign * int(parameters["targetTicks"]) * 0.25
                    intents.append(TradeIntent(
                        id=f"{experiment['strategy_hash'][:16]}:{session['sha256'][:16]}:{len(intents)}", decision_timestamp_ns=event.ts_event,
                        direction=direction, order_type="MARKET", entry_price=entry, stop_price=stop,
                        targets=(target,), contracts=1, valid_until_ns=event.ts_event + 15_000_000_000,
                        time_stop_duration_ns=60_000_000_000,
                        exit_level_reference="ENTRY_RELATIVE",
                        signal_timestamp_ns=event.ts_event,
                        feature_snapshot={"cumulativeDelta": directional_delta, "featureVersion": FEATURE_VERSION},
                        signal_score=min(1.0, abs(directional_delta) / max(int(parameters["deltaThreshold"]) * 2, 1)),
                        invalidation=("STRUCTURE_BREAK", "DELTA_REVERSAL", "SIGNAL_EXPIRY"),
                        state_snapshot={"eventIndex": index, "timestamp": event.timestamp},
                        data_fingerprint=session["sha256"],
                        strategy_version=f"research-v1:{experiment['strategy_hash'][:12]}",
                        model_version=BASELINE_MODEL_ID,
                    ))
                    last_candidate_ns = event.ts_event
            if processed % chunk_size == 0:
                requested = _control_status(job_id, control_check)
                if requested:
                    return _finish_interruption(
                        job_id, experiment["id"], requested,
                        processed=processed, last_timestamp_ns=last_timestamp_ns,
                    )
                update_research_job(
                    job_id, "RUNNING", progress=min(0.95, processed / total),
                    checkpoint={
                        "eventsProcessed": processed,
                        "lastTimestampNs": str(last_timestamp_ns),
                        "phase": "scan",
                    },
                )

        requested = _control_status(job_id, control_check)
        if requested:
            return _finish_interruption(
                job_id, experiment["id"], requested,
                processed=processed, last_timestamp_ns=last_timestamp_ns,
            )
        update_research_job(
            job_id, "RUNNING", progress=0.95,
            checkpoint={
                "eventsProcessed": processed,
                "lastTimestampNs": str(last_timestamp_ns),
                "phase": "backtest",
                "candidatesProcessed": 0,
                "candidateCount": len(intents),
            },
        )

        last_reported_candidate = 0

        def report_backtest_progress(completed_intents: int, total_intents: int) -> None:
            nonlocal last_reported_candidate
            requested_status = _control_status(job_id, control_check)
            if requested_status:
                raise BacktestInterrupted(requested_status)
            report_every = max(1, total_intents // 100) if total_intents else 1
            if completed_intents != total_intents and completed_intents - last_reported_candidate < report_every:
                return
            last_reported_candidate = completed_intents
            fraction = completed_intents / total_intents if total_intents else 1.0
            update_research_job(
                job_id, "RUNNING", progress=min(0.994, 0.95 + 0.044 * fraction),
                checkpoint={
                    "eventsProcessed": processed,
                    "lastTimestampNs": str(last_timestamp_ns),
                    "phase": "backtest",
                    "candidatesProcessed": completed_intents,
                    "candidateCount": total_intents,
                },
            )

        fill_model = FillModel(mode=config["fillMode"])
        backtest = EventDrivenBacktester(fill_model=fill_model, seed=int(config["seed"])).run_market_streaming(
            stream_normalized_events(Path(session["file_path"])),
            intents,
            control_check=control_check,
            progress_callback=report_backtest_progress,
        )
        existing_strategy = next(
            (item for item in list_strategy_versions() if item["strategy_hash"] == experiment["strategy_hash"]),
            None,
        )
        data_fingerprints = _accumulate_session_fingerprints(existing_strategy, session["sha256"])
        session_metrics = _independent_session_metrics(data_fingerprints)

        metrics = dict(backtest["metrics"])
        metrics.update({
            **session_metrics,
            "maximumDrawdownR": 0 if not metrics["trades"] else round(float(metrics["maximumDrawdownUsd"]) / 75, 3),
            "costDegradation": 0 if not metrics["trades"] else 0.2,
            "parameterStability": 0.0,
            "regimeStability": 0.0,
            "lockedDataUntouched": experiment["split_name"] != "Locked Test",
        })
        validation = evaluate_promotion(metrics)
        feature_contract = features.contract(data_complete=session["completeness"] == "complete")
        microstructure_contract = microstructure.contract(book)
        feature_contract["microstructure"] = microstructure_contract
        feature_contract["topOfBookLiquidityContracts"] = microstructure_contract["orderBook"]["topOfBookLiquidityContracts"]
        result = {
            "sessionId": session["id"], "datasetFingerprint": session["sha256"],
            "eventsProcessed": processed, "actionCounts": dict(actions),
            "boundedBacktestEvents": len(bounded_events), "backtestTruncated": processed > maximum_events,
            "candidateCount": len(intents), "featureVersion": FEATURE_VERSION,
            "featureSnapshot": feature_contract,
            "backtest": backtest, "metrics": metrics, "validation": validation,
            "deterministicInputs": {
                "strategyHash": experiment["strategy_hash"], "parameterHash": experiment["parameter_hash"],
                "seed": experiment["seed"], "fillModelVersion": experiment["fill_model_version"],
                "costModelVersion": experiment["cost_model_version"], "codeVersion": experiment["code_version"],
            },
            "profitabilityClaim": False,
        }
        update_experiment(experiment["id"], "COMPLETED", metrics=metrics, validation=validation)
        completed = update_research_job(
            job_id, "COMPLETED", progress=1, result=result,
            checkpoint={
                "eventsProcessed": processed,
                "lastTimestampNs": str(last_timestamp_ns),
                "phase": "completed",
                "candidatesProcessed": len(intents),
                "candidateCount": len(intents),
            }, completed_at=utc_now(),
        )
        if existing_strategy:
            save_strategy_version({
                **existing_strategy,
                "status": "PROMOTION_ELIGIBLE" if validation["eligible"] else "RESEARCH_ONLY",
                "validation_status": validation["status"],
                "data_fingerprints": data_fingerprints,
            })
        append_audit("RESEARCH_JOB_COMPLETED", {
            "jobId": job_id, "eventsProcessed": processed, "candidateCount": len(intents),
            "trades": metrics["trades"], "promotionEligible": validation["eligible"],
            "profitabilityClaim": False,
        }, session_id=session["id"])
        return completed
    except BacktestInterrupted as exc:
        return _finish_interruption(
            job_id, experiment["id"], exc.status,
            processed=processed, last_timestamp_ns=last_timestamp_ns,
        )
    except Exception as exc:
        update_experiment(experiment["id"], "FAILED", validation={"error": str(exc)})
        append_audit("RESEARCH_JOB_FAILED", {"jobId": job_id, "errorType": type(exc).__name__}, session_id=session["id"])
        return update_research_job(job_id, "FAILED", error_message=str(exc), completed_at=utc_now())


def cancel_research_job(job_id: str) -> dict[str, Any]:
    job = get_research_job(job_id)
    if not job:
        raise ValueError("Research job not found.")
    if job["status"] not in {"QUEUED", "RUNNING", "PAUSED"}:
        return job
    cancelled = update_research_job(job_id, "CANCELLED", completed_at=utc_now())
    if job.get("experiment_id"):
        update_experiment(str(job["experiment_id"]), "CANCELLED")
    append_audit("RESEARCH_JOB_CANCELLED", {"jobId": job_id}, session_id=job["session_id"])
    return cancelled


def pause_research_job(job_id: str) -> dict[str, Any]:
    job = get_research_job(job_id)
    if not job:
        raise ValueError("Research job not found.")
    if job["status"] not in {"QUEUED", "RUNNING"}:
        return job
    paused = update_research_job(job_id, "PAUSED")
    if job.get("experiment_id"):
        update_experiment(str(job["experiment_id"]), "PAUSED")
    append_audit("RESEARCH_JOB_PAUSED", {"jobId": job_id, "checkpoint": paused.get("checkpoint", {})}, session_id=job["session_id"])
    return paused


def resume_research_job(job_id: str) -> dict[str, Any]:
    job = get_research_job(job_id)
    if not job:
        raise ValueError("Research job not found.")
    if job["status"] not in {"PAUSED", "FAILED"}:
        raise ValueError("Only paused or failed research jobs can be resumed.")
    resumed = update_research_job(job_id, "QUEUED", progress=0, error_message=None, completed_at=None)
    if job.get("experiment_id"):
        update_experiment(str(job["experiment_id"]), "QUEUED")
    append_audit("RESEARCH_JOB_RESUMED", {
        "jobId": job_id, "checkpoint": job.get("checkpoint", {}),
        "deterministicRestart": True, "reason": "order_book_state_is_rebuilt_from_source",
    }, session_id=job["session_id"])
    return resumed


def promote_strategy(strategy_hash: str) -> dict[str, Any]:
    strategy = next((item for item in list_strategy_versions() if item["strategy_hash"] == strategy_hash), None)
    if not strategy:
        raise ValueError("Strategy version not found.")
    experiments = [
        item for item in list_experiments(500)
        if item["strategy_hash"] == strategy_hash and item["status"] == "COMPLETED"
    ]
    eligible = [
        item for item in experiments
        if bool(item.get("validation", {}).get("eligible"))
        and str(item.get("fill_model_version", "")).endswith((":realistic", ":stressed"))
    ]
    if not eligible:
        raise ValueError("Strategy cannot be promoted until every validation gate passes on independent chronological data.")
    promoted = save_strategy_version({
        **strategy,
        "status": "ACTIVE",
        "validation_status": "VALIDATED",
        "promoted_at": utc_now(),
        "rejected_at": None,
    })
    append_audit("MODEL_PROMOTED", {
        "strategyHash": strategy_hash,
        "eligibleExperimentIds": [item["id"] for item in eligible],
        "automaticOrderExecution": False,
    })
    return promoted


def reject_strategy(strategy_hash: str) -> dict[str, Any]:
    strategy = next((item for item in list_strategy_versions() if item["strategy_hash"] == strategy_hash), None)
    if not strategy:
        raise ValueError("Strategy version not found.")
    rejected = save_strategy_version({
        **strategy,
        "status": "REJECTED",
        "validation_status": "REJECTED",
        "promoted_at": None,
        "rejected_at": utc_now(),
    })
    append_audit("MODEL_REJECTED", {"strategyHash": strategy_hash, "automaticOrderExecution": False})
    return rejected


def rollback_strategy(strategy_hash: str) -> dict[str, Any]:
    strategy = next((item for item in list_strategy_versions() if item["strategy_hash"] == strategy_hash), None)
    if not strategy:
        raise ValueError("Strategy version not found.")
    rolled_back = save_strategy_version({
        **strategy,
        "status": "RESEARCH_ONLY",
        "validation_status": "ROLLBACK",
        "promoted_at": None,
    })
    append_audit("MODEL_ROLLED_BACK", {"strategyHash": strategy_hash, "automaticOrderExecution": False})
    return rolled_back


def _research_readiness(datasets: list[dict[str, Any]], coverage: dict[str, Any]) -> dict[str, Any]:
    registered_complete = [
        item
        for item in datasets
        if item.get("completeness") == "complete" and item.get("integrity_status") == "passed"
    ]
    qualifying = qualifying_independent_full_l3_sessions(datasets)
    excluded = excluded_session_diagnostics(datasets)
    independent_dates = sorted({str(item.get("start_at") or "")[:10] for item in qualifying if item.get("start_at")})
    independent_months = sorted({date[:7] for date in independent_dates})
    calendar = coverage.get("economicCalendar", {})
    news = coverage.get("news", {})

    dataset_starts = [parsed for item in qualifying if (parsed := parse_utc_datetime(item.get("start_at")))]
    dataset_ends = [parsed for item in qualifying if (parsed := parse_utc_datetime(item.get("end_at")))]
    required_start = min(dataset_starts) if dataset_starts else None
    required_end = max(dataset_ends) if dataset_ends else None

    def covers_all(source: dict[str, Any]) -> bool:
        coverage_start = parse_utc_datetime(source.get("coverageStart"))
        coverage_end = parse_utc_datetime(source.get("coverageEnd"))
        return bool(
            source.get("declaredCoverage")
            and int(source.get("rowCount") or 0) > 0
            and coverage_start and coverage_end and required_start and required_end
            and coverage_start <= required_start and coverage_end >= required_end
        )

    calendar_ready = covers_all(calendar)
    news_ready = covers_all(news)
    blockers: list[str] = []
    if len(independent_months) < 6:
        blockers.append("NEED_SIX_MONTHS")
    if len(independent_dates) < 100:
        blockers.append("NEED_MORE_INDEPENDENT_SESSIONS")
    if not calendar_ready:
        blockers.append("CALENDAR_COVERAGE_MISSING")
    if not news_ready:
        blockers.append("NEWS_COVERAGE_MISSING")
    return {
        "current": {
            "completeL3Sessions": len(qualifying),
            "registeredCompleteSessions": len(registered_complete),
            "excludedSessionCount": len(excluded),
            "excludedSessions": excluded,
            "independentDates": len(independent_dates),
            "independentMonths": len(independent_months),
            "economicEvents": int(calendar.get("rowCount") or 0),
            "newsEvents": int(news.get("rowCount") or 0),
            "calendarCoverageComplete": calendar_ready,
            "newsCoverageComplete": news_ready,
        },
        "target": {
            "months": 6,
            "independentSessions": 100,
            "developmentSessions": 60,
            "validationSessions": 20,
            "lockedSessions": 20,
            "calendarCoverageRequired": True,
            "newsCoverageRequired": True,
            "minimumIndependentSessionHours": 6,
            "requiredCoverageStart": required_start.isoformat().replace("+00:00", "Z") if required_start else None,
            "requiredCoverageEnd": required_end.isoformat().replace("+00:00", "Z") if required_end else None,
        },
        "blockers": blockers,
        "signalMode": "PAPER_REPLAY_ONLY" if not blockers else "WAIT_UNTIL_CONTEXT_AND_VALIDATION",
        "readyForValidatedSignals": not blockers,
    }


def _research_blueprint() -> dict[str, Any]:
    return {
        "version": "flowdesk-research-blueprint-v1",
        "requirements": [
            "MARKET_STRUCTURE",
            "FULL_L3_ORDERFLOW",
            "ECONOMIC_CALENDAR_POINT_IN_TIME",
            "NEWS_POINT_IN_TIME",
            "REGIME_CLASSIFICATION",
            "MULTI_STRATEGY_SEARCH",
            "WALK_FORWARD_VALIDATION",
            "LOCKED_OUT_OF_SAMPLE",
            "MANUAL_EXECUTION_ONLY",
        ],
        "strategyFamilies": [
            "MES_L3_MOMENTUM",
            "MES_PULLBACK_RETEST",
            "MES_VWAP_MEAN_REVERSION",
            "MES_OPENING_RANGE_BREAKOUT",
            "MES_ABSORPTION_REVERSAL",
        ],
        "signalStates": ["LONG", "SHORT", "WAIT", "NO_TRADE"],
        "automaticOrderExecution": False,
        "profitabilityClaim": False,
    }


def research_status() -> dict[str, Any]:
    coverage = sync_context_files()
    datasets = session_library()
    return {
        "datasets": datasets,
        # Status polling must remain lightweight. Full result payloads are available
        # from /research/jobs/{job_id} and are intentionally excluded here.
        "jobs": list_research_jobs(include_result=False),
        "experiments": list_experiments(),
        "strategies": list_strategy_versions(),
        "models": list_model_versions(),
        "signals": list_signal_snapshots(),
        "readiness": _research_readiness(datasets, coverage),
        "contextCoverage": coverage,
        "blueprint": _research_blueprint(),
        "defaults": {
            "fillMode": "realistic", "seed": 7, "featureVersion": FEATURE_VERSION,
            "fillModelVersion": FILL_MODEL_VERSION, "costModelVersion": COST_MODEL_VERSION,
        },
        "automaticOrderExecution": False,
        "profitabilityClaim": False,
    }
