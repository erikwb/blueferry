# Mute apps

With **All iPhone Notifications** selected, every iPhone app gets a desktop
popup. Muting an app hides its popups and nothing else: the notification
still arrives on the iPhone, and nothing is sent to the phone.

## Mute and unmute

```bash
blueferry notifications apps                     # apps seen since the service started
blueferry notifications mute com.example.Chat    # hide its popups
blueferry notifications unmute com.example.Chat  # show them again
```

The list shows the apps that sent a notification since the background
service started, plus every muted app, with `muted` in front of the muted
ones. A change applies to the next notification; no restart is needed.
You can also mute an app before it ever sent a notification, by its bundle
ID. BlueFerry fetches app notifications only while **All iPhone Notifications** is
selected, so with another policy the list holds only muted apps.

The Quickshell panel shows the same list as **Muted apps** under **iPhone
settings › Notifications**, each app with **Mute** / **Unmute** (see
[Clients](clients.md)). The KDE, GTK and terminal clients get it in a
separate change; until then use the command line.

## What muting does and doesn't do

- Only the desktop popup is skipped. A muted Phone app also hides missed
  call popups for calls that came through its notification.
- Apple Messages cannot be muted here: its popups come from MAP. Use the
  contacts-only setting or the notification policy for messages.
- One-time code copying only reads messages, so it is not affected.
- At most 100 apps can be muted.

## Muting and the environment filter

`BLUEFERRY_ANCS_APP_ALLOWLIST` and `BLUEFERRY_ANCS_APP_BLOCKLIST` in
`local.env` stay a hard filter applied first (see
[Filter apps](privacy.md#filter-apps)). An app they exclude is never fetched
from the iPhone, so it never appears in the list and cannot be muted.
Muting is for the apps that pass the filter. When either variable is set,
the list says so, so you know why some apps never show up.

## Storage and privacy

- The list of seen apps (bundle ID, display name, when it last sent a
  notification) is kept in memory only, at most 64 apps, and is gone when
  the service stops. Nothing is written to disk when a notification
  arrives.
- Muted apps are saved with their display name as `ancs_muted_apps` in
  `~/.config/blueferry/settings.json`, under the same
  [storage mode](privacy.md#local-data) as starred conversations
  (encrypted by default). Changing the storage mode clears them.
- While the wallet is locked, or with **Do not retain local data**, no app
  is muted and muting is unavailable. BlueFerry never overwrites the saved
  list while it cannot read it.
- While `settings.json` itself can't be read, muting and unmuting are
  refused; repair or remove the file and try again. Apps muted before the
  file broke stay muted. If the service started while the file was broken,
  their popups show until the file reads again.
- The D-Bus signal `NotificationAppsChanged` carries no content; clients
  fetch the list with `ListNotificationApps`. `GetStatus` reports only the
  number of muted apps (`notification_muted_app_count`).

## Limits

- Muting has only been tested with simulated notifications, not yet on a
  real iPhone.
