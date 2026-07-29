"use client";

import { useState } from "react";
import { CirclePause, CirclePlay, LocateFixed, RotateCcw, SkipForward, StepForward } from "lucide-react";
import { marketApi } from "./api";
import type { ReplayState } from "./types";
import { reasonText, useI18n } from "./i18n";

export function DataBadge({ completeness, reliability }: { completeness?: string; reliability?: string }): React.ReactElement {
  const { t } = useI18n();
  const complete = completeness === "complete" && reliability === "guaranteed";
  return <span className={`status-label ${complete ? "status-ok" : "status-warn"}`}>{complete ? t("market.completeBook") : t("market.partialBook")}</span>;
}

export function DomTable({ state }: { state: ReplayState }): React.ReactElement {
  const { t, number } = useI18n();
  const book = state.book;
  const asks = [...(book?.asks ?? [])].reverse();
  const bids = book?.bids ?? [];
  return (
    <section className="data-panel dom-panel" data-testid="dom-table">
      <header className="panel-heading"><span>{t("market.dom")}</span><DataBadge completeness={book?.completeness} reliability={book?.reliability} /></header>
      <div className="table-scroll">
        <table className="terminal-table dom-table-grid">
          <thead><tr><th>{t("market.askOrders")}</th><th>{t("market.askSize")}</th><th>{t("market.price")}</th><th>{t("market.bidSize")}</th><th>{t("market.bidOrders")}</th></tr></thead>
          <tbody>
            {asks.map((level) => (
              <tr key={`ask-${level.priceFixed}`} className={level.priceFixed === book?.bestAsk?.priceFixed ? "best-ask" : "ask-row"}>
                <td>{level.orderCount}</td><td>{number(level.totalSize)}</td><td>{number(level.displayPrice, { minimumFractionDigits: 2 })}</td><td /><td />
              </tr>
            ))}
            <tr className="spread-row"><td colSpan={2}>Best Ask {book?.bestAsk ? number(book.bestAsk.displayPrice, { minimumFractionDigits: 2 }) : "–"}</td><td>{book?.spreadTicks ?? "–"} {t("common.tick")}</td><td colSpan={2}>Best Bid {book?.bestBid ? number(book.bestBid.displayPrice, { minimumFractionDigits: 2 }) : "–"}</td></tr>
            {bids.map((level) => (
              <tr key={`bid-${level.priceFixed}`} className={level.priceFixed === book?.bestBid?.priceFixed ? "best-bid" : "bid-row"}>
                <td /><td /><td>{number(level.displayPrice, { minimumFractionDigits: 2 })}</td><td>{number(level.totalSize)}</td><td>{level.orderCount}</td>
              </tr>
            ))}
            {!asks.length && !bids.length ? <tr><td colSpan={5} className="empty-cell">{t("market.noBook")}</td></tr> : null}
          </tbody>
        </table>
      </div>
    </section>
  );
}

export function TapePanel({ state }: { state: ReplayState }): React.ReactElement {
  const { t, number } = useI18n();
  const tape = state.features?.tape ?? [];
  return (
    <section className="data-panel tape-panel" data-testid="tape-panel">
      <header className="panel-heading"><span>Tape</span><span className="mono muted">{state.features?.tradeSummary.tradePacePerSecond.toFixed(1) ?? "0"}/s</span></header>
      <div className="table-scroll">
        <table className="terminal-table">
          <thead><tr><th>{t("market.time")}</th><th>{t("market.price")}</th><th>{t("market.size")}</th><th>{t("market.side")}</th></tr></thead>
          <tbody>{tape.map((trade, index) => (
            <tr key={`${trade.tsEventNs}-${index}`} className={`${trade.side}-trade ${trade.large ? "large-trade" : ""}`}>
              <td>{trade.timestamp.slice(11, 23)}</td><td>{number(trade.price, { minimumFractionDigits: 2 })}</td><td>{number(trade.size)}</td><td>{trade.side === "buy" ? t("market.buy") : t("market.sell")}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
    </section>
  );
}

export function FootprintPanel({ state }: { state: ReplayState }): React.ReactElement {
  const { t } = useI18n();
  const rows = state.features?.footprint ?? [];
  const bar = state.features?.footprintBar;
  const barStart = bar ? new Date(Number(bar.startNs) / 1_000_000).toISOString().slice(11, 19) : "–";
  return (
    <section className="data-panel footprint-panel" data-testid="footprint-panel">
      <header className="panel-heading"><span>{bar?.completed ? t("market.footprintCompleted") : t("market.footprintForming")}</span><span className="muted">Bid × Ask · Delta</span></header>
      <div className="footprint-meta"><span>{t("market.barStart")} <b className="mono">{barStart} UTC</b></span><span>{t("market.elapsed")} <b className="mono">{bar ? `${bar.elapsedSeconds.toFixed(1)}s` : "–"}</b></span><span>{t("market.remaining")} <b className="mono">{bar ? `${bar.remainingSeconds.toFixed(1)}s` : "–"}</b></span><span>{t("market.completed")} <b>{bar?.completed ? t("common.yes") : t("common.no")}</b></span><span>{t("market.completedBars")} <b>{state.features?.barStatus.completed1m ?? 0}</b></span></div>
      <div className="footprint-grid">
        {rows.slice(0, 24).map((row) => (
          <div key={row.priceFixed} className={`footprint-row imbalance-${row.imbalance}`}>
            <span className="mono">{row.bidVolume}</span><span className="footprint-price mono">{row.price.toFixed(2)}</span><span className="mono">{row.askVolume}</span><span className={row.delta >= 0 ? "positive" : "negative"}>{row.delta > 0 ? "+" : ""}{row.delta}</span>{row.stackedImbalance ? <b title="Stacked imbalance">S</b> : null}
          </div>
        ))}
        {!rows.length ? <p className="empty-copy">{t("market.noFootprint")}</p> : null}
      </div>
    </section>
  );
}

export function ReplayControls({ state, onState }: { state: ReplayState; onState: (state: ReplayState) => void }): React.ReactElement {
  const { t } = useI18n();
  const [busy, setBusy] = useState(false);
  const futureLocked = state.blind ? !state.blind.futureSeekAllowed : false;
  const replayUnavailable = busy || Boolean(state.loading);
  async function run(action: () => Promise<ReplayState>): Promise<void> {
    setBusy(true);
    try { onState(await action()); } finally { setBusy(false); }
  }
  const speeds = ["0.25", "0.5", "1", "2", "5", "10", "50", "max"];
  return (
    <section className="replay-controls" aria-label={t("market.replayControls")}>
      <div className="transport-controls">
        <button className="icon-button" title={t("market.resetSession")} disabled={replayUnavailable} onClick={() => run(() => marketApi.action("reset"))}><RotateCcw /></button>
        <button className="icon-button" title={futureLocked ? t("market.futureSeekLocked") : t("market.firstTrade")} disabled={replayUnavailable || futureLocked} onClick={() => run(() => marketApi.jump("first_trade"))}><LocateFixed /></button>
        <button className="icon-button" title={t("market.nextTrade")} disabled={replayUnavailable} onClick={() => run(() => marketApi.step("trade"))}><SkipForward /></button>
        <button className="icon-button" title={t("market.nextEventGroup")} disabled={replayUnavailable} onClick={() => run(() => marketApi.step("event_group"))}><StepForward /></button>
        <button className="icon-button primary-control" title={state.blind?.pendingTradePlan ? t("market.tradePlanRequired") : state.playing ? t("market.pauseReplay") : t("market.startReplay")} disabled={replayUnavailable || Boolean(state.blind?.pendingTradePlan)} onClick={() => run(() => marketApi.action(state.playing ? "pause" : "play"))}>{state.playing ? <CirclePause /> : <CirclePlay />}</button>
      </div>
      <div className="speed-control"><span className="control-label">{t("market.speed")}</span><div className="segmented-control">{speeds.map((speed) => <button key={speed} className={state.speed === speed ? "selected" : ""} onClick={() => run(() => marketApi.speed(speed))}>{speed === "max" ? "MAX" : `${speed}x`}</button>)}</div></div>
      <div className="seek-control"><div className="seek-label"><span>{futureLocked ? t("market.seekLocked") : t("market.seek")}</span><span className="mono">{Math.round((state.progress ?? 0) * 100)}%</span></div><input aria-label={t("market.replayPosition")} disabled={replayUnavailable || futureLocked} type="range" min="0" max="1000" value={Math.round((state.progress ?? 0) * 1000)} onChange={(event) => run(() => marketApi.seek(Number(event.target.value) / 1000))} /></div>
      <div className="time-readout"><span className="control-label">{t("market.dataTime")}</span><strong className="mono">{state.timestamp?.slice(11, 23) ?? "–"}</strong><small>{state.timestamp?.slice(0, 10) ?? t("market.noSession")} UTC</small></div>
    </section>
  );
}

export function DecisionPanel({ state, compact = false }: { state: ReplayState; compact?: boolean }): React.ReactElement {
  const { t, number, dateTime } = useI18n();
  const signal = state.signal;
  const status = signal?.status ?? "WAIT";
  const label = status === "LONG" ? t("signal.long") : status === "SHORT" ? t("signal.short") : status === "NO_TRADE" ? t("signal.noTrade") : t("signal.wait");
  const evidence = [...(signal?.opposingEvidence ?? []), ...(signal?.missingEvidence ?? [])];
  return (
    <section className={`decision-panel signal-${status.toLowerCase().replace("_", "-")}`} data-testid="decision-panel">
      <div className="decision-title"><span>{t("signal.title")}</span><strong>{label}{signal?.quality && signal.quality !== "NONE" ? ` · ${t("signal.quality")} ${signal.quality}` : ""}</strong></div>
      <div className="decision-grid signal-summary-grid">
        <div><span className="control-label">{t("common.setup")}</span><strong>{signal?.setup ?? "MES Pullback / Retest"}</strong></div>
        <div><span className="control-label">{t("signal.confidence")}</span><strong className="mono">{signal?.confidence ?? 0}%</strong></div>
        <div><span className="control-label">{t("signal.regime")}</span><strong>{signal?.regime?.replaceAll("_", " ") ?? "–"}</strong></div>
        <div><span className="control-label">{t("common.data")}</span><DataBadge completeness={state.book?.completeness} reliability={state.book?.reliability} /></div>
      </div>
      {signal?.entryZone ? <div className="trade-parameters"><div><span>{t("signal.entry")}</span><strong>{number(signal.entryZone.min, { minimumFractionDigits: 2 })}–{number(signal.entryZone.max, { minimumFractionDigits: 2 })}</strong><small>{t("signal.preferred")} {number(signal.entryZone.preferred, { minimumFractionDigits: 2 })} {signal.entryZone.orderType}</small></div><div><span>{t("signal.stop")}</span><strong>{number(signal.stop?.price ?? 0, { minimumFractionDigits: 2 })}</strong><small>{signal.stop?.ticks ?? 0} {t("common.ticks")}</small></div><div><span>{t("signal.targets")}</span><strong>{signal.targets.map((target) => number(target.price, { minimumFractionDigits: 2 })).join(" · ")}</strong></div><div><span>{t("signal.contracts")}</span><strong>{signal.contracts}</strong></div><div><span>{t("signal.risk")}</span><strong>{number(signal.riskUsd, { style: "currency", currency: "USD" })}</strong></div><div><span>{t("signal.rewardRisk")}</span><strong>{signal.rewardRisk ?? "–"}</strong></div><div><span>{t("signal.validUntil")}</span><strong>{signal.validUntil ? dateTime(signal.validUntil, { timeStyle: "medium" }) : "–"}</strong></div></div> : <p className="signal-status-copy">{signal?.strategyValidationStatus !== "VALIDATED" ? t("signal.researchOnly") : state.decision?.state === "blocked" ? t("setup.blockedCopy") : t("setup.waitCopy")}</p>}
      {compact ? <div className="decision-compact"><ul>{evidence.slice(0, 3).map((reason) => <li key={reason.code}>{reasonText(t, reason)}</li>)}</ul><a href="/setups">{t("nav.setups")}</a></div> : <div className="signal-evidence"><div><span className="control-label">{t("signal.supporting")}</span><ul>{signal?.supportingEvidence.slice(0, 4).map((reason) => <li key={reason.code}>{reasonText(t, reason)}</li>)}</ul></div><div><span className="control-label">{status === "WAIT" ? t("signal.missing") : t("signal.opposing")}</span><ul>{evidence.slice(0, 4).map((reason) => <li key={reason.code}>{reasonText(t, reason)}</li>)}</ul></div>{signal?.invalidation.length ? <div><span className="control-label">{t("signal.invalidation")}</span><ul>{signal.invalidation.map((code) => <li key={code}>{t(`invalidation.${code}`)}</li>)}</ul></div> : null}</div>}
    </section>
  );
}

export function RiskPanel({ state }: { state: ReplayState }): React.ReactElement {
  const { t, number } = useI18n();
  const risk = state.risk;
  const profile = risk?.challengeProfile ?? {};
  const progress = Math.max(0, Math.min(1, Number(profile.profitTargetProgress ?? 0)));
  const violations = Array.isArray(profile.violations) ? profile.violations.map(String) : [];
  return (
    <section className="risk-panel" data-testid="risk-panel">
      <header className="panel-heading"><span>{t("risk.title")}</span><span className={`risk-state risk-${risk?.state ?? "caution"}`}>{t(`risk.${risk?.state ?? "caution"}`)}</span></header>
      <div className="risk-metrics">
        <div><span>{t("risk.remainingBuffer")}</span><strong>{number(risk?.remainingDrawdown ?? 0, { style: "currency", currency: "USD", maximumFractionDigits: 0 })}</strong></div>
        <div><span>{t("risk.plannedRisk")}</span><strong>{number(risk?.plannedRiskUsd ?? 0, { style: "currency", currency: "USD" })}</strong></div>
        <div><span>{t("risk.dayPnl")}</span><strong className={(risk?.dayPnl ?? 0) < 0 ? "negative" : "positive"}>{number(risk?.dayPnl ?? 0, { style: "currency", currency: "USD" })}</strong></div>
        <div><span>{t("risk.tradesToday")}</span><strong>{risk?.tradesToday ?? 0}</strong></div>
      </div>
      <div className="challenge-summary"><div className="challenge-progress"><span>{t("risk.profitTargetProgress")}</span><strong>{number(progress, { style: "percent", maximumFractionDigits: 0 })}</strong><i><b style={{ width: `${progress * 100}%` }} /></i></div><div className="challenge-grid"><div><span>{t("risk.dailyLimit")}</span><strong>{number(Number(profile.dailyLossLimit ?? 0), { style: "currency", currency: "USD" })}</strong></div><div><span>{t("risk.contractLimit")}</span><strong>{String(profile.maximumContracts ?? "–")} MES</strong></div><div><span>{t("risk.consistency")}</span><strong>{number(Number(profile.consistencyActual ?? 0), { style: "percent", maximumFractionDigits: 0 })} / {number(Number(profile.consistencyRule ?? 0), { style: "percent", maximumFractionDigits: 0 })}</strong></div><div><span>{t("risk.tradingWindow")}</span><strong>{String(profile.allowedTradingStart ?? "–")}–{String(profile.allowedTradingEnd ?? "–")}</strong></div></div><div className={`challenge-violations ${violations.length ? "negative" : "positive"}`}><span>{t("risk.violations")}</span><strong>{violations.length ? violations.map((code) => t(`risk.${code === "DAILY_LOSS_LIMIT" ? "dailyLossLimit" : code === "MAX_TRADES" ? "maximumTrades" : code === "CONSECUTIVE_LOSSES" ? "lossStreak" : code === "DRAWDOWN_BUFFER" ? "drawdownBuffer" : code === "INSTRUMENT_NOT_ALLOWED" ? "instrumentNotAllowed" : "outsideTradingHours"}`)).join(" · ") : t("risk.noViolations")}</strong></div></div>
      <p className="manual-note">{t("risk.manualData")}</p>
    </section>
  );
}
