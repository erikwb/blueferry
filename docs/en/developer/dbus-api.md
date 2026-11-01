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
  operations, settings, notification clicks, reads, and status.
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

When a phone is configured, `GetStatus` returns `phone_profile_id`: a random,
stable UUIDv4 for that local pairing profile. It contains no Bluetooth address
or contact data. Clients can use it to scope their own per-phone metadata. The
key is `null` when no locally paired phone is selected; environment overrides
for a different phone do not reuse the saved profile ID. Forgetting the phone
clears the ID, and configuring a different phone creates a new one. Older
profiles receive an ID on their first status read. During a target switch, the
old daemon returns `null` rather than labeling its data with the new phone's
ID. Migration also returns `null` while pairing configuration is being written
and retries on a later status read.

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
| `ListContactCards` | `u offset, u limit, s query` → `s json`: `{"contacts": [{"id", "name", "organization", "primary", "phone_count", "email_count", "address_count", "has_photo"}], "total", "offset", "query"}` in display-name order, at most 150 per page. `query` (may be empty) matches name, organization, title, email and postal-address text ignoring case, accents and Unicode forms (`Muller` finds `Müller`), and phone numbers by their digits (three or more). `total` counts all matches. Ids stay valid while a card is unchanged within one daemon run; `GetStatus` reports a content-free integer `contact_card_epoch` that changes on every daemon restart and contact reload, so a client holding ids reloads its list when it differs (an id that no longer resolves fails with `NotFound`). Rows carry no photo bytes; `has_photo` is false while contact photos are off. `GetStatus` reports `contact_card_count` (also the capability marker). Shape it with `blueferry.contacts_view` |
| `Search` | `s query, u limit` → `s json`: `{"query", "limit", "conversations", "messages", "contacts", "calls"}`, each group `{"total", "results", "available"}`, at most `limit` (1–50) results per group. Matching ignores case, accents and Unicode forms (`Muller` finds `Müller`); `match_start`/`match_length` refer to the original snippet text. Conversations (`thread_key`, `name`, `is_group`, `timestamp`) match by name or member, or by the digits of their name, members and recipients (three or more digits); messages (`thread_key`, `handle`, `thread_name`, `is_group`, `sender`, `outgoing`, `timestamp`, `snippet`, `match_start`, `match_length`) by text, newest first; contacts are `ListContactCards` rows; calls (`id`, `direction`, `timestamp`, `address`, `name`, `contact_name`) match by name or number digits, and `id` is the opaque one `ListCallHistory` reports. The daemon searches only what it holds decrypted in memory (recent and starred conversations, the contact cache, retained call history); nothing is indexed on disk. Matching runs off the main loop; while several searches are still queued, a new one fails with `NotReady`. `available` is false for contacts without a cache and for calls while call history is off or storage is locked. Queries shorter than two characters return empty groups. `GetStatus` reports `search_result_limit` (also the capability marker). Shape it with `blueferry.search_view` |
| `GetContact` | `s contact_id` → `s json, ay photo`: `{"id", "name", "organization", "title", "phones": [{"kind", "label", "value", "number"}], "emails": [{"kind", "label", "value"}], "addresses": [{"kind", "label", "po_box", "extended", "street", "locality", "region", "postal_code", "country"}], "has_photo", "photo_retry"}` plus the card's bounded JPEG/PNG photo (empty without one or while contact photos are off). `kind` is `mobile`, `iphone`, `home`, `work`, `main`, `home_fax`, `work_fax`, `other_fax`, `pager`, `other` or `custom` (then `label` holds the iPhone label); `value` is the number as stored on the phone, `number` its normalized form for messaging and dialling (empty when it has none). `photo_retry` means a contact sync held the photo store; ask again later. `NotFound` for an unknown or stale id. Bucket `contact-photo` |
| `SyncContacts` | → `u cached_count` |

### Status and settings

| Method | Arguments → result |
| --- | --- |
| `GetStatus` | → `s json` |
| `IsHealthy` | → `b healthy` |
| `ReconnectPhone` | → `s result`: `started`, `in-progress`, `connected`, `profile-reset` (the link was up but a profile was stuck, so the device was disconnected and connected again; `GetStatus`: `profile_reset_*`), `call-active` (a profile is stuck but a call is active, so nothing was reset), or `bluez-unresponsive`/`bluez-kernel` while bluetoothd does not answer D-Bus (`GetStatus`: `bluez_unresponsive`, `bluez_unresponsive_reason`). Clears the Classic reconnect backoff and pages the iPhone once; `GetStatus` reports the outcome in `phone_reconnect_state` (`connected`, `connecting`, `waiting`, `unreachable`), `phone_reconnect_paused` and `phone_reconnect_next_in_sec`. Own rate bucket (6 per minute) |
| `GetNotificationPolicy` / `SetNotificationPolicy` | `s policy`: `messages`, `all`, or `none` |
| `GetContactsOnlyNotifications` / `SetContactsOnlyNotifications` | `b enabled` |
| `GetAncsNotificationActions` / `SetAncsNotificationActions` | `b enabled` (saved opt-in for iPhone action buttons) |
| `SetAutoProfileReset` | `b enabled` → `b selected`; saved as `auto_profile_reset` in settings.json (wins over `BLUEFERRY_AUTO_PROFILE_RESET`), effective at once; state is `profile_reset_auto` in `GetStatus`. Does not affect the manual reset through `ReconnectPhone` |
| `SetPhoneBatteryWarning` | `b enabled` → `b selected`; state is `phone_battery_warning` in `GetStatus` |
| `ListNotifications` | `u limit` → `s json`: `enabled`, `content`, `pins_max`, `notifications` (pinned records first, most recently pinned on top, then the rest newest first; each: `key` (opaque string for pinning), `id` (ANCS UID, informational only), `app_id`, `app_name`, `time`, `pinned`, `removed` (the iPhone removed it), `actionable` (still a live notification of the current ANCS session: false once removed, after a session reset and for pins loaded at startup); `title`, `subtitle`, `body` only with notification content shown); empty unless `BLUEFERRY_NOTIFICATION_HISTORY` is on; `NotReady` while storage is locked; own rate bucket |
| `SetNotificationPinned` | `s key, b pinned` → `b pinned` (the new state; unpinning an unpinned record returns false). A pinned record stays listed until unpinned, also after removal on the iPhone, after it left the 200-record ring and across daemon restarts: pins are saved in `settings.json` (`pinned_notifications`, encrypted under the storage policy; memory only with `none`; content fields only if the record had them). `InvalidArgs` for an unknown key ("unknown notification key"), a full list (at most `pins_max` = 20, also `notification_pins_max` in `GetStatus`; "can be pinned") or the 64 KiB storage cap ("too long to store"); `NotReady` while the list is off or storage is locked. Emits `NotificationsChanged`. Own rate bucket `notifications-pin` (30/min, 300/h). With the list off, saved pins are deleted at daemon start; `ClearHistory` and storage policy changes delete them too |
| `GetFeatures` | → `s json`: per switch (`show_notification_content`, `mark_read_on_dismiss`, `notification_history`) `value`, `running`, `source` (`environment`, `settings`, `local.env`, `default`), `variable`, `restart_required` |
| `SetFeature` | `s name, b enabled` → `s result`: `active`, `restart-required` or `environment`; stored in settings.json, applied at the next daemon start; own rate bucket |
| `SetMirrorNotificationRemovals` | `b enabled` → `b selected` (saved opt-in: a removal on the iPhone closes the popup); state is `mirror_iphone_removals` in `GetStatus` |
| `SetContactPhotos` | `b enabled` → `s status_json`: saves the contact-photo opt-in in `settings.json` and applies it at once (off stops serving photos and queues erasure after any active contact sync; failed file and database deletions are retried); returns the `contact_photo*` status keys, including `contact_photo_cleanup_pending` (true until both file and database cleanup finish). `GetStatus` reports the current value as `contact_photos`; `saved_opt_ins: true` advertises the runtime setter |
| `SetOtpAutocopy` | `b enabled` → `s status_json`: saves the one-time code auto-copy opt-in in `settings.json` and applies it at once; returns `{"otp_autocopy": b}`. `GetStatus` reports the current value as `otp_autocopy`; `saved_opt_ins: true` advertises the runtime setter |
| `GetStoragePolicy` / `SetStoragePolicy` | `s policy`: `encrypted`, `plaintext`, or `none`; `Set` returns `s status_json` |
| `UnlockStorage` | → `s status_json` |
| `GetNotificationOpenMap` | → `s rules_json` (list of `bundle_id`, `target`, `kind`) |
| `SetNotificationOpenTarget` | `s bundle_id, s target` → `s rules_json` |
| `RemoveNotificationOpenTarget` | `s bundle_id` → `b removed` |
| `ListNotificationApps` | → `s apps_json`: `{"apps": [{"app_id", "app_name", "muted", "seen", "last_seen"}], "muted_count", "max_muted", "mute_available", "all_notifications", "env_allowlist", "env_blocklist"}`. `apps` joins the non-Messages apps seen since the daemon started (memory only, at most 64; `last_seen` is Unix seconds, `null` for a muted app not seen yet) with the muted apps saved in `settings.json`, sorted by name. `app_name` is empty while the iPhone has not reported one. `mute_available` is false while local storage is locked, disabled or unreadable (then nothing is muted). `env_allowlist`/`env_blocklist` say that `BLUEFERRY_ANCS_APP_ALLOWLIST`/`BLOCKLIST` are set; apps they exclude never appear. Shape it with `blueferry.notification_apps_view` |
| `SetNotificationAppMuted` | `s app_id, b muted` → `b selected`: hides (or shows again) the desktop popups of one app, effective with the next notification; `InvalidArgs` for a malformed ID, Messages, an app excluded by the environment rules or more than `max_muted` apps; `NotReady` while `mute_available` is false. Bucket `settings`. `GetStatus` reports `notification_muted_app_count` (also the capability marker) |
| `OpenNotificationClick` | `s click_id, s activation_token` → `b opened` (a notification shell ran a mapped popup's stored argv; the ID is random per popup and stays valid for 10 s after the user dismissed the popup) |
| `OpenLegacyGtkMessage` | `s handle, s application_owner` → `b delivered` (upgrade compatibility) |

## CallHistory1 methods

The opt-in mirror of the iPhone's recent calls. Whether it is on is reported
by `Messages1.GetStatus` (`call_history_enabled`,
`missed_call_notifications`); while it is off, `ListCallHistory` and
`SyncCallHistory` return `NotReady`.

| Method | Arguments → result | Notes |
| --- | --- | --- |
| `ListCallHistory` | `u limit` → `s json` | Newest first; each entry has `id` (opaque keyed hash, stable across syncs within one daemon run, carries no number), `direction` (`missed`, `incoming`, `outgoing`), `timestamp`, `address`, `name`, `contact_name`. Private: callers and times |
| `SyncCallHistory` | → `u retained_count` | Pulls all three call lists from the iPhone now; `CallHistorySyncFailed` on a PBAP failure |
| `SetCallHistory` | `b enabled, b missed_call_notifications` → `s status_json` | Saves the opt-in; `enabled=false` erases the retained calls at once |
`GetStatus` reports `notification_open_map_revision`, a content-free number
that changes whenever a click rule is added, changed or removed. Clients
reread the rules only when it changes, and a backend without the key does
not support click rules.

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
| `GetNowPlaying` | → `s json` | `enabled`, `available`, `detail` (`disabled`, `requires-notification-access-mode`, `le-link-state-unknown`, `waiting-for-iphone`, `ready`), plus `player`, `queue`, `track`, `supported_commands` and `supported_commands_known` (false while the phone has not sent its list; commands are then sent as asked) while available |
| `SetMediaControl` | `b enabled` → `s status_json` | Saves the opt-in and starts or stops media control at once |
| `SetMprisPlayer` | `b enabled` → `s status_json` | Saves the separate MPRIS opt-in (`media_mpris_enabled`, `media_mpris_active`); the player exists only while media control is on and uses its own private connection |
| `SendMediaCommand` | `s command` | `play`, `pause`, `toggle`, `next`, `previous`, `volume-up`, `volume-down`, `repeat`, `shuffle`, `skip-forward`, `skip-backward`, `like`, `dislike`, `bookmark`; only commands the iPhone currently offers are sent |
| `SetPhoneAudioRoute` | `s route` → `s accepted` | `pc` opens, `phone` closes the iPhone's A2DP stream; `NotReady` while unavailable or busy, `AudioRouteFailed` when BlueZ refuses; own rate-limit bucket |

**Volume.** There is no separate volume method: a client sends
`SendMediaCommand("volume-up")` or `("volume-down")`, one relative iPhone step
each (Apple Media Service has no absolute volume). The level is read-only,
`player.volume` in `GetNowPlaying` (0.0 to 1.0, `null` until the iPhone
reports it). Volume steps share the `media-command` rate-limit bucket (60 per
minute, 1200 per hour) with the other media commands, which is ample for
button presses. A step the current player does not advertise in
`supported_commands` is refused with `NotReady` ("the iPhone does not
currently offer this media command") and nothing is sent; while
`supported_commands_known` is false it is sent as asked. With media control
off or not connected the call also fails with `NotReady`; a refused GATT
write arrives as `MediaCommandFailed`. Clients derive the buttons from
`blueferry.media_volume_view.volume_controls(snapshot)`.

The phone-audio keys in `GetStatus` (independent of the media-control
opt-in; clients show the switch only when `phone_audio_route` exists):

- `phone_audio_route`: `pc` (the A2DP stream to this computer is open),
  `phone`, or `unavailable` with `phone_audio_reason`
  (`keep_phone_audio_on_phone`, `phone_disconnected`, `no_a2dp_source`,
  `unknown`).
- `phone_audio_pending`: the route being switched to, or empty.
- `phone_audio_playing`: where the sound plays right now. `pc` while the
  phone's `MediaTransport1.State` is `active`; otherwise `phone` while Apple
  Media Service reports playback, `none` while it reports a pause, and
  `unknown` without media control. A change to `pc` is reported at once,
  every other change only after it held for two seconds. `phone` together
  with route `pc` means the user picked another output on the iPhone.
- `phone_audio_delay_reporting`: `true` while the open A2DP stream negotiated
  AVDTP Delay Reporting (BlueZ exports `MediaTransport1.Delay`), `false` when
  the stream has no `Delay` (older adapter or BlueZ; video on the iPhone can
  run ahead of the sound), `null` while no stream is open.
- `phone_audio_delay_ms`: the delay BlueZ reports to the phone, in
  milliseconds (`MediaTransport1.Delay` is in 1/10 ms), or `null` without
  delay reporting. It follows `PropertiesChanged`; iOS shifts video by it.
  Both keys are reported while the transport exists, also when it is idle;
  the value is the last report. A `Delay` that BlueZ only invalidates keeps
  the last value (`null` with reporting `true` if none was seen yet).

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
| `SendTones` | `s call_id, s tones` | DTMF on the active call: `0-9`, `*`, `#`. Requests are played one after another in arrival order (at most 32 waiting); the log records only how many tones went out |
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
| `NotificationsChanged` | none | Fetch `Messages1.ListNotifications` again (opt-in list) |
| `NotificationAppsChanged` | none | The notification-app list changed (an app seen for the first time, a newly known name, a mute toggled, storage unlocked, the popup policy changed); call `ListNotificationApps` again |
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
