import type { SessionRecord } from "./types";

export function resolveResearchSessionId(sessions: SessionRecord[], currentId: string): string {
  if (currentId && sessions.some((session) => session.id === currentId)) return currentId;
  return (sessions.find((session) => session.completeness === "complete") ?? sessions[0])?.id ?? "";
}
