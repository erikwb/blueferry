# Plugins und Begleitprogramme

BlueFerry bleibt eine Brücke für Nachrichten. Alles andere läuft als
**Plugin**, ein eigener Prozess auf deinem Sitzungsbus, oder ist ein
**Begleitprogramm**, das BlueFerry nur für dich startet. Nichts davon
läuft im Hintergrunddienst, und nichts ist ab Werk installiert.

## Begleitprogramme

Der Abschnitt **Tools** der Telefonkarte (im GTK-Client unter
**Plugins & Tools**) und `blueferry tools`
starten drei Programme, die BlueFerry nicht mitbringt. Jeder Eintrag bleibt
ausgegraut, mit dem Hinweis, was zu installieren ist, bis das Programm da
ist.

| Eintrag | Braucht | Was er tut |
| --- | --- | --- |
| Mirror iPhone screen | [UxPlay](https://github.com/FDH2/UxPlay) | Startet einen AirPlay-Empfänger; am iPhone Kontrollzentrum › Bildschirmsynchronisierung öffnen und diesen Computer wählen. Derselbe Eintrag beendet ihn. Ein `uxplay.desktop`-Eintrag mit eigenen Optionen wird bevorzugt. |
| Send a file (LocalSend) | LocalSend (Flathub oder `localsend`) | Öffnet LocalSend. Ausgeblendet, solange das LocalSend-Plugin aktiv ist. |
| iPhone photos (USB) | `ifuse`, libimobiledevice, ein USB-Kabel | Bindet die Aufnahmen in einem privaten Ordner ein und öffnet ihn; **Eject** hängt sie wieder aus. Fragt das iPhone, „Diesem Computer vertrauen“ bestätigen. |

Ersetzt ein Plugin UxPlay, kannst du einen von BlueFerry bereits gestarteten
Empfänger weiterhin beenden. **Stop mirroring** bleibt sichtbar, bis er
beendet ist; solange das Plugin aktiviert ist, sind neue Starts gesperrt.

```bash
blueferry tools            # welche Programme bereit sind
blueferry tools mirror
blueferry tools photos
blueferry tools eject
```

## Ein Plugin installieren

Im GTK- oder KDE-Client **Settings › Plugins** öffnen. Der Store listet ausgewählte
Plugins; **Install** zeigt Quelle, Version, welche Funktionen das Plugin anbietet
(seine Fähigkeiten), und den Befehl, den es ausführt (`Exec` aus seinem Manifest),
und fragt vorher nach. Man kann auch jede https-Git-URL einfügen. Im
Terminal:

```bash
blueferry plugins available
blueferry plugins install https://github.com/joshii-h/blueferry-plugin-immich
blueferry plugins list
```

Jedes Plugin bekommt eine eigene Python-Umgebung unter
`~/.local/share/blueferry/plugins`; seine Abhängigkeiten kommen aus dem
Netz. Plugins, ihre Abhängigkeiten und ihr Installationscode laufen mit
deinen Benutzerrechten. Sie sind nicht in einer Sandbox: Fähigkeiten
beschreiben Funktionen und beschränken den Zugriff auf deine Dateien, das
Netzwerk oder Sitzungsdienste nicht. Installiere Plugins und Updates nur
aus Quellen, denen du vertraust. Nichts fragt nach root.
**Update**, **Remove** und der Schalter neben einem Plugin (oder
`blueferry plugins update`, `remove`, `disable`) beenden einen laufenden
Plugin-Prozess, den BlueFerry
installiert hat. Deaktivieren sorgt außerdem dafür, dass BlueFerry das
Plugin sofort nicht mehr aufruft.

## Ein Plugin einrichten

**Settings** neben einem Plugin öffnet ein Formular aus dem, was das Plugin
braucht: Felder mit Beispielen und „More information“-Links (manche Plugins
benennen sie selbst, etwa „Which port?“), **Test connection** und bei
manchen Diensten eine Anmeldung im Browser. Manche Plugins setzen eigene
Knöpfe über die Felder, etwa **Set up iPhone**; sie arbeiten mit den
gespeicherten Einstellungen, also Änderungen vorher speichern. Speichern
geht erst, wenn die Pflichtfelder gültig sind. Passwörter und Schlüssel
werden nie wieder angezeigt, nur neu eingegeben. BlueFerry speichert keine
Geheimnisse von Plugins: sie gehen ins Plugin, das sie selbst aufbewahrt
(die veröffentlichten Plugins nutzen den Schlüsselbund des Desktops).

```bash
blueferry plugins config immich --set url=https://photos.example.org --secret api_key --test
blueferry plugins config ID                  # Einstellungen, eigene Knöpfe
blueferry plugins config ID --action ACTION  # einen davon drücken
```

## Was Plugins hinzufügen

- **From Plugins** auf der Telefonkarte: zum Beispiel der nächste
  Kalendereintrag oder eine laufende Übertragung, mit Knöpfen. Ein Plugin,
  das versagt, erscheint als ausgegraute Zeile.
- **Send to…** am Ende dieses Abschnitts: gibt Dateien an ein Plugin-Ziel
  wie „iPhone (LocalSend)“; manche Einträge der Karte nehmen Dateien direkt
  an (darauf ziehen).
- **Karteneinträge und Send to** auf der Kommandozeile: `blueferry cards`
  und `blueferry send`.
- **Popups**, die deinen Mitteilungseinstellungen folgen.
- **Photos**: ein Plugin mit Fotos, etwa das Immich-Plugin, listet die
  neuesten und füllt den Reiter **Photos**. Immich liefert Vorschaubilder
  als WebP. Der GTK-Client dekodiert sie mit GdkPixbuf, das dafür einen
  WebP-Loader braucht; fehlt er, zeigt der Reiter statt der Vorschaubilder
  ein Banner, das ihn nennt. Der KDE-Client braucht stattdessen Qts
  Bildformat-Plugins. Die nativen Pakete ziehen sie mit; auf anderen
  Systemen installierst du sie selbst:

  | System | GTK-Client | KDE-Client |
  | --- | --- | --- |
  | Arch Linux | `webp-pixbuf-loader` | `qt6-imageformats` |
  | Debian, Ubuntu | `webp-pixbuf-loader` | `qt6-image-formats-plugins` |
  | Fedora | eingebaut (gdk-pixbuf2 nutzt glycin) | `qt6-qtimageformats` |
  | Gentoo | `gui-libs/gdk-pixbuf-loader-webp` | `dev-qt/qtimageformats:6` |

  Nach der Installation den Client neu starten.

```bash
blueferry cards
blueferry send photo.jpg --to localsend
blueferry photos recent
```

## Protokolle und Datenschutz

Plugins schreiben `~/.local/state/blueferry/plugins/<id>.log` (nur für dich
lesbar, Geheimnisse maskiert); **Show Log** oder `blueferry plugins log ID` öffnet es.
BlueFerry zeigt Plugin-Text als reinen Text und lässt ein Plugin nur
Weblinks oder Dateien aus seinem eigenen Cache öffnen. Popups eines Plugins
folgen deinen Mitteilungseinstellungen: keine bei **None**, nur der Name
des Plugins, solange Mitteilungsinhalte verborgen sind. Ihr Text läuft nie
über ein D-Bus-Signal, das andere Programme lesen könnten: BlueFerry holt
ihn beim geprüften Plugin ab.

Ein Plugin schreiben: siehe [PLUGINS.md](https://github.com/erikwb/blueferry/blob/main/PLUGINS.md).
