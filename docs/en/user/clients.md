# Clients

All clients talk to the same background service, so you can switch between
them or run several at once. Messages, contacts, and settings are shared.

## GTK (GNOME and similar)

`blueferry-gtk` is a GTK4/libadwaita app for GNOME, Cinnamon, and similar
desktops.

![GTK client](../../images/gtk.png)

## KDE Plasma (Kirigami)

`blueferry-qt` is a Kirigami app for KDE Plasma. It isn't available on
Ubuntu 24.04, Linux Mint 22.3, or Pop!_OS 24.04.

![KDE client](../../images/qt.png)

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
