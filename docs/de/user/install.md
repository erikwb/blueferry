# Installation

BlueFerry gibt es als native Pakete für jede Distribution. Installiere
`blueferry-backend` und den Client für deinen Desktop. Das Backend-Paket
enthält auch den Terminal-Client `blueferry-tui`.

| Paket | Desktop |
| --- | --- |
| `blueferry-gtk` | GNOME, Cinnamon und ähnliche Desktops |
| `blueferry-qt` | KDE Plasma |
| `blueferry-quickshell` | Quickshell und Omarchy (nur Arch-Pakete) |

Lade die Pakete für deine Distribution aus dem
[neuesten Release](https://github.com/erikwb/blueferry/releases/latest) herunter
und führe den passenden Befehl in dem Ordner aus, in dem sie liegen.

## Arch Linux und CachyOS

```bash
sudo pacman -U ./blueferry-backend-*.pkg.tar.zst ./blueferry-gtk-*.pkg.tar.zst
```

## Debian, Ubuntu, Linux Mint, Pop!_OS und PikaOS

```bash
sudo apt install ./blueferry-backend_*.deb ./blueferry-gtk_*.deb
```

Ubuntu 24.04, Linux Mint 22.3 und Pop!_OS 24.04 bringen die nötigen
Qt-Abhängigkeiten nicht mit; nimm dort den GTK- oder Terminal-Client.

## Fedora

Fedora 43 und 44 nutzen die `.fc43`-Pakete, Fedora 45 die `.fc45`-Pakete.

```bash
# Fedora 43 und 44; für Fedora 45 fc45 verwenden.
sudo dnf install ./blueferry-backend-*.fc43.noarch.rpm \
  ./blueferry-gtk-*.fc43.noarch.rpm
```

Ersetze `blueferry-gtk` für KDE Plasma durch `blueferry-qt`.

## Gentoo, Alpine und andere Systeme ohne systemd

Dafür gibt es noch keine Pakete. BlueFerry läuft dort aus einer
Quellcode-Installation; siehe [Betrieb ohne systemd](openrc.md).

## Was die Pakete an Bluetooth ändern

- Pakete für **Arch und Fedora** verlangen BlueZ 5.86 oder neuer und
  installieren ein Drop-in für `bluetooth.service`, das `bluetoothd -E`
  startet. Damit steht die BlueZ-Schnittstelle für iPhone-Mitteilungen
  bereit. Bluetooth wird nur neu gestartet, wenn es bereits läuft.
- Pakete für die **Debian-Familie** ändern Bluetooth nicht und starten es
  nicht neu. Nachrichten und Kontakte funktionieren; Mitteilungen kommen nur
  hinzu, wenn das BlueZ des Rechners sie bereits unterstützt.

## Getestete Distributionen

| Rezept | Getestet auf | Clients |
| --- | --- | --- |
| Arch | Arch Linux, CachyOS | TUI, GTK, Qt, Quickshell |
| DEB | Debian 13, Ubuntu 26.04, PikaOS IV | TUI, GTK, Qt |
| DEB | Ubuntu 24.04, Linux Mint 22.3, Pop!_OS 24.04 | TUI, GTK |
| RPM | Fedora 43, 44, 45 | TUI, GTK, Qt |

Einzelheiten stehen in
[packaging/README.md](https://github.com/erikwb/blueferry/blob/main/packaging/README.md)
(Englisch).

## Pakete aus dem Quellcode bauen

```bash
git clone https://github.com/erikwb/blueferry.git
cd blueferry
```

**Arch-basierte Distributionen:** alle Pakete bauen und installieren. Ohne
`-i` werden sie nur gebaut, nach `packaging/arch/`.

```bash
sudo pacman -S --needed base-devel python
./build.sh -si
```

Der Build nutzt immer `/usr/bin/python` und ignoriert Interpreter aus
virtualenv, pyenv, conda oder mise, die weiter vorne im `PATH` stehen.

**Debian-basierte Distributionen:** die Pakete landen in `dist/deb/`.

```bash
sudo apt-get install devscripts equivs
sudo mk-build-deps -i -r -t 'apt-get -y --no-install-recommends' packaging/deb/control
./packaging/build-deb.sh
```

**Fedora:** die Pakete landen in `dist/rpm/`.

```bash
sudo dnf install dnf-plugins-core rpm-build
sudo dnf builddep packaging/rpm/blueferry.spec
./packaging/build-rpm.sh
```

## Aktualisieren und entfernen

Paket-Updates werden automatisch erkannt: Ein veraltetes Backend startet sich
neu, und Clients bitten dich um ein Update, wenn sie nicht mehr zum Backend
passen.

Beim Deinstallieren bleiben deine Konfiguration in `~/.config/blueferry` und
deine lokalen Daten in `~/.local/state/blueferry` erhalten. Lösche diese
Ordner selbst, wenn du sauber neu anfangen willst.

## Nächster Schritt

[Dein iPhone koppeln](pairing.md).
