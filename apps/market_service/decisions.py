from __future__ import annotations

from collections import defaultdict
from statistics import median
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .storage import get_settings, list_journal


def _reason(
    code: str,
    state: str,
    title_key: str,
    *,
    detail_key: str | None = None,
    measured_value: Any = None,
    required_value: Any = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "state": state,
        "titleKey": title_key,
        "detailKey": detail_key,
        "measuredValue": measured_value,
        "requiredValue": required_value,
    }


def risk_decision(settings: dict[str, Any] | None = None, *, timestamp: str | None = None, symbol: str = "MES") -> dict[str, Any]:
    config = (settings or get_settings())["risk"]
    journal = list_journal()
    day_pnl = float(config.get("manualDayPnl", 0))
    total_pnl = float(config.get("manualTotalPnl", 0))
    max_loss = float(config.get("maximumLossEod", 1500))
    planned_risk = float(config.get("maxRiskPerTrade", 75))
    open_risk = float(config.get("openRiskUsd", 0))
    max_daily = float(config.get("maxDailyLoss", 150))
    max_trades = int(config.get("maximumTradesPerDay", config.get("maxTrades", 3)))
    loss_limit = int(config.get("dailyStopAfterLosses", config.get("consecutiveLossLimit", 2)))
    maximum_contracts = int(config.get("maxMicroContracts", 30))
    today = max((entry["date"] for entry in journal), default="")
    todays_entries = [entry for entry in journal if entry["date"] == today]
    daily_results: dict[str, float] = defaultdict(float)
    for entry in journal:
        daily_results[str(entry["date"])] += float(entry.get("resultUsd") or 0)
    positive_days = [value for value in daily_results.values() if value > 0]
    largest_positive_day = max(positive_days, default=0.0)
    realized_positive_total = sum(positive_days)
    consistency_actual = largest_positive_day / realized_positive_total if realized_positive_total else 0.0
    consecutive_losses = 0
    for entry in todays_entries:
        if (entry.get("resultUsd") or 0) < 0:
            consecutive_losses += 1
        else:
            break
    remaining_drawdown = max(0.0, max_loss + min(total_pnl, 0) - open_risk)
    reasons: list[str] = []
    codes: list[str] = []
    reason_objects: list[dict[str, Any]] = []
    state = "allowed"
    if day_pnl <= -max_daily:
        state = "blocked"; codes.append("DAILY_LOSS_LIMIT"); reasons.append("Das manuell gepflegte Tagesverlustlimit ist erreicht.")
        reason_objects.append(_reason("DAILY_LOSS_LIMIT", "blocking", "risk.dailyLossLimit", measured_value=day_pnl, required_value=-max_daily))
    if len(todays_entries) >= max_trades:
        state = "blocked"; codes.append("MAX_TRADES"); reasons.append("Die maximale Anzahl Trades für den Tag ist erreicht.")
        reason_objects.append(_reason("MAX_TRADES", "blocking", "risk.maximumTrades", measured_value=len(todays_entries), required_value=max_trades))
    if consecutive_losses >= loss_limit:
        state = "blocked"; codes.append("CONSECUTIVE_LOSSES"); reasons.append("Das Consecutive-Loss-Limit erzwingt eine Pause.")
        reason_objects.append(_reason("CONSECUTIVE_LOSSES", "blocking", "risk.lossStreak", measured_value=consecutive_losses, required_value=loss_limit))
    if remaining_drawdown < planned_risk:
        state = "blocked"; codes.append("DRAWDOWN_BUFFER"); reasons.append("Der verbleibende Drawdown-Puffer deckt das geplante Risiko nicht.")
        reason_objects.append(_reason("DRAWDOWN_BUFFER", "blocking", "risk.drawdownBuffer", measured_value=remaining_drawdown, required_value=planned_risk))
    allowed_instruments = [str(item).upper() for item in config.get("allowedInstruments", ["MES"])]
    if symbol.upper() not in allowed_instruments:
        state = "blocked"; codes.append("INSTRUMENT_NOT_ALLOWED"); reasons.append("Das Instrument ist im Challenge-Profil nicht freigegeben.")
        reason_objects.append(_reason("INSTRUMENT_NOT_ALLOWED", "blocking", "risk.instrumentNotAllowed", measured_value=symbol))
    if timestamp:
        local_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(ZoneInfo("Europe/Berlin")).strftime("%H:%M")
        allowed_start = str(config.get("allowedTradingStart", "00:00"))
        allowed_end = str(config.get("allowedTradingEnd", "23:59"))
        if not allowed_start <= local_time <= allowed_end:
            state = "blocked"; codes.append("OUTSIDE_TRADING_HOURS"); reasons.append("Die aktuelle Zeit liegt außerhalb des Challenge-Handelsfensters.")
            reason_objects.append(_reason("OUTSIDE_TRADING_HOURS", "blocking", "risk.outsideTradingHours", measured_value=local_time, required_value=f"{allowed_start}-{allowed_end}"))
    if state == "allowed" and (remaining_drawdown < planned_risk * 3 or day_pnl <= -max_daily * 0.7):
        state = "caution"; codes.append("BUFFER_NEAR_LIMIT"); reasons.append("Der manuell gepflegte Risikopuffer nähert sich dem persönlichen Limit.")
        reason_objects.append(_reason("BUFFER_NEAR_LIMIT", "partially_fulfilled", "risk.bufferNearLimit"))
    if not reasons:
        codes.append("WITHIN_MANUAL_LIMITS")
        reasons.append("Die manuell gepflegten Werte liegen innerhalb der konfigurierten Grenzen.")
        reason_objects.append(_reason("WITHIN_MANUAL_LIMITS", "fulfilled", "risk.withinLimits"))
    return {
        "state": state,
        "manuallyMaintained": True,
        "accountType": config.get("accountType"),
        "accountSize": float(config.get("accountSize", 50000)),
        "dayPnl": day_pnl,
        "totalPnl": total_pnl,
        "remainingDrawdown": round(remaining_drawdown, 2),
        "plannedRiskUsd": planned_risk,
        "maximumContracts": maximum_contracts,
        "openRiskUsd": open_risk,
        "tradesToday": len(todays_entries),
        "consecutiveLosses": consecutive_losses,
        "reasonCodes": codes,
        "reasons": reason_objects,
        "humanReasons": reasons,
        "challengeProfile": {
            "startBalance": float(config.get("accountSize", 50000)),
            "drawdownMode": config.get("drawdownMode", "trailing"),
            "profitTarget": float(config.get("profitTarget", 2500)),
            "dailyLossLimit": max_daily,
            "maximumDrawdown": max_loss,
            "maximumContracts": maximum_contracts,
            "minimumTradingDays": int(config.get("minimumTradingDays", 5)),
            "consistencyRule": float(config.get("consistencyRule", 0.4)),
            "consistencyActual": round(consistency_actual, 4),
            "tradingDays": len(daily_results),
            "profitTargetProgress": round(max(0.0, total_pnl) / max(float(config.get("profitTarget", 2500)), 1), 4),
            "allowedTradingStart": config.get("allowedTradingStart", "15:00"),
            "allowedTradingEnd": config.get("allowedTradingEnd", "22:00"),
            "newsTradingAllowed": bool(config.get("newsTradingAllowed", False)),
            "overnightHoldingAllowed": bool(config.get("overnightHoldingAllowed", False)),
            "allowedInstruments": config.get("allowedInstruments", ["MES"]),
            "maximumTradesPerDay": max_trades,
            "riskPerTrade": planned_risk,
            "cooldownMinutes": int(config.get("cooldownMinutes", 20)),
            "dailyStopAfterLosses": loss_limit,
            "scalingRules": config.get("scalingRules", "fixed_contract_cap"),
            "violations": [reason["code"] for reason in reason_objects if reason["state"] == "blocking"],
            "todayTradingStatus": state,
            "automaticOrderExecution": False,
        },
    }


def setup_decision(
    *, timestamp: str, completeness: str, features: dict[str, Any], risk: dict[str, Any]
) -> dict[str, Any]:
    summary = features["tradeSummary"]
    one_minute = [bar for bar in features["bars"] if bar["timeframe"] == "1m" and bar.get("completed", True)]
    structures = {item["timeframe"]: item for item in features["marketStructure"]}
    setup_reasons: list[dict[str, Any]] = []
    codes: list[str] = []
    confidence = 20
    direction = "long" if summary["delta"] >= 0 else "short"

    if completeness != "complete":
        codes.append("PARTIAL_BOOK")
        setup_reasons.append(_reason("PARTIAL_BOOK", "unavailable", "setup.completeInitialBook", detail_key="setup.partialBookDetail"))
    else:
        setup_reasons.append(_reason("COMPLETE_INITIAL_BOOK", "fulfilled", "setup.completeInitialBook"))
        confidence += 15
    if len(one_minute) < 3:
        codes.append("INSUFFICIENT_BARS")
        setup_reasons.append(_reason("INSUFFICIENT_BARS", "missing", "setup.completedOneMinuteBars", measured_value=len(one_minute), required_value=3))
    else:
        setup_reasons.append(_reason("COMPLETED_BARS", "fulfilled", "setup.completedOneMinuteBars", measured_value=len(one_minute), required_value=3))
        confidence += 15
    if structures.get("5m", {}).get("state") in {"trend_up", "trend_down"}:
        confidence += 15
        setup_reasons.append(_reason("DIRECTIONAL_5M_STRUCTURE", "fulfilled", "setup.directionalFiveMinuteStructure"))
    else:
        codes.append("STRUCTURE_UNCONFIRMED")
        setup_reasons.append(_reason("STRUCTURE_UNCONFIRMED", "missing", "setup.directionalFiveMinuteStructure"))
    delta_supports = (direction == "long" and summary["delta"] > 0) or (direction == "short" and summary["delta"] < 0)
    if delta_supports and abs(summary["delta"]) >= 5:
        confidence += 15
        setup_reasons.append(_reason("DELTA_CONFIRMATION", "fulfilled", "setup.deltaShift", measured_value=summary["delta"], required_value=5))
    else:
        codes.append("DELTA_CONFIRMATION_MISSING")
        setup_reasons.append(_reason("DELTA_CONFIRMATION_MISSING", "missing", "setup.deltaShift", measured_value=summary["delta"], required_value=5))
    absorptions = features.get("absorptionCandidates", [])
    if absorptions:
        confidence += 10
        setup_reasons.append(_reason("ABSORPTION_OBSERVED", "partially_fulfilled", "setup.absorptionCandidate", detail_key="setup.heuristicCandidateDetail"))
    else:
        codes.append("ABSORPTION_MISSING")
        setup_reasons.append(_reason("ABSORPTION_MISSING", "missing", "setup.orderflowRetestConfirmation"))

    unresolved = [reason for reason in setup_reasons if reason["state"] in {"missing", "blocking", "contradictory", "unavailable"}]
    state = "trade_ready" if not unresolved and confidence >= 75 else "wait"
    if risk["state"] == "blocked":
        state = "blocked"
        codes.insert(0, "RISK_GUARD_OVERRIDE")
        setup_reasons.insert(0, _reason("RISK_GUARD_OVERRIDE", "blocking", "setup.riskGuardOverride"))
    elif risk["state"] == "allowed":
        setup_reasons.append(_reason("RISK_GUARD_ALLOWED", "fulfilled", "setup.riskGuardAllowed"))
    last_price = one_minute[-1]["close"] if one_minute else None
    entry_zone = None
    invalidation = None
    targets = None
    if last_price is not None:
        entry_zone = {"min": round(last_price - 0.5, 2), "max": round(last_price + 0.5, 2)}
        invalidation = round(last_price - 2 if direction == "long" else last_price + 2, 2)
        targets = [round(last_price + 4 if direction == "long" else last_price - 4, 2)]
    return {
        "state": state,
        "direction": direction,
        "timestamp": timestamp,
        "setupName": "MES Pullback / Retest",
        "entryZone": entry_zone,
        "invalidation": invalidation,
        "targets": targets,
        "estimatedRiskTicks": 8 if last_price is not None else None,
        "estimatedRewardTicks": 16 if last_price is not None else None,
        "reasonCodes": codes or ["REFERENCE_SETUP_CONFIRMED"],
        "reasons": setup_reasons,
        "humanReasons": [reason["titleKey"] for reason in setup_reasons if reason["state"] in {"fulfilled", "partially_fulfilled"}],
        "passedConditions": [reason["titleKey"] for reason in setup_reasons if reason["state"] == "fulfilled"],
        "observedEvidence": [reason["titleKey"] for reason in setup_reasons if reason["state"] == "partially_fulfilled"],
        "missingConditions": [reason["titleKey"] for reason in setup_reasons if reason["state"] in {"missing", "blocking", "contradictory", "unavailable"}],
        "confidence": min(confidence, 92),
        "dataReliability": "complete_book" if completeness == "complete" else "partial_book_l3_not_guaranteed",
    }


def explanation(decision: dict[str, Any], risk: dict[str, Any]) -> str:
    if decision["state"] == "blocked":
        return "KEIN TRADE. Der Risk Guard blockiert das Setup aufgrund der aktuell gepflegten Limits."
    missing = [item for item in decision.get("reasons", []) if item["state"] in {"missing", "contradictory", "unavailable"}]
    if decision["state"] == "trade_ready":
        return "Das Referenz-Setup ist vollständig bestätigt. Prüfe Entry, Risiko und Invalidation vor jeder manuellen Order."
    if missing:
        return "ABWARTEN. Die Marktstruktur ist noch nicht vollständig bestätigt; die fehlenden Bedingungen werden einzeln angezeigt."
    return "ABWARTEN. Das Setup hat die erforderliche Konfidenz noch nicht erreicht."


def backtest_summary() -> dict[str, Any]:
    entries = [entry for entry in list_journal() if entry.get("resultUsd") is not None]
    wins = [entry for entry in entries if float(entry["resultUsd"]) > 0]
    losses = [entry for entry in entries if float(entry["resultUsd"]) < 0]
    results = [float(entry["resultUsd"]) for entry in entries]
    r_values = [float(entry["resultR"]) for entry in entries if entry.get("resultR") is not None]
    gross_win = sum(float(entry["resultUsd"]) for entry in wins)
    gross_loss = abs(sum(float(entry["resultUsd"]) for entry in losses))
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for result in reversed(results):
        equity += result
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return {
        "mode": "Manual Replay Review",
        "trades": len(entries),
        "winRate": round(len(wins) / max(len(entries), 1) * 100, 2),
        "lossRate": round(len(losses) / max(len(entries), 1) * 100, 2),
        "averageWin": round(gross_win / max(len(wins), 1), 2),
        "averageLoss": round(-gross_loss / max(len(losses), 1), 2),
        "expectancy": round(sum(results) / max(len(entries), 1), 2),
        "profitFactor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "maximumDrawdown": round(max_drawdown, 2),
        "averageR": round(sum(r_values) / max(len(r_values), 1), 2),
        "medianR": round(median(r_values), 2) if r_values else 0,
        "mae": None, "mfe": None, "averageTimeInTrade": None,
        "sampleSizeWarning": len(entries) < 30,
        "slippageTicks": 2, "commissionPerContract": 1.25,
        "fillAssumption": "next observed trade at or beyond trigger plus conservative slippage",
    }
