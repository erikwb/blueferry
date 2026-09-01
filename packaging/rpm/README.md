# Fedora RPM packages

This spec builds `blueferry-backend`, `blueferry-gtk`, and `blueferry-qt`
using only Fedora repository dependencies. Package CI builds one artifact set
for each Python ABI in the supported Fedora releases, then installs each exact
set on the compatible releases. The `.fc43` packages cover Fedora 43 and 44;
Fedora 45 gets separate `.fc45` packages because it uses Python 3.15.
The backend requires BlueZ 5.86 or newer because that is the first upstream
release with working per-bearer connection methods.

From the repository root:

```bash
sudo dnf install dnf-plugins-core rpm-build
sudo dnf builddep packaging/rpm/blueferry.spec
./packaging/build-rpm.sh
```

Finished packages are written to `dist/rpm/`. Install the backend and your
preferred graphical client, for example:

```bash
sudo dnf install dist/rpm/blueferry-backend-*.noarch.rpm \
  dist/rpm/blueferry-gtk-*.noarch.rpm
```

`noarch` means the package contains no CPU-specific machine code; it does not
make Python installation paths or dependencies ABI-independent. Release RPMs
must therefore have the `.fcNN` tag matching the installed Fedora release.

`blueferry-backend` includes the `blueferry-tui` command and a pinned private
Textual 8 runtime under `/usr/lib/blueferry/vendor`. The package build is offline: its
wheel bundle is stored in the source tree and verified against committed
SHA-256 checksums. Nothing is installed into Python's system package directory,
so the bundle cannot replace or conflict with Fedora's `python3-textual`.

The backend installs a systemd drop-in that runs `bluetoothd -E`. RPM
scriptlets reload systemd and use `try-restart`, so an already-running
Bluetooth daemon immediately adopts the setting while a stopped or masked
service remains stopped. Removal restarts a running daemon with Fedora's
original BlueZ unit.

The spec is Fedora-specific. openSUSE is the other important desktop RPM
family, but its dependency names and RPM macros differ enough that it should
get a separate spec and openSUSE Build Service test rather than an unsupported
compatibility claim.
