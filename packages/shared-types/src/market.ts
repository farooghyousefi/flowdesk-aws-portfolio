export const MARKET_CONTRACT_VERSION = 1 as const;

export type MarketMode = "historical" | "replay" | "live";
export type DataCompleteness = "complete" | "partial" | "unknown";
export type BookReliability = "guaranteed" | "not_guaranteed";
export type ExternalVerificationStatus = "externally_verified" | "external_verification_pending";

export interface MarketEvent {
  version: typeof MARKET_CONTRACT_VERSION;
  tsEventNs: string;
  tsReceiveNs?: string;
  publisherId: number;
  instrumentId: number;
  sequence: number;
  action: string;
  side: "bid" | "ask" | "none";
  priceFixed: string;
  size: number;
  orderId?: string;
  flags: number;
}

export interface PriceLevel {
  priceFixed: string;
  displayPrice: number;
  totalSize: number;
  orderCount: number;
}

export interface OrderBookSnapshot {
  version: typeof MARKET_CONTRACT_VERSION;
  timestampNs: string;
  instrumentId: number;
  bestBid?: PriceLevel;
  bestAsk?: PriceLevel;
  spreadTicks?: number;
  bids: PriceLevel[];
  asks: PriceLevel[];
  completeness: DataCompleteness;
  reliability: BookReliability;
}

export interface DataSourceHealth {
  state: "connected" | "disconnected" | "disabled" | "error";
  mode: MarketMode;
  message: string;
  lastEventAt?: string;
}

export interface MarketDataSource {
  connect(): Promise<void>;
  disconnect(): Promise<void>;
  stream(): AsyncIterable<MarketEvent>;
  health(): Promise<DataSourceHealth>;
}

export interface SetupDecision {
  state: "trade_ready" | "wait" | "blocked";
  direction?: "long" | "short";
  timestamp: string;
  setupName: string;
  entryZone?: { min: number; max: number };
  invalidation?: number;
  targets?: number[];
  estimatedRiskTicks?: number;
  estimatedRewardTicks?: number;
  reasonCodes: string[];
  humanReasons: string[];
  missingConditions: string[];
  confidence: number;
  dataReliability: string;
}

export interface RiskGuardDecision {
  state: "allowed" | "caution" | "blocked";
  manuallyMaintained: true;
  remainingDrawdown: number;
  plannedRiskUsd: number;
  reasonCodes: string[];
  humanReasons: string[];
}
