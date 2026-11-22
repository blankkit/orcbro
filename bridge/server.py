"""Bridge HTTP API for the OrcaIOs app (JSON, Server-Sent Events, MJPEG).

Printer routes live under `/api/printers/<serial>/…` (status, controls, AMS, SD card, camera, profiles).
The same paths without the printer prefix (`/api/status`, etc.) use the default printer for older apps.
Jobs (`/api/jobs`) are shared across printers; each job belongs to one printer (`printer`).

Authentication: the app exchanges the access key (`BRIDGE_ACCESS_TOKEN`) for an app token once
(`POST /api/pair`), then sends `Authorization: Bearer <token>`. The HMAC-derived legacy token survives
restarts and is revoked by changing the key."""
from __future__ import annotations

from .i18n import m, message_of, render, language_middleware

import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
from queue import Full as QueueFull, Queue
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from aiohttp import web

from . import __version__, controls, ftps, preview
from .apns import APNsClient, APNsConfig
from .relay import RelayClient
from .branding import LEGACY_TOKEN_CONTEXT
from .auth import AuthError, InvalidCode, PairingStore, Principal, RateLimited, display_code
from .camera import Camera
from .config import Config
from .devices import Catalogs, Device, Devices, PrinterSettings, PrinterStore
from .hms import Texts
from .identity import BridgeIdentity, load_identity
from .models import DUAL_NOZZLE_MODELS, LEGACY_MODELS, Models
from .probe import ProbeError, probe, read_serial
from .live import ENVIRONMENTS, SHARED_DEVICE, LiveActivities, Registry, public_printer_id
from .spools import Plan, SpoolStore, SpoolTracker
from .notify import EVENTS as NOTIFY_EVENTS
from .mdns import MDNSAdvertiser
from .jobs import (Job, JobError, JobStore, check_complete, classify, open_archive, plate_thumbnail, project_filaments,
                   project_plates, project_printer, read_member, read_objects, read_slice_info, remote_name)
from .printer import Printer, PrinterError
from .print_command import build_project_file
from .print_metadata import read_print_metadata
from .profiles import BED_LABELS, BED_TYPES, SUPPORT_TYPES, Catalog, ProfileError
from .slicer import SliceError, Slicer
from .state import EXTERNAL_SLOT, model_id_from_name
from .tls import TLSState
from .web import PublicOrigin, URLConfigurationError, invitation_page, pairing_url, public_origin, setup_page

log = logging.getLogger(__name__)

IDLE_STATES = controls.IDLE


def app_token(secret: str) -> str:
    return hmac.new(secret.encode(), LEGACY_TOKEN_CONTEXT, hashlib.sha256).hexdigest()


def error(status: int, message: str) -> web.Response:
    return web.json_response(render({"error": message}), status=status, headers={"Cache-Control": "no-store"})


def ok(data, status: int = 200) -> web.Response:
    return web.json_response(render(data), status=status, headers={"Cache-Control": "no-store"})


class EventHub:
    """Distribute status and job changes to all SSE connections."""

    def __init__(self):
        self._queues: set[asyncio.Queue] = set()

    def publish(self, event: str, data: dict) -> None:
        for queue in list(self._queues):
            if queue.qsize() > 50:
                continue  # Slow clients lose intermediate updates, but still receive the next status.
            queue.put_nowait((event, data))

    def listen(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._queues.add(queue)
        return queue

    def close(self, queue: asyncio.Queue) -> None:
        self._queues.discard(queue)


DEVICES = web.AppKey("devices", Devices)
JOBS = web.AppKey("jobs", JobStore)
SLICER = web.AppKey("slicer", Slicer)
CATALOGS = web.AppKey("catalogs", Catalogs)
CONFIG = web.AppKey("config", Config)
EVENTS = web.AppKey("events", EventHub)
TASKS = web.AppKey("tasks", set)
PUSH = web.AppKey("push", Registry)
SPOOLS = web.AppKey("spools", SpoolStore)
APNS = web.AppKey("apns", object)
HMS = web.AppKey("hms", Texts)
PROBE = web.AppKey("probe", object)
AUTH = web.AppKey("auth", PairingStore)
IDENTITY = web.AppKey("identity", BridgeIdentity)
TLS = web.AppKey("tls", TLSState)
PRINCIPAL = web.RequestKey("principal", Principal)


class Problem(web.HTTPException):
    """Raise a JSON error from a helper (`raise Problem(404, "…")`)."""

    def __init__(self, status: int, message: str, **extra):
        self.status_code = status
        super().__init__(text=json.dumps(render({"error": message, **extra})), content_type="application/json",
                         headers={"Cache-Control": "no-store"})


def device(request: web.Request) -> Device:
    """Request printer: the path (`/api/printers/<id>/…`), otherwise the default printer."""
    devices = request.app[DEVICES]
    printer_id = request.match_info.get("printer_id")
    if printer_id is None:
        found = devices.default()
        if found is None:
            raise Problem(409, m('No printer configured yet.'))
        return found
    try:
        return devices.get(printer_id)
    except KeyError:
        raise Problem(404, m('Printer not found.')) from None


def job_device(app: web.Application, job: Job) -> Device:
    """Job printer; older jobs without a printer belong to the default printer."""
    devices = app[DEVICES]
    if job.printer and job.printer in devices.devices:
        return devices.get(job.printer)
    if job.printer:
        raise Problem(409, m('The printer for this job is no longer configured.'))
    found = devices.default()
    if found is None:
        raise Problem(409, m('No printer configured yet.'))
    return found


async def catalog_of(dev: Device) -> Catalog:
    try:
        return await dev.catalog()
    except ProfileError as exc:
        raise Problem(409, message_of(exc)) from None


# --- Authentication ---

PUBLIC = {"/healthz", "/api/pair"}


def _peer(request: web.Request) -> str | None:
    peer = request.transport.get_extra_info("peername") if request.transport else None
    return peer[0] if isinstance(peer, tuple) and peer else None


def _own_tls(request: web.Request) -> bool:
    return request.transport is not None and request.transport.get_extra_info("sslcontext") is not None


def _via_trusted_https(request: web.Request) -> bool:
    """Through a trusted reverse proxy serving HTTPS (`X-Forwarded-Proto: https`)."""
    return (_trusted_proxy(_peer(request), request.app[CONFIG].trusted_proxies)
            and request.headers.get("X-Forwarded-Proto", "").lower() == "https")


@web.middleware
async def transport_middleware(request: web.Request, handler):
    """Restrict HTTP API access to avoid sending tokens and keys in clear text.

    With built-in TLS, HTTP serves only setup and `/healthz`; API and invitations require HTTPS or
    a trusted HTTPS proxy. Without built-in TLS, only `BRIDGE_TRUSTED_PROXIES` may use the API, if set."""
    if _own_tls(request) or request.path in ("/", "/healthz") or not (
            request.path.startswith("/api/") or request.path == "/invite"):
        return await handler(request)
    config = request.app[CONFIG]
    if request.app[TLS].enabled:
        if not _via_trusted_https(request):
            port = config.https_port
            return error(403, m('HTTPS only (port {p0}). Bambro connects this way automatically after pairing.', p0=port))
    elif config.trusted_proxies and not _trusted_proxy(_peer(request), config.trusted_proxies):
        return error(403, m('Only available through the configured reverse proxy (BRIDGE_TRUSTED_PROXIES).'))
    return await handler(request)


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.path in PUBLIC or not request.path.startswith("/api/"):
        return await handler(request)
    header = request.headers.get("Authorization", "")
    token = header[7:] if header.startswith("Bearer ") else ""
    try:
        principal = request.app[AUTH].authenticate(token) if token else None
    except AuthError as exc:
        log.error(m('Could not update credentials: %s'), exc)
        return error(500, m('Credentials could not be saved securely.'))
    if principal is None:
        return error(401, m('Authentication required'))
    request[PRINCIPAL] = principal
    request.app[AUTH].track(principal)
    try:
        return await handler(request)
    finally:
        request.app[AUTH].untrack(principal)


async def healthz(_request: web.Request) -> web.Response:
    return ok({"ok": True})


async def pair(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        return error(400, m('Invalid JSON.'))
    if not isinstance(body, dict):
        return error(400, m('Expected a JSON object.'))
    key = body.get("token")
    secret = request.app[CONFIG].bridge_token
    if key is not None:
        if not secret or not isinstance(key, str) or not _same_secret(key.strip(), secret):
            await asyncio.sleep(1.5)  # Rate-limit attempts through the legacy access-key path.
            return error(401, m('Incorrect access key.'))
        return ok({"app_token": app_token(secret), "bridge_version": __version__})
    code, name = body.get("code"), body.get("device_name")
    if not isinstance(code, str) or not isinstance(name, str):
        return error(422, m('code and device_name are required.'))
    try:
        token, paired = await request.app[AUTH].pair(code, name)
    except ValueError as exc:
        return error(422, message_of(exc))
    except InvalidCode:
        return error(401, m('Setup code is invalid or has expired.'))
    except RateLimited as exc:
        return web.json_response(render({"error": m('Too many incorrect setup codes.')}), status=429,
                                 headers={"Cache-Control": "no-store", "Retry-After": str(exc.retry_after)})
    except AuthError as exc:
        log.error(m('Could not save device: %s'), exc)
        return error(500, m('Device could not be saved securely.'))
    identity = request.app[IDENTITY]
    return ok({
        "app_token": token,
        "device_id": paired.id,
        "bridge": {"id": identity.id, "name": identity.name, "version": __version__,
                   "fingerprint": own_certificate_fingerprint(request)},
    }, status=201)


def _same_secret(given: str, expected: str) -> bool:
    """Constant-time comparison; values that cannot be encoded as UTF-8 simply fail."""
    try:
        return hmac.compare_digest(given.encode(), expected.encode())
    except UnicodeEncodeError:
        return False


def own_certificate_fingerprint(request: web.Request) -> str | None:
    """Offer a pinning fingerprint only on the built-in TLS listener with no public URL configured.
    Behind a reverse proxy, the app sees the proxy certificate; pinning the internal certificate
    would make all subsequent requests fail."""
    if request.app[CONFIG].public_url:
        return None
    own_tls = request.transport is not None and request.transport.get_extra_info("sslcontext") is not None
    return request.app[TLS].fingerprint if own_tls else None


async def create_invitation(request: web.Request) -> web.Response:
    try:
        invitation = await request.app[AUTH].invite(request[PRINCIPAL])
    except ValueError as exc:
        return error(429, message_of(exc))
    return ok({"code": display_code(invitation.code), "expires_at": invitation.expires_at}, status=201)


async def list_client_devices(request: web.Request) -> web.Response:
    principal = request[PRINCIPAL]
    return ok({"devices": request.app[AUTH].list_devices(principal),
               "authentication": principal.authentication})


async def delete_client_device(request: web.Request) -> web.Response:
    try:
        removed = await request.app[AUTH].remove(request.match_info["device_id"])
        if removed:
            # Revoked devices must also stop receiving Live Activity messages.
            request.app[PUSH].remove_device(request.match_info["device_id"])
    except AuthError as exc:
        log.error(m('Could not revoke device: %s'), exc)
        return error(500, m('Device could not be revoked securely.'))
    if not removed:
        return error(404, m('Device not found.'))
    return web.Response(status=204, headers={"Cache-Control": "no-store"})


PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'self'; "
                               "frame-ancestors 'none'",
}


def _page_origin(request: web.Request) -> tuple[PublicOrigin | None, bool]:
    """Public QR-code URL and whether the page arrived over HTTPS (required before asking for a key):
    the built-in TLS listener or a trusted proxy with `X-Forwarded-Proto: https`."""
    peer_address = _peer(request)
    config, tls_state = request.app[CONFIG], request.app[TLS]
    origin = public_origin(
        request_host=request.host,
        peer=peer_address,
        forwarded_proto=request.headers.get("X-Forwarded-Proto"),
        public_url=config.public_url,
        trusted_proxies=config.trusted_proxies,
        tls_enabled=tls_state.enabled,
        https_port=config.https_port,
    )
    # Only actual HTTPS counts: built-in TLS or a trusted HTTPS proxy. The Host header alone proves
    # nothing, so configure BRIDGE_TRUSTED_PROXIES behind a reverse proxy.
    return origin, _own_tls(request) or _via_trusted_https(request)


def _trusted_proxy(peer: str | None, networks: tuple[str, ...]) -> bool:
    try:
        address = ipaddress.ip_address(peer or "")
        return any(address in ipaddress.ip_network(network, strict=False) for network in networks)
    except ValueError:
        return False


async def home(request: web.Request, invite_error: str | None = None, http_status: int = 200) -> web.Response:
    try:
        origin, secure = _page_origin(request)
    except URLConfigurationError as exc:
        return web.Response(text=render(message_of(exc)), status=400, content_type="text/plain")
    config, tls_state = request.app[CONFIG], request.app[TLS]
    printers = []
    for dev in request.app[DEVICES].list():
        status = dev.printer.status()
        printers.append({"name": dev.name, "model": dev.model,
                         "connected": bool(status.get("connection", {}).get("connected"))})
    body = setup_page(
        identity=request.app[IDENTITY],
        printers=printers,
        setup_code=request.app[AUTH].setup_code,
        origin=origin,
        fingerprint=tls_state.fingerprint,
        fixed_key=bool(config.bridge_token),
        can_invite=bool(config.bridge_token and secure and origin),
        invite_error=invite_error,
    )
    return web.Response(text=body, status=http_status, content_type="text/html", headers=PAGE_HEADERS)


async def web_invite(request: web.Request) -> web.Response:
    """Setup-page invitation: the fixed access key grants a QR code for another device.
    Requires HTTPS; incorrect keys are rate-limited as in pairing."""
    config = request.app[CONFIG]
    try:
        origin, secure = _page_origin(request)
    except URLConfigurationError as exc:
        return web.Response(text=render(message_of(exc)), status=400, content_type="text/plain")
    if not config.bridge_token or not origin or not secure:
        return await home(request, m('Invitations using an access key require HTTPS and a fixed access key.'), 400)
    form = await request.post()
    key = form.get("key")
    if not isinstance(key, str) or not _same_secret(key.strip(), config.bridge_token):
        await asyncio.sleep(1.5)  # Rate-limit attempts as in pairing.
        return await home(request, m('Incorrect access key.'), 401)
    try:
        invitation = await request.app[AUTH].invite(Principal("access_key"))
    except ValueError as exc:
        return await home(request, message_of(exc), 429)
    fingerprint = request.app[TLS].fingerprint if origin.include_fingerprint else None
    identity = request.app[IDENTITY]
    body = invitation_page(identity=identity, link=pairing_url(origin, invitation.code, identity.name, fingerprint),
                           code=invitation.code, minutes=max(1, (invitation.expires_at - int(time.time())) // 60))
    return web.Response(text=body, content_type="text/html", headers=PAGE_HEADERS)


# --- Status ---

def described_status(app: web.Application, dev: Device) -> dict:
    """Printer status with readable message texts for the app and Live Activity."""
    dev.refresh_context()
    status = dev.printer.status()
    app[HMS].describe(status)
    return status


def full_status(app: web.Application, dev: Device) -> dict:
    status = described_status(app, dev)
    catalog = dev.catalog_now()
    printer = status["printer"]
    printer.update({"id": dev.id, "name": dev.name, "model": dev.model or printer.get("model"),
                    "model_id": dev.model_id, "machine": dev.machine_profile})
    printer["flow_calibration"] = catalog.flow_calibration(printer.get("model")) if catalog else None
    camera = dev.camera
    status["camera"] = {"active": camera.active, "frame_at": camera.frame_at, "error": camera.error}
    status["bridge"] = {"version": __version__, "slicing": app[SLICER].busy}
    # Filament library: the spool in each slot (Bambu chip or NFC tag) with its remaining amount.
    loaded = app[SPOOLS].loaded(dev.id)
    for unit in status["ams"].get("units") or []:
        for tray in unit.get("trays") or []:
            tray["spool"] = SpoolStore.public(loaded[tray["slot"]]) if tray["slot"] in loaded else None
    for tray in status["ams"].get("externals") or ([status["ams"]["external"]] if status["ams"].get("external") else []):
        if tray:
            tray["spool"] = SpoolStore.public(loaded[tray["slot"]]) if tray["slot"] in loaded else None
    return status


async def _load_catalog(dev: Device) -> None:
    """Load the profile catalog before status, including whether flow dynamics measurement is supported."""
    with contextlib.suppress(ProfileError):
        await dev.catalog()


async def get_status(request: web.Request) -> web.Response:
    dev = device(request)
    await dev.printer.pushall()
    await _load_catalog(dev)
    return ok(full_status(request.app, dev))


async def events(request: web.Request) -> web.StreamResponse:
    """Server-Sent Events: `status` on changes (at most twice per second), `job` for job changes."""
    response = web.StreamResponse(headers={
        "Content-Type": "text/event-stream", "Cache-Control": "no-store", "X-Accel-Buffering": "no"})
    dev = device(request)
    await _load_catalog(dev)
    await response.prepare(request)
    hub = request.app[EVENTS]
    queue = hub.listen()
    try:
        await _send_event(response, "status", full_status(request.app, dev))
        await dev.printer.pushall()
        devices = request.app[DEVICES]
        last_status = time.monotonic()
        pending_status = False
        while True:
            timeout = 15 if not pending_status else max(0.0, 0.5 - (time.monotonic() - last_status))
            try:
                event, data = await asyncio.wait_for(queue.get(), timeout)
            except TimeoutError:
                if pending_status:
                    dev = devices.devices.get(dev.id, dev)
                    await _send_event(response, "status", full_status(request.app, dev))
                    last_status, pending_status = time.monotonic(), False
                else:
                    await response.write(b": ping\n\n")
                continue
            if event == "status":
                if data.get("printer") != dev.id:
                    continue
                if time.monotonic() - last_status >= 0.5:
                    dev = devices.devices.get(dev.id, dev)
                    await _send_event(response, "status", full_status(request.app, dev))
                    last_status, pending_status = time.monotonic(), False
                else:
                    pending_status = True
            else:
                await _send_event(response, event, data)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        hub.close(queue)
    return response


async def _send_event(response: web.StreamResponse, event: str, data: dict) -> None:
    await response.write(f"event: {event}\ndata: {json.dumps(render(data), separators=(',', ':'))}\n\n".encode())


# --- Controls ---

DEVELOPER_MODE_OFF = (m('The printer is not accepting commands: Developer Mode is off (for example, after a firmware update). Enable it on the printer: Settings → WLAN → LAN Only mode → Developer Mode.'))


def _commands_refused(status: dict) -> str | None:
    """Why commands would currently be rejected; None if no rejection is known, including unknown status."""
    if status["connection"].get("developer_mode") is False:
        return DEVELOPER_MODE_OFF
    return None


async def control(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        return error(400, m('Invalid JSON.'))
    if not isinstance(body, dict):
        return error(400, m('Not a JSON object.'))
    dev = device(request)
    printer = dev.printer
    if not printer.connected:
        return error(409, m('Printer not connected.'))
    action, value = body.get("action"), body.get("value")
    status = printer.status()
    if refused := _commands_refused(status):
        return error(409, refused)
    if action == "clear_error":
        return await _clear_error(printer, status)
    objects = None
    if action == "skip_objects":
        try:
            objects = (await current_objects(request.app, dev))["objects"]
        except (JobError, OSError, ValueError) as exc:
            return error(409, m('Cannot read objects: {p0}', p0=exc))
    suggestion = None
    if action == "dry":
        try:
            suggestion = await drying_suggestion(dev, status, value.get("ams_id") if isinstance(value, dict) else None)
        except controls.ControlError as exc:
            return error(exc.status, exc.message)
    try:
        commands = controls.build(action, value, status, objects, drying_suggestion=suggestion)
        for section, command in commands:
            await printer.command(section, command)
    except controls.ControlError as exc:
        return error(exc.status, exc.message)
    except PrinterError as exc:
        return error(409, message_of(exc))
    if action in ("light", "speed", "skip_objects", "load", "unload", "option", "calibrate", "dry", "dry_stop"):
        # The printer reports the new state with its next full push.
        await printer.pushall(force=True)
    return ok({"ok": True})


DRY_FALLBACK = {"temperature": 45, "hours": 4}  # Empty AMS or no filament presets: use conservative defaults.
DRY_COOLING_FALLBACK = 50  # Match OrcaSlicer when no filament preset exists.


async def drying_suggestion(dev: Device, status: dict, ams_id) -> dict:
    """AMS drying suggestion from loaded spool profiles, following OrcaSlicer: the lowest recommended
    temperature, longest duration, lowest heat distortion limit and lowest softening point as the
    cooling target. Use printing presets when a print is running."""
    unit = controls.drying_unit(status, ams_id)
    kind = unit["type"]
    low, high = unit["drying"]["temperature_range"]
    printing = status["job"]["state"] in controls.PRINTING
    presets = []
    try:
        catalog = await dev.catalog()
    except ProfileError:
        catalog = None
    for tray in unit["trays"]:
        if catalog is None or tray["empty"]:
            continue
        try:
            preset = catalog.drying_preset(catalog.filament_for_tray(tray.get("info_idx"), tray.get("material")))
        except ProfileError:
            preset = None
        if preset and kind in preset:
            presets.append(preset)
    mode = "printing" if printing else "idle"
    if presets:
        values = [p[kind][mode] for p in presets]
        lowest = min(presets, key=lambda p: p[kind][mode]["temperature"])
        temperature, hours = min(v["temperature"] for v in values), max(v["hours"] for v in values)
        distortion = [p["heat_distortion_temperature"] for p in presets if p.get("heat_distortion_temperature")]
        softening = [p["softening_temperature"] for p in presets if p.get("softening_temperature")]
        maximum = min([high, *distortion])
        cooling = min(softening) if softening else DRY_COOLING_FALLBACK
        filament_type = lowest.get("type") or ""
    else:
        temperature, hours, maximum = DRY_FALLBACK["temperature"], DRY_FALLBACK["hours"], high
        cooling, filament_type = DRY_COOLING_FALLBACK, ""
    maximum = max(low, int(maximum))
    first, last = unit["drying"]["hours_range"]
    return {
        "ams_id": unit["id"], "type": kind, "printing": printing,
        "temperature": int(min(max(temperature, low), maximum)), "hours": int(min(max(hours, first), last)),
        "temperature_range": [low, high], "max_temperature": maximum, "hours_range": [first, last],
        "cooling_temperature": int(cooling), "filament_type": filament_type,
        "from_profiles": bool(presets),
    }


async def ams_drying(request: web.Request) -> web.Response:
    """Drying suggestion: `?ams_id=n` → temperature, duration, limits and cooling target."""
    dev = device(request)
    raw = request.query.get("ams_id", "")
    try:
        return ok(await drying_suggestion(dev, dev.printer.status(), int(raw) if raw.lstrip("-").isdigit() else None))
    except controls.ControlError as exc:
        return error(exc.status, exc.message)


async def _clear_error(printer: Printer, status: dict) -> web.Response:
    """Acknowledge the display message, like OK in Bambu Studio's error dialog."""
    try:
        quit_error, close_dialog = controls.clear_error(status)
        await printer.command(*quit_error)
    except controls.ControlError as exc:
        return error(exc.status, exc.message)
    except PrinterError as exc:
        return error(409, message_of(exc))
    try:
        # Close the display dialog; unsupported firmware may ignore this command.
        await printer.command(*close_dialog, wait=3)
    except PrinterError as exc:
        log.info(m('Dismiss error dialog: %s'), exc)
    await printer.pushall(force=True)
    return ok({"ok": True})


async def set_tray(request: web.Request) -> web.Response:
    """Set AMS slot: {"slot": n, "filament": "<profile name>", "color": "#RRGGBB"}."""
    try:
        body = await request.json()
    except ValueError:
        return error(400, m('Invalid JSON.'))
    dev = device(request)
    printer = dev.printer
    if not printer.connected:
        return error(409, m('Printer not connected.'))
    slot, name, color = body.get("slot"), body.get("filament"), body.get("color")
    status = printer.status()
    if refused := _commands_refused(status):
        return error(409, refused)
    tray = controls.find_tray(status, slot)
    if tray is None:
        return error(422, m('Slot not found.'))
    if status["job"]["active"] and status["ams"].get("active_slot") == slot:
        return error(409, m('This slot is currently being used for printing.'))
    try:
        info = (await catalog_of(dev)).filament_info(str(name))
        section, command = controls.tray_setting(slot, info, str(color or ""), tray.get("source"))
        await printer.command(section, command)
    except ProfileError as exc:
        return error(422, message_of(exc))
    except controls.ControlError as exc:
        return error(exc.status, exc.message)
    except PrinterError as exc:
        return error(409, message_of(exc))
    await printer.pushall(force=True)
    return ok({"ok": True})


async def k_values(request: web.Request) -> web.Response:
    """Printer K value table: flow dynamics per filament for the installed nozzle."""
    printer = device(request).printer
    if not printer.connected:
        return error(409, m('Printer not connected.'))
    status = printer.status()
    if refused := _commands_refused(status):
        return error(409, refused)
    try:
        section, command = controls.k_values_query(status["nozzle"].get("diameter"))
        reply = await printer.command(section, command, wait=10)
    except controls.ControlError as exc:
        return error(exc.status, exc.message)
    except PrinterError as exc:
        return error(409, message_of(exc))
    if reply is None:
        return error(504, m('The printer did not send its K values.'))
    return ok({"nozzle_diameter": command["nozzle_diameter"], "values": controls.k_values(reply)})


async def select_k_value(request: web.Request) -> web.Response:
    """Select a slot K value: {"slot": n, "cali_idx": i}; -1 uses the filament profile default."""
    try:
        body = await request.json()
    except ValueError:
        return error(400, m('Invalid JSON.'))
    if not isinstance(body, dict):
        return error(400, m('Not a JSON object.'))
    printer = device(request).printer
    if not printer.connected:
        return error(409, m('Printer not connected.'))
    slot = body.get("slot")
    status = printer.status()
    if refused := _commands_refused(status):
        return error(409, refused)
    tray = controls.find_tray(status, slot)
    if tray is None:
        return error(422, m('Slot not found.'))
    if status["job"]["active"] and status["ams"].get("active_slot") == slot:
        return error(409, m('This slot is currently being used for printing.'))
    try:
        section, command = controls.k_select(slot, tray, body.get("cali_idx"), status["nozzle"].get("diameter"))
        await printer.command(section, command)
    except controls.ControlError as exc:
        return error(exc.status, exc.message)
    except PrinterError as exc:
        return error(409, message_of(exc))
    await printer.pushall(force=True)
    return ok({"ok": True})


# --- Current print: objects ---

async def current_print_file(app: web.Application, dev: Device) -> tuple[Path, int]:
    """Current print's 3MF and plate. Bridge prints use the local file; other prints download
    `<job>.gcode.3mf` from the SD card, where OrcaSlicer and Bambu Studio store it."""
    status = dev.printer.status()
    job = status["job"]
    if not job["active"] or not job["name"]:
        raise JobError(m('No print is running.'))
    plate = job.get("plate_index") or 1
    for candidate in app[JOBS].list():
        if (candidate.sent and candidate.name == job["name"] and candidate.printer in (None, dev.id)
                and app[JOBS].output_path(candidate).exists()):
            return app[JOBS].output_path(candidate), candidate.sent.get("plate") or plate
    config = app[CONFIG]
    remote = f"{job['name']}.gcode.3mf"
    # Thumbnail, preview and object requests often overlap: use one printer session and one download.
    async with dev.print_file_lock:
        listing = await asyncio.to_thread(ftps.list_dir, dev.host, dev.access_code, "/", context=dev.trust.context())
        entry = next((e for e in listing if e["name"] == remote and not e["is_dir"]), None)
        if entry is None:
            raise JobError(m('“{p0}” is not on the SD card.', p0=remote))
        cache = config.data_dir / "printcache"
        cache.mkdir(parents=True, exist_ok=True)
        local = cache / f"{hashlib.sha1(f'{dev.id}/{remote}'.encode()).hexdigest()[:16]}-{entry['size']}.gcode.3mf"
        if not local.exists():
            try:
                await asyncio.to_thread(ftps.download, dev.host, dev.access_code, f"/{remote}", local, context=dev.trust.context())
            except (OSError, EOFError):
                # At print start, busy printers often drop FTP connections; retry once.
                await asyncio.sleep(2)
                await asyncio.to_thread(ftps.download, dev.host, dev.access_code, f"/{remote}", local, context=dev.trust.context())
            for old in sorted(cache.glob("*.gcode.3mf"), key=lambda f: f.stat().st_mtime)[:-3 * len(app[DEVICES].devices)]:
                old.unlink(missing_ok=True)
    return local, plate


async def current_objects(app: web.Application, dev: Device) -> dict:
    path, plate = await current_print_file(app, dev)
    objects = await asyncio.to_thread(read_objects, path, plate)
    skipped = set(dev.printer.status()["job"].get("skipped_objects") or [])
    for obj in objects:
        obj["skipped"] = obj["skipped"] or obj["id"] in skipped
    return {"plate": plate, "bed": (await catalog_of(dev)).bed_size(), "objects": objects}


async def print_thumbnail(request: web.Request) -> web.Response:
    """Current plate thumbnail, including prints from OrcaSlicer or Bambu Studio."""
    try:
        path, plate = await current_print_file(request.app, device(request))
        data = await asyncio.to_thread(_read_thumbnail, path, plate)
    except (JobError, OSError, ValueError, KeyError) as exc:
        return error(404, m('No thumbnail: {p0}', p0=exc))
    return web.Response(body=data, content_type="image/png", headers={"Cache-Control": "max-age=600"})


def _read_member(path: Path, member: str) -> bytes:
    with open_archive(path) as archive:
        return read_member(archive, member)


def _read_thumbnail(path: Path, plate: int) -> bytes:
    with open_archive(path) as archive:
        member = plate_thumbnail(archive.namelist(), plate)
        if member is None:
            raise JobError(m('No thumbnail for plate {p0} in the file.', p0=plate))
        return read_member(archive, member)


async def print_objects(request: web.Request) -> web.Response:
    try:
        return ok(await current_objects(request.app, device(request)))
    except (JobError, OSError, ValueError) as exc:
        return error(409, message_of(exc))


# --- SD card ---

SD_DIRS = {"/": "print", "/cache": "print", "/timelapse": "timelapse"}
VIDEO_SUFFIXES = (".avi", ".mp4", ".mov")


def sd_path(raw) -> tuple[str, str]:
    """Validate an SD card path: known directories only, no `..`. Return (directory, name)."""
    if not isinstance(raw, str) or ".." in raw or "\\" in raw:
        raise JobError(m('Invalid path.'))
    directory, _, name = ("/" + raw.lstrip("/")).rpartition("/")
    directory = directory or "/"
    if directory not in SD_DIRS or not name:
        raise JobError(m('Invalid path.'))
    return directory, name


def iso_modified(value: str | None, now: datetime | None = None) -> str | None:
    """LIST date (`Nov 20 14:48` without a year, or `2023-08-31`) → ISO 8601."""
    if not value:
        return None
    now = now or datetime.now()
    try:
        if ":" in value:
            parsed = datetime.strptime(f"{value} {now.year}", "%b %d %H:%M %Y")
            if parsed > now + timedelta(days=1):
                parsed = parsed.replace(year=now.year - 1)
            return parsed.isoformat(timespec="minutes")
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


async def sdcard(request: web.Request) -> web.Response:
    dev = device(request)
    files = []
    try:
        for directory, kind in SD_DIRS.items():
            entries = await asyncio.to_thread(ftps.list_dir, dev.host, dev.access_code, directory, context=dev.trust.context())
            for entry in entries:
                name = entry["name"]
                # Skip directories and macOS helper files (`._Name`) on cards written by a Mac.
                if entry["is_dir"] or name.startswith("._"):
                    continue
                if kind == "print" and not name.lower().endswith(".gcode.3mf"):
                    continue
                if kind == "timelapse" and not name.lower().endswith(VIDEO_SUFFIXES):
                    continue
                path = f"{directory.rstrip('/')}/{name}"
                files.append({"path": path, "name": name, "size": entry["size"], "kind": kind,
                              "modified": iso_modified(entry["modified"])})
    except (OSError, EOFError) as exc:
        return error(502, m('Cannot read SD card: {p0}', p0=exc))
    files.sort(key=lambda f: f["modified"] or "", reverse=True)
    return ok({"files": files})


async def sdcard_delete(request: web.Request) -> web.Response:
    try:
        directory, name = sd_path(request.query.get("path"))
    except JobError as exc:
        return error(422, message_of(exc))
    dev = device(request)
    job = dev.printer.status()["job"]
    if job["active"] and job["name"] and name.startswith(job["name"]):
        return error(409, m('This file is currently being printed.'))
    try:
        await asyncio.to_thread(ftps.delete_path, dev.host, dev.access_code, f"{directory.rstrip('/')}/{name}", context=dev.trust.context())
    except Exception as exc:  # noqa: BLE001
        return error(502, m('Could not delete: {p0}', p0=exc))
    return ok({"ok": True})


async def sdcard_import(request: web.Request) -> web.Response:
    """Import a sliced SD card 3MF as a job; print the file in place."""
    try:
        body = await request.json()
        directory, name = sd_path(body.get("path") if isinstance(body, dict) else None)
    except (ValueError, JobError) as exc:
        return error(422, message_of(exc) or m('Invalid JSON.'))
    if not name.lower().endswith(".gcode.3mf"):
        return error(422, m('Only sliced 3MF files (.gcode.3mf).'))
    store, dev = request.app[JOBS], device(request)
    job = store.create(name, dev.id)
    remote = f"{directory.rstrip('/')}/{name}"
    try:
        await asyncio.to_thread(ftps.download, dev.host, dev.access_code, remote, store.input_path(job), context=dev.trust.context())
        job.result = {**await asyncio.to_thread(read_slice_info, store.input_path(job)), "warnings": []}
        job.result["plate"] = job.result["plates"][0]["index"]
    except Exception as exc:  # noqa: BLE001
        store.delete(job.id)
        return error(502, m('Could not import: {p0}', p0=exc))
    job.state, job.remote_path = "sliced", remote.lstrip("/")
    _publish_job(request.app, job)
    store.prune()
    return ok(job.public(), status=201)


class _TransferStopped(Exception):
    """The app canceled the download; stop the FTPS thread."""


_DOWNLOADS: set[asyncio.Future] = set()
DOWNLOAD_TYPES = {".avi": "video/x-msvideo", ".mp4": "video/mp4", ".mov": "video/quicktime"}


async def sdcard_download(request: web.Request) -> web.StreamResponse:
    """Stream an SD card file, such as a timelapse, block by block as it arrives from the printer.

    Previously the whole file was downloaded first, leaving the app at 0% for a long time. The app
    knows the size from the card listing, so use chunked transfer without Content-Length. If the
    printer disconnects mid-file, end without a final chunk so the app detects the failure."""
    try:
        directory, name = sd_path(request.query.get("path"))
    except JobError as exc:
        return error(422, message_of(exc))
    dev = device(request)
    remote = f"{directory.rstrip('/')}/{name}"
    blocks: Queue = Queue(maxsize=32)  # Backpressure: a slow app throttles the FTPS thread.
    stop = threading.Event()
    done = object()

    def put(item) -> None:
        while not stop.is_set():
            try:
                blocks.put(item, timeout=0.5)
                return
            except QueueFull:
                continue
        raise _TransferStopped

    def run() -> None:
        try:
            ftps.retrieve(dev.host, dev.access_code, remote, put, context=dev.trust.context())
            result = done
        except _TransferStopped:
            return
        except Exception as exc:  # noqa: BLE001 - FTPS, TLS, certificate errors
            result = exc
        with contextlib.suppress(_TransferStopped):
            put(result)

    worker = asyncio.ensure_future(asyncio.to_thread(run))
    _DOWNLOADS.add(worker)
    worker.add_done_callback(_DOWNLOADS.discard)
    try:
        item = await asyncio.to_thread(blocks.get)
        if isinstance(item, Exception):
            return error(502, m('Download failed: {p0}', p0=_describe_transfer(item)))
        response = web.StreamResponse(headers={
            "Content-Type": DOWNLOAD_TYPES.get(Path(name).suffix.lower(), "application/octet-stream"),
            "Content-Disposition": f"attachment; filename=\"{Path(name).name}\"",
            "Cache-Control": "no-store"})
        await response.prepare(request)
        while item is not done:
            if isinstance(item, Exception):
                # Omit the final chunk so the app detects interruption and does not save a partial file.
                log.warning(m('Download from %s interrupted: %s'), remote, _describe_transfer(item))
                if request.transport:
                    request.transport.abort()
                return response
            try:
                await response.write(item)
            except ConnectionError:
                return response  # The app canceled.
            item = await asyncio.to_thread(blocks.get)
        await response.write_eof()
        return response
    finally:
        stop.set()  # App disconnected or finished: the thread stops within half a second.


def _describe_transfer(exc: Exception) -> str:
    from .trust import CHANGED, CertificateChanged
    return CHANGED if isinstance(exc, CertificateChanged) else message_of(exc) or type(exc).__name__


# --- Camera ---

async def camera_snapshot(request: web.Request) -> web.Response:
    dev = device(request)
    dev.refresh_context()
    camera = dev.camera
    try:
        frame = await camera.snapshot()
    except (TimeoutError, RuntimeError):
        return error(504, camera.error or m('No camera image.'))
    return web.Response(body=frame, content_type="image/jpeg", headers={"Cache-Control": "no-store"})


async def camera_stream(request: web.Request) -> web.StreamResponse:
    boundary = "frame"
    dev = device(request)
    dev.refresh_context()
    camera = dev.camera
    if not getattr(camera, "available", True):
        return error(504, camera.error or m('No camera.'))
    response = web.StreamResponse(headers={
        "Content-Type": f"multipart/x-mixed-replace; boundary={boundary}", "Cache-Control": "no-store",
        "X-Accel-Buffering": "no"})
    await response.prepare(request)
    try:
        # aclosing releases the viewer immediately on disconnect, without waiting for garbage collection.
        async with contextlib.aclosing(camera.frames()) as frames:
            async for frame in frames:
                # Capture time per frame: the first frame may be an older cached image.
                taken = getattr(camera, "frame_at", None) or time.time()
                await response.write(
                    f"--{boundary}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(frame)}\r\n"
                    f"X-Frame-Time: {taken:.3f}\r\n\r\n".encode() + frame + b"\r\n")
    except (ConnectionResetError, asyncio.CancelledError, TimeoutError):
        pass
    return response


# --- Profiles ---

async def profiles(request: web.Request) -> web.Response:
    catalog = await catalog_of(device(request))
    return ok({
        "machine": catalog.machine_name,
        "bed": catalog.bed_size(),
        "qualities": catalog.quality_choices(),
        "default_quality": catalog.default_process(),
        "plates": [{"id": k, "label": BED_LABELS[k]} for k in BED_TYPES],
        "default_plate": "textured_pei",
        "supports": list(SUPPORT_TYPES),
        "filaments": catalog.filament_choices(),
    })


# --- Jobs ---

def _job_or_404(request: web.Request) -> Job:
    try:
        return request.app[JOBS].get(request.match_info["job_id"])
    except (KeyError, JobError):
        raise web.HTTPNotFound(text=json.dumps(render({"error": m('Job not found.')})), content_type="application/json")


def _publish_job(app: web.Application, job: Job) -> None:
    app[JOBS].save(job)
    app[EVENTS].publish("job", job.public())


async def list_jobs(request: web.Request) -> web.Response:
    """All jobs; `/api/printers/<id>/jobs` includes only that printer's jobs."""
    jobs = request.app[JOBS].list()
    if "printer_id" in request.match_info:
        dev = device(request)
        default = request.app[DEVICES].default_id
        jobs = [j for j in jobs if j.printer == dev.id or (j.printer is None and dev.id == default)]
    return ok({"jobs": [j.public() for j in jobs]})


async def get_job(request: web.Request) -> web.Response:
    return ok(_job_or_404(request).public())


async def delete_job(request: web.Request) -> web.Response:
    job = _job_or_404(request)
    if job.state in ("slicing", "sending"):
        return error(409, m('Job is in progress.'))
    request.app[JOBS].delete(job.id)
    return ok({"ok": True})


async def upload_job(request: web.Request) -> web.Response:
    """Multipart upload (`file` field). Sliced 3MF is ready to print; everything else needs slicing.

    Assign the job to the printer in the path (`/api/printers/<id>/jobs`), otherwise the default printer."""
    store = request.app[JOBS]
    dev = device(request)
    limit = request.app[CONFIG].max_upload_mb * 1024 * 1024
    try:
        reader = await request.multipart()
    except (AssertionError, ValueError):
        return error(400, m('Expected a multipart upload.'))
    field = await reader.next()
    while field is not None and field.name != "file":
        field = await reader.next()
    if field is None or not field.filename:
        return error(400, m('The “file” field is missing.'))
    try:
        classify(field.filename)
        job = store.create(Path(field.filename).name, dev.id)
    except JobError as exc:
        return error(422, message_of(exc))
    path = store.input_path(job)
    size = 0
    try:
        with path.open("wb") as handle:
            while chunk := await field.read_chunk(256 * 1024):
                size += len(chunk)
                if size > limit:
                    raise JobError(m('File is larger than {p0} MB.', p0=request.app[CONFIG].max_upload_mb))
                handle.write(chunk)
        if size == 0:
            raise JobError(m('Empty file.'))
        # Unpack and scan in a thread so large files cannot block the API.
        await asyncio.to_thread(check_complete, path)
        if job.kind == "sliced":
            job.result = {**await asyncio.to_thread(read_slice_info, path), "plate": None, "warnings": []}
            job.result["plate"] = job.result["plates"][0]["index"]
            job.state = "sliced"
        elif job.kind == "project":
            printer = await asyncio.to_thread(project_printer, path)
            try:
                catalog = await dev.catalog()
            except ProfileError as exc:
                raise JobError(message_of(exc)) from None
            if _project_fits(catalog, printer):
                job.settings = {"project_filaments": await asyncio.to_thread(project_filaments, path),
                                "project_plates": await asyncio.to_thread(project_plates, path)}
            else:
                # No settings for this printer (PrusaSlicer 3MF, model download, Bambu project for A1/X1, etc.):
                # retain only geometry and slice with our profiles as a model. Keep the file's orientation, which
                # the designer likely chose deliberately; arrange objects again for this bed.
                job.kind = "model"
                job.settings = {"orient": False, "source_printer": (printer or {}).get("model")}
    except (JobError, OSError, ValueError) as exc:
        store.delete(job.id)
        return error(422, message_of(exc))
    except Exception as exc:  # noqa: BLE001 - damaged ZIP/XML files
        store.delete(job.id)
        return error(422, m('Cannot read file: {p0}', p0=exc))
    _publish_job(request.app, job)
    store.prune()
    return ok(job.public(), status=201)


def _project_fits(catalog: Catalog, printer: dict | None) -> bool:
    """Does the 3MF project target exactly this printer and nozzle? Only then retain its settings."""
    if not printer:
        return False
    machine = catalog.resolve("machine", catalog.machine_name)
    nozzle = machine.get("nozzle_diameter")
    nozzle = float(nozzle[0] if isinstance(nozzle, list) else str(nozzle).split(";")[0]) if nozzle else None
    return printer.get("model") == machine.get("printer_model") and (
        printer.get("nozzle") is None or nozzle is None or abs(printer["nozzle"] - nozzle) < 0.01)


# OrcaSlicer puts excess copies on additional plates; the app prints them one at a time.
MAX_COPIES = 50


async def slice_job(request: web.Request) -> web.Response:
    """Start slicing. Body: {slot | filament, quality, plate, support, infill, walls, brim, orient, plate_index}."""
    job = _job_or_404(request)
    if job.kind == "sliced":
        return error(409, m('The file is already sliced.'))
    if job.state in ("slicing", "sending"):
        return error(409, m('Job is in progress.'))
    if request.app[SLICER].busy:
        return error(409, m('The slicer is busy.'))
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        return error(400, m('Not a JSON object.'))
    target = body.get("printer")
    if target is not None and target != job.printer:
        # Reslicing for another printer is limited to models; projects target their original printer.
        if target not in request.app[DEVICES].devices:
            return error(422, m('Printer not found.'))
        if job.kind != "model":
            return error(409, m('Projects cannot be sliced for a different printer.'))
        job.printer = target
    dev = job_device(request.app, job)
    catalog = await catalog_of(dev)
    filaments: list[str] = []
    try:
        if job.kind == "model":
            filament = body.get("filament")
            slot = body.get("slot")
            tray = _find_tray(dev.printer.status(), slot)
            if filament is None:
                if tray is None:
                    return error(422, m('Slot not found or empty.'))
                filament = catalog.filament_for_tray(tray.get("info_idx"), tray.get("material"))
            filaments = [filament]
            colors = [tray.get("color") if tray else None]
            options = {k: body[k] for k in ("plate", "support", "infill", "walls", "brim") if k in body}
            options.setdefault("plate", "textured_pei")
            # Validate options and profiles before starting so errors return 422 immediately.
            catalog.write_settings(request.app[JOBS].dir(job.id) / "work" / "check", process=body.get("quality"),
                                   filaments=filaments, options=options, colors=colors)
        else:
            options, colors = {}, []
    except ProfileError as exc:
        return error(422, message_of(exc))
    try:
        plate = int(body.get("plate_index") or 1)
        copies = int(body.get("copies") or 1)
    except (TypeError, ValueError):
        return error(422, m('Plate and copy count must be numbers.'))
    if not 1 <= copies <= MAX_COPIES:
        return error(422, m('Copies: 1 to {p0}.', p0=MAX_COPIES))
    if copies > 1 and job.filename.lower().endswith(".3mf"):
        # OrcaSlicer cannot clone 3MF objects (--clone-objects only supports STL, OBJ and STEP).
        return error(422, m('Multiple copies are not supported for 3MF files yet. Use an STL file.'))
    job.settings = {**job.settings, "machine": catalog.machine_name,
                    "quality": body.get("quality") or catalog.default_process(),
                    "filaments": filaments, "colors": colors, "options": options, "slot": body.get("slot"),
                    "plate_index": plate, "copies": copies,
                    # Orient STL and similar files by default, as OrcaSlicer's Auto Orient does;
                    # retain 3MF model orientation unless the app explicitly overrides it.
                    "orient": bool(body.get("orient", job.settings.get("orient", job.kind == "model")))}
    job.state, job.error, job.result = "slicing", None, None
    job.sent_plates, job.uploaded = [], None
    _publish_job(request.app, job)
    task = asyncio.create_task(_run_slice(request.app, job))
    request.app[TASKS].add(task)
    task.add_done_callback(request.app[TASKS].discard)
    return ok(job.public(), status=202)


def _find_tray(status: dict, slot) -> dict | None:
    if not isinstance(slot, int):
        return None
    ams = status["ams"]
    trays = [t for unit in ams["units"] for t in unit["trays"]]
    trays.extend(ams.get("externals") or ([ams["external"]] if ams.get("external") else []))
    tray = next((t for t in trays if t["slot"] == slot), None)
    return tray if tray and not tray["empty"] else None


async def _run_slice(app: web.Application, job: Job) -> None:
    store = app[JOBS]
    try:
        catalog = await app[CATALOGS].get(job.settings["machine"])
        result = await app[SLICER].slice(
            catalog=catalog, workdir=store.dir(job.id) / "work", model=store.input_path(job), output=store.output_path(job),
            kind=job.kind, plate=job.settings.get("plate_index", 1), process=job.settings.get("quality"),
            filaments=job.settings.get("filaments"), options=job.settings.get("options"),
            colors=job.settings.get("colors"), orient=job.settings.get("orient", False),
            copies=job.settings.get("copies", 1))
        job.result, job.state = result, "sliced"
        for cache in store.dir(job.id).glob("preview*.bin"):
            cache.unlink(missing_ok=True)
    except (SliceError, ProfileError, JobError, OSError) as exc:
        job.state, job.error = "failed", message_of(exc)
    except Exception as exc:  # noqa: BLE001
        log.exception(m('Slicing failed'))
        job.state, job.error = "failed", m('Slicing failed: {p0}', p0=exc)
    _publish_job(app, job)
    if job.state == "sliced":
        try:
            await job_preview_data(app, job)  # Precompute so the app can retrieve it immediately.
        except Exception:  # noqa: BLE001 - preview is optional and separate from slicing
            log.exception(m('Preview failed for %s'), job.id)


PREVIEW_LOCKS = web.AppKey("preview_locks", dict)


async def job_bed(app: web.Application, job: Job) -> tuple[float, float]:
    """Preview bed size: the profile used for slicing, otherwise the printer's profile."""
    machine = job.settings.get("machine")
    if machine:
        try:
            return tuple((await app[CATALOGS].get(machine)).bed_size())
        except ProfileError:
            pass
    return tuple((await catalog_of(job_device(app, job))).bed_size())


async def job_preview_data(app: web.Application, job: Job, plate: int | None = None) -> bytes:
    """Sliced job plate preview (OPV1); calculated once, then cached as `preview-<plate>.bin`."""
    if not job.result:
        raise JobError(m('Not sliced yet.'))
    plates = [p["index"] for p in job.result["plates"]]
    plate = plate or job.result.get("plate") or plates[0]
    if plate not in plates:
        raise JobError(m('Plate {p0} does not exist.', p0=plate))
    cache = app[JOBS].dir(job.id) / f"preview-{plate}.bin"
    lock = app[PREVIEW_LOCKS].setdefault(job.id, asyncio.Lock())
    async with lock:
        if cache.exists():
            return cache.read_bytes()
        bed = await job_bed(app, job)
        data = await asyncio.to_thread(preview.build_from_3mf, app[JOBS].output_path(job), plate, bed)
        cache.write_bytes(data)
        return data


def _binary(data: bytes) -> web.Response:
    response = web.Response(body=data, content_type="application/octet-stream", headers={"Cache-Control": "no-store"})
    response.enable_compression()
    return response


async def job_preview(request: web.Request) -> web.Response:
    """Job preview; `?plate=n` selects a specific plate."""
    job = _job_or_404(request)
    if job.state == "slicing":
        return error(409, m('Slicing in progress.'))
    plate = request.query.get("plate")
    try:
        return _binary(await job_preview_data(request.app, job, int(plate) if plate and plate.isdigit() else None))
    except (JobError, OSError, KeyError, ValueError) as exc:
        return error(409, m('No preview: {p0}', p0=exc))


async def print_preview(request: web.Request) -> web.Response:
    """Current print preview, including OrcaSlicer/Bambu Studio prints; the app marks the current layer."""
    dev = device(request)
    try:
        path, plate = await current_print_file(request.app, dev)
        cache = path.with_name(f"{path.stem}-plate{plate}.opv")
        if not cache.exists():
            bed = tuple((await catalog_of(dev)).bed_size())
            data = await asyncio.to_thread(preview.build_from_3mf, path, plate, bed)
            cache.write_bytes(data)
        return _binary(cache.read_bytes())
    except (JobError, OSError, KeyError, ValueError) as exc:
        return error(409, m('No preview: {p0}', p0=exc))


async def job_thumbnail(request: web.Request) -> web.Response:
    job = _job_or_404(request)
    plate = request.query.get("plate")
    data = request.app[JOBS].thumbnail(job, int(plate) if plate and plate.isdigit() else None)
    if data is None:
        return error(404, m('No thumbnail.'))
    return web.Response(body=data, content_type="image/png", headers={"Cache-Control": "max-age=3600"})


def build_mapping(plate: dict, chosen: dict[int, int], status: dict) -> tuple[list[int], bool]:
    """AMS print-command mapping: index = project filament (zero-based), value = global slot.

    Unused filaments get -1. The external spool (254) must be used alone and disables the AMS."""
    used = [f["id"] for f in plate["filaments"]]
    if not used:
        raise ProfileError(m('The plate uses no filament.'))
    missing = [i for i in used if i not in chosen]
    if missing:
        raise ProfileError(m('No slot selected for filament {p0}.', p0=', '.join(map(str, missing))))
    slots = [chosen[i] for i in used]
    if EXTERNAL_SLOT in slots:
        if len(used) > 1:
            raise ProfileError(m('The external spool can only be used for single-color prints.'))
        return [], False
    for slot in slots:
        if _find_tray(status, slot) is None:
            raise ProfileError(m('Slot {p0} is empty or missing.', p0=slot_label(slot)))
    mapping = [-1] * max(used)
    for fid in used:
        mapping[fid - 1] = chosen[fid]
    return mapping, True


def build_new_mapping(metadata: dict, chosen: dict[int, int], status: dict, *, dual: bool) \
        -> tuple[list[int], list[dict], bool]:
    """New mapping with actual AMS IDs and one entry per project filament."""
    filaments = metadata["filaments"]
    used = [item for item in filaments if item["used"]]
    missing = [item["id"] for item in used if item["id"] not in chosen]
    if missing:
        raise ProfileError(m('No slot selected for filament {p0}.', p0=', '.join(map(str, missing))))
    mapping = []
    mapping2 = []
    use_ams = False
    for item in filaments:
        if not item["used"]:
            mapping.append(-1)
            mapping2.append({"ams_id": 255, "slot_id": 255})
            continue
        tray = _find_tray(status, chosen[item["id"]])
        if tray is None:
            raise ProfileError(m('Slot {p0} is empty or missing.', p0=slot_label(chosen[item['id']])))
        source = tray.get("source") or {}
        ams_id, slot_id = source.get("ams_id"), source.get("slot_id")
        if not isinstance(ams_id, int) or not isinstance(slot_id, int):
            raise ProfileError(m('The protocol address of the selected slot is unknown.'))
        if dual:
            nozzle = item.get("nozzle_id")
            if nozzle not in (0, 1):
                raise ProfileError(m('The 3MF does not specify an unambiguous nozzle for filament {p0}.', p0=item['id']))
            if tray.get("extruder_id") not in (0, 1):
                raise ProfileError(m('The nozzle binding of slot {p0} is unknown.', p0=slot_label(chosen[item['id']])))
            if tray["extruder_id"] != nozzle:
                raise ProfileError(m('Slot {p0} feeds the wrong nozzle.', p0=slot_label(chosen[item['id']])))
        mapping2.append({"ams_id": ams_id, "slot_id": slot_id})
        if ams_id in (254, 255):
            mapping.append(-1)
        elif ams_id == 16:
            mapping.append(24 + slot_id)
            use_ams = True
        elif 128 <= ams_id <= 135:
            mapping.append(ams_id + slot_id)
            use_ams = True
        elif 0 <= ams_id <= 3 and 0 <= slot_id <= 3:
            mapping.append(ams_id * 4 + slot_id)
            use_ams = True
        else:
            raise ProfileError(m('The AMS protocol address is not supported.'))
    return mapping, mapping2, use_ams


def slot_label(slot: int) -> str:
    if slot == EXTERNAL_SLOT:
        return m('External Spool')
    return f"{'ABCD'[slot // 4] if slot // 4 < 4 else slot // 4 + 1}{slot % 4 + 1}"


# Background print sending: attempts, retry delays and deadlines.
PRINT_ATTEMPTS = 3
PRINT_RETRY_DELAYS = (5.0, 15.0)
PRINTER_WAIT = 60.0        # Maximum wait for the printer connection.
START_CONFIRM_WAIT = 120.0  # Maximum wait for the printer to report preparing or printing.
RESUME_WINDOW = 600.0      # Resume only jobs younger than this after a restart.
STARTED_STATES = {"PREPARE", "RUNNING", "PAUSE", "SLICING"}


class PrintRefused(Exception):
    """The print cannot proceed this way; retrying will not help."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


async def print_job(request: web.Request) -> web.Response:
    """Accept a print job and send it in the background (202 response, progress via job events).

    Body: {"mapping": {"<filament-id>": <slot>}, "plate": n, "timelapse": bool, "bed_leveling": bool,
    "flow_cali": bool}. For one filament without `mapping`, use the slot selected when slicing.
    Omit `flow_cali` on printers without automatic flow dynamics measurement (`Catalog.flow_calibration`)."""
    app = request.app
    job = _job_or_404(request)
    if job.state not in ("sliced", "sent", "failed") or not job.result:
        return error(409, m('Slice the file first.'))
    dev = job_device(app, job)
    if dev.print_lock.locked() or any(j.state == "sending" and (j.printer or app[DEVICES].default_id) == dev.id
                                      for j in app[JOBS].list()):
        return error(409, m('Another print is being sent to this printer.'))
    try:
        body = await request.json()
    except ValueError:
        body = {}
    try:
        job.pending = await _print_plan(app, dev, job, body)
    except PrintRefused as exc:
        return error(exc.status, message_of(exc))
    job.state, job.error, job.progress, job.step = "sending", None, 0.0, m('Accepted')
    _publish_job(app, job)
    _start_sending(app, job)
    return web.json_response(render(job.public()), status=202)


async def _print_plan(app: web.Application, dev: Device, job: Job, body: Any) -> dict:
    """Validate everything that can be checked before sending and persist the print plan."""
    printer = dev.printer
    status = printer.status()
    if not printer.connected:
        raise PrintRefused(m('Printer not connected.'))
    if refused := _commands_refused(status):
        raise PrintRefused(refused)
    if status["job"]["state"] not in IDLE_STATES:
        raise PrintRefused(m('The printer is busy.'))
    if not status["connection"]["sdcard"]:
        raise PrintRefused(m('No SD card in the printer.'))
    if not isinstance(body, dict):
        raise PrintRefused(m('Not a JSON object.'), 400)
    try:
        plate_index = int(body.get("plate") or job.result.get("plate") or 1)
    except (TypeError, ValueError):
        raise PrintRefused(m('Invalid plate number.'), 422) from None
    plate = next((p for p in job.result["plates"] if p["index"] == plate_index), None)
    if plate is None:
        raise PrintRefused(m('Plate not found.'), 422)
    chosen: dict[int, int] = {}
    raw_mapping = body.get("mapping") or {}
    if not isinstance(raw_mapping, dict):
        raise PrintRefused(m('mapping must be an object.'), 422)
    for key, slot in raw_mapping.items():
        if not str(key).isdigit() or not isinstance(slot, int):
            raise PrintRefused(m('mapping: Filament ID → slot number.'), 422)
        chosen[int(key)] = slot
    if not chosen and len(plate["filaments"]) == 1 and isinstance(job.settings.get("slot"), int):
        chosen[plate["filaments"][0]["id"]] = job.settings["slot"]
    capabilities = status.get("printer", {}).get("capabilities") or {}
    model_id = status.get("printer", {}).get("model_id") or dev.model_id or model_id_from_name(dev.model)
    if model_id is None:
        raise PrintRefused(m('The bridge does not recognize this printer model and cannot determine which print command its firmware expects. Update the bridge and OrcaSlicer data.'))
    new_protocol = bool(capabilities.get("new_print_protocol"))
    if model_id not in LEGACY_MODELS and not new_protocol:
        # New model without OrcaSlicer data in the image: the old file:///sdcard command would be wrong.
        raise PrintRefused(m('The bridge is missing data for this printer model; starting a print with the older command would be rejected. Update the bridge.'))
    try:
        if new_protocol:
            metadata = await asyncio.to_thread(read_print_metadata, app[JOBS].output_path(job), plate_index)
            mapping, mapping2, use_ams = build_new_mapping(
                metadata, chosen, status, dual=model_id in DUAL_NOZZLE_MODELS)
        else:
            mapping, use_ams = build_mapping(plate, chosen, status)
            mapping2 = None
    except (ProfileError, JobError) as exc:
        raise PrintRefused(message_of(exc), 422) from None
    if capabilities.get("hotend_rack"):
        # Required fila_info/nozzle_info data is not yet fully verified in the bridge.
        raise PrintRefused(m('H2C print start rejected: The hotend rack mapping cannot be fully and safely determined from this 3MF.'), 409)
    plan = {
        "plate": plate_index, "ams_mapping": mapping, "use_ams": use_ams,
        "remote_name": job.remote_path or remote_name(job.name, job.id),
        "timelapse": bool(body.get("timelapse", False)),
        "bed_leveling": bool(body.get("bed_leveling", True)),
        "flow_cali": bool(body.get("flow_cali", False))
        and (await catalog_of(dev)).flow_calibration(dev.model) is not False,
        "model_id": model_id, "new_protocol": new_protocol,
        "requested_at": time.time(),
    }
    if mapping2 is not None:
        plan["ams_mapping2"] = mapping2
    if capabilities.get("calibrations", {}).get("nozzle_offset"):
        plan["nozzle_offset_cali"] = bool(body.get("nozzle_offset_cali", False))
    if capabilities.get("pa_mode"):
        mode = body.get("extrude_cali_manual_mode", 0)
        if mode not in (0, 1):
            raise PrintRefused(m('extrude_cali_manual_mode must be 0 or 1.'), 422)
        plan["extrude_cali_manual_mode"] = mode
    return plan


def _start_sending(app: web.Application, job: Job) -> None:
    task = asyncio.create_task(_send_print(app, job_device(app, job), job))
    app[TASKS].add(task)
    task.add_done_callback(app[TASKS].discard)


def _set_step(app: web.Application, job: Job, step: str, progress: float | None = None) -> None:
    job.step = step
    if progress is not None:
        job.progress = progress
    _publish_job(app, job)


async def _send_print(app: web.Application, dev: Device, job: Job) -> None:
    """Send an accepted job, retrying network or printer failures."""
    async with dev.print_lock:
        plan = job.pending or {}
        for attempt in range(1, PRINT_ATTEMPTS + 1):
            # If the IP or access code changed, continue with the newly configured printer.
            dev = app[DEVICES].devices.get(dev.id, dev)
            try:
                reply = await _attempt_print(app, dev, job, plan)
            except PrintRefused as exc:
                _finish_print(app, job, error=message_of(exc))
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - FTP, TLS and printer errors vary widely
                log.warning(m('Print %s, attempt %d: %s'), job.id, attempt, exc)
                if attempt == PRINT_ATTEMPTS:
                    _finish_print(app, job, error=m('Could not send: {p0}', p0=exc))
                    return
                _set_step(app, job, m('Retry {p0} of {p1} ({p2})', p0=attempt + 1, p1=PRINT_ATTEMPTS, p2=exc))
                await asyncio.sleep(PRINT_RETRY_DELAYS[min(attempt - 1, len(PRINT_RETRY_DELAYS) - 1)])
                continue
            _finish_print(app, job, reply=reply)
            await dev.printer.pushall(force=True)
            return


async def _attempt_print(app: web.Application, dev: Device, job: Job, plan: dict) -> dict | None:
    printer = dev.printer
    config = app[CONFIG]
    name = plan["remote_name"]

    _set_step(app, job, m('Waiting for the printer'))
    deadline = time.monotonic() + PRINTER_WAIT
    while not printer.connected:
        if time.monotonic() > deadline:
            raise RuntimeError(m('Printer unreachable'))
        await asyncio.sleep(1)
    state = printer.status()["job"]
    if state["state"] in STARTED_STATES and state.get("file") and name in state["file"]:
        # An earlier attempt already started printing; only its confirmation was lost.
        return None
    if state["state"] not in IDLE_STATES:
        raise PrintRefused(m('The printer is busy.'))

    if not job.remote_path and not plan.get("uploaded") and job.uploaded == name:
        # Another plate of the same slice result: reuse the SD card file if its size matches.
        _set_step(app, job, m('Checking the file on the SD card'))
        listing = await asyncio.to_thread(ftps.list_dir, dev.host, dev.access_code, "/", context=dev.trust.context())
        size = next((e["size"] for e in listing if e["name"] == name), None)
        plan["uploaded"] = size == app[JOBS].output_path(job).stat().st_size

    if not job.remote_path and not plan.get("uploaded"):
        local = app[JOBS].output_path(job)
        loop = asyncio.get_running_loop()
        last = [0.0]

        def progress(sent: int, total: int) -> None:
            now = time.monotonic()
            if now - last[0] > 0.5 or sent == total:
                last[0] = now
                job.progress = round(sent / total, 3)
                loop.call_soon_threadsafe(app[EVENTS].publish, "job", job.public())

        _set_step(app, job, m('Uploading to the SD card'), 0.0)
        await asyncio.to_thread(ftps.upload, dev.host, dev.access_code, local, name, progress, context=dev.trust.context())
        _set_step(app, job, m('Checking the file on the SD card'))
        listing = await asyncio.to_thread(ftps.list_dir, dev.host, dev.access_code, "/", context=dev.trust.context())
        size = next((e["size"] for e in listing if e["name"] == name), None)
        if size != local.stat().st_size:
            raise RuntimeError(m('File on the SD card is incomplete ({p0} of {p1} bytes)', p0=size, p1=local.stat().st_size))
        plan["uploaded"] = True
        job.uploaded = name
        app[JOBS].save(job)

    _set_step(app, job, m('Starting the print'), None)
    command = build_project_file(plan.get("model_id") or dev.model_id, job.name, plan,
                                 print_url=config.print_url,
                                 capabilities=printer.status().get("printer", {}).get("capabilities"))
    reply = await printer.command("print", command, wait=15)
    if reply and str(reply.get("result", "")).lower() not in ("", "success", "ok"):
        raise PrintRefused(m('The printer rejected the request: {p0}', p0=reply.get('reason') or reply.get('result')))

    _set_step(app, job, m('Waiting for the print to start'))
    deadline = time.monotonic() + START_CONFIRM_WAIT
    while time.monotonic() < deadline:
        state = printer.status()["job"]
        if state["state"] in STARTED_STATES:
            return reply
        if state["state"] == "FAILED" and state.get("print_error"):
            raise PrintRefused(m('The printer reports an error (code {p0:08X}).', p0=state['print_error']))
        await asyncio.sleep(1)
    raise RuntimeError(m('The printer did not confirm the print start'))


def _finish_print(app: web.Application, job: Job, *, reply: dict | None = None, error: str | None = None) -> None:
    plan = job.pending or {}
    if error:
        job.state, job.error = "failed", error
    else:
        job.state, job.error = "sent", None
        job.sent = {"at": time.time(), "remote_name": plan.get("remote_name"), "plate": plan.get("plate"),
                    "ams_mapping": plan.get("ams_mapping"), "use_ams": plan.get("use_ams"),
                    "confirmed": True, "acknowledged": reply is not None}
        if plan.get("ams_mapping2") is not None:
            job.sent["ams_mapping2"] = plan["ams_mapping2"]
        if plan.get("plate") and plan["plate"] not in job.sent_plates:
            job.sent_plates = sorted([*job.sent_plates, plan["plate"]])
    job.pending, job.progress, job.step = None, None, None
    _publish_job(app, job)


def resume_pending_prints(app: web.Application) -> None:
    """After restart, resume recent accepted jobs; older jobs are considered unsent."""
    for job in app[JOBS].list():
        if job.state != "sending":
            continue
        requested = (job.pending or {}).get("requested_at", 0)
        try:
            job_device(app, job)
        except Problem:
            _finish_print(app, job, error=m('The printer for this job is no longer configured.'))
            continue
        if job.pending and time.time() - requested < RESUME_WINDOW:
            log.info(m('Resuming print %s after restart'), job.id)
            _start_sending(app, job)
        else:
            _finish_print(app, job, error=m('The bridge restarted; the print was not sent.'))


# --- Printer setup ---

def _printer_body(body: Any, *, partial: bool) -> dict:
    """Validate the app's {host, access_code, name, serial}. `partial` validates only supplied changes."""
    if not isinstance(body, dict):
        raise Problem(400, m('Not a JSON object.'))
    out: dict = {}
    for key in ("host", "access_code", "name", "serial"):
        value = body.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise Problem(422, m('{p0} must be text.', p0=key))
        out[key] = value.strip()
    if "host" in out and not re.fullmatch(r"[A-Za-z0-9.:\-\[\]]{1,253}", out["host"]):
        raise Problem(422, m('Invalid printer IP address or name.'))
    if "access_code" in out and not re.fullmatch(r"[A-Za-z0-9]{4,32}", out["access_code"]):
        raise Problem(422, m('The access code contains letters and numbers (usually 8 characters).'))
    if "name" in out and len(out["name"]) > 60:
        raise Problem(422, m('Name: Up to 60 characters.'))
    if not partial and not ("host" in out and "access_code" in out):
        raise Problem(422, m('IP address and access code are missing.'))
    return out


async def _json_body(request: web.Request) -> Any:
    try:
        return await request.json()
    except ValueError:
        raise Problem(400, m('Invalid JSON.')) from None


def _probe_problem(exc: ProbeError) -> Problem:
    status = 422 if exc.code in ("access_code", "not_bambu") else 409 if exc.code in (
        "certificate_changed", "other_printer") else 502
    return Problem(status, message_of(exc), code=exc.code)


async def _probed(app: web.Application, data: dict) -> dict:
    """Log in to the printer (`probe`) and add model, profile and existing-configuration information."""
    try:
        if data.get("pin"):
            found = await app[PROBE](data["host"], data["access_code"], data.get("serial"), pin=data["pin"])
        else:
            found = await app[PROBE](data["host"], data["access_code"], data.get("serial"))
    except ProbeError as exc:
        raise _probe_problem(exc) from None
    model = app[DEVICES].models.find(found.get("model"))
    found["model"] = model.name if model else found.get("model")
    found["model_id"] = model.model_id if model else None
    found["machine"] = app[CATALOGS].machine_for(found["model"], found.get("nozzle_diameter"))
    found["supported"] = found["machine"] is not None
    found["configured"] = found["serial"] in app[DEVICES].devices
    return found


def _summary(app: web.Application, dev: Device) -> dict:
    return dev.summary(default=dev.id == app[DEVICES].default_id)


async def list_printers(request: web.Request) -> web.Response:
    devices = request.app[DEVICES]
    return ok({"printers": [_summary(request.app, d) for d in devices.list()], "default": devices.default_id,
               "models": devices.models.names()})


async def probe_printer(request: web.Request) -> web.Response:
    """Check IP and access code without saving: {host, access_code} → serial number, model, firmware, etc."""
    data = _printer_body(await _json_body(request), partial=False)
    return ok(await _probed(request.app, data))


async def add_printer(request: web.Request) -> web.Response:
    """Set up a printer: {host, access_code, name?}. Verify the printer login first."""
    data = _printer_body(await _json_body(request), partial=False)
    found = await _probed(request.app, data)
    if found["configured"]:
        return error(409, m('This printer is already configured. Change its access code or IP address there.'))
    settings = PrinterSettings(serial=found["serial"], host=data["host"], access_code=data["access_code"],
                               name=data.get("name") or "", model=found.get("model"))
    dev = await request.app[DEVICES].add(settings, fingerprint=found.get("fingerprint"))
    log.info(m('Printer %s (%s) configured'), dev.name, found.get("model") or m('model unknown'))
    return ok({**_summary(request.app, dev), "probe": found}, status=201)


async def get_printer(request: web.Request) -> web.Response:
    return ok(_summary(request.app, device(request)))


async def update_printer(request: web.Request) -> web.Response:
    """Change name, IP or access code: {name?, host?, access_code?, default?}. Verify new credentials
    first; they must lead to the same printer serial number."""
    dev = device(request)
    body = await _json_body(request)
    data = _printer_body(body, partial=True)
    make_default = body.get("default") is True
    devices = request.app[DEVICES]
    if dev.settings.source == "env" and data:
        return error(409, m('This printer is configured through the bridge environment variables; change its name, IP address and access code there (BAMBU_NAME, BAMBU_HOST, BAMBU_ACCESS_CODE).'))
    fingerprint = None
    if "host" in data or "access_code" in data:
        candidate = {"host": data.get("host", dev.host), "access_code": data.get("access_code", dev.access_code),
                     "serial": dev.id}
        if "access_code" not in data:
            # Send saved access codes only to the same certificate; otherwise a paired device could redirect
            # the address to its own computer and capture the code. A newly entered code trusts the new certificate.
            candidate["pin"] = devices.store.pins.get(dev.id)
            if not candidate["pin"]:
                return error(409, m('Enter the access code again for a new address (the bridge does not recognize this printer’s certificate yet).'))
        found = await _probed(request.app, candidate)
        if found["serial"] != dev.id:
            return error(409, m('A different printer is responding at this address.'))
        fingerprint = found.get("fingerprint")
    if data:
        dev = await devices.update(dev.id, host=data.get("host"), access_code=data.get("access_code"),
                                   name=data.get("name"), fingerprint=fingerprint)
    if make_default:
        devices.set_default(dev.id)
    return ok(_summary(request.app, dev))


async def delete_printer(request: web.Request) -> web.Response:
    dev = device(request)
    if dev.settings.source == "env":
        return error(409, m('This printer is configured through the bridge environment variables and can only be removed there.'))
    if dev.print_lock.locked():
        return error(409, m('A print is being sent to this printer.'))
    await request.app[DEVICES].remove(dev.id)
    log.info(m('Printer %s removed'), dev.name)
    return ok({"ok": True})


# --- Live Activity ---

# --- Filament library ---------------------------------------------------------------------------------------------

async def spool_plan(app: web.Application, dev: Device) -> Plan:
    """Filaments of the printed plate and, for prints sent by the bridge, the filament → slot mapping."""
    path, plate = await current_print_file(app, dev)
    info = await asyncio.to_thread(read_slice_info, path)
    plates = info.get("plates") or []
    chosen = next((p for p in plates if p["index"] == plate), plates[0] if plates else None)
    filaments = chosen["filaments"] if chosen else []
    job = dev.printer.status()["job"]
    mapping = None
    for candidate in app[JOBS].list():
        if candidate.sent and candidate.name == job.get("name") and candidate.printer in (None, dev.id):
            slots = candidate.sent.get("ams_mapping") or []
            # ams_mapping lists one slot per project filament (ID = position + 1); -1 = not used.
            mapping = {i + 1: slot for i, slot in enumerate(slots) if isinstance(slot, int) and slot >= 0} or None
            break
    return filaments, mapping


def _spool_reply(spool: dict | None, outcome: dict) -> dict:
    reply = {"spool": SpoolStore.public(spool) if spool else None, **outcome}
    if "deleted" in outcome:
        reply["deleted"] = SpoolStore.public({**outcome["deleted"], "id": outcome["deleted"]["id"]})
    return reply


async def load_spool(request: web.Request) -> web.Response:
    """A spool with an NFC tag in a slot: `{"slot", "uid", "action": "load"|"write", "choice": "auto"|"restore"|"new",
    "weight", "remaining", "type", "brand", "subtype", "color", "nozzle_temp_min", "nozzle_temp_max"}`.

    `load` (tag read): known spools keep the counted amount; for a deleted spool the reply may be
    `{"decision": "choose", "suggest": "new"|"restore", "deleted": {…}}` and the app asks again with a choice.
    `write` (tag about to be written): same filament keeps the amount, otherwise a new full spool. The reply's
    `spool.remaining` is what the app writes to the tag."""
    try:
        body = await request.json()
    except ValueError:
        return error(400, m('Invalid JSON.'))
    if not isinstance(body, dict):
        return error(400, m('Invalid JSON.'))
    dev = device(request)
    slot, uid = body.get("slot"), body.get("uid")
    action, choice = body.get("action", "load"), body.get("choice", "auto")
    if not isinstance(slot, int) or controls.find_tray(dev.printer.status(), slot) is None:
        return error(422, m('Slot not found.'))
    if action not in ("load", "write") or choice not in ("auto", "restore", "new") or not isinstance(uid, str):
        return error(422, m('Invalid spool request.'))
    info = {key: body.get(key) for key in ("type", "brand", "subtype", "color", "nozzle_temp_min", "nozzle_temp_max")}
    store = request.app[SPOOLS]
    try:
        if action == "write":
            spool, outcome = store.written(dev.id, slot, uid, weight=body.get("weight"), info=info)
        else:
            spool, outcome = store.load(dev.id, slot, uid, weight=body.get("weight"), remaining=body.get("remaining"),
                                        info=info, choice=choice)
    except (TypeError, ValueError):
        return error(422, m('Invalid spool data (tag ID or amount).'))
    request.app[EVENTS].publish("status", {"printer": dev.id})
    return ok(_spool_reply(spool, outcome))


async def list_spools(request: web.Request) -> web.Response:
    devices = request.app[DEVICES].devices
    spools = []
    for spool in request.app[SPOOLS].library():
        item = SpoolStore.public(spool)
        dev = devices.get(spool.get("printer") or "")
        item["printer_name"] = dev.name if dev else None
        spools.append(item)
    return ok({"spools": spools})


async def update_spool(request: web.Request) -> web.Response:
    """`{"remaining": grams, "note": text, "unload": true}`; Bambu spools take only a note and unloading."""
    try:
        body = await request.json()
    except ValueError:
        return error(400, m('Invalid JSON.'))
    if not isinstance(body, dict):
        return error(400, m('Invalid JSON.'))
    note = body.get("note")
    try:
        spool = request.app[SPOOLS].update(request.match_info["spool_id"], remaining=body.get("remaining"),
                                           note=note if isinstance(note, str) else None, unload=body.get("unload") is True)
    except (TypeError, ValueError):
        return error(422, m('The AMS reports the amount of Bambu spools; it cannot be changed here.'))
    if spool is None:
        return error(404, m('Spool not found.'))
    if spool.get("printer"):
        request.app[EVENTS].publish("status", {"printer": spool["printer"]})
    return ok({"spool": SpoolStore.public(spool)})


async def delete_spool(request: web.Request) -> web.Response:
    store = request.app[SPOOLS]
    spool = store.get(request.match_info["spool_id"])
    if spool is None or not store.delete(spool["id"]):
        return error(404, m('Spool not found.'))
    return web.Response(status=204, headers={"Cache-Control": "no-store"})


async def live_status(request: web.Request) -> web.Response:
    live = device(request).live
    registry = request.app[PUSH]
    return ok({"enabled": bool(live and live.enabled), "devices": registry.start_devices(),
               "notifications": registry.alert_devices(),
               "activities": len(live.push.activities) if live else 0,
               "printing": bool(live and live.push.current is not None)})


# App installation ID (identifierForVendor): a new start token replaces the same iPhone's old token.
INSTALLATION = re.compile(r"[0-9A-Fa-f-]{8,64}")


async def live_register(request: web.Request) -> web.Response:
    """Register an app token: {"kind": "start"|"update"|"alert", "token": "<hex>",
    "environment": "development"|"production", "activity_id": "…", "printer": "<id>",
    "installation": "…", "events": [...], "language": "en"|"de"}. Start tokens apply to all printers;
    update tokens belong to one printer's Live Activity (default printer if omitted); alert tokens
    receive the requested notification events (an empty list unregisters). Language defaults to English."""
    try:
        body = await request.json()
    except ValueError:
        return error(400, m('Invalid JSON.'))
    if not isinstance(body, dict):
        return error(400, m('Not a JSON object.'))
    kind, token, environment = body.get("kind"), body.get("token"), body.get("environment")
    language = body.get("language", "en")
    if language not in ("en", "de"):
        return error(422, m("language must be en or de."))
    if kind not in ("start", "update", "alert") or environment not in ENVIRONMENTS:
        return error(422, "kind: start|update|alert, environment: development|production.")
    events = body.get("events")
    if kind == "alert" and not (isinstance(events, list) and all(e in NOTIFY_EVENTS for e in events)):
        return error(422, m('events: List from {p0}.', p0=', '.join(NOTIFY_EVENTS)))
    if not isinstance(token, str) or not 32 <= len(token) <= 400 or any(c not in "0123456789abcdefABCDEF" for c in token):
        return error(422, m('Invalid token.'))
    activity_id = body.get("activity_id")
    printer_id = body.get("printer")
    devices = request.app[DEVICES].devices
    if kind == "update":
        if printer_id is None:
            printer_id = request.app[DEVICES].default_id
        if printer_id not in devices:
            # Newer apps use the derived attributes ID instead of the serial number.
            printer_id = next((serial for serial in devices if public_printer_id(serial) == printer_id), None)
        if printer_id not in devices:
            return error(422, m('Printer not found.'))
    installation = body.get("installation")
    if installation is not None and not (isinstance(installation, str) and INSTALLATION.fullmatch(installation)):
        return error(422, m('Invalid installation ID.'))
    principal = request[PRINCIPAL]
    request.app[PUSH].add(kind, token.lower(), environment, activity_id if isinstance(activity_id, str) else None,
                          printer_id or "", device=principal.device_id or SHARED_DEVICE,
                          installation=installation.upper() if installation else None,
                          events=list(dict.fromkeys(events)) if kind == "alert" else None, language=language)
    return ok({"registered": kind})


# --- Construction ---

# Printer routes: `/api/printers/<id>` and, for the default printer, directly under `/api`.
PRINTER_ROUTES = (
    ("GET", "/status", "get_status"),
    ("GET", "/events", "events"),
    ("POST", "/control", "control"),
    ("POST", "/ams/tray", "set_tray"),
    ("GET", "/ams/k-values", "k_values"),
    ("POST", "/ams/k-value", "select_k_value"),
    ("GET", "/ams/drying", "ams_drying"),
    ("GET", "/print/objects", "print_objects"),
    ("GET", "/print/thumbnail", "print_thumbnail"),
    ("GET", "/print/preview", "print_preview"),
    ("GET", "/sdcard", "sdcard"),
    ("DELETE", "/sdcard", "sdcard_delete"),
    ("POST", "/sdcard/import", "sdcard_import"),
    ("GET", "/sdcard/download", "sdcard_download"),
    ("GET", "/camera/snapshot", "camera_snapshot"),
    ("GET", "/camera/stream", "camera_stream"),
    ("GET", "/profiles", "profiles"),
    ("GET", "/live", "live_status"),
    ("POST", "/ams/spool", "load_spool"),
)

ENV_RETRY = 30.0  # Number of attempts to read the environment printer's serial number.


def env_settings(config: Config) -> PrinterSettings | None:
    if not config.host or not config.access_code:
        return None
    return PrinterSettings(serial=config.serial or "", host=config.host, access_code=config.access_code,
                           name=config.name, machine_profile=config.machine_profile, source="env")


async def _add_env_printer(app: web.Application, settings: PrinterSettings) -> None:
    """Environment printer without `BAMBU_SERIAL`: repeatedly read its serial number from the certificate."""
    while True:
        try:
            settings.serial = await read_serial(settings.host)
            break
        except ProbeError as exc:
            log.warning(m('Printer from BAMBU_HOST: %s Retrying in %.0f s.'), exc, ENV_RETRY)
            await asyncio.sleep(ENV_RETRY)
    devices = app[DEVICES]
    devices.devices.pop(settings.serial, None)  # Environment configuration takes precedence over the same stored printer.
    devices.add_device(Device(settings, devices.catalogs, devices.models, trust=devices.store.trust(settings.serial)),
                       env=True)


def create_app(config: Config, *, printer: Printer | None = None, camera: Camera | None = None,
               catalog: Catalog | None = None, live: LiveActivities | None = None, probe_fn=None,
               identity: BridgeIdentity | None = None, tls_state: TLSState | None = None,
               pairing_store: PairingStore | None = None, start_background: bool = True) -> web.Application:
    app = web.Application(middlewares=[language_middleware, transport_middleware, auth_middleware], client_max_size=(config.max_upload_mb + 1) * 1024 * 1024)
    app[CONFIG] = config
    app[IDENTITY] = identity or load_identity(config.auth_dir / "bridge.json", config.bridge_name)
    app[TLS] = tls_state or TLSState(None, None)
    app[AUTH] = pairing_store or PairingStore(config.auth_dir / "auth.json", config.bridge_token)
    catalogs = Catalogs(config.profiles_dir, config.filament_ids)
    if catalog is not None:
        catalogs.put(catalog)
    app[CATALOGS] = catalogs
    devices = Devices(PrinterStore(config.data_dir / "printers.json"), catalogs, Models(config.models_dir))
    app[DEVICES] = devices
    app[SLICER] = Slicer(config.orca_cmd, config.slice_timeout)
    app[JOBS] = JobStore(config.data_dir, config.keep_jobs)
    app[EVENTS] = EventHub()
    app[TASKS] = set()
    app[PREVIEW_LOCKS] = {}
    app[HMS] = Texts(config.hms_dir)
    app[PROBE] = probe_fn or probe
    apns = APNsConfig.load(config.apns_key_file, config.apns_key_id, config.apns_team_id, config.apns_bundle_id)
    # Use an APNs key when configured, otherwise the relay; BRIDGE_PUSH_RELAY=off disables pushes.
    app[APNS] = APNsClient(apns) if apns else (
        RelayClient(config.push_relay, config.data_dir / "relay.json") if config.push_relay else None)
    app[PUSH] = live.registry if live is not None else Registry(config.data_dir / "push.json")
    app[SPOOLS] = SpoolStore(config.data_dir / "spools.json")
    warming: set[str] = set()

    def changed(dev: Device) -> None:
        app[EVENTS].publish("status", {"printer": dev.id})
        if dev.live is not None:
            dev.live.observe(described_status(app, dev))
        if dev.spools is not None:
            dev.spools.observe(dev.printer.status())
        machine = dev.machine_profile
        # Load the catalog once model and nozzle are known (status then includes flow calibration support).
        if machine and catalogs.cached(machine) is None and machine not in warming:
            warming.add(machine)
            task = asyncio.get_running_loop().create_task(catalogs.get(machine))
            task.add_done_callback(lambda t, m=machine: (warming.discard(m), t.cancelled() or t.exception()))

    def wire(dev: Device) -> None:
        if dev.live is None:
            first = not any(d.live for d in devices.devices.values() if d is not dev)
            dev.live = live if (live is not None and first) else LiveActivities(
                app[APNS], app[PUSH], lambda pid=dev.id: devices.devices[pid].name if pid in devices.devices else m('Printer'),
                dev.id, adopt=first)
        if dev.spools is None:
            dev.spools = SpoolTracker(app[SPOOLS], dev.id, lambda d=dev: spool_plan(app, d),
                                      lambda d=dev: app[EVENTS].publish("status", {"printer": d.id}))
        dev.printer.subscribe(lambda d=dev: changed(d))

    devices.on_added.append(wire)
    pending_env = None
    if printer is not None:
        settings = env_settings(config) or PrinterSettings(serial=printer.serial, host=printer.host, access_code="",
                                                           source="env")
        settings.serial = settings.serial or printer.serial
        settings.name = settings.name or printer.name
        devices.add_device(Device(settings, catalogs, devices.models, printer=printer,
                                  camera=camera or Camera(settings.host, settings.access_code)), env=True)
        devices.load()
    else:
        env = env_settings(config)
        if env is not None and not env.serial:
            pending_env, env = env, None
        devices.load(env)

    app.router.add_get("/", home)
    app.router.add_post("/invite", web_invite)
    app.router.add_get("/healthz", healthz)
    app.router.add_post("/api/pair", pair)
    app.router.add_post("/api/pairing", create_invitation)
    app.router.add_get("/api/devices", list_client_devices)
    app.router.add_delete("/api/devices/{device_id}", delete_client_device)
    for method, path, handler in PRINTER_ROUTES:
        function = globals()[handler]
        app.router.add_route(method, f"/api{path}", function)
        app.router.add_route(method, f"/api/printers/{{printer_id}}{path}", function)
    app.router.add_get("/api/printers", list_printers)
    app.router.add_post("/api/printers", add_printer)
    app.router.add_post("/api/printers/probe", probe_printer)
    app.router.add_get("/api/printers/{printer_id}", get_printer)
    app.router.add_patch("/api/printers/{printer_id}", update_printer)
    app.router.add_delete("/api/printers/{printer_id}", delete_printer)
    app.router.add_get("/api/printers/{printer_id}/jobs", list_jobs)
    app.router.add_post("/api/printers/{printer_id}/jobs", upload_job)
    app.router.add_post("/api/live/register", live_register)
    app.router.add_get("/api/spools", list_spools)
    app.router.add_patch("/api/spools/{spool_id}", update_spool)
    app.router.add_delete("/api/spools/{spool_id}", delete_spool)
    app.router.add_get("/api/jobs", list_jobs)
    app.router.add_post("/api/jobs", upload_job)
    app.router.add_get("/api/jobs/{job_id}", get_job)
    app.router.add_delete("/api/jobs/{job_id}", delete_job)
    app.router.add_post("/api/jobs/{job_id}/slice", slice_job)
    app.router.add_post("/api/jobs/{job_id}/print", print_job)
    app.router.add_get("/api/jobs/{job_id}/thumbnail", job_thumbnail)
    app.router.add_get("/api/jobs/{job_id}/preview", job_preview)

    if start_background:
        async def background(app: web.Application):
            tls = app[TLS]
            advertiser = None
            if config.mdns:
                advertiser = MDNSAdvertiser(
                    identity=app[IDENTITY],
                    port=config.https_port if tls.enabled else config.port,
                    fingerprint=tls.fingerprint,
                    tls_enabled=tls.enabled,
                    public_url=config.public_url,
                )
                await advertiser.start()
            devices.start()
            env_task = asyncio.create_task(_add_env_printer(app, pending_env)) if pending_env else None
            log.info(m('Printer: %s'), ", ".join(f"{d.name} ({d.host})" for d in devices.list()) or
                     (m('read from BAMBU_HOST') if pending_env else m('none configured yet')))
            if not app[TLS].enabled and not config.trusted_proxies:
                log.warning(m('BRIDGE_TLS=off without BRIDGE_TRUSTED_PROXIES: The API is accessible over HTTP to every device on the network. Behind a reverse proxy, add its addresses to BRIDGE_TRUSTED_PROXIES.'))
            log.info("Live Activity: %s", m('own APNs key') if apns else
                     m('via relay {p0}', p0=config.push_relay) if app[APNS] else "aus")
            log.info("Error texts: %s", "loaded" if app[HMS].available else f"missing in {config.hms_dir}")
            if app[AUTH].setup_code:
                log.info(m('Setup code: %s'), display_code(app[AUTH].setup_code))
            elif config.bridge_token:
                log.info(m('Pairing: Fixed access key or an invitation from a paired device'))
            resume_pending_prints(app)
            yield
            if env_task:
                env_task.cancel()
            await devices.stop()
            if app[APNS]:
                await app[APNS].close()
            await app[AUTH].close()
            if advertiser:
                await advertiser.stop()
            for pending in list(app[TASKS]):
                pending.cancel()
        app.cleanup_ctx.append(background)
    else:
        async def pairing_cleanup(app: web.Application):
            yield
            await app[AUTH].close()
        app.cleanup_ctx.append(pairing_cleanup)
    return app
