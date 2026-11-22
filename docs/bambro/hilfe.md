---
title: Bambro – Hilfe
permalink: /bambro/hilfe/
lang: de
---

[English](../support/)

# Hilfe zu Bambro

Bambro überwacht und steuert 3D-Drucker von Bambu Lab im Heimnetz, direkt oder über die Bridge
[Orcbro](https://github.com/blankkit/orcbro). Fragen und Fehlermeldungen gerne an
[bambro@blankk.it](mailto:bambro@blankk.it) oder als [Issue auf GitHub](https://github.com/blankkit/orcbro/issues).
In der App: Einstellungen → Hilfe → Fehler melden.

Bambro ist auf Englisch und Deutsch verfügbar. Einstellungen → Sprache öffnet die iOS-Sprachauswahl für Bambro.

## Was der Drucker braucht

1. **LAN-Modus** und **Developer Mode** am Drucker einschalten (Einstellungen → WLAN bzw. Netzwerk → LAN-Modus →
   Developer Mode). Ohne Developer Mode lehnt aktuelle Firmware Befehle von Fremd-Apps ab.
2. iPhone und Drucker im selben Netz.
3. **IP-Adresse** und **Zugangscode** stehen am Drucker unter Einstellungen → WLAN (P1, X1) bzw. Netzwerk (A1, H2).
   Nach einem WLAN-Wechsel vergibt der Drucker einen neuen Zugangscode.

## Häufige Fragen

**Welche Drucker gehen?** X1 Carbon, X1, X1E, P1P, P1S, P2S, A1, A1 mini, A2L, H2S, H2D, H2D Pro, H2C und X2D. Getestet
am echten Gerät ist bisher der P1S.

**Mit oder ohne Bridge?** Ohne Bridge kann Bambro Zustand, Steuerung, AMS, SD-Karte, Kamera (P1, A1, A2L) und das
Drucken fertig geslicter Dateien. Mit Orcbro kommen Slicen in der App, 3D-Vorschau, Objekte überspringen, die Kamera
der X1-, P2S-, H2- und X2D-Modelle und Live-Aktivitäten bei geschlossener App dazu.

**Eine 3MF von MakerWorld lässt sich nicht drucken.** MakerWorld bietet Projektdateien ohne G-Code an. Ohne Bridge
braucht Bambro eine geslicte Datei: in Bambu Studio oder [OrcaSlicer](https://github.com/OrcaSlicer/OrcaSlicer) öffnen, slicen und „Datei → Exportieren →
Geslicte Platte exportieren“ wählen (.gcode.3mf). Mit Orcbro slict Bambro die Projektdatei selbst.

**Wo sind meine Zeitraffer?** Drucken → SD-Karte → Zeitraffer. Geladene Videos wandelt Bambro in MP4 um, damit
sie auf dem iPhone laufen; sie liegen in der Dateien-App unter Auf meinem iPhone → Bambro → Zeitraffer.

**Woher weiß Bambro, wie viel Filament noch da ist?** Mit Bridge listet AMS → Filament-Bibliothek alle Spulen.
Bambu-Spulen melden ihre Menge über den Chip (mit „Restmenge schätzen“ in den AMS-Einstellungen). Für eigene Spulen
einmal einen NFC-Sticker beschreiben (Filament, Farbe, Spulengröße) und ihn beim Einlegen lesen: Die Bridge zieht dann
ab, was jeder Druck verbraucht, auch bei Drucken aus OrcaSlicer oder Bambu Studio, und Bambro schreibt die aktuelle
Menge bei jedem Lesen auf den Sticker. Versehentlich gelöscht? Sticker erneut lesen und „Wiederherstellen“ wählen.

**„Developer Mode ist aus“.** Nach Firmware-Updates schaltet der Drucker ihn manchmal ab. Am Drucker wieder
einschalten, siehe oben.

**„Das Zertifikat des Druckers hat sich geändert“.** Bambro merkt sich beim Einrichten das Zertifikat des Druckers.
Hast du den Drucker getauscht oder zurückgesetzt, entferne ihn in Bambro und richte ihn neu ein.

**Ausprobieren ohne Drucker?** Auf dem Startbildschirm „Ohne Drucker ausprobieren“ wählen. Der Demo-Drucker lässt
sich unter Einstellungen → Drucker wieder entfernen.

**Wie schalte ich Mitteilungen ein?** Mit verbundener Bridge unter Einstellungen → Mitteilungen die Mitteilungen
einschalten. Auf Nachfrage in iOS erlauben und die gewünschten Ereignisse auswählen.

## Datenschutz

Siehe [Datenschutzerklärung](../datenschutz/).

Bambro und Orcbro sind unabhängige Projekte und stehen in keiner Verbindung zu Bambu Lab.
