import type { KeyLevel, KeyLevelKind, OhlcvBar } from "@trading-assistant/shared-types";
import { calculateVwapState } from "./vwap";
import { detectSwingPoints } from "./structure";

export function calculateKeyLevels(bars: OhlcvBar[], currentPrice: number, nowIso = new Date().toISOString()): KeyLevel[] {
  const closed = bars.filter((bar) => bar.isClosed);
  const levels: KeyLevel[] = [];
  const daily = groupByDay(closed);
  const days = Array.from(daily.keys()).sort();
  const previousDay = days.at(-2);
  if (previousDay) {
    const barsForDay = daily.get(previousDay) ?? [];
    levels.push(level("PDH", Math.max(...barsForDay.map((bar) => bar.high)), "Previous Day High", "daily", nowIso, currentPrice, true));
    levels.push(level("PDL", Math.min(...barsForDay.map((bar) => bar.low)), "Previous Day Low", "daily", nowIso, currentPrice, true));
    levels.push(level("PDC", barsForDay.at(-1)?.close ?? currentPrice, "Previous Day Close", "daily", nowIso, currentPrice, true));
  }

  const overnight = closed.filter((bar) => bar.session === "Overnight");
  if (overnight.length > 0) {
    levels.push(level("ONH", Math.max(...overnight.map((bar) => bar.high)), "Overnight High", "session", nowIso, currentPrice, true));
    levels.push(level("ONL", Math.min(...overnight.map((bar) => bar.low)), "Overnight Low", "session", nowIso, currentPrice, true));
  }

  const currentSession = closed.filter((bar) => bar.session !== "Overnight" && bar.session !== "Closed");
  if (currentSession.length > 0) {
    levels.push(level("CSH", Math.max(...currentSession.map((bar) => bar.high)), "Current Session High", "session", nowIso, currentPrice, false));
    levels.push(level("CSL", Math.min(...currentSession.map((bar) => bar.low)), "Current Session Low", "session", nowIso, currentPrice, false));
    const openingRange = currentSession.slice(0, 6);
    if (openingRange.length >= 2) {
      levels.push(level("ORH", Math.max(...openingRange.map((bar) => bar.high)), "Opening Range High", "session", nowIso, currentPrice, false));
      levels.push(level("ORL", Math.min(...openingRange.map((bar) => bar.low)), "Opening Range Low", "session", nowIso, currentPrice, false));
    }
  }

  const vwap = calculateVwapState(closed);
  if (vwap.vwap !== null) levels.push(level("VWAP", vwap.vwap, "Session VWAP", "session", nowIso, currentPrice, false));
  addVwapBand(levels, "VWAP_SD1_UP", vwap.standardDeviation1Upper, nowIso, currentPrice);
  addVwapBand(levels, "VWAP_SD1_DOWN", vwap.standardDeviation1Lower, nowIso, currentPrice);
  addVwapBand(levels, "VWAP_SD2_UP", vwap.standardDeviation2Upper, nowIso, currentPrice);
  addVwapBand(levels, "VWAP_SD2_DOWN", vwap.standardDeviation2Lower, nowIso, currentPrice);

  const swings = detectSwingPoints(closed.filter((bar) => bar.timeframe === "5m")).slice(-8);
  for (const swing of swings) {
    levels.push(level(swing.type === "high" ? "SWING_HIGH" : "SWING_LOW", swing.price, "Swing", "5m", swing.at, currentPrice, false));
  }

  return levels.sort((a, b) => Math.abs(a.distanceToPrice) - Math.abs(b.distanceToPrice));
}

function addVwapBand(levels: KeyLevel[], kind: KeyLevelKind, price: number | null, nowIso: string, currentPrice: number): void {
  if (price !== null) levels.push(level(kind, price, "VWAP Standard Deviation", "session", nowIso, currentPrice, false));
}

function level(
  kind: KeyLevelKind,
  price: number,
  source: string,
  timeframe: KeyLevel["timeframe"],
  createdAt: string,
  currentPrice: number,
  sessionComplete: boolean
): KeyLevel {
  return {
    kind,
    price: round(price),
    source,
    timeframe,
    createdAt,
    ageMinutes: Math.max(0, Math.round((Date.now() - new Date(createdAt).getTime()) / 60000)),
    distanceToPrice: round(price - currentPrice),
    tested: Math.abs(price - currentPrice) <= 0.5,
    strength: kind.startsWith("VWAP") ? "mittel" : kind.startsWith("SWING") ? "hoch" : "sehr hoch",
    sessionComplete
  };
}

function groupByDay(bars: OhlcvBar[]): Map<string, OhlcvBar[]> {
  return bars.reduce((map, bar) => {
    const day = bar.startTime.slice(0, 10);
    map.set(day, [...(map.get(day) ?? []), bar]);
    return map;
  }, new Map<string, OhlcvBar[]>());
}

function round(value: number): number {
  return Math.round((value + Number.EPSILON) * 100) / 100;
}
