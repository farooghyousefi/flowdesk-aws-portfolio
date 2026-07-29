import type { OhlcvBar, VwapState } from "@trading-assistant/shared-types";

export function calculateVwapState(bars: OhlcvBar[]): VwapState {
  const closed = bars.filter((bar) => bar.isClosed && bar.timeframe === "5m");
  if (closed.length === 0) return emptyVwap();

  let pv = 0;
  let volume = 0;
  const prices: number[] = [];
  const vwaps: number[] = [];
  for (const bar of closed) {
    const typical = (bar.high + bar.low + bar.close) / 3;
    pv += typical * bar.volume;
    volume += bar.volume;
    prices.push(typical);
    vwaps.push(pv / Math.max(volume, 1));
  }

  const vwap = vwaps.at(-1) ?? null;
  if (vwap === null) return emptyVwap();
  const variance = prices.reduce((sum, price) => sum + (price - vwap) ** 2, 0) / Math.max(prices.length, 1);
  const sd = Math.sqrt(variance);
  const lastClose = closed.at(-1)?.close ?? vwap;
  const previousVwap = vwaps.at(-4) ?? vwaps.at(0) ?? vwap;
  const crossingCount = countCrossings(closed, vwap);

  return {
    timeframe: "5m",
    vwap: round(vwap),
    standardDeviation1Upper: round(vwap + sd),
    standardDeviation1Lower: round(vwap - sd),
    standardDeviation2Upper: round(vwap + sd * 2),
    standardDeviation2Lower: round(vwap - sd * 2),
    priceRelation: Math.abs(lastClose - vwap) <= sd * 0.1 ? "inside" : lastClose > vwap ? "above" : "below",
    slope: Math.abs(vwap - previousVwap) < 0.01 ? "flat" : vwap > previousVwap ? "bullish" : "bearish",
    multipleCrossing: crossingCount >= 3,
    crossingCount,
    dataQuality: volume > 0 ? "confirmed" : "not_available"
  };
}

function emptyVwap(): VwapState {
  return {
    timeframe: "5m",
    vwap: null,
    standardDeviation1Upper: null,
    standardDeviation1Lower: null,
    standardDeviation2Upper: null,
    standardDeviation2Lower: null,
    priceRelation: "unknown",
    slope: "unknown",
    multipleCrossing: false,
    crossingCount: 0,
    dataQuality: "not_available"
  };
}

function countCrossings(bars: OhlcvBar[], vwap: number): number {
  let crossings = 0;
  for (let index = 1; index < bars.length; index += 1) {
    const previous = bars[index - 1].close - vwap;
    const current = bars[index].close - vwap;
    if ((previous <= 0 && current > 0) || (previous >= 0 && current < 0)) crossings += 1;
  }
  return crossings;
}

function round(value: number): number {
  return Math.round((value + Number.EPSILON) * 100) / 100;
}
