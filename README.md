# Orcbro

Orcbro is a small self-hosted bridge between **Bambu Lab printers in LAN mode** and **Bambro**, an iOS app for
monitoring, controlling and printing. It runs as one container on any always-on machine in your home network
(NAS, mini PC, Raspberry Pi 5) and speaks **Brotocol**, a simple HTTPS/JSON API, to the app.

No Bambu cloud account is involved. Your printers stay on your network.

## What it does

- **Status and control** of one or more printers over MQTT: progress, temperatures, fans, speed, light, pause,
  resume, stop, axes, calibration, print options, skipping objects, acknowledging printer errors with plain-text
  messages.
- **Slicing** STL, OBJ, STEP and 3MF with the OrcaSlicer command line and the official Bambu profiles, including
  preview, multiple plates and supports. The profile follows the printer model and the nozzle that is installed.
- **Printing** with automatic upload (FTPS), AMS mapping, retries and confirmation that the printer actually started.
- **AMS**: slots, colours, remaining filament, humidity, setting filament profiles (including vendor profiles such as
  SUNLU, Polymaker, eSUN), K values, and **drying** on AMS 2 Pro and AMS HT. A new spool without a Bambu chip is set to
  Generic of the slot's previous material in grey instead of keeping the old filament and colour, and Bambro asks you
  to set its filament.
- **Camera**: live MJPEG stream for every model (JPEG stream on P1/A1/A2L, RTSPS via ffmpeg on X1/P2S/H2/X2D).
- **Live Activities** on the iPhone lock screen and Dynamic Island, also for prints started from OrcaSlicer or Bambu
  Studio.
- **Filament library**: Bambu spools (from their chip) and your own spools with NFC tags (OpenSpool); the bridge
  subtracts what each print uses, also for prints from OrcaSlicer or Bambu Studio.
- **Notifications** when a print finishes, fails, pauses or the printer reports an error, in the iPhone's language.
- **SD card**: list, reprint, download timelapses.

## Supported printers

All printers that OrcaSlicer 2.5 knows: X1 Carbon, X1, X1E, P1P, P1S, P2S, A1, A1 mini, A2L, H2S, H2D, H2D Pro, H2C
and X2D. Capabilities (camera type, chamber, temperature limits, dual nozzle, calibrations) come from OrcaSlicer's
printer data, so new models arrive with OrcaSlicer updates.

Tested on real hardware: **P1S**. The other models are implemented from OrcaSlicer, Bambu Studio and community
protocol documentation and covered by tests, but not yet confirmed on the device. Reports are very welcome.

## Requirements

- Printer in **LAN Only mode** with **Developer Mode** enabled (printer settings → Wi-Fi/Network → LAN Only →
  Developer Mode). Without Developer Mode, current firmware rejects print and control commands from any third-party
  software.
- A machine with Docker on the same network as the printer, `linux/amd64` or `linux/arm64`. Idle it needs little; slicing
  large models briefly takes several CPU cores and up to 2–3 GB RAM.

## Quick start

```sh
curl -O https://raw.githubusercontent.com/blankkit/orcbro/main/docker-compose.yml
docker compose up -d
docker compose logs orcbro   # shows the setup code
```

1. Take the **setup code** from the log (`Setup code for the first device: ABCD-EFGH`). Only someone who can read
   the bridge's log gets it; the setup page in the browser does not show it.
2. In Bambro, pick the bridge found on your network (or enter its address) and type the code. Before sending it,
   Bambro shows the bridge's certificate fingerprint; compare it with `Certificate fingerprint: …` in the same log.
   For a QR code instead,
   open **https://\<host\>:8443** in a browser (confirm the warning about the self-signed certificate), enter the
   code there and scan the QR code that appears.
3. Add your printer in the app: IP address and access code, both shown on the printer under Settings → Wi-Fi or
   Network. Orcbro checks them immediately and reads serial number, model, firmware and nozzle from the printer.

The setup code pairs only the first device and is valid for one hour after the bridge starts; `docker compose restart
orcbro` writes a new one to the log. Further iPhones are invited from a paired device (Settings → Bridge → Devices →
Pair Another Device). If you remove the last paired device, pairing stays closed until you restart the bridge. With a
fixed `BRIDGE_ACCESS_TOKEN`, there is no setup code; the setup page (over HTTPS) creates an invitation QR code once you
enter the key.

**Where is the access key?** Orcbro has none by default; pairing then uses the one-time setup code. If you set
`BRIDGE_ACCESS_TOKEN` (20+ characters, for example `openssl rand -base64 30`) in `docker-compose.yml` or a Kubernetes
secret, that value is the access key: type it into the app, or enter it on the setup page to get an invitation QR code.
Keep it in your password manager; the bridge cannot show it again.

## Finding the bridge in the app

Bambro looks for bridges on your network with Bonjour (`_orcbro._tcp`). Bonjour is multicast and only works where
the announcement reaches your Wi-Fi:

- **Docker with host networking** (the provided `docker-compose.yml`): works out of the box. Behind a reverse proxy,
  set `BRIDGE_PUBLIC_URL`; the announcement then carries that HTTPS address.
- **Docker without host networking, Kubernetes, or another isolated network:** the bridge cannot reach your LAN's
  multicast. Run the same image a second time in announce-only mode with host networking. It needs no data and no
  printer access:

  ```yaml
  services:
    orcbro-announce:
      image: ghcr.io/blankkit/orcbro:latest
      command: announce
      network_mode: host
      environment:
        BRIDGE_PUBLIC_URL: https://orcbro.example.com   # HTTPS address of your bridge
        BRIDGE_NAME: Orcbro
      restart: unless-stopped
  ```

  On Kubernetes this needs a pod with `hostNetwork: true` (a namespace that allows it), or let a host on the LAN
  announce the bridge, for example Avahi with a static service file containing `url=https://orcbro.example.com`.
- **No discovery at all:** enter the address in the app, or open the setup page over HTTPS, enter the setup code
  and scan its QR code.

## Security

- The bridge is meant for your **local network**. For remote access use a VPN (Tailscale, WireGuard); do not expose
  it to the internet.
- Each paired device gets its own random token. The bridge stores only a SHA-256 hash; devices can be revoked
  individually, which also closes their open streams. Wrong pairing codes and access keys are slowed down; after ten
  failures within ten minutes, that client address is blocked for ten minutes. Other clients can still pair.
- The setup page shows no pairing codes, no certificate fingerprint and no printers. The first-device setup code is
  only in the bridge log; the page shows its QR code only to someone who enters it over HTTPS.
- HTTPS on port 8443 uses a self-signed certificate created on first start. The QR code contains its SHA-256
  fingerprint, and the app pins it; the startup log shows it too, in the groups of four the app uses. With this built-in HTTPS, the plain HTTP port 8080 serves only the setup page; the
  API needs HTTPS. With `BRIDGE_TLS=off`, the API answers only the reverse proxies in `BRIDGE_TRUSTED_PROXIES`, and
  only for requests they received over HTTPS; without trusted proxies the bridge does not start unless you set
  `BRIDGE_PLAIN_HTTP=on`.
- Orcbro pins each printer's certificate (at setup, or on first contact after an update) and checks it before it
  sends the access code over MQTT, FTPS or the JPEG camera (P1, A1, A2L). If the printer is replaced or reset, add it
  again with its access code. A new printer address with the stored code is accepted only if the same certificate
  answers there. The printer from `BAMBU_HOST` keeps the serial number and certificate it was first enrolled with;
  to switch to another printer, set its `BAMBU_SERIAL` or its new `BAMBU_ACCESS_CODE`.
- RTSPS cameras (X1, P2S, H2, X2D) run through ffmpeg, which cannot check the pin. Right before starting ffmpeg, the
  bridge checks the certificate on the camera port (322) itself and does not start ffmpeg if it differs. ffmpeg's own
  connection is not pinned, and its command line contains the access code.
- The app can only probe printer addresses on the local network (private, unique local and link-local addresses).
- Removing a paired device also removes its Live Activity and notification push tokens. Push registrations belong to
  the device that made them; those made with the fixed access key stop when `BRIDGE_ACCESS_TOKEN` changes.
- Printer access codes are stored in `/data/printers.json`, push tokens in `/data/push.json`; both are readable only
  by the bridge.
- Known limits: every paired device can manage devices and printers; on RTSPS models the camera access code is
  visible in the `ffmpeg` command line to users who can list processes on the host. See the reference for details.

## Configuration

Everything is optional; the defaults are meant for a plain home network.

| Variable | Default | Purpose |
|---|---|---|
| `BAMBU_HOST`, `BAMBU_ACCESS_CODE` | – | Add one printer from the environment (more printers are added in the app) |
| `BAMBU_NAME`, `BAMBU_SERIAL`, `BAMBU_MACHINE_PROFILE` | – | Name, serial (otherwise read from the printer certificate on first start and kept), fixed slicer profile |
| `BRIDGE_ACCESS_TOKEN` | – | Fixed pairing key (20+ characters) instead of setup codes |
| `BRIDGE_NAME` | `Orcbro` | Name shown in the app and on the network |
| `PORT`, `BRIDGE_HTTPS_PORT` | `8080`, `8443` | HTTP and HTTPS ports |
| `BRIDGE_TLS` | `on` | `off` disables the built-in HTTPS (for example behind a reverse proxy) |
| `BRIDGE_TLS_CERT`, `BRIDGE_TLS_KEY` | – | Use your own certificate |
| `BRIDGE_PUBLIC_URL` | – | External HTTPS address for QR codes when running behind a reverse proxy |
| `BRIDGE_TRUSTED_PROXIES` | – | Proxy addresses whose `X-Forwarded-*` headers are trusted; with `BRIDGE_TLS=off` the API answers only them, over HTTPS |
| `BRIDGE_PLAIN_HTTP` | `off` | `on` lets the API answer plain HTTP from every device when `BRIDGE_TLS=off` without trusted proxies |
| `BRIDGE_MDNS` | `on` | Bonjour announcement (`_orcbro._tcp`) so the app finds the bridge |
| `BRIDGE_PUSH_RELAY` | `https://push.blankk.it` | Relay for Live Activities and notifications (see below); another HTTPS URL or `off` |
| `BRIDGE_KEEP_JOBS`, `BRIDGE_MAX_UPLOAD_MB` | `40`, `300` | Jobs kept on disk, upload limit |
| `BRIDGE_RESET_NEW_SPOOLS` | `on` | `off` keeps the firmware's behaviour: a new spool without a Bambu chip gets the slot's previous filament and colour |
| `TZ` | UTC | Time zone for log timestamps, for example `Europe/Berlin` |

### Behind a reverse proxy (optional)

Orcbro does not need a reverse proxy. If you want a trusted certificate or your own domain, put it behind Traefik,
Caddy or nginx and forward to port 8080. Then set:

- `BRIDGE_TLS=off` and `BRIDGE_PUBLIC_URL=https://orcbro.example.com`
- `BRIDGE_TRUSTED_PROXIES` to the proxy's own address or a narrow network, for example `172.18.0.2` on a Docker
  network or the pod network on Kubernetes. Never list your whole LAN: every device in the list can claim HTTPS.

The proxy must send `X-Forwarded-Proto: https` (Traefik and Caddy do; nginx needs `proxy_set_header
X-Forwarded-Proto $scheme;`) and should send `X-Forwarded-For`, so wrong pairing codes are throttled per client
rather than for the proxy as a whole. Keep port 8080 away from other devices: no host networking for this setup,
publish the port only where the proxy can reach it (for example `127.0.0.1:8080:8080`), or restrict it with a firewall
or network policy. The app then uses normal certificate validation instead of pinning.

## Live Activities, notifications and the push relay

Besides the Live Activity, Orcbro sends notifications when a print finishes, fails or is canceled, pauses (for
example when filament runs out), or when the printer reports a serious error (HMS). Each iPhone chooses which of these
it wants in Bambro; the app only registers for notifications when they are allowed in iOS, and unregisters when you
turn them off there. Push texts and Live Activity content use the iPhone's current Bambro app language, English or
German. Settings → Language opens the iOS language choice for Bambro.

Apple delivers pushes only with the app publisher's push key, which a self-hosted bridge does not have. Orcbro
therefore sends them through a small relay (`relay/`, a Cloudflare Worker at `https://push.blankk.it`):

- Each bridge gets its own relay key on first use (`/data/relay.json`). A push token belongs to the bridge that uses
  it first; other bridges are refused. The relay keeps only a hash of the token and the bridge's random ID, and drops
  the binding after seven days without pushes.
- Visible texts come from fixed templates in the relay; a bridge sends only the template and short values (job and
  printer name, error text).
- It accepts only Live Activities and notifications for Bambro, rate-limits per device and bridge, and keeps no logs.
  Job name, progress and error texts pass through it on their way to Apple.

Set `BRIDGE_PUSH_RELAY=off` to disable pushes, or point it to your own deployment of `relay/`. A bridge with its own
APNs key for the app (`APNS_KEY_FILE`) does not use the relay.

## Data and backups

Everything lives in `/data`: printers, paired devices (`auth/`), the certificate (`tls/`), jobs and previews. Back up
the volume to keep pairings and the certificate across reinstalls.

## Brotocol (API)

The app talks to the bridge over HTTPS with JSON, Server-Sent Events for live status and MJPEG for the camera.
Printer-specific routes live under `/api/printers/{serial}/…`. The full reference (behaviour, routes, control actions,
status fields) is in [docs/reference.md](docs/reference.md). Bridge error messages, setup and invitation pages, and
descriptive status texts are in English by default. Send `Accept-Language: de` for German or `Accept-Language: en`
for English; unsupported languages fall back to English. Bambro sends its current app language with each request.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
PYTHONPATH=.:tests .venv/bin/python -m unittest discover -s tests   # no printer needed
(cd relay && npm test)
```

Running locally against a printer needs an OrcaSlicer installation for slicing; see the reference for the variables
(`ORCA_CMD`, `ORCA_PROFILES_DIR`, `ORCA_PRINTERS_DIR`).

## License

Orcbro is MIT-licensed. The container image bundles OrcaSlicer (AGPL-3.0, unmodified) and other components with their
own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Bambu Lab is a trademark of its owner. Orcbro and Bambro are independent projects, not affiliated with or endorsed
by Bambu Lab or OrcaSlicer.
