# Letzte iPhone-Mitteilungen

BlueFerry kann eine Liste der letzten App-Mitteilungen des iPhones (außer
Nachrichten) für den Reiter **Notifications** des KDE- und des GTK-Clients
und die Kommandozeile führen. Die Liste liegt nur im Speicher des Hintergrunddienstes
und wird nie auf die Festplatte geschrieben.

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
```

## Was behalten wird

- Die letzten 200 Mitteilungen, nur im Speicher. Nach jedem Neustart des
  Hintergrunddienstes ist die Liste leer, und **Clear History** leert sie.
- Titel, Untertitel und Text nur, solange **Show message content in popups**
  an ist; sonst nur App und Zeit.
- Aktualisiert das iPhone eine Mitteilung, ersetzt sie ihren Eintrag.
- Es ist ein Verlauf, kein Spiegel der iPhone-Mitteilungszentrale:
  Mitteilungen, die du am iPhone wegwischst, bleiben in der Liste.
- Solange die verschlüsselten lokalen Daten gesperrt sind, wird die Liste
  verweigert.

## Datenschutz

Die Liste gibt es nur über BlueFerrys authentifizierte, ratenbegrenzte
D-Bus-Methode; das Änderungssignal enthält nichts. Die Clients lesen sie nur,
solange ihre Liste sichtbar ist, und vergessen sie danach. Nichts von diesen
Mitteilungen landet in BlueFerrys Verlauf, der nur Nachrichten behält.
