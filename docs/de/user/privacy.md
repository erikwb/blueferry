# Datenschutz und Speicher

BlueFerry läuft vollständig auf deinem Rechner. Es spricht über Bluetooth
direkt mit deinem iPhone und schickt nichts an einen Server.

## Mitteilungen

Auf der iPhone-Seite des Clients wählst du einen von drei Modi:

| Modus (im Client) | Desktop-Mitteilungen |
| --- | --- |
| Messages only (Standard) | Neue Nachrichten |
| All iPhone notifications | Neue Nachrichten und Mitteilungen anderer iPhone-Apps |
| None | Keine |

Mitteilungen anderer Apps werden angezeigt und danach verworfen. Sie landen
nie im Nachrichtenverlauf und werden nie an andere Programme verteilt.
Nachrichten, die sowohl über MAP als auch über ANCS ankommen, erscheinen nur
einmal. Die ANCS-Kopie einer Mitteilung aus Apple Messages (Titel, Untertitel
und höchstens die ersten 1024 Byte des Texts) bleibt im lokalen Verlauf, im unten
gewählten Speichermodus, weil sie die Gruppenangaben enthält, die MAP fehlen.

Standardmäßig markiert das Schließen einer Nachrichtenmitteilung die
Nachricht auch auf dem iPhone als gelesen. Manche „Nicht stören“- oder
„Blockieren“-Aktionen von Benachrichtigungszentralen schließen Mitteilungen,
statt sie nur auszublenden. Dann würden Nachrichten als gelesen markiert,
ohne dass du sie gesehen hast. Mit `BLUEFERRY_MARK_READ_ON_DISMISS=false`
schaltest du das ab.

### Apps filtern

Im Modus **All iPhone notifications** kannst du über die genaue Bundle-ID
festlegen, welche Apps Mitteilungen erzeugen. Groß- und Kleinschreibung
zählt:

```bash
# Alle Apps außer diesen erlauben:
BLUEFERRY_ANCS_APP_BLOCKLIST=com.example.Chat,com.example.Mail

# Oder nur diese erlauben:
# BLUEFERRY_ANCS_APP_ALLOWLIST=com.example.Calendar,com.example.Reminders
```

Sind beide gesetzt, gewinnt die Blockliste. Eine leere Erlaubnisliste
blockiert alle Apps außer Nachrichten. Bundle-IDs findest du im Log, während
die App eine Mitteilung schickt. BlueFerry protokolliert jede App einmal,
ohne Inhalt:

```bash
journalctl --user -u blueferry -f | grep "ANCS app observed"
```

### Klickregeln

[Klickregeln](notification-click-rules.md) öffnen beim Klick auf das Popup
einer App eine feste Adresse oder App. Nichts aus der Mitteilung wird
weitergegeben. Die Regeln liegen unverschlüsselt in `settings.json`; der
Benachrichtigungsserver sieht sie nie, nur eine feste Aktion „Öffnen“ und
eine zufällige ID pro Popup.

## Lokale Daten

BlueFerry speichert Nachrichtenverlauf und einen Kontakt-Cache, damit
Unterhaltungen einen Neustart überstehen. Du entscheidest, wie:

| Speichermodus | Verhalten |
| --- | --- |
| Verschlüsselt (Standard) | Verschlüsselt mit einem Zufallsschlüssel aus GNOME Schlüsselbund oder KDE Wallet |
| Unverschlüsselt | Ohne Verschlüsselung gespeichert |
| Keine lokalen Daten behalten | Verlauf und Kontakte werden nicht auf die Festplatte geschrieben |

- Ist der Schlüsselbund gesperrt, kommen neue Nachrichten trotzdem an.
  Gespeicherter Verlauf und Kontaktnamen warten, bis du ihn entsperrst (im
  Client oder mit `blueferry storage-unlock`).
- Ein Wechsel des Speichermodus leert den bisherigen Cache, damit
  verschlüsselte und unverschlüsselte Daten nie gemischt werden.
- Markierte Unterhaltungen, gespeicherte Gruppenmitglieder und
  Gruppenbestätigungen folgen demselben Speichermodus.
- `blueferry history-clear` löscht den lokalen Nachrichtenverlauf.

Mit den optionalen [Kontaktfotos](contact-photos.md) enthält der
Kontakt-Cache auch die Bilder deiner Kontakte, im selben Speichermodus. Für
Popup-Symbole legt das Backend kurzlebige Kopien, die nur dir gehören, in
`$XDG_RUNTIME_DIR/blueferry` ab. Schaltest du die Option aus, löscht der
nächste Backend-Start die gespeicherten Fotos.

Die Verschlüsselung schützt gespeicherte Daten. Andere Programme, die unter
deinem Benutzer laufen, können trotzdem die D-Bus-API von BlueFerry nutzen
und bei entsperrtem Schlüsselbund unter Umständen dessen Geheimnisse lesen.

## Dateien und Ordner

| Pfad | Inhalt |
| --- | --- |
| `~/.config/blueferry/` | Konfiguration (`local.env`) und Einstellungen (`settings.json`) |
| `~/.local/state/blueferry/` | Nachrichtenverlauf, Kontakt-Cache, bereinigte Kopplungsberichte |

Beim Deinstallieren der Pakete bleiben beide Ordner erhalten.

## Einstellungen in local.env

Bearbeite `~/.config/blueferry/local.env` und starte den Dienst danach mit
`systemctl --user restart blueferry` neu.

| Einstellung | Standard | Bedeutung |
| --- | --- | --- |
| `BLUEFERRY_SHOW_NOTIFICATION_CONTENT` | `true` | Nachrichtentext in Mitteilungen zeigen |
| `BLUEFERRY_NOTIFICATION_TIMEOUT_MS` | `8000` | Anzeigedauer einer Mitteilung (1000–60000) |
| `BLUEFERRY_MARK_READ_ON_DISMISS` | `true` | Schließen einer Mitteilung markiert die Nachricht auf dem iPhone als gelesen |
| `BLUEFERRY_HISTORY_RETENTION_DAYS` | `30` | Aufbewahrung des Verlaufs in Tagen (1–3650) |
| `BLUEFERRY_HISTORY_MAX_EVENTS` | `10000` | Höchstzahl gespeicherter Ereignisse (100–1000000) |
| `BLUEFERRY_HISTORY_MAX_PAYLOAD_BYTES` | `268435456` | Höchstmenge gespeicherter Daten in Byte (16 MiB–2 GiB) |
| `BLUEFERRY_KEEP_PHONE_AUDIO_ON_PHONE` | `true` | Anrufe und Musik auf dem iPhone lassen |
| `BLUEFERRY_ANCS_APP_ALLOWLIST` | nicht gesetzt | Siehe [Apps filtern](#apps-filtern) |
| `BLUEFERRY_ANCS_APP_BLOCKLIST` | nicht gesetzt | Siehe [Apps filtern](#apps-filtern) |
| `BLUEFERRY_CONTACT_PHOTOS` | `false` | Kontaktfotos für Avatare und Popup-Symbole behalten; siehe [Kontaktfotos](contact-photos.md) |

Die Kopplung schreibt Telefon- und Adapter-Einstellungen in dieselbe Datei.
Ändere diese durch eine neue Kopplung statt von Hand.

## Mediensteuerung

Die optionale [Mediensteuerung](media-control.md) ist standardmäßig aus.
Ist sie an, bleiben Titel, Interpret, Album und App-Name in BlueFerry; Clients
holen sie über die authentifizierte D-Bus-Schnittstelle. Zu deiner Musik
wird nichts auf die Festplatte oder ins Protokoll geschrieben. Gespeichert
wird nur die Wahl an/aus, in `settings.json`. Die getrennte MPRIS-Option macht
den aktuellen Titel bewusst für jede Anwendung in deiner Sitzung lesbar, wie
bei jedem Desktop-Musikplayer.

## Anrufe und Musik bleiben auf dem iPhone

Mit WirePlumber 0.5 oder neuer schreibt BlueFerry vor der Kopplung
`~/.config/wireplumber/wireplumber.conf.d/99-blueferry-keep-phone-audio.conf`.
Damit dient der Rechner dem iPhone nicht als Bluetooth-Lautsprecher oder
-Headset, und Anrufe und Musik bleiben auf dem Telefon. Mit
`BLUEFERRY_KEEP_PHONE_AUDIO_ON_PHONE=false` entfernst du diese Datei.
