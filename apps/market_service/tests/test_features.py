from __future__ import annotations

from apps.connectors.databento.src.dbn_reader import MboEvent, OrderBook
from apps.market_service.features import OrderflowFeatures
from apps.market_service.microstructure import MicrostructureFeatures


def event(*, ts: int, action: str = "T", side: str = "B", price: int = 5_000_000_000_000, size: int = 1, order_id: int = 1, flags: int = 128) -> MboEvent:
    return MboEvent(
        timestamp="2026-07-15T00:00:00.000000000Z", ts_event=ts, action=action, side=side,
        price=price, size=size, order_id=order_id, instrument_id=42, sequence=ts,
        flags=flags, ts_recv=ts, publisher_id=1, channel_id=1,
    )


def test_trade_volume_delta_footprint_profile_and_vwap() -> None:
    features = OrderflowFeatures(large_trade_threshold=5, imbalance_ratio=3)
    features.observe(event(ts=1_000_000_000, side="B", size=9))
    features.observe(event(ts=1_100_000_000, side="A", price=5_000_250_000_000, size=2))
    features.observe(event(ts=1_100_000_001, action="F", side="B", size=30))
    contract = features.contract()
    assert contract["tradeSummary"]["buyVolume"] == 9
    assert contract["tradeSummary"]["sellVolume"] == 2
    assert contract["tradeSummary"]["delta"] == 7
    assert contract["tradeSummary"]["tradeCount"] == 2
    assert contract["tradeSummary"]["vwap"] == 5000.045454545
    assert sum(level["totalVolume"] for level in contract["footprint"]) == 11
    assert contract["volumeProfile"]["poc"] == 5000.0
    assert len(contract["bars"]) == 3


def test_imbalance_pulling_stacking_absorption_and_iceberg_candidates() -> None:
    features = OrderflowFeatures(
        large_trade_threshold=2,
        imbalance_ratio=3,
        absorption_minimum_aggressive_volume=10,
    )
    price = 5_000_000_000_000
    for index in range(4):
        ts = 1_000_000_000 + index * 200_000_000
        features.observe(event(ts=ts, action="A", price=price, size=2, order_id=100 + index, flags=0))
        features.observe(event(ts=ts + 1, action="T", side="B", price=price, size=3, order_id=200 + index, flags=0))
    features.observe(event(ts=1_700_000_000, action="C", price=price, size=1, flags=0), before_order=type("Order", (), {"price": price, "size": 2})())
    contract = features.contract(data_complete=True)
    assert contract["footprint"][0]["imbalance"] == "buy"
    assert contract["pullingStacking"]["stackedSize"] == 8
    assert contract["pullingStacking"]["pulledSize"] == 1
    assert contract["absorptionCandidates"]
    assert contract["icebergCandidates"] == []
    candidate = contract["absorptionCandidates"][0]
    assert "REPEATED_FILLS_SAME_PRICE" in candidate["reasonCodes"]
    assert candidate["scoreComponents"]["dataCompleteness"] == 1


def test_short_start_burst_is_filtered_and_candidates_are_limited() -> None:
    burst = OrderflowFeatures(large_trade_threshold=2)
    for index in range(8):
        burst.observe(event(ts=1_000_000_000 + index * 20_000_000, price=5_000_000_000_000 + index * 250_000_000, size=20, flags=0))
    assert burst.contract()["absorptionCandidates"] == []

    persistent = OrderflowFeatures(
        large_trade_threshold=2, absorption_minimum_elapsed_ms=100,
        absorption_minimum_aggressive_volume=3, absorption_candidate_limit=2,
    )
    for level in range(4):
        price = 5_000_000_000_000 + level * 250_000_000
        for index in range(3):
            persistent.observe(event(ts=1_000_000_000 + index * 100_000_000, price=price, size=3, order_id=level * 10 + index, flags=0))
    candidates = persistent.contract(data_complete=True)["absorptionCandidates"]
    assert len(candidates) <= 2
    assert len({(item["side"], item["priceFixed"]) for item in candidates}) == len(candidates)


def test_bar_contract_distinguishes_forming_and_completed() -> None:
    features = OrderflowFeatures()
    features.observe(event(ts=1_000_000_000, size=2))
    forming = features.contract()
    assert forming["barStatus"]["completed1m"] == 0
    assert forming["barStatus"]["forming1m"] is True
    features.observe(event(ts=61_000_000_000, action="A", flags=0, size=1))
    completed = features.contract()
    assert completed["barStatus"]["completed1m"] == 1
    assert next(bar for bar in completed["bars"] if bar["timeframe"] == "1m")["completed"] is True


def test_snapshot_adds_are_not_counted_as_stacking() -> None:
    features = OrderflowFeatures()
    features.observe(event(ts=1, action="A", flags=32 | 128, size=100))
    assert features.contract()["pullingStacking"]["stackedSize"] == 0


def test_microprice_depth_imbalance_rates_and_sweep_detection() -> None:
    book = OrderBook()
    features = MicrostructureFeatures(window_seconds=5, large_trade_size=5)

    def apply(item: MboEvent) -> None:
        before = book.orders.get(item.order_id)
        book.apply(item)
        features.observe(item, book=book, before_order=before)

    apply(event(ts=1_000_000_000, action="A", side="B", price=5_000_000_000_000, size=10, order_id=10))
    apply(event(ts=1_000_000_001, action="A", side="A", price=5_000_250_000_000, size=2, order_id=11))
    apply(event(ts=1_050_000_000, action="T", side="B", price=5_000_250_000_000, size=6, order_id=0))
    apply(event(ts=1_090_000_000, action="T", side="B", price=5_000_500_000_000, size=7, order_id=0))
    contract = features.contract(book)
    assert contract["orderBook"]["bestBid"] == 5000.0
    assert contract["orderBook"]["bestAsk"] == 5000.25
    assert contract["orderBook"]["microprice"] == 5000.208333333
    assert contract["orderBook"]["queueImbalance"] == 0.6667
    assert contract["orderBook"]["depthImbalance"]["3"] == 0.6667
    assert contract["orderActivity"]["addRate"] > 0
    assert contract["tradeAggression"]["largeTradeClusters"] == 2
    assert contract["tradeAggression"]["sweep"]["detected"] is True
    assert contract["tradeAggression"]["sweep"]["levels"] == 2


def test_context_features_use_completed_bars_and_expose_regime_without_future_values() -> None:
    features = OrderflowFeatures()
    for index in range(31):
        features.observe(event(
            ts=1_000_000_000 + index * 60_000_000_000,
            price=5_000_000_000_000 + index * 250_000_000,
            size=2,
        ))
    context = features.contract()["context"]
    assert context["openingRange"]["complete"] is False
    assert context["openingRange"]["definition"].startswith("US cash open")
    assert context["atr1m"] is not None
    assert context["regime"] == "momentum"
    assert context["previousSessionStatus"] == "not_available"
    assert context["overnightStatus"] == "not_available"
