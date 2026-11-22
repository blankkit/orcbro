"""Bridge printers: settings and running components for each printer.

Store IP, access code and name in private `printers.json` on the volume. Printers come from the
app (`POST /api/printers`) or environment (`BAMBU_HOST`, `BAMBU_ACCESS_CODE`, e.g. Kubernetes secrets).
Recreate the environment printer on startup, so it cannot be edited in the app.

Each printer has an MQTT connection, camera, print/file locks and a Live Activity. The printer
profile follows the reported model and installed nozzle; changing nozzles selects the matching
profile for subsequent slicing."""
from __future__ import annotations

from .i18n import m

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .camera import Camera
from .models import Models
from .printer import Printer
from .trust import PrinterTrust
from .profiles import Catalog, ProfileError, machine_profiles, nozzle_variant

log = logging.getLogger(__name__)


@dataclass
class PrinterSettings:
    serial: str
    host: str
    access_code: str = field(repr=False)
    name: str = ""
    # Setup model hint (Bambu Lab P1S), used until the printer reports its own model.
    model: str | None = None
    # Fixed profile (BAMBU_MACHINE_PROFILE); otherwise derive it from model and nozzle.
    machine_profile: str | None = None
    source: str = "app"  # app | env
    added_at: float = field(default_factory=time.time)

    @property
    def id(self) -> str:
        return self.serial

    def public(self) -> dict:
        """App representation excluding the access code."""
        return {"id": self.id, "serial": self.serial, "host": self.host, "name": self.name,
                "machine_profile": self.machine_profile, "managed": self.source == "env"}


class PrinterStore:
    """`printers.json`: saved app printers and the default printer."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.printers: dict[str, PrinterSettings] = {}
        self.default: str | None = None
        # Certificate pin per serial number, including the BAMBU_HOST printer (trust.py).
        self.pins: dict[str, str] = {}
        self._lock = threading.Lock()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.error(m('Cannot read %s: %s'), self.path, exc)
            return
        for entry in data.get("printers", []):
            try:
                settings = PrinterSettings(**{k: v for k, v in entry.items() if k in PrinterSettings.__dataclass_fields__})
            except TypeError:
                continue
            settings.source = "app"
            self.printers[settings.id] = settings
        self.default = data.get("default")
        pins = data.get("pins")
        if isinstance(pins, dict):
            self.pins = {str(k): str(v) for k, v in pins.items() if isinstance(v, str)}

    def save(self) -> None:
        with self._lock:  # Also called from threads, where FTPS remembers a certificate.
            data = {"default": self.default, "pins": dict(self.pins),
                    "printers": [asdict(p) for p in self.printers.values() if p.source == "app"]}
            tmp = self.path.with_suffix(".tmp")
            # Contains the access code in clear text; readable only by the bridge.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=1)
            tmp.replace(self.path)

    def trust(self, serial: str) -> PrinterTrust:
        """Pinned certificate; remember and persist the first certificate encountered."""
        def remember(fingerprint: str) -> None:
            self.pins[serial] = fingerprint
            log.info(m('Pinned certificate for %s'), serial)
            self.save()
        return PrinterTrust(serial, self.pins.get(serial), remember)


class Catalogs:
    """Share one loaded catalog per printer profile, such as two P1S printers with 0.4 mm nozzles."""

    def __init__(self, root: Path, filament_ids: Path | None):
        self.root = Path(root)
        self.filament_ids = filament_ids
        self._cache: dict[str, Catalog] = {}
        self._machines: dict[tuple[str, str], str] | None = None
        self._lock = asyncio.Lock()

    def machine_for(self, model: str | None, nozzle: float | None) -> str | None:
        """Profile for a model (Bambu Lab P1S) and nozzle (0.4), or None if unavailable."""
        if not model:
            return None
        if self._machines is None:
            self._machines = machine_profiles(self.root) if (self.root / "machine").is_dir() else {}
        return self._machines.get((model, nozzle_variant(nozzle)))

    def cached(self, machine: str) -> Catalog | None:
        return self._cache.get(machine)

    def put(self, catalog: Catalog) -> None:
        self._cache[catalog.machine_name] = catalog

    async def get(self, machine: str) -> Catalog:
        if machine in self._cache:
            return self._cache[machine]
        async with self._lock:
            if machine not in self._cache:
                self._cache[machine] = await asyncio.to_thread(Catalog, self.root, machine, self.filament_ids)
        return self._cache[machine]


class Device:
    """One printer and all its associated components."""

    def __init__(self, settings: PrinterSettings, catalogs: Catalogs, models: Models, *,
                 printer: Printer | None = None, camera: Camera | None = None, trust: PrinterTrust | None = None):
        self.settings = settings
        self.catalogs = catalogs
        self.models = models
        # Share one certificate across MQTT, FTPS and camera (trust.py).
        self.trust = trust or (printer.trust if printer is not None else PrinterTrust(settings.serial))
        self.printer = printer or Printer(settings.host, settings.serial, settings.access_code, settings.name,
                                          models=models, model_hint=settings.model, trust=self.trust)
        self.printer.models = models
        self.printer.model_hint = settings.model
        self.camera = camera or Camera(settings.host, settings.access_code, trust=self.trust)
        self.print_lock = asyncio.Lock()
        self.print_file_lock = asyncio.Lock()
        self.preview_locks: dict[str, asyncio.Lock] = {}
        self.live = None  # LiveActivities, set by the application.
        self.spools = None  # SpoolTracker, set by the application.
        self.tasks: list[asyncio.Task] = []

    @property
    def id(self) -> str:
        return self.settings.id

    @property
    def host(self) -> str:
        return self.settings.host

    @property
    def access_code(self) -> str:
        return self.settings.access_code

    @property
    def name(self) -> str:
        return self.settings.name or self.model or "Bambu Lab"

    @property
    def model(self) -> str | None:
        """Model display name reported by the printer, such as Bambu Lab P1S."""
        record = self.printer.model_record()
        if record:
            return record.name
        ota = next((m for m in self.printer.version.get("module", []) if m.get("name") == "ota"), {})
        name = ota.get("product_name")
        model = self.models.find(name)
        return model.name if model else name or self.settings.model

    @property
    def model_id(self) -> str | None:
        model = self.printer.model_record() or self.models.find(self.model)
        return model.model_id if model else None

    def refresh_context(self) -> None:
        """Select model-specific transports only after canonical model resolution."""
        model = self.printer.model_record()
        if model is None or not hasattr(self.camera, "set_mode"):
            return
        capabilities = model.capabilities(self.printer.status()["printer"].get("firmware"), self.printer.raw)
        rtsp = self.printer.raw.get("ipcam") if isinstance(self.printer.raw.get("ipcam"), dict) else {}
        self.camera.set_mode(capabilities.get("camera"), disabled=rtsp.get("rtsp_url") == "disable")

    @property
    def nozzle(self) -> float | None:
        try:
            return float(self.printer.raw.get("nozzle_diameter"))
        except (TypeError, ValueError):
            return None

    @property
    def machine_profile(self) -> str | None:
        return self.settings.machine_profile or self.catalogs.machine_for(self.model, self.nozzle)

    def catalog_now(self) -> Catalog | None:
        """Already loaded catalog for synchronous consumers such as status."""
        machine = self.machine_profile
        return self.catalogs.cached(machine) if machine else None

    async def catalog(self) -> Catalog:
        machine = self.machine_profile
        if machine is None:
            if self.model is None:
                raise ProfileError(m('The printer has not reported its model yet. Wait until it connects.'))
            raise ProfileError(m('No printer profile for {p0} with a {p1} mm nozzle.', p0=self.model, p1=nozzle_variant(self.nozzle)))
        return await self.catalogs.get(machine)

    def summary(self, *, default: bool) -> dict:
        """Compact status for the app printer list."""
        self.refresh_context()
        status = self.printer.status()
        job = status["job"]
        return {**self.settings.public(), "name": self.name, "model": self.model, "model_id": self.model_id,
                "machine": self.machine_profile, "default": default,
                "connected": status["connection"]["connected"], "error": status["connection"].get("error"),
                "state": job["state"], "progress": job.get("progress"), "job_name": job.get("name"),
                "remaining_minutes": job.get("remaining_minutes")}

    def start(self) -> None:
        self.tasks.append(asyncio.create_task(self.printer.run(), name=f"printer-{self.id}"))
        if self.live is not None and self.live.enabled:
            self.tasks.append(asyncio.create_task(self.live.run(), name=f"live-{self.id}"))
        if self.spools is not None:
            self.tasks.append(asyncio.create_task(self.spools.run(), name=f"spools-{self.id}"))

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown errors can be ignored
                pass
        self.tasks.clear()
        await self.camera.close()


class Devices:
    """All printers: default selection, add, update and remove; persist via `PrinterStore`."""

    def __init__(self, store: PrinterStore, catalogs: Catalogs, models: Models,
                 make: Callable[[PrinterSettings], Device] | None = None):
        self.store = store
        self.catalogs = catalogs
        self.models = models
        self._make = make or (lambda settings: Device(settings, catalogs, models, trust=store.trust(settings.serial)))
        self.devices: dict[str, Device] = {}
        self._env: str | None = None
        self.on_added: list[Callable[[Device], None]] = []
        self.running = False

    def add_device(self, device: Device, *, env: bool = False) -> Device:
        self.devices[device.id] = device
        if env:
            self._env = device.id
        for callback in self.on_added:
            callback(device)
        if self.running:
            device.start()
        return device

    def load(self, env_settings: PrinterSettings | None = None) -> None:
        if env_settings is not None:
            env_settings.source = "env"
            self.add_device(self._make(env_settings), env=True)
        for settings in self.store.printers.values():
            if settings.id not in self.devices:
                self.add_device(self._make(settings))

    def list(self) -> list[Device]:
        return sorted(self.devices.values(), key=lambda d: (d.id != self.default_id, d.settings.added_at))

    @property
    def default_id(self) -> str | None:
        if self.store.default in self.devices:
            return self.store.default
        if self._env in self.devices:
            return self._env
        return min(self.devices.values(), key=lambda d: d.settings.added_at).id if self.devices else None

    def default(self) -> Device | None:
        return self.devices.get(self.default_id) if self.default_id else None

    def get(self, printer_id: str) -> Device:
        return self.devices[printer_id]

    def set_default(self, printer_id: str) -> None:
        self.store.default = printer_id
        self.store.save()

    def start(self) -> None:
        self.running = True
        for device in self.devices.values():
            device.start()

    async def stop(self) -> None:
        self.running = False
        for device in self.devices.values():
            await device.stop()

    async def add(self, settings: PrinterSettings, fingerprint: str | None = None) -> Device:
        if settings.id in self.devices:
            raise ValueError(m('This printer is already configured.'))
        settings.source = "app"
        self.store.printers[settings.id] = settings
        if fingerprint:
            self.store.pins[settings.id] = fingerprint
        if self.store.default is None and not self.devices:
            self.store.default = settings.id
        self.store.save()
        return self.add_device(self._make(settings))

    async def update(self, printer_id: str, *, host: str | None = None, access_code: str | None = None,
                     name: str | None = None, fingerprint: str | None = None) -> Device:
        """New IP or access code restarts connections; a new name applies immediately."""
        device = self.devices[printer_id]
        settings = device.settings
        if name is not None:
            settings.name = name
            device.printer.name = name
        reconnect = (host is not None and host != settings.host) or (
            access_code is not None and access_code != settings.access_code)
        if host is not None:
            settings.host = host
        if access_code is not None:
            settings.access_code = access_code
        if fingerprint:
            reconnect = reconnect or self.store.pins.get(printer_id) != fingerprint
            self.store.pins[printer_id] = fingerprint
        self.store.printers[printer_id] = settings
        self.store.save()
        if not reconnect:
            return device
        live = device.live
        await device.stop()
        replacement = self._make(settings)
        replacement.live = live
        self.devices[printer_id] = replacement
        if self.running:
            replacement.start()
        for callback in self.on_added:
            callback(replacement)
        return replacement

    async def remove(self, printer_id: str) -> None:
        device = self.devices.pop(printer_id)
        await device.stop()
        self.store.printers.pop(printer_id, None)
        self.store.pins.pop(printer_id, None)
        if self.store.default == printer_id:
            self.store.default = None
        self.store.save()
