# Deterministic Order Book Reconstruction

Der erste Reconstructor folgt Databentos offizieller State-Management-Semantik:

- `A` Add: Order unter ihrer Order-ID einfuegen.
- `M` Modify: Preis und/oder volle neue Groesse setzen.
- `C` Cancel: gemeldete Groesse abziehen; bei Groesse null Order entfernen.
- `R` Clear: alle Orders fuer das Instrument entfernen.
- `T` Trade: Buch nicht veraendern.
- `F` Fill: Buch nicht veraendern.
- `N` None: Buch nicht veraendern.

Trade und Fill werden nicht noch einmal vom Buch abgezogen, weil Databento die resultierende Aenderung als Cancel-Record liefert. So wird Volumen nicht doppelt reduziert.

Ein Publisher-Event kann aus mehreren Records bestehen. Das Buch wird deshalb nur nach einem Record mit `F_LAST` als vollstaendig betrachtet. `databento:book:test` verarbeitet alle Records, behaelt aber nur den letzten vollstaendigen Snapshot fuer die Terminalausgabe.

Davon getrennt ist die Vollstaendigkeit des gesamten Buchzustands: Erst wenn ein Databento-Record mit `F_SNAPSHOT` verarbeitet wurde, meldet der Test `SNAPSHOT-BASED`. Bei einem kurzen Intraday-Download meldet er `PARTIAL - NO INITIAL SNAPSHOT`; Best Bid und Best Ask sind dann nur die besten beobachteten Level des Teilfensters.

## Datenstrukturen

- `orders_by_id` fuer direkten Zugriff auf Side, Preis, Groesse und Prioritaetszeit
- `SortedDict` fuer inkrementell gepflegte Bid- und Ask-Preislevel
- `PriceLevel` mit bereits aggregierter Gesamtgroesse, Orderzahl und Order-IDs
- Orderzahl und Gesamtgroesse je Preislevel
- Bids absteigend, Asks aufsteigend
- Best Bid, Best Ask und Spread in nativen Fixed-Point-Preiseinheiten
- Top 10 je Seite
- begrenzte deterministische Duplicate-Erkennung fuer buchveraendernde Events
- unbekannte Order-IDs werden gezaehlt und uebersprungen, nicht still erfunden

Add, Cancel und Modify aktualisieren nur die direkt betroffene Order und ihr Preislevel. Ein leeres Level wird sofort entfernt. `F_LAST` setzt nur den Konsistenzstatus; es erzeugt keinen Snapshot. `databento:book:test` erstellt genau einen Top-10-Snapshot nach dem letzten vollstaendigen Event.

Trade, Fill und None werden vollstaendig in der Statistik gezaehlt, aber nicht dedupliziert und veraendern das Resting Book nicht. Databento kann mehrere semantisch getrennte Fill-Records mit identischen sichtbaren Handelsfeldern liefern.

## Snapshot-Zustaende

- `NO_SNAPSHOT`: Noch kein vollstaendiges Event und kein Snapshot beobachtet.
- `SNAPSHOT_LOADING`: `F_SNAPSHOT` wurde ab dem Clear-Record erkannt; Adds werden geladen.
- `SNAPSHOT_READY`: Snapshot-Adds wurden mit abschliessendem `F_LAST` abgeschlossen.
- `PARTIAL_NATURAL_REFRESH`: Intraday-Teilfenster ohne initialen Snapshot.

Im Partial-Modus werden unbekannte Cancel- und Modify-Referenzen gezaehlt, ohne den Lauf abzubrechen. Nach `SNAPSHOT_READY` werden dieselben Faelle zusaetzlich als Integritaetswarnung gezaehlt.

## Performance

Die Laufzeit ist linear zur Recordzahl plus logarithmische `SortedDict`-Updates pro betroffenem Preislevel. Progress wird alle 100.000 Records ausgegeben. Am Ende meldet der Befehl Laufzeit, Records pro Sekunde, Peak RSS, offene Orders, Levelzahlen, Fehlerzaehler und den Snapshot-Status.

## Grenze dieser ersten Version

Der Connector ist auf ein einzelnes MES-Continuous-Requestfenster begrenzt. Records einer zweiten Instrument-ID werden gezaehlt und uebersprungen, damit Buecher nicht vermischt werden. Er implementiert noch keine Strategie, Signale, Queue-Position, Persistenz eines laufenden Buchs, Live-Subscription oder Multi-Instrument-Buecher. Das lokale Tages-Ledger besitzt noch keine Prozess-uebergreifende Dateisperre; parallele Downloads sollen deshalb nicht gestartet werden.
