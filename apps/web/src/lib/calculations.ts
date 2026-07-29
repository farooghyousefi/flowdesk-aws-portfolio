import { getMarket } from "@/lib/markets";
import type {
  ChallengeConfig,
  Direction,
  MarketSymbol,
  RiskInput,
  RiskResult,
  Trade
} from "@/lib/types";

export function roundCurrency(value: number): number {
  return Math.round((value + Number.EPSILON) * 100) / 100;
}

export function calculateRiskUsd(
  market: MarketSymbol,
  entry: number,
  stop: number,
  contracts: number
): number {
  const distance = Math.abs(entry - stop);
  return roundCurrency(distance * getMarket(market).pointValue * contracts);
}

export function calculateRewardUsd(
  market: MarketSymbol,
  entry: number,
  target: number,
  contracts: number
): number {
  const distance = Math.abs(target - entry);
  return roundCurrency(distance * getMarket(market).pointValue * contracts);
}

export function calculatePnl(
  market: MarketSymbol,
  direction: Direction,
  entry: number,
  exit: number,
  contracts: number,
  fees = 0,
  slippage = 0
): number {
  const multiplier = direction === "LONG" ? 1 : -1;
  const gross = (exit - entry) * multiplier * getMarket(market).pointValue * contracts;
  return roundCurrency(gross - fees - slippage);
}

export function calculateRMultiple(netPnl: number, riskUsd: number): number {
  if (riskUsd <= 0) return 0;
  return Math.round((netPnl / riskUsd) * 100) / 100;
}

export function calculateProfitFactor(trades: Trade[]): number {
  const wins = trades.filter((trade) => trade.netPnl > 0).reduce((sum, trade) => sum + trade.netPnl, 0);
  const losses = Math.abs(
    trades.filter((trade) => trade.netPnl < 0).reduce((sum, trade) => sum + trade.netPnl, 0)
  );
  if (losses === 0) return wins > 0 ? Number.POSITIVE_INFINITY : 0;
  return Math.round((wins / losses) * 100) / 100;
}

export function calculateMinimumEquity(challenge: ChallengeConfig): number {
  return roundCurrency(challenge.eodHighWaterMark - challenge.maximumLossLimit);
}

export function groupProfitByDay(trades: Trade[]): Map<string, number> {
  return trades.reduce((days, trade) => {
    days.set(trade.date, roundCurrency((days.get(trade.date) ?? 0) + trade.netPnl));
    return days;
  }, new Map<string, number>());
}

export function calculateLargestWinningDay(trades: Trade[]): number {
  const dayValues = Array.from(groupProfitByDay(trades).values());
  return roundCurrency(Math.max(0, ...dayValues));
}

export function calculateTotalProfit(trades: Trade[]): number {
  return roundCurrency(trades.reduce((sum, trade) => sum + trade.netPnl, 0));
}

export function calculateConsistency(trades: Trade[]): number {
  const totalProfit = calculateTotalProfit(trades);
  if (totalProfit <= 0) return 0;
  return Math.round((calculateLargestWinningDay(trades) / totalProfit) * 10000) / 100;
}

export function calculateRequiredProfitForConsistency(largestWinningDay: number, limit = 0.4): number {
  if (largestWinningDay <= 0) return 0;
  return roundCurrency(largestWinningDay / limit);
}

export function calculateRisk(input: RiskInput): RiskResult {
  const riskUsd = calculateRiskUsd(input.market, input.entry, input.stop, input.contracts);
  const rewardUsd = calculateRewardUsd(input.market, input.entry, input.target, input.contracts);
  const rr = riskUsd > 0 ? Math.round((rewardUsd / riskUsd) * 100) / 100 : 0;
  const drawdownPercent = Math.round((riskUsd / input.maxLossLimit) * 10000) / 100;
  const oneContractRisk = calculateRiskUsd(input.market, input.entry, input.stop, 1);
  const maxContracts = oneContractRisk > 0 ? Math.max(0, Math.floor(input.riskLimitPerTrade / oneContractRisk)) : 0;
  const warnings: string[] = [];

  if (input.stop === input.entry || input.stop <= 0) {
    warnings.push("Kein gültiger Stop-Loss definiert.");
  }
  if (riskUsd > input.riskLimitPerTrade) {
    warnings.push(`Dein geplantes Risiko beträgt ${drawdownPercent.toFixed(2)} % des gesamten Drawdowns.`);
    warnings.push("Dieses Risiko ist zu hoch.");
  }
  if (rr < 1.5) {
    warnings.push("Chance-Risiko-Verhältnis unter 1,5.");
  }
  if (riskUsd > input.dailyLossBuffer) {
    warnings.push("Der verbleibende Tagesrisikopuffer reicht für diesen Trade nicht aus.");
  }

  return {
    riskUsd,
    rewardUsd,
    rr,
    drawdownPercent,
    maxContracts,
    remainingDailyBuffer: roundCurrency(input.dailyLossBuffer - riskUsd),
    remainingTotalDrawdown: roundCurrency(input.maxLossLimit - riskUsd),
    warnings
  };
}
