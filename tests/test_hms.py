import gzip
import json
import tempfile
import unittest
from pathlib import Path

from bridge.hms import Texts
from bridge.i18n import render, language_context


def write(directory: Path, language: str, table: dict) -> None:
    with gzip.open(directory / f"hms_{language}.json.gz", "wt", encoding="utf-8") as handle:
        json.dump(table, handle)


class TextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        write(self.dir, "de", {
            "device_error": {"0300400C": {"Die Aufgabe wurde  abgebrochen.": []},
                             "05024007": {"": []},
                             "0500C011": {"": [], "SD-Karte langsam.": ["P1P", "P1S"]},
                             "07008011": {"Nur H2D": ["H2D"], "Filament leer.": [], "Filament am P1S leer.": ["P1S"]}},
            "device_hms": {"0500050000010007": {"MQTT-Befehl abgewiesen.": []},
                           "0700200000020001": {"AMS A Slot 1 leer.": []}},
        })
        write(self.dir, "en", {
            "device_error": {"03008001": {"Paused by the user.": []},
                             "0300400C": {"The job was  canceled.": []},
                             "05024007": {"": []},
                             "0500C011": {"": [], "SD card is slow.": ["P1P", "P1S"]},
                             "07008011": {"H2D only": ["H2D"], "Out of filament.": [],
                                          "P1S out of filament.": ["P1S"]}},
            "device_hms": {"0500050000010007": {"MQTT command rejected.": []},
                           "0700200000020001": {"AMS A Slot 1 empty.": []}},
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_lookup(self):
        texts = Texts(self.dir)
        self.assertTrue(texts.available)
        self.assertEqual(texts.error("0300_400C", "Bambu Lab P1S"), "The job was canceled.")
        self.assertEqual(texts.error("07008011", "Bambu Lab P1S"), "P1S out of filament.")  # Model-specific text.
        self.assertEqual(texts.error("07008011", "Bambu Lab A1"), "Out of filament.")
        self.assertEqual(texts.error("03008001", "Bambu Lab P1S"), "Paused by the user.")  # English fallback when German is missing.
        self.assertIsNone(texts.error("0BAD0000", "Bambu Lab P1S"))  # Unknown.
        self.assertEqual(texts.error("0502_4007", "Bambu Lab P1S"), "")  # Internal: empty text.
        self.assertEqual(texts.error("0500_C011", "Bambu Lab P1S"), "SD card is slow.")  # Model-specific text takes precedence over empty general text.
        self.assertEqual(texts.error("0500_C011", "Bambu Lab A1"), "")
        self.assertEqual(texts.hms("0500_0500_0001_0007", None), "MQTT command rejected.")

    def test_describe_and_missing_tables(self):
        status = {"printer": {"model": "Bambu Lab P1S"}, "job": {"error": {"code": "0300_400C", "value": 1}},
                  "hms": [{"code": "0700_2000_0002_0001"}, {"code": "0500_0500_0001_0007"}]}
        Texts(self.dir).describe(status)
        self.assertEqual(status["job"]["error"]["text"], "The job was canceled.")
        self.assertFalse(status["job"]["error"]["internal"])
        self.assertEqual(status["hms"][0]["text"], "AMS A Slot 1 empty.")
        internal = {"printer": {"model": "Bambu Lab P1S"}, "job": {"error": {"code": "0502_4007", "value": 1}}, "hms": []}
        Texts(self.dir).describe(internal)
        self.assertEqual((internal["job"]["error"]["text"], internal["job"]["error"]["internal"]), (None, True))
        self.assertIn("Developer Mode", status["hms"][1]["text"])  # Bridge-specific text instead of the Studio update suggestion.
        empty = Texts(self.dir / "fehlt")
        self.assertFalse(empty.available)
        self.assertIsNone(empty.error("0300_400C", "Bambu Lab P1S"))

    def test_german_and_english_fallback(self):
        texts = Texts(self.dir)
        with language_context("de"):
            self.assertEqual(texts.error("0300_400C", "Bambu Lab P1S"), "Die Aufgabe wurde abgebrochen.")
            self.assertEqual(texts.error("03008001", "Bambu Lab P1S"), "Paused by the user.")
        status = {"printer": {"model": "Bambu Lab P1S"}, "job": {"error": {"code": "0300_400C"}}, "hms": []}
        texts.describe(status)
        self.assertEqual(render(status, "de")["job"]["error"]["text"], "Die Aufgabe wurde abgebrochen.")
        self.assertEqual(render(status, "en")["job"]["error"]["text"], "The job was canceled.")

    def test_german_table_without_english_entry(self):
        write(self.dir, "en", {"device_error": {}, "device_hms": {}})
        status = {"printer": {"model": "Bambu Lab P1S"}, "job": {"error": {"code": "0300_400C"}}, "hms": []}
        Texts(self.dir).describe(status)
        self.assertIsNone(render(status, "en")["job"]["error"]["text"])
        self.assertFalse(render(status, "de")["job"]["error"]["internal"])
        self.assertEqual(render(status, "de")["job"]["error"]["text"], "Die Aufgabe wurde abgebrochen.")
        from bridge.live import content_state
        status["job"].update({"state": "FAILED", "file": "part.3mf"})
        state = content_state(status)
        self.assertEqual(render(state, "en")["error"], "Error code 0300_400C")
        self.assertEqual(render(state, "de")["error"], "Die Aufgabe wurde abgebrochen.")
