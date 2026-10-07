# iPhone koppeln

Starte den Client, der zu deinem Desktop passt:

```bash
blueferry-gtk         # GNOME, Cinnamon und ähnliche Desktops
blueferry-qt          # KDE Plasma
blueferry-quickshell  # Quickshell
```

Der Terminal-Assistent `blueferry pair-setup` bietet denselben Ablauf.

## Schritte

1. Öffne auf dem iPhone **Einstellungen → Bluetooth** und lass es entsperrt.
2. Lass BlueFerry deinen Bluetooth-Controller prüfen.
3. Wähle in BlueFerry **Scan**, dann das iPhone und **Pair**. BlueFerry
   startet die Anfrage; du musst den Rechner auf dem iPhone nicht unter
   **Andere Geräte** antippen.
4. Wenn die Anfrage auf dem iPhone erscheint, bestätige sie und prüfe, dass
   beide Geräte denselben Code zeigen. Das kann rund 15 Sekunden dauern.
5. Tippe auf dem iPhone auf **ⓘ** neben dem Rechner und schalte die
   Schalter unten ein. Wenn iOS fragt, ob Systemmitteilungen erlaubt werden
   sollen, bestätige auch das.
6. Warte, bis Nachrichten und Kontakte in BlueFerry als verbunden angezeigt
   werden. Beim voreingestellten verschlüsselten Speicher bestätigst du
   zusätzlich die Abfrage des Desktop-Schlüsselbunds.

Nach der Einrichtung startet das Backend automatisch mit deiner Sitzung und
verbindet sich nach normalen Bluetooth-Unterbrechungen neu.

## Die Schalter am iPhone

Die Schalter stehen auf der **ⓘ**-Seite des Rechners in den
Bluetooth-Einstellungen des iPhones. iOS zeigt sie in der Sprache deines
Telefons:

| iOS auf Deutsch | iOS in English | Aktiviert in BlueFerry |
| --- | --- | --- |
| Mitteilungen zu Nachrichten anzeigen | Show Message Notifications | Nachrichten (MAP) |
| Kontakte synchronisieren | Sync Contacts | Kontakte (PBAP) |
| Systemmitteilungen teilen | Share System Notifications | iPhone-Mitteilungen und Gruppendetails (ANCS) |

- Die Schalter können ein paar Minuten brauchen. Fehlen sie, geh zurück zur
  Geräteliste und öffne die **ⓘ**-Seite ein paar Mal neu.
- Wenn das iPhone den Rechner zweimal auflistet, prüfe beide Einträge; die
  Schalter können bei jedem der beiden auftauchen.
- **Systemmitteilungen teilen** ist optional. Ohne diesen Schalter
  funktionieren normale Nachrichten und Kontakte, aber eine Gruppennachricht
  kann wie eine Einzelunterhaltung mit dem Absender aussehen.

## Kopplungsoptionen

Die meisten lassen beide Optionen ausgeschaltet. In den Clients heißen sie
auf Englisch:

- **Compatibility pairing for iOS 18 or earlier** (Kompatibilitätskopplung)
  behält das Signal bei, mit dem die Schalter für Nachrichten und Kontakte
  erscheinen, verbindet aber keine iPhone-Systemmitteilungen. BlueFerry wählt
  das auch automatisch, wenn der lokale Bluetooth-Stack sie nicht
  unterstützt.
- **Use explicit Bluetooth pairing** (explizite Kopplung) lässt BlueZ sofort
  koppeln, statt zuerst zu verbinden. Probier das nur, wenn die normale
  Kopplung auf deinem Controller immer wieder abbricht. Manche
  Realtek-Adapter nutzen es standardmäßig.

Der Terminal-Assistent kennt dieselben Optionen:

```bash
blueferry pair-setup
blueferry pair-setup --compatibility-mode
blueferry pair-setup --explicit-pairing     # oder --no-explicit-pairing
```

## Sauber neu beginnen

Alter Bluetooth-Zustand auf dem iPhone kann überleben, wenn du die Kopplung
nur auf einer Seite entfernst. Setze vor einer neuen Kopplung beide Seiten
zurück:

1. Auf dem iPhone: **Einstellungen → Bluetooth → ⓘ** neben dem Rechner →
   **Dieses Gerät ignorieren**. Das für jeden Eintrag mit dem Namen des
   Rechners.
2. Unter Linux: das iPhone in BlueFerry entfernen oder
   `bluetoothctl remove <iPhone-Adresse>` ausführen.
3. Wie oben beschrieben neu koppeln.

Wenn die Kopplung fehlschlägt, speichert BlueFerry einen bereinigten Bericht.
Siehe [Fehlerbehebung](troubleshooting.md#ein-problem-melden).
