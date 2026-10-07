# Entwicklung

BlueFerry ist in Python geschrieben. Ein unprivilegiertes Backend pro
Benutzer besitzt jede Bluetooth-Verbindung zum iPhone und alle gespeicherten
Daten. Die Clients für GTK, Qt/Kirigami, Quickshell und das Terminal sowie die
CLI sind schlanke Clients einer privaten D-Bus-API auf dem Session-Bus.

```text
GTK client ───────┐
Qt client ────────┤
TUI client ───────┼── session D-Bus ── backend daemon ── BlueZ system D-Bus
Quickshell client ┘                         │
                                           ├── BlueZ OBEX session D-Bus
                                           └── private state and notifications
```

## Weiterlesen

Die Entwicklerdokumentation ist auf Englisch. Auf Deutsch gibt es diesen
Überblick und eine Zusammenfassung der [Architektur](architecture.md).

| Seite | Maßgebliche Quelle |
| --- | --- |
| [Architektur](architecture.md) | [ARCHITECTURE.md](https://github.com/erikwb/blueferry/blob/main/ARCHITECTURE.md): Modulübersicht, Prozessgrenzen, Datenschutzregeln |
| [D-Bus API](../../en/developer/dbus-api.md) (Englisch) | [`data/io.weirdware.BlueFerry.xml`](https://github.com/erikwb/blueferry/blob/main/data/io.weirdware.BlueFerry.xml) und `src/blueferry/protocol.py` |
| [Testing](../../en/developer/testing.md) (Englisch) | [TESTING.md](https://github.com/erikwb/blueferry/blob/main/TESTING.md) |
| [Contributing](../../en/developer/contributing.md) (Englisch) | Qualitätsprüfungen und Konventionen |
| Bluetooth-Verhalten | [PROTOCOL.md](https://github.com/erikwb/blueferry/blob/main/PROTOCOL.md): beobachtetes Verhalten von iPhone und BlueZ |

Die Dokumente im Wurzelverzeichnis des Repositorys sind maßgeblich. Diese
Seiten bieten einen Einstieg und verlinken darauf, statt sie zu kopieren.
Issues, Pull Requests und Commit-Nachrichten bitte auf Englisch.

## Aufbau des Repositorys

| Pfad | Inhalt |
| --- | --- |
| `src/blueferry/` | Backend, gemeinsame Client-Schicht, CLI, TUI, GTK (`ui/`), Qt (`qt/`) |
| `data/` | D-Bus-Schnittstellen-XML, Desktop-Dateien, AppStream-Metadaten, Quickshell-Client |
| `systemd/` | User-Service und die Hilfs-Unit für die Class of Device mit ihrer Polkit-Regel |
| `packaging/` | Rezepte für Arch, DEB und RPM |
| `tests/` | Hermetische Testsuite |
| `po/` | Übersetzungsinventar |
| `website/public/` | Statische Projekt-Website |
| `docs/` | Diese Seiten |
