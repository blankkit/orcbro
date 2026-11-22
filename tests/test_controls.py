import copy
import io
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path

from bridge import controls
from bridge.controls import ControlError, build, tray_setting
from bridge.ftps import ImplicitFTPS, parse_list_line
from bridge.jobs import JobError, read_objects
from bridge.printer import Printer
from bridge.server import iso_modified, sd_path
from helpers import load, sliced_3mf


def status_with(**print_fields):
    printer = Printer("h", "s", "c")
    printer.handle(load("pushall_p1s.json"))
    if print_fields:
        printer.handle({"print": {"command": "push_status", **print_fields}})
    return printer.status()


OBJECTS = [{"id": 22, "name": "a", "skipped": False}, {"id": 30, "name": "b", "skipped": False},
           {"id": 41, "name": "c", "skipped": False}]


class StateRuleTests(unittest.TestCase):
    def test_motion_only_when_idle(self):
        running = status_with(gcode_state="RUNNING")
        for action, value in [("home", None), ("move", {"axis": "X", "distance": 10}), ("extrude", 10),
                              ("load", 2), ("unload", None)]:
            with self.subTest(action=action), self.assertRaises(ControlError) as ctx:
                build(action, value, running)
            self.assertEqual(ctx.exception.status, 409)

    def test_nothing_moves_before_homing(self):
        # Real P1S home_flag with X/Y/Z homed; higher bits describe printer capabilities.
        homed = 23152015
        self.assertEqual(status_with(gcode_state="IDLE", home_flag=homed)["homed"], {"x": True, "y": True, "z": True})
        moves = [("move", {"axis": "X", "distance": 10}), ("move", {"axis": "Z", "distance": -1}), ("extrude", 10),
                 ("load", 2), ("unload", None)]
        for flag, label in ((homed & ~0b111, "nichts referenziert"), (homed & ~0b100, "Z fehlt")):
            state = status_with(gcode_state="IDLE", home_flag=flag)
            for action, value in moves:
                with self.subTest(label=label, action=action), self.assertRaises(ControlError) as ctx:
                    build(action, value, state)
                self.assertEqual(ctx.exception.status, 409)
                self.assertIn('Home the printer first', str(ctx.exception))
            self.assertTrue(build("home", None, state))  # Homing itself remains available.
        unknown = status_with(gcode_state="IDLE")
        unknown["homed"] = None  # The printer has not reported home_flag yet.
        with self.assertRaises(ControlError):
            build("move", {"axis": "X", "distance": 10}, unknown)
        self.assertTrue(build("move", {"axis": "X", "distance": 10}, status_with(gcode_state="IDLE", home_flag=homed)))

    def test_move_command(self):
        (section, body), = build("move", {"axis": "z", "distance": -5}, status_with(gcode_state="IDLE"))
        self.assertEqual(section, "print")
        self.assertEqual(body["command"], "gcode_line")
        self.assertIn("G91 \n", body["param"])
        self.assertIn("G1 Z-5.0 F600 \n", body["param"])
        self.assertTrue(body["param"].endswith("M211 R \n"))
        with self.assertRaises(ControlError):
            build("move", {"axis": "X", "distance": 500}, status_with(gcode_state="IDLE"))
        with self.assertRaises(ControlError):
            build("move", {"axis": "E", "distance": 1}, status_with(gcode_state="IDLE"))

    def test_extrude_needs_hot_nozzle(self):
        cold = status_with(gcode_state="IDLE", nozzle_temper=40)
        with self.assertRaises(ControlError) as ctx:
            build("extrude", 10, cold)
        self.assertIn('too cold', ctx.exception.message)
        (_, body), = build("extrude", -5, status_with(gcode_state="IDLE", nozzle_temper=215))
        self.assertIn("G0 E-5.0 F300", body["param"])

    def test_temperature_and_fan_limits(self):
        printing = status_with(gcode_state="RUNNING")
        (_, body), = build("temperature", {"heater": "bed", "target": 60}, printing)
        self.assertEqual(body["param"], "M140 S60 \n")
        (_, body), = build("fan", {"fan": "aux", "percent": 50}, printing)
        self.assertEqual(body["param"], "M106 P2 S128 \n")
        for value in ({"heater": "nozzle", "target": 350}, {"heater": "bed", "target": -1}, {"heater": "chamber", "target": 40}):
            with self.subTest(value=value), self.assertRaises(ControlError):
                build("temperature", value, printing)
        with self.assertRaises(ControlError):
            build("fan", {"fan": "part", "percent": 120}, printing)

    def test_pause_resume_stop(self):
        self.assertEqual(build("pause", None, status_with(gcode_state="RUNNING"))[0][1]["command"], "pause")
        with self.assertRaises(ControlError):
            build("resume", None, status_with(gcode_state="RUNNING"))
        with self.assertRaises(ControlError):
            build("stop", None, status_with(gcode_state="IDLE"))
        with self.assertRaises(ControlError):
            build("selbstzerstörung", None, status_with())


class FilamentTests(unittest.TestCase):
    def test_load_slot(self):
        (_, body), = build("load", 3, status_with(gcode_state="IDLE"))
        self.assertEqual(body["command"], "ams_change_filament")
        self.assertEqual((body["ams_id"], body["slot_id"], body["target"]), (0, 3, 3))
        self.assertEqual(body["tar_temp"], 210)  # PLA 190–230 → midpoint.
        with self.assertRaises(ControlError):
            build("load", 254, status_with(gcode_state="IDLE"))  # External spool must be loaded by hand.

    def test_unload_active(self):
        (_, body), = build("unload", None, status_with(gcode_state="FINISH"))
        self.assertEqual((body["target"], body["slot_id"], body["ams_id"]), (255, 255, 0))
        nothing = status_with(gcode_state="IDLE")
        nothing["ams"]["active_slot"] = None
        with self.assertRaises(ControlError):
            build("unload", None, nothing)

    def test_tray_setting(self):
        info = {"bambu_id": "GFA00", "type": "PLA", "nozzle_temp_min": 190, "nozzle_temp_max": 230}
        section, body = tray_setting(1, info, "#3f8e43")
        self.assertEqual(body["tray_color"], "3F8E43FF")
        self.assertEqual((body["ams_id"], body["slot_id"], body["tray_id"]), (0, 1, 1))
        self.assertEqual(body["tray_info_idx"], "GFA00")
        _, ext = tray_setting(254, info, "#FFFFFF")
        self.assertEqual((ext["ams_id"], ext["tray_id"]), (255, 254))
        with self.assertRaises(ControlError):
            tray_setting(1, info, "grün")

    def test_load_temp(self):
        self.assertEqual(controls.load_temp(None), 220)
        self.assertEqual(controls.load_temp({"nozzle_temp_min": 230, "nozzle_temp_max": 280}), 255)


class KValueTests(unittest.TestCase):
    REPLY = {"command": "extrusion_cali_get", "result": "success", "nozzle_diameter": "0.4", "filaments": [
        {"cali_idx": 3, "filament_id": "GFSNL08", "setting_id": "", "name": "SUNLU PETG", "k_value": "0.034000",
         "n_coef": "0.000000"},
        {"cali_idx": 1, "filament_id": "GFA00", "name": "Basic PLA", "k_value": 0.02, "n_coef": 1.4},
        {"cali_idx": 2, "filament_id": "GFA00", "name": "kaputt", "k_value": "x"},
        {"filament_id": "GFA00", "name": "ohne Index", "k_value": "0.02"},
    ]}

    def test_parse_table(self):
        values = controls.k_values(self.REPLY)
        self.assertEqual([(v["cali_idx"], v["filament_id"], v["k"]) for v in values],
                         [(1, "GFA00", 0.02), (3, "GFSNL08", 0.034)])
        self.assertIsNone(values[1]["setting_id"])
        self.assertEqual(controls.k_values(None), [])

    def test_query_and_select(self):
        self.assertEqual(controls.k_values_query(0.4)[1],
                         {"command": "extrusion_cali_get", "filament_id": "", "nozzle_diameter": "0.4"})
        with self.assertRaises(ControlError):
            controls.k_values_query(None)
        status = status_with(gcode_state="IDLE")
        tray = controls.find_tray(status, 0)
        _, body = controls.k_select(0, tray, 3, 0.4)
        self.assertEqual((body["command"], body["tray_id"], body["ams_id"], body["slot_id"], body["cali_idx"]),
                         ("extrusion_cali_sel", 0, 0, 0, 3))
        self.assertEqual((body["filament_id"], body["nozzle_diameter"]), (tray["info_idx"], "0.4"))
        _, body = controls.k_select(6, {"info_idx": "GFA00"}, -1, 0.4)
        self.assertEqual((body["tray_id"], body["ams_id"], body["slot_id"]), (6, 1, 2))
        _, body = controls.k_select(254, {"info_idx": "GFL99"}, 1, 0.4)
        self.assertEqual((body["tray_id"], body["ams_id"], body["slot_id"]), (254, 255, 0))
        for slot, tray, idx in ((0, {"info_idx": None}, 1), (0, {"info_idx": "GFA00"}, -2), (0, {"info_idx": "GFA00"}, True),
                                (99, {"info_idx": "GFA00"}, 1)):
            with self.subTest(slot=slot, idx=idx), self.assertRaises(ControlError):
                controls.k_select(slot, tray, idx, 0.4)

    def test_tray_reports_selection(self):
        tray = controls.find_tray(status_with(gcode_state="IDLE"), 0)
        self.assertEqual(tray["cali_idx"], -1)


class SkipTests(unittest.TestCase):
    def test_skip_rules(self):
        running = status_with(gcode_state="RUNNING")
        (_, body), = build("skip_objects", [30], running, copy.deepcopy(OBJECTS))
        self.assertEqual(body, {"command": "skip_objects", "obj_list": [30]})
        with self.assertRaises(ControlError):
            build("skip_objects", [99], running, OBJECTS)
        with self.assertRaises(ControlError) as ctx:
            build("skip_objects", [22, 30, 41], running, OBJECTS)
        self.assertIn('At least one object', ctx.exception.message)
        with self.assertRaises(ControlError):
            build("skip_objects", [30], status_with(gcode_state="IDLE"), OBJECTS)

    def test_already_skipped_count(self):
        running = status_with(gcode_state="RUNNING", s_obj=[22, 30])
        with self.assertRaises(ControlError):
            build("skip_objects", [41], running, OBJECTS)
        self.assertEqual(running["job"]["skipped_objects"], [22, 30])

    def test_read_objects_with_boxes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.gcode.3mf"
            path.write_bytes(sliced_3mf())
            with zipfile.ZipFile(path, "a") as archive:
                archive.writestr("Metadata/plate_1.json",
                                 '{"bbox_objects": [{"id": 35, "name": "cube.stl", "bbox": [118, 118, 138, 138]}]}')
            objects = read_objects(path, 1)
            self.assertEqual(objects, [{"id": 1, "name": "cube.stl", "skipped": False, "bbox": [118.0, 118.0, 138.0, 138.0]}])
            with self.assertRaises(JobError):
                read_objects(path, 2)

    def test_plate_index_from_gcode_file(self):
        status = status_with(gcode_state="RUNNING", gcode_file="/data/Metadata/plate_3.gcode")
        self.assertEqual(status["job"]["plate_index"], 3)


class SDCardTests(unittest.TestCase):
    def test_parse_list_line(self):
        entry = parse_list_line("-rw-rw-rw-   1 root  root    859153 Nov 20 14:48 solder-holder-fanttik.gcode.3mf")
        self.assertEqual(entry["name"], "solder-holder-fanttik.gcode.3mf")
        self.assertEqual(entry["size"], 859153)
        self.assertFalse(entry["is_dir"])
        self.assertEqual(parse_list_line("drw-rw-rw-   1 root  root  0 May 22 2023 model")["modified"], "2023-05-22")
        self.assertIsNone(parse_list_line("-rw 1 x"))
        # Preserve spaces in filenames.
        self.assertEqual(parse_list_line("-rw-rw-rw- 1 root root 5 Sep 02 2023 Hive Tonie Regal.gcode.3mf")["name"],
                         "Hive Tonie Regal.gcode.3mf")

    def test_upload_skips_tls_shutdown(self):
        # The printer ignores close_notify; data-channel unwrap() would hang until timeout.
        class DataChannel:
            def __init__(self):
                self.received, self.unwrapped, self.closed = b"", False, False

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.closed = True

            def sendall(self, data):
                self.received += data

            def unwrap(self):
                self.unwrapped = True

        channel, commands, chunks = DataChannel(), [], []
        ftp = ImplicitFTPS()
        ftp.voidcmd = lambda cmd: commands.append(cmd)
        ftp.transfercmd = lambda cmd, rest=None: commands.append(cmd) or channel
        ftp.voidresp = lambda: "226 Transfer complete"
        reply = ftp.storbinary("STOR /x.gcode.3mf", io.BytesIO(b"a" * 10), blocksize=4, callback=chunks.append)
        self.assertEqual(reply, "226 Transfer complete")
        self.assertEqual(commands, ["TYPE I", "STOR /x.gcode.3mf"])
        self.assertEqual(channel.received, b"a" * 10)
        self.assertEqual([len(c) for c in chunks], [4, 4, 2])
        self.assertFalse(channel.unwrapped)
        self.assertTrue(channel.closed)

    def test_iso_modified(self):
        now = datetime(2026, 11, 20, 16, 0)
        self.assertEqual(iso_modified("Nov 20 14:48", now), "2026-11-20T14:48")
        self.assertEqual(iso_modified("Dec 30 10:00", now), "2025-12-30T10:00")
        self.assertEqual(iso_modified("2023-08-31", now), "2023-08-31")
        self.assertIsNone(iso_modified("kaputt", now))

    def test_sd_path(self):
        self.assertEqual(sd_path("/timelapse/video.avi"), ("/timelapse", "video.avi"))
        self.assertEqual(sd_path("Unbenannt.gcode.3mf"), ("/", "Unbenannt.gcode.3mf"))
        for bad in ("/../etc/passwd", "/model/x.gcode", "/cache/", None, "/a\\b"):
            with self.subTest(bad=bad), self.assertRaises(JobError):
                sd_path(bad)


if __name__ == "__main__":
    unittest.main()


class ListRetryTests(unittest.TestCase):
    def test_list_dir_retries_once(self):
        from unittest import mock
        from bridge import ftps
        calls = []

        def flaky(*args):
            calls.append(args)
            if len(calls) == 1:
                raise OSError("EOF occurred in violation of protocol")
            return [{"name": "a.gcode.3mf", "size": 1, "is_dir": False, "modified": None}]

        with mock.patch.object(ftps, "_list_dir_once", flaky), mock.patch.object(ftps.time, "sleep"):
            self.assertEqual(len(ftps.list_dir("h", "c", "/")), 1)
        self.assertEqual(len(calls), 2)
        with mock.patch.object(ftps, "_list_dir_once", mock.Mock(side_effect=OSError("weg"))), \
                mock.patch.object(ftps.time, "sleep"), self.assertRaises(OSError):
            ftps.list_dir("h", "c", "/")


class OptionTests(unittest.TestCase):
    def test_print_options(self):
        status = status_with(gcode_state="RUNNING")  # Options remain available during printing.
        self.assertEqual(build("option", {"key": "ams_backup", "enabled": False}, status),
                         [("print", {"command": "print_option", "auto_switch_filament": False})])
        self.assertEqual(build("option", {"key": "auto_recovery", "enabled": True}, status),
                         [("print", {"command": "print_option", "option": 1, "auto_recovery": True})])
        # RFID and remainder settings share a command; retain the printer's other reported values.
        [(_, body)] = build("option", {"key": "rfid_on_startup", "enabled": True}, status)
        self.assertEqual(body, {"command": "ams_user_setting", "ams_id": -1, "startup_read_option": True,
                                "tray_read_option": True, "calibrate_remain_flag": True})
        for value in ({"key": "turbo", "enabled": True}, {"key": "ams_backup", "enabled": "ja"}, None):
            with self.subTest(value=value), self.assertRaises(ControlError) as ctx:
                build("option", value, status)
            self.assertEqual(ctx.exception.status, 422)

    def test_ams_settings_need_reported_state(self):
        status = status_with()
        status["options"]["rfid_on_insert"] = None
        with self.assertRaises(ControlError) as ctx:
            build("option", {"key": "remain_estimate", "enabled": False}, status)
        self.assertEqual(ctx.exception.status, 409)
        status["ams"]["units"] = []
        with self.assertRaises(ControlError):
            build("option", {"key": "ams_backup", "enabled": True}, status)

    def test_calibration(self):
        idle = status_with(gcode_state="IDLE")
        self.assertEqual(build("calibrate", {"bed_leveling": True, "vibration": True, "motor_noise": True}, idle),
                         [("print", {"command": "calibration", "option": 14})])
        self.assertEqual(build("calibrate", {"motor_noise": True}, idle)[0][1]["option"], 8)
        self.assertEqual(build("calibrate", {"bed_leveling": True, "vibration": True}, idle)[0][1]["option"], 6)
        for value in ({}, {"bed_leveling": False}, {"lidar": True}, {"vibration": "ja"}, None):
            with self.subTest(value=value), self.assertRaises(ControlError) as ctx:
                build("calibrate", value, idle)
            self.assertEqual(ctx.exception.status, 422)
        with self.assertRaises(ControlError) as ctx:
            build("calibrate", {"vibration": True}, status_with(gcode_state="RUNNING"))
        self.assertEqual(ctx.exception.status, 409)
        idle["capabilities"]["motor_noise_calibration"] = False
        with self.assertRaises(ControlError) as ctx:
            build("calibrate", {"motor_noise": True}, idle)
        self.assertEqual(ctx.exception.status, 409)

    def test_clear_error(self):
        with self.assertRaises(ControlError):
            controls.clear_error(status_with())
        (quit_section, quit_body), (dialog_section, dialog_body) = controls.clear_error(status_with(print_error=0x0300400C))
        self.assertEqual((quit_section, quit_body), ("print", {"command": "clean_print_error", "subtask_id": "0",
                                                               "print_error": 0x0300400C}))
        self.assertEqual(dialog_section, "system")
        self.assertEqual((dialog_body["command"], dialog_body["err"]), ("uiop", "0300400C"))
