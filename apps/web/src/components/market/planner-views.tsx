"use client";

import { useEffect, useRef, useState } from "react";
import {
  AlertTriangle, Check, ChevronRight, Clock3, Database, FileCheck2, Gauge,
  LockKeyhole, Play, RefreshCw, ScanSearch, ShieldCheck, X,
} from "lucide-react";
import { MarketApiError, marketApi } from "./api";
import { authorizationBusy, authorizationDisabledReason } from "./authorization-state";
import type {
  ApplicationLockState, AuthorizationResult, BacktestPlan, CostLedger, DatasetRequestPlan, DownloadAuthorizationState,
  PlannerDownloadJob, PlannerEstimate, PlannerEstimateJob, PlannerResult, ProtocolStatus, RangePlan, RangePlannerPreview, RangePlannerResult, ReplayState, SessionLibraryRecord,
} from "./types";
import { formatNumber, statusText, useI18n, type Locale } from "./i18n";

const emptyCosts: CostLedger = {
  estimatedToday: 0, authorizedToday: 0, downloadedToday: 0,
  estimatedWeek: 0, authorizedWeek: 0, downloadedWeek: 0, estimatedMonth: 0, authorizedMonth: 0, downloadedMonth: 0,
  actualChargedToday: 0, actualChargedWeek: 0, actualChargedMonth: 0,
  avoidedDuplicateRequests: 0, localReusableDatasets: 0,
};

function yesterday(): string {
  const value = new Date();
  value.setDate(value.getDate() - 1);
  return value.toISOString().slice(0, 10);
}

function usd(value: number, locale: Locale = "de"): string {
  return formatNumber(locale, value, { style: "currency", currency: "USD", minimumFractionDigits: value < 1 ? 4 : 2, maximumFractionDigits: value < 1 ? 6 : 2 });
}

function integer(value: number, locale: Locale = "de"): string {
  return formatNumber(locale, value, { maximumFractionDigits: 0 });
}

function bytes(value: number, locale: Locale = "de"): string {
  if (value >= 1024 ** 3) return `${formatNumber(locale, value / 1024 ** 3, { maximumFractionDigits: 2 })} GiB`;
  if (value >= 1024 ** 2) return `${formatNumber(locale, value / 1024 ** 2, { maximumFractionDigits: 1 })} MiB`;
  return `${formatNumber(locale, value / 1024, { maximumFractionDigits: 1 })} KiB`;
}

function StatusTag({ children, tone = "neutral" }: { children: React.ReactNode; tone?: "neutral" | "ok" | "warn" | "bad" | "cyan" }): React.ReactElement {
  return <span className={`planner-tag planner-tag-${tone}`}>{children}</span>;
}

function Ledger({ costs }: { costs: CostLedger }): React.ReactElement {
  const { t, locale } = useI18n();
  const values: Array<[string, string]> = [
    [t("planner.ledgerEstimatedToday"), usd(costs.estimatedToday, locale)], [t("planner.ledgerAuthorizedToday"), usd(costs.authorizedToday, locale)],
    [t("planner.ledgerDownloadedToday"), usd(costs.downloadedToday, locale)], [t("planner.ledgerEstimatedWeek"), usd(costs.estimatedWeek, locale)],
    [t("planner.ledgerDownloadedWeek"), usd(costs.downloadedWeek, locale)], [t("planner.ledgerEstimatedMonth"), usd(costs.estimatedMonth, locale)],
    [t("planner.ledgerDownloadedMonth"), usd(costs.downloadedMonth, locale)], [t("planner.ledgerLocalReusable"), String(costs.localReusableDatasets)],
    [t("planner.ledgerActualChargedToday"), usd(costs.actualChargedToday, locale)], [t("planner.ledgerDuplicatesAvoided"), String(costs.avoidedDuplicateRequests)],
  ];
  return <section className="planner-ledger" aria-label="Local cost ledger">{values.map(([label, value]) => <div key={label}><span>{label}</span><strong className="mono">{value}</strong></div>)}</section>;
}

type Review = Awaited<ReturnType<typeof marketApi.purchaseReview>>;

function authorizationError(reason: unknown, t: (key: string, values?: Record<string, string | number>) => string): string {
  if (reason instanceof MarketApiError) {
    return `${t(`planner.error.${reason.code}`)} ${t("planner.nextAction")}: ${reason.nextAction}`;
  }
  return reason instanceof Error ? reason.message : t("common.failed");
}

function PurchaseModal({ review, onClose, onSubmitted, onReestimate }: { review: Review; onClose: () => void; onSubmitted: (result: AuthorizationResult) => Promise<void>; onReestimate: () => void }): React.ReactElement {
  const { t, locale } = useI18n();
  const [acknowledged, setAcknowledged] = useState(false);
  const [confirmation, setConfirmation] = useState("");
  const [state, setState] = useState<DownloadAuthorizationState>(review.existingAuthorization?.authorization.state ?? (review.expired ? "EXPIRED" : "IDLE"));
  const [remainingSeconds, setRemainingSeconds] = useState(review.remainingSeconds);
  const [idempotencyKey, setIdempotencyKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const estimate = review.estimate;
  const expired = remainingSeconds <= 0 || state === "EXPIRED";
  const reason = authorizationDisabledReason({
    canSubmit: review.canSubmit, expired, acceptedTerms: acknowledged,
    confirmationMatches: confirmation === review.confirmationPhrase,
    idempotencyReady: Boolean(idempotencyKey), state,
  });

  useEffect(() => {
    const storageKey = `flowdesk-download-authorization:${estimate.estimateId}`;
    let value = window.localStorage.getItem(storageKey);
    if (!value) {
      value = window.crypto.randomUUID();
      window.localStorage.setItem(storageKey, value);
    }
    setIdempotencyKey(value);
    const timer = window.setInterval(() => {
      const seconds = Math.max(0, Math.ceil((new Date(review.expiresAt).getTime() - Date.now()) / 1000));
      setRemainingSeconds(seconds);
      if (seconds === 0) setState((current) => authorizationBusy(current) ? current : "EXPIRED");
    }, 250);
    return () => window.clearInterval(timer);
  }, [estimate.estimateId, review.expiresAt]);

  async function submit(): Promise<void> {
    if (reason) return;
    setState("SUBMITTING"); setError(null);
    try {
      const result = await marketApi.authorizeEstimate(estimate.estimateId, {
        estimateId: estimate.estimateId, fingerprint: review.fingerprint, mode: estimate.mode,
        acceptedTerms: acknowledged, confirmationPhrase: confirmation,
        displayedAuthorizationAmount: review.authorizationAmountDisplay, idempotencyKey,
      });
      setState(result.authorization.state);
      await onSubmitted(result);
      onClose();
    } catch (failure) {
      if (failure instanceof MarketApiError && failure.code === "ESTIMATE_EXPIRED") setState("EXPIRED");
      else setState("FAILED");
      setError(authorizationError(failure, t));
      try {
        const recovered = await marketApi.estimateAuthorization(estimate.estimateId);
        setState(recovered.authorization.state);
        await onSubmitted(recovered);
        onClose();
      } catch { /* The original structured error remains visible. */ }
    }
  }

  const minutes = Math.floor(remainingSeconds / 60);
  const seconds = String(remainingSeconds % 60).padStart(2, "0");

  return <div className="modal-backdrop" role="presentation">
    <section className="purchase-modal" role="dialog" aria-modal="true" aria-labelledby="purchase-title">
      <header><div><span>{t("planner.costAuthorization")}</span><h2 id="purchase-title">{t("planner.purchaseTitle")}</h2></div><div className={`authorization-ttl ${expired ? "expired" : ""}`}><Clock3 /><span>{t("planner.ttlRemaining")}</span><strong className="mono">{minutes}:{seconds}</strong></div><button className="icon-button" title={t("common.close")} disabled={authorizationBusy(state)} onClick={onClose}><X /></button></header>
      <dl className="purchase-summary mono">
        <div><dt>{t("planner.mode")}</dt><dd>{t(`planner.mode.${estimate.mode}`)}</dd></div><div><dt>{t("planner.instrument")}</dt><dd>{estimate.rawSymbol} · {estimate.instrumentId}</dd></div>
        <div><dt>{t("planner.requestUtc")}</dt><dd>{estimate.requestStartUtc} → {estimate.requestEndUtc}</dd></div><div><dt>{t("planner.visibleReplay")}</dt><dd>{estimate.replayStartLocal} → {estimate.replayEndLocal}</dd></div>
        <div><dt>{t("planner.estimatedRecords")}</dt><dd>{integer(estimate.estimatedRecords, locale)}</dd></div><div><dt>{t("planner.estimatedSize")}</dt><dd>{bytes(estimate.billableBytes, locale)}</dd></div>
        <div><dt>{t("planner.estimatedCost")}</dt><dd>{usd(estimate.estimatedCostUsd, locale)}</dd></div><div><dt>{t("planner.safetyReserve")}</dt><dd>{usd(estimate.safetyReserveUsd, locale)}</dd></div>
        <div><dt>{t("planner.maximumAuthorized")}</dt><dd>{usd(estimate.maximumAuthorizedUsd, locale)}</dd></div><div><dt>{t("planner.dailyRemaining")}</dt><dd>{usd(estimate.dailyRemainingUsd, locale)}</dd></div>
        <div><dt>{t("planner.expiresAt")}</dt><dd>{estimate.expiresAt}</dd></div>
      </dl>
      <p className="purchase-warning">{t("planner.authorizationExplain", { amount: new Intl.NumberFormat(locale === "de" ? "de-DE" : "en-US", { style: "currency", currency: "USD" }).format(estimate.maximumAuthorizedUsd) })}</p>
      <p className="purchase-policy">{t("planner.reserveExplain")} {t("planner.confirmationRule")}</p>
      <label className="purchase-check"><input type="checkbox" disabled={expired || authorizationBusy(state)} checked={acknowledged} onChange={(event) => { setAcknowledged(event.target.checked); setState("VALIDATING"); }} /><span>{t("planner.understandCost")}</span></label>
      <label className="confirmation-field"><span>{t("planner.exactConfirmation")} <code>{review.confirmationPhrase}</code></span><input className="mono" disabled={expired || authorizationBusy(state)} value={confirmation} autoComplete="off" onChange={(event) => { setConfirmation(event.target.value); setState("VALIDATING"); }} /></label>
      <div className="authorization-state-line"><StatusTag tone={state === "FAILED" || state === "EXPIRED" ? "bad" : state === "AUTHORIZED" || state === "QUEUED" ? "ok" : "cyan"}>{t(`planner.authorizationState.${state}`)}</StatusTag><span>{t(`planner.disabledReason.${reason ?? "ready"}`)}</span></div>
      {!review.canSubmit ? <div className="inline-alert"><AlertTriangle />{review.expired ? t("planner.expiredRetry") : review.existingAuthorization ? t("planner.alreadyAuthorized") : t("planner.blocked")}</div> : null}
      {review.executionMode === "dry_run" ? <div className="inline-alert"><ShieldCheck />{t("planner.dryRunNotice")}</div> : null}
      {review.executionMode === "disabled" ? <div className="inline-alert bad"><AlertTriangle />{t("planner.queueDisabled")}</div> : null}
      {error ? <div className="inline-alert bad"><AlertTriangle />{error}</div> : null}
      <footer><button className="secondary-button" disabled={authorizationBusy(state)} onClick={onClose}>{t("common.cancel")}</button>{expired ? <button className="command-button purchase-submit" onClick={onReestimate}><RefreshCw />{t("planner.reestimate")}</button> : <button className="command-button purchase-submit" disabled={Boolean(reason)} title={reason ? t(`planner.disabledReason.${reason}`) : t("planner.authorize")} onClick={submit}><ShieldCheck />{authorizationBusy(state) ? t("planner.submitting") : t("planner.authorize")}</button>}</footer>
    </section>
  </div>;
}

function ModeTable({ estimates, selected, onSelect, onReview }: { estimates: PlannerEstimate[]; selected?: PlannerEstimate; onSelect: (estimate: PlannerEstimate) => void; onReview: (estimate: PlannerEstimate) => void }): React.ReactElement {
  const { t, locale } = useI18n();
  return <section className="data-panel mode-comparison">
    <header className="panel-heading"><span>{t("planner.modeComparison")}</span><span>{t("planner.estimateOnly")}</span></header>
    <div className="table-scroll"><table className="terminal-table planner-table"><thead><tr><th>{t("planner.mode")}</th><th>{t("planner.schemas")}</th><th>{t("planner.requestUtc")}</th><th>{t("common.records")}</th><th>{t("planner.billable")}</th><th>{t("planner.estimate")}</th><th>{t("planner.confidence")}</th><th>{t("common.status")}</th><th /></tr></thead>
      <tbody>{estimates.map((estimate) => {
        const expired = Date.now() >= new Date(estimate.expiresAt).getTime();
        return <tr key={estimate.estimateId} className={selected?.estimateId === estimate.estimateId ? "selected-row" : ""} onClick={() => onSelect(estimate)}>
          <td><strong>{t(`planner.mode.${estimate.mode}`)}</strong><small>{t(`planner.modeDescription.${estimate.mode}`)}</small></td>
          <td>{estimate.schemas.join(" + ")}</td><td>{estimate.requestStartUtc.slice(11, 16)}–{estimate.requestEndUtc.slice(11, 16)}</td>
          <td>{integer(estimate.estimatedRecords, locale)}</td><td>{bytes(estimate.billableBytes, locale)}</td><td className="cost-cell">{usd(estimate.estimatedCostUsd, locale)}</td>
          <td><StatusTag tone={estimate.confidence === "HIGH" ? "ok" : "warn"}>{statusText(t, estimate.confidence)}</StatusTag></td>
          <td><StatusTag tone={expired ? "bad" : estimate.localReuse ? "cyan" : estimate.allowed ? "ok" : "warn"}>{expired ? t("common.expired") : estimate.localReuse ? t("planner.localReuse") : statusText(t, estimate.status)}</StatusTag></td>
          <td><button className="row-action" onClick={(event) => { event.stopPropagation(); onReview(estimate); }}>{t("planner.review")} <ChevronRight /></button></td>
        </tr>;
      })}</tbody></table></div>
  </section>;
}

function EstimateInspector({ estimate }: { estimate?: PlannerEstimate }): React.ReactElement | null {
  const { t, locale } = useI18n();
  if (!estimate) return null;
  return <section className="estimate-inspector">
    <div className="data-panel estimate-main"><header className="panel-heading"><span>{t(`planner.mode.${estimate.mode}`)}</span><StatusTag tone={estimate.allowed ? "ok" : "warn"}>{estimate.allowed ? t("planner.withinLimits") : t("planner.blocked")}</StatusTag></header>
      <div className="estimate-cost"><span>{t("planner.estimatedCost")}</span><strong className="mono">{usd(estimate.estimatedCostUsd, locale)}</strong><small>+ {usd(estimate.safetyReserveUsd, locale)} {t("planner.safetyReserve")} · {t("planner.maximumAuthorized")} {usd(estimate.maximumAuthorizedUsd, locale)}</small></div>
      <dl className="detail-grid"><div><dt>{t("planner.rawContract")}</dt><dd>{estimate.rawSymbol}</dd></div><div><dt>{t("planner.instrumentId")}</dt><dd>{estimate.instrumentId}</dd></div><div><dt>{t("planner.mapping")}</dt><dd>{estimate.contract.mappingValidFrom} → {estimate.contract.mappingValidTo}</dd></div><div><dt>{t("common.fingerprint")}</dt><dd title={estimate.fingerprint}>{estimate.fingerprint.slice(0, 16)}…</dd></div><div><dt>{t("planner.estimateTtl")}</dt><dd>{estimate.expiresAt}</dd></div><div><dt>{t("planner.blocks")}</dt><dd>{statusText(t, estimate.confidence)}</dd></div></dl>
    </div>
    <div className="data-panel feature-matrix"><header className="panel-heading"><span>{t("planner.capabilityContract")}</span><span>{estimate.schemas.join(" + ")}</span></header><div className="feature-columns"><div><h3>{t("planner.enabled")}</h3>{estimate.availableFeatures.map((item) => <span key={item}><Check />{item}</span>)}</div><div><h3>{t("planner.disabled")}</h3>{estimate.disabledFeatures.map((item) => <span key={item}><X />{item}</span>)}</div></div></div>
    <div className="data-panel limit-panel"><header className="panel-heading"><span>{t("planner.limitsWarnings")}</span><span>{t("planner.localLedger")}</span></header><dl><div><dt>{t("planner.request")}</dt><dd>{usd(estimate.estimatedCostUsd, locale)} / {usd(estimate.requestLimitUsd, locale)}</dd></div><div><dt>{t("planner.dailyRemaining")}</dt><dd>{usd(estimate.dailyRemainingUsd, locale)}</dd></div><div><dt>{t("planner.weeklyRemaining")}</dt><dd>{usd(estimate.weeklyRemainingUsd, locale)}</dd></div><div><dt>{t("planner.monthlyRemaining")}</dt><dd>{usd(estimate.monthlyRemainingUsd, locale)}</dd></div></dl>{estimate.warnings.length ? <ul>{estimate.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul> : <p className="safe-copy"><Check />{t("planner.noBlockingWarnings")}</p>}</div>
  </section>;
}

function DownloadJobs({ jobs, onCancel, onRefresh, onRetry, onDownload }: { jobs: PlannerDownloadJob[]; onCancel: (job: PlannerDownloadJob) => void; onRefresh: () => void; onRetry: (job: PlannerDownloadJob) => void; onDownload: (job: PlannerDownloadJob) => void }): React.ReactElement | null {
  const { t, locale, dateTime } = useI18n();
  if (!jobs.length) return null;
  return <section className="download-jobs">
    <header className="panel-heading"><span>{t("planner.downloadJobs")}</span><span className="job-heading-actions">{t("planner.persistedServerState")}<button className="icon-button" title={t("common.refresh")} onClick={onRefresh}><RefreshCw /></button></span></header>
    <div className="download-job-list">{jobs.map((job) => {
      const active = ["AUTHORIZED", "SUBMITTING", "QUEUED", "DOWNLOADING", "IMPORTING", "VALIDATING_IMPORT"].includes(job.state);
      const tone = job.state === "FAILED" || job.state === "EXPIRED" ? "bad" : job.state === "COMPLETED" ? "ok" : active ? "cyan" : "warn";
      return <article className="download-job-card" key={job.id}>
        <header><div><span>{t("planner.downloadJob")}</span><strong>{job.rawSymbol ?? "MES"} · {t(`planner.mode.${job.mode ?? "full_l3"}`)} · {job.schema}</strong></div><StatusTag tone={tone}>{t(`planner.authorizationState.${job.state}`)}</StatusTag></header>
        <dl>
          <div><dt>{t("planner.jobId")}</dt><dd title={job.id}>{job.id.slice(0, 12)}…</dd></div>
          <div><dt>{t("planner.estimateId")}</dt><dd title={job.estimateId}>{job.estimateId.slice(0, 12)}…</dd></div>
          <div><dt>{t("planner.maximumAuthorized")}</dt><dd>{job.authorizationAmountUsd == null ? "–" : usd(job.authorizationAmountUsd, locale)}</dd></div>
          <div><dt>{t("planner.remoteJobId")}</dt><dd>{job.remoteJobId ?? t("planner.notSubmitted")}</dd></div>
          <div><dt>{t("planner.requestUtc")}</dt><dd>{job.requestStartUtc?.slice(0, 16) ?? "–"} → {job.requestEndUtc?.slice(0, 16) ?? "–"}</dd></div>
          <div><dt>{t("planner.progress")}</dt><dd>{formatNumber(locale, job.progress * 100, { maximumFractionDigits: 0 })}%</dd></div>
          <div><dt>{t("planner.actualCost")}</dt><dd>{job.actualCostUsd == null ? t("planner.pendingActual") : usd(job.actualCostUsd, locale)}</dd></div>
          <div><dt>{t("planner.downloadSize")}</dt><dd>{job.downloadBytes == null ? t("planner.pendingActual") : bytes(job.downloadBytes, locale)}</dd></div>
          <div><dt>{t("planner.updatedAt")}</dt><dd>{dateTime(job.updatedAt)}</dd></div>
          <div><dt>{t("planner.executionMode")}</dt><dd>{t(`planner.executionMode.${job.executionMode}`)}</dd></div>
        </dl>
        {job.recovered ? <div className="job-recovery-warning"><AlertTriangle />{t("planner.recoveredJobWarning")}</div> : null}
        {job.error ? <div className="inline-alert bad"><AlertTriangle /><span><strong>{job.error.code}</strong> {job.error.message}</span></div> : null}
        <div className="job-timeline">{job.timeline.map((event) => <div key={event.id}><time className="mono">{dateTime(event.createdAt)}</time><i /><span>{t(`planner.audit.${event.eventType}`)}</span></div>)}</div>
        <footer>{job.error ? <button className="secondary-button" disabled={!job.retrySafe || !job.authorizationId} title={job.retrySafe ? t("common.retry") : t("planner.retryUnsafe")} onClick={() => onRetry(job)}><RefreshCw />{t("common.retry")}</button> : null}{job.readyForDownload ? <button className="command-button" onClick={() => onDownload(job)}><Database />{t("planner.download")}</button> : null}<button className="secondary-button" disabled={Boolean(job.remoteJobId) || !["AUTHORIZED", "FAILED"].includes(job.state)} title={job.remoteJobId ? t("planner.remoteCancelReason") : t("common.cancel")} onClick={() => onCancel(job)}><X />{t("common.cancel")}</button></footer>
      </article>;
    })}</div>
  </section>;
}

function SessionLibrary({ sessions }: { sessions: SessionLibraryRecord[] }): React.ReactElement {
  const { t, locale } = useI18n();
  return <section className="data-panel library-panel"><header className="panel-heading"><span>{t("planner.sessionLibrary")}</span><span>{t("planner.localDatasets", { count: sessions.length })}</span></header><div className="table-scroll"><table className="terminal-table planner-table"><thead><tr><th>{t("common.date")}</th><th>{t("common.contract")}</th><th>{t("planner.instrument")}</th><th>{t("planner.mode")}</th><th>{t("planner.rawWindow")}</th><th>{t("common.records")}</th><th>{t("planner.fileSize")}</th><th>{t("planner.snapshot")}</th><th>{t("common.integrity")}</th><th>{t("common.split")}</th></tr></thead><tbody>{sessions.map((session) => <tr key={session.id}><td>{session.start_at.slice(0, 10)}</td><td>{session.contract_symbol}</td><td>{session.instrument_id}</td><td><StatusTag tone={session.completeness === "complete" ? "cyan" : "warn"}>{t(`planner.mode.${session.data_mode}`)}</StatusTag></td><td>{session.start_at.slice(11, 19)}–{session.end_at.slice(11, 19)}</td><td>{integer(session.record_count, locale)}</td><td>{bytes(session.local_compressed_bytes, locale)}</td><td>{statusText(t, session.snapshot_status)}</td><td>{statusText(t, session.integrity_status)}</td><td>{t(`split.${session.split.split_name}`)}</td></tr>)}</tbody></table></div></section>;
}

function historicalMonthRange(months: number): { startDate: string; endDate: string } {
  const now = new Date();
  const end = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 0));
  const start = new Date(Date.UTC(end.getUTCFullYear(), end.getUTCMonth() - Math.max(0, months - 1), 1));
  return { startDate: start.toISOString().slice(0, 10), endDate: end.toISOString().slice(0, 10) };
}

function RangePlannerPanel({ savedPlans, onGlobalRefresh }: { savedPlans: RangePlan[]; onGlobalRefresh: () => Promise<void> }): React.ReactElement {
  const { locale, dateTime } = useI18n();
  const initialRange = historicalMonthRange(1);
  const [form, setForm] = useState({
    market: "MES", dataset: "GLBX.MDP3", symbol: "MES.v.0",
    startDate: initialRange.startDate, endDate: initialRange.endDate,
    timezone: "Europe/Berlin", replayStart: "00:00", replayEnd: "22:00",
    contextMinutes: 0, budgetUsd: 125, includeWeekends: false,
  });
  const [preview, setPreview] = useState<RangePlannerPreview | null>(null);
  const [job, setJob] = useState<PlannerEstimateJob | null>(null);
  const [plan, setPlan] = useState<RangePlan | null>(savedPlans[0] ?? null);
  const [busy, setBusy] = useState<"estimate" | "authorize" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [confirmation, setConfirmation] = useState("");
  const [idempotencyKey, setIdempotencyKey] = useState("");
  const previewSequence = useRef(0);
  const de = locale === "de";

  useEffect(() => {
    const storageKey = "flowdesk-range-authorization";
    let key = window.localStorage.getItem(storageKey);
    if (!key) {
      key = window.crypto.randomUUID();
      window.localStorage.setItem(storageKey, key);
    }
    setIdempotencyKey(key);
  }, []);

  useEffect(() => {
    if (!plan && savedPlans[0]) setPlan(savedPlans[0]);
  }, [savedPlans, plan]);

  useEffect(() => {
    const sequence = ++previewSequence.current;
    const timer = window.setTimeout(() => {
      marketApi.previewRangePlan(form).then((next) => {
        if (sequence !== previewSequence.current) return;
        setPreview(next); setError(null);
      }).catch((reason: Error) => {
        if (sequence !== previewSequence.current) return;
        setPreview(null); setError(reason.message);
      });
    }, 180);
    return () => window.clearTimeout(timer);
  }, [form]);

  useEffect(() => {
    if (!job || !["PENDING", "RUNNING"].includes(job.status)) return;
    let cancelled = false;
    const poll = async (): Promise<void> => {
      try {
        const next = await marketApi.estimateJob(job.id);
        if (cancelled) return;
        setJob(next);
        if (next.status === "COMPLETED" && next.result) {
          const result = next.result as unknown as RangePlannerResult;
          setPlan(result.rangePlan); setBusy(null); setError(null); setAcknowledged(false); setConfirmation("");
          await onGlobalRefresh();
        } else if (["FAILED", "EXPIRED", "CANCELLED"].includes(next.status)) {
          setBusy(null); setError(next.error?.message ?? (de ? "Mehrmonats-Schätzung fehlgeschlagen." : "Range estimate failed."));
        }
      } catch (reason) {
        if (!cancelled) setError(reason instanceof Error ? reason.message : (de ? "Mehrmonats-Schätzung fehlgeschlagen." : "Range estimate failed."));
      }
    };
    const timer = window.setInterval(poll, 1000);
    poll();
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [job?.id, job?.status]);

  function setPreset(months: number): void {
    const range = historicalMonthRange(months);
    setForm((current) => ({ ...current, ...range }));
    setPlan(null); setJob(null); setAcknowledged(false); setConfirmation(""); setError(null);
  }

  function change(values: Partial<typeof form>): void {
    setForm((current) => ({ ...current, ...values }));
    setPlan(null); setJob(null); setAcknowledged(false); setConfirmation(""); setError(null);
  }

  async function estimate(): Promise<void> {
    if (!preview) return;
    setBusy("estimate"); setError(null); setPlan(null); setAcknowledged(false); setConfirmation("");
    try {
      const next = await marketApi.createRangeEstimateJob(form);
      setJob(next);
      if (next.status === "COMPLETED" && next.result) {
        const result = next.result as unknown as RangePlannerResult;
        setPlan(result.rangePlan); setBusy(null);
      }
    } catch (reason) {
      setBusy(null); setError(reason instanceof Error ? reason.message : (de ? "Mehrmonats-Schätzung fehlgeschlagen." : "Range estimate failed."));
    }
  }

  async function cancelEstimate(): Promise<void> {
    if (!job) return;
    try {
      const next = await marketApi.cancelEstimateJob(job.id);
      setJob(next); setBusy(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : (de ? "Abbruch fehlgeschlagen." : "Cancellation failed."));
    }
  }

  async function authorize(): Promise<void> {
    if (!plan || !plan.summary.allowed) return;
    setBusy("authorize"); setError(null);
    try {
      const result = await marketApi.authorizeRangePlan(plan.id, {
        rangePlanId: plan.id,
        acceptedTerms: acknowledged,
        confirmationPhrase: confirmation,
        displayedAuthorizationAmount: plan.summary.maximumAuthorizedUsd.toFixed(2),
        idempotencyKey,
      });
      setPlan(result.rangePlan); setBusy(null);
      await onGlobalRefresh();
    } catch (reason) {
      setBusy(null); setError(authorizationError(reason, (key) => key));
    }
  }

  async function refreshRange(): Promise<void> {
    if (!plan) return;
    try {
      await marketApi.refreshDownloadJobs();
      setPlan(await marketApi.rangePlan(plan.id));
      await onGlobalRefresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : (de ? "Statusaktualisierung fehlgeschlagen." : "Status refresh failed."));
    }
  }

  async function downloadReady(): Promise<void> {
    if (!plan || plan.readyJobs <= 0) return;
    try {
      const next = await marketApi.downloadReadyRangeJobs(plan.id);
      setPlan(next.rangePlan);
      await onGlobalRefresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : (de ? "Mehrfachdownload fehlgeschlagen." : "Bulk download failed."));
    }
  }

  const summary = plan?.summary;
  const confirmationReady = Boolean(summary && acknowledged && confirmation === summary.confirmationPhrase && idempotencyKey);
  const checkpoint = job?.checkpoint ?? {};
  const completedDays = checkpoint.completedDays ?? 0;
  const totalDays = checkpoint.totalDays ?? preview?.sessionDays ?? 0;
  const assignments = new Map(summary?.splitPlan.assignments.map((item) => [item.sessionDate, item]) ?? []);

  return <section className="range-planner data-panel">
    <header className="panel-heading">
      <span>{de ? "Mehrmonats-Datenplaner" : "Multi-month data planner"}</span>
      <StatusTag tone="cyan">MBO · FULL L3 · MES</StatusTag>
    </header>
    <div className="range-intro">
      <div><strong>{de ? "Historische Rohdaten einmal kaufen, lokal unbegrenzt wiederverwenden." : "Buy historical raw data once and reuse it locally without limit."}</strong><span>{de ? "Die Schätzung fragt nur Databento-Metadaten ab. Erst die exakte Bestätigungsphrase erzeugt Tagesaufträge." : "The estimate only requests Databento metadata. Daily orders are created only after the exact confirmation phrase."}</span></div>
      <div className="range-presets"><button className="secondary-button" disabled={busy !== null} onClick={() => setPreset(1)}>1 {de ? "Monat" : "month"}</button><button className="secondary-button" disabled={busy !== null} onClick={() => setPreset(3)}>3 {de ? "Monate" : "months"}</button><button className="secondary-button" disabled={busy !== null} onClick={() => setPreset(6)}>6 {de ? "Monate" : "months"}</button></div>
    </div>
    <div className="planner-form range-form">
      <label><span>{de ? "Startdatum" : "Start date"}</span><input type="date" value={form.startDate} disabled={busy !== null} onChange={(event) => change({ startDate: event.target.value })} /></label>
      <label><span>{de ? "Enddatum" : "End date"}</span><input type="date" max={yesterday()} value={form.endDate} disabled={busy !== null} onChange={(event) => change({ endDate: event.target.value })} /></label>
      <label><span>{de ? "Beginn Berlin" : "Start Berlin"}</span><input type="time" value={form.replayStart} disabled={busy !== null} onChange={(event) => change({ replayStart: event.target.value })} /></label>
      <label><span>{de ? "Ende Berlin" : "End Berlin"}</span><input type="time" value={form.replayEnd} disabled={busy !== null} onChange={(event) => change({ replayEnd: event.target.value })} /></label>
      <label><span>{de ? "Kontext-Minuten" : "Context minutes"}</span><input type="number" min="0" max="1440" value={form.contextMinutes} disabled={busy !== null} onChange={(event) => change({ contextMinutes: Number(event.target.value) })} /></label>
      <label><span>{de ? "Maximales Budget USD" : "Maximum budget USD"}</span><input type="number" min="1" max="500" step="0.01" value={form.budgetUsd} disabled={busy !== null} onChange={(event) => change({ budgetUsd: Number(event.target.value) })} /></label>
      <label className="range-checkbox"><input type="checkbox" checked={form.includeWeekends} disabled={busy !== null} onChange={(event) => change({ includeWeekends: event.target.checked })} /><span>{de ? "Wochenenden einbeziehen" : "Include weekends"}</span></label>
      <div className="planner-actions"><button className="command-button" disabled={!preview || busy !== null} onClick={estimate}><Database />{busy === "estimate" ? (de ? "Exakte Kosten werden berechnet…" : "Calculating exact cost…") : (de ? "Exakte Kostenvorschau" : "Exact cost preview")}</button></div>
    </div>
    {preview ? <div className="range-preview-strip"><span><b>{preview.sessionDays}</b> {de ? "Sessions" : "sessions"}</span><span><b>{preview.calendarDays}</b> {de ? "Kalendertage" : "calendar days"}</span><span>{preview.startDate} → {preview.endDate}</span><span>{preview.replayStartLocal}–{preview.replayEndLocal} Berlin</span><span>Dev {preview.splitPlan.developmentSessions} · Val {preview.splitPlan.validationSessions} · Locked {preview.splitPlan.lockedSessions}</span></div> : null}
    {job && ["PENDING", "RUNNING"].includes(job.status) ? <div className="range-progress"><RefreshCw /><div><strong>{de ? "Databento-Metadaten werden tageweise geprüft" : "Checking Databento metadata day by day"}</strong><span>{completedDays} / {totalDays} · {checkpoint.sessionDate ?? "–"}</span><i><b style={{ width: `${Math.max(job.progress * 100, totalDays ? completedDays / totalDays * 100 : 0)}%` }} /></i></div><button className="secondary-button" onClick={cancelEstimate}>{de ? "Abbrechen" : "Cancel"}</button></div> : null}
    {error ? <div className="inline-alert bad"><AlertTriangle />{error}</div> : null}
    {summary ? <>
      <div className="range-summary">
        <div><span>{de ? "Exakte Rohkosten" : "Exact raw cost"}</span><strong>{usd(summary.rawEstimatedCostUsd, locale)}</strong></div>
        <div><span>{de ? "Maximal autorisiert" : "Maximum authorized"}</span><strong>{usd(summary.maximumAuthorizedUsd, locale)}</strong></div>
        <div><span>{de ? "Budget danach" : "Budget after"}</span><strong className={summary.remainingBudgetAfterUsd < 0 ? "negative" : "positive"}>{usd(summary.remainingBudgetAfterUsd, locale)}</strong></div>
        <div><span>{de ? "Lokal vorhanden" : "Local reuse"}</span><strong>{summary.localReuseDays}</strong></div>
        <div><span>{de ? "Neu zu laden" : "New downloads"}</span><strong>{summary.downloadDays}</strong></div>
        <div><span>{de ? "Geschätzte Datenmenge" : "Estimated size"}</span><strong>{bytes(summary.billableBytes, locale)}</strong></div>
        <div><span>{de ? "Geschätzte Records" : "Estimated records"}</span><strong>{integer(summary.estimatedRecords, locale)}</strong></div>
        <div><span>Status</span><StatusTag tone={summary.allowed ? "ok" : "bad"}>{summary.status}</StatusTag></div>
      </div>
      <div className="range-split-band"><span>Development <b>{summary.splitPlan.developmentSessions}</b></span><span>Validation <b>{summary.splitPlan.validationSessions}</b></span><span>Locked Test <b>{summary.splitPlan.lockedSessions}</b></span></div>
      {summary.errors.length ? <div className="inline-alert bad"><AlertTriangle /><span>{summary.errors.map((item) => `${item.sessionDate}: ${item.message}`).join(" · ")}</span></div> : null}
      <div className="table-scroll range-table"><table className="terminal-table planner-table"><thead><tr><th>{de ? "Datum" : "Date"}</th><th>{de ? "Kontrakt" : "Contract"}</th><th>Split</th><th>{de ? "Records" : "Records"}</th><th>{de ? "Kosten" : "Cost"}</th><th>{de ? "Quelle" : "Source"}</th><th>Status</th></tr></thead><tbody>{summary.dailyEstimates.map((estimate, index) => {
        const sessionDate = summary.preview.sessionDates[index] ?? estimate.requestStartUtc.slice(0, 10);
        const split = assignments.get(sessionDate);
        return <tr key={estimate.estimateId}><td>{sessionDate}</td><td>{estimate.rawSymbol}</td><td>{split?.splitName ?? "–"}</td><td>{integer(estimate.estimatedRecords, locale)}</td><td>{estimate.localReuse ? usd(0, locale) : usd(estimate.rawEstimatedCostUsd, locale)}</td><td>{estimate.localReuse ? (de ? "lokal" : "local") : "Databento"}</td><td><StatusTag tone={estimate.localReuse || estimate.allowed ? "ok" : "bad"}>{estimate.localReuse ? (de ? "WIEDERVERWENDEN" : "REUSE") : estimate.allowed ? (de ? "BEREIT" : "READY") : (de ? "BLOCKIERT" : "BLOCKED")}</StatusTag></td></tr>;
      })}</tbody></table></div>
      <div className="range-confirmation">
        <div><strong>{de ? "Kostenfreigabe" : "Cost authorization"}</strong><span>{de ? "Keine automatische Orderausführung. Dies autorisiert ausschließlich historische Databento-Batchdaten." : "No automatic trade execution. This authorizes historical Databento batch data only."}</span><code>{summary.confirmationPhrase}</code></div>
        <label><input type="checkbox" checked={acknowledged} disabled={!summary.allowed || busy !== null} onChange={(event) => setAcknowledged(event.target.checked)} />{de ? "Ich bestätige den angezeigten Maximalbetrag und die tageweisen Batch-Aufträge." : "I confirm the displayed maximum amount and daily batch jobs."}</label>
        <input value={confirmation} disabled={!summary.allowed || busy !== null} placeholder={summary.confirmationPhrase} onChange={(event) => setConfirmation(event.target.value)} />
        <button className="command-button" disabled={!confirmationReady || busy !== null || summary.executionMode === "disabled"} onClick={authorize}><ShieldCheck />{busy === "authorize" ? (de ? "Wird autorisiert…" : "Authorizing…") : summary.executionMode === "dry_run" ? (de ? "Dry-Run autorisieren" : "Authorize dry run") : (de ? `${summary.downloadDays} Tage verbindlich autorisieren` : `Authorize ${summary.downloadDays} days`)}</button>
      </div>
      {plan ? <div className="range-runtime"><span>Plan <b className="mono">{plan.id.slice(0, 12)}…</b></span><span>Status <b>{plan.status}</b></span><span>{de ? "Remote-Jobs" : "Remote jobs"} <b>{plan.remoteJobs}</b></span><span>{de ? "Downloadbereit" : "Ready"} <b>{plan.readyJobs}</b></span><span>{de ? "Importiert" : "Imported"} <b>{plan.completedJobs}</b></span><span>{de ? "Aktualisiert" : "Updated"} <b>{dateTime(plan.updatedAt)}</b></span><button className="secondary-button" onClick={refreshRange}><RefreshCw />{de ? "Remote-Status prüfen" : "Check remote status"}</button>{plan.readyJobs > 0 ? <button className="command-button" onClick={downloadReady}><Database />{de ? `${plan.readyJobs} bereite Tage laden und importieren` : `Download and import ${plan.readyJobs} ready days`}</button> : null}</div> : null}
    </> : <div className="range-safe-state"><ShieldCheck /><span>{de ? "Noch keine Kostenabfrage gestartet. Keine Bestellung, keine neuen Databento-Kosten." : "No cost query started. No order and no new Databento cost."}</span></div>}
  </section>;
}


function estimateJobMatchesPlan(job: PlannerEstimateJob, plan: DatasetRequestPlan): boolean {
  return job.request.date === plan.sessionDate
    && job.request.timezone === plan.timezone
    && job.request.replayStart === plan.replayStartLocal
    && job.request.replayEnd === plan.replayEndLocal
    && Number(job.request.contextMinutes) === plan.contextMinutes;
}

export function DataPlannerView(): React.ReactElement {
  const { t, dateTime } = useI18n();
  const [form, setForm] = useState({ market: "MES", dataset: "GLBX.MDP3", symbol: "MES.v.0", date: yesterday(), timezone: "Europe/Berlin", replayStart: "15:00", replayEnd: "16:30", contextMinutes: 30, days: 1 });
  const [status, setStatus] = useState<{ costs: CostLedger; sessions: SessionLibraryRecord[]; estimates: PlannerEstimate[]; estimateJobs: PlannerEstimateJob[]; jobs: PlannerDownloadJob[]; rangePlans: RangePlan[] }>({ costs: emptyCosts, sessions: [], estimates: [], estimateJobs: [], jobs: [], rangePlans: [] });
  const [preview, setPreview] = useState<DatasetRequestPlan | null>(null);
  const [result, setResult] = useState<PlannerResult | null>(null);
  const [selected, setSelected] = useState<PlannerEstimate>();
  const [review, setReview] = useState<Review | null>(null);
  const [busy, setBusy] = useState<"estimate" | "optimize" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const [estimatingPlan, setEstimatingPlan] = useState<DatasetRequestPlan | null>(null);
  const [activeJob, setActiveJob] = useState<PlannerEstimateJob | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const previewSequence = useRef(0);

  async function refreshStatus(restore = false): Promise<void> {
    const next = await marketApi.plannerStatus();
    setStatus({ costs: next.costs, sessions: next.sessions, estimates: next.estimates, estimateJobs: next.estimateJobs, jobs: next.jobs, rangePlans: next.rangePlans });
    setResult((current) => current ? {
      ...current,
      estimates: current.estimates.map((estimate) => next.estimates.find((item) => item.estimateId === estimate.estimateId) ?? estimate),
      costs: next.costs,
    } : current);
    setSelected((current) => current ? next.estimates.find((item) => item.estimateId === current.estimateId) ?? current : current);
    if (next.requestPlan) {
      setForm((current) => ({
        ...current, date: next.requestPlan!.sessionDate, timezone: next.requestPlan!.timezone,
        replayStart: next.requestPlan!.replayStartLocal, replayEnd: next.requestPlan!.replayEndLocal,
        contextMinutes: next.requestPlan!.contextMinutes,
      }));
      setPreview(next.requestPlan);
      if (restore) {
        const job = next.estimateJobs.find((candidate) => estimateJobMatchesPlan(candidate, next.requestPlan!));
        if (job) {
          setActiveJob(job); setStale(false);
          if (job.status === "COMPLETED" && job.result) {
            setResult(job.result); setSelected(job.result.estimates[0]); setBusy(null); setEstimatingPlan(null);
          } else if (["PENDING", "RUNNING"].includes(job.status)) {
            setBusy(job.jobKind === "range" ? null : job.jobKind); setEstimatingPlan(next.requestPlan);
          } else if (job.error?.message) setError(job.error.message);
        } else if (next.estimates.length) {
          const latest = next.estimates[0];
          const estimates = next.estimates.filter((item) => item.requestStartUtc === latest.requestStartUtc && item.requestEndUtc === latest.requestEndUtc).filter((item, index, all) => all.findIndex((candidate) => candidate.mode === item.mode) === index);
          const restored: PlannerResult = {
            generatedAt: latest.createdAt,
            input: {
              market: "MES", dataset: latest.dataset, symbol: latest.inputSymbol, date: next.requestPlan!.sessionDate,
              timezone: next.requestPlan!.timezone, replayStartLocal: latest.replayStartLocal,
              replayEndLocal: latest.replayEndLocal, replayStartUtc: next.requestPlan!.replayStartUtc,
              replayEndUtc: next.requestPlan!.replayEndUtc, requestStartUtc: latest.requestStartUtc,
              requestEndUtc: latest.requestEndUtc, contextMinutes: next.requestPlan!.contextMinutes, days: 1,
            },
            contract: latest.contract, estimates, costs: next.costs, downloadStarted: false,
            message: t("planner.restoredEstimate"),
          };
          setResult(restored); setSelected(estimates[0]); setStale(false);
        }
      }
    }
  }
  useEffect(() => { refreshStatus(true).catch((reason: Error) => setError(reason.message)); }, []);
  useEffect(() => {
    const sequence = ++previewSequence.current;
    const timer = window.setTimeout(() => {
      marketApi.previewPlan(form).then((next) => {
        if (sequence !== previewSequence.current) return;
        setPreview(next.requestPlan); setError(null);
      }).catch((reason: Error) => {
        if (sequence !== previewSequence.current) return;
        setPreview(null); setError(reason.message);
      });
    }, 120);
    return () => window.clearTimeout(timer);
  }, [form]);
  useEffect(() => {
    if (!activeJob || !["PENDING", "RUNNING"].includes(activeJob.status)) return;
    let cancelled = false;
    const poll = async (): Promise<void> => {
      try {
        const next = await marketApi.estimateJob(activeJob.id);
        if (cancelled) return;
        setActiveJob(next);
        if (next.status === "COMPLETED" && next.result) {
          setResult(next.result); setSelected(next.result.estimates[0]); setBusy(null); setEstimatingPlan(null); setStale(false); setError(null);
          await refreshStatus(false);
        } else if (["FAILED", "EXPIRED", "CANCELLED"].includes(next.status)) {
          setBusy(null); setEstimatingPlan(null); setError(next.error?.message ?? t(`planner.job${next.status[0]}${next.status.slice(1).toLowerCase()}`));
        }
      } catch (reason) {
        if (!cancelled) setError(reason instanceof Error ? reason.message : t("planner.jobFailed"));
      }
    };
    const timer = window.setInterval(poll, 750);
    poll();
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [activeJob?.id, activeJob?.status]);

  function changeForm(values: Partial<typeof form>): void {
    setForm((current) => ({ ...current, ...values }));
    setResult(null); setSelected(undefined); setReview(null); setStale(true); setError(null);
  }

  async function run(kind: "estimate" | "optimize"): Promise<void> {
    if (!preview) return;
    setBusy(kind); setError(null); setReview(null); setResult(null); setSelected(undefined); setStale(false); setEstimatingPlan(preview);
    try {
      const job = await marketApi.createEstimateJob(form, kind);
      setActiveJob(job);
      if (job.status === "COMPLETED" && job.result) {
        setResult(job.result); setSelected(job.result.estimates[0]); setBusy(null); setEstimatingPlan(null);
      }
    } catch (reason) { setError(reason instanceof Error ? reason.message : t("planner.jobFailed")); }
  }
  async function retryActiveJob(): Promise<void> {
    if (!activeJob) return;
    setError(null); setResult(null); setSelected(undefined); setBusy(activeJob.jobKind === "range" ? null : activeJob.jobKind); setEstimatingPlan(preview);
    const next = await marketApi.retryEstimateJob(activeJob.id);
    setActiveJob(next);
  }
  async function cancelActiveJob(): Promise<void> {
    if (!activeJob) return;
    const next = await marketApi.cancelEstimateJob(activeJob.id);
    setActiveJob(next); setBusy(null); setEstimatingPlan(null);
  }
  async function openReview(estimate: PlannerEstimate): Promise<void> {
    try { setReview(await marketApi.purchaseReview(estimate.estimateId)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : t("planner.reviewFailed")); }
  }
  async function authorizationSubmitted(next: AuthorizationResult): Promise<void> {
    await refreshStatus(false);
    setToast(next.authorization.executionMode === "dry_run" ? t("planner.dryRunAuthorized") : t("planner.authorizationQueued"));
    window.setTimeout(() => setToast(null), 5000);
  }
  async function cancelDownloadJob(job: PlannerDownloadJob): Promise<void> {
    try {
      await marketApi.cancelAuthorizationJob(job.id);
      await refreshStatus(false);
      setToast(t("planner.authorizationCancelled"));
    } catch (reason) {
      setError(authorizationError(reason, t));
    }
  }
  async function refreshDownloadJobs(): Promise<void> {
    try {
      await marketApi.refreshDownloadJobs();
      await refreshStatus(false);
      setToast(t("planner.jobsRefreshed"));
    } catch (reason) { setError(authorizationError(reason, t)); }
  }
  async function retryDownloadJob(job: PlannerDownloadJob): Promise<void> {
    if (!job.authorizationId) return;
    try {
      await marketApi.retryAuthorization(job.authorizationId);
      await refreshStatus(false);
      setToast(t("planner.retryQueued"));
    } catch (reason) { setError(authorizationError(reason, t)); }
  }
  async function downloadJob(job: PlannerDownloadJob): Promise<void> {
    try {
      await marketApi.downloadJob(job.id);
      await refreshStatus(false);
      setToast(t("planner.downloadCompleted"));
    } catch (reason) { setError(authorizationError(reason, t)); }
  }

  return <div className="planner-view">
    <header className="planner-title"><div><span>{t("planner.subtitle")}</span><h1>{t("planner.title")}</h1></div><div className="planner-security"><ShieldCheck /><span>{t("planner.estimateFirst")}</span><i /><span>{t("planner.explicitAuthorization")}</span><i /><span>{t("planner.atomicImport")}</span></div></header>
    {toast ? <div className="planner-toast" role="status"><Check />{toast}</div> : null}
    <nav className="planner-steps" aria-label={t("planner.stepsLabel")}>{["scope", "time", "compare", "authorize", "validate"].map((item, index) => <div key={item} className={result ? index <= 2 ? "done" : index === 3 ? "current" : "" : index === 0 ? "current" : ""}><b>{index + 1}</b><span>{t(`planner.step.${item}`)}</span></div>)}</nav>
    <RangePlannerPanel savedPlans={status.rangePlans} onGlobalRefresh={() => refreshStatus(false)} />
    <section className="planner-input data-panel"><header className="panel-heading"><span>{t("planner.scope")}</span><span>{t("planner.singleDay")}</span></header><div className="planner-form">
      <label><span>{t("planner.market")}</span><select value={form.market} onChange={(event) => changeForm({ market: event.target.value })}><option>MES</option></select></label>
      <label><span>{t("planner.dataset")}</span><input value={form.dataset} readOnly /></label><label><span>{t("planner.symbol")}</span><input value={form.symbol} readOnly /></label>
      <label><span>{t("planner.date")}</span><input type="date" max={new Date().toISOString().slice(0, 10)} value={form.date} onChange={(event) => changeForm({ date: event.target.value })} /></label>
      <label><span>{t("planner.timezone")}</span><select value={form.timezone} onChange={(event) => changeForm({ timezone: event.target.value })}><option>Europe/Berlin</option></select></label>
      <label><span>{t("planner.replayStart")}</span><input type="time" value={form.replayStart} onChange={(event) => changeForm({ replayStart: event.target.value })} /></label>
      <label><span>{t("planner.replayEnd")}</span><input type="time" value={form.replayEnd} onChange={(event) => changeForm({ replayEnd: event.target.value })} /></label>
      <label><span>{t("planner.context")}</span><input type="number" min="0" max="1440" step="10" value={form.contextMinutes} onChange={(event) => changeForm({ contextMinutes: Number(event.target.value) })} /></label>
      <div className="planner-actions"><button className="secondary-button" disabled={busy !== null || !preview} onClick={() => run("optimize")}><Gauge />{busy === "optimize" ? t("planner.estimating") : t("planner.optimize")}</button><button className="command-button" disabled={busy !== null || !preview} onClick={() => run("estimate")}><Database />{busy === "estimate" ? t("planner.estimating") : t("planner.compare")}</button></div>
    </div>{preview ? <div className="time-conversion"><span>{t("planner.berlin")} <b className="mono">{preview.replayStartLocal} → {preview.replayEndLocal}</b></span><ChevronRight /><span>UTC <b className="mono">{preview.replayStartUtc.slice(11, 16)} → {preview.replayEndUtc.slice(11, 16)}</b></span><span>{t("planner.economyRequest")} <b className="mono">{preview.requestStartUtc.slice(11, 16)} → {preview.requestEndUtc.slice(11, 16)} UTC</b></span>{result ? <span className="contract-resolved"><FileCheck2 />{result.contract.rawSymbol} · ID {result.contract.instrumentId}</span> : null}</div> : null}</section>
    {error ? <div className="inline-alert bad"><AlertTriangle />{error}{activeJob && ["FAILED", "EXPIRED", "CANCELLED"].includes(activeJob.status) ? <button className="row-action" onClick={retryActiveJob}>{t("common.retry")}</button> : null}</div> : null}
    {stale ? <div className="inline-alert stale"><RefreshCw />{t("planner.stale")}</div> : null}
    {activeJob ? <section className={`estimate-job-state job-${activeJob.status.toLowerCase()}`}><RefreshCw /><div><strong>{t(`planner.job${activeJob.status[0]}${activeJob.status.slice(1).toLowerCase()}`)}</strong><span>{t("planner.jobPersistence")}</span></div><dl><div><dt>{t("planner.createdAt")}</dt><dd>{dateTime(activeJob.createdAt)}</dd></div><div><dt>{t("planner.expiresAt")}</dt><dd>{dateTime(activeJob.expiresAt)}</dd></div></dl>{["PENDING", "RUNNING"].includes(activeJob.status) ? <button className="secondary-button" onClick={cancelActiveJob}>{t("common.cancel")}</button> : null}</section> : null}
    {busy && estimatingPlan ? <div className="estimating-state"><RefreshCw /><span><strong>{t("planner.estimating")}</strong><b className="mono">{estimatingPlan.replayStartLocal}–{estimatingPlan.replayEndLocal} Berlin · {estimatingPlan.replayStartUtc.slice(11, 16)}–{estimatingPlan.replayEndUtc.slice(11, 16)} UTC</b></span></div> : null}
    <Ledger costs={status.costs} />
    {result && !stale ? <><ModeTable estimates={result.estimates} selected={selected} onSelect={setSelected} onReview={openReview} /><EstimateInspector estimate={selected} /></> : !busy ? <section className="planner-empty"><Database /><strong>{stale ? t("planner.stale") : t("planner.noEstimate")}</strong><span>{t("planner.estimateOnly")}</span></section> : null}
    <DownloadJobs jobs={status.jobs} onCancel={cancelDownloadJob} onRefresh={refreshDownloadJobs} onRetry={retryDownloadJob} onDownload={downloadJob} />
    <SessionLibrary sessions={status.sessions} />
    {review ? <PurchaseModal review={review} onClose={() => setReview(null)} onSubmitted={authorizationSubmitted} onReestimate={() => { setReview(null); run("estimate"); }} /> : null}
  </div>;
}

function PhaseBand({ phases }: { phases: ProtocolStatus["phases"] }): React.ReactElement {
  const { t } = useI18n();
  return <section className="phase-band">{phases.map((phase) => { const progress = Math.min(100, phase.complete / Math.max(phase.target, 1) * 100); return <div key={phase.mode}><span>{t(`backtest.phase.${phase.mode}`)}</span><strong className="mono">{phase.complete} / {phase.target}</strong><i><b style={{ width: `${progress}%` }} /></i></div>; })}</section>;
}

function PlanSummary({ plan }: { plan?: BacktestPlan | null }): React.ReactElement {
  const { t } = useI18n();
  if (!plan) return <div className="no-plan"><LockKeyhole /><span>{t("backtest.noActivePlan")}</span></div>;
  return <div className="active-plan"><div><span>{t("backtest.activeProtocol")}</span><strong>{plan.strategy}</strong></div><StatusTag tone={plan.mode === "locked" ? "warn" : "cyan"}>{t(`backtest.phase.${plan.mode}`)}</StatusTag><div><span>{t("research.strategyHash")}</span><b className="mono" title={plan.strategy_hash}>{plan.strategy_hash.slice(0, 18)}…</b></div></div>;
}

export function BacktestPlanView({ state, onState, lockState }: { state: ReplayState; onState: (state: ReplayState) => void; lockState: ApplicationLockState }): React.ReactElement {
  const { t, locale } = useI18n();
  const [status, setStatus] = useState<ProtocolStatus | null>(null);
  const [strategy, setStrategy] = useState("MES Pullback / Retest");
  const [fill, setFill] = useState<Record<string, string | number>>({});
  const [startingBalance, setStartingBalance] = useState(50_000);
  const [riskPerTrade, setRiskPerTrade] = useState(75);
  const [maximumTradesPerDay, setMaximumTradesPerDay] = useState(3);
  const [uiPracticeOnly, setUiPracticeOnly] = useState(true);
  const [showArchived, setShowArchived] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [trade, setTrade] = useState({ direction: "long", entry: 0, stop: 0, target: 0, contracts: 1 });

  async function load(planId?: string): Promise<void> {
    const next = await marketApi.protocolStatus(planId);
    setStatus(next);
    if (!Object.keys(fill).length) setFill(next.defaults.fill);
  }
  async function loadAfterMutation(planId?: string): Promise<void> {
    try {
      await load(planId);
    } catch {
      await new Promise((resolve) => window.setTimeout(resolve, 180));
      await load(planId);
    }
  }
  useEffect(() => { load().catch((reason: Error) => setError(reason.message)); }, []);
  useEffect(() => {
    const decision = state.decision;
    if (!decision) return;
    const entry = decision.entryZone ? (decision.entryZone.min + decision.entryZone.max) / 2 : 0;
    setTrade((current) => ({ ...current, direction: decision.direction ?? "long", entry, stop: decision.invalidation ?? 0, target: decision.targets?.[0] ?? 0 }));
  }, [state.decision?.timestamp]);

  const active = status?.activePlan;
  const report = status?.report ?? {};
  const activeSession = active?.session_ids[0];
  const activeRun = status?.currentRun;
  const openTrade = status?.trades.find((item) => item.status === "OPEN");
  const metrics: Array<[string, string]> = [
    [t("common.trades"), String(report.trades ?? 0)], [t("backtest.winRate"), `${report.winRate ?? 0}%`], ["Expectancy R", String(report.expectancyR ?? 0)],
    ["Expectancy USD", usd(Number(report.expectancyUsd ?? 0), locale)], [t("research.profitFactor"), String(report.profitFactor ?? "–")],
    [t("research.maxDrawdown"), usd(Number(report.maximumDrawdown ?? 0), locale)], [t("backtest.fees"), usd(Number(report.fees ?? 0), locale)],
    [t("backtest.slippage"), usd(Number(report.slippage ?? 0), locale)], [t("backtest.netResult"), usd(Number(report.netResult ?? 0), locale)],
  ];

  async function create(mode: "practice" | "pilot" | "locked"): Promise<void> {
    setBusy(`create-${mode}`); setError(null);
    try {
      const plan = await marketApi.createPlan({ strategy, instrument: "MES", sessionIds: [], mode, fill, startingBalance, riskPerTrade, maximumTradesPerDay, requireFullL3: true });
      onState(await marketApi.replayState());
      await loadAfterMutation(plan.id);
    } catch (reason) { setError(reason instanceof Error ? reason.message : t("backtest.errorCreate")); }
    finally { setBusy(null); }
  }
  async function assign(sessionId: string): Promise<void> {
    if (!active) return;
    setBusy(`assign-${sessionId}`); setError(null);
    try { await marketApi.assignPlanSession(active.id, sessionId); await loadAfterMutation(active.id); }
    catch (reason) { setError(reason instanceof Error ? reason.message : t("backtest.errorAssign")); }
    finally { setBusy(null); }
  }
  async function clone(sessionId: string): Promise<void> {
    if (!active) return;
    setBusy(`clone-${sessionId}`); setError(null);
    try { await marketApi.clonePlanSession(active.id, sessionId, uiPracticeOnly); await loadAfterMutation(active.id); }
    catch (reason) { setError(reason instanceof Error ? reason.message : t("backtest.errorClone")); }
    finally { setBusy(null); }
  }
  async function start(): Promise<void> {
    if (!active || !activeSession) return;
    setBusy("start"); setError(null);
    try { onState(await marketApi.startBlind(active.id, activeSession)); await loadAfterMutation(active.id); }
    catch (reason) { setError(reason instanceof Error ? reason.message : t("backtest.errorStart")); }
    finally { setBusy(null); }
  }
  async function exitRun(): Promise<void> {
    setBusy("exit"); setError(null);
    try { const result = await marketApi.exitBacktestRun(); onState(result.state); await loadAfterMutation(active?.id); }
    catch (reason) { setError(reason instanceof Error ? reason.message : t("backtest.errorExit")); }
    finally { setBusy(null); }
  }
  async function scan(): Promise<void> {
    if (!activeSession) return;
    setBusy("scan"); setError(null);
    try { await marketApi.scanSession(activeSession, active?.id); await loadAfterMutation(active?.id); }
    catch (reason) { setError(reason instanceof Error ? reason.message : t("backtest.errorScan")); }
    finally { setBusy(null); }
  }
  async function saveTrade(): Promise<void> {
    setBusy("trade"); setError(null);
    try { await marketApi.recordBlindTrade({ ...trade, targets: [trade.target] }); await loadAfterMutation(active?.id); }
    catch (reason) { setError(reason instanceof Error ? reason.message : t("backtest.errorTradePlan")); }
    finally { setBusy(null); }
  }
  async function candidateJump(candidateId: number): Promise<void> {
    setBusy(`jump-${candidateId}`); setError(null);
    try { onState(await marketApi.jumpToCandidate(candidateId)); await loadAfterMutation(active?.id); }
    catch (reason) { setError(reason instanceof Error ? reason.message : t("backtest.errorJump")); }
    finally { setBusy(null); }
  }

  const candidates = (status?.candidates ?? []).filter((candidate) => candidate.planId === active?.id);
  const scanEvent = status?.audit.find((event) => event.eventType === "CANDIDATE_SCAN_COMPLETED");
  const candidateCounts = (scanEvent?.payload.counts ?? {}) as Record<string, number>;
  const runLabel = activeRun?.status === "ACTIVE" ? t(`backtest.phase.${activeRun.mode}`) : t("backtest.notStarted");

  return <div className="backtest-planner">
    <header className="planner-title"><div><span>{t("backtest.subtitle")}</span><h1>{t("backtest.title")}</h1></div><PlanSummary plan={active} /></header>
    <section className="plan-lifecycle-actions data-panel"><button className="command-button" disabled={busy !== null} onClick={() => create("practice")}><Play />{busy === "create-practice" ? t("backtest.creating") : t("backtest.newPractice")}</button><button className="secondary-button" disabled={busy !== null} onClick={() => create("pilot")}><Gauge />{t("backtest.newPilot")}</button><button className="secondary-button" disabled={busy !== null} onClick={() => create("locked")}><LockKeyhole />{t("backtest.newLocked")}</button><button className="secondary-button" onClick={() => setShowArchived((current) => !current)}><Database />{t("backtest.viewArchived")}</button></section>
    <PhaseBand phases={status?.phases ?? [{ mode: "practice", label: "Practice", complete: 0, target: 10 }, { mode: "pilot", label: "Pilot", complete: 0, target: 30 }, { mode: "locked", label: "Locked Test", complete: 0, target: 100 }, { mode: "forward", label: "Forward Paper", complete: 0, target: 20 }]} />
    {error ? <div className="inline-alert bad"><AlertTriangle />{error}</div> : null}
    <section className="active-plan-overview data-panel"><header className="panel-heading"><span>{t("backtest.activePlan")}</span><StatusTag tone={active?.mode === "locked" ? "warn" : active ? "cyan" : "neutral"}>{active ? statusText(t, active.status) : t("common.none")}</StatusTag></header><div className="plan-overview-grid"><div><span>{t("backtest.plan")}</span><strong>{active ? `${active.strategy} · ${t(`backtest.phase.${active.mode}`)}` : t("backtest.noUserProtocol")}</strong></div><div><span>{t("planner.mode")}</span><strong>{active ? t(`backtest.phase.${active.mode}`) : "–"}</strong></div><div><span>{t("common.status")}</span><strong>{active ? statusText(t, active.status) : "–"}</strong></div><div><span>{t("nav.settings")}</span><strong className={lockState.locked ? "negative" : "positive"}>{lockState.locked ? t("backtest.lockedByRun") : t("common.editable")}</strong></div><div><span>{t("backtest.blindReplay")}</span><strong>{runLabel}</strong></div><div><span>{t("backtest.assignedSessions")}</span><strong>{active?.assignments.length ?? 0}</strong></div><div><span>{t("backtest.progress")}</span><strong>{t("backtest.recordedTrades", { count: String(report.trades ?? 0) })}</strong></div><div><span>{t("research.strategyHash")}</span><strong className="mono">{active?.strategy_hash.slice(0, 18) ?? "–"}{active ? "…" : ""}</strong></div></div>{lockState.locked ? <div className="active-lock-callout"><AlertTriangle /><span><strong>{t("backtest.whyLocked")}</strong> {t("backtest.lockExplanation")}</span><button className="secondary-button" disabled={busy !== null} onClick={exitRun}>{t("backtest.exitLocked")}</button></div> : null}</section>
    {showArchived ? <section className="archived-plans data-panel"><header className="panel-heading"><span>{t("backtest.archivedPlans")}</span><span>{t("backtest.preserved", { count: status?.archivedPlans.length ?? 0 })}</span></header>{status?.archivedPlans.map((plan) => <button key={plan.id} onClick={() => load(plan.id)}><span><strong>{plan.strategy} · {t(`backtest.phase.${plan.mode}`)}</strong><small>{t(`backtest.artifact.${plan.artifact_kind}`)} · {plan.created_at}</small></span><b className="mono">{plan.strategy_hash.slice(0, 18)}…</b></button>)}{!status?.archivedPlans.length ? <p className="empty-copy">{t("backtest.noArchived")}</p> : null}</section> : null}
    <section className="backtest-builder">
      <div className="data-panel plan-config"><header className="panel-heading"><span>{t("backtest.nextConfig")}</span><StatusTag tone={lockState.locked ? "warn" : "cyan"}>{lockState.locked ? t("backtest.boundToRun") : t("common.editable")}</StatusTag></header><div className="planner-form backtest-form"><label className="span-two"><span>{t("common.strategy")}</span><input disabled={lockState.locked} value={strategy} onChange={(event) => setStrategy(event.target.value)} /></label><label><span>{t("backtest.startingBalance")}</span><input disabled={lockState.locked} type="number" value={startingBalance} onChange={(event) => setStartingBalance(Number(event.target.value))} /></label><label><span>{t("backtest.riskPerTrade")}</span><input disabled={lockState.locked} type="number" value={riskPerTrade} onChange={(event) => setRiskPerTrade(Number(event.target.value))} /></label><label><span>{t("backtest.maxTradesDay")}</span><input disabled={lockState.locked} type="number" value={maximumTradesPerDay} onChange={(event) => setMaximumTradesPerDay(Number(event.target.value))} /></label>{["commissionPerSide", "exchangeClearingPerSide", "slippageEntryTicks", "slippageExitTicks", "stopSlippageTicks", "maximumPosition"].map((key) => <label key={key}><span>{t(`backtest.fill.${key}`)}</span><input disabled={lockState.locked} type="number" step="0.05" value={String(fill[key] ?? 0)} onChange={(event) => setFill({ ...fill, [key]: Number(event.target.value) })} /></label>)}</div></div>
      <div className="data-panel session-assignment"><header className="panel-heading"><span>{t("backtest.assignedSessions")}</span><span>{t("backtest.assignedCount", { count: active?.assignments.length ?? 0 })}</span></header><div>{status?.sessions.map((session) => { const assignment = active?.assignments.find((item) => item.session_id === session.id); const eligibility = session.eligibility; const blocked = !assignment && !eligibility?.selectable && !eligibility?.canClone; return <div key={session.id} className={`session-assignment-row ${blocked ? "disabled-session" : ""}`}><span><strong>{session.contract_symbol} · {session.start_at.slice(0, 10)}</strong><small>{statusText(t, session.completeness)} · {integer(session.record_count, locale)} {t("common.records")} · {t("backtest.sourceSplit")} {t(`split.${session.split.split_name}`)}</small>{assignment?.contaminated ? <small className="warning">{t("backtest.contaminatedCopy")}</small> : null}{!assignment && eligibility ? <small className="session-lock-reason" title={`${t(eligibility.detailKey)} ${t(eligibility.nextActionKey)}`}>{t(eligibility.detailKey)} <b>{t(eligibility.nextActionKey)}</b></small> : null}</span><StatusTag tone={assignment ? assignment.contaminated ? "warn" : "ok" : eligibility?.selectable ? "ok" : "warn"}>{assignment ? t("backtest.assigned") : eligibility?.selectable ? t("common.available").toUpperCase() : t("common.unavailable").toUpperCase()}</StatusTag>{!assignment && active && eligibility?.canClone ? <div className="clone-actions"><label><input type="checkbox" checked={uiPracticeOnly} onChange={(event) => setUiPracticeOnly(event.target.checked)} />{t("backtest.uiPracticeOnly")}</label><button className="row-action" disabled={busy !== null} onClick={() => clone(session.id)}>{t(eligibility.nextActionKey)}</button></div> : null}{!assignment && active && eligibility?.selectable ? <button className="row-action" disabled={busy !== null} onClick={() => assign(session.id)}>{t(eligibility.nextActionKey)}</button> : null}</div>; })}</div>{!active ? <p className="assignment-help">{t("session.createPlanFirst")}</p> : null}</div>
    </section>
    <section className="protocol-command data-panel"><div><span>{t("backtest.currentRun")}</span><strong>{runLabel}</strong><small>{activeRun ? `Run ${activeRun.id} · ${t("common.session")} ${activeRun.session_id}` : active ? t("backtest.planReady") : t("backtest.createAssignFirst")}</small></div><div className="protocol-actions"><button className="secondary-button" disabled={!activeSession || busy !== null} onClick={scan}><ScanSearch />{busy === "scan" ? t("backtest.scanning") : t("backtest.candidateScan")}</button>{activeRun ? <button className="secondary-button" disabled={busy !== null} onClick={exitRun}>{t("backtest.exitRun")}</button> : null}<button className="command-button" disabled={!active || !activeSession || busy !== null} onClick={start}><Play />{busy === "start" ? t("backtest.loading") : t("backtest.startReplay", { mode: active ? t(`backtest.phase.${active.mode}`) : "" })}</button></div></section>
    {state.blind?.status === "ACTIVE" && state.blind.planId === active?.id ? <section className="data-panel trade-capture"><header className="panel-heading"><span>{t("backtest.precommit")}</span><span>{state.blind?.pendingTradePlan ? t("backtest.requiredBeforeContinue") : t("backtest.decisionSnapshot")}</span></header><div className="planner-form"><label><span>{t("journal.direction")}</span><select value={trade.direction} onChange={(event) => setTrade({ ...trade, direction: event.target.value })}><option value="long">Long</option><option value="short">Short</option></select></label><label><span>Entry</span><input type="number" step="0.25" value={trade.entry} onChange={(event) => setTrade({ ...trade, entry: Number(event.target.value) })} /></label><label><span>Stop</span><input type="number" step="0.25" value={trade.stop} onChange={(event) => setTrade({ ...trade, stop: Number(event.target.value) })} /></label><label><span>Target</span><input type="number" step="0.25" value={trade.target} onChange={(event) => setTrade({ ...trade, target: Number(event.target.value) })} /></label><label><span>{t("signal.contracts")}</span><input type="number" min="1" value={trade.contracts} onChange={(event) => setTrade({ ...trade, contracts: Number(event.target.value) })} /></label><div className="planner-actions"><button className="command-button" disabled={busy !== null || Boolean(openTrade)} onClick={saveTrade}><FileCheck2 />{busy === "trade" ? t("common.saving") : t("backtest.commitTradePlan")}</button></div></div></section> : null}
    {candidates.length ? <section className="data-panel candidate-results"><header className="panel-heading"><span>{t("backtest.candidateScan")} · ReplayEngine</span><span>{t("setup.tradeSetup")} {candidateCounts.trade_ready ?? 0} · {t("signal.wait")} {candidateCounts.wait ?? 0} · {t("risk.blocked")} {candidateCounts.blocked ?? 0}</span></header><div className="table-scroll"><table className="terminal-table"><thead><tr><th>{t("backtest.timestamp")}</th><th>{t("research.decision")}</th><th>{t("journal.direction")}</th><th>{t("signal.confidence")}</th><th>{t("common.data")}</th><th>{t("backtest.reasons")}</th><th /></tr></thead><tbody>{candidates.slice(0, 12).map((candidate) => <tr key={String(candidate.id)}><td>{String(candidate.timestamp).slice(11, 23)}</td><td className={candidate.decision === "trade_ready" ? "positive" : candidate.decision === "blocked" ? "negative" : "warning"}>{t(`backtest.decision.${String(candidate.decision)}`)}</td><td>{String(candidate.direction ?? "–").toUpperCase()}</td><td>{String(candidate.confidence)}%</td><td>{t(`backtest.dataQuality.${String(candidate.dataQuality)}`)}</td><td>{Array.isArray(candidate.reasons) ? candidate.reasons.slice(0, 2).map((code) => t(`backtest.reason.${String(code)}`)).join(" · ") : "–"}</td><td><button className="row-action" disabled={busy !== null} onClick={() => candidateJump(Number(candidate.id))}>{t("backtest.blindJump")} <ChevronRight /></button></td></tr>)}</tbody></table></div><p className="candidate-disclaimer">{t("backtest.candidateDisclaimer")}</p></section> : null}
    <section className="backtest-evidence"><div className="data-panel report-panel"><header className="panel-heading"><span>{t("backtest.conservativeReport")}</span><StatusTag tone={report.assessment === "Negative expectancy" ? "bad" : "warn"}>{report.assessment === "Negative expectancy" ? t("backtest.negativeExpectancy") : t("backtest.insufficientSample")}</StatusTag></header><div className="report-metrics">{metrics.map(([label, value]) => <div key={label}><span>{label}</span><strong className="mono">{String(value ?? "–")}</strong></div>)}</div><div className="report-warning"><AlertTriangle /><span>{t("backtest.oosRequired")} {t("research.noProfitClaim")}</span></div></div><div className="data-panel audit-panel"><header className="panel-heading"><span>{t("backtest.auditLog")}</span><button className="icon-button" title={t("common.refresh")} onClick={() => load(active?.id)}><RefreshCw /></button></header><div className="audit-list">{status?.audit.slice(0, 12).map((event) => <div key={event.id}><time className="mono">{event.createdAt.slice(11, 19)}</time><strong>{t(`audit.${event.eventType}`)}</strong><small>{event.sessionId?.slice(0, 8) ?? t("backtest.plan")}</small></div>)}{!status?.audit.length ? <span className="empty-copy">{t("backtest.noEvents")}</span> : null}</div></div></section>
  </div>;
}
