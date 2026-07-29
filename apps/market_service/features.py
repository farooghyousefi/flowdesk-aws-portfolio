from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, time
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

from apps.connectors.databento.src.dbn_reader import F_SNAPSHOT, MboEvent
from .contracts import display_price

TIMEFRAMES_NS = {"1m": 60_000_000_000, "5m": 300_000_000_000, "15m": 900_000_000_000}


@dataclass
class MutableBar:
    timeframe: str
    start_ns: int
    end_ns: int
    open_fixed: int
    high_fixed: int
    low_fixed: int
    close_fixed: int
    volume: int = 0
    buy_volume: int = 0
    sell_volume: int = 0
    trade_count: int = 0
    price_size_sum: int = 0

    def observe(self, event: MboEvent) -> None:
        self.high_fixed = max(self.high_fixed, event.price)
        self.low_fixed = min(self.low_fixed, event.price)
        self.close_fixed = event.price
        self.volume += event.size
        self.trade_count += 1
        self.price_size_sum += event.price * event.size
        if event.side == "B":
            self.buy_volume += event.size
        elif event.side == "A":
            self.sell_volume += event.size

    def contract(self, cumulative_delta: int, *, completed: bool) -> dict[str, Any]:
        vwap_fixed = self.price_size_sum // self.volume if self.volume else self.close_fixed
        return {
            "timeframe": self.timeframe,
            "startNs": str(self.start_ns),
            "endNs": str(self.end_ns),
            "openFixed": str(self.open_fixed),
            "highFixed": str(self.high_fixed),
            "lowFixed": str(self.low_fixed),
            "closeFixed": str(self.close_fixed),
            "open": display_price(self.open_fixed),
            "high": display_price(self.high_fixed),
            "low": display_price(self.low_fixed),
            "close": display_price(self.close_fixed),
            "volume": self.volume,
            "buyVolume": self.buy_volume,
            "sellVolume": self.sell_volume,
            "delta": self.buy_volume - self.sell_volume,
            "cumulativeDelta": cumulative_delta,
            "tradeCount": self.trade_count,
            "vwap": display_price(vwap_fixed),
            "completed": completed,
        }


class OrderflowFeatures:
    """Deterministic features. Only action T contributes trade volume; action F never does."""

    def __init__(
        self,
        *,
        large_trade_threshold: int = 10,
        imbalance_ratio: float = 3.0,
        absorption_window_seconds: int = 3,
        absorption_minimum_observations: int = 3,
        absorption_minimum_elapsed_ms: int = 500,
        absorption_minimum_aggressive_volume: int = 20,
        absorption_candidate_limit: int = 5,
        replenishment_threshold: int = 3,
    ) -> None:
        self.large_trade_threshold = large_trade_threshold
        self.imbalance_ratio = imbalance_ratio
        self.absorption_window_seconds = max(1, absorption_window_seconds)
        self.absorption_minimum_observations = max(2, absorption_minimum_observations)
        self.absorption_minimum_elapsed_ms = max(1, absorption_minimum_elapsed_ms)
        self.absorption_minimum_aggressive_volume = max(1, absorption_minimum_aggressive_volume)
        self.absorption_candidate_limit = max(1, absorption_candidate_limit)
        self.replenishment_threshold = max(1, replenishment_threshold)
        self.buy_volume = 0
        self.sell_volume = 0
        self.trade_count = 0
        self.price_size_sum = 0
        self.last_trade_price: int | None = None
        self.first_trade_price: int | None = None
        self.last_ts = 0
        self.first_ts = 0
        self.bars: dict[tuple[str, int], MutableBar] = {}
        self.footprint: dict[tuple[int, int], list[int]] = defaultdict(lambda: [0, 0])
        self.profile: dict[int, int] = defaultdict(int)
        self.tape: deque[dict[str, Any]] = deque(maxlen=100)
        self.flow_events: deque[tuple[int, str, int, int]] = deque(maxlen=20_000)
        self.trade_windows: dict[int, deque[tuple[int, str, int]]] = defaultdict(lambda: deque(maxlen=200))
        self.replenishments: dict[int, deque[int]] = defaultdict(lambda: deque(maxlen=200))

    @property
    def cumulative_delta(self) -> int:
        return self.buy_volume - self.sell_volume

    def observe(self, event: MboEvent, *, before_order: Any | None = None) -> None:
        self.last_ts = event.ts_event
        self.first_ts = self.first_ts or event.ts_event
        if event.action == "T" and event.price > 0 and event.size > 0:
            self._observe_trade(event)
            return
        if event.flags & F_SNAPSHOT:
            return
        if event.action == "A" and event.price > 0:
            self.flow_events.append((event.ts_event, "stack", event.price, event.size))
            self.replenishments[event.price].append(event.ts_event)
        elif event.action == "C" and before_order is not None:
            self.flow_events.append((event.ts_event, "pull", before_order.price, min(before_order.size, event.size)))
        elif event.action == "M" and before_order is not None:
            if event.price != before_order.price:
                self.flow_events.append((event.ts_event, "pull", before_order.price, before_order.size))
                self.flow_events.append((event.ts_event, "stack", event.price, event.size))
            elif event.size > before_order.size:
                self.flow_events.append((event.ts_event, "stack", event.price, event.size - before_order.size))
            elif event.size < before_order.size:
                self.flow_events.append((event.ts_event, "pull", event.price, before_order.size - event.size))
        elif event.action == "F" and event.price > 0:
            self.flow_events.append((event.ts_event, "execute", event.price, event.size))

    def _observe_trade(self, event: MboEvent) -> None:
        self.trade_count += 1
        self.price_size_sum += event.price * event.size
        self.profile[event.price] += event.size
        self.last_trade_price = event.price
        self.first_trade_price = self.first_trade_price or event.price
        if event.side == "B":
            self.buy_volume += event.size
            self.footprint[(event.ts_event // TIMEFRAMES_NS["1m"], event.price)][1] += event.size
            side = "buy"
        else:
            self.sell_volume += event.size
            self.footprint[(event.ts_event // TIMEFRAMES_NS["1m"], event.price)][0] += event.size
            side = "sell"
        self.trade_windows[event.price].append((event.ts_event, side, event.size))
        self.tape.appendleft({
            "tsEventNs": str(event.ts_event), "timestamp": event.timestamp, "priceFixed": str(event.price),
            "price": display_price(event.price), "size": event.size, "side": side,
            "large": event.size >= self.large_trade_threshold,
        })
        for timeframe, duration in TIMEFRAMES_NS.items():
            start = (event.ts_event // duration) * duration
            key = (timeframe, start)
            bar = self.bars.get(key)
            if bar is None:
                bar = MutableBar(timeframe, start, start + duration, event.price, event.price, event.price, event.price)
                self.bars[key] = bar
            bar.observe(event)

    def _profile_contract(self) -> dict[str, Any]:
        if not self.profile:
            return {"levels": [], "poc": None, "valueAreaHigh": None, "valueAreaLow": None}
        levels = sorted(self.profile.items())
        poc_fixed = max(levels, key=lambda item: item[1])[0]
        total = sum(volume for _, volume in levels)
        selected: set[int] = set()
        running = 0
        for price, volume in sorted(levels, key=lambda item: item[1], reverse=True):
            selected.add(price)
            running += volume
            if running >= total * 0.7:
                break
        return {
            "levels": [{"priceFixed": str(price), "price": display_price(price), "volume": volume} for price, volume in levels[-80:]],
            "poc": display_price(poc_fixed),
            "valueAreaHigh": display_price(max(selected)),
            "valueAreaLow": display_price(min(selected)),
        }

    def _candidate_contracts(self, *, data_complete: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        absorptions: list[dict[str, Any]] = []
        icebergs: list[dict[str, Any]] = []
        cutoff = self.last_ts - self.absorption_window_seconds * 1_000_000_000
        for price, trades in self.trade_windows.items():
            recent = [trade for trade in trades if trade[0] >= cutoff]
            if len(recent) < self.absorption_minimum_observations:
                continue
            aggressive = sum(size for _, _, size in recent)
            elapsed_ms = max(0.0, (recent[-1][0] - recent[0][0]) / 1_000_000)
            if aggressive < self.absorption_minimum_aggressive_volume or elapsed_ms < self.absorption_minimum_elapsed_ms:
                continue
            buys = sum(size for _, side, size in recent if side == "buy")
            sells = aggressive - buys
            displacement = abs((self.last_trade_price or price) - price) / 250_000_000
            replenishments = sum(1 for ts in self.replenishments.get(price, ()) if ts >= cutoff)
            if displacement <= 2:
                side = "ask" if buys >= sells else "bid"
                volume_score = min(1.0, aggressive / max(self.absorption_minimum_aggressive_volume * 2, 1))
                displacement_score = max(0.0, 1.0 - displacement / 3.0)
                replenishment_score = min(1.0, replenishments / self.replenishment_threshold)
                observation_score = min(1.0, len(recent) / (self.absorption_minimum_observations * 2))
                elapsed_score = min(1.0, elapsed_ms / (self.absorption_minimum_elapsed_ms * 2))
                persistence_score = (observation_score + elapsed_score) / 2
                completeness_score = 1.0 if data_complete else 0.35
                confidence = (
                    volume_score * 0.28 + displacement_score * 0.22 + replenishment_score * 0.20
                    + persistence_score * 0.20 + completeness_score * 0.10
                )
                reason_codes = ["HIGH_AGGRESSIVE_VOLUME", "LOW_PRICE_DISPLACEMENT", "PERSISTENCE_CONFIRMED"]
                if replenishments:
                    reason_codes.append("REPLENISHMENT_OBSERVED")
                if len(recent) >= self.absorption_minimum_observations:
                    reason_codes.append("REPEATED_FILLS_SAME_PRICE")
                absorptions.append({
                    "kind": "absorption",
                    "side": side, "price": display_price(price), "priceFixed": str(price),
                    "startNs": str(recent[0][0]), "endNs": str(recent[-1][0]),
                    "aggressiveVolume": aggressive, "priceDisplacementTicks": displacement,
                    "observations": len(recent), "elapsedMs": round(elapsed_ms),
                    "replenishmentScore": replenishments, "confidence": round(confidence, 3),
                    "scoreComponents": {
                        "volume": round(volume_score, 3), "displacement": round(displacement_score, 3),
                        "replenishment": round(replenishment_score, 3), "persistence": round(persistence_score, 3),
                        "dataCompleteness": round(completeness_score, 3),
                    },
                    "reasonCodes": list(dict.fromkeys(reason_codes)),
                })
            elif replenishments >= self.replenishment_threshold:
                icebergs.append({
                    "kind": "iceberg",
                    "side": "ask" if buys >= sells else "bid", "price": display_price(price), "priceFixed": str(price),
                    "executedVolume": aggressive, "replenishments": replenishments,
                    "observations": len(recent), "elapsedMs": round(elapsed_ms),
                    "confidence": round(min(0.8, 0.35 + replenishments * 0.06 + min(elapsed_ms / 5000, 0.15)), 3),
                    "reasonCodes": ["REPEATED_FILLS_SAME_PRICE", "VISIBLE_SIZE_REPLENISHED"],
                })
        absorptions.sort(key=lambda item: (item["confidence"], item["aggressiveVolume"]), reverse=True)
        icebergs.sort(key=lambda item: (item["confidence"], item["executedVolume"]), reverse=True)
        used = {(item["side"], item["priceFixed"]) for item in absorptions}
        icebergs = [item for item in icebergs if (item["side"], item["priceFixed"]) not in used]
        return absorptions[:self.absorption_candidate_limit], icebergs[:self.absorption_candidate_limit]

    def bars_contract(self) -> list[dict[str, Any]]:
        cumulative = 0
        result: list[dict[str, Any]] = []
        for (_, _), bar in sorted(self.bars.items(), key=lambda item: (item[0][0], item[0][1])):
            cumulative += bar.buy_volume - bar.sell_volume
            result.append(bar.contract(cumulative, completed=bar.end_ns <= self.last_ts))
        return result

    def footprint_contract(self) -> list[dict[str, Any]]:
        if not self.footprint:
            return []
        latest_bucket = max(bucket for bucket, _ in self.footprint)
        rows = []
        for (bucket, price), (bid_volume, ask_volume) in sorted(self.footprint.items(), reverse=True):
            if bucket != latest_bucket:
                continue
            ratio = ask_volume / max(bid_volume, 1)
            inverse = bid_volume / max(ask_volume, 1)
            rows.append({
                "priceFixed": str(price), "price": display_price(price), "bidVolume": bid_volume,
                "askVolume": ask_volume, "delta": ask_volume - bid_volume, "totalVolume": ask_volume + bid_volume,
                "imbalance": "buy" if ratio >= self.imbalance_ratio else "sell" if inverse >= self.imbalance_ratio else "none",
            })
        for index, row in enumerate(rows):
            side = row["imbalance"]
            row["stackedImbalance"] = side != "none" and sum(1 for candidate in rows[max(0, index - 2): index + 3] if candidate["imbalance"] == side) >= 3
        return rows[:80]


    def footprint_bar_contract(self) -> dict[str, Any] | None:
        if not self.footprint:
            return None
        bucket = max(bucket for bucket, _ in self.footprint)
        start_ns = bucket * TIMEFRAMES_NS["1m"]
        end_ns = start_ns + TIMEFRAMES_NS["1m"]
        elapsed_seconds = min(max((self.last_ts - start_ns) / 1_000_000_000, 0), 60)
        completed = end_ns <= self.last_ts
        return {
            "startNs": str(start_ns), "endNs": str(end_ns), "completed": completed,
            "elapsedSeconds": round(60 if completed else elapsed_seconds, 3),
            "remainingSeconds": round(0 if completed else 60 - elapsed_seconds, 3),
        }

    def structure_contract(self) -> list[dict[str, Any]]:
        result = []
        bars = self.bars_contract()
        for timeframe in TIMEFRAMES_NS:
            series = [bar for bar in bars if bar["timeframe"] == timeframe and bar["completed"]]
            if len(series) < 3:
                result.append({"timeframe": timeframe, "state": "insufficient_data", "triggerLevels": [], "invalidation": None, "confidence": 0, "dataTimestampNs": str(self.last_ts)})
                continue
            recent = series[-3:]
            rising = recent[0]["low"] < recent[1]["low"] < recent[2]["low"] and recent[0]["high"] < recent[1]["high"] < recent[2]["high"]
            falling = recent[0]["low"] > recent[1]["low"] > recent[2]["low"] and recent[0]["high"] > recent[1]["high"] > recent[2]["high"]
            state = "trend_up" if rising else "trend_down" if falling else "range"
            invalidation = recent[-2]["low"] if rising else recent[-2]["high"] if falling else None
            result.append({
                "timeframe": timeframe, "state": state,
                "triggerLevels": [recent[-2]["high"], recent[-2]["low"]], "invalidation": invalidation,
                "confidence": 0.7 if rising or falling else 0.45, "dataTimestampNs": str(self.last_ts),
            })
        return result

    def contract(self, *, data_complete: bool = False) -> dict[str, Any]:
        volume = self.buy_volume + self.sell_volume
        elapsed = max((self.last_ts - self.first_ts) / 1_000_000_000, 0.001)
        vwap_fixed = self.price_size_sum // volume if volume else None
        recent_cutoff = self.last_ts - 2_000_000_000
        recent_flow = [item for item in self.flow_events if item[0] >= recent_cutoff]
        absorptions, icebergs = self._candidate_contracts(data_complete=data_complete)
        bars = self.bars_contract()
        prices = list(self.profile)
        completed_counts = {
            timeframe: sum(1 for bar in bars if bar["timeframe"] == timeframe and bar["completed"])
            for timeframe in TIMEFRAMES_NS
        }
        completed_one_minute = [bar for bar in bars if bar["timeframe"] == "1m" and bar["completed"]]
        recent_one_minute = completed_one_minute[-30:]
        ranges = [float(bar["high"]) - float(bar["low"]) for bar in recent_one_minute[-14:]]
        closes = [float(bar["close"]) for bar in recent_one_minute]
        moves = [right - left for left, right in zip(closes, closes[1:])]
        realized_volatility = (sum(move * move for move in moves) / len(moves)) ** 0.5 if moves else 0.0
        path = sum(abs(move) for move in moves)
        trend_strength = abs(closes[-1] - closes[0]) / path if len(closes) > 1 and path else 0.0
        regime = "momentum" if trend_strength >= 0.6 else "mean_reversion" if trend_strength <= 0.25 and len(closes) >= 5 else "chop"
        eastern = ZoneInfo("America/New_York")
        event_time = datetime.fromtimestamp(self.last_ts / 1_000_000_000, UTC).astimezone(eastern) if self.last_ts else None
        dated_bars: list[tuple[dict[str, Any], datetime]] = []
        for bar in completed_one_minute:
            start_ns = int(str(bar.get("startNs") or "0"))
            if start_ns:
                dated_bars.append((bar, datetime.fromtimestamp(start_ns / 1_000_000_000, UTC).astimezone(eastern)))
        trading_date = event_time.date() if event_time else None
        opening_bars = [
            bar for bar, local_time in dated_bars
            if trading_date and local_time.date() == trading_date and time(9, 30) <= local_time.time() < time(10, 0)
        ]
        opening_range = {
            "high": max((float(bar["high"]) for bar in opening_bars), default=None),
            "low": min((float(bar["low"]) for bar in opening_bars), default=None),
            "complete": len(opening_bars) >= 30,
            "bars": len(opening_bars),
            "definition": "US cash open 09:30-10:00 America/New_York",
        }
        overnight_bars = [
            bar for bar, local_time in dated_bars
            if trading_date and (
                (local_time.date() == trading_date and local_time.time() < time(9, 30))
                or (local_time.date().toordinal() == trading_date.toordinal() - 1 and local_time.time() >= time(18, 0))
            )
        ]
        overnight_high = max((float(bar["high"]) for bar in overnight_bars), default=None)
        overnight_low = min((float(bar["low"]) for bar in overnight_bars), default=None)
        cash_open = datetime.combine(event_time.date(), time(9, 30), tzinfo=event_time.tzinfo) if event_time else None
        seconds_to_cash_open = int((cash_open - event_time).total_seconds()) if event_time and cash_open else None
        if not event_time:
            session_phase = "unavailable"
        elif event_time.time() < time(9, 30):
            session_phase = "premarket"
        elif event_time.time() < time(10, 30):
            session_phase = "cash_open"
        elif event_time.time() < time(14, 0):
            session_phase = "midday"
        elif event_time.time() < time(16, 0):
            session_phase = "cash_close"
        else:
            session_phase = "after_hours"
        ranked_profile = sorted(self.profile.items(), key=lambda item: item[1], reverse=True)
        return {
            "tradeSummary": {
                "buyVolume": self.buy_volume, "sellVolume": self.sell_volume, "delta": self.cumulative_delta,
                "tradeCount": self.trade_count, "averageTradeSize": round(volume / max(self.trade_count, 1), 2),
                "tradePacePerSecond": round(self.trade_count / elapsed, 2), "volumePerSecond": round(volume / elapsed, 2),
                "vwap": display_price(vwap_fixed) if vwap_fixed else None,
            },
            "bars": bars[-240:], "barStatus": {
                "completed1m": completed_counts["1m"], "completed5m": completed_counts["5m"],
                "completed15m": completed_counts["15m"],
                "forming1m": any(bar["timeframe"] == "1m" and not bar["completed"] for bar in bars),
            },
            "footprint": self.footprint_contract(), "footprintBar": self.footprint_bar_contract(), "tape": list(self.tape),
            "pullingStacking": {
                "stackedSize": sum(size for _, kind, _, size in recent_flow if kind == "stack"),
                "pulledSize": sum(size for _, kind, _, size in recent_flow if kind == "pull"),
                "executedSize": sum(size for _, kind, _, size in recent_flow if kind == "execute"),
                "windowSeconds": 2, "initialSnapshotExcluded": True,
            },
            "absorptionCandidates": absorptions, "icebergCandidates": icebergs,
            "volumeProfile": self._profile_contract(), "marketStructure": self.structure_contract(),
            "context": {
                "sessionOpen": display_price(self.first_trade_price) if self.first_trade_price else None,
                "sessionHigh": display_price(max(prices)) if prices else None,
                "sessionLow": display_price(min(prices)) if prices else None,
                "vwap": display_price(vwap_fixed) if vwap_fixed else None,
                "sessionVwap": display_price(vwap_fixed) if vwap_fixed else None,
                "anchoredVwap": display_price(vwap_fixed) if vwap_fixed else None,
                "openingRange": opening_range,
                "atr1m": round(mean(ranges), 4) if ranges else None,
                "realizedVolatility1m": round(realized_volatility, 4),
                "trendStrength": round(trend_strength, 4),
                "regime": regime,
                "liquidityRegime": "active" if volume / elapsed >= 10 else "thin",
                "sessionPhase": session_phase,
                "timeUntilUsCashOpenSeconds": seconds_to_cash_open,
                "highVolumeNodes": [display_price(price) for price, _ in ranked_profile[:3]],
                "lowVolumeNodes": [display_price(price) for price, _ in sorted(self.profile.items(), key=lambda item: item[1])[:3]],
                "overnightHigh": overnight_high, "overnightLow": overnight_low,
                "overnightStatus": "available" if overnight_bars else "not_available",
                "previousSessionHigh": None, "previousSessionLow": None, "previousSessionClose": None,
                "previousSessionStatus": "not_available",
            },
        }
