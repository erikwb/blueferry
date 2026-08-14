# Arch packages

BlueFerry builds as four split pacman packages: the backend with its terminal
client, plus clients for GNOME, KDE, and Quickshell. All build and runtime dependencies
come from the official Arch repositories. The same packages are intended for
Arch Linux and CachyOS and are built in both environments in package CI.
The backend requires BlueZ 5.86 or newer because that is the first upstream
release with working per-bearer connection methods.

The `blueferry-bluetooth.conf` systemd drop-in is Arch-specific. In particular,
its `/usr/lib/bluetooth/bluetoothd` executable path must not be copied to
Debian or Ubuntu, where the usual path is `/usr/libexec/bluetooth/bluetoothd`.
Using the wrong path prevents the entire Bluetooth service from starting.
The backend also installs a pacman hook. Arch's standard systemd hook reloads
the manager first; BlueFerry's later hook then restarts `bluetooth.service` if
it is running. This applies the drop-in on install and upgrade and restores the
vendor unit on removal without starting a stopped service.

From the repository root, build and install everything with:

```bash
./build.sh -si
```

To build without installing, leave off `-i`. To inspect or modify the source
snapshot before invoking makepkg:

```bash
./build.sh --prepare-only
cd packaging/arch
makepkg --cleanbuild --force -si
```

`build.sh` snapshots the current working tree, including uncommitted files,
and writes the checksum used by the PKGBUILD. Building alone does not pair a
phone or change the live Bluetooth configuration. Installing the backend
restarts Bluetooth if it is already running, briefly disconnecting active
devices. Package checks run the linters and device-isolated test suite before
building. Finished package archives are left in `packaging/arch/`.

The resulting packages are:

- `blueferry-backend` for the daemon, CLI, terminal client, D-Bus service,
  BlueZ setup, and pairing support;
- `blueferry-gtk` for the GTK/libadwaita client;
- `blueferry-qt` for the Qt/Kirigami client;
- `blueferry-quickshell` for the standalone Quickshell client.

Install only the clients you use. Each graphical client can perform first-run
pairing, and opening one starts the backend through D-Bus. Once a phone has
been configured, the backend also starts at login. Package upgrades and normal
Bluetooth reconnects do not require manual systemd commands.

The optional Omarchy bar widget is distributed separately from
[blueferry-quattro](https://github.com/erikwb/blueferry-quattro).

## Removing BlueFerry

For example, to remove all four packages:

```bash
sudo pacman -Rns blueferry-backend blueferry-gtk blueferry-qt blueferry-quickshell
```

Pacman removes the daemon, clients, units, BlueZ drop-in, and application
metadata. It deliberately leaves per-user configuration and local history
under `~/.config/blueferry` and `~/.local/state/blueferry`. Delete those
directories—and the “BlueFerry local storage key” in your desktop wallet—only
if you want to erase the pairing configuration, contacts, and history too.
