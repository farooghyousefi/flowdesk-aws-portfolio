# Databento Cost Control

Der Connector trennt Schaetzung und Download strikt.

1. `databento:estimate` validiert Symbol und Zeitraum lokal.
2. `Historical.metadata.get_cost(...)` liefert die Schaetzung, ohne Daten herunterzuladen.
3. Ein Receipt bindet Dataset, Schema, Symbol, Symbology, Start und Ende aneinander.
4. `databento:download:test` akzeptiert nur ein maximal 30 Minuten altes, exakt passendes Receipt.
5. Vor `Historical.timeseries.get_range(...)` werden Request- und UTC-Tageslimit erneut geprueft.
6. Nach einem erfolgreichen, validierten Download wird der geschaetzte Betrag im lokalen Ledger addiert.

## Standardlimits

```env
DATABENTO_MAX_REQUEST_COST_USD=1.00
DATABENTO_MAX_DAILY_COST_USD=5.00
```

Ein Betrag gleich dem Limit ist erlaubt. Ein hoeherer Betrag wird blockiert. Das Tageslimit basiert auf erfolgreich heruntergeladenen Schaetzbetragen und ist ein lokaler Schutzmechanismus, keine Abrechnungsauskunft von Databento. Die tatsaechliche Rechnung kann vom Estimate abweichen.

## Geblockte Requests

- leeres Symbol
- Wildcards wie `*`, `?`, `[` oder `]`
- `ALL_SYMBOLS`
- Listen, Kommas oder mehrere Symbole
- andere Symbole als `MES.v.0`
- ungueltige oder zeitzonenlose Zeitstempel
- Ende vor oder gleich Start
- mehr als 60 Minuten
- Kosten ueber dem Requestlimit
- Download ohne Receipt oder `--confirm`
- Download, der das lokale Tageslimit ueberschreitet

Receipts, Ledger, DBN-Dateien und Manifeste befinden sich unter `data/databento/` und werden durch Git ignoriert.
