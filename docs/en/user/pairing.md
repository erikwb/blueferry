# Pair an iPhone

Start the client that fits your desktop:

```bash
blueferry-gtk         # GNOME, Cinnamon, and similar desktops
blueferry-qt          # KDE Plasma
blueferry-quickshell  # Quickshell
```

The terminal wizard `blueferry pair-setup` offers the same flow.

## Steps

1. On the iPhone, keep **Settings → Bluetooth** open and the phone unlocked.
2. Let BlueFerry check your Bluetooth controller.
3. In BlueFerry, choose **Scan**, select the iPhone, and choose **Pair**.
   BlueFerry starts the request; you don't need to tap the computer under
   **Other Devices** on the phone.
4. When the request appears on the iPhone, approve it and check that both
   devices show the same code. It can take around 15 seconds to appear.
5. On the iPhone, tap **ⓘ** next to the computer and turn on the switches
   below. If iOS asks to allow system notifications, approve that too.
6. Wait until Messages and Contacts show as connected in BlueFerry. With the
   default encrypted storage, approve the desktop wallet prompt.

After setup, the backend starts automatically with your session and
reconnects after normal Bluetooth interruptions.

## The iPhone switches

The switches appear on the computer's **ⓘ** page in the iPhone's Bluetooth
settings. iOS shows them in your phone's language:

| iOS in English | iOS auf Deutsch | Enables in BlueFerry |
| --- | --- | --- |
| Show Message Notifications | Mitteilungen zu Nachrichten anzeigen | Messages (MAP) |
| Sync Contacts | Kontakte synchronisieren | Contacts (PBAP) |
| Share System Notifications | Systemmitteilungen teilen | iPhone notifications and group details (ANCS) |

- The switches can take a few minutes to appear. If they're missing, go back
  to the device list and reopen the **ⓘ** page a few times.
- If the phone lists the computer twice, check both entries; the switches can
  appear under either one.
- **Share System Notifications** is optional. Without it, ordinary messages
  and contacts still work, but a group message may look like a direct
  conversation with its sender.

## Pairing options

Most people should leave both options off.

- **Compatibility pairing for iOS 18 or earlier** keeps the signal that makes
  the Messages and Contacts switches appear, but doesn't connect iPhone
  system notifications. BlueFerry also picks this automatically when the
  local Bluetooth stack can't support them.
- **Use explicit Bluetooth pairing** asks BlueZ to pair immediately instead of
  connecting first. Try it only if normal pairing keeps getting canceled on
  your Bluetooth controller. Some Realtek adapters use it by default.

The terminal wizard has the same options:

```bash
blueferry pair-setup
blueferry pair-setup --compatibility-mode
blueferry pair-setup --explicit-pairing     # or --no-explicit-pairing
```

## Start over cleanly

Old Bluetooth state on the phone can survive when you remove the pairing on
only one side. Before you pair again, reset both sides:

1. On the iPhone: **Settings → Bluetooth → ⓘ** next to the computer →
   **Forget This Device** (German: **Dieses Gerät ignorieren**). Do this for
   every entry with the computer's name.
2. On Linux: remove the iPhone in BlueFerry, or run
   `bluetoothctl remove <iPhone address>`.
3. Pair again as described above.

If pairing fails, BlueFerry saves a scrubbed report. See
[Troubleshooting](troubleshooting.md#report-a-problem).
