import type { OhlcvBar, RuleEvaluation, RuleInput } from "@trading-assistant/shared-types";
import { evaluateRules } from "./index";

export interface ReplayFrame {
  index: number;
  bar: OhlcvBar;
  evaluation: RuleEvaluation;
}

export interface ReplayReport {
  frames: ReplayFrame[];
  signalCount: number;
  allowedCount: number;
  blockedCount: number;
  waitingCount: number;
}

export function replayClosedBars(bars: OhlcvBar[], buildInput: (barsUntilNow: OhlcvBar[], current: OhlcvBar) => RuleInput): ReplayReport {
  const closed = bars.filter((bar) => bar.isClosed).sort((a, b) => a.endTime.localeCompare(b.endTime));
  const frames = closed.map((bar, index) => {
    const barsUntilNow = closed.slice(0, index + 1);
    return { index, bar, evaluation: evaluateRules(buildInput(barsUntilNow, bar)) };
  });
  return {
    frames,
    signalCount: frames.filter((frame) => frame.evaluation.findings.some((finding) => finding.status === "passed")).length,
    allowedCount: frames.filter((frame) => frame.evaluation.status === "TRADE_ERLAUBT").length,
    blockedCount: frames.filter((frame) => frame.evaluation.status === "TRADE_VERBOTEN").length,
    waitingCount: frames.filter((frame) => frame.evaluation.status === "WARTEN").length
  };
}
