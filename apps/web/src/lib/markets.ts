import type { MarketConfig, MarketSymbol } from "@/lib/types";

export const markets: Record<MarketSymbol, MarketConfig> = {
  MES: {
    symbol: "MES",
    label: "Micro E-mini S&P 500",
    pointValue: 5,
    tickSize: 0.25,
    tickValue: 1.25,
    biasTimeframes: ["15m"],
    setupTimeframes: ["5m"],
    entryTimeframes: ["1m", "2m"],
    preferredBerlinWindow: "15:35-17:00",
    character: "Vergleichsweise strukturiert und ruhiger"
  },
  MNQ: {
    symbol: "MNQ",
    label: "Micro E-mini Nasdaq",
    pointValue: 2,
    tickSize: 0.25,
    tickValue: 0.5,
    biasTimeframes: ["15m", "30m"],
    setupTimeframes: ["5m"],
    entryTimeframes: ["1m", "2m"],
    preferredBerlinWindow: "15:35-17:00",
    character: "Schneller, volatiler, mehr Fakeouts",
    warning: "Achte auf Chasing und zu enge Stop-Distanzen."
  },
  MGC: {
    symbol: "MGC",
    label: "Micro Gold",
    pointValue: 10,
    tickSize: 0.1,
    tickValue: 1,
    biasTimeframes: ["30m"],
    setupTimeframes: ["5m"],
    entryTimeframes: ["1m", "2m"],
    preferredBerlinWindow: "14:30-17:00",
    character: "Makro- und news-sensitiv"
  },
  GC: {
    symbol: "GC",
    label: "Gold Futures",
    pointValue: 100,
    tickSize: 0.1,
    tickValue: 10,
    biasTimeframes: ["30m", "1H"],
    setupTimeframes: ["5m"],
    entryTimeframes: ["1m", "2m"],
    preferredBerlinWindow: "14:30-17:00",
    character: "Sehr volatil",
    warning: "Deutliche Risikowarnung: GC kann Challenge-Limits sehr schnell gefährden."
  }
};

export function getMarket(symbol: MarketSymbol): MarketConfig {
  return markets[symbol];
}
