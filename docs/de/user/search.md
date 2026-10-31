# Suche

Eine Suche durchsucht deine Unterhaltungen, Nachrichten, Kontakte und die
Anrufliste auf einmal.

```bash
blueferry search mittag         # alle Gruppen, je bis zu 10 Treffer
blueferry search "079 123" -n 30
blueferry search alice --ids    # zusätzlich die IDs zum Öffnen der Treffer
```

Die Treffer sind gruppiert: Unterhaltungen (nach Name oder
Gruppenmitglied), Nachrichten (nach Text, neueste zuerst, mit
hervorgehobener Fundstelle), Kontakte (Name, Firma, Mail, Postadresse oder
Nummer) und Anrufe (Name oder Nummer). Jede Gruppe zeigt, wie viele Treffer
wegen des Limits fehlen. Groß-/Kleinschreibung, Akzente und
Schreibvarianten spielen keine Rolle: `muller` findet `Müller`. Nummern
werden über ihre Ziffern gefunden, sobald die Suche mindestens drei Ziffern
enthält, auch bei Unterhaltungen. Der Hintergrunddienst liefert pro Gruppe
höchstens 50 Treffer (`-n 50`). Suchen mit weniger als zwei
Zeichen finden nichts.

Suche in den Clients:

- **KDE-Client:**
  noch nicht; nutze die Kommandozeile.
- **GTK-Client:**
  noch nicht; nutze die Kommandozeile.
- **Terminal-Client:**
  noch nicht; nutze die Kommandozeile.
- **Quickshell-Panel:**
  der Suchknopf (oder `Strg+F`) sucht beim Tippen, zeigt je Gruppe bis zu 10
  Treffer und öffnet aus einem Treffer die Unterhaltung oder die
  Kontaktkarte (siehe [Clients](clients.md#kontakte-und-suche-im-panel)).

## Was durchsucht wird

- Nachrichten: die Unterhaltungen, die BlueFerry für seine Clients
  bereithält, also die letzten 2000 Nachrichten und markierte
  Unterhaltungen. Ältere Nachrichten außerhalb markierter Unterhaltungen
  werden nicht durchsucht.
- Kontakte: der Kontakt-Cache, inklusive der [Kartenangaben](contacts.md).
- Anrufe: die gespeicherte [Anrufliste](call-history.md), nur wenn sie
  eingeschaltet ist.

## Datenschutz

Der lokale Verlauf ist verschlüsselt, deshalb legt BlueFerry keinen
Suchindex auf der Festplatte an. Der Hintergrunddienst durchsucht die
Daten, die er für die offenen Clients ohnehin entschlüsselt im Speicher
hält, und zwar nur, wenn du suchst. Damit die Suche beim schnellen Tippen
flüssig bleibt, behält er eine kleingeschriebene Kopie der durchsuchten
Nachrichten im Speicher, bis sich die Unterhaltungen ändern; vorab wird
nichts aufbereitet und nichts auf die Festplatte geschrieben. Solange die Brieftasche gesperrt ist, werden Anrufe nicht
durchsucht.

## Grenzen

- Nur mit Beispieldaten getestet, noch nicht mit dem Verlauf eines echten
  iPhones.
