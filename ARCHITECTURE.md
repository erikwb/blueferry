# Architecture

BlueFerry is a per-user Bluetooth bridge from a paired iPhone to the Linux
desktop, with several interchangeable presentation clients. One unprivileged
backend daemon owns every long-lived iPhone profile session (MAP, PBAP, ANCS)
and all persistent message state; clients talk to it over a private session
D-Bus API.

```text
GTK client ───────┐
Qt client ────────┤
TUI client ───────┼── session D-Bus ── backend daemon ── BlueZ system D-Bus
Quickshell client ┘                         │
                                           ├── BlueZ OBEX session D-Bus
                                           └── private state and notifications
```

Empirical iPhone and BlueZ behavior is recorded in `PROTOCOL.md`; test safety
rules are in `TESTING.md`.

## Module map

All paths are relative to `src/blueferry/` unless noted.

### Backend core

| Module | Responsibility |
| --- | --- |
| `daemon.py` | Orchestrates lifecycle: publishes D-Bus, then starts Bluetooth, supervisors, state, and sinks; builds `BackendDependencies`. |
| `backend_operations.py` | Toolkit- and transport-neutral application operations: validation, thread routing, and policy. |
| `dbus_service.py` | Session D-Bus adapter (`Messages1`/`Events1`/`Presence1`) that maps operations to wire types; claims the bus name. |
| `dbus_security.py` | Caller UID validation and per-connection/daemon-wide rate limits. |
| `protocol.py` | Stable D-Bus identifiers and the API-generation compatibility check. |
| `event_dispatcher.py` | Builds messages from MAP/ANCS events and fans them out to persistence, desktop, and D-Bus sinks. |
| `events.py` | Normalized MAP event dataclasses (`SmsEvent`) and address/timestamp normalization. |
| `threads.py` | Backend-owned conversation projection, thread keys, and reply-routing metadata. |
| `grouping.py` | Correlates MAP iMessages with ANCS Messages notification metadata to recover group membership. |
| `named_groups.py` | Named-group identity keys and saved reply routes. |
| `confirmed_groups.py` | Persistent confirmed group rosters in the owner-only settings document. |
| `group_routes.py` | Saved named-group reply rosters in the settings document, outside history retention. |
| `starred_threads.py` | Persistent starred-conversation keys in the settings document. |
| `notification_policy.py` | Persistent desktop notification preferences. |
| `private_preferences.py` | Encrypts a whole preference collection under the storage policy. |
| `settings_store.py` | Small atomic store shared by daemon-owned preferences. |
| `glib_timers.py` | `schedule_periodic`: a repeating GLib timer whose owner forgets the source id once GLib destroys the source (false return or exception). |
| `read_receipts.py` | Delays MAP read acknowledgements so ANCS can still deliver group metadata. |
| `recipients.py` | Recipient validation, participant-line parsing, and group confirmation tokens (shared by backend and Python clients). |
| `connectivity.py` | MAP/PBAP connectivity state machine and retry policy, independent of GLib and BlueZ. |
| `profile_supervisor.py` | Owns MAP/PBAP open, close, retry, loss, and resume transitions. |
| `background_worker.py` | Bounded local jobs completed on the owning GLib loop. |
| `bus.py` | Lazy D-Bus connections and GLib integration, including the OBEX worker's private bus. |
| `limits.py` | Central safety and resource limits. |
| `errors.py` | Application error hierarchy shared across transport and presentation. |
| `commands.py` | The only path for running external commands (argv, absolute paths, normalized failures). |
| `config.py` | Environment-backed configuration (`local.env`) and private runtime paths. |
| `private_files.py` | Race-resistant owner-only reads and atomic writes for small files. |
| `build_info.py` | Package release + source-SHA build identity. |
| `wireplumber_policy.py` | Manages one WirePlumber fragment that keeps iPhone audio on the phone. |
| `proximity_lock.py` | Opt-in lock-only desktop lock after the iPhone's bearers stay down for a grace period; lock dispatch via ScreenSaver, then logind. |

### Bluetooth transports and supervision

| Module | Responsibility |
| --- | --- |
| `obex/sessions.py` | Long-lived MAP and PBAP OBEX sessions, reopened only on observed failure. |
| `obex/worker.py` | Single worker serializing all blocking OBEX operations. |
| `obex/transfer.py` | Shared `Transfer1` completion handling. |
| `obex/map_events.py` | MAP MNS event subscription and asynchronous bMessage retrieval. |
| `obex/map_query.py` | Direct MAP folder listing and message fetch. |
| `obex/map_send.py` | Builds and pushes outgoing bMessages (direct and group). |
| `obex/map_read.py` | Best-effort MAP read-state write-back. |
| `obex/bmessage.py` | Minimal bMessage parser. |
| `contacts.py` | PBAP phonebook pull, vCard parsing, and address-to-name resolution. |
| `contact_sync.py` | Schedules PBAP pulls (MAP grace period, daily refresh, joined manual requests) and discards pulls that span a storage key or policy change. |
| `contact_repository.py` | Contact-cache SQLite schema, replacement transaction, encryption, legacy cleanup. |
| `call_history.py` | Opt-in: pure parsing of PBAP call-history vCards (`ich`/`och`/`mch`) and their merge. |
| `call_history_repository.py` | Encrypted call-history mirror, retention, and the already-announced missed-call set. |
| `call_history_sync.py` | Polls call history on the OBEX worker and reports newly seen missed calls. |
| `vcard.py` | Linear, resource-bounded vCard block extraction. |
| `ancs/client.py` | ANCS GATT client: subscribes to characteristics, requests attributes, emits `AncsEvent`s. |
| `ancs/parsers.py` | Pure ANCS wire-format parsers and command builders. |
| `ancs/constants.py` | ANCS spec constants. |
| `ancs/events.py` | `AncsEvent`, the normalized per-app notification. |
| `ancs/sequencer.py` | Bounded, duplicate-aware backlog of serialized ANCS requests. |
| `bearer_supervisor.py` | Connects BR/EDR first, then keeps LE connected alongside it. |
| `solicitation_supervisor.py` | Keeps the ANCS solicitation advertisement on air until ANCS is proven healthy. |
| `adapter_class_supervisor.py` | Detects Class-of-Device drift and repairs it through the constrained system helper. |
| `bluetooth_recovery.py` | Last-resort, rate-limited adapter power cycle for persistent ANCS outages. |
| `bluez_setup.py` | Adapter preparation: Class-of-Device and the ANCS solicitation advertisement. |
| `bluetooth_capabilities.py` | Controller capability probing and packaged BlueZ activation. |
| `bluetooth_devices.py` | Typed BlueZ device projection for setup and clients. |

### Sinks

| Module | Responsibility |
| --- | --- |
| `sinks/__init__.py` | Sink protocol: `handle(event)` plus optional `handle_ancs`. |
| `sinks/sqlite.py` | Persists events to the private history store. |
| `sinks/libnotify.py` | Desktop notifications via `org.freedesktop.Notifications`, including open and dismiss actions. |

### Storage and privacy

| Module | Responsibility |
| --- | --- |
| `history.py` | Bounded, encrypted SQLite event history with a monotonic revision. |
| `storage_security.py` | Retention policy and Secret Service-backed AES-256-GCM encryption. |
| `storage_preparation.py` | Worker-side storage validation before the daemon commits it. |
| `text_safety.py` | Escaping for remote text at terminal presentation boundaries. |
| `message_links.py` | Escaped markup with safe clickable web URLs, shared by GTK and QML. |

### Pairing and setup

| Module | Responsibility |
| --- | --- |
| `pair_setup.py` | Low-level discovery, pairing, and first-run configuration. |
| `setup_client.py` | Typed setup API used by GTK, Qt, and the CLI wizard; runs the isolated pairing helper. |
| `pairing_policy.py` | Resolves controller capability and user overrides into one of two pairing recipes. |
| `pairing_agent.py` | Short-lived BlueZ pairing agent for interactive setup. |
| `pairing_types.py` | Typed state passed between pairing stages. |
| `pairing_cli.py` | Interactive terminal pairing wizard. |
| `pairing_diagnostics.py` | Bounded, privacy-preserving pairing diagnostics. |
| `quirks_report.py` | Scrubbed pairing/adapter reports for issue filing. |
| `setup_verification.py` | Non-sensitive evidence that iPhone setup capabilities work. |
| `onboarding.py` | Toolkit-neutral first-run stage derivation. |

### Shared client layer

| Module | Responsibility |
| --- | --- |
| `client.py` | Synchronous, toolkit-neutral daemon client. |
| `client_wire.py` | Validates and decodes `Messages1` JSON into models for every Python client. |
| `models.py` | Typed client-side models (`BackendStatus`, `Thread`, …); `Thread` derives unread counts, approval tokens, and roster-warning keys. |
| `conversation_state.py` | Toolkit-neutral conversation snapshot, contact search, and reply planning. |
| `backend_lifecycle.py` | Starts the daemon and restarts one that predates installed files. |
| `client_activation.py` | Picks and activates one desktop client (recency files, notification-open forwarding). |
| `glib_client_activation.py` | GLib adapter for client activation (GTK and the Quickshell bridge). |
| `time_display.py` | Human-readable local timestamps for all clients. |
| `i18n.py` | gettext helpers for Python presentation layers. |

### Clients and entry points

| Module | Responsibility |
| --- | --- |
| `cli.py`, `__main__.py` | Typer CLI (`run`, `doctor`, sync, setup, and hidden `pairing-*` JSON helpers). |
| `cli_messages.py` | CLI message listing, recipient selection, and send. |
| `cli_calls.py` | Opt-in `calls-history` listing. |
| `cli_common.py` | Small CLI presentation helpers. |
| `cli_proximity.py` | `proximity-lock` status, dry run, enable, and disable. |
| `tui.py` | Textual terminal client. |
| `tui_launcher.py` | Launches the TUI with the package-private Textual bundle when present. |
| `ui/app.py` | GTK4/libadwaita application entry point. |
| `ui/window.py` | Main GTK window. |
| `ui/conversations.py` | GTK conversations page: history, group confirmation, replies. |
| `ui/status.py` | GTK iPhone page: setup, health, preferences, maintenance. |
| `ui/status_presenter.py` | Pure presentation rules for the status page. |
| `ui/client.py` | Asynchronous GTK backend calls and D-Bus invalidations. |
| `ui/setup_runner.py` | GTK-independent worker for blocking setup operations. |
| `ui/util.py` | Small UI helpers. |
| `qt/app.py` | PySide6/Kirigami entry point. |
| `qt/controller.py` | Asynchronous `BridgeController` exposed to QML. |
| `qt/tasks.py` | Qt worker primitive. |
| `qt/activation.py` | Qt adapter for client activation. |
| `qt/qml/Main.qml` | Kirigami window: navigation and composition. |
| `qt/qml/ConversationLogic.qml` | Thread lookup, roster-warning dedup, participant parsing (also used by Quickshell). |
| `qt/qml/PhoneSettingsPage.qml` | Qt setup and preferences page. |
| `qt/qml/PhoneSettingsDialogs.qml` | Window-owned settings/pairing dialogs that outlive the page. |
| `qt/qml/OnboardingSummary.qml` | Renders the onboarding stage message. |
| `qt/qml/ProximityLockSettings.qml` | Away-lock toggle, grace period, and warning; loaded only for daemons that report it. |
| `qt/qml/GroupConfirmationDialog.qml` | Group recipient confirmation before sending. |
| `qt/qml/NewMessageDialog.qml` | New message composition. |
| `qt/qml/ExpandingMessageComposer.qml` | Growing message editor. |
| `qt/qml/MessageBubble.qml` | Message bubble. |
| `qt/qml/RecentCallsPage.qml` | Opt-in recent-calls list, created through a `Loader`. |
| `quickshell_bridge.py` | Persistent stdin/stdout JSON bridge from Quickshell to the session D-Bus API. |

### Quickshell client (`data/quickshell/`)

| File | Responsibility |
| --- | --- |
| `shell.qml` | Root: conversations, group recipients, and roster warnings. |
| `BackendBridge.qml` | Drives `blueferry-quickshell-bridge`; request IDs and latest-only delivery. |
| `ConversationLogic.qml` | Development wrapper importing the Qt file; packaging installs the Qt file directly. |
| `OnboardingState.qml` | QML re-implementation of first-run stage derivation (see known duplication). |
| `SetupController.qml` | Pure setup state; emits requests with monotonic IDs. |
| `SetupTransport.qml` | Creates one `SetupJob` per request and routes tagged output. |
| `SetupJob.qml` | One `pairing-*` helper process with a deadline. |
| `PhoneSettingsPage.qml` | Setup and preferences view. |
| `Theme.qml` | Reads public Omarchy theme files. |
| `ThemePalette.qml` | Pure color/geometry tokens with a system-palette fallback. |
| `Ferry*.qml` | Styled controls (button, check box, combo box, label, text field, composer, section label, info row). |
| `QuickshellMessageBubble.qml`, `QuickshellThreadPreview.qml` | Message bubble and thread preview. |

`data/blueferry-quickshell` is the launcher script, and
`data/io.weirdware.BlueFerry.xml` is the canonical D-Bus introspection
contract.

## Process boundaries

- `blueferry-backend` owns MAP, PBAP, ANCS, contact resolution, thread
  identity, history retention, notification policy, and the private D-Bus API.
  It runs unprivileged and has no sudo command path.
- GTK, Qt, TUI, Quickshell, and the CLI message commands are replaceable
  clients. They receive opaque thread keys and cannot construct different
  recipients for an existing thread.
- Within the backend, `dbus_service` is only a wire adapter over
  `backend_operations`; `daemon` orchestrates lifecycle, `event_dispatcher`
  owns fan-out, and `profile_supervisor` owns profile transitions. Its worker,
  session, and timer protocols make races testable without BlueZ.
- PBAP transport and parsing (`contacts`) stay separate from persistence
  (`contact_repository`). The opt-in call history follows the same split
  (`call_history`, `call_history_repository`, `call_history_sync`) and exists
  in the daemon only when `BLUEFERRY_CALL_HISTORY_ENABLED` is set.

## D-Bus API and compatibility

- `Messages1` carries commands and unicast snapshots; `Events1` carries
  content-free live coordination. `Presence1` holds desktop-presence
  controls that are not messaging (the opt-in away lock); their state is
  reported through `Messages1.GetStatus`, and the compatibility check runs
  through `Messages1` on the same owner. Identifiers live in `protocol.py`.
- `data/io.weirdware.BlueFerry.xml` is canonical, installed under
  `dbus-1/interfaces`, and checked against the service's dbus-python
  decorators.
- `GetStatus.api_version` is the messaging compatibility generation
  (currently 2, roster-bound replies), independent of the package release.
  Additive compatible changes keep the generation; incompatible changes need a
  new interface suffix, never a silent contract change.
- Shared clients check the generation on the same owner-bound proxy they
  invoke. A verified daemon is remembered by its unique bus name, which a
  replacement never reuses, so it is checked once rather than per call. Missing, malformed, or
  different generations prompt the user to update before any read or
  mutation. Lifecycle recovery alone reads status unchecked so it can restart
  an outdated daemon first.
- Payloads cross the bus as JSON and are decoded immediately by `client_wire`
  into `models`, which retain unknown fields for forward compatibility.
- **What never crosses the bus:** `HistoryChanged` carries only a daemon-local
  revision, `StatusChanged` and `CallHistoryChanged` have no arguments, and
  `OpenMessageRequested` carries only a bounded opaque MAP handle. Call
  records are read only through the authenticated `ListCallHistory`. Message records, sender
  identities, ANCS fields, contacts, and connectivity details are never
  broadcast. Expected errors use stable, length-bounded names under
  `io.weirdware.BlueFerry.Error`; unexpected exceptions and OBEX details stay
  in the daemon log.
- Every public method checks the caller's UID against the backend's and
  applies per-connection plus daemon-wide quotas, with separate limits for
  sends, contact sync, storage unlock, destructive operations, reads, and
  status. Reconnecting does not reset daemon-wide limits. Snapshot sizes,
  query text, and replies are bounded.
- `ListThreads` fits an 8 MiB budget by keeping the newest contiguous tail of
  each thread, capped at 500 messages per thread, and includes retained
  starred threads outside the normal 2,000-event window. `messages_truncated`
  signals truncation. These limits never delete events or change reply
  recipients.

### Group replies and roster tokens

- The backend owns reply routing. Reply addresses always come from the
  backend projection.
- Group replies use `SendToThreadChecked`, which binds approval to the exact
  roster and roster-warning token the client displayed. The backend rejects
  stale tokens, even if another client already confirmed the new roster.
  Legacy `SendToThread` still works for direct threads only.
- Tokens are computed by `recipients.group_confirmation_token` over the exact
  displayed addresses, formatting included. Only transport destinations are
  normalized.
- `models.Thread` derives unread counts, approval tokens, and roster-warning
  keys from existing backend fields, so they need no new API generation. Qt
  and the Quickshell bridge serialize them for QML so JavaScript does not
  recompute them.
- Group sender labels are display-only and never affect identity or routing.

## Client architecture

### Shared layer

- All Python transports decode through `client_wire`. Scheduling
  (synchronous, GLib, Qt, Textual) is toolkit-specific.
- `conversation_state` holds snapshot and reply logic for GTK, Qt, and TUI.
  Snapshots distinguish an untouched source, a failed read, and an empty
  history. A failed status read marks the backend unavailable but keeps the
  last history. Storage state is unknown until the first successful read.
- The reply planner validates the roster token held by the confirmation
  dialog. GTK and TUI keep the dialog's token across refreshes.
- `onboarding` derives the first-run stage from configuration, read-only
  controller capabilities (never vendor names), the selected bond, and backend
  status. Raw hardware capability is kept separate from the saved target's
  ANCS policy, so a compatibility pairing can reach `ready-without-ancs`.
- No presentation main loop blocks on the backend, BlueZ, or systemd.
- User-visible Python strings go through gettext, QML strings through `qsTr`.
  `po/POTFILES.in` is the translation inventory.

### Per-toolkit notes

- **GTK** (GTK4/libadwaita): snapshot reads are serialized on a private bus
  connection owned by a worker, and sends use dbus-python reply handlers.
  Asynchronous results feed `ConversationState`. Onboarding uses
  `onboarding.OnboardingState`.
- **Qt/Kirigami**: `BridgeController` serializes work in a QThreadPool,
  coalesces `Events1` invalidations, and keeps `ConversationState` across
  refreshes and sends. The pending group dialog is bound to its original
  conversation and draft. `Main.qml` handles only navigation. Settings
  dialogs belong to the window so pairing prompts survive closing the page.
- **TUI** (Textual): shares the snapshot loader with Qt. Blocking calls run in
  workers with thread-owned D-Bus connections. Signals trigger coalesced
  refreshes, with a bounded periodic fallback. Every native backend package
  includes it. DEB/RPM bundle Textual in a private vendor directory that only
  packaged entry points activate.
- **Quickshell**: QML has no generic D-Bus client, so one persistent
  `quickshell_bridge` process handles all messaging, contact, status, and
  preference requests over stdin. Private data never goes in process argv.
  Setup uses the separate short-lived `pairing-*` helpers, because setup
  happens before the daemon is available. Quickshell sends the displayed
  roster token so the backend can reject stale routes. Superseded or
  cancelled setup results are ignored, and each helper has a deadline.

### Known duplication

The Quickshell client does not share all of its semantics with the Python
layer:

- `data/quickshell/OnboardingState.qml` re-implements stage derivation and
  pending iPhone setup tasks rather than using `onboarding.py`.
- `qt/qml/ConversationLogic.qml`, used by both Qt and Quickshell,
  re-implements thread lookup, roster-warning deduplication, and participant
  parsing (written to match `recipients.participant_lines`). Quickshell uses no
  `conversation_state` logic. Python/QML behavior tests cover parts of these
  contracts, such as serialized tokens and participant parsing.

A change to these rules has to be made in both places.

## Pairing and setup

- **Layers:** `pair_setup` is the low-level boundary. `setup_client` exposes
  typed operations to GTK, Qt, and the CLI wizard, and Quickshell reaches the
  same operations through the `pairing-*` JSON helpers. Setup writes the
  selected MAC and ANCS policy to user configuration and asks systemd for
  privileged steps.
- **Two independent axes** (`pairing_policy`) replace a table of device
  quirks:
  - *Delivery mode*: `full` when ANCS is supported and selected. Otherwise
    compatibility mode persists `BLUEFERRY_ANCS_ENABLED=false`, MAP/PBAP is the
    success boundary, and the daemon never enables LE/ANCS for that target.
  - *Authentication*: normally `iphone-initiated-connect`, where a
    device-scoped agent is registered and `Device1.Connect()` lets the iPhone
    start authentication. `explicit-device-pair` (`Device1.Pair()`) is an
    override for controllers that cancel Connect-first.
- **Confirmation is mandatory.** `SetupClient.complete()` requires a
  confirmation callback. GTK and Qt run the agent in a helper isolated from
  GLib and D-Bus, and its stdout is reserved for the JSON interaction
  protocol. The Quickshell pairing and forget helpers require an explicit
  interactive flag and a positive approval over the line protocol. No caller
  can fall through to a desktop Bluetooth agent.
- The temporary agent stays registered until the daemon's first MAP/PBAP
  attempt. Success means MAP/PBAP works end to end, not only that a bond
  exists on the Linux side.
- ANCS solicitation is separate from ANCS connection policy. It is broadcast
  after the Classic bond settles, in compatibility mode too, because older iOS
  uses it to expose the MAP/PBAP permission toggles.
- Pairing reports record the resolved policy, controller capability, ordered
  timeline, build ID, and full source SHA, with addresses and home paths
  scrubbed.

## Bluetooth supervision and recovery

- **Worker model:** one dedicated worker with its own session-bus connection
  serializes every blocking MAP/PBAP operation. Its backlog is bounded, and
  results return through GLib before touching daemon state. Slow D-Bus
  methods use deferred replies, so status and BlueZ events stay dispatchable
  during transfers. Wallet operations use a separate worker (120 s
  cancellable deadline) and never occupy the Bluetooth worker.
- **Profiles:** connectivity moves through explicit states (initializing,
  connecting, ready, degraded, reconnecting, authorization-required,
  map-connection-refused, stopping). Retries happen every 5 s until the first
  success and every 15 s afterwards. MAP and PBAP recover independently, and
  full readiness requires both. `Connection refused (111)` is kept so clients
  can explain that another computer may hold the phone's single MAP
  connection.
- **Bearers:** `bearer_supervisor` keeps BR/EDR and LE connected, independent
  of desktop applets. In full mode a missing LE bearer holds back MAP/PBAP
  reconnects. Compatibility mode leaves LE disabled.
- **Solicitation:** `solicitation_supervisor` keeps the advertisement on air
  until MAP/PBAP and an ANCS Control Point round trip are both healthy. It
  re-registers the advertisement if BlueZ releases it or changes owner.
- **Class-of-Device:** this setting is volatile. The daemon checks it at
  startup, on BlueZ owner change, and periodically. On drift it runs one fixed
  systemd helper that can only set the validated adapter to A/V Hands-Free, as
  permitted by a narrow Polkit rule. No general `btmgmt` or systemd access is
  exposed.
- **Recovery:** `bluetooth_recovery` performs a last-resort power cycle of the
  selected controller only. It runs after a sustained ANCS outage on a setup
  that previously worked, tries an LE-only reset first, and allows one cycle
  per outage. Only sustained, verified ANCS health rearms it, and cycles are
  at least an hour apart. Limits and pending restoration survive restarts.
  It skips adapters in use by other devices, discovery, or transfers. User
  details are in `README.md`.
- **Proximity lock** (opt-in) reads only the bearer supervisor's cached
  BR/EDR and LE state. It arms after an observed connection, locks once after
  a continuous grace period, and is inhibited by suspend, adapter power-off,
  discovery, recovery, and a forgotten phone. It never unlocks: Bluetooth
  presence is not an authentication factor. Locking is asynchronous
  (`org.freedesktop.ScreenSaver.Lock`, then logind `Session.Lock`). It is
  configured through `Presence1.SetProximityLock`, and `GetStatus` reports
  only its state keys through the existing argument-free `StatusChanged`.
- **Read receipts** go through `read_receipts`, which delays MAP write-back so
  ANCS can still fetch group metadata. Local reads take effect immediately.

## Storage and privacy

- **Remote input is untrusted.** Names, vCards, notification text,
  recipients, and timestamps from the iPhone are parsed and validated before
  use. Parsers, transfers, contacts, replies, and retained payloads all have
  explicit limits (`limits.py`).
- **Identity:** normalized phone or email addresses are identities, and
  contact names are display metadata only.
- **Group routing:** named-group ANCS notifications start as read-only
  threads. Only the backend retains a user-supplied route, and every observed
  sender must remain in it. A sender outside the route raises a roster-change
  warning and disables replies. Routes are local and never modify iPhone
  groups. They are preferences in `settings.json`, not history events, so
  retention and the bounded conversation window never discard a route that
  is still in use. Two named groups with the same name share a key because ANCS has no
  conversation ID. Versioned keys preserve spelling (NFC, trimmed), and editing
  a roster does not change the key. Legacy name-folded keys remain aliases
  only when history shows a single spelling, and reading history never
  rewrites the database.
- **Encryption at rest:** history, the contact cache, and the opt-in call
  history live in `0700` directories as `0600` SQLite files. Sensitive records, including event kind,
  timestamp, and content, are encrypted with AES-256-GCM under one random key
  held by the Secret Service through libsecret. Clients never handle the key,
  and keyring lookup attributes are non-sensitive. Starred keys, saved group
  routes, and confirmed rosters in `settings.json` are encrypted per
  collection.
- **Fail closed:** passive startup only loads a key from an already unlocked
  collection and never prompts. If the key is missing or wrong, or plaintext
  is unframed, storage becomes unavailable without deleting records while live
  delivery continues. Only an explicit client action may create or unlock the
  key. History writes are transactional, and a monotonic revision invalidates
  the projection.
- **ANCS content:** the app is identified before any content is requested.
  The default policy never fetches content from other apps. The opt-in `all`
  policy applies exact bundle-ID allow/block rules first and delivers content
  only to an ephemeral popup sink, never retained or broadcast. Apple Messages
  keeps only the fields needed for group correlation.
- **Logs** exclude message bodies, notification text, and recipient
  identities at every level. Markup and terminal output are escaped at their
  display boundaries.
- **Configuration** files are owner-only, size-bounded, opened without
  following final symlinks, and restricted to named settings. systemd never
  sources them as a process environment.
- **Trust boundary:** the Unix login session. Same-user processes can call
  the API and, while the wallet is unlocked, may be able to request its
  secrets. Encryption protects data at rest, not against a compromised
  session. Sandboxed clients need explicit access to the
  `io.weirdware.BlueFerry` name.

## Lifecycle and packaging

- The backend is a `Type=dbus` systemd user service that can be D-Bus
  activated. It is autostarted through a package-owned
  `default.target.wants` link and skipped by `ConditionPathExists` when no
  pairing configuration exists.
- D-Bus is published before hardware work, and `GetStatus` reports
  `initializing` and degraded state explicitly.
- Packages install release and source-SHA markers. The daemon publishes them
  as `_build_id` and exits with status 75 when the markers change, so systemd
  restarts it. Clients compare `_build_id` and fall back to a serialized
  restart. Package scripts never address other users' service managers.
- All external commands go through `commands.run_command`. Lifecycle tests
  replace marker reads and command runners, so they can never restart a real
  service.

## Tests

Parser, routing, retention, and lifecycle tests run without a live D-Bus, and
the harness rejects the real session and system buses. Presentation tests
inject fake clients. The Arch package check also runs one API round trip on a
private bus, validates desktop/AppStream metadata, lints the Kirigami and
Quickshell QML, and builds every split package. Architecture should depend
only on findings that stay reproducible in `PROTOCOL.md`.

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
