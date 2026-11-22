# Orcbro – technical reference

Detailed description of behavior, Brotocol (HTTP API) and architecture. For an introduction and quick start, see the
[README](../README.md).

## What it does

- **Printers:** one bridge for any number of Bambu printers. Add them in the app with their IP address and access
  code (`POST /api/printers`); the bridge checks authentication first and reads the serial number (from the printer's
  TLS certificate), model, firmware, nozzle and Developer Mode. Settings are stored in `/data/printers.json`
  (readable only by the bridge). The app can only probe and add addresses on the local network: private IPv4,
  unique local IPv6 and link-local addresses; the bridge resolves a host name once, checks the address and connects
  to that address. One printer can also be configured through `BAMBU_HOST`/`BAMBU_ACCESS_CODE`; the app cannot
  change it. The printer profile follows the reported model and installed nozzle (`printer_model`
  and `printer_variant` in OrcaSlicer profiles); after a nozzle change, the bridge slices with the matching profile.
  Models and their capabilities come from OrcaSlicer's `resources/printers`.
- **Status:** one MQTT connection to each printer (8883, TLS, `bblp` + access code), with the client ID
  `orcbro-<first 8 characters of the bridge ID>-<last 6 of the serial>`: a reconnect replaces the bridge's own
  earlier session, and two bridges never push each other off. P1 partial updates are merged and normalized for the
  app: job, progress, temperatures, fans, light and HMS messages. A full report (the `pushall` reply) lists every
  AMS unit, so a removed AMS disappears with the next one (every five minutes at the latest).
- **Plain-text messages:** display errors (`print_error`) and HMS messages use Bambu Lab's English text for the
  model by default. Requests with `Accept-Language: de` use German, falling back to English when unavailable. The
  tables (`hms_de.json.gz`, `hms_en.json.gz`) come from a pinned version of ha-bambulab (MIT); the image downloads
  them with checksum verification (`Dockerfile`, `bridge/hms.py`). Display messages can be acknowledged from the app.
- **Recovery actions:** a display message lists the buttons Bambu Studio's error dialog offers for its code in
  `job.error.actions` (Resume, Retry, Stop …). Bambu Studio takes them from Bambu Lab's HMS action table per printer
  type; `bridge/error_actions.json` is that table for the buttons the bridge can send (`tools/error-actions.py`,
  `bridge/recovery.py`). Without a print, a failed AMS filament change also offers `abort` when the printer reports
  support (`flag3` bit 13), like the Stop button next to Retry in Bambu Studio's filament panel.
  `job.error.acknowledge` is `false` when Bambu's dialog has no close button: the printer waits for one of the
  actions, so the app offers no plain acknowledgment.
  Codes follow Bambu Studio: `print_error` as `%08X` ("0700_8011"), HMS from `attr` and `code`
  ("0700_2000_0002_0001", severity = upper 16 bits of `code`). If Bambu has only an empty text for a code, it is
  considered internal (`internal: true`); Bambu Studio hides such messages, as does the app. The Live Activity shows
  the first sentence of the text.
- **Developer Mode:** the bridge detects whether the printer accepts unsigned commands
  (`connection.developer_mode`): `fun` bit 29 on firmware that reports `fun`, `flag3` bit 4 on the P1S
  (set = signed commands only), and HMS message `0500_0500_0001_0007` (command rejected) if both are missing.
  If Developer Mode is off, the bridge immediately rejects commands and print starts with instructions for enabling
  it, instead of sending commands that will be ignored.
- **Print options:** AMS backup (switch to an equivalent spool when empty), resume after step loss, read RFID on
  startup and insertion, estimate remaining filament; current values appear under `options` in the status.
- **AMS:** slots with material, color, remaining amount, humidity (A–E and %), active slot and external spool.
- **Camera:** P1/A1/A2L use the JPEG protocol on port 6000; X1/P2S/H2/X2D use RTSPS and `ffmpeg` → a common MJPEG
  stream and snapshot API. Connects only while someone is watching; for unknown models, port 6000 is not probed
  because it is a file tunnel on newer models.
- **Slicing:** OrcaSlicer CLI 2.5.1 (AppImage, under Xvfb for preview images) with system profiles from the OrcaSlicer
  repository. STL/OBJ/STEP files are sliced with printer, quality and filament profiles. The filament profile follows
  the selected AMS slot (Bambu ID → OrcaSlicer ID through `bambu_filament_ids.json`, otherwise `Generic <Material>`),
  and the spool color is written into the 3MF. Bambu Studio/OrcaSlicer 3MF projects created for this printer and nozzle
  (`printer_model`, `nozzle_diameter` in `Metadata/project_settings.config`) are sliced with their own settings.
  All other 3MF files (PrusaSlicer, CAD, downloaded models, Bambu projects for A1/X1, etc.) are treated as models:
  geometry only, bridge profiles, rearranged, with the orientation from the file preserved. Multiple copies are not
  supported for these files (OrcaSlicer does not clone objects from 3MF). `.gcode.3mf` files pass through directly,
  but print only on the model and nozzle they were sliced for (`printer_model`, `nozzle_diameter` in
  `Metadata/project_settings.config`, otherwise `printer_model_id`, `nozzle_diameters` in `Metadata/slice_info.config`);
  otherwise the print request returns 409, as in Bambro's direct mode. This also covers SD card imports and printing
  a job again after a nozzle change.
- **Multiple plates:** if the copies do not all fit on one plate, OrcaSlicer places the rest on further plates
  (`--arrange 1` with `--slice 0`). Each plate is printed separately; the file is uploaded to the SD card only once.
  The job records which plates have already been started (`sent_plates`).
- **Vendor profiles:** SUNLU and Polymaker profiles from subdirectories of `BBL/filament` work directly. OrcaSlicer
  enables products from `OrcaFilamentLibrary`, Creality and Anycubic only for other printers; the bridge adapts them:
  Bambu Generic for the material plus the vendor's material values (temperatures, fans, flow, density). Start G-code,
  pressure advance and retraction stay from the Bambu profile, and volumetric flow is capped at its limit.
  OrcaSlicer has no Geeetech profiles, so those use `Generic <Material>`.
- **Live Activity:** on paired iPhones' Lock Screen and Dynamic Island through APNs (`live.py`, `apns.py`). When a
  print starts, whether from the app or OrcaSlicer, the bridge starts the activity through push to start; then sends
  updates (progress at most once a minute, pauses/errors/preparation steps immediately, otherwise every 10 minutes),
  and finally the final state as an update. That stays visible for one hour, then an `end` event removes it, also
  when the printer is switched off or offline by then; when the next print starts earlier, the bridge removes it
  first, so each printer shows one activity. A final update that did not reach an iPhone (network or relay failure)
  is sent again once a minute until then. Removing a printer in the app removes its activity right away. Tokens and
  the current print are stored in `/data/push.json`. Texts use the language registered for each token.
- **Printing:** the bridge accepts the job immediately (202) and sends it in the background: wait for the printer,
  upload over FTPS (990, implicit TLS, without TLS shutdown on the data channel), check the size on the card, send
  `project_file` over MQTT with AMS mapping, then wait for the printer to report "Preparing/Printing". A `FAILED`
  state with an error code counts as a refused start only when it appears after the command (`print_error` changed,
  or the printer left `FAILED` and returned): after a stopped print the printer keeps `FAILED` and 0300400C until
  the message is acknowledged, and its reply to `project_file` arrives before the first status of the new job. Up to
  three attempts; before retrying, the bridge checks whether the printer is already printing the file (no duplicate
  start).
  Progress and the current step (`step`) arrive as job events. After a restart, accepted jobs resume if they are less
  than 10 minutes old.

Control commands (pause, resume, stop, light, speed, options, print start) are sent only on explicit request,
checked against the printer state first, and never retried automatically.

**Printer requirements:** LAN Only mode and Developer Mode. Since firmware 01.08.02 (P1), the printer otherwise
rejects control commands from unauthorized software (OrcaSlicer: "print_job failed, result = -26").

### Model matrix

These details come from OrcaSlicer 2.5.1 and the protocol research cited in the tests; models other than the P1S
have not been tested on hardware.

| Models | Status/camera | Print start | Limitations |
|---|---|---|---|
| X1 Carbon, X1, X1E | complete / RTSPS | legacy payload, `file:///sdcard/…` | LAN Liveview must be enabled on the printer |
| P1P, P1S | complete / JPEG :6000 | legacy payload, `file:///sdcard/…` | P1S chamber value deliberately stays `null` |
| A1 mini, A1 | complete / JPEG :6000 | legacy payload, `file:///sdcard/…` | Y/Z movement direction is adapted for i3 |
| P2S, H2S, A2L | new temperatures/AMS / RTSPS or JPEG | new payload, `ftp:///…` | no guessed internal storage paths |
| H2D, H2D Pro, X2D | both nozzles and external spools / RTSPS | only with unambiguous nozzle binding from the 3MF | incorrect or unknown binding is rejected before upload |
| H2C (O1C/O1C2) | status and RTSPS | still rejected with an active hotend rack | rack mapping needs a confirmed printer response; positions are not guessed |

AMS HT uses Orca IDs 128–135 in the LAN payload and always also includes `ams_mapping2`. The mixed A2L AMS lite
(unit 16) is exposed as slots 24–27. On dual-nozzle models, external spools remain separate: left 254, right 255.
Write operations for these new topologies (filament profile and K value) are rejected until their write protocol is
unambiguously established. Chamber temperature control stays disabled for the same reason; status and model-specific
limits are still shown.

## API

All `/api/*` routes except `/api/pair` require `Authorization: Bearer <app_token>`. Errors: `{"error": "<plain text>"}`.

**Language:** English is the default for API errors, setup and invitation pages, and descriptive status texts
(stage names, error/HMS texts and labels). Bambro sends `Accept-Language: en` or `Accept-Language: de` with every
bridge request, matching its current UI language. German is selected with `Accept-Language: de`; missing or unsupported
languages fall back to English. JSON keys, identifiers and codes stay the same in both languages. Bambro's
Settings → Language opens the iOS language choice for Bambro.

The bridge can be self-hosted without a fixed access key: while no device is paired, the bridge writes a one-time
setup code for the first device to its log at startup (`Setup code for the first device: ABCD-EFGH`). The code is
valid for one hour; a restart issues a new one. The setup page (`GET /`) never shows it, nor the certificate
fingerprint or the printers; over HTTPS it offers a form (`POST /setup` with `code`) that shows the pairing QR code
(address, code and fingerprint) to whoever enters the code. After the first device, new devices pair only through a
ten-minute invitation from a paired device. Removing the last device does not reopen pairing; restart the bridge for
a new setup code. If `BRIDGE_ACCESS_TOKEN` is set, there is no setup code; use the fixed key or an invitation to
pair. An invitation can also be created on the setup page (`POST /invite` with the key as a form): only over HTTPS
(built-in TLS listener or a trusted proxy forwarding HTTPS); the page then shows the QR code and code. Device tokens
are issued only once and stored only as SHA-256 digests.

**Throttling and limits:** wrong setup codes, invitation codes and access keys (`/api/pair`, `/setup`, `/invite`)
count per client address: ten failures within ten minutes block that address for ten minutes, also for correct
values; other clients are not affected. Behind a trusted proxy, the client address is the nearest untrusted entry of
`X-Forwarded-For`; IPv6 clients count per /64. Each failure holds one of four bridge-wide check slots for 1.5 seconds,
and with more than 32 waiting checks, further attempts get 429 without a check. Request bodies are limited while they
are read: 4 KiB for `/api/pair`, `/setup` and `/invite`, 64 KiB for other JSON routes (413 beyond). Only
authenticated uploads (`POST /api/jobs`) stream up to `BRIDGE_MAX_UPLOAD_MB`, at most two at a time (429 otherwise).

**Per printer:** status, events, controls, AMS, current print, SD card, camera, profiles and Live Activity state are
available under `/api/printers/{id}/…` (id = serial number), for example `/api/printers/{id}/status`. Paths without a
printer component (`/api/status`, etc., in the table below) address the default printer; Bambro uses them while no
printer is selected. Jobs are shared; each belongs to a printer (`printer`), uploaded through `/api/printers/{id}/jobs`
or `/api/jobs` (default printer). `POST /api/jobs/{id}/slice` with `{"printer": "<id>"}` slices a model for another
printer. Jobs saved by 0.x bridges, which had no printer, are assigned to the default printer at startup.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/printers` | printers with a brief status (model, printer profile, connected, print state), `default`, known models |
| POST | `/api/printers/probe` | check `{"host", "access_code"}` without saving → serial number, model, firmware, nozzle, Developer Mode, printer profile, `configured`. Errors include `code`: `unreachable` and `timeout` (502), `not_bambu`, `access_code` and `not_local` (422, not a local network address) |
| POST | `/api/printers` | set up: `{"host", "access_code", "name"?}` → 201; the first becomes the default |
| GET/PATCH/DELETE | `/api/printers/{id}` | read / change (`name`, `host`, `access_code`, `default: true`; new credentials are checked and must lead to the same printer: 409 with `code` `other_printer`, or `certificate_changed` for a new address with the stored code) / remove. A printer configured through environment variables can only be changed through the environment (409); making it the default is allowed |
| GET/POST | `/api/printers/{id}/jobs` | jobs for this printer / upload a file for it |

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | `{"ok": true}`, no authentication |
| POST | `/api/pair` | `{"token": "<access key>"}` → `{"app_token": …}`, derived from the key (HMAC-SHA256): the same token after restarts, a new one when the key changes |
| POST | `/api/pair` | `{"code": "ABCD-EFGH", "device_name": "…"}` → device token and bridge identity (201) |
| POST | `/api/pairing` | create a single-use, ten-minute invitation while authenticated |
| GET | `/api/devices` | paired app devices and current authentication type |
| DELETE | `/api/devices/{id}` | revoke a device; self-removal is allowed |
| GET | `/api/status` | normalized printer status, including `connection.developer_mode`, `job.error` (`code`, `value`, `text`, `actions`, `acknowledge`), `job.job_id`, `hms[].text`, `options`, `ams.change` (`{"target": slot}` while the AMS loads that slot, `{"target": null}` while unloading, otherwise `null`), and per slot `spool` and `new_spool` (see Filament library and New spools) |
| GET | `/api/events` | Server-Sent Events: `status` for this printer (at most 2/s, the latest state always follows), `job`; a `: ping` comment after 15 s without events |
| POST | `/api/control` | `{"action": …, "value": …}`, see below |
| POST | `/api/ams/tray` | assign a slot: `{"slot", "filament": "<profile name>", "color": "#RRGGBB[AA]", "colors"?, "gradient"?}` (`ams_filament_setting`). Alpha `00` = clear, `80` = translucent. `colors`: two to four colors of a multicolor spool, as segments or with `gradient: true` as a gradient; printers with capability `multi_color` store them (`cols`, `ctype`), others only the first. The printer may report the previous filament for a few seconds; the bridge requests the full status again 3 and 10 seconds later |
| GET | `/api/ams/k-values` | printer's K value table for the installed nozzle: `{"nozzle_diameter", "values": [{"cali_idx", "filament_id", "name", "k", "n"}]}` |
| POST | `/api/ams/k-value` | `{"slot", "cali_idx"}`: select a K value for a slot, `-1` = filament profile default; not for the slot currently in use for printing; full status again 3 and 10 seconds later, as for `/api/ams/tray` |
| GET | `/api/ams/drying?ams_id=n` | drying suggestion (AMS 2 Pro, AMS HT): `temperature`, `hours`, `max_temperature`, `temperature_range`, `hours_range`, `cooling_temperature`, `filament_type`, `from_profiles` |
| GET | `/api/print/objects` | objects in the current print: `{"plate", "bed", "objects": [{"id", "name", "skipped", "bbox"}]}`; `id` is the object's `identify_id` (for `skip_objects`), `bbox` its outline on the bed in mm or `null`, `bed` the bed size |
| GET | `/api/print/thumbnail` | thumbnail of the current print's plate (`Cache-Control: no-store`) |
| GET/DELETE | `/api/sdcard` | print files (`/`, `/cache`) and timelapses (`/timelapse`) / delete (`?path=`) |
| POST | `/api/sdcard/import` | import `.gcode.3mf` (up to `BRIDGE_MAX_UPLOAD_MB`) from the card as a job; print from its existing location, only while size and listing time on the card are unchanged |
| GET | `/api/sdcard/download` | stream a file (`?path=`, e.g. timelapse AVI), in chunks during the FTPS transfer, without `Content-Length`; a printer-side interruption ends the response without a closing chunk |
| GET | `/api/camera/snapshot` | current JPEG |
| GET | `/api/camera/stream` | MJPEG (`multipart/x-mixed-replace`), each frame includes `X-Frame-Time` (capture time, Unix) |
| GET | `/api/profiles` | qualities, build plates, supports, filaments |
| GET/POST | `/api/jobs` | list jobs / upload a file (multipart, field `file`) |
| GET/DELETE | `/api/jobs/{id}` | read / delete a job |
| POST | `/api/jobs/{id}/slice` | `{"slot", "quality", "plate", "support", "infill", "walls", "brim", "orient", "copies", "plate_index"}`; STL and similar files are oriented (`orient`) and arranged by default, `copies` 1–50 (overflow goes on further plates), `support` `off`/`tree`/`normal` (both "auto": supports only where needed) |
| GET | `/api/jobs/{id}/preview` | slicing preview (OPV1 format, see `bridge/preview.py`), `?plate=n` for a specific plate; the first is precomputed after slicing |
| GET | `/api/print/preview` | preview of the current print |
| POST | `/api/jobs/{id}/print` | `{"mapping": {"<filament>": <slot>}, "plate", "timelapse", "bed_leveling", "flow_cali", "nozzle_offset_cali"?, "extrude_cali_manual_mode"?}` → 202, sends in the background; `flow_cali` only where `printer.flow_calibration` is not `false`; `nozzle_offset_cali` (`true`/`false`) on models with nozzle offset calibration (`capabilities.calibrations.nozzle_offset`), `extrude_cali_manual_mode` (0 or 1) on models with `capabilities.pa_mode` |
| GET | `/api/jobs/{id}/thumbnail` | plate thumbnail (PNG, `Cache-Control: no-store`: slicing again changes it) |
| GET | `/api/live` | Live Activity: `enabled` (APNs key or relay configured), iPhones with push to start (`devices`), iPhones with notifications (`notifications`), this printer's activity update tokens (`activities`) and `printing`: the bridge holds an activity for this printer, from the start of a print until it removes the finished activity |
| POST | `/api/ams/spool` | Spool with an NFC tag in a slot: `{slot, uid, action: load\|write, choice: auto\|restore\|new, weight, remaining, type, brand, subtype, color, nozzle_temp_min, nozzle_temp_max}` → `{spool, decision}`; see Filament library |
| GET | `/api/spools` | Filament library: all spools (Bambu and tagged) with remaining amount, share, length, location and statistics |
| POST | `/api/spools` | Spool with an NFC tag added in the library, without a slot: the body of `/api/ams/spool` without `slot` → `{spool, decision}`; a new spool stays unloaded, a known one stays where it is; see Filament library |
| PATCH / DELETE | `/api/spools/{id}` | Correct the remaining amount (tagged spools, grams as a number), note (text), `unload: true`; other types return 422 without changing anything; delete (kept as a tombstone for 180 days) |
| POST | `/api/live/register` | app token: `{"kind": "start"\|"update"\|"alert", "token": "<hex>", "environment": "development"\|"production", "activity_id", "printer"?, "installation", "events", "language"?}`; `printer` names an update token's printer by the activity's `printerId` attribute (default printer if omitted). A new start or device token for the same installation (or device) replaces the old one. `alert` is the device token for notifications; `events` selects from `finished`, `failed`, `paused`, `error`; an empty list unregisters. Registrations belong to the paired device that made them (or to the shared access key): another credential cannot replace them, and a token registered by another device that is still paired returns 409, unless the same `installation` registers it again (the iPhone paired anew) |
| DELETE | `/api/live/register?installation=<ID>` | remove the start, update and alert registrations this app installation made with the calling credential (its paired device, or the shared access key), for example when the app disconnects from the bridge → `{"removed": n}`. For a paired device, its registrations without an installation ID (older app versions) go too. 422 for a missing or invalid ID; older bridges answer 405 |

**Push language:** `POST /api/live/register` accepts an optional `"language": "en"|"de"` (default `"en"`). The bridge
stores it per token, for `start`, `update` and `alert` tokens. Notifications, the Live Activity start alert and
texts inside the Live Activity content state use that token's language, including while the app is closed. The app
registers its current language; `Accept-Language` selects request texts, while the token's `language` selects pushes.

Slots are numbered globally: AMS 1 = 0–3 (A1–A4), AMS 2 = 4–7, external spool = 254.

### Control actions (`POST /api/control`)

| Action | `value` | Allowed | Printer command |
|---|---|---|---|
| `pause` / `resume` / `stop` | – | printing / paused / printing | `print.pause` / `resume` / `stop` |
| `light` | `true`/`false` | always | `system.ledctrl` |
| `speed` | 1–4 | printing | `print.print_speed` |
| `temperature` | `{"heater": "nozzle"\|"bed", "target", "extruder"?}` | always; model-specific limits, `extruder` 0/1 for dual nozzles | `M104`/`M140` or `set_nozzle_temp`/`set_bed_temp` |
| `fan` | `{"fan": "part"\|"aux"\|"chamber", "percent"}` | always | `M106 P1/P2/P3` |
| `home` | – | not printing | `G28` (homing); `print.back_to_center` on models with `capabilities.mqtt_homing` |
| `move` | `{"axis": "X"\|"Y"\|"Z", "distance"}` | not printing, after homing, X/Y ≤ 100, Z ≤ 50 mm | relative, software endstops enabled |
| `extrude` | mm (±50) | not printing, after homing, nozzle ≥ 170 °C | `M83` + `G0 E` |
| `load` | slot | not printing, after homing | `print.ams_change_filament` |
| `unload` | – | not printing, after homing | `print.ams_change_filament` (target 255) |
| `read_rfid` | AMS slot (not an external spool, also not one reported as `vir_slot` 254/255) | not printing, not the slot loaded in the toolhead | `print.ams_get_rfid` (`ams_id`, `slot_id`); `M620 R<tray>` on older firmware (P1 < 01.08.01, X1 < 01.09, A1 < 01.06, X1E). The AMS turns the spool for about 20 s; a spool without a chip ends up blank. The bridge fetches the status again after 25 and 60 s |
| `skip_objects` | list of `identify_id` | printing, never all objects | `print.skip_objects` |
| `clear_error` | – | printer shows a message (`print_error`) | `print.clean_print_error`, then `system.uiop` (close dialog, optional) |
| `error_action` | `{"code": "0700_8011", "action"}` | `code` is the message the printer shows, `action` is in its `job.error.actions` | see below, then `system.uiop` (close dialog, optional) |
| `option` | `{"key", "enabled"}` | always; AMS options require an AMS | see below |
| `calibrate` | `{"bed_leveling", "vibration", "motor_noise"}`, each `true`/`false` | not printing; motor noise only where `capabilities.motor_noise_calibration` is not `false` | `print.calibration`, `option` = bitmask (2 bed, 4 vibration, 8 motor noise) |
| `dry` | `{"ams_id", "temperature", "hours", "rotate_tray"?}` | AMS 2 Pro (45–65 °C) or AMS HT (45–85 °C), firmware with remote drying (`fun2` bit 5), not already drying, no blocking condition; at most the loaded filament's heat deflection temperature, 1–24 h | `print.ams_filament_drying`, `mode` 1; filament type and cooling target from the bridge's suggested settings |
| `dry_stop` | `{"ams_id"}` | AMS with a heater | `print.ams_filament_drying`, `mode` 0 |

Calibration: the printer runs it as `auto_cali_for_user.gcode`; status then reports `job.calibration` and
`job.name` "Calibration" ("Kalibrierung" in German), the planned steps as `job.stages` (`stg`), and the current step as
`job.stage_code` (`stg_cur`). Motor noise calibration availability is in `home_flag` bit 21 (`capabilities`).

Recovery actions (`error_action`), as Bambu Studio sends them (`err` = message code as a decimal string,
`job_id` = `job.job_id` or empty):

| `action` | Bambu Studio button | Printer command |
|---|---|---|
| `resume`, `resume_defects`, `resume_solved`, `resume_loaded` | Resume Printing; Resume (defects acceptable); Resume (problem solved) or Problem Solved and Resume; Filament Loaded, Resume | `print.resume` with `err`, `param: "reserve"`, `job_id` |
| `stop` | Stop Printing | `print.stop` with `err`, `param: "reserve"`, `job_id` |
| `ignore`, `ignore_resume` | Ignore. Don't Remind Next Time; Ignore this and Resume | `print.ignore` with `err`, `param: "reserve"`, `job_id` |
| `continue`, `retry`, `retry_extrusion` | Finished, Continue; Retry (problem solved) or Retry; Not Extruded Yet, Retry | `print.ams_control`, `param: "resume"` |
| `extruded` | Filament Extruded, Continue | `print.ams_control`, `param: "done"` |
| `abort` | Abort | `print.ams_control`, `param: "abort"` |
| `alarm_off` | Got it, Turn off the Fire Alarm | `print.buzzer_ctrl`, `mode: 0` |
| `stop_drying` | Stop Drying | `print.auto_stop_ams_dry` |

Options (`key`): `auto_recovery` → `print.print_option` with `auto_recovery` (and the `option` bitmask),
`ams_backup` → `print.print_option` with `auto_switch_filament`, `rfid_on_startup`, `rfid_on_insert`,
`remain_estimate` → `print.ams_user_setting` (all three together; the other two retain the printer's reported values).
The bridge reads their state from `home_flag` (bit 4 step loss, bit 7 remaining amount, bit 10 AMS backup) and the AMS
block (`power_on_flag`, `insert_flag`, and `calibrate_remain_flag` if reported). If Developer Mode is off, all actions
return 409 with instructions for enabling it.

Drying: status reports each AMS unit's state under `drying` (`null` for AMS and AMS lite): `supported` (firmware
allows remote drying), `status` (`off`, `checking`, `drying`, `cooling`, `stopping`, `error`, `heat_out_of_control`),
`phase` (`heating`, `dehumidifying`), `remaining_minutes`, current `settings`, and `blocked` (reasons drying cannot
start, e.g. `task_occupied`, `insufficient_power`, `filament_at_outlet`). Read as in OrcaSlicer: bits 4–7 and 22–23 of
`info`, `dry_time`, `dry_setting`, `dry_sf_reason`. Suggested settings come from filament profiles
(`filament_dev_ams_drying_temperature` and `…_time` per AMS type, idle and while printing; softening point as the
cooling target, heat deflection temperature as the upper limit); for multiple spools, the lowest temperature and
longest duration apply. Without profile values, the bridge conservatively suggests 45 °C for 4 h. Not tested on
hardware (no heated AMS available).

"Not printing" means IDLE, FINISH or FAILED. "After homing" means `home_flag` reports X, Y and Z as homed (bits 0–2,
exposed as `homed` in the status); if the printer reports nothing, it is treated as not homed. Until the printer has
reported its state (UNKNOWN), the bridge rejects movement and print starts. Print starts are mutually exclusive.
The bridge finds objects in its own job file or downloads `<job>.gcode.3mf` from the SD card, as stored there by
OrcaSlicer and Bambu Studio. Thumbnail, objects, preview, `skip_objects` checks and filament booking use the same
match of the running print to a job, because job names are not unique: first the card file the printer reports
(`gcode_file`; bridge uploads carry the job ID in their name, imports keep their card path), then the print the bridge
started itself until the printer leaves it, and the name only when the printer reports no card file, the bridge did
not see the print start (for example after a restart) and exactly one sent job has that name, plate and subtask.
A job sliced again while it prints, or an imported file replaced on the card, is read from the card instead. Card
downloads are limited to `BRIDGE_MAX_UPLOAD_MB`; the `printcache` keeps the newest files with their previews.

## Architecture

| Module | Responsibility |
|---|---|
| `__main__.py` | start HTTP and HTTPS listeners and stop cleanly on SIGTERM; `python -m bridge announce` runs `announce.py` |
| `server.py` | aiohttp app: routes, transport and authentication checks, SSE, slicing and print sending as background tasks, resuming after restart, shutdown order |
| `auth.py`, `identity.py` | pairing (setup code, invitations, fixed access key), device tokens, throttling and revocation; the bridge's ID and name (`/data/auth`) |
| `tls.py`, `web.py` | built-in HTTPS certificate (`/data/tls`); setup page, invitation page, QR links and trusted-proxy checks |
| `mdns.py`, `announce.py` | Bonjour announcement of the bridge, and announce-only mode for bridges without LAN multicast |
| `devices.py` | printers: settings in `printers.json`, MQTT, camera, locks, Live Activity, spool tracker and new spool watcher per printer; shared profile catalogs per printer profile |
| `trust.py` | printer certificate pins for MQTT, FTPS, the JPEG camera and the RTSPS port check |
| `probe.py` | check a printer: serial number from its certificate, authentication, version and state (read only) |
| `models.py` | printer models from OrcaSlicer (`resources/printers`), capabilities per firmware |
| `printer.py` | MQTT client (aiomqtt): connection with backoff, regular `pushall`, commands with `sequence_id` and waiting for the response |
| `state.py` | merge partial updates (`merge`, lists by `id`; a full report drops AMS units that are gone) and normalize for the app (`normalize`): job, AMS, HMS, fans, Developer Mode, options |
| `hms.py` | plain text for `print_error` and HMS per model (English by default; German on request, with English fallback) |
| `recovery.py` | recovery actions per display message from Bambu Lab's action table (`error_actions.json`) and their commands |
| `controls.py` | control actions → MQTT commands, with the state rules in the table above |
| `camera.py`, `camera_rtsp.py` | model-specific camera facade: JPEG on port 6000 or a shared RTSPS/ffmpeg process; disconnects 20 s after the last viewer |
| `print_command.py`, `print_metadata.py` | legacy/new print payloads and complete filament/nozzle mapping from the sliced 3MF |
| `ftps.py` | implicit FTPS with TLS session reuse; upload without TLS shutdown on the data channel (the P1S does not answer `close_notify`) |
| `profiles.py` | profile catalog: resolve inheritance, adapt vendor profiles, write app options into process profiles |
| `slicer.py` | invoke the OrcaSlicer CLI (arguments, timeout, understandable error texts) |
| `jobs.py` | job storage on the volume (`/data/jobs/<id>/job.json`), 3MF parsing, SD card filenames |
| `live.py`, `apns.py`, `relay.py` | Live Activity: printer state → start/update/end, tokens; APNs over HTTP/2 with ES256 JWT, or the push relay |
| `notify.py` | notifications for finished, failed, canceled and paused prints and severe HMS messages |
| `spools.py` | filament library (`spools.json`): Bambu and tagged spools per slot, usage booked per print |
| `newspool.py` | new spools without a chip: insertion detection and the Generic setting per slot (see New spools) |
| `preview.py` | G-code → OPV1 preview format (lines per layer and type, simplified, uint16 in 0.01 mm) |
| `config.py`, `branding.py` | environment variables; fixed identifiers (Bonjour service type, pairing link scheme, relay URL) |
| `i18n.py`, `i18n_de.py` | deferred English messages, rendered in the reader's language at the boundary; German texts |

### Jobs

States: `uploaded` → `slicing` → `sliced` → `sending` → `sent`, or `failed` on error. A job holds slicing settings
(`settings`), the result per plate (`result.plates[]`: time, weight, filaments, objects, `outside`), the saved print
request (`pending`) and current step (`step`) while sending, then `sent`.
On upload, the bridge checks that the file is complete (`check_complete`): 3MF requires the ZIP central directory at
the end, binary STL requires as many bytes as the triangle count in its header specifies, and text STL requires
`endsolid`. Truncated files (interrupted downloads) are rejected with 422 and an explanation instead of failing
later during slicing.
**3MF limits:** at most 20,000 archive entries and 8 GiB uncompressed in total; XML and JSON metadata up to 16 MiB;
G-code is read in chunks, up to 2 GiB per plate, and lines longer than 64 KiB are skipped. Plate numbers must be
unique and between 1 and 1000, filament IDs between 1 and 64; project filament data (`project_filaments`) keeps only
short text values. Previews split long paths and simplify the layers read so far while they are built, so a preview
stays within a fixed number of points; G-code that is still too complex gets no preview (409).
A damaged `job.json` does not stop the bridge: it is renamed to `job.json.damaged` and logged, other jobs load.
`remote_path` means the file is already on the SD card and will not be uploaded again. `uploaded` is the name under
which the current slice result is already on the card; `card_file` holds size and listing time of the imported or
uploaded card file, and a print from the card (an import, or a further plate) requires both to be unchanged.
`sent_plates` records plates that have already been started. Slicing again resets them.

**Supports:** OrcaSlicer sets `support_used` in the 3MF even when supports are merely enabled. The bridge therefore
reports `support_used` = "the G-code contains support paths" (`; FEATURE: Support…`) and `support_enabled` = "enabled".
With `tree`/`normal` (auto), OrcaSlicer places supports only under overhangs shallower than the quality profile's
threshold (`support_threshold_angle`, 15° at 0.08 mm to 40° at 0.28 mm, default 30°).

**Flow dynamics (K factor):** the bridge determines whether the printer measures flow dynamics before a print
(`flow_cali`) from the start G-code of the printer profile for the reported model. The printer measures only where
it checks `extrude_cali_flag` (X1 with Lidar; A1, H2 and P2S with a force sensor). The P1S has no sensor for this.
The result appears in status as `printer.flow_calibration` (`true`/`false`, `null` for an unknown model); when `false`,
the bridge does not forward `flow_cali`. `GET /api/ams/k-values` reads stored K values (for example from pressure
advance calibration in OrcaSlicer or Bambu Studio) from the printer (`extrusion_cali_get`). `POST /api/ams/k-value`
selects one for a slot (`extrusion_cali_sel`). Status reports the selected entry per slot as `cali_idx`
(`-1` = filament profile default), and the effective value as `k`.

**Limitations, not tested on hardware:** the bridge reads dual-nozzle mapping from the 3MF (`filament_map` counts
from 1 like Bambu Studio: 1 left, 2 right; the printer reports 0 right, 1 left). The RTSPS camera starts ffmpeg with
the access code in the URL, making it readable in the process list (`/proc`) inside the container. ffmpeg provides
no other way to pass it. The bridge expects the camera port (322) to present the same certificate as MQTT, as port
990 and port 6000 do on the P1S; otherwise the camera reports a changed certificate.

## Configuration

| Variable | Required | Meaning |
|---|---|---|
| `BRIDGE_ACCESS_TOKEN` | | optional shared access key, at least 20 characters; setting it suppresses the public setup code |
| `BRIDGE_NAME` | | bridge display name; without this variable, the name in `/data/auth/bridge.json` is preserved |
| `BRIDGE_TLS` | | `on` (default) or `off`; HTTPS also listens on 8443 by default |
| `BRIDGE_HTTPS_PORT` | | HTTPS port, default 8443; HTTP remains available on `PORT` (default 8080) |
| `BRIDGE_TLS_CERT`, `BRIDGE_TLS_KEY` | | custom certificate and key, only together; otherwise a persistent self-signed P-256 certificate under `/data/tls` |
| `BRIDGE_PUBLIC_URL` | | external HTTPS base URL for QR links, for example behind a reverse proxy |
| `BRIDGE_TRUSTED_PROXIES` | | comma-separated peer CIDRs whose `X-Forwarded-Proto` and `X-Forwarded-For` are used. With `BRIDGE_TLS=off`, the API, `/invite` and `/setup` answer only these peers, and only for requests forwarded with `X-Forwarded-Proto: https`. Keep it to the proxy itself: every listed address can claim HTTPS |
| `BRIDGE_PLAIN_HTTP` | | `on` lets the API answer plain HTTP from every peer when `BRIDGE_TLS=off` and `BRIDGE_TRUSTED_PROXIES` is unset (isolated networks, VPN); logged as a warning. Without it, that combination does not start |
| `BRIDGE_MDNS` | | `on` (default) or `off`; announces `_orcbro._tcp.local.` without codes or tokens, with `BRIDGE_PUBLIC_URL` as `url` in the TXT record. Addresses come from the host name and from the interface used for multicast and the default route (hosts whose name resolves to 127.0.1.1 still announce their LAN address); they are checked every minute, so a bridge without an address retries and a new address is announced. Without multicast to the LAN (Kubernetes, Docker without host networking): a second container running `python -m bridge announce` with host networking and `BRIDGE_PUBLIC_URL` |
| `BAMBU_HOST`, `BAMBU_ACCESS_CODE` | | one printer from the environment (both or neither); further printers are configured in the app |
| `BAMBU_SERIAL` | | its serial number; if unset, the bridge reads serial number and certificate from the printer on first start and keeps them (`env` and `pins` in `printers.json`). A different device at the address later fails the certificate check. Setting `BAMBU_SERIAL` or a new `BAMBU_ACCESS_CODE` enrolls again; a new access code also trusts the printer's current certificate, like entering the code again in the app |
| `BAMBU_NAME` | | display name (default: model) |
| `BAMBU_MACHINE_PROFILE` | | fixed printer profile for this printer; otherwise follows the model and nozzle |
| `BAMBU_PRINT_URL` | | explicit path for `project_file`; if unset, automatically uses legacy `file:///sdcard/{name}` or new-generation `ftp:///{name}` |
| `BRIDGE_DATA_DIR`, `BRIDGE_KEEP_JOBS`, `BRIDGE_MAX_UPLOAD_MB` | | `/data`, 40 jobs, 300 MB |
| `ORCA_CMD`, `ORCA_PROFILES_DIR`, `ORCA_FILAMENT_IDS`, `ORCA_PRINTERS_DIR`, `ORCA_SLICE_TIMEOUT` | | preset in the image |
| `HMS_TEXTS_DIR` | | plain-text message tables, `/opt/hms` in the image; if absent, only codes are available |
| `APNS_KEY_FILE`, `APNS_KEY_ID`, `APNS_TEAM_ID`, `APNS_BUNDLE_ID` | | Live Activity with your own APNs key for the app (only if you build the app yourself). Default bundle ID `it.blankk.bambro` |
| `BRIDGE_PUSH_RELAY` | | without your own APNs key, Live Activities and notifications use the relay at `https://push.blankk.it` (`relay/`); another HTTPS URL or `off`. Relay ID and key are stored in `/data/relay.json` |
| `BRIDGE_RESET_NEW_SPOOLS` | | `on` (default) or `off`: a spool without a Bambu chip inserted into an AMS slot is set to Generic of the slot's previous material in gray instead of keeping the old filament and color (see New spools) |
| `TZ` | | time zone for log timestamps, for example `Europe/Berlin`; UTC by default |

## Development and checks

```sh
pip install -r requirements.txt
PYTHONPATH=.:tests python3 -m unittest discover -s tests -v   # no printer; fake_orca.py replaces the CLI
# Locally against a real printer, using OrcaSlicer on the Mac:
BAMBU_HOST=<printer-IP> BAMBU_ACCESS_CODE=… BRIDGE_DATA_DIR=/tmp/orcbro \
  ORCA_PROFILES_DIR=<OrcaSlicer-repo>/resources/profiles/BBL \
  ORCA_FILAMENT_IDS=<OrcaSlicer-repo>/resources/printers/bambu_filament_ids.json \
  ORCA_PRINTERS_DIR=<OrcaSlicer-repo>/resources/printers \
  ORCA_CMD=/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer PORT=8787 python3 -m bridge
```

If `OrcaFilamentLibrary`, `Creality/filament` and `Anycubic/filament` are next to the BBL directory, the bridge also
reads their vendor profiles.

To build the image locally on Apple Silicon, see the aarch64 AppImage build arguments at the top of `Dockerfile`.
`tests/fixtures/*_p1s.json` are real, anonymized messages from a P1S (firmware 01.10.00.00).

## Filament library

`bridge/spools.py`, stored in `spools.json`. Two kinds of spools:

- **Bambu spools** (chip): the AMS reports a spool ID (`tray_uuid`), the full weight and, with "estimate remaining"
  on, a remaining percentage. The bridge records every Bambu spool it sees in a slot and keeps its last state when the
  spool is removed. Their amount always comes from the AMS.
- **Tagged spools** (NFC, OpenSpool): the AMS cannot read the tag. Bambro reads it when the spool is loaded and calls
  `POST /api/ams/spool` with the tag UID, the full weight (`weight`) and the amount on the tag. A known spool keeps
  the amount the bridge counted; a new one starts with the tag's amount, else full. A slot the AMS reports physically
  empty (or a Bambu spool in it) unloads the spool: `present: false` on units with exist bits, otherwise `empty`. A
  spool that is present but has no filament set yet (the P1 series reports a newly inserted chipless spool as a bare
  slot until the app sets it) stays loaded, and for two minutes after a tag is read or written for a slot, an empty
  slot does not unload it. External spools have no sensor and stay until another tag is read for that slot.
  Removing a printer unloads its spools.
- **In the library** (Bambro: Filament Library → Add Spool), a tag is read or written without a slot:
  `POST /api/spools` takes the same body and makes the same decisions as `/api/ams/spool`, but places nothing. A new
  spool stays unloaded until its tag is read for a slot; a known spool keeps its slot and its counted amount.

After each print the bridge books the usage: grams per filament from the sliced plate (`slice_info`), mapped to slots
by the bridge's own `ams_mapping` or, for prints from OrcaSlicer/Bambu Studio, by material, color and the time each
slot was active (`tray_now`; AMS backup splits by time). Canceled or failed prints count with their progress. Tagged
spools lose the grams; for Bambu spools only the statistics change (prints, used grams, last used). The print in
progress is stored, so a restart keeps the time per slot. When the print file cannot be read (not on the card, FTPS
error), the bridge tries again after 30 s, doubling up to 10 minutes, at most 10 times per print; calibrations need
no file.

Deleted spools stay as tombstones for 180 days so a tag scanned again is recognized: other filament data on the tag
means a rewritten tag → new spool; the same data returns `decision: "choose"` with `suggest: "new"` when the old spool
was nearly empty (≤ 20 g or ≤ 5 %, the tag likely moved to a fresh spool) or `"restore"` otherwise, and the app asks.
Writing a tag (`action: "write"`) keeps the counted amount when material and color stay the same, else it starts a new
spool with `remaining` (an opened spool; at most `weight`) or full; the app writes the returned `remaining` to the tag
(`remaining_weight`). A Bambu spool that shows up
again after being deleted simply returns.

The status adds `spool` (same fields as `/api/spools`) to every slot with a known spool.

## New spools

`bridge/newspool.py`. When a spool without a Bambu chip goes into an AMS slot, the P1 series applies the slot's
previous filament and color again (the AMS keeps the last settings for third-party filament). A new spool is rarely the
same filament, so the bridge sets the slot to the Generic profile of the previous material instead (SUNLU PLA Orange →
Generic PLA, PETG → Generic PETG; a material without a Generic profile gets its most general profile, an unknown
material Generic PLA), in neutral gray `#8E8E93`, with that profile's filament ID and nozzle temperatures
(`ams_filament_setting`, the same command as `POST /api/ams/tray`). The log says which slot was set to what.

- **Insertion**: a slot whose `present` goes from `false` to `true` (`tray_exist_bits`) while the bridge is connected.
  Slots without presence bits (`present: null`) are left alone, and a reconnect is not an insertion.
- **Settle**: the AMS first reads the spool (`tray_reading_bits`), then the firmware reports the slot's old setting. The
  bridge waits until the AMS no longer reads the slot and its report (material, color, filament ID, chip) has not
  changed for 5 s. A slot that still reports no filament waits until 30 s after the insertion, and on printers that do
  not report reading bits the bridge waits at least 20 s (the time the AMS needs to turn a spool). 60 s after the
  insertion it decides anyway; a slot the AMS is still reading then is left alone. The setting is sent once.
- **Left alone**: Bambu spools (`tag_uid` or `tray_uuid` from the chip); slots whose filament was set
  (`POST /api/ams/tray`) or for which a tag was read (`POST /api/ams/spool`) after the insertion or in the two minutes
  before it; slots that settle on a filament other than their last setting (set on the printer or in Bambu Studio
  meanwhile); the slot a print runs from (`active_slot` during PREPARE, RUNNING or PAUSE, for example a spool replaced
  after a runout); and printers with Developer Mode off. The log names the reason. Re-inserting the same spool counts
  as a new spool: the AMS cannot tell the two apart.
- **Flag**: the slot reports `new_spool: true` (all other slots `false`) until its filament is set, a tag is read for
  it, the printer shows another filament there after it showed the Generic setting (set on the printer or in Bambu
  Studio), a Bambu spool is read in it, or the spool is removed. Bambro shows a hint on the slot. The flag lives in
  memory; it is gone after a bridge restart.
- `BRIDGE_RESET_NEW_SPOOLS=off` turns this off. Bambro's direct mode does the same while it is connected, with a
  setting per printer (Settings → Printers → printer → Reset New Spools to Generic).

## Notifications and push relay

Alongside the Live Activity, the bridge sends notifications (`bridge/notify.py`): print finished, failed or canceled
(`0300_400C` counts as canceled), paused (with a reason from the error or HMS), and new severe HMS messages (`fatal`,
`serious`) during a print unless the pause already covers them. Only transitions the bridge observes are reported;
a restart therefore does not produce a second "finished" notification. Each iPhone receives only the events it
registered under `events`. The app registers its device token only with notification permission and unregisters with
an empty list as soon as permission is missing or notifications are turned off in Bambro. Notification and Live
Activity texts use each token's registered `language`, English by default or German when selected in the app.

Without your own APNs key, Live Activities and notifications go through the relay (`relay/`, Cloudflare Worker):

- `POST /v1/bridges`: register once, receiving an ID and key (HMAC of the ID using the relay secret, stateless),
  stored in `relay.json` (0600). Then use `Authorization: Bearer <id>.<key>`; 401 triggers a new registration. While
  the relay rotates its secret, it still accepts the old key and returns the new one in `x-bridge-key`; the bridge
  stores it and keeps its ID, so its tokens stay bound to it.
- A push token belongs to the first bridge that uses it (KV: SHA-256 of token → ID, seven days, refreshed daily on
  use). Another bridge receives 403 `TokenBoundElsewhere` and discards the token; the app registers it again on its
  next launch. The relay checks the binding before counting a push against the device's rate limit.
- `POST /v1/live-activity` accepts Live Activities without `alert`; the relay builds the start alert from
  `content-state.jobName` and `attributes.printerName`. It rebuilds the payload from the known fields (others are
  dropped), shortens texts and keeps `stale-date`, `dismissal-date` and `finishAt` close to `timestamp`, which must be
  within a day of the relay's clock. `POST /v1/notification` accepts `{token, environment, template, job, printer,
  detail?, thread?, language?, id?}` (templates `finished`, `failed`, `cancelled`, `paused`, `error`); a notification
  with the same `id` reaches a token only once a day. Both
  endpoints accept optional `"language": "en"|"de"` (default `"en"`, any other value returns 422). The bridge sends
  the token's language so the relay builds the start alert and notification in the same language. The texts match
  in `relay/src/templates.ts` and `bridge/notify.py`; Live Activity content state already comes from the bridge in
  the token's language. Relay error messages are in English.

## Operations

Back up the bridge identity (`/data/auth/bridge.json`), paired devices (`/data/auth/auth.json`), the generated
certificate (`/data/tls/`) and printers (`/data/printers.json`) with the data directory; backing up the whole volume
covers all of them. After a restore, check the startup line `Bridge <name> (ID <id>), paired devices: <n>` before
exposing the service again: the ID must be the one from before (otherwise the app no longer recognizes the bridge)
and the count must match Settings → Bridge → Devices in Bambro. Without a fixed key, a setup code in the log means
`auth.json` is missing and pairing is open for one hour. Corrupt identity or authentication files deliberately
prevent startup instead of silently opening a new initial setup. Files are written atomically and readable only by
the bridge process. Revoking a device
also ends that device token's active SSE and camera stream requests and removes its Live Activity and notification
tokens (`push.json`, permissions 0600). Before every push and at startup, the bridge checks that the registering
device is still paired, so a revoked device gets nothing even if `push.json` could not be saved. Registrations made
with the fixed access key belong to that key: `push.json` keeps a salted digest of it, and changing or removing
`BRIDGE_ACCESS_TOKEN` discards them at the next start (iPhones paired with the new key register again). At startup,
the bridge also discards tokens from before device binding was introduced; the app registers them again. APNs and
the relay receive a derived value (`p` + 16 hexadecimal SHA-256 characters) instead of the serial number.

**Printer certificate.** The bridge remembers each printer's SHA-256 certificate fingerprint (during setup, or on
first contact for older installations, then only with a matching serial number in the certificate) and checks it
during the TLS handshake before sending the access code: MQTT, FTPS (including the resumed data channel) and the JPEG
camera. Before starting ffmpeg (RTSPS), it completes a handshake on the camera port (322) itself and checks the pin
there; ffmpeg then opens its own, unpinned connection. A new address with a stored code is accepted only with the same
certificate; a new certificate requires entering the access code again. Stored under `pins` in `printers.json`; the
`BAMBU_HOST` printer's enrolled serial number is under `env` (with a digest of its access code).

**HTTP and HTTPS.** With built-in TLS, port 8080 serves only the setup page and `/healthz`; API, pairing and
invitation requests return 403 there, except through a trusted proxy with `X-Forwarded-Proto: https`. The access key
field for invitations appears only over actual HTTPS.

The self-signed certificate protects initial pairing only together with the SHA-256 fingerprint in the QR link or
the Bonjour announcement. The setup code reaches the operator through the log, not the page. Behind an HTTPS reverse
proxy, set `BRIDGE_PUBLIC_URL` and `BRIDGE_TRUSTED_PROXIES`; forwarded headers from other peers are ignored, and with
`BRIDGE_TLS=off` the bridge refuses to start without trusted proxies unless `BRIDGE_PLAIN_HTTP=on`.

- **Logs:** `docker compose logs -f orcbro`. Startup logs show printers, the pairing method and setup code. HTTPX
  request lines for APNs (own APNs key) show `/3/device/<token>` instead of the device token.
- **Shutdown** (SIGTERM, `docker compose stop`): the bridge first stops its background work (slicing, including
  OrcaSlicer itself, uploads to the printer, status refreshes), then the printer connections, pushes and pairing.
  A print that was still being sent resumes when the bridge is back within 10 minutes of the print request.
- **New access code** (the printer generates one, for example, after switching Wi-Fi networks): change the IP address
  or access code in the app under Settings → Printers; for a printer configured through environment variables, change
  `BAMBU_ACCESS_CODE` there and restart the container.
- **Stored printers:** `/data/printers.json` (permissions 0600, contains access codes); back it up with the volume.

| Symptom | Cause and solution |
|---|---|
| Log: "Incorrect access code" | printer's code changed (Wi-Fi switch) → update it in the app or `BAMBU_ACCESS_CODE`, see above |
| App: "No printer configured yet" | bridge has no `BAMBU_HOST` and no stored printers → add a printer in the app |
| Upload fails after 60 s with "The read operation timed out" | TLS shutdown on the FTPS data channel; fixed (`ImplicitFTPS.storbinary`) |
| OrcaSlicer: "print_job failed, result = -26" | Developer Mode is off on the printer |
| App: "Developer Mode is off", commands return 409 | `flag3` bit 4 set, often after a firmware update → on the printer, Settings → WLAN or Network → LAN Only → Developer Mode |
| Status or camera frequently drops out | printer's Wi-Fi; `connection.wifi_signal` in status, unreliable below roughly −80 dBm |
| Job: "The bridge was restarted …" | restart while sending, job older than 10 minutes → start it again in the app |
| No model image in the Printer tab for prints from OrcaSlicer | the bridge fetches `<job>.gcode.3mf` from the card over FTPS; the printer often drops FTP connections at print start. The bridge retries (listing and download, one download at a time); the app checks up to 15 times |
| "The printer is busy" | printer is not in IDLE/FINISH/FAILED, or its state is still unknown (UNKNOWN) |
| Log: "Printer profile … is unavailable" | the profile (`BAMBU_MACHINE_PROFILE`, or the one for the reported model and nozzle) is missing or damaged → check the name; the bridge tries again every five minutes |

## Open issues

Known issues from the security review on November 21, 2026 (the others have been resolved):

- **Roles:** every paired device can invite and revoke devices, change printers and probe printer addresses (local
  network only). Idea: roles for viewing, controlling and administering.
- **RTSPS camera:** ffmpeg receives the URL including the access code on its command line; anyone who can list host
  processes can see it. In the container, this affects only users with access to the host. ffmpeg's own connection
  is not pinned; the bridge checks the camera port's certificate in a separate handshake right before.

- Copies per object in 3MF projects (`--clone-objects "1,3,1"`), multiple files in one job, advance upload.
