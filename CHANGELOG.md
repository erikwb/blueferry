# Changelog

## [0.6.0] — 2026-08-08

Shipping and multi-client release.

- Made the standalone Quickshell client Omarchy Quattro-theme-aware. It tracks
  the active palette, popup surface roles, type and spacing scales, user shell
  overrides, and Hyprland rounding while retaining a Qt-palette fallback.
  Cyclic custom color aliases fall back safely, and failed helper responses
  clear stale connection capabilities instead of leaving old health data on
  screen.

- Finalized the pre-release `Events1` contract as invalidation-only: history
  signals expose only a local revision and status signals carry no fields.
  Sensitive records now cross D-Bus only in unicast replies.
  Every method verifies the caller's bus credentials and Unix UID; separate
  per-connection and daemon-wide quotas protect sends, sync, reads, unlocks,
  and destructive settings. Snapshot sizes and JSON replies are bounded, and
  unexpected or lower-level errors no longer leak implementation details.

- Added default-on, keyring-backed encryption for retained messages and
  contacts. The backend uses GNOME Keyring or KWallet through Secret Service,
  keeps only a random AES-256-GCM key there, rejects unauthenticated plaintext
  records, and fails closed without blocking live delivery. GTK, Kirigami, and
  Quickshell expose encrypted and no-retention policies plus explicit unlock.

- Renamed the substantially incompatible `iphonebridge` pre-release project
  to BlueFerry. Runtime identifiers, packages, state, commands, and clients
  make a clean break while the README preserves the original project's
  provenance and copyright.
- Replaced 2,220 lines of obsolete interactive protocol and deployment spikes
  with a maintained `PROTOCOL.md` that separates captured behavior from
  inference and records later corrections to the early ANCS conclusions.
- Hardened the backend's remote-input and routing boundaries: OBEX sends now
  require a terminal transfer result, MAP/PBAP payloads and outgoing bodies
  have byte limits, ANCS queues/caches are bounded, state permissions fail
  closed, and group replies use backend-owned routing through `SendToThread`.
- Hardened owner configuration against symlinks, oversized input, permissive
  modes, unsupported environment keys, and malformed Bluetooth identifiers.
  The systemd unit no longer sources user configuration as process environment,
  and installed helper commands use fixed absolute paths.
- Bounded OBEX work, desktop read-state subscriptions, contact cardinality,
  remote metadata fields, and total retained payload bytes. Encrypted history
  now conceals event kind and time as well as content, and authenticated mode
  rejects rather than migrates unframed plaintext records.
- Added conservative systemd sandboxing around the unprivileged backend and
  escaped dynamic Kirigami/Quickshell text at the presentation boundary.
- Removed the unused shell notification-feed adapter and made Apple Messages
  ANCS correlation rows backend-internal rather than public event-query data.
- Added a cached conversation projection and indexed group correlation, split
  Bluetooth capability/device, ANCS sequencing, transfer monitoring, Qt task,
  and GTK presentation/worker responsibilities into testable modules, and
  added checkout-pinned pytest and Ruff validation.
- Centralized MAP/PBAP open, close, loss, retry, and resume behavior in a
  generation-safe `ProfileSupervisor`, preventing stale asynchronous profile
  completions from publishing false readiness after a reconnect begins.
- Replaced the JSONL event log with a transactional, indexed SQLite history
  store. This unreleased project starts with a fresh database; retention no
  longer rewrites a whole log, and conversation caches use a database revision.
- Removed unreleased compatibility shims and unused contact metadata. Raw MAP
  sender identity is now consistently named `sender_address`, since it can be
  either a telephone number or an Apple-ID email address.
- Expanded mypy from selected policy modules to the complete backend and added
  protocol interfaces at the profile lifecycle boundary. Added deterministic,
  hardware-inert Hypothesis coverage for bMessage/vCard, ANCS fragmentation,
  recipient injection, and conservative group routing.

- Replaced the generated-looking README with an evidence-based guide to the
  actual user experience, limitations, packaging, privacy model, Bluetooth
  profiles, process architecture, lifecycle, and test isolation.
- Standardized project metadata on GPL-2.0-only and identified Erik Bourget
  <erik@ebourget.net> and Gabe Shatunovsky <gabriel@shatunovsky.com> as the
  copyright holders.
- Added session D-Bus activation and package-release lifecycle tracking. The
  graphical clients now start the user backend on demand, and an active daemon
  restarts itself after an Arch package upgrade without terminal intervention.
- Replaced the passwordless sudoers rule with an explicit Polkit prompt during
  pairing; the long-running backend no longer has a privileged command path.
- Published D-Bus before slow Bluetooth startup, made bus connections lazy,
  centralized protocol identifiers and service metadata, documented component
  boundaries, and added lifecycle/privilege regression tests.
- Moved all blocking MAP/PBAP work to one serialized worker with its own D-Bus
  connection. Session creation, sends, contact pulls, live queries, and
  incoming message downloads no longer stall the daemon's GLib/D-Bus loop.
- Migrated the D-Bus, desktop, AppStream, icon, and Flatpak reverse-DNS
  namespace to `io.weirdware.BlueFerry`.
- Made tests fail closed against the user's real D-Bus and paired hardware.
  The public API round trip uses an activation-free private bus; automated
  tests cannot start BlueZ, OBEX, notifications, the installed daemon, or send
  and retrieve messages from a phone.
- Removed the HFP/oFono calling subsystem, call D-Bus API, call notifications,
  CLI commands, client pages, optional dependency, and supporting tests.
- Added complete graphical Bluetooth setup to GTK, Qt/KDE, and Quickshell:
  BlueZ activation through Polkit, discovery, secure pair/repair, trust,
  daemon configuration, ANCS bearer connection, and local bond removal.
- Qt now identifies a stale or unavailable backend instead of displaying
  ambiguous “Unknown” values, reports contact-sync results, and can restart
  the user daemon from its Status page.
- Reworked both desktop clients around their native platform conventions. GTK
  now uses adaptive libadwaita navigation and asynchronous D-Bus snapshots;
  KDE now uses Qt Quick/Kirigami, event-driven QDBus updates, and an
  asynchronous controller. Both include native dialogs, application actions,
  translatable strings, keyboard access, and accessible control names.
- Added capability-driven first-run onboarding across GTK, Kirigami,
  Quickshell, and CLI. Setup selects a compatible controller from BR/EDR, LE
  advertising, and secure-pairing features rather than vendor, detects bonds
  that require a two-sided re-pair, stores the selected adapter, and waits for
  live MAP/PBAP status before reporting verified success. ANCS remains an
  explicitly reported optional capability.
- Fixed first-pair races: peer-initiated pairing is allowed to finish instead
  of starting a competing transaction, and early BR/EDR service resolution is
  no longer misclassified as proof that ANCS requires a destructive re-pair.
- A preserved MAC without a corresponding BlueZ bond now returns clients to
  first-run setup. The backend stays idle instead of advertising ANCS and
  repeatedly attempting OBEX against an unpaired phone.
- Reduced the controller-dependent ANCS advertisement wait from 30 to 15
  seconds. Clients now describe this phase as pairing preparation instead of
  claiming the confirmation code is already visible.
- Thread projection now resolves historical bare addresses against the current
  contacts cache. Provisional and reply-ready copies of an identical group
  roster collapse onto the single verified route; conflicting verified routes
  remain separate rather than risking a misdirected reply.
- The Qt client now exits cleanly on terminal SIGINT/SIGTERM. Its permanent
  navigation tabs use Kirigami's native persistent-page pattern, the refresh
  shortcut declares all standard bindings, and optional setup fields are
  converted to explicit booleans, eliminating the reported startup warnings.
- Qt setup completion notices are now emitted only when onboarding actually
  changes state. ANCS notifications replayed by an iPhone after reconnect are
  suppressed across daemon restarts by device, ID, and content while genuine
  modifications to an existing notification continue to be delivered.
- Incoming and outgoing halves of a verified group now retain one participant
  roster and project as one replyable thread. Existing sent records that lack
  the roster are reconciled by their generated group name and a matching known
  sender address, with conflicting recipient routes still kept separate.
- Added a persistent daemon-owned desktop notification policy with All,
  Messages Only, and None choices; Messages Only is the default. GTK,
  Qt/Kirigami, and Quickshell expose the same live setting while notification
  history and group-correlation metadata remain available independently.
- Removed routine Kirigami passive notifications from the Qt client. Success
  is reflected directly in conversation, status, and settings state, while
  actionable backend and setup failures remain visible inline on their page
  instead of covering the permanent navigation bar.
- Simplified GTK, Qt/Kirigami, and Quickshell to two user-goal destinations:
  Messages is the normal launch view, while one adaptive iPhone page owns
  first-run pairing, live connection health, popup policy, contact sync,
  storage, and troubleshooting. The internal ANCS log is no longer presented
  as a separate application destination.
- Split the Arch build into backend, GTK, Qt/KDE, and Quickshell/Omarchy
  packages, using only official repository dependencies.
- Moved event history, thread identity, group correlation, and reply routing
  behind the backend D-Bus API. Threads are address-keyed, same-name contacts
  stay separate, and clients cannot silently change a group's recipients.
- Added first-send group participant confirmation and reject ambiguous
  repeated-body MAP/ANCS correlations.
- Added PBAP email-address caching for Apple-ID-only contacts.
- Added ANCS Data Source fragmentation, serialized requests, response bounds,
  timeouts, and preservation of category/silent metadata.
- Replaced ordinary obexd restarts with targeted stale-session cleanup,
  runtime loss monitoring, suspend/resume refresh, and reconnect backoff.
- Added an explicit connectivity state machine with bounded exponential retry,
  authorization-required reporting, and status visibility in GTK and Qt.
- Separated application operations from the D-Bus transport, event fan-out
  from daemon orchestration, terminal pairing presentation from BlueZ setup,
  and message commands from the CLI entrypoint.
- Added a shared application error hierarchy and argv-only external-command
  runner, removing scattered subprocess launch and timeout handling.
- Made login autostart package-owned through a vendor `default.target.wants`
  link gated by the user's pairing configuration. Uninstall removes both the
  unit and enablement without addressing arbitrary logged-in user sessions.
- Added a shipped, regression-tested D-Bus introspection contract and shared
  forward-compatible status, thread, event, and setup models. GTK and Qt now
  call one direct setup API instead of launching hidden CLI subprocesses;
  Quickshell retains the JSON commands as a thin adapter over that same API.
- Added 30-day/10,000-event retention defaults, history deletion, notification
  content privacy controls, duplicate Messages-popup suppression, markup
  escaping, and terminal-control neutralization.
- Updated first-run guidance, service units, desktop metadata, and the Arch
  build/check pipeline. Removed remaining source-tree deployment scripts and
  the old hard-coded-user sudoers example.

## [0.1.0] — 2026-05-19

First tagged release. Working BlueFerry daemon on Pop!_OS 24.04
against iPhone 16 Pro Max running iOS 26.5.

### Confirmed working
- Real-time SMS + iMessage notifications via MAP MNS push
- Outgoing SMS + iMessage send via MAP `PushMessage` — iOS auto-routes
  as iMessage when the recipient is iMessage-capable
- 1000+ contacts pulled via PBAP, cached in SQLite, name-resolved for
  incoming messages
- systemd user service for autostart, graceful degradation when iPhone
  toggles are off, automatic retry every 60s
- DBus service `io.weirdware.BlueFerry.Messages1` with Send,
  ListRecent, IsHealthy methods
- CLI: `run`, `doctor`, `pair-setup`, `sms-list`, `sms-send`,
  `contacts-sync`, `version`

### Documented constraints (won't change)
- No iMessage attachments / reactions / read receipts / typing
  indicators (MAP doesn't expose them)
- No group iMessage / MMS / RCS (MAP is 1:1 only)
- No outgoing call audio routing (HFP HF role — Phase 2c)

## [0.5.0] — 2026-08-08

Security pass over the message path. One feature removed, several
hardening fixes. No new dependencies.

### Fixed — ANCS pairing advertisement and lifecycle

- The ANCS solicitation advertisement now carries the discoverable flag and
  inert private manufacturer/service data used by the proven ancs4linux
  pairing flow.  The previous solicitation-only packet did not establish an
  LE bond with the tested iPhone/MediaTek MT7922 combination.
- BLE advertisement registration waits long enough for hardware offload and
  verifies that a timed-out call increased the active-instance count.  It no
  longer treats any unrelated active advertisement as proof of success.
- Partial daemon startup is now covered by cleanup.  Earlier startup crashes
  leaked one hardware advertisement each and exhausted all 20 controller
  slots; normal users should never need to reset Bluetooth to recover them.
- `pair-setup` now asks for the iPhone's System Notifications toggle and warns
  when the resulting bond does not expose the ANCS service UUID.
- The Arch package enables BlueZ's experimental D-Bus API with a vendor
  `bluetooth.service` drop-in. `ancs-enable` now selects
  `Device1.PreferredBearer=le` and calls `Bearer.LE1.Connect` directly; it no
  longer edits BlueZ's private bond database or needs a privileged helper.
- ANCS is confirmed working on MediaTek MT7922 while MAP/PBAP remain connected
  over BR/EDR. The earlier Intel-only assumption was too strong.

### Added — group iMessage threads and replies

- Incoming MAP messages are correlated with their matching Apple Messages
  ANCS notification. On the tested iPhone, ANCS supplies the otherwise-missing
  group participant names in its subtitle.
- The GTK app groups those messages by participant set and enables replies
  once each participant name resolves to one unambiguous phone number or
  Apple-ID email.
- Group replies use one MAP bMessage with repeated recipient vCards. This was
  confirmed against iOS 26.5: a matching participant set sends into the
  existing iMessage group, not separate one-to-one conversations.

### Changed — ANCS desktop popup lifetime

- Mirrored app/system and SMS/iMessage notifications now request an
  eight-second desktop lifetime instead of remaining in KDE's notification
  center indefinitely. ANCS events are still recorded and shown in
  BlueFerry's Notifications page. Explicitly dismissing an SMS/iMessage
  popup before expiry still marks it read; timeout alone never does.
- ANCS popups also carry the freedesktop `transient` hint so Plasma bypasses
  its persistent notification history rather than retaining them after their
  visible timeout.

### Removed — verification-code clipboard copy (breaking)

- `ClipboardSink`, `blueferry.clipboard`, and the `wl-clipboard` /
  `xclip` dependency are gone. The 0.4.2 claim that detection "requires a
  verification keyword *and* a 4-8 digit number, so an ordinary text
  doesn't trigger" did not hold: `"meeting at 1430, security badge
  needed"` copied `1430`, and `"Amazon order 112-3456789 confirmed, login
  to track"` copied `3456789`. Because the keywords are common words,
  anyone who knew your number could silently overwrite your clipboard with
  digits of their choosing, unprompted. Not worth the paste convenience.

### Fixed — privacy of local state

- The state dir is now created `0700` with `0600` files, and an existing
  `0755` dir from an earlier version is repaired on startup. Previously
  `events.jsonl` (every message body) and `contacts.sqlite` (your whole
  phonebook) were world-readable at `0644`.
- Message bodies and ANCS notification content no longer go to the journal
  at INFO. Counts and senders still do; content moved to DEBUG (`-v`).

### Fixed — sender-name spoofing

- `contact_name` is now populated *only* from the local contacts cache.
  The inbound bMessage's own vCard `FN:` was previously used as a fallback
  display name, so a sender could label themselves "Mom" in the
  notification, the UI thread list, and the JSONL log.
- `display_sender` no longer shows a raw `TEL:` value that isn't
  phone-shaped, closing the same hole one field over (`TEL:Mom`).

### Fixed — bMessage injection via recipient

- `Send()` and `send_message()` validate the recipient against strict phone
  and email-address patterns. Apple-ID email recipients are encoded using
  the MAP vCard `EMAIL:` property while retaining iOS's iMessage-compatible
  `TYPE:SMS_GSM` path. A recipient containing CRLF previously injected
  arbitrary bMessage structure — including a second recipient vCard, which
  would silently deliver the message to an extra number.
- Escaped the ampersand in the UI's `SMS & iMessage (MAP)` subtitle so
  libadwaita's markup parser no longer emits a warning.
- Rebuilding the conversation sidebar no longer fires its row-selection
  callback and draws a newly sent or received message twice. The daemon and
  iPhone already sent and recorded the message only once; this was visual.
- PBAP contact sync now uses PhonebookAccess1's `MaxCount` filter and lowercase
  `vcard30`; it previously mixed in MAP's `MaxListCount` spelling, causing
  BlueZ 5.87 to create an immediately failed, zero-byte transfer. Fast
  completed transfers and empty-phonebook errors are also handled cleanly.
- `contacts-sync` now refreshes through the running daemon's existing PBAP
  session. It previously restarted `obex.service` behind the daemon's back,
  leaving its MAP session stale and stopping message reception until restart.
  When the daemon is absent, the CLI opens PBAP only instead of MAP + PBAP.

### Fixed — descriptor leak (remote DoS)

- `tempfile.mkstemp(...)[1]` discarded the open descriptor, leaking one per
  message received and per message sent; ~1000 texts walked the daemon into
  `EMFILE`. Both call sites now close the fd.
- A `Message1.Get` transfer that never reports `complete`/`error` now times
  out after 120s instead of holding its temp file and signal subscription
  for the daemon's lifetime.

## [0.4.2] — 2026-05-20

### Verification codes auto-copied to the clipboard

- When an incoming text carries a one-time / 2FA code, the daemon detects it
  and copies it straight to the system clipboard, with a short "Code copied"
  notification — paste with Ctrl+V, no reaching for the phone. New
  `ClipboardSink`.
- Detection requires a verification keyword *and* a 4-8 digit number, so an
  ordinary text that just happens to contain a number doesn't trigger.
- Uses `wl-copy` (Wayland) or `xclip` / `xsel` (X11) — install `wl-clipboard`
  for the Wayland path.

## [0.4.1] — 2026-05-20

### Sent messages in conversation history

- The daemon now records every message sent through BlueFerry (the UI
  compose box or `blueferry sms-send`) to `events.jsonl` as a `sms_sent`
  event, and broadcasts a `MessageSent` signal on `Events1`. The UI threads
  these in, so a conversation shows both sides — incoming **and** the replies
  you sent from the desktop.
- No desktop notification fires for your own sent messages.
- Note: messages composed on the iPhone itself remain invisible — iOS does
  not expose sent content over MAP (the `sent` folder is empty, and no MNS
  push fires for outgoing). This was verified empirically; see the commit.

## [0.4.0] — 2026-05-20

### Phase 2d — GTK4 / libadwaita desktop app

- **`blueferry-ui`** — a standalone GTK4 / libadwaita app, separate from
  the daemon, talking to it over D-Bus. Four surfaces:
  - **Messages** — SMS/iMessage threads with history and a compose box
  - **Notifications** — a live feed of per-app ANCS notifications
  - **Calls** — a dialer plus answer / hang-up controls for active calls
  - **Setup** — daemon health, data counts, and the iPhone-toggle checklist
- New `src/blueferry/ui/` package; `DaemonClient` subscribes to the
  daemon's live signals and reads history from `events.jsonl`.
- Daemon broadcasts a live event feed on a new D-Bus interface
  `io.weirdware.BlueFerry.Events1` (`MessageReceived`, `MessageSeen`,
  `AncsNotification` signals) for the UI to consume.
- `data/` — `.desktop` entry, AppStream metainfo, and an app icon.

## [0.3.0] — 2026-05-20

### Phase 2c — HFP Hands-Free calls

- **Take and place iPhone calls on the laptop.** New `src/blueferry/hfp/`
  subsystem: call control runs through oFono (`org.ofono`, system bus), and
  call audio (SCO) rides PipeWire's oFono HFP backend.
- Incoming calls raise a desktop notification with **Answer / Decline**
  buttons; caller ID is resolved against the contacts cache.
- New CLI: `call <number|contact>`, `hangup`, `calls`, and `hfp-enable`
  (writes the WirePlumber config that routes HFP through oFono).
- New D-Bus interface `io.weirdware.BlueFerry.Calls1` — `Dial`,
  `AnswerCall`, `HangupCall`, `HangupAll`, `ListCalls`, and a
  `CallStateChanged` signal.
- Daemon: sinks now initialise independently of the MAP/PBAP sessions, so
  ANCS and call notifications reach the desktop even in degraded mode.
- Empirically confirmed against iPhone 16 Pro Max / iOS 26.5 — including
  **3/3 reliable outgoing dials**, which overturns the old "HFP HF can't
  reliably ATD on iPhone" assumption. The retained observation is summarized
  in the historical HFP section of `PROTOCOL.md`.
- `pyproject.toml`: `testpaths = ["tests"]` so a bare `pytest` no longer
  recurses (and hangs on) the whole repo tree.

## [Unreleased]

### Project-defining discoveries (2026-05-19, post-launch)

- **Incoming iMessage IS exposed via MAP on iOS 26.5 / iPhone 16 Pro Max**, labeled as `Type: sms-gsm` indistinguishably from SMS. This contradicts every prior Bluetooth-on-Linux writeup. Verified: sender (Contact B, confirmed iMessage thread, both on iPhone) sent "test-blueferry-XYZ123" → daemon received and rendered the body within ~2s.

- **Outgoing iMessage via MAP `PushMessage` ALSO works.** The experiment recorded in `PROTOCOL.md` constructed a minimal bMessage (originator + BENV-wrapped recipient VCARD), called `MessageAccess1.PushMessage(sourcefile, "telecom/msg/outbox", {})` — transfer completed, and the iPhone's outgoing bubble appeared **blue** (iMessage) in the recipient thread.

Together: **BlueFerry is potentially the first free open-source Linux iMessage bridge that does not require a Mac relay**. README, BACKLOG, and the protocol findings were updated accordingly.

### Phase 1 — MVP daemon (2026-05-19)
- Working BlueFerry daemon: BLE-advert / CoD startup dance, long-lived MAP + PBAP sessions, MAP MNS push subscription, bMessage parsing, SQLite contacts cache, libnotify + JSONL sinks.
- Typer CLI: `run`, `doctor`, `sms-list`, `contacts-sync`, `version`.
- systemd user service for auto-start.
- sudoers.d entry (`install-cod-sudoers.sh`) for passwordless `btmgmt class 4 8` so CoD survives reboots.
- End-to-end verified: SMS from a known contact arrives as a GNOME desktop notification within ~20 ms of the iPhone push.

### Phase 0 — Empirical spike (2026-05-19)
- Confirmed against iPhone 16 Pro Max / iOS 26.5: MAP read ✓, MAP MNS push ✓, PBAP (1957 contacts) ✓, HFP HF role partial (needs WirePlumber config work), ANCS deferred (needs BLE-only pairing flow incompatible with the BR/EDR pair MAP/PBAP need).
- Documented the original non-obvious findings now maintained in `PROTOCOL.md`: the permission-toggle pairing requirements, long-lived OBEX sessions, message text in `Subject`, PBAP `Select` vs `SetFolder`, and the initially incomplete BR/EDR-versus-LE conclusion later corrected by BlueZ's dual-bearer API.
