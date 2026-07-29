from __future__ import annotations

from dataclasses import dataclass
from math import floor


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    tick_size: float
    tick_value_usd: float
    point_value_usd: float
    minimum_contracts: int = 1


INSTRUMENTS = {
    "MES": InstrumentSpec(
        symbol="MES",
        tick_size=0.25,
        tick_value_usd=1.25,
        point_value_usd=5.0,
    ),
}


def instrument_spec(symbol: str) -> InstrumentSpec:
    try:
        return INSTRUMENTS[symbol.upper()]
    except KeyError as exc:
        raise ValueError(f"Unsupported instrument: {symbol}") from exc


def size_position(
    *,
    symbol: str,
    entry_price: float,
    stop_price: float,
    maximum_risk_usd: float,
    remaining_drawdown_usd: float,
    maximum_contracts: int,
    estimated_slippage_ticks: float,
    round_trip_fees_usd: float,
    liquidity_contract_limit: int | None = None,
) -> dict[str, float | int | str | bool]:
    spec = instrument_spec(symbol)
    stop_ticks = abs(entry_price - stop_price) / spec.tick_size
    per_contract_risk = (
        stop_ticks * spec.tick_value_usd
        + max(0.0, estimated_slippage_ticks) * spec.tick_value_usd
        + max(0.0, round_trip_fees_usd)
    )
    risk_budget = max(0.0, min(maximum_risk_usd, remaining_drawdown_usd))
    liquidity_limit = maximum_contracts if liquidity_contract_limit is None else max(0, liquidity_contract_limit)
    contract_cap = max(0, min(maximum_contracts, liquidity_limit))
    contracts = min(contract_cap, floor(risk_budget / per_contract_risk)) if per_contract_risk > 0 else 0
    allowed = contracts >= spec.minimum_contracts
    return {
        "allowed": allowed,
        "contracts": contracts if allowed else 0,
        "riskUsd": round(per_contract_risk * contracts, 2) if allowed else round(per_contract_risk, 2),
        "riskPerContractUsd": round(per_contract_risk, 2),
        "stopTicks": round(stop_ticks, 2),
        "riskBudgetUsd": round(risk_budget, 2),
        "reasonCode": "POSITION_WITHIN_LIMITS" if allowed else "MINIMUM_POSITION_EXCEEDS_RISK",
    }

