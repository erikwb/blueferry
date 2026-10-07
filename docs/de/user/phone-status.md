# Akku, Empfang und Netz des Telefons (optional)

Ist die optionale Funktion [Anrufe](calls.md) eingeschaltet, zeigt BlueFerry
auch Akkustand, Signalstärke und Netzbetreiber des iPhones. Die Werte stammen
aus den Standard-Freisprechanzeigen, die oFono vom Telefon liest. Ohne
eingeschaltete Anrufe ändert sich nichts und es wird nichts angezeigt.

## Was es tut

```mermaid
flowchart LR
    P[iPhone] -- HFP-Anzeigen --> O[oFono-Modem]
    O -- BatteryChargeLevel, Strength, Name --> B[BlueFerry-Daemon]
    B -- StatusChanged, ohne Werte --> C[Clients]
    C -- GetStatus --> B
```

- **Qt**: eine kleine Akku- und Signalanzeige neben „Conversations“; der
  Netzname erscheint beim Darüberfahren.
- **Terminal-Client, Quickshell-Kopfzeile, GTK-Statusseite**: Akku und
  Signal werden an die Verbindungszeile angehängt.
- **CLI**:

  ```bash
  blueferry phone-status          # Battery: about 60 % / Signal: 80 % / Network: …
  blueferry phone-status --json
  ```

- **Optionale Akkuwarnung**: eine Desktop-Mitteilung, wenn der Akku eine
  Schwelle erreicht.

Die Werte erscheinen, sobald das Freisprech-Modem des iPhones eingeschaltet
ist, auch wenn die Anrufsteuerung noch darauf wartet, dass es online geht.

## Einschalten

1. [Anrufe](calls.md) einrichten und einschalten
   (`BLUEFERRY_CALLS_ENABLED=true`). Für Akku, Signal und Netz braucht es
   sonst nichts.
2. Für die Akkuwarnung in `~/.config/blueferry/local.env` eintragen und das
   Backend neu starten:

   ```bash
   BLUEFERRY_PHONE_BATTERY_NOTIFY=true         # Standard: aus
   BLUEFERRY_PHONE_BATTERY_LOW_PERCENT=20      # 0-80, Standard 20
   ```

## Grenzen

- Grob: iPhones melden den Akku an Freisprechgeräte in sechs Stufen,
  BlueFerry zeigt also 0, 20, 40, 60, 80 oder 100 %. Auch das Signal bewegt
  sich in 20-%-Schritten. Eine Ladeanzeige gibt es nicht.
- Der Netzname ist, was das Telefon über HFP meldet (bis 16 Zeichen), und
  bleibt leer, solange das Telefon kein Netz hat.
- Die Warnung kommt erst wieder, wenn das Telefon mindestens 20 % über die
  Schwelle geladen wurde, und nach einem Neustart von BlueFerry noch einmal,
  falls der Akku dann noch niedrig ist.
- Bei Dual-SIM-Telefonen wird nur die Standard-Sprachleitung gemeldet.

## Datenschutz

- Die Werte liefert nur die authentifizierte Methode `GetStatus`. Clients
  erfahren von Änderungen über das argumentlose Signal `StatusChanged`;
  Werte werden nie verteilt.
- oFono fragt beim ersten Lesen des Akkus die eigene Nummer des Telefons ab.
  BlueFerry verwirft diese Nummer und speichert, protokolliert oder liefert
  sie nie.
- Logs enthalten nie die Werte oder den Netznamen.
