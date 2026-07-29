# Flowdesk in 10 Minuten verwenden

## 0-2 Minuten: System und Daten pruefen

```bash
cd flowdesk-aws
npm run local:doctor
npm run dev:trading
```

Oeffne die in der Konsole angezeigte Frontend-Adresse. Unter **Datenstatus** muss die gewaehlte Session `COMPLETE` und `POST_SNAPSHOT` zeigen. Bei `PARTIAL BOOK`, Sequenzfehlern oder Luecken bleiben L3-Signale gesperrt.

## 2-4 Minuten: Replay verstehen

Waehle im **Replay** eine vollstaendige Session, starte langsam und pruefe DOM, Tape, Footprint und Heatmap gemeinsam. Die Entscheidungskarte zeigt Richtung, Datenqualitaet, Gruende, Invalidation und Strategieversion. `KEIN TRADE` ist ein gueltiges und beabsichtigtes Ergebnis.

## 4-6 Minuten: Risiko setzen

Unter **Risiko** kontrollierst du Kontotyp, Tagesverlust, maximales Risiko pro Trade, Trade-Limit, Verlustserie, erlaubte Handelszeit und Instrument. Speichere die Regeln. Ein verletztes Limit blockiert das Signal; es wird nicht nur als Warnung angezeigt.

## 6-8 Minuten: Research ausfuehren

Oeffne das **Research Lab**, waehle eine Development-Session und starte einen kurzen Lauf. Jobs bleiben in SQLite erhalten und koennen pausiert, fortgesetzt oder abgebrochen werden. Ein Fortsetzen startet deterministisch von der Quelle neu; es ist kein serialisierter In-Memory-Checkpoint.

Pruefe beim Kandidaten mindestens:

- zeitliche Splits und Purge-Fenster
- realistische oder gestresste Fill-Annahme
- Nettoergebnis nach Kommission und Slippage
- maximalen Drawdown und Stabilitaet ueber Splits
- Datenfingerprint sowie Strategie- und Modellversion

Nur Kandidaten, die das Promotion Gate bestehen, koennen `ACTIVE` werden. Ablehnung und Rollback werden auditiert.

## 8-10 Minuten: Manuell beobachten

Wechsle in den **Challenge**-Modus. Beobachte aktive Signale zuerst in Replay oder Paper Trading. Pruefe vor jeder manuellen Order im Broker: Richtung, Entry, Stop, Risiko, Invalidation, Datenstatus und verbleibendes Tageslimit.

Flowdesk sendet keine Order. Es gibt keine Garantie auf Profit oder Fehlerfreiheit. Die erste sichere Stufe ist eine gelockte Out-of-Sample-Auswertung, danach Forward Paper Trading; erst anschliessend ist eine kleine manuelle Pilotphase sinnvoll.

## Databento sicher verwenden

Eine Schaetzung im **Data Planner** ist metadata-only und loest keinen Kauf aus. Ein Download benoetigt eine frische Schaetzung, Budgetpruefung, schaetzungsspezifische Bestaetigung und den separaten Submit-Schritt. Ohne ausdrueckliche Autorisierung nichts absenden.

Der Schluessel gehoert nur in `.env.local`:

```bash
DATABENTO_API_KEY="<your-local-databento-api-key>"
```

Nie in Browser-Code, Screenshots, Git oder `NEXT_PUBLIC_*` schreiben.
