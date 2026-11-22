import copy
import unittest

from bridge.state import EXTERNAL_SLOT, fan_percent, hms_code, humidity_letter, merge, normalize
from helpers import load

PRINTER = {"name": "Test", "serial": "01P00A000000000", "model": "Bambu Lab P1S", "firmware": "01.10.00.00"}


def raw_state():
    return copy.deepcopy({k: v for k, v in load("pushall_p1s.json")["print"].items() if k not in ("command", "sequence_id")})


class MergeTests(unittest.TestCase):
    def test_partial_update_keeps_other_fields(self):
        raw = raw_state()
        for message in load("partials_p1s.json"):
            merge(raw, message["print"])
        self.assertEqual(raw["gcode_state"], "FAILED")
        self.assertEqual(raw["nozzle_temper"], load("partials_p1s.json")[-1]["print"]["nozzle_temper"])
        self.assertEqual(len(raw["ams"]["ams"][0]["tray"]), 4)

    def test_tray_update_merges_by_id(self):
        raw = raw_state()
        merge(raw, {"ams": {"ams": [{"id": "0", "tray": [{"id": "2", "remain": 40}]}]}})
        trays = raw["ams"]["ams"][0]["tray"]
        self.assertEqual([t["id"] for t in trays], ["0", "1", "2", "3"])
        self.assertEqual(trays[2]["remain"], 40)
        self.assertEqual(trays[2]["tray_type"], "PLA")  # Preserved unchanged.
        self.assertEqual(raw["ams"]["ams"][0]["humidity"], "5")

    def test_plain_list_is_replaced(self):
        raw = {"hms": [{"attr": 1, "code": 2}]}
        merge(raw, {"hms": []})
        self.assertEqual(raw["hms"], [])


class NormalizeTests(unittest.TestCase):
    def test_real_p1s_status(self):
        status = normalize(raw_state(), connected=True, last_message_at=1.0, printer=PRINTER)
        self.assertEqual(status["job"]["state"], "FAILED")
        self.assertFalse(status["job"]["active"])
        self.assertIsNone(status["job"]["remaining_minutes"])
        self.assertTrue(status["connection"]["sdcard"])
        self.assertTrue(status["lights"]["chamber"])
        self.assertEqual(status["speed"], {"level": 2, "name": "Standard", "percent": 100})
        unit = status["ams"]["units"][0]
        self.assertEqual(unit["humidity_level"], 5)
        self.assertEqual(unit["humidity_letter"], "A")
        self.assertEqual(unit["humidity_percent"], 21)
        slots = [t["slot"] for t in unit["trays"]]
        self.assertEqual(slots, [0, 1, 2, 3])
        petg, pla_generic, pla_basic = unit["trays"][0], unit["trays"][1], unit["trays"][2]
        self.assertEqual(petg["material"], "PETG")
        self.assertIsNone(petg["remain"])  # -1: no chip.
        self.assertEqual(petg["color"], "#161616FF")
        self.assertEqual(pla_generic["info_idx"], "GFL99")
        self.assertEqual(pla_basic["name"], "PLA Basic")
        self.assertEqual(pla_basic["remain"], 61)
        self.assertTrue(pla_basic["rfid"])
        self.assertEqual(status["ams"]["active_slot"], 2)
        self.assertEqual(status["ams"]["external"]["slot"], EXTERNAL_SLOT)
        self.assertEqual(status["temperatures"]["nozzle"], 49.125)

    def test_running_job(self):
        raw = raw_state()
        merge(raw, {"gcode_state": "RUNNING", "mc_remaining_time": 42, "mc_percent": 37, "stg_cur": 0,
                    "layer_num": 20, "total_layer_num": 100})
        status = normalize(raw, connected=True, last_message_at=1.0, printer=PRINTER)
        self.assertTrue(status["job"]["active"])
        self.assertEqual(status["job"]["remaining_minutes"], 42)
        self.assertEqual(status["job"]["progress"], 37)
        self.assertEqual(status["job"]["stage"], 'Printing')
        self.assertIsNotNone(status["job"]["eta"])

    def test_offline_without_data(self):
        status = normalize({}, connected=False, last_message_at=None, printer=PRINTER)
        self.assertEqual(status["job"]["state"], "OFFLINE")
        self.assertEqual(status["ams"]["units"], [])
        self.assertIsNone(status["ams"]["external"])

    def test_empty_tray(self):
        raw = raw_state()
        merge(raw, {"ams": {"ams": [{"id": "0", "tray": [{"id": "1", "tray_type": "", "tray_color": ""}]}]}})
        tray = normalize(raw, connected=True, last_message_at=1.0, printer=PRINTER)["ams"]["units"][0]["trays"][1]
        self.assertTrue(tray["empty"])
        self.assertIsNone(tray["color"])


class HelperTests(unittest.TestCase):
    def test_humidity_letter(self):
        self.assertEqual(humidity_letter(5), "A")
        self.assertEqual(humidity_letter(1), "E")
        self.assertIsNone(humidity_letter(0))

    def test_fan_percent(self):
        self.assertEqual(fan_percent("15"), 100)
        self.assertEqual(fan_percent("0"), 0)
        self.assertEqual(fan_percent("8"), 50)
        self.assertIsNone(fan_percent(None))

    def test_hms_code(self):
        self.assertEqual(hms_code({"attr": 0x03000100, "code": 0x00010007}), "0300_0100_0001_0007")
        self.assertIsNone(hms_code({"attr": "x"}))


if __name__ == "__main__":
    unittest.main()


class PrinterFlagTests(unittest.TestCase):
    def status(self, **fields):
        raw = raw_state()
        merge(raw, fields)
        return normalize(raw, connected=True, last_message_at=1.0, printer=PRINTER)

    def test_developer_mode(self):
        # Real P1S flag3 with Developer Mode (bit 4 off); disabling it sets bit 4.
        self.assertTrue(self.status()["connection"]["developer_mode"])
        self.assertFalse(self.status(flag3=90783)["connection"]["developer_mode"])
        # Firmware with `fun`: bit 29 overrides flag3.
        self.assertFalse(self.status(fun="3EC1AFFF9CFF")["connection"]["developer_mode"])
        self.assertTrue(self.status(fun="3EC18FFF9CFF", flag3=90783)["connection"]["developer_mode"])
        # Command rejection matters only when neither `fun` nor `flag3` is reported.
        rejected = [{"attr": 0x05000500, "code": 0x00010007}]
        self.assertTrue(self.status(hms=rejected)["connection"]["developer_mode"])
        without_flags = normalize({"hms": rejected}, connected=True, last_message_at=None, printer=PRINTER)
        self.assertFalse(without_flags["connection"]["developer_mode"])
        self.assertIsNone(normalize({}, connected=True, last_message_at=None, printer=PRINTER)["connection"]["developer_mode"])

    def test_calibration_and_stages(self):
        self.assertTrue(self.status()["capabilities"]["motor_noise_calibration"])  # P1S home_flag bit 21.
        job = self.status()["job"]
        self.assertFalse(job["calibration"])
        self.assertEqual(job["stages"], [])
        job = self.status(gcode_state="RUNNING", gcode_file="/usr/etc/print/auto_cali_for_user.gcode",
                          subtask_name="", stg=[13, 1, 3, 25], stg_cur=3)["job"]
        self.assertTrue(job["calibration"])
        self.assertEqual(job["name"], 'Calibration')
        self.assertEqual([s["code"] for s in job["stages"]], [13, 1, 3, 25])
        self.assertEqual(job["stages"][2]["text"], 'Vibration compensation')
        self.assertEqual(job["stage"], 'Vibration compensation')
        self.assertEqual([s["code"] for s in self.status(gcode_state="PREPARE", stg=[{"a": 2}, {"b": 13}])["job"]["stages"]],
                         [2, 13])

    def test_print_error_code(self):
        self.assertIsNone(self.status()["job"]["error"])
        job = self.status(print_error=0x0300400C)["job"]
        self.assertEqual(job["error"], {"code": "0300_400C", "value": 0x0300400C})
        self.assertEqual(job["subtask_id"], "0")

    def test_options(self):
        # Real P1S flags: backup/remainder on, recovery off, RFID only on insertion.
        self.assertEqual(self.status()["options"], {
            "auto_recovery": False, "ams_backup": True, "rfid_on_startup": False, "rfid_on_insert": True,
            "remain_estimate": True})
        changed = self.status(home_flag=23152015 | 0x10, ams={"power_on_flag": True, "calibrate_remain_flag": False})
        self.assertTrue(changed["options"]["auto_recovery"])
        self.assertTrue(changed["options"]["rfid_on_startup"])
        self.assertFalse(changed["options"]["remain_estimate"])  # Explicit AMS setting overrides home_flag.
        without_ams = normalize({"home_flag": 16}, connected=True, last_message_at=None, printer=PRINTER)["options"]
        self.assertEqual(without_ams, {"auto_recovery": True, "ams_backup": None, "rfid_on_startup": None,
                                       "rfid_on_insert": None, "remain_estimate": None})


class BareTrayTests(unittest.TestCase):
    """The P1 series reports a removed spool as a bare `{"id": n}`; the old fields must not linger."""

    def test_bare_slot_replaces_the_old_spool(self):
        from bridge.printer import Printer
        printer = Printer("h", "s", "c")
        printer.handle(load("pushall_p1s.json"))
        before = next(t for t in printer.status()["ams"]["units"][0]["trays"] if t["slot"] == 2)
        self.assertFalse(before["empty"])
        printer.handle({"print": {"command": "push_status", "ams": {
            "tray_exist_bits": "f", "ams": [{"id": "0", "tray": [{"id": "2"}]}]}}})
        tray = next(t for t in printer.status()["ams"]["units"][0]["trays"] if t["slot"] == 2)
        self.assertTrue(tray["empty"])
        self.assertFalse(tray["rfid"])
        self.assertIsNone(tray["spool_uuid"])
        self.assertTrue(tray["present"])  # A spool is in the slot, its filament is unknown.
        # Partial updates with more fields still merge into the slot.
        printer.handle({"print": {"command": "push_status", "ams": {"tray_exist_bits": "b", "ams": [{"id": "0", "tray": [
            {"id": "1", "remain": 42}]}]}}})
        trays = {t["slot"]: t for t in printer.status()["ams"]["units"][0]["trays"]}
        self.assertEqual(trays[1]["remain"], 42)
        self.assertIsNotNone(trays[1]["material"])
        self.assertFalse(trays[2]["present"])

    def test_multicolor_slot(self):
        from bridge.printer import Printer
        printer = Printer("h", "s", "c")
        printer.handle(load("pushall_p1s.json"))
        printer.handle({"print": {"command": "push_status", "ams": {"ams": [{"id": "0", "tray": [
            {"id": "0", "tray_type": "PLA", "tray_color": "E94B3CFF", "cols": ["E94B3CFF", "FFFFFFFF"], "ctype": 0},
            {"id": "1", "tray_type": "PLA", "tray_color": "FF0000FF", "cols": ["FF0000FF", "00FF00FF"], "ctype": 1}]}]}}})
        status = printer.status()
        trays = {t["slot"]: t for t in status["ams"]["units"][0]["trays"]}
        self.assertEqual(trays[0]["colors"], ["#E94B3CFF", "#FFFFFFFF"])
        self.assertTrue(trays[0]["gradient"])
        self.assertFalse(trays[1]["gradient"])
