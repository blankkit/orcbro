"""Lifecycle of printers and background work: replacing a printer's device, the environment printer, the MQTT
connection loop, profile catalog failures, full AMS reports, live events, cameras, discovery and shutdown."""
import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import aiomqtt
from aiohttp.test_utils import TestClient, TestServer

from bridge import mdns, server
from bridge.camera import JpegCamera, auth_packet
from bridge.camera_rtsp import RtspCamera
from bridge.config import Config
from bridge.devices import CATALOG_RETRY, Catalogs, PrinterSettings
from bridge.identity import BridgeIdentity
from bridge.jobs import JobStore, remote_name
from bridge.mdns import MDNSAdvertiser
from bridge.printer import Printer, PrinterError, client_id
from bridge.probe import ProbeError
from bridge.profiles import Catalog, ProfileError
from bridge.server import (DEVICES, EVENTS, JOBS, TASKS, _add_env_printer, _find_tray, app_token, create_app,
                           env_settings)
from bridge.slicer import Slicer
from bridge.state import merge
from bridge.trust import CHANGED, CertificateChanged
from helpers import FILAMENT_IDS, MODELS, PROFILES, load, sliced_3mf
from test_pairing_mdns import FakeZeroconf
from test_print_fixes import PrintTestCase

SECRET = "s3cret-bridge-access-token"
FIRST = "01P00A000000000"
SECOND = "01P00B000000000"
BRIDGE = "bd92e1e8-c87d-46ee-a2c5-4d859adc8597"


async def idle(*_args):
    await asyncio.Event().wait()


def running(name: str) -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if t.get_name() == name and not t.done()]


class AppTestCase(unittest.IsolatedAsyncioTestCase):
    """A bridge without network: printer, Live Activity and spool loops idle instead of connecting."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for target in ("bridge.printer.Printer.run", "bridge.live.LiveActivities.run", "bridge.spools.SpoolTracker.run"):
            patcher = mock.patch(target, idle)
            patcher.start()
            self.addCleanup(patcher.stop)

    def config(self, **values) -> Config:
        return Config(bridge_token=SECRET, data_dir=Path(self.tmp.name), profiles_dir=PROFILES,
                      filament_ids=FILAMENT_IDS, models_dir=MODELS, mdns=False, **values)

    def store_printer(self, serial: str, host: str) -> None:
        Path(self.tmp.name, "printers.json").write_text(json.dumps({"printers": [
            {"serial": serial, "host": host, "access_code": "87654321", "name": "Stored"}]}))


class DeviceReplacementTests(AppTestCase):
    async def test_new_address_keeps_spool_tracking(self):
        """QO-6: the device that replaces a printer after a new IP or access code gets its spool tracker running."""
        app = create_app(self.config(), start_background=False)
        devices = app[DEVICES]
        devices.running = True
        self.addAsyncCleanup(devices.stop)
        old = await devices.add(PrinterSettings(serial=SECOND, host="192.0.2.20", access_code="87654321"))
        tasks = [f"new-spools-{SECOND}", f"printer-{SECOND}", f"spools-{SECOND}"]
        self.assertEqual(sorted(t.get_name() for t in old.tasks), tasks)
        old_tasks = list(old.tasks)
        new = await devices.update(SECOND, host="192.0.2.21")
        self.assertIsNot(new, old)
        self.assertEqual(sorted(t.get_name() for t in new.tasks), tasks)
        self.assertTrue(all(t.done() for t in old_tasks))
        self.assertEqual(len(running(f"printer-{SECOND}")), 1)
        self.assertIs(new.live, old.live)
        self.assertIsNot(new.spools, old.spools)  # Its plan reads the new device's printer.
        self.assertIs(new.new_spools.printer, new.printer)
        self.assertEqual(new.printer.host, "192.0.2.21")

    async def test_environment_printer_replaces_a_stored_printer(self):
        """QO-7: the BAMBU_HOST printer read after startup stops the stored printer with the same serial number."""
        self.store_printer(FIRST, "192.0.2.10")
        config = self.config(host="192.0.2.10", access_code="12345678")
        app = create_app(config, start_background=False)
        devices = app[DEVICES]
        devices.start()
        self.addAsyncCleanup(devices.stop)
        stored = devices.get(FIRST)
        stored_tasks = list(stored.tasks)
        identity = mock.AsyncMock(side_effect=[ProbeError("unreachable", "No printer"), (FIRST, "aa" * 32)])
        with mock.patch("bridge.server.read_identity", identity), mock.patch("bridge.server.ENV_RETRY", 0):
            await _add_env_printer(app, env_settings(config))
        self.assertEqual(identity.await_count, 2)  # Retried until the printer answered.
        replacement = devices.get(FIRST)
        self.assertIsNot(replacement, stored)
        self.assertEqual((replacement.settings.source, replacement.access_code), ("env", "12345678"))
        self.assertTrue(all(t.done() for t in stored_tasks))
        self.assertEqual(len(running(f"printer-{FIRST}")), 1)
        self.assertIs(replacement.live, stored.live)
        self.assertEqual(devices.store.env["serial"], FIRST)
        self.assertEqual(devices.store.pins[FIRST], "aa" * 32)
        # The stored entry stays, ignored while the environment printer is configured, also after a restart.
        saved = json.loads(Path(self.tmp.name, "printers.json").read_text())
        self.assertEqual([p["serial"] for p in saved["printers"]], [FIRST])
        again = create_app(config, start_background=False)[DEVICES]
        self.assertEqual([(d.id, d.settings.source) for d in again.list()], [(FIRST, "env")])

    async def test_jobs_of_0x_bridges_get_the_default_printer(self):
        """QO-19: jobs saved before printers were configurable belong to the default printer from then on."""
        self.store_printer(SECOND, "192.0.2.20")
        jobs = Path(self.tmp.name, "jobs")
        job_id = "0123456789abcdef"
        (jobs / job_id).mkdir(parents=True)
        (jobs / job_id / "job.json").write_text(json.dumps({
            "id": job_id, "name": "Cube", "filename": "cube.gcode.3mf", "kind": "sliced", "created_at": 1.0,
            "state": "sliced"}))
        app = create_app(self.config(), start_background=False)
        self.assertEqual(app[JOBS].get(job_id).printer, SECOND)
        self.assertEqual(json.loads((jobs / job_id / "job.json").read_text())["printer"], SECOND)


class LiveRegistrationTests(AppTestCase):
    async def test_update_tokens_name_the_printer_by_its_activity_id(self):
        """QO-19: update tokens name the printer by the activity's `printerId`; serial numbers are not accepted."""
        from bridge.live import public_printer_id
        self.store_printer(SECOND, "192.0.2.20")
        client = TestClient(TestServer(create_app(self.config(), start_background=False)))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        auth = {"Authorization": f"Bearer {app_token(SECRET)}"}
        body = {"kind": "update", "token": "ab" * 32, "environment": "production", "activity_id": "a1"}
        response = await client.post("/api/live/register", json={**body, "printer": SECOND}, headers=auth)
        self.assertEqual(response.status, 422)
        response = await client.post("/api/live/register", json={**body, "printer": public_printer_id(SECOND)},
                                     headers=auth)
        self.assertEqual(response.status, 200)
        response = await client.post("/api/live/register", json={**body, "token": "cd" * 32}, headers=auth)
        self.assertEqual(response.status, 200)  # Without a printer: the default printer.
        self.assertEqual(set(client.server.app[server.PUSH].section(SECOND).activities), {"ab" * 32, "cd" * 32})


class ShutdownTests(AppTestCase):
    async def test_background_work_ends_before_printers_and_clients_close(self):
        """QO-13: shutdown cancels and awaits slicing, sending and status refreshes while printers are still open."""
        self.store_printer(SECOND, "192.0.2.20")
        app = create_app(self.config())
        client = TestClient(TestServer(app))
        await client.start_server()
        seen = {}

        async def work():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                seen["devices running"] = app[DEVICES].running
                await asyncio.sleep(0)
                seen["finished"] = True
                raise

        task = asyncio.create_task(work())
        app[TASKS].add(task)
        refresh = asyncio.create_task(idle())
        server._background.add(refresh)
        refresh.add_done_callback(server._background.discard)
        await client.close()
        self.assertEqual(seen, {"devices running": True, "finished": True})
        self.assertTrue(refresh.cancelled())
        self.assertEqual(running(f"printer-{SECOND}"), [])


class SlicerShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_canceled_slice_ends_the_slicer_and_its_children(self):
        """QO-13: canceling a slice (bridge shutdown) kills the slicer's whole process group, as the timeout does."""
        with tempfile.TemporaryDirectory() as folder:
            pids = Path(folder) / "pids"
            script = ("import os, subprocess, sys, time; child = subprocess.Popen([sys.executable, '-c', "
                      "'import time; time.sleep(60)']); open(sys.argv[1], 'w').write(f'{os.getpid()} {child.pid}'); "
                      "time.sleep(60)")
            slicer = Slicer((sys.executable, "-c", script, str(pids)))
            model = Path(folder) / "model.3mf"
            model.write_bytes(b"model")
            task = asyncio.create_task(slicer.slice(catalog=None, workdir=Path(folder) / "work", model=model,
                                                    output=Path(folder) / "out.gcode.3mf", kind="project"))
            for _ in range(200):
                if pids.exists() and pids.read_text().count(" ") == 1:
                    break
                await asyncio.sleep(0.02)
            parent, child = map(int, pids.read_text().split())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            for pid in (parent, child):
                for _ in range(100):
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    await asyncio.sleep(0.02)
                else:
                    self.fail(f"process {pid} still runs")

    async def test_slice_result_is_read_off_the_event_loop(self):
        """QO-9: reading the sliced file (all plates' G-code) runs in a worker thread."""
        threads = []
        from bridge import slicer as module
        original = module.read_slice_info

        def read(path):
            threads.append(threading.current_thread())
            return original(path)

        with tempfile.TemporaryDirectory() as folder, mock.patch("bridge.slicer.read_slice_info", read):
            model = Path(folder) / "project.3mf"
            model.write_bytes(b"project")
            info = await Slicer((sys.executable, str(Path(__file__).parent / "fake_orca.py"))).slice(
                catalog=None, workdir=Path(folder) / "work", model=model, output=Path(folder) / "out.gcode.3mf",
                kind="project")
        self.assertEqual(info["plates"][0]["index"], 1)
        self.assertEqual(len(threads), 1)
        self.assertIsNot(threads[0], threading.main_thread())


class UploadShutdownTests(PrintTestCase):
    async def test_canceled_sending_stops_the_upload_thread(self):
        """QO-13: when sending is canceled (shutdown), the FTPS upload thread stops instead of running on."""
        started, ended = threading.Event(), threading.Event()

        def upload(host, code, local, remote, progress=None, **_kw):
            try:
                started.set()
                for sent in range(1, 500):
                    progress(sent, 500)
                    time.sleep(0.01)
            finally:
                ended.set()

        job = await self.upload("a.gcode.3mf", sliced_3mf())
        with self.ftps(), mock.patch("bridge.server.ftps.upload", upload):
            response = await self.client.post(f"/api/jobs/{job['id']}/print", json={"mapping": {"1": 2}},
                                              headers=self.auth)
            self.assertEqual(response.status, 202)
            self.assertTrue(await asyncio.to_thread(started.wait, 5))
            [sending] = [t for t in self.app[TASKS] if t.get_coro().__name__ == "_send_print"]
            sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)
            self.assertTrue(await asyncio.to_thread(ended.wait, 2))
        self.assertEqual(self.app[JOBS].get(job["id"]).state, "sending")  # Resumed after the restart.


class SlotAndFileNameTests(PrintTestCase):
    async def test_true_is_not_slot_1(self):
        """QO-17: every slot lookup rejects booleans, as the controls do."""
        status = self.printer.status()
        self.assertIsNotNone(_find_tray(status, 1))
        self.assertIsNone(_find_tray(status, True))
        job = await self.upload("a.gcode.3mf", sliced_3mf())
        response = await self.client.post(f"/api/jobs/{job['id']}/print", json={"mapping": {"1": True}},
                                          headers=self.auth)
        self.assertEqual(response.status, 422)

    def test_names_without_ascii_letters(self):
        """QO-20: such files are called `print`; jobs from earlier versions keep their `druck` file. QO-18: a
        deleted job's preview lock goes with it."""
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder))
            new = store.create("日本.stl")
            self.assertEqual(store.input_path(new).name, "print.stl")
            self.assertTrue(remote_name(new.name, new.id).startswith("print_"))
            old = store.create("日本.stl")
            legacy = store.dir(old.id) / "druck.stl"
            legacy.write_bytes(b"solid")
            self.assertEqual(store.input_path(old), legacy)
            lock = store.preview_lock(old.id)
            self.assertIs(store.preview_lock(old.id), lock)
            store.delete(old.id)
            self.assertIsNot(store.preview_lock(old.id), lock)


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_host_name_on_127_0_1_1_uses_the_outgoing_interface(self):
        """QO-10: Debian-style hosts resolve their name to 127.0.1.1 only; the outgoing interface still counts."""
        entries = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.1.1", 0))]
        route = [(socket.AF_INET, "192.168.1.20"), (socket.AF_INET6, "2001:db8::20")]
        with mock.patch("bridge.mdns.socket.getaddrinfo", return_value=entries), \
                mock.patch("bridge.mdns._route_addresses", return_value=route):
            self.assertEqual(mdns._addresses(), sorted([socket.inet_aton("192.168.1.20"),
                                                        socket.inet_pton(socket.AF_INET6, "2001:db8::20")]))

    def test_outgoing_interface_without_sending(self):
        class FakeSocket:
            def __init__(self, family, kind):
                self.family = family

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def connect(self, target):
                if self.family == socket.AF_INET6:
                    raise OSError("Network is unreachable")

            def getsockname(self):
                return ("192.168.1.20", 50000)

        with mock.patch("bridge.mdns.socket.socket", FakeSocket):
            self.assertEqual(mdns._route_addresses(), [(socket.AF_INET, "192.168.1.20")])

    async def test_announces_once_an_address_appears_and_again_when_it_changes(self):
        created = []

        def factory(**_kwargs):
            created.append(FakeZeroconf())
            return created[-1]

        async def until(condition):
            for _ in range(200):
                if condition():
                    return
                await asyncio.sleep(0.01)
            self.fail("condition not reached")

        addresses = []
        advertiser = MDNSAdvertiser(identity=BridgeIdentity(BRIDGE, "Test"), port=8443, fingerprint=None,
                                    tls_enabled=False, zeroconf_factory=factory)
        with mock.patch("bridge.mdns._addresses", lambda: list(addresses)), mock.patch("bridge.mdns.REFRESH", 0.01):
            with self.assertLogs("bridge.mdns", "WARNING") as logs:
                self.assertFalse(await advertiser.start())
                await asyncio.sleep(0.05)
            self.assertEqual(len(logs.records), 1)  # Retried quietly.
            addresses.append(socket.inet_aton("192.168.1.20"))
            await until(lambda: created)
            self.assertEqual(created[0].registered[0].addresses, [socket.inet_aton("192.168.1.20")])
            addresses[:] = [socket.inet_aton("192.168.1.21")]
            await until(lambda: len(created) == 2)
            self.assertTrue(created[0].closed)
            self.assertIsNotNone(created[0].unregistered)
            self.assertEqual(created[1].registered[0].addresses, [socket.inet_aton("192.168.1.21")])
            await advertiser.stop()
        self.assertTrue(created[1].closed)
        self.assertEqual(len(created), 2)


class CatalogFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_is_logged_once_and_retried_later(self):
        """QO-12: a missing printer profile is not reloaded on every printer message."""
        catalogs = Catalogs(PROFILES, FILAMENT_IDS)
        with mock.patch("bridge.devices.Catalog", side_effect=ProfileError("missing")) as load_catalog:
            with self.assertLogs("bridge.devices", "WARNING") as logs:
                for _ in range(3):
                    with self.assertRaises(ProfileError):
                        await catalogs.get("Bambu Lab Q1 0.4 nozzle")
            self.assertEqual((load_catalog.call_count, len(logs.records)), (1, 1))
            later = time.monotonic() + CATALOG_RETRY + 1
            with mock.patch("bridge.devices.time.monotonic", return_value=later), self.assertNoLogs("bridge.devices"):
                with self.assertRaises(ProfileError):
                    await catalogs.get("Bambu Lab Q1 0.4 nozzle")
            self.assertEqual(load_catalog.call_count, 2)
        self.assertEqual((await catalogs.get("Bambu Lab P1S 0.4 nozzle")).machine_name, "Bambu Lab P1S 0.4 nozzle")

    def test_missing_printer_is_found_before_reading_all_profiles(self):
        from bridge import profiles
        folders = []
        original = profiles._load_folder

        def load_folder(folder):
            folders.append(folder.name)
            return original(folder)

        with mock.patch("bridge.profiles._load_folder", load_folder), self.assertRaises(ProfileError):
            Catalog(PROFILES, "Bambu Lab Q1 0.4 nozzle")
        self.assertEqual(folders, ["machine"])


class FullReportTests(unittest.TestCase):
    def test_ams_unit_missing_from_a_full_report_is_dropped(self):
        """QO-15: a full report (`pushall` reply, `msg` 0) lists every AMS unit; partial updates keep the others."""
        printer = Printer("192.0.2.10", FIRST, "12345678")
        printer.handle(load("pushall_p1s.json"))
        second = {"id": "1", "humidity": "4", "tray": [{"id": "0", "tray_type": "PETG"}]}
        printer.handle({"print": {"command": "push_status", "msg": 1, "ams": {"ams": [second]}}})
        self.assertEqual([u["id"] for u in printer.raw["ams"]["ams"]], ["0", "1"])
        printer.handle({"print": {"command": "push_status", "msg": 1, "ams": {"ams": [{"id": "0", "humidity": "3"}]}}})
        self.assertEqual([u["id"] for u in printer.raw["ams"]["ams"]], ["0", "1"])
        printer.handle(load("pushall_p1s.json"))
        self.assertEqual([u["id"] for u in printer.raw["ams"]["ams"]], ["0"])
        self.assertEqual([u["id"] for u in printer.status()["ams"]["units"]], [0])

    def test_full_report_keeps_merging_slots(self):
        raw = {}
        merge(raw, {"ams": {"ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "PLA"}, {"id": "1"}]}]}}, full=True)
        merge(raw, {"ams": {"ams": [{"id": "0", "tray": [{"id": "0", "remain": 50}]}]}}, full=True)
        self.assertEqual(raw["ams"]["ams"][0]["tray"], [{"id": "0", "tray_type": "PLA", "remain": 50}, {"id": "1"}])


class FakeMessage:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode()


class FakeMQTT:
    """aiomqtt.Client stand-in: each connection follows the next step of `script`."""

    def __init__(self, script: list, created: list):
        self.script, self.created = script, created

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        self.created.append(kwargs)
        self.published = []
        return self

    async def __aenter__(self):
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        self.step = step
        return self

    async def __aexit__(self, *_exc):
        return False

    async def subscribe(self, topic):
        self.topic = topic

    async def publish(self, topic, payload):
        self.published.append(json.loads(payload))

    @property
    def messages(self):
        async def stream():
            for payload in self.step["messages"]:
                yield FakeMessage(payload)
            await self.step["drop"].wait()
            raise aiomqtt.MqttError("Connection lost")
        return stream()


class PrinterConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconnects_with_backoff_and_fails_pending_commands(self):
        """QO-11: connection failures back off (2, 4 … s), a connection resets it, and a lost connection fails the
        commands that wait for a reply."""
        drop = asyncio.Event()
        report = {"print": {"command": "push_status", "msg": 0, "gcode_state": "IDLE"}}
        script = [aiomqtt.MqttError("Connection refused"), aiomqtt.MqttError("Connection refused"),
                  {"messages": [report], "drop": drop}, asyncio.CancelledError()]
        created, delays = [], []
        real_sleep = asyncio.sleep

        async def sleep(delay):
            if delay >= 60:  # The full-status refresh: wait for the test instead.
                await asyncio.Event().wait()
            if delay:
                delays.append(delay)
            await real_sleep(0)

        printer = Printer("192.0.2.10", FIRST, "12345678", bridge_id=BRIDGE)
        changes = []
        printer.subscribe(lambda: changes.append(printer.connected))
        with mock.patch("bridge.printer.aiomqtt.Client", FakeMQTT(script, created)), \
                mock.patch("bridge.printer.asyncio.sleep", sleep):
            task = asyncio.create_task(printer.run())
            for _ in range(100):
                if printer.raw.get("gcode_state") == "IDLE":
                    break
                await real_sleep(0.01)
            self.assertTrue(printer.connected)
            self.assertEqual(delays, [2, 4])
            command = asyncio.create_task(printer.command("print", {"command": "pause"}, wait=30))
            await real_sleep(0.01)
            drop.set()
            with self.assertRaises(PrinterError):
                await command
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(delays, [2, 4, 2])  # Reset after the successful connection.
        self.assertFalse(printer.connected)
        self.assertEqual(changes[-1], False)
        self.assertEqual({c["identifier"] for c in created}, {client_id(FIRST, BRIDGE)})
        self.assertEqual(printer.last_error, "Connection lost")

    def test_client_id_is_short_and_unique_per_bridge(self):
        """QO-14: stable for one bridge and printer, different for another bridge, within MQTT's 23 characters."""
        ours = client_id(FIRST, BRIDGE)
        self.assertEqual(ours, "orcbro-bd92e1e8-000000")
        self.assertEqual(client_id(FIRST, BRIDGE), ours)
        self.assertNotEqual(client_id(FIRST, "0c2d3e4f-0000-4000-8000-000000000000"), ours)
        self.assertLessEqual(max(len(ours), len(client_id(FIRST))), 23)

    def test_wrong_access_code_is_recognized_like_the_probe(self):
        """QO-17: both connection paths read the broker's refusal the same way."""
        from bridge.printer import _describe
        from bridge.probe import _mqtt_error
        for text in ("[code:4] Bad user name or password", "[code:5] Not authorized", "[code:134] Bad user name or password"):
            self.assertIn("Incorrect access code", _describe(aiomqtt.MqttError(text)))
            self.assertEqual(_mqtt_error(aiomqtt.MqttError(text), "192.0.2.10").code, "access_code")


class EventStreamTests(PrintTestCase):
    async def read_events(self, response, seconds: float) -> list[tuple[str, dict | None]]:
        """Events (and pings as `ping`) that arrive within `seconds`."""
        found, lines = [], []
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            try:
                line = (await asyncio.wait_for(response.content.readline(), left)).decode().rstrip("\n")
            except TimeoutError:
                break
            if line == ": ping":
                found.append(("ping", None))
            elif line:
                lines.append(line)
            elif lines:
                event = lines[0].removeprefix("event: ")
                found.append((event, json.loads(lines[1].removeprefix("data: "))))
                lines = []
        return found

    async def test_status_is_throttled_per_printer_and_pings_keep_the_stream_open(self):
        """QO-11: status events at most every STATUS_GAP with the last change last, other printers filtered out,
        job events right away, and a ping after EVENT_PING seconds of silence."""
        with mock.patch("bridge.server.STATUS_GAP", 0.3), mock.patch("bridge.server.EVENT_PING", 0.5):
            response = await self.client.get("/api/events", headers=self.auth)
            self.addCleanup(response.close)
            first = await self.read_events(response, 0.1)
            self.assertEqual([e for e, _ in first], ["status"])
            hub = self.app[EVENTS]
            for progress in range(5):
                self.status(mc_percent=progress * 10)
                hub.publish("status", {"printer": self.dev.id})
            hub.publish("status", {"printer": "01P00Z000000000"})
            hub.publish("job", {"id": "j1", "state": "sliced"})
            events = await self.read_events(response, 0.45)
            self.assertEqual([e for e, _ in events], ["job", "status"])
            self.assertEqual(events[1][1]["job"]["progress"], 40)
            self.assertEqual([e for e, _ in await self.read_events(response, 0.7)], ["ping"])


class CameraLoopTests(unittest.IsolatedAsyncioTestCase):
    JPEG = b"\xff\xd8frame\xff\xd9"

    def setUp(self):
        self.sleeps = []
        real_sleep = asyncio.sleep
        self.pause = real_sleep

        async def sleep(delay):
            self.sleeps.append(delay)
            await real_sleep(0.01)
        self.sleep = sleep

    async def test_jpeg_camera_logs_in_streams_and_stops_without_viewers(self):
        """QO-11: the port-6000 loop sends the login, passes frames on and ends after the last viewer left."""
        reader = asyncio.StreamReader()
        for data in (b"not a jpeg", self.JPEG):
            reader.feed_data(len(data).to_bytes(4, "little") + bytes(12) + data)
        writer = mock.Mock(drain=mock.AsyncMock())
        with mock.patch("bridge.camera.asyncio.open_connection", mock.AsyncMock(return_value=(reader, writer))), \
                mock.patch("bridge.camera.IDLE_STOP", 0.05), mock.patch("bridge.camera.asyncio.sleep", self.sleep):
            camera = JpegCamera("192.0.2.10", "12345678")
            frames = camera.frames()
            self.assertEqual(await asyncio.wait_for(anext(frames), 2), self.JPEG)
            await frames.aclose()
            await self.pause(0.1)  # No viewer for longer than IDLE_STOP.
            reader.feed_eof()  # The printer closes the connection.
            await asyncio.wait_for(camera._task, 2)
        writer.write.assert_called_once_with(auth_packet("12345678"))
        writer.close.assert_called()
        self.assertIn("closed the connection", camera.error)
        self.assertEqual(self.sleeps, [2])
        self.assertFalse(camera.active)

    async def test_rtsp_camera_checks_the_pin_before_ffmpeg(self):
        """QO-11: the RTSPS loop reads MJPEG from ffmpeg, stops it when the viewer leaves, and does not start it
        when the camera port presents another certificate."""
        process = mock.Mock(returncode=None, wait=mock.AsyncMock())
        process.stdout = asyncio.StreamReader()
        process.stdout.feed_data(b"noise" + self.JPEG[:4])
        process.stdout.feed_data(self.JPEG[4:])
        started = mock.AsyncMock(return_value=process)
        trust = mock.Mock(check=mock.AsyncMock())
        with mock.patch("bridge.camera_rtsp.asyncio.create_subprocess_exec", started), \
                mock.patch("bridge.camera_rtsp.IDLE_STOP", 0.05), mock.patch("bridge.camera_rtsp.asyncio.sleep", self.sleep):
            camera = RtspCamera("192.0.2.10", "12345678", trust=trust)
            frames = camera.frames()
            self.assertEqual(await asyncio.wait_for(anext(frames), 2), self.JPEG)
            await frames.aclose()
            await self.pause(0.1)
            process.stdout.feed_eof()  # ffmpeg exits.
            await asyncio.wait_for(camera._task, 2)
            trust.check.assert_awaited_with("192.0.2.10", 322)
            self.assertEqual(started.await_args.args[0], "ffmpeg")
            process.terminate.assert_called()

            started.reset_mock()
            trust.check.side_effect = CertificateChanged(CHANGED)
            camera = RtspCamera("192.0.2.10", "12345678", trust=trust)
            frames = camera.frames()
            with self.assertLogs("bridge.camera_rtsp", "WARNING"), self.assertRaises(TimeoutError):
                await asyncio.wait_for(anext(frames), 0.1)
            await frames.aclose()
            await asyncio.wait_for(camera.close(), 2)
        started.assert_not_awaited()
        self.assertEqual(camera.error, CHANGED)


if __name__ == "__main__":
    unittest.main()
