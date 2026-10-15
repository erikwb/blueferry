# Sync notifications with iPhone

With **All iPhone Notifications** selected, BlueFerry shows a desktop popup
for each iPhone app notification. Normally that popup stays until it times
out, even if you already cleared the notification on the phone. With this
opt-in setting, a notification that is removed or dismissed on the iPhone
also closes its popup on the desktop.

## Turn it on

Turn on **Sync notifications with iPhone** under *Desktop Notifications* in
the iPhone settings of the Qt client, or run:

```bash
blueferry notifications sync enable
blueferry notifications sync status
blueferry notifications sync disable
```

The choice is saved as `mirror_iphone_removals` in
`~/.config/blueferry/settings.json` and applies immediately. `GetStatus`
reports it as `mirror_iphone_removals`.

## What it does and doesn't do

- It only closes popups. It never sends anything to the iPhone, and closing
  a popup on the desktop never clears the notification on the phone.
- Only popups of iPhone app notifications are affected. Messages popups come
  from MAP and keep their own behaviour.
- Popups with [action buttons](notification-actions.md) already close when
  their notification is removed on the iPhone, with or without this setting.
- iOS numbers notifications per Bluetooth session and reuses the numbers.
  When the connection is re-established, or the iPhone replays its existing
  notifications, BlueFerry forgets which popup belonged to which number.
  Popups from before that point then simply time out.

## Limits

- Off by default.
- The GTK, Quickshell and terminal clients have no switch yet; use the CLI.
- It relies on iOS reporting the removal over ANCS (`NotificationRemoved`).
  The automated tests use simulated ANCS events; on a real iPhone it has only
  been in daily use on one developer setup, without a targeted test.

## Privacy

BlueFerry keeps only the notification number and the desktop popup id, in
memory, at most 64 entries. Nothing is logged, stored or broadcast.
