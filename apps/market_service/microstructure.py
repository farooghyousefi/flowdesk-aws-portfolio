from __future__ import annotations

from collections import Counter, deque
from math import sqrt
from statistics import mean, median
from typing import Any

from apps.connectors.databento.src.dbn_reader import F_LAST, F_SNAPSHOT, MboEvent, Order, OrderBook

from .contracts import display_price


TICK_FIXED = 250_000_000


class MicrostructureFeatures:
    """Bounded, incremental market-microstructure observations for replay and research."""

    def __init__(self, *, window_seconds: int = 5, large_trade_size: int = 10) -> None:
        self.window_ns = max(1, window_seconds) * 1_000_000_000
        self.large_trade_size = max(1, large_trade_size)
        self.actions: deque[tuple[int, str, str, int, int]] = deque(maxlen=100_000)
        self.trades: deque[tuple[int, str, int, int]] = deque(maxlen=50_000)
        self.order_births: dict[int, int] = {}
        self.order_lifetimes_ms: deque[float] = deque(maxlen=10_000)
        self.best_history: deque[tuple[int, int | None, int | None, int, int]] = deque(maxlen=10_000)
        self.wall_first_seen: dict[tuple[str, int], int] = {}
        self.last_ts = 0

    def observe(self, event: MboEvent, *, book: OrderBook, before_order: Order | None = None) -> None:
        self.last_ts = event.ts_event
        if not event.flags & F_SNAPSHOT:
            self.actions.append((event.ts_event, event.action, event.side, event.price, event.size))
        if event.action == "A" and event.order_id:
            self.order_births[event.order_id] = event.ts_event
        elif event.action in {"C", "F", "M"} and event.order_id:
            born = self.order_births.pop(event.order_id, None)
            if born is not None and event.ts_event >= born:
                self.order_lifetimes_ms.append((event.ts_event - born) / 1_000_000)
            if event.action == "M" and event.size > 0:
                self.order_births[event.order_id] = event.ts_event
        if event.action == "T" and event.price > 0 and event.size > 0:
            self.trades.append((event.ts_event, event.side, event.price, event.size))
        if event.flags & F_LAST:
            snapshot = book.snapshot(10)
            bid = snapshot.best_bid.price if snapshot.best_bid else None
            ask = snapshot.best_ask.price if snapshot.best_ask else None
            bid_size = snapshot.best_bid.total_size if snapshot.best_bid else 0
            ask_size = snapshot.best_ask.total_size if snapshot.best_ask else 0
            self.best_history.append((event.ts_event, bid, ask, bid_size, ask_size))
            for side, levels in (("B", snapshot.bids), ("A", snapshot.asks)):
                if not levels:
                    continue
                wall = max(levels, key=lambda level: (level.total_size, -abs(level.price - levels[0].price)))
                self.wall_first_seen.setdefault((side, wall.price), event.ts_event)
        self._prune(event.ts_event)

    def _prune(self, timestamp_ns: int) -> None:
        cutoff = timestamp_ns - self.window_ns
        while self.actions and self.actions[0][0] < cutoff:
            self.actions.popleft()
        while self.trades and self.trades[0][0] < cutoff:
            self.trades.popleft()
        while self.best_history and self.best_history[0][0] < cutoff:
            self.best_history.popleft()
        wall_cutoff = timestamp_ns - 60_000_000_000
        self.wall_first_seen = {key: value for key, value in self.wall_first_seen.items() if value >= wall_cutoff}

    @staticmethod
    def _imbalance(bid_size: int, ask_size: int) -> float:
        total = bid_size + ask_size
        return round((bid_size - ask_size) / total, 4) if total else 0.0

    @staticmethod
    def _volatility(prices: list[int]) -> float:
        if len(prices) < 2:
            return 0.0
        changes = [(right - left) / TICK_FIXED for left, right in zip(prices, prices[1:])]
        average = mean(changes)
        return round(sqrt(sum((value - average) ** 2 for value in changes) / len(changes)), 4)

    def _sweep(self) -> dict[str, Any]:
        if len(self.trades) < 2:
            return {"detected": False, "side": None, "levels": 0, "volume": 0, "durationMs": 0}
        latest = self.trades[-1][0]
        candidates = [trade for trade in self.trades if trade[0] >= latest - 100_000_000]
        grouped: dict[str, list[tuple[int, str, int, int]]] = {"B": [], "A": []}
        for trade in candidates:
            grouped.setdefault(trade[1], []).append(trade)
        side, sequence = max(grouped.items(), key=lambda item: sum(trade[3] for trade in item[1]))
        prices = {trade[2] for trade in sequence}
        levels = round((max(prices) - min(prices)) / TICK_FIXED) + 1 if prices else 0
        return {
            "detected": levels >= 2 and len(sequence) >= 2,
            "side": "buy" if side == "B" else "sell" if side == "A" else None,
            "levels": levels,
            "volume": sum(trade[3] for trade in sequence),
            "durationMs": round((sequence[-1][0] - sequence[0][0]) / 1_000_000, 3) if len(sequence) > 1 else 0,
        }

    def contract(self, book: OrderBook) -> dict[str, Any]:
        snapshot = book.snapshot(10)
        bid = snapshot.best_bid
        ask = snapshot.best_ask
        mid_fixed = (bid.price + ask.price) / 2 if bid and ask else None
        microprice_fixed = None
        if bid and ask and bid.total_size + ask.total_size:
            microprice_fixed = (ask.price * bid.total_size + bid.price * ask.total_size) / (bid.total_size + ask.total_size)
        depth: dict[str, float] = {}
        for level_count in (1, 3, 5, 10):
            bid_depth = sum(level.total_size for level in snapshot.bids[:level_count])
            ask_depth = sum(level.total_size for level in snapshot.asks[:level_count])
            depth[str(level_count)] = self._imbalance(bid_depth, ask_depth)
        bid_total = sum(level.total_size for level in snapshot.bids)
        ask_total = sum(level.total_size for level in snapshot.asks)
        total_depth = bid_total + ask_total
        front_depth = sum(level.total_size for level in snapshot.bids[:3]) + sum(level.total_size for level in snapshot.asks[:3])
        back_levels = [*snapshot.bids[3:], *snapshot.asks[3:]]
        front_levels = [*snapshot.bids[:3], *snapshot.asks[:3]]
        near_average = mean(level.total_size for level in front_levels) if front_levels else 0
        far_average = mean(level.total_size for level in back_levels) if back_levels else 0
        book_slope = (near_average - far_average) / max(near_average + far_average, 1)
        largest_bid = max(snapshot.bids, key=lambda level: level.total_size, default=None)
        largest_ask = max(snapshot.asks, key=lambda level: level.total_size, default=None)
        now = self.last_ts
        wall_persistence = {
            "bidMs": round((now - self.wall_first_seen.get(("B", largest_bid.price), now)) / 1_000_000) if largest_bid else 0,
            "askMs": round((now - self.wall_first_seen.get(("A", largest_ask.price), now)) / 1_000_000) if largest_ask else 0,
        }
        counts = Counter(action for _, action, _, _, _ in self.actions)
        elapsed = max(0.001, min(self.window_ns, max(0, now - self.actions[0][0] if self.actions else 0)) / 1_000_000_000)
        buy_volume = sum(size for _, side, _, size in self.trades if side == "B")
        sell_volume = sum(size for _, side, _, size in self.trades if side == "A")
        volume = buy_volume + sell_volume
        trade_count = len(self.trades)
        split = now - self.window_ns // 2
        early_delta = sum(size if side == "B" else -size for ts, side, _, size in self.trades if ts < split)
        late_delta = sum(size if side == "B" else -size for ts, side, _, size in self.trades if ts >= split)
        trade_prices = [price for _, _, price, _ in self.trades]
        best_changes = 0
        for previous, current in zip(self.best_history, list(self.best_history)[1:]):
            if previous[1:3] != current[1:3]:
                best_changes += 1
        current_mid = mid_fixed
        first_mid = None
        if self.best_history and self.best_history[0][1] is not None and self.best_history[0][2] is not None:
            first_mid = (self.best_history[0][1] + self.best_history[0][2]) / 2
        add_count = counts["A"]
        cancel_count = counts["C"]
        modify_count = counts["M"]
        sweep = self._sweep()
        top_liquidity = min(bid.total_size if bid else 0, ask.total_size if ask else 0)
        return {
            "orderBook": {
                "bestBid": display_price(bid.price) if bid else None,
                "bestAsk": display_price(ask.price) if ask else None,
                "spreadTicks": round(snapshot.spread / TICK_FIXED, 4) if snapshot.spread is not None else None,
                "midprice": display_price(round(mid_fixed)) if mid_fixed is not None else None,
                "microprice": display_price(round(microprice_fixed)) if microprice_fixed is not None else None,
                "queueImbalance": self._imbalance(bid.total_size if bid else 0, ask.total_size if ask else 0),
                "depthImbalance": depth,
                "bookSlope": round(book_slope, 4),
                "depthConcentration": round((front_depth / total_depth) if total_depth else 0, 4),
                "wallPersistence": wall_persistence,
                "bidWallDistanceTicks": round((mid_fixed - largest_bid.price) / TICK_FIXED, 2) if mid_fixed is not None and largest_bid else None,
                "askWallDistanceTicks": round((largest_ask.price - mid_fixed) / TICK_FIXED, 2) if mid_fixed is not None and largest_ask else None,
                "bestLevelChurnPerSecond": round(best_changes / elapsed, 3),
                "liquidityMigrationTicks": round((current_mid - first_mid) / TICK_FIXED, 3) if current_mid is not None and first_mid is not None else 0,
                "topOfBookLiquidityContracts": top_liquidity,
            },
            "orderActivity": {
                "windowSeconds": round(elapsed, 3),
                "addRate": round(add_count / elapsed, 3),
                "cancelRate": round(cancel_count / elapsed, 3),
                "modifyRate": round(modify_count / elapsed, 3),
                "cancelToTradeRatio": round(cancel_count / max(trade_count, 1), 3),
                "addToTradeRatio": round(add_count / max(trade_count, 1), 3),
                "medianOrderLifetimeMs": round(median(self.order_lifetimes_ms), 3) if self.order_lifetimes_ms else None,
                "queueDepletionSize": sum(size for _, action, _, _, size in self.actions if action in {"C", "F"}),
                "queueReplenishmentSize": sum(size for _, action, _, _, size in self.actions if action == "A"),
            },
            "tradeAggression": {
                "aggressiveBuyVolume": buy_volume,
                "aggressiveSellVolume": sell_volume,
                "signedVolume": buy_volume - sell_volume,
                "buySellImbalance": round((buy_volume - sell_volume) / volume, 4) if volume else 0,
                "tradesPerSecond": round(trade_count / elapsed, 3),
                "volumePerSecond": round(volume / elapsed, 3),
                "averageTradeSize": round(volume / max(trade_count, 1), 3),
                "largeTradeClusters": sum(1 for _, _, _, size in self.trades if size >= self.large_trade_size),
                "deltaMomentum": late_delta - early_delta,
                "shortTermVolatilityTicks": self._volatility(trade_prices),
                "sweep": sweep,
                "tradeBurst": trade_count >= max(5, round(elapsed * 8)),
                "exhaustionCandidate": bool(volume and sweep["detected"] and abs(late_delta) < max(1, abs(early_delta) * 0.25)),
            },
            "dataTimestampNs": str(now),
            "heuristicOnly": True,
        }
