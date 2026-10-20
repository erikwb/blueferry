# Running under WSL2

BlueFerry runs in a WSL2 distribution on Windows, but WSL2 has no Bluetooth
of its own. The adapter is passed in from Windows over USB/IP, and several
parts of the Linux stack behave differently from a desktop install: firmware
loading, WirePlumber's Bluetooth monitor, polkit, the desktop keyring, and
obexd's message notification service.

This was set up on Debian 13 (BlueZ 5.82, WirePlumber 0.5.8) under WSL 3.0.3
with a TP-Link UB500 (RTL8761BU). WSL2 is not part of the CI package matrix.

## What is different

- **The adapter belongs to Windows.** `usbipd-win` detaches it from Windows
  and attaches it to WSL2. Windows cannot use that adapter while it is
  attached, so use a dedicated USB dongle rather than the PC's built-in
  radio if Windows needs Bluetooth for a mouse, keyboard or headset.
- **The kernel cannot see the distribution's firmware.** The WSL2 kernel
  loads firmware from the utility VM's root file system, not from
  `/lib/firmware` inside your distribution. Adapters whose driver loads
  firmware, such as every Realtek adapter, fail with
  `Direct firmware load for rtl_bt/... failed with error -2` even when the
  firmware package is installed.
- **Terminals have no seat.** WSL2 starts a logind session for the default
  user at boot, but that session has no seat, and shells opened from Windows
  Terminal belong to no session at all. WirePlumber starts its Bluetooth
  monitor only when the seat is `active`; here it stays `online`, so PipeWire
  never registers A2DP or hands-free endpoints with BlueZ. `Device1.Connect`
  then fails with `br-connection-profile-unavailable`. On the setup above,
  explicit pairing bonded but then failed with "The iPhone's Classic
  Bluetooth connection did not settle" until WirePlumber was fixed.
- **Polkit sees no active local session.** The packaged rule that lets
  BlueFerry set the adapter class requires `subject.active` and
  `subject.local`, which are false for processes outside a session.
- **No keyring exists and nothing unlocks one.** Encrypted history needs a
  default keyring. A distribution that never had a graphical login has none,
  the keyring prompt cannot open a window because user services do not get
  WSLg's `DISPLAY`, and without a PAM login the keyring is never unlocked
  automatically. Until it is set up, BlueFerry sends messages but keeps no
  history, so clients show no conversations.
- **obexd may register no profiles.** Without `evolution-data-server`,
  obexd's PBAP and IrMC server plugins make a failing registry lookup during
  startup that runs obexd's BlueZ watch before its MAP servers exist. obexd
  registers its profiles only in that watch, so none are registered, among
  them the Message Notification Server (`0x1133`) the iPhone needs to push
  new messages. Reading and sending still work, but new messages never
  arrive, and the daemon logs "the iPhone did not open MAP notifications".
  This is not specific to WSL2.

## How to set it up

1. In Windows, install [usbipd-win](https://github.com/dorssel/usbipd-win).
   In an administrator PowerShell, find the adapter, share it once, and
   attach it:

   ```powershell
   usbipd list
   usbipd bind --busid <BUSID>
   usbipd attach --wsl --busid <BUSID>
   ```

   In WSL2, `lsusb` now lists the adapter.

2. Install the adapter's firmware package in the distribution, for Realtek
   `sudo apt install firmware-realtek` on Debian. Then copy the firmware to
   `/mnt/wsl`, which the WSL2 kernel can read, and add it to the kernel's
   firmware search path. Both are lost on every restart, so do it from a
   boot service. Save this as `/usr/local/sbin/wsl-bt-firmware`:

   ```sh
   #!/bin/sh
   set -eu
   src=/lib/firmware
   dst=/mnt/wsl/firmware
   mkdir -p "$dst/rtl_bt"
   cp -p "$src"/rtl_bt/* "$dst/rtl_bt/"
   printf '%s' "$dst" > /sys/module/firmware_class/parameters/path
   # An adapter attached before this ran has already failed its firmware
   # load; reload btusb so it probes again with the new search path.
   if grep -q '^btusb ' /proc/modules; then
       modprobe -r btusb
       modprobe btusb
   fi
   ```

   and this as `/etc/systemd/system/wsl-bt-firmware.service`:

   ```ini
   [Unit]
   Description=Expose Bluetooth firmware to the WSL2 kernel
   ConditionVirtualization=wsl
   ConditionPathIsDirectory=/mnt/wsl
   Before=bluetooth.service

   [Service]
   Type=oneshot
   RemainAfterExit=yes
   ExecStart=/usr/local/sbin/wsl-bt-firmware

   [Install]
   WantedBy=multi-user.target
   ```

   Then enable it:

   ```sh
   sudo chmod 755 /usr/local/sbin/wsl-bt-firmware
   sudo systemctl enable --now wsl-bt-firmware.service
   sudo systemctl restart bluetooth
   ```

   `sudo dmesg | grep -i rtl` should show the firmware and config files
   loading, and `bluetoothctl show` a powered controller. For an adapter
   from another vendor, copy its firmware directory instead of `rtl_bt`.

3. Let WirePlumber start its Bluetooth monitor without an active seat.
   Create `~/.config/wireplumber/wireplumber.conf.d/50-wsl2-no-seat-monitoring.conf`:

   ```
   wireplumber.profiles = {
     main = {
       monitor.bluez.seat-monitoring = disabled
     }
   }
   ```

   Then run `systemctl --user restart wireplumber`. `bluetoothctl show`
   now lists `Audio Sink`, `Audio Source` and `Handsfree` UUIDs.

4. Allow the adapter-class helper for your user. As an administrator, create
   `/etc/polkit-1/rules.d/49-blueferry-cod-wsl2.rules`, replacing `you` with
   your user name:

   ```javascript
   polkit.addRule(function(action, subject) {
       if (action.id !== "org.freedesktop.systemd1.manage-units" ||
           action.lookup("verb") !== "start" ||
           subject.user !== "you") {
           return polkit.Result.NOT_HANDLED;
       }
       var unit = action.lookup("unit");
       if (unit && /^blueferry-btmgmt-set-class@[0-9]+\.service$/.test(unit)) {
           return polkit.Result.YES;
       }
       return polkit.Result.NOT_HANDLED;
   });
   ```

   It grants the same single sandboxed unit as the packaged rule.

5. Start obexd without its PBAP and IrMC server plugins, so it registers the
   Message Notification Server. BlueFerry uses only obexd's PBAP client,
   which is unaffected. Create
   `~/.config/systemd/user/obex.service.d/50-no-pbap-server.conf`:

   ```ini
   [Service]
   ExecStart=
   ExecStart=/usr/libexec/bluetooth/obexd -P pbap,irmc
   ```

   Then run `systemctl --user daemon-reload` and
   `systemctl --user restart obex`. `bluetoothctl show` now lists
   `Message Notification Se..` (`00001133-…`). Skip this step if
   `evolution-data-server` is installed and that UUID already appears.

6. Pair as usual. With an adapter that does not default to explicit pairing,
   pass `--explicit-pairing` if Connect-first fails:

   ```sh
   blueferry pair-setup --explicit-pairing
   ```

7. Set up the keyring for encrypted history. Give user services WSLg's
   display, so the keyring prompt can open a window. Create
   `~/.config/environment.d/50-wslg.conf`:

   ```
   DISPLAY=:0
   WAYLAND_DISPLAY=wayland-0
   ```

   Apply it to the running session:

   ```sh
   systemctl --user import-environment DISPLAY WAYLAND_DISPLAY
   dbus-update-activation-environment --systemd DISPLAY WAYLAND_DISPLAY
   ```

   If `~/.local/share/keyrings/` does not exist, create a default keyring.
   Save this as `create-keyring.py` and run it with
   `/usr/bin/python3 create-keyring.py`; a window asks for the new keyring's
   password:

   ```python
   import gi
   gi.require_version("Secret", "1")
   from gi.repository import Secret

   service = Secret.Service.get_sync(Secret.ServiceFlags.NONE, None)
   Secret.Collection.create_sync(
       service, "Login", "default", Secret.CollectionCreateFlags.NONE, None
   )
   ```

   If no window appears, run `systemctl --user restart gnome-keyring-daemon`
   and try again. Then `blueferry storage-unlock` should report
   `"storage_state": "ready"`. Alternatively, choose unencrypted history with
   `blueferry storage-policy-set unencrypted`.

## Limits

- **After every restart**, `wsl --shutdown` or a Windows reboot, run
  `usbipd attach` again; `usbipd bind` persists. The firmware service
  handles the firmware, whether the adapter is attached before or after it
  runs. The keyring starts locked, so run `blueferry storage-unlock` and
  enter its password; until then BlueFerry sends messages but keeps no
  history.
- **ANCS may not return after the iPhone leaves range.** Debian 13's BlueZ
  5.82 predates the bearer API, so BlueFerry cannot dial the LE link itself
  and waits for the iPhone to reconnect to its advertisement. On the setup
  above, Classic Bluetooth, and with it messages, came back on its own, but
  the LE link stayed down overnight, and the clients showed the hint about
  stale Bluetooth state. Turning Bluetooth off and on in the iPhone's
  Control Center brought ANCS back without restarting `bluetooth.service`.
- **Polkit rule scope.** The WSL2 rule matches a user name instead of an
  active local session, so it also applies to that user's SSH and background
  sessions. It can still only start the class-setting unit.
- **WirePlumber setting scope.** Disabling seat monitoring keeps the
  Bluetooth monitor running regardless of which session is active. On a
  single-user WSL2 distribution that is the intended behavior; do not copy
  this setting to a shared desktop machine.
- USB/IP adds latency to every HCI packet. Pairing and MAP/PBAP worked on
  the setup above. An internal Intel AX200 shared the same way logged
  `hci0: Invalid exception type` firmware faults during pairing attempts;
  that test ran before the WirePlumber fix, so it is unclear whether the
  AX200 or USB/IP was to blame.
