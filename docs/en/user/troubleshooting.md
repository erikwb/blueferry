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

- **Permanent fix:** set `ControllerMode = dual` (or remove the line) and
  restart Bluetooth.
- **Until the next Bluetooth restart:** `sudo btmgmt --index 0 le on`
  (replace `0` with your adapter's index, for example `1` for `hci1`).

Then pair again.

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
