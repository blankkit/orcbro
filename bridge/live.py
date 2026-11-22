"""Current print Live Activities on paired iPhones (Lock Screen and Dynamic Island).

The app registers one push-to-start token per device and one update token per Live Activity.
Every print, including those started in OrcaSlicer, starts an activity on all registered devices.
The app then registers update tokens. Progress updates are throttled to once per minute; pauses
and errors are sent immediately. An end event sends final status.

Persist tokens and current prints in `push.json` so restarting during a print does not start
duplicate activities. `ATTRIBUTES_TYPE` and `content_state` keys match the app's
`PrintActivityAttributes.ContentState` synthesized camelCase Codable contract."""
from __future__ import annotations

from .i18n import m, render, Excerpt

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

from .apns import APNsClient, APNsError

log = logging.getLogger(__name__)

ATTRIBUTES_TYPE = "PrintActivityAttributes"
# Shared-access-key devices have no individual ID; revoke their tokens only by changing the key.
# Individual token revocation requires a device-specific identity.
SHARED_DEVICE = "access_key"


def public_printer_id(serial: str) -> str:
    """Live Activity printer ID: send a derived value instead of the serial number through APNs and
    the relay. The app computes it identically (BambroCore `LiveActivityID`)."""
    return "p" + hashlib.sha256(serial.encode()).hexdigest()[:16] if serial else ""
ACTIVE = {"PREPARE", "RUNNING", "PAUSE"}
ENDED = {"FINISH", "FAILED", "IDLE"}
UPDATE_INTERVAL = 60       # Seconds between progress-only updates.
HEARTBEAT = 10 * 60        # Maximum interval between updates to keep the display fresh.
STALE_AFTER = 20 * 60      # The iPhone marks status stale after this interval.
DISMISS_AFTER = 60 * 60    # Keep the ended activity on the Lock Screen for this long.
ENVIRONMENTS = ("development", "production")


def _first_sentence(text: str | None, limit: int = 90, fallback: str | None = None) -> str | None:
    """Lock Screen summary of a Bambu text: first sentence, at most `limit` characters."""
    if not text:
        return fallback
    return Excerpt(text, limit, fallback)


def content_state(status: dict) -> dict:
    """Live Activity content from normalized printer status."""
    job = status["job"]
    name = job.get("name") or re.sub(r"(\.gcode)?(\.3mf)?$", "", (job.get("file") or "").rsplit("/", 1)[-1]) or m('Print')
    error = None
    message = job.get("error")
    hms = [h for h in status.get("hms") or [] if not h.get("internal")]
    if message and not message.get("internal"):
        error = _first_sentence(message.get("text"), fallback=m('Error code {p0}', p0=message['code']))
    elif job.get("print_error") and "error" not in job:
        error = m('Error code {p0:08X}', p0=job['print_error'])
    elif job["state"] == "FAILED":
        error = m('Print canceled')
    elif hms:
        error = _first_sentence(hms[0].get("text"), fallback=m('Message {p0}', p0=hms[0]['code']))
    return {
        "phase": job["state"],
        "jobName": name,
        "progress": int(job.get("progress") or 0),
        "layer": job.get("layer"),
        "totalLayers": job.get("total_layers"),
        "remainingMinutes": job.get("remaining_minutes"),
        "finishAt": round(job["eta"]) if job.get("eta") else None,
        "stage": job.get("stage"),
        "error": error,
    }


class PrinterPush:
    """A printer's current print and the update tokens of its Live Activities."""

    def __init__(self, registry: "Registry", data: dict):
        self._registry = registry
        self._data = data
        data.setdefault("activities", {})
        data.setdefault("current", None)

    @property
    def activities(self) -> dict[str, dict]:
        return self._data["activities"]

    @property
    def current(self) -> dict | None:
        return self._data["current"]

    @current.setter
    def current(self, value: dict | None) -> None:
        self._data["current"] = value

    def save(self) -> None:
        self._registry.save()


def _same_installation(entry: dict, device: str, installation: str | None) -> bool:
    if installation and entry.get("installation") == installation:
        return True
    # A device-specific access token identifies one installation; shared access uses the installation ID.
    return device != SHARED_DEVICE and entry.get("device") == device


def _owner(token: str, entry: dict) -> str:
    if entry.get("installation"):
        return "i:" + entry["installation"]
    if entry.get("device") and entry["device"] != SHARED_DEVICE:
        return "d:" + entry["device"]
    return "t:" + token


class Registry:
    """Persist iPhone tokens and each printer's current print in `push.json`.

    Push-to-start tokens belong to a device and cover all printers; update tokens belong to one
    printer's activity. Adopt older single-printer files with top-level `current`/`activities`
    into the default printer via `adopt`."""

    LEGACY = "_legacy"

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.start: dict[str, dict] = data.get("start", {})
        # App device notification tokens and the events requested by each iPhone (notify.py).
        self.alerts: dict[str, dict] = {t: e for t, e in data.get("alerts", {}).items() if "device" in e}
        self.printers: dict[str, dict] = data.get("printers", {})
        if "current" in data or "activities" in data:
            self.printers.setdefault(self.LEGACY, {"current": data.get("current"),
                                                   "activities": data.get("activities", {})})
        # Drop tokens saved before device binding; otherwise revoked iPhones could continue receiving
        # messages. Paired apps register tokens again on next launch.
        dropped = sum(1 for e in self.start.values() if "device" not in e)
        self.start = {t: e for t, e in self.start.items() if "device" in e}
        for section in self.printers.values():
            activities = section.get("activities", {})
            dropped += sum(1 for e in activities.values() if "device" not in e)
            section["activities"] = {t: e for t, e in activities.items() if "device" in e}
        for tokens in [self.start, self.alerts, *(s.get("activities", {}) for s in self.printers.values())]:
            for entry in tokens.values():
                if entry.get("language") not in ("en", "de"):
                    entry["language"] = "en"
        if dropped:
            log.info(m('Discarded %d push tokens without a device; the app will register them again.'), dropped)
        if self.path.exists():
            os.chmod(self.path, 0o600)

    def section(self, printer_id: str, *, adopt: bool = False) -> PrinterPush:
        if printer_id not in self.printers and adopt and self.LEGACY in self.printers:
            self.printers[printer_id] = self.printers.pop(self.LEGACY)
        return PrinterPush(self, self.printers.setdefault(printer_id, {}))

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        # Make push tokens readable only by the bridge, regardless of umask.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"start": self.start, "alerts": self.alerts, "printers": self.printers}, handle, indent=1)
        tmp.replace(self.path)

    def add(self, kind: str, token: str, environment: str, activity_id: str | None = None,
            printer_id: str = "", device: str = SHARED_DEVICE, installation: str | None = None,
            events: list[str] | None = None, language: str = "en") -> None:
        entry = {"environment": environment, "updated": time.time(), "device": device,
                 "language": language if language in ("en", "de") else "en"}
        if installation:
            entry["installation"] = installation
        if kind == "alert":
            # As with start tokens, keep one notification device token per iPhone. No requested events means
            # notifications are off in iOS or the app: unregister the token and stop sending.
            self.alerts = {t: e for t, e in self.alerts.items()
                           if t != token and not _same_installation(e, device, installation)}
            if events:
                self.alerts[token] = {**entry, "events": list(events)}
        elif kind == "start":
            # Each iPhone has exactly one start token. Rotation, Xcode instead of TestFlight, or reinstallation
            # replaces the old token to avoid counting the same iPhone twice and sending duplicate pushes.
            self.start = {t: e for t, e in self.start.items()
                          if t == token or not _same_installation(e, device, installation)}
            self.start[token] = entry
        else:
            self.section(printer_id).activities[token] = {**entry, "activity_id": activity_id}
        self.save()

    def start_devices(self) -> int:
        """Count iPhones accepting push-started Live Activities, rather than counting tokens."""
        return len({_owner(token, entry) for token, entry in self.start.items()})

    def alert_devices(self) -> int:
        """Count iPhones receiving notifications."""
        return len({_owner(token, entry) for token, entry in self.alerts.items()})

    def remove(self, token: str) -> None:
        self.start.pop(token, None)
        self.alerts.pop(token, None)
        for section in self.printers.values():
            section.get("activities", {}).pop(token, None)
        self.save()

    def remove_device(self, device: str) -> int:
        """Remove every token of a revoked device, including start/update tokens for all printers."""
        before = len(self.start) + len(self.alerts) + sum(len(s.get("activities", {})) for s in self.printers.values())
        self.start = {t: e for t, e in self.start.items() if e.get("device") != device}
        self.alerts = {t: e for t, e in self.alerts.items() if e.get("device") != device}
        for section in self.printers.values():
            section["activities"] = {t: e for t, e in section.get("activities", {}).items() if e.get("device") != device}
        removed = before - len(self.start) - len(self.alerts) - sum(len(s.get("activities", {})) for s in self.printers.values())
        if removed:
            self.save()
        return removed


class LiveActivities:
    """One printer's Live Activity; attributes include `printer_id` so the app can route updates."""

    def __init__(self, client: APNsClient | None, registry: Registry, printer_name=lambda: m('Printer'),
                 printer_id: str = "", *, adopt: bool = True):
        self.client = client
        self.registry = registry
        self.printer_name = printer_name
        self.printer_id = printer_id
        self.push = registry.section(printer_id, adopt=adopt)
        self._status: dict | None = None
        self._changed = asyncio.Event()
        self._last: dict | None = None
        self._last_at = 0.0
        from .notify import PrintAlerts  # notify uses helpers from this module.
        self.alerts = PrintAlerts()

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def observe(self, status: dict) -> None:
        """Called synchronously for each printer message; `run` performs asynchronous work."""
        self._status = status
        self._changed.set()

    async def run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=60)
            except TimeoutError:
                pass
            self._changed.clear()
            if self._status is not None:
                try:
                    await self.process(self._status)
                except Exception:  # noqa: BLE001 - one failed cycle must not stop the loop
                    log.exception("Live Activity")

    async def process(self, status: dict, now: float | None = None) -> None:
        if not self.enabled or not status["connection"]["connected"]:
            return
        now = now or time.time()
        for template, job_name, detail in self.alerts.changes(status):
            await self._notify(template, job_name, detail)
        job = status["job"]
        state = content_state(status)
        current = self.push.current
        if job["state"] in ACTIVE:
            key = f"{job.get('name')}|{job.get('file')}"
            if current is None or current.get("key") != key:
                self.push.current = {"key": key, "started": now}
                self.push.activities.clear()  # Tokens belong to earlier prints' Live Activities.
                self.push.save()
                await self._start(state, now)
                self._remember(state, now)
                return
            last = self._last or {}
            major = any(state.get(k) != last.get(k) for k in ("phase", "stage", "error"))
            minor = any(state.get(k) != last.get(k) for k in ("progress", "layer"))
            if major or (minor and now - self._last_at >= UPDATE_INTERVAL) or now - self._last_at >= HEARTBEAT:
                await self._send_all(self.push.activities, self._payload("update", state, now), 10 if major else 5)
                self._remember(state, now)
        elif current is not None and job["state"] in ENDED:
            await self._send_all(self.push.activities, self._payload("end", state, now), 10)
            self.push.current = None
            self.push.activities.clear()
            self.push.save()
            self._last = None

    def _remember(self, state: dict, now: float) -> None:
        self._last, self._last_at = state, now

    def _payload(self, event: str, state: dict, now: float) -> dict:
        aps: dict = {"timestamp": int(now), "event": event, "content-state": state}
        if event == "end":
            aps["dismissal-date"] = int(now + DISMISS_AFTER)
        else:
            aps["stale-date"] = int(now + STALE_AFTER)
        return {"aps": aps}

    async def _start(self, state: dict, now: float) -> None:
        payload = self._payload("start", state, now)
        payload["aps"].update({
            "attributes-type": ATTRIBUTES_TYPE,
            "attributes": {"printerName": self.printer_name(), "printerId": public_printer_id(self.printer_id)},
            # Apple requires a push-to-start alert, displayed when the Live Activity starts.
            "alert": {"title": m('Print Started'), "body": m('{p0} on {p1}', p0=state['jobName'], p1=self.printer_name())},
        })
        await self._send_all(self.registry.start, payload, 10)

    async def _send_all(self, tokens: dict[str, dict], payload: dict, priority: int) -> None:
        groups: dict[str, list[tuple[str, dict]]] = {}
        for token, entry in list(tokens.items()):
            groups.setdefault(entry.get("language", "en"), []).append((token, entry))
        for language, entries in groups.items():
            localized = {**render(payload, language), "language": language}
            for token, entry in entries:
                try:
                    await self.client.send_live_activity(token, entry["environment"], localized, priority)
                except APNsError as exc:
                    self._rejected(token, f"Live Activity ({payload['aps']['event']})", exc)
                except Exception as exc:  # noqa: BLE001 - retry network failures on the next cycle
                    log.warning("Live Activity (%s): %s", payload["aps"]["event"], exc)

    async def _notify(self, template: str, job_name: str, detail: str | None) -> None:
        from .notify import TEMPLATE_EVENT, note
        event = TEMPLATE_EVENT[template]
        items = {}
        for token, entry in list(self.registry.alerts.items()):
            if event not in entry.get("events", ()):
                continue
            language = entry.get("language", "en")
            if language not in items:
                items[language] = note(template, job_name, self.printer_name(), detail, self.printer_id, language)
            try:
                await self.client.send_notification(token, entry["environment"], items[language])
            except APNsError as exc:
                self._rejected(token, m('Notification ({p0})', p0=template), exc)
            except Exception as exc:  # noqa: BLE001 - omit this notification on network failure
                log.warning(m('Notification (%s): %s'), template, exc)

    def _rejected(self, token: str, what: str, exc: APNsError) -> None:
        if exc.reason == "TokenBoundElsewhere":
            # This iPhone token was already used with another bridge (relay binding lasts seven days). Forget it;
            # the app registers it on next launch and it will work once the old binding expires.
            log.warning(m('%s: The iPhone is bound to another bridge through the relay; forgetting token.'), what)
            self.registry.remove(token)
            return
        log.warning("%s: %s", what, exc)
        if exc.token_gone:
            self.registry.remove(token)
