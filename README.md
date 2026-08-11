# BlueFerry

![BlueFerry messaging client](screenshot.png)

iMessage Bluetooth bridge for Linux

<p align="center">
  <img src="https://upload.wikimedia.org/wikipedia/commons/thumb/9/90/Ferry_Blue_star_1_Rhodes.jpg/1920px-Ferry_Blue_star_1_Rhodes.jpg" width="320" alt="Blue Star ferry at Rhodes">
</p>

BlueFerry brings messages from a paired iPhone to a Linux desktop. It can
receive and send SMS and iMessage, use the phone's contacts, and optionally
mirror other iPhone notifications. It talks directly to the phone over
Bluetooth; there is no Mac relay, Apple login, cloud service, or subscription.

This is experimental software. Development has mostly used an iPhone 16 Pro
Max running iOS 26.5, and Apple may change the behavior BlueFerry relies on.
Don't make it your only way to receive an important message yet.

## How it works

BlueFerry is not a reimplementation of Apple's private Messages protocol. It
uses the Bluetooth accessory services exposed by the iPhone. On the tested
phone, the Message Access Profile carries both SMS and iMessage; iOS still
decides how an outgoing message is delivered.

There are native clients for GNOME, KDE, and Quickshell. They share one local
backend, so pairing, contacts, preferences, and message history are the same
whichever client you open.

New messages appear as they arrive and can be answered from the conversation.
You can write to a phone number, an Apple-ID email address registered with
iMessage, or a synced contact. BlueFerry only knows about messages it has seen;
it is not an iCloud history browser and pairing will not download your complete
Messages archive.

Message popups are actionable: clicking one presents every currently running
BlueFerry client and selects the matching conversation. The KDE client also
provides a system-tray item; closing its window leaves it available there until
you choose **Quit**.

Group chats work when BlueFerry can safely reconstruct the participants from
the message and its matching iPhone notification. Bluetooth MAP does not send
a group identifier or roster, so ambiguous groups remain read-only rather than
risk replying to the wrong people.

Attachments, reactions, typing indicators, FaceTime, calls, and a complete
sent-message history are not supported. MMS, RCS, and named groups have not
been tested well enough to promise anything.

## Install on Arch Linux

Clone this repository and run:

```bash
./build.sh -si
```

The build uses dependencies from the official Arch repositories—nothing from
the AUR or PyPI—and produces four ordinary pacman packages:
`blueferry-backend`, `blueferry-gtk`, `blueferry-qt`, and
`blueferry-quickshell`. Running `./build.sh` without `-i` builds them without
installing them. The finished package archives are written to
`packaging/arch/`.

See [packaging/arch/README.md](packaging/arch/README.md) if you want to build
individual packages or understand exactly what pacman owns.

## Pair an iPhone

Open the client for your desktop:

```bash
blueferry-gtk         # GNOME
blueferry-qt          # KDE
blueferry-quickshell  # Quickshell
```

On first launch it opens the iPhone setup page.

1. Let BlueFerry check the Bluetooth controller. If it offers to activate
   Bluetooth support, approve the Polkit prompt. Bluetooth will restart once,
   briefly disconnecting other devices.
2. Open your Bluetooth settings, click **Scan**, pick your phone, then hit
   **Pair**. When this computer shows up in **Other Devices**, tap it and
   approve the prompts. The confirmation code can take around 15 seconds to
   appear on some controllers.
3. Confirm the same code on both devices when prompted.
4. On the iPhone, open **Settings → Bluetooth → ⓘ** beside the computer and
   enable **Show Message Notifications** and **Sync Contacts**. You may need
   to back out and tap **ⓘ** again before these toggles appear.
5. Wait for Messages and Contacts to show as connected. For the default
   encrypted storage, approve the desktop wallet prompt that opens automatically.

BlueFerry keeps a valid existing bond and will not repeatedly re-pair the
phone. If you need a truly clean repair, forget the device on both sides and
start again with the iPhone's Bluetooth page open.

For a machine without a graphical client, the same setup is available from:

```bash
blueferry pair-setup
```

Once pairing is complete, opening a client starts the backend automatically.
It also reconnects after normal Bluetooth interruptions and restarts itself
after package upgrades; routine use should not require `systemctl --user`.

The Textual terminal client and its UI dependency are included in
`blueferry-backend`. After pairing with a graphical client or
`blueferry pair-setup`, start it with `blueferry-tui` (or `blueferry tui`). It
provides searchable conversation previews, a message timeline, mouse support,
a multiline composer, responsive narrow-terminal navigation, themes, and a
command palette. Use the arrow keys or `j`/`k` to select a conversation,
**Enter** to open it and to send from the composer, **Shift+Enter** for a new
line, `/` to search, `n` for a new message, `?` for the keyboard map, and
**Ctrl+P** for every command. Group
replies show the backend-owned participant list in a confirmation dialog.

### Omarchy Quattro

The native bar panel lives in
[omarchy-blueferry](https://github.com/erikwb/omarchy-blueferry):

```bash
omarchy plugin add https://github.com/erikwb/omarchy-blueferry.git
```

Its popup follows Quattro's own panel controls and shows connection health and
recent conversations. The full Quickshell client handles messages, pairing,
and preferences.

Enable it from **Setup › Plugins**. If BlueFerry is not installed, clicking
the widget opens a terminal with the source-build instructions. The standalone
Quickshell client also follows the active Quattro theme.

## Notifications and local data

The iPhone page can show message notifications only (the default), all iPhone
notifications, or none. Ordinary app notifications are shown and discarded;
they are not added to message history or exposed as a notification feed.
Messages arriving through both MAP and ANCS are deduplicated.
Because ordinary mirrored app notifications have no corresponding local view,
only message notifications offer the open-conversation action.

By default, message history and synced contacts are encrypted with a random key
stored in GNOME Keyring or KDE Wallet. If the wallet is locked, live messages
still work but history and contact lookup remain unavailable until you unlock
it. The iPhone settings also offer unencrypted local storage, with an explicit
warning, or **Do not retain local data**, which clears the cache and keeps new
events ephemeral. Changing storage modes clears existing local history and
cached contacts so encrypted and plaintext records are never mixed.

BlueFerry stores configuration in `~/.config/blueferry` and local state in
`~/.local/state/blueferry`. Pacman leaves those directories alone when the
packages are removed. Delete them yourself if you want a complete reset; the
encrypted mode's key is named “BlueFerry local storage key” in your wallet
manager.

The default popup lifetime and history limits can be changed in
`~/.config/blueferry/local.env`:

```bash
BLUEFERRY_SHOW_NOTIFICATION_CONTENT=false
BLUEFERRY_NOTIFICATION_TIMEOUT_MS=8000
BLUEFERRY_HISTORY_RETENTION_DAYS=30
BLUEFERRY_HISTORY_MAX_EVENTS=10000
BLUEFERRY_HISTORY_MAX_PAYLOAD_BYTES=268435456
```

Restart the user service after changing those environment settings.

## Command line

The graphical clients cover normal use, but the CLI is handy for diagnostics
and scripts:

```bash
blueferry sms-list
blueferry sms-send '+15551234567' 'on my way'
blueferry sms-send person@icloud.com 'hello from Linux'
blueferry sms-send Alice 'running late'
blueferry contacts-sync
blueferry history-clear
blueferry doctor
```

`sms-list` asks the live phone first and falls back to retained history.
Ambiguous contact names are resolved interactively rather than guessed.

## Troubleshooting

Start with the iPhone page in the app. It reports Messages, Contacts, and ANCS
separately, which matters: messages and contact sync can work even when the
optional notification connection does not.

For logs and prerequisite checks:

```bash
blueferry doctor
journalctl --user -u blueferry -f
```

If messages work but names do not, run **Sync Contacts** or
`blueferry contacts-sync`. When reporting a hardware problem, include the
iPhone model, iOS and BlueZ versions, and Bluetooth controller model. Remove
phone numbers, names, message bodies, and Bluetooth addresses from logs first.

## Technical notes

BlueFerry uses three Bluetooth paths:

- MAP over OBEX carries messages, inbox queries, read state, and sends.
- PBAP over OBEX supplies vCard contacts.
- ANCS over BLE supplies optional app notifications and sometimes enough
  display information to identify an unnamed group.

MAP and PBAP need a BR/EDR-capable controller. ANCS additionally needs usable
Bluetooth LE support. The full path has been tested with a MediaTek MT7922, but
BlueFerry checks controller capabilities rather than requiring a particular
vendor.

One unprivileged user daemon owns the Bluetooth sessions and exposes a small
session D-Bus API to the clients. Private records are returned by method call,
not broadcast in signals. Clients reply through opaque thread identities so a
UI cannot silently change the recipients of an existing conversation. The
default local encryption protects data at rest; unencrypted storage
deliberately gives up that protection, and neither mode is a sandbox against
another process already running as the same Unix user.

The pairing helper configures BlueZ through Polkit and presents the computer as
an ordinary accessory. It does not exploit the phone, bypass pairing consent,
or speak a hidden iCloud protocol. The long-running daemon is unprivileged and
there is no sudoers rule.

BlueFerry used
[iphonebridge](https://github.com/gabrielmeir53/iphonebridge), created by Gabe
Shatunovsky, as its initial implementation point.

The ANCS constants and wire-format
parser/builders are adapted from
[ANCS4Linux](https://github.com/bmh129/ancs4linux), by Paweł Zmarzły and
Bradley Harmon, under GPL-2.0-or-later.

The details live in [ARCHITECTURE.md](ARCHITECTURE.md),
[PROTOCOL.md](PROTOCOL.md), and [TESTING.md](TESTING.md).

BlueFerry is licensed under [GPL-2.0-only](LICENSE).
