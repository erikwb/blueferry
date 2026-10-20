# Running under WSL2

BlueFerry runs in a WSL2 distribution on Windows, but WSL2 has no Bluetooth
of its own. The adapter is passed in from Windows over USB/IP, and three
parts of the Linux stack behave differently from a desktop install: firmware
loading, WirePlumber's Bluetooth monitor, and polkit.

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
   firmware search path. For an RTL8761BU:

   ```sh
   sudo mkdir -p /mnt/wsl/firmware/rtl_bt
   sudo cp /lib/firmware/rtl_bt/rtl8761bu_fw.bin \
     /lib/firmware/rtl_bt/rtl8761bu_config.bin /mnt/wsl/firmware/rtl_bt/
   echo -n /mnt/wsl/firmware | sudo tee /sys/module/firmware_class/parameters/path
   sudo modprobe -r btusb && sudo modprobe btusb
   sudo systemctl restart bluetooth
   ```

   `sudo dmesg | grep -i rtl` should show the firmware and config files
   loading, and `bluetoothctl show` a powered controller.

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

5. Pair as usual. With an adapter that does not default to explicit pairing,
   pass `--explicit-pairing` if Connect-first fails:

   ```sh
   blueferry pair-setup --explicit-pairing
   ```

## Limits

- **Nothing in steps 1 and 2 survives a restart.** After `wsl --shutdown` or
  a Windows reboot, run `usbipd attach` again and repeat the firmware copy,
  search path and `btusb` reload. `/mnt/wsl` is a tmpfs and the search path
  resets with the kernel. `usbipd bind` persists.
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
