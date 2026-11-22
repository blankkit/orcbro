"""Pre-release hardening: pairing, request limits, 3MF bounds, printer identity, cameras, spools and logs."""
import asyncio
import io
import json
import sys
import tempfile
import time
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock

import httpx
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from bridge import auth, preview
from bridge.apns import APNsClient, APNsConfig, APNsError
from bridge.auth import InvalidCode, PairingStore, RateLimited
from bridge.camera_rtsp import RTSPS_PORT, RtspCamera
from bridge.config import Config
from bridge.devices import PrinterSettings, PrinterStore
from bridge.i18n import m, pack, render, unpack
from bridge.identity import BridgeIdentity
from bridge.jobs import (MAX_FILAMENTS, JobError, JobStore, gcode_lines, project_filaments, read_slice_info)
from bridge.printer import Printer
from bridge.probe import ProbeError, local_address, probe
from bridge.server import AUTH, CONFIG, DEVICES, SPOOLS, UPLOADS, _source, app_token, create_app
from bridge.spools import SpoolStore
from bridge.tls import TLSState
from bridge.trust import CHANGED, CertificateChanged
from helpers import FILAMENT_IDS, MODELS, PROFILES, load, sliced_3mf

SECRET = "s3cret-bridge-access-token"
SERIAL = "01P00A000000000"
OTHER = "01P00A999999999"


async def no_wait(_seconds):
    return None


class ThrottleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1_795_251_600.0

    def tearDown(self):
        self.tmp.cleanup()

    def store(self, secret=None, sleep=no_wait):
        return PairingStore(Path(self.tmp.name) / "auth.json", secret, clock=lambda: self.now, sleep=sleep)

    async def test_one_client_cannot_lock_out_another(self):
        store = self.store()
        for _ in range(9):
            with self.assertRaises(InvalidCode):
                await store.pair("AAAA-AAAA", "Telefon", "198.51.100.7")
        with self.assertRaises(RateLimited):
            await store.pair("AAAA-AAAA", "Telefon", "198.51.100.7")
        # The blocked client is refused even with the right code; another client still pairs.
        with self.assertRaises(RateLimited):
            await store.pair(store.setup_code, "Telefon", "198.51.100.7")
        self.assertEqual(store.retry_after("198.51.100.7"), auth.BLOCK_SECONDS)
        self.assertIsNone(store.retry_after("192.0.2.4"))
        token, _device = await store.pair(store.setup_code, "Telefon", "192.0.2.4")
        self.assertTrue(token)

    async def test_fixed_key_uses_the_same_throttle(self):
        store = self.store(SECRET)
        await store.check_access_key(f" {SECRET} ", "192.0.2.4")
        for _ in range(9):
            with self.assertRaises(InvalidCode):
                await store.check_access_key("falsch", "198.51.100.7")
        with self.assertRaises(RateLimited):
            await store.check_access_key("falsch", "198.51.100.7")
        with self.assertRaises(RateLimited):
            await store.check_access_key(SECRET, "198.51.100.7")
        with self.assertRaises(InvalidCode):
            await self.store().check_access_key(SECRET, "192.0.2.4")  # No fixed key configured.

    async def test_setup_code_expires_and_check_does_not_use_it_up(self):
        store = self.store()
        code = store.setup_code
        self.assertEqual(await store.check_setup(code.lower(), "192.0.2.4"), code)
        self.assertEqual(store.setup_code, code)
        with self.assertRaises(InvalidCode):
            await store.check_setup("AAAA-AAAA", "192.0.2.4")
        self.assertEqual(store.setup_remaining, auth.SETUP_SECONDS)
        self.now += auth.SETUP_SECONDS
        self.assertFalse(store.setup_open)
        with self.assertRaises(InvalidCode):
            await store.pair(code, "Zu spät", "192.0.2.4")
        self.assertEqual(store.devices, [])

    async def test_failed_checks_share_a_bridge_wide_budget(self):
        release = asyncio.Event()

        async def held(_seconds):
            await release.wait()

        store = self.store(sleep=held)
        pending = [asyncio.create_task(store.pair("AAAA-AAAA", "Telefon", f"198.51.100.{i}"))
                   for i in range(auth.MAX_WAITING)]
        await asyncio.sleep(0.01)
        # Only PARALLEL_CHECKS checks run; the queue is full, so the next request is refused without a check.
        with self.assertRaises(RateLimited) as busy:
            await store.pair(store.setup_code, "Telefon", "192.0.2.4")
        self.assertEqual(busy.exception.retry_after, auth.BUSY_SECONDS)
        release.set()
        results = await asyncio.gather(*pending, return_exceptions=True)
        self.assertTrue(all(isinstance(result, InvalidCode) for result in results))
        token, _device = await store.pair(store.setup_code, "Telefon", "192.0.2.4")
        self.assertTrue(token)


class SourceTests(unittest.TestCase):
    def request(self, peer, forwarded=None, proxies=()):
        transport = mock.Mock()
        transport.get_extra_info = lambda name, default=None: (peer, 1234) if name == "peername" else default
        app = {CONFIG: Config(trusted_proxies=proxies)}
        headers = {"X-Forwarded-For": forwarded} if forwarded else {}
        return make_mocked_request("POST", "/api/pair", headers=headers, transport=transport, app=app)

    def test_peer_forwarded_for_and_ipv6_prefix(self):
        self.assertEqual(_source(self.request("192.0.2.4")), "192.0.2.4")
        # Forwarded headers count only from trusted proxies; the nearest untrusted address is the client.
        self.assertEqual(_source(self.request("192.0.2.4", "203.0.113.9")), "192.0.2.4")
        proxied = self.request("10.42.0.5", "203.0.113.66, 198.51.100.7, 10.42.1.2", ("10.42.0.0/16",))
        self.assertEqual(_source(proxied), "198.51.100.7")
        self.assertEqual(_source(self.request("10.42.0.5", "kaputt", ("10.42.0.0/16",))), "10.42.0.5")
        self.assertEqual(_source(self.request("2001:db8:1:2:3:4:5:6")), "2001:db8:1:2::/64")
        self.assertEqual(_source(self.request("::ffff:192.0.2.4")), "192.0.2.4")


class PairingRouteTests(unittest.IsolatedAsyncioTestCase):
    """Proxy deployment with HTTPS forwarded, so the setup page offers its form."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.https = {"Host": "orcbro.example.com", "X-Forwarded-Proto": "https"}

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def start(self, **kwargs):
        config = Config(data_dir=Path(self.tmp.name), profiles_dir=PROFILES, filament_ids=FILAMENT_IDS, mdns=False,
                        models_dir=MODELS, tls=False, public_url="https://orcbro.example.com",
                        trusted_proxies=("127.0.0.0/8",), **kwargs)
        store = PairingStore(config.auth_dir / "auth.json", config.bridge_token, sleep=no_wait)
        self.app = create_app(config, identity=BridgeIdentity("bd92e1e8-c87d-46ee-a2c5-4d859adc8597", "Test"),
                              tls_state=TLSState(None, None), pairing_store=store, start_background=False)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def test_setup_qr_code_needs_the_code_from_the_log(self):
        await self.start()
        code = self.app[AUTH].setup_code
        page = await (await self.client.get("/", headers=self.https)).text()
        self.assertIn('action="/setup"', page)
        self.assertNotIn(code[:4], page)
        wrong = await self.client.post("/setup", data={"code": "AAAA-AAAA"}, headers=self.https)
        self.assertEqual(wrong.status, 401)
        self.assertNotIn(code[:4], await wrong.text())
        shown = await self.client.post("/setup", data={"code": code.lower()}, headers=self.https)
        body = await shown.text()
        self.assertEqual(shown.status, 200)
        self.assertIn("bambro://pair?url=https%3A%2F%2Forcbro.example.com", body)
        self.assertIn(f"{code[:4]}-{code[4:]}", body)
        # Showing the QR code does not use up the code.
        paired = await self.client.post("/api/pair", json={"code": code, "device_name": "iPhone"}, headers=self.https)
        self.assertEqual(paired.status, 201)
        closed = await self.client.post("/setup", data={"code": code}, headers=self.https)
        self.assertEqual(closed.status, 400)

    async def test_pairing_throttle_follows_the_forwarded_client(self):
        await self.start()
        attacker = {**self.https, "X-Forwarded-For": "198.51.100.7"}
        for _ in range(auth.FAILURE_LIMIT):
            await self.client.post("/api/pair", json={"code": "AAAA-AAAA", "device_name": "x"}, headers=attacker)
        blocked = await self.client.post("/api/pair", json={"code": "AAAA-AAAA", "device_name": "x"}, headers=attacker)
        self.assertEqual(blocked.status, 429)
        self.assertIn("Retry-After", blocked.headers)
        owner = {**self.https, "X-Forwarded-For": "192.0.2.4"}
        paired = await self.client.post("/api/pair", json={"code": self.app[AUTH].setup_code, "device_name": "iPhone"},
                                        headers=owner)
        self.assertEqual(paired.status, 201)

    async def test_public_bodies_are_small(self):
        await self.start(bridge_token=SECRET)
        large = json.dumps({"token": "x" * 10_000})
        response = await self.client.post("/api/pair", data=large, headers={**self.https, "Content-Type": "application/json"})
        self.assertEqual(response.status, 413)

        async def stream():  # Without Content-Length: enforced while reading.
            for _ in range(10):
                yield b" " * 1024
            yield b'{"token": "x"}'

        response = await self.client.post("/api/pair", data=stream(), headers=self.https)
        self.assertEqual(response.status, 413)
        form = await self.client.post("/invite", data={"key": "x" * 10_000}, headers=self.https)
        self.assertEqual(form.status, 413)
        token = await self.client.post("/api/pair", json={"token": SECRET}, headers=self.https)
        self.assertEqual(token.status, 200)
        bearer = {**self.https, "Authorization": f"Bearer {app_token(SECRET)}"}
        control = await self.client.post("/api/control", data=json.dumps({"action": "x" * 100_000}),
                                         headers={**bearer, "Content-Type": "application/json"})
        self.assertEqual(control.status, 413)


class UploadLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        config = Config(host="192.0.2.10", serial=SERIAL, access_code="12345678", bridge_token=SECRET,
                        data_dir=Path(self.tmp.name), profiles_dir=PROFILES, filament_ids=FILAMENT_IDS,
                        models_dir=MODELS, orca_cmd=(sys.executable, "-c", "pass"), mdns=False)
        printer = Printer(config.host, config.serial, config.access_code, "Test")
        printer.handle(load("pushall_p1s.json"))
        printer.handle(load("version_p1s.json"))
        self.app = create_app(config, printer=printer, start_background=False)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.auth = {"Authorization": f"Bearer {app_token(SECRET)}"}

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def upload(self, data: bytes):
        form = FormData()
        form.add_field("file", data, filename="cube.gcode.3mf", content_type="application/octet-stream")
        return await self.client.post("/api/jobs", data=form, headers=self.auth)

    async def test_uploads_keep_their_large_limit_but_are_capped_in_number(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as out:
            with zipfile.ZipFile(io.BytesIO(sliced_3mf())) as source:
                for item in source.infolist():
                    out.writestr(item, source.read(item))
            out.writestr("Metadata/padding.bin", bytes(200_000))  # Larger than the JSON limit.
        response = await self.upload(archive.getvalue())
        self.assertEqual(response.status, 201, await response.text())
        slots = self.app[UPLOADS]
        for _ in range(2):
            await slots.acquire()
        try:
            busy = await self.upload(sliced_3mf())
            self.assertEqual(busy.status, 429)
        finally:
            slots.release()
            slots.release()


class ArchiveBoundTests(unittest.TestCase):
    def archive(self, members: dict) -> zipfile.ZipFile:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as out:
            for name, data in members.items():
                out.writestr(name, data)
        return zipfile.ZipFile(io.BytesIO(buffer.getvalue()))

    def test_gcode_lines_skip_long_lines_and_stop_at_the_member_limit(self):
        data = b"G1 X1\n" + b";" + b"x" * 5000 + b"\nG1 X2\r\nG1 X3"
        with self.archive({"plate.gcode": data}) as archive:
            self.assertEqual(list(gcode_lines(archive, "plate.gcode", line_limit=1000)), [b"G1 X1", b"G1 X2", b"G1 X3"])
            with self.assertRaises(JobError):
                list(gcode_lines(archive, "plate.gcode", limit=100))

    def write(self, tmp, data: bytes) -> Path:
        path = Path(tmp) / "a.gcode.3mf"
        path.write_bytes(data)
        return path

    def test_plate_numbers_are_unique_and_filament_ids_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # zipfile warns about the duplicate G-code member.
                twice = self.write(tmp, sliced_3mf(plates=((1, [(1, "PLA", "#FF0000", "GFA00", 1.0)]),
                                                           (1, [(2, "PLA", "#FF0000", "GFA00", 1.0)]))))
            with self.assertRaises(JobError):
                read_slice_info(twice)
            for fid in (0, MAX_FILAMENTS + 1, 10 ** 9):
                huge = self.write(tmp, sliced_3mf(plates=((1, [(fid, "PLA", "#FF0000", "GFA00", 1.0)]),)))
                with self.subTest(fid=fid), self.assertRaises(JobError):
                    read_slice_info(huge)

    def test_project_filaments_are_bounded_scalars(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.3mf"
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as out:
                out.writestr("Metadata/project_settings.config", json.dumps({
                    "filament_type": [{"$message": "{missing}", "params": {}}, "PLA"] + ["PETG"] * 100,
                    "filament_colour": ["#FFFFFF", "x" * 1000], "filament_settings_id": [["list"]]}))
            path.write_bytes(buffer.getvalue())
            filaments = project_filaments(path)
        self.assertEqual(len(filaments), MAX_FILAMENTS)
        self.assertEqual(filaments[0], {"id": 1, "type": None, "color": "#FFFFFF", "profile": None})
        self.assertEqual(filaments[1], {"id": 2, "type": "PLA", "color": None, "profile": None})


class JobLoadTests(unittest.TestCase):
    def test_file_data_is_never_decoded_as_a_message_and_damaged_jobs_are_set_aside(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            good = store.create("cube.stl")
            good.error = m("Slicing failed: {p0}", p0=m("Printer not connected."))
            forged = {"$message": "{p0.__class__} {missing}", "params": {"p0": "x"}}
            good.settings = {"project_filaments": [{"id": 1, "type": forged, "color": "#FFFFFF", "profile": None}]}
            store.save(good)
            broken = store.create("broken.stl")
            store.save(broken)
            meta = store.dir(broken.id) / "job.json"
            data = json.loads(meta.read_text())
            data["error"] = {"$message": "Slicing failed: {p0}", "params": {}}  # Formatting would raise KeyError.
            data["pending"] = "kaputt"
            meta.write_text(json.dumps(data))
            wrong = store.create("wrong.stl")
            store.save(wrong)
            (store.dir(wrong.id) / "job.json").write_text("[1, 2]")
            with self.assertLogs("bridge.jobs", "ERROR"):
                again = JobStore(Path(tmp))
            self.assertEqual(list(again.jobs), [good.id])
            loaded = again.get(good.id)
            self.assertEqual(render(loaded.error, "de"), "Slicen fehlgeschlagen: Drucker nicht verbunden.")
            self.assertIsNone(loaded.settings["project_filaments"][0]["type"])
            self.assertTrue((store.dir(broken.id) / "job.json.damaged").exists())
            self.assertFalse((store.dir(broken.id) / "job.json").exists())

    def test_unpack_falls_back_to_text(self):
        self.assertEqual(unpack({"$message": "Plate {p0} does not exist.", "params": {}}), "Plate {p0} does not exist.")
        self.assertEqual(render(unpack(pack(m("Plate {p0} does not exist.", p0=2))), "de"), "Platte 2 gibt es nicht.")
        with self.assertRaises(ValueError):
            unpack({"$message": ["x"], "params": {}})


class PreviewBoundTests(unittest.TestCase):
    def test_long_paths_are_split_and_features_capped(self):
        lines = ["; Z_HEIGHT: 0.2", "G1 Z0.2", "; FEATURE: Wall", "G1 X0 Y0"]
        lines += [f"G1 X{(i % 200) * 0.5:.2f} Y{i // 200 * 0.5:.2f} E0.1" for i in range(1, 2500)]
        with mock.patch.object(preview, "MAX_PATH_POINTS", 1000):
            result = preview.parse_gcode(lines)
        self.assertGreaterEqual(len(result.layers[0].paths), 3)
        self.assertTrue(all(len(path) <= 1000 + 64 for _, path in result.layers[0].paths))
        names = ["; Z_HEIGHT: 0.2", "G1 Z0.2"]
        for i in range(400):
            names += [f"; FEATURE: Type {i}", "G1 X0 Y0", f"G1 X{i % 50 + 1} Y1 E0.1"]
        many = preview.parse_gcode(names)
        self.assertLessEqual(len(many.features), preview.MAX_FEATURES)
        preview.encode(many)  # One byte per path type.

    def test_empty_layers_are_not_collected(self):
        wall = ["G1 X0 Y0", "G1 X10 Y0 E0.5"]
        lines = ["; Z_HEIGHT: 0.2", *wall] + [f"; Z_HEIGHT: {i / 10:.1f}" for i in range(3, 10_000)]
        lines += ["; Z_HEIGHT: 1000.2", *wall]
        result = preview.parse_gcode(lines)
        self.assertEqual([layer.z for layer in result.layers], [0.2, 1000.2])
        # The provisional height still comes from the layer directly below, even an empty one.
        self.assertAlmostEqual(result.layers[1].height, 0.3, places=3)

    def test_points_beyond_the_budget_are_simplified_while_building(self):
        lines = ["; Z_HEIGHT: 0.2", "G1 Z0.2", "; FEATURE: Wall", "G1 X0 Y0"]
        lines += [f"G1 X{i * 0.01:.2f} Y{(i % 2) * 0.01:.2f} E0.01" for i in range(1, 3000)]
        with mock.patch.object(preview, "MAX_BUILD_POINTS", 500), mock.patch.object(preview, "MAX_PATH_POINTS", 200):
            result = preview.parse_gcode(lines)
        self.assertLessEqual(result.point_count, 500 + 200)
        self.assertGreaterEqual(result.simplified, 0)
        zigzag = ["; Z_HEIGHT: 0.2", "G1 Z0.2", "G1 X0 Y0"]
        zigzag += [f"G1 X{i} Y{(i % 2) * 10} E0.1" for i in range(1, 3000)]
        with mock.patch.object(preview, "MAX_BUILD_POINTS", 500), self.assertRaises(preview.TooComplex):
            preview.parse_gcode(zigzag)
        with self.assertRaises(preview.TooComplex):
            preview.simplify([(i, (i % 2) * 10.0) for i in range(500)], 0.01, preview.Budget(100))


class EnvironmentPrinterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "printers.json"

    def tearDown(self):
        self.tmp.cleanup()

    def settings(self, serial="", code="12345678"):
        return PrinterSettings(serial=serial, host="192.0.2.10", access_code=code, source="env")

    def test_enrolled_serial_survives_restarts_until_the_operator_changes_it(self):
        store = PrinterStore(self.path)
        self.assertIsNone(store.resolve_env(self.settings()))
        store.enroll_env(SERIAL, "12345678", fingerprint="aa" * 32)
        again = PrinterStore(self.path)
        self.assertEqual(again.resolve_env(self.settings()), SERIAL)
        self.assertEqual(again.pins[SERIAL], "aa" * 32)
        self.assertNotIn("12345678", self.path.read_text())
        # A new access code is explicit re-enrollment: read serial and certificate again.
        self.assertIsNone(again.resolve_env(self.settings(code="87654321")))
        # BAMBU_SERIAL always wins and is recorded; a new code with it drops the old pin.
        self.assertEqual(again.resolve_env(self.settings(serial=OTHER)), OTHER)
        self.assertEqual(PrinterStore(self.path).env["serial"], OTHER)
        again.pins[OTHER] = "bb" * 32
        self.assertEqual(again.resolve_env(self.settings(serial=OTHER, code="87654321")), OTHER)
        self.assertNotIn(OTHER, again.pins)

    def test_upgrade_with_serial_keeps_the_existing_pin(self):
        store = PrinterStore(self.path)
        store.pins[SERIAL] = "aa" * 32
        store.save()
        again = PrinterStore(self.path)
        self.assertEqual(again.resolve_env(self.settings(serial=SERIAL)), SERIAL)
        self.assertEqual(again.pins[SERIAL], "aa" * 32)

    async def test_bridge_uses_the_enrolled_serial_instead_of_asking_the_address(self):
        config = Config(host="192.0.2.10", access_code="12345678", bridge_token=SECRET, data_dir=Path(self.tmp.name),
                        profiles_dir=PROFILES, filament_ids=FILAMENT_IDS, models_dir=MODELS, mdns=False)
        identity = mock.AsyncMock(return_value=(SERIAL, "aa" * 32))
        with mock.patch("bridge.server.read_identity", identity):
            app = create_app(config, start_background=False)
            self.assertEqual(app[DEVICES].devices, {})
            from bridge.server import _add_env_printer, env_settings
            await _add_env_printer(app, env_settings(config))
        self.assertEqual(list(app[DEVICES].devices), [SERIAL])
        self.assertEqual(app[DEVICES].store.pins[SERIAL], "aa" * 32)
        # After a restart, a different device at the address cannot become the printer: the serial is fixed
        # and its pin is checked; the address is not asked for a serial again.
        identity = mock.AsyncMock(return_value=(OTHER, "bb" * 32))
        with mock.patch("bridge.server.read_identity", identity):
            again = create_app(config, start_background=False)
        identity.assert_not_awaited()
        device = again[DEVICES].devices[SERIAL]
        self.assertEqual(device.trust.pin, "aa" * 32)
        with self.assertRaises(CertificateChanged):
            device.trust.verify(b"other certificate")


class CameraPortTests(unittest.IsolatedAsyncioTestCase):
    async def test_ffmpeg_starts_only_after_the_camera_port_presents_the_pin(self):
        trust = mock.Mock()
        trust.check = mock.AsyncMock(side_effect=CertificateChanged("changed"))
        camera = RtspCamera("192.0.2.10", "12345678", trust=trust)
        camera._last_viewer = time.monotonic()

        async def stop(_seconds):
            camera._last_viewer = 0.0

        with mock.patch("bridge.camera_rtsp.asyncio.create_subprocess_exec") as start, \
                mock.patch("bridge.camera_rtsp.asyncio.sleep", stop), self.assertLogs("bridge.camera_rtsp", "WARNING"):
            await camera._run()
        trust.check.assert_awaited_once_with("192.0.2.10", RTSPS_PORT)
        start.assert_not_called()
        self.assertEqual(camera.error, CHANGED)


class ProbeAddressTests(unittest.IsolatedAsyncioTestCase):
    def test_local_addresses(self):
        for address in ("192.168.1.5", "10.0.0.2", "172.16.4.1", "169.254.3.3", "fd12::1", "fe80::1%eth0"):
            self.assertTrue(local_address(address), address)
        for address in ("8.8.8.8", "127.0.0.1", "::1", "0.0.0.0", "224.0.0.251", "2a00:1450::1", "100.64.0.1"):
            self.assertFalse(local_address(address), address)

    async def test_probe_refuses_other_networks_before_connecting(self):
        with mock.patch("bridge.probe.read_identity") as identity:
            for host in ("8.8.8.8", "127.0.0.1", "::1"):
                with self.assertRaises(ProbeError) as refused:
                    await probe(host, "12345678")
                self.assertEqual(refused.exception.code, "not_local")
        identity.assert_not_called()

    async def test_probe_connects_to_the_checked_address(self):
        async def resolved(*_args, **_kwargs):
            return [(2, 1, 6, "", ("192.168.1.50", 8883))]

        loop = asyncio.get_running_loop()
        with mock.patch.object(loop, "getaddrinfo", resolved), \
                mock.patch("bridge.probe.read_identity", mock.AsyncMock(side_effect=ProbeError("unreachable", "x"))) as identity:
            with self.assertRaises(ProbeError):
                await probe("printer.lan", "12345678")
        identity.assert_awaited_once_with("192.168.1.50")


class SpoolValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "spools.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_invalid_fields_change_nothing(self):
        store = SpoolStore(self.path)
        spool, _ = store.load(SERIAL, 0, "04A1B2C3D4E5F6", weight=1000, info={"type": "PLA"})
        for info in ({"type": {"x": 1}}, {"color": ["#FFF"]}, {"brand": "x" * 500}, {"nozzle_temp_min": "220"},
                     {"nozzle_temp_max": True}):
            with self.subTest(info=info), self.assertRaises(ValueError):
                store.load(SERIAL, 1, "04A1B2C3D4E5F7", weight=1000, info=info)
        self.assertEqual(list(store.spools), ["04A1B2C3D4E5F6"])
        for remaining in (True, "500", float("nan"), -1):
            with self.subTest(remaining=remaining), self.assertRaises(ValueError):
                store.update(spool["id"], remaining=remaining, note="neu")
        self.assertIsNone(store.get(spool["id"])["note"])

    def test_damaged_records_are_set_aside_on_load(self):
        good = {"id": "04A1B2C3D4E5F6", "kind": "tag", "type": "PLA", "weight": 1000.0, "remaining": 800.0,
                "printer": SERIAL, "slot": 0}
        bad = {"id": "04A1B2C3D4E5F7", "kind": "tag", "type": {"x": 1}, "remaining": 5.0, "printer": SERIAL, "slot": 1}
        self.path.write_text(json.dumps({"spools": {good["id"]: good, bad["id"]: bad}, "printing": {}}))
        with self.assertLogs("bridge.spools", "WARNING"):
            store = SpoolStore(self.path)
        self.assertEqual([s["id"] for s in store.library()], [good["id"]])
        self.assertEqual(set(store.loaded(SERIAL)), {0})
        SpoolStore.public(store.library()[0])
        store.save()
        self.assertIn(bad["id"], json.loads(self.path.read_text())["damaged"])
        self.path.write_text("[]")
        self.assertEqual(SpoolStore(self.path).spools, {})


class SpoolRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        config = Config(host="192.0.2.10", serial=SERIAL, access_code="12345678", bridge_token=SECRET,
                        data_dir=Path(self.tmp.name), profiles_dir=PROFILES, filament_ids=FILAMENT_IDS,
                        models_dir=MODELS, mdns=False)
        printer = Printer(config.host, config.serial, config.access_code, "Test")
        printer.handle(load("pushall_p1s.json"))
        self.app = create_app(config, printer=printer, start_background=False)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.auth = {"Authorization": f"Bearer {app_token(SECRET)}"}

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_spool_routes_reject_bad_types(self):
        loaded = await self.client.post("/api/ams/spool", headers=self.auth, json={
            "slot": 0, "uid": "04A1B2C3D4E5F6", "weight": 1000, "type": {"nested": True}})
        self.assertEqual(loaded.status, 422)
        self.assertEqual(self.app[SPOOLS].spools, {})
        loaded = await self.client.post("/api/ams/spool", headers=self.auth, json={
            "slot": 0, "uid": "04A1B2C3D4E5F6", "weight": 1000, "type": "PLA"})
        self.assertEqual(loaded.status, 200)
        for body, text in (({"remaining": "viel"}, "Invalid spool data"), ({"note": 5}, "Invalid spool data"),
                           ({"unload": "ja"}, "Invalid spool data")):
            response = await self.client.patch("/api/spools/04A1B2C3D4E5F6", headers=self.auth, json=body)
            self.assertEqual(response.status, 422)
            self.assertIn(text, (await response.json())["error"])
        listed = await self.client.get("/api/spools", headers=self.auth)
        self.assertEqual(listed.status, 200)


class APNsLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_device_tokens_do_not_reach_the_log(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        key = ec.generate_private_key(ec.SECP256R1())
        path = Path(tmp.name) / "AuthKey.p8"
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
        replies = iter([httpx.Response(200, json={}), httpx.Response(410, json={"reason": "Unregistered"})])
        client = APNsClient(APNsConfig.load(path, "KEYID12345", "TEAMID1234", "it.example.app"),
                            transport=httpx.MockTransport(lambda _request: next(replies)))
        self.addAsyncCleanup(client.close)
        token = "ab" * 32
        with self.assertLogs("httpx", "INFO") as logs:
            await client.send_live_activity(token, "production", {"aps": {}})
            with self.assertRaises(APNsError):
                await client.send_notification(token, "development", {"template": "finished", "job": "J",
                                                                      "printer": "P", "language": "en"})
        self.assertEqual(len(logs.records), 2)
        text = "\n".join(logs.output)
        self.assertNotIn(token, text)
        self.assertIn("/3/device/<token>", text)


if __name__ == "__main__":
    unittest.main()
