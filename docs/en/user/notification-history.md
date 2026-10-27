# Recent iPhone notifications

BlueFerry can keep a list of the iPhone's recent app notifications (other
than Messages) for the **Notifications** tab of the KDE and GTK clients
and the command line. The list lives only in the background service's memory and is never
written to disk.

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
under **iPhone Notifications** in its command palette. From a terminal:

```bash
blueferry notifications recent             # the last 20
blueferry notifications recent --limit 100
```

## What is kept

- The last 200 notifications, in memory only. The list starts empty after
  every restart of the background service, and **Clear History** empties it.
- Titles, subtitles and text only while **Show message content in popups**
  is on; otherwise just the app and the time.
- A notification the iPhone updates replaces its entry and moves to the top
  of the list, like a new one.
- It is a recent history, not a mirror of the iPhone's Notification Center:
  notifications you dismiss on the iPhone stay in the list.
- While encrypted local data is locked, the list is refused.

## Privacy

The list is only handed out through BlueFerry's authenticated, rate-limited
D-Bus method; the change signal carries no content. The clients read it only
while their list is shown and forget it when it is hidden. Nothing about these
notifications is stored in BlueFerry's history, which keeps only Messages.
