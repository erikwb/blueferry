# Mitteilungen mit dem iPhone abgleichen

Mit **All iPhone Notifications** zeigt BlueFerry für jede Mitteilung einer
iPhone-App ein Desktop-Popup. Normalerweise bleibt dieses Popup stehen, bis
es abläuft, auch wenn die Mitteilung auf dem Telefon schon entfernt wurde.
Mit dieser optionalen Einstellung schließt sich das Popup auf dem Desktop,
sobald die Mitteilung auf dem iPhone entfernt oder weggewischt wird.

## Einschalten

In den iPhone-Einstellungen des Qt-Clients unter *Desktop Notifications*
**Sync notifications with iPhone** einschalten, oder:

```bash
blueferry notifications sync enable
blueferry notifications sync status
blueferry notifications sync disable
```

Die Wahl wird als `mirror_iphone_removals` in
`~/.config/blueferry/settings.json` gespeichert und gilt sofort. `GetStatus`
meldet sie als `mirror_iphone_removals`.

## Was es tut und was nicht

- Es schließt nur Popups. Es sendet nie etwas an das iPhone, und ein auf
  dem Desktop geschlossenes Popup entfernt die Mitteilung auf dem Telefon
  nicht.
- Betroffen sind nur Popups von iPhone-App-Mitteilungen. Popups von
  Nachrichten kommen über MAP und behalten ihr eigenes Verhalten.
- Popups mit [Aktions-Knöpfen](notification-actions.md) schliessen sich
  schon heute, wenn ihre Mitteilung auf dem iPhone entfernt wird, mit oder
  ohne diese Einstellung.
- iOS nummeriert Mitteilungen pro Bluetooth-Sitzung und verwendet die
  Nummern wieder. Wird die Verbindung neu aufgebaut oder spielt das iPhone
  seine vorhandenen Mitteilungen erneut ab, vergisst BlueFerry, welches
  Popup zu welcher Nummer gehörte. Ältere Popups laufen dann einfach ab.

## Grenzen

- Standardmäßig aus.
- Der GTK-, Quickshell- und Terminal-Client haben noch keinen Schalter;
  dort die CLI verwenden.
- Es setzt voraus, dass iOS das Entfernen über ANCS meldet
  (`NotificationRemoved`). Die automatischen Tests verwenden simulierte
  ANCS-Ereignisse; mit einem echten iPhone ist es nur auf einem
  Entwickler-System im Alltag in Gebrauch, ohne gezielten Test.

## Datenschutz

BlueFerry behält nur die Nummer der Mitteilung und die Popup-ID im
Arbeitsspeicher, höchstens 64 Einträge. Nichts wird protokolliert,
gespeichert oder auf D-Bus gesendet.
