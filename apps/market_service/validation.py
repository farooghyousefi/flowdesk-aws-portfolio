from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SPLIT_ORDER = ("Development", "Training", "Validation", "Pilot", "Locked Test", "Forward Paper")


@dataclass(frozen=True)
class ValidationPolicy:
    minimum_independent_sessions: int = 30
    minimum_locked_sessions: int = 10
    minimum_trades: int = 100
    minimum_profit_factor: float = 1.05
    maximum_drawdown_r: float = 12.0
    maximum_cost_degradation: float = 0.35


def chronological_session_split(sessions: list[dict[str, Any]]) -> dict[str, list[str]]:
    ordered = sorted(sessions, key=lambda item: (item["start_at"], item["id"]))
    count = len(ordered)
    boundaries = {
        "Development": (0, round(count * 0.30)),
        "Training": (round(count * 0.30), round(count * 0.55)),
        "Validation": (round(count * 0.55), round(count * 0.75)),
        "Pilot": (round(count * 0.75), round(count * 0.85)),
        "Locked Test": (round(count * 0.85), round(count * 0.95)),
        "Forward Paper": (round(count * 0.95), count),
    }
    return {name: [item["id"] for item in ordered[start:end]] for name, (start, end) in boundaries.items()}


def purged_walk_forward_windows(session_ids: list[str], *, train_size: int, test_size: int, embargo: int = 1) -> list[dict[str, list[str]]]:
    if train_size < 1 or test_size < 1 or embargo < 0:
        raise ValueError("Train size, test size, and embargo must define a positive chronology.")
    windows = []
    cursor = train_size
    while cursor + embargo + test_size <= len(session_ids):
        windows.append({
            "train": session_ids[cursor - train_size:cursor],
            "embargo": session_ids[cursor:cursor + embargo],
            "test": session_ids[cursor + embargo:cursor + embargo + test_size],
        })
        cursor += test_size
    return windows


def evaluate_promotion(metrics: dict[str, Any], *, policy: ValidationPolicy | None = None) -> dict[str, Any]:
    active = policy or ValidationPolicy()
    checks = [
        ("MINIMUM_SESSIONS", int(metrics.get("independentSessions", 0)) >= active.minimum_independent_sessions),
        ("MINIMUM_LOCKED_SESSIONS", int(metrics.get("lockedSessions", 0)) >= active.minimum_locked_sessions),
        ("MINIMUM_TRADES", int(metrics.get("trades", 0)) >= active.minimum_trades),
        ("POSITIVE_NET_EXPECTANCY", float(metrics.get("netExpectancyUsd", 0)) > 0),
        ("PROFIT_FACTOR", float(metrics.get("profitFactor", 0) or 0) >= active.minimum_profit_factor),
        ("DRAWDOWN", float(metrics.get("maximumDrawdownR", 999)) <= active.maximum_drawdown_r),
        ("COST_SENSITIVITY", float(metrics.get("costDegradation", 1)) <= active.maximum_cost_degradation),
        ("PARAMETER_STABILITY", float(metrics.get("parameterStability", 0)) >= 0.6),
        ("REGIME_STABILITY", float(metrics.get("regimeStability", 0)) >= 0.6),
        ("LOCKED_UNTOUCHED", bool(metrics.get("lockedDataUntouched", False))),
    ]
    failed = [code for code, passed in checks if not passed]
    return {
        "eligible": not failed,
        "status": "PROMOTION_ELIGIBLE" if not failed else "RESEARCH_ONLY",
        "checks": [{"code": code, "passed": passed} for code, passed in checks],
        "failedReasons": failed,
        "profitabilityClaim": False,
    }

