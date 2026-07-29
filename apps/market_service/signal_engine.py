from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from .instruments import instrument_spec, size_position


@dataclass(frozen=True)
class SignalPolicy:
    debounce_ms: int = 250
    minimum_hold_ms: int = 750
    cooldown_ms: int = 5_000
    expiry_seconds: int = 45


def _reason(code: str, state: str, title_key: str, **values: Any) -> dict[str, Any]:
    return {"code": code, "state": state, "titleKey": title_key, **values}


class SignalEngine:
    def __init__(
        self,
        *,
        policy: SignalPolicy | None = None,
        persist: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.policy = policy or SignalPolicy()
        self.persist = persist
        self.current_status = "WAIT"
        self.pending_status: str | None = None
        self.pending_since_ns = 0
        self.last_change_ns = 0
        self.cooldown_until_ns = 0
        self.last_signature = ""

    def reset(self) -> None:
        self.current_status = "WAIT"
        self.pending_status = None
        self.pending_since_ns = 0
        self.last_change_ns = 0
        self.cooldown_until_ns = 0
        self.last_signature = ""

    def update(
        self,
        *,
        timestamp: str,
        timestamp_ns: int,
        setup_decision: dict[str, Any],
        risk: dict[str, Any],
        features: dict[str, Any],
        completeness: str,
        session_id: str | None,
        run_id: str | None,
        strategy_status: str = "RESEARCH_ONLY",
        strategy_version: str = "mes-retest-research-v1",
        model_version: str = "rules-baseline-v1",
    ) -> dict[str, Any]:
        desired = self._desired_status(setup_decision, risk, completeness, strategy_status)
        status = self._stable_status(desired, timestamp_ns)
        signal = self._build_signal(
            status=status,
            timestamp=timestamp,
            timestamp_ns=timestamp_ns,
            setup_decision=setup_decision,
            risk=risk,
            features=features,
            completeness=completeness,
            strategy_status=strategy_status,
            strategy_version=strategy_version,
            model_version=model_version,
        )
        signature_payload = {
            "status": signal["status"], "entryZone": signal["entryZone"], "stop": signal["stop"],
            "targets": signal["targets"], "contracts": signal["contracts"],
            "support": [item["code"] for item in signal["supportingEvidence"]],
            "oppose": [item["code"] for item in signal["opposingEvidence"]],
            "missing": [item["code"] for item in signal["missingEvidence"]],
        }
        signature = hashlib.sha256(json.dumps(signature_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        signal["signature"] = signature
        if self.persist and signature != self.last_signature:
            self.persist({
                "id": str(uuid.uuid4()), "session_id": session_id, "run_id": run_id,
                "timestamp": timestamp, "status": signal["status"],
                "strategy_version": strategy_version, "model_version": model_version,
                "payload": signal, "signature": signature,
            })
        self.last_signature = signature
        return signal

    @staticmethod
    def _desired_status(setup: dict[str, Any], risk: dict[str, Any], completeness: str, strategy_status: str) -> str:
        if risk["state"] == "blocked" or completeness != "complete":
            return "NO_TRADE"
        if strategy_status not in {"VALIDATED", "PAPER_ONLY"}:
            return "WAIT"
        if setup["state"] == "trade_ready":
            return "LONG" if setup.get("direction") == "long" else "SHORT"
        if setup["state"] == "blocked":
            return "NO_TRADE"
        return "WAIT"

    def _stable_status(self, desired: str, timestamp_ns: int) -> str:
        if desired == "NO_TRADE":
            if self.current_status in {"LONG", "SHORT"}:
                self.cooldown_until_ns = timestamp_ns + self.policy.cooldown_ms * 1_000_000
            self.current_status = desired
            self.last_change_ns = timestamp_ns
            self.pending_status = None
            return desired
        if timestamp_ns < self.cooldown_until_ns and desired in {"LONG", "SHORT"}:
            return "WAIT"
        if desired == self.current_status:
            self.pending_status = None
            return self.current_status
        if self.pending_status != desired:
            self.pending_status = desired
            self.pending_since_ns = timestamp_ns
            return self.current_status
        debounce_ns = self.policy.debounce_ms * 1_000_000
        hold_ns = self.policy.minimum_hold_ms * 1_000_000
        if timestamp_ns - self.pending_since_ns < debounce_ns:
            return self.current_status
        if self.last_change_ns and timestamp_ns - self.last_change_ns < hold_ns:
            return self.current_status
        self.current_status = desired
        self.last_change_ns = timestamp_ns
        self.pending_status = None
        return self.current_status

    def _build_signal(
        self,
        *,
        status: str,
        timestamp: str,
        timestamp_ns: int,
        setup_decision: dict[str, Any],
        risk: dict[str, Any],
        features: dict[str, Any],
        completeness: str,
        strategy_status: str,
        strategy_version: str,
        model_version: str,
    ) -> dict[str, Any]:
        active_trade = status in {"LONG", "SHORT"}
        raw_zone = setup_decision.get("entryZone") if active_trade else None
        raw_stop = setup_decision.get("invalidation") if active_trade else None
        raw_targets = (setup_decision.get("targets") or []) if active_trade else []
        spec = instrument_spec("MES")
        sizing: dict[str, Any] = {"allowed": False, "contracts": 0, "riskUsd": 0, "reasonCode": "NO_ACTIVE_TRADE"}
        preferred = None
        if raw_zone and raw_stop is not None:
            preferred = round(((float(raw_zone["min"]) + float(raw_zone["max"])) / 2) / spec.tick_size) * spec.tick_size
            sizing = size_position(
                symbol="MES", entry_price=preferred, stop_price=float(raw_stop),
                maximum_risk_usd=float(risk.get("plannedRiskUsd", 0)),
                remaining_drawdown_usd=float(risk.get("remainingDrawdown", 0)),
                maximum_contracts=int(risk.get("maximumContracts", 30)), estimated_slippage_ticks=3,
                round_trip_fees_usd=2.2,
                liquidity_contract_limit=int(features.get("topOfBookLiquidityContracts", 30)),
            )
            if not sizing["allowed"]:
                status = "NO_TRADE"
                active_trade = False
        spread_ticks = float(features.get("microstructure", {}).get("orderBook", {}).get("spreadTicks") or 0)
        if active_trade and spread_ticks > 2:
            status = "NO_TRADE"
            active_trade = False
            sizing = {"allowed": False, "contracts": 0, "riskUsd": 0, "reasonCode": "SPREAD_TOO_WIDE"}

        setup_reasons = setup_decision.get("reasons", [])
        supporting = [item for item in setup_reasons if item.get("state") in {"fulfilled", "partially_fulfilled"}]
        opposing = [item for item in setup_reasons if item.get("state") in {"blocking", "contradictory"}]
        missing = [item for item in setup_reasons if item.get("state") in {"missing", "unavailable"}]
        if strategy_status != "VALIDATED":
            missing.append(_reason("VALIDATED_STRATEGY_MISSING", "missing", "signal.validatedStrategyMissing"))
        if completeness != "complete":
            opposing.append(_reason("DATA_QUALITY_INSUFFICIENT", "blocking", "signal.dataQualityInsufficient"))
        if risk["state"] == "blocked":
            opposing.extend(risk.get("reasons", []))
        if raw_zone and not sizing.get("allowed"):
            reason_code = str(sizing.get("reasonCode") or "MINIMUM_POSITION_EXCEEDS_RISK")
            opposing.append(_reason(reason_code, "blocking", "signal.spreadTooWide" if reason_code == "SPREAD_TOO_WIDE" else "signal.minimumPositionExceedsRisk"))

        data_quality = "COMPLETE_L3" if completeness == "complete" else "DEGRADED"
        data_score = 1.0 if data_quality == "COMPLETE_L3" else 0.35
        validation_score = 1.0 if strategy_status == "VALIDATED" else 0.25
        fill_score = 0.8 if active_trade else 0.5
        base_score = float(setup_decision.get("confidence", 0)) / 100
        confidence = round(100 * (0.45 * base_score + 0.2 * data_score + 0.2 * validation_score + 0.15 * fill_score))
        quality = "A" if confidence >= 75 else "B" if confidence >= 62 else "C" if confidence >= 50 else "NONE"
        reward_risk = None
        if active_trade and preferred is not None and raw_stop is not None and raw_targets:
            risk_points = abs(preferred - float(raw_stop))
            reward_points = abs(float(raw_targets[0]) - preferred)
            reward_risk = round(reward_points / risk_points, 2) if risk_points else None
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        valid_until = (parsed.astimezone(UTC) + timedelta(seconds=self.policy.expiry_seconds)).isoformat().replace("+00:00", "Z") if active_trade else None
        context = features.get("context", {})
        regime = context.get("regime") or next((item.get("state") for item in features.get("marketStructure", []) if item.get("timeframe") == "5m"), "insufficient_data")
        return {
            "status": status,
            "setup": setup_decision.get("setupName", "MES Pullback / Retest"),
            "timestamp": timestamp,
            "timestampNs": str(timestamp_ns),
            "validUntil": valid_until,
            "entryZone": ({
                "min": raw_zone["min"], "max": raw_zone["max"], "preferred": preferred, "orderType": "LIMIT",
            } if active_trade and raw_zone else None),
            "stop": ({
                "price": raw_stop, "ticks": sizing.get("stopTicks", 0), "reasonCode": "STRUCTURE_INVALIDATION",
            } if active_trade and raw_stop is not None else None),
            "targets": ([{
                "price": target, "sizePercent": round(100 / len(raw_targets)), "reasonCode": "REFERENCE_STRUCTURE_TARGET",
            } for target in raw_targets] if active_trade else []),
            "contracts": int(sizing.get("contracts", 0)) if active_trade else 0,
            "riskUsd": float(sizing.get("riskUsd", 0)) if active_trade else 0,
            "rewardRisk": reward_risk,
            "estimatedFillQuality": "REALISTIC_MEDIUM" if active_trade else "NOT_APPLICABLE",
            "confidence": confidence,
            "quality": quality,
            "regime": regime,
            "supportingEvidence": supporting,
            "opposingEvidence": opposing,
            "missingEvidence": missing,
            "invalidation": (["STRUCTURE_BREAK", "DELTA_REVERSAL", "SIGNAL_EXPIRY"] if active_trade else []),
            "dataQuality": data_quality,
            "strategyVersion": strategy_version,
            "strategyValidationStatus": strategy_status,
            "modelVersion": model_version,
            "paperSignal": strategy_status == "PAPER_ONLY",
            "manualExecutionOnly": True,
            "automaticOrderExecution": False,
        }
