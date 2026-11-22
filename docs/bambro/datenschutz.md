---
title: Bambro – Datenschutz
permalink: /bambro/datenschutz/
lang: de
---

[English](../privacy/)

# Datenschutzerklärung für Bambro

Stand: 21. November 2026

Bambro ist eine iOS-App, mit der du 3D-Drucker von Bambu Lab im Heimnetz (LAN-Modus) überwachst, steuerst und
bedienst. Diese Erklärung beschreibt, welche Daten die App verarbeitet und wohin sie gehen.

## Verantwortlich

Sebastian Blank
E-Mail: [bambro@blankk.it](mailto:bambro@blankk.it)

## Kurz gesagt

- Bambro hat kein Benutzerkonto, keine Werbung, keine Analyse- oder Tracking-Dienste.
- Die App spricht deinen Drucker direkt im Heimnetz an oder deine eigene Bridge Orcbro, die du selbst betreibst.
- Der Entwickler erhält keine Daten über dich, deine Drucker oder deine Druckaufträge und speichert keine.

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
nicht in iCloud oder an den Entwickler übertragen. Löschst du die App, entfernt iOS ihre Daten. Einträge im
Schlüsselbund kann iOS dabei behalten; entferne deshalb vorher in der App den Drucker oder trenne die Bridge, dann
löscht Bambro Zugangscode und Zugangsschlüssel.

## Verbindungen

**Zum Drucker (ohne Bridge).** Bambro verbindet sich im lokalen Netz verschlüsselt mit deinem Drucker (MQTT, FTPS und
bei P1/A1 die Kamera). Übertragen werden Steuerbefehle, Druckdateien und der Zugangscode zur Anmeldung; zurück kommen
Zustand, Kamerabilder und Dateilisten der SD-Karte. Dafür fragt iOS nach der Erlaubnis „Lokales Netzwerk“.

**Zu Orcbro (mit Bridge).** Nutzt du Orcbro, verbindet sich Bambro mit deiner Bridge in deinem Netz. Was die Bridge
speichert, liegt auf deinem eigenen Rechner; Bambro sendet ihr Druckdateien, Steuerbefehle und für
Live-Aktivitäten das Push-Token deines iPhones.

**Live-Aktivitäten und Mitteilungen.** Damit Fortschritt und Restzeit eines Drucks auf dem Sperrbildschirm erscheinen
und du erfährst, wenn ein Druck fertig ist, pausiert oder einen Fehler meldet, während Bambro geschlossen ist, schickt
deine Bridge Pushes über den Apple Push Notification Service (APNs). Mitteilungen gibt es nur, wenn du sie in Bambro
einschaltest und iOS sie erlaubt; ohne Erlaubnis meldet Bambro der Bridge kein Token. Hat die Bridge keinen eigenen
Push-Schlüssel, leitet sie die Pushes über einen Weiterleitungsdienst des Entwicklers (`push.blankk.it`, betrieben
bei Cloudflare). Dabei passieren Push-Token, Name von Drucker und Druck, Fortschritt, Restzeit, Druckerzustand,
Fehlertexte des Druckers und eine aus der Seriennummer abgeleitete Kennung (nicht die Seriennummer selbst) diesen
Dienst auf dem Weg zu Apple. Damit nur deine Bridge an dein iPhone senden kann, speichert der Dienst einen Hash des
Push-Tokens zusammen mit einer zufälligen Kennung deiner Bridge, höchstens sieben Tage nach dem letzten Push. Inhalte
speichert er nicht, Protokolle führt er keine; Cloudflare verarbeitet die Anfragen als technischer Dienstleister.
Entfernst du ein iPhone aus der Bridge, verwirft sie dessen Push-Tokens. Ohne Bridge aktualisiert Bambro die
Live-Aktivität selbst auf dem Gerät, ohne Push.

Die Bridge und der Weiterleitungsdienst erzeugen Texte in der Sprache der App.

## Berechtigungen

- **Lokales Netzwerk:** für die Verbindung zu Drucker und Bridge.
- **Kamera:** nur zum Scannen des QR-Codes der Bridge. Bilder werden weder gespeichert noch übertragen.
- **NFC:** zum Lesen und Beschreiben von Spulen-Stickern. Die Daten (Material, Farbe, Temperaturen) bleiben auf dem
  Gerät und dem Sticker.
- **Mitteilungen:** für Meldungen zu deinen Drucken, nur wenn du sie in Bambro einschaltest; abschaltbar in Bambro
  und in den Einstellungen von iOS.
- **Fotos (nur hinzufügen):** um einen Zeitraffer auf deinen Wunsch in der Fotomediathek zu sichern. Bambro kann
  deine Fotos nicht lesen.
- **Live-Aktivitäten:** für die Anzeige laufender Drucke auf dem Sperrbildschirm; abschaltbar in den Einstellungen von iOS.

## Deine Rechte

Da der Entwickler keine personenbezogenen Daten von dir speichert, gibt es bei ihm in der Regel nichts zu beauskunften,
zu berichtigen oder zu löschen. Fragen dazu beantwortet [bambro@blankk.it](mailto:bambro@blankk.it). Du hast außerdem
das Recht, dich bei einer Datenschutz-Aufsichtsbehörde zu beschweren.

## Änderungen

Ändert sich, was Bambro verarbeitet, wird diese Seite angepasst. Das Datum oben zeigt den Stand.

Bambro ist ein unabhängiges Projekt und steht in keiner Verbindung zu Bambu Lab.
