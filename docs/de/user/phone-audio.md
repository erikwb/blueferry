# iPhone-Ton am Computer

BlueFerry kann die Medienwiedergabe des iPhones (Musik, Podcasts, Videos) auf
die Lautsprecher dieses Computers holen und wieder zurück aufs iPhone
schicken, per Schalter oder Kommandozeile. PipeWire wird dabei nicht neu
gestartet; BlueFerry bittet BlueZ, den Bluetooth-Audiostrom (A2DP) des
iPhones zu öffnen oder zu schließen.

Der Schalter zeigt die aktuelle Bluetooth-Audioweiterleitung. Bis du in
BlueFerry eine Ausgabe wählst, gelten die Desktop- und iPhone-Einstellungen.
Die Audiorollen des Adapters bleiben für die Kopplung verfügbar.

## Voraussetzungen

- Der Audio-Server des Desktops muss die Bluetooth-Audioempfängerrolle
  (`a2dp_sink`) anbieten.
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
blueferry audio auto       # Auswahl vergessen; Desktop-Routing verwenden
```

Die Auswahl PC oder iPhone wird für das ausgewählte iPhone gespeichert und
nach dem erneuten Verbinden wiederhergestellt. Ein anderes Telefon übernimmt
diese Auswahl nicht. Der Schalter folgt weiterhin der tatsächlichen Route;
`blueferry audio` zeigt zusätzlich die gespeicherte Auswahl.

Ab WirePlumber 0.5 installiert die erste Auswahl eine Regel, die automatische
Profilverbindungen nur für dieses Telefon deaktiviert. Das Laden oder Entfernen
der Regel startet WirePlumber neu und kann anderes Bluetooth-Audio kurz
unterbrechen. Weitere Wechsel zwischen PC und iPhone benötigen keinen Neustart.
Andere Audioregeln und die angebotenen Audiorollen bleiben erhalten.

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
- **Erneut verbinden:** BlueFerry wartet auf das Ende der Kopplung und auf
  betriebsbereite Nachrichtenverbindungen sowie, falls aktiviert, ANCS. Pro
  Verbindung gibt es höchstens einen automatischen Wechsel. Wie beim manuellen
  Wechsel bleibt zuvor pausierte Wiedergabe pausiert. Ohne unterstütztes
  WirePlumber lässt sich die
  Auswahl wiederherstellen, aber die Verbindungsversuche des Audioservers lassen
  sich nicht unterdrücken.
- **Anrufe:** Diese Einstellung betrifft Medien (A2DP). Die getrennte
  Anrufoption und die HFP-Rollen des Desktops bleiben unverändert.

## Datenschutz

Schalter und Status enthalten keine Titel- oder App-Daten: nur die Route
(`pc`, `phone`, `unavailable`), einen Grund-Code und wo der Ton spielt
(`pc`, `phone`, `none`, `unknown`).
Auswahl und Telefonadresse werden lokal in `settings.json` gespeichert.
`audio auto` entfernt die WirePlumber-Regel `zz-blueferry-phone-audio.conf`.

## Fehlerbehebung

- *Bluetooth is still changing the iPhone's audio connection.* bluetoothd ist
  noch mit einer früheren Änderung beschäftigt; nach ein paar Sekunden erneut
  versuchen.
- Der Schalter sagt „dieser Computer“, aber nichts ist zu hören: das
  AirPlay-Menü am iPhone und die Lautstärke der neuen Bluetooth-Ausgabe in den
  Toneinstellungen des Desktops prüfen.
