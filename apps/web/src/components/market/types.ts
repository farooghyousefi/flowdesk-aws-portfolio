export type ViewName = "Dashboard" | "Replay" | "Orderflow" | "Setups" | "Risk" | "Journal" | "Backtest" | "Data Planner" | "Data Health" | "Research Lab" | "Settings";

export interface SessionRecord {
  id: string;
  instrument: string;
  symbol: string;
  contract_symbol: string;
  instrument_id: number;
  start_at: string;
  end_at: string;
  record_count: number;
  snapshot_status: string;
  completeness: "complete" | "partial";
  file_path: string;
  sha256: string;
  integrity_status: string;
  unknown_pre: number;
  unknown_during: number;
  unknown_post: number;
  sequence_regressions: number;
  sequence_gaps: number;
  out_of_order_events: number;
  duplicate_events: number;
  dataset_name: string;
  schema_name: string;
  contract_mapping_status: string;
  processing_rate: number;
  peak_rss_mb: number;
  external_verification: string;
  external_book_verification: ExternalBookVerification;
  data_health: DataHealthContract;
}

export interface DataHealthContract {
  completeness: "complete" | "partial";
  mboL3Available: boolean;
  mbp10Available: boolean;
  tradesAvailable: boolean;
  ohlcvAvailable: boolean;
  snapshotPosition: string;
  sequenceGaps: number;
  outOfOrderEvents: number;
  duplicateEvents: number;
  contractMapping: string;
  instrumentId: number;
  timeRange: { start: string; end: string };
  bookReconstructionStatus: "COMPLETE" | "PARTIAL" | "UNAVAILABLE";
  featureAvailability: Record<string, boolean>;
  signalCapability: "FULL_L3_SIGNAL" | "L2_ORDERFLOW_SIGNAL" | "CHART_CONTEXT_ONLY" | "REPLAY_ONLY" | "UNUSABLE";
  fullL3Claim: boolean;
}

export interface SetupReason {
  code: string;
  state: "fulfilled" | "partially_fulfilled" | "missing" | "blocking" | "contradictory" | "unavailable";
  titleKey: string;
  detailKey?: string | null;
  measuredValue?: number | string | null;
  requiredValue?: number | string | null;
}

export interface ExternalBookVerification {
  sessionId: string;
  mboFileHash: string;
  status: "not_requested" | "pending" | "passed" | "failed";
  comparedGroups?: number | null;
  bboMatches?: number | null;
  top10Matches?: number | null;
  mismatches?: number | null;
  reportPath?: string | null;
  verifiedAt?: string | null;
}

export interface ApplicationLockState {
  locked: boolean;
  reason: "none" | "active_locked_run" | "locked_protocol_not_running" | "archived_locked_protocol";
  protocolId?: string | null;
  runId?: string | null;
  sessionId?: string | null;
  strategyHash?: string | null;
}

export interface PriceLevel {
  priceFixed: string;
  displayPrice: number;
  totalSize: number;
  orderCount: number;
}

export interface Bar {
  timeframe: "1m" | "5m" | "15m";
  startNs: string;
  endNs: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  buyVolume: number;
  sellVolume: number;
  delta: number;
  cumulativeDelta: number;
  tradeCount: number;
  vwap: number;
  completed: boolean;
}

export interface TapeTrade {
  tsEventNs: string;
  timestamp: string;
  priceFixed: string;
  price: number;
  size: number;
  side: "buy" | "sell";
  large: boolean;
}

export interface FootprintLevel {
  priceFixed: string;
  price: number;
  bidVolume: number;
  askVolume: number;
  delta: number;
  totalVolume: number;
  imbalance: "buy" | "sell" | "none";
  stackedImbalance: boolean;
}

export interface ReplayState {
  version: 1;
  loaded: boolean;
  mode?: "replay";
  playing: boolean;
  speed?: string;
  revision: number;
  loading?: boolean;
  loadingSessionId?: string | null;
  loadError?: string | null;
  session?: SessionRecord;
  eventCursor?: number;
  eventCount?: number;
  eventGroupCursor?: number;
  eventGroupCount?: number;
  progress?: number;
  timestamp?: string;
  timestampNs?: string;
  book?: {
    timestampNs: string;
    instrumentId: number;
    bestBid?: PriceLevel;
    bestAsk?: PriceLevel;
    spreadTicks?: number;
    bids: PriceLevel[];
    asks: PriceLevel[];
    completeness: "complete" | "partial";
    reliability: "guaranteed" | "not_guaranteed";
  };
  features?: {
    tradeSummary: { buyVolume: number; sellVolume: number; delta: number; tradeCount: number; averageTradeSize: number; tradePacePerSecond: number; volumePerSecond: number; vwap: number | null };
    bars: Bar[];
    barStatus: { completed1m: number; completed5m: number; completed15m: number; forming1m: boolean };
    footprint: FootprintLevel[];
    footprintBar?: { startNs: string; endNs: string; completed: boolean; elapsedSeconds: number; remainingSeconds: number } | null;
    tape: TapeTrade[];
    pullingStacking: { stackedSize: number; pulledSize: number; executedSize: number; windowSeconds: number; initialSnapshotExcluded: boolean };
    absorptionCandidates: Candidate[];
    icebergCandidates: Candidate[];
    volumeProfile: { levels: Array<{ price: number; priceFixed: string; volume: number }>; poc: number | null; valueAreaHigh: number | null; valueAreaLow: number | null };
    marketStructure: Array<{ timeframe: string; state: string; triggerLevels: number[]; invalidation: number | null; confidence: number; dataTimestampNs: string }>;
    context: { sessionOpen: number | null; sessionHigh: number | null; sessionLow: number | null; vwap: number | null; previousSessionStatus: string };
  };
  heatmap?: Array<{ timestampNs: string; bids: Array<{ price: number; size: number }>; asks: Array<{ price: number; size: number }> }>;
  decision?: {
    state: "trade_ready" | "wait" | "blocked";
    direction?: "long" | "short";
    timestamp: string;
    setupName: string;
    entryZone?: { min: number; max: number } | null;
    invalidation?: number | null;
    targets?: number[] | null;
    estimatedRiskTicks?: number | null;
    estimatedRewardTicks?: number | null;
    reasonCodes: string[];
    reasons: SetupReason[];
    humanReasons: string[];
    passedConditions: string[];
    observedEvidence: string[];
    missingConditions: string[];
    confidence: number;
    dataReliability: string;
  };
  risk?: {
    state: "allowed" | "caution" | "blocked";
    manuallyMaintained: boolean;
    accountType: string;
    accountSize: number;
    dayPnl: number;
    totalPnl: number;
    remainingDrawdown: number;
    plannedRiskUsd: number;
    maximumContracts: number;
    openRiskUsd: number;
    tradesToday: number;
    consecutiveLosses: number;
    reasonCodes: string[];
    reasons: SetupReason[];
    humanReasons: string[];
    challengeProfile: Record<string, unknown>;
  };
  signal?: TradingSignal;
  explanation?: string;
  liveStatus?: string;
  manualExecutionOnly?: boolean;
  blind?: {
    mode: "practice" | "pilot" | "locked";
    planId?: string | null;
    runId?: string | null;
    status: "ACTIVE" | "NOT_STARTED";
    futureSeekAllowed: boolean;
    settingsLocked: boolean;
    pendingTradePlan?: boolean;
  };
  applicationLock?: ApplicationLockState;
}

export interface TradingSignal {
  status: "LONG" | "SHORT" | "WAIT" | "NO_TRADE";
  setup: string;
  timestamp: string;
  timestampNs: string;
  validUntil: string | null;
  entryZone: { min: number; max: number; preferred: number; orderType: "MARKET" | "LIMIT" | "STOP" } | null;
  stop: { price: number; ticks: number; reasonCode: string } | null;
  targets: Array<{ price: number; sizePercent: number; reasonCode: string }>;
  contracts: number;
  riskUsd: number;
  rewardRisk: number | null;
  estimatedFillQuality: string;
  confidence: number;
  quality: "A" | "B" | "C" | "NONE";
  regime: string;
  supportingEvidence: SetupReason[];
  opposingEvidence: SetupReason[];
  missingEvidence: SetupReason[];
  invalidation: string[];
  dataQuality: "COMPLETE_L3" | "PARTIAL_L2" | "DEGRADED";
  strategyVersion: string;
  strategyValidationStatus: string;
  modelVersion: string;
  signature: string;
  paperSignal: boolean;
  manualExecutionOnly: true;
  automaticOrderExecution: false;
}

export interface Candidate {
  side: string;
  price?: number;
  confidence: number;
  reasonCodes: string[];
  aggressiveVolume?: number;
  executedVolume?: number;
  replenishments?: number;
  observations?: number;
  elapsedMs?: number;
  kind?: "absorption" | "iceberg";
  scoreComponents?: { volume: number; displacement: number; replenishment: number; persistence: number; dataCompleteness: number };
}

export interface JournalEntry {
  id: string;
  sessionId?: string | null;
  date: string;
  session: string;
  symbol: string;
  direction: string;
  setup: string;
  entry: number;
  stop: number;
  targets: number[];
  exit?: number | null;
  contracts: number;
  riskUsd: number;
  resultUsd?: number | null;
  resultR?: number | null;
  screenshotPath?: string | null;
  notes: string;
  emotion: string;
  mistakeTags: string[];
  createdAt: string;
  updatedAt: string;
}

export type SettingValue = string | number | boolean | null | string[];
export type Settings = Record<string, Record<string, SettingValue>>;
export type BacktestSummary = Record<string, string | number | boolean | null>;

export interface PlannerEstimate {
  estimateId: string;
  fingerprint: string;
  mode: "full_l3" | "economy" | "context";
  label: string;
  dataset: string;
  schemas: string[];
  inputSymbol: string;
  rawSymbol: string;
  instrumentId: number;
  requestStartUtc: string;
  requestEndUtc: string;
  replayStartLocal: string;
  replayEndLocal: string;
  timezone: string;
  estimatedRecords: number;
  billableBytes: number;
  billableMiB: number;
  billableGiB: number;
  estimatedCostUsd: number;
  rawEstimatedCostUsd: number;
  safetyReserveUsd: number;
  maximumAuthorizedUsd: number;
  requestLimitUsd: number;
  dailyLimitUsd: number;
  weeklyLimitUsd: number;
  monthlyLimitUsd: number;
  dailyRemainingUsd: number;
  weeklyRemainingUsd: number;
  monthlyRemainingUsd: number;
  allowed: boolean;
  confidence: "HIGH" | "MEDIUM" | "LOW";
  warnings: string[];
  status: string;
  localReuse: boolean;
  reuse?: { sessionId: string; file: string; action: string } | null;
  availableFeatures: string[];
  disabledFeatures: string[];
  suitability: string[];
  schemaDetails: Array<{ schema: string; records: number; billableBytes: number; estimatedCostUsd: number; unitPriceUsdPerGiB: number }>;
  contract: { inputSymbol: string; rawSymbol: string; instrumentId: number; mappingValidFrom: string; mappingValidTo: string };
  createdAt: string;
  expiresAt: string;
  requestPlan?: DatasetRequestPlan | null;
}

export interface DatasetRequestPlan {
  sessionDate: string;
  timezone: "Europe/Berlin";
  replayStartLocal: string;
  replayEndLocal: string;
  contextMinutes: number;
  replayStartUtc: string;
  replayEndUtc: string;
  requestStartUtc: string;
  requestEndUtc: string;
}

export interface PlannerResult {
  generatedAt: string;
  input: {
    market: string; dataset: string; symbol: string; date: string; timezone: string;
    replayStartLocal: string; replayEndLocal: string; replayStartUtc: string; replayEndUtc: string;
    requestStartUtc: string; requestEndUtc: string;
    contextMinutes: number; days: number;
  };
  contract: PlannerEstimate["contract"];
  estimates: PlannerEstimate[];
  alternatives?: Array<{ label: string; estimate: PlannerEstimate }>;
  costs: CostLedger;
  downloadStarted: false;
  message: string;
}

export interface PlannerEstimateJob {
  id: string;
  requestFingerprint: string;
  jobKind: "estimate" | "optimize" | "range";
  status: "PENDING" | "RUNNING" | "COMPLETED" | "FAILED" | "EXPIRED" | "CANCELLED";
  request: Record<string, string | number | boolean>;
  result: PlannerResult | null;
  error: { code: string; message: string } | null;
  retryOf?: string | null;
  createdAt: string;
  startedAt?: string | null;
  completedAt?: string | null;
  expiresAt: string;
  cancelledAt?: string | null;
  updatedAt: string;
  progress: number;
  checkpoint: { phase?: string; completedDays?: number; totalDays?: number; sessionDate?: string };
  reused: boolean;
}

export interface RangePlannerPreview {
  valid: true;
  metadataRequested: false;
  downloadStarted: false;
  startDate: string;
  endDate: string;
  calendarDays: number;
  sessionDays: number;
  sessionDates: string[];
  timezone: string;
  replayStartLocal: string;
  replayEndLocal: string;
  firstRequestStartUtc: string;
  lastRequestEndUtc: string;
  budgetUsd: number;
  splitPlan: RangeSplitPlan;
}

export interface RangeSplitPlan {
  developmentSessions: number;
  validationSessions: number;
  lockedSessions: number;
  assignments: Array<{ sessionDate: string; splitName: "Development" | "Validation" | "Locked Test"; locked: boolean }>;
}

export interface RangePlanSummary {
  preview: RangePlannerPreview;
  rangePlanId: string;
  status: string;
  estimatedSessionDays: number;
  estimatedDaysCompleted: number;
  localReuseDays: number;
  downloadDays: number;
  blockedDays: number;
  estimatedRecords: number;
  billableBytes: number;
  rawEstimatedCostUsd: number;
  maximumAuthorizedUsd: number;
  budgetUsd: number;
  alreadyReservedUsd: number;
  remainingBudgetAfterUsd: number;
  allowed: boolean;
  confirmationPhrase: string;
  executionMode: "disabled" | "dry_run" | "live";
  splitPlan: RangeSplitPlan;
  dailyEstimates: PlannerEstimate[];
  errors: Array<{ sessionDate: string; message: string }>;
  warnings: string[];
  authorizationIds?: string[];
  authorizedAt?: string;
}

export interface RangePlan {
  id: string;
  requestFingerprint: string;
  request: Record<string, string | number | boolean>;
  estimateIds: string[];
  summary: RangePlanSummary;
  status: string;
  createdAt: string;
  expiresAt: string;
  updatedAt: string;
  authorizationStates: string[];
  remoteJobs: number;
  readyJobs: number;
  completedJobs: number;
}

export interface RangePlannerResult {
  rangePlan: RangePlan;
  downloadStarted: false;
  message: string;
}

export interface RangeAuthorizationResult {
  rangePlan: RangePlan;
  authorizationIds: string[];
  jobIds: string[];
  executionMode: string;
  idempotentReplay: boolean;
  remoteSubmissionCreated: false;
  chargeCreated: false;
}

export type DownloadAuthorizationState =
  | "IDLE" | "VALIDATING" | "SUBMITTING" | "AUTHORIZED" | "QUEUED"
  | "DOWNLOADING" | "IMPORTING" | "VALIDATING_IMPORT" | "COMPLETED"
  | "EXPIRED" | "CANCELLED" | "FAILED";

export interface PlannerAuthorization {
  id: string;
  estimateId: string;
  idempotencyKey: string;
  fingerprint: string;
  mode: string;
  state: DownloadAuthorizationState;
  acceptedTerms: boolean;
  authorizationAmount: number;
  authorizationAmountDisplay: string;
  executionMode: "disabled" | "dry_run" | "live" | "recovered";
  error: { code: string; message: string; retrySafe: boolean } | null;
  recovered: boolean;
  createdAt: string;
  updatedAt: string;
  authorizedAt?: string | null;
}

export interface PlannerAuditEvent {
  id: number;
  eventType: string;
  payload: Record<string, unknown>;
  createdAt: string;
}

export interface PlannerDownloadJob {
  id: string;
  authorizationId?: string | null;
  estimateId: string;
  schema: string;
  mode?: string;
  rawSymbol?: string;
  requestStartUtc?: string;
  requestEndUtc?: string;
  remoteJobId?: string | null;
  state: DownloadAuthorizationState;
  rawState: string;
  readyForDownload: boolean;
  progress: number;
  error: { code: string; message: string; retrySafe?: boolean } | null;
  retrySafe?: boolean;
  authorizationAmountUsd?: number;
  actualCostUsd?: number | null;
  downloadBytes?: number | null;
  executionMode: string;
  recovered: boolean;
  createdAt: string;
  updatedAt: string;
  downloadedAt?: string | null;
  chargedAt?: string | null;
  timeline: PlannerAuditEvent[];
}

export interface AuthorizationResult {
  authorization: PlannerAuthorization;
  jobs: PlannerDownloadJob[];
  timeline: PlannerAuditEvent[];
  idempotentReplay: boolean;
  chargeCreated: false;
  remoteSubmissionCreated: false;
}

export interface CostLedger {
  estimatedToday: number;
  authorizedToday: number;
  downloadedToday: number;
  estimatedWeek: number;
  authorizedWeek: number;
  downloadedWeek: number;
  estimatedMonth: number;
  authorizedMonth: number;
  downloadedMonth: number;
  actualChargedToday: number;
  actualChargedWeek: number;
  actualChargedMonth: number;
  avoidedDuplicateRequests: number;
  localReusableDatasets: number;
}

export interface SessionLibraryRecord extends SessionRecord {
  split: { session_id: string; split_name: string; reason: string; locked: boolean; viewed_at?: string | null };
  local_compressed_bytes: number;
  data_mode: string;
  download_status: string;
  backtest_status: string;
  eligibility?: SessionEligibility;
}

export interface SessionEligibility {
  selectable: boolean;
  canClone: boolean;
  reasonCode: string;
  detailKey: string;
  nextActionKey: string;
}

export interface BacktestPlan {
  id: string;
  mode: "practice" | "pilot" | "locked";
  strategy: string;
  config: {
    fill: Record<string, string | number>;
    startingBalance: number;
    riskPerTrade: number;
    maximumTradesPerDay: number;
    requireFullL3: boolean;
  };
  session_ids: string[];
  strategy_hash: string;
  status: string;
  created_at: string;
  locked_at?: string | null;
  artifact_kind: "user" | "test_artifact";
  archived_at?: string | null;
  assignments: Array<{
    plan_id: string; session_id: string; split_name: string; assignment_type: string;
    reused: boolean; contaminated: boolean; ui_practice_only: boolean; source_plan_id?: string | null;
  }>;
}

export interface ProtocolStatus {
  plans: BacktestPlan[];
  archivedPlans: BacktestPlan[];
  activePlan?: BacktestPlan | null;
  inspectedPlan?: BacktestPlan | null;
  currentRun?: { id: string; plan_id: string; session_id: string; mode: string; status: string; started_at: string; ended_at?: string | null } | null;
  applicationLock: ApplicationLockState;
  phases: Array<{ mode: string; label: string; complete: number; target: number }>;
  sessions: SessionLibraryRecord[];
  trades: Array<Record<string, unknown>>;
  candidates: Array<Record<string, unknown>>;
  audit: Array<{ id: number; eventType: string; createdAt: string; sessionId?: string; payload: Record<string, unknown> }>;
  report: Record<string, string | number | boolean | null | Array<Record<string, unknown>>>;
  defaults: { fill: Record<string, string | number> };
}

export interface ResearchJob {
  id: string;
  experiment_id: string;
  session_id: string;
  status: "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED" | "CANCELLED" | "PAUSED";
  progress: number;
  checkpoint: Record<string, unknown>;
  config: Record<string, unknown>;
  result?: Record<string, unknown> | null;
  error_message?: string | null;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  updated_at: string;
}

export interface ResearchExperiment {
  id: string;
  name: string;
  strategy_name: string;
  strategy_hash: string;
  parameter_hash: string;
  dataset_fingerprint: string;
  split_name: string;
  seed: number;
  fill_model_version: string;
  cost_model_version: string;
  feature_version: string;
  code_version: string;
  status: string;
  config: Record<string, unknown>;
  metrics: Record<string, string | number | boolean | null>;
  validation: { eligible?: boolean; status?: string; failedReasons?: string[]; checks?: Array<{ code: string; passed: boolean }> };
  created_at: string;
  updated_at: string;
}

export interface ResearchStatus {
  datasets: SessionLibraryRecord[];
  jobs: ResearchJob[];
  experiments: ResearchExperiment[];
  strategies: Array<Record<string, unknown>>;
  models: Array<Record<string, unknown>>;
  signals: Array<{ id: string; timestamp: string; status: string; payload: TradingSignal }>;
  readiness: {
    current: {
      completeL3Sessions: number;
      registeredCompleteSessions: number;
      excludedSessionCount: number;
      excludedSessions: Array<Record<string, unknown>>;
      independentDates: number;
      independentMonths: number;
      economicEvents: number;
      newsEvents: number;
      calendarCoverageComplete: boolean;
      newsCoverageComplete: boolean;
    };
    target: {
      months: number;
      independentSessions: number;
      developmentSessions: number;
      validationSessions: number;
      lockedSessions: number;
      calendarCoverageRequired: boolean;
      newsCoverageRequired: boolean;
      minimumIndependentSessionHours: number;
      requiredCoverageStart: string | null;
      requiredCoverageEnd: string | null;
    };
    blockers: string[];
    signalMode: string;
    readyForValidatedSignals: boolean;
  };
  contextCoverage: Record<string, unknown>;
  blueprint: {
    version: string;
    requirements: string[];
    strategyFamilies: string[];
    signalStates: string[];
    automaticOrderExecution: false;
    profitabilityClaim: false;
  };
  defaults: Record<string, string | number>;
  automaticOrderExecution: false;
  profitabilityClaim: false;
}
