# Fehlerbehebung

Fang mit der iPhone-Seite im Client an. Sie zeigt Nachrichten, Kontakte und
iPhone-Mitteilungen getrennt an. Nachrichten und Kontakte können
funktionieren, auch wenn die optionale Mitteilungsverbindung es nicht tut.

Prüfung der Voraussetzungen und Logs:

```bash
blueferry doctor
journalctl --user -u blueferry -f
```

## Nachrichten kommen an, aber Namen fehlen

Nutze **Sync Contacts** im Client oder `blueferry contacts-sync`. Prüfe, dass
**Kontakte synchronisieren** (englisch: **Sync Contacts**) in den
Bluetooth-Einstellungen des iPhones für den Rechner eingeschaltet ist.

Wenn eine lokale Nummer wie die australische `0412345678` nicht zur Antwort
von `+61412345678` passt, prüfe die Nummernregion. BlueFerry verwendet das
Land aus `LC_ALL`, danach `LC_TELEPHONE`, danach `LANG`. Zum Überschreiben
trage `BLUEFERRY_PHONE_REGION=AU` (einen zweistelligen Ländercode) in
`~/.config/blueferry/local.env` ein und starte den Dienst mit
`systemctl --user restart blueferry` neu. Ohne erkannte Region nimmt BlueFerry
kein Land für lokale Nummern an.

Wenn eine frühere Version das `+` aus Kontakten entfernt hat, führe nach dem
Update `blueferry contacts-sync` aus. Nur ein erneuter Abruf vom iPhone kann
die ursprünglichen internationalen Vorwahlen im Cache wiederherstellen.

## Die Schalter erscheinen nicht auf dem iPhone

Die Schalter **Mitteilungen zu Nachrichten anzeigen** und
**Kontakte synchronisieren** können ein paar Minuten brauchen. Öffne die
**ⓘ**-Seite des Rechners mehrmals neu und prüfe beide Einträge, falls das
iPhone den Rechner zweimal auflistet. In Tests erschienen sie nur, solange
BlueFerrys Bluetooth-LE-Advertisement aktiv war. Der Adapter braucht also
funktionierendes LE (siehe [Bluetooth LE ist am Adapter ausgeschaltet](#bluetooth-le-ist-am-adapter-ausgeschaltet)).

## Nachrichten melden „connection refused“

Das iPhone bedient immer nur eine Nachrichtenverbindung (MAP) gleichzeitig.
Ein anderer Rechner, der mit demselben Telefon gekoppelt ist, kann sie
belegen. Trenne diesen Rechner oder schalte dort Bluetooth aus. Eine veraltete
oder unvollständige Kopplung kann ähnlich aussehen; dann hilft
[sauber neu beginnen](pairing.md#sauber-neu-beginnen).

## Senden funktioniert, aber eingehende Nachrichten kommen nie an

Für eingehende MAP-Nachrichten braucht das iPhone einen lokalen **Message
Notification Server (MNS)** auf dem Rechner. Senden und Kontaktabgleich können
auch ohne diesen Dienst funktionieren. Führe bei verbundenem BlueFerry
`blueferry doctor` aus: Es prüft die MNS-UUID
`00001133-0000-1000-8000-00805f9b34fb` am konfigurierten Adapter. Eine vorhandene
UUID bestätigt die lokale Registrierung, aber noch keine Verbindung des iPhones.

Fehlt MNS trotz laufendem obexd, prüfe dessen Logs und Startoptionen. Bei systemd:

```bash
journalctl --user -u obex.service -b
systemctl --user cat obex.service
```

Eine bestätigte Ursache ist ein BlueZ-Initialisierungsfehler in obexd-Builds mit
Evolutions Adressbuch-Backend. Er wurde unter Debian 13 mit BlueZ 5.82 in
[BlueFerry #280](https://github.com/erikwb/blueferry/issues/280) bestätigt und in
[BlueZ #2315](https://github.com/bluez/bluez/issues/2315) beschrieben. obexd kann
starten, ohne seine Serverprofile zu registrieren. Die Logs können
`Unable to acquire registry` und einen fehlenden
`evolution-source-registry.service` melden. Die fehlende MNS-UUID allein beweist
diese Ursache nicht; deaktivierte Plugins oder ein noch startender Dienst können
sie ebenfalls erklären.

Für diesen Initialisierungsfehler wurde folgende Umgehung bestätigt:

1. Öffne `systemctl --user edit obex.service` und füge den folgenden Override
   hinzu. Verwende den obexd-Pfad aus `systemctl --user cat obex.service` und
   behalte vorhandene Argumente bei. Das Beispiel verwendet Debians Pfad:

   ```ini
   [Service]
   ExecStart=
   ExecStart=/usr/libexec/bluetooth/obexd -P irmc,pbap
   ```

2. Führe `systemctl --user daemon-reload` und anschließend
   `systemctl --user restart obex.service` aus. Das unterbricht laufende
   OBEX-Übertragungen sowie Nachrichten- und Kontaktsitzungen. Lass BlueFerry
   neu verbinden, führe `blueferry doctor` erneut aus und teste eine eingehende
   Nachricht.

`-P irmc,pbap` deaktiviert die IrMC- und Telefonbuch-**Serverplugins** des Rechners.
BlueFerrys Telefonbuch-**Client**, der Kontakte vom iPhone liest, bleibt verfügbar.
Andere Apps, die das Adressbuch des Rechners per Bluetooth bereitstellen, können
diese Serverplugins benötigen. Ist bereits `-P` oder `--noplugin` gesetzt, ergänze
`irmc,pbap` in dieser Liste statt einer zweiten Option. Ohne systemd ergänze die
Option im vorhandenen obexd-Starter der Sitzung. Zum Rückgängigmachen entferne
den hinzugefügten Override und lade/starte obex.service erneut.

## Nachrichten und Kontakte gehen, iPhone-Mitteilungen verbinden nie

Meist ist dann die Bluetooth-LE-Hälfte der Kopplung veraltet. Eine Kopplung
umfasst zwei Verbindungen: Bluetooth Classic für Nachrichten und Kontakte und
Bluetooth LE für Mitteilungen. Beide können auseinanderlaufen, typischerweise
wenn die Kopplung nur am Rechner entfernt wurde.

Typische Anzeichen:

- Nachrichten und Kontakte funktionieren normal.
- iPhone-Mitteilungen werden nie verbunden.
- Die LE-Verbindung baut sich alle paar Sekunden auf und bricht wieder ab.
  `btmon` zeigt, dass die LE-Verschlüsselung mit dem gespeicherten Schlüssel
  scheitert und danach getrennt wird.

Weder BlueZ noch BlueFerry können den Schlüssel reparieren. Setze beide Seiten
zurück und kopple neu:

1. Auf dem iPhone: **Einstellungen → Bluetooth → ⓘ** neben dem Rechner →
   **Dieses Gerät ignorieren** (englisch: **Forget This Device**). Entferne
   jeden Eintrag mit dem Namen des Rechners.
2. Unter Linux: `bluetoothctl remove <iPhone-Adresse>`.
3. [Neu koppeln](pairing.md).

## Bluetooth LE ist am Adapter ausgeschaltet

Manche Systeme betreiben einen Dual-Mode-Adapter mit ausgeschaltetem LE. Die
Kopplung klappt dann über Bluetooth Classic, aber das LE-Advertisement startet
nicht. Die Schalter auf dem iPhone erscheinen nicht, und Mitteilungen können
sich nicht verbinden.

Vergleiche, was der Adapter kann, mit dem, was gerade eingeschaltet ist:

```bash
sudo btmgmt info
```

Steht `le` unter **supported settings**, aber nicht unter
**current settings**, ist LE aus. Häufigste Ursache ist diese Zeile in
`/etc/bluetooth/main.conf`:

```ini
[General]
ControllerMode = bredr
```

`ControllerMode = dual` setzen (oder die Zeile entfernen) und Bluetooth neu
starten:

```bash
sudo systemctl restart bluetooth   # OpenRC: sudo rc-service bluetooth restart
```

`sudo btmgmt le on` allein hilft hier nicht. Mit `ControllerMode = bredr`
bietet BlueZ für den Adapter keine LE-Advertisements an, bis es im Dual-Modus
neu startet. Steht `ControllerMode` nicht auf `bredr`, schaltet ein
Bluetooth-Neustart LE von selbst wieder ein.

Danach neu koppeln. Der Kopplungsassistent und der KDE-Client können erneut
prüfen, ohne von vorn zu beginnen.

## bluetoothd hängt und lässt sich nicht neu starten

Selten bleibt `bluetoothd` im Kernel hängen. Bluetooth reagiert nicht mehr,
`bluetoothctl` hängt, und ein Neustart des Bluetooth-Dienstes wird nicht
fertig. Prüfe den Prozesszustand:

```bash
ps -o pid,stat,cmd -C bluetoothd
```

Ein `D` in der Spalte `STAT` heißt, dass der Prozess ununterbrechbar im Kernel
wartet. Er lässt sich nicht beenden; **nur ein Neustart des Rechners hilft**.
Das ist ein Kernel-Fehler, kein Problem von BlueFerry oder BlueZ. Bitte melde
ihn auf der
[linux-bluetooth-Mailingliste](https://subspace.kernel.org/vger.kernel.org.html)
(`linux-bluetooth@vger.kernel.org`), mit Kernel-Version, Adaptermodell und
der Ausgabe von `dmesg` und `sudo cat /proc/<pid>/stack`, erfasst vor dem
Neustart.

BlueFerry erkennt so einen Hänger selbst, pausiert seine eigene
Bluetooth-Arbeit und meldet das in jedem Client; siehe
[Neu verbinden und Selbstheilung](reconnect.md).

## Mitteilungen funktionierten, jetzt nicht mehr

Haben iPhone-Mitteilungen mit demselben Telefon und Adapter schon
funktioniert und bleiben fünf Minuten lang weg, schaltet BlueFerry den Adapter
unter Umständen einmal aus und wieder ein. Zuerst versucht es einen reinen
LE-Reset. Es verzichtet darauf, solange ein anderes Bluetooth-Gerät mit dem
Adapter gekoppelt oder verbunden ist, eine Gerätesuche oder eine Übertragung
läuft, und wartet zwischen zwei Versuchen mindestens eine Stunde. Die
Entscheidungen stehen im Journal des Backends. Einzelheiten im
[README](https://github.com/erikwb/blueferry#troubleshooting) (Englisch).

## Gruppennachrichten sehen wie Einzelnachrichten aus

BlueFerry ordnet eine Nachricht nur dann ihrer Gruppe zu, wenn es auch die
zugehörige iPhone-Mitteilung erhält. Schalte auf dem iPhone
**Systemmitteilungen teilen** (englisch: **Share System Notifications**) ein
und prüfe, dass iPhone-Mitteilungen als verbunden angezeigt werden.

## Ein Problem melden

Schlägt eine Kopplung fehl, speichert BlueFerry einen bereinigten Bericht. Er
enthält Paketversion, Kopplungsmodus, Controller-Angaben und einen Ablauf der
Einrichtung. Bluetooth-Adressen und Pfade im Home-Verzeichnis werden
entfernt; schau ihn trotzdem durch, bevor du ihn veröffentlichst.

```bash
blueferry pairing-issue            # öffnet ein vorausgefülltes GitHub-Issue
blueferry pairing-issue --no-open  # gibt nur die URL aus
```

Bitte ergänze iPhone-Modell und iOS-Version im
[Issue](https://github.com/erikwb/blueferry/issues). Issues bitte auf
Englisch.
