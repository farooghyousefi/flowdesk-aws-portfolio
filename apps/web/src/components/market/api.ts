import type { AuthorizationResult, BacktestPlan, BacktestSummary, CostLedger, DatasetRequestPlan, JournalEntry, PlannerDownloadJob, PlannerEstimate, PlannerEstimateJob, PlannerResult, ProtocolStatus, RangeAuthorizationResult, RangePlan, RangePlannerPreview, RangePlannerResult, ReplayState, ResearchJob, ResearchStatus, SessionLibraryRecord, SessionRecord, Settings } from "./types";

export const MARKET_URL = process.env.NEXT_PUBLIC_MARKET_SERVICE_URL ?? "/market-api";

export class MarketApiError extends Error {
  constructor(public code: string, message: string, public nextAction: string, public status: number) {
    super(message);
    this.name = "MarketApiError";
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${MARKET_URL}${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options?.headers ?? {}) },
    cache: "no-store"
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    const detail = body.detail ?? body;
    if (detail && typeof detail === "object") {
      throw new MarketApiError(detail.code ?? "REQUEST_FAILED", detail.message ?? `Request failed (${response.status}).`, detail.nextAction ?? "Reload and try again.", response.status);
    }
    throw new MarketApiError("REQUEST_FAILED", String(detail ?? `Request failed (${response.status}).`), "Reload and try again.", response.status);
  }
  return response.json() as Promise<T>;
}

export const marketApi = {
  sessions: () => request<SessionRecord[]>("/sessions"),
  replayState: () => request<ReplayState>("/replay/state"),
  load: (sessionId: string) => request<ReplayState>("/replay/load", { method: "POST", body: JSON.stringify({ sessionId }) }),
  action: (action: "play" | "pause" | "reset") => request<ReplayState>(`/replay/${action}`, { method: "POST", body: "{}" }),
  step: (kind: "event_group" | "trade") => request<ReplayState>("/replay/step", { method: "POST", body: JSON.stringify({ kind }) }),
  seek: (progress: number) => request<ReplayState>("/replay/seek", { method: "POST", body: JSON.stringify({ progress }) }),
  jump: (kind: "first_trade" | "high_volume") => request<ReplayState>("/replay/jump", { method: "POST", body: JSON.stringify({ kind }) }),
  speed: (speed: string) => request<ReplayState>("/replay/speed", { method: "POST", body: JSON.stringify({ speed }) }),
  settings: () => request<Settings>("/settings"),
  saveSettings: (settings: Settings) => request<Settings>("/settings", { method: "PUT", body: JSON.stringify(settings) }),
  journal: () => request<JournalEntry[]>("/journal"),
  createJournal: (entry: Partial<JournalEntry>) => request<JournalEntry>("/journal", { method: "POST", body: JSON.stringify(entry) }),
  updateJournal: (id: string, entry: Partial<JournalEntry>) => request<JournalEntry>(`/journal/${id}`, { method: "PUT", body: JSON.stringify(entry) }),
  deleteJournal: (id: string) => request<{ deleted: boolean }>(`/journal/${id}`, { method: "DELETE" }),
  importJournal: (entries: Partial<JournalEntry>[]) => request<{ imported: number }>("/journal/import", { method: "POST", body: JSON.stringify({ entries }) }),
  backtest: () => request<BacktestSummary>("/backtest"),
  plannerStatus: () => request<{ costs: CostLedger; estimates: PlannerEstimate[]; jobs: PlannerDownloadJob[]; estimateJobs: PlannerEstimateJob[]; rangePlans: RangePlan[]; sessions: SessionLibraryRecord[]; requestPlan?: DatasetRequestPlan | null; downloadStarted: false }>("/data-planner/status"),
  previewRangePlan: (payload: Record<string, unknown>) => request<RangePlannerPreview>("/data-planner/range/preview", { method: "POST", body: JSON.stringify(payload) }),
  createRangeEstimateJob: (payload: Record<string, unknown>) => request<PlannerEstimateJob>("/data-planner/range/estimate-jobs", { method: "POST", body: JSON.stringify({ ...payload, kind: "range" }) }),
  rangePlan: (planId: string) => request<RangePlan>(`/data-planner/range-plans/${planId}`),
  authorizeRangePlan: (planId: string, payload: Record<string, unknown>) => request<RangeAuthorizationResult>(`/data-planner/range-plans/${planId}/authorize`, { method: "POST", body: JSON.stringify(payload) }),
  downloadReadyRangeJobs: (planId: string) => request<{ rangePlan: RangePlan; scheduledReadyJobs: number; backgroundDownloadStarted: boolean }>(`/data-planner/range-plans/${planId}/download-ready`, { method: "POST", body: "{}" }),
  previewPlan: (payload: Record<string, unknown>) => request<{ requestPlan: DatasetRequestPlan; valid: true; metadataRequested: false; downloadStarted: false }>("/data-planner/preview", { method: "POST", body: JSON.stringify(payload) }),
  estimatePlan: (payload: Record<string, unknown>) => request<PlannerResult>("/data-planner/estimate", { method: "POST", body: JSON.stringify(payload) }),
  optimizePlan: (payload: Record<string, unknown>) => request<PlannerResult>("/data-planner/optimize", { method: "POST", body: JSON.stringify(payload) }),
  createEstimateJob: (payload: Record<string, unknown>, kind: "estimate" | "optimize") => request<PlannerEstimateJob>("/data-planner/estimate-jobs", { method: "POST", body: JSON.stringify({ ...payload, kind }) }),
  estimateJob: (jobId: string) => request<PlannerEstimateJob>(`/data-planner/estimate-jobs/${jobId}`),
  retryEstimateJob: (jobId: string) => request<PlannerEstimateJob>(`/data-planner/estimate-jobs/${jobId}/retry`, { method: "POST", body: "{}" }),
  cancelEstimateJob: (jobId: string) => request<PlannerEstimateJob>(`/data-planner/estimate-jobs/${jobId}/cancel`, { method: "POST", body: "{}" }),
  purchaseReview: (estimateId: string) => request<{ estimate: PlannerEstimate; expired: boolean; expiresAt: string; remainingSeconds: number; confirmationPhrase: string; confirmationCaseSensitive: true; authorizationAmountDisplay: string; fingerprint: string; canSubmit: boolean; executionMode: string; existingAuthorization: AuthorizationResult | null; chargeCreated: false; fileSaved: false; nextSafeStep: string }>(`/data-planner/estimates/${estimateId}/review`),
  authorizeEstimate: (estimateId: string, payload: Record<string, unknown>) => request<AuthorizationResult>(`/data-planner/estimates/${estimateId}/authorize`, { method: "POST", body: JSON.stringify(payload) }),
  estimateAuthorization: (estimateId: string) => request<AuthorizationResult>(`/data-planner/estimates/${estimateId}/authorization`),
  cancelAuthorizationJob: (jobId: string) => request<AuthorizationResult>(`/data-planner/jobs/${jobId}/cancel-authorization`, { method: "POST", body: "{}" }),
  retryAuthorization: (authorizationId: string) => request<AuthorizationResult>(`/data-planner/authorizations/${authorizationId}/retry`, { method: "POST", body: "{}" }),
  refreshDownloadJobs: () => request<Array<Record<string, unknown>>>("/data-planner/jobs/refresh", { method: "POST", body: "{}" }),
  downloadJob: (jobId: string) => request<Record<string, unknown>>(`/data-planner/jobs/${jobId}/download`, { method: "POST", body: "{}" }),
  sessionLibrary: () => request<SessionLibraryRecord[]>("/session-library"),
  setSessionSplit: (sessionId: string, splitName: string, reason = "", lock = false) => request<SessionLibraryRecord["split"]>(`/session-library/${sessionId}/split`, { method: "PUT", body: JSON.stringify({ splitName, reason, lock }) }),
  protocolStatus: (planId?: string) => request<ProtocolStatus>(`/backtest/plans${planId ? `?planId=${encodeURIComponent(planId)}` : ""}`),
  createPlan: (payload: Record<string, unknown>) => request<BacktestPlan>("/backtest/plans", { method: "POST", body: JSON.stringify(payload) }),
  assignPlanSession: (planId: string, sessionId: string) => request<Record<string, unknown>>(`/backtest/plans/${planId}/sessions`, { method: "POST", body: JSON.stringify({ sessionId }) }),
  clonePlanSession: (planId: string, sessionId: string, uiPracticeOnly: boolean) => request<Record<string, unknown>>(`/backtest/plans/${planId}/sessions/clone`, { method: "POST", body: JSON.stringify({ sessionId, uiPracticeOnly }) }),
  exitBacktestRun: () => request<{ state: ReplayState } & Record<string, unknown>>("/backtest/runs/exit", { method: "POST", body: "{}" }),
  scanSession: (sessionId: string, planId?: string) => request<Record<string, unknown>>("/backtest/scan", { method: "POST", body: JSON.stringify({ sessionId, planId }) }),
  jumpToCandidate: (candidateId: number) => request<ReplayState>("/backtest/candidates/jump", { method: "POST", body: JSON.stringify({ candidateId }) }),
  startBlind: (planId: string, sessionId: string) => request<ReplayState>("/blind/start", { method: "POST", body: JSON.stringify({ planId, sessionId }) }),
  recordBlindTrade: (payload: Record<string, unknown>) => request<Record<string, unknown>>("/blind/trades", { method: "POST", body: JSON.stringify(payload) }),
  closeBlindTrade: (tradeId: string, payload: Record<string, unknown>) => request<Record<string, unknown>>(`/blind/trades/${tradeId}/close`, { method: "POST", body: JSON.stringify(payload) }),
  researchStatus: () => request<ResearchStatus>("/research/status"),
  createResearchJob: (payload: Record<string, unknown>) => request<{ job: ResearchJob } & Record<string, unknown>>("/research/jobs", { method: "POST", body: JSON.stringify(payload) }),
  researchJob: (jobId: string) => request<ResearchJob>(`/research/jobs/${jobId}`),
  cancelResearchJob: (jobId: string) => request<ResearchJob>(`/research/jobs/${jobId}/cancel`, { method: "POST", body: "{}" }),
  pauseResearchJob: (jobId: string) => request<ResearchJob>(`/research/jobs/${jobId}/pause`, { method: "POST", body: "{}" }),
  resumeResearchJob: (jobId: string) => request<ResearchJob>(`/research/jobs/${jobId}/resume`, { method: "POST", body: "{}" }),
  strategyAction: (strategyHash: string, action: "promote" | "reject" | "rollback") => request<Record<string, unknown>>(`/research/strategies/${encodeURIComponent(strategyHash)}/${action}`, { method: "POST", body: "{}" })
};

export function replaySocketUrl(): string {
  const direct = process.env.NEXT_PUBLIC_MARKET_SERVICE_WS_URL;
  if (direct) return direct.replace(/\/$/, "") + "/replay/stream";
  if (MARKET_URL.startsWith("http")) return MARKET_URL.replace(/^http/, "ws") + "/replay/stream";
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}${MARKET_URL}/replay/stream`;
}
