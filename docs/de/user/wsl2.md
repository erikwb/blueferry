# Betrieb unter WSL2

BlueFerry läuft in einer WSL2-Distribution unter Windows, aber WSL2 hat kein
eigenes Bluetooth. Der Adapter wird per USB/IP von Windows durchgereicht, und
drei Teile des Linux-Stacks verhalten sich anders als bei einer
Desktop-Installation: das Laden der Firmware, WirePlumbers Bluetooth-Monitor
und polkit.

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
   das Verzeichnis in den Firmware-Suchpfad des Kernels auf. Für einen
   RTL8761BU:

   ```sh
   sudo mkdir -p /mnt/wsl/firmware/rtl_bt
   sudo cp /lib/firmware/rtl_bt/rtl8761bu_fw.bin \
     /lib/firmware/rtl_bt/rtl8761bu_config.bin /mnt/wsl/firmware/rtl_bt/
   echo -n /mnt/wsl/firmware | sudo tee /sys/module/firmware_class/parameters/path
   sudo modprobe -r btusb && sudo modprobe btusb
   sudo systemctl restart bluetooth
   ```

   `sudo dmesg | grep -i rtl` sollte zeigen, dass Firmware- und
   Konfigurationsdatei geladen werden, und `bluetoothctl show` einen
   eingeschalteten Controller.

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

5. Kopple wie gewohnt. Mit einem Adapter, der nicht standardmäßig explizit
   koppelt, gib `--explicit-pairing` an, wenn Connect-first scheitert:

   ```sh
   blueferry pair-setup --explicit-pairing
   ```

## Grenzen

- **Schritt 1 und 2 überstehen keinen Neustart.** Nach `wsl --shutdown` oder
  einem Windows-Neustart führe `usbipd attach` erneut aus und wiederhole das
  Kopieren der Firmware, den Suchpfad und das Neuladen von `btusb`.
  `/mnt/wsl` ist ein tmpfs, und der Suchpfad wird mit dem Kernel
  zurückgesetzt. `usbipd bind` bleibt erhalten.
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
