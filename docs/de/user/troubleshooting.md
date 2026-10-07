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

- **Dauerhaft:** `ControllerMode = dual` setzen (oder die Zeile entfernen)
  und Bluetooth neu starten.
- **Bis zum nächsten Bluetooth-Neustart:** `sudo btmgmt --index 0 le on`
  (`0` durch den Index deines Adapters ersetzen, zum Beispiel `1` für
  `hci1`).

Danach neu koppeln.

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
