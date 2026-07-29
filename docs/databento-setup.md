# Databento Historical Setup

Der Connector laeuft lokal mit Python 3. Der API-Key bleibt in der bereits vorhandenen Repository-Datei `.env.local`. Diese Datei ist in `.gitignore` eingetragen und sollte Dateirechte `0600` besitzen.

## 1. Python-Umgebung installieren

Vom Repository-Root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r apps/connectors/databento/requirements.txt
```

Alternativ fuehrt dieses Root-Script dieselben Schritte aus:

```bash
npm run databento:setup
```

## 2. Connector-Abhaengigkeiten installieren

Die Mindestabhaengigkeiten sind der offizielle `databento`-Client und `python-dotenv`. `pytest` wird fuer die Connector-Tests installiert.

```bash
.venv/bin/pip install -r apps/connectors/databento/requirements.txt
```

## 3. Kostenschaetzung ausfuehren

```bash
npm run databento:estimate -- \
  --start "2026-07-14T13:30:00Z" \
  --end "2026-07-14T13:40:00Z"
```

`metadata.get_cost(...)` laedt keine Marktdaten herunter. Eine erfolgreiche Schaetzung erzeugt ein request-genaues Receipt unter `data/databento/estimates/`.

## 4. Testdownload bestaetigen

Den folgenden Befehl erst nach Pruefung von `Estimated cost USD` und `Allowed: YES` ausfuehren:

```bash
npm run databento:download:test -- \
  --start "2026-07-14T13:30:00Z" \
  --end "2026-07-14T13:40:00Z" \
  --confirm
```

Ohne `--confirm`, ohne passendes Receipt oder bei ueberschrittenem Kostenlimit wird vor dem Datenrequest abgebrochen.

## 5. Datei validieren

Ohne `--file` wird die neueste lokale MES-Datei verwendet:

```bash
npm run databento:validate
```

Explizit:

```bash
npm run databento:validate -- \
  --file "data/databento/raw/MES/2026-07-14/MES.v.0_mbo_20260714T133000Z_20260714T134000Z.dbn.zst"
```

## 6. Events anzeigen

Die Vorschau ist hart auf maximal 100 Records begrenzt:

```bash
npm run databento:preview -- --limit 100
```

## 7. Orderbuch testen

```bash
npm run databento:book:test
```

Der Befehl verarbeitet die Datei streaming-basiert und zeigt nur den letzten vollstaendigen Zustand nach `F_LAST` sowie Top-10-Level an.

## 8. Daten loeschen

Zuerst den exakten Tagesordner pruefen, dann gezielt entfernen:

```bash
find data/databento/raw/MES/2026-07-14 -maxdepth 1 -type f -print
rm -rf -- data/databento/raw/MES/2026-07-14
```

Estimate-Receipts und das lokale Tages-Ledger koennen getrennt entfernt werden:

```bash
rm -rf -- data/databento/estimates
rm -f -- data/databento/cost-ledger.json
```

## 9. Kostenlimits aendern

Nur lokal in `.env.local` setzen:

```env
DATABENTO_MAX_REQUEST_COST_USD=1.00
DATABENTO_MAX_DAILY_COST_USD=5.00
```

Fehlen diese Werte, gelten automatisch 1,00 USD pro Request und 5,00 USD pro UTC-Tag.
