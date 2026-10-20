# Betrieb unter WSL2

BlueFerry läuft in einer WSL2-Distribution unter Windows, aber WSL2 hat kein
eigenes Bluetooth. Der Adapter wird per USB/IP von Windows durchgereicht, und
mehrere Teile des Linux-Stacks verhalten sich anders als bei einer
Desktop-Installation: das Laden der Firmware, WirePlumbers Bluetooth-Monitor,
polkit, der Desktop-Schlüsselbund und obexds Dienst für
Nachrichtenbenachrichtigungen.

Eingerichtet wurde das unter Debian 13 (BlueZ 5.82, WirePlumber 0.5.8) mit
WSL 3.0.3 und einem TP-Link UB500 (RTL8761BU). WSL2 ist nicht Teil der
CI-Paketmatrix.

## Was anders ist

- **Der Adapter gehört Windows.** `usbipd-win` löst ihn von Windows und
  hängt ihn an WSL2 an. Solange er angehängt ist, kann Windows ihn nicht
  nutzen. Braucht Windows Bluetooth für Maus, Tastatur oder Headset, nimm
  einen eigenen USB-Dongle statt des eingebauten Funkmoduls.
- **Der Kernel sieht die Firmware der Distribution nicht.** Der WSL2-Kernel
  lädt Firmware aus dem Dateisystem der Hilfs-VM, nicht aus `/lib/firmware`
  in deiner Distribution. Adapter, deren Treiber Firmware lädt, etwa alle
  Realtek-Adapter, scheitern mit
  `Direct firmware load for rtl_bt/... failed with error -2`, auch wenn das
  Firmware-Paket installiert ist.
- **Terminals haben keinen Seat.** WSL2 startet beim Booten eine
  logind-Sitzung für den Standardbenutzer, aber ohne Seat, und Shells aus
  Windows Terminal gehören zu gar keiner Sitzung. WirePlumber startet seinen
  Bluetooth-Monitor nur, wenn der Seat `active` ist; hier bleibt er
  `online`, also meldet PipeWire nie A2DP- oder Freisprech-Endpunkte bei
  BlueZ an. `Device1.Connect` scheitert dann mit
  `br-connection-profile-unavailable`. Auf dem oben beschriebenen System
  koppelte die explizite Kopplung zwar, scheiterte danach aber mit „The
  iPhone's Classic Bluetooth connection did not settle", bis WirePlumber
  korrigiert war.
- **Polkit sieht keine aktive lokale Sitzung.** Die mitgelieferte Regel, mit
  der BlueFerry die Geräteklasse des Adapters setzt, verlangt
  `subject.active` und `subject.local`. Für Prozesse außerhalb einer Sitzung
  ist beides falsch.
- **Es gibt keinen Schlüsselbund, und nichts entsperrt ihn.** Der
  verschlüsselte Verlauf braucht einen Standard-Schlüsselbund. Eine
  Distribution ohne grafische Anmeldung hat keinen, die Passwortabfrage kann
  kein Fenster öffnen, weil Benutzerdienste WSLgs `DISPLAY` nicht kennen, und
  ohne PAM-Anmeldung wird der Schlüsselbund nie automatisch entsperrt. Bis er
  eingerichtet ist, sendet BlueFerry Nachrichten, speichert aber keinen
  Verlauf, und die Clients zeigen keine Unterhaltungen.
- **obexd meldet unter Umständen keine Profile an.** Ohne
  `evolution-data-server` schlagen die PBAP- und IrMC-Server-Plugins von
  obexd beim Start bei einer Registry-Abfrage fehl, die obexds BlueZ-Watch
  ausführt, bevor seine MAP-Server existieren. obexd meldet seine Profile
  nur in diesem Watch an, also wird keines angemeldet, darunter der Message
  Notification Server (`0x1133`), über den das iPhone neue Nachrichten
  meldet. Lesen und Senden funktionieren weiter, aber neue Nachrichten
  kommen nie an, und der Daemon protokolliert „the iPhone did not open MAP
  notifications". Das betrifft nicht nur WSL2.

## Einrichtung

1. Installiere unter Windows
   [usbipd-win](https://github.com/dorssel/usbipd-win). Suche den Adapter in
   einer Administrator-PowerShell, gib ihn einmal frei und hänge ihn an:

   ```powershell
   usbipd list
   usbipd bind --busid <BUSID>
   usbipd attach --wsl --busid <BUSID>
   ```

   In WSL2 zeigt `lsusb` den Adapter jetzt an.

2. Installiere das Firmware-Paket des Adapters in der Distribution, für
   Realtek unter Debian `sudo apt install firmware-realtek`. Kopiere die
   Firmware dann nach `/mnt/wsl`, das der WSL2-Kernel lesen kann, und nimm
   das Verzeichnis in den Firmware-Suchpfad des Kernels auf. Beides geht bei
   jedem Neustart verloren, also erledigt das ein Dienst beim Booten.
   Speichere dies als `/usr/local/sbin/wsl-bt-firmware`:

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

   und dies als `/etc/systemd/system/wsl-bt-firmware.service`:

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

   Aktiviere ihn dann:

   ```sh
   sudo chmod 755 /usr/local/sbin/wsl-bt-firmware
   sudo systemctl enable --now wsl-bt-firmware.service
   sudo systemctl restart bluetooth
   ```

   `sudo dmesg | grep -i rtl` sollte zeigen, dass Firmware- und
   Konfigurationsdatei geladen werden, und `bluetoothctl show` einen
   eingeschalteten Controller. Für einen Adapter eines anderen Herstellers
   kopiere dessen Firmware-Verzeichnis statt `rtl_bt`.

3. Lass WirePlumber seinen Bluetooth-Monitor ohne aktiven Seat starten. Lege
   `~/.config/wireplumber/wireplumber.conf.d/50-wsl2-no-seat-monitoring.conf`
   an:

   ```
   wireplumber.profiles = {
     main = {
       monitor.bluez.seat-monitoring = disabled
     }
   }
   ```

   Führe dann `systemctl --user restart wireplumber` aus. `bluetoothctl show`
   listet jetzt die UUIDs `Audio Sink`, `Audio Source` und `Handsfree`.

4. Erlaube deinem Benutzer den Helfer für die Geräteklasse. Lege als
   Administrator `/etc/polkit-1/rules.d/49-blueferry-cod-wsl2.rules` an und
   ersetze `you` durch deinen Benutzernamen:

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

   Sie erlaubt dieselbe einzelne, abgeschottete Unit wie die mitgelieferte
   Regel.

5. Starte obexd ohne seine PBAP- und IrMC-Server-Plugins, damit er den
   Message Notification Server anmeldet. BlueFerry nutzt nur obexds
   PBAP-Client, und der ist nicht betroffen. Lege
   `~/.config/systemd/user/obex.service.d/50-no-pbap-server.conf` an:

   ```ini
   [Service]
   ExecStart=
   ExecStart=/usr/libexec/bluetooth/obexd -P pbap,irmc
   ```

   Führe dann `systemctl --user daemon-reload` und
   `systemctl --user restart obex` aus. `bluetoothctl show` listet jetzt
   `Message Notification Se..` (`00001133-…`). Ist `evolution-data-server`
   installiert und die UUID schon vorhanden, überspringe diesen Schritt.

6. Kopple wie gewohnt. Mit einem Adapter, der nicht standardmäßig explizit
   koppelt, gib `--explicit-pairing` an, wenn Connect-first scheitert:

   ```sh
   blueferry pair-setup --explicit-pairing
   ```

7. Richte den Schlüsselbund für den verschlüsselten Verlauf ein. Gib den
   Benutzerdiensten WSLgs Display, damit die Passwortabfrage ein Fenster
   öffnen kann. Lege `~/.config/environment.d/50-wslg.conf` an:

   ```
   DISPLAY=:0
   WAYLAND_DISPLAY=wayland-0
   ```

   Übernimm das in die laufende Sitzung:

   ```sh
   systemctl --user import-environment DISPLAY WAYLAND_DISPLAY
   dbus-update-activation-environment --systemd DISPLAY WAYLAND_DISPLAY
   ```

   Existiert `~/.local/share/keyrings/` nicht, lege einen
   Standard-Schlüsselbund an. Speichere dies als `create-keyring.py` und
   führe es mit `/usr/bin/python3 create-keyring.py` aus; ein Fenster fragt
   nach dem Passwort für den neuen Schlüsselbund:

   ```python
   import gi
   gi.require_version("Secret", "1")
   from gi.repository import Secret

   service = Secret.Service.get_sync(Secret.ServiceFlags.NONE, None)
   Secret.Collection.create_sync(
       service, "Login", "default", Secret.CollectionCreateFlags.NONE, None
   )
   ```

   Erscheint kein Fenster, führe
   `systemctl --user restart gnome-keyring-daemon` aus und versuche es
   erneut. Danach sollte `blueferry storage-unlock`
   `"storage_state": "ready"` melden. Alternativ wählst du mit
   `blueferry storage-policy-set unencrypted` einen unverschlüsselten
   Verlauf.

## Grenzen

- **Nach jedem Neustart**, also `wsl --shutdown` oder einem
  Windows-Neustart, führe `usbipd attach` erneut aus; `usbipd bind` bleibt
  erhalten. Um die Firmware kümmert sich der Dienst, egal ob der Adapter vor
  oder nach ihm angehängt wird. Der Schlüsselbund startet gesperrt, also
  führe `blueferry storage-unlock` aus und gib sein Passwort ein; bis dahin
  sendet BlueFerry Nachrichten, speichert aber keinen Verlauf.
- **Reichweite der polkit-Regel.** Die WSL2-Regel prüft einen Benutzernamen
  statt einer aktiven lokalen Sitzung und gilt daher auch für SSH- und
  Hintergrundsitzungen dieses Benutzers. Sie kann trotzdem nur die Unit
  starten, die die Geräteklasse setzt.
- **Reichweite der WirePlumber-Einstellung.** Ohne Seat-Überwachung läuft der
  Bluetooth-Monitor unabhängig davon, welche Sitzung aktiv ist. In einer
  WSL2-Distribution mit einem Benutzer ist das gewollt; übernimm die
  Einstellung nicht auf einen gemeinsam genutzten Desktop-Rechner.
- USB/IP verzögert jedes HCI-Paket. Kopplung und MAP/PBAP funktionierten auf
  dem oben beschriebenen System. Ein auf dieselbe Weise durchgereichter
  interner Intel AX200 meldete bei Kopplungsversuchen
  `hci0: Invalid exception type`-Firmwarefehler; dieser Test lief vor der
  WirePlumber-Korrektur, daher ist unklar, ob der AX200 oder USB/IP schuld
  war.
