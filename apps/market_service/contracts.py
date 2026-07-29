from __future__ import annotations

from typing import Any

from apps.connectors.databento.src.dbn_reader import BookSnapshot, MboEvent

CONTRACT_VERSION = 1
PRICE_SCALE = 1_000_000_000
MES_TICK_FIXED = 250_000_000


def display_price(price_fixed: int) -> float:
    return price_fixed / PRICE_SCALE


def event_contract(event: MboEvent) -> dict[str, Any]:
    side = "bid" if event.side == "B" else "ask" if event.side == "A" else "none"
    return {
        "version": CONTRACT_VERSION,
        "tsEventNs": str(event.ts_event),
        "tsReceiveNs": str(event.ts_recv),
        "publisherId": event.publisher_id,
        "instrumentId": event.instrument_id,
        "sequence": event.sequence,
        "action": event.action,
        "side": side,
        "priceFixed": str(event.price),
        "size": event.size,
        "orderId": str(event.order_id) if event.order_id else None,
        "flags": event.flags,
    }


def level_contract(level: Any) -> dict[str, Any]:
    return {
        "priceFixed": str(level.price),
        "displayPrice": display_price(level.price),
        "totalSize": level.total_size,
        "orderCount": level.order_count,
    }


def book_contract(
    snapshot: BookSnapshot,
    *,
    timestamp_ns: int,
    instrument_id: int,
    complete: bool,
) -> dict[str, Any]:
    spread_ticks = None
    if snapshot.spread is not None and snapshot.spread >= 0:
        spread_ticks = snapshot.spread // MES_TICK_FIXED
    return {
        "version": CONTRACT_VERSION,
        "timestampNs": str(timestamp_ns),
        "instrumentId": instrument_id,
        "bestBid": level_contract(snapshot.best_bid) if snapshot.best_bid else None,
        "bestAsk": level_contract(snapshot.best_ask) if snapshot.best_ask else None,
        "spreadTicks": spread_ticks,
        "bids": [level_contract(level) for level in snapshot.bids],
        "asks": [level_contract(level) for level in snapshot.asks],
        "completeness": "complete" if complete else "partial",
        "reliability": "guaranteed" if complete else "not_guaranteed",
    }
