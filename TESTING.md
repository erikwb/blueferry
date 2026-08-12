# Testing

The automated suite must be safe to run on a desktop that has a paired,
connected iPhone. It never opens live BlueZ or OBEX connections, reads from a
phone, sends a message, changes pairing state, or talks to the installed
BlueFerry daemon or notification service.

`tests/conftest.py` enforces that boundary for ordinary tests by replacing the
session- and system-bus constructors with failures. Tests inject small fakes at
the I/O edge. The public D-Bus round-trip test is the sole exception: it runs
only when explicitly opted into a bus created from `tests/dbus-test.conf`.
That configuration has no service-activation directories, so it cannot start
installed desktop, Bluetooth, OBEX, notification, or BlueFerry services.

```sh
# Safe unit/contract suite; private-D-Bus test skips. The checkout's `src/`
# path is also pinned in pyproject.toml so an installed copy cannot win.
python -m pytest

# Full hermetic suite, including the public D-Bus round trip.
dbus-run-session --config-file=tests/dbus-test.conf -- sh -c '
  export BLUEFERRY_TEST_DBUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS"
  export DBUS_SYSTEM_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS"
  export PYTHONPATH=src
  python -m pytest
'
```

The repository quality workflow runs the same hermetic suite on Arch Linux,
along with Ruff, Bandit, mypy, QML linting, and a coverage report. The package
build remains the final split-package and desktop-metadata integration check.

## What belongs in the suite

- Protocol tests use inert strings or captured, reviewed fixtures and assert
  exact parsing or wire-format behavior.
- Hypothesis tests generate malformed and boundary-shaped bMessage, vCard,
  ANCS, recipient, and group-correlation input. They call pure functions only,
  use deterministic settings, and remain behind the same fatal D-Bus guard.
- Security tests assert trust boundaries and adversarial inputs.
- Storage tests inject deterministic in-memory key providers. They never load
  libsecret, contact GNOME Keyring/KWallet, create wallet entries, display an
  unlock prompt, or inspect the user's encrypted BlueFerry databases.
- Lifecycle and concurrency tests assert externally meaningful outcomes, not
  private call order unless the order itself prevents a leak or race.
- Packaging tests keep runtime identifiers and installed metadata consistent.
- A test should remain valid if the implementation is rewritten without
  changing the behavior it protects.

Real-device experiments are manual development work, never automated test work.
They require the operator to understand the exact action being performed;
outgoing-message experiments require a deliberately chosen recipient and
must not be hidden behind an automated test command.

`test_dbus_contract.py` compares the shipped introspection XML with the
dbus-python decorators, including signatures and stable application errors.
Client-model and setup-facade tests use plain mappings and monkeypatched
operations; they must not probe BlueZ merely to exercise serialization.
Bluetooth compatibility tests feed inert `btmgmt info` text through the parser
and fake BlueZ's object inventory. They assert capabilities rather than
controller brands and never execute `btmgmt` against the host.
The GTK client worker test replaces both the bus and GLib handoff, while the
Kirigami controller test disables QDBus subscription and autostart. QML lint
and an optional offscreen load may construct presentation objects only with an
injected inert controller; a GUI smoke test must never use the default
controller because that would activate the installed backend.

The Arch package check runs Ruff over the complete source and test tree,
Bandit over the Python security boundaries, and type-checks every backend
module with mypy. Toolkit clients remain outside that
broad type-checking pass because their dynamically generated GObject and Qt
APIs have little useful static type information; their pure presenters and
models retain focused tests instead.
