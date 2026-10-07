# Testing

> **Note:** This is a summary.
> [TESTING.md](https://github.com/erikwb/blueferry/blob/main/TESTING.md) is
> authoritative and lists what each part of the suite covers.

The automated suite must be safe on a desktop with a paired, connected
iPhone. It never opens live BlueZ or OBEX connections, reads from a phone,
sends a message, changes pairing state, or talks to the installed BlueFerry
backend or notification service.

## Run the tests

```sh
# Unit and contract suite; the private D-Bus test is skipped.
python -m pytest

# Full hermetic suite, including the public D-Bus round trip
# on a private bus without service activation.
dbus-run-session --config-file=tests/dbus-test.conf -- sh -c '
  export BLUEFERRY_TEST_DBUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS"
  export DBUS_SYSTEM_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS"
  export PYTHONPATH=src
  python -m pytest
'
```

The quality workflow also runs:

```sh
ruff check .
bandit -r src/blueferry -q
mypy src/blueferry
qmllint src/blueferry/qt/qml/*.qml data/quickshell/*.qml
```

## Rules for new tests

- `tests/conftest.py` replaces the session- and system-bus constructors with
  failures. Inject small fakes at the I/O edge instead.
- Build daemons with the `make_daemon` fixture, which isolates every state
  path. Don't assemble one with `Daemon.__new__`.
- Parser tests use inert strings or reviewed fixtures. Hypothesis tests feed
  malformed bMessage, vCard, ANCS, and recipient input to pure functions.
- Storage tests use in-memory key providers and never touch the real
  keyring.
- QML tests load components only with an injected inert controller.
- Assert observable behavior so a test stays valid when the implementation
  is rewritten.

Experiments with a real iPhone are manual development work, never part of the
automated suite. Record lasting findings in
[PROTOCOL.md](https://github.com/erikwb/blueferry/blob/main/PROTOCOL.md).

## Packages

The native package matrix builds each package once and installs it in clean
containers for every tested distribution. On targets with the Qt client it
also runs `packaging/smoke-qt.py` against the installed files.
