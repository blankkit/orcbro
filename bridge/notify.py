"""Notifications for finished, failed, canceled or paused prints and printer HMS errors.

Use the Live Activity transport (APNs key or relay) with the app's device token. The app registers
only when iOS notification permission is granted, along with its desired `events`; revocation
unregisters it.

Texts match relay/src/templates.ts exactly. Relay delivery sends template values and language;
the relay renders the notification."""
from __future__ import annotations

from .i18n import m, render

import logging
import re

from .live import ACTIVE, _first_sentence, content_state, public_printer_id

log = logging.getLogger(__name__)

# Individually selectable notification events and their templates.
EVENTS = ("finished", "failed", "paused", "error")
TEMPLATE_EVENT = {"finished": "finished", "failed": "failed", "cancelled": "failed", "paused": "paused", "error": "error"}
LIMITS = {"job": 120, "printer": 60, "detail": 200}
CANCELLED_CODE = "0300_400C"  # The job was canceled, including stops from the printer or app.
SERIOUS = {"fatal", "serious"}


def clean(value: str | None, limit: int) -> str | None:
    """Match the relay: remove controls, collapse whitespace and truncate to `limit`."""
    if not value:
        return None
    text = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f-\x9f  ]", " ", value)).strip()
    if not text:
        return None
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def alert(template: str, job: str, printer: str, detail: str | None, language: str = "en") -> dict:
    return render(_alert(template, job, printer, detail), language)


def _alert(template: str, job: str, printer: str, detail: str | None) -> dict:
    """Notification title and body, matching `alert` in relay/src/templates.ts."""
    if template == "finished":
        return {"title": m('Print Finished'), "body": m('{p0} on {p1} is done.', p0=job, p1=printer)}
    if template == "failed":
        return {"title": m('Print Failed'),
                "body": m('{p0} on {p1}: {p2}', p0=job, p1=printer, p2=detail) if detail else m('{p0} on {p1} did not finish.', p0=job, p1=printer)}
    if template == "cancelled":
        return {"title": m('Print Canceled'), "body": m('{p0} on {p1} was canceled.', p0=job, p1=printer)}
    if template == "paused":
        return {"title": m('Print Paused'),
                "body": m('{p0} on {p1}: {p2}', p0=job, p1=printer, p2=detail) if detail else m('{p0} on {p1} is paused.', p0=job, p1=printer)}
    if template == "error":
        return {"title": m('Message from {p0}', p0=printer), "body": detail or m('The printer reports an error.')}
    raise ValueError(template)


def note(template: str, job: str, printer: str, detail: str | None, printer_id: str, language: str = "en") -> dict:
    """Relay notification values; `apns_payload` builds the push when using an APNs key."""
    job, printer, detail = render(job, language), render(printer, language), render(detail, language)
    result = {"language": language, "template": template, "job": clean(job, LIMITS["job"]) or render(m('Print'), language),
              "printer": clean(printer, LIMITS["printer"]) or render(m('Printer'), language)}
    if detail := clean(detail, LIMITS["detail"]):
        result["detail"] = detail
    if printer_id:
        result["thread"] = public_printer_id(printer_id)
    return result


def apns_payload(item: dict) -> dict:
    aps = {"alert": alert(item["template"], item["job"], item["printer"], item.get("detail"), item.get("language", "en")), "sound": "default"}
    if item.get("thread"):
        aps["thread-id"] = item["thread"]
    return {"aps": aps}


class PrintAlerts:
    """Detect notification-worthy status changes observed by this bridge. After a restart,
    do not announce an already finished print again."""

    def __init__(self) -> None:
        self._state: str | None = None
        self._job: str | None = None
        self._hms: set[str] = set()

    def changes(self, status: dict) -> list[tuple[str, str, str | None]]:
        """Return [(template, job name, detail)] for this status and remember the new state."""
        job = status["job"]
        state = job["state"]
        name = content_state(status)["jobName"]
        visible = [h for h in status.get("hms") or [] if not h.get("internal")]
        codes = {h["code"] for h in visible if h.get("severity") in SERIOUS}
        previous, self._state = self._state, state
        new_codes = codes - self._hms
        self._hms = codes
        if previous is None:
            return []  # First status after startup: do not notify.
        result: list[tuple[str, str, str | None]] = []
        error = job.get("error") or {}
        error_text = None if error.get("internal") else _first_sentence(error.get("text"), 160)
        hms_text = next((_first_sentence(h.get("text"), 160) for h in visible if h.get("text")), None)
        if previous in ACTIVE and state == "FINISH":
            result.append(("finished", name, None))
        elif previous in ACTIVE and state == "FAILED":
            if not error or error.get("code") == CANCELLED_CODE:
                result.append(("cancelled", name, None))
            else:
                result.append(("failed", name, _first_sentence(error.get("text"), 160, m('Error code {p0}', p0=error['code']))))
        elif previous in ("PREPARE", "RUNNING") and state == "PAUSE":
            result.append(("paused", name, error_text or hms_text))
        # New serious HMS messages during printing, unless already explained by a pause notification.
        if state in ACTIVE and new_codes and not any(t == "paused" for t, _, _ in result):
            for entry in visible:
                if entry["code"] in new_codes:
                    result.append(("error", name, _first_sentence(entry.get("text"), 160, m('Message {p0}', p0=entry['code']))))
        return result
