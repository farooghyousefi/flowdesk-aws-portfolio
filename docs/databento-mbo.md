# Databento MES MBO

Die Anfrage ist bewusst fest verdrahtet:

```text
dataset: GLBX.MDP3
schema: mbo
symbol: MES.v.0
stype_in: continuous
```

MBO ist Market by Order beziehungsweise L3. Jeder Datensatz enthaelt unter anderem `ts_event`, `instrument_id`, `action`, `side`, `price`, `size`, `order_id`, `sequence` und `flags`. Preise sind als Integer mit Faktor `1e-9` codiert; der Reader formatiert sie mit Decimal-Arithmetik ohne Float-Rundungsfehler.

## Dateiformat und Manifest

Der offizielle Client streamt native, Zstandard-komprimierte DBN-Dateien nach:

```text
data/databento/raw/MES/YYYY-MM-DD/
```

Neben jeder Datei liegt ein `.manifest.json` mit Requestdaten, Estimate, Downloadzeit, SHA-256, Recordzahl, Instrument-IDs, Symbolen und `historical-exchange-feed`. API-Key oder andere Secrets werden nie gespeichert.

## Validierung

Der Validator prueft:

- lesbares DBN und Schema `mbo`
- Dataset `GLBX.MDP3`
- mindestens einen Record
- Eventzeitstempel und Instrument-ID
- alle benoetigten MBO-Felder waehrend des Lesens
- bekannte Actions `A`, `C`, `M`, `R`, `T`, `F`, `N`
- MES-Metadaten ohne fremde Produkte

`rawSymbols` verwendet die im DBN-Metadatenblock gespeicherten Request-Symbole. Bei `stype_in=continuous` ist das mindestens `MES.v.0`; die tatsaechlichen Kontrakte werden ueber Instrument-IDs repraesentiert.

## Intraday-Snapshot-Grenze

Databento liefert den synthetischen historischen MBO-Gesamtsnapshot am Beginn eines UTC-Tages. Ein kurzes Fenster ab `13:30Z` enthaelt diesen Initialzustand nicht. Die Events sind echte Exchange-Daten und einzeln valide, aber ein daraus aufgebautes Buch kennt nur Orders, die innerhalb des Fensters sichtbar wurden. Der Book-Test kennzeichnet diesen Zustand als `PARTIAL - NO INITIAL SNAPSHOT` und darf nicht als vollstaendiges BBO interpretiert werden.
