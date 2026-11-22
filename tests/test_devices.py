import datetime
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from bridge.config import Config, ConfigError
from bridge.devices import PrinterSettings, PrinterStore
from bridge.models import Models
from bridge.printer import Printer
from bridge.probe import ProbeError, serial_from_certificate
from bridge.profiles import machine_profiles, nozzle_variant
from bridge.server import app_token, create_app
from helpers import FILAMENT_IDS, MODELS, PROFILES, load, sliced_3mf

SECRET = "s3cret-bridge-access-token"
FIRST = "01P00A000000000"
SECOND = "01P00B000000000"


def certificate(common_name: str, issuer: str) -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
            .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer)]))
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.DER)


class FakeProbe:
    """Fake printer login: known printers by IP, accepting one access code."""

    def __init__(self):
        self.printers = {"192.0.2.20": SECOND, "192.0.2.10": FIRST}
        self.fingerprints: dict[str, str] = {}
        self.calls = []

    async def __call__(self, host, access_code, serial=None, pin=None):
        if host not in self.printers:
            raise ProbeError("unreachable", f"Unter {host} antwortet kein Drucker.")
        # Like the real probe, verify serial number and fingerprint before sending the access code.
        if serial and self.printers[host] != serial:
            raise ProbeError("other_printer", 'A different printer is responding at this address.')
        fingerprint = self.fingerprints.get(host, "fp-" + self.printers[host])
        if pin and fingerprint != pin:
            raise ProbeError("certificate_changed", 'A device with a different certificate is responding at this address.')
        self.calls.append((host, access_code, serial))
        if access_code != "87654321":
            raise ProbeError("access_code", 'Incorrect access code.')
        return {"serial": self.printers[host], "host": host, "model": "Bambu Lab P1S", "firmware": "01.10.00.00",
                "nozzle_diameter": 0.4, "developer_mode": True, "fingerprint": fingerprint}


class CertificateTests(unittest.TestCase):
    def test_serial_comes_from_bambu_certificate(self):
        self.assertEqual(serial_from_certificate(certificate("01p00a000000000", "BBL CA")), FIRST)
        self.assertEqual(serial_from_certificate(certificate(FIRST, "BBL Device CA")), FIRST)

    def test_other_devices_are_refused(self):
        for cn, issuer in ((FIRST, "Let's Encrypt R3"), ("router.local", "BBL CA")):
            with self.assertRaises(ProbeError) as ctx:
                serial_from_certificate(certificate(cn, issuer))
            self.assertEqual(ctx.exception.code, "not_bambu")
        with self.assertRaises(ProbeError):
            serial_from_certificate(b"kein Zertifikat")


class ConfigTests(unittest.TestCase):
    def test_printer_from_environment_is_optional(self):
        config = Config.from_env({"BRIDGE_ACCESS_TOKEN": SECRET})
        self.assertIsNone(config.host)
        config = Config.from_env({"BRIDGE_ACCESS_TOKEN": SECRET, "BAMBU_HOST": "192.0.2.10",
                                  "BAMBU_ACCESS_CODE": "12345678", "BAMBU_SERIAL": "01p00a000000000"})
        self.assertEqual((config.host, config.serial), ("192.0.2.10", FIRST))
        with self.assertRaises(ConfigError):
            Config.from_env({"BRIDGE_ACCESS_TOKEN": SECRET, "BAMBU_HOST": "192.0.2.10"})


class ProfileSelectionTests(unittest.TestCase):
    def test_machine_follows_model_and_nozzle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "BBL"
            shutil.copytree(PROFILES, root)
            base = json.loads((root / "machine" / "Bambu Lab P1S 0.4 nozzle.json").read_text())
            (root / "machine" / "Bambu Lab P1S 0.6 nozzle.json").write_text(json.dumps({
                "name": "Bambu Lab P1S 0.6 nozzle", "inherits": base["name"], "instantiation": "true",
                "printer_variant": "0.6"}))
            machines = machine_profiles(root)
        self.assertEqual(machines[("Bambu Lab P1S", "0.4")], "Bambu Lab P1S 0.4 nozzle")
        self.assertEqual(machines[("Bambu Lab P1S", "0.6")], "Bambu Lab P1S 0.6 nozzle")
        self.assertEqual([nozzle_variant(v) for v in (0.4, 0.6000001, None)], ["0.4", "0.6", "0.4"])

    def test_models_layer_firmware(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "C12.json").write_text(json.dumps({
                "00.00.00.00": {"display_name": "Bambu Lab P1S", "model_id": "C12",
                                "print": {"support_tunnel_mqtt": False, "nozzle_temp_range": [0, 300]}},
                "01.03.50.01": {"print": {"support_tunnel_mqtt": True}}}))
            Path(tmp, "bambu_filament_ids.json").write_text("{}")
            models = Models(Path(tmp))
        model = models.find("P1S")
        self.assertEqual((model.model_id, model.name), ("C12", "Bambu Lab P1S"))
        self.assertFalse(model.info("01.02.00.00")["print"]["support_tunnel_mqtt"])
        self.assertTrue(model.info("01.10.00.00")["print"]["support_tunnel_mqtt"])
        self.assertEqual(model.info()["print"]["nozzle_temp_range"], [0, 300])


class StoreTests(unittest.TestCase):
    def test_saved_privately_without_environment_printers(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PrinterStore(Path(tmp) / "printers.json")
            store.printers[SECOND] = PrinterSettings(serial=SECOND, host="192.0.2.20", access_code="87654321")
            store.printers[FIRST] = PrinterSettings(serial=FIRST, host="192.0.2.10", access_code="x", source="env")
            store.default = SECOND
            store.save()
            path = Path(tmp) / "printers.json"
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            again = PrinterStore(path)
        self.assertEqual(list(again.printers), [SECOND])
        self.assertEqual((again.default, again.printers[SECOND].access_code), (SECOND, "87654321"))


class PrinterApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(
            host="192.0.2.10", serial=FIRST, access_code="12345678", bridge_token=SECRET,
            data_dir=Path(self.tmp.name), profiles_dir=PROFILES, filament_ids=FILAMENT_IDS, models_dir=MODELS,
            orca_cmd=(sys.executable, str(Path(__file__).parent / "fake_orca.py")))
        self.printer = Printer(self.config.host, self.config.serial, self.config.access_code, "Werkstatt")
        self.printer.handle(load("pushall_p1s.json"))
        self.printer.handle(load("version_p1s.json"))
        self.printer.connected = True
        self.probe = FakeProbe()
        self.client = await self.start()
        self.auth = {"Authorization": f"Bearer {app_token(SECRET)}"}

    async def start(self):
        app = create_app(self.config, printer=self.printer, probe_fn=self.probe, start_background=False)
        client = TestClient(TestServer(app))
        await client.start_server()
        return client

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def call(self, method, path, body=None):
        response = await self.client.request(method, path, json=body, headers=self.auth)
        return response.status, await response.json()

    async def add_second(self):
        status, data = await self.call("POST", "/api/printers",
                                       {"host": "192.0.2.20", "access_code": "87654321", "name": "Keller"})
        self.assertEqual(status, 201, data)
        return data

    async def test_list_shows_environment_printer_as_default(self):
        status, data = await self.call("GET", "/api/printers")
        self.assertEqual(status, 200)
        self.assertEqual(data["default"], FIRST)
        [printer] = data["printers"]
        self.assertEqual((printer["id"], printer["name"], printer["managed"], printer["default"]),
                         (FIRST, "Werkstatt", True, True))
        self.assertEqual((printer["machine"], printer["model_id"]), ("Bambu Lab P1S 0.4 nozzle", "C12"))
        self.assertNotIn("access_code", printer)

    async def test_probe_reports_reasons(self):
        status, data = await self.call("POST", "/api/printers/probe", {"host": "192.0.2.20", "access_code": "87654321"})
        self.assertEqual(status, 200)
        self.assertEqual((data["serial"], data["supported"], data["configured"]), (SECOND, True, False))
        self.assertEqual(data["machine"], "Bambu Lab P1S 0.4 nozzle")
        status, data = await self.call("POST", "/api/printers/probe", {"host": "192.0.2.20", "access_code": "11111111"})
        self.assertEqual((status, data["code"]), (422, "access_code"))
        status, data = await self.call("POST", "/api/printers/probe", {"host": "192.0.2.99", "access_code": "87654321"})
        self.assertEqual((status, data["code"]), (502, "unreachable"))
        status, _ = await self.call("POST", "/api/printers/probe", {"host": "192.0.2.20"})
        self.assertEqual(status, 422)
        status, _ = await self.call("POST", "/api/printers/probe", {"host": "x; rm -rf /", "access_code": "87654321"})
        self.assertEqual(status, 422)

    async def test_add_saves_and_survives_restart(self):
        added = await self.add_second()
        self.assertEqual((added["id"], added["name"], added["managed"], added["default"]), (SECOND, "Keller", False, False))
        self.assertEqual((added["model"], added["machine"]), ("Bambu Lab P1S", "Bambu Lab P1S 0.4 nozzle"))
        status, _ = await self.call("POST", "/api/printers", {"host": "192.0.2.20", "access_code": "87654321"})
        self.assertEqual(status, 409)
        saved = json.loads(Path(self.tmp.name, "printers.json").read_text())
        self.assertEqual([p["serial"] for p in saved["printers"]], [SECOND])
        await self.client.close()
        self.client = await self.start()
        _, data = await self.call("GET", "/api/printers")
        self.assertEqual([p["id"] for p in data["printers"]], [FIRST, SECOND])

    async def test_each_printer_has_its_own_routes(self):
        await self.add_second()
        status, data = await self.call("GET", f"/api/printers/{SECOND}/status")
        self.assertEqual(status, 200)
        self.assertEqual((data["printer"]["id"], data["connection"]["connected"]), (SECOND, False))
        _, data = await self.call("GET", f"/api/printers/{FIRST}/status")
        self.assertEqual((data["printer"]["id"], data["printer"]["name"]), (FIRST, "Werkstatt"))
        _, data = await self.call("GET", "/api/status")  # Older app uses the default printer.
        self.assertEqual(data["printer"]["id"], FIRST)
        status, _ = await self.call("GET", "/api/printers/UNBEKANNT/status")
        self.assertEqual(status, 404)
        status, data = await self.call("POST", f"/api/printers/{SECOND}/control", {"action": "light", "value": True})
        self.assertEqual((status, data["error"]), (409, 'Printer not connected.'))

    async def test_jobs_belong_to_a_printer(self):
        await self.add_second()
        form = FormData()
        form.add_field("file", sliced_3mf(), filename="fertig.gcode.3mf", content_type="application/octet-stream")
        response = await self.client.post(f"/api/printers/{SECOND}/jobs", data=form, headers=self.auth)
        job = await response.json()
        self.assertEqual((response.status, job["printer"]), (201, SECOND))
        _, data = await self.call("GET", f"/api/printers/{FIRST}/jobs")
        self.assertEqual(data["jobs"], [])
        _, data = await self.call("GET", f"/api/printers/{SECOND}/jobs")
        self.assertEqual([j["id"] for j in data["jobs"]], [job["id"]])
        status, data = await self.call("POST", f"/api/jobs/{job['id']}/print", {"mapping": {"1": 0}})
        self.assertEqual((status, data["error"]), (409, 'Printer not connected.'))

    async def test_update_checks_new_access_code_and_default(self):
        await self.add_second()
        status, data = await self.call("PATCH", f"/api/printers/{SECOND}", {"access_code": "11111111"})
        self.assertEqual((status, data["code"]), (422, "access_code"))
        self.probe.printers["192.0.2.30"] = "01P00C000000000"
        status, data = await self.call("PATCH", f"/api/printers/{SECOND}", {"host": "192.0.2.30"})
        self.assertEqual(status, 409)
        self.assertIn('different printer', data["error"])
        self.probe.printers["192.0.2.21"] = SECOND
        status, data = await self.call("PATCH", f"/api/printers/{SECOND}",
                                       {"host": "192.0.2.21", "name": "Keller links", "default": True})
        self.assertEqual(status, 200, data)
        self.assertEqual((data["host"], data["name"], data["default"]), ("192.0.2.21", "Keller links", True))
        device = self.client.server.app[__import__("bridge.server", fromlist=["DEVICES"]).DEVICES].get(SECOND)
        self.assertEqual(device.printer.host, "192.0.2.21")  # Reconnect with the new IP.
        _, data = await self.call("GET", "/api/status")
        self.assertEqual(data["printer"]["id"], SECOND)

    async def test_new_address_with_stored_code_needs_same_certificate(self):
        """A paired device must not redirect the saved access code to its own computer."""
        await self.add_second()
        store = self.client.server.app[__import__("bridge.server", fromlist=["DEVICES"]).DEVICES].store
        self.assertEqual(store.pins[SECOND], "fp-" + SECOND)
        # Another device claims the serial number but presents a different certificate.
        self.probe.printers["192.0.2.66"] = SECOND
        self.probe.fingerprints["192.0.2.66"] = "fp-angreifer"
        before = len(self.probe.calls)
        status, data = await self.call("PATCH", f"/api/printers/{SECOND}", {"host": "192.0.2.66"})
        self.assertEqual((status, data["code"]), (409, "certificate_changed"))
        self.assertEqual(len(self.probe.calls), before)  # No login means no access code sent.
        # Re-entering the code permits the new address and certificate.
        status, data = await self.call("PATCH", f"/api/printers/{SECOND}", {"host": "192.0.2.66", "access_code": "87654321"})
        self.assertEqual(status, 200, data)
        self.assertEqual(store.pins[SECOND], "fp-angreifer")

    async def test_new_address_without_known_certificate_needs_code(self):
        await self.add_second()
        store = self.client.server.app[__import__("bridge.server", fromlist=["DEVICES"]).DEVICES].store
        store.pins.pop(SECOND)
        self.probe.printers["192.0.2.21"] = SECOND
        status, data = await self.call("PATCH", f"/api/printers/{SECOND}", {"host": "192.0.2.21"})
        self.assertEqual(status, 409)
        self.assertIn('Enter the access code again', data["error"])

    async def test_environment_printer_is_read_only(self):
        status, data = await self.call("PATCH", f"/api/printers/{FIRST}", {"access_code": "87654321"})
        self.assertEqual(status, 409)
        self.assertIn("BAMBU_ACCESS_CODE", data["error"])
        status, _ = await self.call("PATCH", f"/api/printers/{FIRST}", {"name": "Werkbank"})
        self.assertEqual(status, 409)
        await self.add_second()
        status, data = await self.call("PATCH", f"/api/printers/{SECOND}", {"default": True})
        self.assertEqual((status, data["default"]), (200, True))
        status, data = await self.call("PATCH", f"/api/printers/{FIRST}", {"default": True})
        self.assertEqual((status, data["default"]), (200, True))  # Default selection is also allowed here.
        status, _ = await self.call("DELETE", f"/api/printers/{FIRST}")
        self.assertEqual(status, 409)

    async def test_remove(self):
        await self.add_second()
        status, _ = await self.call("DELETE", f"/api/printers/{SECOND}")
        self.assertEqual(status, 200)
        _, data = await self.call("GET", "/api/printers")
        self.assertEqual([p["id"] for p in data["printers"]], [FIRST])
        self.assertEqual(json.loads(Path(self.tmp.name, "printers.json").read_text())["printers"], [])

    async def test_needs_login(self):
        response = await self.client.get("/api/printers")
        self.assertEqual(response.status, 401)


class WithoutPrinterTests(unittest.IsolatedAsyncioTestCase):
    async def test_bridge_starts_without_printer(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(bridge_token=SECRET, data_dir=Path(tmp), profiles_dir=PROFILES, filament_ids=FILAMENT_IDS,
                            models_dir=MODELS)
            probe = FakeProbe()
            client = TestClient(TestServer(create_app(config, probe_fn=probe, start_background=False)))
            await client.start_server()
            auth = {"Authorization": f"Bearer {app_token(SECRET)}"}
            try:
                response = await client.get("/api/status", headers=auth)
                self.assertEqual(response.status, 409)
                self.assertIn('No printer', (await response.json())["error"])
                with mock.patch("bridge.devices.Printer.run", mock.AsyncMock()):
                    response = await client.post("/api/printers", json={"host": "192.0.2.20",
                                                                         "access_code": "87654321"}, headers=auth)
                self.assertEqual(response.status, 201)
                data = await (await client.get("/api/printers", headers=auth)).json()
                self.assertEqual((data["default"], data["printers"][0]["default"]), (SECOND, True))
                response = await client.get("/api/status", headers=auth)
                self.assertEqual(response.status, 200)
            finally:
                await client.close()


if __name__ == "__main__":
    unittest.main()
