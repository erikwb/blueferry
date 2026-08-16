# Architecture

BlueFerry is a per-user Bluetooth bridge with multiple presentation
clients. The backend is the only process that owns long-lived iPhone profile
sessions or persistent message state.

```text
GTK client ───────┐
Qt client ────────┤
TUI client ───────┼── session D-Bus ── backend daemon ── BlueZ system D-Bus
Quickshell client ┘                         │
                                           ├── BlueZ OBEX session D-Bus
                                           └── private state and notifications
```

## Process boundaries

- `blueferry-backend` owns MAP, PBAP, ANCS, contact resolution, thread
  identity, history retention, notification policy, and the private D-Bus API.
- GTK, Qt, TUI, and Quickshell are replaceable clients. They receive opaque
  thread keys and cannot construct different recipients for an existing thread.
- `pair_setup` is the low-level setup boundary. `setup_client` exposes its
  typed operations to GTK and Qt, and the hidden JSON commands adapt the same
  operations for Quickshell. `pairing_policy` resolves controller capability
  and user overrides into one concrete recipe. Setup discovers and pairs
  devices, writes the selected MAC and ANCS policy to the user's configuration,
  and asks systemd to authorize and run system-level setup operations.
- `onboarding` derives a toolkit-neutral first-run stage from configuration,
  controller capabilities, the selected bond, and backend status. Controller
  support is detected from read-only capabilities instead of vendor names. A
  raw hardware view remains separate from the saved target's ANCS policy so a
  compatibility pairing can reach `ready-without-ancs` without making the
  controller appear permanently incapable of full pairing. GTK, Kirigami,
  Quickshell, and the CLI share pair/configure/re-pair and verified-readiness
  semantics.
- The daemon runs unprivileged and has no sudo command path.

Stable D-Bus identifiers live in `blueferry.protocol`. The service exports
`Messages1` for commands and snapshots and `Events1` for content-free live
coordination. A future
incompatible API must use a new interface suffix rather than silently changing
existing method contracts. `data/io.weirdware.BlueFerry.xml` is the
canonical introspection contract, is installed under `dbus-1/interfaces`, and
is checked against the dbus-python decorators in the service implementation.

`Events1.HistoryChanged` carries only a daemon-local revision and
`Events1.StatusChanged` has no arguments. `Events1.OpenMessageRequested`
carries only a bounded, opaque MAP handle after the user invokes a desktop
message notification; clients locate it in their own private thread snapshot.
Complete message records, sender identities, ANCS fields, contacts, and
connectivity details are never broadcast on the session bus. The daemon emits
invalidations at message, contact-cache, connectivity, ANCS subscription,
initialization, and storage transitions. Clients verify MAP/PBAP success
without mistaking Linux-side bond creation for end-to-end success.

Backend payloads cross D-Bus as JSON to keep the wire contract simple. Both
Python client implementations decode them immediately into the shared models
in `blueferry.models`; those records retain unknown fields for forward
compatibility. Toolkit presentation code either consumes their typed fields or
explicitly converts them to dictionaries. Quickshell has no generic QML D-Bus
client, so its persistent stdin bridge converts the same models back to JSON
while calling the daemon over the session bus. This keeps private request data
out of process arguments without adding a Plasma-specific dependency.
All Python transports share `client_wire` for response-shape validation and
model conversion; synchronous and toolkit-specific scheduling remain separate.
Group-thread messages include a display-only sender label derived from the
resolved MAP contact or the correlated Messages notification. It never
participates in thread identity or reply routing; outgoing labels are localized
by each client from the existing `outgoing` flag.

`backend_operations` owns validation, thread routing, and application policy.
`dbus_service` is only a wire-type adapter over those operations. `daemon`
orchestrates lifecycle and supplies one typed `BackendDependencies` object,
while `event_dispatcher` owns persistence/notification
fan-out. `ProfileSupervisor` owns MAP/PBAP open, close, retry, loss, and resume
transitions; its worker, session, and timer protocols make those races testable
without BlueZ. Interactive pairing rendering lives separately from the BlueZ
pairing service. GTK and Qt run the agent workflow in a GLib/D-Bus-isolated
helper and reserve stdout for its JSON interaction protocol. The normal
interactive transaction registers a device-scoped default agent and calls
`Device1.Connect()` so the iPhone initiates authentication. A separate
explicit-pairing override calls `Device1.Pair()` for controllers that cancel
Connect-first. The temporary agent remains present while the bond is trusted,
Classic settles, solicitation is registered, and the daemon makes its first
MAP/PBAP attempt; cleanup then releases the advertisement and agent in order.
CLI message commands use the same backend client as graphical UIs. Group
replies use `SendToThread`, so routing always comes from the backend's current
conversation projection rather than client-supplied recipients.

## Pairing policy

Pairing has two independent axes rather than a growing table of device quirks:

- Delivery mode is `full` when ANCS is supported and selected. Compatibility
  mode, selected explicitly for older iOS or automatically when ANCS is
  unavailable, persists `BLUEFERRY_ANCS_ENABLED=false`; MAP and PBAP remain the
  success boundary and the daemon never enables LE/ANCS for that target.
- Authentication strategy is normally `iphone-initiated-connect`. The user may
  independently select `explicit-device-pair`; non-interactive callers also use
  that strategy because they cannot present BlueFerry's confirmation UI.

ANCS connection policy and ANCS solicitation are deliberately separate. After
the Classic bond is trusted and settled, setup broadcasts the short-lived
solicitation whenever the controller can advertise, including compatibility
mode. Real-device testing indicates that older iOS uses this signal to expose
its MAP/PBAP permission toggles even though BlueFerry must not connect ANCS.
Full mode starts the daemon with LE held back until its first MAP/PBAP attempt
completes; compatibility mode leaves LE disabled. Pairing reports record the
resolved policy, controller capability, ordered timeline, package build ID, and
full source-content SHA.

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

Native packages install release and source-content SHA markers owned by the
backend package. The daemon publishes their combined identity as the private
`_build_id` status field, formatted as the package release plus a twelve-digit
SHA prefix; pairing reports retain that ID and the complete SHA. A running
daemon notices a changed marker and exits with status 75 so systemd restarts
it; clients compare `_build_id` and perform a serialized fallback restart if an
older process is still running. Missing SHA markers retain compatibility with
older/source builds, and lifecycle tests replace marker reads and command
runners so installed host state can never restart a real service. Content
hashing distinguishes local rebuilds that retain the same package version.
Package scripts never try to address arbitrary logged-in users' service
managers; distro systemd hooks reload changed user-unit metadata.

## Data and trust

- The iPhone is remote input. Sender-provided names, vCards, notification text,
  recipients, and timestamps are parsed and validated before use.
- Contact names are display metadata; normalized phone or email addresses are
  identities. This keeps same-name contacts and group participants distinct.
- The backend owns reply routing. Clients use an opaque thread key and must
  confirm a group's participants before the first reply. Named-group ANCS
  notifications initially produce a read-only thread; only the backend can
  retain a user-supplied route, and it requires all observed senders to remain
  in that route. A sender outside an established route produces a distinct
  roster-change warning and disables replies. Routes are local metadata and do
  not modify iPhone groups; named groups with the same name necessarily share
  one key because ANCS provides no conversation identifier.
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

PBAP download and vCard parsing live in `contacts`; `contact_repository` owns
the contact schema, replacement transaction, encrypted records, and legacy
plaintext cleanup. This keeps Bluetooth failure handling outside persistence.

Slow public D-Bus methods use deferred replies, so Bluetooth waits never block
the daemon's event loop. Status, lifecycle signals, and incoming BlueZ events
remain dispatchable while an OBEX transfer is active. The GTK client serializes
snapshot reads on a worker-owned private bus connection and marshals results to
GLib; its sends use dbus-python reply handlers. The Qt/KDE client exposes an
asynchronous controller to Kirigami, serializes work in a QThreadPool, and
coalesces Events1 invalidations into snapshot refreshes. Neither presentation main loop waits
on the backend, BlueZ, or systemd.

Every native backend package includes the TUI. Arch uses the distribution's
`python-textual` package, while DEB and RPM backend packages carry the Textual
runtime in a private vendor directory. The launcher activates that private
runtime only from packaged entry points, so
it cannot hijack a venv or source checkout. Blocking snapshot reads and sends
run in Textual workers with thread-owned D-Bus connections;
content-free HistoryChanged and StatusChanged signals trigger coalesced refreshes,
with a bounded periodic refresh as a fallback. The client pumps its GLib signal
context without blocking Textual, follows notification-open requests, adapts to
narrow terminals, and preserves backend-owned direct and group reply routing.

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
  changes. Keep `Events1` payloads content-free; private records remain behind
  unicast `Messages1` snapshot calls.
- Split `cli.py` or the Qt page module when a new feature would add another
  independent domain; avoid moving code solely to reduce line counts.
- Keep toolkit root files focused on navigation and composition. Put cohesive
  state derivation and presentation in loadable QML components with behavioral
  tests rather than growing root-level functions.
- Prefer narrowly tested helpers over broad exception handling. Best-effort
  cleanup may catch broadly, but command paths must return actionable errors.
