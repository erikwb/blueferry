# Letzte iPhone-Mitteilungen

BlueFerry kann eine Liste der letzten App-Mitteilungen des iPhones (außer
Nachrichten) für den Reiter **Notifications** des KDE- und des GTK-Clients
und die Kommandozeile führen. Die Liste liegt nur im Speicher des
Hintergrunddienstes; nur Mitteilungen, die du **anheftest**, werden gespeichert
und überstehen so einen Neustart.

Sie ist **standardmäßig aus**.

## Einschalten

Im KDE- oder GTK-Client **Settings › Notifications** öffnen und **Keep
recent iPhone notifications** einschalten (der Knopf **Open Notification
Settings** im Reiter führt dorthin), oder:

```bash
blueferry features on notification_history
```

Beides gilt nach einem Neustart des Hintergrunddienstes; die
Einstellungsseite bietet **Restart Service** an.
`BLUEFERRY_NOTIFICATION_HISTORY=true` in `~/.config/blueferry/local.env`
bewirkt dasselbe.

Mitteilungen anderer Apps erreichen BlueFerry nur mit **All iPhone
Notifications** unter **Show Popups**.

## Benutzen

Der Reiter **Notifications** zeigt die neuesten zuerst, mit App, Zeit und,
wenn erlaubt, Titel und Text; der Terminal-Client zeigt dieselbe Liste
unter **iPhone Notifications** in seiner Befehlspalette. Im Terminal:

```bash
blueferry notifications recent             # die letzten 20
blueferry notifications recent --limit 100
blueferry notifications recent --json      # Rohliste samt Schlüsseln
```

Einträge, die das iPhone entfernt hat (dort weggewischt, geöffnet oder
gelöscht), sind mit **Removed on iPhone** markiert; sie bleiben in der Liste.

## Mitteilungen anheften

Heftest du eine Mitteilung an, bleibt sie erhalten: Angeheftete Mitteilungen
stehen oben (die zuletzt angeheftete zuerst), bleiben, bis du sie wieder
löst, und überstehen einen Neustart des Hintergrunddienstes – auch wenn das
iPhone sie entfernt hat oder sie aus den letzten 200 herausgefallen sind. In
den Clients dient **Pin** / **Unpin** am Eintrag dazu. Im KDE-Client
erscheint die Anheft-Taste, wenn du auf einen Eintrag zeigst oder ihn mit der
Tastatur auswählst; ein angehefteter Eintrag behält seine Taste **Unpin** und
trägt eine Nadel am Symbol. Auf dem iPhone entfernte Einträge und Einträge
[stummgeschalteter Apps](notification-mute.md) sind ausgegraut. Sind 20
angeheftet, ist **Pin** gesperrt und der Reiter sagt das; schlägt das
Anheften oder Lösen fehl, steht der Grund über der Liste. Im Terminal mit dem
Schlüssel, der am Ende jeder Zeile steht:

```bash
blueferry notifications pin SCHLÜSSEL
blueferry notifications unpin SCHLÜSSEL
```

- Höchstens 20 Mitteilungen lassen sich anheften, und ihr gespeicherter Text
  ist insgesamt auf 64 KiB begrenzt. Ist eine Grenze erreicht, zuerst eine
  lösen.
- Eine angeheftete Mitteilung ist eine Momentaufnahme: Nach einem erneuten
  Verbinden oder Neustart ist sie nicht mehr mit der Mitteilung auf dem
  iPhone verknüpft.
- Eine gelöste Mitteilung wird vergessen, sobald sie aus der Liste der
  letzten Mitteilungen fällt.

## Was behalten wird

- Die letzten 200 Mitteilungen, nur im Speicher. Bis auf angeheftete
  Mitteilungen ist die Liste nach jedem Neustart des Hintergrunddienstes
  leer. **Clear History** leert sie, angeheftete Mitteilungen eingeschlossen.
- Titel, Untertitel und Text nur, solange **Show message content in popups**
  an ist; sonst nur App und Zeit.
- Aktualisiert das iPhone eine Mitteilung, ersetzt sie ihren Eintrag.
- Es ist ein Verlauf, kein Spiegel der iPhone-Mitteilungszentrale:
  Mitteilungen, die du am iPhone wegwischst, bleiben in der Liste, markiert
  mit **Removed on iPhone**.
- Angeheftete Mitteilungen liegen in BlueFerrys Einstellungsdatei, geschützt
  wie der Nachrichtenverlauf: standardmäßig mit dem Schlüsselbund
  verschlüsselt, mit **Keine lokalen Daten behalten** nie gespeichert (dann
  nur im Speicher) und nur lesbar, solange die lokalen Daten entsperrt sind.
  Jede behält genau, was die Liste zeigte; wird **Show message content in
  popups** später ausgeschaltet, wird auch ihr gespeicherter Text entfernt.
  Ein Wechsel des Speichermodus löst alle.
- Solange die verschlüsselten lokalen Daten gesperrt sind, werden die Liste
  und das Anheften verweigert.
- Ist die Liste ausgeschaltet, löscht der Hintergrunddienst beim Start
  gespeicherte angeheftete Mitteilungen, damit nichts zurückbleibt.

## Datenschutz

Die Liste gibt es nur über BlueFerrys authentifizierte, ratenbegrenzte
D-Bus-Methoden; das Änderungssignal enthält nichts. Die Clients lesen sie nur,
solange ihre Liste sichtbar ist, und vergessen sie danach. Nichts von diesen
Mitteilungen landet in BlueFerrys Verlauf, der nur Nachrichten behält;
gespeichert werden nur angeheftete Mitteilungen wie oben beschrieben.
