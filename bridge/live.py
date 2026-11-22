"""Current print Live Activities on paired iPhones (Lock Screen and Dynamic Island).

The app registers one push-to-start token per device and one update token per Live Activity.
Every print, including those started in OrcaSlicer, starts an activity on all registered devices.
The app then registers update tokens. Progress updates are throttled to once per minute; pauses
and errors are sent immediately.

A finished, failed or canceled print stays on the Lock Screen with its final status, sent as an update
rather than an end event so the activity stays under the bridge's control. It is ended and removed when
the next print starts, before that print's activity, so only one is shown; otherwise after `DISMISS_AFTER`,
also while the printer is off or offline. A final update that did not go out is sent again until then.
Removing a printer ends its activity right away.

Registrations belong to the paired device that made them, or to the shared access key. Only that owner can
replace or remove them, and only registrations of a device that is still paired, or of the current shared key,
receive pushes.

Persist tokens and current prints in `push.json` so restarting during a print does not start
duplicate activities. `ATTRIBUTES_TYPE` and `content_state` keys match the app's
`PrintActivityAttributes.ContentState` synthesized camelCase Codable contract."""
from __future__ import annotations

from .i18n import m, render, Excerpt

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Callable
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
DISMISS_AFTER = 60 * 60    # Keep a finished print on the Lock Screen for this long.
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
    """Same app installation of the same owner. Another device's registration is never replaced."""
    if entry.get("device") != device:
        return False
    # A device-specific access token identifies one installation; shared access uses the installation ID.
    return device != SHARED_DEVICE or bool(installation) and entry.get("installation") == installation


def _transient(exc: APNsError) -> bool:
    """A rejection that may pass later (rate limit, server or provider token trouble), not a bad token or payload."""
    if exc.token_gone or exc.reason == "TokenBoundElsewhere":
        return False
    return exc.status in (401, 403, 429) or exc.status >= 500


def _key_digest(secret: str, salt: str) -> str:
    return hmac.new(bytes.fromhex(salt), secret.encode(), hashlib.sha256).hexdigest()


def _owner(token: str, entry: dict) -> str:
    if entry.get("installation"):
        return "i:" + entry["installation"]
    if entry.get("device") and entry["device"] != SHARED_DEVICE:
        return "d:" + entry["device"]
    return "t:" + token


class Registry:
    """Persist iPhone tokens and each printer's current print in `push.json`.

    Push-to-start tokens belong to a device and cover all printers; update tokens belong to one
    printer's activity. Registrations of revoked devices, and those saved before device binding, are discarded
    by `bind` at startup."""

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
        # Salted digest of the shared access key the shared registrations were made with (see `bind`).
        self.shared_key: dict | None = data.get("shared_key")
        self._paired: Callable[[str], bool] | None = None
        self._shared = True
        for tokens in [self.start, self.alerts, *(s.get("activities", {}) for s in self.printers.values())]:
            for entry in tokens.values():
                if entry.get("language") not in ("en", "de"):
                    entry["language"] = "en"
        if self.path.exists():
            os.chmod(self.path, 0o600)

    def section(self, printer_id: str) -> PrinterPush:
        return PrinterPush(self, self.printers.setdefault(printer_id, {}))

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        # Make push tokens readable only by the bridge, regardless of umask.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"start": self.start, "alerts": self.alerts, "printers": self.printers,
                       "shared_key": self.shared_key}, handle, indent=1)
        tmp.replace(self.path)

    def bind(self, paired: Callable[[str], bool], shared_secret: str | None) -> int:
        """Deliver only to registrations whose device is still paired (`paired(device_id)`). Shared-access-key
        registrations belong to the key they were made with: a changed or removed key discards them. Registrations
        saved before this check are adopted for the current key. Returns how many were discarded."""
        self._paired = paired
        self._shared = bool(shared_secret)
        stored = self.shared_key
        valid = isinstance(stored, dict) and isinstance(stored.get("salt"), str) and isinstance(stored.get("digest"), str)
        rotated = False
        if shared_secret and valid:
            try:
                rotated = not hmac.compare_digest(_key_digest(shared_secret, stored["salt"]), stored["digest"])
            except ValueError:
                rotated = True
        record = stored
        if not shared_secret:
            record = None
        elif rotated or not valid:
            salt = secrets.token_hex(16)
            record = {"salt": salt, "digest": _key_digest(shared_secret, salt)}
        removed = self._drop(lambda entry: not self.allowed(entry) or rotated and entry.get("device") == SHARED_DEVICE)
        if record != self.shared_key:
            self.shared_key = record
            self.save()
        if removed:
            log.info(m('Discarded %d push tokens of revoked devices or an old access key.'), removed)
        return removed

    def allowed(self, entry: dict) -> bool:
        """May this registration still receive pushes?"""
        device = entry.get("device")
        if device == SHARED_DEVICE:
            return self._shared
        # Registrations without a device (saved before device binding) can never be revoked: never deliver them.
        return isinstance(device, str) and (self._paired is None or self._paired(device))

    def _tokens(self, kind: str) -> list[dict[str, dict]]:
        if kind == "start":
            return [self.start]
        if kind == "alert":
            return [self.alerts]
        return [section.setdefault("activities", {}) for section in self.printers.values()]

    def owner(self, kind: str, token: str) -> str | None:
        """Device (or `SHARED_DEVICE`) that registered `token` and may still receive pushes."""
        for tokens in self._tokens(kind):
            entry = tokens.get(token)
            if entry is not None and self.allowed(entry):
                return entry.get("device")
        return None

    def _same_installation_token(self, kind: str, token: str, installation: str | None) -> bool:
        return bool(installation) and any((tokens.get(token) or {}).get("installation") == installation
                                          for tokens in self._tokens(kind))

    def add(self, kind: str, token: str, environment: str, activity_id: str | None = None,
            printer_id: str = "", device: str = SHARED_DEVICE, installation: str | None = None,
            events: list[str] | None = None, language: str = "en") -> bool:
        """Register a token for `device`. False: the token belongs to another paired device (or the shared key) and
        stays with it; it can move once that owner is revoked, or when the same app installation registers it again
        (the iPhone paired anew while its old pairing still exists)."""
        owner = self.owner(kind, token)
        if owner is not None and owner != device and not self._same_installation_token(kind, token, installation):
            return False
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
            for section in self.printers.values():
                section.get("activities", {}).pop(token, None)
            self.section(printer_id).activities[token] = {**entry, "activity_id": activity_id}
        self.save()
        return True

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

    def forget(self, tokens: list[str]) -> None:
        """Remove tokens that may no longer receive pushes, saving once."""
        if not tokens:
            return
        for token in tokens:
            self.start.pop(token, None)
            self.alerts.pop(token, None)
            for section in self.printers.values():
                section.get("activities", {}).pop(token, None)
        self.save()

    def _drop(self, matches: Callable[[dict], bool]) -> int:
        """Remove the start, alert and update registrations `matches` selects; saves when any were removed."""
        removed = 0
        for tokens in [self.start, self.alerts, *(s.setdefault("activities", {}) for s in self.printers.values())]:
            for token in [t for t, e in tokens.items() if matches(e)]:
                del tokens[token]
                removed += 1
        if removed:
            self.save()
        return removed

    def remove_device(self, device: str) -> int:
        """Remove every token of a revoked device, including start/update tokens for all printers."""
        return self._drop(lambda entry: entry.get("device") == device)

    def remove_installation(self, device: str, installation: str) -> int:
        """Remove the registrations an app installation made as `device` (start, update and alert), for example when
        the app leaves this bridge. An individually paired device is one installation: its registrations without an
        installation ID (older app versions) go as well."""
        def mine(entry: dict) -> bool:
            if entry.get("device") != device:
                return False
            return entry.get("installation") == installation or (device != SHARED_DEVICE and not entry.get("installation"))
        return self._drop(mine)

    def drop_printer(self, printer_id: str) -> None:
        """Forget a removed printer's current print and update tokens."""
        if self.printers.pop(printer_id, None) is not None:
            self.save()


class LiveActivities:
    """One printer's Live Activity; attributes include `printer_id` so the app can route updates."""

    def __init__(self, client: APNsClient | None, registry: Registry, printer_name=lambda: m('Printer'),
                 printer_id: str = ""):
        self.client = client
        self.registry = registry
        self.printer_name = printer_name
        self.printer_id = printer_id
        self.push = registry.section(printer_id)
        self._status: dict | None = None
        self._changed = asyncio.Event()
        self._last: dict | None = None
        self._last_at = 0.0
        self._retried_at = 0.0
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
            try:
                # Without a status (printer unreachable since startup), a finished activity still ends on time.
                await self.process(self._status)
            except Exception:  # noqa: BLE001 - one failed cycle must not stop the loop
                log.exception("Live Activity")

    async def process(self, status: dict | None, now: float | None = None) -> None:
        if not self.enabled:
            return
        now = now or time.time()
        current = self.push.current
        if current is not None and current.get("ended"):
            # The printer is often switched off after a print: retry and remove the finished activity from what was
            # stored, without waiting for the printer.
            if now - current["ended"] >= DISMISS_AFTER:
                await self._dismiss(current.get("state") or self._fallback(), now)
                self.push.current = None
                self.push.save()
            elif current.get("retry") and now - self._retried_at >= UPDATE_INTERVAL:
                await self._send_final(current, now)
        if status is None or not status["connection"]["connected"]:
            return
        for template, job_name, detail in self.alerts.changes(status):
            await self._notify(template, job_name, detail)
        job = status["job"]
        state = content_state(status)
        current = self.push.current
        if job["state"] in ACTIVE:
            key = f"{job.get('name')}|{job.get('file')}"
            # A finished print of the same file printed again is a new print.
            if current is None or current.get("key") != key or current.get("ended"):
                # The previous print's activity, finished or not, goes before the new one starts.
                await self._dismiss((current or {}).get("state") or self._last or state, now)
                self.push.current = {"key": key, "started": now}
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
        elif current is not None and job["state"] in ENDED and not current.get("ended"):
            current.update(ended=now, state=state)
            await self._send_final(current, now)
            self._last = None

    async def retire(self, now: float | None = None) -> None:
        """The printer was removed: end its activity right away and forget its current print and update tokens."""
        now = now or time.time()
        if self.enabled and self.push.activities:
            await self._dismiss((self.push.current or {}).get("state") or self._last or self._fallback(), now)
        self.registry.drop_printer(self.printer_id)

    def _fallback(self) -> dict:
        return content_state(self._status if self._status is not None else {"job": {"state": "IDLE"}})

    async def _send_final(self, current: dict, now: float) -> None:
        """Final status as an update, so the next print can still remove it. Activities it did not reach because of
        a network or relay failure get it again on a later cycle."""
        tokens = self.push.activities
        if current.get("retry"):
            tokens = {t: e for t, e in tokens.items() if t in current["retry"]}
        failed = await self._send_all(tokens, self._payload("update", current["state"], now), 10)
        self._retried_at = now
        if failed:
            current["retry"] = failed
        else:
            current.pop("retry", None)
        self.push.save()

    async def _dismiss(self, state: dict, now: float) -> None:
        """Remove the current activity from the Lock Screen right away and forget its update tokens."""
        if self.push.activities:
            payload = self._payload("end", state, now)
            payload["aps"]["dismissal-date"] = int(now)
            await self._send_all(self.push.activities, payload, 10)
        self.push.activities.clear()

    def _remember(self, state: dict, now: float) -> None:
        self._last, self._last_at = state, now

    def _payload(self, event: str, state: dict, now: float) -> dict:
        aps: dict = {"timestamp": int(now), "event": event, "content-state": state}
        if event == "end":
            aps["dismissal-date"] = int(now + DISMISS_AFTER)
        elif state["phase"] in ENDED:
            # A finished print does not change any more: not stale until it is removed.
            aps["stale-date"] = int(now + DISMISS_AFTER + STALE_AFTER)
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

    def _deliverable(self, tokens: dict[str, dict]) -> list[tuple[str, dict]]:
        """Registrations that may still receive pushes; forget those of revoked devices or an old shared key."""
        entries = list(tokens.items())
        self.registry.forget([token for token, entry in entries if not self.registry.allowed(entry)])
        return [(token, entry) for token, entry in entries if self.registry.allowed(entry)]

    async def _send_all(self, tokens: dict[str, dict], payload: dict, priority: int) -> list[str]:
        """Send to every token; returns the tokens not reached because of a network or relay failure."""
        groups: dict[str, list[tuple[str, dict]]] = {}
        failed: list[str] = []
        for token, entry in self._deliverable(tokens):
            groups.setdefault(entry.get("language", "en"), []).append((token, entry))
        for language, entries in groups.items():
            localized = {**render(payload, language), "language": language}
            for token, entry in entries:
                try:
                    await self.client.send_live_activity(token, entry["environment"], localized, priority)
                except APNsError as exc:
                    self._rejected(token, f"Live Activity ({payload['aps']['event']})", exc)
                    if _transient(exc):
                        failed.append(token)
                except Exception as exc:  # noqa: BLE001 - retry network failures on the next cycle
                    log.warning("Live Activity (%s): %s", payload["aps"]["event"], exc)
                    failed.append(token)
        return failed

    async def _notify(self, template: str, job_name: str, detail: str | None) -> None:
        from .notify import TEMPLATE_EVENT, note
        event = TEMPLATE_EVENT[template]
        items = {}
        for token, entry in self._deliverable(self.registry.alerts):
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
