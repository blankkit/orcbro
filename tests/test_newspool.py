"""New spools without a chip are set to Generic <previous material> in gray (bridge/newspool.py)."""
import copy
import unittest
from unittest import mock

from bridge.newspool import BARE_WAIT, CAP, GRAY, SETTLE, NewSpools, NewSpoolWatcher, generic_info, reading_slots
from bridge.printer import Printer, PrinterError
from bridge.profiles import Catalog
from bridge.spools import PLACE_GRACE
from bridge.state import merge, normalize
from helpers import FILAMENT_IDS, PROFILES, load

PRINTER = {"name": "Werkstatt", "serial": "01P00A000000000", "model": "Bambu Lab P1S", "model_id": "C12"}
# Slot 0 holds SUNLU PETG (black) in the P1S fixture; all four slots report a spool.
REMOVED = {"tray_exist_bits": "e", "tray_reading_bits": "0", "ams": [{"id": "0", "tray": [{"id": "0", "state": 0}]}]}
INSERTED = {"tray_exist_bits": "f", "tray_reading_bits": "1", "ams": [{"id": "0", "tray": [{"id": "0", "state": 0}]}]}
READ = {"tray_reading_bits": "0", "tray_read_done_bits": "f"}
# The firmware applies the slot's last setting again: SUNLU PETG, black.
OLD_AGAIN = {"ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "PETG", "tray_info_idx": "GFSNL08",
                                         "tray_color": "161616FF", "nozzle_temp_min": "230", "nozzle_temp_max": "280",
                                         "tag_uid": "0000000000000000", "tray_uuid": "0" * 32}]}]}
BAMBU = {"ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "PLA", "tray_sub_brands": "PLA Basic",
                                     "tray_info_idx": "GFA00", "tray_color": "FFFFFFFF", "tag_uid": "AABBCCDD00000000",
                                     "tray_uuid": "7D3F0A1C9E2B4C5D8E6F00112233AABB", "tray_weight": "1000"}]}]}


class Feed:
    """Printer reports merged like the bridge does, normalized for `NewSpools`."""

    def __init__(self, extra: dict | None = None):
        self.raw = copy.deepcopy(load("pushall_p1s.json")["print"])
        self.raw.pop("command", None)
        self.raw["gcode_state"] = "IDLE"
        if extra:
            merge(self.raw, copy.deepcopy(extra))
        self.state = NewSpools()
        self.connected = True

    def status(self) -> dict:
        return normalize(copy.deepcopy(self.raw), connected=self.connected, last_message_at=None, printer=dict(PRINTER))

    def step(self, now: float, update: dict | None = None, *, ams: dict | None = None, placed=None) -> list[dict]:
        if update:
            merge(self.raw, copy.deepcopy(update))
        if ams:
            merge(self.raw, {"ams": copy.deepcopy(ams)})
        return self.state.update(self.status(), now, reading=reading_slots(self.raw), placed=placed)

    def flags(self) -> list[int]:
        status = self.status()
        self.state.annotate(status)
        return [t["slot"] for u in status["ams"]["units"] for t in u["trays"] if t["new_spool"]]

    def insert(self, start: float = 10.0) -> None:
        self.step(0)
        self.step(start, ams=REMOVED)
        self.step(start + 10, ams=INSERTED)


class DetectionTests(unittest.TestCase):
    def test_insertion_waits_for_the_old_setting_and_resets_once(self):
        feed = Feed()
        self.assertEqual(feed.step(0), [])
        self.assertEqual(feed.step(10, ams=REMOVED), [])
        self.assertEqual(feed.step(20, ams=INSERTED), [])        # Bare slot, the AMS reads the spool.
        self.assertEqual(feed.step(30), [])                      # Still reading.
        self.assertEqual(feed.step(36, ams={**READ, **OLD_AGAIN}), [])  # The old setting is back.
        self.assertEqual(feed.step(36 + SETTLE - 1), [])         # Not quiet long enough yet.
        self.assertEqual(feed.state.deadline(), 36 + SETTLE)
        due = feed.step(36 + SETTLE)
        self.assertEqual([(d["slot"], d["material"]) for d in due], [(0, "PETG")])
        feed.state.sent(0, "GFG99")
        self.assertEqual(feed.flags(), [0])
        self.assertEqual(feed.step(60), [])                      # Once.
        self.assertIsNone(feed.state.deadline())

    def test_flag_follows_the_printer_until_the_filament_changes(self):
        feed = Feed()
        feed.insert()
        feed.step(25, ams={**READ, **OLD_AGAIN})
        self.assertEqual(len(feed.step(31)), 1)
        feed.state.sent(0, "GFG99")
        feed.step(32)  # The printer still shows the old setting for a moment.
        self.assertEqual(feed.flags(), [0])
        generic = {"ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "PETG", "tray_info_idx": "GFG99",
                                                 "tray_color": "8E8E93FF"}]}]}
        feed.step(33, ams=generic)
        self.assertEqual(feed.flags(), [0])
        feed.step(80, ams={"ams": [{"id": "0", "tray": [{"id": "0", "tray_info_idx": "GFL99", "tray_type": "PLA",
                                                         "tray_color": "FF0000FF"}]}]})
        self.assertEqual(feed.flags(), [])  # Set on the printer or in Bambu Studio.

    def test_flag_ends_when_the_filament_is_set_or_the_spool_is_removed(self):
        feed = Feed()
        feed.insert()
        feed.step(25, ams={**READ, **OLD_AGAIN})
        feed.step(31)
        feed.state.sent(0, "GFG99")
        feed.state.user_set(0, 40)
        self.assertEqual(feed.flags(), [])
        feed.state.sent(0, "GFG99")
        feed.step(50, ams=REMOVED)
        self.assertEqual(feed.flags(), [])

    def test_filament_set_after_the_insertion(self):
        feed = Feed()
        feed.insert()
        feed.step(25, ams={**READ, **OLD_AGAIN})
        feed.state.user_set(0, 27)  # POST /api/ams/tray or a tag read for the slot.
        self.assertEqual(feed.step(40), [])
        self.assertEqual(feed.step(20 + CAP), [])
        self.assertEqual(feed.flags(), [])

    def test_filament_set_shortly_before_the_insertion(self):
        feed = Feed()
        feed.step(0)
        feed.step(10, ams=REMOVED)
        feed.state.user_set(0, 12)  # Set for the empty slot, then the spool goes in.
        feed.step(20, ams={**INSERTED, **READ, **OLD_AGAIN})
        self.assertEqual(feed.step(30), [])

    def test_tagged_spool_placed_in_the_slot(self):
        feed = Feed()
        feed.step(0)
        feed.step(100, ams=REMOVED)
        feed.step(110, ams={**INSERTED, **READ, **OLD_AGAIN})
        # The tag was read for this slot before the spool went in, within the spool tracker's grace.
        self.assertEqual(feed.step(120, placed=lambda slot: 105 if slot == 0 else None), [])
        feed = Feed()
        feed.step(0)
        feed.step(1000, ams=REMOVED)
        feed.step(1010, ams={**INSERTED, **READ, **OLD_AGAIN})
        old_tag = lambda slot: 1010 - PLACE_GRACE - 1  # noqa: E731 - read long before, for the previous spool.
        self.assertEqual(len(feed.step(1020, placed=old_tag)), 1)

    def test_filament_set_on_the_printer_meanwhile(self):
        """The slot settles on a filament other than its last setting: set on the printer screen or in Bambu Studio."""
        feed = Feed()
        feed.insert()
        feed.step(25, ams={**READ, **OLD_AGAIN})
        feed.step(28, ams={"ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "PLA", "tray_info_idx": "GFL99",
                                                         "tray_color": "FF6A13FF"}]}]})
        self.assertEqual(feed.step(40), [])
        self.assertIsNone(feed.state.deadline())

    def test_bambu_spool(self):
        feed = Feed()
        feed.insert()
        feed.step(25, ams={**READ, **BAMBU})
        self.assertEqual(feed.step(40), [])
        self.assertEqual(feed.step(20 + CAP), [])
        self.assertEqual(feed.flags(), [])

    def test_units_without_presence_bits(self):
        feed = Feed()
        del feed.raw["ams"]["tray_exist_bits"]
        feed.step(0)
        feed.step(10, ams={"ams": [{"id": "0", "tray": [{"id": "0"}]}]})
        feed.step(20, ams=OLD_AGAIN)
        self.assertEqual(feed.step(40), [])
        self.assertEqual(feed.step(20 + CAP + 1), [])
        self.assertIsNone(feed.state.deadline())

    def test_printing_from_the_slot(self):
        """Filament ran out: the print pauses with the slot loaded, and a new spool goes in."""
        feed = Feed({"gcode_state": "PAUSE", "ams": {"tray_now": "0"}})
        feed.insert()
        feed.step(25, ams={**READ, **OLD_AGAIN})
        self.assertEqual(feed.step(40), [])
        # Another slot during the same print is reset.
        feed.step(50, ams={"tray_exist_bits": "d", "ams": [{"id": "0", "tray": [{"id": "1"}]}]})
        feed.step(60, ams={"tray_exist_bits": "f", "tray_reading_bits": "0"})
        self.assertEqual([d["slot"] for d in feed.step(60 + BARE_WAIT)], [1])

    def test_bare_slot_without_a_known_material(self):
        """The firmware applies nothing; the slot was empty since the bridge connected."""
        feed = Feed({"ams": {"tray_exist_bits": "e", "ams": [{"id": "0", "tray": [{"id": "0"}]}]}})
        feed.step(0)
        feed.step(10, ams={"tray_exist_bits": "f", "tray_reading_bits": "0"})
        self.assertEqual(feed.step(10 + BARE_WAIT - 1), [])
        self.assertEqual([(d["slot"], d["material"]) for d in feed.step(10 + BARE_WAIT)], [(0, None)])

    def test_still_reading_at_the_cap(self):
        feed = Feed()
        feed.insert()
        self.assertEqual(feed.step(20 + CAP), [])
        self.assertIsNone(feed.state.deadline())

    def test_reconnect_is_no_insertion(self):
        feed = Feed()
        feed.step(0)
        feed.step(10, ams=REMOVED)
        feed.connected = False
        feed.step(15)
        feed.connected = True
        feed.step(20, ams={**INSERTED, **READ, **OLD_AGAIN})
        self.assertEqual(feed.step(40), [])
        self.assertIsNone(feed.state.deadline())

    def test_reading_bits(self):
        self.assertEqual(reading_slots({"ams": {"tray_reading_bits": "21"}}), {0, 5})
        self.assertEqual(reading_slots({"ams": {"tray_reading_bits": "0"}}), set())
        self.assertIsNone(reading_slots({"ams": {}}))
        self.assertIsNone(reading_slots({"ams": {"tray_reading_bits": "zz"}}))


class GenericProfileTests(unittest.TestCase):
    def test_generic_of_the_material(self):
        catalog = Catalog(PROFILES, "Bambu Lab P1S 0.4 nozzle", FILAMENT_IDS)
        self.assertEqual(generic_info(catalog, "PLA")["bambu_id"], "GFL99")
        self.assertEqual(generic_info(catalog, "PETG")["name"], "Generic PETG")
        self.assertEqual(generic_info(catalog, None)["name"], "Generic PLA")
        self.assertEqual(generic_info(catalog, "PLA-S")["name"], "Generic PLA")  # No profile: Generic PLA.


class WatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.printer = Printer("192.0.2.10", "01P00A000000000", "12345678", "Test")
        self.printer.handle(load("pushall_p1s.json"))
        self.printer.handle(load("version_p1s.json"))
        self.printer.connected = True
        self.printer._publish = mock.AsyncMock()
        self.printer.command = mock.AsyncMock(return_value={"result": "success"})
        self.catalog = Catalog(PROFILES, "Bambu Lab P1S 0.4 nozzle", FILAMENT_IDS)
        self.now = 1000.0
        self.changed = 0

        async def catalog():
            return self.catalog

        def changed():
            self.changed += 1

        self.watcher = NewSpoolWatcher(self.printer, catalog, changed=changed, clock=lambda: self.now)

    def report(self, ams: dict) -> None:
        self.printer.handle({"print": {"command": "push_status", "ams": copy.deepcopy(ams)}})

    async def at(self, now: float, ams: dict | None = None) -> list[int]:
        self.now = now
        if ams:
            self.report(ams)
        return await self.watcher.process()

    async def test_generic_setting_is_sent_once_and_flagged(self):
        await self.at(1000)
        await self.at(1010, REMOVED)
        await self.at(1020, INSERTED)
        await self.at(1030, {**READ, **OLD_AGAIN})
        self.assertEqual(await self.at(1030 + SETTLE), [0])
        self.assertEqual(await self.at(1060), [])
        self.printer.command.assert_awaited_once()
        section, body = self.printer.command.call_args.args
        self.assertEqual(section, "print")
        self.assertEqual({k: body[k] for k in ("command", "ams_id", "slot_id", "tray_id", "tray_type", "tray_color",
                                                "tray_info_idx")},
                         {"command": "ams_filament_setting", "ams_id": 0, "slot_id": 0, "tray_id": 0,
                          "tray_type": "PETG", "tray_color": "8E8E93FF", "tray_info_idx": "OFYPdQJh"})
        self.assertEqual(self.changed, 1)
        status = self.printer.status()
        self.watcher.annotate(status)
        self.assertTrue(status["ams"]["units"][0]["trays"][0]["new_spool"])
        self.assertFalse(status["ams"]["units"][0]["trays"][1]["new_spool"])
        self.watcher.user_set(0)
        status = self.printer.status()
        self.watcher.annotate(status)
        self.assertFalse(status["ams"]["units"][0]["trays"][0]["new_spool"])

    async def test_already_generic_needs_no_command(self):
        generic = {"ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "PLA", "tray_info_idx": "GFL99",
                                                 "tray_color": GRAY[1:] + "FF"}]}]}
        await self.at(1000)
        await self.at(1001, generic)
        await self.at(1010, REMOVED)
        await self.at(1020, INSERTED)
        await self.at(1030, {**READ, **generic})
        self.assertEqual(await self.at(1040), [0])
        self.printer.command.assert_not_awaited()
        self.assertTrue(self.watcher.state.flagged(0))

    async def test_set_while_sending_wins(self):
        await self.at(1000)
        await self.at(1010, REMOVED)
        await self.at(1020, INSERTED)
        await self.at(1030, {**READ, **OLD_AGAIN})

        async def answer(section, body, wait=8.0):
            self.watcher.user_set(0)  # POST /api/ams/tray while the printer answers.
            return {"result": "success"}
        self.printer.command.side_effect = answer
        self.assertEqual(await self.at(1040), [])
        self.assertFalse(self.watcher.state.flagged(0))

    async def test_rejected_command_and_switched_off(self):
        self.printer.command.side_effect = PrinterError("Printer not connected.")
        await self.at(1000)
        await self.at(1010, REMOVED)
        await self.at(1020, INSERTED)
        await self.at(1030, {**READ, **OLD_AGAIN})
        with self.assertLogs("bridge.newspool", "WARNING"):
            self.assertEqual(await self.at(1040), [])
        self.assertFalse(self.watcher.state.flagged(0))
        self.watcher.enabled = False
        await self.at(1050, REMOVED)
        await self.at(1060, {**INSERTED, **READ, **OLD_AGAIN})
        self.assertEqual(await self.at(1100), [])
        self.assertIsNone(self.watcher.state.deadline())


class ApiTests(unittest.IsolatedAsyncioTestCase):
    """The bridge flags the slot in its status; setting the filament or reading a tag for it ends the flag."""

    async def asyncSetUp(self):
        from test_server import ServerTests
        self.helper = ServerTests()
        await ServerTests.asyncSetUp(self.helper)
        self.client, self.auth, self.printer = self.helper.client, self.helper.auth, self.helper.printer
        from bridge.server import DEVICES
        self.watcher = self.client.server.app[DEVICES].default().new_spools

    async def asyncTearDown(self):
        await ServerTests_tearDown(self.helper)

    async def new_spool(self) -> list[bool]:
        status = await (await self.client.get("/api/status", headers=self.auth)).json()
        return [tray["new_spool"] for tray in status["ams"]["units"][0]["trays"]]

    async def test_flag_in_status_until_the_filament_is_set(self):
        self.assertEqual(await self.new_spool(), [False] * 4)
        self.watcher.state.sent(0, "GFG99")
        self.watcher.state.sent(1, "GFL99")
        self.assertEqual(await self.new_spool(), [True, True, False, False])
        with mock.patch.object(self.printer, "command", mock.AsyncMock(return_value={"result": "success"})):
            response = await self.client.post("/api/ams/tray", headers=self.auth,
                                              json={"slot": 0, "filament": "Generic PETG", "color": "#202020"})
        self.assertEqual(response.status, 200)
        self.assertEqual(await self.new_spool(), [False, True, False, False])
        tag = {"slot": 1, "uid": "04A1B2C3", "weight": 1000, "type": "PLA", "color": "#FFFFFF"}
        self.assertEqual((await self.client.post("/api/ams/spool", json=tag, headers=self.auth)).status, 200)
        self.assertEqual(await self.new_spool(), [False] * 4)

    def test_setting(self):
        from bridge.config import Config, ConfigError
        self.assertTrue(Config.from_env({}).reset_new_spools)
        self.assertFalse(Config.from_env({"BRIDGE_RESET_NEW_SPOOLS": "off"}).reset_new_spools)
        with self.assertRaises(ConfigError):
            Config.from_env({"BRIDGE_RESET_NEW_SPOOLS": "maybe"})


async def ServerTests_tearDown(helper) -> None:
    from test_server import ServerTests
    await ServerTests.asyncTearDown(helper)


if __name__ == "__main__":
    unittest.main()
