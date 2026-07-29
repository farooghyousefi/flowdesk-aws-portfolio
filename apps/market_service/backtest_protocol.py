from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from statistics import mean
from typing import Any

from .replay import ReplayEngine
from .storage import (
    append_audit,
    activate_backtest_plan,
    close_blind_trade,
    derive_application_lock_state,
    exit_active_backtest_run,
    get_application_state,
    get_backtest_plan,
    get_session,
    get_session_split,
    get_settings,
    list_audit,
    list_backtest_plans,
    list_blind_trades,
    list_scan_candidates,
    list_sessions,
    mark_session_viewed,
    save_backtest_plan,
    save_blind_trade,
    save_plan_session_assignment,
    save_scan_candidates,
    session_library,
    set_session_split,
    start_backtest_run,
    update_backtest_plan,
    utc_now,
)

MODE_TARGETS = {"practice": 10, "pilot": 30, "locked": 100}
DEFAULT_FILL_CONFIG: dict[str, Any] = {
    "commissionPerSide": 0.85,
    "exchangeClearingPerSide": 0.70,
    "slippageEntryTicks": 1,
    "slippageExitTicks": 1,
    "stopSlippageTicks": 2,
    "targetFillRule": "trade_through",
    "maximumPosition": 3,
    "tickSize": 0.25,
    "tickValue": 1.25,
    "pointValue": 5.0,
    "fillAssumption": "next observed trade through trigger; unresolved when queue evidence is absent",
}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def strategy_hash(
    *, strategy: str, instrument: str, session_ids: list[str], config: dict[str, Any],
    settings: dict[str, Any] | None = None, protocol_id: str | None = None,
) -> str:
    active_settings = settings or get_settings()
    sessions = []
    for session_id in sorted(session_ids):
        session = get_session(session_id)
        if not session:
            raise ValueError(f"Unknown session: {session_id}")
        sessions.append({"id": session_id, "sha256": session["sha256"], "instrumentId": session["instrument_id"]})
    contract = {
        "strategy": strategy,
        "setupEngine": "setup_decision:v1",
        "instrument": instrument,
        "sessions": sessions,
        "orderflow": active_settings["orderflow"],
        "risk": active_settings["risk"],
        "fill": config["fill"],
        "protocolId": protocol_id,
    }
    return hashlib.sha256(_canonical(contract).encode("ascii")).hexdigest()


def normalize_config(payload: dict[str, Any]) -> dict[str, Any]:
    fill = {**DEFAULT_FILL_CONFIG, **dict(payload.get("fill") or {})}
    numeric_nonnegative = (
        "commissionPerSide", "exchangeClearingPerSide", "slippageEntryTicks",
        "slippageExitTicks", "stopSlippageTicks",
    )
    for key in numeric_nonnegative:
        fill[key] = float(fill[key])
        if fill[key] < 0:
            raise ValueError(f"{key} cannot be negative.")
    fill["maximumPosition"] = int(fill["maximumPosition"])
    if fill["maximumPosition"] < 1:
        raise ValueError("maximumPosition must be at least one.")
    if fill["targetFillRule"] not in {"trade_through", "touch_unresolved"}:
        raise ValueError("Unsupported target fill rule.")
    return {
        "fill": fill,
        "startingBalance": float(payload.get("startingBalance", 50_000)),
        "riskPerTrade": float(payload.get("riskPerTrade", 75)),
        "maximumTradesPerDay": int(payload.get("maximumTradesPerDay", 3)),
        "requireFullL3": bool(payload.get("requireFullL3", True)),
    }


def create_plan(payload: dict[str, Any]) -> dict[str, Any]:
    mode = str(payload.get("mode") or "practice").lower()
    if mode not in MODE_TARGETS:
        raise ValueError("Mode must be practice, pilot, or locked.")
    session_ids = list(dict.fromkeys(str(item) for item in payload.get("sessionIds") or []))
    sessions = [get_session(session_id) for session_id in session_ids]
    if any(session is None for session in sessions):
        raise ValueError("The plan includes an unknown session.")
    config = normalize_config(payload)
    if config["requireFullL3"] and any(session and session["completeness"] != "complete" for session in sessions):
        raise ValueError("This strategy requires complete L3 sessions.")
    strategy = str(payload.get("strategy") or "MES Pullback / Retest").strip()
    instrument = str(payload.get("instrument") or "MES").strip().upper()
    plan_id = str(uuid.uuid4())
    for session_id in session_ids:
        split = get_session_split(session_id)
        if split["locked"] and mode != "locked":
            raise ValueError(
                "This session split is locked and cannot be moved silently. Create the plan first, then use Clone session assignment into Practice."
            )
    digest = strategy_hash(
        strategy=strategy, instrument=instrument, session_ids=session_ids, config=config, protocol_id=plan_id,
    )
    now = utc_now()
    plan = save_backtest_plan({
        "id": plan_id, "mode": mode, "strategy": strategy, "config": config,
        "session_ids": session_ids, "strategy_hash": digest, "status": "READY" if session_ids else "DRAFT",
        "created_at": now, "locked_at": now if mode == "locked" else None,
    })
    destination = {"practice": "Development", "pilot": "Pilot", "locked": "Locked Test"}[mode]
    for session_id in session_ids:
        previous = get_session_split(session_id)
        if mode == "locked" and previous.get("viewed_at") and previous["split_name"] != "Locked Test":
            append_audit(
                "DATA_SNOOPING_WARNING", {"previousSplit": previous["split_name"], "viewedAt": previous["viewed_at"]},
                plan_id=plan_id, session_id=session_id,
            )
        split = set_session_split(session_id, destination, f"Assigned by {mode} plan {plan_id}", lock=mode == "locked")
        save_plan_session_assignment(plan_id, session_id, split_name=destination)
        append_audit(
            "SESSION_SPLIT_ASSIGNED", {"from": previous["split_name"], "to": split["split_name"], "locked": split["locked"]},
            plan_id=plan_id, session_id=session_id,
        )
    append_audit(
        "PLAN_CREATED", {"mode": mode, "strategyHash": digest, "config": config, "sessionIds": session_ids},
        plan_id=plan_id,
    )
    plan = activate_backtest_plan(plan_id)
    append_audit("PLAN_ACTIVATED", {"mode": mode, "settingsLocked": False}, plan_id=plan_id)
    return {
        **plan, "targetTrades": MODE_TARGETS[mode], "settingsLocked": False,
        "applicationLock": derive_application_lock_state(),
    }


def _rehash_plan(plan_id: str) -> dict[str, Any]:
    plan = get_backtest_plan(plan_id)
    if not plan:
        raise ValueError("Backtest plan not found.")
    digest = strategy_hash(
        strategy=plan["strategy"], instrument="MES", session_ids=plan["session_ids"],
        config=plan["config"], protocol_id=plan_id,
    )
    return update_backtest_plan(plan_id, strategy_hash=digest)


def assign_session_to_plan(plan_id: str, session_id: str) -> dict[str, Any]:
    plan = get_backtest_plan(plan_id)
    session = get_session(session_id)
    if not plan or plan["status"] == "ARCHIVED":
        raise ValueError("Backtest plan not found or archived.")
    if not session:
        raise ValueError("Session not found.")
    eligibility = session_eligibility(plan, {**session, "split": get_session_split(session_id)})
    if not eligibility["selectable"]:
        raise ValueError(f"Session assignment blocked: {eligibility['reasonCode']}.")
    split = get_session_split(session_id)
    if split["locked"] and plan["mode"] != "locked":
        raise ValueError(
            "This Locked Test assignment stays unchanged. Use Clone session assignment into Practice as the safe alternative."
        )
    destination = {"practice": "Development", "pilot": "Pilot", "locked": "Locked Test"}[plan["mode"]]
    updated_split = set_session_split(
        session_id, destination, f"Assigned by {plan['mode']} plan {plan_id}", lock=plan["mode"] == "locked"
    )
    assignment = save_plan_session_assignment(plan_id, session_id, split_name=destination)
    plan = _rehash_plan(plan_id)
    append_audit(
        "SESSION_ASSIGNED", {"from": split["split_name"], "to": updated_split["split_name"], "locked": updated_split["locked"]},
        plan_id=plan_id, session_id=session_id,
    )
    return {"plan": plan, "assignment": assignment}


def clone_session_assignment_into_practice(
    plan_id: str, session_id: str, *, ui_practice_only: bool = False,
) -> dict[str, Any]:
    plan = get_backtest_plan(plan_id)
    session = get_session(session_id)
    if not plan or plan["status"] == "ARCHIVED" or plan["mode"] != "practice":
        raise ValueError("Cloning a locked assignment is available only for a non-archived Practice plan.")
    if not session:
        raise ValueError("Session not found.")
    split = get_session_split(session_id)
    if not split["locked"]:
        return assign_session_to_plan(plan_id, session_id)
    source_plan = next(
        (candidate for candidate in list_backtest_plans() if candidate["mode"] == "locked" and session_id in candidate["session_ids"]),
        None,
    )
    assignment = save_plan_session_assignment(
        plan_id, session_id, split_name="Development", assignment_type="clone",
        reused=True, contaminated=True, ui_practice_only=ui_practice_only,
        source_plan_id=source_plan["id"] if source_plan else None,
    )
    plan = _rehash_plan(plan_id)
    append_audit(
        "SESSION_ASSIGNMENT_CLONED",
        {
            "sourceSplit": split["split_name"], "sourceAssignmentUnchanged": True,
            "reused": True, "contaminated": True, "uiPracticeOnly": ui_practice_only,
            "rawFileReused": session["file_path"],
        },
        plan_id=plan_id, session_id=session_id,
    )
    return {"plan": plan, "assignment": assignment, "sourceSplit": split}


def session_eligibility(plan: dict[str, Any] | None, session: dict[str, Any]) -> dict[str, Any]:
    if not plan:
        return {"selectable": False, "canClone": False, "reasonCode": "CREATE_PLAN_FIRST", "detailKey": "session.createPlanFirst", "nextActionKey": "session.actionCreatePlan"}
    if plan["status"] == "ARCHIVED":
        return {"selectable": False, "canClone": False, "reasonCode": "PLAN_ARCHIVED", "detailKey": "session.planArchived", "nextActionKey": "session.actionCreatePlan"}
    app_state = get_application_state()
    if app_state.get("activeRun"):
        return {"selectable": False, "canClone": False, "reasonCode": "ACTIVE_RUN", "detailKey": "session.activeRun", "nextActionKey": "session.actionExitRun"}
    assignment = next((item for item in plan.get("assignments", []) if item["session_id"] == session["id"]), None)
    if assignment:
        return {"selectable": False, "canClone": False, "reasonCode": "ALREADY_ASSIGNED", "detailKey": "session.alreadyAssigned", "nextActionKey": "session.actionStartReplay"}
    maximum_sessions = int(plan.get("config", {}).get("maximumSessions", 20))
    if len(plan.get("assignments", [])) >= maximum_sessions:
        return {"selectable": False, "canClone": False, "reasonCode": "MAXIMUM_SESSIONS", "detailKey": "session.maximumSessions", "nextActionKey": "session.actionCreatePlan"}
    if session.get("integrity_status") not in {"passed", "warning"}:
        return {"selectable": False, "canClone": False, "reasonCode": "INTEGRITY_FAILED", "detailKey": "session.integrityFailed", "nextActionKey": "session.actionValidateData"}
    if session.get("completeness") != "complete" and plan["config"].get("requireFullL3", True):
        return {"selectable": False, "canClone": False, "reasonCode": "SESSION_INCOMPLETE", "detailKey": "session.incomplete", "nextActionKey": "session.actionChooseComplete"}
    if session.get("schema_name", "mbo") != "mbo":
        return {"selectable": False, "canClone": False, "reasonCode": "UNSUITABLE_DATA_TYPE", "detailKey": "session.unsuitableDataType", "nextActionKey": "session.actionChooseMbo"}
    split = session.get("split") or get_session_split(session["id"])
    contaminated_elsewhere = any(
        assignment.get("contaminated")
        for candidate in list_backtest_plans()
        for assignment in candidate.get("assignments", [])
        if assignment["session_id"] == session["id"]
    )
    if plan["mode"] == "locked" and contaminated_elsewhere:
        return {"selectable": False, "canClone": False, "reasonCode": "SESSION_CONTAMINATED", "detailKey": "session.contaminated", "nextActionKey": "session.actionChooseUntouched"}
    if split.get("locked") and plan["mode"] != "locked":
        can_clone = plan["mode"] == "practice"
        return {
            "selectable": False, "canClone": can_clone, "reasonCode": "LOCKED_SPLIT_WRONG_MODE",
            "detailKey": "session.lockedSplitWrongMode",
            "nextActionKey": "session.actionClonePractice" if can_clone else "session.actionChooseMatchingSplit",
        }
    assigned_elsewhere = next((
        candidate for candidate in list_backtest_plans()
        if candidate["id"] != plan["id"] and candidate["status"] != "ARCHIVED" and session["id"] in candidate["session_ids"]
    ), None)
    if assigned_elsewhere and plan["mode"] != "practice":
        return {"selectable": False, "canClone": False, "reasonCode": "ASSIGNED_TO_OTHER_PLAN", "detailKey": "session.assignedOtherPlan", "nextActionKey": "session.actionChooseAnother"}
    return {"selectable": True, "canClone": False, "reasonCode": "AVAILABLE", "detailKey": "session.available", "nextActionKey": "session.actionAssign"}


def start_blind_session(engine: ReplayEngine, plan_id: str, session_id: str) -> dict[str, Any]:
    plan = get_backtest_plan(plan_id)
    if not plan:
        raise ValueError("Backtest plan not found.")
    if session_id not in plan["session_ids"]:
        raise ValueError("Session is not assigned to this backtest plan.")
    app_state = get_application_state()
    if app_state["activePlanId"] != plan_id:
        raise ValueError("Activate this plan before starting its blind replay.")
    run_id = str(uuid.uuid4())
    engine.load(session_id)
    start_backtest_run(plan_id, session_id, plan["mode"], run_id)
    state = engine.configure_blind(plan["mode"], plan_id, run_id)
    mark_session_viewed(session_id)
    append_audit(
        "BLIND_SESSION_STARTED", {"mode": plan["mode"], "strategyHash": plan["strategy_hash"], "runId": run_id},
        plan_id=plan_id, session_id=session_id,
    )
    return state


def exit_locked_run(engine: ReplayEngine) -> dict[str, Any]:
    lock = derive_application_lock_state()
    run = exit_active_backtest_run()
    engine.exit_blind()
    if run:
        append_audit(
            "LOCKED_RUN_EXITED" if run["mode"] == "locked" else "BLIND_RUN_EXITED",
            {"runId": run["id"], "completedTradesUnchanged": True, "previousLock": lock},
            plan_id=run["plan_id"], session_id=run["session_id"],
        )
    return {"run": run, "applicationLock": derive_application_lock_state(), "state": engine.state()}


def record_trade_plan(engine: ReplayEngine, payload: dict[str, Any]) -> dict[str, Any]:
    state = engine.state()
    if not state.get("loaded") or not state.get("session"):
        raise ValueError("Load a blind replay session first.")
    plan_id = str(state.get("blind", {}).get("planId") or "")
    plan = get_backtest_plan(plan_id)
    if not plan:
        raise ValueError("No active blind plan.")
    direction = str(payload.get("direction") or state.get("decision", {}).get("direction") or "").lower()
    if direction not in {"long", "short"}:
        raise ValueError("Direction must be long or short.")
    entry = float(payload.get("entry"))
    stop = float(payload.get("stop"))
    targets = [float(item) for item in payload.get("targets") or []]
    contracts = int(payload.get("contracts", 1))
    if entry == stop or not targets:
        raise ValueError("Entry, a distinct stop, and at least one target are required.")
    if contracts < 1 or contracts > int(plan["config"]["fill"]["maximumPosition"]):
        raise ValueError("Position exceeds the configured maximum.")
    per_side = plan["config"]["fill"]["commissionPerSide"] + plan["config"]["fill"]["exchangeClearingPerSide"]
    trade_id = str(uuid.uuid4())
    trade = save_blind_trade({
        "id": trade_id, "plan_id": plan_id, "session_id": state["session"]["id"],
        "direction": direction, "entry": entry, "stop": stop, "targets": targets,
        "fees_usd": 2 * per_side * contracts,
        "decision_snapshot": state.get("decision", {}), "risk_snapshot": state.get("risk", {}),
        "features_snapshot": {**state.get("features", {}), "contracts": contracts, "timestamp": state.get("timestamp")},
    })
    engine.record_trade_plan()
    append_audit(
        "TRADE_PLAN_RECORDED", {"tradeId": trade_id, "entry": entry, "stop": stop, "targets": targets, "contracts": contracts},
        plan_id=plan_id, session_id=state["session"]["id"],
    )
    return trade


def finish_trade(trade_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    trade = next((item for item in list_blind_trades() if item["id"] == trade_id), None)
    if not trade:
        raise ValueError("Blind trade not found.")
    if trade["status"] != "OPEN":
        raise ValueError("Blind trade is already closed.")
    plan = get_backtest_plan(trade["plan_id"])
    if not plan:
        raise ValueError("Backtest plan not found.")
    exit_price = float(payload.get("exitPrice"))
    exit_reason = str(payload.get("exitReason") or "manual")
    contracts = int(trade["featuresSnapshot"].get("contracts", 1))
    fill = plan["config"]["fill"]
    direction_sign = 1 if trade["direction"] == "long" else -1
    gross = direction_sign * (exit_price - float(trade["entry"])) * float(fill["pointValue"]) * contracts
    exit_ticks = float(fill["stopSlippageTicks"] if exit_reason == "stop" else fill["slippageExitTicks"])
    slippage_ticks = float(fill["slippageEntryTicks"]) + exit_ticks
    slippage_usd = slippage_ticks * float(fill["tickValue"]) * contracts
    fees_usd = 2 * (float(fill["commissionPerSide"]) + float(fill["exchangeClearingPerSide"])) * contracts
    net = gross - fees_usd - slippage_usd
    initial_risk = abs(float(trade["entry"]) - float(trade["stop"])) * float(fill["pointValue"]) * contracts
    result_r = net / initial_risk if initial_risk else 0.0
    closed = close_blind_trade(trade_id, {
        "result_r": result_r, "result_usd": net, "mae": float(payload.get("mae", 0)),
        "mfe": float(payload.get("mfe", 0)), "holding_seconds": float(payload.get("holdingSeconds", 0)),
        "fees_usd": fees_usd, "slippage_usd": slippage_usd, "immutable": plan["mode"] == "locked",
    })
    append_audit(
        "TRADE_CLOSED", {
            "tradeId": trade_id, "exitPrice": exit_price, "exitReason": exit_reason,
            "grossUsd": round(gross, 2), "feesUsd": round(fees_usd, 2),
            "slippageUsd": round(slippage_usd, 2), "netUsd": round(net, 2), "resultR": round(result_r, 4),
        },
        plan_id=trade["plan_id"], session_id=trade["session_id"],
    )
    return closed


def scan_session(session_id: str, plan_id: str | None = None) -> dict[str, Any]:
    if plan_id:
        plan = get_backtest_plan(plan_id)
        if not plan or session_id not in plan["session_ids"]:
            raise ValueError("Session is not assigned to this plan.")
    result = ReplayEngine().scan_candidates(session_id)
    save_scan_candidates(session_id, result["candidates"], plan_id)
    append_audit(
        "CANDIDATE_SCAN_COMPLETED", {"engine": result["engine"], "counts": result["counts"], "candidateCount": len(result["candidates"])},
        plan_id=plan_id, session_id=session_id,
    )
    return {**result, "profitableClaim": False, "purpose": "candidate discovery and deterministic QA"}


def jump_to_candidate(engine: ReplayEngine, candidate_id: int) -> dict[str, Any]:
    candidate = next((item for item in list_scan_candidates() if int(item["id"]) == candidate_id), None)
    if not candidate:
        raise ValueError("Setup candidate not found.")
    plan_id = str(candidate.get("planId") or "")
    plan = get_backtest_plan(plan_id) if plan_id else None
    if plan:
        if candidate["sessionId"] not in plan["session_ids"]:
            raise ValueError("Candidate session is not assigned to this plan.")
        if not engine.session or engine.session["id"] != candidate["sessionId"]:
            engine.load(candidate["sessionId"])
        run_id = str(get_application_state().get("activeRunId") or "")
        engine.configure_blind(plan["mode"], plan_id, run_id or None)
    state = engine.jump_to_candidate(int(candidate["timestampNs"]))
    append_audit(
        "CANDIDATE_JUMP", {"candidateId": candidate_id, "timestampNs": candidate["timestampNs"], "laterDataExposed": False},
        plan_id=plan_id or None, session_id=candidate["sessionId"],
    )
    return state


def _breakdown(trades: list[dict[str, Any]], key: Any) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        groups.setdefault(str(key(trade) or "unknown"), []).append(trade)
    return [
        {
            "label": label, "trades": len(rows),
            "netResultUsd": round(sum(float(row["result_usd"]) for row in rows), 2),
            "expectancyR": round(mean(float(row["result_r"]) for row in rows), 3),
        }
        for label, rows in sorted(groups.items())
    ]


def conservative_report(plan_id: str | None = None) -> dict[str, Any]:
    rows = [row for row in list_blind_trades(plan_id) if row["status"] == "CLOSED"]
    results = [float(row["result_usd"]) for row in rows]
    r_values = [float(row["result_r"]) for row in rows]
    wins = [row for row in rows if float(row["result_usd"]) > 0]
    losses = [row for row in rows if float(row["result_usd"]) < 0]
    breakeven = [row for row in rows if float(row["result_usd"]) == 0]
    gross_win = sum(float(row["result_usd"]) for row in wins)
    gross_loss = abs(sum(float(row["result_usd"]) for row in losses))
    equity = peak = drawdown = 0.0
    consecutive = maximum_consecutive = 0
    for result in results:
        equity += result
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        consecutive = consecutive + 1 if result < 0 else 0
        maximum_consecutive = max(maximum_consecutive, consecutive)
    expectancy_r = mean(r_values) if r_values else 0.0
    assessment = "Insufficient sample" if len(rows) < 30 else "Negative expectancy" if expectancy_r <= 0 else "Positive observed expectancy"
    validation = "Requires out-of-sample validation" if len(rows) < 100 or not plan_id else "Observed on the selected plan; no profitability guarantee"
    return {
        "planId": plan_id, "trades": len(rows), "wins": len(wins), "losses": len(losses),
        "breakeven": len(breakeven), "winRate": round(len(wins) / max(len(rows), 1) * 100, 2),
        "averageWinR": round(mean(float(row["result_r"]) for row in wins), 3) if wins else 0,
        "averageLossR": round(mean(float(row["result_r"]) for row in losses), 3) if losses else 0,
        "expectancyR": round(expectancy_r, 3), "expectancyUsd": round(mean(results), 2) if results else 0,
        "profitFactor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "maximumDrawdown": round(drawdown, 2), "maximumConsecutiveLosses": maximum_consecutive,
        "mae": round(mean(float(row["mae"] or 0) for row in rows), 3) if rows else 0,
        "mfe": round(mean(float(row["mfe"] or 0) for row in rows), 3) if rows else 0,
        "averageHoldingSeconds": round(mean(float(row["holding_seconds"] or 0) for row in rows), 1) if rows else 0,
        "fees": round(sum(float(row["fees_usd"]) for row in rows), 2),
        "slippage": round(sum(float(row["slippage_usd"]) for row in rows), 2),
        "netResult": round(sum(results), 2),
        "longVsShort": _breakdown(rows, lambda row: row["direction"]),
        "timeOfDay": _breakdown(rows, lambda row: datetime.fromisoformat(row["opened_at"].replace("Z", "+00:00")).astimezone(UTC).strftime("%H:00 UTC")),
        "setupReason": _breakdown(rows, lambda row: row["decisionSnapshot"].get("setupName")),
        "dataQuality": _breakdown(rows, lambda row: row["decisionSnapshot"].get("dataReliability")),
        "sampleSizeWarning": len(rows) < 100, "assessment": assessment, "validation": validation,
        "profitabilityClaim": False,
    }


def protocol_status(plan_id: str | None = None) -> dict[str, Any]:
    plans = list_backtest_plans()
    app_state = get_application_state()
    active = get_backtest_plan(str(app_state["activePlanId"])) if app_state.get("activePlanId") else None
    inspected = get_backtest_plan(plan_id) if plan_id else active
    trades = list_blind_trades(inspected["id"] if inspected else None)
    closed_by_mode = {mode: 0 for mode in MODE_TARGETS}
    for plan in (item for item in plans if item["status"] != "ARCHIVED"):
        closed_by_mode[plan["mode"]] += sum(1 for trade in list_blind_trades(plan["id"]) if trade["status"] == "CLOSED")
    phases = [
        {"mode": mode, "label": {"practice": "Practice", "pilot": "Pilot", "locked": "Locked Test"}[mode], "complete": closed_by_mode[mode], "target": target}
        for mode, target in MODE_TARGETS.items()
    ]
    forward_days = len({item["start_at"][:10] for item in session_library() if item["split"]["split_name"] == "Forward Paper"})
    phases.append({"mode": "forward", "label": "Forward Paper", "complete": forward_days, "target": 20})
    library = session_library()
    for session in library:
        session["eligibility"] = session_eligibility(active, session)
    return {
        "plans": [plan for plan in plans if plan["status"] != "ARCHIVED"],
        "archivedPlans": [plan for plan in plans if plan["status"] == "ARCHIVED"],
        "activePlan": active, "inspectedPlan": inspected, "currentRun": app_state.get("activeRun"),
        "applicationLock": derive_application_lock_state(), "phases": phases,
        "sessions": library, "trades": trades,
        "candidates": list_scan_candidates(), "audit": list_audit(inspected["id"] if inspected else None),
        "report": conservative_report(inspected["id"] if inspected else None),
        "defaults": {"fill": DEFAULT_FILL_CONFIG},
    }
