# Orcbro – technical reference

Detailed description of behavior, Brotocol (HTTP API) and architecture. For an introduction and quick start, see the
[README](../README.md).

## What it does

- **Printers:** one bridge for any number of Bambu printers. Add them in the app with their IP address and access
  code (`POST /api/printers`); the bridge checks authentication first and reads the serial number (from the printer's
  TLS certificate), model, firmware, nozzle and Developer Mode. Settings are stored in `/data/printers.json`
  (readable only by the bridge). One printer can also be configured through `BAMBU_HOST`/`BAMBU_ACCESS_CODE`;
  the app cannot change it. The printer profile follows the reported model and installed nozzle (`printer_model`
  and `printer_variant` in OrcaSlicer profiles); after a nozzle change, the bridge slices with the matching profile.
  Models and their capabilities come from OrcaSlicer's `resources/printers`.
- **Status:** one MQTT connection to each printer (8883, TLS, `bblp` + access code). P1 partial updates are merged
  and normalized for the app: job, progress, temperatures, fans, light and HMS messages.
- **Plain-text messages:** display errors (`print_error`) and HMS messages use Bambu Lab's English text for the
  model by default. Requests with `Accept-Language: de` use German, falling back to English when unavailable. The
  tables (`hms_de.json.gz`, `hms_en.json.gz`) come from a pinned version of ha-bambulab (MIT); the image downloads
  them with checksum verification (`Dockerfile`, `bridge/hms.py`). Display messages can be acknowledged from the app.
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
  supported for these files (OrcaSlicer does not clone objects from 3MF). `.gcode.3mf` files pass through directly.
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
  and finally an `end` event with the final state (visible for one hour). Tokens and the current print are stored in
  `/data/push.json`. Texts use the language registered for each token.
- **Printing:** the bridge accepts the job immediately (202) and sends it in the background: wait for the printer,
  upload over FTPS (990, implicit TLS, without TLS shutdown on the data channel), check the size on the card, send
  `project_file` over MQTT with AMS mapping, then wait for the printer to report "Preparing/Printing". Up to three
  attempts; before retrying, the bridge checks whether the printer is already printing the file (no duplicate start).
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

The bridge can be self-hosted without a fixed access key: before the first device is paired, `GET /` shows a
one-time setup code and QR code on the trusted LAN. After that, new devices pair only through a ten-minute invitation
from a paired device. If `BRIDGE_ACCESS_TOKEN` is set, there is no public setup code, either on the page or in the
log; use the fixed key or an invitation to pair. An invitation can also be created on the setup page (`POST /invite`
with the key as a form): only over HTTPS (built-in TLS listener, trusted proxy, or the host of `BRIDGE_PUBLIC_URL`).
Incorrect keys are rate-limited; the page then shows the QR code and code. Device tokens are issued only once and
stored only as SHA-256 digests.

**Per printer:** status, events, controls, AMS, current print, SD card, camera, profiles and Live Activity state are
available under `/api/printers/{id}/…` (id = serial number), for example `/api/printers/{id}/status`. Paths without a
printer component (`/api/status`, etc., in the table below) address the default printer so older app versions keep
working. Jobs are shared; each belongs to a printer (`printer`), uploaded through `/api/printers/{id}/jobs` or
`/api/jobs` (default printer). `POST /api/jobs/{id}/slice` with `{"printer": "<id>"}` slices a model for another printer.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/printers` | printers with a brief status (model, printer profile, connected, print state), `default`, known models |
| POST | `/api/printers/probe` | check `{"host", "access_code"}` without saving → serial number, model, firmware, nozzle, Developer Mode, printer profile, `configured`. Errors include `code`: `unreachable`, `not_bambu`, `access_code`, `timeout` |
| POST | `/api/printers` | set up: `{"host", "access_code", "name"?}` → 201; the first becomes the default |
| GET/PATCH/DELETE | `/api/printers/{id}` | read / change (`name`, `host`, `access_code`, `default: true`; new credentials are checked and must lead to the same printer) / remove. A printer configured through environment variables can only be changed through the environment (409); making it the default is allowed |
| GET/POST | `/api/printers/{id}/jobs` | jobs for this printer / upload a file for it |

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | `{"ok": true}`, no authentication |
| POST | `/api/pair` | `{"token": "<access key>"}` → `{"app_token": …}` (HMAC of the key, `orcaios-app-v1`) |
| POST | `/api/pair` | `{"code": "ABCD-EFGH", "device_name": "…"}` → device token and bridge identity (201) |
| POST | `/api/pairing` | create a single-use, ten-minute invitation while authenticated |
| GET | `/api/devices` | paired app devices and current authentication type |
| DELETE | `/api/devices/{id}` | revoke a device; self-removal is allowed |
| GET | `/api/status` | normalized printer status, including `connection.developer_mode`, `job.error` (`code`, `text`), `hms[].text`, `options` |
| GET | `/api/events` | Server-Sent Events: `status` (at most 2/s), `job` |
| POST | `/api/control` | `{"action": …, "value": …}`, see below |
| POST | `/api/ams/tray` | assign a slot: `{"slot", "filament": "<profile name>", "color": "#RRGGBB"}` (`ams_filament_setting`) |
| GET | `/api/ams/k-values` | printer's K value table for the installed nozzle: `{"nozzle_diameter", "values": [{"cali_idx", "filament_id", "name", "k", "n"}]}` |
| POST | `/api/ams/k-value` | `{"slot", "cali_idx"}`: select a K value for a slot, `-1` = filament profile default; not for the slot currently in use for printing |
| GET | `/api/ams/drying?ams_id=n` | drying suggestion (AMS 2 Pro, AMS HT): `temperature`, `hours`, `max_temperature`, `temperature_range`, `hours_range`, `cooling_temperature`, `filament_type`, `from_profiles` |
| GET | `/api/print/objects` | objects in the current print: `identify_id`, name, outline in mm, skipped |
| GET | `/api/print/thumbnail` | thumbnail of the current print's plate |
| GET/DELETE | `/api/sdcard` | print files (`/`, `/cache`) and timelapses (`/timelapse`) / delete (`?path=`) |
| POST | `/api/sdcard/import` | import `.gcode.3mf` from the card as a job; print from its existing location |
| GET | `/api/sdcard/download` | stream a file (`?path=`, e.g. timelapse AVI), in chunks during the FTPS transfer, without `Content-Length`; a printer-side interruption ends the response without a closing chunk |
| GET | `/api/camera/snapshot` | current JPEG |
| GET | `/api/camera/stream` | MJPEG (`multipart/x-mixed-replace`), each frame includes `X-Frame-Time` (capture time, Unix) |
| GET | `/api/profiles` | qualities, build plates, supports, filaments |
| GET/POST | `/api/jobs` | list jobs / upload a file (multipart, field `file`) |
| GET/DELETE | `/api/jobs/{id}` | read / delete a job |
| POST | `/api/jobs/{id}/slice` | `{"slot", "quality", "plate", "support", "infill", "walls", "brim", "orient", "copies", "plate_index"}`; STL and similar files are oriented (`orient`) and arranged by default, `copies` 1–50 (overflow goes on further plates), `support` `off`/`tree`/`normal` (both "auto": supports only where needed) |
| GET | `/api/jobs/{id}/preview` | slicing preview (OPV1 format, see `bridge/preview.py`), `?plate=n` for a specific plate; the first is precomputed after slicing |
| GET | `/api/print/preview` | preview of the current print |
| POST | `/api/jobs/{id}/print` | `{"mapping": {"<filament>": <slot>}, "plate", "timelapse", "bed_leveling", "flow_cali"}` → 202, sends in the background; `flow_cali` only where `printer.flow_calibration` is not `false` |
| GET | `/api/jobs/{id}/thumbnail` | plate thumbnail (PNG) |
| GET | `/api/live` | Live Activity: configured?, number of iPhones (`devices`), iPhones with notifications (`notifications`) and active displays |
| POST | `/api/ams/spool` | Spool with an NFC tag in a slot: `{slot, uid, action: load\|write, choice: auto\|restore\|new, weight, remaining, type, brand, subtype, color, nozzle_temp_min, nozzle_temp_max}` → `{spool, decision}`; see Filament library |
| GET | `/api/spools` | Filament library: all spools (Bambu and tagged) with remaining amount, share, length, location and statistics |
| PATCH / DELETE | `/api/spools/{id}` | Correct the remaining amount (tagged spools), note, `unload: true`; delete (kept as a tombstone for 180 days) |
| POST | `/api/live/register` | app token: `{"kind": "start"\|"update"\|"alert", "token": "<hex>", "environment": "development"\|"production", "activity_id", "installation", "events", "language"?}`; a new start or device token for the same installation (or device) replaces the old one. `alert` is the device token for notifications; `events` selects from `finished`, `failed`, `paused`, `error`; an empty list unregisters |

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
| `home` | – | not printing | `G28` (homing) |
| `move` | `{"axis": "X"\|"Y"\|"Z", "distance"}` | not printing, after homing, X/Y ≤ 100, Z ≤ 50 mm | relative, software endstops enabled |
| `extrude` | mm (±50) | not printing, after homing, nozzle ≥ 170 °C | `M83` + `G0 E` |
| `load` | slot | not printing, after homing | `print.ams_change_filament` |
| `unload` | – | not printing, after homing | `print.ams_change_filament` (target 255) |
| `skip_objects` | list of `identify_id` | printing, never all objects | `print.skip_objects` |
| `clear_error` | – | printer shows a message (`print_error`) | `print.clean_print_error`, then `system.uiop` (close dialog, optional) |
| `option` | `{"key", "enabled"}` | always; AMS options require an AMS | see below |
| `calibrate` | `{"bed_leveling", "vibration", "motor_noise"}`, each `true`/`false` | not printing; motor noise only where `capabilities.motor_noise_calibration` is not `false` | `print.calibration`, `option` = bitmask (2 bed, 4 vibration, 8 motor noise) |
| `dry` | `{"ams_id", "temperature", "hours", "rotate_tray"?}` | AMS 2 Pro (45–65 °C) or AMS HT (45–85 °C), firmware with remote drying (`fun2` bit 5), not already drying, no blocking condition; at most the loaded filament's heat deflection temperature, 1–24 h | `print.ams_filament_drying`, `mode` 1; filament type and cooling target from the bridge's suggested settings |
| `dry_stop` | `{"ams_id"}` | AMS with a heater | `print.ams_filament_drying`, `mode` 0 |

Calibration: the printer runs it as `auto_cali_for_user.gcode`; status then reports `job.calibration` and
`job.name` "Calibration" ("Kalibrierung" in German), the planned steps as `job.stages` (`stg`), and the current step as
`job.stage_code` (`stg_cur`). Motor noise calibration availability is in `home_flag` bit 21 (`capabilities`).

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
OrcaSlicer and Bambu Studio.

## Architecture

| Module | Responsibility |
|---|---|
| `server.py` | aiohttp app: routes, authentication (HMAC app token), SSE, slicing and print sending as background tasks, resuming after restart |
| `devices.py` | printers: settings in `printers.json`, MQTT, camera, locks and Live Activity per printer; shared profile catalogs per printer profile |
| `probe.py` | check a printer: serial number from its certificate, authentication, version and state (read only) |
| `models.py` | printer models from OrcaSlicer (`resources/printers`), capabilities per firmware |
| `printer.py` | MQTT client (aiomqtt): connection with backoff, regular `pushall`, commands with `sequence_id` and waiting for the response |
| `state.py` | merge partial updates (`merge`, lists by `id`) and normalize for the app (`normalize`): job, AMS, HMS, fans, Developer Mode, options |
| `hms.py` | plain text for `print_error` and HMS per model (English by default; German on request, with English fallback) |
| `controls.py` | control actions → MQTT commands, with the state rules in the table above |
| `camera.py`, `camera_rtsp.py` | model-specific camera facade: JPEG on port 6000 or a shared RTSPS/ffmpeg process; disconnects 20 s after the last viewer |
| `print_command.py`, `print_metadata.py` | legacy/new print payloads and complete filament/nozzle mapping from the sliced 3MF |
| `ftps.py` | implicit FTPS with TLS session reuse; upload without TLS shutdown on the data channel (the P1S does not answer `close_notify`) |
| `profiles.py` | profile catalog: resolve inheritance, adapt vendor profiles, write app options into process profiles |
| `slicer.py` | invoke the OrcaSlicer CLI (arguments, timeout, understandable error texts) |
| `jobs.py` | job storage on the volume (`/data/jobs/<id>/job.json`), 3MF parsing, SD card filenames |
| `live.py`, `apns.py` | Live Activity: printer state → start/update/end, tokens; APNs over HTTP/2 with ES256 JWT |
| `preview.py` | G-code → OPV1 preview format (lines per layer and type, simplified, uint16 in 0.01 mm) |
| `config.py` | environment variables |

### Jobs

States: `uploaded` → `slicing` → `sliced` → `sending` → `sent`, or `failed` on error. A job holds slicing settings
(`settings`), the result per plate (`result.plates[]`: time, weight, filaments, objects, `outside`), the saved print
request (`pending`) and current step (`step`) while sending, then `sent`.
On upload, the bridge checks that the file is complete (`check_complete`): 3MF requires the ZIP central directory at
the end, binary STL requires as many bytes as the triangle count in its header specifies, and text STL requires
`endsolid`. Truncated files (interrupted downloads) are rejected with 422 and an explanation instead of failing
later during slicing.
`remote_path` means the file is already on the SD card and will not be uploaded again. `uploaded` is the name under
which the current slice result is already on the card (further plates only check the size); `sent_plates` records
plates that have already been started. Slicing again resets both.

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
no other way to pass it.

## Configuration

| Variable | Required | Meaning |
|---|---|---|
| `BRIDGE_ACCESS_TOKEN` | | optional shared access key, at least 20 characters; setting it suppresses the public setup code |
| `BRIDGE_NAME` | | bridge display name; without this variable, the name in `/data/bridge.json` is preserved |
| `BRIDGE_TLS` | | `on` (default) or `off`; HTTPS also listens on 8443 by default |
| `BRIDGE_HTTPS_PORT` | | HTTPS port, default 8443; HTTP remains available on `PORT` (default 8080) |
| `BRIDGE_TLS_CERT`, `BRIDGE_TLS_KEY` | | custom certificate and key, only together; otherwise a persistent self-signed P-256 certificate under `/data/tls` |
| `BRIDGE_PUBLIC_URL` | | external HTTPS base URL for QR links, for example behind a reverse proxy |
| `BRIDGE_TRUSTED_PROXIES` | | comma-separated peer CIDRs whose `X-Forwarded-Proto: https` may be used. With `BRIDGE_TLS=off`, the API answers only these networks (every device if unset, with a warning in the log) |
| `BRIDGE_MDNS` | | `on` (default) or `off`; announces `_orcbro._tcp.local.` without codes or tokens, with `BRIDGE_PUBLIC_URL` as `url` in the TXT record. Without multicast to the LAN (Kubernetes, Docker without host networking): a second container running `python -m bridge announce` with host networking and `BRIDGE_PUBLIC_URL` |
| `BAMBU_HOST`, `BAMBU_ACCESS_CODE` | | one printer from the environment (both or neither); further printers are configured in the app |
| `BAMBU_SERIAL` | | its serial number; if unset, the bridge reads it from the printer's certificate |
| `BAMBU_NAME` | | display name (default: model) |
| `BAMBU_MACHINE_PROFILE` | | fixed printer profile for this printer; otherwise follows the model and nozzle |
| `BAMBU_PRINT_URL` | | explicit path for `project_file`; if unset, automatically uses legacy `file:///sdcard/{name}` or new-generation `ftp:///{name}` |
| `BRIDGE_DATA_DIR`, `BRIDGE_KEEP_JOBS`, `BRIDGE_MAX_UPLOAD_MB` | | `/data`, 40 jobs, 300 MB |
| `ORCA_CMD`, `ORCA_PROFILES_DIR`, `ORCA_FILAMENT_IDS`, `ORCA_PRINTERS_DIR`, `ORCA_SLICE_TIMEOUT` | | preset in the image |
| `HMS_TEXTS_DIR` | | plain-text message tables, `/opt/hms` in the image; if absent, only codes are available |
| `APNS_KEY_FILE`, `APNS_KEY_ID`, `APNS_TEAM_ID`, `APNS_BUNDLE_ID` | | Live Activity with your own APNs key for the app (only if you build the app yourself). Default bundle ID `it.blankk.bambro` |
| `BRIDGE_PUSH_RELAY` | | without your own APNs key, Live Activities and notifications use the relay at `https://push.blankk.it` (`relay/`); another HTTPS URL or `off`. Relay ID and key are stored in `/data/relay.json` |

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
  the amount the bridge counted; a new one starts with the tag's amount, else full. An empty slot (or a Bambu spool
  in it) unloads the spool; external spools have no sensor and stay until another tag is read for that slot.

After each print the bridge books the usage: grams per filament from the sliced plate (`slice_info`), mapped to slots
by the bridge's own `ams_mapping` or, for prints from OrcaSlicer/Bambu Studio, by material, color and the time each
slot was active (`tray_now`; AMS backup splits by time). Canceled or failed prints count with their progress. Tagged
spools lose the grams; for Bambu spools only the statistics change (prints, used grams, last used). The print in
progress is stored, so a restart keeps the time per slot.

Deleted spools stay as tombstones for 180 days so a tag scanned again is recognized: other filament data on the tag
means a rewritten tag → new spool; the same data returns `decision: "choose"` with `suggest: "new"` when the old spool
was nearly empty (≤ 20 g or ≤ 5 %, the tag likely moved to a fresh spool) or `"restore"` otherwise, and the app asks.
Writing a tag (`action: "write"`) keeps the counted amount when material and color stay the same, else it starts a new
full spool; the app writes the returned `remaining` to the tag (`remaining_weight`). A Bambu spool that shows up
again after being deleted simply returns.

The status adds `spool` (same fields as `/api/spools`) to every slot with a known spool.

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
  stored in `relay.json` (0600). Then use `Authorization: Bearer <id>.<key>`; 401 triggers a new registration.
- A push token belongs to the first bridge that uses it (KV: SHA-256 of token → ID, seven days, refreshed daily on
  use). Another bridge receives 403 `TokenBoundElsewhere` and discards the token; the app registers it again on its
  next launch.
- `POST /v1/live-activity` accepts Live Activities without `alert`; the relay builds the start alert from
  `content-state.jobName` and `attributes.printerName`. `POST /v1/notification` accepts `{token, environment, template,
  job, printer, detail?, thread?, language?}` (templates `finished`, `failed`, `cancelled`, `paused`, `error`). Both
  endpoints accept optional `"language": "en"|"de"` (default `"en"`, any other value returns 422). The bridge sends
  the token's language so the relay builds the start alert and notification in the same language. The texts match
  in `relay/src/templates.ts` and `bridge/notify.py`; Live Activity content state already comes from the bridge in
  the token's language. Relay error messages are in English.

## Operations

Back up the bridge identity (`/data/bridge.json`), paired devices (`/data/auth.json`) and generated certificate with
the data directory. Corrupt identity or authentication files deliberately prevent startup instead of silently
opening a new initial setup. Files are written atomically and readable only by the bridge process. Revoking a device
also ends that device token's active SSE and camera stream requests and removes its Live Activity tokens
(`push.json`, permissions 0600). At startup, the bridge discards tokens from before this binding was introduced;
the app registers them again. APNs and the relay receive a derived value (`p` + 16 hexadecimal SHA-256 characters)
instead of the serial number.

**Printer certificate.** The bridge remembers each printer's SHA-256 certificate fingerprint (during setup, or on
first contact for older installations, then only with a matching serial number in the certificate) and checks it
during the TLS handshake before sending the access code: MQTT, FTPS (including the resumed data channel) and the JPEG
camera. Before ffmpeg (RTSPS), it checks the handshake on the MQTT port. A new address with a stored code is accepted
only with the same certificate; a new certificate requires entering the access code again. Stored under `pins` in
`printers.json`.

**HTTP and HTTPS.** With built-in TLS, port 8080 serves only the setup page and `/healthz`; API, pairing and
invitation requests return 403 there, except through a trusted proxy with `X-Forwarded-Proto: https`. The access key
field for invitations appears only over actual HTTPS.

The self-signed certificate protects initial pairing only together with the SHA-256 fingerprint in the QR link.
Therefore, expose the public setup page with its code only on a trusted LAN. Behind an HTTPS reverse proxy, set
`BRIDGE_PUBLIC_URL` or explicitly allow its peer networks through `BRIDGE_TRUSTED_PROXIES`; unverified forwarded
headers are ignored.

- **Logs:** `docker compose logs -f orcbro`. Startup logs show printers, the pairing method and setup code.
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

## Open issues

Known issues from the security review on November 21, 2026 (the others have been resolved):

- **Initial setup:** while no device is paired, the first device that reaches the setup page can pair; removing the
  last device opens this again. Intended for a trusted LAN. Idea: show the code only to requests from private
  networks, with activation through the console.
- **Roles:** every paired device can invite and revoke devices and change printers. Idea: roles for viewing,
  controlling and administering.
- **RTSPS camera:** ffmpeg receives the URL including the access code on its command line; anyone who can list host
  processes can see it. In the container, this affects only users with access to the host.

- Copies per object in 3MF projects (`--clone-objects "1,3,1"`), multiple files in one job, advance upload.
