from __future__ import annotations

from collections import deque
from enum import StrEnum
from pathlib import Path
from typing import Iterable, Iterator, Protocol

from .market_events import NormalizedMarketEvent, stream_normalized_events


class LiveState(StrEnum):
    CONNECTING = "CONNECTING"
    LIVE = "LIVE"
    DELAYED = "DELAYED"
    DEGRADED = "DEGRADED"
    DISCONNECTED = "DISCONNECTED"
    RESYNCING = "RESYNCING"


class MarketEventProvider(Protocol):
    source: str

    def events(self) -> Iterator[NormalizedMarketEvent]: ...


class HistoricalProvider:
    source = "historical"

    def __init__(self, path: Path) -> None:
        self.path = path

    def events(self) -> Iterator[NormalizedMarketEvent]:
        yield from stream_normalized_events(self.path)


class ReplayProvider:
    source = "replay"

    def __init__(self, events: Iterable[NormalizedMarketEvent]) -> None:
        self._events = tuple(sorted(events, key=lambda item: item.sort_key))

    def events(self) -> Iterator[NormalizedMarketEvent]:
        yield from self._events


class LiveProvider:
    """Transport-neutral live boundary. It never places or exposes broker orders."""

    source = "live"

    def __init__(self, *, maximum_buffer: int = 100_000) -> None:
        self.state = LiveState.DISCONNECTED
        self.signal_eligible = False
        self.reason_code = "LIVE_DISCONNECTED"
        self._buffer: deque[NormalizedMarketEvent] = deque(maxlen=maximum_buffer)
        self._last_sequence: dict[tuple[int, int], int] = {}
        self._snapshot_ready = False

    def connect(self) -> None:
        self.state = LiveState.CONNECTING
        self.signal_eligible = False
        self.reason_code = "WAITING_FOR_SYNCHRONIZED_SNAPSHOT"

    def disconnect(self) -> None:
        self.state = LiveState.DISCONNECTED
        self.signal_eligible = False
        self.reason_code = "LIVE_DISCONNECTED"

    def mark_delayed(self) -> None:
        if self.state == LiveState.LIVE:
            self.state = LiveState.DELAYED
        self.signal_eligible = False
        self.reason_code = "LIVE_DATA_DELAYED"

    def begin_resync(self, reason_code: str = "SEQUENCE_GAP") -> None:
        self.state = LiveState.RESYNCING
        self.signal_eligible = False
        self.reason_code = reason_code
        self._snapshot_ready = False
        self._last_sequence.clear()

    def ingest(self, event: NormalizedMarketEvent) -> bool:
        key = (event.publisher_id, event.channel_id)
        previous = self._last_sequence.get(key)
        if previous is not None and event.sequence > previous + 1:
            self.state = LiveState.DEGRADED
            self.signal_eligible = False
            self.reason_code = "SEQUENCE_GAP"
            self._snapshot_ready = False
            return False
        if previous is not None and event.sequence <= previous:
            self.state = LiveState.DEGRADED
            self.signal_eligible = False
            self.reason_code = "OUT_OF_ORDER_EVENT"
            return False
        self._last_sequence[key] = event.sequence
        self._buffer.append(event)
        if event.snapshot:
            self._snapshot_ready = True
        if event.action == "R":
            self.begin_resync("BOOK_RESET")
            return True
        if self._snapshot_ready and self.state in {LiveState.CONNECTING, LiveState.RESYNCING, LiveState.DEGRADED, LiveState.DELAYED}:
            self.state = LiveState.LIVE
            self.signal_eligible = True
            self.reason_code = "LIVE_SYNCHRONIZED"
        return True

    def events(self) -> Iterator[NormalizedMarketEvent]:
        while self._buffer:
            yield self._buffer.popleft()

    def status(self) -> dict[str, str | bool | int]:
        return {
            "state": self.state.value,
            "signalEligible": self.signal_eligible,
            "reasonCode": self.reason_code,
            "snapshotSynchronized": self._snapshot_ready,
            "bufferedEvents": len(self._buffer),
            "automaticOrderExecution": False,
        }
