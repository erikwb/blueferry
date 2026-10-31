# Troubleshooting

Start with the iPhone page in the client. It reports Messages, Contacts, and
iPhone Notifications separately. Messages and contacts can work even when the
optional notification connection doesn't.

For prerequisite checks and logs:

```bash
blueferry doctor
journalctl --user -u blueferry -f
```

## Messages work, but names are missing

Use **Sync Contacts** in the client or run `blueferry contacts-sync`. Make sure
**Sync Contacts** (German: **Kontakte synchronisieren**) is on in the iPhone's
Bluetooth settings for the computer.

If a local number such as Australian `0412345678` does not match a reply
from `+61412345678`, check the numbering region. BlueFerry uses the territory
in `LC_ALL`, then `LC_TELEPHONE`, then `LANG`. To override it, add
`BLUEFERRY_PHONE_REGION=AU` (a two-letter country code) to
`~/.config/blueferry/local.env`, then run `systemctl --user restart blueferry`.
With no recognized region, BlueFerry does not guess a country for local numbers.

If contact selections lost their `+` prefix in an earlier version, run
`blueferry contacts-sync` after upgrading. The cache needs a fresh copy from
the iPhone to recover the original international prefixes.

## The switches don't appear on the iPhone

The **Show Message Notifications** and **Sync Contacts** switches can take a
few minutes to appear. Reopen the computer's **ⓘ** page a few times, and check
both entries if the phone lists the computer twice. In testing they appeared
only while BlueFerry's Bluetooth LE advertisement was active, so the adapter
needs working LE (see [Bluetooth LE is switched off](#bluetooth-le-is-switched-off-on-the-adapter)).

## Messages show "connection refused"

The iPhone serves only one message (MAP) connection at a time. Another
computer paired with the same phone may hold it. Disconnect that computer, or
turn off its Bluetooth. A stale or incomplete pairing can look similar; in
that case [start over cleanly](pairing.md#start-over-cleanly).

## Sending works, but incoming messages never arrive

The iPhone needs a local **Message Notification Server (MNS)** service on the
computer to deliver incoming MAP messages. Sending and contact sync can work
even when that service is missing. With BlueFerry connected, run
`blueferry doctor`: it checks for MNS UUID
`00001133-0000-1000-8000-00805f9b34fb` on the configured adapter. A present UUID
confirms local registration; it does not prove that the iPhone connected to it.

If MNS is missing while obexd is running, inspect its logs and launch options.
On systemd:

```bash
journalctl --user -u obex.service -b
systemctl --user cat obex.service
```

One confirmed cause is a BlueZ initialization bug in obexd builds that use
Evolution's address-book backend, reported on Debian 13 with BlueZ 5.82 in
[BlueFerry #280](https://github.com/erikwb/blueferry/issues/280) and described in
[BlueZ #2315](https://github.com/bluez/bluez/issues/2315). obexd can finish
starting without registering any of its server profiles. The logs may include
`Unable to acquire registry` and a missing `evolution-source-registry.service`.
Missing MNS alone does not establish this cause; disabled plugins or a service
still starting can also explain it.

For that initialization bug, the reporter confirmed this workaround:

1. Run `systemctl --user edit obex.service` and add the following override.
   Use the obexd executable path shown by `systemctl --user cat obex.service`,
   and preserve any existing arguments. This example uses Debian's path:

   ```ini
   [Service]
   ExecStart=
   ExecStart=/usr/libexec/bluetooth/obexd -P irmc,pbap
   ```

2. Run `systemctl --user daemon-reload`, then
   `systemctl --user restart obex.service`. This interrupts current OBEX
   transfers and message/contact sessions. Let BlueFerry reconnect and run
   `blueferry doctor` again, then check with an incoming test message.

`-P irmc,pbap` disables the computer's IrMC and phonebook **server** plugins;
BlueFerry's phonebook **client**, which reads contacts from the iPhone, remains
available. Other apps that serve the computer's address book over Bluetooth
may need those server plugins. If obexd already has `-P` or `--noplugin`, add
`irmc,pbap` to that existing list instead of adding a second option. On systems
without systemd, apply the same option to the session's existing obexd launcher.
To undo it, remove the override you added and reload/restart obex.service.

## Messages and contacts work, but iPhone notifications never connect

This usually means the Bluetooth LE half of the pairing is stale. The pairing
covers two links: Bluetooth Classic for messages and contacts, and Bluetooth
LE for notifications. They can get out of sync, typically after the pairing
was removed on the computer only.

Typical signs:

- Messages and contacts work normally.
- iPhone Notifications never become connected.
- The LE link connects and drops every few seconds. `btmon` shows LE
  encryption failing with the stored key, followed by a disconnect.

Neither BlueZ nor BlueFerry can repair the key. Reset both sides and pair
again:

1. On the iPhone: **Settings → Bluetooth → ⓘ** next to the computer →
   **Forget This Device** (German: **Dieses Gerät ignorieren**). Remove every
   entry with the computer's name.
2. On Linux: `bluetoothctl remove <iPhone address>`.
3. [Pair again](pairing.md).

## Bluetooth LE is switched off on the adapter

Some systems run a dual-mode adapter with LE switched off. Pairing then
completes over Bluetooth Classic, but the LE advertisement can't start, so the
iPhone switches don't appear and notifications can't connect.

Compare what the adapter supports with what is currently enabled:

```bash
sudo btmgmt info
```

If `le` appears under **supported settings** but not under
**current settings**, LE is off. The usual cause is this line in
`/etc/bluetooth/main.conf`:

```ini
[General]
ControllerMode = bredr
```

Set `ControllerMode = dual` (or remove the line) and restart Bluetooth:

```bash
sudo systemctl restart bluetooth   # OpenRC: sudo rc-service bluetooth restart
```

`sudo btmgmt le on` alone doesn't help here. With `ControllerMode = bredr`,
BlueZ doesn't offer LE advertising for the adapter until it restarts in dual
mode. If `ControllerMode` isn't `bredr`, restarting Bluetooth switches LE back
on by itself.

Then pair again. The pairing wizard and the KDE client can check again
without starting over.

## bluetoothd hangs and can't be restarted

Rarely, `bluetoothd` gets stuck inside the kernel. Bluetooth stops responding,
`bluetoothctl` hangs, and restarting the Bluetooth service doesn't finish.
Check the process state:

```bash
ps -o pid,stat,cmd -C bluetoothd
```

A `D` in the `STAT` column means the process is waiting uninterruptibly in the
kernel. It can't be killed, so **only a reboot helps**. This is a kernel bug,
not a BlueFerry or BlueZ problem. Please report it to the
[linux-bluetooth mailing list](https://subspace.kernel.org/vger.kernel.org.html)
(`linux-bluetooth@vger.kernel.org`) with your kernel version, adapter model,
and the output of `dmesg` and `sudo cat /proc/<pid>/stack` captured before you
reboot.

BlueFerry notices such a hang by itself, pauses its own Bluetooth work and
says so in every client; see [Reconnect and self-healing](reconnect.md).

## Notifications stopped after working before

If iPhone notifications previously worked with the same phone and adapter but
stay unavailable for five minutes, BlueFerry may power-cycle the adapter once.
It first tries an LE-only reset. It skips this while another Bluetooth device
is paired or connected to the adapter, during discovery, or during a transfer,
and it waits at least an hour between attempts. The decisions are logged in
the backend journal. Details are in the
[README](https://github.com/erikwb/blueferry#troubleshooting).

## Group messages look like direct messages

BlueFerry files a message under its group only when it also receives the
iPhone's notification for it. Turn on **Share System Notifications** (German:
**Systemmitteilungen teilen**) on the iPhone and check that iPhone
Notifications show as connected.

## Report a problem

When pairing fails, BlueFerry saves a scrubbed report. It contains the package
build, pairing mode, controller details, and a setup timeline. Bluetooth
addresses and home-directory paths are removed, but check it before posting.

```bash
blueferry pairing-issue            # opens a prefilled GitHub issue
blueferry pairing-issue --no-open  # prints the URL instead
```

Please add your iPhone model and iOS version to the
[issue](https://github.com/erikwb/blueferry/issues).
