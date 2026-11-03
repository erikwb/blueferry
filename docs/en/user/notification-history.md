# Recent iPhone notifications

BlueFerry can keep a list of the iPhone's recent app notifications (other
than Messages) for the **Notifications** tab of the KDE and GTK clients
and the command line. The list lives only in the background service's memory;
only the notifications you **pin** are saved, so they survive a restart.

It is **off by default**.

## Turn it on

In the KDE or GTK client open **Settings › Notifications** and switch on
**Keep recent iPhone notifications** (the tab's **Open Notification
Settings** button leads there), or run:

```bash
blueferry features on notification_history
```

Both apply after the background service restarts; the settings page offers
**Restart Service**. `BLUEFERRY_NOTIFICATION_HISTORY=true` in
`~/.config/blueferry/local.env` does the same.

Other apps' notifications only reach BlueFerry with **All iPhone
Notifications** under **Show Popups**.

## Use it

The **Notifications** tab lists the newest first, with the app, the time
and, if allowed, title and text; the terminal client shows the same list
under **iPhone Notifications** in its command palette, and the Quickshell
panel under **iPhone settings › Notifications** with **Show Recent
Notifications** (see [Clients](clients.md)). From a terminal:

```bash
blueferry notifications recent             # the last 20
blueferry notifications recent --limit 100
blueferry notifications recent --json      # raw list, including the keys
```

Entries the iPhone has removed (dismissed, opened or cleared there) are
marked **Removed on iPhone**; they stay in the list.

## Pin notifications

Pin a notification to keep it: pinned notifications are listed first (the
most recently pinned on top), stay until you unpin them, and survive a
restart of the background service — also after the iPhone removed them or
they dropped out of the last 200. In the clients, use **Pin** / **Unpin** on
an entry. In the KDE client the pin button appears when you point at an
entry or move the keyboard focus to it; a pinned entry keeps its **Unpin**
button visible and shows a pin on its icon. Entries removed on the iPhone and
entries of [muted apps](notification-mute.md) are greyed out there. When 20
are pinned, **Pin** is disabled and the tab says so; a failed pin or unpin is
explained above the list. In the GTK client it is the pin button at the end
of each entry; pinned entries show a pin icon, and the button is greyed out,
with the reason in its tooltip, once 20 are pinned. In the terminal client's
**iPhone Notifications**, `P` pins and unpins. In the Quickshell panel's
**Show Recent Notifications** each entry has **Pin** / **Unpin**; removed
entries and those of muted apps are greyed out, and at 20 pins only
**Unpin** stays available. From a terminal, use the key shown at the end of
each line:

```bash
blueferry notifications pin KEY
blueferry notifications unpin KEY
```

- At most 20 notifications can be pinned, and their saved text is limited
  to 64 KiB in total. When either is reached, unpin one first.
- A pinned notification is a snapshot: it is no longer linked to the
  notification on the iPhone after a reconnect or restart.
- An unpinned notification is forgotten once it leaves the recent list.

## What is kept

- The last 200 notifications, in memory only. Apart from pinned
  notifications the list starts empty after every restart of the background
  service. **Clear History** empties it, pinned notifications included.
- Titles, subtitles and text only while **Show message content in popups**
  is on; otherwise just the app and the time.
- A notification the iPhone updates replaces its entry and moves to the top
  of the list, like a new one.
- It is a recent history, not a mirror of the iPhone's Notification Center:
  notifications you dismiss on the iPhone stay in the list, marked
  **Removed on iPhone**.
- Pinned notifications are saved in BlueFerry's settings file, protected
  like the message history: encrypted with the desktop keyring by default,
  never saved with **Do not retain local data** (they are then kept in memory
  only), and readable only while local data is unlocked. Each one keeps
  exactly what the list showed; if **Show message content in popups** is
  off later, their saved text is removed too. Changing the local data
  policy unpins everything.
- While encrypted local data is locked, the list and pinning are refused.
- With the list switched off, saved pinned notifications are deleted when
  the background service starts, so nothing is retained.

## Privacy

The list is only handed out through BlueFerry's authenticated, rate-limited
D-Bus methods; the change signal carries no content. The clients read it only
while their list is shown and forget it when it is hidden. Nothing about these
notifications is stored in BlueFerry's history, which keeps only Messages;
only pinned notifications are saved, as described above.
The KDE client fetches the list only while its tab is visible and clears its
copy when the tab is hidden, the service becomes unavailable, or access is
revoked. Turning off content also clears any text already shown in the client
when that setting takes effect after the service restarts.
