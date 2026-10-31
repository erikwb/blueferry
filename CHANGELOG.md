# Changelog

## Unreleased

### Added

- `plugin_card_watch` (dbus-python) hears `CardChanged` from enabled card
  plugins only, one match per plugin bus name checked against its owner, for
  the clients without the GTK or Qt D-Bus stack.
- Quickshell panel: **From Plugins** under the now-playing strip shows the
  enabled card plugins' items (for example AirPods or the calendar) with
  their buttons while the conversations are shown. The bridge serves
  `plugin_cards` and `plugin_action` on the shared `PluginSurfacesModel` and
  forwards `CardChanged` (through `plugin_card_watch`) as
  `plugin-cards-changed`. Buttons that send files are left out: the panel
  has no file picker.
- `blueferry media volume up|down` sends one iPhone volume step and confirms
  it with the last reported level; `blueferry media volume` prints the level.
  A shared presenter (`media_volume_view`) prepares the volume buttons for the
  graphical and terminal clients.
- Pin entries of the opt-in iPhone notification list so they stay listed,
  also across restarts, until unpinned (`Messages1.SetNotificationPinned`,
  `blueferry notifications pin`/`unpin`). Pins are saved under the local data
  policy, at most 20; entries removed on the iPhone are marked as such.
- Mute the desktop popups of single iPhone apps: `Messages1.ListNotificationApps`
  lists the apps seen since the daemon started (memory only) plus the muted
  ones, `SetNotificationAppMuted` saves a choice under the storage policy, and
  `Events1.NotificationAppsChanged` invalidates the list. CLI:
  `blueferry notifications apps|mute|unmute`. The environment allowlist and
  blocklist stay a hard filter in front of it.
- Quickshell: volume down/up buttons with the iPhone's level in the
  now-playing strip, **Pin** / **Unpin** in the recent notification list
  (removed entries and entries of muted apps greyed out, the pin limit
  shown), and **Muted apps** under iPhone settings › Notifications. The
  bridge gains `set_notification_pinned`, `notification_apps` and
  `toggle_notification_app` and forwards `NotificationAppsChanged`.
- Contact cards: the contact cache now keeps each number with its iPhone
  label, labelled email addresses, postal addresses, company and job title,
  inside the encrypted contact records only. `Messages1.ListContactCards`
  pages through the cards (optionally filtered, no photo bytes) and
  `GetContact` returns one card with its photo; `GetStatus` reports
  `contact_card_count` and `contact_card_epoch` (changes when card ids may
  no longer resolve). Card queries ignore case, accents and Unicode forms.
  CLI: `blueferry contacts list|show`.
- Global search: `Messages1.Search` looks through conversations, messages,
  contact cards and call history in one call and groups the hits with the
  ids to open them. It searches only the decrypted data the daemon already
  holds in memory; there is no search index on disk. `ListCallHistory`
  entries gain an opaque `id`, and `GetStatus` reports `search_result_limit`.
  CLI: `blueferry search`.
- Client models: `contacts_model` and `search_model` hold the paging,
  typing delay, stale-id and busy handling of the contact list and the
  global search for the KDE, GTK and terminal clients; recent-call rows
  from `phone_overview.call_groups` carry the ids of the folded calls.
- Quickshell: a search button (and Ctrl+F) opens a popup that lists and
  searches the contact cards and, from two characters on, searches
  conversations, messages, contacts and calls. Hits open their
  conversation or contact card; a card's numbers and addresses and call
  hits offer Call (confirmed first) and Message. Read only while the popup
  is open.

## [0.9.1](https://github.com/erikwb/blueferry/releases/tag/v0.9.1) - 2026-10-30

### Fixed

- Continue message and contact setup when BlueZ rejects the ANCS advertisement,
  while retaining notification configuration and retries. Keep the backend
  available for pairing when the saved phone's bond is missing at startup
  ([#302](https://github.com/erikwb/blueferry/pull/302)).
- Resume Bluetooth recovery when BlueZ's `Powered` and terminal `PowerState`
  properties disagree, instead of leaving all phone services paused
  ([#303](https://github.com/erikwb/blueferry/pull/303)).
- Reject excessively nested plugin indexes before decoding, independently of
  Python's stack limit. Preserve a valid cached catalogue after a bad download
  ([#304](https://github.com/erikwb/blueferry/pull/304)).

### Diagnostics

- Detect missing local incoming-message service registration in `blueferry
  doctor`, and document the BlueZ workaround and its tradeoffs
  ([#301](https://github.com/erikwb/blueferry/pull/301)).

### Known issues

- Encrypted history authentication can fail after reboot on the Fedora/KDE
  setup reported in [#298](https://github.com/erikwb/blueferry/issues/298).
  The cause remains under investigation; this release does not change history
  encryption or claim to resolve that report.
- BlueZ advertisement rejection and the spontaneous bond loss reported in
  [#300](https://github.com/erikwb/blueferry/issues/300) remain unresolved at
  their source. This release fixes BlueFerry's handling of advertisement failure
  and a bond already missing at startup.
- Some explicit-pairing setups still time out after bonding
  ([#270](https://github.com/erikwb/blueferry/issues/270)). The new MNS diagnostic
  helps identify [#280](https://github.com/erikwb/blueferry/issues/280), but does
  not fix the underlying BlueZ service-registration problem.

## [0.9.0](https://github.com/erikwb/blueferry/releases/tag/v0.9.0) - 2026-10-27

### Added

- Optional iPhone calls through oFono, phone battery and signal information,
  recent calls, and missed-call notifications.
- Optional iPhone media control and an MPRIS player. Explicitly route media to
  the PC or phone, with a saved choice that is reapplied after reconnects.
- Optional contact photos, one-time-code copying, notification action buttons,
  notification click rules, and locking the desktop when the phone moves away.
- Optional Bluetooth tethering through the iPhone Personal Hotspot, with
  NetworkManager integration, separate autoconnect controls, and respect for
  connections managed by other applications.
- A plugin API and CLI management, with support for plugin notifications,
  cards, file sending, and photo providers.
- A shared desktop tray that opens the active client, confirmed dialing from
  `tel:` links, and additional media and call-history controls across clients.
- OpenRC support, English and German documentation, and WSL2 setup guidance.
- Report a stable opaque phone profile ID in `GetStatus` for clients that need
  to scope local per-phone metadata.

### Fixed

- Restore normal desktop Bluetooth audio profiles during pairing and remove
  the previous phone-audio policy that could prevent pairing and notification
  authorization. Audio routing is now an explicit user choice.
- Wait for outstanding disconnect operations before reconnecting. Improve
  Classic and LE recovery, restore ANCS and media subscriptions after drops,
  and detect stale LE bonds and controllers with LE disabled.
- Offer ANCS on supported older BlueZ versions without per-bearer interfaces.
  Recover MAP when the phone drops its incoming-message connection, and delay
  phone read acknowledgements so notification group metadata can arrive.
- Preserve international contact numbers and alphanumeric senders, handle
  oversized and duplicate contact photos, and exclude the owner's PBAP card
  from peer contact matching.
- Load Quickshell conversations correctly on Qt 6.12, restore keyboard thread
  navigation, and avoid empty media placeholders while the phone is idle.
- Harden asynchronous storage, conversation, call, media, and plugin handling
  against stale replies, shutdown, and reconnects.
- Verify caller Unix users in plugin services, reject excessively nested
  plugin catalogs without crashing, and explain plugin permissions before
  installation or updates.
- Fix package smoke tests and path-dependent plugin tests. Optional Qt avatar
  and QML tests now skip when the separate QtTest binding is unavailable.
  Declare Fedora's required wheel-builder and GTK introspection dependencies.
  Include Git and Python virtual-environment support needed to install plugins.

### Upgrading

- Upgrade the backend and installed clients together. The new calls, photos,
  code-copying, proximity-lock, media, tethering, and plugin features are opt-in.
- Arch and Fedora package upgrades restart Bluetooth if it is running; active
  Bluetooth connections will briefly disconnect. Debian packages do not change
  or restart the system Bluetooth service.

### Known issues

- Some explicit-pairing setups still time out after a successful bond when the
  phone drops its idle Classic connection ([#270](https://github.com/erikwb/blueferry/issues/270)).
- Incoming MAP notifications remain unavailable on the setup reported in
  [#280](https://github.com/erikwb/blueferry/issues/280). Investigation is ongoing;
  this release does not claim to fix that report.

## [0.8.1](https://github.com/erikwb/blueferry/releases/tag/v0.8.1) - 2026-09-14

### Added

- Make web links clickable in GTK, Qt, and Quickshell messages while keeping
  message text literal and restricting links to HTTP and HTTPS.

### Changed

- Enable explicit pairing by default for Bluetooth adapters with USB IDs
  `0bda:8771`, `0bda:8922`, and `13d3:3586`, with a per-adapter override.
- Identify adapters that lack the required Classic Bluetooth, LE, or advertising
  support before pairing. Prefer a compatible adapter when choosing automatically
  and keep an existing saved phone when its replacement cannot be paired.

### Fixed

- Recover encrypted storage automatically when the desktop keyring becomes
  available after startup, without restarting the daemon. Validate stored data
  before resuming writes and restore contact and conversation caches.
- Open notification conversations in the active or most recently used desktop
  client, preserve existing GTK windows across upgrades, and launch clients
  outside the daemon's service sandbox.
- Honor GTK pairing options across confirmation dialogs and background work.
  Handle pairing helper pipe failures without hanging and retain diagnostic
  output and report paths in errors.
- Show live message and contact connection status during Quickshell pairing,
  and avoid showing iPhone permission instructions for incompatible adapters.
- Let the installed Qt package smoke test run without a desktop session bus.

## [0.8.0](https://github.com/erikwb/blueferry/releases/tag/v0.8.0) - 2026-09-07

### Added

- Star conversations and keep them at the top of the list, show unread threads,
  and open the matching conversation from notifications and client launches.
- Filter message notifications to known contacts and configure which iPhone
  applications can send ANCS notifications to the desktop.
- Provide separate Fedora 45 RPMs for its Python version.

### Changed

- Group direct conversations across addresses that belong unambiguously to the
  same synced contact, preserving the sender and reply destination.
- Match Quickshell's fonts, colors, and controls to Omarchy themes while keeping
  outgoing message bubbles blue. Improve text selection, long-message layout,
  and single-line composer alignment.
- Use the saved group members directly for Quickshell replies without a separate
  confirmation checkbox.
- Share conversation refresh, routing, and setup logic across clients, and
  extract Qt and Quickshell settings components for easier maintenance.

### Fixed

- Improve Bluetooth pairing and recovery across Classic and LE reconnects,
  adapter resets, and stale ANCS notification subscriptions.
- Apply the WirePlumber phone-audio policy before pairing and avoid unnecessary
  OBEX session cleanup after shutdown or transport loss.
- Preserve conversation scroll, read state, and saved preferences across
  refreshes, contact merges, and storage changes.
- Separate named groups by spelling, migrate legacy routes safely, and prevent
  stale group rosters or delayed responses from enabling incorrect replies.
- Check backend API compatibility after recovering stale packaged daemons and
  provide guidance to install the most recent version when incompatible.
- Build Arch packages with the system Python and give Quickshell tests a private
  runtime directory. Support Ubuntu 24.04's setuptools metadata format and ship
  missing Qt startup dependencies in Debian and Fedora packages.
- Check installed Qt startup with the default KDE style across the supported
  native package matrix.

## [0.7.7](https://github.com/erikwb/blueferry/releases/tag/v0.7.7) - 2026-08-17

### Fixed

- Keep the Debian backend user service compatible with systemd 255 while
  retaining stronger sandboxing in other native packages.

## [0.7.6](https://github.com/erikwb/blueferry/releases/tag/v0.7.6) - 2026-08-17

### Added

- Delete individual local conversations from every client, including their
  retained notification evidence and group metadata.
- Record compatibility-mode and explicit-pairing choices in pairing reports.

### Fixed

- Restore the initial ANCS subscription bootstrap after pairing.
- Avoid stale OBEX session cleanup after the transport is lost.
- Start the backend user service on systemd 255-based distributions.

### Changed

- License BlueFerry under GPL-2.0-or-later.
- Document installation from native GitHub release packages.

## [0.7.5](https://github.com/erikwb/blueferry/releases/tag/v0.7.5) - 2026-08-16

### Fixed

- Recover ANCS when a previously authorized iPhone stops responding after a
  Bluetooth reconnect.

## [0.7.4](https://github.com/erikwb/blueferry/releases/tag/v0.7.4) - 2026-08-16

### Changed

- Make message composers grow, wrap, and scroll in every client.
- Keep GTK message bubbles within the conversation viewport.
- Repair Qt utility pages and expose locked or unavailable conversation
  storage with an unlock retry.

### Fixed

- Recover ANCS after Bluetooth LE reconnects without stale session races.
- Verify contact synchronization after successful empty phonebook pulls.

## [0.7.2](https://github.com/erikwb/blueferry/releases/tag/v0.7.2) - 2026-08-16

### Fixed

- Prevent the privileged Bluetooth setup helper from hanging under systemd.

## [0.7.1](https://github.com/erikwb/blueferry/releases/tag/v0.7.1) - 2026-08-15

### Changed

- Make Bluetooth controller capability checks advisory.
- Use packaged systemd units for privileged Bluetooth setup.
- Polish Qt startup, connection health, and group messaging.

### Fixed

- Correct D-Bus advertisement typing and tagged release publication.

## [0.7.0](https://github.com/erikwb/blueferry/releases/tag/v0.7.0) - 2026-08-14

### Added

- Add native packages for supported Arch, Debian, and Fedora systems.
- Add compatibility and explicit iPhone pairing modes.
- Ship the terminal client as part of every backend package.

### Fixed

- Restart stale backend builds and recover ANCS after BlueZ restarts.

## 0.6.3 - 2026-08-13

### Added

- Let users select a Bluetooth controller and keep pairing, scanning, and
  forgetting the device on that radio.
- Add the initial native package scaffold.

## 0.6.2 - 2026-08-10

### Changed

- Improve interactive Bluetooth pairing and diagnostics.
- Handle devices forgotten outside BlueFerry more clearly.

## 0.6.1 - 2026-08-10

### Added

- Add the Textual terminal client, notification deep links, and KDE tray
  integration.

### Fixed

- Restart the daemon automatically after package upgrades.

## 0.6.0 - 2026-08-09

### Added

- Add backend-owned safe thread routing, group replies, Apple ID contacts,
  reconnect supervision, privacy controls, and split desktop clients.
