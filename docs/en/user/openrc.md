# Running without systemd (OpenRC)

BlueFerry's packages target systemd, but the backend also runs on OpenRC
and other hosts without systemd. Nothing needs to be switched on: BlueFerry
detects that no systemd is running and uses the session bus to start and stop
its backend. systemd hosts behave exactly as before.

OpenRC is not part of the CI package matrix and has no package recipe. The
packaging details are in
[packaging/openrc/README.md](../../../packaging/openrc/README.md).

## What it does

```mermaid
flowchart TD
    A[Client or pairing needs the backend] --> B{systemd running?}
    B -- yes --> S[systemctl --user start/restart/stop blueferry]
    B -- no --> C{OpenRC user service<br/>started or enabled<br/>and desktop bus = $XDG_RUNTIME_DIR/bus?}
    C -- yes --> R[rc-service --user blueferry start/restart/stop]
    C -- no --> D[D-Bus activation]
    D --> D1[Start: GetStatus activates the daemon]
    D --> D2[Stop: SIGTERM to the same-user owner of io.weirdware.BlueFerry]
```

- **Start**: a client asks the daemon for its status, and the session bus
  starts `blueferry run` from the installed activation file.
- **Stop and restart** (after pairing, forgetting a phone, or an upgrade):
  BlueFerry asks the bus daemon which process owns `io.weirdware.BlueFerry`,
  checks that it runs as your user, and sends it `SIGTERM`. If it has not
  exited after 180 seconds, it gets `SIGKILL`. A restart then activates a new
  daemon. No root and no process scanning are involved.
- **Repair hints** name `sudo rc-service bluetooth restart` on OpenRC and a
  generic "restart the Bluetooth service" when the init system is unknown.

## How to set it up

1. Install BlueFerry so that `io.weirdware.BlueFerry.service` is in
   `/usr/share/dbus-1/services/`. No init script is needed.
2. For iPhone system notifications (ANCS), start `bluetoothd` with `-E`.
   As an administrator, edit `/etc/conf.d/bluetooth`:
   - Gentoo: `BLUETOOTH_OPTS="-E"`
   - Alpine: `command_args="-E"`

   Then run `sudo rc-service bluetooth restart`. This briefly disconnects all
   Bluetooth devices. Until then BlueFerry pairs for messages and contacts
   and shows these steps instead of an activation button.
3. Set the adapter's device class before pairing, and again after Bluetooth
   restarts (the setting does not survive a restart):

   ```sh
   sudo /usr/lib/blueferry/blueferry-set-cod 0   # adapter hci0
   ```

4. Pair as usual with `blueferry-qt`, `blueferry-gtk`,
   `blueferry-quickshell`, or `blueferry pair`.

### Optional: OpenRC user service

Only use the user service in `packaging/openrc/blueferry` (OpenRC 0.62 or
newer with a `pam_openrc` session) if your desktop's session bus is
`$XDG_RUNTIME_DIR/bus`. Check it in the desktop:

```sh
echo "$DBUS_SESSION_BUS_ADDRESS"
```

If it shows `unix:path=/tmp/dbus-…`, as with Plasma started by greetd or
SDDM through `dbus-run-session`, **do not** create a user service. It would
run a second daemon on a second bus, competing for Bluetooth. D-Bus
activation alone is correct there. If the service is enabled on such a
desktop anyway, BlueFerry logs a warning and ignores it.

With a matching bus:

```sh
rc-update --user add blueferry default
rc-service --user blueferry start
```

The daemon then logs to `~/.local/state/blueferry/daemon.log`.

## Limits

- **Less sandboxing.** The systemd unit confines the daemon
  (`ProtectSystem=strict`, `PrivateDevices=`, `RestrictAddressFamilies=` and
  more). D-Bus activation and OpenRC have no equivalent, so the daemon runs
  with your normal user rights. The user service keeps `umask 077` and
  `no_new_privs`.
- D-Bus activation also starts the daemon before a phone is paired.
- `blueferry pair` stops the old daemon synchronously and can wait up to
  about three minutes if that daemon hangs.
- Experimental mode is found through `/proc`. A bundled option such as `-nE`
  is not recognized, and with `/proc` mounted `hidepid=1` or `2` it reads as
  inactive.
- WirePlumber is restarted after a policy change only if it runs as an OpenRC
  user service. With a session launcher such as `gentoo-pipewire-launcher`,
  restart WirePlumber yourself or log in again.

## Privacy

Nothing about data handling changes. The lifecycle code only asks the session
bus for the owner's user ID and process ID of `io.weirdware.BlueFerry`, and
signals that process only if it belongs to you. It reads no message or
contact data and logs no personal data.
