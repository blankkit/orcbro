import unittest

from bridge.controls import ControlError, build, k_select, tray_setting
from bridge.printer import Printer
from helpers import load


def status(capabilities: dict) -> dict:
    printer = Printer("host", "serial", "code")
    printer.handle(load("pushall_p1s.json"))
    printer.handle({"print": {"command": "push_status", "gcode_state": "IDLE", "nozzle_temper": 220}})
    result = printer.status()
    result["printer"]["capabilities"] = capabilities
    return result


class ModelControlTests(unittest.TestCase):
    def test_i3_negates_y_and_z_only(self):
        state = status({"architecture": "i3"})
        for axis, sent in (("X", "X10.0"), ("Y", "Y-10.0"), ("Z", "Z-10.0")):
            with self.subTest(axis=axis):
                command = build("move", {"axis": axis, "distance": 10}, state)[0][1]["param"]
                self.assertIn(sent, command)

    def test_dual_nozzle_temperature_needs_explicit_side(self):
        caps = {"dual_nozzle": True, "temperature_limits": {"nozzle": [0, 350], "bed": [0, 120]}}
        state = status(caps)
        with self.assertRaises(ControlError):
            build("temperature", {"heater": "nozzle", "target": 300}, state)
        self.assertEqual(build("temperature", {"heater": "nozzle", "target": 320, "extruder": 1}, state),
                         [("print", {"command": "set_nozzle_temp", "extruder_index": 1, "target_temp": 320})])
        with self.assertRaises(ControlError):
            build("temperature", {"heater": "nozzle", "target": 351, "extruder": 1}, state)

    def test_new_bed_command_homing_and_two_h2_lights(self):
        caps = {"mqtt_bed_temperature": True, "mqtt_homing": True, "second_chamber_light": True,
                "temperature_limits": {"nozzle": [0, 350], "bed": [0, 120]}}
        state = status(caps)
        self.assertEqual(build("temperature", {"heater": "bed", "target": 110}, state),
                         [("print", {"command": "set_bed_temp", "temp": 110})])
        self.assertEqual(build("home", None, state), [("print", {"command": "back_to_center"})])
        lights = build("light", True, state)
        self.assertEqual([body["led_node"] for _, body in lights], ["chamber_light", "chamber_light2"])
        self.assertEqual(lights[0][1]["led_on_time"], 500)

    def test_chamber_control_is_conservatively_refused(self):
        state = status({"chamber_heater": True, "temperature_limits": {"chamber": [0, 65]}})
        with self.assertRaises(ControlError) as caught:
            build("temperature", {"heater": "chamber", "target": 45}, state)
        self.assertEqual(caught.exception.status, 409)

    def test_unproven_ams_writes_are_refused(self):
        info = {"bambu_id": "GFA00", "type": "PLA"}
        with self.assertRaises(ControlError):
            tray_setting(128, info, "#FFFFFF", {"ams_id": 128, "slot_id": 0})
        tray = {"info_idx": "GFA00", "source": {"ams_id": 16, "slot_id": 0}}
        with self.assertRaises(ControlError):
            k_select(24, tray, 1, 0.4)


if __name__ == "__main__":
    unittest.main()
