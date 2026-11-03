# Überblick

BlueFerry verbindet deinen Linux-Rechner über Bluetooth direkt mit deinem
iPhone und nutzt dafür Bluetooth-Profile, die iOS von Haus aus unterstützt.
Ein Hintergrunddienst auf dem Rechner hält die Verbindung; die grafischen
Clients und der Terminal-Client zeigen deine Unterhaltungen an.

![GTK-Client von BlueFerry mit einer Beispielunterhaltung](../../images/gtk.png)

## Was funktioniert

- SMS, RCS und iMessage über das iPhone empfangen und senden.
- Neue Unterhaltungen beginnen und synchronisierte Kontakte durchsuchen.
- Kontakte synchronisieren, einschließlich Telefonnummern und
  Apple-ID-E-Mail-Adressen.
- Nachrichten am Desktop als gelesen markieren.
- Desktop-Mitteilungen für Nachrichten und auf Wunsch auch für andere
  iPhone-Apps anzeigen.
- Gruppenchats, sofern BlueFerry die Teilnehmer sicher erkennen kann.
- Den lokalen Verlauf mit GNOME Schlüsselbund oder KDE Wallet verschlüsseln.
- Native Clients für GTK, KDE/Kirigami, Quickshell und das Terminal.

## Was nicht funktioniert

Das sind Grenzen dessen, was iOS über Bluetooth anbietet, keine fehlenden
Einstellungen:

- BlueFerry kennt nur Nachrichten, die es während einer Verbindung sieht. Es
  lädt weder dein iCloud-Nachrichtenarchiv noch deinen vollständigen Verlauf
  gesendeter Nachrichten.
- Anhänge, Reaktionen und Tippanzeigen werden nicht unterstützt.
- Anrufe und FaceTime werden nicht unterstützt; Anrufe und Musik bleiben auf
  dem iPhone.
- Bluetooth liefert BlueFerry keine verlässliche Gruppen-ID und keine
  vollständige Mitgliederliste. Antworten in Gruppen sind deshalb bewusst
  vorsichtig (siehe [Gruppenchats](#gruppenchats)).
- Mitteilungen anderer iPhone-Apps werden nur angezeigt. Du kannst nicht
  darauf antworten.

## So verbindet sich BlueFerry

BlueFerry nutzt drei Standard-Bluetooth-Dienste:

| Dienst | Bluetooth | Liefert |
| --- | --- | --- |
| MAP (Message Access Profile) | Classic | Nachrichten, Lesestatus, Senden |
| PBAP (Phone Book Access Profile) | Classic | Kontakte |
| ANCS (Apple Notification Center Service) | Low Energy | Optionale Mitteilungen und Gruppendetails |

Nachrichten und Kontakte funktionieren auch ohne ANCS. ANCS liefert die
Angaben, mit denen BlueFerry Gruppennachrichten erkennt, und ermöglicht das
Spiegeln anderer App-Mitteilungen.

## Voraussetzungen

- Ein Bluetooth-Adapter mit Bluetooth Classic **und** Bluetooth 4.0 oder
  neuer mit LE-Advertising. LE-Advertising ist auch für Nachrichten und
  Kontakte nötig, weil das iPhone erst dadurch deren Schalter anzeigt.
  Adapter, die nur Bluetooth 3 können, funktionieren nicht.
- BlueZ 5.72 oder neuer für Nachrichten und Kontakte. iPhone-Mitteilungen
  (ANCS) brauchen BlueZ 5.86 oder neuer.
- Ein iPhone. Entwickelt wurde vor allem mit einem iPhone 16 Pro Max unter
  iOS 26.5, zusätzlich erfolgreich getestet mit einem iPhone 17 Pro Max mit
  einer Beta von iOS 27. Für iOS 18 oder älter nimm die
  Kompatibilitätskopplung (siehe [iPhone koppeln](pairing.md#kopplungsoptionen)).

Die Unterstützung hängt von Adapter und iOS-Version ab. Realtek-Adapter
unterstützen Systemmitteilungen und Gruppenchats meist nicht.

## Unterhaltungen

Direkte Unterhaltungen fassen die Telefonnummern und E-Mail-Adressen
zusammen, die eindeutig zu einem synchronisierten Kontakt gehören. Antworten
gehen an die zuletzt eingehende Adresse, die über der Unterhaltung als
**Reply to** steht. Gemeinsam genutzte Adressen und Kontakte, die nur
gleich heißen, bleiben getrennt.

## Gruppenchats

Bluetooth verrät BlueFerry weder die ID noch die vollständige Mitgliederliste
einer Gruppe. Wenn BlueFerry die Teilnehmer nicht sicher kennt, fragt es dich
deshalb:

- Bei einer benannten Gruppe lernt BlueFerry die Mitglieder nur von den
  Personen, die hineinschreiben.
- Bei einer unbenannten Gruppe nennt das iPhone die Mitglieder. BlueFerry
  ergänzt die Adressen, die es zuordnen kann. Hat jemand mehrere
  Telefonnummern oder E-Mail-Adressen, etwa eine Nummer und eine Apple-ID,
  wählst du selbst.
- Wenn du an eine solche Gruppe sendest, bittet BlueFerry dich zuerst, die
  Mitglieder zu bestätigen. Beim Tippen schlägt es Kontakte vor und nimmt auch
  eine Telefonnummer oder E-Mail-Adresse an. Deine Nachricht bleibt im
  Antwortfeld, bis die Liste gespeichert ist.
- Die gespeicherte Liste bleibt, bis du die Unterhaltung löschst, den Verlauf
  leerst oder den Speichermodus änderst. Die Gruppe auf dem iPhone bleibt
  unverändert.
- Schreibt jemand, der nicht auf der gespeicherten Liste steht, antwortet
  BlueFerry erst wieder, wenn du die Liste geprüft hast.
- BlueFerry erkennt eine unbenannte Gruppe an den Namen ihrer Mitglieder.
  Benennst du jemanden in den Kontakten um oder ändert sich die Gruppe, wird
  sie zu einer neuen Unterhaltung, und BlueFerry fragt erneut.
- Eine Nachricht landet nur dann in ihrer Gruppe, wenn BlueFerry auch die
  zugehörige iPhone-Mitteilung erhält. Sonst erscheint sie in der
  Einzelunterhaltung mit dem Absender.
- Kann eine Nachricht in einer Einzelunterhaltung auch an eine Gruppe
  gegangen sein, etwa dasselbe „ok“ in einer Gruppe und an dich, fragt
  BlueFerry vor dem Antworten, ob die Antwort nur an den Absender gehen soll.
  Kommt eine weitere solche Nachricht, fragt es erneut.

## Nächste Schritte

[BlueFerry installieren](install.md), dann [dein iPhone koppeln](pairing.md).
