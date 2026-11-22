"""AMS drying (AMS 2 Pro, AMS HT): status, filament profile presets and commands.

Messages are synthetic, based on OrcaSlicer DevFilaSystem.cpp, DevFilaSystemCtrl.cpp and
AMSDryControl.cpp; no heated AMS was available for hardware testing."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from bridge import controls
from bridge.config import Config
from bridge.printer import Printer
from bridge.profiles import Catalog
from bridge.server import app_token, create_app
from bridge.state import normalize
from helpers import FILAMENT_IDS, MODELS, PROFILES, load

SECRET = "s3cret-bridge-access-token"

# Type 3 (AMS 2 Pro), state 2 (drying, bits 4–7), phase 1 (heating, bits 22–23).
DRYING_INFO = f"{(1 << 22) | (2 << 4) | 3:08X}"


def ams_2_pro(info=DRYING_INFO, **extra):
    return {"id": "0", "info": info, "humidity": "2", "humidity_raw": "30", "temp": "52",
            "tray": [{"id": "0", "tray_type": "PLA", "tray_info_idx": "GFA00", "tray_color": "FF0000FF"},
                     {"id": "1", "tray_type": "PETG", "tray_info_idx": "GFG99", "tray_color": "00FF00FF"},
                     {"id": "2"}, {"id": "3"}], **extra}


def status_with(unit: dict, *, fun2="20", state="IDLE") -> dict:
    raw = {"gcode_state": state, "fun2": fun2, "ams": {"ams": [unit], "tray_now": "255"}}
    return normalize(raw, connected=True, last_message_at=1, printer={"model": "Bambu Lab H2D"})


class StatusTests(unittest.TestCase):
    def test_drying_state_is_read(self):
        unit = ams_2_pro(dry_time=125, dry_setting={"dry_filament": "PLA", "dry_temperature": 55, "dry_duration": 8},
                         dry_sf_reason=[0, 3])
        drying = status_with(unit)["ams"]["units"][0]["drying"]
        self.assertEqual(drying, {
            "supported": True, "status": "drying", "phase": "heating", "remaining_minutes": 125,
            "settings": {"filament": "PLA", "temperature": 55, "hours": 8},
            "blocked": ["task_occupied", "filament_at_outlet"],
            "temperature_range": [45, 65], "hours_range": [1, 24]})

    def test_ams_ht_range_and_idle(self):
        unit = {"id": "128", "info": "00000004", "tray": [{"id": "0", "tray_type": "PA"}]}
        drying = status_with(unit)["ams"]["units"][0]["drying"]
        self.assertEqual((drying["status"], drying["temperature_range"], drying["settings"]), ("off", [45, 85], None))

    def test_without_remote_drying_firmware_nothing_is_claimed(self):
        drying = status_with(ams_2_pro(), fun2="0")["ams"]["units"][0]["drying"]
        self.assertEqual((drying["supported"], drying["status"], drying["phase"]), (False, None, None))

    def test_ams_without_heater_has_no_drying(self):
        unit = load("pushall_p1s.json")["print"]["ams"]["ams"][0]
        self.assertIsNone(status_with(unit)["ams"]["units"][0]["drying"])


class PresetTests(unittest.TestCase):
    def test_drying_preset_from_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "BBL"
            shutil.copytree(PROFILES, root)
            path = root / "filament" / "fdm_filament_pla.json"
            data = json.loads(path.read_text())
            data.update({"filament_dev_ams_drying_temperature": ["55", "55", "45", "45"],
                         "filament_dev_ams_drying_time": ["8", "8", "12", "12"],
                         "filament_dev_drying_softening_temperature": ["45"],
                         "filament_dev_ams_drying_heat_distortion_temperature": ["53"]})
            path.write_text(json.dumps(data))
            catalog = Catalog(root, "Bambu Lab P1S 0.4 nozzle", FILAMENT_IDS)
            preset = catalog.drying_preset(catalog.filament_for_tray("GFA00", "PLA"))
        self.assertEqual(preset["type"], "PLA")
        self.assertEqual(preset["ams_2_pro"], {"idle": {"temperature": 55.0, "hours": 8.0},
                                               "printing": {"temperature": 45.0, "hours": 12.0}})
        self.assertEqual((preset["softening_temperature"], preset["heat_distortion_temperature"]), (45.0, 53.0))

    def test_profile_without_drying_values(self):
        catalog = Catalog(PROFILES, "Bambu Lab P1S 0.4 nozzle", FILAMENT_IDS)
        self.assertIsNone(catalog.drying_preset(catalog.filament_for_tray("GFA00", "PLA")))


class CommandTests(unittest.TestCase):
    SUGGESTION = {"temperature_range": [45, 65], "max_temperature": 53, "hours_range": [1, 24],
                  "cooling_temperature": 45, "filament_type": "PLA"}

    def idle(self, **extra):
        return status_with(ams_2_pro(info="00000003", **extra))

    def test_start_command(self):
        section, command = controls.dry({"ams_id": 0, "temperature": 50, "hours": 8, "rotate_tray": True},
                                        self.idle(), self.SUGGESTION)
        self.assertEqual(section, "print")
        self.assertEqual(command, {"command": "ams_filament_drying", "ams_id": 0, "mode": 1, "filament": "PLA",
                                   "temp": 50, "duration": 8, "humidity": 0, "rotate_tray": True,
                                   "cooling_temp": 45, "close_power_conflict": False})

    def test_limits_and_refusals(self):
        cases = [
            ({"ams_id": 0, "temperature": 60, "hours": 8}, self.idle(), 422),  # Above the heat distortion temperature.
            ({"ams_id": 0, "temperature": 40, "hours": 8}, self.idle(), 422),
            ({"ams_id": 0, "temperature": 50, "hours": 25}, self.idle(), 422),
            ({"ams_id": 0, "temperature": 50, "hours": 8, "rotate_tray": "ja"}, self.idle(), 422),
            ({"ams_id": 3, "temperature": 50, "hours": 8}, self.idle(), 422),
            ({"ams_id": 0, "temperature": 50, "hours": 8}, status_with(ams_2_pro()), 409),  # Already drying.
            ({"ams_id": 0, "temperature": 50, "hours": 8}, self.idle(dry_sf_reason=[1]), 409),
            ({"ams_id": 0, "temperature": 50, "hours": 8}, status_with(ams_2_pro(), fun2="0"), 409),
        ]
        for value, status, code in cases:
            with self.subTest(value=value):
                with self.assertRaises(controls.ControlError) as ctx:
                    controls.dry(value, status, self.SUGGESTION)
                self.assertEqual(ctx.exception.status, code)
        p1s = normalize(load("pushall_p1s.json")["print"], connected=True, last_message_at=1,
                        printer={"model": "Bambu Lab P1S"})
        with self.assertRaises(controls.ControlError) as ctx:
            controls.dry({"ams_id": 0, "temperature": 50, "hours": 8}, p1s, self.SUGGESTION)
        self.assertIn('AMS 2 Pro and AMS HT only', ctx.exception.message)

    def test_stop_command(self):
        _, command = controls.dry_stop({"ams_id": 0}, status_with(ams_2_pro()))
        self.assertEqual((command["command"], command["mode"], command["temp"]), ("ams_filament_drying", 0, 0))


class EndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        config = Config(host="192.0.2.10", serial="01P00A000000000", access_code="12345678", bridge_token=SECRET,
                        data_dir=Path(self.tmp.name), profiles_dir=PROFILES, filament_ids=FILAMENT_IDS,
                        models_dir=MODELS, orca_cmd=(sys.executable, "-c", "pass"))
        self.printer = Printer(config.host, config.serial, config.access_code, "Werkstatt")
        self.printer.handle(load("pushall_p1s.json"))
        self.printer.handle(load("version_p1s.json"))
        # Synthetic: same printer with AMS 2 Pro and remote-drying firmware support.
        self.printer.handle({"print": {"command": "push_status", "fun2": "20",
                                       "ams": {"ams": [ams_2_pro(info="00000003")]}}})
        self.printer.connected = True
        self.printer._publish = mock.AsyncMock()
        self.client = TestClient(TestServer(create_app(config, printer=self.printer, start_background=False)))
        await self.client.start_server()
        self.auth = {"Authorization": f"Bearer {app_token(SECRET)}"}

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_suggestion_and_start(self):
        response = await self.client.get("/api/ams/drying?ams_id=0", headers=self.auth)
        suggestion = await response.json()
        self.assertEqual(response.status, 200, suggestion)
        # Test profiles have no drying presets; use conservative fallbacks.
        self.assertEqual((suggestion["temperature"], suggestion["hours"], suggestion["from_profiles"]), (45, 4, False))
        self.assertEqual((suggestion["max_temperature"], suggestion["cooling_temperature"]), (65, 50))
        with mock.patch.object(self.printer, "command", mock.AsyncMock(return_value={"result": "success"})) as cmd:
            response = await self.client.post("/api/control", json={
                "action": "dry", "value": {"ams_id": 0, "temperature": 55, "hours": 6}}, headers=self.auth)
            self.assertEqual(response.status, 200, await response.text())
        section, command = cmd.await_args_list[0].args
        self.assertEqual((section, command["command"], command["temp"], command["duration"], command["cooling_temp"]),
                         ("print", "ams_filament_drying", 55, 6, 50))

    async def test_unknown_ams(self):
        response = await self.client.get("/api/ams/drying?ams_id=7", headers=self.auth)
        self.assertEqual(response.status, 422)


if __name__ == "__main__":
    unittest.main()
