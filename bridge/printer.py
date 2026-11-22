"""LAN printer MQTT connection (port 8883, TLS, user `bblp`, password = access code).

Keep one connection and distribute status to all app clients. Send commands only on explicit
request; never automatically repeat them."""
from __future__ import annotations

from .i18n import m, message_of

import asyncio
import itertools
import json
import logging
import secrets
import time
from collections.abc import Callable

import aiomqtt

from .models import Model, Models
from .state import merge, normalize
from .trust import CHANGED, PrinterTrust

log = logging.getLogger(__name__)

# P1 printers have limited resources; request full `pushall` status only every few minutes.
PUSHALL_INTERVAL = 300
PUSHALL_MIN_GAP = 30
# Stands in for the bridge ID where a printer is created without one (tests, tools).
_INSTANCE = secrets.token_hex(4)


def client_id(serial: str, bridge_id: str | None = None) -> str:
    """MQTT client ID, at most 23 characters: the same for one bridge and printer, so a reconnect replaces the
    bridge's own stale session, and different for every other bridge, which would otherwise push this one off."""
    return f"orcbro-{(bridge_id or _INSTANCE)[:8]}-{serial[-6:]}"


class PrinterError(Exception):
    """Command failure whose readable message is returned to the app."""


class Printer:
    def __init__(self, host: str, serial: str, access_code: str, name: str = "Bambu Lab", *,
                 models: Models | None = None, model_hint: str | None = None, trust: PrinterTrust | None = None,
                 bridge_id: str | None = None):
        self.host = host
        self.serial = serial
        self.bridge_id = bridge_id
        # Pinned certificate: do not log in unless the fingerprint matches (trust.py).
        self.trust = trust or PrinterTrust(serial)
        self._code = access_code
        self.name = name
        self.models = models
        self.model_hint = model_hint
        self.raw: dict = {}
        self.version: dict = {}
        self.connected = False
        self.last_message_at: float | None = None
        self.last_error: str | None = None
        self.revision = 0
        self._client: aiomqtt.Client | None = None
        self._listeners: list[Callable[[], None]] = []
        self._pending: dict[str, asyncio.Future] = {}
        self._seq = itertools.count(int(time.time()) % 100000 * 10)
        self._last_pushall = 0.0

    # --- Status ---

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback) if callback in self._listeners else None

    def _changed(self) -> None:
        self.revision += 1
        for callback in list(self._listeners):
            try:
                callback()
            except Exception:  # noqa: BLE001 - a broken listener must not disrupt the connection
                log.exception(m('Listener failed'))

    def status(self) -> dict:
        ota = next((m for m in self.version.get("module", []) if m.get("name") == "ota"), {})
        firmware = ota.get("sw_ver") or next((m.get("sw_ver") for m in self.version.get("module", [])
                                               if m.get("name") in ("rv1126", "esp32")), None)
        model = self.model_record()
        printer = {
            "name": self.name,
            "serial": self.serial,
            "model": model.name if model else (ota.get("product_name") or self.model_hint),
            "model_id": model.model_id if model else None,
            "firmware": firmware,
            "ams_firmware": [m.get("sw_ver") for m in self.version.get("module", []) if str(m.get("name", "")).startswith("ams/")],
        }
        printer["capabilities"] = model.capabilities(firmware, self.raw) if model else {}
        out = normalize(self.raw, connected=self.connected, last_message_at=self.last_message_at, printer=printer)
        out["connection"]["error"] = None if self.connected else self.last_error
        out["revision"] = self.revision
        return out

    def model_record(self) -> Model | None:
        """Canonical model from protocol ID, version report or setup hint."""
        if self.models is None:
            return None
        ota = next((m for m in self.version.get("module", []) if m.get("name") == "ota"), {})
        project = next((m.get("project_name") for m in self.version.get("module", []) if m.get("project_name")), None)
        return (self.models.find(model_id=self.raw.get("printer_type")) or self.models.find(model_id=project) or
                self.models.find(ota.get("product_name")) or self.models.find(self.model_hint))

    def handle(self, payload: dict) -> None:
        """Handle a printer message, also usable by network-free tests."""
        self.last_message_at = time.time()
        changed = False
        for section in ("print", "info", "system"):
            body = payload.get(section)
            if not isinstance(body, dict):
                continue
            seq = str(body.get("sequence_id", ""))
            future = self._pending.pop(seq, None) if body.get("command") != "push_status" else None
            if future and not future.done():
                future.set_result(body)
            if section == "print" and body.get("command") == "push_status":
                # `msg` 0 marks a full report (the `pushall` reply), 1 a partial update.
                merge(self.raw, {k: v for k, v in body.items() if k not in ("command", "sequence_id", "msg")},
                      full=body.get("msg") == 0)
                changed = True
            elif section == "info" and body.get("command") == "get_version":
                self.version = body
                changed = True
        if changed:
            self._changed()

    # --- Connection ---

    async def run(self) -> None:
        """Maintain the connection and reconnect with exponential backoff after failure."""
        backoff = 2
        context = self.trust.context()
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self.host, port=8883, username="bblp", password=self._code,
                    tls_context=context, identifier=client_id(self.serial, self.bridge_id), keepalive=30, timeout=15,
                ) as client:
                    self._client = client
                    self.connected = True
                    self.last_error = None
                    backoff = 2
                    log.info(m('Connected to printer %s'), self.host)
                    await client.subscribe(f"device/{self.serial}/report")
                    await self._publish({"info": {"sequence_id": self._next(), "command": "get_version"}})
                    await self.pushall(force=True)
                    self._changed()
                    refresher = asyncio.create_task(self._refresh_loop())
                    try:
                        async for message in client.messages:
                            try:
                                payload = json.loads(message.payload)
                            except (TypeError, ValueError):
                                continue
                            if isinstance(payload, dict):
                                self.handle(payload)
                    finally:
                        refresher.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - retry all network failures
                self.last_error = _describe(exc)
                log.warning(m('Printer connection: %s'), self.last_error)
            finally:
                was_connected = self.connected
                self._client = None
                self.connected = False
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(PrinterError(m('Printer connection lost.')))
                self._pending.clear()
                if was_connected:
                    self._changed()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(PUSHALL_INTERVAL)
            await self.pushall(force=True)

    async def pushall(self, force: bool = False) -> None:
        """Request full status. A disconnected printer is a no-op, rather than an error,
        preventing app live streams from failing and endlessly reconnecting."""
        if not force and time.time() - self._last_pushall < PUSHALL_MIN_GAP:
            return
        self._last_pushall = time.time()
        try:
            await self._publish({"pushing": {"sequence_id": self._next(), "command": "pushall"}})
        except PrinterError as exc:
            log.info(m('Skipped pushall: %s'), exc)

    def _next(self) -> str:
        return str(next(self._seq))

    async def _publish(self, payload: dict) -> None:
        client = self._client
        if client is None or not self.connected:
            raise PrinterError(m('Printer not connected.'))
        try:
            await client.publish(f"device/{self.serial}/request", json.dumps(payload))
        except aiomqtt.MqttError as exc:
            raise PrinterError(m('Could not send to the printer: {p0}', p0=exc)) from exc

    async def command(self, section: str, body: dict, *, wait: float = 8.0) -> dict | None:
        """Send a command and briefly await a reply with the same `sequence_id`.

        Return None after `wait` seconds: the command may still have arrived, and status will
        confirm it. A rejection (`result` other than success) raises an error."""
        seq = self._next()
        payload = {section: {"sequence_id": seq, **body}}
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[seq] = future
        try:
            await self._publish(payload)
            reply = await asyncio.wait_for(future, wait)
        except TimeoutError:
            return None
        finally:
            self._pending.pop(seq, None)
        result = str(reply.get("result", "success")).lower()
        if result not in ("success", "ok", ""):
            reason = reply.get("reason") or reply.get("err_code") or result
            raise PrinterError(m('Printer rejected the request: {p0}', p0=reason))
        return reply


def login_refused(text: str) -> bool:
    """Does an MQTT connection error mean the printer refused the access code? Also used by `probe`."""
    return any(sign in text for sign in ("Not authorized", "Bad user name or password", "code:4", "code:5"))


def _describe(exc: Exception) -> str:
    text = message_of(exc) or exc.__class__.__name__
    if m('printer certificate has changed') in text:
        return CHANGED
    if login_refused(text):
        return m('Incorrect access code (Printer → Settings → WLAN).')
    if "timed out" in text.lower():
        return m('Printer not responding (off, not on Wi-Fi or LAN Only mode is off).')
    return text
