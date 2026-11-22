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
In der App: Einstellungen → Hilfe → Fehler melden; du siehst die E-Mail mit ihren technischen Angaben, bevor du sie
abschickst (siehe [Datenschutzerklärung](../datenschutz/)).

Bambro ist auf Englisch und Deutsch verfügbar. Einstellungen → Sprache öffnet die iOS-Sprachauswahl für Bambro.

## Was der Drucker braucht

1. **LAN-Modus** und **Developer Mode** am Drucker einschalten (Einstellungen → WLAN bzw. Netzwerk → LAN-Modus →
   Developer Mode). Ohne Developer Mode lehnt aktuelle Firmware Befehle von Fremd-Apps ab.
2. iPhone und Drucker im selben Netz.
3. **IP-Adresse** und **Zugangscode** stehen am Drucker unter Einstellungen → WLAN (P1, X1) bzw. Netzwerk (A1, H2).
   Nach einem WLAN-Wechsel vergibt der Drucker einen neuen Zugangscode.

## Häufige Fragen

**Welche Drucker gehen?** Bambro ist für den P1S mit AMS gemacht und daran getestet. Die anderen Drucker von Bambu Lab
mit LAN-Modus (X1 Carbon, X1, X1E, P1P, P2S, A1, A1 mini, A2L, H2S, H2D, H2D Pro, H2C und X2D) können funktionieren:
Bambro unterstützt sie anhand der Druckerdaten von OrcaSlicer und der Protokoll-Dokumentation, am echten Gerät getestet
sind sie aber noch nicht, und manche Funktionen fehlen womöglich oder verhalten sich anders, etwa das Trocknen mit
AMS 2 Pro und AMS HT. Rückmeldungen sind willkommen.

**Mit oder ohne Bridge?** Ohne Bridge kann Bambro Zustand, Steuerung, AMS, SD-Karte, Kamera (P1, A1, A2L) und das
Drucken fertig geslicter Dateien. Mit Orcbro kommen Slicen in der App, 3D-Vorschau, Objekte überspringen, die
Filament-Bibliothek, Mitteilungen, Beschreibungen der Druckermeldungen im Klartext, die Kamera der X1-, P2S-, H2- und
X2D-Modelle und Live-Aktivitäten bei geschlossener App dazu.

**Eine 3MF von MakerWorld lässt sich nicht drucken.** MakerWorld bietet Projektdateien ohne G-Code an. Ohne Bridge
braucht Bambro eine geslicte Datei: in Bambu Studio oder [OrcaSlicer](https://github.com/OrcaSlicer/OrcaSlicer) öffnen, slicen und „Datei → Exportieren →
Geslicte Platte exportieren“ wählen (.gcode.3mf). Mit Orcbro slict Bambro die Projektdatei selbst.

**Wo sind meine Zeitraffer?** Drucken → SD-Karte → Zeitraffer. Geladene Videos wandelt Bambro in MP4 um, damit
sie auf dem iPhone laufen; sie liegen in der Dateien-App unter Auf meinem iPhone → Bambro → Zeitraffer.

**Woher weiß Bambro, wie viel Filament noch da ist?** Mit Bridge listet AMS → Filament-Bibliothek alle Spulen.
Bambu-Spulen melden ihre Menge über den Chip (mit „Restmenge schätzen“ in den AMS-Einstellungen). Für eigene Spulen
einmal einen NFC-Sticker beschreiben (Filament, Farbe, Spulengröße) und ihn beim Einlegen lesen: Die Bridge zieht dann
ab, was jeder Druck verbraucht, auch bei Drucken aus OrcaSlicer oder Bambu Studio, und Bambro schreibt die aktuelle
Menge bei jedem Lesen auf den Sticker. Eine Spule, die noch nicht im AMS ist, fügst du in der Bibliothek mit „Spule
hinzufügen“ hinzu: Bambro liest den Sticker oder beschreibt einen leeren. Versehentlich gelöscht? Sticker erneut lesen
und „Wiederherstellen“ wählen. Der Demo-Drucker zeigt Beispielspulen in der Bibliothek.

**Warum zeigt eine neue Spule Generic PLA in Grau?** Kommt eine Spule ohne Bambu-Chip in ein AMS-Fach, würde der
Drucker wieder Filament und Farbe der vorigen Spule nehmen. Stattdessen wird das Fach auf Generic desselben Materials
in Grau gesetzt (mit Bridge, oder ohne Bridge, solange Bambro verbunden ist), und der AMS-Tab zeigt „Neue Spule –
Filament einstellen“, bis du das Filament einstellst oder den NFC-Sticker der Spule liest. Bambu-Spulen behalten, was
ihr Chip sagt. Wer das Verhalten des Druckers behalten will, setzt an der Bridge `BRIDGE_RESET_NEW_SPOOLS=off` oder
schaltet ohne Bridge unter Einstellungen → Drucker → dein Drucker → „Neue Spulen auf Generic setzen“ aus.

**„Developer Mode ist aus“.** Nach Firmware-Updates schaltet der Drucker ihn manchmal ab. Am Drucker wieder
einschalten, siehe oben.

**„Das Zertifikat des Druckers hat sich geändert“.** Bambro merkt sich beim Einrichten das Zertifikat des Druckers.
Hast du den Drucker getauscht oder zurückgesetzt, entferne ihn in Bambro und richte ihn neu ein.

**Ausprobieren ohne Drucker?** Auf dem Startbildschirm „Ohne Drucker ausprobieren“ wählen. Der Demo-Drucker
simuliert einen P1S mit laufendem Druck, AMS und Filament-Bibliothek und druckt die Beispieldatei. Er lässt sich unter
Einstellungen → Drucker wieder entfernen.

**Wie schalte ich Mitteilungen ein?** Mit verbundener Bridge unter Einstellungen → Mitteilungen die Mitteilungen
einschalten. Auf Nachfrage in iOS erlauben und die gewünschten Ereignisse auswählen.

## Datenschutz

Siehe [Datenschutzerklärung](../datenschutz/).

Bambro und Orcbro sind unabhängige Projekte und stehen in keiner Verbindung zu Bambu Lab.
