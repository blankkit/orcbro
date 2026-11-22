"""Review regressions: proxy fingerprints, unknown models, cameras without transport,
filament_map nozzle assignment, token special characters and X1 fun bits."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from bridge.auth import PairingStore
from bridge.config import Config
from bridge.models import Models
from bridge.print_metadata import _nozzle_list
from bridge.printer import Printer
from bridge.server import CONFIG, TLS, app_token, create_app, own_certificate_fingerprint
from helpers import FILAMENT_IDS, MODELS, PROFILES, load, sliced_3mf

SECRET = "s3cret-bridge-access-token"


class FingerprintTests(unittest.TestCase):
    def request(self, *, tls: bool, public_url=None):
        app = {CONFIG: SimpleNamespace(public_url=public_url), TLS: SimpleNamespace(fingerprint="ab" * 32)}
        transport = SimpleNamespace(get_extra_info=lambda key: object() if tls and key == "sslcontext" else None)
        return SimpleNamespace(app=app, transport=transport)

    def test_only_over_own_tls_listener(self):
        self.assertEqual(own_certificate_fingerprint(self.request(tls=True)), "ab" * 32)
        self.assertIsNone(own_certificate_fingerprint(self.request(tls=False)))  # Reverse proxy → HTTP 8080.
        self.assertIsNone(own_certificate_fingerprint(self.request(tls=True, public_url="https://bridge.example")))


class NozzleMapTests(unittest.TestCase):
    def test_filament_map_is_one_based(self):
        self.assertEqual(_nozzle_list({"filament_map": ["1", "2", "3"]}, 4), [1, 0, None, None])
        self.assertEqual(_nozzle_list({"filament_extruder_id": [0, 1]}, 2), [0, 1])


class TokenTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_ascii_token_is_just_wrong(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PairingStore(Path(tmp) / "auth.json", SECRET)
            self.assertIsNone(store.authenticate("äöü-token"))
            self.assertIsNone(store.authenticate("\ud800"))
            self.assertEqual(store.authenticate(app_token(SECRET)).authentication, "legacy")


class FunBitTests(unittest.TestCase):
    def test_x1_keeps_gcode_homing(self):
        models = Models(MODELS)
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "BL-P001.json").write_text(json.dumps(
                {"00.00.00.00": {"display_name": "Bambu Lab X1 Carbon", "model_id": "BL-P001", "print": {}}}))
            x1 = Models(Path(tmp)).find("Bambu Lab X1 Carbon")
            capabilities = x1.capabilities("01.09.00.00", {"fun": f"{1 << 32 | 1 << 39:X}"})
        self.assertFalse(capabilities["mqtt_homing"])
        self.assertFalse(capabilities["mqtt_bed_temperature"])
        self.assertIsNotNone(models.find("Bambu Lab P1S"))


class ServerFixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        config = Config(host="192.0.2.10", serial="01P00A000000000", access_code="12345678", bridge_token=SECRET,
                        data_dir=Path(self.tmp.name), profiles_dir=PROFILES, filament_ids=FILAMENT_IDS,
                        models_dir=MODELS, orca_cmd=(sys.executable, "-c", "pass"))
        self.printer = Printer(config.host, config.serial, config.access_code, "Werkstatt")
        self.printer.handle(load("pushall_p1s.json"))
        self.printer.connected = True
        self.printer._publish = mock.AsyncMock()
        self.client = TestClient(TestServer(create_app(config, printer=self.printer, start_background=False)))
        await self.client.start_server()
        self.auth = {"Authorization": f"Bearer {app_token(SECRET)}"}

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_unknown_model_refuses_print(self):
        # Without a version report, the model is unknown; do not guess a print command.
        form = FormData()
        form.add_field("file", sliced_3mf(), filename="teil.gcode.3mf", content_type="application/octet-stream")
        job = await (await self.client.post("/api/jobs", data=form, headers=self.auth)).json()
        response = await self.client.post(f"/api/jobs/{job['id']}/print", json={"mapping": {"1": 0}}, headers=self.auth)
        self.assertEqual(response.status, 409)
        self.assertIn('does not recognize this printer model', (await response.json())["error"])

    async def test_new_model_without_orca_data_refuses_print(self):
        # P2S identified but no model data in the image: never send a legacy command to the new generation.
        self.printer.handle({"info": {"command": "get_version", "module": [
            {"name": "ota", "product_name": "Bambu Lab P2S", "sw_ver": "01.02.00.00"}]}})
        form = FormData()
        form.add_field("file", sliced_3mf(), filename="teil.gcode.3mf", content_type="application/octet-stream")
        job = await (await self.client.post("/api/jobs", data=form, headers=self.auth)).json()
        response = await self.client.post(f"/api/jobs/{job['id']}/print", json={"mapping": {"1": 0}}, headers=self.auth)
        self.assertEqual(response.status, 409)
        self.assertIn('missing data for this printer model', (await response.json())["error"])

    async def test_legacy_pair_with_broken_unicode(self):
        response = await self.client.post("/api/pair", data='{"token": "\\ud800"}',
                                          headers={"Content-Type": "application/json"})
        self.assertEqual(response.status, 401)

    async def test_stream_without_camera_transport(self):
        response = await self.client.get("/api/camera/stream", headers=self.auth)
        self.assertEqual(response.status, 504)


if __name__ == "__main__":
    unittest.main()
