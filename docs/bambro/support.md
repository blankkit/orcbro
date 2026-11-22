---
title: Bambro – Support
permalink: /bambro/support/
lang: en
---

[Deutsch](../hilfe/)

# Bambro Support

Bambro monitors and controls Bambu Lab 3D printers on your home network, directly or through the bridge
[Orcbro](https://github.com/blankkit/orcbro). Questions and bug reports are welcome at
[bambro@blankk.it](mailto:bambro@blankk.it) or as an [issue on GitHub](https://github.com/blankkit/orcbro/issues).
In the app: Settings → Help → Report a Problem; you see the email with its technical details before you send it
(see the [privacy policy](../privacy/)).

Bambro is available in English and German. Settings → Language opens the iOS language choice for Bambro.

## What the printer needs

1. Turn on **LAN mode** and **Developer Mode** on the printer (Settings → WLAN or Network → LAN Only → Developer
   Mode). Without Developer Mode, current firmware rejects commands from third-party apps.
2. iPhone and printer on the same network.
3. The **IP address** and **access code** are on the printer under Settings → WLAN (P1, X1) or Network (A1, H2).
   After switching Wi-Fi networks the printer issues a new access code.

## Frequently asked questions

**Which printers work?** Bambro is made for and tested with the P1S and AMS. The other Bambu Lab printers with LAN
Only mode (X1 Carbon, X1, X1E, P1P, P2S, A1, A1 mini, A2L, H2S, H2D, H2D Pro, H2C and X2D) may work: Bambro supports
them based on OrcaSlicer's printer data and protocol documentation, but they have not been tested on a device yet, and
some features may be missing or behave differently, such as drying with AMS 2 Pro and AMS HT. Reports are welcome.

**With or without a bridge?** Without a bridge, Bambro covers status, controls, AMS, SD card, camera (P1, A1, A2L) and
printing files that are already sliced. Orcbro adds slicing in the app, 3D preview, skipping objects, the filament
library, notifications, plain-text descriptions of printer messages, the camera of the X1, P2S, H2 and X2D models, and
Live Activities while the app is closed.

**A 3MF from MakerWorld won't print.** MakerWorld offers project files without G-code. Without a bridge, Bambro needs
a sliced file: open it in Bambu Studio or [OrcaSlicer](https://github.com/OrcaSlicer/OrcaSlicer), slice it and choose
"File → Export → Export plate sliced file" (.gcode.3mf). With Orcbro, Bambro slices the project file itself.

**Where are my timelapses?** Print → SD Card → Timelapses. Downloaded videos are converted to MP4 so they play on
the iPhone; you find them in Bambro's timelapse folder in the Files app under On My iPhone.

**How does Bambro know how much filament is left?** With a bridge, AMS → Filament Library lists every spool. Bambu
spools report their amount through their chip (with "Estimate Remaining" on in the AMS settings). For your own spools,
write an NFC tag once (filament, color, spool size) and read it when you load the spool: the bridge then subtracts
what each print uses, also for prints from OrcaSlicer or Bambu Studio, and Bambro writes the current amount back to
the tag whenever you read it. To add a spool before it goes into the AMS, use Add Spool in the library: it reads the
tag, or writes a blank one. To change a spool, open it in the library and tap Edit Spool: Bambro reads its tag first
and writes the changes to the tag and to the library. Change the amount left only after weighing the spool: it
replaces what the bridge counted. Deleted a spool by mistake? Read its tag again and choose Restore. The demo printer
shows sample spools in the library.

**Why does a new spool show Generic PLA in gray?** When a spool without a Bambu chip goes into an AMS slot, the printer
would use the previous spool's filament and color again. Instead, the slot is set to Generic of that material in gray
(with the bridge, or in direct mode while Bambro is connected), and the AMS tab shows "New spool – set the filament"
until you set the filament or read the spool's NFC tag. Bambu spools keep what their chip says. To keep the printer's
behavior, set `BRIDGE_RESET_NEW_SPOOLS=off` on the bridge, or in direct mode turn off Settings → Printers → your
printer → Reset New Spools to Generic.

**"Developer Mode is off".** After firmware updates the printer sometimes turns it off. Turn it back on at the
printer, see above.

**"The printer's certificate has changed".** Bambro remembers the printer's certificate when you set it up. If you
replaced or reset the printer, remove it in Bambro and set it up again.

**Try it without a printer?** Choose "Try Without a Printer" on the start screen. The demo printer simulates a P1S
with a running print, AMS and filament library, and prints the sample file. It can be removed under Settings →
Printers.

**How do I turn on notifications?** With a bridge connected, open Settings → Notifications and turn on Notifications.
Allow notifications in iOS when asked, then choose the events you want to receive.

## Privacy

See the [privacy policy](../privacy/).

Bambro and Orcbro are independent projects and are not affiliated with Bambu Lab.
