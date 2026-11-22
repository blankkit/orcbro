"""Language selection at HTTP/persistence/push boundaries, without local TCP listeners."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from aiohttp.test_utils import make_mocked_request

from bridge.auth import Principal
from bridge.config import Config
from bridge.controls import ControlError, build
from bridge.i18n import (accept_language, current_language, language_context, language_middleware,
                         m, message_of, render, t, LocalizedText)
from bridge.jobs import JobStore
from bridge.live import LiveActivities, Registry
from bridge.notify import alert, apns_payload
from bridge.server import (PRINCIPAL, PUSH, JOBS, _run_slice, _send_event, app_token, create_app,
                           live_register)
from bridge.state import STAGES, normalize
from bridge.web import invitation_page
from bridge.identity import BridgeIdentity
from helpers import FILAMENT_IDS, PROFILES
from test_live import FakeAPNs, status


class LanguageTests(unittest.TestCase):
    def test_accept_language(self):
        cases = {None: "en", "": "en", "de": "de", "DE-de": "de", "en-US": "en", "fr": "en",
                 "de;q=0.3,en;q=0.9": "en", "en;q=0.1,de-DE;q=0.8": "de",
                 "fr-FR,de;q=0.8": "de", "de;q=0": "en", "de;q=invalid,en": "en",
                 "de;q=2,en": "en", "de;q=nan": "en", "de,en": "de", "*": "en"}
        for header, expected in cases.items():
            with self.subTest(header=header):
                self.assertEqual(accept_language(header), expected)

    def test_render_and_logs_stay_independent(self):
        message = m("Temperature: 0 to {p0} °C.", p0=300)
        with language_context("de"):
            self.assertEqual(t("Printer not connected."), "Drucker nicht verbunden.")
            self.assertEqual(render(message), "Temperatur: 0 bis 300 °C.")
            self.assertEqual(str(message), "Temperature: 0 to 300 °C.")
            self.assertEqual(t("Uncataloged text"), "Uncataloged text")
        self.assertEqual(current_language(), "en")

    def test_drying_reasons_remain_deferred(self):
        state = {"job": {"state": "IDLE"}, "ams": {"units": [{"id": 0, "type": "ams_2_pro",
                 "drying": {"supported": True, "status": "off", "blocked": ["insufficient_power"]}}]}}
        with self.assertRaises(ControlError) as caught:
            build("dry", {"ams_id": 0}, state)
        self.assertEqual(render(caught.exception.message, "de"),
                         "Trocknen geht gerade nicht. Die Stromversorgung reicht nicht (AMS-Netzteil anschließen).")

    def test_slicer_report_messages_remain_deferred(self):
        from bridge.slicer import _failure_text
        message = _failure_text({"error_string": "Object is outside the plate"}, "", 1)
        self.assertEqual(render(message, "en"), "Slicing failed: Object is outside the plate")
        self.assertEqual(render(message, "de"), "Slicen fehlgeschlagen: Objekt liegt außerhalb der Platte")

    def test_notification_templates_in_both_languages(self):
        english = {
            "finished": ("Print Finished", "Job on Printer is done."),
            "failed": ("Print Failed", "Job on Printer did not finish."),
            "cancelled": ("Print Canceled", "Job on Printer was canceled."),
            "paused": ("Print Paused", "Job on Printer is paused."),
            "error": ("Message from Printer", "The printer reports an error."),
        }
        german = {
            "finished": ("Druck fertig", "Job auf Printer ist fertig."),
            "failed": ("Druck fehlgeschlagen", "Job auf Printer wurde nicht fertig."),
            "cancelled": ("Druck abgebrochen", "Job auf Printer wurde abgebrochen."),
            "paused": ("Druck pausiert", "Job auf Printer ist pausiert."),
            "error": ("Meldung von Printer", "Der Drucker meldet einen Fehler."),
        }
        for language, templates in (("en", english), ("de", german)):
            for template, (title, body) in templates.items():
                self.assertEqual(alert(template, "Job", "Printer", None, language), {"title": title, "body": body})
            for template in ("failed", "paused"):
                self.assertEqual(alert(template, "Job", "Printer", "Detail", language)["body"],
                                 f"Job {'auf' if language == 'de' else 'on'} Printer: Detail")


class RequestLanguageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(data_dir=Path(self.tmp.name), profiles_dir=PROFILES, filament_ids=FILAMENT_IDS,
                             bridge_token="test-access-key-with-enough-characters", tls=False, mdns=False)
        self.app = create_app(self.config, start_background=False)
        self.app.freeze()
        self.auth = {"Authorization": f"Bearer {app_token(self.config.bridge_token)}"}

    async def asyncTearDown(self):
        await self.app.cleanup()
        self.tmp.cleanup()

    async def call(self, path, *, language=None, method="GET", body=None, authenticated=True):
        headers = dict(self.auth) if authenticated else {}
        if language is not None:
            headers["Accept-Language"] = language
        request = make_mocked_request(method, path, headers=headers, app=self.app)
        if body is not None:
            request.json = AsyncMock(return_value=body)
        return await self.app._handle(request)

    async def test_api_errors_follow_request_and_default_to_english(self):
        for language, expected in ((None, "Authentication required"), ("de-DE", "Anmeldung erforderlich"),
                                   ("fr", "Authentication required")):
            response = await self.call("/api/status", language=language, authenticated=False)
            self.assertEqual(response.status, 401)
            self.assertEqual(json.loads(response.text)["error"], expected)
        self.assertEqual(current_language(), "en")

    async def test_setup_and_invitation_pages(self):
        for language, label in (("en", "Printer"), ("de", "Drucker")):
            response = await self.call("/", language=language)
            self.assertIn(f'<html lang="{language}">', response.text)
            self.assertIn(f'>{label}</p>', response.text)
            self.assertEqual(response.headers["Content-Language"], language)
            with language_context(language):
                page = invitation_page(identity=BridgeIdentity("test", "Bridge"), link="https://example.test",
                                       code="ABCDEFGH", minutes=10)
            self.assertIn(f'<html lang="{language}">', page)
            self.assertIn("Gültig 10 Minuten" if language == "de" else "Valid for 10 minutes", page)

    async def test_status_labels_follow_reader(self):
        value = normalize({"gcode_state": "RUNNING", "stg_cur": 0, "spd_lvl": 1}, connected=True,
                          last_message_at=None, printer={})

        async def handler(_request):
            from bridge.server import ok
            return ok(value)

        for language, stage, speed in (("en", "Printing", "Silent"), ("de", "Druckt", "Leise")):
            request = make_mocked_request("GET", "/api/status", headers={"Accept-Language": language})
            response = await language_middleware(request, handler)
            data = json.loads(response.text)
            self.assertEqual(data["job"]["stage"], stage)
            self.assertEqual(data["speed"]["name"], speed)

    async def test_job_messages_survive_persistence_and_reader_language(self):
        store = self.app[JOBS]
        job = store.create("part.stl")
        with language_context("de"):
            job.error = message_of(RuntimeError(m("Slicing failed: {p0}", p0=m("Printer not connected."))))
            job.step = m("Waiting for the printer")
            job.result = {"warnings": [m("Plate {p0} does not exist.", p0=2)]}
            store.save(job)
        store.jobs[job.id] = JobStore(Path(self.tmp.name)).get(job.id)
        for language, expected in (("en", "Slicing failed: Printer not connected."),
                                   ("de", "Slicen fehlgeschlagen: Drucker nicht verbunden.")):
            response = await self.call(f"/api/jobs/{job.id}", language=language)
            data = json.loads(response.text)
            self.assertEqual(data["error"], expected)
            self.assertIsInstance(data["step"], str)
            self.assertEqual(data["result"]["warnings"],
                             ["Platte 2 gibt es nicht." if language == "de" else "Plate 2 does not exist."])

    async def test_background_slice_errors_are_not_captured_in_request_language(self):
        store = self.app[JOBS]
        job = store.create("part.stl")
        job.settings = {"machine": "missing"}
        with language_context("de"):
            await _run_slice(self.app, job)
        self.assertIn("Printer profile", render(job.error, "en"))
        self.assertIn("Druckerprofil", render(job.error, "de"))
        self.assertIsInstance(JobStore(Path(self.tmp.name)).get(job.id).error, str)

    async def test_sse_uses_each_reader_language(self):
        job = self.app[JOBS].create("part.stl")
        job.error = m("The bridge restarted.")
        data = job.public()
        for language, expected in (("en", "The bridge restarted."), ("de", "Bridge wurde neu gestartet.")):
            response = AsyncMock()
            with language_context(language):
                await _send_event(response, "job", data)
            encoded = response.write.call_args.args[0].decode()
            self.assertEqual(json.loads(encoded.split("data: ", 1)[1])["error"], expected)

    async def test_registration_defaults_validates_and_stores_language(self):
        for index, (language, expected) in enumerate(((None, "en"), ("de", "de"), ("en", "en"))):
            for kind in ("start", "alert"):
                body = {"kind": kind, "token": f"{index + 1:02x}" * 32, "environment": "production", "events": ["finished"]}
                if language is not None:
                    body["language"] = language
                response = await self.call("/api/live/register", method="POST", body=body, language="de")
                self.assertEqual(response.status, 200)
                entries = self.app[PUSH].start if kind == "start" else self.app[PUSH].alerts
                self.assertEqual(entries[body["token"]]["language"], expected)
        response = await self.call("/api/live/register", method="POST", language="de",
                                   body={"language": "fr"})
        self.assertEqual(response.status, 422)
        self.assertEqual(json.loads(response.text)["error"], "language muss en oder de sein.")

    async def test_middleware_resets_language_after_failure_and_between_tasks(self):
        async def fail(_request):
            self.assertEqual(current_language(), "de")
            raise RuntimeError("test failure")
        request = make_mocked_request("GET", "/", headers={"Accept-Language": "de"})
        with self.assertRaises(RuntimeError):
            await language_middleware(request, fail)
        self.assertEqual(current_language(), "en")
        async def read(language):
            async def handler(_request):
                await asyncio.sleep(0)
                from bridge.server import ok
                return ok({"text": m("Printer not connected.")})
            request = make_mocked_request("GET", "/", headers={"Accept-Language": language})
            return json.loads((await language_middleware(request, handler)).text)["text"]
        self.assertEqual(await asyncio.gather(read("de"), read("en")),
                         ["Drucker nicht verbunden.", "Printer not connected."])


class PushLanguageTests(unittest.IsolatedAsyncioTestCase):
    async def test_per_token_start_update_end_and_notifications(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = Registry(Path(directory) / "push.json")
            for language in ("en", "de"):
                registry.add("start", language, "production", device=language, language=language)
                registry.add("alert", language, "production", device=language, language=language,
                             events=["paused", "failed", "finished", "error"])
            apns = FakeAPNs()
            live = LiveActivities(apns, registry, lambda: "P1S", "serial")
            await live.process(status(state="IDLE"), now=1000)
            await live.process(status(stage=STAGES[1]), now=1001)
            for token, _, payload, _ in apns.sent:
                self.assertEqual(payload["language"], token)
                self.assertEqual(payload["aps"]["alert"]["title"], "Druck gestartet" if token == "de" else "Print Started")
                self.assertEqual(payload["aps"]["content-state"]["stage"],
                                 "Automatische Bettnivellierung" if token == "de" else "Auto bed leveling")
            for language in ("en", "de"):
                registry.add("update", language, "production", printer_id="serial", device=language, language=language)
            paused = status(state="PAUSE", stage=STAGES[6])
            paused["job"]["error"] = {"code": "0700_8011", "internal": False,
                "text": LocalizedText("Filament ran out. Replace it.", "Filament ist aufgebraucht. Neues einlegen.")}
            await live.process(paused, now=1002)
            updates = [entry for entry in apns.sent if entry[2]["aps"]["event"] == "update"]
            self.assertEqual(len(updates), 2)
            for token, _, payload, _ in updates:
                self.assertEqual(payload["aps"]["content-state"]["error"],
                                 "Filament ist aufgebraucht." if token == "de" else "Filament ran out.")
            for token, _, item in apns.notes:
                self.assertEqual(item["language"], token)
                self.assertEqual(apns_payload(item)["aps"]["alert"]["title"],
                                 "Druck pausiert" if token == "de" else "Print Paused")
            await live.process(status(state="FINISH"), now=1003)
            final = [entry for entry in apns.sent[-2:] if entry[2]["aps"]["event"] == "update"]
            self.assertEqual(len(final), 2)
            for token, _, payload, _ in final:
                self.assertEqual(payload["language"], token)
            reloaded = Registry(registry.path)
            self.assertEqual(reloaded.start["de"]["language"], "de")
            self.assertEqual(reloaded.alerts["en"]["language"], "en")

    def test_existing_tokens_default_to_english(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "push.json"
            entry = {"device": "phone", "environment": "production"}
            path.write_text(json.dumps({"start": {"s": entry}, "alerts": {"a": entry},
                                        "printers": {"serial": {"activities": {"u": entry}}}}))
            registry = Registry(path)
            self.assertEqual(registry.start["s"]["language"], "en")
            self.assertEqual(registry.alerts["a"]["language"], "en")
            self.assertEqual(registry.section("serial").activities["u"]["language"], "en")

    async def test_update_registration_language(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            from aiohttp import web
            from bridge.server import DEVICES
            registry = Registry(Path(directory) / "push.json")
            app = web.Application()
            app[PUSH] = registry
            app[DEVICES] = SimpleNamespace(devices={"serial": object()}, default_id="serial")
            request = make_mocked_request("POST", "/api/live/register", app=app)
            request[PRINCIPAL] = Principal("device", "device")
            request.json = AsyncMock(return_value={"kind": "update", "token": "ab" * 32,
                                                   "environment": "production", "language": "de"})
            self.assertEqual((await live_register(request)).status, 200)
            self.assertEqual(Registry(registry.path).section("serial").activities["ab" * 32]["language"], "de")
