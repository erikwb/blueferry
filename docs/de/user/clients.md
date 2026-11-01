# Clients

Alle Clients sprechen mit demselben Hintergrunddienst. Du kannst also
zwischen ihnen wechseln oder mehrere gleichzeitig nutzen. Nachrichten,
Kontakte und Einstellungen sind überall dieselben.

Die Clients selbst sind derzeit auf Englisch.

## GTK (GNOME und ähnliche)

`blueferry-gtk` ist eine GTK4/libadwaita-App für GNOME, Cinnamon und ähnliche
Desktops.

![GTK-Client](../../images/gtk.png)

Das Fenster ist eine Telefon-Übersicht. Eine Karte über den Reitern zeigt
Name und Verbindung des iPhones, Akku, Empfang und Netz sowie, was gerade
läuft (mit zurück, Wiedergabe/Pause und weiter). Ihr Knopf **Quick
Settings** enthält die Schalter für [den Ton](phone-audio.md) und die
[Abwesenheitssperre](proximity-lock.md). Ist etwas ausgeschaltet, nennt die
Karte die Einstellung, die es einschaltet. Die Reiter wechselt man
in der Kopfzeile, in einem schmalen Fenster unten:

- **Messages**: Unterhaltungen und der Chat.
- **Calls**: laufende [Anrufe](calls.md) mit annehmen, halten/fortsetzen,
  auflegen, Tastentönen (0-9, `*`, `#`) für den aktiven Anruf sowie dem
  Ton-Schalter, ein Wählfeld, das auch deine
  Kontakte nach Namen durchsucht, und die [Anrufliste](call-history.md) nach Tagen
  gruppiert, mit All und Missed, Rückruf- und Nachrichtenknöpfen und
  **Refresh from iPhone**. Jeder Anruf fragt vorher nach.
- **Notifications**: die [letzten iPhone-Mitteilungen](notification-history.md)
  anderer Apps. Ist die Liste aus, sagt der Reiter warum und öffnet die
  Einstellung.

**Settings** (Hauptmenü, `Strg+,`) hat dieselben Kategorien wie der
KDE-Client, je eine Seite; die Umschaltleiste sitzt in der Kopfzeile, in
einem schmalen Fenster unten, und der Suchknopf findet jede Einstellung:

| Kategorie | Inhalt |
| --- | --- |
| Phone | Kopplung, Verbindungszustand mit **Reconnect**, **Run Diagnostics** (dieselben Prüfungen wie `blueferry doctor`; der Bericht öffnet sich in einem eigenen Dialog mit Kopierknopf), Kopplungsbericht, Kontaktabgleich, Kontaktfotos |
| Notifications | Welche Ereignisse Popups erzeugen, nur Kontakte, Klickregeln, Mitteilungsinhalt, Aktionsknöpfe, Mitteilungen mit dem iPhone abgleichen, beim Wegklicken als gelesen markieren, letzte Mitteilungen behalten |
| Calls & Audio | Anrufe, Anrufliste, Akku-Warnung, [iPhone-Ton am Computer](phone-audio.md), Mediensteuerung |
| Network | Optionales [Bluetooth-Tethering](bluetooth-tethering.md) |
| Security | Einmalcodes, Abwesenheitssperre, lokale Daten und ihre Verschlüsselung, Verlauf löschen |
| About | Version, Hintergrunddienst, About BlueFerry, Tastenkürzel, **Restart Service**, Beenden |

Die Schalter, die einen Neustart brauchen (unten), funktionieren wie im
KDE-Client: Solange eine Änderung auf den Neustart wartet, beginnt jede
Seite mit einem Hinweis und **Restart Service**. Die Plugin-Einstellungen
und den Reiter Photos hat der GTK-Client noch nicht; dafür gibt es den
KDE-Client oder auf der Kommandozeile `blueferry plugins` und
`blueferry photos`.

## KDE Plasma (Kirigami)

`blueferry-qt` ist eine Kirigami-App für KDE Plasma. Unter Ubuntu 24.04,
Linux Mint 22.3 und Pop!_OS 24.04 gibt es sie nicht.

![KDE-Client](../../images/qt.png)

Das Fenster ist eine Telefon-Übersicht. Links zeigt eine Karte Name,
Verbindung, Akku und Empfang des iPhones, was gerade läuft (mit zurück,
Wiedergabe/Pause und weiter) und Schnellschalter für [den
Ton](phone-audio.md) und die [Abwesenheitssperre](proximity-lock.md).
Ausgeschaltete Funktionen bleiben sichtbar, aber ausgegraut, mit der
Einstellung, die sie einschaltet. Rechts gibt es drei Reiter:

- **Messages**: Unterhaltungen mit den Filtern All, Unread und Starred und
  der Chat.
- **Calls**: [Anrufe](calls.md) mit Wählfeld und laufenden Anrufen als
  Karten (annehmen, halten, auflegen, Tastentöne für den aktiven Anruf) und die [Anrufliste](call-history.md)
  nach Tagen gruppiert, mit All und Missed.
- **Notifications**: die [letzten iPhone-Mitteilungen](notification-history.md)
  anderer Apps.

In einem schmalen Fenster klappt die Karte hinter den Telefon-Knopf in der
Kopfzeile.

**Settings** (das Zahnrad in der Kopfzeile) hat links Kategorien, wie KDEs
Systemeinstellungen:

| Kategorie | Inhalt |
| --- | --- |
| Phone | Kopplung, Verbindungszustand, **Run Diagnostics** (dieselben Prüfungen wie `blueferry doctor`, im Fenster angezeigt), Kontaktabgleich, Kontaktfotos |
| Notifications | Welche Ereignisse Popups erzeugen, nur Kontakte, Klickregeln, Mitteilungsinhalt, Aktionsknöpfe, Mitteilungen mit dem iPhone abgleichen, beim Wegklicken als gelesen markieren |
| Calls & Audio | Anrufe, Anrufliste, Akku-Warnung, [iPhone-Ton am Computer](phone-audio.md), Mediensteuerung |
| Network | [Bluetooth-Tethering](bluetooth-tethering.md), iPhone-Internet teilen, automatische Hotspot-Verbindung |
| Security | Einmalcodes, Abwesenheitssperre, lokale Daten und ihre Verschlüsselung, Verlauf löschen |
| About | Version, Hintergrunddienst, Tastenkürzel, Beenden |

In einem schmalen Fenster werden die Kategorien zur Liste; eine Kategorie
öffnet sich einzeln, **All Settings** führt zurück.

### Schalter, die einen Neustart brauchen

Einige Ein/Aus-Einstellungen brauchten bisher eine Änderung in
`~/.config/blueferry/local.env`: Mitteilungsinhalt anzeigen und eine Nachricht
beim Wegklicken des Popups als gelesen markieren. Die Einstellungsseite und `blueferry features` speichern sie
jetzt in `~/.config/blueferry/settings.json`, wo sie `local.env`
überschreiben. Sie gelten nach einem Neustart des Hintergrunddienstes; die
Seite sagt das und bietet **Restart Service** an. Eine Variable aus der
Umgebung des Dienstes gewinnt weiterhin, der Schalter sagt dann das.

```bash
blueferry features                                 # alle Schalter mit Wert und Quelle
blueferry features off mark_read_on_dismiss        # gilt nach einem Neustart
```

### Symbol im Systemabschnitt der Kontrollleiste

`blueferry-tray` kommt mit `blueferry-qt` und legt ein Telefon-Symbol in den
Systemabschnitt der Kontrollleiste (unter KDE Plasma ein
StatusNotifierItem). Das Symbol zeigt die Zahl der ungelesenen Nachrichten,
der Tooltip die Verbindung, den Akku, den Empfang und das Netz. Ein Klick
öffnet BlueFerry oder holt ein offenes Fenster nach vorne. Dabei nimmt es
den Client, den du benutzt: zuerst ein laufendes BlueFerry-Fenster, sonst den
zuletzt benutzten Client, sonst den, der zu deinem Desktop passt.

Starte es im Anwendungsmenü als **BlueFerry Tray**. Beim Anmelden startet es
nur, wenn du das willst: Hake im Menü des Symbols **Start at login** an, oder
führe aus:

```bash
blueferry-tray --autostart on     # off oder status
```

Das legt `~/.config/autostart/io.weirdware.BlueFerry.Tray.desktop` an, also
dieselbe Datei, die auch die Autostart-Einstellungen des Desktops anzeigen.

Das Symbol folgt nur einem laufenden Hintergrunddienst und startet ihn nie
selbst. Ohne eingerichtetes BlueFerry bleibt es ausgeblendet. Solange es
läuft, verzichtet `blueferry-qt` auf sein eigenes Symbol und beendet sich,
wenn du sein Fenster schließt; das Symbol öffnet es wieder. **Quit Tray Icon**
im Menü entfernt nur das Symbol; BlueFerry und offene Fenster laufen weiter.

## Quickshell

`blueferry-quickshell` übernimmt die aktive Farbpalette und die
Monospace-Systemschrift von Omarchy, mit kompakten Bedienelementen und
dünnen Rahmen. Gesendete Nachrichten bleiben in jedem Theme blau. Außerhalb
von Omarchy nutzt der Client die Desktop-Palette. Pakete gibt es für
Arch-basierte Distributionen.

![Quickshell-Client](../../images/quickshell.png)

Unter Omarchy Quattro zeigt das Leisten-Widget
[omarchy-blueferry](https://github.com/erikwb/omarchy-blueferry) den
Verbindungsstatus und ungelesene Unterhaltungen, jeweils mit einem
Antwortfeld:

```bash
omarchy plugin add https://github.com/erikwb/omarchy-blueferry.git
```

Aktiviere es unter **Setup → Plugins** und füge es deiner Leiste hinzu.
Kopplung, Nachrichten und Einstellungen erledigt weiterhin der vollständige
Quickshell-Client.

### Einstellungen

**iPhone settings** hat dieselben Kategorien wie die Einstellungen des Qt-,
GTK- und Terminal-Clients, untereinander angeordnet. Eine Einstellung
erscheint nur, wenn der laufende Hintergrunddienst sie anbietet.

| Kategorie | Was du dort findest |
|---|---|
| Phone | Kopplung, Verbindungszustand mit **Reconnect**, automatisches Zurücksetzen einer hängenden Verbindung, **Run Diagnostics**, Kontaktabgleich (**Sync Contacts**), Kontaktfotos |
| Notifications | Welche Ereignisse Popups erzeugen, nur Kontakte, Mitteilungsinhalt, Aktionsknöpfe, Mitteilungen mit dem iPhone abgleichen, beim Wegklicken als gelesen markieren, letzte Mitteilungen behalten und die Liste dieser Mitteilungen |
| Calls & Audio | Telefonanrufe, Anrufliste und Popups für verpasste Anrufe, Akku-Warnung, iPhone-Ton auf diesem Computer, Mediensteuerung und MPRIS |
| Security | Einmalcodes, Abwesenheitssperre, die lokale Speicherung |
| About | Der Hintergrunddienst, seine Version und **Restart Service** |

Schalter, die einen Neustart brauchen, zeigen oben auf der Seite den
Neustart-Hinweis mit **Restart Service**. Ein älterer Dienst, der sie noch
nicht kennt, zeigt jeden Schalter ausgegraut mit der Variablen, die du
stattdessen in `local.env` setzt.

**Show Recent Notifications** listet die letzten 30 Mitteilungen, die
**Keep recent iPhone notifications** behält: App, Uhrzeit und, falls
erlaubt, Titel und Text. Die Liste wird nur gelesen, solange sie sichtbar
ist, folgt neuen Mitteilungen und wird vergessen, sobald du sie
ausblendest oder die Seite verlässt. Ist sie aus, leer oder nicht
verfügbar, sagt sie, warum.

### Was das Panel weglässt

Quickshell ist ein kompaktes Panel, deshalb bleiben einige Teile bei den
anderen Clients:

| Weggelassen | Warum | Stattdessen |
|---|---|---|
| Plugin-Verwaltung und Plugin-Einstellungen | Jedes Plugin bringt sein eigenes Einstellungsformular mit Geheimnissen, Knöpfen und Protokoll mit, dazu kommt der Store mit installierbaren Plugins; das ist zu viel und zu unterschiedlich für ein kompaktes Panel | Qt- oder GTK-Einstellungen, `blueferry plugins` |
| **Send to…** und der Tab **Photos** (Plugin-Funktionen) | Send to braucht eine Dateiauswahl oder Drag & Drop, die das Panel nicht hat; beides kommt von Plugins, die das Panel nicht verwaltet | Qt-Client (Photos), Qt- oder GTK-Client (Send to), `blueferry send`, `blueferry photos` |
| Begleitwerkzeuge | Desktop-Helfer richtet man einmal ein; das ist keine Aufgabe für ein Panel | Qt- oder GTK-Einstellungen, `blueferry tools` |
| Nachrichtenverlauf löschen | Bietet auch der Terminal-Client nicht an | `blueferry history-clear` |
| Click Rules, letzte Anrufe, das Anruf-Panel | Noch nicht im Quickshell-Client | Qt-, GTK- oder Terminal-Client |

Das Panel lädt keinen Übersetzungskatalog, seine eigenen Beschriftungen
sind deshalb englisch. Texte aus dem gemeinsamen BlueFerry-Code, etwa der
Hinweis zum Wiederverbinden, der Neustart-Hinweis oder der Platzhalter der
Mitteilungsliste, folgen deiner Sprache.

## Terminal (TUI)

Der Terminal-Client ist auf jeder Distribution Teil von `blueferry-backend`.

```bash
blueferry-tui
# oder
blueferry tui
```

`?` zeigt die Tastenbelegung, `Strg+P` die Befehlspalette. Der Client hat eine
Suche in Unterhaltungen, ein mehrzeiliges Eingabefeld, Mausunterstützung,
Themes und ein Layout, das sich schmalen Terminals anpasst.

![Terminal-Client](../../images/terminal.png)

### Einstellungen

`o` öffnet **Settings** mit denselben Kategorien wie die Einstellungen von Qt
und GTK, als Reiter. Eine Einstellung erscheint nur, wenn der laufende
Hintergrunddienst sie anbietet.

| Kategorie | Inhalt |
|---|---|
| Phone | Zustand des Hintergrunddienstes, **Reconnect** (auch `b`), automatisches Zurücksetzen einer hängenden Verbindung (auch `B`), **Run Diagnostics**, Kontaktabgleich, Kontaktfotos |
| Notifications | Welche Ereignisse Popups erzeugen, nur Kontakte, **Click Rules** (auch `l`), Mitteilungsinhalt, Aktionsknöpfe, Mitteilungen mit dem iPhone abgleichen, beim Wegklicken als gelesen markieren, letzte iPhone-Mitteilungen behalten |
| Calls & Audio | Telefonate, Anrufliste und Popups für verpasste Anrufe, Akkuwarnung, iPhone-Ton auf diesem Computer (auch `a`), Mediensteuerung und MPRIS |
| Security | Einmalcodes, Sperre bei Abwesenheit, die Speichereinstellung (nur angezeigt; ändern mit `blueferry storage-policy-set`) |
| About | Version, der Hintergrunddienst mit **Restart Service**, die Tastenbelegung |

Die Schalter, die einen Neustart brauchen (siehe oben), zeigen den Hinweis
oben in Settings mit **Restart Service**. Ein Dienst, der sie noch nicht kennt,
zeigt jeden Schalter ausgegraut mit der `local.env`-Variable, die stattdessen
zu setzen ist.

**Run Diagnostics** führt `blueferry doctor` aus und zeigt den Bericht.
**Copy Report** legt ihn über das Terminal (OSC 52) in die Zwischenablage;
reicht das Terminal das nicht weiter, markiere den Text mit der Maus.

Die Befehlspalette (`Strg+P`) kennt außerdem **Settings: Phone** … **Settings:
About** und **Run Diagnostics**. Die Kopplung bleibt bei `blueferry
pair-setup` oder einem grafischen Client; Plugin-Einstellungen hat der
Terminal-Client noch nicht.

### iPhone, Anrufe und Mitteilungen

**iPhone Overview** in der Befehlspalette ist die Telefon-Karte des
Terminals: der Name des iPhones, die Verbindung, Akku, Empfang und Netz, was
gerade läuft, und für jede ausgeschaltete optionale Funktion der Schalter,
der sie einschaltet, samt seiner Kategorie in Settings. Die Knöpfe öffnen die
Mitteilungsliste, die letzten Anrufe, das Anruf-Panel und Settings.

**iPhone Notifications** (in der Palette und in der Übersicht) listet die
letzten Mitteilungen von iPhone-Apps, die **Keep recent iPhone
notifications** behält: App, Zeit und, wenn erlaubt, Titel und Text. Die
Liste wird nur gelesen, solange sie offen ist, folgt neuen Mitteilungen und
wird beim Schließen vergessen. Ist sie ausgeschaltet, leer oder nicht
verfügbar, sagt sie warum; **Open Notification Settings** führt zu
Settings › Notifications.

Das Anruf-Panel (`c` oder **Phone Calls** in der Palette) beschreibt jeden
Anruf wie Qt und GTK (eingehend, gehalten, die Dauer). Seine eine Knopfreihe
wirkt auf den naheliegenden Anruf: **Answer** nimmt den klingelnden an,
**Hold** bzw. **Resume** hält den verbundenen oder holt ihn zurück, **Hang
Up** (oder **Decline**) beendet zuerst den aktiven; ein Anruf, der wartet,
während ein anderer verbunden ist, bekommt ein eigenes **Decline**. Während eines aktiven
Anrufs gehen die Tasten 0–9, `*` und `#` sofort als Tastentöne hinaus, etwa
an ein Telefonmenü, ausser ein Textfeld hat den Fokus; das Panel zeigt sie
als „Sent: …“. Das Tastentonfeld sendet eine ganze Folge wie eine PIN. Wer im Nummernfeld einen Namen tippt, bekommt die
Telefonnummern passender Kontakte vorgeschlagen. Jeder Anruf wird zuerst
bestätigt.

**Recent calls** (`h`) sind nach Tagen gruppiert; `m` zeigt nur verpasste
Anrufe, `s` (oder **Refresh from iPhone**) liest die Liste des iPhones neu
ein. Enter oder **Call Back** ruft die gewählte Nummer nach der
Bestätigung an, **Send a Message** beginnt eine neue Nachricht an sie.

## Kommandozeile

Der Befehl `blueferry` hilft bei der Diagnose und in Skripten:

```bash
blueferry doctor                     # Voraussetzungen prüfen
blueferry sms-list                   # letzte Nachrichten (--source local für den Cache)
blueferry sms-send '+41791234567' 'bin unterwegs'
blueferry sms-send person@icloud.com 'Hallo von Linux'
blueferry sms-send Alice 'komme später'
blueferry contacts-sync              # Kontakte neu vom iPhone holen
blueferry storage-policy-set encrypted   # oder plaintext, none
blueferry storage-unlock             # Schlüsselbund zum Entsperren auffordern
blueferry history-clear              # lokalen Nachrichtenverlauf löschen
blueferry pairing-issue              # GitHub-Issue mit dem letzten Kopplungsbericht öffnen
blueferry version
```

Ist ein Kontaktname mehrdeutig, listet BlueFerry die Treffer zur Auswahl auf,
statt zu raten. `blueferry <befehl> --help` zeigt alle Optionen.

## Welcher Client sich bei einer Mitteilung öffnet

Ein Klick auf eine Nachrichtenmitteilung öffnet die Unterhaltung in einem
laufenden grafischen Client, bevorzugt in dem zuletzt benutzten. Läuft keiner,
öffnet BlueFerry den zuletzt benutzten Client. Ohne frühere Wahl bevorzugt es
GTK unter GNOME, Qt unter KDE und Quickshell unter Omarchy/Hyprland und
weicht auf einen installierten Client aus.
