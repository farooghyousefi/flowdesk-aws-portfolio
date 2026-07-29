"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Ban, Beaker, Check, Database, FlaskConical, Gauge, Pause, Play, RefreshCw, ShieldCheck, X } from "lucide-react";
import { marketApi } from "./api";
import { statusText, useI18n } from "./i18n";
import { resolveResearchSessionId } from "./research-selection";
import type { ResearchExperiment, ResearchStatus, SessionRecord } from "./types";

type Tab = "datasets" | "experiments" | "candidates" | "models" | "signals";

function percent(value: number): string {
  return `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`;
}

function shortHash(value: unknown): string {
  const text = String(value ?? "");
  return text ? `${text.slice(0, 14)}…` : "–";
}

function ExperimentMetrics({ experiment }: { experiment: ResearchExperiment }): React.ReactElement {
  const { t, number } = useI18n();
  const metrics = experiment.metrics;
  return <div className="research-metrics">
    <div><span>{t("common.trades")}</span><strong>{number(Number(metrics.trades ?? 0))}</strong></div>
    <div><span>{t("research.netExpectancy")}</span><strong>{number(Number(metrics.netExpectancyUsd ?? 0), { style: "currency", currency: "USD" })}</strong></div>
    <div><span>{t("research.profitFactor")}</span><strong>{metrics.profitFactor == null ? "–" : number(Number(metrics.profitFactor), { maximumFractionDigits: 2 })}</strong></div>
    <div><span>{t("research.maxDrawdown")}</span><strong>{number(Number(metrics.maximumDrawdownUsd ?? 0), { style: "currency", currency: "USD" })}</strong></div>
    <div><span>{t("research.sessions")}</span><strong>{number(Number(metrics.independentSessions ?? 0))}</strong></div>
  </div>;
}

export function ResearchLabView({ sessions }: { sessions: SessionRecord[] }): React.ReactElement {
  const { t, number, dateTime } = useI18n();
  const [status, setStatus] = useState<ResearchStatus | null>(null);
  const [tab, setTab] = useState<Tab>("datasets");
  const [sessionId, setSessionId] = useState(resolveResearchSessionId(sessions, ""));
  const [fillMode, setFillMode] = useState<"optimistic" | "realistic" | "stressed">("realistic");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const loadInFlight = useRef<Promise<void> | null>(null);

  useEffect(() => {
    const resolved = resolveResearchSessionId(sessions, sessionId);
    if (resolved !== sessionId) setSessionId(resolved);
  }, [sessionId, sessions]);

  const load = useCallback(async (): Promise<void> => {
    if (loadInFlight.current) return loadInFlight.current;
    const operation = marketApi.researchStatus()
      .then((nextStatus) => { setStatus(nextStatus); setError(null); })
      .finally(() => {
        if (loadInFlight.current === operation) loadInFlight.current = null;
      });
    loadInFlight.current = operation;
    return operation;
  }, []);

  useEffect(() => { load().catch((reason: Error) => setError(reason.message)); }, [load]);
  const activeJobs = useMemo(() => status?.jobs.filter((job) => ["QUEUED", "RUNNING"].includes(job.status)) ?? [], [status]);
  const visibleJobs = useMemo(() => status?.jobs.filter((job) => ["QUEUED", "RUNNING", "PAUSED"].includes(job.status)) ?? [], [status]);
  const activeJobKey = activeJobs.map((job) => job.id).sort().join(":");
  useEffect(() => {
    if (!activeJobKey) return;
    let cancelled = false;
    let timer: number | undefined;
    const poll = async (): Promise<void> => {
      try { await load(); }
      catch (reason) { setError(reason instanceof Error ? reason.message : "Research status refresh failed."); }
      if (!cancelled) timer = window.setTimeout(poll, 1500);
    };
    timer = window.setTimeout(poll, 1000);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [activeJobKey, load]);

  async function start(): Promise<void> {
    if (!sessionId) return;
    setBusy(true); setError(null);
    try {
      await marketApi.createResearchJob({ sessionId, fillMode, mode: "search", strategy: "MES L3 Strategy Search", seed: 7 });
      setTab("experiments"); await load();
    } catch (reason) { setError(reason instanceof Error ? reason.message : t("common.failed")); }
    finally { setBusy(false); }
  }

  async function cancel(jobId: string): Promise<void> {
    setBusy(true); setError(null);
    try { await marketApi.cancelResearchJob(jobId); await load(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : t("research.operationFailed")); }
    finally { setBusy(false); }
  }

  async function pause(jobId: string): Promise<void> {
    setBusy(true); setError(null);
    try { await marketApi.pauseResearchJob(jobId); await load(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : t("research.operationFailed")); }
    finally { setBusy(false); }
  }

  async function resume(jobId: string): Promise<void> {
    setBusy(true); setError(null);
    try { await marketApi.resumeResearchJob(jobId); await load(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : t("research.operationFailed")); }
    finally { setBusy(false); }
  }

  async function strategyAction(strategyHash: string, action: "promote" | "reject" | "rollback"): Promise<void> {
    setBusy(true); setError(null);
    try { await marketApi.strategyAction(strategyHash, action); await load(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : t("research.operationFailed")); }
    finally { setBusy(false); }
  }

  const tabs: Array<[Tab, string]> = [
    ["datasets", "research.datasets"], ["experiments", "research.experiments"], ["candidates", "research.candidates"],
    ["models", "research.models"], ["signals", "research.signals"],
  ];
  const experiments = status?.experiments ?? [];
  const searchCandidates = useMemo(() => {
    const candidates = experiments.filter((experiment) => experiment.config?.mode === "search-candidate");
    const source = candidates.length ? candidates : experiments;
    const seen = new Set<string>();
    return source.filter((experiment) => {
      if (seen.has(experiment.strategy_hash)) return false;
      seen.add(experiment.strategy_hash);
      return true;
    });
  }, [experiments]);

  function parameterSummary(experiment: ResearchExperiment): string {
    const parameters = (experiment.config?.parameters ?? {}) as Record<string, unknown>;
    if (!Object.keys(parameters).length) return "";
    const family = String(experiment.config?.family ?? parameters.family ?? "");
    const extras: string[] = [];
    if (parameters.vwapDistanceTicks) extras.push(`VWAP ${String(parameters.vwapDistanceTicks)}t`);
    if (parameters.minimumTrendStrength) extras.push(`Trend ${String(parameters.minimumTrendStrength)}`);
    if (parameters.openingRangeBufferTicks) extras.push(`OR ${String(parameters.openingRangeBufferTicks)}t`);
    if (parameters.absorptionConfidence) extras.push(`Abs ${String(parameters.absorptionConfidence)}`);
    return `${family} · SV ${String(parameters.signedVolumeThreshold ?? "–")} · DM ${String(parameters.deltaMomentumThreshold ?? "–")} · QI ${String(parameters.queueImbalanceThreshold ?? "–")} · Stop ${String(parameters.stopTicks ?? "–")} · Target ${String(parameters.targetTicks ?? "–")}${extras.length ? ` · ${extras.join(" · ")}` : ""}`;
  }

  return <div className="research-view">
    <header className="planner-title research-title"><div><span>{t("research.subtitle")}</span><h1>{t("research.title")}</h1></div><div className="research-safety"><ShieldCheck /><span>{t("research.noProfitClaim")}</span></div></header>
    {status?.readiness ? <section className={`research-readiness data-panel ${status.readiness.readyForValidatedSignals ? "ready" : "blocked"}`}><header><div><strong>{t("research.systemPlan")}</strong><span>{status.blueprint?.version ?? "flowdesk-research-blueprint-v1"}</span></div><b>{status.readiness.readyForValidatedSignals ? t("research.validatedReady") : t("research.validationIncomplete")}</b></header><div className="research-readiness-grid"><div><span>{t("research.completeL3Days")}</span><strong>{number(status.readiness.current.independentDates)} / {number(status.readiness.target.independentSessions)}</strong></div><div><span>{t("research.monthCoverage")}</span><strong>{number(status.readiness.current.independentMonths)} / {number(status.readiness.target.months)}</strong></div><div><span>{t("research.calendarEvents")}</span><strong>{number(status.readiness.current.economicEvents)}</strong></div><div><span>{t("research.newsEvents")}</span><strong>{number(status.readiness.current.newsEvents)}</strong></div><div><span>{t("research.strategyFamilies")}</span><strong>{number(status.blueprint?.strategyFamilies?.length ?? 0)}</strong></div><div><span>{t("research.signalMode")}</span><strong>{statusText(t, status.readiness.signalMode)}</strong></div></div>{status.readiness.blockers.length ? <div className="research-blockers"><AlertTriangle />{status.readiness.blockers.map((code) => <span key={code}>{t(`diagnosis.${code}`)}</span>)}</div> : null}</section> : null}
    <section className="research-command data-panel">
      <div><label><span>{t("common.session")}</span><select value={sessionId} onChange={(event) => setSessionId(event.target.value)}>{sessions.map((session) => <option key={session.id} value={session.id}>{session.contract_symbol} · {session.start_at.slice(0, 10)} · {statusText(t, session.completeness)}</option>)}</select></label></div>
      <div className="fill-mode-control"><span>{t("research.fillModel")}</span><div className="segmented-control">{(["optimistic", "realistic", "stressed"] as const).map((mode) => <button key={mode} className={fillMode === mode ? "selected" : ""} onClick={() => setFillMode(mode)}>{t(`research.${mode}`)}</button>)}</div></div>
      <button className="command-button" disabled={busy || !sessionId || activeJobs.length > 0} onClick={start}><Play />{busy ? t("research.running") : t("research.newSearch")}</button>
    </section>
    {error ? <div className="inline-alert bad"><AlertTriangle />{error}</div> : null}
    {visibleJobs.length ? <section className="research-job-strip">{visibleJobs.map((job) => <div key={job.id}>{job.status === "PAUSED" ? <Pause /> : <RefreshCw />}<span><strong>{statusText(t, job.status)}</strong><small>{shortHash(job.id)} · {number(Number(job.checkpoint.eventsProcessed ?? 0))} {t("research.events")}</small></span><i><b style={{ width: percent(job.progress) }} /></i><strong>{percent(job.progress)}</strong><div className="toolbar-actions">{job.status === "PAUSED" ? <button className="icon-button" disabled={busy} title={t("research.resume")} onClick={() => resume(job.id)}><Play /></button> : <button className="icon-button" disabled={busy} title={t("research.pause")} onClick={() => pause(job.id)}><Pause /></button>}<button className="icon-button" disabled={busy} title={t("common.cancel")} onClick={() => cancel(job.id)}><X /></button></div></div>)}</section> : null}
    <nav className="research-tabs" aria-label={t("research.title")}>{tabs.map(([key, label]) => <button key={key} className={tab === key ? "active" : ""} onClick={() => setTab(key)}>{t(label)}</button>)}</nav>

    {tab === "datasets" ? <section className="data-panel research-table"><header className="panel-heading"><span>{t("research.datasets")}</span><span>{status?.datasets.length ?? 0}</span></header><div className="table-scroll"><table className="terminal-table"><thead><tr><th>{t("common.session")}</th><th>{t("common.split")}</th><th>{t("common.data")}</th><th>{t("common.integrity")}</th><th>{t("health.capability")}</th><th>{t("common.records")}</th><th>{t("common.fingerprint")}</th></tr></thead><tbody>{status?.datasets.map((dataset) => <tr key={dataset.id}><td><strong>{dataset.contract_symbol}</strong><small>{dataset.start_at.slice(0, 10)}</small></td><td>{t(`split.${dataset.split.split_name}`)}</td><td>{dataset.schema_name.toUpperCase()} · {statusText(t, dataset.completeness)}</td><td>{statusText(t, dataset.integrity_status)}</td><td><span className={`capability capability-${dataset.data_health.signalCapability.toLowerCase()}`}>{t(`health.capability.${dataset.data_health.signalCapability}`)}</span></td><td>{number(dataset.record_count)}</td><td className="mono" title={dataset.sha256}>{shortHash(dataset.sha256)}</td></tr>)}</tbody></table></div></section> : null}

    {tab === "experiments" ? <section className="data-panel research-table"><header className="panel-heading"><span>{t("research.experiments")}</span><button className="icon-button" title={t("common.refresh")} onClick={() => load()}><RefreshCw /></button></header>{experiments.length ? <div className="experiment-list">{experiments.map((experiment) => { const job = status?.jobs.find((item) => item.experiment_id === experiment.id); return <article key={experiment.id}><header><div><strong>{experiment.name}</strong><span>{experiment.strategy_name} · {experiment.fill_model_version}</span></div><span className={`experiment-status status-${experiment.status.toLowerCase()}`}>{statusText(t, experiment.status)}</span></header><ExperimentMetrics experiment={experiment} /><footer><span className="mono">{t("research.strategyHash")} {shortHash(experiment.strategy_hash)} · {t("research.parameterHash")} {shortHash(experiment.parameter_hash)}</span><span>{dateTime(experiment.created_at)}</span>{job?.error_message ? <span className="negative">{job.error_message}</span> : null}</footer></article>; })}</div> : <div className="research-empty"><Beaker /><strong>{t("research.noResults")}</strong></div>}</section> : null}

    {tab === "candidates" ? <section className="data-panel research-table candidate-table"><header className="panel-heading"><span>{t("research.candidates")}</span><span>{t("research.promotionGates")}</span></header><div className="table-scroll"><table className="terminal-table"><thead><tr><th>{t("common.strategy")}</th><th>{t("common.split")}</th><th>{t("common.trades")}</th><th>{t("research.netExpectancy")}</th><th>{t("research.stability")}</th><th>{t("research.diagnosis")}</th><th>{t("research.validation")}</th><th>{t("common.actions")}</th></tr></thead><tbody>{searchCandidates.map((experiment) => { const eligible = Boolean(experiment.validation?.eligible); const paperStatus = String((experiment.validation as Record<string, unknown>)?.paperStatus ?? experiment.validation?.status ?? "PENDING"); const diagnosis = ((experiment.validation as Record<string, unknown>)?.diagnosis as string[] | undefined) ?? []; return <tr key={experiment.id}><td><strong>{experiment.strategy_name}</strong><small>{parameterSummary(experiment)}</small></td><td>{t(`split.${experiment.split_name}`)}</td><td>{String(experiment.metrics.trades ?? 0)}</td><td>{number(Number(experiment.metrics.netExpectancyUsd ?? 0), { style: "currency", currency: "USD" })}</td><td>{number(Number(experiment.metrics.regimeStability ?? experiment.metrics.parameterStability ?? 0), { style: "percent" })}</td><td><small>{diagnosis.slice(0, 3).map((code) => t(`diagnosis.${code}`)).join(" · ") || t("research.noDiagnosis")}</small></td><td><span className={paperStatus === "PAPER_ACTIVE" || eligible ? "positive" : "warning"}>{statusText(t, paperStatus)}</span>{paperStatus === "PAPER_ACTIVE" ? <small>{t("research.paperSignalReady")}</small> : !eligible ? <small>{(experiment.validation?.failedReasons ?? []).slice(0, 2).map((code) => t(`validation.${code}`)).join(" · ")}</small> : null}</td><td><div className="candidate-actions"><button className="row-action" disabled={busy || !eligible} title={!eligible ? t("research.promoteBlocked") : t("research.promote")} onClick={() => strategyAction(experiment.strategy_hash, "promote")}><Check />{t("research.promote")}</button><button className="row-action" disabled={busy} onClick={() => strategyAction(experiment.strategy_hash, "reject")}><Ban />{t("research.reject")}</button></div></td></tr>; })}</tbody></table></div>{!searchCandidates.length ? <div className="research-empty"><Gauge /><strong>{t("research.noResults")}</strong></div> : null}</section> : null}

    {tab === "models" ? <section className="data-panel research-table"><header className="panel-heading"><span>{t("research.models")}</span><span>{status?.models.length ?? 0}</span></header><div className="registry-list">{status?.models.map((model, index) => <div key={String(model.id ?? index)}><FlaskConical /><span><strong>{String(model.name ?? model.id)}</strong><small>{String(model.model_type ?? t("research.modelType"))} · {t("research.featureVersion")} {String(model.feature_version ?? "–")}</small></span><b>{statusText(t, model.status ?? "PENDING")}</b></div>)}{status?.strategies.map((strategy, index) => <div key={String(strategy.id ?? index)}><Database /><span><strong>{String(strategy.name ?? strategy.id)}</strong><small>{String(strategy.version ?? "–")} · {shortHash(strategy.strategy_hash)}</small></span><b>{statusText(t, strategy.validation_status ?? strategy.status)}</b>{strategy.status === "ACTIVE" ? <button className="row-action" disabled={busy} onClick={() => strategyAction(String(strategy.strategy_hash), "rollback")}><RefreshCw />{t("research.rollback")}</button> : null}</div>)}</div></section> : null}

    {tab === "signals" ? <section className="data-panel research-table"><header className="panel-heading"><span>{t("research.signals")}</span><span>{status?.signals.length ?? 0}</span></header>{status?.signals.length ? <div className="signal-review-list">{status.signals.slice(0, 50).map((signal) => <div key={signal.id}><time>{dateTime(signal.timestamp)}</time><strong className={signal.status === "LONG" ? "positive" : signal.status === "SHORT" || signal.status === "NO_TRADE" ? "negative" : "warning"}>{statusText(t, signal.status)}</strong><span>{signal.payload.setup}</span><span>{signal.payload.confidence}% · {statusText(t, signal.payload.dataQuality)}</span><span>{statusText(t, signal.payload.strategyValidationStatus)}</span></div>)}</div> : <div className="research-empty"><Ban /><strong>{t("research.noResults")}</strong><span>{t("signal.researchOnly")}</span></div>}</section> : null}
    <div className="research-disclaimer"><AlertTriangle /><span>{t("research.noProfitClaim")}</span><Check /><span>{t("common.manualOnly")}</span></div>
  </div>;
}
