import { describe, expect, it } from "vitest";
import { formatDateTime, formatNumber, translate } from "./i18n";

describe("market i18n", () => {
  it("uses German by default semantics and interpolates authorization copy", () => {
    expect(translate("de", "planner.authorizationExplain", { amount: "1,00 USD" })).toContain("höchstens 1,00 USD");
    expect(translate("de", "risk.blocked")).toBe("BLOCKIERT");
    expect(formatNumber("de", 1234.56, { minimumFractionDigits: 2 })).toBe("1.234,56");
  });

  it("provides English status, validation, and audit copy without exposing keys", () => {
    expect(translate("en", "status.RESEARCH_ONLY")).toBe("Research only");
    expect(translate("en", "status.COMPLETE_L3")).toBe("Complete L3");
    expect(translate("en", "planner.mode.orderflow_partial")).toBe("Partial orderflow");
    expect(translate("de", "split.Locked Test")).toBe("Gesperrter Test");
    expect(translate("en", "validation.MINIMUM_SESSIONS")).toBe("Too few independent sessions");
    expect(translate("en", "audit.RESEARCH_JOB_COMPLETED")).toBe("Research run completed");
  });

  it("renders dates in Europe/Berlin", () => {
    expect(formatDateTime("de", "2026-07-15T13:00:00Z", { dateStyle: undefined, timeStyle: "short" })).toBe("15:00");
    expect(formatDateTime("en", "2026-07-15T13:00:00Z", { dateStyle: undefined, timeStyle: "short" })).toBe("3:00 PM");
  });
});
