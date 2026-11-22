import unittest

from bridge.controls import build
from bridge.print_command import build_project_file
from bridge.printer import Printer
from helpers import load


LEGACY_MODELS = ("BL-P001", "BL-P002", "C13", "C11", "C12", "N1", "N2S")


def p1s_status(**fields):
    printer = Printer("drucker", "01P00A000000000", "code")
    printer.handle(load("pushall_p1s.json"))
    if fields:
        printer.handle({"print": {"command": "push_status", **fields}})
    return printer.status()


class LegacyPrintGoldenTests(unittest.TestCase):
    """Existing LAN print commands retain identical fields and types for all legacy models."""

    def test_all_legacy_models_use_the_exact_existing_payload(self):
        plan = {
            "plate": 2,
            "remote_name": "teil.gcode.3mf",
            "timelapse": True,
            "bed_leveling": False,
            "flow_cali": False,
            "use_ams": True,
            "ams_mapping": [3, -1, 6],
        }
        expected = {
            "command": "project_file",
            "param": "Metadata/plate_2.gcode",
            "project_id": "0", "profile_id": "0", "task_id": "0", "subtask_id": "0",
            "subtask_name": "Teil", "url": "file:///sdcard/teil.gcode.3mf", "timelapse": True,
            "bed_type": "auto", "bed_leveling": False, "bed_levelling": False, "flow_cali": False,
            "vibration_cali": False, "layer_inspect": False, "use_ams": True,
            "ams_mapping": [3, -1, 6],
        }
        for model_id in LEGACY_MODELS:
            with self.subTest(model_id=model_id):
                self.assertEqual(build_project_file(model_id, "Teil", plan), expected)

    def test_external_spool_stays_empty_mapping_and_without_ams(self):
        plan = {
            "plate": 1, "remote_name": "extern.gcode.3mf", "timelapse": False,
            "bed_leveling": True, "flow_cali": False, "use_ams": False, "ams_mapping": [],
        }
        command = build_project_file("C12", "Extern", plan)
        self.assertEqual(command["ams_mapping"], [])
        self.assertIs(command["use_ams"], False)
        self.assertNotIn("file", command)
        self.assertNotIn("ams_mapping2", command)
        self.assertNotIn("auto_bed_leveling", command)


class P1SControlGoldenTests(unittest.TestCase):
    """Complete dictionaries and G-code strings for deployed P1S behavior."""

    def test_light(self):
        self.assertEqual(build("light", True, p1s_status()), [("system", {
            "command": "ledctrl", "led_node": "chamber_light", "led_mode": "on",
            "led_on_time": 500, "led_off_time": 500, "loop_times": 0, "interval_time": 0,
        })])

    def test_move(self):
        self.assertEqual(build("move", {"axis": "Z", "distance": -5}, p1s_status(gcode_state="IDLE")), [
            ("print", {"command": "gcode_line", "param":
                       "M211 S \nM211 X1 Y1 Z1 \nM1002 push_ref_mode \nG91 \nG1 Z-5.0 F600 \n"
                       "M1002 pop_ref_mode \nM211 R \n"})])

    def test_temperature_fan_and_filament(self):
        status = p1s_status(gcode_state="IDLE")
        self.assertEqual(build("temperature", {"heater": "nozzle", "target": 220}, status), [
            ("print", {"command": "gcode_line", "param": "M104 S220 \n"})])
        self.assertEqual(build("fan", {"fan": "aux", "percent": 50}, status), [
            ("print", {"command": "gcode_line", "param": "M106 P2 S128 \n"})])
        self.assertEqual(build("load", 3, status), [("print", {
            "command": "ams_change_filament", "ams_id": 0, "slot_id": 3, "target": 3,
            "curr_temp": 210, "tar_temp": 210,
        })])


if __name__ == "__main__":
    unittest.main()
