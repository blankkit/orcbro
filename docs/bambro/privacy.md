---
title: Bambro – Privacy Policy
permalink: /bambro/privacy/
lang: en
---

[Deutsch](../datenschutz/)

# Privacy Policy for Bambro

Last updated: November 22, 2026

Bambro is an iOS app for monitoring and controlling Bambu Lab 3D printers on your home network (LAN mode). This
policy describes which data the app processes and where it goes.

## Controller

Sebastian Blank
Email: [bambro@blankk.it](mailto:bambro@blankk.it)

## In short

- Bambro has no user account, no advertising, and no analytics or tracking services.
- The app talks directly to your printer on your home network, or to your own bridge Orcbro, which you run yourself.
- The developer stores no content about you, your printers or your print jobs, except problem reports you choose to
  send by email (see below).
- Only if your bridge sends pushes through the developer's relay does the relay keep a hash of your iPhone's push
  token with a random ID of your bridge, for at most seven days after the last push (see below).

## Data on your iPhone

Bambro stores on your device:

- **Printers:** name, IP address, serial number, model and the fingerprint of the printer's certificate. The printer's
  **access code** is kept in the iOS keychain, on this device only.
- **Bridge (if used):** the address of your Orcbro bridge and the access token it issues when pairing (in the keychain).
- **Print files** you open in Bambro, and details read from them such as plates, print time and preview images.
- **Timelapse videos** you download from the printer's SD card. They are kept in Bambro's folder, which you can see in
  the Files app under "On My iPhone", until you delete them.

This data leaves your device only towards your printer or your bridge, as described below. It is not sent to iCloud.
A problem report you send contains only the printer model and firmware, not the other details. Deleting the app
removes its data. iOS may keep keychain entries; to remove the access code and the bridge token as well, remove the
printer or disconnect the bridge in the app first.

## Connections

**To the printer (without a bridge).** Bambro connects to your printer over the local network with encryption (MQTT,
FTPS and, on P1/A1, the camera). It sends control commands, print files and the access code for signing in, and
receives status, camera images and SD card file lists. iOS asks for "Local Network" permission for this.

**To Orcbro (with a bridge).** If you use Orcbro, Bambro connects to your bridge on your network. What the bridge
stores stays on your own computer. Bambro sends it print files, control commands, the device type ("iPhone") when
pairing, and, for Live Activities and notifications, your iPhone's push tokens with an ID of this app installation,
the app language and the events you chose. When you read or write an NFC spool tag, Bambro also sends the tag's ID and
the spool details (see Permissions); the bridge keeps them in its filament library.

**Live Activities and notifications.** To show progress and remaining time on the Lock Screen, and to tell you when a
print finishes, pauses or reports an error while Bambro is closed, your bridge sends pushes through the Apple Push
Notification service (APNs). Notifications are sent only if you turn them on in Bambro and iOS allows them; without
permission, Bambro does not give the bridge a token. Without a bridge, Bambro updates the Live Activity itself on the
device, without push.

## Push relay

If your bridge has no push key of its own, it forwards the pushes through a relay run by the developer
(`push.blankk.it`). The push token, printer and job name, progress, remaining time, printer state, error texts from
the printer, the app language and an identifier derived from the serial number (not the serial number itself) pass
through the relay on their way to Apple. The relay does not store this content and keeps no logs.

So that only your bridge can send to your iPhone, the relay stores a hash (SHA-256) of each push token together with a
random identifier of your bridge. It stores neither the token itself nor your name, IP address or printer. To limit
abuse, it also counts requests per device and per bridge, and registrations of new bridges per IP address; these
counters expire after one minute.

The relay runs on Cloudflare (Cloudflare, Inc., USA), which processes the requests on the developer's behalf;
transfers to the USA are based on the EU-U.S. Data Privacy Framework. Legal basis: Art. 6(1)(f) GDPR; the legitimate
interest is delivering the pushes you turned on, and only from your own bridge.

**How the entry is deleted.** The relay deletes the entry automatically, at the latest seven days after the last push
to that token. Pushes stop when you remove the iPhone from the bridge (the bridge then discards its push tokens) or
delete Bambro (Apple then rejects the token, and the bridge discards it); the entry expires within seven days after
that. Because the relay stores neither your name nor the token itself, the developer cannot tell which entry is yours.
To have your bridge's entries deleted sooner, send its relay ID (the value `bridge` in the file `relay.json` in
Orcbro's data folder; never send the `key`) to [bambro@blankk.it](mailto:bambro@blankk.it).

## Problem reports by email

Settings → Help → Report a Problem opens an email to [bambro@blankk.it](mailto:bambro@blankk.it) in your mail app. You
can read and change everything in it before you send it; nothing is sent automatically. Besides what you write, it
contains: app version, iOS version and device model (such as "iPhone18,1"), connection mode and state, Orcbro version,
printer model and firmware, printer state, whether Developer Mode is on, the code of the current printer message and
the AMS types. It contains no IP addresses, serial numbers, access codes or tokens. The developer also receives your
email address and anything you attach.

The developer uses these emails only to answer you and to fix the problem, and deletes them once the problem is
resolved and the conversation has ended. They are stored with the developer's email provider. Legal basis: Art.
6(1)(f) GDPR (answering your request). The same applies to other emails you send to this address. Issues on GitHub
are public and subject to GitHub's privacy statement.

## Permissions

- **Local Network:** to connect to the printer and the bridge.
- **Camera:** only to scan the bridge's QR code. Images are neither stored nor transmitted.
- **NFC:** to read and write spool tags. A tag holds material, brand, color, temperatures and the spool amount.
  Without a bridge, this data stays on the device and the tag. With a bridge, Bambro also sends the tag's ID and these
  details to your bridge, which keeps the spool in its filament library.
- **Notifications:** for messages about your prints, only if you turn them on in Bambro; can be turned off in Bambro
  and in iOS Settings.
- **Photos (add only):** to save a timelapse to your photo library when you ask for it. Bambro cannot read your photos.
- **Live Activities:** to show running prints on the Lock Screen; can be turned off in iOS Settings.

## Your rights

You have the right of access, rectification, erasure, restriction of processing, data portability and objection
(Art. 15–21 GDPR). The developer holds only the relay entries described above, which are deleted automatically, and
problem reports you sent. Requests go to [bambro@blankk.it](mailto:bambro@blankk.it); for relay entries, include your
bridge's relay ID, because the developer cannot otherwise tell which entries are yours. You also have the right to
lodge a complaint with a data protection authority.

## Changes

If what Bambro processes changes, this page is updated. The date at the top shows the current version.

Bambro is an independent project and is not affiliated with Bambu Lab.
