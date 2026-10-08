# OpenRC

BlueFerry's native packages target systemd. On OpenRC, which is outside the
CI package matrix and has no package recipe here, the backend works without a
service manager. D-Bus activation works with any OpenRC version; only the
optional user service needs OpenRC 0.62 or newer with a `pam_openrc` user
session.

## Default: D-Bus activation

No init script is needed. Install the packaged
`io.weirdware.BlueFerry.service` activation file in
`/usr/share/dbus-1/services/`. Without systemd activation the session bus
ignores its `SystemdService=` line and starts `Exec=/usr/bin/blueferry run`
itself, just as it starts BlueZ's `obexd`.

The backend detects that no service manager runs it and manages its lifecycle
through the session bus:

- **Start**: a client calls `GetStatus`, and the bus activates the daemon.
- **Stop and restart** (after pairing, forgetting a phone, or a package
  upgrade): BlueFerry asks the bus daemon which process owns
  `io.weirdware.BlueFerry` and checks that it runs as the same user. It
  sends that process `SIGTERM` through a pidfd, re-checking the owner first,
  and waits for the name to be released, but never longer than the caller's
  timeout (30 to 45 seconds for every caller today). Only a caller that
  allows the full 180-second grace period of the systemd unit and the OpenRC
  script gets the `SIGKILL` escalation with 5 more seconds; otherwise the
  request fails like a timed-out `systemctl stop`, with `SIGTERM` still in
  effect so an in-flight Bluetooth operation can finish. A restart then
  activates a new daemon within the remaining time. It does not scan
  processes and does not use root.
- **systemd without `/usr/bin/systemctl`** (NixOS): detection needs both the
  `/run/systemd/system` marker and the fixed `systemctl` path, so such a
  host takes the session-bus path above instead of failing on the missing
  command as it did before.

Unlike the systemd unit's `ConditionPathExists=`, D-Bus activation also starts
the daemon before a phone is paired.

## Optional: OpenRC user service

Use `packaging/openrc/blueferry` **only** if your desktop session uses the
session bus at `$XDG_RUNTIME_DIR/bus`, for example because the OpenRC user
`dbus` service provides it. Check with `echo $DBUS_SESSION_BUS_ADDRESS` in the
desktop.

> **Do not create a user service** on desktops started through
> `dbus-run-session` or `dbus-launch`, such as Plasma under greetd or SDDM,
> whose bus address is `unix:path=/tmp/dbus-…`. Do not add the user `dbus`
> service there either. It would start a second session bus, the service's
> daemon would sit on that bus, and D-Bus activation would start a second
> daemon on the desktop bus, with both competing for BlueZ. D-Bus activation
> alone is correct there.

Install the script as `/etc/user/init.d/blueferry` (mode 0755) or
`~/.config/rc/init.d/blueferry`, then:

```sh
rc-update --user add blueferry default
rc-service --user blueferry start
```

Once the service is enabled in one of your runlevels or running, and the
desktop's `DBUS_SESSION_BUS_ADDRESS` is `unix:path=$XDG_RUNTIME_DIR/bus`,
BlueFerry uses `rc-service --user blueferry start|restart|stop` instead of
D-Bus activation. If the desktop uses another bus, it logs a warning, ignores
the service, and uses D-Bus activation.

- `start_pre` refuses to start until `~/.config/blueferry/local.env` exists,
  standing in for `ConditionPathExists=`. OpenRC has no silent skip, so this
  appears as a failed start with a hint.
- The daemon runs `${BLUEFERRY_BIN:-/usr/bin/blueferry} run` under
  `supervise-daemon`. Set `BLUEFERRY_BIN` in `~/.config/rc/conf.d/blueferry`
  for other install locations.
- `supervise-daemon` respawns the daemon 5 seconds after **any** exit, not
  only failures. That covers exit status 75, the daemon's restart request
  after a package upgrade, and also exit status 0. More than five exits within
  two minutes stop respawning.
- Output is appended to
  `${XDG_STATE_HOME:-~/.local/state}/blueferry/daemon.log` (mode 0600). OpenRC
  does not rotate it.
- If a client asks for BlueFerry before the service has started, D-Bus
  activation wins the race and the service's daemon fails to acquire the bus
  name. Because `pam_openrc` waits for the user's `boot` runlevel, adding
  `dbus` and `blueferry` to that runlevel should avoid this. This has not been
  tested.

### Reduced hardening

The systemd unit confines the daemon with `ProtectSystem=strict`,
`PrivateDevices=`, `PrivateTmp=`, `RestrictAddressFamilies=AF_UNIX`,
`RestrictNamespaces=`, `SystemCallArchitectures=native`, and related
directives. Neither D-Bus activation nor OpenRC offers an equivalent, so the
daemon runs with the user's ordinary filesystem, device, and network access.
The init script keeps `umask 077` and `no_new_privs`.

## BlueZ experimental mode

iPhone system notifications need BlueZ 5.86 or newer running `bluetoothd -E`.
Arch and Fedora packages install a systemd drop-in for this. On OpenRC an
administrator enables it once in `/etc/conf.d/bluetooth`:

- Gentoo: `BLUETOOTH_OPTS="-E"`
- Alpine, whose init script has no options variable: `command_args="-E"`

Then run `sudo rc-service bluetooth restart`, which briefly disconnects all
Bluetooth devices. Until then, BlueFerry pairs for messages and contacts and
shows these steps instead of an activation button.

BlueFerry finds the running `bluetoothd` through `/proc` and looks for `-E` or
`--experimental`. Known limits: a bundled short option such as `-nE` is not
recognized, and with `/proc` mounted `hidepid=1` or `2` the daemon is invisible
and experimental mode reads as inactive.

## Bluetooth device class

Pairing needs the adapter's Class of Device set to A/V Hands-Free. systemd
packages set it through the argument-validated `blueferry-set-cod` helper,
started as a Polkit-authorized system unit. OpenRC has no such path yet, so
set the class as an administrator before pairing, and again after Bluetooth
restarts because the setting is volatile:

```sh
sudo /usr/lib/blueferry/blueferry-set-cod 0   # adapter hci0
```

## WirePlumber

BlueFerry restarts WirePlumber after changing its phone-audio policy only from
the pairing flow, and only when WirePlumber runs as an OpenRC user service.
The daemon itself never blocks on `rc-service`. Desktops that start PipeWire
through a session launcher, such as `gentoo-pipewire-launcher`, are left
alone; the log then asks you to restart WirePlumber or log in again.
