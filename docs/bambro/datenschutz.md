---
title: Bambro – Datenschutz
permalink: /bambro/datenschutz/
lang: de
---

[English](../privacy/)

# Datenschutzerklärung für Bambro

Stand: 22. November 2026

Bambro ist eine iOS-App, mit der du 3D-Drucker von Bambu Lab im Heimnetz (LAN-Modus) überwachst, steuerst und
bedienst. Diese Erklärung beschreibt, welche Daten die App verarbeitet und wohin sie gehen.

## Verantwortlich

Sebastian Blank
E-Mail: [bambro@blankk.it](mailto:bambro@blankk.it)

## Kurz gesagt

- Bambro hat kein Benutzerkonto, keine Werbung, keine Analyse- oder Tracking-Dienste.
- Die App spricht deinen Drucker direkt im Heimnetz an oder deine eigene Bridge Orcbro, die du selbst betreibst.
- Der Entwickler speichert keine Inhalte über dich, deine Drucker oder deine Druckaufträge, außer Fehlermeldungen,
  die du selbst per E-Mail schickst (siehe unten).
- Nur wenn deine Bridge Pushes über den Weiterleitungsdienst des Entwicklers schickt, speichert dieser einen Hash des
  Push-Tokens deines iPhones mit einer zufälligen Kennung deiner Bridge, höchstens sieben Tage nach dem letzten Push
  (siehe unten).

## Daten auf deinem iPhone

Bambro speichert auf deinem Gerät:

- **Drucker:** Name, IP-Adresse, Seriennummer, Modell und den Fingerabdruck des Druckerzertifikats. Den
  **Zugangscode** des Druckers legt die App im Schlüsselbund von iOS ab, nur auf diesem Gerät.
- **Bridge (falls genutzt):** Adresse deiner Orcbro-Bridge und den Zugangsschlüssel, den die Bridge beim Koppeln
  ausgibt (im Schlüsselbund).
- **Druckdateien,** die du in Bambro öffnest, und daraus gelesene Angaben wie Platten, Druckzeit und Vorschaubilder.
- **Zeitraffer-Videos,** die du von der SD-Karte des Druckers lädst. Sie liegen im Ordner von Bambro, den du in der
  Dateien-App unter „Auf meinem iPhone“ siehst, bis du sie löschst.

Diese Daten verlassen dein Gerät nur in Richtung deines Druckers oder deiner Bridge, wie unten beschrieben. Sie werden
nicht in iCloud übertragen. Eine Fehlermeldung, die du schickst, enthält davon nur Modell und Firmware des Druckers.
Löschst du die App, entfernt iOS ihre Daten. Einträge im Schlüsselbund kann iOS dabei behalten; entferne deshalb
vorher in der App den Drucker oder trenne die Bridge, dann löscht Bambro Zugangscode und Zugangsschlüssel.

## Verbindungen

**Zum Drucker (ohne Bridge).** Bambro verbindet sich im lokalen Netz verschlüsselt mit deinem Drucker (MQTT, FTPS und
bei P1/A1 die Kamera). Übertragen werden Steuerbefehle, Druckdateien und der Zugangscode zur Anmeldung; zurück kommen
Zustand, Kamerabilder und Dateilisten der SD-Karte. Dafür fragt iOS nach der Erlaubnis „Lokales Netzwerk“.

**Zu Orcbro (mit Bridge).** Nutzt du Orcbro, verbindet sich Bambro mit deiner Bridge in deinem Netz. Was die Bridge
speichert, liegt auf deinem eigenen Rechner. Bambro sendet ihr Druckdateien, Steuerbefehle, beim Koppeln die
Geräteart („iPhone“) und für Live-Aktivitäten und Mitteilungen die Push-Tokens deines iPhones mit einer Kennung
dieser App-Installation, die Sprache der App und die gewählten Ereignisse. Liest oder beschreibst du einen
NFC-Sticker einer Spule, sendet Bambro außerdem dessen Kennung und die Angaben zur Spule (siehe Berechtigungen); die
Bridge führt sie in ihrer Filament-Bibliothek.

**Live-Aktivitäten und Mitteilungen.** Damit Fortschritt und Restzeit eines Drucks auf dem Sperrbildschirm erscheinen
und du erfährst, wenn ein Druck fertig ist, pausiert oder einen Fehler meldet, während Bambro geschlossen ist, schickt
deine Bridge Pushes über den Apple Push Notification Service (APNs). Mitteilungen gibt es nur, wenn du sie in Bambro
einschaltest und iOS sie erlaubt; ohne Erlaubnis meldet Bambro der Bridge kein Token. Ohne Bridge aktualisiert Bambro
die Live-Aktivität selbst auf dem Gerät, ohne Push.

## Weiterleitungsdienst für Pushes

Hat deine Bridge keinen eigenen Push-Schlüssel, leitet sie die Pushes über einen Weiterleitungsdienst des Entwicklers
(`push.blankk.it`). Dabei passieren Push-Token, Name von Drucker und Druck, Fortschritt, Restzeit, Druckerzustand,
Fehlertexte des Druckers, die Sprache der App und eine aus der Seriennummer abgeleitete Kennung (nicht die
Seriennummer selbst) diesen Dienst auf dem Weg zu Apple. Diese Inhalte speichert er nicht, Protokolle führt er keine.

Damit nur deine Bridge an dein iPhone senden kann, speichert der Dienst einen Hash (SHA-256) jedes Push-Tokens zusammen
mit einer zufälligen Kennung deiner Bridge. Das Token selbst, deinen Namen, deine IP-Adresse oder deinen Drucker
speichert er nicht. Gegen Missbrauch zählt er außerdem Anfragen pro Gerät und pro Bridge sowie Anmeldungen neuer
Bridges pro IP-Adresse; diese Zähler verfallen nach einer Minute.

Der Dienst läuft bei Cloudflare (Cloudflare, Inc., USA), das die Anfragen im Auftrag des Entwicklers verarbeitet;
die Übermittlung in die USA stützt sich auf das EU-US Data Privacy Framework. Rechtsgrundlage: Art. 6 Abs. 1 lit. f
DSGVO; das berechtigte Interesse ist, die von dir eingeschalteten Pushes zuzustellen, und zwar nur von deiner eigenen
Bridge.

**Wie der Eintrag gelöscht wird.** Der Dienst löscht den Eintrag automatisch, spätestens sieben Tage nach dem letzten
Push an dieses Token. Die Pushes enden, wenn du das iPhone aus der Bridge entfernst (die Bridge verwirft dann seine
Push-Tokens) oder Bambro löschst (Apple lehnt das Token dann ab, und die Bridge verwirft es); danach verfällt der
Eintrag innerhalb von sieben Tagen. Weil der Dienst weder deinen Namen noch das Token selbst speichert, kann der
Entwickler nicht erkennen, welcher Eintrag deiner ist. Sollen die Einträge deiner Bridge früher gelöscht werden,
schicke ihre Relay-Kennung (den Wert `bridge` in der Datei `relay.json` im Datenordner von Orcbro; niemals den `key`)
an [bambro@blankk.it](mailto:bambro@blankk.it).

## Fehlermeldungen per E-Mail

Einstellungen → Hilfe → Fehler melden öffnet in deiner Mail-App eine E-Mail an
[bambro@blankk.it](mailto:bambro@blankk.it). Du kannst alles darin lesen und ändern, bevor du sie abschickst;
automatisch wird nichts gesendet. Neben deinem Text enthält sie: App-Version, iOS-Version und Gerätemodell (etwa
„iPhone18,1“), Verbindungsart und -zustand, Orcbro-Version, Modell und Firmware des Druckers, Druckerzustand, ob der
Developer Mode an ist, den Code der aktuellen Druckermeldung und die AMS-Typen. Sie enthält keine IP-Adressen,
Seriennummern, Zugangscodes oder Tokens. Der Entwickler erhält außerdem deine E-Mail-Adresse und alles, was du anhängst.

Der Entwickler nutzt diese E-Mails nur, um dir zu antworten und den Fehler zu beheben, und löscht sie, sobald der
Fehler behoben und die Unterhaltung beendet ist. Sie liegen beim E-Mail-Anbieter des Entwicklers. Rechtsgrundlage:
Art. 6 Abs. 1 lit. f DSGVO (Beantwortung deiner Anfrage). Für andere E-Mails an diese Adresse gilt dasselbe. Issues
auf GitHub sind öffentlich; dort gilt die Datenschutzerklärung von GitHub.

## Berechtigungen

- **Lokales Netzwerk:** für die Verbindung zu Drucker und Bridge.
- **Kamera:** nur zum Scannen des QR-Codes der Bridge. Bilder werden weder gespeichert noch übertragen.
- **NFC:** zum Lesen und Beschreiben von Spulen-Stickern. Ein Sticker enthält Material, Marke, Farbe, Temperaturen
  und die Menge auf der Spule. Ohne Bridge bleiben diese Daten auf dem Gerät und dem Sticker. Mit Bridge sendet Bambro
  außerdem die Kennung des Stickers und diese Angaben an deine Bridge, die die Spule in ihrer Filament-Bibliothek führt.
- **Mitteilungen:** für Meldungen zu deinen Drucken, nur wenn du sie in Bambro einschaltest; abschaltbar in Bambro
  und in den Einstellungen von iOS.
- **Fotos (nur hinzufügen):** um einen Zeitraffer auf deinen Wunsch in der Fotomediathek zu sichern. Bambro kann
  deine Fotos nicht lesen.
- **Live-Aktivitäten:** für die Anzeige laufender Drucke auf dem Sperrbildschirm; abschaltbar in den Einstellungen von iOS.

## Deine Rechte

Du hast das Recht auf Auskunft, Berichtigung, Löschung, Einschränkung der Verarbeitung, Datenübertragbarkeit und
Widerspruch (Art. 15–21 DSGVO). Beim Entwickler liegen nur die oben beschriebenen Einträge des Weiterleitungsdienstes,
die automatisch gelöscht werden, und Fehlermeldungen, die du geschickt hast. Anfragen an
[bambro@blankk.it](mailto:bambro@blankk.it); für Einträge des Weiterleitungsdienstes gib die Relay-Kennung deiner
Bridge an, weil der Entwickler sonst nicht erkennen kann, welche Einträge deine sind. Du hast außerdem das Recht, dich
bei einer Datenschutz-Aufsichtsbehörde zu beschweren.

## Änderungen

Ändert sich, was Bambro verarbeitet, wird diese Seite angepasst. Das Datum oben zeigt den Stand.

Bambro ist ein unabhängiges Projekt und steht in keiner Verbindung zu Bambu Lab.
