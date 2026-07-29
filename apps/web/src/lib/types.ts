export type MarketSymbol = "MES" | "MNQ" | "MGC" | "GC";
export type Direction = "LONG" | "SHORT";
export type Session = "RTH" | "NYAM" | "NYPM" | "London" | "Asia";
export type CriteriaStatus = "yes" | "no" | "unknown";
export type ApprovalState = "allowed" | "waiting" | "blocked";
export type TradeOutcome = "Gewinn" | "Verlust" | "Break-even";

export interface MarketConfig {
  symbol: MarketSymbol;
  label: string;
  pointValue: number;
  tickSize: number;
  tickValue: number;
  biasTimeframes: string[];
  setupTimeframes: string[];
  entryTimeframes: string[];
  preferredBerlinWindow: string;
  character: string;
  warning?: string;
}

export interface ChallengeConfig {
  name: string;
  startBalance: number;
  profitTarget: number;
  maximumLossLimit: number;
  dailyLossLimit: number | null;
  consistencyRule: number;
  contractLimit: string;
  rewardShare: number;
  maxPayout: number;
  currentBalance: number;
  eodHighWaterMark: number;
}

export interface TradeCriteria {
  id: string;
  name: string;
  required: boolean;
  hardBlock: boolean;
  timeframe: string;
  strength: "niedrig" | "mittel" | "hoch" | "sehr hoch";
  explanation: string;
  waitFor: string;
  status: CriteriaStatus;
  comment?: string;
  appliesTo?: Direction;
}

export interface RiskInput {
  market: MarketSymbol;
  direction: Direction;
  entry: number;
  stop: number;
  target: number;
  contracts: number;
  accountBalance: number;
  maxLossLimit: number;
  dailyLossBuffer: number;
  riskLimitPerTrade: number;
}

export interface RiskResult {
  riskUsd: number;
  rewardUsd: number;
  rr: number;
  drawdownPercent: number;
  maxContracts: number;
  remainingDailyBuffer: number;
  remainingTotalDrawdown: number;
  warnings: string[];
}

export interface Trade {
  id: string;
  date: string;
  time: string;
  market: MarketSymbol;
  direction: Direction;
  session: Session;
  biasTimeframe: string;
  setupTimeframe: string;
  entryTimeframe: string;
  setupName: string;
  entry: number;
  stop: number;
  target: number;
  exit: number;
  contracts: number;
  risk: number;
  fees: number;
  slippage: number;
  grossPnl: number;
  netPnl: number;
  rMultiple: number;
  outcome: TradeOutcome;
  durationMinutes: number;
  mfe: number;
  mae: number;
  entryReason: string;
  exitReason: string;
  emotion: string;
  mistakes: string;
  learning: string;
  ruleCompliant: boolean;
  tradedDespiteBlock: boolean;
  rating: 1 | 2 | 3 | 4 | 5;
  screenshots: ScreenshotItem[];
  createdAt: string;
}

export interface ScreenshotItem {
  id: string;
  type: "pre" | "entry" | "exit" | "review";
  name: string;
  dataUrl: string;
  note?: string;
}

export interface SetupPlaybook {
  id: string;
  name: string;
  description: string;
  longRules: string[];
  shortRules: string[];
  active: boolean;
}

export interface JournalState {
  trades: Trade[];
  challenge: ChallengeConfig;
  playbook: SetupPlaybook[];
}

export interface ApprovalResult {
  state: ApprovalState;
  title: "TRADE ERLAUBT" | "SETUP UNVOLLSTÄNDIG" | "TRADE VERBOTEN" | "NICHT TRADEN";
  message: string;
  missing: TradeCriteria[];
  blockers: TradeCriteria[];
}
