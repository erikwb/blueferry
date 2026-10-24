# Neu verbinden und Selbstheilung

BlueFerry hält die klassische Bluetooth-Verbindung zum iPhone selbst aufrecht.
Diese Seite erklärt, was es tut, wenn das Telefon weg ist, wenn bluetoothd
nicht mehr antwortet und wenn ein Profil bei bestehender Verbindung hängt, und
wie du eingreifen kannst.

## Sofort neu verbinden

War das iPhone weg, versucht BlueFerry die klassische Verbindung mit wachsender
Pause erneut, bis zu 10 Minuten. Ein Telefon, das kein Lebenszeichen zeigt,
etwa weil auf dem iPhone Bluetooth ausgeschaltet wurde, ruft es nicht mehr
ständig an, sondern prüft nur noch alle 10 Minuten, bis sich das Telefon über
LE, eine Gerätesuche oder eine eingehende Verbindung wieder meldet. Einmal hat
das Anrufen eines abwesenden Telefons bluetoothd im Kernel hängen lassen; jeder
gesparte Versuch zählt.

Um sofort neu zu verbinden:

- KDE-Client: **Reconnect** neben **iPhone Link** unter Connection Health auf
  der iPhone-Seite. Das Hauptfenster zeigt eine Meldung mit der Aktion
  **Reconnect**, solange das Telefon nicht erreichbar ist oder ein Profil hängt.
- Terminal-Client: Taste `b`.
- Kommandozeile: `blueferry reconnect` wartet auf das Ergebnis
  (`--no-wait` kehrt sofort zurück).

„iPhone not reachable. Is Bluetooth switched on on the iPhone?“ heißt, der
Versuch ist gescheitert; prüfe Bluetooth auf dem Telefon. Ein weiterer Klick,
während ein Versuch läuft, startet keinen zweiten.

## Der Bluetooth-Dienst des Systems antwortet nicht

Antwortet bluetoothd nicht mehr über D-Bus (zwei unbeantwortete Pings im
Abstand von 15 Sekunden), stellt BlueFerry seine eigenen Verbindungsversuche
und die Adapter-Wiederherstellung ein, weil sie sich nur hinter dem hängenden
Aufruf anstellen würden, und die Clients melden „The system Bluetooth service
is not responding“. Hängt bluetoothd im Kernel (Prozesszustand D), hilft nur
ein Neustart des Rechners. Sobald bluetoothd wieder antwortet oder neu
gestartet wurde, macht BlueFerry von selbst weiter.

## Anrufe werden nie verfügbar (hängendes HFP)

Mit eingeschalteten [Anrufen](calls.md) lädt bluetoothd 5.87 mit `-E` sein
eigenes `hfp`-Plugin, das mit oFono um den Anrufkanal des iPhones konkurriert.
Gewinnt bluetoothd, etwa nach einem Neustart, läuft das Einschalten des
oFono-Modems immer wieder in eine Zeitüberschreitung
(`org.ofono.Error.Timedout` im Log), und Anrufe kommen nie zustande.

Dauerhaft hilft es, bluetoothd ohne dieses Plugin zu starten (`-E` behalten):

- OpenRC: `-P hfp` zu `BLUETOOTH_OPTS` in `/etc/conf.d/bluetooth` hinzufügen,
  dann `sudo rc-service bluetooth restart`.
- systemd: `-P hfp` in einem Drop-in zum `ExecStart` von bluetoothd hinzufügen
  (`sudo systemctl edit bluetooth.service`), dann
  `sudo systemctl restart bluetooth`.

`blueferry doctor` warnt, solange oFono neben einem bluetoothd läuft, das das
Plugin noch lädt.

Bis dahin heilt BlueFerry das selbst: Nach zwei Zeitüberschreitungen in Folge
trennt es das iPhone, wartet drei Sekunden und verbindet es neu. Das passiert
nur bei bestehender klassischer Verbindung, nie während eines Anrufs, während
bluetoothd nicht antwortet oder während einer Adapter-Wiederherstellung, und
höchstens alle zehn Minuten (bis zu einer Stunde, solange der Reset nicht
hilft). Der Reset unterbricht kurz auch die iPhone-Mitteilungen. Sonst wird **Reconnect** angeboten, und
`blueferry reconnect` führt denselben Reset auf Wunsch aus. `blueferry doctor`
zeigt die Einstellung und den letzten Reset.

Während eines Anrufs setzt **Reconnect** nichts zurück und meldet, dass ein
Anruf läuft; versuche es nach dem Anruf erneut.

Um den automatischen Reset auszuschalten, entferne in den Einstellungen (Qt,
GTK, Quickshell) das Häkchen bei **Reset a stuck iPhone connection
automatically**, drücke `B` im Terminal-Client oder führe
`blueferry reconnect --no-auto-reset` aus. Die Wahl wird als
`"auto_profile_reset"` in `~/.config/blueferry/settings.json` gespeichert,
gilt sofort und hat Vorrang vor `BLUEFERRY_AUTO_PROFILE_RESET=false` in
`~/.config/blueferry/local.env` (das einen Backend-Neustart braucht).

## „Please wait a moment, then try again“

BlueFerry begrenzt, wie oft eine Aktion angefragt werden darf. Die Meldung
heißt, die Grenze für diese Aktion ist erreicht; nach spätestens einer Minute
geht es wieder.

## Grenzen

Dämpfung, BlueZ-Zustandsprüfung und Reset sind gegen Attrappen getestet. Der
HFP-Auslöser, die BlueZ-Zustandsprüfung und das manuelle Neuverbinden sind auf
Hardware noch nicht gezielt getestet.
