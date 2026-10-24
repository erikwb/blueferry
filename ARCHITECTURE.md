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
| `dbus_service.py` | Session D-Bus adapter (`Messages1`/`Events1`/`Presence1`/`CallHistory1`) that maps operations to wire types; claims the bus name. |
| `dbus_security.py` | Caller UID validation and per-connection/daemon-wide rate limits. |
| `dbus_call.py` | Asynchronous dbus-python calls with an explicit input signature (`call_async`), plus typed `ay`/`a{sv}` builders. |
| `protocol.py` | Stable D-Bus identifiers and the API-generation compatibility check. |
| `event_dispatcher.py` | Builds messages from MAP/ANCS events and fans them out to persistence, desktop, and D-Bus sinks. |
| `events.py` | Normalized MAP event dataclasses (`SmsEvent`) and address/timestamp normalization. |
| `threads.py` | Backend-owned conversation projection, thread keys, and reply-routing metadata. |
| `grouping.py` | Correlates MAP iMessages with ANCS Messages notification metadata to recover group membership. |
| `named_groups.py` | Named-group identity keys and saved reply routes. |
| `confirmed_groups.py` | Persistent confirmed group rosters in the owner-only settings document. |
| `group_routes.py` | Saved named-group reply rosters in the settings document, outside history retention. |
| `starred_threads.py` | Persistent starred-conversation keys in the settings document. |
| `notification_policy.py` | Persistent desktop notification preferences, including per-app click rules. |
| `notification_open_map.py` | Strict validation and exact-match resolution of notification click rules (bundle ID to http(s) URL or desktop-entry ID). |
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
| `service_manager.py` | Detects the init system; maps backend start/restart/stop onto `systemctl --user`, `rc-service --user`, or D-Bus activation plus bus-verified SIGTERM. |
| `config.py` | Environment-backed configuration (`local.env`) and private runtime paths. |
| `private_files.py` | Race-resistant owner-only reads and atomic writes for small files. |
| `build_info.py` | Package release + source-SHA build identity. |
| `wireplumber_policy.py` | Manages one WirePlumber fragment that keeps iPhone audio on the phone (keeps the hands-free roles when calls are enabled). |
| `proximity_lock.py` | Opt-in lock-only desktop lock after the iPhone's bearers stay down for a grace period; lock dispatch via ScreenSaver, then logind. |
| `media.py` | Opt-in now-playing projection, media command policy, coalesced change listeners, and the persisted opt-in (`MediaControlSettings`). |
| `phone_audio_route.py` | Content-free iPhone A2DP route (`pc`/`phone`/`unavailable`) from BlueZ `MediaTransport1`/`Device1` over GDBus, switched through `ConnectProfile`/`DisconnectProfile`; also where the sound plays (`phone_audio_playing`) from `MediaTransport1.State` and the AMS playback state, debounced. |
| `gio_dbus.py` | `GioBus`: a two-method seam (typed asynchronous `call`, `subscribe`) over a `Gio.DBusConnection`, with dbus-python-compatible error names; used by `phone_audio_route.py`. |
| `phone_audio_view.py` | Toolkit-free wording of the PC/iPhone sound switch for every client. |
| `mpris.py` | Optional MPRIS2 player (`org.mpris.MediaPlayer2.blueferry_iphone`) over `media.py`, exported on its own private session-bus connection so the MPRIS name never addresses the BlueFerry object. |

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
| `call_history_sync.py` | Schedules call-history pulls (ANCS-triggered, missed-calls-only fallback poll, one OBEX worker job per listing) and reports newly seen missed calls. |
| `call_history_settings.py` | The saved call-history opt-in (`settings.json`, seeded from `local.env`). |
| `vcard.py` | Linear, resource-bounded vCard block extraction; the photo-aware variant splits PHOTO out under its own budget. |
| `contact_photos.py` | Opt-in contact photos: bounded base64 PHOTO decoding and volatile owner-only copies for notification icons. |
| `contact_photo_settings.py` | The saved contact-photo opt-in (`settings.json`, seeded by `BLUEFERRY_CONTACT_PHOTOS`); the daemon passes it as a callable to the contact cache, sync and storage preparation. |
| `ancs/client.py` | ANCS GATT client: subscribes to characteristics, requests attributes, emits `AncsEvent`s, and sends opt-in `PerformNotificationAction` writes. |
| `ancs/parsers.py` | Pure ANCS wire-format parsers and command builders. |
| `ancs/constants.py` | ANCS spec constants. |
| `ancs/events.py` | `AncsEvent`, the normalized per-app notification. |
| `ancs/sequencer.py` | Bounded, duplicate-aware backlog of serialized ANCS requests. |
| `gatt.py` | BlueZ GATT call options for the Apple services (plain Write Request, never a reliable write). |
| `ams/client.py` | Opt-in Apple Media Service GATT client on the ANCS LE link; asynchronous, serialized, bounded. |
| `ams/parsers.py` | Pure AMS wire-format parsers and command/registration builders. |
| `ams/state.py` | `NowPlaying` projection of Player, Queue, and Track attributes. |
| `ams/constants.py` | AMS UUIDs, identifiers, and public command names. |
| `bearer_supervisor.py` | Connects BR/EDR first, then keeps LE connected alongside it; reports a suspected stale LE bond from persistent bursts of very short LE links; damps Classic reconnects to an absent phone. |
| `solicitation_supervisor.py` | Keeps the ANCS solicitation advertisement on air until ANCS is proven healthy. |
| `adapter_class_supervisor.py` | Detects Class-of-Device drift and repairs it through the constrained system helper. |
| `bluetooth_recovery.py` | Last-resort, rate-limited adapter power cycle for persistent ANCS outages. |
| `profile_reset.py` | Detects an HFP power-up (oFono Timedout) that bluetoothd keeps stuck and asks the bearer supervisor for one rate-limited Disconnect/Connect of the device. |
| `bluez_health.py` | Detects a bluetoothd that no longer answers D-Bus (unanswered `Peer.Ping`s, `/proc` state) and pauses BlueFerry's own Bluetooth work. |
| `bluez_setup.py` | Adapter preparation: Class-of-Device and the ANCS solicitation advertisement. |
| `bluetooth_capabilities.py` | Controller capability probing and packaged BlueZ activation. |
| `bluetooth_devices.py` | Typed BlueZ device projection for setup and clients. |
| `calls/settings.py` | Saved phone-calls opt-in (`settings.json`, seeded by `BLUEFERRY_CALLS_ENABLED`). |
| `calls/model.py` | Optional HFP calls: pure oFono property parsing, modem selection, dial/DTMF/call-id validation. |
| `calls/ofono.py` | Asynchronous oFono system-bus transport (hand-built calls with NO_AUTO_START, no synchronous owner lookup). |
| `calls/controller.py` | Optional HFP calls: oFono modem discovery, Powered→Online bring-up, call tracking and control, backoff; watches the phone's battery/signal interfaces while online. |
| `phone_battery.py` | The phone's battery over LE (BlueZ `Battery1` or GATT Battery Level), asynchronous, no HFP; saved low-battery warning opt-in. |
| `calls/phone_status.py` | Optional phone status: pure parsing of oFono's Handsfree/NetworkRegistration properties and the once-per-cycle low-battery decision. |

### Sinks

| Module | Responsibility |
| --- | --- |
| `sinks/__init__.py` | Sink protocol: `handle(event)` plus optional `handle_ancs`, `handle_call`, and `handle_phone_battery_low` (optional HFP calls, desktop UI only). |
| `sinks/sqlite.py` | Persists events to the private history store. |
| `sinks/otp_clipboard.py` | Opt-in: copies one-time codes from new, unread MAP messages from non-contacts to the clipboard (five-minute freshness and current unread metadata, at most three eligibility checks per minute, five-second group grace) and adds a line to the message popup or shows a transient confirmation. |
| `otp.py` | Pure one-time code detection: a number must be bound to a code noun or an entry instruction in an authentication sentence, with false-positive filters. |
| `otp_context.py` | Bounded, transient MAP/ANCS context for live group correlation and read-state changes; works without history storage. |
| `otp_clipboard.py` | Chooses wl-copy/xclip/xsel and owns one foreground clipboard helper; first copy waits for the worker capability probe; reaps through a GLib child watch. Cleanup only stops its own source, preserving selections owned by other programs. |
| `sinks/libnotify.py` | Desktop notifications via `org.freedesktop.Notifications`, including open and dismiss actions, optional incoming-call Answer/Decline, opt-in iPhone action buttons, and the optional phone low-battery warning. |
| `otp_settings.py` | The saved one-time code opt-in (`otp_autocopy` in `settings.json`, seeded by `BLUEFERRY_OTP_AUTOCOPY`). |

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
| `notification_open.py` | Opens a click rule's URL or desktop entry through Gio in a helper process, via a transient systemd user unit when available. |
| `time_display.py` | Human-readable local timestamps for all clients. |
| `i18n.py` | gettext helpers for Python presentation layers. |

### Clients and entry points

| Module | Responsibility |
| --- | --- |
| `cli.py`, `__main__.py` | Typer CLI (`run`, `doctor`, sync, setup, and hidden `pairing-*` JSON helpers). |
| `cli_messages.py` | CLI message listing, recipient selection, and send. |
| `cli_contacts.py` | `contact-photos enable`/`disable`/`status` and `contacts-photo` export of one cached contact photo. |
| `cli_call_history.py` | Opt-in `call-history` listing, enable, and disable. |
| `cli_common.py` | Small CLI presentation helpers. |
| `cli_proximity.py` | `proximity-lock` status, dry run, enable, and disable. |
| `cli_reconnect.py` | `reconnect`: manual Classic reconnect that waits for the outcome. |
| `reconnect_view.py` | Toolkit-neutral texts for the manual reconnect and rate limits (Qt banner and iPhone page, TUI key `b`, CLI). |
| `cli_notifications.py` | `notifications open-map` rule editing. |
| `cli_media.py` | `blueferry media` now-playing status, commands, and `enable`/`disable`. |
| `plugin_api/` | Plugin contract: manifest parser and discovery, settings schema (`config.py`, with groups, a pre-check and `ShowIf`), `TestConfig`/`ConfigLogin` replies (`config_flow.py`), surfaces (`card`, `share`, content-free `notify`) with checked `open_uri`, the standard log file, a validating `PluginClient`, `PluginService`/`PhotosService` bases (bounded worker pool, per-caller budget) and a test harness (`FakeHost`). Imports nothing else from `blueferry`. A symlink to `plugin-api/src/blueferry/plugin_api`, which `plugin-api/pyproject.toml` also builds as the distribution `blueferry-plugin-api` for plugins in their own repositories. |
| `plugin_manager.py` | Installs plugins from https Git URLs: pinned ref, clone, confirmation, own venv, manifest and D-Bus service file; update, remove, enable/disable under a `flock`; validates `installs.json` on every read. |
| `plugin_stopper.py` | After an update, removal or disabling, SIGTERMs the owner of the plugin's bus name if it runs as the user from the plugin's old venv (pidfd, never SIGKILL, never BlueFerry itself); blocking, worker thread or CLI only. |
| `plugin_index.py` | Plugin store: fetches (https on every redirect hop), validates and caches curated index files. |
| `plugin_prefs.py` | Reads which plugins are disabled without the plugin manager. |
| `plugin_logs.py` | Finds a plugin's standard log file and reads its tail without following symlinks. |
| `cli_plugins.py` | `blueferry plugins …` and `blueferry plugins ALIAS …` (exec of the plugin's own CLI; refused for a disabled plugin). |
| `desktop_open.py` | Opens a checked URI with the desktop's default handler for the CLIs. |
| `cli_audio.py` | `blueferry audio [status\|pc\|phone]`. |
| `cli_notification_actions.py` | `notification-actions` status, enable, and disable for the opt-in iPhone action buttons. |
| `cli_calls.py` | Optional `blueferry calls` commands over `Calls1` and `blueferry phone-status` (battery, signal, network from `GetStatus`). |
| `cli_otp.py` | `blueferry otp status`, `enable`, `disable` and `check` for one-time code auto-copy (`otp-status` and `otp-check` stay as aliases). |
| `tui.py` | Textual terminal client. |
| `tui_launcher.py` | Launches the TUI with the package-private Textual bundle when present. |
| `tui_calls.py` | Optional Textual calls panel. |
| `tui_options.py` | Textual "Options" screen (`o`): one checkbox per opt-in the daemon reports. |
| `tui_notification_rules.py` | Textual editor for notification click rules. |
| `tui_phone.py` | Opt-in Textual now-playing line and read-only recent-calls screen. |
| `phone_overview.py` | Toolkit-free now-playing and recent-call rules shared by the GTK, TUI and Quickshell clients (mirrors the Qt QML rules). |
| `ui/app.py` | GTK4/libadwaita application entry point. |
| `ui/window.py` | Main GTK window. |
| `ui/conversations.py` | GTK conversations page: history, group confirmation, replies. |
| `ui/status.py` | GTK iPhone page: setup, health, preferences, maintenance. |
| `ui/status_presenter.py` | Pure presentation rules for the status page (including the optional phone battery/signal suffix). |
| `ui/phone.py` | GTK iPhone dialog: battery/signal/network, opt-in now playing and recent calls; fetches only while open. |
| `ui/phone_presenter.py` | Pure presentation rules for the iPhone dialog. |
| `ui/saved_choice.py` | Keeps a settings switch on the user's choice while its save and the next status settle. |
| `ui/client.py` | Asynchronous GTK backend calls and D-Bus invalidations. |
| `ui/avatars.py` | Bounded GTK photo cache and worker-side thumbnail decoding; late results from earlier contact generations are discarded. |
| `ui/setup_runner.py` | GTK-independent worker for blocking setup operations. |
| `ui/util.py` | Small UI helpers. |
| `qt/app.py` | PySide6/Kirigami entry point. |
| `qt/controller.py` | Asynchronous `BridgeController` exposed to QML. |
| `qt/tray.py`, `qt/tray_presenter.py` | `blueferry-tray`: standalone StatusNotifierItem (unread badge, battery/signal tooltip); asynchronous QtDBus calls with auto-start disabled, so it never starts the daemon; owns `io.weirdware.BlueFerry.Client.Tray`, which `qt/app.py` watches to hide its own icon; pure presenters. |
| `tray_autostart.py` | The tray's opt-in per-user XDG autostart entry. |
| `qt/tasks.py` | Qt worker primitive. |
| `qt/avatars.py` | Image provider that decodes opt-in contact photos with `QImageReader` after header and size checks. |
| `qt/activation.py` | Qt adapter for client activation. |
| `qt/qml/Main.qml` | Kirigami window: navigation and composition. |
| `qt/qml/ConversationLogic.qml` | Thread lookup, roster-warning dedup, participant parsing (also used by Quickshell). |
| `qt/qml/PhoneSettingsPage.qml` | Qt setup and preferences page. |
| `qt/qml/PhoneSettingsDialogs.qml` | Window-owned settings/pairing dialogs that outlive the page. |
| `qt/qml/NotificationOpenMapEditor.qml` | Loaded editor for notification click rules (shown with the "all" policy). |
| `qt/qml/OnboardingSummary.qml` | Renders the onboarding stage message. |
| `qt/qml/ProximityLockSettings.qml` | Away-lock toggle, grace period, and warning; loaded only for daemons that report it. |
| `qt/qml/GroupConfirmationDialog.qml` | Group recipient confirmation before sending. |
| `qt/qml/NewMessageDialog.qml` | New message composition. |
| `qt/qml/CallsDialog.qml` | Optional phone-calls dialog (list, dial with confirmation, answer, hang up). |
| `qt/qml/ContactPhotoSettings.qml` | Contact-photo checkbox; loaded only for daemons that report `contact_photos`. |
| `qt/qml/OtpAutocopySettings.qml` | One-time code auto-copy checkbox; loaded only for daemons that report `otp_autocopy`. |
| `qt/qml/PhoneCallsSettings.qml` | Phone-calls opt-in checkbox; loaded only for daemons that report `calls_enabled`. |
| `qt/qml/PhoneStatusIndicator.qml` | Optional iPhone battery/signal indicator (loaded only when values are known; plain-text tooltip). |
| `qt/qml/ExpandingMessageComposer.qml` | Growing message editor. |
| `qt/qml/MessageBubble.qml` | Message bubble. |
| `qt/qml/ContactAvatar.qml` | Conversation icon, replaced by the contact photo when the option is on. |
| `qt/qml/RecentCallsPage.qml` | Opt-in recent-calls list, created through a `Loader`. |
| `qt/qml/CallHistorySettings.qml` | Call-history opt-in checkboxes in the iPhone settings. |
| `qt/qml/NowPlayingBar.qml` | Opt-in iPhone now-playing bar with transport buttons. |
| `qt/qml/MediaControlSettings.qml` | Media-control opt-in checkbox and state; loaded only for daemons that report it. |
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
| `AvatarCache.qml`, `ContactAvatar.qml` | Bounded asynchronous contact photos in the conversation list and header; the bridge delivers validated bytes as data URLs without writing avatar files. |
| `SavedChoice.qml` | The same for Quickshell checkboxes that save through the daemon. |
| `QuickshellPhoneStatus.qml` | Optional iPhone battery/signal caption in the header (hidden when unknown). |
| `QuickshellNowPlaying.qml` | Opt-in now-playing strip with transport buttons; renders the bridge's array-free `now_playing` view. |

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
  in the daemon only while the user has opted in (`CallHistory1.SetCallHistory`,
  seeded from `BLUEFERRY_CALL_HISTORY_ENABLED`).

## D-Bus calls inside the daemon

Everything in the daemon shares one GLib main loop, so a call that waits for
a reply stalls BlueZ signals, timers, and every client meanwhile.

- Calls from loop code are asynchronous: `reply_handler`/`error_handler` on
  dbus-python proxies. `dbus_call.call_async` does that and always passes the
  input signature. Never pass a bare `{}`/`[]` to a proxy fetched with
  `introspect=False`: dbus-python cannot type it and raises `ValueError`
  before sending.
- Blocking work (`time.sleep`, `subprocess.run`, OBEX transfers, slow SQLite
  opens) runs on the OBEX worker or in a helper process, never on the loop.
- `tools/lint_mainloop.py` enforces this for modules the daemon imports at
  import time (run in CI and by `tests/test_lint_mainloop.py`). Deliberate,
  safe exceptions (worker-thread, CLI and helper-process code) go in its
  `ALLOWLIST` with a reason. Existing calls that do block the loop are
  tracked in `KNOWN_DEBT` and leave it only with their fix; never add to it.
- The autouse `sync_dbus_guard` in `tests/conftest.py` catches blocking
  calls the AST cannot see. Those that predate it are in the lint's
  `RUNTIME_KNOWN_DEBT`.

Suggested order for paying off `KNOWN_DEBT`, one module per change:
`sinks/libnotify.py` (`Notify`, with the popup id recorded in the reply
handler), `bearer_supervisor.py` and `bluez_setup.current_cod`, then
`ancs/client.py` (its subscribe path needs an asynchronous state machine like
AMS), and last `dbus_security.CallerGuard` and `dbus_service`, where caller
credentials become an asynchronous lookup before dispatch. Signal watches
(`add_signal_receiver`) still make one blocking `AddMatch` each in
dbus-python; the guard tolerates that.

## D-Bus API and compatibility

- `Messages1` carries commands and unicast snapshots; `Events1` carries
  content-free live coordination. `Presence1` holds desktop-presence
  controls that are not messaging (the opt-in away lock), and `CallHistory1`
  the opt-in mirror of the iPhone's recent calls. Their state is reported
  through `Messages1.GetStatus`, and the compatibility check runs through
  `Messages1` on the same owner. The opt-in `Media1` interface returns the
  now-playing snapshot and sends validated media commands; its
  `NowPlayingChanged` invalidation on `Events1` has no arguments.
  Identifiers live in `protocol.py`.
- `Calls1` is the optional, default-off HFP call interface. It is always
  exported because it also carries the opt-in itself, `SetCallsEnabled`
  (saved in `settings.json`, `BLUEFERRY_CALLS_ENABLED` only seeds it); while
  calls are off its other methods fail with `CallsDisabled`, `GetStatus`
  reports only `calls_enabled=false`, and a missing oFono or modem yields
  `CallsUnavailable`.
  Its `CallsChanged` invalidation on `Events1` has no arguments; caller
  numbers and names are only returned by the rate-limited `ListCalls`.
  Dialing has its own strict quota. Adding it did not change the API
  generation.
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
  sends, contact sync, storage unlock, destructive operations, reads,
  status, media reads, and media commands. Reconnecting does not reset daemon-wide limits. Snapshot sizes,
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
  It sends a `host` event at startup, and its `status` replies also carry
  `bluetooth_restart_command`: the host's BlueZ restart command (`""` when
  unknown), used in the ANCS repair hint even while the daemon is down.
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
  reconnects. Compatibility mode leaves LE disabled. LE transitions come
  from `Bearer.LE1.Disconnected` and `Bearer.LE1` `Connected` property
  changes as they happen, so ANCS, AMS and the battery client also see a
  drop that is back before the next five-second poll; polling stays as the
  fallback. Classic reconnects back off exponentially (up to 10 min without
  a live LE link). After three failed
  attempts with no sign of the phone (no LE link, no RSSI from a discovery in
  the last two minutes), or when BlueZ reports that the phone closed the
  Classic link itself (`Bearer.BREDR1.Disconnected` with `Reason.Remote`,
  e.g. Bluetooth switched off on the iPhone; immediately when LE is known to
  be down and solicitation can bring the phone back, otherwise after one
  failed attempt), the supervisor stops paging and probes only every 10 min
  until an inbound link, LE or a discovery sighting shows the phone again.
  On 2026-10-06 (kernel 7.2.8) a BR/EDR reconnect after Bluetooth was
  switched off on the iPhone coincided with bluetoothd stuck in D state in
  `l2cap_chan_connect` (via `rfcomm_dlc_open`, kernel hung-task trace), so
  fewer pages matter.
  `Messages1.ReconnectPhone()` (own `reconnect` rate bucket, 6/min) clears
  the backoff and pages once on the next main-loop iteration; it never starts
  a second attempt while one is running. GetStatus reports
  `phone_reconnect_state` (`connected`/`connecting`/`waiting`/`unreachable`),
  `phone_reconnect_paused` and `phone_reconnect_next_in_sec`;
  `reconnect_view.py` turns them into the same texts for every client.
- **Profile reset:** `profile_reset` counts a symptom seen on 2026-10-06:
  oFono's `Modem.Powered=true` failing with `org.ofono.Error.Timedout` twice
  in a row with BR/EDR up (bluetoothd's own hfp plugin held the RFCOMM
  channel; doctor recommends `-P hfp`). It then asks
  `BearerSupervisor.reset_profiles()` for `Device1.Disconnect`, 3 s pause,
  `Device1.Connect`; the supervisor's health tick and the manual reconnect
  stand aside meanwhile. Only with BR/EDR up, never while bluetoothd is
  unresponsive, during a call (oFono reports one) or during the adapter
  recovery; at most one automatic reset per 10 min, doubling up to 60 min
  until a profile works again. The symptom only exists with the opt-in
  calls, so nothing is reset without them. Opt-out at runtime through
  `Messages1.SetAutoProfileReset` (saved as `auto_profile_reset` in
  settings.json, which wins over `BLUEFERRY_AUTO_PROFILE_RESET=false`). While
  a profile stays stuck on a live link, `ReconnectPhone` performs the reset
  and answers `profile-reset`, or `call-active` during a call.
  GetStatus: `profile_reset_auto`, `profile_reset_suggested` (reason token),
  `profile_reset_last` (`hfp_powered_timeout`/`manual`),
  `profile_reset_last_at` (Unix time) and `profile_reset_last_auto`. With calls on,
  `blueferry doctor` warns when oFono runs next to a bluetoothd that loads
  its hfp plugin (`-E` without `-P hfp`, read from `/proc`) and shows the
  reset setting, a stuck profile and the last reset.
- **BlueZ health:** `bluez_health` marks bluetoothd unresponsive after two
  unanswered asynchronous `Peer.Ping`s 15 s apart (pinged every 30 s; a
  NoReply from a bearer read only triggers an early ping). While
  unresponsive the bearer supervisor and the power-cycle recovery stay idle,
  `ReconnectPhone` answers `bluez-unresponsive`/`bluez-kernel`, GetStatus
  carries `bluez_unresponsive` and `bluez_unresponsive_reason` (`kernel` when
  `/proc/<pid>/status` of the `org.bluez` owner shows state D), one WARNING is
  logged and dbus-python's "Introspect error … NoReply" lines are throttled
  to one per 10 min. A successful ping or a new `org.bluez` owner resumes
  normal operation.
- **Media (opt-in):** `ams/client` never dials. It follows the bearer
  supervisor's LE observations and BlueZ owner changes, subscribes after the
  link settles, and resets without `StopNotify` on loss. `media` owns the
  command policy; the optional `mpris` adapter owns its bus name only while a
  player is active. MPRIS broadcasts metadata session-wide by design, which
  is why it is a separate opt-in from `Media1`.
- **Stale LE bond (report only):** the same supervisor watches
  `Bearer.LE1.Disconnected` (polled transitions as a fallback). It sets
  `le_bond_suspect` only when Classic stays connected across the whole burst,
  at least five LE links of at most 5 s drop within any minute with reason
  Timeout, Remote or Authentication, and that rate persists for three
  minutes (nine when polling). Local, Unknown and Suspend drops never count.
  The flag changes no connection behaviour: LE dials, resets and the adapter
  power cycle run as before. An authorized ANCS round trip, a held link, a
  new bond, a new bluetoothd generation, Classic being gone for two
  minutes, or ten minutes without a counted drop clears it; the drop count
  decays after a quiet minute. Detection
  is off without ANCS and on controllers flagged as ANCS-limited. `GetStatus`
  carries the flag, the drop count, and a fixed reason token. `doctor`,
  pairing reports, Qt, GTK, Quickshell and the TUI explain the possible
  remedy.
- **Solicitation:** `solicitation_supervisor` keeps the advertisement on air
  until MAP/PBAP and an ANCS Control Point round trip are both healthy. It
  re-registers the advertisement if BlueZ releases it or changes owner.
- **Class-of-Device:** this setting is volatile. The daemon checks it at
  startup, on BlueZ owner change, and periodically. On drift it runs one fixed
  systemd helper that can only set the validated adapter to A/V Hands-Free, as
  permitted by a narrow Polkit rule. No general `btmgmt` or systemd access is
  exposed. Without systemd, the same helper runs through `sudo -n`; an
  administrator can authorize it with a narrow sudoers rule. BlueFerry never prompts for or stores
  credentials, skips sudo under `no_new_privs`, refuses a helper that anyone
  but root could replace, and backs off failed sudo repairs (from 15 minutes
  after a refusal up to six hours, other failures from one up to five
  minutes; a BlueZ restart resets it). On systemd a failed repair is retried
  every minute. When `btmgmt` reports the adapter busy (status 0x0a, common
  right after bluetoothd restarts), the repair is retried after 3 seconds,
  up to five times in a row, before it counts as a failure.
- **Bluetooth LE switched off:** when the controller supports LE but runs
  without it, setup detects it, stops full-mode pairing early (after two
  re-probes, since bluetoothd switches LE on asynchronously) and explains the
  fix. BlueFerry does not switch LE on itself: under `ControllerMode = bredr`
  bluetoothd creates no GATT database or LE advertising manager for the
  adapter, so `btmgmt le on` would not help until bluetoothd restarts in
  another mode. BlueFerry never edits `/etc/bluetooth/main.conf`; it only
  reads `ControllerMode` with GKeyFile, the parser bluetoothd itself uses.
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

## Optional phone calls

- `calls.controller` only observes and drives oFono on the system bus; BlueZ
  and PipeWire/WirePlumber own the HFP profile and SCO audio. oFono is an
  optional runtime service: `ServiceUnknown` means "unavailable", retried
  every 60 s and immediately on an `org.ofono` owner change. Calls carry
  NO_AUTO_START, so BlueFerry never makes the bus activate oFono. oFono's
  shipped D-Bus policy only admits root and `at_console`; `AccessDenied` is
  also "unavailable" and logged once.
- `Dial` accepts plain numbers only (`+` and digits); `*`/`#` service codes
  are rejected so a caller cannot reconfigure the phone (for example call
  forwarding). Keypad symbols remain available as DTMF on an active call.
  Well-known emergency numbers are refused (they belong on the phone), and
  every client confirms a number before dialing. `Answer`/`HoldAndAnswer`
  have their own quota; hanging up is never blocked by it.
- The modem must end in the configured iPhone's `dev_…` path and be of type
  `hfp`; the configured adapter wins, then Online, then Powered. iOS needs
  `Powered=true`, a confirming `PropertyChanged`, then `Online=true`.
  `Powered` is only requested while the Classic bearer is connected, so an
  absent phone is not paged; bearer status changes poke the controller.
- Discovery and bring-up back off 1/2/4/8/15 s, then poll every 30 s; a
  30 s watchdog retries a modem that never confirms. Replies and signals
  carry a generation, so an oFono restart or modem removal drops stale
  state; control replies still reach their D-Bus caller.
- If a `Powered=true` bring-up times out or is rejected while bluetoothd's
  own experimental HFP plugin is active (`-E` without `-P hfp`, read from
  bluetoothd's argv by process name), the state becomes `bluez_conflict`
  and paging stops until the Classic link returns or oFono makes progress.
- `stop()` (daemon exit or switching calls off) sends `Powered=false`,
  flushed and without awaiting a reply, for the modem BlueFerry powered
  itself; a modem another oFono client powered is left alone.
- While the phone reports any call, Bluetooth recovery treats the link as
  busy and does not power-cycle the adapter.
- Call events go to local desktop sinks only (`handle_call`); they are not
  persisted and nothing about them is broadcast except `CallsChanged`.
- Phone status: from `Powered=true` on (oFono creates these atoms in
  `hfp_pre_sim`, independent of `Online`), the controller watches `Handsfree`
  and `NetworkRegistration` (only when listed in the modem's `Interfaces`)
  and reads them with an asynchronous `GetProperties`. A failed read other
  than a vanished interface (typically `InProgress` while oFono queries
  `AT+CNUM`) is retried once after 30 s. Values are cleared on
  `Powered=false`, interface or modem removal, an oFono owner change, and
  stop.
- The battery does not need HFP: `phone_battery.py` reads it over LE from
  the GATT Battery Level characteristic, with BlueZ's `Battery1` as the
  fallback (async
  `GetManagedObjects`, `ReadValue`, `StartNotify`; `PropertiesChanged`
  afterwards) and restarts with bluetoothd. The daemon merges both sources
  into additive `GetStatus` keys (`null` when unknown; LE wins, only while
  the phone is connected; signal and network only with calls on) and
  publishes them with the argument-free `StatusChanged` only when a shown
  value changed, at most every 10 s. Calls-state changes are coalesced per
  main-loop iteration. The opt-in low-battery warning (saved in
  `settings.json`, `Messages1.SetPhoneBatteryWarning`) goes to sinks through
  `handle_phone_battery_low` and fires once per discharge cycle (and again
  after a daemon restart).

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
- **Contact photos** are opt-in (`contact_photos` in `settings.json`, set
  through `Messages1.SetContactPhotos` and seeded by
  `BLUEFERRY_CONTACT_PHOTOS`; switching applies without a restart). The daemon keeps
  inline JPEG/PNG bytes within size limits. It checks the declared canvas by
  reading the header and never decodes pixels. It stores the raw bytes
  AES-GCM-sealed in a BLOB next to the contact records, in the same
  transaction, and erases them when the option is turned off (on the OBEX
  worker, after any queued pull) or at startup when it is off. The photo
  table is created only when a sync stores a photo, and the off-path check is
  read-only, so a profile that never opted in keeps the upstream schema.
  A trigger created with the table deletes all photos whenever
  `secure_contacts` rows are deleted, so a release without this feature
  removes them with its own contact clear or next sync after a downgrade.
  Photo reads open the database read-only with a short lock timeout. While a
  sync holds the database, or a hot journal from a crash awaits rollback, they
  return `NotReady`, which clients retry. Clients get them through
  `GetContactPhoto(address) -> ay`. It has its own rate-limit bucket (with a
  higher daemon-wide window, so several clients can fill their lists) and
  returns a photo only for an unambiguous address. Clients drop cached photos
  when the content-free `contact_photo_revision` status key changes.
  Presentation processes decode the images with toolkit loaders. Notification
  icons are temporary `image-path` files in the runtime directory; a contact
  refresh retires them (no reuse, still readable for popups already shown,
  bounded to 64 files) and storage changes or daemon stop delete them. The
  pull logs one content-free summary per sync (size buckets, rejection
  reasons) so the caps can be checked against a real phone.
- **ANCS content:** the app is identified before any content is requested.
  The default policy never fetches content from other apps. The opt-in `all`
  policy applies exact bundle-ID allow/block rules first and delivers content
  only to an ephemeral popup sink, never retained or broadcast. Apple Messages
  keeps only the fields needed for group correlation.
- **Logs** exclude message bodies, notification text, one-time codes, and
  recipient identities at every level. Markup and terminal output are escaped
  at their display boundaries.
- **One-time codes** (opt-in `BLUEFERRY_OTP_AUTOCOPY`) go from the daemon
  straight to a clipboard helper's stdin, never to argv, the BlueFerry API,
  logs, or storage. A confirmation popup includes the code only when
  notification content is enabled, and then the notification server sees
  it, as it sees the message popup. The daemon writes the clipboard
  itself: a background Wayland client needs a data-control helper such as
  `wl-copy`, and a GUI client would need the code over the bus.
- **Notification click rules** map an exact bundle ID to an `http(s)` URL or
  a desktop-entry ID. They are fixed user configuration: the popup's content
  is never interpolated into a target, and nothing is passed to a shell. The
  store, the D-Bus method, the daemon's spawn, the helper's command line, and
  the final Gio launch each revalidate the target. The daemon starts a
  helper process (never launching on its GLib loop or inside its sandbox);
  under systemd the helper asks the user manager for a transient
  `app-blueferry-open-*.service` so the app runs outside the backend's
  cgroup and restrictions, then launches through Gio with the notification
  server's activation token. Removing a rule takes effect even for popups
  that are already visible. Shells that run a stored argv instead of sending
  `ActionInvoked` (Omarchy's `omarchy-exec-argv` hint) get only a random
  per-popup click ID; the helper hands it back through `OpenNotificationClick`,
  so the current rule, the per-target throttle, and the one-shot tracker
  apply there too. Such a shell dismisses the popup before the helper's call
  arrives, so the ID stays valid for 10 s after a dismissal (not after an
  expiry or any other close). Neither the target nor the bundle ID is put in
  a hint.
- **ANCS actions** (off by default; saved in `settings.json`,
  `BLUEFERRY_ANCS_ACTIONS` is the initial value): action labels
  are app-defined content, so they are requested only while notification
  content is shown, only for non-Messages notifications that announce an
  action, shown only as popup buttons (markup characters removed), and never
  retained, logged, or broadcast. A phone action runs only after a click on its
  button, once per notification, and only with the content-free token of the
  event that offered it: any later event for the UID, a PreExisting replay, or
  a session reset retires the offer and closes its popup. Actions go through
  the serialized Control Point queue. Clients only toggle the preference
  (`SetAncsNotificationActions`); no D-Bus method performs an action, because
  clients never see ANCS notifications or UIDs.
- **Following removals on the iPhone** (off by default; `mirror_iphone_removals`
  in `settings.json`, `Messages1.SetMirrorNotificationRemovals`): when the
  iPhone reports an ANCS NotificationRemoved, the libnotify sink closes every
  popup it showed for that UID, with or without action buttons. The sink
  keeps a bounded desktop-id to UID map that is dropped whenever the UID
  session ends (subscription torn down or renewed, or a PreExisting replay
  after live events), because iOS may reuse the UIDs; those popups then just
  expire. Only UIDs and desktop ids are kept; nothing is broadcast.
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
- Without systemd, `service_manager` treats the session bus as the service
  manager: start is D-Bus activation, and stop signals the same-user process
  the bus daemon reports as the name owner (SIGTERM, SIGKILL after 180
  seconds), then waits for the name to disappear. The caller's timeout bounds
  the whole request; when it ends first the request fails with SIGTERM still
  in effect, like a timed-out `systemctl stop`. A host booted with systemd
  but without `/usr/bin/systemctl` (NixOS) takes this path too. Only a running or enabled
  OpenRC user service (`packaging/openrc/blueferry`) on the desktop's own bus
  is driven through `rc-service --user`. Neither path has the unit's
  sandboxing.
- D-Bus is published before hardware work, and `GetStatus` reports
  `initializing` and degraded state explicitly.
- Packages install release and source-SHA markers. The daemon publishes them
  as `_build_id` and exits with status 75 when the markers change, so systemd
  (or OpenRC's supervisor) restarts it. Clients compare `_build_id` and fall
  back to a serialized restart. Package scripts never address other users'
  service managers.
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
- Never block the daemon's main loop; see
  [D-Bus calls inside the daemon](#d-bus-calls-inside-the-daemon).
- Prefer narrowly tested helpers over broad exception handling. Best-effort
  cleanup may catch broadly, but command paths must return actionable errors.
