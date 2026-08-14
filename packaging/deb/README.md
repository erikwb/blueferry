# Debian-family packages

This recipe builds `blueferry-backend`, `blueferry-gtk`, and `blueferry-qt`.
It targets Debian 13, Ubuntu 24.04 and 26.04, PikaOS IV, Linux Mint 22.3, and
Pop!_OS 24.04. CI builds on Debian 13, then installs those exact DEBs in clean
target environments. Ubuntu 24.04, Mint, and Pop!_OS are tested with the
backend and GTK client because their Ubuntu 24.04 package base does not ship
the Qt 6 Python/Kirigami dependencies required by the Qt client.

From the repository root, install the build dependencies and build with:

```bash
sudo apt-get install devscripts equivs
sudo mk-build-deps -i -r -t 'apt-get -y --no-install-recommends' packaging/deb/control
./packaging/build-deb.sh
```

Finished packages are written to `dist/deb/`. Install the backend and the
client for your desktop, for example:

```bash
sudo apt install ./dist/deb/blueferry-backend_*.deb ./dist/deb/blueferry-gtk_*.deb
```

`blueferry-backend` includes `blueferry-tui` and a pinned private Textual 8
runtime under `/usr/lib/blueferry/vendor`. The package build is offline: its
wheel bundle is stored in the source tree and verified against committed
SHA-256 checksums. Nothing is installed into Python's system package directory,
so the bundle cannot replace or conflict with `python3-textual`.

The backend intentionally leaves the distribution's `bluetooth.service`
unchanged: installing, upgrading, or removing the DEB never enables `-E` or
restarts Bluetooth. MAP messages and PBAP contacts work with the package's
BlueZ 5.72 minimum. ANCS notifications are available only when the machine
already has BlueZ 5.86 or newer and its running `bluetoothd` exposes the
experimental bearer API through `-E` or `--experimental`; otherwise BlueFerry
automatically stays in MAP/PBAP-only mode.
