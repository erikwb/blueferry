# Plugins

BlueFerry bleibt eine Brücke für Nachrichten. Alles andere läuft als
**Plugin**, ein eigener Prozess auf deinem Sitzungsbus. Nichts davon
läuft im Hintergrunddienst, und nichts ist ab Werk installiert.

## Ein Plugin installieren

Der Store listet ausgewählte Plugins. Eine Installation zeigt Quelle,
Version, was das Plugin darf (seine Fähigkeiten) und den Befehl, den es
ausführt (`Exec` aus seinem Manifest), und fragt vorher nach. Jede
https-Git-URL geht auch:

```bash
blueferry plugins available
blueferry plugins install https://github.com/joshii-h/blueferry-plugin-immich
blueferry plugins list
```

Jedes Plugin bekommt eine eigene Python-Umgebung unter
`~/.local/share/blueferry/plugins`; seine Abhängigkeiten kommen aus dem
Netz. Alles läuft als du, nichts fragt nach root. `blueferry plugins update`,
`remove` und `disable` beenden einen laufenden Plugin-Prozess, den BlueFerry
installiert hat. Deaktivieren sorgt außerdem dafür, dass BlueFerry das
Plugin sofort nicht mehr aufruft.

## Ein Plugin einrichten

`blueferry plugins config ID` zeigt die Einstellungen eines Plugins;
`--set` ändert sie, `--secret` fragt ein Passwort oder einen Schlüssel ohne
Echo ab, `--test` prüft sie und `--login` startet eine Anmeldung im Browser,
wo das Plugin eine anbietet. BlueFerry speichert keine Geheimnisse von
Plugins: sie gehen ins Plugin, das sie selbst aufbewahrt (die
veröffentlichten Plugins nutzen den Schlüsselbund des Desktops).

```bash
blueferry plugins config immich --set url=https://photos.example.org --secret api_key --test
```

## Was Plugins hinzufügen

- **Karteneinträge und Send to** auf der Kommandozeile: `blueferry cards`
  und `blueferry send`.
- **Popups**, die deinen Mitteilungseinstellungen folgen.
- **Photos**: ein Plugin mit Fotos, etwa das Immich-Plugin, listet die
  neuesten.

```bash
blueferry cards
blueferry send photo.jpg --to localsend
blueferry photos recent
```

## Protokolle und Datenschutz

Plugins schreiben `~/.local/state/blueferry/plugins/<id>.log` (nur für dich
lesbar, Geheimnisse maskiert); `blueferry plugins log ID` öffnet es.
BlueFerry zeigt Plugin-Text als reinen Text und lässt ein Plugin nur
Weblinks oder Dateien aus seinem eigenen Cache öffnen. Popups eines Plugins
folgen deinen Mitteilungseinstellungen: keine bei **None**, nur der Name
des Plugins, solange Mitteilungsinhalte verborgen sind. Ihr Text läuft nie
über ein D-Bus-Signal, das andere Programme lesen könnten: BlueFerry holt
ihn beim geprüften Plugin ab.

Ein Plugin schreiben: siehe [PLUGINS.md](https://github.com/erikwb/blueferry/blob/main/PLUGINS.md).
