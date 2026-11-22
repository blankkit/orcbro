import json
import tempfile
import unittest
from pathlib import Path

from bridge.profiles import Catalog, ProfileError, apply_options
from helpers import FILAMENT_IDS, PROFILES

MACHINE = "Bambu Lab P1S 0.4 nozzle"


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog(PROFILES, MACHINE, FILAMENT_IDS)

    def test_flatten_resolves_inherits_and_include(self):
        flat = self.catalog.flat("machine", MACHINE)
        self.assertEqual(flat["printer_technology"], "FFF")  # Inherited.
        self.assertEqual(flat["machine_start_gcode"], "G28 ; start")  # Template overrides parent value.
        self.assertNotIn("include", flat)
        self.assertEqual(flat["inherits"], "")
        self.assertEqual(flat["name"], MACHINE)

    def test_flow_calibration_by_model(self):
        # The fixture P1S start G-code never checks extrude_cali_flag: no automatic measurement.
        self.assertIs(self.catalog.flow_calibration("Bambu Lab P1S"), False)
        self.assertIsNone(self.catalog.flow_calibration("Bambu Lab H9 Unbekannt"))
        self.assertIsNone(self.catalog.flow_calibration(None))
        with tempfile.TemporaryDirectory() as tmp:
            import shutil
            root = Path(tmp) / "BBL"
            shutil.copytree(PROFILES, root)
            (root / "machine" / "Test X1C template machine_start_gcode.json").write_text(json.dumps({
                "type": "machine", "name": "Test X1C template machine_start_gcode", "instantiation": "false",
                "machine_start_gcode": "M1002 judge_flag extrude_cali_flag\nM622 J1\nM623"}))
            (root / "machine" / "Bambu Lab X1 Carbon 0.4 nozzle.json").write_text(json.dumps({
                "type": "machine", "name": "Bambu Lab X1 Carbon 0.4 nozzle", "inherits": "fdm_common",
                "instantiation": "true", "printer_model": "Bambu Lab X1 Carbon",
                "include": ["Test X1C template machine_start_gcode"]}))
            catalog = Catalog(root, MACHINE, FILAMENT_IDS)
            self.assertIs(catalog.flow_calibration("Bambu Lab X1 Carbon"), True)
            self.assertIs(catalog.flow_calibration("Bambu Lab P1S"), False)

    def test_only_compatible_processes(self):
        self.assertEqual(self.catalog.processes, ["0.12mm Fine @BBL X1C", "0.20mm Standard @BBL X1C"])
        self.assertEqual(self.catalog.default_process(), "0.20mm Standard @BBL X1C")
        labels = [q["label"] for q in self.catalog.quality_choices()]
        self.assertEqual(labels, ["0.12mm Fine", "0.20mm Standard"])

    def test_filament_for_tray(self):
        f = self.catalog.filament_for_tray
        self.assertEqual(f("GFA00", "PLA"), "Bambu PLA Basic @BBL P1S 0.4 nozzle")
        self.assertEqual(f("GFL99", "PLA"), "Generic PLA")
        # BBL/filament/SUNLU contains SUNLU PETG profiles compatible with P1S.
        self.assertEqual(f("GFSNL08", "PETG"), "SUNLU PETG @BBL X1C")
        # Without a matching ID, use Generic PETG instead of PETG HF or vendor profiles.
        self.assertEqual(f("GFXX99", "PETG"), "Generic PETG")
        self.assertEqual(f(None, "PETG"), "Generic PETG")
        with self.assertRaises(ProfileError):
            f(None, "PEEK")

    def test_vendor_profile_is_transferred(self):
        name = "CR-PETG @Creality P1S"
        self.assertIn(name, self.catalog.filaments)
        flat = self.catalog.flat("filament", name)
        # Vendor material values from the K1C variant, rather than Ender-3.
        self.assertEqual(flat["nozzle_temperature"], ["250"])
        self.assertEqual(flat["textured_plate_temp"], ["75"])
        # Bambu printer-specific values remain; zero plate temperature means unsuitable and is not copied.
        self.assertEqual(flat["filament_start_gcode"], ["; bambu filament start"])
        self.assertEqual(flat["pressure_advance"], ["0.02"])
        self.assertEqual(flat["cool_plate_temp"], ["60"])
        # Cap volumetric flow at the Bambu profile's limit (vendor value: 20).
        self.assertEqual(flat["filament_max_volumetric_speed"], ["12"])
        self.assertEqual(flat["compatible_printers"], [MACHINE])
        self.assertEqual(flat["filament_settings_id"], [name])
        info = self.catalog.filament_info(name)
        self.assertEqual((info["vendor"], info["type"], info["bambu_id"]), ("Creality", "PETG", "OF3WQaKK"))
        self.assertEqual((info["nozzle_temp_min"], info["nozzle_temp_max"]), (220, 270))
        # The configured AMS slot resolves to the same profile.
        self.assertEqual(self.catalog.filament_for_tray("OF3WQaKK", "PETG"), name)

    def test_library_profiles_and_precedence(self):
        names = self.catalog.filaments
        flat = self.catalog.flat("filament", "Overture PLA @Overture P1S")
        self.assertEqual(flat["nozzle_temperature"], ["215"])  # Wrap a scalar in a list to match Bambu's shape.
        self.assertEqual(flat["filament_type"], ["PLA"])
        # SUNLU PETG already exists in BBL; do not duplicate the library variant.
        self.assertEqual([n for n in names if n.startswith("SUNLU PETG")], ["SUNLU PETG @BBL X1C"])
        # Exclude Generic profiles from vendor directories.
        self.assertFalse([n for n in names if "@Generic" in n or n.startswith("Generic PETG @Creality")])

    def test_vendor_profile_writes_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, filaments = self.catalog.write_settings(Path(tmp), process=None, filaments=["CR-PETG @Creality P1S"],
                                                       options={})
            data = json.loads(filaments[0].read_text())
        self.assertEqual(data["name"], "CR-PETG @Creality P1S")
        self.assertEqual(data["inherits"], "")

    def test_unknown_machine(self):
        with self.assertRaises(ProfileError):
            Catalog(PROFILES, "Bambu Lab X9", FILAMENT_IDS)

    def test_write_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings, filaments = self.catalog.write_settings(
                Path(tmp), process=None, filaments=["Generic PLA"],
                options={"plate": "textured_pei", "support": "tree", "infill": 25, "walls": 3, "brim": True},
                colors=["#3f8e43ff"])
            proc = json.loads(settings[1].read_text())
            self.assertEqual(proc["curr_bed_type"], "Textured PEI Plate")
            self.assertEqual(proc["enable_support"], "1")
            self.assertEqual(proc["support_type"], "tree(auto)")
            self.assertEqual(proc["sparse_infill_density"], "25%")
            self.assertEqual(proc["wall_loops"], "3")
            self.assertEqual(proc["brim_type"], "auto_brim")
            self.assertEqual(json.loads(filaments[0].read_text())["filament_type"], ["PLA"])
            self.assertEqual(json.loads(filaments[0].read_text())["filament_colour"], ["#3F8E43"])
            with self.assertRaises(ProfileError):
                self.catalog.write_settings(Path(tmp), process="0.20mm Standard @BBL A1", filaments=[], options={})
            with self.assertRaises(ProfileError):
                self.catalog.write_settings(Path(tmp), process=None, filaments=["Fremdes PLA"], options={})


class OptionTests(unittest.TestCase):
    def test_rejects_bad_values(self):
        for options in ({"plate": "glas"}, {"support": "maybe"}, {"infill": 150}, {"walls": 0}, {"infill": "viel"}):
            with self.subTest(options=options), self.assertRaises(ProfileError):
                apply_options({}, options)

    def test_support_off(self):
        proc = {"support_type": "normal(auto)"}
        apply_options(proc, {"support": "off"})
        self.assertEqual(proc["enable_support"], "0")
        self.assertEqual(proc["support_type"], "normal(auto)")


if __name__ == "__main__":
    unittest.main()
