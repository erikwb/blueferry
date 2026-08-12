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
- No LE advertisement is active while Classic pairing is in flight. An
  unbonded ANCS solicitation advert lets iOS connect the LE peripheral as a
  separate accessory and keep two device records for one computer (observed
  on an Intel AX210 with BlueZ 5.87 and iOS 26.5). The connectable,
  temporarily discoverable advert soliciting ANCS UUID
  `7905f431-b5ce-4e99-a40f-4b1e122d00d0` is registered only after the bond
  exists and the Classic bearer is connected.
- The working advertisement also contains inert private/test manufacturer and
  service identifiers `0xffff` and `0x9999`, following the behavior established
  by ancs4linux. They do not claim an Apple or hardware-vendor identity.
- The adapter is powered, pairable, and discoverable while the user confirms
  the same passkey on Linux and the iPhone.
- Which side initiates the *authentication* decides whether one pairing
  covers both transports: the authentication initiator derives the LE keys
  over SMP-on-BR/EDR after encryption, and only the link central can. The
  iPhone role-switches itself to central of the ACL in every observed
  connection. A Linux-initiated `Device1.Pair()` therefore produced a
  BR/EDR-only bond on an Intel AX210 — Linux initiated authentication but
  was no longer central, the iPhone was central but had not initiated, and a
  btmon capture shows neither side opening the SMP channel despite both
  advertising it and the link key being Secure Connections (`Type=8`). The
  same `Pair()` produced a dual bond on a MediaTek MT7922.
- The sequence that produces the dual bond on every tested controller is to
  register the pairing agent and then call `Device1.Connect()` on the
  *unpaired* device: iOS refuses the profile connection without security,
  initiates the pairing itself over the existing ACL, and — as central and
  authentication initiator — runs cross-transport derivation. A btmon
  capture shows `BR/EDR SMP: Pairing Request/Response`, IRK exchange, and a
  new LTK immediately after encryption; `Bearer.LE1` appears with
  `LE.Paired/Bonded` about a second later and the iPhone keeps a single
  device record for the computer. The user confirms one numeric comparison
  on both screens; the adapter never needs to become discoverable.

There are two reliable client transactions, depending on who owns the pairing
UI. On a desktop running KDE BlueDevil, GNOME Shell, Cinnamon, or Blueman,
BlueFerry leaves confirmation and its surrounding connection lifecycle to that
desktop manager and starts pairing with `Device1.Pair()`. Registering
BlueFerry's agent and issuing the unpaired `Device1.Connect()` at the same time
races the desktop manager; on KDE/BlueDevil this produced
`le-connection-abort-by-local`. In a session without an interactive Bluetooth
manager, BlueFerry registers its `DisplayYesNo` agent and uses the unpaired
`Device1.Connect()` transaction described above. This is the path that produces
the dual bond reliably on the Intel AX210 and supplies confirmation UI on
minimal desktops.

BlueZ exposes neither its registered-agent list nor the identity of its default
agent. Clients therefore select between these transactions using a deliberately
narrow session-bus heuristic: known interactive manager names are recognized,
and KDE counts only when the `bluedevil` module is loaded in `kded5` or `kded6`.
Generic headless agents such as a `NoInputNoOutput` `bt-agent` do not count;
they cannot display numeric comparison and must not suppress BlueFerry's agent.
This choice lives in the shared setup implementation, so the GTK, Qt, and
Quickshell clients all use the same transaction on a given desktop.

After pairing, the iPhone offers **Show Message Notifications** and
**Sync Contacts** in the computer's Bluetooth entry. These permissions are
independent, and three behaviors were observed on iOS 26.5:

- The toggles can take minutes to appear, and appeared only while the ANCS
  solicitation advert was actively broadcasting. MAP connection attempts
  alone (OBEX `Forbidden`/`0x43` responses) did not surface them.
- When iOS holds two records for the computer, the toggles can appear under
  either record. Users must check both entries.
- Closing and reopening the entry's detail page refreshes iOS's view.

An iOS 18 test did not follow the same post-pair behavior: an outbound LE
connection hung and ended in `le-connection-abort-by-local`, while MAP/PBAP
worked when attempted first. BlueZ still reported its LE bearer as paired and
bonded, so those properties cannot predict whether the phone will answer.

The experimental pairing flow therefore keeps the ANCS solicitation
advertisement active, activates obexd's local Message Notification Server
before pairing, and makes MAP/PBAP the first post-pair profile attempt. LE is
enabled after that attempt completes, whether it succeeds or fails. Before
each connection request the daemon explicitly selects the corresponding BlueZ
`PreferredBearer`; otherwise the pairing-time BR/EDR preference prevents the
later LE request from completing.

On a clean iOS 26 test this ordering opened MAP/PBAP first; the pending LE
request completed three seconds later, resolved all three ANCS characteristics,
and presented the system-notification consent prompt. The first Control Point
request returned `NotPermitted`; the retry five seconds later succeeded after
the prompt was approved. MAP, PBAP, and authorized ANCS then shared one
iPhone-side record. Failed or partial pairing attempts can still leave two
same-named records on the phone, so both must be removed before another clean
test.

Starting notifications on the ANCS Notification Source and Data Source proves
only that the GATT subscriptions exist. It does not prove that iOS authorized
notification contents. During setup BlueFerry therefore sends a minimal
Control Point request for the Messages app's display name and reports ANCS
ready only after the corresponding Data Source response arrives. This probe
contains no notification identifier or notification content. A rejected or
interrupted probe is retried while MAP/PBAP remains available.

Without the relevant permission, MAP or PBAP can be visible at the SDP level
but reject an OBEX connection with `Forbidden`/`0x43`; that state resolves
once the toggle is enabled and is not a reason to re-pair. A MAP `Connection
refused (111)` at the transport level is a different, unrecoverable state
observed with stale or incomplete bonds. BlueFerry therefore treats a
completed bond as only the beginning of setup and verifies the live profiles
before reporting success.

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
- For the observed named group, the title is the sender and the subtitle is the
  group name. This identifies the conversation but contains no member roster.

BlueFerry correlates these records only within a bounded time window, refuses
ambiguous repeated-body matches, and requires every name to resolve to exactly
one address before enabling an unnamed-group reply. Named groups remain
read-only until the user supplies a recipient roster. The route is invalidated
when a previously unseen sender appears, but iOS provides no event when a
silent member is added or removed, so the user must maintain that roster. This
is a conservative observation-based heuristic, not a general iMessage group
protocol; alternative iOS notification formats remain uncharacterized.

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

An iPhone permits only one computer to hold its MAP session at a time. If MAP
is already in use by another computer, iOS rejects `CreateSession(MAP)` with
`Connection refused (111)`. This is not a pairing or authorization failure:
keep the daemon available, expose the refusal to clients, and continue polling
so Blueferry connects when the competing session is released.

The saved target MAC is also the MAP trust boundary. Discovery or a Classic
bond alone must never put a phone into Blueferry's configuration. The pairing
flow persists the MAC and starts the daemon only after it has successfully
connected the same bond over LE for ANCS. If ANCS/LE setup is incomplete, the
phone may remain paired in BlueZ, but Blueferry does not persist that target
MAC and therefore cannot attempt a MAP session to it.

- Keep one MAP and one PBAP session open for the daemon lifetime.
- Serialize blocking MAP/PBAP operations on one worker.
- Before opening, remove only stale sessions for the target phone and profile.
- On `Forbidden`, retry once after targeted stale-session cleanup rather than
  restarting all of obexd.
- Treat disappearance of a session object or the `org.bluez.obex` bus owner as
  connection loss. Poll MAP/PBAP every 5 seconds until the first successful
  connection, then every 15 seconds for later reconnects. Preserve an iPhone
  `Connection refused (111)` response as a distinct MAP refusal state because
  another computer may currently own the phone's single MAP connection.
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
reliable sequence is to connect the Classic bearer first, wait until it is
connected across a short settling interval, and only then connect
`org.bluez.Bearer.LE1`. `Bearer.BREDR1` can be marker-only (no `Connect`
method or `Connected` property) depending on the packaged BlueZ build, so the
Classic bearer is driven and observed through `org.bluez.Device1` instead.
During first-time setup, `Device1.PreferredBearer=le` followed by
`Bearer.LE1.Connect` established (or confirmed) the LE connection on BlueZ
5.87; the supervising daemon afterwards does not keep re-setting
`PreferredBearer`, since BlueZ only applies that property while disconnected
and preferring LE removes the device from the normal auto-connect list.
BlueFerry supervises both connections for the daemon lifetime, repeats the
sequence after disconnects and system resume, and backs off exponentially
when a bearer keeps refusing to connect: a rejected `Connect` repeated every
five seconds against the iPhone was observed to keep the bond in a
half-connected flapping state.

After LE connects, BlueFerry waits for BlueZ to enumerate the ANCS service and
its Notification Source, Data Source, and Control Point characteristics. A
`StartNotify` call can race GATT readiness even after the characteristics have
appeared, so subscription failures are retried without requiring rediscovery.

This works on the MediaTek MT7922 and the Intel AX210 while MAP and PBAP stay
connected over BR/EDR; on the AX210 the LE half of the bond exists only when
the iPhone initiated the authentication (see "Pairing and iPhone
permissions").
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
