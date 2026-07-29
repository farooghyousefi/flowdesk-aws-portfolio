"use client";

import { useEffect, useMemo, useRef } from "react";
import type { Bar, ReplayState, TapeTrade } from "./types";
import { useI18n } from "./i18n";

function compactBars(state: ReplayState): Bar[] {
  const oneMinute = state.features?.bars.filter((bar) => bar.timeframe === "1m") ?? [];
  if (oneMinute.length >= 4) return oneMinute.slice(-80);
  const trades = [...(state.features?.tape ?? [])].reverse();
  const grouped = new Map<number, TapeTrade[]>();
  for (const trade of trades) {
    const bucket = Math.floor(Number(trade.tsEventNs) / 250_000_000);
    grouped.set(bucket, [...(grouped.get(bucket) ?? []), trade]);
  }
  return [...grouped.entries()].map(([bucket, rows]) => {
    const buyVolume = rows.filter((row) => row.side === "buy").reduce((sum, row) => sum + row.size, 0);
    const sellVolume = rows.filter((row) => row.side === "sell").reduce((sum, row) => sum + row.size, 0);
    const prices = rows.map((row) => row.price);
    return {
      timeframe: "1m", startNs: String(bucket * 250_000_000), endNs: String((bucket + 1) * 250_000_000),
      open: prices[0], high: Math.max(...prices), low: Math.min(...prices), close: prices.at(-1) ?? prices[0],
      volume: buyVolume + sellVolume, buyVolume, sellVolume, delta: buyVolume - sellVolume,
      cumulativeDelta: 0, tradeCount: rows.length,
      vwap: rows.reduce((sum, row) => sum + row.price * row.size, 0) / Math.max(buyVolume + sellVolume, 1),
      completed: false
    } satisfies Bar;
  }).slice(-80);
}

export function MarketChart({ state }: { state: ReplayState }): React.ReactElement {
  const { t } = useI18n();
  const bars = useMemo(() => compactBars(state), [state]);
  const oneMinuteBars = state.features?.bars.filter((bar) => bar.timeframe === "1m") ?? [];
  const internalBuckets = oneMinuteBars.length >= 4 ? 0 : bars.length;
  const width = 1000;
  const height = 430;
  const plotBottom = 340;
  const priceValues = bars.flatMap((bar) => [bar.high, bar.low]);
  const context = state.features?.context;
  if (context?.sessionHigh) priceValues.push(context.sessionHigh);
  if (context?.sessionLow) priceValues.push(context.sessionLow);
  const fallback = state.book?.bestBid?.displayPrice ?? 0;
  const min = priceValues.length ? Math.min(...priceValues) : fallback - 1;
  const max = priceValues.length ? Math.max(...priceValues) : fallback + 1;
  const range = Math.max(max - min, 0.25);
  const y = (value: number) => 24 + ((max - value) / range) * (plotBottom - 40);
  const step = (width - 95) / Math.max(bars.length, 1);
  const maxVolume = Math.max(...bars.map((bar) => bar.volume), 1);
  const grid = Array.from({ length: 6 }, (_, index) => max - (range / 5) * index);
  const decision = state.decision;

  return (
    <div className="viz-shell chart-shell" data-testid="market-chart">
      <div className="viz-heading">
        <span>{state.session?.contract_symbol ?? "MES"} · Replay</span>
        <span className="mono muted">{t("market.completed1m")} {state.features?.barStatus.completed1m ?? 0} · {t("market.forming")} {state.features?.barStatus.forming1m ? t("common.yes") : t("common.no")} · {t("market.internalBuckets")} {internalBuckets} · VWAP {context?.vwap?.toFixed(2) ?? "–"}</span>
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Candlestick chart with real replay trades">
        <rect width={width} height={height} fill="#071013" />
        {grid.map((value) => (
          <g key={value}>
            <line x1="0" x2="930" y1={y(value)} y2={y(value)} stroke="#1b2a2f" strokeWidth="1" />
            <text x="940" y={y(value) + 4} fill="#708187" fontSize="12" fontFamily="monospace">{value.toFixed(2)}</text>
          </g>
        ))}
        {context?.vwap ? <line x1="0" x2="930" y1={y(context.vwap)} y2={y(context.vwap)} stroke="#26c7d9" strokeWidth="1.5" /> : null}
        {context?.sessionHigh ? <line x1="0" x2="930" y1={y(context.sessionHigh)} y2={y(context.sessionHigh)} stroke="#7f9197" strokeDasharray="5 5" /> : null}
        {context?.sessionLow ? <line x1="0" x2="930" y1={y(context.sessionLow)} y2={y(context.sessionLow)} stroke="#7f9197" strokeDasharray="5 5" /> : null}
        {decision?.entryZone ? (
          <rect x="0" y={y(decision.entryZone.max)} width="930" height={Math.max(y(decision.entryZone.min) - y(decision.entryZone.max), 2)} fill="#27c7d91a" />
        ) : null}
        {decision?.invalidation ? <line x1="0" x2="930" y1={y(decision.invalidation)} y2={y(decision.invalidation)} stroke="#ff655e" strokeDasharray="3 4" /> : null}
        {bars.map((bar, index) => {
          const candleWidth = Math.min(Math.max(step * 0.58, 3), 13);
          const x = 14 + index * step + Math.max((step - candleWidth) / 2, 0);
          const rising = bar.close >= bar.open;
          const color = rising ? "#29c486" : "#f05d5e";
          const bodyTop = y(Math.max(bar.open, bar.close));
          const bodyHeight = Math.max(y(Math.min(bar.open, bar.close)) - bodyTop, 2);
          const volumeHeight = (bar.volume / maxVolume) * 58;
          return (
            <g key={`${bar.startNs}-${index}`}>
              <line x1={x + candleWidth / 2} x2={x + candleWidth / 2} y1={y(bar.high)} y2={y(bar.low)} stroke={color} />
              <rect x={x} y={bodyTop} width={candleWidth} height={bodyHeight} fill={color} />
              <rect x={x} y={410 - volumeHeight} width={candleWidth} height={volumeHeight} fill={`${color}99`} />
            </g>
          );
        })}
        <line x1="0" x2="930" y1="350" y2="350" stroke="#26363b" />
        {!bars.length ? <text x="500" y="210" fill="#829398" textAnchor="middle" fontSize="16">{t("market.waitingTrades")}</text> : null}
      </svg>
    </div>
  );
}

export function LiquidityHeatmap({ state }: { state: ReplayState }): React.ReactElement {
  const { t } = useI18n();
  const canvasRef = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const context = canvas.getContext("2d");
    if (!context) return;
    const ratio = window.devicePixelRatio || 1;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    canvas.width = width * ratio;
    canvas.height = height * ratio;
    context.scale(ratio, ratio);
    context.fillStyle = "#071013";
    context.fillRect(0, 0, width, height);
    const frames = state.heatmap ?? [];
    const levels = frames.flatMap((frame) => [...frame.bids, ...frame.asks]);
    if (!levels.length) {
      context.fillStyle = "#718287";
      context.font = "12px sans-serif";
      context.fillText(t("market.waitingHeatmap"), 16, 28);
      return;
    }
    const prices = levels.map((level) => level.price);
    const min = Math.min(...prices);
    const max = Math.max(...prices);
    const maxSize = Math.max(...levels.map((level) => level.size), 1);
    const cellWidth = width / Math.max(frames.length, 1);
    frames.forEach((frame, frameIndex) => {
      for (const [side, rows] of [["bid", frame.bids], ["ask", frame.asks]] as const) {
        for (const level of rows) {
          const normalized = Math.log1p(level.size) / Math.log1p(maxSize);
          const y = ((max - level.price) / Math.max(max - min, 0.25)) * (height - 8);
          context.fillStyle = side === "bid" ? `rgba(38, 200, 143, ${0.1 + normalized * 0.65})` : `rgba(239, 91, 94, ${0.1 + normalized * 0.65})`;
          context.fillRect(frameIndex * cellWidth, y, Math.max(cellWidth + 1, 2), 4);
        }
      }
    });
  }, [state, t]);
  return (
    <div className="viz-shell heatmap-shell" data-testid="liquidity-heatmap">
      <div className="viz-heading"><span>Liquidity Heatmap</span><span className="muted">{t("market.localLogNormalization")}</span></div>
      <canvas ref={canvasRef} aria-label="Liquidity heatmap" />
    </div>
  );
}
