# Privacy and storage

BlueFerry runs entirely on your computer. It talks directly to your iPhone
over Bluetooth and sends nothing to a server.

## Notifications

Choose one of three notification modes in the client's iPhone page:

| Mode | Desktop popups |
| --- | --- |
| Messages only (default) | New messages |
| All iPhone notifications | New messages and notifications from other iPhone apps |
| None | No popups |

Notifications from other apps are shown and then discarded. They're never
added to message history and never broadcast to other programs. Messages
that arrive through both MAP and ANCS appear only once. The ANCS copy of an
Apple Messages notification (title, subtitle and up to the first 1024 bytes of
the text) is kept in local history under the storage mode below, because it
carries the group details that MAP lacks.

By default, dismissing a message popup also marks the message read on the
iPhone. Some do-not-disturb or "block" actions in notification centers
dismiss popups instead of hiding them, which would mark messages read without
you seeing them. Set `BLUEFERRY_MARK_READ_ON_DISMISS=false` to turn that off.

### Filter apps

With **All iPhone notifications**, you can limit which apps create popups by
their exact, case-sensitive bundle ID:

```bash
# Allow every app except these:
BLUEFERRY_ANCS_APP_BLOCKLIST=com.example.Chat,com.example.Mail

# Or allow only these:
# BLUEFERRY_ANCS_APP_ALLOWLIST=com.example.Calendar,com.example.Reminders
```

The blocklist wins if both are set. An empty allowlist blocks every app
except Messages. To find bundle IDs, watch the log while the app sends a
notification. BlueFerry logs each app once, without content:

```bash
journalctl --user -u blueferry -f | grep "ANCS app observed"
```

## Local data

BlueFerry keeps message history and a contact cache so conversations survive
restarts. You choose how:

| Storage mode | Behavior |
| --- | --- |
| Encrypted (default) | Encrypted with a random key kept in GNOME Keyring or KDE Wallet |
| Unencrypted | Stored without encryption |
| Do not retain local data | History and contacts are not written to disk |

- If the wallet is locked, live messages still arrive. Stored history and
  contact names wait until you unlock it (in the client, or with
  `blueferry storage-unlock`).
- Changing the storage mode clears the existing cache, so encrypted and
  unencrypted records are never mixed.
- Starred conversations, saved group members, and group confirmations follow
  the same storage mode.
- `blueferry history-clear` deletes local message history.

Encryption protects data at rest. Other programs running as your user can
still use the BlueFerry D-Bus API and, while the wallet is unlocked, may be
able to read its secrets.

## Files and folders

| Path | Content |
| --- | --- |
| `~/.config/blueferry/` | Configuration (`local.env`) and preferences (`settings.json`) |
| `~/.local/state/blueferry/` | Message history, contact cache, scrubbed pairing reports |

Uninstalling the packages doesn't remove either folder.

## Settings in local.env

Edit `~/.config/blueferry/local.env`, then restart the service with
`systemctl --user restart blueferry`.

| Setting | Default | Meaning |
| --- | --- | --- |
| `BLUEFERRY_SHOW_NOTIFICATION_CONTENT` | `true` | Show message text in popups |
| `BLUEFERRY_NOTIFICATION_TIMEOUT_MS` | `8000` | Popup timeout (1000–60000) |
| `BLUEFERRY_MARK_READ_ON_DISMISS` | `true` | Dismissing a popup marks the message read on the iPhone |
| `BLUEFERRY_HISTORY_RETENTION_DAYS` | `30` | Days of history to keep (1–3650) |
| `BLUEFERRY_HISTORY_MAX_EVENTS` | `10000` | Maximum stored events (100–1000000) |
| `BLUEFERRY_HISTORY_MAX_PAYLOAD_BYTES` | `268435456` | Maximum stored data in bytes (16 MiB–2 GiB) |
| `BLUEFERRY_KEEP_PHONE_AUDIO_ON_PHONE` | `true` | Keep calls and music on the iPhone |
| `BLUEFERRY_ANCS_APP_ALLOWLIST` | unset | See [Filter apps](#filter-apps) |
| `BLUEFERRY_ANCS_APP_BLOCKLIST` | unset | See [Filter apps](#filter-apps) |

Pairing writes the phone and adapter settings to the same file; change those
by pairing again rather than by hand.

## Calls and music stay on the iPhone

With WirePlumber 0.5 or newer, BlueFerry writes
`~/.config/wireplumber/wireplumber.conf.d/99-blueferry-keep-phone-audio.conf`
before pairing. It stops the computer from acting as a Bluetooth speaker or
headset for the iPhone, so calls and music stay on the phone. Set
`BLUEFERRY_KEEP_PHONE_AUDIO_ON_PHONE=false` to remove that file.
