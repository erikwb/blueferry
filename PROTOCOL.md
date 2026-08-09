# Bluetooth protocol findings

This document records the device behavior that BlueFerry relies on. It replaces
the original one-off experiment scripts with conclusions that are useful to
maintainers. These are empirical observations, not promises made by Apple.

Unless stated otherwise, the behavior was observed with an iPhone 16 Pro Max
running iOS 26.5, BlueZ 5.72 or newer, and Linux controllers supporting both
BR/EDR and LE. The complete MAP, PBAP, and ANCS combination has been exercised
with a MediaTek MT7922. Other iPhone, iOS, BlueZ, and controller versions need
independent verification.

## Pairing and iPhone permissions

iOS exposes accessory permissions according to the capabilities Linux presents
during pairing. The reliable setup has these properties:

- The adapter Class of Device is A/V Hands-Free: major class 4, minor class 8.
  `btmgmt class 4 8` produces a base class of `0x240408`; BlueZ may add service
  bits and report a value such as `0x7c0408`.
- A connectable, temporarily discoverable LE peripheral advertisement solicits
  ANCS service UUID `7905f431-b5ce-4e99-a40f-4b1e122d00d0` while pairing.
- The working advertisement also contains inert private/test manufacturer and
  service identifiers `0xffff` and `0x9999`, following the behavior established
  by ancs4linux. They do not claim an Apple or hardware-vendor identity.
- The adapter is powered, pairable, and discoverable while the user confirms
  the same passkey on Linux and the iPhone.

After pairing, the iPhone normally offers **Show Message Notifications** and
**Sync Contacts** in the computer's Bluetooth entry. These permissions are
independent. Some iOS versions do not show a separate system-notification
toggle; ANCS may still become available through the negotiated LE bond.

Without the relevant permission, MAP or PBAP can be visible at the SDP level
but reject an OBEX connection with `Forbidden`/`0x43`. BlueFerry therefore
treats a completed bond as only the beginning of setup and verifies the live
profiles before reporting success.

## MAP receive behavior

iOS exposes SMS and iMessage through the Bluetooth Message Access Profile. On
the tested phone they are indistinguishable on the wire:

- Both arrive as `Type: sms-gsm`.
- MAP Message Notification Service events appear within a few seconds and
  expose a BlueZ `org.bluez.obex.Message1` object.
- A full `Get()` produces a standard bMessage. Its originator vCard may carry a
  telephone number in `TEL` or an Apple-ID address in `EMAIL`.
- `ListMessages` puts the message text in the `Subject` property. It also
  supplies sender, timestamp, folder, type, and read state without requiring a
  full bMessage download.
- Changing `Message1.Read` is reflected on the phone. Conversely, opening a
  message on the phone produces a read-property update that can close the
  corresponding desktop popup.

MAP does not say whether a message used SMS or iMessage, and BlueFerry must not
infer the transport from `sms-gsm`. It also does not provide reactions, typing
state, iMessage read receipts, useful attachment payloads, or a stable Messages
conversation identifier in the observed format.

The iPhone's MAP sent folder was empty in testing, and sending did not produce
a useful outgoing MNS event. BlueFerry records successful local sends itself;
it cannot reconstruct a complete sent history from the phone.

## MAP send behavior

`org.bluez.obex.MessageAccess1.PushMessage` accepts a MAP 1.4 bMessage in
`telecom/msg/outbox`. The successful shape is:

1. An empty originator vCard outside `BENV`.
2. One or more recipient vCards inside `BENV`.
3. A UTF-8 `BBODY`/`MSG` payload.
4. `TYPE:SMS_GSM`, even for Apple-ID recipients and iMessage delivery.

Telephone recipients use the vCard `TEL` property. Apple-ID email recipients
use `EMAIL`. When the target is registered for iMessage, iOS chooses iMessage
and the sent bubble is blue; otherwise the phone chooses its available route.
Linux cannot force or verify that choice before sending.

A bMessage containing repeated recipient vCards was observed to enter an
existing iMessage group when its recipient set matched that group. It did not
fan out into separate one-to-one conversations. Because a wrong address set
could target the wrong people, BlueFerry permits this only for a backend-owned,
unambiguously resolved roster and asks for confirmation before its first reply.

Recipient text is structural input to the bMessage format. It must be
validated before interpolation so CR/LF or vCard delimiters cannot inject an
additional recipient. Message lines beginning with bMessage structural tokens
also require MAP byte-stuffing.

## Group metadata side channel

MAP delivers an incoming group message with only one sender and no participant
list or conversation ID. On the tested iOS release, the corresponding Apple
Messages ANCS notification supplies the missing display information:

- ANCS app ID is `com.apple.MobileSMS`.
- The notification body matches the MAP message body. ANCS bodies requested by
  BlueFerry are capped at 256 characters, so a longer MAP body can match that
  exact prefix.
- For the observed unnamed group, the title is the sender and the subtitle is
  shaped like `To you & participant` (with further names separated by commas
  or ampersands).

BlueFerry correlates these records only within a bounded time window, refuses
ambiguous repeated-body matches, and requires every name to resolve to exactly
one address before enabling a reply. This is a conservative observation-based
heuristic, not a general iMessage group protocol. Named groups and alternative
iOS notification formats remain uncharacterized.

## PBAP behavior

The iPhone phonebook is available through BlueZ's
`org.bluez.obex.PhonebookAccess1` after **Sync Contacts** is enabled.

- Select the main phonebook with `Select("int", "pb")`; this is not MAP's
  `SetFolder` API.
- Pull it with `PullAll(path, {"MaxCount": ..., "Format": "vcard30"})`.
  `MaxListCount` is a MAP option and causes a PBAP transfer to fail or yield a
  zero-byte file.
- vCards contain `FN`, multiple `TEL` values, and `EMAIL` values. Retaining
  emails is necessary for contacts that exist only as Apple-ID destinations.
- Small or fast BlueZ transfers can disappear from D-Bus with status `gone`
  just before the output file becomes visible. Preserve the initial transfer
  properties and allow a short, bounded file-visibility grace period.

Contact names are display data, not identities. Phone numbers and email
addresses are normalized and stored separately; a name that resolves to more
than one address must remain ambiguous.

## OBEX lifecycle

iOS and obexd behave poorly when MAP/PBAP sessions or operations are repeatedly
created, destroyed, or overlapped:

- Keep one MAP and one PBAP session open for the daemon lifetime.
- Serialize blocking MAP/PBAP operations on one worker.
- Before opening, remove only stale sessions for the target phone and profile.
- On `Forbidden`, retry once after targeted stale-session cleanup rather than
  restarting all of obexd.
- Treat disappearance of a session object or the `org.bluez.obex` bus owner as
  connection loss and reconnect with bounded backoff.
- A transfer object can disappear after successful completion. `complete` and
  a disappearance after observable progress are successful terminal outcomes;
  explicit `error`, a timeout, or a missing output file is not.

This replaced the early experimental workaround of restarting the user's
entire `obex.service` before each operation, which disrupted the daemon's MAP
listener and could stop incoming messages.

## ANCS and the dual bearer

ANCS is GATT over LE, while MAP and PBAP use BR/EDR. An early experiment paired
with the ANCS solicitation advertisement active but saw only the BR/EDR
profiles, leading to the provisional conclusion that one adapter could not
carry both. That conclusion was incomplete.

With BlueZ's experimental API enabled, the same bond can carry both transports.
Pairing only creates the bond; it does not guarantee a live connection. The
reliable sequence is to connect `org.bluez.Bearer.BREDR1` first, wait until its
`Connected` property is true, and then connect `org.bluez.Bearer.LE1`. BlueFerry
supervises both connections for the daemon lifetime and repeats the sequence
after disconnects and system resume. It does not set `Device1.PreferredBearer`:
BlueZ only applies that property while disconnected, and preferring LE removes
the device from the normal auto-connect list.

After LE connects, BlueFerry waits for BlueZ to enumerate the ANCS service and
its Notification Source, Data Source, and Control Point characteristics. A
`StartNotify` call can race GATT readiness even after the characteristics have
appeared, so subscription failures are retried without requiring rediscovery.

This works on the MediaTek MT7922 while MAP and PBAP stay connected over BR/EDR.
BlueFerry's Arch package enables the necessary bluetoothd experimental API. The
requirement is based on controller capabilities, not an Intel vendor check.

ANCS responses have no outer total-length field and may arrive fragmented.
Control Point requests must be serialized and reassembled according to the
requested attribute sequence. The iPhone can replay existing notifications
after a reconnect, so startup/reconnect delivery needs deduplication without
suppressing genuine modifications.

Apple Messages also appears through ANCS. BlueFerry retains that copy for group
correlation but suppresses its desktop popup because MAP already provides the
message and read-state synchronization. Other ANCS application notifications
are transient display-only events; ANCS provides no general reply mechanism.
BlueFerry first requests only the owning app identifier. Unless live mirroring
of all notifications is enabled, it does not request unrelated notification
content at all. Even when mirroring is enabled, non-Messages content is never
written to history or placed on BlueFerry's D-Bus event feed.

## Historical HFP result

HFP calling is not part of BlueFerry, but the experiment produced one useful
independent result: an iPhone exposed call control through oFono's
`VoiceCallManager`, with caller ID, answer/hangup, mSBC SCO audio, and three of
three successful outgoing dials. The working Linux arrangement used oFono for
HFP control and PipeWire for audio with
`bluez5.hfphsp-backend = "ofono"`.

oFono and PipeWire's native HFP backend race to register the same BlueZ
profile, making startup ordering and distribution integration fragile. That
complexity, dependency burden, and the project's messaging focus are why the
feature was removed despite protocol feasibility.

## Maintenance rule

When behavior changes, record the phone model, iOS version, BlueZ version, and
controller, and distinguish a captured observation from an inference. Never
turn a real-device experiment into an automated test: the test suite must not
connect to a paired phone, read private device data, change pairing state, or
send a message.
