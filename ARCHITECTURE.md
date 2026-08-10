# Architecture

BlueFerry is a per-user Bluetooth bridge with multiple presentation
clients. The backend is the only process that owns long-lived iPhone profile
sessions or persistent message state.

```text
GTK client ───────┐
Qt client ────────┼── session D-Bus ── backend daemon ── BlueZ system D-Bus
Quickshell client ┘                         │
                                           ├── BlueZ OBEX session D-Bus
                                           └── private state and notifications
```

## Process boundaries

- `blueferry-backend` owns MAP, PBAP, ANCS, contact resolution, thread
  identity, history retention, notification policy, and the private D-Bus API.
- GTK, Qt, and Quickshell are replaceable clients. They receive opaque thread
  keys and cannot construct different recipients for an existing thread.
- `pair_setup` is the low-level setup boundary. `setup_client` exposes its
  typed operations to GTK and Qt, and the hidden JSON commands adapt the same
  operations for Quickshell. It discovers and pairs devices, writes the
  selected MAC to the user's configuration, and requests explicit Polkit
  authorization for system-level setup operations.
- `onboarding` derives a toolkit-neutral first-run stage from configuration,
  controller capabilities, the selected bond, and backend status. Controller
  support is detected from read-only capabilities instead of vendor names.
  GTK, Kirigami, Quickshell, and the CLI share pair/configure/re-pair and
  verified-readiness semantics.
- The daemon runs unprivileged and has no sudo command path.

Stable D-Bus identifiers live in `blueferry.protocol`. The service exports
`Messages1` for commands and snapshots and `Events1` for content-free live
invalidations. A future
incompatible API must use a new interface suffix rather than silently changing
existing method contracts. `data/io.weirdware.BlueFerry.xml` is the
canonical introspection contract, is installed under `dbus-1/interfaces`, and
is checked against the dbus-python decorators in the service implementation.

`Events1.HistoryChanged` carries only a daemon-local revision and
`Events1.StatusChanged` has no arguments. Clients respond by fetching the
corresponding private snapshot with a unicast `Messages1` method call. Complete
message records, ANCS fields, contacts, and connectivity details are never
broadcast on the session bus. The daemon emits invalidations at message,
contact-cache, connectivity, ANCS subscription, initialization, and storage
transitions. Clients verify MAP/PBAP success without mistaking Linux-side bond
creation for end-to-end success.

Backend payloads cross D-Bus as JSON to keep the wire contract simple. Both
Python client implementations decode them immediately into the shared models
in `blueferry.models`; those records retain unknown fields for forward
compatibility. Toolkit presentation code either consumes their typed fields or
explicitly converts them to dictionaries. Quickshell's CLI adapter converts
the same models back to JSON rather than defining a separate backend client.

`backend_operations` owns validation, thread routing, and application policy.
`dbus_service` is only a wire-type adapter over those operations. `daemon`
orchestrates lifecycle, while `event_dispatcher` owns persistence/notification
fan-out. `ProfileSupervisor` owns MAP/PBAP open, close, retry, loss, and resume
transitions; its worker, session, and timer protocols make those races testable
without BlueZ. Interactive pairing rendering lives separately from the BlueZ
pairing service, and CLI message commands use the same backend client as
graphical UIs. Group replies use `SendToThread`, so routing always comes from
the backend's current conversation projection rather than client-supplied
recipients.

## Lifecycle

The backend unit is a systemd user service with `Type=dbus`. Session D-Bus can
activate it on demand. The backend package owns a vendor
`default.target.wants` link, and `ConditionPathExists` skips users without a
pairing configuration. This makes login autostart package-owned and reversible
while still receiving incoming events when no client is open.

Startup publishes the D-Bus control surface before beginning hardware work.
Bluetooth initialization is deferred until the GLib loop is dispatching, and
`GetStatus` reports `initializing` and degraded profile state explicitly.
MAP/PBAP connectivity moves through explicit initializing, connecting, ready,
degraded, reconnecting, authorization-required, map-connection-refused, and
stopping states. Failed profile opens poll every 5 seconds until the first
successful MAP/PBAP connection, then every 15 seconds for later reconnects.
Suspend/resume and observed OBEX loss enter the same reconnection path. The
refusal state preserves the iPhone's
`Connection refused (111)` detail so clients can explain that another computer
may currently own its single MAP connection.

Arch packages install a release marker owned by the backend package. A running
daemon notices a changed marker and exits with a failure status so systemd
restarts it. Clients perform a serialized fallback restart for daemons released
before self-restart support. Pacman scripts never try to address arbitrary
logged-in users' service managers; Arch's systemd package hook reloads changed
user-unit metadata.

## Data and trust

- The iPhone is remote input. Sender-provided names, vCards, notification text,
  recipients, and timestamps are parsed and validated before use.
- Contact names are display metadata; normalized phone or email addresses are
  identities. This keeps same-name contacts and group participants distinct.
- The backend owns reply routing. Clients use an opaque thread key and must
  confirm a group's participants before the first reply.
- Message history and the contact cache are user-private (`0700` directories,
  `0600` SQLite files). Their sensitive records are authenticated and encrypted
  with AES-256-GCM under one random application key held by the desktop Secret
  Service. GNOME Keyring and KWallet are reached through the same libsecret
  interface; toolkit clients never handle the key. Generic key lookup
  attributes are intentionally non-sensitive.
- Passive daemon startup loads an existing key only from an already unlocked
  collection. It neither creates an item nor requests a wallet prompt. If the
  key is unavailable or ciphertext authentication fails, storage fails closed
  while live message delivery continues. An explicit client action may create
  or unlock the key through the desktop's normal wallet UI.
- History writes, pruning, and clearing are transactions; a monotonic database
  revision invalidates the conversation projection. Event kind, timestamp, and
  content are all inside the authenticated ciphertext. Unframed plaintext and
  a wrong or replaced key fail closed without deleting the stored records.
- ANCS is app-identified before content is requested. Under the default
  policy, unrelated app/system content is never fetched. Under the opt-in
  `all` policy it is delivered only to an explicitly ephemeral popup sink,
  never retained or broadcast on D-Bus. Apple Messages retains only the fields
  required for group correlation, and those raw records are not exposed by the
  public event-query method.
- Logs exclude message bodies, notification text, and recipient identities at
  every level. UI markup and terminal output are escaped at their respective
  display boundaries.
- Configuration files are owner-only, size-bounded, and opened without
  following final symlinks. Only named BlueFerry settings are accepted;
  systemd never sources the file as a general process environment.
- Every public method obtains the caller's unique bus name and credentials from
  the bus daemon, rejects callers whose Unix UID differs from the backend, and
  applies per-connection plus daemon-wide quotas. Sends, contact sync, storage
  unlock, destructive operations, reads, and status probes have separate
  limits. Disconnecting revokes the cached caller identity; reconnecting does
  not reset daemon-wide consequential-operation limits. Snapshot counts, event
  kinds, body projections, query text, and serialized JSON replies are bounded.
- Expected D-Bus failures expose stable, length-bounded messages. Unexpected
  exceptions and lower-level OBEX details stay in the private daemon log rather
  than returning device paths or implementation details to callers.
- The Unix login session is the local security boundary. Native processes
  running as the same user can call the session D-Bus API and, while the wallet
  is unlocked, may be able to request its secrets. Encryption protects retained
  data at rest; it does not isolate BlueFerry from a compromised login session.
  Sandboxed clients must be granted the `io.weirdware.BlueFerry` bus name
  explicitly.

## I/O model

The backend uses dbus-python with one GLib event loop. Bus connections are
created lazily at the I/O edge, so importing parsers or models does not require
a live desktop or system bus. One dedicated worker owns a private session-bus
connection and serializes every blocking MAP/PBAP operation: session creation,
sends, phonebook pulls, message queries, and incoming-body downloads. Its
backlog is bounded, and results are marshalled back through GLib before
touching daemon state. Bluetooth transfers, parsers, contact cardinality,
desktop read-state watches, D-Bus replies, and retained payload bytes all have
explicit resource ceilings.

Slow public D-Bus methods use deferred replies, so Bluetooth waits never block
the daemon's event loop. Status, lifecycle signals, and incoming BlueZ events
remain dispatchable while an OBEX transfer is active. The GTK client serializes
snapshot reads on a worker-owned private bus connection and marshals results to
GLib; its sends use dbus-python reply handlers. The Qt/KDE client exposes an
asynchronous controller to Kirigami, serializes work in a QThreadPool, and
coalesces Events1 invalidations into snapshot refreshes. Neither presentation main loop waits
on the backend, BlueZ, or systemd.

GTK follows the GNOME stack with GTK4, libadwaita navigation, adaptive
breakpoints, application actions, and Adwaita dialogs. KDE follows the Plasma
stack with Qt Quick Controls, Kirigami navigation and pages, KDE's desktop
style, and event-driven QDBus subscriptions. User-visible Python strings pass
through gettext and QML strings use `qsTr`; `po/POTFILES.in` is the translation
source inventory.

All external commands cross `commands.run_command`, which accepts argv rather
than shell text; installed runtime commands use absolute paths. The boundary
normalizes missing executables, timeouts, and non-zero exits. Expected
application failures use the hierarchy in `errors`; the D-Bus adapter maps
those to stable names below `io.weirdware.BlueFerry.Error`.

## Tests

Pure parser, routing, retention, and lifecycle tests run without a live D-Bus.
The harness rejects attempts to open the user's real session or system bus.
Presentation tests inject fake clients and verify typed model conversion and
off-main-thread dispatch. The Arch package check additionally runs one public
API round trip inside an activation-free private bus, validates
desktop/AppStream metadata, imports both Python clients, lints Kirigami and
Quickshell QML, and builds every split package from a deterministic snapshot.
See `TESTING.md` for the safety and quality rules.

Empirical iPhone and BlueZ behavior is recorded separately in `PROTOCOL.md`.
Architecture should depend only on findings that remain reproducible there,
not on one-off experiment scripts.

## Growth rules

- Keep Bluetooth and storage ownership in the backend; do not duplicate that
  logic in toolkit clients.
- Keep cryptographic keys out of configuration, logs, D-Bus payloads, and
  presentation processes. Keyring lookup attributes are public metadata.
- Add protocol behavior behind the D-Bus boundary before adding UI controls.
- Change a versioned interface only compatibly and update the canonical XML and
  typed client models in the same patch; use a new suffix for incompatible
  changes. The initially shipped `Events1` contract is invalidation-only.
- Split `cli.py` or the Qt page module when a new feature would add another
  independent domain; avoid moving code solely to reduce line counts.
- Prefer narrowly tested helpers over broad exception handling. Best-effort
  cleanup may catch broadly, but command paths must return actionable errors.
