import type { Locale } from "./i18n";
import type { SessionRecord } from "./types";

function calendarDate(iso: string, locale: Locale): string {
  const [year = "", month = "", day = ""] = iso.slice(0, 10).split("-");
  return locale === "de" ? `${day}.${month}.${year}` : `${month}/${day}/${year}`;
}

export function replaySessionLabel(session: SessionRecord, locale: Locale): string {
  const startDateIso = session.start_at.slice(0, 10);
  const endDateIso = session.end_at.slice(0, 10);
  const startDate = calendarDate(session.start_at, locale);
  const startTime = session.start_at.slice(11, 16);
  const endTime = session.end_at.slice(11, 16);
  const endLabel = endDateIso === startDateIso
    ? endTime
    : `${calendarDate(session.end_at, locale)} ${endTime}`;
  const modeLabel = session.data_health.fullL3Claim
    ? "Full L3"
    : session.completeness === "complete"
      ? (locale === "de" ? "Vollständiger Snapshot" : "Complete snapshot")
      : (locale === "de" ? "Partielle Session" : "Partial session");

  return `${session.contract_symbol} · ${startDate} · ${startTime}–${endLabel} UTC · ${new Intl.NumberFormat(locale).format(session.record_count)} · ${modeLabel}`;
}
