# Reconnect and self-healing

BlueFerry keeps the iPhone's Classic Bluetooth link up by itself. This page
explains what it does when the phone is away, when bluetoothd stops
answering, and when a profile hangs on a live link, and how to step in.

## Reconnect now

When the iPhone has been away, BlueFerry retries the Classic connection with
a growing pause, up to 10 minutes. It stops paging a phone that shows no sign
of being nearby, for example after Bluetooth was switched off on the iPhone,
and probes only every 10 minutes until the phone shows up again over LE, a
discovery or an incoming connection. Paging an absent phone once hung
bluetoothd in the kernel, so fewer pages matter.

To reconnect at once:

- KDE client: **Reconnect** next to **iPhone Link** under Connection Health
  on the iPhone page. The main window shows a message with a **Reconnect**
  action while the phone is unreachable or a profile is stuck.
- Terminal client: press `b`.
- Command line: `blueferry reconnect` waits for the outcome
  (`--no-wait` returns at once).

"iPhone not reachable. Is Bluetooth switched on on the iPhone?" means the
attempt failed; check Bluetooth on the phone. Pressing Reconnect again
while an attempt runs does not start a second one.

## The system Bluetooth service is not responding

If bluetoothd stops answering D-Bus (two unanswered pings 15 seconds apart),
BlueFerry stops its own connection attempts and the adapter recovery, since
they would only queue behind the stuck call, and the clients say "The system
Bluetooth service is not responding". When bluetoothd is stuck in the kernel
(process state D), only restarting the computer helps. BlueFerry resumes on
its own once bluetoothd answers again or is restarted.

## Calls never become available (stuck HFP)

With [phone calls](calls.md) on, bluetoothd 5.87 started with `-E` loads its
own `hfp` plugin, which races oFono for the iPhone's call channel. When
bluetoothd wins, for example after a reboot, oFono's modem power-up keeps
timing out (`org.ofono.Error.Timedout` in the log) and calls never come up.

The lasting fix is to start bluetoothd without that plugin (keep `-E`):

- OpenRC: add `-P hfp` to `BLUETOOTH_OPTS` in `/etc/conf.d/bluetooth`, then
  `sudo rc-service bluetooth restart`.
- systemd: add `-P hfp` to bluetoothd's `ExecStart` in a drop-in
  (`sudo systemctl edit bluetooth.service`), then
  `sudo systemctl restart bluetooth`.

`blueferry doctor` warns while oFono runs next to a bluetoothd that still
loads the plugin.

Until then BlueFerry heals it itself: after two power-up timeouts in a row it
disconnects the iPhone, waits three seconds and connects it again. It does
this only while the Classic link is up, never during a call, while
bluetoothd does not answer, or during an adapter recovery, and at most once
every ten minutes (doubling up to an hour while the reset does not help).
The reset briefly drops iPhone notifications too.
Otherwise **Reconnect** is offered, and `blueferry reconnect` performs the
same reset on request. `blueferry doctor` shows the setting and the last
reset.

During a call, **Reconnect** does not reset anything and says that a call is
active; try again after the call.

To switch the automatic reset off, untick **Reset a stuck iPhone connection
automatically** in the settings (Qt, GTK, Quickshell), press `B` in the
terminal client, or run `blueferry reconnect --no-auto-reset`. The choice is
saved as `"auto_profile_reset"` in `~/.config/blueferry/settings.json`, takes
effect at once and takes precedence over `BLUEFERRY_AUTO_PROFILE_RESET=false`
in `~/.config/blueferry/local.env` (which needs a backend restart).

## "Please wait a moment, then try again"

BlueFerry limits how often each action may be requested. This message means
the limit for that action was reached; it clears within a minute.

## Limits

The damping, the BlueZ health check and the reset are tested against fakes.
The HFP trigger, the BlueZ health check and the manual reconnect have no
targeted hardware test yet.
