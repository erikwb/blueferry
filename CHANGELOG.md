# Changelog

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
