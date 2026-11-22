"""Filament library: tagged and Bambu spools, remaining amounts (bridge/spools.py)."""
import json
import tempfile
import time
import unittest
from pathlib import Path

from bridge.spools import SpoolStore, SpoolTracker, allocate, length_m

UID = "04A1B2C3D4E5F6"
PLA_WHITE = {"type": "PLA", "color": "#FFFFFFFF", "brand": "Bambu Lab", "subtype": "Basic"}


def tray(slot, material="PLA", color="#FFFFFFFF", empty=False, rfid=False, **extra):
    return {"slot": slot, "material": None if empty else material, "color": color, "empty": empty, "rfid": rfid,
            "name": material, **extra}


def status(state="RUNNING", active=0, trays=None, progress=50, name="Box"):
    return {"connection": {"connected": True},
            "job": {"state": state, "name": name, "file": "box.gcode.3mf", "progress": progress},
            "ams": {"units": [{"trays": trays if trays is not None else [tray(0), tray(1, color="#000000FF")]}],
                    "active_slot": active, "externals": []}}


class AllocateTests(unittest.TestCase):
    def test_known_mapping(self):
        filaments = [{"id": 1, "type": "PLA", "color": "#FFFFFF", "used_g": 20.0},
                     {"id": 2, "type": "PLA", "color": "#000000", "used_g": 5.0}]
        self.assertEqual(allocate(filaments, {}, {}, {1: 3, 2: 0}), {3: 20.0, 0: 5.0})
        self.assertEqual(allocate(filaments, {}, {}, {1: 3, 2: 0}, fraction=0.5), {3: 10.0, 0: 2.5})

    def test_external_print_matches_material_and_color(self):
        trays = {0: tray(0, "PETG", "#FF0000FF"), 1: tray(1, "PLA", "#FFFFFFFF"), 2: tray(2, "PLA", "#101010FF")}
        filaments = [{"id": 1, "type": "PLA", "color": "#000000", "used_g": 30.0},
                     {"id": 2, "type": "PLA", "color": "#F8F8F8", "used_g": 10.0}]
        self.assertEqual(allocate(filaments, {1: 100, 2: 300}, trays), {2: 30.0, 1: 10.0})

    def test_ams_backup_splits_by_active_time(self):
        trays = {0: tray(0, "PLA", "#FFFFFFFF"), 1: tray(1, "PLA", "#FEFEFEFF")}
        filaments = [{"id": 1, "type": "PLA", "color": "#FFFFFF", "used_g": 90.0}]
        self.assertEqual(allocate(filaments, {0: 200, 1: 100}, trays), {0: 60.0, 1: 30.0})

    def test_nothing_seen_books_nothing(self):
        self.assertEqual(allocate([{"id": 1, "type": "PLA", "color": "#FFFFFF", "used_g": 9.0}], {}, {}), {})

    def test_length(self):
        self.assertAlmostEqual(length_m(1000, "PLA"), 335.3, delta=0.5)
        self.assertIsNone(length_m(None, "PLA"))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SpoolStore(Path(self.tmp.name) / "spools.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_keeps_the_bridge_amount_for_known_spools(self):
        spool, outcome = self.store.load("P1", 0, "04:a1:b2:c3:d4:e5:f6", weight=1000, info=PLA_WHITE)
        self.assertEqual((spool["id"], spool["remaining"], spool["slot"], outcome["decision"]), (UID, 1000.0, 0, "new"))
        self.store.consume("P1", {0: 120.5})
        again, outcome = self.store.load("P1", 2, UID, weight=1000, remaining=1000, info=PLA_WHITE)
        self.assertEqual((again["remaining"], again["slot"], outcome["decision"]), (879.5, 2, "loaded"))
        self.assertEqual((again["prints"], again["used"]), (1, 120.5))
        self.assertIsNone(self.store.at("P1", 0))
        reloaded = SpoolStore(self.store.path)
        self.assertEqual(reloaded.at("P1", 2)["remaining"], 879.5)
        self.assertEqual(oct(self.store.path.stat().st_mode & 0o777), "0o600")

    def test_new_spool_takes_the_amount_from_the_tag_and_replaces_the_slot(self):
        self.assertEqual(self.store.load("P1", 1, "AABBCCDD", weight=1000, remaining=640)[0]["remaining"], 640.0)
        self.store.load("P1", 1, "11223344", weight=250)
        self.assertEqual(self.store.at("P1", 1)["id"], "11223344")
        self.assertIsNone(self.store.get("AABBCCDD")["slot"])

    def test_deleted_spool_scanned_again(self):
        self.store.load("P1", 0, UID, weight=1000, info=PLA_WHITE)
        self.store.delete(UID)
        self.assertIsNone(self.store.get(UID))
        self.assertEqual(self.store.library(), [])
        # Same filament with plenty left: probably deleted by mistake → suggest restoring.
        spool, outcome = self.store.load("P1", 0, UID, weight=1000, info=PLA_WHITE)
        self.assertIsNone(spool)
        self.assertEqual((outcome["decision"], outcome["suggest"]), ("choose", "restore"))
        spool, outcome = self.store.load("P1", 0, UID, weight=1000, info=PLA_WHITE, choice="restore")
        self.assertEqual((outcome["decision"], spool["remaining"]), ("restored", 1000.0))
        # Nearly empty and deleted: the tag probably moved to a fresh spool → suggest new.
        self.store.consume("P1", {0: 990})
        self.store.delete(UID)
        self.assertEqual(self.store.load("P1", 0, UID, weight=1000, info=PLA_WHITE)[1]["suggest"], "new")
        spool, outcome = self.store.load("P1", 0, UID, weight=1000, info=PLA_WHITE, choice="new")
        self.assertEqual((outcome["decision"], spool["remaining"], spool["prints"]), ("new", 1000.0, 0))
        # Other filament on the tag: rewritten for another spool → new without asking.
        self.store.delete(UID)
        spool, outcome = self.store.load("P1", 0, UID, weight=250, info={"type": "PETG", "color": "#FF0000"})
        self.assertEqual((outcome["decision"], spool["remaining"], spool["type"]), ("new", 250.0, "PETG"))

    def test_writing_a_tag(self):
        self.store.load("P1", 0, UID, weight=1000, info=PLA_WHITE)
        self.store.consume("P1", {0: 300})
        spool, outcome = self.store.written("P1", 0, UID, weight=1000, info=PLA_WHITE)
        self.assertEqual((outcome["decision"], spool["remaining"]), ("updated", 700.0))
        spool, outcome = self.store.written("P1", 0, UID, weight=250, info={"type": "PETG", "color": "#000000"})
        self.assertEqual((outcome["decision"], spool["remaining"], spool["used"]), ("new", 250.0, 0.0))

    def test_bambu_spools_come_from_the_ams(self):
        ams = tray(2, "PLA", "#FF0000FF", rfid=True, spool_uuid="ABC123", spool_weight=1000, remain=40,
                   nozzle_temp_min=190, nozzle_temp_max=230)
        self.assertTrue(self.store.sync_bambu("P1", ams))
        self.assertFalse(self.store.sync_bambu("P1", ams))
        spool = self.store.get("bambu-ABC123")
        self.assertEqual((spool["kind"], spool["remaining"], spool["slot"]), ("bambu", 400.0, 2))
        public = SpoolStore.public(spool)
        self.assertEqual((public["remaining_percent"], public["brand"]), (40, "Bambu Lab"))
        with self.assertRaises(ValueError):
            self.store.update("bambu-ABC123", remaining=10)
        self.store.consume("P1", {2: 50})
        self.assertEqual((spool["remaining"], spool["used"]), (400.0, 50.0))  # the AMS keeps the amount
        self.store.delete("bambu-ABC123")
        self.store.sync_bambu("P1", ams)  # seen again → back
        self.assertIsNotNone(self.store.get("bambu-ABC123"))

    def test_validation_update_and_tombstone_expiry(self):
        with self.assertRaises(ValueError):
            self.store.load("P1", 0, "xyz!", weight=1000)
        with self.assertRaises(ValueError):
            self.store.load("P1", 0, UID, weight=-5)
        self.store.load("P1", 0, UID, weight=250)
        self.store.consume("P1", {0: 400})
        self.assertEqual(self.store.at("P1", 0)["remaining"], 0.0)
        spool = self.store.update(UID, remaining=180, note=" Rest  ", unload=True)
        self.assertEqual((spool["remaining"], spool["note"], spool["slot"]), (180.0, "Rest", None))
        self.store.delete(UID)
        self.store.spools[UID]["deleted"] = time.time() - 200 * 86400
        self.store.save()
        self.assertNotIn(UID, SpoolStore(self.store.path).spools)


class TrackerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SpoolStore(Path(self.tmp.name) / "spools.json")
        self.store.load("P1", 0, UID, weight=1000, info=PLA_WHITE)
        self.plans = 0
        self.notified = 0

        async def plan():
            self.plans += 1
            return [{"id": 1, "type": "PLA", "color": "#FFFFFF", "used_g": 40.0}], None

        def changed():
            self.notified += 1
        self.tracker = SpoolTracker(self.store, "P1", plan, changed)

    def tearDown(self):
        self.tmp.cleanup()

    async def test_finished_print_is_booked_once(self):
        await self.tracker.process(status("PREPARE"), now=1000)
        for t in range(1010, 1100, 10):
            await self.tracker.process(status("RUNNING", active=0), now=t)
        changed = await self.tracker.process(status("FINISH", progress=100), now=1100)
        self.assertEqual(changed[0]["remaining"], 960.0)
        self.assertEqual((self.plans, self.notified), (1, 1))
        self.assertEqual(await self.tracker.process(status("FINISH", progress=100), now=1110), [])
        self.assertEqual(self.store.printing, {})

    async def test_canceled_print_counts_its_progress_and_survives_a_restart(self):
        await self.tracker.process(status("RUNNING", active=0), now=1000)
        await self.tracker.process(status("RUNNING", active=0), now=1060)
        restarted = SpoolTracker(SpoolStore(self.store.path), "P1", self.tracker.plan)
        await restarted.process(status("RUNNING", active=0), now=1100)
        await restarted.process(status("RUNNING", active=0), now=1160)
        changed = await restarted.process(status("FAILED", progress=25), now=1200)
        self.assertEqual(changed[0]["remaining"], 990.0)
        self.assertEqual(json.loads(self.store.path.read_text())["spools"][UID]["prints"], 1)

    async def test_empty_slot_unloads_and_bambu_spools_appear(self):
        trays = [tray(0, empty=True), tray(1, rfid=True, spool_uuid="FEED01", spool_weight=1000, remain=75)]
        await self.tracker.process(status("IDLE", trays=trays), now=1000)
        self.assertIsNone(self.store.at("P1", 0))
        self.assertEqual(self.store.at("P1", 1)["id"], "bambu-FEED01")
        self.assertEqual(self.notified, 1)


if __name__ == "__main__":
    unittest.main()


class SpoolApiTests(unittest.IsolatedAsyncioTestCase):
    """Endpoints of the filament library on a server with a fake printer."""

    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient
        from test_server import ServerTests
        self.helper = ServerTests()
        await ServerTests.asyncSetUp(self.helper)
        self.client: TestClient = self.helper.client
        self.auth = self.helper.auth

    async def asyncTearDown(self):
        await ServerTests_tearDown(self.helper)

    async def test_load_list_update_delete(self):
        status = await (await self.client.get("/api/status", headers=self.auth)).json()
        slot = next(t["slot"] for u in status["ams"]["units"] for t in u["trays"])
        body = {"slot": slot, "uid": "04A1B2C3", "weight": 1000, "remaining": 800, "type": "PLA", "color": "#FFFFFF"}
        reply = await (await self.client.post("/api/ams/spool", json=body, headers=self.auth)).json()
        self.assertEqual((reply["decision"], reply["spool"]["remaining"]), ("new", 800.0))
        status = await (await self.client.get("/api/status", headers=self.auth)).json()
        loaded = next(t for u in status["ams"]["units"] for t in u["trays"] if t["slot"] == slot)
        self.assertEqual(loaded["spool"]["id"], "04A1B2C3")
        listing = await (await self.client.get("/api/spools", headers=self.auth)).json()
        self.assertEqual([s["id"] for s in listing["spools"]], ["04A1B2C3"])
        patched = await self.client.patch("/api/spools/04:a1:b2:c3", json={"remaining": 500}, headers=self.auth)
        self.assertEqual((await patched.json())["spool"]["remaining"], 500.0)
        self.assertEqual((await self.client.delete("/api/spools/04A1B2C3", headers=self.auth)).status, 204)
        again = await (await self.client.post("/api/ams/spool", json=body, headers=self.auth)).json()
        self.assertEqual((again["decision"], again["suggest"], again["spool"]), ("choose", "restore", None))
        self.assertEqual(again["deleted"]["remaining"], 500.0)
        bad = await self.client.post("/api/ams/spool", json={**body, "uid": "nope!"}, headers=self.auth)
        self.assertEqual(bad.status, 422)
        german = await self.client.delete("/api/spools/FFFF0000", headers={**self.auth, "Accept-Language": "de"})
        self.assertEqual((await german.json())["error"], "Spule nicht gefunden.")


async def ServerTests_tearDown(helper):
    from test_server import ServerTests
    await ServerTests.asyncTearDown(helper)
