# BlueFerry — Backlog

Park ideas here so they don't derail Phase 1.

## Phase 1 polish (after MVP works)
- [x] Reconnect-on-suspend-resume logic with bounded retry backoff
- [x] Notification dismissal sync — dismissing a message popup marks it read
  through MAP without treating expiry as a human dismissal.
- [x] First-run pairing wizard (CLI) — guide user through iPhone-side toggles
- [x] `blueferry sms-list` — recent inbox dump
- [x] `blueferry doctor` — check BlueZ, obexd, sessions, toggles
- [ ] Better contact resolution for international numbers (E.164 normalization)

## Phase 2 (revised after iMessage-over-MAP discovery 2026-05-19)

- [x] **MAP send / iMessage send** (`MessageAccess1.PushMessage`) — **CONFIRMED WORKING 2026-05-19; see `PROTOCOL.md`**. iOS routes outgoing to iMessage-capable recipients as iMessage (blue bubble). BlueFerry is now read+send. NEXT: build a proper `blueferry sms-send <number> <body>` CLI command backed by a daemon DBus method (so we don't have to stop/restart the daemon to free the MAP session per send).
- [x] **Graceful toggle-disabled handling** — daemon remains available,
  reports authorization-required, and retries with bounded exponential backoff.
- [x] **First-run pair-setup wizard** — graphical and CLI flows use Polkit and
  guide users through the required iPhone-side toggles.
- [x] **Notification dismissal sync** — explicit libnotify dismissal marks the
  MAP message read on the iPhone.
- [x] **`blueferry sms-list` from MAP, not just local history** — pulls the recent
  inbox through the daemon and falls back to local history.
- [x] **ANCS** for per-app notifications (Slack/WhatsApp/etc.) — **DONE
  2026-08-08.** BlueZ's experimental `PreferredBearer` / `Bearer.LE1` API
  connects LE alongside the BR/EDR MAP/PBAP link. Confirmed on MediaTek
  MT7922; the client subscribes when the ANCS GATT characteristics appear.
- [x] **GTK4 / libadwaita app** — **DONE 2026-05-20.** Standalone `blueferry-ui`
  (separate process, talks to the daemon over D-Bus): conversations and an
  integrated iPhone setup/status page. Daemon gained an
  invalidation-only `Events1` D-Bus interface for the UI to subscribe to.

## Phase 3 / nice-to-have
- [x] Keyring-backed authenticated encryption for message and contact records
- [ ] Multi-device support (currently hard-coded to one iPhone MAC)
- [~] Flatpak packaging — draft manifest for the UI in `packaging/flatpak/`
  (UI-only; daemon stays native). Needs a build pass; see its README for the
  one open issue (port `ui/client.py` to GDBus to drop the dbus-python module).
- [ ] iOS version regression test matrix
- [x] D-Bus service `io.weirdware.BlueFerry` with a versioned event feed

## Won't do
- Per-app reply (ANCS is read-only, no protocol path)
- Named-group, MMS, and RCS handling remains uncharacterized. Unnamed iMessage
  groups now work by correlating MAP with ANCS participant metadata and sending
  one multi-recipient bMessage.
- Phone-call integration. The HFP/oFono feature was removed from the product.
- Read receipts, typing indicators, message reactions, full attachments
