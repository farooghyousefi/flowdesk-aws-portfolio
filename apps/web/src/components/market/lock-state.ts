import type { ApplicationLockState, ReplayState } from "./types";

export function deriveApplicationLockState(state: Pick<ReplayState, "applicationLock" | "blind" | "session">): ApplicationLockState {
  if (state.applicationLock) return state.applicationLock;
  const blind = state.blind;
  if (blind?.mode === "locked" && blind.runId && blind.status === "ACTIVE") {
    return {
      locked: true,
      reason: "active_locked_run",
      protocolId: blind.planId,
      runId: blind.runId,
      sessionId: state.session?.id,
    };
  }
  if (blind?.mode === "locked" && blind.planId) {
    return { locked: false, reason: "locked_protocol_not_running", protocolId: blind.planId };
  }
  return { locked: false, reason: "none", protocolId: blind?.planId };
}
