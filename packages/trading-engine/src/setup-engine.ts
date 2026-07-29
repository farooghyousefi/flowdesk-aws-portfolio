import type {
  Direction,
  DynamicChecklistItem,
  KeyLevel,
  MarketStructure,
  SetupEvaluation,
  SetupId,
  VwapState
} from "@trading-assistant/shared-types";

export interface SetupEngineInput {
  direction: Direction;
  currentPrice: number;
  bias: MarketStructure;
  setup: {
    fiveMinuteClose: number;
    retestConfirmed: boolean;
    rejectionConfirmed: boolean;
    entryTriggerConfirmed: boolean;
    wickOnlyBreak: boolean;
    directNewsSpike: boolean;
  };
  levels: KeyLevel[];
  vwap: VwapState;
  distanceToNextLevelR: number;
}

export function evaluateAllSetups(input: SetupEngineInput): SetupEvaluation[] {
  return [
    openingRangeBreakRetest(input),
    vwapTrendPullback(input),
    pdhPdlBreakRetest(input),
    onhOnlBreakRetest(input),
    failedBreakoutReversal(input),
    liquiditySweepReclaim(input)
  ];
}

export function bestSetup(evaluations: SetupEvaluation[]): SetupEvaluation {
  return [...evaluations].sort((a, b) => b.confidenceScore - a.confidenceScore)[0];
}

function openingRangeBreakRetest(input: SetupEngineInput): SetupEvaluation {
  const level = findLevel(input.levels, input.direction === "LONG" ? "ORH" : "ORL");
  return setup("opening_range_break_retest", "Opening Range Break and Retest", input, level, [
    item("bias", "HTF-Bias", input.bias.trend === trendFor(input.direction), "15m", "hoch", input.bias.trend, "Warte auf eindeutigen HTF-Bias.", "Bias bricht gegen die Trade-Richtung."),
    item("close", "5m Close durch Opening Range", breaksLevel(input, level), "5m", "hoch", value(level), "Warte auf abgeschlossenen 5m Close durch das Opening-Range-Level.", "Nur Wick-Break oder Close zurück in Range."),
    item("wick", "Kein reiner Wick-Break", !input.setup.wickOnlyBreak, "5m", "hoch", input.setup.wickOnlyBreak ? "Wick-Break" : "Close-Break", "Warte auf echten Close-Break.", "Wick ohne Close invalidiert."),
    item("retest", "Pullback und Retest", input.setup.retestConfirmed, "5m", "sehr hoch", yn(input.setup.retestConfirmed), "Warte auf Retest und Hold.", "Level hält nicht."),
    item("trigger", "Entry-Trigger", input.setup.entryTriggerConfirmed, "1m", "hoch", yn(input.setup.entryTriggerConfirmed), "Warte auf 1m/2m Trigger.", "Trigger gegen Richtung.")
  ]);
}

function vwapTrendPullback(input: SetupEngineInput): SetupEvaluation {
  return setup("vwap_trend_pullback", "VWAP Trend Pullback", input, undefined, [
    item("bias", "HTF-Struktur", input.bias.trend === trendFor(input.direction), "15m", "hoch", input.bias.trend, "Warte auf klare Trendstruktur.", "Trend kippt."),
    item("vwap-side", "Preis auf korrekter VWAP-Seite", input.direction === "LONG" ? input.vwap.priceRelation === "above" : input.vwap.priceRelation === "below", "5m", "hoch", input.vwap.priceRelation, "Warte auf korrekte VWAP-Seite.", "Preis verliert VWAP."),
    item("vwap-slope", "VWAP-Slope unterstützt", input.direction === "LONG" ? input.vwap.slope !== "bearish" : input.vwap.slope !== "bullish", "5m", "mittel", input.vwap.slope, "Warte auf stabilen VWAP.", "VWAP kippt gegen Setup."),
    item("vwap-cross", "Kein mehrfaches VWAP-Crossing", !input.vwap.multipleCrossing, "5m", "hoch", `${input.vwap.crossingCount} Crossings`, "Warte auf sauberere VWAP-Struktur.", "Mindestens drei schnelle Crossings."),
    item("trigger", "Entry-Trigger", input.setup.entryTriggerConfirmed, "1m", "hoch", yn(input.setup.entryTriggerConfirmed), "Warte auf Trigger.", "Trigger scheitert.")
  ]);
}

function pdhPdlBreakRetest(input: SetupEngineInput): SetupEvaluation {
  const level = findLevel(input.levels, input.direction === "LONG" ? "PDH" : "PDL");
  return setup("pdh_pdl_break_retest", "PDH/PDL Break and Retest", input, level, [
    item("break", "Break durch PDH/PDL", breaksLevel(input, level), "5m", "hoch", value(level), "Warte auf 5m Close durch PDH/PDL.", "Close zurück hinter Level."),
    item("retest", "Retest", input.setup.retestConfirmed, "5m", "sehr hoch", yn(input.setup.retestConfirmed), "Warte auf Retest.", "Level hält nicht."),
    item("rejection", "Rejection", input.setup.rejectionConfirmed, "5m", "hoch", yn(input.setup.rejectionConfirmed), "Warte auf Rejection.", "Keine Reaktion am Level."),
    item("trigger", "Entry-Trigger", input.setup.entryTriggerConfirmed, "1m", "hoch", yn(input.setup.entryTriggerConfirmed), "Warte auf Trigger.", "Trigger scheitert.")
  ]);
}

function onhOnlBreakRetest(input: SetupEngineInput): SetupEvaluation {
  const level = findLevel(input.levels, input.direction === "LONG" ? "ONH" : "ONL");
  return setup("onh_onl_break_retest", "Overnight High/Low Break and Retest", input, level, [
    item("break", "Break durch ONH/ONL", breaksLevel(input, level), "5m", "hoch", value(level), "Warte auf 5m Close durch ONH/ONL.", "Close zurück hinter Level."),
    item("retest", "Retest", input.setup.retestConfirmed, "5m", "sehr hoch", yn(input.setup.retestConfirmed), "Warte auf Retest.", "Level hält nicht."),
    item("trigger", "Entry-Trigger", input.setup.entryTriggerConfirmed, "1m", "hoch", yn(input.setup.entryTriggerConfirmed), "Warte auf Trigger.", "Trigger scheitert.")
  ]);
}

function failedBreakoutReversal(input: SetupEngineInput): SetupEvaluation {
  const swing = findLevel(input.levels, input.direction === "LONG" ? "SWING_LOW" : "SWING_HIGH");
  return setup("failed_breakout_reversal", "Failed Breakout Reversal", input, swing, [
    item("failed", "Failed Auction bestätigt", input.setup.rejectionConfirmed, "5m", "hoch", yn(input.setup.rejectionConfirmed), "Warte auf Rückkehr in die Range.", "Breakout setzt sich fort."),
    item("micro-bos", "Gegengerichteter Micro-BOS", input.setup.entryTriggerConfirmed, "1m", "hoch", yn(input.setup.entryTriggerConfirmed), "Warte auf Micro-BOS.", "Kein Strukturwechsel."),
    item("space", "Platz zum Mean-Reversion-Ziel", input.distanceToNextLevelR >= 2, "5m", "hoch", `${input.distanceToNextLevelR.toFixed(2)}R`, "Warte auf besseren Abstand.", "Unter 2R Platz.")
  ]);
}

function liquiditySweepReclaim(input: SetupEngineInput): SetupEvaluation {
  const swing = findLevel(input.levels, input.direction === "LONG" ? "SWING_LOW" : "SWING_HIGH");
  return setup("liquidity_sweep_reclaim", "Liquidity Sweep and Reclaim", input, swing, [
    item("sweep", "Swing-Liquidität genommen", Boolean(swing), "5m", "hoch", value(swing), "Warte auf Sweep eines klaren Swing Levels.", "Kein echtes Liquiditätslevel."),
    item("reclaim", "Reclaim bestätigt", input.setup.rejectionConfirmed, "5m", "hoch", yn(input.setup.rejectionConfirmed), "Warte auf Close zurück hinter das Level.", "Kein Reclaim."),
    item("news", "Keine News-Spike-Situation", !input.setup.directNewsSpike, "session", "sehr hoch", input.setup.directNewsSpike ? "News Spike" : "frei", "Warte bis News-Risiko vorbei ist.", "Direkter News Spike.")
  ]);
}

function setup(setupId: SetupId, name: string, input: SetupEngineInput, level: KeyLevel | undefined, checklist: DynamicChecklistItem[]): SetupEvaluation {
  const hardBlocks = checklist.filter((item) => item.status === "harte Sperre").length;
  const missing = checklist.filter((item) => item.status === "fehlt" || item.status === "unklar").length;
  const passed = checklist.filter((item) => item.status === "erfüllt").length;
  const confidenceScore = Math.max(0, Math.round((passed / checklist.length) * 100 - hardBlocks * 25 - missing * 8));
  const targetDistance = Math.max(Math.abs((level?.price ?? input.currentPrice) - input.currentPrice), 1);
  const stopDistance = Math.max(targetDistance / 2, 1);
  return {
    setupId,
    name,
    direction: input.direction,
    status: hardBlocks > 0 ? "invalid" : missing > 0 ? "waiting" : "valid",
    confidenceScore,
    checklist,
    entryZone: level ? [round(level.price - 0.5), round(level.price + 0.5)] : undefined,
    triggerEntry: input.currentPrice,
    structuralStop: input.direction === "LONG" ? round(input.currentPrice - stopDistance) : round(input.currentPrice + stopDistance),
    target1: input.direction === "LONG" ? round(input.currentPrice + targetDistance) : round(input.currentPrice - targetDistance),
    target2: input.direction === "LONG" ? round(input.currentPrice + targetDistance * 2) : round(input.currentPrice - targetDistance * 2),
    nextKeyLevel: level
  };
}

function item(
  id: string,
  rule: string,
  passed: boolean,
  timeframe: DynamicChecklistItem["timeframe"],
  strength: DynamicChecklistItem["strength"],
  measuredValue: string,
  waitFor: string,
  invalidation: string
): DynamicChecklistItem {
  return {
    id,
    rule,
    status: passed ? "erfüllt" : strength === "sehr hoch" ? "harte Sperre" : "fehlt",
    timeframe,
    strength,
    measuredValue,
    explanation: passed ? "Regel erfüllt." : waitFor,
    waitFor,
    invalidation
  };
}

function findLevel(levels: KeyLevel[], kind: KeyLevel["kind"]): KeyLevel | undefined {
  return levels.find((level) => level.kind === kind);
}

function breaksLevel(input: SetupEngineInput, level: KeyLevel | undefined): boolean {
  if (!level) return false;
  return input.direction === "LONG" ? input.setup.fiveMinuteClose > level.price : input.setup.fiveMinuteClose < level.price;
}

function trendFor(direction: Direction): MarketStructure["trend"] {
  return direction === "LONG" ? "bullish" : "bearish";
}

function value(level: KeyLevel | undefined): string {
  return level ? `${level.kind} ${level.price}` : "Level nicht verfügbar";
}

function yn(value: boolean): string {
  return value ? "ja" : "nein";
}

function round(value: number): number {
  return Math.round((value + Number.EPSILON) * 100) / 100;
}
