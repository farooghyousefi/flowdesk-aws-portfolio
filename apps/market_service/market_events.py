from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from apps.connectors.databento.src.dbn_reader import F_SNAPSHOT, MboEvent, iter_events


ACTION_TYPES = {
    "A": "order_add",
    "M": "order_modify",
    "C": "order_cancel",
    "T": "trade",
    "R": "book_reset",
    "F": "order_fill",
}


@dataclass(frozen=True)
class NormalizedMarketEvent:
    event_type: str
    timestamp: str
    timestamp_ns: int
    receive_timestamp_ns: int
    publisher_id: int
    channel_id: int
    sequence: int
    stable_tie_breaker: int
    side: str
    price_fixed: int
    size: int
    order_id: int
    instrument_id: int
    action: str
    flags: int
    snapshot: bool

    @property
    def sort_key(self) -> tuple[int, int, int, int, int]:
        return (
            self.timestamp_ns,
            self.publisher_id,
            self.channel_id,
            self.sequence,
            self.stable_tie_breaker,
        )

    def contract(self) -> dict[str, int | str | bool]:
        return {
            "eventType": self.event_type,
            "timestamp": self.timestamp,
            "timestampNs": self.timestamp_ns,
            "receiveTimestampNs": self.receive_timestamp_ns,
            "publisherId": self.publisher_id,
            "channelId": self.channel_id,
            "sequence": self.sequence,
            "stableTieBreaker": self.stable_tie_breaker,
            "side": self.side,
            "priceFixed": self.price_fixed,
            "size": self.size,
            "orderId": self.order_id,
            "instrumentId": self.instrument_id,
            "action": self.action,
            "flags": self.flags,
            "snapshot": self.snapshot,
        }


def normalize_mbo_event(event: MboEvent, stable_tie_breaker: int) -> NormalizedMarketEvent:
    return NormalizedMarketEvent(
        event_type=ACTION_TYPES.get(event.action, "unknown"),
        timestamp=event.timestamp,
        timestamp_ns=event.ts_event,
        receive_timestamp_ns=event.ts_recv,
        publisher_id=event.publisher_id,
        channel_id=event.channel_id,
        sequence=event.sequence,
        stable_tie_breaker=stable_tie_breaker,
        side=event.side,
        price_fixed=event.price,
        size=event.size,
        order_id=event.order_id,
        instrument_id=event.instrument_id,
        action=event.action,
        flags=event.flags,
        snapshot=bool(event.flags & F_SNAPSHOT),
    )


def normalize_events(events: Iterable[MboEvent]) -> list[NormalizedMarketEvent]:
    normalized = [normalize_mbo_event(event, index) for index, event in enumerate(events)]
    return sorted(normalized, key=lambda item: item.sort_key)


def stream_normalized_events(path: Path) -> Iterator[NormalizedMarketEvent]:
    chunk: list[NormalizedMarketEvent] = []
    last_timestamp = -1
    for index, event in enumerate(iter_events(path)):
        normalized = normalize_mbo_event(event, index)
        if last_timestamp >= 0 and normalized.timestamp_ns != last_timestamp and chunk:
            yield from sorted(chunk, key=lambda item: item.sort_key)
            chunk.clear()
        chunk.append(normalized)
        last_timestamp = normalized.timestamp_ns
    if chunk:
        yield from sorted(chunk, key=lambda item: item.sort_key)

