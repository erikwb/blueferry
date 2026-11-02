# Clients

All clients talk to the same background service, so you can switch between
them or run several at once. Messages, contacts, and settings are shared.

## GTK (GNOME and similar)

`blueferry-gtk` is a GTK4/libadwaita app for GNOME, Cinnamon, and similar
desktops.

![GTK client](../../images/gtk.png)

The window is a phone overview. A card above the tabs shows the iPhone's
name and connection, battery, signal and network, and what is playing (with
previous, play/pause and next). Its **Quick Settings** button holds the
switches for [the sound](phone-audio.md) and the
[away lock](proximity-lock.md). When something is off, the card says which
setting turns it on. The tabs are switched in the header bar, or at the
bottom in a narrow window:

- **Messages**: conversations and the chat.
- **Calls**: running [phone calls](calls.md) with answer, hold/resume,
  hang up, keypad tones (0-9, `*`, `#`) for the active call and the sound
  switch, a dial pad that also searches your
  contacts by name, and the [call history](call-history.md) grouped by day,
  with All and Missed, call back and message buttons, and **Refresh from
  iPhone**. Every call asks for confirmation first.
- **Notifications**: the [recent iPhone notifications](notification-history.md)
  of other apps. When the list is off, the tab says why and opens the
  setting.

**Settings** (main menu, `Ctrl+,`) has the same categories as the KDE
client, one page each; the switcher sits in the header bar, or at the
bottom in a narrow window, and the search button finds any setting:

| Category | What's there |
| --- | --- |
| Phone | Pairing, connection health with **Reconnect**, **Run Diagnostics** (the same checks as `blueferry doctor`; the report opens in its own dialog with a copy button), pairing report, contact sync, contact photos |
| Notifications | Which events create popups, contacts only, click rules, notification content, action buttons, sync notifications with iPhone, mark read on dismiss, keeping recent notifications |
| Calls & Audio | Phone calls, call history, battery warning, [iPhone sound on this computer](phone-audio.md), media control |
| Network | Opt-in [Bluetooth tethering](bluetooth-tethering.md) |
| Security | One-time codes, away lock, local storage and its encryption, Clear History |
| Plugins | Installed [plugins](plugins.md) with their switch, **Show Log**, **Check for Update** and **Remove**; the store, your own Git URL and the plugin lists. Every install asks first. A plugin's **Settings** open as a page of their own |
| About | Version, the background service, About BlueFerry, keyboard shortcuts, **Restart Service**, Quit |

The switches that need a restart (below) work as in the KDE client: while a
change waits for the restart, every page starts with a notice and
**Restart Service**. The GTK client doesn't have the Photos tab and the
plugin section of the card yet; use the KDE client, or `blueferry photos`
and `blueferry cards` on the command line, for those.

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
  cards (answer, hold, hang up, keypad tones for the active call), and the [call history](call-history.md)
  grouped by day, with All and Missed. Dialing asks you to confirm the number;
  names and numbers with extensions are never converted into another number.
- **Notifications**: the [recent iPhone notifications](notification-history.md)
  of other apps.

In a narrow window the card folds behind the phone button in the header.
Messages are marked read only while their conversation is visible and the
window has focus. The phone card's sound switch controls media playback;
it does not switch the audio of a phone call.

**Settings** (the gear in the header) has categories on the left, like KDE's
System Settings:

| Category | What's there |
| --- | --- |
| Phone | Pairing, connection health, **Run Diagnostics** (the same checks as `blueferry doctor`, shown in the window), contact sync, contact photos |
| Notifications | Which events create popups, contacts only, click rules, notification content, action buttons, sync notifications with iPhone, mark read on dismiss |
| Calls & Audio | Phone calls, call history, battery warning, [iPhone sound on this computer](phone-audio.md), media control |
| Network | [Bluetooth tethering](bluetooth-tethering.md), share iPhone Internet, automatic hotspot connection |
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
It opens the client you use: a running BlueFerry window first, otherwise the
client you used last, otherwise the one that suits your desktop.

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
window; the tray opens it again. **Quit Tray Icon** in its menu only removes
the tray icon; BlueFerry and its open windows keep running.

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

### Settings

**iPhone settings** has the same categories as the Qt, GTK and terminal
settings, one below the other. A setting appears only when the running
background service offers it.

| Category | What you find there |
|---|---|
| Phone | Pairing, connection health with **Reconnect**, resetting a stuck connection automatically, **Run Diagnostics**, **Sync Contacts**, contact photos |
| Notifications | Which events create popups, contacts only, notification content, action buttons, sync notifications with iPhone, mark read on dismiss, keeping recent iPhone notifications, and the recent notifications themselves |
| Calls & Audio | Phone calls, call history and missed-call popups, battery warning, iPhone sound on this computer, media control and MPRIS |
| Security | One-time codes, away lock, the local storage setting |
| About | The background service, its version and **Restart Service** |

The switches that need a restart show the restart notice at the top of the
page with **Restart Service**. A service that predates them shows each
switch greyed out with the `local.env` variable to set instead.

**Show Recent Notifications** lists the last 30 notifications that **Keep
recent iPhone notifications** keeps: the app, the time and, if allowed,
title and text. The list is read only while it is shown, follows new
notifications, and is forgotten when you hide it or leave the page. When it
is off, empty or unavailable, it says why.

### What the panel leaves out

Quickshell is a compact panel, so some parts stay with the other clients:

| Left out | Why | Use instead |
|---|---|---|
| Plugin management and plugin settings | Each plugin brings its own settings form, secrets, buttons and log, and the store lists plugins to install; that is too much, and too varied, for a compact panel | Qt or GTK settings, `blueferry plugins` |
| **Send to…** and the **Photos** tab (plugin features) | Send to needs a file picker or file drop, which the panel does not have; both come from plugins, which the panel does not manage | Qt client (Photos), Qt or GTK client (Send to), `blueferry send`, `blueferry photos` |
| Companion tools | Setting up desktop helpers is a one-time job, not a panel task | Qt or GTK settings, `blueferry tools` |
| Clearing the message history | Not offered by the terminal client either | `blueferry history-clear` |
| Click Rules, recent calls, the calls panel | Not in the Quickshell client yet | Qt, GTK or terminal client |

The panel loads no translation catalog, so its own labels are English.
Texts that come from the shared BlueFerry code, such as the reconnect hint,
the restart notice or the notification placeholder, follow your language.

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

### iPhone, calls and notifications

**iPhone Overview** in the command palette is the terminal's phone card:
the iPhone's name, the connection, battery, signal and network, what is
playing, and for each optional feature that is off the switch that turns it
on and its Settings category. Its buttons open the notification list, the
recent calls, the calls panel and Settings.

**iPhone Notifications** (in the palette and the overview) lists the recent
iPhone app notifications that **Keep recent iPhone notifications** keeps:
the app, the time and, if allowed, title and text. It is read only while it
is open, follows new notifications, and is forgotten when you close it.
When the list is off, empty or unavailable, it says why; **Open
Notification Settings** goes to Settings › Notifications.

The calls panel (`c`, or **Phone Calls** in the palette) words each call
like Qt and GTK (incoming, on hold, the duration). Its one row of buttons
acts on the obvious call: **Answer** the ringing one, **Hold** or
**Resume** the connected one, **Hang Up** (or **Decline**) the active one
first; a call waiting while another is connected gets its own **Decline**.
During an active call, the keys 0-9, `*` and `#` are sent at once as
keypad tones, for example to a phone menu, unless a text field has the
focus; the panel shows them as "Sent: …". The keypad tone field sends a
whole sequence such as a PIN. Typing a name in the number field suggests the
phone numbers of matching contacts. A call is always confirmed first.

**Recent calls** (`h`) are grouped by day; `m` shows only missed calls and
`s` (or **Refresh from iPhone**) reads the iPhone's list again. Enter or
**Call Back** calls the selected number after the confirmation, **Send a
Message** starts a new message to it.

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
