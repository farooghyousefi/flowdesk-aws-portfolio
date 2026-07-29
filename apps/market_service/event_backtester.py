from __future__ import annotations

import random
from heapq import heappop, heappush
from collections import deque
from dataclasses import dataclass, field
from statistics import mean
from typing import Callable, Iterable, Literal

from .instruments import instrument_spec
from .market_events import NormalizedMarketEvent


FillMode = Literal["optimistic", "realistic", "stressed"]
OrderType = Literal["MARKET", "LIMIT", "STOP"]
ExitLevelReference = Literal["ABSOLUTE_MARKET_LEVEL", "ENTRY_RELATIVE"]
ResearchControlCheck = Callable[[], str | None]
ResearchProgressCallback = Callable[[int, int], None]


class BacktestInterrupted(RuntimeError):
    def __init__(self, status: str) -> None:
        super().__init__(f"Backtest interrupted: {status}")
        self.status = status


@dataclass(frozen=True)
class FillModel:
    mode: FillMode = "realistic"
    latency_ms: int = 35
    entry_slippage_ticks: float = 1.0
    exit_slippage_ticks: float = 1.0
    stop_slippage_ticks: float = 2.0
    commission_per_side_usd: float = 0.62
    exchange_clearing_per_side_usd: float = 0.48
    limit_fill_probability: float = 0.72
    version: str = "fill-v1"


@dataclass(frozen=True)
class TradeIntent:
    id: str
    decision_timestamp_ns: int
    direction: Literal["long", "short"]
    order_type: OrderType
    entry_price: float
    stop_price: float
    targets: tuple[float, ...]
    contracts: int
    exit_level_reference: ExitLevelReference = "ABSOLUTE_MARKET_LEVEL"
    valid_until_ns: int | None = None
    time_stop_ns: int | None = None
    time_stop_duration_ns: int | None = None
    signal_timestamp_ns: int | None = None
    market_regime: str = "unknown"
    feature_snapshot: dict[str, object] = field(default_factory=dict)
    signal_score: float | None = None
    invalidation: tuple[str, ...] = ()
    state_snapshot: dict[str, object] = field(default_factory=dict)
    data_fingerprint: str = "unknown"
    strategy_version: str = "research"
    model_version: str = "rules-baseline-v1"


class SimulatedBook:
    def __init__(self) -> None:
        self.orders: dict[int, tuple[str, int, int]] = {}
        self.bids: dict[int, int] = {}
        self.asks: dict[int, int] = {}

    def _remove(self, order_id: int, size: int | None = None) -> None:
        previous = self.orders.get(order_id)
        if not previous:
            return
        side, price, current_size = previous
        removed_size = current_size if size is None else min(max(size, 0), current_size)
        remaining_size = current_size - removed_size
        levels = self.bids if side == "B" else self.asks
        levels[price] = max(0, levels.get(price, 0) - removed_size)
        if levels[price] == 0:
            levels.pop(price, None)
        if remaining_size:
            self.orders[order_id] = (side, price, remaining_size)
        else:
            self.orders.pop(order_id, None)

    def apply(self, event: NormalizedMarketEvent) -> None:
        if event.action == "R":
            self.orders.clear(); self.bids.clear(); self.asks.clear()
            return
        if event.action in {"C", "F"}:
            self._remove(event.order_id, event.size)
        elif event.action == "M":
            self._remove(event.order_id)
        if event.action in {"A", "M"} and event.side in {"B", "A"} and event.size > 0:
            self.orders[event.order_id] = (event.side, event.price_fixed, event.size)
            levels = self.bids if event.side == "B" else self.asks
            levels[event.price_fixed] = levels.get(event.price_fixed, 0) + event.size

    @property
    def best_bid(self) -> int | None:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> int | None:
        return min(self.asks) if self.asks else None

    def depth_at(self, side: str, price_fixed: int) -> int:
        return (self.bids if side == "B" else self.asks).get(price_fixed, 0)

    def market_fill(self, direction: Literal["long", "short"], contracts: int) -> tuple[float, int] | None:
        """Return a non-mutating depth-walk VWAP for an aggressive order.

        The historical book must not be changed by a hypothetical backtest order, but the
        fill still has to respect the quantity actually displayed across price levels.
        """
        if contracts < 1 or self.best_bid is None or self.best_ask is None or self.best_bid >= self.best_ask:
            return None
        levels = self.asks if direction == "long" else self.bids
        prices = sorted(levels) if direction == "long" else sorted(levels, reverse=True)
        remaining = contracts
        filled = 0
        notional_fixed = 0
        for price_fixed in prices:
            available = max(0, levels.get(price_fixed, 0))
            quantity = min(remaining, available)
            if quantity:
                notional_fixed += price_fixed * quantity
                filled += quantity
                remaining -= quantity
            if remaining == 0:
                break
        if filled == 0:
            return None
        return notional_fixed / filled / 1_000_000_000, filled


@dataclass
class _ActiveMarketPosition:
    intent: TradeIntent
    entry_timestamp_ns: int
    entry_price: float
    stop_price: float
    targets: tuple[float, ...]
    filled_contracts: int
    last_trade_price: float
    mae: float = 0.0
    mfe: float = 0.0


class EventDrivenBacktester:
    def __init__(self, *, symbol: str = "MES", fill_model: FillModel | None = None, seed: int = 7) -> None:
        self.spec = instrument_spec(symbol)
        self.fill_model = fill_model or FillModel()
        self.seed = seed

    def run(
        self,
        events: Iterable[NormalizedMarketEvent],
        intents: Iterable[TradeIntent],
        *,
        control_check: ResearchControlCheck | None = None,
        progress_callback: ResearchProgressCallback | None = None,
    ) -> dict[str, object]:
        ordered = sorted(events, key=lambda item: item.sort_key)
        intent_list = list(intents)
        trades: list[dict[str, object]] = []
        total_intents = len(intent_list)
        for index, intent in enumerate(intent_list, start=1):
            self._raise_if_interrupted(control_check)
            trade = self._simulate(ordered, intent, control_check=control_check)
            if trade is not None:
                trades.append(trade)
            if progress_callback is not None:
                progress_callback(index, total_intents)
        metrics = self._metrics(trades)
        return {
            "fillMode": self.fill_model.mode,
            "fillModelVersion": self.fill_model.version,
            "seed": self.seed,
            "trades": trades,
            "metrics": metrics,
            "futureLeakageGuard": True,
        }

    def run_market_streaming(
        self,
        events: Iterable[NormalizedMarketEvent],
        intents: Iterable[TradeIntent],
        *,
        control_check: ResearchControlCheck | None = None,
        progress_callback: ResearchProgressCallback | None = None,
    ) -> dict[str, object]:
        """Backtest aggressive entries in one chronological, memory-bounded pass.

        Unlike run(), this never stores or sorts the full event session and never rebuilds
        the L3 book once per signal. It keeps one live book, a small queue of pending
        decisions, and at most one position for this strategy. This is the production path
        for full-session research gates.
        """
        intent_list = sorted(intents, key=lambda item: (item.decision_timestamp_ns, item.id))
        if any(intent.order_type != "MARKET" for intent in intent_list):
            raise ValueError("run_market_streaming only supports aggressive MARKET intents.")
        waiting: deque[TradeIntent] = deque()
        next_intent = 0
        completed_intents = 0
        trades: list[dict[str, object]] = []
        book = SimulatedBook()
        active: _ActiveMarketPosition | None = None
        last_event: NormalizedMarketEvent | None = None
        last_sort_key: tuple[object, ...] | None = None
        events_processed = 0

        def report_completed() -> None:
            if progress_callback is not None:
                progress_callback(completed_intents, len(intent_list))

        for event in events:
            events_processed += 1
            if events_processed % 2_048 == 0:
                self._raise_if_interrupted(control_check)
            if last_sort_key is not None and event.sort_key < last_sort_key:
                raise ValueError("Streaming backtest events must be chronologically ordered.")
            last_sort_key = event.sort_key
            last_event = event
            book.apply(event)

            while next_intent < len(intent_list) and (
                intent_list[next_intent].decision_timestamp_ns
                + self.fill_model.latency_ms * 1_000_000
                <= event.timestamp_ns
            ):
                waiting.append(intent_list[next_intent])
                next_intent += 1

            if active is not None and event.action == "T":
                exit_result = self._streaming_exit(active, event)
                if exit_result is not None:
                    trades.append(exit_result)
                    active = None
                    completed_intents += 1
                    report_completed()

            while active is None and waiting:
                intent = waiting[0]
                if intent.valid_until_ns is not None and event.timestamp_ns > intent.valid_until_ns:
                    waiting.popleft()
                    completed_intents += 1
                    report_completed()
                    continue
                fill = book.market_fill(intent.direction, intent.contracts)
                if fill is None:
                    break
                waiting.popleft()
                raw_entry_price, filled_contracts = fill
                direction = 1 if intent.direction == "long" else -1
                entry_slippage = self.fill_model.entry_slippage_ticks * self.spec.tick_size
                if self.fill_model.mode == "stressed":
                    entry_slippage *= 1.5
                entry_price = raw_entry_price + direction * entry_slippage
                stop_price, targets = self._resolved_exit_levels(intent, entry_price)
                active = _ActiveMarketPosition(
                    intent=intent,
                    entry_timestamp_ns=event.timestamp_ns,
                    entry_price=entry_price,
                    stop_price=stop_price,
                    targets=targets,
                    filled_contracts=filled_contracts,
                    last_trade_price=entry_price,
                )

        if active is not None and last_event is not None:
            trades.append(self._streaming_end_of_data(active, last_event.timestamp_ns))
            completed_intents += 1
            report_completed()

        completed_intents += len(waiting) + (len(intent_list) - next_intent)
        if progress_callback is not None and completed_intents:
            progress_callback(min(completed_intents, len(intent_list)), len(intent_list))
        return {
            "fillMode": self.fill_model.mode,
            "fillModelVersion": self.fill_model.version,
            "seed": self.seed,
            "trades": trades,
            "metrics": self._metrics(trades),
            "futureLeakageGuard": True,
            "streaming": True,
            "eventsProcessed": events_processed,
            "eventBufferSize": 1,
        }

    def run_market_streaming_groups(
        self,
        events: Iterable[NormalizedMarketEvent],
        intent_groups: dict[str, Iterable[TradeIntent]],
        *,
        control_check: ResearchControlCheck | None = None,
        progress_callback: ResearchProgressCallback | None = None,
    ) -> dict[str, object]:
        """Backtest independent aggressive strategies in one bounded event pass.

        Every group has its own pending-intent queue and position, while all groups
        observe the same immutable historical L3 book. This prevents cross-strategy
        position blocking without decoding an eight-million-event session once per
        candidate.
        """
        grouped = {
            str(group): sorted(intents, key=lambda item: (item.decision_timestamp_ns, item.id))
            for group, intents in sorted(intent_groups.items())
        }
        if any(
            intent.order_type != "MARKET"
            for intents in grouped.values()
            for intent in intents
        ):
            raise ValueError("run_market_streaming_groups only supports aggressive MARKET intents.")

        total_intents = sum(len(intents) for intents in grouped.values())
        ready: list[tuple[int, str, str, TradeIntent]] = []
        for group, intents in grouped.items():
            for intent in intents:
                heappush(
                    ready,
                    (
                        intent.decision_timestamp_ns + self.fill_model.latency_ms * 1_000_000,
                        group,
                        intent.id,
                        intent,
                    ),
                )

        waiting = {group: deque() for group in grouped}
        waiting_groups: set[str] = set()
        active: dict[str, _ActiveMarketPosition] = {}
        trades = {group: [] for group in grouped}
        completed_intents = 0
        book = SimulatedBook()
        last_event: NormalizedMarketEvent | None = None
        last_sort_key: tuple[object, ...] | None = None
        events_processed = 0

        def report_completed() -> None:
            if progress_callback is not None:
                progress_callback(completed_intents, total_intents)

        for event in events:
            events_processed += 1
            if events_processed % 2_048 == 0:
                self._raise_if_interrupted(control_check)
            if last_sort_key is not None and event.sort_key < last_sort_key:
                raise ValueError("Streaming backtest events must be chronologically ordered.")
            last_sort_key = event.sort_key
            last_event = event
            book.apply(event)

            while ready and ready[0][0] <= event.timestamp_ns:
                _, group, _, intent = heappop(ready)
                waiting[group].append(intent)
                if group not in active:
                    waiting_groups.add(group)

            if event.action == "T":
                for group, position in tuple(active.items()):
                    exit_result = self._streaming_exit(position, event)
                    if exit_result is None:
                        continue
                    trades[group].append(exit_result)
                    del active[group]
                    completed_intents += 1
                    if waiting[group]:
                        waiting_groups.add(group)
                    report_completed()

            for group in tuple(sorted(waiting_groups)):
                if group in active:
                    waiting_groups.discard(group)
                    continue
                queue = waiting[group]
                while queue:
                    intent = queue[0]
                    if intent.valid_until_ns is not None and event.timestamp_ns > intent.valid_until_ns:
                        queue.popleft()
                        completed_intents += 1
                        report_completed()
                        continue
                    fill = book.market_fill(intent.direction, intent.contracts)
                    if fill is None:
                        break
                    queue.popleft()
                    raw_entry_price, filled_contracts = fill
                    direction = 1 if intent.direction == "long" else -1
                    entry_slippage = self.fill_model.entry_slippage_ticks * self.spec.tick_size
                    if self.fill_model.mode == "stressed":
                        entry_slippage *= 1.5
                    entry_price = raw_entry_price + direction * entry_slippage
                    stop_price, targets = self._resolved_exit_levels(intent, entry_price)
                    active[group] = _ActiveMarketPosition(
                        intent=intent,
                        entry_timestamp_ns=event.timestamp_ns,
                        entry_price=entry_price,
                        stop_price=stop_price,
                        targets=targets,
                        filled_contracts=filled_contracts,
                        last_trade_price=entry_price,
                    )
                    break
                if not queue or group in active:
                    waiting_groups.discard(group)

        if last_event is not None:
            for group, position in tuple(active.items()):
                trades[group].append(
                    self._streaming_end_of_data(position, last_event.timestamp_ns)
                )
                completed_intents += 1
                report_completed()

        completed_intents += sum(len(queue) for queue in waiting.values()) + len(ready)
        if progress_callback is not None and total_intents:
            progress_callback(min(completed_intents, total_intents), total_intents)
        return {
            "fillMode": self.fill_model.mode,
            "fillModelVersion": self.fill_model.version,
            "seed": self.seed,
            "groups": {
                group: {
                    "trades": rows,
                    "metrics": self._metrics(rows),
                }
                for group, rows in trades.items()
            },
            "futureLeakageGuard": True,
            "streaming": True,
            "eventsProcessed": events_processed,
            "eventBufferSize": 1,
        }

    @staticmethod
    def _raise_if_interrupted(control_check: ResearchControlCheck | None) -> None:
        if control_check is None:
            return
        status = control_check()
        if status:
            raise BacktestInterrupted(status)

    def _simulate(
        self,
        events: list[NormalizedMarketEvent],
        intent: TradeIntent,
        *,
        control_check: ResearchControlCheck | None = None,
    ) -> dict[str, object] | None:
        if intent.contracts < 1 or not intent.targets or intent.entry_price == intent.stop_price:
            return None
        book = SimulatedBook()
        earliest = intent.decision_timestamp_ns + self.fill_model.latency_ms * 1_000_000
        entry_fixed = round(intent.entry_price * 1_000_000_000)
        queue_ahead: float | None = None
        entry_event_index: int | None = None
        entry_price: float | None = None
        filled_contracts = 0
        rng = random.Random(f"{self.seed}:{intent.id}")

        for index, event in enumerate(events):
            if index % 2_048 == 0:
                self._raise_if_interrupted(control_check)
            book.apply(event)
            if event.timestamp_ns < earliest:
                continue
            if intent.valid_until_ns is not None and event.timestamp_ns > intent.valid_until_ns:
                break
            if intent.order_type == "MARKET":
                fill = book.market_fill(intent.direction, intent.contracts)
                if fill is None:
                    continue
                entry_price, filled_contracts = fill
            elif intent.order_type == "STOP":
                if event.action != "T":
                    continue
                crossed = event.price_fixed >= entry_fixed if intent.direction == "long" else event.price_fixed <= entry_fixed
                if not crossed:
                    continue
                entry_price = event.price_fixed / 1_000_000_000
                filled_contracts = min(intent.contracts, max(1, event.size))
            else:
                resting_side = "B" if intent.direction == "long" else "A"
                if queue_ahead is None:
                    queue_ahead = float(book.depth_at(resting_side, entry_fixed))
                    if self.fill_model.mode == "stressed":
                        queue_ahead *= 1.25
                if event.action != "T":
                    continue
                marketable = event.price_fixed <= entry_fixed and event.side == "A" if intent.direction == "long" else event.price_fixed >= entry_fixed and event.side == "B"
                if not marketable:
                    continue
                if self.fill_model.mode == "optimistic":
                    queue_ahead = 0
                else:
                    queue_ahead = max(0.0, queue_ahead - event.size)
                if queue_ahead > 0 or (self.fill_model.mode != "optimistic" and rng.random() > self.fill_model.limit_fill_probability):
                    continue
                filled_contracts = min(intent.contracts, max(1, event.size))
                entry_price = intent.entry_price
            if entry_price is not None:
                entry_event_index = index
                break

        if entry_event_index is None or entry_price is None or filled_contracts < 1:
            return None

        direction = 1 if intent.direction == "long" else -1
        entry_slippage = 0.0 if intent.order_type == "LIMIT" else self.fill_model.entry_slippage_ticks * self.spec.tick_size
        if self.fill_model.mode == "stressed":
            entry_slippage *= 1.5
        entry_price += direction * entry_slippage
        stop_price, resolved_targets = self._resolved_exit_levels(intent, entry_price)
        stop_fixed = round(stop_price * 1_000_000_000)
        target_fixed = [round(target * 1_000_000_000) for target in resolved_targets]
        exit_price: float | None = None
        exit_reason = "NO_EXIT"
        exit_timestamp_ns = events[entry_event_index].timestamp_ns
        mae = 0.0
        mfe = 0.0
        last_trade_price = entry_price

        for exit_index, event in enumerate(events[entry_event_index + 1:], start=entry_event_index + 1):
            if exit_index % 2_048 == 0:
                self._raise_if_interrupted(control_check)
            if event.action != "T":
                continue
            price = event.price_fixed / 1_000_000_000
            last_trade_price = price
            excursion = direction * (price - entry_price)
            mfe = max(mfe, excursion)
            mae = max(mae, -excursion)
            stop_hit = event.price_fixed <= stop_fixed if intent.direction == "long" else event.price_fixed >= stop_fixed
            target_hit = any(event.price_fixed >= target if intent.direction == "long" else event.price_fixed <= target for target in target_fixed)
            resolved_time_stop_ns = self._resolved_time_stop_ns(intent, events[entry_event_index].timestamp_ns)
            timed_out = resolved_time_stop_ns is not None and event.timestamp_ns >= resolved_time_stop_ns
            if stop_hit:
                slip = self.fill_model.stop_slippage_ticks * self.spec.tick_size
                if self.fill_model.mode == "stressed":
                    slip *= 1.5
                exit_price = price - direction * slip
                exit_reason = "STOP"
            elif target_hit:
                slip = 0.0 if self.fill_model.mode == "optimistic" else self.fill_model.exit_slippage_ticks * self.spec.tick_size
                exit_price = price - direction * slip
                exit_reason = "TARGET"
            elif timed_out:
                exit_price = price - direction * self.fill_model.exit_slippage_ticks * self.spec.tick_size
                exit_reason = "TIME_STOP"
            if exit_price is not None:
                exit_timestamp_ns = event.timestamp_ns
                break

        if exit_price is None:
            exit_price = last_trade_price - direction * self.fill_model.exit_slippage_ticks * self.spec.tick_size
            exit_reason = "END_OF_DATA"
            exit_timestamp_ns = events[-1].timestamp_ns

        gross = direction * (exit_price - entry_price) * self.spec.point_value_usd * filled_contracts
        fees = 2 * (self.fill_model.commission_per_side_usd + self.fill_model.exchange_clearing_per_side_usd) * filled_contracts
        net = gross - fees
        initial_risk = abs(entry_price - stop_price) * self.spec.point_value_usd * filled_contracts + fees
        slippage_ticks = 0.0 if intent.order_type == "LIMIT" else self.fill_model.entry_slippage_ticks
        slippage_ticks += self.fill_model.stop_slippage_ticks if exit_reason == "STOP" else self.fill_model.exit_slippage_ticks
        if self.fill_model.mode == "stressed":
            slippage_ticks *= 1.5
        slippage_usd = slippage_ticks * self.spec.tick_value_usd * filled_contracts
        return {
            "intentId": intent.id,
            "signalTimestampNs": intent.signal_timestamp_ns or intent.decision_timestamp_ns,
            "decisionTimestampNs": intent.decision_timestamp_ns,
            "entryTimestampNs": events[entry_event_index].timestamp_ns,
            "exitTimestampNs": exit_timestamp_ns,
            "direction": intent.direction,
            "orderType": intent.order_type,
            "requestedContracts": intent.contracts,
            "filledContracts": filled_contracts,
            "partialFill": filled_contracts < intent.contracts,
            "entryOrder": {
                "type": intent.order_type, "requestedPrice": intent.entry_price,
                "validUntilNs": intent.valid_until_ns, "latencyMs": self.fill_model.latency_ms,
            },
            "fillDetails": {
                "mode": self.fill_model.mode, "queueAware": intent.order_type == "LIMIT",
                "requestedContracts": intent.contracts, "filledContracts": filled_contracts,
                "partialFill": filled_contracts < intent.contracts,
            },
            "entryPrice": round(entry_price, 6),
            "exitPrice": round(exit_price, 6),
            "stopPrice": round(stop_price, 6),
            "targets": [round(target, 6) for target in resolved_targets],
            "requestedStopPrice": intent.stop_price,
            "requestedTargets": list(intent.targets),
            "exitLevelReference": intent.exit_level_reference,
            "grossUsd": round(gross, 2),
            "feesUsd": round(fees, 2),
            "slippageUsd": round(slippage_usd, 2),
            "netUsd": round(net, 2),
            "resultR": round(net / initial_risk, 4) if initial_risk else 0,
            "maePoints": round(mae, 4),
            "mfePoints": round(mfe, 4),
            "holdingNanoseconds": max(0, exit_timestamp_ns - events[entry_event_index].timestamp_ns),
            "exitReason": exit_reason,
            "fillMode": self.fill_model.mode,
            "marketRegime": intent.market_regime,
            "featureSnapshot": intent.feature_snapshot,
            "signalScore": intent.signal_score,
            "invalidation": list(intent.invalidation),
            "stateSnapshot": intent.state_snapshot,
            "dataFingerprint": intent.data_fingerprint,
            "strategyVersion": intent.strategy_version,
            "modelVersion": intent.model_version,
        }

    @staticmethod
    def _resolved_exit_levels(intent: TradeIntent, actual_entry_price: float) -> tuple[float, tuple[float, ...]]:
        if intent.exit_level_reference == "ENTRY_RELATIVE":
            stop_distance = intent.stop_price - intent.entry_price
            target_distances = tuple(target - intent.entry_price for target in intent.targets)
            return (
                actual_entry_price + stop_distance,
                tuple(actual_entry_price + distance for distance in target_distances),
            )
        return intent.stop_price, intent.targets

    @staticmethod
    def _resolved_time_stop_ns(intent: TradeIntent, actual_entry_timestamp_ns: int) -> int | None:
        if intent.time_stop_duration_ns is not None:
            return actual_entry_timestamp_ns + intent.time_stop_duration_ns
        return intent.time_stop_ns

    def _streaming_exit(
        self,
        position: _ActiveMarketPosition,
        event: NormalizedMarketEvent,
    ) -> dict[str, object] | None:
        intent = position.intent
        direction = 1 if intent.direction == "long" else -1
        price = event.price_fixed / 1_000_000_000
        position.last_trade_price = price
        excursion = direction * (price - position.entry_price)
        position.mfe = max(position.mfe, excursion)
        position.mae = max(position.mae, -excursion)
        stop_hit = price <= position.stop_price if intent.direction == "long" else price >= position.stop_price
        target_hit = any(
            price >= target if intent.direction == "long" else price <= target
            for target in position.targets
        )
        time_stop_ns = self._resolved_time_stop_ns(intent, position.entry_timestamp_ns)
        timed_out = time_stop_ns is not None and event.timestamp_ns >= time_stop_ns
        if not (stop_hit or target_hit or timed_out):
            return None
        if stop_hit:
            slip_ticks = self.fill_model.stop_slippage_ticks
            if self.fill_model.mode == "stressed":
                slip_ticks *= 1.5
            exit_price = price - direction * slip_ticks * self.spec.tick_size
            exit_reason = "STOP"
        elif target_hit:
            slip_ticks = 0.0 if self.fill_model.mode == "optimistic" else self.fill_model.exit_slippage_ticks
            exit_price = price - direction * slip_ticks * self.spec.tick_size
            exit_reason = "TARGET"
        else:
            slip_ticks = self.fill_model.exit_slippage_ticks
            exit_price = price - direction * slip_ticks * self.spec.tick_size
            exit_reason = "TIME_STOP"
        return self._streaming_trade_result(
            position, exit_timestamp_ns=event.timestamp_ns,
            exit_price=exit_price, exit_reason=exit_reason,
        )

    def _streaming_end_of_data(
        self,
        position: _ActiveMarketPosition,
        exit_timestamp_ns: int,
    ) -> dict[str, object]:
        direction = 1 if position.intent.direction == "long" else -1
        exit_price = (
            position.last_trade_price
            - direction * self.fill_model.exit_slippage_ticks * self.spec.tick_size
        )
        return self._streaming_trade_result(
            position, exit_timestamp_ns=exit_timestamp_ns,
            exit_price=exit_price, exit_reason="END_OF_DATA",
        )

    def _streaming_trade_result(
        self,
        position: _ActiveMarketPosition,
        *,
        exit_timestamp_ns: int,
        exit_price: float,
        exit_reason: str,
    ) -> dict[str, object]:
        intent = position.intent
        direction = 1 if intent.direction == "long" else -1
        contracts = position.filled_contracts
        gross = direction * (exit_price - position.entry_price) * self.spec.point_value_usd * contracts
        fees = 2 * (
            self.fill_model.commission_per_side_usd
            + self.fill_model.exchange_clearing_per_side_usd
        ) * contracts
        net = gross - fees
        initial_risk = abs(position.entry_price - position.stop_price) * self.spec.point_value_usd * contracts + fees
        slippage_ticks = self.fill_model.entry_slippage_ticks
        slippage_ticks += (
            self.fill_model.stop_slippage_ticks
            if exit_reason == "STOP"
            else self.fill_model.exit_slippage_ticks
        )
        if self.fill_model.mode == "stressed":
            slippage_ticks *= 1.5
        return {
            "intentId": intent.id,
            "signalTimestampNs": intent.signal_timestamp_ns or intent.decision_timestamp_ns,
            "decisionTimestampNs": intent.decision_timestamp_ns,
            "entryTimestampNs": position.entry_timestamp_ns,
            "exitTimestampNs": exit_timestamp_ns,
            "direction": intent.direction,
            "orderType": intent.order_type,
            "requestedContracts": intent.contracts,
            "filledContracts": contracts,
            "partialFill": contracts < intent.contracts,
            "entryOrder": {
                "type": intent.order_type,
                "requestedPrice": intent.entry_price,
                "validUntilNs": intent.valid_until_ns,
                "latencyMs": self.fill_model.latency_ms,
            },
            "fillDetails": {
                "mode": self.fill_model.mode,
                "queueAware": False,
                "depthWalked": True,
                "requestedContracts": intent.contracts,
                "filledContracts": contracts,
                "partialFill": contracts < intent.contracts,
            },
            "entryPrice": round(position.entry_price, 6),
            "exitPrice": round(exit_price, 6),
            "stopPrice": round(position.stop_price, 6),
            "targets": [round(target, 6) for target in position.targets],
            "requestedStopPrice": intent.stop_price,
            "requestedTargets": list(intent.targets),
            "exitLevelReference": intent.exit_level_reference,
            "grossUsd": round(gross, 2),
            "feesUsd": round(fees, 2),
            "slippageUsd": round(slippage_ticks * self.spec.tick_value_usd * contracts, 2),
            "netUsd": round(net, 2),
            "resultR": round(net / initial_risk, 4) if initial_risk else 0,
            "maePoints": round(position.mae, 4),
            "mfePoints": round(position.mfe, 4),
            "holdingNanoseconds": max(0, exit_timestamp_ns - position.entry_timestamp_ns),
            "exitReason": exit_reason,
            "fillMode": self.fill_model.mode,
            "marketRegime": intent.market_regime,
            "featureSnapshot": intent.feature_snapshot,
            "signalScore": intent.signal_score,
            "invalidation": list(intent.invalidation),
            "stateSnapshot": intent.state_snapshot,
            "dataFingerprint": intent.data_fingerprint,
            "strategyVersion": intent.strategy_version,
            "modelVersion": intent.model_version,
        }

    @staticmethod
    def _metrics(trades: list[dict[str, object]]) -> dict[str, float | int | None]:
        results = [float(trade["netUsd"]) for trade in trades]
        r_values = [float(trade["resultR"]) for trade in trades]
        wins = [value for value in results if value > 0]
        losses = [value for value in results if value < 0]
        equity = peak = drawdown = 0.0
        for value in results:
            equity += value
            peak = max(peak, equity)
            drawdown = max(drawdown, peak - equity)
        return {
            "trades": len(trades),
            "netResultUsd": round(sum(results), 2),
            "netExpectancyUsd": round(mean(results), 3) if results else 0,
            "expectancyR": round(mean(r_values), 4) if r_values else 0,
            "profitFactor": round(sum(wins) / abs(sum(losses)), 4) if losses else None,
            "maximumDrawdownUsd": round(drawdown, 2),
            "partialFills": sum(1 for trade in trades if trade["partialFill"]),
        }
