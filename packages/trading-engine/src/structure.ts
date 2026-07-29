import type { MarketStructure, OhlcvBar, StructureSignal, Timeframe } from "@trading-assistant/shared-types";

export interface StructureConfig {
  leftStrength: number;
  rightStrength: number;
  minSwingDistanceTicks?: number;
}

export interface SwingPoint {
  type: "high" | "low";
  price: number;
  at: string;
  index: number;
}

export function detectSwingPoints(bars: OhlcvBar[], config: StructureConfig = { leftStrength: 2, rightStrength: 2 }): SwingPoint[] {
  const closed = bars.filter((bar) => bar.isClosed);
  const swings: SwingPoint[] = [];
  for (let index = config.leftStrength; index < closed.length - config.rightStrength; index += 1) {
    const current = closed[index];
    const left = closed.slice(index - config.leftStrength, index);
    const right = closed.slice(index + 1, index + 1 + config.rightStrength);
    if (left.every((bar) => current.high > bar.high) && right.every((bar) => current.high > bar.high)) {
      swings.push({ type: "high", price: current.high, at: current.endTime, index });
    }
    if (left.every((bar) => current.low < bar.low) && right.every((bar) => current.low < bar.low)) {
      swings.push({ type: "low", price: current.low, at: current.endTime, index });
    }
  }
  return swings;
}

export function evaluateMarketStructure(
  bars: OhlcvBar[],
  timeframe: Timeframe,
  config: StructureConfig = { leftStrength: 2, rightStrength: 2 }
): { structure: MarketStructure; signals: StructureSignal[]; swings: SwingPoint[] } {
  const closed = bars.filter((bar) => bar.isClosed && bar.timeframe === timeframe);
  const swings = detectSwingPoints(closed, config);
  const highs = swings.filter((swing) => swing.type === "high").slice(-2);
  const lows = swings.filter((swing) => swing.type === "low").slice(-2);
  const hasHigherHigh = highs.length === 2 && highs[1].price > highs[0].price;
  const hasHigherLow = lows.length === 2 && lows[1].price > lows[0].price;
  const hasLowerHigh = highs.length === 2 && highs[1].price < highs[0].price;
  const hasLowerLow = lows.length === 2 && lows[1].price < lows[0].price;
  const lastClose = closed.at(-1)?.close ?? 0;
  const previousHigh = highs.at(-1)?.price;
  const previousLow = lows.at(-1)?.price;
  const signals: StructureSignal[] = [];

  if (hasHigherHigh) signals.push(signal("higher_high", timeframe, highs[1].price, highs[1].at, "Higher High erkannt."));
  if (hasHigherLow) signals.push(signal("higher_low", timeframe, lows[1].price, lows[1].at, "Higher Low erkannt."));
  if (hasLowerHigh) signals.push(signal("lower_high", timeframe, highs[1].price, highs[1].at, "Lower High erkannt."));
  if (hasLowerLow) signals.push(signal("lower_low", timeframe, lows[1].price, lows[1].at, "Lower Low erkannt."));
  if (previousHigh && lastClose > previousHigh) signals.push(signal("break_of_structure", timeframe, lastClose, closed.at(-1)?.endTime ?? "", "Close über letztem Swing High."));
  if (previousLow && lastClose < previousLow) signals.push(signal("break_of_structure", timeframe, lastClose, closed.at(-1)?.endTime ?? "", "Close unter letztem Swing Low."));

  const trend =
    hasHigherHigh && hasHigherLow ? "bullish" : hasLowerHigh && hasLowerLow ? "bearish" : highs.length >= 2 && lows.length >= 2 ? "range" : "unclear";
  signals.push(
    signal(
      trend === "bullish" ? "bullish_trend" : trend === "bearish" ? "bearish_trend" : trend === "range" ? "range" : "unclear",
      timeframe,
      lastClose,
      closed.at(-1)?.endTime ?? "",
      `Struktur: ${trend}.`
    )
  );

  return {
    structure: {
      timeframe,
      trend,
      hasHigherHigh,
      hasHigherLow,
      hasLowerHigh,
      hasLowerLow,
      strength: trend === "unclear" ? "mittel" : "hoch"
    },
    signals,
    swings
  };
}

function signal(type: StructureSignal["type"], timeframe: Timeframe, price: number | undefined, at: string, explanation: string): StructureSignal {
  return { type, timeframe, price, at, explanation, strength: "hoch" };
}
