import jsPDF from "jspdf";
import type { JournalState, Trade } from "@/lib/types";

export function tradesToCsv(trades: Trade[]): string {
  const headers = [
    "Trade-ID",
    "Datum",
    "Uhrzeit",
    "Markt",
    "Richtung",
    "Setup",
    "Entry",
    "Stop",
    "Target",
    "Exit",
    "Kontrakte",
    "Risiko",
    "Netto-PnL",
    "R-Multiple",
    "Regelkonform"
  ];
  const rows = trades.map((trade) => [
    trade.id,
    trade.date,
    trade.time,
    trade.market,
    trade.direction,
    trade.setupName,
    trade.entry,
    trade.stop,
    trade.target,
    trade.exit,
    trade.contracts,
    trade.risk,
    trade.netPnl,
    trade.rMultiple,
    trade.ruleCompliant ? "Ja" : "Nein"
  ]);
  return [headers, ...rows]
    .map((row) => row.map((cell) => `"${String(cell).replaceAll("\"", "\"\"")}"`).join(","))
    .join("\n");
}

export function downloadText(filename: string, content: string, mime: string): void {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

export function exportCsv(trades: Trade[]): void {
  downloadText("trades.csv", tradesToCsv(trades), "text/csv;charset=utf-8");
}

export function exportJson(state: JournalState): void {
  downloadText("futures-journal-backup.json", JSON.stringify(state, null, 2), "application/json");
}

export function exportExcel(trades: Trade[]): void {
  const headers = ["TradeID", "Datum", "Uhrzeit", "Markt", "Richtung", "Setup", "Entry", "Stop", "Target", "Exit", "Kontrakte", "Risiko", "NettoPnL", "RMultiple", "Regelkonform"];
  const rows = trades.map((trade) => [
    trade.id,
    trade.date,
    trade.time,
    trade.market,
    trade.direction,
    trade.setupName,
    trade.entry,
    trade.stop,
    trade.target,
    trade.exit,
    trade.contracts,
    trade.risk,
    trade.netPnl,
    trade.rMultiple,
    trade.ruleCompliant ? "Ja" : "Nein"
  ]);
  const html = `<!doctype html><html><head><meta charset="utf-8"></head><body><table>${[headers, ...rows]
    .map((row) => `<tr>${row.map((cell) => `<td>${escapeHtml(String(cell))}</td>`).join("")}</tr>`)
    .join("")}</table></body></html>`;
  downloadText("trades.xls", html, "application/vnd.ms-excel;charset=utf-8");
}

export function exportTradeReviewPdf(trade: Trade): void {
  const doc = new jsPDF();
  doc.setFont("helvetica", "bold");
  doc.setFontSize(16);
  doc.text(`Trade Review ${trade.id}`, 14, 18);
  doc.setFont("helvetica", "normal");
  doc.setFontSize(10);
  const lines = [
    `Datum: ${trade.date} ${trade.time}`,
    `Markt: ${trade.market} ${trade.direction}`,
    `Setup: ${trade.setupName}`,
    `Netto-P&L: ${trade.netPnl.toFixed(2)} USD`,
    `R-Multiple: ${trade.rMultiple.toFixed(2)}R`,
    `Regelkonform: ${trade.ruleCompliant ? "Ja" : "Nein"}`,
    `Entry-Grund: ${trade.entryReason}`,
    `Exit-Grund: ${trade.exitReason}`,
    `Emotion: ${trade.emotion}`,
    `Fehler: ${trade.mistakes}`,
    `Learning: ${trade.learning}`
  ];
  lines.forEach((line, index) => doc.text(line, 14, 32 + index * 8));
  doc.save(`${trade.id}-review.pdf`);
}

function escapeHtml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll("\"", "&quot;")
    .replaceAll("'", "&#039;");
}
