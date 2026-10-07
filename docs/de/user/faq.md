# FAQ

## Brauche ich einen Mac, ein Apple-Konto oder eine App auf dem iPhone?

Nein. BlueFerry spricht über Standard-Bluetooth-Profile direkt mit dem
iPhone. Es gibt kein Relais, keine Apple-Anmeldung, keine iPhone-App, keinen
Jailbreak, keinen Cloud-Dienst und kein Abo.

## Sehe ich meine älteren Nachrichten?

Nur Nachrichten, die BlueFerry während einer Verbindung gesehen hat. Es lädt
weder dein iCloud-Nachrichtenarchiv noch deinen vollständigen Verlauf
gesendeter Nachrichten.

## Kann ich Bilder senden, auf Nachrichten reagieren oder Tippanzeigen sehen?

Nein. Anhänge, Reaktionen und Tippanzeigen sind über die Bluetooth-Profile,
die iOS anbietet, nicht verfügbar.

## Kann ich telefonieren?

Nein. Anrufe und FaceTime werden nicht unterstützt. BlueFerry lässt Anrufe
und Musik bewusst auf dem iPhone.

## Warum kann ich in einer Gruppe nicht antworten?

Bluetooth verrät BlueFerry weder die ID noch die vollständige
Mitgliederliste einer Gruppe. Bei einer benannten Gruppe lernt BlueFerry die
Mitglieder von den Personen, die hineinschreiben, und bittet dich einmal, die
vollständige Liste zu bestätigen. Sind die Mitglieder unklar oder schreibt
jemand Neues, bleibt das Antworten gesperrt, bis du erneut bestätigst. Siehe
[Gruppenchats](overview.md#gruppenchats).

## Können zwei Rechner dasselbe iPhone nutzen?

Koppeln lassen sich beide, aber das iPhone bedient die
Nachrichtenverbindung immer nur für einen davon. Der andere zeigt an, dass
die Verbindung abgelehnt wurde.

## Welche Bluetooth-Adapter funktionieren?

Adapter mit Bluetooth Classic und Bluetooth 4.0 oder neuer mit
LE-Advertising. Adapter, die nur Bluetooth 3 können, funktionieren nicht.
Realtek-Adapter unterstützen iPhone-Systemmitteilungen und Gruppenchats meist
nicht. Das Verhalten hängt vom Adapter ab; berichte gern, was bei dir geht.

## Geht es mit iOS 18 oder älter?

Nimm die Option **Compatibility pairing for iOS 18 or earlier**. Nachrichten
und Kontakte funktionieren, iPhone-Systemmitteilungen verbinden sich nicht.
Siehe [Kopplungsoptionen](pairing.md#kopplungsoptionen).

## Schickt BlueFerry meine Daten irgendwohin?

Nein. Alles bleibt auf deinem Rechner. Der Nachrichtenverlauf ist
standardmäßig verschlüsselt; siehe [Datenschutz und Speicher](privacy.md).

## Kann ich mich darauf verlassen?

Noch nicht als einzigen Weg für wichtige Nachrichten. BlueFerry ist
experimentell, und Apple kann das Bluetooth-Verhalten ändern, auf das es
angewiesen ist.

## Wo melde ich Fehler?

In den [GitHub-Issues](https://github.com/erikwb/blueferry/issues), bitte auf
Englisch. Bei Kopplungsproblemen bereitet `blueferry pairing-issue` einen
bereinigten Bericht vor; siehe [Ein Problem melden](troubleshooting.md#ein-problem-melden).
