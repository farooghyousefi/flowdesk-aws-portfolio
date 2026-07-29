# Databento Historical Connector

Lokaler, serverseitiger Python-Connector fuer einen eng begrenzten Historical-Download von `MES.v.0` aus `GLBX.MDP3` im MBO-Schema. Er verwendet den offiziellen Databento-Python-Client und uebertraegt den API-Key weder an die Next.js-App noch an Browsercode.

## Schnellstart

```bash
npm run databento:setup
npm run databento:estimate -- --start "2026-07-14T13:30:00Z" --end "2026-07-14T13:40:00Z"
```

Erst nach gepruefter Schaetzung:

```bash
npm run databento:download:test -- \
  --start "2026-07-14T13:30:00Z" \
  --end "2026-07-14T13:40:00Z" \
  --confirm
```

Der Download nutzt `Historical.timeseries.get_range(...)` und streamt die komprimierte native DBN-Datei direkt auf die lokale Platte. Die Kostenschaetzung nutzt `Historical.metadata.get_cost(...)` und laedt keine Marktdaten herunter.

## Schutzregeln

- Erlaubt ist nur `MES.v.0` mit `stype_in=continuous`.
- Wildcards, `ALL_SYMBOLS`, mehrere Symbole und Zeitraeume ueber 60 Minuten werden lokal blockiert.
- Pro Request gelten standardmaessig 1,00 USD, pro UTC-Tag 5,00 USD.
- Ein Estimate-Receipt gilt 30 Minuten und muss exakt zum Download passen.
- DBN-Daten und lokale Kostenmetadaten liegen unter `data/databento/` und werden nicht committed.
- Fehlertexte werden vor der Ausgabe von API-Key-Mustern und dem geladenen Key bereinigt.

Der MBO-Book-Test verwendet `sortedcontainers.SortedDict` fuer inkrementelle Bid-/Ask-Level. Dadurch wird bei `F_LAST` keine Vollaggregation ueber alle offenen Orders mehr ausgefuehrt; ein Top-10-Snapshot entsteht nur am Dateiende.

## Deterministische lokale Pruefung

`validate`, `preview` und `book` akzeptieren entweder `--file` oder `--latest`. Ohne beide Optionen werden alle gefundenen Dateien aufgelistet und die deterministische Auswahl nach relativem Pfad klar ausgegeben. Eine stille Auswahl nach Dateiaenderungszeit findet nicht statt.

```bash
npm run databento:validate -- --file /absoluter/pfad/zur/datei.dbn.zst
npm run databento:book:test -- --latest
```

Der Book-Test trennt `PRE_SNAPSHOT`, `SNAPSHOT_LOADING`, `SNAPSHOT_READY` und `POST_SNAPSHOT`. Snapshot-Sequenzen und Natural-Feed-Sequenzen werden getrennt ausgewertet; unbekannte Cancel-, Modify- und Fill-Referenzen werden zusaetzlich nach Phase ausgewiesen.

## MBO gegen MBP-10 verifizieren

Der Estimate-Befehl ermittelt zuerst den konkreten Kontrakt ueber Databento Symbology Resolution. Er verwendet danach die Instrument-ID mit `stype_in=instrument_id`, bestimmt automatisch ein maximal zwei Sekunden langes Fenster ab dem ersten natuerlichen `F_LAST` nach `SNAPSHOT_READY` und laedt keine Marktdaten herunter:

```bash
npm run databento:verify:estimate -- \
  --mbo-file /absoluter/pfad/zur/mbo-datei.dbn.zst \
  --limit 1000
```

Der sichere Standard ist 1.000 Records; die nicht ueberschreibbare harte Grenze liegt bei 10.000 Records. Derselbe Limit-Wert wird an `metadata.get_cost`, `metadata.get_billable_size` und `timeseries.get_range` uebergeben. Falls die 1.000-Record-Schaetzung ueber dem Requestlimit liegt, wird automatisch nur eine weitere Schaetzung mit 100 Records ausgefuehrt.

Nur ein frisches, erlaubtes Estimate-Receipt plus `--confirm` kann den passenden Download starten:

```bash
npm run databento:verify:book -- \
  --mbo-file /absoluter/pfad/zur/mbo-datei.dbn.zst \
  --limit 1000 \
  --confirm
```

Referenzen werden unter `data/databento/reference/mbp-10/MES/` abgelegt. Der Vergleich erfolgt ausschliesslich an `F_LAST`-Eventgrenzen, ordnet Zustaende anhand Instrument, Publisher, Timestamp und Sequence zu und verwendet exakte Integerwerte fuer Preis, Groesse und Orderanzahl ohne Toleranz. Text- und JSON-Berichte landen unter `data/databento/reports/book-verification/`.

## Offizielle Referenzen

- [Historical metadata.get_cost](https://databento.com/docs/api-reference-historical/metadata/metadata-get-cost)
- [Historical timeseries.get_range](https://databento.com/docs/api-reference-historical/timeseries/timeseries-get-range)
- [Market by order schema](https://databento.com/docs/schemas-and-data-formats/mbo)
- [State management of resting orders](https://databento.com/docs/examples/order-book/order-tracking)
