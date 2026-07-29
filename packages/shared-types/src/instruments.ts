import type { Instrument, InstrumentConfig } from "./index";

export const instrumentConfigs: Record<Instrument, InstrumentConfig> = {
  MES: {
    instrument: "MES",
    tickSize: 0.25,
    tickValue: 1.25,
    pointValue: 5,
    biasTimeframes: ["15m"],
    setupTimeframe: "5m",
    entryTimeframes: ["1m", "2m"],
    minimumLevelDistanceR: 2,
    volatilityStrictness: "normal"
  },
  MNQ: {
    instrument: "MNQ",
    tickSize: 0.25,
    tickValue: 0.5,
    pointValue: 2,
    biasTimeframes: ["15m", "30m"],
    setupTimeframe: "5m",
    entryTimeframes: ["1m", "2m"],
    minimumLevelDistanceR: 2.25,
    volatilityStrictness: "high"
  },
  MGC: {
    instrument: "MGC",
    tickSize: 0.1,
    tickValue: 1,
    pointValue: 10,
    biasTimeframes: ["30m"],
    setupTimeframe: "5m",
    entryTimeframes: ["1m", "2m"],
    minimumLevelDistanceR: 2,
    volatilityStrictness: "high"
  },
  GC: {
    instrument: "GC",
    tickSize: 0.1,
    tickValue: 10,
    pointValue: 100,
    biasTimeframes: ["30m", "60m"],
    setupTimeframe: "5m",
    entryTimeframes: ["1m", "2m"],
    minimumLevelDistanceR: 2.5,
    volatilityStrictness: "very_high"
  }
};
