# Native package support

BlueFerry uses each distribution's native Python and desktop libraries. It
does not download from PyPI during a package build or bundle native system
libraries. The private TUI-only Python runtime is the deliberate exception
described below.

| Package recipe | Tested distributions | Output |
| --- | --- | --- |
| `arch/PKGBUILD` | Arch Linux, CachyOS | backend with TUI, GTK, Qt, Quickshell |
| `deb/` | Debian 13, Ubuntu 26.04, PikaOS IV | MAP/PBAP backend with TUI, GTK, Qt |
| `deb/` | Ubuntu 24.04, Linux Mint 22.3, Pop!_OS 24.04 | MAP/PBAP backend with TUI, GTK |
| `rpm/blueferry.spec` | Fedora 43, Fedora 44 | backend with TUI, GTK, Qt |

The matrix tests current package ecosystems rather than claiming that one
artifact works forever. Arch and CachyOS are rolling releases, and PikaOS IV
tracks Debian Sid with PikaOS rebuilds. CI builds each artifact once on its
designated family base, then installs that exact artifact in clean
containers for every listed target. Ubuntu 24.04, Mint 22.3, and Pop!_OS 24.04
do not provide BlueFerry's Qt 6 Python/Kirigami dependencies, so those claims
cover the backend, TUI, and native GTK client.

Arch and RPM backends require BlueZ 5.86 or newer and install a vendor
`bluetooth.service` drop-in that runs `bluetoothd -E`. Their package lifecycle
reloads systemd and restarts Bluetooth only when it is already running. The DEB
backend instead uses the common BlueZ 5.72 baseline supplied by all listed
Debian-family targets. It supports MAP messages and PBAP contacts, assumes ANCS
system notifications are unavailable, and neither changes nor restarts the
system Bluetooth service.

openSUSE is the next sensible RPM target. Rocky Linux and AlmaLinux are popular
RPM server distributions, but they are less relevant to a Bluetooth desktop
application and their conservative stacks are a poor initial compatibility
target. Fedora derivatives may accept the Fedora RPM, but they are not claimed
as tested until they are present in the matrix.

Arch uses its native `python-textual` package. DEB and RPM repositories do not
provide a sufficiently recent Textual, so those backend packages contain a
pinned private copy of Textual 8 and its pure-Python dependency closure. The
private bundle is not installed into the system Python package directory. See
each recipe's README for local commands.
