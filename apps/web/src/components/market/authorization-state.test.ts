import { describe, expect, it } from "vitest";
import { authorizationBusy, authorizationDisabledReason } from "./authorization-state";

const ready = { canSubmit: true, expired: false, acceptedTerms: true, confirmationMatches: true, idempotencyReady: true, state: "VALIDATING" as const };

describe("authorization state", () => {
  it("identifies all blocking reasons in priority order", () => {
    expect(authorizationDisabledReason({ ...ready, expired: true })).toBe("expired");
    expect(authorizationDisabledReason({ ...ready, canSubmit: false })).toBe("blocked");
    expect(authorizationDisabledReason({ ...ready, idempotencyReady: false })).toBe("preparing");
    expect(authorizationDisabledReason({ ...ready, acceptedTerms: false })).toBe("terms");
    expect(authorizationDisabledReason({ ...ready, confirmationMatches: false })).toBe("confirmation");
    expect(authorizationDisabledReason(ready)).toBeNull();
    expect(authorizationDisabledReason({ ...ready, state: "SUBMITTING" })).toBe("submitting");
  });

  it("keeps network submission states busy and terminal states stable", () => {
    expect(authorizationBusy("VALIDATING")).toBe(false);
    expect(authorizationBusy("SUBMITTING")).toBe(true);
    expect(authorizationBusy("FAILED")).toBe(false);
    expect(authorizationDisabledReason({ ...ready, state: "AUTHORIZED" })).toBe("alreadyAuthorized");
  });
});
