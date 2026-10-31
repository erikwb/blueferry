# Kontakte

BlueFerry hält eine lokale Kopie des iPhone-Adressbuchs (siehe
[Kopplung](pairing.md) für **Sync Contacts**). Neben den Namen und Adressen
für Nachrichten behält jede Kontaktkarte jetzt, was das iPhone darauf
speichert: jede Telefonnummer so geschrieben wie auf dem iPhone, mit ihrer
Bezeichnung (Mobil, Privat, Arbeit, iPhone, Fax, Pager oder eine eigene
Bezeichnung), E-Mail-Adressen mit Bezeichnung, Postadressen, die Firma und
die Position.

## Kontakte ansehen

```bash
blueferry contacts list                 # die ersten 50 Karten mit ihren IDs
blueferry contacts list --all           # alle Karten
blueferry contacts list -q "example"    # nach Name, Firma, Mail, Adresse oder Nummer filtern
blueferry contacts show 3f2a9c…         # alle Angaben einer Karte
blueferry contacts show Alice           # klappt, wenn genau eine Karte passt
blueferry contacts show Alice --photo alice.jpg   # Foto zusätzlich speichern
```

Ein Nummernfilter vergleicht die Ziffern, `-q "079 123"` findet also
`+41 79 123 45 67`. Kurze Ziffernfolgen (weniger als drei Ziffern) gelten
nicht als Nummer.

Die IDs der Karten bleiben gleich, solange sich eine Karte nicht ändert und
der Hintergrunddienst nicht neu startet. Nach einem Abgleich, der eine Karte
geändert hat, gilt ihre alte ID nicht mehr; liste die Kontakte dann neu auf.

Kontaktansicht in den Clients:

- **KDE-Client:**
  noch nicht; nutze die Kommandozeile.
- **GTK-Client:**
  noch nicht; nutze die Kommandozeile.
- **Terminal-Client:**
  noch nicht; nutze die Kommandozeile.
- **Quickshell-Panel:**
  der Suchknopf (oder `Strg+F`) findet einen Kontakt und ruft eine seiner
  Nummern an oder schreibt an eine Nummer oder Adresse (siehe
  [Clients](clients.md#kontakte-und-suche-im-panel)).

## Fotos

Das Foto einer Karte erscheint nur mit eingeschalteten
[Kontaktfotos](contact-photos.md). Die Liste selbst überträgt nie Fotos;
`contacts show` holt nur das Foto der geöffneten Karte.

## Speicherung und Datenschutz

- Die zusätzlichen Angaben werden nur in den verschlüsselten
  Kontakteinträgen gespeichert, im selben
  [Speichermodus](privacy.md#lokale-daten) wie der übrige Kontakt-Cache. Sie
  landen nie lesbar auf der Festplatte.
- Im unverschlüsselten Speichermodus behält BlueFerry wie bisher nur Namen,
  Nummern und E-Mail-Adressen: Bezeichnungen, Postadressen, Firma und
  Position werden nicht gespeichert, und Karten zeigen die Nummern ohne
  Bezeichnung.
- Kontakte, die eine frühere Version abgeglichen hat, zeigen ihre Nummern
  bis zum nächsten Kontaktabgleich ohne Bezeichnung.
- Notizen, Geburtstage, Websites und andere Kartenfelder werden nicht
  gespeichert.

## Grenzen

- Nur mit Beispiel-vCards getestet, noch nicht mit dem Adressbuch eines
  echten iPhones.
