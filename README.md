# BlueFerry

![BlueFerry messaging client](screenshot.png)

Use your iPhone's messages on Linux over Bluetooth.

<p align="center">
  <img src="ferry.jpg" width="320" alt="Blue Star ferry at Rhodes">
</p>

BlueFerry brings SMS, RCS, and iMessage from a paired iPhone to your Linux desktop.
You can read and reply to messages, start a conversation, search synced
contacts, and optionally mirror other iPhone notifications. There is no Mac
relay, Apple login, cloud service, or subscription.

This is still experimental software. Most development has used an iPhone 16
Pro Max on iOS 26.5, with additional successful testing on an iPhone 17 Pro Max
running an iOS 27 beta. Apple can change the Bluetooth behavior BlueFerry relies
on, so don't make it your only way to receive an important message yet.

## What works

- Receive and send SMS, RCS, and iMessage through the iPhone.
- Sync contacts, including phone numbers and Apple-ID email addresses.
- Mark messages read from the desktop.
- Use native GTK, KDE/Kirigami, Quickshell, or terminal clients.
- Keep local history encrypted with GNOME Keyring or KDE Wallet.
- Group chats, when BlueFerry can identify the participants safely.

BlueFerry only knows about messages it sees while connected; it does not
download your iCloud Messages archive. Attachments, reactions, typing
indicators, FaceTime, and complete sent-message history are not supported.
Phone calls are off by default; an experimental, opt-in oFono integration is
described under [Phone calls](#phone-calls-optional-experimental).

Direct conversations combine the phone numbers and email addresses that belong
unambiguously to one synced contact. Replies use the most recent incoming
address, shown as **Reply to** above the conversation. Shared addresses and
contacts that merely have the same name stay separate. Original message
addresses are retained; editing and syncing contacts updates the grouping.

Group replies are deliberately cautious. Bluetooth does not give BlueFerry a
reliable group ID or complete roster, so it disables replies when the
participants are unclear. For a named group, BlueFerry learns members only from
the people who send to it, so it asks you to confirm the full reply list once.
That saved list is kept until you delete the conversation, clear history, or
change the storage mode; it does not change the group on the iPhone. A message is filed under its group
only when BlueFerry also receives the iPhone's notification for it; without
one, it appears in the sender's one-to-one conversation.

## Install

Download the native packages for your distribution from the
[latest BlueFerry release](https://github.com/erikwb/blueferry/releases/latest).
Install `blueferry-backend` plus the client for your desktop. The backend also
includes the `blueferry-tui` terminal client.

Clicking a message notification opens its conversation in a running graphical
client, preferring the most recently used one. If none is running, BlueFerry
opens the last client you used. With no previous choice, it prefers GTK on
GNOME, Qt on KDE, and Quickshell on Omarchy/Hyprland, using an installed client
if the preferred one is unavailable.

For Arch Linux or CachyOS, download the `.pkg.tar.zst` files and install them
with pacman. For example, to install the GTK client:

```bash
sudo pacman -U ./blueferry-backend-*.pkg.tar.zst ./blueferry-gtk-*.pkg.tar.zst
```

For Debian, Ubuntu, Mint, Pop!_OS, or PikaOS, download the `.deb` files and
install them with apt:

```bash
sudo apt install ./blueferry-backend_*.deb ./blueferry-gtk_*.deb
```

For Fedora 43 or 44, download the `.fc43.noarch.rpm` files. For Fedora 45,
download the `.fc45.noarch.rpm` files. Fedora 43 and 44 use the same package
build. Install the backend and your client with dnf:

```bash
# Fedora 43 and 44; use fc45 below for Fedora 45.
sudo dnf install ./blueferry-backend-*.fc43.noarch.rpm \
  ./blueferry-gtk-*.fc43.noarch.rpm
```

Replace the GTK package with `blueferry-qt` for KDE Plasma. Arch and CachyOS
also provide `blueferry-quickshell`. The tested matrix currently covers Arch
Linux, CachyOS, Debian 13, Ubuntu 24.04 and 26.04, Linux Mint 22.3, Pop!_OS
24.04, PikaOS IV, and Fedora 43, 44, and 45. Ubuntu 24.04, Mint, and Pop!_OS do
not provide necessary Qt dependencies, so use the GTK or terminal client there.

Arch and Fedora packages set up the newer Bluetooth support needed for iPhone
system notifications. Debian-family packages do not change or restart
Bluetooth; messages and contacts still work, and notifications are added only
when that machine already supports them.

### Build packages from source

Clone the repository, then choose the build instructions for your
distribution:

```bash
git clone https://github.com/erikwb/blueferry.git
cd blueferry
```

#### Arch based distros

Install the basic build tools, then build and install all four packages:

```bash
sudo pacman -S --needed base-devel python
./build.sh -si
```

Run `./build.sh` without `-i` to build without installing. Finished packages
are written to `packaging/arch/`.

The build always uses `/usr/bin/python`, because the build and test
dependencies are Arch packages for the system interpreter. A mise, pyenv,
conda, or activated virtualenv python earlier in `PATH` is ignored rather
than producing a package for the wrong `site-packages` directory.

#### Debian based distros

```bash
sudo apt-get install devscripts equivs
sudo mk-build-deps -i -r -t 'apt-get -y --no-install-recommends' packaging/deb/control
./packaging/build-deb.sh
```

Finished packages are written to `dist/deb/`.

#### Fedora

```bash
sudo dnf install dnf-plugins-core rpm-build
sudo dnf builddep packaging/rpm/blueferry.spec
./packaging/build-rpm.sh
```

Finished packages are written to `dist/rpm/`.

#### OpenRC systems

On OpenRC, install BlueFerry's D-Bus activation file and let the session bus
start the backend; no init script is needed. An optional OpenRC user service
(OpenRC 0.62 or newer) is only for desktops whose session bus is
`$XDG_RUNTIME_DIR/bus`. Do not create one on `dbus-run-session` desktops such
as Plasma under greetd or SDDM. For iPhone system notifications, start
`bluetoothd` with `-E` (see `/etc/conf.d/bluetooth`) and run
`sudo rc-service bluetooth restart`. Without systemd, the daemon also runs
without the systemd unit's sandboxing. See
[packaging/openrc/README.md](packaging/openrc/README.md) before setting up.

See [packaging/README.md](packaging/README.md) for the exact support matrix and
more packaging details.

## Pair an iPhone

Start the client that fits your desktop:

```bash
blueferry-gtk         # GNOME, Cinnamon, and similar desktops
blueferry-qt          # KDE Plasma
blueferry-quickshell  # Quickshell
```

BlueFerry requires an adapter with Bluetooth Classic and Bluetooth 4.0 or newer
with LE advertising support. LE advertising is needed to enable the iPhone's
messages and contacts, including in compatibility mode. Bluetooth 3-only
adapters are incompatible.

Then:

1. Keep the iPhone unlocked with **Settings → Bluetooth** open.
2. Let BlueFerry check your Bluetooth controller.
3. In BlueFerry, choose **Scan**, select the iPhone, and choose **Pair**.
   BlueFerry starts the pairing request; you do not need to find and tap the
   computer under **Other Devices** on the phone.
4. When the request appears on the iPhone, approve it and confirm that both
   devices show the same code. It can take around 15 seconds to appear.
5. After pairing, tap **ⓘ** beside the computer on the iPhone and enable
   **Show Message Notifications** and **Sync Contacts**. If the toggles are
   missing, return to the Bluetooth device list and reopen the **ⓘ** page a few
   times. If iOS asks to **Allow System Notifications**, approve that too.
6. Wait for Messages and Contacts to show as connected. If you use the default
   encrypted storage, approve the desktop wallet prompt.

System Notification access lets BlueFerry recognize group-message metadata.
When it is unavailable, ordinary messages and contacts still work, but a group
message may look like a direct conversation with its sender.

### Pairing options

Most people should leave both options unchecked.

- **Compatibility pairing for iOS 18 or earlier** keeps the signal that makes
  the Messages and Contacts permissions appear, but does not connect iPhone
  system notifications. BlueFerry also chooses this automatically when the
  local BlueZ stack cannot support them.
- **Use explicit Bluetooth pairing** skips the normal connection-first
  approach and asks BlueZ to pair immediately. Try it only if normal pairing
  keeps getting canceled on that Bluetooth controller. It is independent of
  iOS compatibility mode.

For a clean retry, forget the computer on the iPhone and forget the iPhone on
Linux before pairing again. Stale phone-side Bluetooth state can survive a
one-sided forget, so reset both sides rather than repeatedly pairing over the
old record.

The terminal wizard exposes the same flow:

```bash
blueferry pair-setup
blueferry pair-setup --compatibility-mode
blueferry pair-setup --explicit-pairing
```

Once setup is complete, the backend starts automatically and reconnects after
normal Bluetooth interruptions. Package upgrades and same-version local
rebuilds are detected automatically, so an old backend process is restarted
when needed.

## Terminal client

The TUI is included in `blueferry-backend` on every supported package format.
Arch uses its repository Textual package; DEB and RPM builds carry a private
Textual 8 runtime for the TUI.

Start it with either:

```bash
blueferry-tui
blueferry tui
```

Press `?` for the keyboard map or `Ctrl+P` for the command palette. The TUI has
conversation search, a multiline composer, mouse support, themes, and a layout
that adapts to narrow terminals.

## Omarchy Quattro

The native bar panel lives in
[omarchy-blueferry](https://github.com/erikwb/omarchy-blueferry):

```bash
omarchy plugin add https://github.com/erikwb/omarchy-blueferry.git
```

Enable it from **Setup › Plugins**. Its popup shows connection health and recent
conversations; the full Quickshell client handles pairing, messages, and
preferences.

The Quickshell client follows Omarchy's active palette and system monospace
font, with compact controls and thin frames. Sent message bubbles stay blue
across themes. Outside Omarchy, it uses the desktop palette.

![Quickshell client with sample conversations](docs/images/quickshell.png)

## Notifications and local data

BlueFerry can show message notifications only—the default—all iPhone
notifications, or none. Other app notifications are displayed and discarded;
they are not added to message history. Messages seen through both MAP and ANCS
are deduplicated. The ANCS copy of an Apple Messages notification (title,
subtitle and up to the first 1024 bytes of the text) is kept in local history,
because it carries the group details that MAP lacks.

Message history and contacts are encrypted by default with a random key stored
in GNOME Keyring or KDE Wallet. If the wallet is locked, live messages continue
to work, but retained history and contact lookup wait until you unlock it. You
can also choose unencrypted storage or **Do not retain local data**. Changing
storage modes clears the existing cache so encrypted and plaintext records are
never mixed.

Starred conversations, saved group participants, and group confirmations follow
the same storage policy as history. Older plaintext preferences are encrypted
during upgrade when the wallet is available; if it is locked, those old
preferences are cleared so contact identities are no longer retained in
plaintext. You can star conversations and confirm group rosters again after
unlocking.

Configuration lives in `~/.config/blueferry`; local state lives in
`~/.local/state/blueferry`. Uninstalling packages does not delete either
directory.

The common retention settings live in `~/.config/blueferry/local.env`:

```bash
BLUEFERRY_SHOW_NOTIFICATION_CONTENT=false
BLUEFERRY_KEEP_PHONE_AUDIO_ON_PHONE=true
BLUEFERRY_NOTIFICATION_TIMEOUT_MS=8000
BLUEFERRY_MARK_READ_ON_DISMISS=false
BLUEFERRY_HISTORY_RETENTION_DAYS=30
BLUEFERRY_HISTORY_MAX_EVENTS=10000
BLUEFERRY_HISTORY_MAX_PAYLOAD_BYTES=268435456
```

By default, dismissing a message's desktop popup marks it read on the
iPhone too. Some notification-center "block" or do-not-disturb actions
dismiss the popup instead of just hiding it, which silently marks the message
read on the phone. Set `BLUEFERRY_MARK_READ_ON_DISMISS=false` if you don't
want desktop dismissal to ever change read state on the iPhone.

When **All iPhone Notifications** is selected, optional exact bundle-ID rules
can limit which non-Messages ANCS apps create desktop popups:

```bash
# Keep all iPhone app notifications except these apps:
BLUEFERRY_ANCS_APP_BLOCKLIST=com.example.Chat,com.example.Mail

# Or allow only these apps (omit this setting to allow every unblocked app):
# BLUEFERRY_ANCS_APP_ALLOWLIST=com.example.Calendar,com.example.Reminders
```

If both are configured, the blocklist wins. An explicitly empty allowlist
blocks every non-Messages app. Apple Messages is always processed for group
conversation metadata, but its duplicate ANCS popup remains suppressed in
favor of the MAP message popup.

Bundle IDs are case-sensitive. To discover them, follow the user-service log
while causing the app to send a notification; BlueFerry logs each app once per
daemon run without logging its notification content:

```bash
journalctl --user -u blueferry -f | grep "ANCS app observed"
```

Restart the user service after editing `local.env` settings.

When WirePlumber 0.5 or newer is installed, BlueFerry keeps calls and music on
the iPhone by writing
`~/.config/wireplumber/wireplumber.conf.d/99-blueferry-keep-phone-audio.conf`
before pairing and waiting for WirePlumber to reload it. The fragment removes
the adapter roles that make this computer an A2DP/HFP sink, and disables
auto-connect on phone cards so a later `bluetooth-a2dp-autoconnect` rule cannot
steal the stream. A failed pairing attempt removes a fragment that attempt
installed. After a successful bond the daemon keeps reconciling the same
file. Set `BLUEFERRY_KEEP_PHONE_AUDIO_ON_PHONE=false` to remove BlueFerry's
fragment only. With the optional phone calls enabled, the fragment keeps the
hands-free roles (`hfp_hf`, `hsp_hs`) and still removes `a2dp_sink`: music
stays on the iPhone, calls can come to this computer.

## Phone calls (optional, experimental)

BlueFerry can answer, decline, place, and hang up iPhone calls through
[oFono](https://git.kernel.org/pub/scm/network/ofono/ofono.git)'s Hands-Free
Profile support. This is **off by default** and not needed for messaging.
An earlier HFP experiment was removed from BlueFerry because oFono and
PipeWire's native HFP backend race for the same BlueZ profile (see
[Historical HFP result](PROTOCOL.md#historical-hfp-result)); this opt-in
integration leaves that choice and its setup to you, only suggests oFono as
an optional package, and keeps working normally when oFono is missing.

The integration was developed against oFono 2.18 and BlueZ 5.87 and has been
used with an iPhone on one Gentoo/OpenRC desktop: the modem comes up, an
incoming call rings with a popup, and calls can be answered and hung up.
Call waiting, DTMF, and other setups are untested; treat it as a preview.

Requirements:

- oFono running as a system service, with its HFP hands-free plugin.
  BlueFerry never starts oFono itself (its calls carry D-Bus NO_AUTO_START).
- oFono's D-Bus policy must admit your user. oFono's shipped `ofono.conf`
  (for example `/etc/dbus-1/system.d/ofono.conf` or
  `/usr/share/dbus-1/system.d/ofono.conf`, depending on the distribution)
  only allows root and `at_console` sessions; a desktop without console tracking gets
  `AccessDenied` and BlueFerry reports calls as **unavailable** (logged once).
  A minimal drop-in, for example `/etc/dbus-1/system.d/ofono-local.conf`:

  ```xml
  <!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
   "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
  <busconfig>
    <policy user="your-login">
      <allow send_destination="org.ofono"/>
    </policy>
  </busconfig>
  ```

  Security meaning: every process running as that user (or, with
  `<policy group="…">`, as any member of that group) may fully control oFono,
  i.e. place, answer, and end calls and change settings of every modem oFono
  manages, not only BlueFerry. Prefer `user=` over a broad group. The system
  bus normally picks up the new file by itself; otherwise reload it.
- PipeWire/WirePlumber configured to hand HFP to oFono. Your own fragment
  should only select the backend, for example
  `~/.config/wireplumber/wireplumber.conf.d/51-bluez-ofono.conf`:

  ```text
  monitor.bluez.properties = {
    bluez5.hfphsp-backend = "ofono"
  }
  ```

  Do not set roles there: BlueFerry's `99-` phone-audio fragment (above)
  overrides `bluez5.roles` and, with calls enabled, keeps `hfp_hf` and
  `hsp_hs` while still removing `a2dp_sink`. If you set
  `BLUEFERRY_KEEP_PHONE_AUDIO_ON_PHONE=false`, BlueFerry manages no roles and
  your own `bluez5.roles` must include `hfp_hf`.

Switch it on with **Enable phone calls through this computer** in the Qt
client's iPhone settings, or with `blueferry calls enable` (`disable` turns it
off again). The choice is saved in `settings.json` and applied without a
restart. `BLUEFERRY_CALLS_ENABLED=true` in `~/.config/blueferry/local.env`
still works as the initial value; a saved choice wins.

What turning it on means: while the iPhone is connected, its hands-free link
stays up with this computer, so calls ring here and, once answered here,
their audio plays here. Turning it off (or quitting the backend) powers the
hands-free modem down again (`Powered=false`) if BlueFerry powered it or
found it powered while calls were on, so
call audio goes back to the phone; a call in progress continues there.

Toggling rewrites the phone-audio fragment. BlueFerry restarts
`wireplumber.service` only through systemd; on hosts where WirePlumber is not
a systemd user service (for example OpenRC with a session launcher), restart
WirePlumber yourself, e.g. `gentoo-pipewire-launcher restart`. The backend
log then says "WirePlumber fragment changed; restart WirePlumber to apply".

What happens then:

- The backend looks for the iPhone's oFono modem (type `hfp`, path ending in
  `dev_XX_XX_XX_XX_XX_XX` for the paired phone). iOS does not power this modem
  up by itself: BlueFerry sets `Powered=true` while the Classic link is up,
  waits for oFono to confirm it, then sets `Online=true`. Call control is
  ready once the modem is online and lists oFono's call manager. It also
  raises oFono's call volume to 100 % because the 50 % default is nearly
  inaudible with an iPhone.
- An incoming call shows a desktop notification with **Answer** and
  **Decline** (without the caller when
  `BLUEFERRY_SHOW_NOTIFICATION_CONTENT=false`; the contacts-only notification
  setting deliberately does not apply to calls). The Qt client has a
  **Phone Calls** dialog, the terminal client a calls panel (`c`) and an
  "Incoming call" notice, and the CLI a `calls` command group:

  ```bash
  blueferry calls                 # state and current calls
  blueferry calls enable          # or: disable
  blueferry calls dial '+41 79 123 45 67'   # asks first; --yes in scripts
  blueferry calls answer          # the ringing call
  blueferry calls dtmf 1234#      # tones on the active call
  blueferry calls hangup          # or: hangup --all
  ```

- `dial` accepts plain numbers only (an optional leading `+` and digits;
  spaces, dashes, dots, parentheses, and slashes are ignored). `*` and `#`
  are refused: dialed, they form service codes such as `**21*…#` that
  reconfigure the phone (call forwarding) rather than place a call. Use
  `dtmf` for keypad symbols during a call.
- A fixed list of known emergency numbers (112, 911, 999, 000, 110, 117, 118,
  119, 144 and about fifty other national ones, `EMERGENCY_NUMBERS` in
  `calls/model.py`) is refused: call them on the iPhone, where the call does
  not depend on this computer's Bluetooth link or audio, and iOS's Emergency
  SOS and location sharing apply. The list matches whole numbers only and
  cannot be complete, so the phone stays the way to make emergency calls.
  Every client asks before it dials, because
  premium-rate prefixes differ by country and cannot be listed reliably.
- Bluetooth recovery (the adapter power cycle after a long ANCS outage) is
  held back while a call is in progress.
- With a second call, answering holds the active call (HoldAndAnswer);
  `swap` and `hold-answer` are available, but "release and answer" is not
  exposed. Hanging up the held call of two relies on the phone supporting
  `AT+CHLD=1x` through oFono and still needs hardware verification.
- If oFono is not installed, not running, or denies access, calls report
  **unavailable**; BlueFerry notices when oFono starts. Call details are only
  returned by the private `Calls1.ListCalls` method; the broadcast
  `CallsChanged` signal carries no content. Calls are not written to message
  history.

Troubleshooting: if `blueferry calls` stays at **searching** although the
iPhone is connected, the likely cause is the startup-order race between oFono
and WirePlumber for the HFP profile. Restart oFono after WirePlumber
(`sudo rc-service ofono restart` on OpenRC, `sudo systemctl restart ofono` on
systemd), then check the backend log for the modem. With BlueZ 5.87 or newer, BlueZ's
own HFP hands-free plugin can also claim the RFCOMM channel before oFono
(oFono's `Powered=true` then times out). When that happens three times in a
row while `bluetoothd` 5.87 or newer runs with `-E` and without `-P hfp`,
BlueFerry reports the call state **bluez_conflict**, logs the remedy once, and
pages the phone only every five minutes, or when it reconnects, until a
power-up succeeds. Disable that plugin by starting `bluetoothd` with
`-P hfp` (for example `BLUETOOTH_OPTS="-E -P hfp"` in
`/etc/conf.d/bluetooth` on Gentoo, or a `bluetooth.service` drop-in on
systemd). Audio routing itself is PipeWire's job; BlueFerry only controls the
call.

## Lock when the iPhone goes away

BlueFerry can lock your desktop session after the paired iPhone has been
disconnected for a while. It is off by default. Turn it on in the Qt client's
iPhone settings (**Away Lock**) or from a terminal:

```bash
blueferry proximity-lock enable --grace 60   # opt in, lock after 60 s away
blueferry proximity-lock status
blueferry proximity-lock test                # dry run, never locks
blueferry proximity-lock disable
```

`BLUEFERRY_PROXIMITY_LOCK=true` and `BLUEFERRY_PROXIMITY_LOCK_GRACE_SEC=60`
(10–3600) in `local.env` set the initial values; a choice saved through a
client or the CLI takes precedence, and the daemon logs once at startup when
it ignores a differing `local.env` or environment value for that reason.

**This is a lock trigger, not a security feature.** Bluetooth presence can be
relayed or spoofed, and the phone turning off Bluetooth looks the same as the
phone leaving. BlueFerry therefore only ever *locks*; it never unlocks the
desktop when the iPhone returns, and you should keep your normal password or
PIN. Use it as a "forgot to lock" safety net.

How it decides:

- Presence is the Bluetooth link state BlueFerry already maintains (Classic
  or LE connected). There is no extra scanning and no signal-strength check.
- It arms only after it has seen the iPhone connected since the service
  started, the system resumed, or the last lock.
- A disconnect starts the grace period; reconnecting within it cancels the
  lock. After locking once it waits until the iPhone is seen again.
- It never locks while the system is suspending, while desktop Bluetooth is
  off, during Bluetooth discovery or pairing, while BlueFerry itself is
  recovering the adapter, or after the iPhone is forgotten.
- When BlueZ reports that this computer ended the connection (for example
  "Disconnect" in a desktop Bluetooth applet), the lock pauses until the
  iPhone is connected again. Older BlueZ versions without this report
  simply do not pause.

Locking uses `org.freedesktop.ScreenSaver.Lock` on the session bus (KDE
Plasma and others) and falls back to `org.freedesktop.login1.Session.Lock`
for your own logind or elogind session. KDE answers only once its lock
screen is up; a slow answer is reported as `screensaver-requested` and does
not fall back to logind. If screen locking is disabled by policy (for
example a KDE Kiosk `lock_screen=false` restriction), Plasma reports the
request as successful without locking, and BlueFerry cannot tell.

## Command line

The graphical clients cover normal use, but the CLI is useful for diagnostics
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

Ambiguous contact names are presented for you to choose from rather than
guessed.

## Troubleshooting

Start with the iPhone page in the app. It reports Messages, Contacts, and iPhone
Notifications separately; messages and contact sync can work even when the
optional notification connection does not.

For logs and prerequisite checks:

```bash
blueferry doctor
journalctl --user -u blueferry -f
```

With the optional OpenRC user service, the backend log is
`~/.local/state/blueferry/daemon.log`.

If messages work but names do not, use **Sync Contacts** or run
`blueferry contacts-sync`.

If `blueferry-qt` does not open a window or looks unstyled,
`blueferry-qt --diagnose-style` prints the Qt Quick Controls style it would
use, how it sets it, and where it looked for the style.

If notifications previously worked with the same phone and adapter but stay
unavailable for five minutes, BlueFerry can attempt one adapter power cycle.
It first tries an LE-only reset and checks that the phone still answers a
read-only MAP request. Automatic cycling is skipped if another Bluetooth
device is paired or connected to that adapter, discovery is active, or BlueFerry
is transferring data. It requires BlueZ to report power transitions and
respects Bluetooth being turned off and explicit permission failures.
The attempt limit survives backend restarts: another cycle requires ten minutes
of verified notification connectivity, and cycles are at least an hour apart.
If a power request times out, the running backend keeps checking the original
controller and retries restoration without issuing another power-off request.
Pending restoration also survives backend restarts and finishes before normal
Bluetooth setup resumes. Once power-on is confirmed by a reply or observed
after power-off, restoration ends; a later manual power-off is left alone.
Once power-off has been requested, a failed journal update does not prevent
power-on. Failed journal cleanup is retried without pausing messaging; further
automatic power cycles wait until cleanup succeeds.
Recovery state is refreshed after acquiring the backend's D-Bus name, and a
second daemon that cannot acquire it does not attempt restoration during
shutdown.
Restoration stops if the controller is unplugged, bluetoothd or the system bus
restarts, the configured phone changes, or rfkill blocks it.
D-Bus cannot make checking an adapter and changing its power atomic;
BlueFerry watches for changes and rechecks immediately before requesting power-off.
Recovery decisions and failures appear in the backend journal.

Pairing failures save a scrubbed report that can be attached to a GitHub issue.
It includes the package build and source SHA, pairing mode, controller details,
and an ordered setup timeline. Please also include the iPhone model and iOS
version. Reports remove Bluetooth addresses and home-directory paths, but it is
still sensible to inspect anything before posting it publicly.

## Technical details

BlueFerry uses three standard Bluetooth services:

- MAP over Bluetooth Classic carries messages, read state, and sends.
- PBAP over Bluetooth Classic supplies contacts.
- ANCS over Bluetooth LE supplies optional notifications and group-message
  display information.

One unprivileged per-user backend owns those connections and exposes a small
session D-Bus API to the clients. Quickshell messaging reaches that API through
one persistent stdin bridge; its short-lived command helpers are limited to
setup before the backend is configured. Pairing and unpairing require approval
in the initiating BlueFerry client and use normal Bluetooth confirmation;
there is no hidden Apple protocol.

The deeper design and protocol notes live in
[ARCHITECTURE.md](ARCHITECTURE.md), [PROTOCOL.md](PROTOCOL.md),
[TESTING.md](TESTING.md). Release history is in [CHANGELOG.md](CHANGELOG.md).

BlueFerry began from
[iphonebridge](https://github.com/gabrielmeir53/iphonebridge), created by Gabe
Shatunovsky. Parts of the optional oFono call controller are adapted from
[tincan](https://github.com/quad341/tincan) under the MIT License. The ANCS constants and wire-format code are adapted from
[ANCS4Linux](https://github.com/bmh129/ancs4linux), by Paweł Zmarzły and
Bradley Harmon, under GPL-2.0-or-later.

BlueFerry is licensed under [GPL-2.0-or-later](LICENSE).
