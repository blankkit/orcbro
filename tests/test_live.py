import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

import httpx
import jwt
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from bridge.apns import APNsClient, APNsConfig, APNsError
from bridge.auth import Principal
from bridge.config import Config
from bridge.live import ATTRIBUTES_TYPE, LiveActivities, Registry, content_state
from bridge.printer import Printer
from bridge.server import app_token, create_app
from helpers import FILAMENT_IDS, PROFILES, load


def status(state="RUNNING", progress=10, layer=3, name="Deckel", stage=None, print_error=0, hms=(), connected=True):
    return {
        "connection": {"connected": connected},
        "job": {"state": state, "name": name, "file": "deckel.gcode.3mf", "progress": progress, "layer": layer,
                "total_layers": 40, "remaining_minutes": 20, "eta": 1_800_000_000.4, "stage": stage,
                "print_error": print_error},
        "hms": list(hms),
    }


class FakeAPNs:
    def __init__(self):
        self.sent: list[tuple[str, str, dict, int]] = []
        self.notes: list[tuple[str, str, dict]] = []
        self.gone: set[str] = set()

    async def send_live_activity(self, token, environment, payload, priority=5):
        if token in self.gone:
            raise APNsError(410, "Unregistered")
        self.sent.append((token, environment, payload, priority))

    async def send_notification(self, token, environment, item):
        if token in self.gone:
            raise APNsError(410, "Unregistered")
        self.notes.append((token, environment, item))

    def events(self, token=None):
        return [(p["aps"]["event"], prio) for t, _, p, prio in self.sent if token is None or t == token]


class ContentStateTests(unittest.TestCase):
    def test_maps_status_to_swift_keys(self):
        state = content_state(status(stage='Leveling the bed'))
        self.assertEqual(set(state), {"phase", "jobName", "progress", "layer", "totalLayers", "remainingMinutes",
                                      "finishAt", "stage", "error"})
        self.assertEqual(state["finishAt"], 1_800_000_000)  # Whole Unix seconds, not a Date encoding.
        self.assertEqual(state["jobName"], "Deckel")
        self.assertIsNone(state["error"])

    def test_error_texts_and_name_fallback(self):
        self.assertEqual(content_state(status(state="FAILED", print_error=0x0300800A))["error"], 'Error code 0300800A')
        self.assertEqual(content_state(status(state="FAILED"))["error"], 'Print canceled')
        self.assertEqual(content_state(status(hms=[{"code": "0300_0100_0001_0007"}]))["error"],
                         'Message 0300_0100_0001_0007')
        self.assertEqual(content_state(status(name=None))["jobName"], "deckel")

    def test_error_uses_first_sentence_and_skips_internal(self):
        failed = status(state="PAUSE", print_error=0x07008011)
        failed["job"]["error"] = {"code": "0700_8011", "value": 0x07008011, "internal": False,
                                  "text": "Das AMS Filament ist aufgebraucht. Bitte setzen Sie ein neues Filament ein."}
        self.assertEqual(content_state(failed)["error"], "Das AMS Filament ist aufgebraucht.")
        failed["job"]["error"]["text"] = None
        self.assertEqual(content_state(failed)["error"], 'Error code 0700_8011')
        internal = status(state="RUNNING", print_error=0x05024007,
                          hms=[{"code": "0500_0100_0002_0003", "internal": True}])
        internal["job"]["error"] = {"code": "0502_4007", "value": 0x05024007, "internal": True, "text": None}
        self.assertIsNone(content_state(internal)["error"])  # Keep internal messages off the Lock Screen.


class LiveActivityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "push.json"
        self.apns = FakeAPNs()
        self.registry = Registry(self.path)
        self.registry.add("start", "aa" * 32, "development")
        self.live = LiveActivities(self.apns, self.registry, lambda: "Werkstatt")

    def tearDown(self):
        self.tmp.cleanup()

    async def test_print_lifecycle(self):
        await self.live.process(status(state="IDLE"), now=1000)
        self.assertEqual(self.apns.sent, [])  # No print, no push.
        await self.live.process(status(state="PREPARE", progress=0, stage='Leveling the bed'), now=1001)
        token, env, payload, prio = self.apns.sent[-1]
        aps = payload["aps"]
        self.assertEqual((token, env, aps["event"], prio), ("aa" * 32, "development", "start", 10))
        self.assertEqual(aps["attributes-type"], ATTRIBUTES_TYPE)
        self.assertEqual(aps["attributes"], {"printerName": "Werkstatt", "printerId": ""})
        self.assertEqual(aps["alert"]["title"], 'Print Started')  # Required for push-to-start.
        self.assertEqual(aps["content-state"]["stage"], 'Leveling the bed')
        self.assertGreater(aps["stale-date"], aps["timestamp"])

        # The app registers the newly started activity's token.
        self.registry.add("update", "bb" * 32, "development", "act-1")
        await self.live.process(status(state="RUNNING", progress=1), now=1010)  # State changes are immediate.
        self.assertEqual(self.apns.events("bb" * 32), [("update", 10)])
        await self.live.process(status(state="RUNNING", progress=2), now=1020)  # Throttle progress-only updates.
        self.assertEqual(len(self.apns.events("bb" * 32)), 1)
        await self.live.process(status(state="RUNNING", progress=3), now=1075)
        self.assertEqual(self.apns.events("bb" * 32)[-1], ("update", 5))
        await self.live.process(status(state="PAUSE", progress=3), now=1080)  # Pause is immediate.
        self.assertEqual(self.apns.events("bb" * 32)[-1], ("update", 10))

        # Finished: final status as an update, so the activity can still be removed later.
        await self.live.process(status(state="FINISH", progress=100), now=2000)
        _, _, payload, _ = self.apns.sent[-1]
        self.assertEqual(payload["aps"]["event"], "update")
        self.assertEqual(payload["aps"]["content-state"]["phase"], "FINISH")
        self.assertGreater(payload["aps"]["stale-date"], 2000 + 3600)
        await self.live.process(status(state="FINISH", progress=100), now=2010)
        self.assertEqual(len(self.apns.events("bb" * 32)), 4)  # No second final update.
        # Nothing new for an hour: end and remove it.
        await self.live.process(status(state="FINISH", progress=100), now=2000 + 3600)
        _, _, payload, _ = self.apns.sent[-1]
        self.assertEqual((payload["aps"]["event"], payload["aps"]["dismissal-date"]), ("end", 2000 + 3600))
        self.assertEqual(payload["aps"]["content-state"]["phase"], "FINISH")
        self.assertIsNone(self.live.push.current)
        self.assertEqual(self.live.push.activities, {})
        await self.live.process(status(state="FINISH", progress=100), now=6000)
        self.assertEqual(len(self.apns.events("bb" * 32)), 5)

    async def test_next_print_replaces_the_finished_one(self):
        """A print started (for example in OrcaSlicer) after a finished one: one activity, not two."""
        await self.live.process(status(state="RUNNING", progress=50), now=1000)
        self.registry.add("update", "bb" * 32, "development", "act-1")
        await self.live.process(status(state="FINISH", progress=100), now=2000)
        nxt = status(state="PREPARE", progress=0)
        nxt["job"]["name"] = "Zweiter Druck"
        await self.live.process(nxt, now=2100)
        events = [(token, payload["aps"]["event"]) for token, _, payload, _ in self.apns.sent]
        self.assertEqual(events[-2:], [("bb" * 32, "end"), ("aa" * 32, "start")])
        ended = self.apns.sent[-2][2]["aps"]
        self.assertEqual(ended["dismissal-date"], 2100)  # Removed right away …
        self.assertEqual(ended["content-state"]["phase"], "FINISH")  # … showing the old print, not the new one.
        self.assertEqual(self.live.push.activities, {})
        self.assertNotIn("ended", self.live.push.current)

    async def test_same_file_printed_again_is_a_new_print(self):
        await self.live.process(status(state="RUNNING"), now=1000)
        await self.live.process(status(state="FINISH"), now=2000)
        await self.live.process(status(state="RUNNING"), now=2100)
        self.assertEqual([e for e, _ in self.apns.events()].count("start"), 2)

    async def test_restart_mid_print_does_not_start_again(self):
        await self.live.process(status(state="RUNNING"), now=1000)
        self.assertEqual(self.apns.events(), [("start", 10)])
        again = LiveActivities(self.apns, Registry(self.path), lambda: "Werkstatt")
        await again.process(status(state="RUNNING"), now=1100)
        self.assertEqual([e for e, _ in self.apns.events()].count("start"), 1)

    async def test_disconnected_printer_changes_nothing(self):
        await self.live.process(status(state="RUNNING"), now=1000)
        await self.live.process(status(state="IDLE", connected=False), now=1100)
        self.assertIsNotNone(self.live.push.current)

    async def test_printers_are_separate_and_old_file_is_adopted(self):
        # Adopt old single-printer push.json current print and tokens into the default printer.
        self.path.write_text('{"start": {}, "current": {"key": "Deckel|", "started": 1}, '
                             '"activities": {"%s": {"environment": "development"}}}' % ("cc" * 32))
        registry = Registry(self.path)
        first = LiveActivities(self.apns, registry, lambda: "Eins", "01P0001", adopt=True)
        second = LiveActivities(self.apns, registry, lambda: "Zwei", "01P0002", adopt=False)
        self.assertEqual(first.push.current["key"], "Deckel|")
        self.assertIsNone(second.push.current)
        registry.add("update", "dd" * 32, "development", "act-2", "01P0002")
        self.assertIn("dd" * 32, second.push.activities)
        self.assertNotIn("dd" * 32, first.push.activities)
        registry.save()
        again = Registry(self.path)
        self.assertEqual(set(again.printers), {"01P0001", "01P0002"})

    async def test_gone_tokens_are_forgotten(self):
        self.apns.gone.add("aa" * 32)
        await self.live.process(status(state="RUNNING"), now=1000)
        self.assertEqual(Registry(self.path).start, {})


class APNsClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        key = ec.generate_private_key(ec.SECP256R1())
        self.public = key.public_key()
        path = Path(self.tmp.name) / "AuthKey.p8"
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
        self.config = APNsConfig.load(path, "KEYID12345", "TEAMID1234", "it.example.app")
        self.requests: list[httpx.Request] = []
        self.reply = (200, {})

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            status_code, body = self.reply
            return httpx.Response(status_code, json=body)

        self.client = APNsClient(self.config, transport=httpx.MockTransport(handler))

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_request_shape(self):
        await self.client.send_live_activity("ab" * 32, "development", {"aps": {"event": "update"}}, priority=10)
        request = self.requests[0]
        self.assertEqual(str(request.url), f"https://api.sandbox.push.apple.com/3/device/{'ab' * 32}")
        self.assertEqual(request.headers["apns-push-type"], "liveactivity")
        self.assertEqual(request.headers["apns-topic"], "it.example.app.push-type.liveactivity")
        self.assertEqual(request.headers["apns-priority"], "10")
        token = request.headers["authorization"].removeprefix("bearer ")
        self.assertEqual(jwt.get_unverified_header(token)["kid"], "KEYID12345")
        claims = jwt.decode(token, self.public, algorithms=["ES256"])
        self.assertEqual(claims["iss"], "TEAMID1234")
        self.assertEqual(json.loads(request.content), {"aps": {"event": "update"}})
        await self.client.send_live_activity("ab" * 32, "production", {"aps": {}})
        self.assertTrue(str(self.requests[1].url).startswith("https://api.push.apple.com/"))

    async def test_errors(self):
        self.reply = (410, {"reason": "Unregistered"})
        with self.assertRaises(APNsError) as gone:
            await self.client.send_live_activity("ab" * 32, "production", {"aps": {}})
        self.assertTrue(gone.exception.token_gone)
        self.reply = (403, {"reason": "InvalidProviderToken"})
        with self.assertRaises(APNsError) as auth:
            await self.client.send_live_activity("ab" * 32, "production", {"aps": {}})
        self.assertFalse(auth.exception.token_gone)

    def test_config_needs_everything(self):
        self.assertIsNone(APNsConfig.load(None, "K", "T", "b"))
        self.assertIsNone(APNsConfig.load(Path("/nicht/da.p8"), "K", "T", "b"))


class LiveEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        config = Config(host="192.0.2.10", serial="01P00A000000000", access_code="12345678",
                        bridge_token="s3cret-bridge-access-token", data_dir=Path(self.tmp.name), profiles_dir=PROFILES,
                        filament_ids=FILAMENT_IDS, orca_cmd=(sys.executable, "-c", "pass"))
        printer = Printer(config.host, config.serial, config.access_code, "Test")
        printer.handle(load("pushall_p1s.json"))
        printer.connected = True
        self.apns = FakeAPNs()
        self.live = LiveActivities(self.apns, Registry(Path(self.tmp.name) / "push.json"))
        app = create_app(config, printer=printer, live=self.live, start_background=False)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.auth = {"Authorization": f"Bearer {app_token(config.bridge_token)}"}

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_register_and_status(self):
        post = lambda body: self.client.post("/api/live/register", json=body, headers=self.auth)
        self.assertEqual((await post({"kind": "start", "token": "AB" * 32, "environment": "production"})).status, 200)
        self.assertEqual(self.live.registry.start["ab" * 32]["environment"], "production")
        self.assertEqual((await post({"kind": "x", "token": "ab" * 32, "environment": "production"})).status, 422)
        self.assertEqual((await post({"kind": "start", "token": "zz" * 32, "environment": "production"})).status, 422)
        self.assertEqual((await post({"kind": "start", "token": "ab" * 32, "environment": "staging"})).status, 422)
        info = await (await self.client.get("/api/live", headers=self.auth)).json()
        self.assertEqual((info["enabled"], info["devices"]), (True, 1))
        self.assertEqual((await self.client.post("/api/live/register", json={})).status, 401)

    async def test_revoked_device_loses_its_push_tokens(self):
        """Revoked iPhones must stop receiving Live Activity messages."""
        from bridge.server import AUTH
        app = self.client.server.app
        token, device = await app[AUTH].pair(app[AUTH].setup_code or (await app[AUTH].invite(Principal("access_key"))).code,
                                             "iPhone")
        own = {"Authorization": f"Bearer {token}"}
        body = {"kind": "start", "token": "cd" * 32, "environment": "production"}
        self.assertEqual((await self.client.post("/api/live/register", json=body, headers=own)).status, 200)
        await self.client.post("/api/live/register", json={**body, "token": "ef" * 32}, headers=self.auth)
        self.assertEqual(self.live.registry.start["cd" * 32]["device"], device.id)
        self.assertEqual(self.live.registry.start["ef" * 32]["device"], "access_key")
        deleted = await self.client.delete(f"/api/devices/{device.id}", headers=self.auth)
        self.assertEqual(deleted.status, 204)
        self.assertNotIn("cd" * 32, self.live.registry.start)
        self.assertIn("ef" * 32, self.live.registry.start)

    async def test_update_token_with_public_printer_id(self):
        from bridge.live import public_printer_id
        body = {"kind": "update", "token": "12" * 32, "environment": "production", "activity_id": "a1",
                "printer": public_printer_id("01P00A000000000")}
        self.assertEqual((await self.client.post("/api/live/register", json=body, headers=self.auth)).status, 200)
        self.assertIn("12" * 32, self.live.registry.section("01P00A000000000").activities)


class RegistrySecurityTests(unittest.TestCase):
    def test_unbound_tokens_are_dropped_and_file_is_private(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "push.json"
            path.write_text(json.dumps({"start": {"aa" * 32: {"environment": "production"}},
                                        "printers": {"S1": {"activities": {"bb" * 32: {"environment": "production"}}}}}))
            os.chmod(path, 0o644)
            registry = Registry(path)
            self.assertEqual(registry.start, {})
            self.assertEqual(registry.section("S1").activities, {})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            registry.add("start", "cc" * 32, "production", device="d1")
            registry.add("update", "dd" * 32, "production", "a", "S1", device="d2")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(registry.remove_device("d1"), 1)
            self.assertEqual(list(Registry(path).section("S1").activities), ["dd" * 32])

    def test_start_attributes_carry_no_serial(self):
        from bridge.live import public_printer_id
        self.assertNotIn("01P00A000000000", public_printer_id("01P00A000000000"))
        self.assertEqual(public_printer_id("01P00A000000000"), public_printer_id("01P00A000000000"))
        self.assertEqual(len(public_printer_id("01P00A000000000")), 17)


class StartTokenTests(unittest.TestCase):
    """Count each iPhone once even when its start token changes (Xcode, TestFlight, reinstallation)."""

    def registry(self, folder) -> Registry:
        return Registry(Path(folder) / "push.json")

    def test_new_token_of_the_same_installation_replaces_the_old_one(self):
        with tempfile.TemporaryDirectory() as folder:
            registry = self.registry(folder)
            registry.add("start", "aa" * 32, "development", installation="INST-1")
            registry.add("start", "bb" * 32, "production", installation="INST-1")
            registry.add("start", "cc" * 32, "production", installation="INST-2")
            self.assertEqual(set(registry.start), {"bb" * 32, "cc" * 32})
            self.assertEqual(registry.start_devices(), 2)

    def test_device_token_principals_keep_one_start_token(self):
        with tempfile.TemporaryDirectory() as folder:
            registry = self.registry(folder)
            registry.add("start", "aa" * 32, "development", device="d1")
            registry.add("start", "bb" * 32, "production", device="d1")
            self.assertEqual(list(registry.start), ["bb" * 32])

    def test_shared_key_without_installation_cannot_be_merged(self):
        with tempfile.TemporaryDirectory() as folder:
            registry = self.registry(folder)
            registry.add("start", "aa" * 32, "production")
            registry.add("start", "bb" * 32, "production")
            self.assertEqual(registry.start_devices(), 2)
            registry.add("start", "cc" * 32, "production", installation="INST-1")
            self.assertEqual(registry.start_devices(), 3)


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    """Notifications go only to iPhones requesting that event, with template-based text."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.apns = FakeAPNs()
        self.registry = Registry(Path(self.tmp.name) / "push.json")
        self.registry.add("alert", "a1" * 32, "production", device="d1", events=["finished", "failed", "paused", "error"])
        self.registry.add("alert", "a2" * 32, "production", device="d2", events=["finished"])
        self.live = LiveActivities(self.apns, self.registry, lambda: "Werkstatt", "01P00A0000")

    def tearDown(self):
        self.tmp.cleanup()

    def templates(self, token=None):
        return [item["template"] for t, _, item in self.apns.notes if token is None or t == token]

    async def test_finished_goes_to_everyone_who_wants_it(self):
        await self.live.process(status(state="FINISH"), now=999)   # Initial pre-print status: no notification.
        await self.live.process(status(state="RUNNING"), now=1000)
        await self.live.process(status(state="FINISH", progress=100), now=1100)
        self.assertEqual(self.templates("a1" * 32), ["finished"])
        self.assertEqual(self.templates("a2" * 32), ["finished"])
        item = self.apns.notes[0][2]
        self.assertEqual(item["job"], "Deckel")
        self.assertEqual(item["printer"], "Werkstatt")
        self.assertRegex(item["thread"], r"^p[0-9a-f]{16}$")  # Never the serial number.
        await self.live.process(status(state="FINISH", progress=100), now=1200)
        self.assertEqual(len(self.apns.notes), 2)  # Only on change.

    async def test_pause_cancel_failure_and_hms(self):
        await self.live.process(status(state="RUNNING"), now=1000)
        paused = status(state="PAUSE", hms=[{"code": "0700_2000_0002_0001", "severity": "serious", "internal": False,
                                            "text": "Filament ist aufgebraucht. Neues einlegen."}])
        await self.live.process(paused, now=1010)
        self.assertEqual(self.templates("a1" * 32), ["paused"])  # The pause notification already contains the HMS message.
        self.assertEqual(self.apns.notes[0][2]["detail"], "Filament ist aufgebraucht.")
        self.assertEqual(self.templates("a2" * 32), [])
        await self.live.process(status(state="RUNNING"), now=1020)
        hot = status(state="RUNNING", hms=[{"code": "0300_0100_0001_0007", "severity": "fatal", "internal": False,
                                           "text": "Heizbett zu heiß."},
                                          {"code": "0500_0100_0002_0003", "severity": "fatal", "internal": True}])
        await self.live.process(hot, now=1030)
        await self.live.process(hot, now=1040)
        self.assertEqual(self.templates("a1" * 32), ["paused", "error"])
        await self.live.process(status(state="FAILED", print_error=0x0300400C), now=1050)
        self.assertEqual(self.templates("a1" * 32)[-1], "cancelled")
        await self.live.process(status(state="RUNNING", name="Zweiter"), now=1060)
        failed = status(state="FAILED", name="Zweiter")
        failed["job"]["error"] = {"code": "0300_8003", "internal": False, "text": "Spaghetti erkannt. Druck gestoppt."}
        await self.live.process(failed, now=1070)
        self.assertEqual(self.apns.notes[-1][2], {"language": "en", "template": "failed", "job": "Zweiter", "printer": "Werkstatt",
                                                  "detail": "Spaghetti erkannt.", "thread": self.apns.notes[0][2]["thread"]})

    def test_registration_replaces_and_unsubscribes(self):
        self.registry.add("alert", "a3" * 32, "production", device="d1", events=["finished"])
        self.assertNotIn("a1" * 32, self.registry.alerts)  # One notification token per iPhone.
        self.assertEqual(self.registry.alert_devices(), 2)
        self.registry.add("alert", "a3" * 32, "production", device="d1", events=[])  # Notifications off.
        self.assertNotIn("a3" * 32, self.registry.alerts)
        self.assertEqual(self.registry.remove_device("d2"), 1)
        self.assertEqual(Registry(self.registry.path).alerts, {})

    async def test_gone_tokens_are_forgotten(self):
        self.apns.gone.add("a2" * 32)
        await self.live.process(status(state="RUNNING"), now=1000)
        await self.live.process(status(state="FINISH"), now=1100)
        self.assertNotIn("a2" * 32, self.registry.alerts)
        self.assertIn("a1" * 32, self.registry.alerts)


class NotifyTextTests(unittest.TestCase):
    def test_texts_match_the_relay(self):
        from bridge.notify import alert, apns_payload, note
        # Match relay/src/templates.ts, also checked by relay tests.
        self.assertEqual(alert("finished", "Deckel", "P1S", None), {"title": 'Print Finished', "body": 'Deckel on P1S is done.'})
        self.assertEqual(alert("paused", "Deckel", "P1S", "Filament leer.")["body"], 'Deckel on P1S: Filament leer.')
        item = note("error", "x" * 300, "P1S", "Zeile\nzwei", "")
        self.assertEqual(len(item["job"]), 120)
        self.assertEqual(item["detail"], "Zeile zwei")
        self.assertNotIn("thread", item)
        self.assertEqual(apns_payload(item)["aps"]["alert"]["title"], 'Message from P1S')

