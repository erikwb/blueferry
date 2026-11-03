# FAQ

## Do I need a Mac, an Apple account, or an app on the iPhone?

No. BlueFerry talks to the iPhone directly over standard Bluetooth profiles.
There is no relay, Apple login, iPhone app, jailbreak, cloud service, or
subscription.

## Can I see my older messages?

Messages BlueFerry has seen while connected, plus incoming messages it
catches up on after a reconnect (see
[Messages received while disconnected](privacy.md#messages-received-while-disconnected)).
It doesn't download your iCloud Messages archive or your complete
sent-message history.

## Can I send pictures, react to messages, or see typing indicators?

No. Attachments, reactions, and typing indicators aren't available over the
Bluetooth profiles that iOS offers.

## Can I make calls?

No. Calls and FaceTime aren't supported. BlueFerry deliberately keeps call and
music audio on the iPhone.

## Why can't I reply to a group?

Bluetooth doesn't tell BlueFerry a group's ID or complete member list. When
BlueFerry can't tell who is in a group, it asks you to confirm the members the
first time you send to it, suggesting contacts as you type. If someone new
writes, it asks again. See [Group chats](overview.md#group-chats).

## Can two computers use the same iPhone?

Both can be paired, but the iPhone serves the message connection to only one
of them at a time. The other one shows that the connection was refused.

## Which Bluetooth adapters work?

Adapters with Bluetooth Classic and Bluetooth 4.0 or newer with LE
advertising. Bluetooth 3-only adapters don't work. Realtek adapters generally
don't support iPhone system notifications or group chats. Behavior varies by
adapter, so please report what works for you.

## Does it work with iOS 18 or earlier?

Use **Compatibility pairing for iOS 18 or earlier**. Messages and contacts
work; iPhone system notifications don't connect. See
[Pairing options](pairing.md#pairing-options).

## Does BlueFerry send my data anywhere?

No. Everything stays on your computer. Message history is encrypted by
default; see [Privacy and storage](privacy.md).

## Is it safe to rely on?

Not yet as your only way to receive important messages. BlueFerry is
experimental, and Apple can change the Bluetooth behavior it relies on.

## Where do I report bugs?

In the [GitHub issues](https://github.com/erikwb/blueferry/issues). For
pairing problems, `blueferry pairing-issue` prepares a scrubbed report; see
[Report a problem](troubleshooting.md#report-a-problem).
