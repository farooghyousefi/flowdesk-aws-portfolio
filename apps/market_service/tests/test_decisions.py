from __future__ import annotations

from apps.market_service.decisions import explanation, setup_decision


def feature_payload(*, enough_bars: bool = False, delta: int = 12, absorption: bool = False) -> dict:
    bars = []
    if enough_bars:
        bars = [
            {"timeframe": "1m", "close": 5000 + index, "high": 5001 + index, "low": 4999 + index}
            for index in range(3)
        ]
    return {
        "tradeSummary": {"delta": delta}, "bars": bars,
        "marketStructure": [
            {"timeframe": "1m", "state": "trend_up"},
            {"timeframe": "5m", "state": "trend_up" if enough_bars else "insufficient_data"},
            {"timeframe": "15m", "state": "range"},
        ],
        "absorptionCandidates": [{"confidence": .7}] if absorption else [],
    }


def risk(state: str = "allowed") -> dict:
    return {"state": state, "humanReasons": [], "remainingDrawdown": 1500}


def test_trade_ready_wait_partial_and_risk_override() -> None:
    ready = setup_decision(timestamp="now", completeness="complete", features=feature_payload(enough_bars=True, absorption=True), risk=risk())
    assert ready["state"] == "trade_ready"
    waiting = setup_decision(timestamp="now", completeness="complete", features=feature_payload(), risk=risk())
    assert waiting["state"] == "wait"
    partial = setup_decision(timestamp="now", completeness="partial", features=feature_payload(enough_bars=True, absorption=True), risk=risk())
    assert partial["state"] == "wait"
    assert "PARTIAL_BOOK" in partial["reasonCodes"]
    blocked = setup_decision(timestamp="now", completeness="complete", features=feature_payload(enough_bars=True, absorption=True), risk=risk("blocked"))
    assert blocked["state"] == "blocked"
    assert blocked["reasonCodes"][0] == "RISK_GUARD_OVERRIDE"
    assert explanation(blocked, risk("blocked")).startswith("KEIN TRADE")
