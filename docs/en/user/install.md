# Install

BlueFerry ships as native packages for each distribution. Install
`blueferry-backend` plus the client for your desktop. The backend package also
contains the `blueferry-tui` terminal client.

| Package | Desktop |
| --- | --- |
| `blueferry-gtk` | GNOME, Cinnamon, and similar desktops |
| `blueferry-qt` | KDE Plasma |
| `blueferry-quickshell` | Quickshell and Omarchy (Arch packages only) |

Download the packages for your distribution from the
[latest release](https://github.com/erikwb/blueferry/releases/latest), then
run the matching command in the folder that contains them.

## Arch Linux and CachyOS

```bash
sudo pacman -U ./blueferry-backend-*.pkg.tar.zst ./blueferry-gtk-*.pkg.tar.zst
```

## Debian, Ubuntu, Linux Mint, Pop!_OS, and PikaOS

```bash
sudo apt install ./blueferry-backend_*.deb ./blueferry-gtk_*.deb
```

Ubuntu 24.04, Linux Mint 22.3, and Pop!_OS 24.04 don't provide the Qt
dependencies; use the GTK or terminal client there.

## Fedora

Fedora 43 and 44 share the `.fc43` packages; Fedora 45 uses `.fc45`.

```bash
# Fedora 43 and 44; use fc45 for Fedora 45.
sudo dnf install ./blueferry-backend-*.fc43.noarch.rpm \
  ./blueferry-gtk-*.fc43.noarch.rpm
```

Replace `blueferry-gtk` with `blueferry-qt` for KDE Plasma.

## Bluetooth changes made by the packages

- **Arch and Fedora** packages require BlueZ 5.86 or newer and add a
  `bluetooth.service` drop-in that runs `bluetoothd -E`. That enables the
  BlueZ interface needed for iPhone system notifications. Bluetooth is
  restarted only if it is already running.
- **Debian-family** packages don't change or restart Bluetooth. Messages and
  contacts work; system notifications are added only when that machine's
  BlueZ already supports them.

## Tested distributions

| Recipe | Tested on | Clients |
| --- | --- | --- |
| Arch | Arch Linux, CachyOS | TUI, GTK, Qt, Quickshell |
| DEB | Debian 13, Ubuntu 26.04, PikaOS IV | TUI, GTK, Qt |
| DEB | Ubuntu 24.04, Linux Mint 22.3, Pop!_OS 24.04 | TUI, GTK |
| RPM | Fedora 43, 44, 45 | TUI, GTK, Qt |

Details are in
[packaging/README.md](https://github.com/erikwb/blueferry/blob/main/packaging/README.md).

## Build packages from source

```bash
git clone https://github.com/erikwb/blueferry.git
cd blueferry
```

**Arch-based distributions:** build and install all packages. Without `-i`,
the packages are only built, into `packaging/arch/`.

```bash
sudo pacman -S --needed base-devel python
./build.sh -si
```

The build always uses `/usr/bin/python`, ignoring any virtualenv, pyenv,
conda, or mise interpreter earlier in `PATH`.

**Debian-based distributions:** packages are written to `dist/deb/`.

```bash
sudo apt-get install devscripts equivs
sudo mk-build-deps -i -r -t 'apt-get -y --no-install-recommends' packaging/deb/control
./packaging/build-deb.sh
```

**Fedora:** packages are written to `dist/rpm/`.

```bash
sudo dnf install dnf-plugins-core rpm-build
sudo dnf builddep packaging/rpm/blueferry.spec
./packaging/build-rpm.sh
```

## Upgrades and removal

Package upgrades are detected automatically: an outdated backend restarts
itself, and clients ask you to update if they no longer match the backend.

Uninstalling doesn't delete your configuration in `~/.config/blueferry` or
your local data in `~/.local/state/blueferry`. Remove those folders yourself
if you want a clean slate.

## Next step

[Pair your iPhone](pairing.md).
