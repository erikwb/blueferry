# D-Bus API

The backend exposes a private API on the **session** bus. Every client,
including the CLI, uses it.

| | |
| --- | --- |
| Bus name | `io.weirdware.BlueFerry` |
| Object path | `/io/weirdware/BlueFerry` |
| Interfaces | `io.weirdware.BlueFerry.Messages1`, `io.weirdware.BlueFerry.Events1`, `io.weirdware.BlueFerry.Presence1`, `io.weirdware.BlueFerry.CallHistory1`, `io.weirdware.BlueFerry.Media1`, `io.weirdware.BlueFerry.Calls1` (optional calls) |
| Errors | `io.weirdware.BlueFerry.Error.*` |

> **Note:** The canonical contract is
> [`data/io.weirdware.BlueFerry.xml`](https://github.com/erikwb/blueferry/blob/main/data/io.weirdware.BlueFerry.xml).
> Identifiers live in `src/blueferry/protocol.py`, the implementation in
> `src/blueferry/dbus_service.py`. `tests/test_dbus_contract.py` checks that
> the XML matches the service. If this page and the XML disagree, the XML
> wins.

## Design rules

- **Commands and snapshots** go through `Messages1`. Most results are JSON
  strings, decoded by `client_wire` into typed `models`. Unknown fields are
  kept for forward compatibility.
- **Live updates** go through `Events1` and are content-free. A signal only
  tells clients to fetch a fresh snapshot. Message records, sender
  identities, notification fields, contacts, and connection details are never
  broadcast.
- **Caller checks:** every method checks that the caller runs as the same
  user as the backend and applies per-connection and daemon-wide rate limits,
  with separate limits for sends, contact sync, storage unlock, destructive
  operations, reads, and status.
- **Bounded responses:** `ListThreads` fits an 8 MiB budget, keeps at most 500
  messages per thread, and sets `messages_truncated` when it trims.

## Compatibility

`GetStatus` returns `api_version`, the messaging compatibility generation.
It is currently **2** (roster-bound group replies) and independent of the
package version.

- Additive, compatible changes, such as new `GetStatus` keys, keep the
  generation.
- Incompatible changes need a new interface suffix (`Messages2`), never a
  silent contract change.
- Clients check the generation before any read or write and ask the user to
  update when it differs.

`GetStatus` also returns `_build_id`. Clients compare it with the installed
package to restart an outdated backend after upgrades.

## Messages1 methods

### Sending

| Method | Arguments → result | Notes |
| --- | --- | --- |
| `Send` | `s recipient, s body` → `s transfer_path` | New message to a phone number or email address |
| `SendToThreadChecked` | `s thread_key, s body, b confirm_group, s expected_group_token` → `s transfer_path` | Reply to a thread. Group replies must pass the roster token the client displayed |
| `SendToThread` | `s thread_key, s body, b confirm_group` → `s transfer_path` | Legacy; direct threads only |

### History and conversations

| Method | Arguments → result |
| --- | --- |
| `ListThreads` | `u limit` → `s json` |
| `ListEvents` | `as kinds, u limit` → `s json` |
| `ListRecent` | `s folder, u limit` → `s json` (reads directly from the iPhone over MAP) |
| `MarkThreadRead` | `s thread_key` → `u updated` |
| `SetThreadStarred` | `s thread_key, b starred` → `b starred_state` |
| `SetGroupParticipants` | `s thread_key, as recipients` → `s thread_json` |
| `DeleteThreads` | `as thread_keys, b confirmed` → `u deleted_conversations` |
| `ClearHistory` | `b confirmed` |

### Contacts

| Method | Arguments → result |
| --- | --- |
| `FindContacts` | `s query` → `s json` |
| `ListContacts` | `u offset, u limit` → `s json` |
| `SyncContacts` | → `u cached_count` |

### Status and settings

| Method | Arguments → result |
| --- | --- |
| `GetStatus` | → `s json` |
| `IsHealthy` | → `b healthy` |
| `GetNotificationPolicy` / `SetNotificationPolicy` | `s policy`: `messages`, `all`, or `none` |
| `GetContactsOnlyNotifications` / `SetContactsOnlyNotifications` | `b enabled` |
| `GetAncsNotificationActions` / `SetAncsNotificationActions` | `b enabled` (saved opt-in for iPhone action buttons) |
| `SetPhoneBatteryWarning` | `b enabled` → `b selected`; state is `phone_battery_warning` in `GetStatus` |
| `GetStoragePolicy` / `SetStoragePolicy` | `s policy`: `encrypted`, `plaintext`, or `none`; `Set` returns `s status_json` |
| `UnlockStorage` | → `s status_json` |
| `OpenLegacyGtkMessage` | `s handle, s application_owner` → `b delivered` (upgrade compatibility) |

## CallHistory1 methods

The opt-in mirror of the iPhone's recent calls. Whether it is on is reported
by `Messages1.GetStatus` (`call_history_enabled`,
`missed_call_notifications`); while it is off, `ListCallHistory` and
`SyncCallHistory` return `NotReady`.

| Method | Arguments → result | Notes |
| --- | --- | --- |
| `ListCallHistory` | `u limit` → `s json` | Newest first; each entry has `direction` (`missed`, `incoming`, `outgoing`), `timestamp`, `address`, `name`, `contact_name`. Private: callers and times |
| `SyncCallHistory` | → `u retained_count` | Pulls all three call lists from the iPhone now; `CallHistorySyncFailed` on a PBAP failure |
| `SetCallHistory` | `b enabled, b missed_call_notifications` → `s status_json` | Saves the opt-in; `enabled=false` erases the retained calls at once |
## Presence1 methods

Desktop-presence controls that are not messaging. Their state is reported
through `Messages1.GetStatus` (`proximity_lock*` keys).

| Method | Arguments → result |
| --- | --- |
| `SetProximityLock` | `b enabled, u grace_seconds` → `s status_json` |

## Media1 methods

Opt-in iPhone media control over Apple Media Service. `GetStatus` reports
`media_control_enabled` and `media_control_available`; clients show media
settings only when those keys exist, so they never call `Media1` on an older
backend. Media calls have their own rate-limit buckets.

| Method | Arguments → result | Notes |
| --- | --- | --- |
| `GetNowPlaying` | → `s json` | `enabled`, `available`, `detail` (`disabled`, `requires-notification-access-mode`, `le-link-state-unknown`, `waiting-for-iphone`, `ready`), plus `player`, `queue`, `track` and `supported_commands` while available |
| `SetMediaControl` | `b enabled` → `s status_json` | Saves the opt-in and starts or stops media control at once |
| `SetMprisPlayer` | `b enabled` → `s status_json` | Saves the separate MPRIS opt-in (`media_mpris_enabled`, `media_mpris_active`); the player exists only while media control is on and uses its own private connection |
| `SendMediaCommand` | `s command` | `play`, `pause`, `toggle`, `next`, `previous`, `volume-up`, `volume-down`, `repeat`, `shuffle`, `skip-forward`, `skip-backward`, `like`, `dislike`, `bookmark`; only commands the iPhone currently offers are sent |

## Calls1 methods (optional phone calls)

`Calls1` is always exported, because it also carries the opt-in. While calls
are off, every method except `SetCallsEnabled` fails with `CallsDisabled`, and
`GetStatus` reports only `calls_enabled: false`. With calls on, `GetStatus`
adds `calls_state` (`unavailable`, `searching`, `connecting`, `ready`,
`bluez_conflict`) and `calls_available`. See the
[phone calls guide](../user/calls.md).

| Method | Arguments → result | Notes |
| --- | --- | --- |
| `SetCallsEnabled` | `b enabled` → `s status_json` | Saves the opt-in and applies it at once; returns the `calls_*` status keys. "settings" rate limit |
| `ListCalls` | → `s json` | Current calls with caller number and contact name; treat as private |
| `Dial` | `s number` → `s call_id` | Plain numbers only; `*`/`#` and emergency numbers are refused. 6 per minute, 60 per hour |
| `Answer` | `s call_id` | A waiting call holds the active one. 10 per minute |
| `Hangup` | `s call_id` | Hangs up, or declines a ringing call |
| `HangupAll` | | |
| `SendTones` | `s call_id, s tones` | DTMF on the active call: `0-9`, `*`, `#` |
| `SwapCalls` | | Swap active and held |
| `HoldAndAnswer` | | Hold the active call and answer the waiting one. 10 per minute |

## Events1 signals

| Signal | Arguments | Meaning |
| --- | --- | --- |
| `HistoryChanged` | `a{sv} revision` | History changed; only a daemon-local revision is sent |
| `StatusChanged` | none | Fetch `GetStatus` again |
| `OpenMessageRequested` | `s handle` | A notification was clicked; the handle is a bounded, opaque MAP handle |
| `CallHistoryChanged` | none | Retained call history changed; call `ListCallHistory` again |
| `NowPlayingChanged` | none | Fetch `Media1.GetNowPlaying` again |
| `CallsChanged` | none | Optional calls changed; fetch `Calls1.ListCalls`. Never emitted while calls are off |

## Errors

Expected errors use stable names under `io.weirdware.BlueFerry.Error`, with
bounded messages:

`AuthorizationRequired`, `RateLimited`, `InvalidArgs`, `NotFound`,
`NotReady`, `ConfirmationRequired`, `SendFailed`, `SendOutcomeUnknown`,
`ResponseTooLarge`, `QueryFailed`, `ContactSyncFailed`, `CallHistorySyncFailed`,
`MediaCommandFailed`, and for the optional calls `CallsDisabled`,
`CallsUnavailable`, `CallFailed`

The XML lists which errors each method can return. Unexpected exceptions and
OBEX details stay in the backend log.

## Try it

```bash
busctl --user introspect io.weirdware.BlueFerry /io/weirdware/BlueFerry
busctl --user call io.weirdware.BlueFerry /io/weirdware/BlueFerry \
  io.weirdware.BlueFerry.Messages1 GetStatus
```

`GetStatus` contains no message content, but `ListThreads` and the contact
methods do. Treat their output as private.

## Changing the API

Update the XML, `dbus_service.py`, and the typed client models in the same
change, keep `Events1` content-free, and add new `GetStatus` keys instead of
bumping `api_version` where possible. See the
[growth rules](https://github.com/erikwb/blueferry/blob/main/ARCHITECTURE.md#growth-rules).
