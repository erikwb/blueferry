# Anrufe (optional)

BlueFerry kann eingehende iPhone-Anrufe anzeigen und über die
Freisprech-Unterstützung (HFP) von
[oFono](https://git.kernel.org/pub/scm/network/ofono/ofono.git) Anrufe
annehmen, ablehnen, starten und beenden. Die Funktion ist **standardmäßig
aus** und für Nachrichten nicht nötig. Ist sie aus, spricht BlueFerry gar
nicht mit oFono und verhält sich genau wie bisher.

Ein früherer HFP-Versuch wurde aus BlueFerry entfernt, weil oFono und das
eingebaute HFP-Backend von PipeWire um dasselbe Bluetooth-Profil
konkurrieren. Diese Integration überlässt dir diese Einrichtung, bringt keine
neue Paketabhängigkeit mit und lässt den Daemon normal weiterlaufen, wenn
oFono fehlt.

## Was es tut

```mermaid
flowchart LR
    P[iPhone] -- HFP --> O[oFono]
    O -- System-Bus --> B[BlueFerry-Daemon]
    B -- CallsChanged, ohne Inhalt --> C[Clients]
    C -- Calls1.ListCalls / Answer / Dial --> B
    P -- Gesprächsaudio --> W[PipeWire / WirePlumber]
```

- Findet das oFono-Modem des iPhones, schaltet es ein, solange die
  Bluetooth-Classic-Verbindung steht, und bringt es online (iOS macht das
  nicht von selbst).
- Zeigt bei einem eingehenden Anruf eine Desktop-Mitteilung mit **Answer**
  und **Decline**. Sie schließt sich, sobald es nicht mehr klingelt.
- Der Qt-Client bekommt einen Dialog **Phone Calls**, der Terminal-Client
  ein Anruf-Panel (`c`) und die CLI die Befehlsgruppe `calls`:

  ```bash
  blueferry calls                 # Zustand und laufende Anrufe
  blueferry calls dial '+41 79 123 45 67'
  blueferry calls answer
  blueferry calls dtmf 1234#      # Töne im aktiven Anruf
  blueferry calls hangup          # oder: hangup --all
  ```

- Stellt die Anruflautstärke von oFono auf 100 %, weil die
  Standardeinstellung von 50 % mit einem iPhone kaum hörbar ist.

Das Audio selbst leitet PipeWire. BlueFerry steuert nur den Anruf.

## Einschalten

1. oFono (entwickelt mit 2.18) als Systemdienst installieren und starten.
   BlueFerry startet es nie selbst.
2. Deinem Benutzer den Zugriff auf oFono erlauben. Die mitgelieferte Policy
   lässt nur root und `at_console`-Sitzungen zu. Lege
   `/etc/dbus-1/system.d/ofono-local.conf` an:

   ```xml
   <!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
    "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
   <busconfig>
     <policy user="dein-login">
       <allow send_destination="org.ofono"/>
     </policy>
   </busconfig>
   ```

   Damit kann jeder Prozess dieses Benutzers oFono vollständig steuern, nicht
   nur BlueFerry.
3. WirePlumber soll HFP an oFono übergeben, etwa in
   `~/.config/wireplumber/wireplumber.conf.d/51-bluez-ofono.conf`:

   ```text
   monitor.bluez.properties = {
     bluez5.hfphsp-backend = "ofono"
   }
   ```

   Setze dort keine `bluez5.roles`; BlueFerrys Telefon-Audio-Fragment behält
   die Freisprech-Rollen, solange Anrufe an sind.
4. Ab BlueZ 5.87 das eigene HFP-Plugin von BlueZ abschalten, damit es oFono
   den Kanal nicht wegnimmt: `bluetoothd` mit `-P hfp` starten.
5. In `~/.config/blueferry/local.env` eintragen, danach Backend und
   WirePlumber neu starten:

   ```bash
   BLUEFERRY_CALLS_ENABLED=true
   ```

`blueferry calls` sollte dann `ready` zeigen, solange das iPhone verbunden
ist.

## Grenzen

- Experimentell. Getestet mit einem iPhone (iOS 27) auf einem
  Gentoo/OpenRC-Desktop: Das Modem kommt hoch, eingehende Anrufe klingeln,
  Anrufe lassen sich annehmen und beenden. Anklopfen, das Beenden eines
  gehaltenen Anrufs und DTMF sind ungetestet.
- Der Startwettlauf zwischen oFono und WirePlumber ist nicht gelöst. Bleibt
  der Zustand bei `searching`, starte oFono nach WirePlumber neu.
- `dial` nimmt nur einfache Nummern. `*` und `#` werden abgelehnt, weil sie
  Servicecodes bilden würden (etwa Rufumleitung), statt anzurufen. Für
  Tastentöne im Gespräch gibt es `dtmf`.
- Dual-SIM-Telefone zeigen über HFP nur die Standard-Sprachleitung.
- Wählen ist begrenzt (6 pro Minute, 60 pro Stunde).

## Datenschutz

- Nummern und Namen liefert nur die authentifizierte, ratenbegrenzte Methode
  `Calls1.ListCalls`. Das verteilte Signal `CallsChanged` enthält nichts.
- Anrufe landen nicht im Nachrichtenverlauf.
- Die Mitteilung verbirgt den Anrufer bei
  `BLUEFERRY_SHOW_NOTIFICATION_CONTENT=false`.
- Logs enthalten Anrufzustände und IDs, nie Nummern oder Namen.
