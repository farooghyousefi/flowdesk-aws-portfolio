import { describe, expect, it } from "vitest";
import { resolveResearchSessionId } from "./research-selection";
import type { SessionRecord } from "./types";

function session(id: string, completeness: "complete" | "partial"): SessionRecord {
  return { id, completeness } as SessionRecord;
}

describe("research session selection", () => {
  it("selects a complete session after sessions load asynchronously", () => {
    expect(resolveResearchSessionId([], "")).toBe("");
    expect(resolveResearchSessionId([session("partial", "partial"), session("complete", "complete")], "")).toBe("complete");
  });

  it("keeps an existing valid user selection", () => {
    const sessions = [session("complete", "complete"), session("partial", "partial")];
    expect(resolveResearchSessionId(sessions, "partial")).toBe("partial");
  });
});
