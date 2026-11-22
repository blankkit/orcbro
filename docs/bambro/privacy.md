---
title: Bambro – Privacy Policy
permalink: /bambro/privacy/
lang: en
---

[Deutsch](../datenschutz/)

# Privacy Policy for Bambro

Last updated: November 21, 2026

Bambro is an iOS app for monitoring and controlling Bambu Lab 3D printers on your home network (LAN mode). This
policy describes which data the app processes and where it goes.

## Controller

Sebastian Blank
Email: [bambro@blankk.it](mailto:bambro@blankk.it)

## In short

- Bambro has no user account, no advertising, and no analytics or tracking services.
- The app talks directly to your printer on your home network, or to your own bridge Orcbro, which you run yourself.
- The developer receives and stores no data about you, your printers or your print jobs.

## Data on your iPhone

Bambro stores on your device:

- **Printers:** name, IP address, serial number, model and the fingerprint of the printer's certificate. The printer's
  **access code** is kept in the iOS keychain, on this device only.
- **Bridge (if used):** the address of your Orcbro bridge and the access token it issues when pairing (in the keychain).
- **Print files** you open in Bambro, and details read from them such as plates, print time and preview images.
- **Timelapse videos** you download from the printer's SD card. They are kept in Bambro's folder, which you can see in
  the Files app under "On My iPhone", until you delete them.

This data leaves your device only towards your printer or your bridge, as described below. It is not sent to iCloud
or to the developer. Deleting the app removes its data. iOS may keep keychain entries; to remove the access code and
the bridge token as well, remove the printer or disconnect the bridge in the app first.

## Connections

**To the printer (without a bridge).** Bambro connects to your printer over the local network with encryption (MQTT,
FTPS and, on P1/A1, the camera). It sends control commands, print files and the access code for signing in, and
receives status, camera images and SD card file lists. iOS asks for "Local Network" permission for this.

**To Orcbro (with a bridge).** If you use Orcbro, Bambro connects to your bridge on your network. What the bridge
stores stays on your own computer. Bambro sends it print files, control commands and, for Live Activities, your
iPhone's push token.

**Live Activities and notifications.** To show progress and remaining time on the Lock Screen, and to tell you when a
print finishes, pauses or reports an error while Bambro is closed, your bridge sends pushes through the Apple Push
Notification service (APNs). Notifications are sent only if you turn them on in Bambro and iOS allows them; without
permission, Bambro does not give the bridge a token. If the bridge has no push key of its own, it forwards the pushes
through a relay run by the developer (`push.blankk.it`, hosted on Cloudflare). The push token, printer and job name,
progress, remaining time, printer state, error texts from the printer and an identifier derived from the serial number
(not the serial number itself) pass through this relay on their way to Apple. So that only your bridge can send to
your iPhone, the relay stores a hash of the push token together with a random identifier of your bridge, for at most
seven days after the last push. It stores no content and keeps no logs; Cloudflare processes the requests as a
technical service provider. When you remove an iPhone from the bridge, the bridge discards its push tokens. Without a
bridge, Bambro updates the Live Activity itself on the device, without push.

The bridge and relay generate texts in the app's language.

## Permissions

- **Local Network:** to connect to the printer and the bridge.
- **Camera:** only to scan the bridge's QR code. Images are neither stored nor transmitted.
- **NFC:** to read and write spool tags. The data (material, color, temperatures) stays on the device and the tag.
- **Notifications:** for messages about your prints, only if you turn them on in Bambro; can be turned off in Bambro
  and in iOS Settings.
- **Photos (add only):** to save a timelapse to your photo library when you ask for it. Bambro cannot read your photos.
- **Live Activities:** to show running prints on the Lock Screen; can be turned off in iOS Settings.

## Your rights

Since the developer stores no personal data about you, there is usually nothing to disclose, correct or delete.
Questions go to [bambro@blankk.it](mailto:bambro@blankk.it). You also have the right to lodge a complaint with a data
protection authority.

## Changes

If what Bambro processes changes, this page is updated. The date at the top shows the current version.

Bambro is an independent project and is not affiliated with Bambu Lab.
