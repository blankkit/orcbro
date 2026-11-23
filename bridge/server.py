"""Bridge HTTP API (Brotocol) for the Bambro app: JSON, Server-Sent Events and MJPEG; see `docs/reference.md`.

Printer routes live under `/api/printers/<serial>/…` (status, controls, AMS, SD card, camera, profiles). The same
paths without the printer prefix (`/api/status`, etc.) address the default printer; Bambro uses them while no
printer is selected. Jobs (`/api/jobs`) are shared across printers; each job belongs to one printer (`printer`).

Authentication: a device pairs once (`POST /api/pair`) with the setup code from the log, an invitation from a
paired device, or the fixed access key (`BRIDGE_ACCESS_TOKEN`), and then sends `Authorization: Bearer <token>`.
Device tokens are random and can be revoked one by one (`auth.py`); the token for the fixed key is derived from
the key (HMAC), survives restarts and is revoked by changing the key. The setup page and its forms (`/`,
`/setup`, `/invite`) are served by `web.py`."""
from __future__ import annotations

from .i18n import m, message_of, render, language_middleware, t

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import logging
from queue import Full as QueueFull, Queue
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from aiohttp import web

from . import __version__, controls, ftps, preview
from .apns import APNsClient, APNsConfig
from .relay import RelayClient
from .auth import AuthError, InvalidCode, PairingStore, Principal, RateLimited, display_code, legacy_app_token
from .camera import Camera
from .config import Config
from .devices import Catalogs, Device, Devices, PrinterSettings, PrinterStore
from .hms import Texts
from .identity import BridgeIdentity, load_identity
from .models import DUAL_NOZZLE_MODELS, LEGACY_MODELS, Models
from .probe import ProbeError, probe, read_identity
from .live import ENVIRONMENTS, SHARED_DEVICE, LiveActivities, Registry, public_printer_id
from .spools import TAG_FIELDS, BambuAmount, BambuFilament, CountedAmount, Plan, SpoolStore, SpoolTracker, trays_of
from .notify import EVENTS as NOTIFY_EVENTS
from .mdns import MDNSAdvertiser
from .newspool import NewSpoolWatcher
from .jobs import (Job, JobError, JobStore, check_complete, classify, open_archive, plate_thumbnail, project_filaments,
                   project_plates, project_printer, read_member, read_objects, read_slice_info, remote_name,
                   sliced_for)
from .printer import Printer, PrinterError
from .print_command import build_project_file
from .print_metadata import read_print_metadata
from .profiles import BED_LABELS, BED_TYPES, SUPPORT_TYPES, Catalog, ProfileError
from .slicer import SliceError, Slicer
from .state import EXTERNAL_SLOT, MODEL_IDS_BY_NAME, model_id_from_name
from .tls import TLSState
from .web import (PublicOrigin, URLConfigurationError, invitation_page, pairing_url, public_origin, setup_page,
                  trusted_proxy)

log = logging.getLogger(__name__)

# App token for the fixed access key (`POST /api/pair` with `token`).
app_token = legacy_app_token


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
    """The job's printer."""
    devices = app[DEVICES]
    if job.printer in devices.devices:
        return devices.get(job.printer)
    raise Problem(409, m('The printer for this job is no longer configured.'))


def assign_unowned_jobs(app: web.Application) -> None:
    """Jobs saved by 0.x bridges, before printers were configurable, have no printer: they belong to the default
    printer. Record it once, as soon as there is one."""
    default = app[DEVICES].default_id
    if default is None:
        return
    for job in app[JOBS].list():
        if job.printer is None:
            job.printer = default
            app[JOBS].save(job)


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
    return (trusted_proxy(_peer(request), request.app[CONFIG].trusted_proxies)
            and request.headers.get("X-Forwarded-Proto", "").lower() == "https")


def _source(request: web.Request) -> str:
    """Client identity for pairing throttles: the peer address, or behind a trusted proxy the nearest
    untrusted address in `X-Forwarded-For`. IPv6 clients count per /64, since one host usually holds many."""
    proxies = request.app[CONFIG].trusted_proxies
    address = _peer(request) or ""
    if trusted_proxy(address, proxies):
        for item in reversed(request.headers.get("X-Forwarded-For", "").split(",")):
            try:
                ipaddress.ip_address(item.strip())
            except ValueError:
                break
            address = item.strip()
            if not trusted_proxy(address, proxies):
                break
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return address or "unknown"
    if parsed.version == 6 and parsed.ipv4_mapped:
        parsed = parsed.ipv4_mapped
    if parsed.version == 6:
        return str(ipaddress.ip_network(f"{parsed}/64", strict=False))
    return str(parsed)


# Request body limits, enforced while reading. Model uploads stream through `upload_job` with their own limit;
# the application-wide limit covers JSON routes, and the public routes get a smaller one.
JSON_BODY_LIMIT = 64 * 1024
PUBLIC_BODY_LIMIT = 4 * 1024
MAX_UPLOADS = 2


def _bounded(request: web.Request, limit: int) -> web.Request:
    """The request with a smaller body limit; refuse a declared larger body before reading anything."""
    if request.content_length is not None and request.content_length > limit:
        raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=request.content_length)
    return request.clone(client_max_size=limit)


@web.middleware
async def transport_middleware(request: web.Request, handler):
    """Restrict HTTP API access to avoid sending tokens and keys in clear text.

    With built-in TLS, HTTP serves only setup and `/healthz`; API, invitations and the setup QR code require
    HTTPS or a trusted HTTPS proxy. Without built-in TLS, `BRIDGE_TRUSTED_PROXIES` must forward HTTPS
    (`X-Forwarded-Proto: https`); without trusted proxies, the configuration requires `BRIDGE_PLAIN_HTTP=on`."""
    if _own_tls(request) or request.path in ("/", "/healthz") or not (
            request.path.startswith("/api/") or request.path in ("/invite", "/setup")):
        return await handler(request)
    config = request.app[CONFIG]
    if request.app[TLS].enabled:
        if not _via_trusted_https(request):
            port = config.https_port
            return error(403, m('HTTPS only (port {p0}). Bambro connects this way automatically after pairing.', p0=port))
    elif config.trusted_proxies and not _via_trusted_https(request):
        return error(403, m('Only available over HTTPS through the configured reverse proxy (BRIDGE_TRUSTED_PROXIES).'))
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


def _rate_limited(message: str, retry_after: int) -> web.Response:
    return web.json_response(render({"error": message}), status=429,
                             headers={"Cache-Control": "no-store", "Retry-After": str(retry_after)})


async def pair(request: web.Request) -> web.Response:
    """Public: exchange the fixed access key, the setup code or an invitation for a token. A blocked client is
    refused before its body is read; the body is limited to a few kilobytes."""
    store, source = request.app[AUTH], _source(request)
    blocked = store.retry_after(source)
    if blocked is not None:
        return _rate_limited(m('Too many incorrect setup codes.'), blocked)
    try:
        body = await _bounded(request, PUBLIC_BODY_LIMIT).json()
    except web.HTTPRequestEntityTooLarge:
        return error(413, m('The request is too large.'))
    except ValueError:
        return error(400, m('Invalid JSON.'))
    if not isinstance(body, dict):
        return error(400, m('Expected a JSON object.'))
    key = body.get("token")
    secret = request.app[CONFIG].bridge_token
    if key is not None:
        try:
            await store.check_access_key(key, source)
        except InvalidCode:
            return error(401, m('Incorrect access key.'))
        except RateLimited as exc:
            return _rate_limited(m('Too many incorrect access keys.'), exc.retry_after)
        return ok({"app_token": app_token(secret), "bridge_version": __version__})
    code, name = body.get("code"), body.get("device_name")
    if not isinstance(code, str) or not isinstance(name, str):
        return error(422, m('code and device_name are required.'))
    try:
        token, paired = await store.pair(code, name, source)
    except ValueError as exc:
        return error(422, message_of(exc))
    except InvalidCode:
        return error(401, m('Setup code is invalid or has expired.'))
    except RateLimited as exc:
        return _rate_limited(m('Too many incorrect setup codes.'), exc.retry_after)
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


def grouped_fingerprint(fingerprint: str) -> str:
    """SHA-256 fingerprint in groups of four, as Bambro shows it."""
    return " ".join(fingerprint[i:i + 4] for i in range(0, len(fingerprint), 4))


def own_certificate_fingerprint(request: web.Request) -> str | None:
    """Offer a pinning fingerprint only on the built-in TLS listener with no public URL configured.
    Behind a reverse proxy, the app sees the proxy certificate; pinning the internal certificate
    would make all subsequent requests fail."""
    if request.app[CONFIG].public_url:
        return None
    return request.app[TLS].fingerprint if _own_tls(request) else None


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
            # Revoked devices must also stop receiving Live Activity messages. Pushes check the paired devices before
            # sending and startup discards leftovers, so a failed save here cannot keep the device subscribed.
            try:
                request.app[PUSH].remove_device(request.match_info["device_id"])
            except OSError as exc:
                log.error(m('Could not save push tokens: %s'), exc)
    except AuthError as exc:
        log.error(m('Could not revoke device: %s'), exc)
        return error(500, m('Device could not be revoked securely.'))
    if not removed:
        return error(404, m('Device not found.'))
    if not request.app[AUTH].devices and not request.app[CONFIG].bridge_token:
        log.warning(m('The last paired device was removed. Pairing stays closed until the bridge is restarted; the new setup code then appears in this log.'))
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


async def home(request: web.Request, form_error: str | None = None, http_status: int = 200,
               headers: dict | None = None) -> web.Response:
    """Public setup page. It never shows a pairing code, the certificate fingerprint or the printers; the
    first-device setup code is only in the bridge log, and the QR code needs that code (`POST /setup`)."""
    try:
        origin, secure = _page_origin(request)
    except URLConfigurationError as exc:
        return web.Response(text=render(message_of(exc)), status=400, content_type="text/plain")
    store = request.app[AUTH]
    body = setup_page(
        identity=request.app[IDENTITY],
        origin=origin,
        fixed_key=bool(request.app[CONFIG].bridge_token),
        paired=bool(store.devices),
        setup_minutes=_setup_minutes(store),
        secure=secure,
        error=form_error,
    )
    return web.Response(text=body, status=http_status, content_type="text/html",
                        headers={**PAGE_HEADERS, **(headers or {})})


def _setup_minutes(store: PairingStore) -> int | None:
    seconds = store.setup_remaining
    return (seconds + 59) // 60 if seconds else None


async def _page_form(request: web.Request):
    """Form of a public page, limited to a few kilobytes while reading."""
    return await _bounded(request, PUBLIC_BODY_LIMIT).post()


async def web_invite(request: web.Request) -> web.Response:
    """Setup-page invitation: the fixed access key grants a QR code for another device.
    Requires HTTPS; incorrect keys are throttled per client as in pairing."""
    config = request.app[CONFIG]
    try:
        origin, secure = _page_origin(request)
    except URLConfigurationError as exc:
        return web.Response(text=render(message_of(exc)), status=400, content_type="text/plain")
    if not config.bridge_token or not origin or not secure:
        return await home(request, m('Invitations using an access key require HTTPS and a fixed access key.'), 400)
    store, source = request.app[AUTH], _source(request)
    blocked = store.retry_after(source)
    if blocked is not None:
        return await home(request, m('Too many incorrect access keys.'), 429, {"Retry-After": str(blocked)})
    try:
        form = await _page_form(request)
        await store.check_access_key(form.get("key"), source)
    except web.HTTPRequestEntityTooLarge:
        return await home(request, m('The request is too large.'), 413)
    except InvalidCode:
        return await home(request, m('Incorrect access key.'), 401)
    except RateLimited as exc:
        return await home(request, m('Too many incorrect access keys.'), 429, {"Retry-After": str(exc.retry_after)})
    try:
        invitation = await store.invite(Principal("access_key"))
    except ValueError as exc:
        return await home(request, message_of(exc), 429)
    fingerprint = request.app[TLS].fingerprint if origin.include_fingerprint else None
    identity = request.app[IDENTITY]
    body = invitation_page(identity=identity, link=pairing_url(origin, invitation.code, identity.name, fingerprint),
                           code=invitation.code, minutes=max(1, (invitation.expires_at - int(time.time())) // 60))
    return web.Response(text=body, content_type="text/html", headers=PAGE_HEADERS)


async def web_setup(request: web.Request) -> web.Response:
    """Setup-page QR code for the first device: whoever enters the setup code from the bridge log gets the
    pairing link with this bridge's address and certificate fingerprint. Requires HTTPS; throttled as pairing."""
    try:
        origin, secure = _page_origin(request)
    except URLConfigurationError as exc:
        return web.Response(text=render(message_of(exc)), status=400, content_type="text/plain")
    store, source = request.app[AUTH], _source(request)
    if request.app[CONFIG].bridge_token or not store.setup_open or not origin or not secure:
        return await home(request, m('The setup QR code is available only over HTTPS while pairing is open.'), 400)
    blocked = store.retry_after(source)
    if blocked is not None:
        return await home(request, m('Too many incorrect setup codes.'), 429, {"Retry-After": str(blocked)})
    try:
        form = await _page_form(request)
        code = await store.check_setup(form.get("code"), source)
    except web.HTTPRequestEntityTooLarge:
        return await home(request, m('The request is too large.'), 413)
    except InvalidCode:
        return await home(request, m('Setup code is invalid or has expired.'), 401)
    except RateLimited as exc:
        return await home(request, m('Too many incorrect setup codes.'), 429, {"Retry-After": str(exc.retry_after)})
    fingerprint = request.app[TLS].fingerprint if origin.include_fingerprint else None
    identity = request.app[IDENTITY]
    body = invitation_page(identity=identity, link=pairing_url(origin, code, identity.name, fingerprint),
                           code=code, minutes=_setup_minutes(store) or 1, label=t('Pair Bambro'))
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
    for tray in trays_of(status):
        tray["spool"] = SpoolStore.public(loaded[tray["slot"]]) if tray["slot"] in loaded else None
    # New spools reset to Generic: flagged until their filament is set or a tag is read (newspool.py).
    if dev.new_spools is not None:
        dev.new_spools.annotate(status)
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


STATUS_GAP = 0.5   # Seconds between two status events of one stream.
EVENT_PING = 15.0  # Seconds of silence before a stream sends a comment to keep the connection open.


async def events(request: web.Request) -> web.StreamResponse:
    """Server-Sent Events: `status` on changes of this printer (at most every `STATUS_GAP`, the last change always
    follows), `job` for job changes, and a `: ping` comment after `EVENT_PING` seconds without events."""
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
            timeout = EVENT_PING if not pending_status else max(0.0, STATUS_GAP - (time.monotonic() - last_status))
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
                if time.monotonic() - last_status >= STATUS_GAP:
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
    if action == "error_action":
        return await _error_action(printer, status, value)
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
    if action == "read_rfid":
        # Reading takes about 20 seconds (the AMS turns the spool); fetch the result afterwards.
        _pushall_later(printer, (25, 60))
    return ok({"ok": True})


_background: set[asyncio.Task] = set()

# Right after an AMS setting the printer may still report the slot's previous filament for a few messages (Bambu
# Studio ignores the slot's next pushes), so the `pushall` sent at once can come too early: ask again shortly after.
AMS_SETTING_PUSHALL = (3, 10)


def _pushall_later(printer, delays: tuple[int, ...]) -> None:
    task = asyncio.create_task(_pushall_after(printer, delays))
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _pushall_after(printer, delays: tuple[int, ...]) -> None:
    waited = 0
    for delay in delays:
        await asyncio.sleep(delay - waited)
        waited = delay
        await printer.pushall(force=True)


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
        commands = controls.clear_error(status)
    except controls.ControlError as exc:
        return error(exc.status, exc.message)
    return await _answer_message(printer, *commands)


async def _error_action(printer: Printer, status: dict, value) -> web.Response:
    """Recovery action for the display message (Retry, Resume, Stop …), like a button in Bambu Studio's error
    dialog."""
    try:
        commands = controls.error_action(value, status)
    except controls.ControlError as exc:
        return error(exc.status, exc.message)
    return await _answer_message(printer, *commands)


async def _answer_message(printer: Printer, answer: controls.Command, close_dialog: controls.Command) -> web.Response:
    try:
        await printer.command(*answer)
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
    """Set AMS slot: {"slot": n, "filament": "<profile name>", "color": "#RRGGBB[AA]", "colors"?, "gradient"?}."""
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
        colors = body.get("colors")
        if colors is not None and not isinstance(colors, list):
            return error(422, m('Color as #RRGGBB.'))
        capabilities = status.get("printer", {}).get("capabilities") or {}
        section, command = controls.tray_setting(slot, info, str(color or ""), tray.get("source"), colors,
                                                 bool(body.get("gradient")), bool(capabilities.get("multi_color")))
        if dev.new_spools is not None:
            dev.new_spools.user_set(slot)  # Before sending: a new spool's Generic setting must not follow it.
        await printer.command(section, command)
    except ProfileError as exc:
        return error(422, message_of(exc))
    except controls.ControlError as exc:
        return error(exc.status, exc.message)
    except PrinterError as exc:
        return error(409, message_of(exc))
    await printer.pushall(force=True)
    _pushall_later(printer, AMS_SETTING_PUSHALL)
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
    _pushall_later(printer, AMS_SETTING_PUSHALL)
    return ok({"ok": True})


# --- Current print: objects ---

def _card_stem(path: str | None) -> str | None:
    """SD card file name without directory and `.gcode.3mf`, or None when `path` names no card file (empty, or the
    printer's unpacked `/data/Metadata/plate_N.gcode`)."""
    name = PurePosixPath(str(path or "").strip()).name
    if not name or re.fullmatch(r"plate_\d+\.gcode", name):
        return None
    lower = name.lower()
    suffix = next((s for s in (".gcode.3mf", ".3mf", ".gcode") if lower.endswith(s)), "")
    return name[:len(name) - len(suffix)] or None


def current_print_job(app: web.Application, dev: Device, job: dict) -> Job | None:
    """The bridge job of the running print (`status["job"]`), or None for prints from elsewhere.

    Display names are not unique (file stems, project names), so they count last:
    1. the SD card file the printer reports is the job's card file (bridge uploads carry the job ID in the name,
       imports keep their card path); the most recently sent job wins;
    2. the print this bridge started, until the printer leaves it (`Device.started_job`);
    3. the display name, only when the printer reports no card file, the bridge did not see this print start (for
       example after a restart) and exactly one job fits name, plate and subtask."""
    candidates = [j for j in app[JOBS].list() if j.sent and j.printer == dev.id]
    stem = _card_stem(job.get("file"))
    if stem:
        named = [j for j in candidates if _card_stem(j.sent.get("remote_name")) == stem]
        if named:
            return max(named, key=lambda j: j.sent.get("at") or 0)
    started = next((j for j in candidates if j.id == dev.started_job), None)
    if started is not None and started.name == job.get("name"):
        return started
    if stem or dev.start_seen:
        return None
    plate, subtask = job.get("plate_index"), job.get("subtask_id")
    named = [j for j in candidates if j.name == job.get("name")
             and not (plate and j.sent.get("plate") and j.sent["plate"] != plate)
             and not (subtask and j.sent.get("subtask_id") and j.sent["subtask_id"] != subtask)]
    return named[0] if len(named) == 1 else None


def _same_card_time(a: str | None, b: str | None) -> bool:
    """Compare SD card listing times. LIST shows the time only for recent files (`Nov 20 14:48`) and the date for older
    ones (`2025-05-20`), so a file that ages past that point keeps matching by date."""
    if a == b:
        return True
    first, second = iso_modified(a), iso_modified(b)
    if not first or not second:
        return False
    if "T" in first and "T" in second:
        return first == second
    return first[:10] == second[:10]


async def _card_entry(dev: Device, path: str) -> dict | None:
    """Listing entry of an SD card file (`cache/name.gcode.3mf`), or None when it is not there."""
    directory, _, name = ("/" + path.lstrip("/")).rpartition("/")
    listing = await asyncio.to_thread(ftps.list_dir, dev.host, dev.access_code, directory or "/",
                                      context=dev.trust.context())
    return next((e for e in listing if e["name"] == name and not e["is_dir"]), None)


def _card_unchanged(job: Job, entry: dict | None, local: Path) -> bool:
    """Is the SD card file still the one the bridge imported or uploaded (size, and listing time when recorded)?"""
    if entry is None or entry["size"] != local.stat().st_size:
        return False
    recorded = job.card_file or {}
    return "modified" not in recorded or _same_card_time(recorded["modified"], entry["modified"])


async def current_print_file(app: web.Application, dev: Device) -> tuple[Path, int, Job | None]:
    """Current print's 3MF, plate and bridge job (see `current_print_job`). Bridge prints use the job's file while it is
    what the printer runs; otherwise download the card file: the job's, or `<name>.gcode.3mf`, where OrcaSlicer
    and Bambu Studio store it."""
    status = dev.printer.status()
    job = status["job"]
    if not job["active"] or not job["name"]:
        raise JobError(m('No print is running.'))
    if job.get("calibration"):
        raise JobError(m('A calibration has no print file.'))
    if any(j.state == "sending" and j.name == job["name"] and j.printer == dev.id for j in app[JOBS].list()):
        # The printer already reports the bridge's print, but the bridge has not confirmed it yet.
        raise JobError(m('The print is still starting.'))
    plate = job.get("plate_index") or 1
    candidate = current_print_job(app, dev, job)
    remote = f"{job['name']}.gcode.3mf"
    if candidate is not None:
        plate = candidate.sent.get("plate") or plate
        local = app[JOBS].output_path(candidate)
        remote = candidate.sent.get("remote_name") or remote
        # Sliced again after sending: the card still holds what the printer runs.
        fresh = local.exists() and local.stat().st_mtime <= (candidate.sent.get("at") or 0)
        if fresh and (not candidate.remote_path or dev.started_job == candidate.id):
            return local, plate, candidate
        if fresh:
            # Imported from the card: another slicer may have replaced the file under the same name since.
            async with dev.print_file_lock:
                entry = await _card_entry(dev, remote)
            if _card_unchanged(candidate, entry, local):
                return local, plate, candidate
    return await _cached_card_file(app, dev, remote), plate, candidate


async def _cached_card_file(app: web.Application, dev: Device, remote: str) -> Path:
    """Download an SD card file into `printcache` once per name, size and listing time."""
    config = app[CONFIG]
    limit = config.max_upload_mb * 1024 * 1024
    # Thumbnail, preview and object requests often overlap: use one printer session and one download.
    async with dev.print_file_lock:
        entry = await _card_entry(dev, remote)
        if entry is None:
            raise JobError(m('“{p0}” is not on the SD card.', p0=remote))
        if entry["size"] > limit:
            raise JobError(m('File is larger than {p0} MB.', p0=config.max_upload_mb))
        cache = config.data_dir / "printcache"
        cache.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha1(f"{dev.id}/{remote}/{entry['modified']}".encode()).hexdigest()[:16]
        local = cache / f"{key}-{entry['size']}.gcode.3mf"
        if not local.exists():
            def fetch() -> None:
                ftps.download(dev.host, dev.access_code, "/" + remote.lstrip("/"), local, context=dev.trust.context(),
                              limit=limit)
            try:
                try:
                    await asyncio.to_thread(fetch)
                except (OSError, EOFError):
                    # At print start, busy printers often drop FTP connections; retry once.
                    await asyncio.sleep(2)
                    await asyncio.to_thread(fetch)
            except ftps.TooLarge:
                raise JobError(m('File is larger than {p0} MB.', p0=config.max_upload_mb)) from None
            _prune_print_cache(cache, keep=3 * len(app[DEVICES].devices))
    return local


def _prune_print_cache(cache: Path, keep: int) -> None:
    """Keep the newest downloaded print files; remove older ones together with their previews."""
    for old in sorted(cache.glob("*.gcode.3mf"), key=lambda f: f.stat().st_mtime)[:-keep]:
        old.unlink(missing_ok=True)
    for sidecar in cache.glob("*.opv"):
        if not sidecar.with_name(sidecar.name.rsplit("-plate", 1)[0] + ".3mf").exists():
            sidecar.unlink(missing_ok=True)


async def current_objects(app: web.Application, dev: Device) -> dict:
    path, plate, _ = await current_print_file(app, dev)
    objects = await asyncio.to_thread(read_objects, path, plate)
    skipped = set(dev.printer.status()["job"].get("skipped_objects") or [])
    for obj in objects:
        obj["skipped"] = obj["skipped"] or obj["id"] in skipped
    return {"plate": plate, "bed": (await catalog_of(dev)).bed_size(), "objects": objects}


async def print_thumbnail(request: web.Request) -> web.Response:
    """Current plate thumbnail, including prints from OrcaSlicer or Bambu Studio."""
    try:
        path, plate, _ = await current_print_file(request.app, device(request))
        data = await asyncio.to_thread(_read_thumbnail, path, plate)
    except (JobError, OSError, ValueError, KeyError) as exc:
        return error(404, m('No thumbnail: {p0}', p0=exc))
    # One URL per printer whose picture changes with every print: never cache it.
    return web.Response(body=data, content_type="image/png", headers={"Cache-Control": "no-store"})


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
    store, dev, config = request.app[JOBS], device(request), request.app[CONFIG]
    limit = config.max_upload_mb * 1024 * 1024

    def too_large() -> web.Response:
        return error(422, m('File is larger than {p0} MB.', p0=config.max_upload_mb))

    remote = f"{directory.rstrip('/')}/{name}"
    try:
        entry = await _card_entry(dev, remote)
    except Exception as exc:  # noqa: BLE001 - FTPS, TLS, certificate errors
        return error(502, m('Could not import: {p0}', p0=exc))
    if entry is None:
        return error(404, m('“{p0}” is not on the SD card.', p0=remote.lstrip("/")))
    if entry["size"] > limit:
        return too_large()
    job = store.create(name, dev.id)
    local = store.input_path(job)
    try:
        await asyncio.to_thread(ftps.download, dev.host, dev.access_code, remote, local, context=dev.trust.context(),
                                limit=limit)
        if local.stat().st_size != entry["size"]:
            raise JobError(m('The file changed on the SD card while it was imported. Import it again.'))
        job.result = {**await asyncio.to_thread(read_slice_info, local), "warnings": []}
        job.result["plate"] = job.result["plates"][0]["index"]
    except ftps.TooLarge:
        store.delete(job.id)
        return too_large()
    except Exception as exc:  # noqa: BLE001
        store.delete(job.id)
        return error(502, m('Could not import: {p0}', p0=exc))
    # The job prints the card file in place; record it so a replaced file is not printed unnoticed.
    job.card_file = {"size": entry["size"], "modified": entry["modified"]}
    job.state, job.remote_path = "sliced", remote.lstrip("/")
    _publish_job(request.app, job)
    store.prune()
    return ok(job.public(), status=201)


class _TransferStopped(Exception):
    """The app canceled the download, or the bridge stopped sending a print: stop the FTPS thread."""


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
        jobs = [j for j in jobs if j.printer == dev.id]
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


UPLOADS = web.AppKey("uploads", asyncio.Semaphore)


async def limited_upload(request: web.Request) -> web.Response:
    """At most MAX_UPLOADS uploads at once: each holds disk space and a thread that checks the file."""
    slots = request.app[UPLOADS]
    if slots.locked():
        return error(429, m('Another upload is still in progress. Try again when it has finished.'))
    async with slots:
        return await upload_job(request)


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
    """Start slicing. Body: {slot | filament, quality, plate, support, support_angle, support_on_plate, infill, walls,
    brim, orient, plate_index}."""
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
            options = {k: body[k] for k in ("plate", "support", "support_angle", "support_on_plate", "infill", "walls",
                                            "brim") if k in body}
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
    job.sent_plates, job.uploaded, job.card_file = [], None, None
    _publish_job(request.app, job)
    task = asyncio.create_task(_run_slice(request.app, job))
    request.app[TASKS].add(task)
    task.add_done_callback(request.app[TASKS].discard)
    return ok(job.public(), status=202)


def _find_tray(status: dict, slot) -> dict | None:
    """The slot `slot` when it holds filament (a number, not `true`), else None."""
    tray = controls.find_tray(status, slot)
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
        # Previews of the previous slice result (`.opv` from older bridge versions' current print preview).
        for cache in (*store.dir(job.id).glob("preview*.bin"), *store.dir(job.id).glob("*.opv")):
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
    async with app[JOBS].preview_lock(job.id):
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
        path, plate, job = await current_print_file(request.app, dev)
        if job is not None and job.result and path == request.app[JOBS].output_path(job):
            # The job's own file: share the job preview cache, which slicing again clears.
            return _binary(await job_preview_data(request.app, job, plate))
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
    # Slicing again changes the picture under the same URL.
    return web.Response(body=data, content_type="image/png", headers={"Cache-Control": "no-store"})


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
START_POLL = 1.0           # Seconds between status checks while waiting for that.
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
    if dev.print_lock.locked() or any(j.state == "sending" and j.printer == dev.id for j in app[JOBS].list()):
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
    if status["job"]["state"] not in controls.IDLE:
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
        if not str(key).isdigit() or not isinstance(slot, int) or isinstance(slot, bool):
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
        target = await asyncio.to_thread(sliced_for, app[JOBS].output_path(job), plate_index)
    except (JobError, OSError) as exc:
        raise PrintRefused(m('Cannot read file: {p0}', p0=message_of(exc)), 422) from None
    if refused := _sliced_for_other_printer(dev, status, model_id, target):
        raise PrintRefused(refused)
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


def _sliced_for_other_printer(dev: Device, status: dict, model_id: str, target: dict) -> str | None:
    """Refuse a file sliced for another printer model or nozzle, as the app's direct mode does (`DirectPrint.plan`):
    its start G-code, bed size and extrusion are made for that printer. `target` comes from `sliced_for`."""
    name, raw_id = target.get("model"), target.get("model_id")
    record = (dev.models.find(name) if name else None) or (dev.models.find(model_id=raw_id) if raw_id else None)
    if record is not None:
        wanted, label = record.model_id, record.name
    elif name:
        wanted, label = model_id_from_name(name) or model_id_from_name(f"Bambu Lab {name}"), name
    else:
        wanted = raw_id.removesuffix("-V2") if raw_id else None
        label = next((n for n, i in MODEL_IDS_BY_NAME.items() if i == wanted), raw_id)
    own = dev.models.find(model_id=model_id)
    printer_name = (own.name if own else None) or status["printer"].get("model") or dev.model or model_id
    if label and wanted != model_id and not (record and own and record.name == own.name):
        # Also a model the bridge does not know: then it is not this printer, whose model is known here.
        return m('The file was sliced for {p0}, but this printer is a {p1}. Please slice it again for this printer.',
                 p0=label, p1=printer_name)
    wanted_nozzle, installed = target.get("nozzle"), (status.get("nozzle") or {}).get("diameter")
    if wanted_nozzle and installed and abs(wanted_nozzle - installed) > 0.01:
        return m('The file was sliced for a {p0} mm nozzle, but a {p1} mm nozzle is installed.',
                 p0=f"{wanted_nozzle:g}", p1=f"{installed:g}")
    return None


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
            dev.started_job = job.id  # The running print is this job until the printer leaves it.
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
    if state["state"] not in controls.IDLE:
        raise PrintRefused(m('The printer is busy.'))

    if job.remote_path:
        # Imported from the card and printed in place: only if it is still the file the owner imported.
        _set_step(app, job, m('Checking the file on the SD card'))
        entry = await _card_entry(dev, job.remote_path)
        if entry is None:
            raise PrintRefused(m('“{p0}” is not on the SD card.', p0=job.remote_path))
        if not _card_unchanged(job, entry, app[JOBS].output_path(job)):
            raise PrintRefused(m('The file on the SD card has changed since it was imported. Import it again to print it.'))

    if not job.remote_path and not plan.get("uploaded") and job.uploaded == name:
        # Another plate of the same slice result: reuse the SD card file if size and listing time still match.
        _set_step(app, job, m('Checking the file on the SD card'))
        plan["uploaded"] = _card_unchanged(job, await _card_entry(dev, name), app[JOBS].output_path(job))

    if not job.remote_path and not plan.get("uploaded"):
        local = app[JOBS].output_path(job)
        loop = asyncio.get_running_loop()
        last = [0.0]
        stop = threading.Event()

        def progress(sent: int, total: int) -> None:
            if stop.is_set():
                raise _TransferStopped  # Sending was canceled (shutdown): end the upload thread too.
            now = time.monotonic()
            if now - last[0] > 0.5 or sent == total:
                last[0] = now
                job.progress = round(sent / total, 3)
                loop.call_soon_threadsafe(app[EVENTS].publish, "job", job.public())

        _set_step(app, job, m('Uploading to the SD card'), 0.0)
        try:
            await asyncio.to_thread(ftps.upload, dev.host, dev.access_code, local, name, progress,
                                    context=dev.trust.context())
        finally:
            stop.set()
        _set_step(app, job, m('Checking the file on the SD card'))
        entry = await _card_entry(dev, name)
        size = entry["size"] if entry else None
        if size != local.stat().st_size:
            raise RuntimeError(m('File on the SD card is incomplete ({p0} of {p1} bytes)', p0=size, p1=local.stat().st_size))
        plan["uploaded"] = True
        job.uploaded, job.card_file = name, {"size": entry["size"], "modified": entry["modified"]}
        app[JOBS].save(job)

    _set_step(app, job, m('Starting the print'), None)
    command = build_project_file(plan.get("model_id") or dev.model_id, job.name, plan,
                                 print_url=config.print_url,
                                 capabilities=printer.status().get("printer", {}).get("capabilities"))
    with _StartWatch(printer) as watch:
        reply = await printer.command("print", command, wait=15)
        if reply and str(reply.get("result", "")).lower() not in ("", "success", "ok"):
            raise PrintRefused(m('The printer rejected the request: {p0}', p0=reply.get('reason') or reply.get('result')))

        _set_step(app, job, m('Waiting for the print to start'))
        deadline = time.monotonic() + START_CONFIRM_WAIT
        while time.monotonic() < deadline:
            state = printer.status()["job"]
            if state["state"] in STARTED_STATES:
                subtask = state.get("subtask_id")
                if subtask not in (None, "", "0") and subtask != watch.before["subtask_id"]:
                    plan["subtask_id"] = subtask  # Identifies this print later, where the firmware sets one.
                return reply
            if code := watch.failure(state):
                raise PrintRefused(m('The printer reports an error (code {p0:08X}).', p0=code))
            await asyncio.sleep(START_POLL)
    raise RuntimeError(m('The printer did not confirm the print start'))


class _StartWatch:
    """Printer state changes after the print command, to tell a rejected start from the previous print's state.

    FAILED counts as not printing, so a print may start while the printer still reports the previous print's
    FAILED state and `print_error` (0300400C after a stop, until acknowledged). The reply to `project_file` often
    arrives before the first status of the new job, so only an error that appears after the command counts:
    `print_error` changed, or the printer left FAILED and returned to it."""

    def __init__(self, printer: Printer):
        self.printer = printer
        job = printer.status()["job"]
        self.before = {"print_error": job.get("print_error"), "subtask_id": job.get("subtask_id")}
        self.changed = job["state"] != "FAILED"
        self._stop = None

    def __enter__(self) -> "_StartWatch":
        self._stop = self.printer.subscribe(self._observe)
        return self

    def __exit__(self, *_exc) -> None:
        self._stop()

    def _observe(self) -> None:
        job = self.printer.status()["job"]
        if job["state"] != "FAILED" or job.get("print_error") != self.before["print_error"]:
            self.changed = True

    def failure(self, job: dict) -> int | None:
        """The error code when the printer reports a failure of this start, else None."""
        if job["state"] == "FAILED" and job.get("print_error") and self.changed:
            return job["print_error"]
        return None


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
        if plan.get("subtask_id"):
            job.sent["subtask_id"] = plan["subtask_id"]
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
    status = 422 if exc.code in ("access_code", "not_bambu", "not_local") else 409 if exc.code in (
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


# --- Filament library ---------------------------------------------------------------------------------------------

# Spool plan retries per print: the SpoolTracker asks on every status update until it has a plan, and each attempt
# may cost an FTPS session on the printer.
PLAN_RETRY_FIRST = 30.0
PLAN_RETRY_MAX = 600.0
PLAN_ATTEMPTS = 10


async def spool_plan(app: web.Application, dev: Device) -> Plan:
    """Filaments of the printed plate and, for prints sent by the bridge, the filament → slot mapping.

    Both come from the same file and job (`current_print_file`). After a failure, wait 30 s, doubling up to
    10 minutes, and give up after 10 attempts for this print."""
    job = dev.printer.status()["job"]
    if job.get("calibration"):
        return [], None  # No file and nothing to book.
    key = f"{job.get('name')}|{job.get('file')}"
    retry = dev.plan_retry if dev.plan_retry and dev.plan_retry["key"] == key else None
    if retry and (retry["attempts"] >= PLAN_ATTEMPTS or time.monotonic() < retry["at"]):
        raise JobError(m('The print file is not available yet; trying again later.'))
    try:
        path, plate, source = await current_print_file(app, dev)
        info = await asyncio.to_thread(read_slice_info, path)
    except Exception as exc:
        attempts = retry["attempts"] + 1 if retry else 1
        dev.plan_retry = {"key": key, "attempts": attempts,
                          "at": time.monotonic() + min(PLAN_RETRY_FIRST * 2 ** (attempts - 1), PLAN_RETRY_MAX)}
        if attempts == 1:
            log.info(m('No filament plan for print %s yet: %s'), job.get("name"), message_of(exc) or type(exc).__name__)
        elif attempts == PLAN_ATTEMPTS:
            log.info(m('No filament plan for print %s; filament use is not booked.'), job.get("name"))
        raise
    dev.plan_retry = None
    plates = info.get("plates") or []
    chosen = next((p for p in plates if p["index"] == plate), plates[0] if plates else None)
    filaments = chosen["filaments"] if chosen else []
    mapping = None
    if source is not None:
        slots = source.sent.get("ams_mapping") or []
        # ams_mapping lists one slot per project filament (ID = position + 1); -1 = not used.
        mapping = {i + 1: slot for i, slot in enumerate(slots) if isinstance(slot, int) and slot >= 0} or None
    return filaments, mapping


def _spool_reply(spool: dict | None, outcome: dict) -> dict:
    reply = {"spool": SpoolStore.public(spool) if spool else None, **outcome}
    if "deleted" in outcome:
        reply["deleted"] = SpoolStore.public({**outcome["deleted"], "id": outcome["deleted"]["id"]})
    return reply


async def _spool_body(request: web.Request) -> dict | web.Response:
    try:
        body = await request.json()
    except ValueError:
        return error(400, m('Invalid JSON.'))
    return body if isinstance(body, dict) else error(400, m('Invalid JSON.'))


def _tag_spool(request: web.Request, body: dict, printer: str | None, slot: int | None) -> web.Response:
    """A tag read (`load`) or about to be written (`write`), for a slot or (`slot` None) in the library alone."""
    uid, action, choice = body.get("uid"), body.get("action", "load"), body.get("choice", "auto")
    if action not in ("load", "write") or choice not in ("auto", "restore", "new") or not isinstance(uid, str):
        return error(422, m('Invalid spool request.'))
    info = {key: body.get(key) for key in TAG_FIELDS}
    store = request.app[SPOOLS]
    try:
        if action == "write":
            spool, outcome = store.written(printer, slot, uid, weight=body.get("weight"),
                                           remaining=body.get("remaining"), info=info)
        else:
            spool, outcome = store.load(printer, slot, uid, weight=body.get("weight"), remaining=body.get("remaining"),
                                        info=info, choice=choice)
    except (TypeError, ValueError):
        return error(422, m('Invalid spool data (tag ID or amount).'))
    # The printer whose slot shows the spool: the one it was read for, or where a known spool sits.
    shown = printer or (spool or {}).get("printer")
    if shown:
        request.app[EVENTS].publish("status", {"printer": shown})
    return ok(_spool_reply(spool, outcome))


async def load_spool(request: web.Request) -> web.Response:
    """A spool with an NFC tag in a slot: `{"slot", "uid", "action": "load"|"write", "choice": "auto"|"restore"|"new",
    "weight", "remaining", "type", "brand", "subtype", "color", "nozzle_temp_min", "nozzle_temp_max"}`.

    `load` (tag read): known spools keep the counted amount; for a deleted spool the reply may be
    `{"decision": "choose", "suggest": "new"|"restore", "deleted": {…}}` and the app asks again with a choice.
    `write` (tag about to be written): same filament keeps the amount, otherwise a new spool that starts with
    `remaining`, else full. The reply's `spool.remaining` is what the app writes to the tag."""
    body = await _spool_body(request)
    if isinstance(body, web.Response):
        return body
    dev = device(request)
    slot = body.get("slot")
    if not isinstance(slot, int) or controls.find_tray(dev.printer.status(), slot) is None:
        return error(422, m('Slot not found.'))
    response = _tag_spool(request, body, dev.id, slot)
    if response.status == 200 and dev.new_spools is not None:
        dev.new_spools.user_set(slot)  # A tag read for the slot: no Generic setting, and the new spool hint ends.
    return response


async def add_spool(request: web.Request) -> web.Response:
    """A spool with an NFC tag added in the filament library, without a slot: the body of `POST /api/ams/spool`
    without `slot`, with the same decisions. A new spool stays unloaded; a known one stays where it is."""
    body = await _spool_body(request)
    if isinstance(body, web.Response):
        return body
    return _tag_spool(request, body, None, None)


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
    """`{"note": text, "unload": true, "type", "brand", "subtype", "color", "nozzle_temp_min", "nozzle_temp_max",
    "weight", "remaining": grams, "override_remaining": true}`, all optional.

    Tag fields and `weight` (full spool) follow a rewritten tag of a tagged spool: a field sent as null or "" is
    cleared, one left out stays. `remaining` replaces the counted amount and needs `override_remaining: true`, so an
    edit never resets the counting by accident; `remaining` sent alone (older apps) counts as an override. Bambu spools take only a note and unloading. Nothing changes when
    any field is invalid."""
    body = await _spool_body(request)
    if isinstance(body, web.Response):
        return body
    note, unload, override = body.get("note"), body.get("unload", False), body.get("override_remaining", False)
    if not (note is None or isinstance(note, str)) or not isinstance(unload, bool) or not isinstance(override, bool):
        return error(422, m('Invalid spool data (tag ID or amount).'))
    # Bambro before 0.9.45 corrects the amount with `remaining` alone ("Adjust Amount"): still an explicit override.
    if "override_remaining" not in body and "remaining" in body and not any(key in body for key in (*TAG_FIELDS, "weight")):
        override = True
    try:
        spool = request.app[SPOOLS].update(request.match_info["spool_id"], remaining=body.get("remaining"),
                                           override_remaining=override, note=note, unload=unload,
                                           info={key: body[key] for key in TAG_FIELDS if key in body},
                                           weight=body.get("weight"))
    except BambuAmount:
        return error(422, m('The AMS reports the amount of Bambu spools; it cannot be changed here.'))
    except BambuFilament:
        return error(422, m('The AMS reports the filament of Bambu spools; it cannot be changed here.'))
    except CountedAmount:
        return error(422, m('A new remaining amount replaces the amount the bridge counted; send override_remaining: true with it.'))
    except (TypeError, ValueError):
        return error(422, m('Invalid spool data (tag ID or amount).'))
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


# --- Live Activity ---

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
    "environment": "development"|"production", "activity_id": "…", "printer": "<printerId>",
    "installation": "…", "events": [...], "language": "en"|"de"}. Start tokens apply to all printers;
    update tokens belong to one printer's Live Activity (the activity's `printerId` attribute; the default printer
    if omitted); alert tokens receive the requested notification events (an empty list unregisters). Language
    defaults to English."""
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
        # The activity's `printerId` attribute (`public_printer_id`), or the default printer.
        if printer_id is None:
            printer_id = request.app[DEVICES].default_id
        else:
            printer_id = next((serial for serial in devices if public_printer_id(serial) == printer_id), None)
        if printer_id not in devices:
            return error(422, m('Printer not found.'))
    installation = body.get("installation")
    if installation is not None and not (isinstance(installation, str) and INSTALLATION.fullmatch(installation)):
        return error(422, m('Invalid installation ID.'))
    principal = request[PRINCIPAL]
    added = request.app[PUSH].add(kind, token.lower(), environment, activity_id if isinstance(activity_id, str) else None,
                                  printer_id or "", device=principal.device_id or SHARED_DEVICE,
                                  installation=installation.upper() if installation else None,
                                  events=list(dict.fromkeys(events)) if kind == "alert" else None, language=language)
    if not added:
        return error(409, m('This push token is registered by another paired device.'))
    return ok({"registered": kind})


async def live_unregister(request: web.Request) -> web.Response:
    """`DELETE /api/live/register?installation=<ID>`: remove the start, update and alert registrations this
    installation made with the calling credential (its device, or the shared access key), for example when the app
    leaves this bridge. Returns `{"removed": n}`."""
    installation = request.query.get("installation", "")
    if not INSTALLATION.fullmatch(installation):
        return error(422, m('Invalid installation ID.'))
    principal = request[PRINCIPAL]
    removed = request.app[PUSH].remove_installation(principal.device_id or SHARED_DEVICE, installation.upper())
    return ok({"removed": removed})


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

ENV_RETRY = 30.0  # Seconds between attempts to read the environment printer's serial number.


def env_settings(config: Config) -> PrinterSettings | None:
    if not config.host or not config.access_code:
        return None
    return PrinterSettings(serial=config.serial or "", host=config.host, access_code=config.access_code,
                           name=config.name, machine_profile=config.machine_profile, source="env")


async def _add_env_printer(app: web.Application, settings: PrinterSettings) -> None:
    """Environment printer not enrolled yet (no `BAMBU_SERIAL`, first start or new access code): read serial
    number and certificate from the printer, retrying until it answers, and enroll both (`PrinterStore.resolve_env`)."""
    while True:
        try:
            settings.serial, seen = await read_identity(settings.host)
            break
        except ProbeError as exc:
            log.warning(m('Printer from BAMBU_HOST: %s Retrying in %.0f s.'), exc, ENV_RETRY)
            await asyncio.sleep(ENV_RETRY)
    devices = app[DEVICES]
    devices.store.enroll_env(settings.serial, settings.access_code, fingerprint=seen,
                             renew=devices.store.env is not None)
    await devices.add_env(settings)
    assign_unowned_jobs(app)


def create_app(config: Config, *, printer: Printer | None = None, camera: Camera | None = None,
               catalog: Catalog | None = None, live: LiveActivities | None = None, probe_fn=None,
               identity: BridgeIdentity | None = None, tls_state: TLSState | None = None,
               pairing_store: PairingStore | None = None, start_background: bool = True) -> web.Application:
    # Model uploads read their multipart stream with their own limit (`upload_job`); all other bodies are small.
    app = web.Application(middlewares=[language_middleware, transport_middleware, auth_middleware],
                          client_max_size=JSON_BODY_LIMIT)
    app[CONFIG] = config
    app[IDENTITY] = identity or load_identity(config.auth_dir / "bridge.json", config.bridge_name)
    app[TLS] = tls_state or TLSState(None, None)
    app[AUTH] = pairing_store or PairingStore(config.auth_dir / "auth.json", config.bridge_token)
    catalogs = Catalogs(config.profiles_dir, config.filament_ids)
    if catalog is not None:
        catalogs.put(catalog)
    app[CATALOGS] = catalogs
    devices = Devices(PrinterStore(config.data_dir / "printers.json"), catalogs, Models(config.models_dir),
                      bridge_id=app[IDENTITY].id)
    app[DEVICES] = devices
    app[SLICER] = Slicer(config.orca_cmd, config.slice_timeout)
    app[JOBS] = JobStore(config.data_dir, config.keep_jobs)
    app[EVENTS] = EventHub()
    app[TASKS] = set()
    app[UPLOADS] = asyncio.Semaphore(MAX_UPLOADS)
    app[HMS] = Texts(config.hms_dir)
    app[PROBE] = probe_fn or probe
    apns = APNsConfig.load(config.apns_key_file, config.apns_key_id, config.apns_team_id, config.apns_bundle_id)
    # Use an APNs key when configured, otherwise the relay; BRIDGE_PUSH_RELAY=off disables pushes.
    app[APNS] = APNsClient(apns) if apns else (
        RelayClient(config.push_relay, config.data_dir / "relay.json") if config.push_relay else None)
    app[PUSH] = live.registry if live is not None else Registry(config.data_dir / "push.json")
    # Pushes only reach devices that are still paired, and shared-key registrations only the current key.
    app[PUSH].bind(lambda device_id, auth=app[AUTH]: any(d.id == device_id for d in auth.devices),
                   app[AUTH].legacy_secret)
    app[SPOOLS] = SpoolStore(config.data_dir / "spools.json")
    warming: set[str] = set()

    def changed(dev: Device) -> None:
        app[EVENTS].publish("status", {"printer": dev.id})
        if dev.live is not None:
            dev.live.observe(described_status(app, dev))
        if dev.spools is not None:
            dev.spools.observe(dev.printer.status())
        if dev.new_spools is not None:
            dev.new_spools.observe()
        machine = dev.machine_profile
        # Load the catalog once model and nozzle are known (status then includes flow calibration support).
        if machine and catalogs.cached(machine) is None and machine not in warming:
            warming.add(machine)
            task = asyncio.get_running_loop().create_task(catalogs.get(machine))
            app[TASKS].add(task)
            # Catalogs.get logs a failure once and retries it later.
            task.add_done_callback(lambda t, m=machine: (warming.discard(m), app[TASKS].discard(t),
                                                         t.cancelled() or t.exception()))

    def wire(dev: Device) -> None:
        if dev.live is None:
            first = not any(d.live for d in devices.devices.values() if d is not dev)
            dev.live = live if (live is not None and first) else LiveActivities(
                app[APNS], app[PUSH], lambda pid=dev.id: devices.devices[pid].name if pid in devices.devices else m('Printer'),
                dev.id)
        if dev.spools is None:
            dev.spools = SpoolTracker(app[SPOOLS], dev.id, lambda d=dev: spool_plan(app, d),
                                      lambda d=dev: app[EVENTS].publish("status", {"printer": d.id}))
        if dev.new_spools is None:
            def reset(d: Device = dev) -> None:
                # A Generic setting went out: show the flag, and ask again later like after `/api/ams/tray`.
                app[EVENTS].publish("status", {"printer": d.id})
                _pushall_later(d.printer, AMS_SETTING_PUSHALL)
            dev.new_spools = NewSpoolWatcher(
                dev.printer, dev.catalog, enabled=config.reset_new_spools,
                placed=lambda slot, d=dev: (app[SPOOLS].at(d.id, slot) or {}).get("placed"), changed=reset)
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
        if env is not None:
            env.serial = devices.store.resolve_env(env) or ""
            if not env.serial:
                pending_env, env = env, None
        devices.load(env)
    assign_unowned_jobs(app)

    app.router.add_get("/", home)
    app.router.add_post("/invite", web_invite)
    app.router.add_post("/setup", web_setup)
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
    app.router.add_post("/api/printers/{printer_id}/jobs", limited_upload)
    app.router.add_post("/api/live/register", live_register)
    app.router.add_delete("/api/live/register", live_unregister)
    app.router.add_get("/api/spools", list_spools)
    app.router.add_post("/api/spools", add_spool)
    app.router.add_patch("/api/spools/{spool_id}", update_spool)
    app.router.add_delete("/api/spools/{spool_id}", delete_spool)
    app.router.add_get("/api/jobs", list_jobs)
    app.router.add_post("/api/jobs", limited_upload)
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
                log.warning(m('BRIDGE_PLAIN_HTTP=on: The API, pairing codes and tokens travel over plain HTTP and every device on the network can reach them. Behind a reverse proxy, set BRIDGE_TRUSTED_PROXIES instead.'))
            log.info("Live Activity: %s", m('own APNs key') if apns else
                     m('via relay {p0}', p0=config.push_relay) if app[APNS] else "off")
            log.info("Error texts: %s", "loaded" if app[HMS].available else f"missing in {config.hms_dir}")
            log.info(m('Bridge %s (ID %s), paired devices: %d'), app[IDENTITY].name, app[IDENTITY].id,
                     len(app[AUTH].devices))
            if tls.enabled and tls.fingerprint:
                # Bambro shows the same groups before sending a code to an address typed in or found on the network.
                log.info(m('Certificate fingerprint: %s'), grouped_fingerprint(tls.fingerprint))
            if app[AUTH].setup_open:
                log.info(m('Setup code for the first device: %s (valid for %d minutes; restart the bridge for a new one)'),
                         display_code(app[AUTH].setup_code), (app[AUTH].setup_remaining + 59) // 60)
            elif config.bridge_token:
                log.info(m('Pairing: Fixed access key or an invitation from a paired device'))
            resume_pending_prints(app)
            yield
            # Background work first (slicing, print sending, the environment printer, status refreshes), while the
            # printers, APNs and pairing it may use are still open; accepted prints resume after the restart.
            pending = [task for task in (env_task, *app[TASKS], *_background) if task is not None]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await devices.stop()
            if app[APNS]:
                await app[APNS].close()
            await app[AUTH].close()
            if advertiser:
                await advertiser.stop()
        app.cleanup_ctx.append(background)
    else:
        async def pairing_cleanup(app: web.Application):
            yield
            await app[AUTH].close()
        app.cleanup_ctx.append(pairing_cleanup)
    return app
