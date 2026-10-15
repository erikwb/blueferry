# iPhone-Ton am Computer

BlueFerry kann die Medienwiedergabe des iPhones (Musik, Podcasts, Videos) auf
die Lautsprecher dieses Computers holen und wieder zurück aufs iPhone
schicken, per Schalter oder Kommandozeile. PipeWire wird dabei nicht neu
gestartet; BlueFerry bittet BlueZ, den Bluetooth-Audiostrom (A2DP) des
iPhones zu öffnen oder zu schließen.

Der Schalter ist **standardmäßig aus**: Ab Werk lässt BlueFerry Anrufe und
Musik auf dem iPhone.

## Voraussetzungen

- `BLUEFERRY_KEEP_PHONE_AUDIO_ON_PHONE=false` in
  `~/.config/blueferry/local.env`, danach das Backend neu starten. Damit
  behält dieser Computer die Rolle als Bluetooth-Audioempfänger; mit dem
  Standardwert `true` entfernt BlueFerrys WirePlumber-Fragment diese Rolle,
  und der Schalter sagt, warum er nicht verfügbar ist.
- Das iPhone ist über Bluetooth Classic verbunden (die Verbindung für
  Nachrichten).
- Optional: [Mediensteuerung](media-control.md). Damit kann BlueFerry die
  Wiedergabe nach der Rückgabe fortsetzen und anzeigen, wo der Ton wirklich
  spielt.

## Benutzen

Im Qt-, GTK- oder Quickshell-Client die iPhone-Einstellungen öffnen und
**iPhone Sound** verwenden: an spielt den iPhone-Ton hier, aus schickt ihn
zurück. Im Terminal-Client `a` drücken. Im Terminal:

```bash
blueferry audio            # wohin der Ton geht und wo er spielt
blueferry audio pc         # iPhone-Ton auf diesem Computer
blueferry audio phone      # zurück aufs iPhone
```

Der Schalter ist eine Aktion, keine gespeicherte Einstellung. Er folgt dem,
was BlueZ meldet, und bewegt sich deshalb auch, wenn du die Ausgabe am iPhone
änderst.

## Was passiert

- **Ton hierher holen** öffnet den Strom. Das iPhone wählt diesen Computer
  meist sofort als Ausgabe; was spielt, entscheidet weiterhin das iPhone.
- **Zurückschicken** schließt den Strom. iOS pausiert die Wiedergabe, wenn
  seine Bluetooth-Ausgabe verschwindet, wie bei abgezogenen Kopfhörern. Lief
  etwas und ist die Mediensteuerung an, sendet BlueFerry nach dem Umschalten
  einmal Wiedergabe.
- **AirPlay-Menü am iPhone:** Wählst du dort das iPhone, lässt iOS den Strom
  zu diesem Computer offen, spielt aber über den eigenen Lautsprecher. Mit
  Mediensteuerung sagen die Clients dann *The iPhone is playing on its own
  speaker (chosen on the iPhone)*, und `blueferry audio` gibt dasselbe aus.

## Datenschutz

Schalter und Status enthalten keine Titel- oder App-Daten: nur die Route
(`pc`, `phone`, `unavailable`), einen Grund-Code und wo der Ton spielt
(`pc`, `phone`, `none`, `unknown`).

## Fehlerbehebung

- *Phone audio switching is unavailable. BlueFerry keeps phone audio on the
  iPhone.* `BLUEFERRY_KEEP_PHONE_AUDIO_ON_PHONE=false` setzen und das Backend
  neu starten.
- *Bluetooth is still changing the iPhone's audio connection.* bluetoothd ist
  noch mit einer früheren Änderung beschäftigt; nach ein paar Sekunden erneut
  versuchen.
- Der Schalter sagt „dieser Computer“, aber nichts ist zu hören: das
  AirPlay-Menü am iPhone und die Lautstärke der neuen Bluetooth-Ausgabe in den
  Toneinstellungen des Desktops prüfen.
