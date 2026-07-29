import type { DownloadAuthorizationState } from "./types";

export interface AuthorizationReadiness {
  canSubmit: boolean;
  expired: boolean;
  acceptedTerms: boolean;
  confirmationMatches: boolean;
  idempotencyReady: boolean;
  state: DownloadAuthorizationState;
}

export function authorizationBusy(state: DownloadAuthorizationState): boolean {
  return state === "SUBMITTING";
}

export function authorizationDisabledReason(value: AuthorizationReadiness): string | null {
  if (value.expired || value.state === "EXPIRED") return "expired";
  if (!value.canSubmit) return "blocked";
  if (!value.idempotencyReady) return "preparing";
  if (!value.acceptedTerms) return "terms";
  if (!value.confirmationMatches) return "confirmation";
  if (authorizationBusy(value.state)) return "submitting";
  if (["AUTHORIZED", "QUEUED", "DOWNLOADING", "IMPORTING", "VALIDATING_IMPORT", "COMPLETED"].includes(value.state)) return "alreadyAuthorized";
  return null;
}
