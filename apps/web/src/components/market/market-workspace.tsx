"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Activity, BarChart3, BookOpen, BotOff, ChartCandlestick, Database, FlaskConical, Gauge, Languages, LayoutDashboard, RadioTower, Settings as SettingsIcon, ShieldCheck } from "lucide-react";
import { marketApi, replaySocketUrl } from "./api";
import type { JournalEntry, ReplayState, SessionRecord, Settings, ViewName } from "./types";
import { DashboardView, DataHealthView, JournalView, OrderflowView, ReplayView, RiskView, SetupsView, SettingsView } from "./views";
import { BacktestPlanView, DataPlannerView } from "./planner-views";
import { ResearchLabView } from "./research-view";
import { deriveApplicationLockState } from "./lock-state";
import { I18nProvider, type Locale, useI18n } from "./i18n";
import { replaySessionLabel } from "./session-label";

const nav: Array<{ label: ViewName; labelKey: string; slug: string; icon: React.ElementType }> = [
  { label: "Dashboard", labelKey: "nav.dashboard", slug: "", icon: LayoutDashboard },
  { label: "Replay", labelKey: "nav.replay", slug: "replay", icon: Activity },
  { label: "Orderflow", labelKey: "nav.orderflow", slug: "orderflow", icon: RadioTower },
  { label: "Setups", labelKey: "nav.setups", slug: "setups", icon: Gauge },
  { label: "Risk", labelKey: "nav.risk", slug: "risk", icon: ShieldCheck },
  { label: "Journal", labelKey: "nav.journal", slug: "journal", icon: BookOpen },
  { label: "Backtest", labelKey: "nav.backtest", slug: "backtest", icon: BarChart3 },
  { label: "Research Lab", labelKey: "nav.research", slug: "research", icon: FlaskConical },
  { label: "Data Planner", labelKey: "nav.dataPlanner", slug: "data-planner", icon: Database },
  { label: "Data Health", labelKey: "nav.dataHealth", slug: "data-health", icon: ChartCandlestick },
  { label: "Settings", labelKey: "nav.settings", slug: "settings", icon: SettingsIcon }
];

const viewByPath: Record<string, ViewName> = Object.fromEntries(
  nav.map((item) => [item.slug ? `/${item.slug}` : "/", item.label]),
) as Record<string, ViewName>;

export function MarketWorkspace({ initialView = "Dashboard" }: { initialView?: ViewName }): React.ReactElement {
  const [locale, setLocale] = useState<Locale>("de");
  return <I18nProvider locale={locale}><MarketWorkspaceContent initialView={initialView} locale={locale} onLocale={setLocale} /></I18nProvider>;
}

function MarketWorkspaceContent({ initialView, locale, onLocale }: { initialView: ViewName; locale: Locale; onLocale: (locale: Locale) => void }): React.ReactElement {
  const { t } = useI18n();
  const [activeView, setActiveView] = useState<ViewName>(initialView);
  const [state, setState] = useState<ReplayState>({ version: 1, loaded: false, playing: false, revision: 0 });
  const [sessions, setSessions] = useState<SessionRecord[]>([]);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [journal, setJournal] = useState<JournalEntry[]>([]);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hasUnsavedChanges, setHasUnsavedChanges] = useState(false);
  const mobileNavRef = useRef<HTMLElement>(null);
  const applicationLock = useMemo(() => deriveApplicationLockState(state), [state]);

  const reloadJournal = useCallback(async () => setJournal(await marketApi.journal()), []);
  const exitLockedRun = useCallback(async () => {
    const result = await marketApi.exitBacktestRun();
    setState(result.state);
  }, []);

  useEffect(() => {
    let cancelled = false;
    Promise.all([marketApi.sessions(), marketApi.replayState(), marketApi.settings(), marketApi.journal()])
      .then(([nextSessions, nextState, nextSettings, nextJournal]) => {
        if (cancelled) return;
        setSessions(nextSessions); setState(nextState); setSettings(nextSettings); setJournal(nextJournal); setError(null);
        const savedLocale = nextSettings.ui?.language;
        const nextLocale = savedLocale === "en" || savedLocale === "de" ? savedLocale : (window.localStorage.getItem("flowdesk-language") === "en" ? "en" : "de");
        onLocale(nextLocale);
      })
      .catch((reason: Error) => !cancelled && setError(`Market service: ${reason.message}`));
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    mobileNavRef.current?.querySelector("button.active")?.scrollIntoView({ behavior: "instant", block: "nearest", inline: "center" });
  }, [activeView]);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let stopped = false;
    const connect = () => {
      if (stopped) return;
      socket = new WebSocket(replaySocketUrl());
      socket.onopen = () => setConnected(true);
      socket.onmessage = (event) => {
        try { setState(JSON.parse(event.data) as ReplayState); } catch { setError("Replay stream returned invalid data."); }
      };
      socket.onerror = () => setConnected(false);
      socket.onclose = () => { setConnected(false); if (!stopped) retry = setTimeout(connect, 1500); };
    };
    connect();
    return () => { stopped = true; if (retry) clearTimeout(retry); socket?.close(); };
  }, []);

  useEffect(() => {
    const syncPath = () => setActiveView(viewByPath[window.location.pathname] ?? initialView);
    window.addEventListener("popstate", syncPath);
    return () => window.removeEventListener("popstate", syncPath);
  }, [initialView]);

  function navigate(label: ViewName, slug: string): void {
    if (hasUnsavedChanges && !window.confirm(t("common.unsavedWarning"))) return;
    setHasUnsavedChanges(false);
    setActiveView(label);
    const path = slug ? `/${slug}` : "/";
    if (window.location.pathname !== path) window.history.pushState({}, "", path);
    window.scrollTo({ top: 0, left: 0, behavior: "instant" });
  }

  async function changeLocale(nextLocale: Locale): Promise<void> {
    onLocale(nextLocale);
    window.localStorage.setItem("flowdesk-language", nextLocale);
    try {
      const saved = await marketApi.saveSettings({ ui: { language: nextLocale } });
      setSettings(saved);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("common.saveFailed"));
    }
  }

  async function selectSession(sessionId: string): Promise<void> {
    setError(null);
    try { setState(await marketApi.load(sessionId)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Session could not be loaded."); }
  }

  const content = useMemo(() => {
    if (activeView === "Dashboard") return <DashboardView state={state} sessions={sessions} journal={journal} />;
    if (activeView === "Replay") return <ReplayView state={state} onState={setState} />;
    if (activeView === "Orderflow") return <OrderflowView state={state} />;
    if (activeView === "Setups") return <SetupsView state={state} />;
    if (activeView === "Risk") return <RiskView state={state} settings={settings} onSaved={setSettings} lockState={applicationLock} onExitLockedRun={exitLockedRun} onDirtyChange={setHasUnsavedChanges} />;
    if (activeView === "Journal") return <JournalView state={state} entries={journal} onReload={reloadJournal} />;
    if (activeView === "Backtest") return <BacktestPlanView state={state} onState={setState} lockState={applicationLock} />;
    if (activeView === "Research Lab") return <ResearchLabView sessions={sessions} />;
    if (activeView === "Data Planner") return <DataPlannerView />;
    if (activeView === "Data Health") return <DataHealthView sessions={sessions} activeSessionId={state.session?.id} />;
    return <SettingsView settings={settings} onSaved={setSettings} lockState={applicationLock} onExitLockedRun={exitLockedRun} onDirtyChange={setHasUnsavedChanges} locale={locale} onLocale={changeLocale} />;
  }, [activeView, applicationLock, exitLockedRun, journal, locale, reloadJournal, sessions, settings, state]);

  return (
    <div className="market-app">
      <aside className="app-sidebar">
        <button className="wordmark" onClick={() => navigate("Dashboard", "")}>FLOWDESK</button>
        <nav>{nav.map((item) => { const Icon = item.icon; return <button key={item.label} className={activeView === item.label ? "active" : ""} onClick={() => navigate(item.label, item.slug)}><Icon /><span>{t(item.labelKey)}</span></button>; })}</nav>
        <div className="sidebar-foot"><span>v1.0 · LOCAL</span><span>{t("header.manualOnly")}</span></div>
      </aside>
      <div className="app-stage">
        <header className="app-header">
          <div className="header-mode"><span>{t("header.mode")}</span><strong>REPLAY</strong></div>
          <label className="session-select">
            <span className="session-select-label">{t("header.session")}</span>
            <select
              aria-label={t("header.session")}
              value={state.loadingSessionId ?? state.session?.id ?? ""}
              title={(() => {
                const selected = sessions.find((session) => session.id === (state.loadingSessionId ?? state.session?.id)) ?? state.session;
                return selected ? replaySessionLabel(selected, locale) : t("header.session");
              })()}
              disabled={Boolean(state.loading)}
              aria-busy={Boolean(state.loading)}
              onChange={(event) => selectSession(event.target.value)}
            >
              {sessions.map((session) => <option key={session.id} value={session.id}>{replaySessionLabel(session, locale)}</option>)}
            </select>
          </label>
          <div className="header-datum"><span>{state.session?.instrument ?? "MES"}</span><b className="mono">ID {state.session?.instrument_id ?? "–"}</b><time className="mono">{state.timestamp?.replace("T", " ").slice(0, 23) ?? t("header.noDataTime")} UTC</time></div>
          <div className={`service-status ${connected ? "connected" : "disconnected"}`}><i /><span>{connected ? t("header.connected") : t("header.offline")}</span></div>
          <label className="language-switch"><Languages /><select aria-label={t("settings.language")} value={locale} onChange={(event) => changeLocale(event.target.value as Locale)}><option value="de">DE</option><option value="en">EN</option></select></label>
          <div className="manual-only"><BotOff /><span>{t("header.manualOnly")}</span></div>
        </header>
        <nav className="mobile-nav" ref={mobileNavRef}>{nav.map((item) => { const Icon = item.icon; return <button key={item.label} className={activeView === item.label ? "active" : ""} onClick={() => navigate(item.label, item.slug)}><Icon /><span>{t(item.labelKey)}</span></button>; })}</nav>
        {error ? <div className="service-error">{error}</div> : null}
        {state.loadError ? <div className="service-error">{state.loadError}</div> : null}
        {state.loading ? <div className="partial-warning">{locale === "de" ? "Replay-Session wird geladen. Bitte warten; Wiedergabe ist vorübergehend gesperrt." : "Loading replay session. Please wait; playback is temporarily disabled."}</div> : null}
        {state.session?.completeness === "partial" ? <div className="partial-warning">{t("header.partialWarning")}</div> : null}
        <main className="app-content">{content}</main>
      </div>
    </div>
  );
}
