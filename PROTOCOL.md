# Bluetooth protocol findings

This document records the device behavior that BlueFerry relies on. It replaces
the original one-off experiment scripts with conclusions that are useful to
maintainers. These are empirical observations, not promises made by Apple.

Unless stated otherwise, the behavior was observed with an iPhone 16 Pro Max
running iOS 26.5 and Linux controllers supporting both BR/EDR and LE. MAP/PBAP
behavior spans BlueZ 5.72 or newer; the dual-bearer ANCS flow requires BlueZ
5.86 or newer with its bearer API exposed. The complete MAP, PBAP, and ANCS
combination has been exercised with a MediaTek MT7922. A later first-attempt
clean test also completed all three on a previously tested Intel controller
with an iPhone 17 Pro Max running an iOS 27 beta. Other iPhone, iOS, BlueZ, and
controller versions need independent verification.

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
- Linux discovers the iPhone and initiates the connection to that selected
  device; the user does not start pairing by tapping the computer under iOS
  **Other Devices**. The pre-bond Linux identity does not need a globally
  discoverable LE advertisement. The post-bond solicitation advert carries its
  own bounded discoverability window.
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
- The sequence that produced a dual bond on the tested Intel and MediaTek
  controllers, when the transaction completed, is to
  register the pairing agent and then call `Device1.Connect()` on the
  *unpaired* device: iOS refuses the profile connection without security,
  initiates the pairing itself over the existing ACL, and — as central and
  authentication initiator — runs cross-transport derivation. A btmon
  capture shows `BR/EDR SMP: Pairing Request/Response`, IRK exchange, and a
  new LTK immediately after encryption; `Bearer.LE1` appears with
  `LE.Paired/Bonded` about a second later and the iPhone keeps a single
  device record for the computer. The user confirms one numeric comparison
  on both screens; the adapter never needs to become discoverable.

Interactive BlueFerry clients now default to that Connect-first transaction.
They register their device-scoped `DisplayYesNo` agent as the default, call
`Device1.Connect()` on the unpaired device, and wait for iOS to initiate
authentication. BlueZ exposes neither its registered-agent list nor the
identity of its default agent, so automatic desktop-agent heuristics proved too
indirect to choose a reliable transaction.

Some controllers instead cancel the Connect-first transaction before pairing
finishes. For those systems the independent **Use explicit Bluetooth pairing**
override registers the same device-scoped agent but calls `Device1.Pair()`
directly. Headless callers also use explicit Pair because they cannot present
BlueFerry's interactive confirmation UI. Explicit Pair may produce only a
Classic bond on controllers such as the tested Intel device, so it is a
controller workaround rather than the full-mode default. Delivery mode and
authentication strategy are independent: either full or compatibility mode
can use Connect-first or explicit Pair.

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

BlueFerry therefore resolves two delivery modes. Full mode requires a
controller with BR/EDR, LE, and advertising plus BlueZ 5.86 or newer. Its
bearer API must already be active or be activatable through the package's
systemd drop-in before pairing proceeds. Compatibility mode is selected
automatically when ANCS is unavailable or explicitly for iOS 18 and earlier.
It still broadcasts ANCS solicitation when the controller can advertise,
because that signal exposes the MAP/PBAP permissions, but persists
`BLUEFERRY_ANCS_ENABLED=false` and never enables an outbound LE/ANCS
connection. MAP and PBAP are its successful end state; absence of Notification
Access is expected and group messages may consequently lack ANCS-derived group
metadata.

Both modes activate obexd's local Message Notification Server before pairing.
After authentication, setup trusts the bond, selects BR/EDR as the preferred
bearer, lets the existing Classic ACL settle, and only then registers the
short-lived solicitation advertisement. The daemon starts while that advert
and BlueFerry's temporary pairing agent are still present, and its first
profile operation is MAP/PBAP. Full mode enables LE after a MAP/PBAP attempt
made while Classic is observable, or immediately after a successful profile
open that itself proves Classic reachability. Later whole-device reconnects
reapply the same gate; compatibility mode leaves LE disabled. Before later
connection requests the daemon selects the corresponding BlueZ
`PreferredBearer` when that property is available.

Because `btmgmt class` is reset by controller and bluetoothd lifecycles, the
daemon also reconciles the A/V Hands-Free Class-of-Device at startup, after a
BlueZ owner change, and once per minute. Repair goes through the packaged fixed
systemd helper rather than granting the daemon raw Bluetooth capabilities.

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
refused (111)` at the transport level is different: another computer may own
the iPhone's single MAP session, although stale or incomplete bonds can look
similar. BlueFerry exposes that state distinctly and keeps polling rather than
misreporting a permissions failure. A completed bond is therefore only the
beginning of setup; clients verify the live profiles before reporting success.

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
local roster affects only BlueFerry's reply routing and never modifies the
Messages group itself. Because iOS supplies a group name but no conversation
identifier, distinct named groups with the same normalized name cannot be
distinguished and are projected as one local thread. This is a conservative
observation-based heuristic, not a general iMessage group protocol;
alternative iOS notification formats remain uncharacterized.

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

The saved target MAC is also the MAP trust boundary. Discovery alone must never
put a phone into BlueFerry's configuration. Setup persists only the device it
just authenticated, trusted, and settled over Classic, together with the
resolved ANCS policy, then starts the daemon so MAP/PBAP can be verified.
Compatibility mode intentionally persists a Classic-only target. Full mode may
also persist before ANCS authorization finishes: the daemon continues the
bounded LE connection, GATT discovery, and authorization probe while MAP/PBAP
remain available. Removing the bond invalidates the saved runtime target and
stops the daemon's connection attempts.

- Keep one MAP and one PBAP session open for the daemon lifetime.
- Open and retry MAP and PBAP independently. A successful sibling session must
  remain available when iOS rejects the other profile; in particular, a MAP
  refusal must not prevent PBAP contacts access.
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
5.87. `PreferredBearer` is treated as an instruction for the next outbound
connection, not a durable preference: the supervisor selects `bredr` or `le`
immediately before requesting that bearer and does not leave LE preferred while
idle. BlueFerry supervises both connections for the daemon lifetime and backs
off exponentially when a bearer keeps refusing to connect: a rejected
`Connect` repeated every five seconds against the iPhone was observed to keep
the bond in a half-connected flapping state. Classic remains actively
supervised. LE receives one speculative outbound dial, then yields to the
solicitation advertisement instead of repeatedly calling `Connect`. A genuine
connected-to-disconnected LE transition grants one fresh outbound bootstrap.
If that attempt is spent while the phone remains absent, an observed Classic
return grants one more for the new presence generation. A returning inbound LE
link clears Classic backoff accumulated while the phone was absent, and limits
later Classic backoff to 30 seconds while LE remains healthy. A deliberate
stale-GATT reset and a new BlueZ generation also receive a new outbound
bootstrap. If BlueZ cannot keep the solicitation registered, bounded outbound
LE retries remain enabled rather than treating an unavailable inbound path as
primed.

Solicitation remains registered until both MAP/PBAP and end-to-end ANCS are
healthy, subject to a three-minute minimum permission window. It is restored
when LE or either protocol becomes unhealthy, when BlueZ releases it, and when
bluetoothd changes D-Bus owner. This both preserves the iOS permission signal
and avoids occupying an advertising instance indefinitely after recovery.

After LE connects, BlueFerry waits for BlueZ to enumerate the ANCS service and
its Notification Source, Data Source, and Control Point characteristics. A
`StartNotify` call can race GATT readiness even after the characteristics have
appeared, so subscription failures are retried without requiring rediscovery.
After a physical reconnect, notification registrations owned by the previous
ATT session are explicitly stopped and started again rather than trusting a
cached `Notifying=true`. A previously authorized Control Point failure or
timeout escalates to one serialized `Bearer.LE1.Disconnect`; MAP/PBAP stay
available and LE rebuilds behind the profile-ordering gate.

This works on the MediaTek MT7922 and the Intel AX210 while MAP and PBAP stay
connected over BR/EDR; on the AX210 the LE half of the bond exists only when
the iPhone initiated the authentication (see "Pairing and iPhone
permissions").
BlueFerry's Arch and RPM packages enable the necessary bluetoothd experimental
API and require BlueZ 5.86 or newer. DEB packages target several distributions
with older or divergent BlueZ releases and deliberately do not modify the
system Bluetooth unit; full ANCS is offered there only when the installed
daemon already exposes the 5.86+ bearer API. Otherwise pairing falls back to
MAP/PBAP compatibility mode instead of blocking. The requirement is based on
observed capabilities and the live API, not a controller-vendor check.

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
content at all. When mirroring is enabled, the optional local allow/block rules
are evaluated against that exact identifier before title, subtitle, message,
or app display-name attributes are requested. Even when mirroring is enabled,
included non-Messages content is never written to history or placed on
BlueFerry's D-Bus event feed.

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

## Pairing diagnostics

Each pairing attempt records the resolved delivery mode, authentication
strategy, controller capability, phone/bearer observations, daemon transport
state, and an ordered monotonic timeline. Reports redact device addresses,
object paths, and user directories before they are retained or embedded in a
GitHub issue URL. The first `bluez_trace` entry contains a complete device
snapshot; later entries contain only recursive `changes` from the previous
entry, with JSON `null` removing a field. Unchanged snapshots are represented
by the ordered timeline instead of taking another trace entry. GitHub issue
prefill also omits the redundant root/child interface inventories and limits
the adapter UUID inventory to messaging/phonebook services, while the local
report retains them; if necessary, it drops the timeline only as a final
size-reduction step.

Native packages also bake a SHA-256 of their deterministic source snapshot.
The report's `blueferry_build` uses the same package-release plus short-SHA
format as the daemon's private `_build_id` status field, while
`blueferry_sha` retains the complete digest. Source checkouts fall back to the
Git commit SHA. These fields distinguish same-version local rebuilds without
changing the public D-Bus interface version.

## Maintenance rule

When behavior changes, record the phone model, iOS version, BlueZ version, and
controller, and distinguish a captured observation from an inference. Never
turn a real-device experiment into an automated test: the test suite must not
connect to a paired phone, read private device data, change pairing state, or
send a message.
