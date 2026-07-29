import type { ApprovalResult, CriteriaStatus, Direction, TradeCriteria } from "@/lib/types";

export function createDefaultCriteria(direction: Direction): TradeCriteria[] {
  const applies = (appliesTo?: Direction): boolean => !appliesTo || appliesTo === direction;
  const rows: TradeCriteria[] = [
    criteria("market-structure", "HTF-Marktstruktur klar?", "15-30m", "hoch", "Trendrichtung und Bias", "Warte auf klare HH/HL oder LH/LL Struktur."),
    criteria("long-structure", "Long: Higher High und Higher Low?", "15m", "hoch", "Long-Bias muss strukturell bestätigt sein.", "Warte auf bestätigtes Higher Low.", "LONG"),
    criteria("short-structure", "Short: Lower High und Lower Low?", "15m", "hoch", "Short-Bias muss strukturell bestätigt sein.", "Warte auf bestätigtes Lower High.", "SHORT"),
    criteria("vwap", "Preis auf korrekter Seite des VWAP?", "5m", "mittel", "VWAP bestätigt den Intraday-Bias.", "Warte, bis der Preis auf der richtigen VWAP-Seite handelt."),
    criteria("ema", "EMA 20 und EMA 50 unterstützen die Richtung?", "5m", "mittel", "Trendfilter gegen Chasing und Seitwärtsphasen.", "Warte auf EMA-Ausrichtung in Trade-Richtung."),
    criteria("level", "Wichtiges Level markiert?", "15m / 30m / Daily", "hoch", "Reaktionszonen müssen vor dem Entry bekannt sein.", "Markiere das relevante Level vor dem Trade."),
    criteria("pdh-pdl", "PDH, PDL, ONH oder ONL berücksichtigt?", "Daily / 15m", "hoch", "Vorherige Hochs und Tiefs beeinflussen Liquidität.", "Prüfe PDH, PDL, ONH und ONL."),
    criteria("bos", "Break of Structure auf dem Setup-Timeframe vorhanden?", "5m", "hoch", "Setup-Aktivierung.", "Warte auf einen bestätigten Break of Structure im 5-Minuten-Chart."),
    criteria("pullback", "Pullback oder Retest sauber?", "5m", "sehr hoch", "Entry-Zone nach Aktivierung.", "Warte auf Pullback oder Retest des gebrochenen Levels."),
    criteria("no-chase", "Kein direktes Chasing des Impulses?", "1-5m", "hoch", "Verhindert impulsive schlechte Entries.", "Warte auf Struktur statt in die Bewegung zu springen.", undefined, true),
    criteria("entry-trigger", "Entry-Trigger auf dem Entry-Timeframe bestätigt?", "1-2m", "hoch", "Feintiming für Entry.", "Warte auf Kerzen-/Orderflow-Trigger im Entry-Timeframe."),
    criteria("delta", "Footprint oder Delta bestätigt die Richtung?", "1-2m", "mittel", "Orderflow-Bestätigung.", "Warte auf Delta, Absorption oder aggressiven Flow in Richtung."),
    criteria("news", "Keine High-Impact-News unmittelbar bevorstehend?", "Kalender", "hoch", "News können Setup-Qualität entwerten.", "Warte bis High-Impact-News vorbei sind.", undefined, true),
    criteria("two-r", "Mindestens 2R bis zum nächsten relevanten Level?", "5-15m", "hoch", "Ausreichender Raum bis zur Reaktionszone.", "Warte auf mehr Platz zum Ziel oder passe den Plan an.", undefined, true),
    criteria("risk", "Risiko innerhalb des Limits?", "Trade Plan", "sehr hoch", "Challenge-Drawdown schützen.", "Reduziere Kontrakte oder vergrößere die Planqualität.", undefined, true),
    criteria("revenge", "Kein Revenge Trading?", "Selbstcheck", "sehr hoch", "Emotionale Trades sind harte Stopps.", "Pausiere und dokumentiere den Impuls.", undefined, true),
    criteria("fomo", "Kein FOMO?", "Selbstcheck", "hoch", "Verhindert spät eröffnete Trades.", "Warte auf einen sauberen Re-Test statt hinterherzulaufen.", undefined, true),
    criteria("daily-loss", "Tagesverlustlimit noch nicht erreicht?", "Challenge", "sehr hoch", "Tagesrisiko muss erhalten bleiben.", "Trading stoppen: persönliches Tagesverlustlimit erreicht.", undefined, true),
    criteria("trade-count", "Maximale Trade-Anzahl noch nicht erreicht?", "Tagesplan", "hoch", "Overtrading vermeiden.", "Keine weiteren Trades ohne Reset oder Review.", undefined, true),
    criteria("playbook", "Setup entspricht einem gespeicherten Playbook?", "Playbook", "hoch", "Nur geprüfte Setups handeln.", "Wähle oder aktualisiere ein passendes Playbook-Setup.")
  ];

  return rows.filter((row) => applies(row.appliesTo));
}

function criteria(
  id: string,
  name: string,
  timeframe: string,
  strength: TradeCriteria["strength"],
  explanation: string,
  waitFor: string,
  appliesTo?: Direction,
  hardBlock = false
): TradeCriteria {
  return {
    id,
    name,
    required: true,
    hardBlock,
    timeframe,
    strength,
    explanation,
    waitFor,
    status: "unknown",
    appliesTo
  };
}

export function updateCriteria(
  criteriaRows: TradeCriteria[],
  id: string,
  status: CriteriaStatus,
  comment?: string
): TradeCriteria[] {
  return criteriaRows.map((row) => (row.id === id ? { ...row, status, comment } : row));
}

export function evaluateTradeApproval(criteriaRows: TradeCriteria[]): ApprovalResult {
  const blockers = criteriaRows.filter((row) => row.required && row.hardBlock && row.status === "no");
  const missing = criteriaRows.filter((row) => row.required && row.status !== "yes");

  if (blockers.length > 0) {
    return {
      state: "blocked",
      title: "TRADE VERBOTEN",
      message: blockers[0]?.waitFor ?? "Eine harte Regel wurde verletzt.",
      missing,
      blockers
    };
  }

  if (missing.length > 0) {
    return {
      state: "waiting",
      title: "NICHT TRADEN",
      message: missing[0]?.waitFor ?? "Setup-Kriterien sind noch nicht vollständig erfüllt.",
      missing,
      blockers: []
    };
  }

  return {
    state: "allowed",
    title: "TRADE ERLAUBT",
    message: "Alle verpflichtenden Bedingungen sind erfüllt.",
    missing: [],
    blockers: []
  };
}
