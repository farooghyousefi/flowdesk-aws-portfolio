import { describe, expect, it } from "vitest";
import {
  calculateConsistency,
  calculatePnl,
  calculateProfitFactor,
  calculateRMultiple,
  calculateRequiredProfitForConsistency,
  calculateRisk,
  calculateRiskUsd
} from "@/lib/calculations";
import { evaluateTradeApproval, updateCriteria, createDefaultCriteria } from "@/lib/rules";
import type { Trade } from "@/lib/types";

const baseTrade: Trade = {
  id: "calculation-fixture",
  date: "2026-01-02",
  time: "15:30",
  market: "MES",
  direction: "LONG",
  session: "NYAM",
  biasTimeframe: "15m",
  setupTimeframe: "5m",
  entryTimeframe: "1m",
  setupName: "Calculation fixture",
  entry: 5_000,
  stop: 4_990,
  target: 5_020,
  exit: 5_020,
  contracts: 1,
  risk: 50,
  fees: 0,
  slippage: 0,
  grossPnl: 100,
  netPnl: 100,
  rMultiple: 2,
  outcome: "Gewinn",
  durationMinutes: 20,
  mfe: 110,
  mae: -10,
  entryReason: "Deterministic unit-test fixture",
  exitReason: "Target",
  emotion: "neutral",
  mistakes: "",
  learning: "",
  ruleCompliant: true,
  tradedDespiteBlock: false,
  rating: 3,
  screenshots: [],
  createdAt: "2026-01-02T15:30:00Z"
};

const calculationTrades: Trade[] = [
  baseTrade,
  {
    ...baseTrade,
    id: "calculation-fixture-loss",
    date: "2026-01-03",
    exit: 4_992,
    grossPnl: -40,
    netPnl: -40,
    rMultiple: -0.8,
    outcome: "Verlust"
  }
];

describe("risk calculation", () => {
  it("calculates MES risk", () => {
    expect(calculateRiskUsd("MES", 5275.25, 5265.25, 1)).toBe(50);
  });

  it("calculates MNQ risk", () => {
    expect(calculateRiskUsd("MNQ", 18635.75, 18655.75, 2)).toBe(80);
  });

  it("calculates MGC risk", () => {
    expect(calculateRiskUsd("MGC", 2351.4, 2344.4, 2)).toBe(140);
  });

  it("calculates GC risk", () => {
    expect(calculateRiskUsd("GC", 2334.1, 2341.1, 1)).toBe(700);
  });

  it("blocks missing stop", () => {
    const risk = calculateRisk({
      market: "MES",
      direction: "LONG",
      entry: 5275.25,
      stop: 5275.25,
      target: 5295.25,
      contracts: 1,
      accountBalance: 50000,
      maxLossLimit: 1500,
      dailyLossBuffer: 160,
      riskLimitPerTrade: 80
    });
    expect(risk.warnings).toContain("Kein gültiger Stop-Loss definiert.");
  });

  it("blocks too much risk", () => {
    const risk = calculateRisk({
      market: "GC",
      direction: "LONG",
      entry: 2334.1,
      stop: 2341.1,
      target: 2354.1,
      contracts: 1,
      accountBalance: 50000,
      maxLossLimit: 1500,
      dailyLossBuffer: 160,
      riskLimitPerTrade: 80
    });
    expect(risk.warnings).toContain("Dieses Risiko ist zu hoch.");
  });

  it("blocks reached daily loss buffer", () => {
    const risk = calculateRisk({
      market: "MES",
      direction: "LONG",
      entry: 5275.25,
      stop: 5265.25,
      target: 5295.25,
      contracts: 4,
      accountBalance: 50000,
      maxLossLimit: 1500,
      dailyLossBuffer: 100,
      riskLimitPerTrade: 500
    });
    expect(risk.warnings).toContain("Der verbleibende Tagesrisikopuffer reicht für diesen Trade nicht aus.");
  });
});

describe("challenge and performance calculations", () => {
  it("calculates consistency", () => {
    expect(calculateConsistency(calculationTrades)).toBeGreaterThan(0);
  });

  it("calculates profit target adjustment", () => {
    expect(calculateRequiredProfitForConsistency(1200, 0.4)).toBe(3000);
  });

  it("calculates long pnl", () => {
    expect(calculatePnl("MES", "LONG", 5275.25, 5295.25, 1)).toBe(100);
  });

  it("calculates short pnl", () => {
    expect(calculatePnl("MNQ", "SHORT", 18635.75, 18595.75, 2)).toBe(160);
  });

  it("calculates r multiple", () => {
    expect(calculateRMultiple(100, 50)).toBe(2);
  });

  it("calculates profit factor", () => {
    expect(calculateProfitFactor(calculationTrades)).toBeGreaterThan(1);
  });
});

describe("trade approval", () => {
  it("allows a trade only when every required criterion is fulfilled", () => {
    const approved = createDefaultCriteria("LONG").map((row) => ({ ...row, status: "yes" as const }));
    expect(evaluateTradeApproval(approved).state).toBe("allowed");
  });

  it("returns waiting when a required setup criterion is missing", () => {
    const rows = updateCriteria(createDefaultCriteria("LONG"), "bos", "no");
    expect(evaluateTradeApproval(rows).title).toBe("NICHT TRADEN");
  });

  it("returns blocked when a hard rule is violated", () => {
    const rows = updateCriteria(createDefaultCriteria("LONG"), "risk", "no");
    expect(evaluateTradeApproval(rows).state).toBe("blocked");
  });
});
