import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from bridge.config import Config
from bridge.jobs import JobError
from bridge.print_command import build_project_file
from bridge.print_metadata import read_print_metadata
from bridge.profiles import ProfileError
from bridge.server import build_new_mapping


def sliced_file(path: Path) -> None:
    """Synthetic dual-nozzle 3MF following research §1.4/§1.5 [src]."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Metadata/project_settings.config", json.dumps({
            "filament_type": ["PA", "PLA", "PETG"], "filament_nozzle_id": [1, 0, 0]}))
        archive.writestr("Metadata/slice_info.config", """<config><plate>
          <metadata key="index" value="1"/>
          <filament id="1" type="PA" nozzle_id="1"/>
          <filament id="3" type="PETG" nozzle_id="0"/>
        </plate></config>""")


def dual_status() -> dict:
    return {"ams": {"units": [{"trays": [{
        "slot": 128, "empty": False, "extruder_id": 1, "source": {"ams_id": 128, "slot_id": 0},
    }]}], "externals": [{
        "slot": 254, "empty": False, "extruder_id": 1, "source": {"ams_id": 254, "slot_id": 0},
    }, {
        "slot": 255, "empty": False, "extruder_id": 0, "source": {"ams_id": 255, "slot_id": 0},
    }], "external": None}}


class PrintMetadataTests(unittest.TestCase):
    def test_full_filament_list_and_explicit_nozzles(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dual.gcode.3mf"
            sliced_file(path)
            metadata = read_print_metadata(path, 1)
        self.assertEqual(metadata, {"filaments": [
            {"id": 1, "used": True, "nozzle_id": 1},
            {"id": 2, "used": False, "nozzle_id": 0},
            {"id": 3, "used": True, "nozzle_id": 0}], "used": [1, 3]})

    def test_missing_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.gcode.3mf"
            with zipfile.ZipFile(path, "w"):
                pass
            with self.assertRaises(JobError):
                read_print_metadata(path, 1)


class NewMappingTests(unittest.TestCase):
    METADATA = {"filaments": [
        {"id": 1, "used": True, "nozzle_id": 1},
        {"id": 2, "used": False, "nozzle_id": 0},
        {"id": 3, "used": True, "nozzle_id": 0}], "used": [1, 3]}

    def test_ht_unused_and_external_are_encoded_for_both_protocols(self):
        mapping, mapping2, use_ams = build_new_mapping(
            self.METADATA, {1: 128, 3: 255}, dual_status(), dual=True)
        self.assertEqual(mapping, [128, -1, -1])
        self.assertEqual(mapping2, [
            {"ams_id": 128, "slot_id": 0}, {"ams_id": 255, "slot_id": 255},
            {"ams_id": 255, "slot_id": 0}])
        self.assertTrue(use_ams)

    def test_wrong_or_unknown_nozzle_binding_is_rejected(self):
        status = dual_status()
        status["ams"]["units"][0]["trays"][0]["extruder_id"] = 0
        with self.assertRaises(ProfileError):
            build_new_mapping(self.METADATA, {1: 128, 3: 255}, status, dual=True)
        status["ams"]["units"][0]["trays"][0]["extruder_id"] = None
        with self.assertRaises(ProfileError):
            build_new_mapping(self.METADATA, {1: 128, 3: 255}, status, dual=True)

    def test_a2l_mixed_lite_uses_24_to_27(self):
        metadata = {"filaments": [{"id": 1, "used": True, "nozzle_id": None}], "used": [1]}
        status = {"ams": {"units": [{"trays": [{
            "slot": 27, "empty": False, "source": {"ams_id": 16, "slot_id": 3}, "extruder_id": None,
        }]}], "externals": [], "external": None}}
        self.assertEqual(build_new_mapping(metadata, {1: 27}, status, dual=False),
                         ([27], [{"ams_id": 16, "slot_id": 3}], True))


class NewPayloadTests(unittest.TestCase):
    def test_new_payload_has_deterministic_integer_flags(self):
        plan = {"plate": 1, "remote_name": "auftrag.gcode.3mf", "timelapse": False,
                "bed_leveling": False, "flow_cali": False, "use_ams": True, "new_protocol": True,
                "ams_mapping": [128, -1], "ams_mapping2": [
                    {"ams_id": 128, "slot_id": 0}, {"ams_id": 255, "slot_id": 255}],
                "nozzle_offset_cali": False, "extrude_cali_manual_mode": 0}
        caps = {"calibrations": {"nozzle_offset": True}, "pa_mode": True}
        command = build_project_file("O1D", "Auftrag", plan, capabilities=caps)
        self.assertEqual(command["url"], "ftp:///auftrag.gcode.3mf")
        self.assertEqual(command["file"], "auftrag.gcode.3mf")
        self.assertEqual((command["auto_bed_leveling"], command["extrude_cali_flag"],
                          command["nozzle_offset_cali"], command["extrude_cali_manual_mode"]), (0, 0, 0, 0))
        self.assertNotIn("md5", command)
        self.assertNotIn("bed_levelling", command)

    def test_explicit_url_override_wins(self):
        plan = {"plate": 1, "remote_name": "a.gcode.3mf", "timelapse": False, "bed_leveling": True,
                "flow_cali": True, "use_ams": False, "new_protocol": True, "ams_mapping": [-1],
                "ams_mapping2": [{"ams_id": 255, "slot_id": 0}]}
        command = build_project_file("N7", "A", plan, print_url="ftp:///custom/{name}")
        self.assertEqual(command["url"], "ftp:///custom/a.gcode.3mf")
        self.assertEqual((command["auto_bed_leveling"], command["extrude_cali_flag"]), (1, 1))
        self.assertIsNone(Config(bridge_token="x" * 20).print_url)


if __name__ == "__main__":
    unittest.main()
