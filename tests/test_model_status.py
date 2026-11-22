import copy
import json
import tempfile
import unittest
from pathlib import Path

from bridge.models import Models
from bridge.state import merge, normalize, packed_temperature
from helpers import load


MODEL_DATA = {
    "BL-P001": ("Bambu Lab X1 Carbon", "core_xy"), "BL-P002": ("Bambu Lab X1", "core_xy"),
    "C13": ("Bambu Lab X1E", "core_xy"), "C11": ("Bambu Lab P1P", "core_xy"),
    "C12": ("Bambu Lab P1S", "core_xy"), "N7": ("Bambu Lab P2S", "core_xy"),
    "N1": ("Bambu Lab A1 mini", "i3"), "N2S": ("Bambu Lab A1", "i3"),
    "N9": ("Bambu Lab A2L", "i3"), "O1D": ("Bambu Lab H2D", "core_xy"),
    "O1E": ("Bambu Lab H2D Pro", "core_xy"), "O1S": ("Bambu Lab H2S", "core_xy"),
    "O1C": ("Bambu Lab H2C", "core_xy"), "O1C2": ("Bambu Lab H2C", "core_xy"),
    "N6": ("Bambu Lab X2D", "core_xy"),
}


class ModelResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        for model_id, (name, architecture) in MODEL_DATA.items():
            base = {"display_name": name, "model_id": model_id, "printer_type": model_id,
                    "printer_arch": architecture, "printer_series": "series_o" if model_id.startswith("O1") else "series_x1",
                    "printer_is_enclosed": architecture == "core_xy", "print": {
                        "nozzle_temp_range": [0, 300], "bed_temperature_limit": 100,
                        "support_bed_leveling": 1, "support_chamber_temp_edit": model_id.startswith("O1"),
                        "support_chamber_temp_edit_range": [0, 65]}}
            Path(self.tmp.name, f"{model_id}.json").write_text(json.dumps({"00.00.00.00": base}), encoding="utf-8")
        self.models = Models(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_orca_model_id_resolves(self):
        self.assertEqual(set(self.models.by_id), set(MODEL_DATA))
        for model_id, (name, _) in MODEL_DATA.items():
            with self.subTest(model_id=model_id):
                self.assertEqual(self.models.find(name, model_id).model_id, model_id)
        self.assertEqual(self.models.find("Bambu Lab X1-Carbon").model_id, "BL-P001")
        self.assertEqual(self.models.find(model_id="N7-V2").model_id, "N7")

    def test_p1s_capabilities_are_conservative(self):
        caps = self.models.find(model_id="C12").capabilities(raw={"home_flag": 1 << 21})
        self.assertEqual(caps["temperature_limits"], {"nozzle": [0, 300], "bed": [0, 100], "chamber": None})
        self.assertEqual((caps["camera"], caps["dual_nozzle"], caps["chamber_sensor"]),
                         ("jpeg6000", False, False))
        self.assertEqual(caps["nozzles"], [{"id": 0, "name": 'Nozzle'}])
        self.assertTrue(caps["calibrations"]["motor_noise"])
        self.assertFalse(caps["multi_color"])
        self.assertTrue(self.models.find(model_id="C12").capabilities(raw={"fun2": "800020"})["multi_color"])


class NewStatusTests(unittest.TestCase):
    def test_p1s_chamber_phantom_value_is_hidden(self):
        raw = copy.deepcopy(load("pushall_p1s.json")["print"])
        status = normalize(raw, connected=True, last_message_at=1, printer={"model": "Bambu Lab P1S"})
        self.assertIsNone(status["temperatures"]["chamber"])
        self.assertEqual([tray["slot"] for tray in status["ams"]["units"][0]["trays"]], [0, 1, 2, 3])
        self.assertEqual(status["ams"]["external"]["slot"], 254)

    def test_packed_dual_extruders_and_bed_forms(self):
        raw = {"device": {"extruder": {"state": 2 | (1 << 4), "info": [
            {"id": 0, "temp": 50}, {"id": 1, "temp": (250 << 16) | 245}]},
            "bed": {"info": {"temp": (60 << 16) | 55}}, "ctc": {"info": {"temp": (45 << 16) | 40}}}}
        printer = {"model": "Bambu Lab H2D", "model_id": "O1D"}
        status = normalize(raw, connected=True, last_message_at=1, printer=printer)
        self.assertEqual(status["extruders"], {"active": 1, "items": [
            {"id": 0, "name": 'Right', "temperature": 50.0, "target": 0.0},
            {"id": 1, "name": 'Left', "temperature": 245.0, "target": 250.0}]})
        self.assertEqual((status["temperatures"]["bed"], status["temperatures"]["bed_target"]), (55.0, 60.0))
        self.assertEqual((status["temperatures"]["chamber"], status["temperatures"]["chamber_target"]), (40.0, 45.0))
        raw["device"]["extruder"]["state"] = 2 | (15 << 4)
        self.assertIsNone(normalize(raw, connected=True, last_message_at=1, printer=printer)["extruders"]["active"])
        self.assertEqual(packed_temperature(0), (0.0, 0.0))
        self.assertEqual(packed_temperature("kaputt"), (None, None))

    def test_extruder_partial_updates_merge_by_id(self):
        raw = {"device": {"extruder": {"state": 2, "info": [{"id": 0, "temp": 10}, {"id": 1, "temp": 20}]}}}
        merge(raw, {"device": {"extruder": {"info": [{"id": 1, "temp": 30}]}}})
        self.assertEqual([item["temp"] for item in raw["device"]["extruder"]["info"]], [10, 30])

    def test_ht_mixed_lite_and_two_external_spools(self):
        # Synthetic, following research §4.4 [src]: HT=128/type 4, A2L=16/type 5, binding 0xE + track switch 1.
        raw = {"ams": {"ams": [
            {"id": "128", "info": "00000104", "humidity": "4", "humidity_raw": "25", "temp": "41",
             "tray": [{"id": "0", "tray_type": "PA", "tray_color": "112233FF"}]},
            {"id": "16", "info": "01000E05", "humidity": "5", "humidity_raw": "10", "temp": "30",
             "tray": [{"id": "0", "tray_type": "PLA"}, {"id": "3", "tray_type": "PETG"}]},
        ], "vir_slot": [{"id": "254", "tray_type": "PLA"}, {"id": "255", "tray_type": "PETG"}]}}
        status = normalize(raw, connected=True, last_message_at=1,
                           printer={"model": "Bambu Lab H2D", "model_id": "O1D"})
        ht, mixed = status["ams"]["units"]
        self.assertEqual((ht["type"], ht["extruder_id"], ht["trays"][0]["slot"]), ("ams_ht", 1, 128))
        self.assertEqual((mixed["type"], mixed["extruder_id"], mixed["track_switch"]),
                         ("ams_lite_mixed", None, 1))
        self.assertEqual([tray["slot"] for tray in mixed["trays"]], [24, 27])
        self.assertIsNone(mixed["humidity_percent"])
        self.assertEqual([tray["slot"] for tray in status["ams"]["externals"]], [254, 255])
        self.assertEqual([tray["source"] for tray in status["ams"]["externals"]],
                         [{"ams_id": 254, "slot_id": 0}, {"ams_id": 255, "slot_id": 0}])


if __name__ == "__main__":
    unittest.main()
