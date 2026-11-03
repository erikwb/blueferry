# Apps stummschalten

Mit **All iPhone Notifications** bekommt jede iPhone-App ein Desktop-Popup.
Eine stummgeschaltete App zeigt keine Popups mehr, sonst ändert sich nichts:
Die Mitteilung kommt weiterhin auf dem iPhone an, und nichts wird an das
iPhone gesendet.

## Stummschalten und wieder einschalten

```bash
blueferry notifications apps                     # Apps seit dem Start des Dienstes
blueferry notifications mute com.example.Chat    # Popups ausblenden
blueferry notifications unmute com.example.Chat  # Popups wieder zeigen
```

Die Liste zeigt die Apps, die seit dem Start des Hintergrunddienstes eine
Mitteilung geschickt haben, dazu alle stummgeschalteten Apps, vor denen
`muted` steht. Eine Änderung gilt ab der nächsten Mitteilung, ein Neustart
ist nicht nötig. Über die Bundle-ID kannst du eine App auch stummschalten,
bevor sie überhaupt eine Mitteilung geschickt hat. BlueFerry holt App-Mitteilungen
nur, solange **All iPhone Notifications** gewählt ist; mit einer anderen
Richtlinie enthält die Liste nur die stummgeschalteten Apps.

Der Terminal-Client hat dieselbe Liste unter Settings › Notifications ›
**Muted apps**, mit einem Kästchen je App, und blendet die Einträge
stummgeschalteter Apps in seiner Mitteilungsliste ab. Die grafischen Clients
bekommen sie mit einer separaten Änderung; bis dahin nutze die Kommandozeile
oder den Terminal-Client.

## Was Stummschalten tut und was nicht

- Nur das Desktop-Popup entfällt. Eine stummgeschaltete Telefon-App blendet
  auch das Popup für verpasste Anrufe aus, die über ihre Mitteilung kamen.
- Apple Nachrichten lässt sich hier nicht stummschalten, ihre Popups kommen
  über MAP. Für Nachrichten gibt es die Einstellung „nur Kontakte“ und die
  Mitteilungsrichtlinie.
- Das Kopieren von Einmalcodes liest nur Nachrichten und ist nicht
  betroffen.
- Höchstens 100 Apps lassen sich stummschalten.

## Stummschalten und der Filter in local.env

`BLUEFERRY_ANCS_APP_ALLOWLIST` und `BLUEFERRY_ANCS_APP_BLOCKLIST` in
`local.env` bleiben ein harter Filter, der zuerst greift (siehe
[Apps filtern](privacy.md#apps-filtern)). Eine dadurch ausgeschlossene App
wird gar nicht erst vom iPhone geholt, erscheint also nie in der Liste und
lässt sich nicht stummschalten. Stummschalten ist für die Apps gedacht, die
den Filter passieren. Ist eine der Variablen gesetzt, weist die Liste darauf
hin, damit du weißt, warum manche Apps nie auftauchen.

## Speicherung und Privatsphäre

- Die Liste der gesehenen Apps (Bundle-ID, Anzeigename, Zeitpunkt der
  letzten Mitteilung) liegt nur im Arbeitsspeicher, höchstens 64 Apps, und
  ist weg, sobald der Dienst endet. Beim Eintreffen einer Mitteilung wird
  nichts auf die Festplatte geschrieben.
- Stummgeschaltete Apps werden mit ihrem Anzeigenamen als
  `ancs_muted_apps` in `~/.config/blueferry/settings.json` gespeichert, im
  selben [Speichermodus](privacy.md#lokale-daten) wie markierte Unterhaltungen
  (standardmäßig verschlüsselt). Ein Wechsel des Speichermodus löscht sie.
- Solange der Schlüsselbund gesperrt ist oder **Keine lokalen Daten
  behalten** gewählt ist, ist keine App stummgeschaltet und Stummschalten
  nicht möglich.
  BlueFerry überschreibt die gespeicherte Liste nie, solange es sie nicht
  lesen kann.
- Solange `settings.json` selbst nicht lesbar ist, wird Stummschalten und
  Aufheben abgelehnt; die Datei reparieren oder entfernen und erneut
  versuchen. Vorher stummgeschaltete Apps bleiben stumm. Ist der Dienst mit
  der beschädigten Datei gestartet, erscheinen ihre Popups, bis die Datei
  wieder lesbar ist.
- Das D-Bus-Signal `NotificationAppsChanged` trägt keinen Inhalt; Clients
  holen die Liste mit `ListNotificationApps`. `GetStatus` meldet nur die
  Anzahl stummgeschalteter Apps (`notification_muted_app_count`).

## Grenzen

- Stummschalten ist bisher nur mit simulierten Mitteilungen getestet, noch
  nicht mit einem echten iPhone.
