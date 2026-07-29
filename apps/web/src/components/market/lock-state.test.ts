import { describe, expect, it } from "vitest";
import { deriveApplicationLockState } from "./lock-state";

describe("deriveApplicationLockState", () => {
  it("does not lock for an inactive locked protocol", () => {
    expect(deriveApplicationLockState({ blind: { mode: "locked", planId: "plan", runId: null, status: "NOT_STARTED", futureSeekAllowed: true, settingsLocked: false } }).locked).toBe(false);
  });

  it("locks only for an active locked run", () => {
    const result = deriveApplicationLockState({
      session: { id: "session" } as never,
      blind: { mode: "locked", planId: "plan", runId: "run", status: "ACTIVE", futureSeekAllowed: false, settingsLocked: true },
    });
    expect(result).toMatchObject({ locked: true, reason: "active_locked_run", protocolId: "plan", runId: "run", sessionId: "session" });
  });

  it("keeps Practice editable", () => {
    expect(deriveApplicationLockState({ blind: { mode: "practice", planId: "practice", runId: "run", status: "ACTIVE", futureSeekAllowed: true, settingsLocked: false } })).toMatchObject({ locked: false, reason: "none" });
  });
});
