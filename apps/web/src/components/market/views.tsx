"use client";

import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, Check, Download, FileUp, Plus, Save, Trash2 } from "lucide-react";
import { MARKET_URL, marketApi } from "./api";
import { DataBadge, DecisionPanel, DomTable, FootprintPanel, ReplayControls, RiskPanel, TapePanel } from "./panels";
import type { ApplicationLockState, BacktestSummary, Candidate, JournalEntry, ReplayState, SessionRecord, SettingValue, Settings, SetupReason } from "./types";
import { LiquidityHeatmap, MarketChart } from "./visualizations";
import { reasonText, statusText, type Locale, useI18n } from "./i18n";

type SaveState = "idle" | "saving" | "saved" | "failed";

function SaveToast({ state, success, failure }: { state: SaveState; success: string; failure: string }): React.ReactElement | null {
  if (state !== "saved" && state !== "failed") return null;
  return <div className={`save-toast ${state}`} role="status">{state === "saved" ? <Check /> : <AlertTriangle />}<span>{state === "saved" ? success : failure}</span></div>;
}

function useUnsavedWarning(dirty: boolean): void {
  useEffect(() => {
    const handler = (event: BeforeUnloadEvent): void => {
      if (!dirty) return;
      event.preventDefault();
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty]);
}

function Metric({ label, value, tone }: { label: string; value: string; tone?: "positive" | "negative" | "warning" }): React.ReactElement {
  return <div className="metric"><span>{label}</span><strong className={`mono ${tone ?? ""}`}>{value}</strong></div>;
}

export function DashboardView({ state, sessions, journal }: { state: ReplayState; sessions: SessionRecord[]; journal: JournalEntry[] }): React.ReactElement {
  const { t, number } = useI18n();
  const pnl = journal.reduce((sum, entry) => sum + (entry.resultUsd ?? 0), 0);
  const signalLabel = state.signal?.status === "LONG" ? t("signal.long") : state.signal?.status === "SHORT" ? t("signal.short") : state.signal?.status === "NO_TRADE" ? t("signal.noTrade") : t("signal.wait");
  return (
    <div className="view-stack dashboard-view">
      <section className="dashboard-command">
        <div><span className="section-label">{t("signal.title")}</span><h1>{signalLabel}</h1><p>{state.signal?.strategyValidationStatus !== "VALIDATED" ? t("signal.researchOnly") : state.decision?.state === "blocked" ? t("setup.blockedCopy") : state.decision?.state === "trade_ready" ? t("setup.readyCopy") : t("setup.waitCopy")}</p></div>
        <div className="command-status"><DataBadge completeness={state.book?.completeness} reliability={state.book?.reliability} /><span className={`risk-state risk-${state.risk?.state ?? "caution"}`}>{t("nav.risk").toUpperCase()} {t(`risk.${state.risk?.state ?? "caution"}`)}</span></div>
      </section>
      <section className="metric-band">
        <Metric label={t("planner.mode")} value="REPLAY" />
        <Metric label={t("common.contract")} value={state.session?.contract_symbol ?? "–"} />
        <Metric label={t("planner.instrumentId")} value={String(state.session?.instrument_id ?? "–")} />
        <Metric label={t("planner.snapshot")} value={state.session?.snapshot_status ?? "–"} tone={state.session?.completeness === "partial" ? "warning" : undefined} />
        <Metric label={t("dashboard.journalPnl")} value={number(pnl, { style: "currency", currency: "USD" })} tone={pnl < 0 ? "negative" : "positive"} />
        <Metric label={t("dashboard.riskBuffer")} value={state.risk ? number(state.risk.remainingDrawdown, { style: "currency", currency: "USD", maximumFractionDigits: 0 }) : "–"} />
      </section>
      <div className="dashboard-grid">
        <div className="dashboard-chart"><MarketChart state={state} /><LiquidityHeatmap state={state} /></div>
        <div className="dashboard-side"><DecisionPanel state={state} compact /><RiskPanel state={state} /></div>
      </div>
      <section className="session-strip"><header className="panel-heading"><span>{t("dashboard.localSessions")}</span><span className="muted">{t("dashboard.registered", { count: sessions.length })}</span></header><div className="session-list">{sessions.map((session) => <div key={session.id}><span>{session.contract_symbol}</span><DataBadge completeness={session.completeness} reliability={session.completeness === "complete" ? "guaranteed" : "not_guaranteed"} /><span className="mono">{number(session.record_count)} {t("dashboard.recordsShort")}</span></div>)}</div></section>
    </div>
  );
}

export function ReplayView({ state, onState }: { state: ReplayState; onState: (value: ReplayState) => void }): React.ReactElement {
  return (
    <div className="replay-workspace">
      <div className="replay-main"><MarketChart state={state} /><LiquidityHeatmap state={state} /><FootprintPanel state={state} /><ReplayControls state={state} onState={onState} /></div>
      <aside className="replay-rail"><DomTable state={state} /><TapePanel state={state} /><DecisionPanel state={state} /><RiskPanel state={state} /></aside>
    </div>
  );
}

export function OrderflowView({ state }: { state: ReplayState }): React.ReactElement {
  const { t, number } = useI18n();
  const summary = state.features?.tradeSummary;
  const flow = state.features?.pullingStacking;
  const profile = state.features?.volumeProfile;
  const maxVolume = Math.max(...(profile?.levels.map((level) => level.volume) ?? [1]));
  const candidates = useMemo(() => {
    const merged = new Map<string, Candidate>();
    for (const candidate of [...(state.features?.absorptionCandidates ?? []), ...(state.features?.icebergCandidates ?? [])]) {
      const key = `${candidate.side}-${candidate.price ?? "none"}`;
      const current = merged.get(key);
      merged.set(key, current ? {
        ...current,
        confidence: Math.max(current.confidence, candidate.confidence),
        reasonCodes: [...new Set([...current.reasonCodes, ...candidate.reasonCodes])],
        scoreComponents: current.scoreComponents ?? candidate.scoreComponents,
      } : candidate);
    }
    return [...merged.values()].sort((left, right) => right.confidence - left.confidence);
  }, [state.features?.absorptionCandidates, state.features?.icebergCandidates]);
  return (
    <div className="view-stack">
      <section className="metric-band">
        <Metric label={t("orderflow.aggressiveBuy")} value={number(summary?.buyVolume ?? 0)} tone="positive" />
        <Metric label={t("orderflow.aggressiveSell")} value={number(summary?.sellVolume ?? 0)} tone="negative" />
        <Metric label={t("orderflow.cumulativeDelta")} value={number(summary?.delta ?? 0)} tone={(summary?.delta ?? 0) >= 0 ? "positive" : "negative"} />
        <Metric label={t("orderflow.tradePace")} value={`${number(summary?.tradePacePerSecond ?? 0, { maximumFractionDigits: 1 })}/s`} />
        <Metric label={t("orderflow.averageSize")} value={number(summary?.averageTradeSize ?? 0, { maximumFractionDigits: 1 })} />
        <Metric label="VWAP" value={summary?.vwap == null ? "–" : number(summary.vwap, { minimumFractionDigits: 2 })} />
      </section>
      <div className="orderflow-grid"><FootprintPanel state={state} /><DomTable state={state} /><TapePanel state={state} /></div>
      <div className="analysis-grid">
        <section className="data-panel"><header className="panel-heading"><span>Pulling / Stacking · {flow?.windowSeconds ?? 2}s</span><span className="muted">{t("orderflow.snapshotExcluded")}</span></header><div className="metric-band compact"><Metric label={t("orderflow.stacked")} value={number(flow?.stackedSize ?? 0)} tone="positive" /><Metric label={t("orderflow.pulled")} value={number(flow?.pulledSize ?? 0)} tone="negative" /><Metric label={t("orderflow.executed")} value={number(flow?.executedSize ?? 0)} /></div></section>
        <section className="data-panel"><header className="panel-heading"><span>{t("orderflow.heuristicCandidates")}</span><span className="muted">{t("orderflow.candidateCaveat")}</span></header><div className="candidate-list">{candidates.slice(0, 5).map((candidate) => <div key={`${candidate.side}-${candidate.price}`}><strong>{`${candidate.kind === "iceberg" ? "Iceberg" : "Absorption"} ${t(`orderflow.side.${candidate.side}`)} @ ${candidate.price == null ? "–" : number(candidate.price, { minimumFractionDigits: 2 })}`}</strong><span>{Math.round(candidate.confidence * 100)}%</span><small>{candidate.reasonCodes.map((code) => t(`candidate.${code}`)).join(" · ")}</small>{candidate.scoreComponents ? <small className="candidate-scores">{t("orderflow.volume")} {Math.round(candidate.scoreComponents.volume * 100)} · {t("orderflow.displacement")} {Math.round(candidate.scoreComponents.displacement * 100)} · Replenishment {Math.round(candidate.scoreComponents.replenishment * 100)} · {t("orderflow.persistence")} {Math.round(candidate.scoreComponents.persistence * 100)} · {t("common.data")} {Math.round(candidate.scoreComponents.dataCompleteness * 100)}</small> : null}</div>)}{candidates.length > 5 ? <details className="candidate-more"><summary>{t("orderflow.showMore", { count: candidates.length - 5 })}</summary>{candidates.slice(5).map((candidate) => <p key={`${candidate.side}-${candidate.price}-more`}>{t(`orderflow.side.${candidate.side}`)} @ {candidate.price == null ? "–" : number(candidate.price, { minimumFractionDigits: 2 })} · {Math.round(candidate.confidence * 100)}%</p>)}</details> : null}{!candidates.length ? <p className="empty-copy">{t("orderflow.noCandidates")}</p> : null}</div></section>
        <section className="data-panel profile-panel"><header className="panel-heading"><span>{t("orderflow.volumeProfile")}</span><span className="mono muted">POC {profile?.poc == null ? "–" : number(profile.poc, { minimumFractionDigits: 2 })}</span></header><div className="profile-bars">{profile?.levels.slice(-30).reverse().map((level) => <div key={level.priceFixed}><span className="mono">{number(level.price, { minimumFractionDigits: 2 })}</span><i style={{ width: `${Math.max((level.volume / maxVolume) * 100, 2)}%` }} /><b>{number(level.volume)}</b></div>)}</div></section>
      </div>
    </div>
  );
}

export function SetupsView({ state }: { state: ReplayState }): React.ReactElement {
  const { t, number } = useI18n();
  const decision = state.decision;
  const reasons = decision?.reasons ?? [];
  const byStates = (states: SetupReason["state"][]): SetupReason[] => reasons.filter((reason) => states.includes(reason.state));
  const groups = [
    { title: t("setup.passed"), className: "passed", items: byStates(["fulfilled"]) },
    { title: t("setup.observed"), className: "observed", items: byStates(["partially_fulfilled"]) },
    { title: t("setup.missing"), className: "blocking", items: byStates(["missing", "blocking", "contradictory", "unavailable"]) },
  ];
  return (
    <div className="setups-layout"><section className={`setup-overview data-panel decision-${decision?.state ?? "wait"}`}><header className="panel-heading"><span>MES Pullback / Retest</span><strong>{decision?.state === "trade_ready" ? t("setup.tradeSetup") : decision?.state === "blocked" ? t("signal.noTrade") : t("signal.wait")}</strong></header><p>{decision?.state === "blocked" ? t("setup.blockedCopy") : decision?.state === "trade_ready" ? t("setup.readyCopy") : t("setup.waitCopy")}</p></section><section className="setup-rule-groups">{groups.map((group) => <div className={`data-panel setup-rule-group ${group.className}`} key={group.title}><header className="panel-heading"><span>{group.title}</span><b>{group.items.length}</b></header><ul>{group.items.map((item) => <li key={item.code}>{group.className === "passed" ? <Check /> : group.className === "blocking" ? <AlertTriangle /> : <span className="evidence-dot" />}<span>{reasonText(t, item)}{item.detailKey ? <small>{t(item.detailKey)}</small> : null}</span></li>)}</ul>{!group.items.length ? <p className="empty-copy">{t("setup.none")}</p> : null}</div>)}</section><section className="structure-band data-panel"><header className="panel-heading"><span>{t("setup.multiTimeframe")}</span><span className="mono muted">{t("setup.completedBarsOnly")}</span></header><div className="structure-grid">{state.features?.marketStructure.map((item) => <div key={item.timeframe}><strong>{item.timeframe}</strong><span>{t(`structure.${item.state}`)}</span><small>{t("signal.confidence")} {Math.round(item.confidence * 100)}%</small><small>{t("signal.invalidation")} {item.invalidation == null ? "–" : number(item.invalidation, { minimumFractionDigits: 2 })}</small></div>)}</div></section></div>
  );
}

export function RiskView({ state, settings, onSaved, lockState, onExitLockedRun, onDirtyChange }: { state: ReplayState; settings: Settings | null; onSaved: (settings: Settings) => void; lockState: ApplicationLockState; onExitLockedRun: () => Promise<void>; onDirtyChange: (dirty: boolean) => void }): React.ReactElement {
  const { t } = useI18n();
  const [risk, setRisk] = useState<Record<string, SettingValue>>(() => settings?.risk ?? {});
  const [baseline, setBaseline] = useState(() => JSON.stringify(settings?.risk ?? {}));
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  useEffect(() => {
    if (settings?.risk) { setRisk(settings.risk); setBaseline(JSON.stringify(settings.risk)); }
  }, [settings]);
  const dirty = JSON.stringify(risk) !== baseline;
  useEffect(() => { onDirtyChange(dirty); return () => onDirtyChange(false); }, [dirty, onDirtyChange]);
  useUnsavedWarning(dirty);
  const fields = [
    ["accountSize", "settings.field.accountSize", 100], ["profitTarget", "settings.field.profitTarget", 50], ["maximumLossEod", "settings.field.maximumLossEod", 25],
    ["maxRiskPerTrade", "settings.field.maxRiskPerTrade", 5], ["maxDailyLoss", "settings.field.maxDailyLoss", 5], ["maxTrades", "settings.field.maxTrades", 1],
    ["consecutiveLossLimit", "settings.field.consecutiveLossLimit", 1], ["cooldownMinutes", "settings.field.cooldownMinutes", 5], ["manualDayPnl", "settings.field.manualDayPnl", 10],
    ["manualTotalPnl", "settings.field.manualTotalPnl", 10], ["openRiskUsd", "settings.field.openRiskUsd", 5], ["maxMicroContracts", "settings.field.maxMicroContracts", 1]
  ] as const;
  function changeRisk(key: string, value: number): void {
    setRisk((current) => ({ ...current, [key]: value })); setSaveState("idle");
    const allowsNegative = ["manualDayPnl", "manualTotalPnl"].includes(key);
    setFieldErrors((current) => ({ ...current, [key]: Number.isFinite(value) && (allowsNegative || value >= 0) ? "" : t("validation.invalidNumber") }));
  }
  async function save(): Promise<void> {
    if (lockState.locked || !dirty || Object.values(fieldErrors).some(Boolean)) return;
    setSaveState("saving");
    try {
      const saved = await marketApi.saveSettings({ risk } as Settings);
      onSaved(saved); setBaseline(JSON.stringify(saved.risk)); setSaveState("saved");
      window.setTimeout(() => setSaveState("idle"), 2600);
    } catch { setSaveState("failed"); }
  }
  const buttonLabel = lockState.locked ? t("common.locked") : saveState === "saving" ? t("common.saving") : !dirty ? t("common.noChanges") : t("risk.save");
  return <div className="risk-view"><RiskPanel state={state} /><section className="settings-form data-panel"><header className="panel-heading"><span>{String(risk.accountType ?? "Challenge")} · {lockState.locked ? t("common.locked") : t("common.editable")}</span><div className="toolbar-actions">{lockState.locked ? <button className="secondary-button" onClick={onExitLockedRun}>{t("settings.exitRun")}</button> : null}<button className="command-button" disabled={lockState.locked || saveState === "saving" || !dirty || Object.values(fieldErrors).some(Boolean)} onClick={save}>{lockState.locked ? <AlertTriangle /> : <Save />}{buttonLabel}</button></div></header>{lockState.locked ? <div className="lock-context mono">Plan {lockState.protocolId} · Run {lockState.runId} · {lockState.strategyHash?.slice(0, 18)}…</div> : null}<fieldset disabled={lockState.locked}><div className="form-grid">{fields.map(([key, label, step]) => <label key={key} className={fieldErrors[key] ? "field-invalid" : ""}><span>{t(label)}</span><input type="number" step={step} value={String(risk[key] ?? 0)} onChange={(event) => changeRisk(key, Number(event.target.value))} />{fieldErrors[key] ? <small>{fieldErrors[key]}</small> : null}</label>)}</div></fieldset><p className="manual-note">{lockState.locked ? t("risk.lockedNote") : t("risk.manualNote")}</p></section><SaveToast state={saveState} success={t("risk.savedToast")} failure={t("risk.failedToast")} /></div>;
}

const emptyJournal = (): Partial<JournalEntry> => ({ date: new Date().toISOString().slice(0, 10), session: "Replay", symbol: "MES", direction: "LONG", setup: "MES Pullback / Retest", entry: 0, stop: 0, targets: [], contracts: 1, riskUsd: 0, resultUsd: 0, resultR: 0, notes: "", emotion: "neutral", mistakeTags: [] });

export function JournalView({ state, entries, onReload }: { state: ReplayState; entries: JournalEntry[]; onReload: () => Promise<void> }): React.ReactElement {
  const { t, number } = useI18n();
  const [draft, setDraft] = useState<Partial<JournalEntry>>(emptyJournal);
  const [editing, setEditing] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const visibleEntries = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return entries;
    return entries.filter((entry) => [entry.date, entry.direction, entry.setup, entry.symbol, entry.notes, ...entry.mistakeTags]
      .some((value) => value.toLowerCase().includes(needle)));
  }, [entries, query]);
  function setField(key: keyof JournalEntry, value: unknown): void { setDraft((current) => ({ ...current, [key]: value })); }
  async function save(): Promise<void> {
    const payload = { ...draft, sessionId: state.session?.id, decisionSnapshot: state.decision, riskSnapshot: state.risk, marketContext: state.features?.context };
    if (editing) await marketApi.updateJournal(editing, payload); else await marketApi.createJournal(payload);
    setDraft(emptyJournal()); setEditing(null); await onReload();
  }
  function edit(entry: JournalEntry): void { setEditing(entry.id); setDraft(entry); }
  async function remove(entry: JournalEntry): Promise<void> { if (window.confirm(t("journal.deleteConfirm", { date: entry.date }))) { await marketApi.deleteJournal(entry.id); await onReload(); } }
  async function importFile(file: File | undefined): Promise<void> { if (!file) return; const payload = JSON.parse(await file.text()) as { entries?: Partial<JournalEntry>[] } | Partial<JournalEntry>[]; await marketApi.importJournal(Array.isArray(payload) ? payload : payload.entries ?? []); await onReload(); }
  return <div className="journal-layout"><section className="journal-form data-panel"><header className="panel-heading"><span>{editing ? t("journal.edit") : t("journal.create")}</span><button className="command-button" onClick={save}><Save />{editing ? t("journal.update") : t("journal.add")}</button></header><div className="form-grid journal-fields"><label><span>{t("common.date")}</span><input type="date" value={draft.date ?? ""} onChange={(e) => setField("date", e.target.value)} /></label><label><span>{t("journal.direction")}</span><select value={draft.direction} onChange={(e) => setField("direction", e.target.value)}><option>LONG</option><option>SHORT</option></select></label><label><span>{t("common.setup")}</span><input value={draft.setup ?? ""} onChange={(e) => setField("setup", e.target.value)} /></label><label><span>Entry</span><input type="number" step="0.25" value={draft.entry ?? 0} onChange={(e) => setField("entry", Number(e.target.value))} /></label><label><span>Stop</span><input type="number" step="0.25" value={draft.stop ?? 0} onChange={(e) => setField("stop", Number(e.target.value))} /></label><label><span>Exit</span><input type="number" step="0.25" value={draft.exit ?? 0} onChange={(e) => setField("exit", Number(e.target.value))} /></label><label><span>{t("signal.contracts")}</span><input type="number" value={draft.contracts ?? 1} onChange={(e) => setField("contracts", Number(e.target.value))} /></label><label><span>{t("signal.risk")} USD</span><input type="number" value={draft.riskUsd ?? 0} onChange={(e) => setField("riskUsd", Number(e.target.value))} /></label><label><span>{t("journal.result")} USD</span><input type="number" value={draft.resultUsd ?? 0} onChange={(e) => setField("resultUsd", Number(e.target.value))} /></label><label><span>{t("journal.result")} R</span><input type="number" step="0.1" value={draft.resultR ?? 0} onChange={(e) => setField("resultR", Number(e.target.value))} /></label><label><span>{t("journal.emotion")}</span><select value={draft.emotion} onChange={(e) => setField("emotion", e.target.value)}>{["neutral", "focused", "fomo", "revenge"].map((value) => <option key={value} value={value}>{t(`journal.emotion.${value}`)}</option>)}</select></label><label><span>{t("signal.targets")}</span><input value={(draft.targets ?? []).join(", ")} onChange={(e) => setField("targets", e.target.value.split(",").map(Number).filter(Number.isFinite))} /></label><label><span>{t("journal.screenshotPath")}</span><input value={draft.screenshotPath ?? ""} onChange={(e) => setField("screenshotPath", e.target.value)} /></label><label><span>{t("journal.mistakeTags")}</span><input value={(draft.mistakeTags ?? []).join(", ")} onChange={(e) => setField("mistakeTags", e.target.value.split(",").map((value) => value.trim()).filter(Boolean))} /></label><label className="wide-field"><span>{t("journal.notes")}</span><textarea value={draft.notes ?? ""} onChange={(e) => setField("notes", e.target.value)} /></label></div></section><section className="journal-table data-panel"><header className="panel-heading"><span>{t("journal.local")} · {visibleEntries.length}/{entries.length}</span><div className="toolbar-actions"><input className="journal-filter" aria-label={t("journal.filter")} placeholder={t("journal.filter")} value={query} onChange={(e) => setQuery(e.target.value)} /><a className="icon-button" title={t("journal.csvExport")} href={`${MARKET_URL}/journal/export.csv`}><Download /></a><a className="icon-button" title={t("journal.jsonBackup")} href={`${MARKET_URL}/journal/backup.json`}><Download /></a><label className="icon-button" title={t("journal.jsonImport")}><FileUp /><input type="file" accept="application/json" onChange={(e) => importFile(e.target.files?.[0])} /></label><button className="icon-button" title={t("journal.newEntry")} onClick={() => { setDraft(emptyJournal()); setEditing(null); }}><Plus /></button></div></header><div className="table-scroll"><table className="terminal-table"><thead><tr><th>{t("common.date")}</th><th>{t("market.side")}</th><th>{t("common.setup")}</th><th>{t("signal.risk")}</th><th>{t("journal.result")}</th><th>R</th><th /></tr></thead><tbody>{visibleEntries.map((entry) => <tr key={entry.id} onDoubleClick={() => edit(entry)}><td>{entry.date}</td><td className={entry.direction === "LONG" ? "positive" : "negative"}>{entry.direction}</td><td>{entry.setup}</td><td>{number(entry.riskUsd, { style: "currency", currency: "USD" })}</td><td className={(entry.resultUsd ?? 0) >= 0 ? "positive" : "negative"}>{entry.resultUsd == null ? "–" : number(entry.resultUsd, { style: "currency", currency: "USD" })}</td><td>{entry.resultR?.toFixed(2) ?? "–"}</td><td><button className="row-delete" title={t("journal.delete")} onClick={() => remove(entry)}><Trash2 /></button></td></tr>)}</tbody></table></div></section></div>;
}

export function BacktestView({ summary }: { summary: BacktestSummary | null }): React.ReactElement {
  const { t, number } = useI18n();
  const [mode, setMode] = useState("manual");
  const modes = ["manual", "historical", "candidate"];
  const labels: Array<[string, string]> = [["trades", "common.trades"], ["winRate", "backtest.winRate"], ["lossRate", "backtest.lossRate"], ["expectancy", "backtest.expectancy"], ["profitFactor", "research.profitFactor"], ["maximumDrawdown", "research.maxDrawdown"], ["averageR", "backtest.averageR"], ["medianR", "backtest.medianR"], ["mae", "MAE"], ["mfe", "MFE"], ["averageTimeInTrade", "backtest.timeInTrade"]];
  return <div className="view-stack"><section className="backtest-head"><div className="segmented-control modes">{modes.map((item) => <button key={item} className={mode === item ? "selected" : ""} onClick={() => setMode(item)}>{t(`backtest.mode.${item}`)}</button>)}</div><span>{t("backtest.slippage")} {summary?.slippageTicks ?? 2} {t("common.ticks")} · {t("backtest.commission")} {number(Number(summary?.commissionPerContract ?? 1.25), { style: "currency", currency: "USD" })}</span></section>{summary?.sampleSizeWarning ? <div className="warning-banner"><AlertTriangle />{t("backtest.sampleWarning")}</div> : null}<section className="metric-grid">{labels.map(([key, label]) => <Metric key={key} label={label.includes(".") ? t(label) : label} value={summary?.[key] === null || summary?.[key] === undefined ? t("common.unavailable") : String(summary[key])} />)}</section><section className="data-panel"><header className="panel-heading"><span>{t(`backtest.mode.${mode}`)}</span><span>{t("backtest.conservativeFills")}</span></header><p className="analysis-copy">{summary ? t("backtest.fillAssumption") : t("backtest.noJournalSample")}</p></section></div>;
}

export function DataHealthView({ sessions, activeSessionId }: { sessions: SessionRecord[]; activeSessionId?: string }): React.ReactElement {
  const { t, number, dateTime } = useI18n();
  const [selected, setSelected] = useState(activeSessionId ?? sessions[0]?.id);
  const [inspectionPinned, setInspectionPinned] = useState(false);
  useEffect(() => {
    if (!inspectionPinned && activeSessionId) setSelected(activeSessionId);
  }, [activeSessionId, inspectionPinned]);
  const active = sessions.find((item) => item.id === activeSessionId);
  const session = sessions.find((item) => item.id === selected) ?? active ?? sessions[0];
  const verification = session?.external_book_verification;
  const health = session?.data_health;
  return <div className="health-view"><section className="health-inspection data-panel"><div><strong>{session?.id === activeSessionId ? t("health.active") : t("health.inspecting")}</strong><span>{session?.id !== activeSessionId && active ? `${t("health.activeShort")}: ${active.contract_symbol} · ${t("health.inspectShort")}: ${session?.contract_symbol}` : `${active?.contract_symbol ?? "–"} · ${statusText(t, active?.completeness ?? "unassigned")}`}</span></div><label><span>{t("common.session")}</span><select value={session?.id ?? ""} onChange={(event) => { setSelected(event.target.value); setInspectionPinned(event.target.value !== activeSessionId); }}>{sessions.map((item) => <option key={item.id} value={item.id}>{item.contract_symbol} · {item.completeness === "complete" ? t("header.completeSnapshot") : t("header.partialSession")}</option>)}</select></label></section><div className="health-layout"><section className="session-table data-panel"><header className="panel-heading"><span>{t("health.registry")}</span><span>{sessions.length}</span></header><table className="terminal-table"><thead><tr><th>{t("common.contract")}</th><th>{t("common.records")}</th><th>{t("health.capability")}</th><th>{t("common.integrity")}</th></tr></thead><tbody>{sessions.map((item) => <tr key={item.id} className={item.id === session?.id ? "selected-row" : ""} onClick={() => { setSelected(item.id); setInspectionPinned(item.id !== activeSessionId); }}><td>{item.contract_symbol}</td><td>{number(item.record_count)}</td><td>{t(`health.capability.${item.data_health.signalCapability}`)}</td><td>{statusText(t, item.integrity_status)}</td></tr>)}</tbody></table></section>{session && health ? <section className="health-detail data-panel"><header className="panel-heading"><span>{session.contract_symbol} · {session.instrument_id}</span><span className={`capability capability-${health.signalCapability.toLowerCase()}`}>{t(`health.capability.${health.signalCapability}`)}</span></header><div className={`verification-banner verification-${verification?.status ?? "not_requested"}`}><strong>{verification?.status === "passed" ? t("health.mbpVerified") : t("health.mbpNotVerified")}</strong><span>{verification?.status === "passed" ? t("health.verificationCounts", { groups: number(verification.comparedGroups ?? 0), mismatches: number(verification.mismatches ?? 0) }) : t("health.hashBound")}</span></div><div className="health-grid"><Metric label={t("health.file")} value={session.file_path.split("/").at(-1) ?? "–"} /><Metric label="SHA-256" value={`${session.sha256.slice(0, 12)}…`} /><Metric label={t("health.startBerlin")} value={dateTime(session.start_at)} /><Metric label={t("health.endBerlin")} value={dateTime(session.end_at)} /><Metric label={t("planner.snapshot")} value={health.snapshotPosition} /><Metric label={t("health.book")} value={t(`status.${health.bookReconstructionStatus}`)} tone={health.bookReconstructionStatus !== "COMPLETE" ? "warning" : undefined} /><Metric label={t("health.sequenceGaps")} value={number(health.sequenceGaps)} /><Metric label={t("health.outOfOrder")} value={number(health.outOfOrderEvents)} tone={health.outOfOrderEvents ? "warning" : undefined} /><Metric label={t("health.duplicates")} value={number(health.duplicateEvents)} tone={health.duplicateEvents ? "warning" : undefined} /><Metric label={t("health.mapping")} value={health.contractMapping} /><Metric label="MBO/L3" value={health.mboL3Available ? t("common.available") : t("common.unavailable")} /><Metric label="MBP-10" value={health.mbp10Available ? t("common.available") : t("common.pending")} /><Metric label={t("common.trades")} value={health.tradesAvailable ? t("common.available") : t("common.unavailable")} /><Metric label="OHLCV" value={health.ohlcvAvailable ? t("common.available") : t("common.unavailable")} /></div>{health.sequenceGaps > 0 ? <p className="muted">{t("health.sequenceJumpsInfo")}</p> : null}<div className="feature-capability"><strong>{t("health.features")}</strong>{Object.entries(health.featureAvailability).map(([key, available]) => <span key={key} className={available ? "positive" : "muted"}>{available ? <Check /> : <AlertTriangle />}{t(`health.feature.${key}`)}</span>)}</div>{!health.fullL3Claim ? <p className="health-caveat"><AlertTriangle />{t("health.noFalseComplete")}</p> : null}</section> : null}</div></div>;
}

export function SettingsView({ settings, onSaved, lockState, onExitLockedRun, onDirtyChange, locale, onLocale }: { settings: Settings | null; onSaved: (value: Settings) => void; lockState: ApplicationLockState; onExitLockedRun: () => Promise<void>; onDirtyChange: (dirty: boolean) => void; locale: Locale; onLocale: (locale: Locale) => Promise<void> }): React.ReactElement {
  const { t } = useI18n();
  const [draft, setDraft] = useState<Settings>(() => settings ?? {});
  const [baseline, setBaseline] = useState(() => JSON.stringify(settings ?? {}));
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  useEffect(() => {
    if (settings) { setDraft(settings); setBaseline(JSON.stringify(settings)); }
  }, [settings]);
  const dirty = JSON.stringify(draft) !== baseline;
  useEffect(() => { onDirtyChange(dirty); return () => onDirtyChange(false); }, [dirty, onDirtyChange]);
  useUnsavedWarning(dirty);
  const sections = ["ui", "data", "replay", "orderflow", "ai"];
  async function save(): Promise<void> {
    if (lockState.locked || !dirty || Object.values(fieldErrors).some(Boolean)) return;
    setSaveState("saving");
    try {
      const saved = await marketApi.saveSettings(draft); onSaved(saved); setBaseline(JSON.stringify(saved)); setSaveState("saved");
      window.setTimeout(() => setSaveState("idle"), 2600);
    } catch { setSaveState("failed"); }
  }
  function change(section: string, key: string, value: SettingValue): void {
    setDraft((current) => ({ ...current, [section]: { ...(current[section] ?? {}), [key]: value } })); setSaveState("idle");
    const fieldKey = `${section}.${key}`;
    const invalid = typeof value === "number" && (!Number.isFinite(value) || value < 0);
    const emptyRequired = key === "importDirectory" && !String(value).trim();
    setFieldErrors((current) => ({ ...current, [fieldKey]: invalid || emptyRequired ? t("validation.invalidValue") : "" }));
  }
  const buttonLabel = lockState.locked ? t("common.locked") : saveState === "saving" ? t("common.saving") : !dirty ? t("common.noChanges") : t("settings.save");
  return <div className="view-stack settings-view"><div className="settings-toolbar"><div><h2>{t("settings.title")}</h2><span>{lockState.locked ? t("settings.locked") : t("settings.safe")}</span>{lockState.locked ? <small className="lock-context mono">Plan {lockState.protocolId} · Run {lockState.runId} · {t("common.strategy")} {lockState.strategyHash?.slice(0, 18)}…</small> : null}</div><div className="toolbar-actions">{lockState.locked ? <button className="secondary-button" onClick={onExitLockedRun}>{t("settings.exitRun")}</button> : null}<button className="command-button" disabled={lockState.locked || saveState === "saving" || !dirty || Object.values(fieldErrors).some(Boolean)} onClick={save}>{lockState.locked ? <AlertTriangle /> : <Save />}{buttonLabel}</button></div></div><fieldset disabled={lockState.locked}>{sections.map((section) => <section key={section} className="settings-section data-panel"><header className="panel-heading"><span>{t(`settings.section.${section}`)}</span>{section === "data" ? <span className="status-label status-warn">{t("settings.liveDisabled")}</span> : null}</header><div className="form-grid">{Object.entries(draft[section] ?? {}).map(([key, value]) => { const fieldKey = `${section}.${key}`; return <label key={key} className={fieldErrors[fieldKey] ? "field-invalid" : ""}><span>{key === "language" ? t("settings.language") : t(`settings.field.${key}`)}</span>{key === "language" ? <select value={locale} onChange={(event) => { const next = event.target.value as Locale; change(section, key, next); onLocale(next); }}><option value="de">Deutsch</option><option value="en">English</option></select> : typeof value === "boolean" ? <input type="checkbox" checked={key === "liveEnabled" ? false : value} disabled={key === "liveEnabled"} onChange={(event) => change(section, key, event.target.checked)} /> : key === "provider" ? <select value={String(value)} onChange={(event) => change(section, key, event.target.value)}><option>disabled</option><option>openai</option><option>local</option></select> : <input type={typeof value === "number" ? "number" : "text"} value={Array.isArray(value) ? value.join(", ") : String(value ?? "")} onChange={(event) => change(section, key, Array.isArray(value) ? event.target.value.split(",").map((item) => item.trim()).filter(Boolean) : typeof value === "number" ? Number(event.target.value) : event.target.value)} />}{fieldErrors[fieldKey] ? <small>{fieldErrors[fieldKey]}</small> : null}</label>; })}</div></section>)}</fieldset><SaveToast state={saveState} success={t("settings.savedToast")} failure={t("settings.failedToast")} /></div>;
}
