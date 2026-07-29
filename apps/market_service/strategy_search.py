from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from statistics import mean
from typing import TYPE_CHECKING, Any, Iterable, Literal

from .contracts import display_price
from .event_backtester import FillModel
from .instruments import instrument_spec

if TYPE_CHECKING:
    from apps.connectors.databento.src.dbn_reader import OrderBook


Direction = Literal["long", "short"]
TICK_SIZE = instrument_spec("MES").tick_size
POINT_VALUE = instrument_spec("MES").point_value_usd
ROUND_TRIP_FEES_USD = 2.20
REQUIRED_CONTRACTS = 1
# Every candidate's entry is deferred by this delay and resolved against the live book,
# not fabricated from the signal-time snapshot. Sourced from FillModel so the fast search
# and the realistic EventDrivenBacktester never silently drift apart on this assumption.
SIGNAL_TO_FILL_LATENCY_NS = FillModel().latency_ms * 1_000_000

FAMILY_LABELS = {
    "MES_L3_MOMENTUM": "MES L3 Momentum",
    "MES_PULLBACK_RETEST": "MES Pullback / Retest",
    "MES_VWAP_MEAN_REVERSION": "MES VWAP Mean Reversion",
    "MES_OPENING_RANGE_BREAKOUT": "MES Opening Range Breakout",
    "MES_ABSORPTION_REVERSAL": "MES Absorption Reversal",
}


@dataclass(frozen=True)
class StrategySpec:
    signed_volume_threshold: int
    delta_momentum_threshold: int
    queue_imbalance_threshold: float
    stop_ticks: int
    target_ticks: int
    cooldown_seconds: int = 45
    time_stop_seconds: int = 90
    maximum_spread_ticks: float = 2.0
    minimum_top_liquidity: int = 2
    family: str = "MES_L3_MOMENTUM"
    minimum_trend_strength: float = 0.0
    vwap_distance_ticks: float = 0.0
    maximum_pullback_ticks: float = 8.0
    opening_range_buffer_ticks: float = 1.0
    absorption_confidence: float = 0.65
    allowed_regimes: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return FAMILY_LABELS.get(self.family, self.family)

    def parameters(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "signedVolumeThreshold": self.signed_volume_threshold,
            "deltaMomentumThreshold": self.delta_momentum_threshold,
            "queueImbalanceThreshold": self.queue_imbalance_threshold,
            "stopTicks": self.stop_ticks,
            "targetTicks": self.target_ticks,
            "candidateCooldownSeconds": self.cooldown_seconds,
            "timeStopSeconds": self.time_stop_seconds,
            "maximumSpreadTicks": self.maximum_spread_ticks,
            "minimumTopLiquidity": self.minimum_top_liquidity,
            "minimumTrendStrength": self.minimum_trend_strength,
            "vwapDistanceTicks": self.vwap_distance_ticks,
            "maximumPullbackTicks": self.maximum_pullback_ticks,
            "openingRangeBufferTicks": self.opening_range_buffer_ticks,
            "absorptionConfidence": self.absorption_confidence,
            "allowedRegimes": list(self.allowed_regimes),
        }


@dataclass
class OpenPosition:
    direction: Direction
    decision_timestamp_ns: int
    entry_timestamp_ns: int
    entry_price: float
    stop_price: float
    target_price: float
    segment: str
    regime: str
    feature_snapshot: dict[str, Any]


@dataclass
class PendingEntry:
    """A signal awaiting its latency delay before resolve_pending() may fill it.

    Holding only the signal-time segment/regime/feature context (not a price) is
    deliberate: the fill price always comes from the live book at resolution time,
    never from what the book showed when the signal fired.
    """

    direction: Direction
    decision_timestamp_ns: int
    ready_at_ns: int
    segment: str
    regime: str
    feature_snapshot: dict[str, Any]
    fill_mode: str


@dataclass
class CandidateRuntime:
    spec: StrategySpec
    index: int
    trades: list[dict[str, Any]] = field(default_factory=list)
    open_position: OpenPosition | None = None
    pending: PendingEntry | None = None
    cooldown_until_ns: int = 0

    def on_trade(self, timestamp_ns: int, price: float, *, fill_mode: str) -> None:
        position = self.open_position
        if position is None:
            return
        sign = 1 if position.direction == "long" else -1
        stop_hit = price <= position.stop_price if position.direction == "long" else price >= position.stop_price
        target_hit = price >= position.target_price if position.direction == "long" else price <= position.target_price
        timed_out = timestamp_ns >= position.entry_timestamp_ns + self.spec.time_stop_seconds * 1_000_000_000
        if not (stop_hit or target_hit or timed_out):
            return
        if stop_hit:
            exit_reason = "STOP"
            exit_slippage_ticks = {"optimistic": 1.0, "realistic": 2.0, "stressed": 3.0}[fill_mode]
        elif target_hit:
            exit_reason = "TARGET"
            exit_slippage_ticks = {"optimistic": 0.0, "realistic": 1.0, "stressed": 1.5}[fill_mode]
        else:
            exit_reason = "TIME_STOP"
            exit_slippage_ticks = {"optimistic": 0.0, "realistic": 1.0, "stressed": 1.5}[fill_mode]
        exit_price = price - sign * exit_slippage_ticks * TICK_SIZE
        gross = sign * (exit_price - position.entry_price) * POINT_VALUE
        net = gross - ROUND_TRIP_FEES_USD
        initial_risk = abs(position.entry_price - position.stop_price) * POINT_VALUE + ROUND_TRIP_FEES_USD
        self.trades.append({
            "family": self.spec.family,
            "strategyName": self.spec.name,
            "direction": position.direction,
            "decisionTimestampNs": position.decision_timestamp_ns,
            "entryTimestampNs": position.entry_timestamp_ns,
            "exitTimestampNs": timestamp_ns,
            "holdingSeconds": round((timestamp_ns - position.entry_timestamp_ns) / 1_000_000_000, 3),
            "entryPrice": round(position.entry_price, 6),
            "exitPrice": round(exit_price, 6),
            "stopPrice": round(position.stop_price, 6),
            "targetPrice": round(position.target_price, 6),
            "grossUsd": round(gross, 2),
            "costUsd": ROUND_TRIP_FEES_USD,
            "netUsd": round(net, 2),
            "resultR": round(net / initial_risk, 4) if initial_risk else 0.0,
            "exitReason": exit_reason,
            "segment": position.segment,
            "regime": position.regime,
            "featureSnapshot": position.feature_snapshot,
        })
        self.open_position = None
        self.cooldown_until_ns = timestamp_ns + self.spec.cooldown_seconds * 1_000_000_000

    def maybe_open(
        self,
        *,
        timestamp_ns: int,
        segment: str,
        features: dict[str, Any],
        fill_mode: str,
    ) -> None:
        """Queue a latency-delayed entry from a signal; never opens a position directly.

        The signal's own order-book snapshot is not used for pricing: by the time an
        order could reach the exchange it may already be stale. resolve_pending() prices
        the fill off whatever the live book actually shows once the delay has elapsed.
        """
        if self.open_position is not None or self.pending is not None or timestamp_ns < self.cooldown_until_ns:
            return
        direction = strategy_direction(features, self.spec.parameters())
        if direction is None:
            return
        context = features.get("context", {})
        self.pending = PendingEntry(
            direction=direction,
            decision_timestamp_ns=timestamp_ns,
            ready_at_ns=timestamp_ns + SIGNAL_TO_FILL_LATENCY_NS,
            segment=segment,
            regime=str(context.get("regime") or "unknown"),
            feature_snapshot=_compact_feature_snapshot(features),
            fill_mode=fill_mode,
        )

    def resolve_pending(self, timestamp_ns: int, *, book: "OrderBook") -> None:
        """Fill a pending entry against the live book once its latency delay has elapsed.

        Deliberately conservative: a missing side, zero size, or a locked/crossed book at
        resolution time simply lets the signal expire with no trade, rather than fabricate
        a fill from data that was not actually executable.
        """
        pending = self.pending
        if pending is None or timestamp_ns < pending.ready_at_ns:
            return
        self.pending = None
        if self.open_position is not None:
            return
        best_bid = book.best_bid()
        best_ask = book.best_ask()
        if best_bid is None or best_ask is None:
            return
        bid_price, bid_size = display_price(best_bid.price), best_bid.total_size
        ask_price, ask_size = display_price(best_ask.price), best_ask.total_size
        if bid_price <= 0 or ask_price <= 0 or bid_price >= ask_price:
            return  # missing side, or a locked/crossed book: do not trade through it
        sign = 1 if pending.direction == "long" else -1
        quote_price = ask_price if pending.direction == "long" else bid_price
        available_size = ask_size if pending.direction == "long" else bid_size
        if available_size <= 0:
            return
        # The book can thin between signal and fill (that is the entire point of modeling
        # latency). A candidate declared it trusts the top level once it holds at least
        # minimum_top_liquidity contracts (the same bar used to gate the signal itself); if
        # that has degraded by resolution time, approximate walking to the next price level
        # as one extra conservative tick per unit of shortfall, capped so a single dust-thin
        # print cannot blow out the price arbitrarily. Exact multi-level depth is not exposed
        # at this layer — see the execution-audit report for why this is an accepted
        # fast-search simplification rather than a true depth-walk.
        shortfall = max(0, min(self.spec.minimum_top_liquidity, REQUIRED_CONTRACTS + 3) - available_size)
        entry_slippage_ticks = {"optimistic": 0.0, "realistic": 1.0, "stressed": 1.5}[pending.fill_mode]
        entry_price = quote_price + sign * (entry_slippage_ticks + shortfall) * TICK_SIZE
        self.open_position = OpenPosition(
            direction=pending.direction,
            decision_timestamp_ns=pending.decision_timestamp_ns,
            entry_timestamp_ns=timestamp_ns,
            entry_price=entry_price,
            stop_price=entry_price - sign * self.spec.stop_ticks * TICK_SIZE,
            target_price=entry_price + sign * self.spec.target_ticks * TICK_SIZE,
            segment=pending.segment,
            regime=pending.regime,
            feature_snapshot=pending.feature_snapshot,
        )

    def close_at_end(self, timestamp_ns: int, price: float, *, fill_mode: str) -> None:
        position = self.open_position
        if position is None:
            return
        sign = 1 if position.direction == "long" else -1
        slip_ticks = {"optimistic": 0.0, "realistic": 1.0, "stressed": 1.5}[fill_mode]
        exit_price = price - sign * slip_ticks * TICK_SIZE
        gross = sign * (exit_price - position.entry_price) * POINT_VALUE
        net = gross - ROUND_TRIP_FEES_USD
        initial_risk = abs(position.entry_price - position.stop_price) * POINT_VALUE + ROUND_TRIP_FEES_USD
        self.trades.append({
            "family": self.spec.family,
            "strategyName": self.spec.name,
            "direction": position.direction,
            "decisionTimestampNs": position.decision_timestamp_ns,
            "entryTimestampNs": position.entry_timestamp_ns,
            "exitTimestampNs": timestamp_ns,
            "holdingSeconds": round((timestamp_ns - position.entry_timestamp_ns) / 1_000_000_000, 3),
            "entryPrice": round(position.entry_price, 6),
            "exitPrice": round(exit_price, 6),
            "stopPrice": round(position.stop_price, 6),
            "targetPrice": round(position.target_price, 6),
            "grossUsd": round(gross, 2),
            "costUsd": ROUND_TRIP_FEES_USD,
            "netUsd": round(net, 2),
            "resultR": round(net / initial_risk, 4) if initial_risk else 0.0,
            "exitReason": "END_OF_DATA",
            "segment": position.segment,
            "regime": position.regime,
            "featureSnapshot": position.feature_snapshot,
        })
        self.open_position = None


def curated_strategy_specs() -> list[StrategySpec]:
    """A bounded, auditable search grid across distinct strategy families.

    This intentionally avoids an unbounded optimizer. Each candidate has an economic
    interpretation and is evaluated on the same event stream with the same cost model.
    """
    specs: list[StrategySpec] = []
    risk_pairs = ((6, 10), (8, 14))

    for signed, momentum, imbalance in ((16, 5, 0.08), (28, 10, 0.12), (48, 18, 0.18)):
        for stop_ticks, target_ticks in risk_pairs:
            specs.append(StrategySpec(
                signed, momentum, imbalance, stop_ticks, target_ticks,
                family="MES_L3_MOMENTUM", allowed_regimes=("momentum", "chop"),
            ))

    for signed, momentum, imbalance, trend in ((10, 3, 0.05, 0.35), (18, 6, 0.08, 0.45), (28, 10, 0.12, 0.55)):
        for stop_ticks, target_ticks in risk_pairs:
            specs.append(StrategySpec(
                signed, momentum, imbalance, stop_ticks, target_ticks,
                family="MES_PULLBACK_RETEST", minimum_trend_strength=trend,
                maximum_pullback_ticks=8.0, allowed_regimes=("momentum",),
            ))

    for signed, momentum, imbalance, distance in ((8, 2, 0.04, 4), (14, 4, 0.06, 6), (22, 7, 0.10, 8)):
        for stop_ticks, target_ticks in risk_pairs:
            specs.append(StrategySpec(
                signed, momentum, imbalance, stop_ticks, target_ticks,
                family="MES_VWAP_MEAN_REVERSION", vwap_distance_ticks=distance,
                allowed_regimes=("mean_reversion", "chop"),
            ))

    for signed, momentum, imbalance, buffer_ticks in ((18, 6, 0.08, 1), (32, 12, 0.12, 2)):
        for stop_ticks, target_ticks in ((8, 14), (10, 18)):
            specs.append(StrategySpec(
                signed, momentum, imbalance, stop_ticks, target_ticks,
                family="MES_OPENING_RANGE_BREAKOUT", opening_range_buffer_ticks=buffer_ticks,
                allowed_regimes=("momentum", "chop"), time_stop_seconds=180,
            ))

    for signed, momentum, imbalance, confidence in ((12, 3, 0.04, 0.62), (22, 6, 0.08, 0.72)):
        for stop_ticks, target_ticks in ((6, 10), (8, 12)):
            specs.append(StrategySpec(
                signed, momentum, imbalance, stop_ticks, target_ticks,
                family="MES_ABSORPTION_REVERSAL", absorption_confidence=confidence,
                allowed_regimes=("mean_reversion", "chop"),
            ))
    return specs


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _structure_state(features: dict[str, Any]) -> str:
    structures = features.get("marketStructure") or []
    for timeframe in ("5m", "1m", "15m"):
        row = next((item for item in structures if item.get("timeframe") == timeframe), None)
        if row and row.get("state") not in {None, "insufficient_data"}:
            return str(row.get("state"))
    return "insufficient_data"


def _common_values(features: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any] | None:
    micro = features.get("microstructure", {})
    aggression = micro.get("tradeAggression", {})
    order_book = micro.get("orderBook", {})
    context = features.get("context", {})
    external = features.get("externalContext", {})
    spread_ticks = _number(order_book.get("spreadTicks"), 999)
    top_liquidity = int(_number(order_book.get("topOfBookLiquidityContracts"), 0))
    if spread_ticks > _number(parameters.get("maximumSpreadTicks"), 2.0):
        return None
    if top_liquidity < int(_number(parameters.get("minimumTopLiquidity"), 2)):
        return None
    if external.get("gate") == "blocked":
        return None
    allowed_regimes = tuple(parameters.get("allowedRegimes") or ())
    regime = str(context.get("regime") or "unknown")
    if allowed_regimes and regime not in allowed_regimes:
        return None
    best_bid = _number(order_book.get("bestBid"), 0)
    best_ask = _number(order_book.get("bestAsk"), 0)
    mid = _number(order_book.get("midprice"), (best_bid + best_ask) / 2 if best_bid and best_ask else 0)
    return {
        "signed": _number(aggression.get("signedVolume")),
        "momentum": _number(aggression.get("deltaMomentum")),
        "imbalance": _number(order_book.get("queueImbalance")),
        "mid": mid,
        "vwap": _number(context.get("vwap"), 0),
        "regime": regime,
        "trendStrength": _number(context.get("trendStrength")),
        "structure": _structure_state(features),
        "openingRange": context.get("openingRange") or {},
        "sessionPhase": str(context.get("sessionPhase") or "unknown"),
        "exhaustion": bool(aggression.get("exhaustionCandidate")),
    }


def strategy_direction(features: dict[str, Any], parameters: dict[str, Any]) -> Direction | None:
    values = _common_values(features, parameters)
    if values is None:
        return None
    family = str(parameters.get("family") or "MES_L3_MOMENTUM")
    signed = values["signed"]
    momentum = values["momentum"]
    imbalance = values["imbalance"]
    signed_threshold = _number(parameters.get("signedVolumeThreshold"), 20)
    momentum_threshold = _number(parameters.get("deltaMomentumThreshold"), 8)
    imbalance_threshold = _number(parameters.get("queueImbalanceThreshold"), 0.10)

    if family == "MES_L3_MOMENTUM":
        if signed >= signed_threshold and momentum >= momentum_threshold and imbalance >= imbalance_threshold:
            return "long"
        if signed <= -signed_threshold and momentum <= -momentum_threshold and imbalance <= -imbalance_threshold:
            return "short"
        return None

    if family == "MES_PULLBACK_RETEST":
        vwap = values["vwap"]
        mid = values["mid"]
        if not vwap or not mid or values["trendStrength"] < _number(parameters.get("minimumTrendStrength"), 0.4):
            return None
        distance_ticks = (mid - vwap) / TICK_SIZE
        max_pullback = _number(parameters.get("maximumPullbackTicks"), 8)
        if values["structure"] == "trend_up" and -max_pullback <= distance_ticks <= 2:
            if signed >= signed_threshold and momentum >= momentum_threshold and imbalance >= imbalance_threshold:
                return "long"
        if values["structure"] == "trend_down" and -2 <= distance_ticks <= max_pullback:
            if signed <= -signed_threshold and momentum <= -momentum_threshold and imbalance <= -imbalance_threshold:
                return "short"
        return None

    if family == "MES_VWAP_MEAN_REVERSION":
        vwap = values["vwap"]
        mid = values["mid"]
        if not vwap or not mid:
            return None
        distance_ticks = (mid - vwap) / TICK_SIZE
        minimum_distance = _number(parameters.get("vwapDistanceTicks"), 6)
        if distance_ticks >= minimum_distance:
            if signed <= -signed_threshold and momentum <= -momentum_threshold and imbalance <= -imbalance_threshold:
                return "short"
        if distance_ticks <= -minimum_distance:
            if signed >= signed_threshold and momentum >= momentum_threshold and imbalance >= imbalance_threshold:
                return "long"
        return None

    if family == "MES_OPENING_RANGE_BREAKOUT":
        opening = values["openingRange"]
        if not opening.get("complete") or values["sessionPhase"] not in {"cash_open", "midday"}:
            return None
        high = _number(opening.get("high"), 0)
        low = _number(opening.get("low"), 0)
        mid = values["mid"]
        buffer_ticks = _number(parameters.get("openingRangeBufferTicks"), 1)
        if high and mid >= high + buffer_ticks * TICK_SIZE:
            if signed >= signed_threshold and momentum >= momentum_threshold and imbalance >= imbalance_threshold:
                return "long"
        if low and mid <= low - buffer_ticks * TICK_SIZE:
            if signed <= -signed_threshold and momentum <= -momentum_threshold and imbalance <= -imbalance_threshold:
                return "short"
        return None

    if family == "MES_ABSORPTION_REVERSAL":
        minimum_confidence = _number(parameters.get("absorptionConfidence"), 0.65)
        candidates = features.get("absorptionCandidates") or []
        best = max(candidates, key=lambda item: _number(item.get("confidence")), default=None)
        if not best or _number(best.get("confidence")) < minimum_confidence:
            return None
        # Ask-side absorption means aggressive buying failed to move price: short reversal.
        if best.get("side") == "ask" and signed >= signed_threshold and imbalance <= imbalance_threshold:
            return "short"
        # Bid-side absorption means aggressive selling failed to move price: long reversal.
        if best.get("side") == "bid" and signed <= -signed_threshold and imbalance >= -imbalance_threshold:
            return "long"
        return None
    return None


def strategy_setup_decision(
    *,
    timestamp: str,
    completeness: str,
    features: dict[str, Any],
    risk: dict[str, Any],
    strategy: dict[str, Any],
) -> dict[str, Any]:
    config = strategy.get("config", {})
    parameters = config.get("parameters", config)
    family = str(parameters.get("family") or config.get("family") or "MES_L3_MOMENTUM")
    setup_name = FAMILY_LABELS.get(family, str(strategy.get("name") or family))
    direction = strategy_direction(features, parameters)
    micro = features.get("microstructure", {})
    aggression = micro.get("tradeAggression", {})
    order_book = micro.get("orderBook", {})
    external = features.get("externalContext", {})
    signed_volume = _number(aggression.get("signedVolume"))
    delta_momentum = _number(aggression.get("deltaMomentum"))
    queue_imbalance = _number(order_book.get("queueImbalance"))
    spread_ticks = _number(order_book.get("spreadTicks"), 999)
    reasons = [
        _reason("COMPLETE_INITIAL_BOOK", "fulfilled" if completeness == "complete" else "unavailable", "setup.completeInitialBook"),
        _threshold_reason("SIGNED_VOLUME", signed_volume, _number(parameters.get("signedVolumeThreshold"), 20)),
        _threshold_reason("DELTA_MOMENTUM", delta_momentum, _number(parameters.get("deltaMomentumThreshold"), 8)),
        _threshold_reason("QUEUE_IMBALANCE", queue_imbalance, _number(parameters.get("queueImbalanceThreshold"), 0.10)),
        _reason(
            "SPREAD_LIMIT",
            "fulfilled" if spread_ticks <= _number(parameters.get("maximumSpreadTicks"), 2.0) else "blocking",
            "signal.spreadTooWide",
            measuredValue=spread_ticks,
            requiredValue=_number(parameters.get("maximumSpreadTicks"), 2.0),
        ),
        _reason(
            "ECONOMIC_CALENDAR_COVERAGE",
            "fulfilled" if external.get("calendarCoverage") == "complete" else "missing",
            "strategy.economic_calendar_coverage",
        ),
        _reason(
            "NEWS_COVERAGE",
            "fulfilled" if external.get("newsCoverage") == "complete" else "missing",
            "strategy.news_coverage",
        ),
    ]
    for code in external.get("gateReasons") or []:
        reasons.append(_reason(str(code), "blocking", f"strategy.{str(code).lower()}"))

    context_missing = external.get("calendarCoverage") != "complete" or external.get("newsCoverage") != "complete"
    if completeness != "complete" or risk.get("state") == "blocked" or external.get("gate") == "blocked":
        return {
            "state": "blocked",
            "setupName": setup_name,
            "direction": direction,
            "confidence": 0,
            "entryZone": None,
            "invalidation": None,
            "targets": [],
            "reasons": [*reasons, *risk.get("reasons", [])],
            "strategyHash": strategy.get("strategy_hash"),
        }
    if context_missing or direction is None:
        return {
            "state": "watching",
            "setupName": setup_name,
            "direction": direction,
            "confidence": _signal_confidence(signed_volume, delta_momentum, queue_imbalance, parameters),
            "entryZone": None,
            "invalidation": None,
            "targets": [],
            "reasons": reasons,
            "strategyHash": strategy.get("strategy_hash"),
        }
    raw_price = order_book.get("bestAsk") if direction == "long" else order_book.get("bestBid")
    if raw_price is None:
        raw_price = order_book.get("midprice")
    if raw_price is None:
        return {
            "state": "watching", "setupName": setup_name, "direction": direction,
            "confidence": 0, "entryZone": None, "invalidation": None, "targets": [],
            "reasons": [*reasons, _reason("TOP_OF_BOOK_MISSING", "missing", "signal.dataQualityInsufficient")],
            "strategyHash": strategy.get("strategy_hash"),
        }
    entry = float(raw_price)
    sign = 1 if direction == "long" else -1
    stop_ticks = int(parameters.get("stopTicks", 8))
    target_ticks = int(parameters.get("targetTicks", 14))
    return {
        "state": "trade_ready",
        "setupName": setup_name,
        "direction": direction,
        "confidence": _signal_confidence(signed_volume, delta_momentum, queue_imbalance, parameters),
        "entryZone": {"min": entry - (TICK_SIZE if direction == "long" else 0), "max": entry + (TICK_SIZE if direction == "short" else 0)},
        "invalidation": entry - sign * stop_ticks * TICK_SIZE,
        "targets": [entry + sign * target_ticks * TICK_SIZE],
        "reasons": reasons,
        "strategyHash": strategy.get("strategy_hash"),
        "paperOnly": strategy.get("status") == "PAPER_ACTIVE",
        "timestamp": timestamp,
    }


def segment_name(timestamp_ns: int, start_ns: int, end_ns: int) -> str:
    span = max(1, end_ns - start_ns)
    fraction = (timestamp_ns - start_ns) / span
    if fraction < 0.50:
        return "Development"
    if fraction < 0.75:
        return "Validation"
    return "Intraday Holdout"


def parse_timestamp_ns(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(parsed.timestamp() * 1_000_000_000)


def metrics_for_trades(trades: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(trades)
    results = [float(row["netUsd"]) for row in rows]
    gross_results = [float(row.get("grossUsd", row["netUsd"])) for row in rows]
    costs = [float(row.get("costUsd") or 0) for row in rows]
    r_values = [float(row.get("resultR") or 0) for row in rows]
    wins = [value for value in results if value > 0]
    losses = [value for value in results if value < 0]
    equity = peak = drawdown = 0.0
    for result in results:
        equity += result
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "trades": len(rows),
        "longTrades": sum(1 for row in rows if row.get("direction") == "long"),
        "shortTrades": sum(1 for row in rows if row.get("direction") == "short"),
        "netResultUsd": round(sum(results), 2),
        "grossResultUsd": round(sum(gross_results), 2),
        "costDragUsd": round(sum(costs), 2),
        "netExpectancyUsd": round(mean(results), 3) if results else 0.0,
        "grossExpectancyUsd": round(mean(gross_results), 3) if gross_results else 0.0,
        "expectancyR": round(mean(r_values), 4) if r_values else 0.0,
        "profitFactor": round(sum(wins) / abs(sum(losses)), 4) if losses else (999.0 if wins else None),
        "maximumDrawdownUsd": round(drawdown, 2),
        "winRate": round(len(wins) / len(rows), 4) if rows else 0.0,
        "averageHoldingSeconds": round(mean(float(row.get("holdingSeconds") or 0) for row in rows), 3) if rows else 0.0,
    }


def summarize_candidate(runtime: CandidateRuntime, *, context_coverage: dict[str, bool] | None = None) -> dict[str, Any]:
    context_coverage = context_coverage or {"calendar": True, "news": True}
    by_segment = {
        segment: metrics_for_trades(row for row in runtime.trades if row["segment"] == segment)
        for segment in ("Development", "Validation", "Intraday Holdout")
    }
    regimes = sorted({str(row.get("regime") or "unknown") for row in runtime.trades})
    by_regime = {
        regime: metrics_for_trades(row for row in runtime.trades if str(row.get("regime") or "unknown") == regime)
        for regime in regimes
    }
    aggregate = metrics_for_trades(runtime.trades)
    development = by_segment["Development"]
    validation = by_segment["Validation"]
    holdout = by_segment["Intraday Holdout"]
    positive_regimes = sum(1 for metrics in by_regime.values() if float(metrics["netExpectancyUsd"]) > 0)
    regime_stability = positive_regimes / max(len(by_regime), 1)
    cost_ratio = float(aggregate["costDragUsd"]) / max(abs(float(aggregate["grossResultUsd"])), 1)
    ranking_score = (
        0.45 * float(validation["netExpectancyUsd"]) * min(1.0, int(validation["trades"]) / 8)
        + 0.35 * float(holdout["netExpectancyUsd"]) * min(1.0, int(holdout["trades"]) / 6)
        + 0.20 * float(development["netExpectancyUsd"]) * min(1.0, int(development["trades"]) / 12)
        - 0.01 * float(aggregate["maximumDrawdownUsd"])
        - min(5.0, cost_ratio)
    )
    paper_failures: list[str] = []
    if int(development["trades"]) < 5:
        paper_failures.append("DEVELOPMENT_TRADES")
    if int(validation["trades"]) < 3:
        paper_failures.append("VALIDATION_TRADES")
    if int(holdout["trades"]) < 3:
        paper_failures.append("HOLDOUT_TRADES")
    if float(development["netExpectancyUsd"]) <= 0:
        paper_failures.append("DEVELOPMENT_EXPECTANCY")
    if float(validation["netExpectancyUsd"]) <= 0:
        paper_failures.append("VALIDATION_EXPECTANCY")
    if float(holdout["netExpectancyUsd"]) <= 0:
        paper_failures.append("HOLDOUT_EXPECTANCY")
    if float(validation.get("profitFactor") or 0) < 1.05:
        paper_failures.append("VALIDATION_PROFIT_FACTOR")
    if float(holdout.get("profitFactor") or 0) < 1.05:
        paper_failures.append("HOLDOUT_PROFIT_FACTOR")
    if float(aggregate["maximumDrawdownUsd"]) > 450:
        paper_failures.append("DRAWDOWN")
    if not context_coverage.get("calendar"):
        paper_failures.append("CALENDAR_COVERAGE")
    if not context_coverage.get("news"):
        paper_failures.append("NEWS_COVERAGE")

    diagnosis: list[str] = []
    if int(aggregate["trades"]) == 0:
        diagnosis.append("NO_TRADES")
    elif int(aggregate["trades"]) < 20:
        diagnosis.append("TOO_FEW_TRADES")
    if float(aggregate["netExpectancyUsd"]) <= 0:
        diagnosis.append("NEGATIVE_EXPECTANCY")
    if float(aggregate["grossExpectancyUsd"]) > 0 >= float(aggregate["netExpectancyUsd"]):
        diagnosis.append("COST_DRAG")
    if float(aggregate["maximumDrawdownUsd"]) > 450:
        diagnosis.append("DRAWDOWN_TOO_HIGH")
    if any(float(row["netExpectancyUsd"]) <= 0 for row in by_segment.values()):
        diagnosis.append("INTRADAY_INSTABILITY")
    if by_regime and regime_stability < 0.5:
        diagnosis.append("REGIME_DEPENDENT")
    if not context_coverage.get("calendar"):
        diagnosis.append("CALENDAR_COVERAGE_MISSING")
    if not context_coverage.get("news"):
        diagnosis.append("NEWS_COVERAGE_MISSING")
    diagnosis.append("NEED_MORE_INDEPENDENT_SESSIONS")

    aggregate.update({
        "regimeStability": round(regime_stability, 4),
        "costRatio": round(cost_ratio, 4),
    })
    return {
        "candidateIndex": runtime.index,
        "family": runtime.spec.family,
        "strategyName": runtime.spec.name,
        "parameters": runtime.spec.parameters(),
        "trades": runtime.trades,
        "metrics": aggregate,
        "segmentMetrics": by_segment,
        "regimeMetrics": by_regime,
        "rankingScore": round(ranking_score, 6),
        "paperEligible": not paper_failures,
        "paperFailedReasons": list(dict.fromkeys(paper_failures)),
        "diagnosis": list(dict.fromkeys(diagnosis)),
    }


def _compact_feature_snapshot(features: dict[str, Any]) -> dict[str, Any]:
    micro = features.get("microstructure", {})
    context = features.get("context", {})
    external = features.get("externalContext", {})
    return {
        "orderBook": {
            key: micro.get("orderBook", {}).get(key)
            for key in ("bestBid", "bestAsk", "spreadTicks", "queueImbalance", "topOfBookLiquidityContracts")
        },
        "tradeAggression": {
            key: micro.get("tradeAggression", {}).get(key)
            for key in ("signedVolume", "deltaMomentum", "tradesPerSecond", "volumePerSecond", "exhaustionCandidate")
        },
        "marketContext": {
            key: context.get(key)
            for key in ("vwap", "openingRange", "trendStrength", "regime", "sessionPhase")
        },
        "marketStructure": features.get("marketStructure", []),
        "externalContext": {
            key: external.get(key)
            for key in ("calendarCoverage", "newsCoverage", "eventRisk", "newsRisk", "gateReasons")
        },
    }


def _signal_confidence(signed_volume: float, delta_momentum: float, queue_imbalance: float, parameters: dict[str, Any]) -> int:
    signed_ratio = abs(signed_volume) / max(_number(parameters.get("signedVolumeThreshold"), 20), 1)
    momentum_ratio = abs(delta_momentum) / max(_number(parameters.get("deltaMomentumThreshold"), 8), 1)
    imbalance_ratio = abs(queue_imbalance) / max(_number(parameters.get("queueImbalanceThreshold"), 0.10), 0.001)
    return round(max(0.0, min(0.92, 0.45 + 0.12 * min(signed_ratio, 2) + 0.12 * min(momentum_ratio, 2) + 0.10 * min(imbalance_ratio, 2))) * 100)


def _threshold_reason(code: str, measured: float, required: float) -> dict[str, Any]:
    passed = abs(measured) >= required
    return _reason(
        code,
        "fulfilled" if passed else "missing",
        f"strategy.{code.lower()}",
        measuredValue=round(measured, 4),
        requiredValue=required,
    )


def _reason(code: str, state: str, title_key: str, **values: Any) -> dict[str, Any]:
    return {"code": code, "state": state, "titleKey": title_key, **values}
