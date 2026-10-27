# Clients

All clients talk to the same background service, so you can switch between
them or run several at once. Messages, contacts, and settings are shared.

## GTK (GNOME and similar)

`blueferry-gtk` is a GTK4/libadwaita app for GNOME, Cinnamon, and similar
desktops.

![GTK client](../../images/gtk.png)

**Settings** (main menu, `Ctrl+,`) has the same categories as the KDE
client, one page each; the switcher sits in the header bar, or at the
bottom in a narrow window, and the search button finds any setting:

| Category | What's there |
| --- | --- |
| Phone | Pairing, connection health with **Reconnect**, **Run Diagnostics** (the same checks as `blueferry doctor`; the report opens in its own dialog with a copy button), pairing report, contact sync, contact photos |
| Notifications | Which events create popups, contacts only, click rules, notification content, action buttons, sync notifications with iPhone, mark read on dismiss, keeping recent notifications |
| Calls & Audio | Phone calls, call history, battery warning, [iPhone sound on this computer](phone-audio.md), media control |
| Security | One-time codes, away lock, local storage and its encryption, Clear History |
| About | Version, the background service, About BlueFerry, keyboard shortcuts, **Restart Service**, Quit |

The switches that need a restart (below) work as in the KDE client: while a
change waits for the restart, every page starts with a notice and
**Restart Service**. The GTK client doesn't have the plugin settings yet;
use the KDE client or `blueferry plugins` for those.

## KDE Plasma (Kirigami)

`blueferry-qt` is a Kirigami app for KDE Plasma. It isn't available on
Ubuntu 24.04, Linux Mint 22.3, or Pop!_OS 24.04.

![KDE client](../../images/qt.png)

The window is a phone overview. On the left, a card shows the iPhone's name,
connection, battery and signal, what is playing (with previous, play/pause
and next), and quick switches for [the sound](phone-audio.md) and the
[away lock](proximity-lock.md). Features that are off stay visible but
greyed out, with the setting that turns them on. On the right are three
tabs:

- **Messages**: conversations with All, Unread and Starred filters, and the
  chat.
- **Calls**: [phone calls](calls.md) with a dial pad and running calls as
  cards (answer, hold, hang up), and the [call history](call-history.md)
  grouped by day, with All and Missed.
- **Notifications**: the [recent iPhone notifications](notification-history.md)
  of other apps.

In a narrow window the card folds behind the phone button in the header.

**Settings** (the gear in the header) has categories on the left, like KDE's
System Settings:

| Category | What's there |
| --- | --- |
| Phone | Pairing, connection health, **Run Diagnostics** (the same checks as `blueferry doctor`, shown in the window), contact sync, contact photos |
| Notifications | Which events create popups, contacts only, click rules, notification content, action buttons, sync notifications with iPhone, mark read on dismiss |
| Calls & Audio | Phone calls, call history, battery warning, [iPhone sound on this computer](phone-audio.md), media control |
| Security | One-time codes, away lock, local storage and its encryption, Clear History |
| About | Version, the background service, keyboard shortcuts, Quit |

In a narrow window the categories become a list; a category opens on its
own with **All Settings** to go back.

### Switches that need a restart

A few on/off settings used to need an edit of `~/.config/blueferry/local.env`:
showing notification content and marking a message read when you dismiss
its popup. The settings page and
`blueferry features` now store them in `~/.config/blueferry/settings.json`,
where they override `local.env`. They apply after the background service
restarts; the page says so and offers **Restart Service**. A variable set in
the service's own environment still wins, and the switch then says so.

```bash
blueferry features                                 # every switch, its value and source
blueferry features off mark_read_on_dismiss        # applies after a restart
```

### System tray item

`blueferry-tray` comes with `blueferry-qt` and puts a phone in the system
tray (a StatusNotifierItem on KDE Plasma). The icon shows the number of
unread messages; the tooltip shows the connection, battery, signal, and
network. Click it to open BlueFerry, or to bring an open window to the front.

Start it from the application menu as **BlueFerry Tray**. It does not start
at login unless you ask for that: check **Start at login** in its menu, or
run:

```bash
blueferry-tray --autostart on     # off, or status
```

This writes `~/.config/autostart/io.weirdware.BlueFerry.Tray.desktop`, the
same file the desktop's own autostart settings show.

The tray only follows a running background service; it never starts it.
Without a BlueFerry setup the icon stays hidden. While the tray runs,
`blueferry-qt` leaves out its own tray icon and quits when you close its
window; the tray opens it again.

## Quickshell

`blueferry-quickshell` follows Omarchy's active palette and system monospace
font, with compact controls and thin frames. Sent message bubbles stay blue
across themes. Outside Omarchy, it uses the desktop palette. It's packaged
for Arch-based distributions.

![Quickshell client](../../images/quickshell.png)

On Omarchy Quattro, the
[omarchy-blueferry](https://github.com/erikwb/omarchy-blueferry) bar widget
shows connection status and unread conversations, with a reply field under
each one:

```bash
omarchy plugin add https://github.com/erikwb/omarchy-blueferry.git
```

Enable it in **Setup → Plugins** and add it to your bar. The full Quickshell
client still handles pairing, messages, and preferences.

## Terminal (TUI)

The terminal client is part of `blueferry-backend` on every distribution.

```bash
blueferry-tui
# or
blueferry tui
```

Press `?` for the keyboard map or `Ctrl+P` for the command palette. It has
conversation search, a multiline composer, mouse support, themes, and a layout
that adapts to narrow terminals.

![Terminal client](../../images/terminal.png)

### Settings

`o` opens **Settings**, with the same categories as the Qt and GTK settings as
tabs. A setting appears only when the running background service offers it.

| Category | What you find there |
|---|---|
| Phone | Background service state, **Reconnect** (also `b`), resetting a stuck connection automatically (also `B`), **Run Diagnostics**, contact sync, contact photos |
| Notifications | Which events create popups, contacts only, **Click Rules** (also `l`), notification content, action buttons, sync notifications with iPhone, mark read on dismiss, keeping recent iPhone notifications |
| Calls & Audio | Phone calls, call history and missed-call popups, battery warning, iPhone sound on this computer (also `a`), media control and MPRIS |
| Security | One-time codes, away lock, the local storage setting (shown; change it with `blueferry storage-policy-set`) |
| About | Version, the background service with **Restart Service**, the keyboard map |

The switches that need a restart (see above) show the restart notice at the
top of Settings with **Restart Service**. A service that predates them shows
each switch greyed out with the `local.env` variable to set instead.

**Run Diagnostics** runs `blueferry doctor` and shows its report. **Copy
Report** puts it on the clipboard through the terminal (OSC 52); where the
terminal does not pass that on, select the text with the mouse instead.

The command palette (`Ctrl+P`) also has **Settings: Phone** … **Settings:
About** and **Run Diagnostics**. Pairing stays with `blueferry pair-setup`
or a graphical client; the terminal client has no plugin settings yet.

## Command line

The `blueferry` command is useful for diagnostics and scripts:

```bash
blueferry doctor                     # check prerequisites
blueferry sms-list                   # recent messages (--source local for the cache)
blueferry sms-send '+15551234567' 'on my way'
blueferry sms-send person@icloud.com 'hello from Linux'
blueferry sms-send Alice 'running late'
blueferry contacts-sync              # pull contacts from the iPhone again
blueferry storage-policy-set encrypted   # or plaintext, none
blueferry storage-unlock             # ask the keyring to unlock stored data
blueferry history-clear              # delete local message history
blueferry pairing-issue              # open a GitHub issue with the last pairing report
blueferry version
```

If a contact name is ambiguous, BlueFerry lists the matches for you to choose
from instead of guessing. Run `blueferry <command> --help` for all options.

## Which client opens from a notification

Clicking a message notification opens its conversation in a running graphical
client, preferring the one you used most recently. If none is running,
BlueFerry opens the last client you used. Without a previous choice, it
prefers GTK on GNOME, Qt on KDE, and Quickshell on Omarchy/Hyprland, falling
back to whichever client is installed.
