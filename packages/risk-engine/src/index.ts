import {
  instrumentConfigs,
  type AccountMetricsMessage,
  type ChallengeGuardConfig,
  type ChallengeSnapshot,
  type ExecutionMessage,
  type Instrument,
  type RiskEvaluation,
  type TradePlan
} from "@trading-assistant/shared-types";

export const defaultChallengeGuard: ChallengeGuardConfig = {
  startBalance: 50000,
  profitTarget: 2500,
  maximumLossLimit: 1500,
  dailyLossLimit: null,
  personalDailyLossLimit: 150,
  consistencyRule: 0.4,
  personalDailyProfitLimit: 500,
  maxWinningDayForBaseTarget: 1000,
  maxLosingTradesPerDay: 2,
  maxTradesPerDay: 3,
  maxRiskPerTrade: 75,
  drawdownSafetyReserve: 100
};

export interface RiskContext {
  currentBalance: number;
  eodHighWaterMark: number;
  realizedPnlToday: number;
  losingTradesToday: number;
  tradesToday: number;
  largestWinningDay: number;
  slippageTicks?: number;
}

export function evaluateRisk(plan: TradePlan, config: ChallengeGuardConfig, context: RiskContext): RiskEvaluation {
  const instrument = instrumentConfigs[plan.instrument];
  const stopDistancePoints = Math.abs(plan.entry - plan.stop);
  const stopDistanceTicks = instrument.tickSize > 0 ? stopDistancePoints / instrument.tickSize : 0;
  const riskUsd = round(stopDistancePoints * instrument.pointValue * plan.contracts);
  const rewardUsd = round(Math.abs(plan.target - plan.entry) * instrument.pointValue * plan.contracts);
  const rr = riskUsd > 0 ? round(rewardUsd / riskUsd, 2) : 0;
  const oneContractRisk = stopDistancePoints * instrument.pointValue;
  const maxAllowedContracts = oneContractRisk > 0 ? Math.floor(config.maxRiskPerTrade / oneContractRisk) : 0;
  const minimumEquity = context.eodHighWaterMark - config.maximumLossLimit;
  const remainingTotalDrawdown = round(context.currentBalance - minimumEquity);
  const remainingDailyRisk = round(config.personalDailyLossLimit + context.realizedPnlToday);
  const slippageReserveUsd = round((context.slippageTicks ?? 2) * instrument.tickValue * plan.contracts);
  const violations: string[] = [];

  if (!Number.isFinite(plan.stop) || plan.stop <= 0 || plan.stop === plan.entry) violations.push("Kein gültiger Stop.");
  if (riskUsd > config.maxRiskPerTrade) violations.push("Risiko pro Trade überschreitet das persönliche Limit.");
  if (rr < 1.5) violations.push("Chance-Risiko-Verhältnis ist zu gering.");
  if (remainingDailyRisk <= 0 || riskUsd > remainingDailyRisk) violations.push("Persönliches Daily-Loss-Limit erreicht oder gefährdet.");
  if (context.losingTradesToday >= config.maxLosingTradesPerDay) violations.push("Maximal zwei Verlusttrades pro Tag erreicht.");
  if (context.tradesToday >= config.maxTradesPerDay) violations.push("Maximale Trade-Anzahl pro Tag erreicht.");
  if (remainingTotalDrawdown < riskUsd + config.drawdownSafetyReserve) violations.push("Maximum Loss Limit wird durch Risiko plus Sicherheitsreserve gefährdet.");
  if (context.realizedPnlToday >= config.personalDailyProfitLimit) violations.push("Persönliches Tagesgewinnlimit erreicht.");
  if (context.largestWinningDay > config.maxWinningDayForBaseTarget) violations.push("Consistency-Planung ist durch den größten Gewinntag bereits angespannt.");

  return {
    valid: violations.length === 0,
    stopDistancePoints: round(stopDistancePoints),
    stopDistanceTicks: round(stopDistanceTicks),
    riskUsd,
    riskPercentOfMaxLoss: round((riskUsd / config.maximumLossLimit) * 100, 2),
    rewardUsd,
    rr,
    maxAllowedContracts,
    remainingDailyRisk,
    remainingTotalDrawdown,
    slippageReserveUsd,
    violations
  };
}

export function requiredProfitForConsistency(largestWinningDay: number, limit = 0.4): number {
  return largestWinningDay <= 0 ? 0 : round(largestWinningDay / limit);
}

export function evaluateChallengeSnapshot(
  account: AccountMetricsMessage,
  executionsToday: ExecutionMessage[],
  dayProfitByDate: Record<string, number>,
  config: ChallengeGuardConfig = defaultChallengeGuard
): ChallengeSnapshot {
  const currentBalance = account.cashValue;
  const currentEquity = account.netLiquidation ?? account.cashValue + account.unrealizedPnl;
  const dayPnl = account.realizedPnl + account.unrealizedPnl;
  const minimumEquity = Math.max(config.startBalance, currentBalance) - config.maximumLossLimit;
  const remainingTotalDrawdown = round(currentEquity - minimumEquity);
  const remainingDailyRisk = round(config.personalDailyLossLimit + dayPnl);
  const tradesToday = new Set(executionsToday.map((execution) => execution.orderId)).size;
  const currentLossStreak = calculateLossStreak(executionsToday);
  const highestDayProfit = Math.max(0, ...Object.values(dayProfitByDate));
  const totalProfit = Math.max(0, currentBalance - config.startBalance);
  const consistency = totalProfit > 0 ? round((highestDayProfit / totalProfit) * 100, 2) : 0;
  const requiredTotalProfit = requiredProfitForConsistency(highestDayProfit, config.consistencyRule);

  return {
    currentBalance,
    currentEquity,
    realizedPnl: account.realizedPnl,
    unrealizedPnl: account.unrealizedPnl,
    dayPnl,
    currentLossStreak,
    tradesToday,
    remainingDailyRisk,
    remainingTotalDrawdown,
    highestDayProfit,
    consistency,
    requiredTotalProfit,
    locks: {
      dailyLoss: dayPnl <= -config.personalDailyLossLimit,
      drawdown: remainingTotalDrawdown <= config.drawdownSafetyReserve,
      maxTrades: tradesToday >= config.maxTradesPerDay,
      maxLosingTrades: currentLossStreak >= config.maxLosingTradesPerDay,
      dailyProfit: dayPnl >= config.personalDailyProfitLimit,
      consistency: highestDayProfit > config.maxWinningDayForBaseTarget
    }
  };
}

export function round(value: number, digits = 2): number {
  const factor = 10 ** digits;
  return Math.round((value + Number.EPSILON) * factor) / factor;
}

export function getInstrumentConfig(instrument: Instrument) {
  return instrumentConfigs[instrument];
}

function calculateLossStreak(executions: ExecutionMessage[]): number {
  let streak = 0;
  for (const execution of [...executions].reverse()) {
    if (execution.realizedPnl === undefined) break;
    if (execution.realizedPnl < 0) {
      streak += 1;
    } else {
      break;
    }
  }
  return streak;
}
