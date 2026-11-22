"""Print path regressions: printer/nozzle of sliced files, start confirmation after a failed print, matching the
running print to a job, preview and thumbnail caching, spool plan backoff and SD card transfer checks."""
import asyncio
import io
import json
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from bridge import ftps, preview
from bridge.config import Config
from bridge.jobs import Job, JobError, sliced_for
from bridge.printer import Printer
from bridge.server import (DEVICES, JOBS, PLAN_ATTEMPTS, _card_unchanged, _prune_print_cache, _same_card_time,
                           app_token, create_app, iso_modified, spool_plan)
from helpers import FILAMENT_IDS, MODELS, PROFILES, load, sliced_3mf

SECRET = "s3cret-bridge-access-token"
STOPPED = 0x0300400C  # "The task was canceled", left over after stopping a print.
FILE = object()  # Placeholder for the uploaded file name in `StartAfterFailedPrintTests.replies_first`.


def variant(thumbnail: bytes = b"\x89PNG fake", object_id: int = 1, project: dict | None = None,
            plate_meta: dict | None = None) -> bytes:
    """`sliced_3mf()` with its own thumbnail and object ID, project settings and extra plate metadata."""
    source = zipfile.ZipFile(io.BytesIO(sliced_3mf()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for info in source.infolist():
            data = source.read(info)
            if info.filename == "Metadata/plate_1.png":
                data = thumbnail
            elif info.filename == "Metadata/slice_info.config":
                extra = "".join(f'<metadata key="{k}" value="{v}"/>' for k, v in (plate_meta or {}).items())
                data = (data.decode().replace('identify_id="1"', f'identify_id="{object_id}"')
                        .replace('<metadata key="index" value="1"/>', '<metadata key="index" value="1"/>' + extra)
                        .encode())
            archive.writestr(info.filename, data)
        if project is not None:
            archive.writestr("Metadata/project_settings.config", json.dumps(project))
    return out.getvalue()


def settings(printer: str = "Bambu Lab P1S", nozzle: str = "0.4") -> dict:
    return {"printer_model": printer, "nozzle_diameter": [nozzle]}


class PrintTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        config = Config(host="192.0.2.10", serial="01P00A000000000", access_code="12345678", bridge_token=SECRET,
                        data_dir=Path(self.tmp.name), profiles_dir=PROFILES, filament_ids=FILAMENT_IDS,
                        models_dir=MODELS, orca_cmd=(sys.executable, str(Path(__file__).parent / "fake_orca.py")))
        self.printer = Printer(config.host, config.serial, config.access_code, "Test")
        self.printer.handle(load("pushall_p1s.json"))
        self.printer.handle(load("version_p1s.json"))
        self.printer.connected = True
        self.printer._publish = mock.AsyncMock()
        self.app = create_app(config, printer=self.printer, start_background=False)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.auth = {"Authorization": f"Bearer {app_token(SECRET)}"}
        self.dev = self.app[DEVICES].default()
        self.card: dict[str, bytes] = {}  # SD card root as seen by the FTPS fakes.
        self.times: dict[str, str] = {}

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    def status(self, **values):
        self.printer.handle({"print": {"command": "push_status", **values}})

    async def upload(self, name: str, data: bytes) -> dict:
        form = FormData()
        form.add_field("file", data, filename=name, content_type="application/octet-stream")
        response = await self.client.post("/api/jobs", data=form, headers=self.auth)
        self.assertEqual(response.status, 201, await response.text())
        return await response.json()

    async def wait_state(self, job_id: str, *states: str) -> dict:
        for _ in range(200):
            job = await (await self.client.get(f"/api/jobs/{job_id}", headers=self.auth)).json()
            if job["state"] in states:
                return job
            await asyncio.sleep(0.05)
        self.fail(f"job stayed {job['state']}")

    def ftps(self, downloads: list | None = None):
        """Patch FTPS: listing, upload and download work on `self.card`."""
        def list_dir(host, code, directory="/", **_kw):
            if directory != "/":
                return []
            return [{"name": n, "size": len(d), "is_dir": False, "modified": self.times.get(n, "Nov 20 14:48")}
                    for n, d in self.card.items()]

        def upload(host, code, local, remote, progress=None, **_kw):
            self.card[remote] = local.read_bytes()

        def download(host, code, remote, local, progress=None, limit=None, **_kw):
            if downloads is not None:
                downloads.append(remote)
            data = self.card[remote.lstrip("/")]
            if limit is not None and len(data) > limit:
                raise ftps.TooLarge(limit)
            Path(local).write_bytes(data)
        return mock.patch.multiple("bridge.server.ftps", list_dir=list_dir, upload=upload, download=download)

    def starts(self):
        """Fake print command: the printer reports PREPARE with the submitted file."""
        async def command(section, body, wait=15):
            if body.get("command") == "project_file":
                self.status(gcode_state="PREPARE", print_error=0, gcode_file=body["url"].rsplit("/", 1)[-1],
                            subtask_name=body["subtask_name"])
            return {"result": "success"}
        return mock.patch.object(self.printer, "command", mock.AsyncMock(side_effect=command))

    async def print_job(self, job_id: str, mapping: dict | None = None, expect: str = "sent") -> dict:
        response = await self.client.post(f"/api/jobs/{job_id}/print", json={"mapping": mapping or {"1": 2}},
                                          headers=self.auth)
        self.assertEqual(response.status, 202, await response.text())
        done = await self.wait_state(job_id, "sent", "failed")
        self.assertEqual(done["state"], expect, done.get("error"))
        return done


class SlicedForOtherPrinterTests(PrintTestCase):
    """PAR-01: a pre-sliced file for another model or nozzle is refused before anything is sent."""

    async def refused(self, data: bytes) -> str:
        job = await self.upload("fertig.gcode.3mf", data)
        with self.ftps(), mock.patch.object(self.printer, "command", mock.AsyncMock()) as cmd:
            response = await self.client.post(f"/api/jobs/{job['id']}/print", json={"mapping": {"1": 2}},
                                              headers=self.auth)
        self.assertEqual(response.status, 409, await response.text())
        cmd.assert_not_called()
        return (await response.json())["error"]

    async def test_other_model_from_project_settings(self):
        error = await self.refused(variant(project=settings("Bambu Lab A1 mini")))
        self.assertIn("sliced for Bambu Lab A1 mini, but this printer is a Bambu Lab P1S", error)

    async def test_other_nozzle_from_project_settings(self):
        error = await self.refused(variant(project=settings(nozzle="0.6")))
        self.assertIn("sliced for a 0.6 mm nozzle, but a 0.4 mm nozzle is installed", error)

    async def test_slice_info_is_the_fallback(self):
        error = await self.refused(variant(plate_meta={"printer_model_id": "N2S", "nozzle_diameters": "0.4"}))
        self.assertIn("sliced for Bambu Lab A1,", error)
        error = await self.refused(variant(plate_meta={"printer_model_id": "C12", "nozzle_diameters": "0.2"}))
        self.assertIn("0.2 mm nozzle", error)

    async def test_unknown_model_is_not_this_printer(self):
        error = await self.refused(variant(project=settings("Prusa MK4")))
        self.assertIn("sliced for Prusa MK4", error)

    async def test_refusal_is_translated(self):
        job = await self.upload("fertig.gcode.3mf", variant(project=settings(nozzle="0.6")))
        response = await self.client.post(f"/api/jobs/{job['id']}/print", json={"mapping": {"1": 2}},
                                          headers={**self.auth, "Accept-Language": "de"})
        self.assertEqual((await response.json())["error"],
                         "Die Datei ist für eine 0.6-mm-Düse geslict, eingesetzt ist 0.4 mm.")

    async def test_matching_file_prints(self):
        job = await self.upload("fertig.gcode.3mf", variant(
            project=settings(), plate_meta={"printer_model_id": "C12", "nozzle_diameters": "0.4"}))
        with self.ftps(), self.starts():
            await self.print_job(job["id"])

    async def test_sd_import_and_reprint_are_checked(self):
        self.card["a1mini.gcode.3mf"] = variant(project=settings("Bambu Lab A1 mini"))
        with self.ftps():
            response = await self.client.post("/api/sdcard/import", json={"path": "/a1mini.gcode.3mf"},
                                              headers=self.auth)
        imported = await response.json()
        self.assertEqual(response.status, 201, imported)
        response = await self.client.post(f"/api/jobs/{imported['id']}/print", json={"mapping": {"1": 2}},
                                          headers=self.auth)
        self.assertEqual(response.status, 409)
        self.assertIn("A1 mini", (await response.json())["error"])

        # Printed once with a 0.4 mm nozzle, then the nozzle was swapped: printing it again is refused.
        job = await self.upload("teil.gcode.3mf", variant(project=settings()))
        with self.ftps(), self.starts():
            await self.print_job(job["id"])
        self.status(gcode_state="FINISH", nozzle_diameter="0.6")
        response = await self.client.post(f"/api/jobs/{job['id']}/print", json={"mapping": {"1": 2}},
                                          headers=self.auth)
        self.assertEqual(response.status, 409)
        self.assertIn("0.4 mm nozzle, but a 0.6 mm", (await response.json())["error"])

    def test_sliced_for_reads_project_settings_then_slice_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.gcode.3mf"
            path.write_bytes(variant(project=settings("Bambu Lab P1S", "0.6"),
                                     plate_meta={"printer_model_id": "N1", "nozzle_diameters": "0.2,0.4"}))
            self.assertEqual(sliced_for(path, 1), {"model": "Bambu Lab P1S", "model_id": "N1", "nozzle": 0.6})
            path.write_bytes(variant(plate_meta={"printer_model_id": "N1", "nozzle_diameters": "0.2,0.4"}))
            self.assertEqual(sliced_for(path, 1), {"model": None, "model_id": "N1", "nozzle": 0.2})
            self.assertEqual(sliced_for(path, 2), {"model": None, "model_id": None, "nozzle": None})
            path.write_bytes(sliced_3mf())
            self.assertEqual(sliced_for(path), {"model": None, "model_id": None, "nozzle": None})


class StartAfterFailedPrintTests(PrintTestCase):
    """QO-4/PAR-05: the previous print's FAILED state and error do not fail the next start."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.status(gcode_state="FAILED", print_error=STOPPED, gcode_file="old_123456.gcode.3mf")

    def replies_first(self, *updates):
        """The reply to `project_file` arrives first; status updates follow 0.2 s apart, as on the printer."""
        async def command(section, body, wait=15):
            if body.get("command") == "project_file":
                remote = body["url"].rsplit("/", 1)[-1]
                loop = asyncio.get_running_loop()
                for index, update in enumerate(updates, 1):
                    values = {k: (remote if v is FILE else v) for k, v in update.items()}
                    loop.call_later(0.2 * index, lambda v=values: self.status(**v))
            return {"result": "success"}
        return mock.patch.object(self.printer, "command", mock.AsyncMock(side_effect=command))

    async def test_stale_error_is_not_a_rejection(self):
        job = await self.upload("a.gcode.3mf", sliced_3mf())
        with self.ftps(), self.replies_first({"gcode_state": "FAILED", "print_error": STOPPED},
                                             {"gcode_state": "PREPARE", "print_error": 0, "gcode_file": FILE}):
            await self.print_job(job["id"])

    async def test_new_error_after_the_command_fails_the_start(self):
        job = await self.upload("a.gcode.3mf", sliced_3mf())
        with self.ftps(), self.replies_first({"gcode_state": "FAILED", "print_error": 0x0500C010}):
            done = await self.print_job(job["id"], expect="failed")
        self.assertIn("0500C010", done["error"])

    async def test_failed_again_after_leaving_failed(self):
        job = await self.upload("a.gcode.3mf", sliced_3mf())
        with self.ftps(), self.replies_first({"gcode_state": "IDLE", "print_error": 0},
                                             {"gcode_state": "FAILED", "print_error": STOPPED}):
            done = await self.print_job(job["id"], expect="failed")
        self.assertIn("0300400C", done["error"])

    async def test_error_from_idle_still_fails(self):
        self.status(gcode_state="IDLE", print_error=0)
        job = await self.upload("a.gcode.3mf", sliced_3mf())
        with self.ftps(), self.replies_first({"gcode_state": "FAILED", "print_error": STOPPED}):
            done = await self.print_job(job["id"], expect="failed")
        self.assertIn("0300400C", done["error"])


class CurrentPrintMatchingTests(PrintTestCase):
    """QO-3/PAR-04: thumbnail, objects, preview and spool plan come from the job that is actually printing."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.first = await self.upload("benchy.gcode.3mf", variant(b"\x89PNG first", object_id=7))
        self.second = await self.upload("benchy.gcode.3mf", variant(b"\x89PNG second", object_id=9))
        self.assertEqual(self.first["name"], self.second["name"])
        self.ftps_patch = self.ftps()
        self.ftps_patch.start()
        self.addCleanup(self.ftps_patch.stop)
        with self.starts():
            await self.print_job(self.first["id"], {"1": 2})
            self.status(gcode_state="FINISH")
            await self.print_job(self.second["id"], {"1": 3})
            self.status(gcode_state="FINISH")
            # Print the older job again: it is now the newest print, the other one the newest job.
            await self.print_job(self.first["id"], {"1": 2})

    def remote(self, job: dict) -> str:
        return self.app[JOBS].get(job["id"]).sent["remote_name"]

    async def current(self) -> tuple[bytes | None, list[int] | None, dict | None]:
        thumb = await self.client.get("/api/print/thumbnail", headers=self.auth)
        objects = await self.client.get("/api/print/objects", headers=self.auth)
        try:
            mapping = (await spool_plan(self.app, self.dev))[1]
        except JobError:
            mapping = None
        return ((await thumb.read()) if thumb.status == 200 else None,
                [o["id"] for o in (await objects.json())["objects"]] if objects.status == 200 else None, mapping)

    async def test_print_started_by_the_bridge(self):
        # The printer reports only the unpacked plate path, as H2D and X1 do while printing.
        self.status(gcode_state="RUNNING", gcode_file="/data/Metadata/plate_1.gcode")
        self.assertEqual(await self.current(), (b"\x89PNG first", [7], {1: 2}))
        preview_data = await self.client.get("/api/print/preview", headers=self.auth)
        self.assertEqual(preview_data.status, 200)
        self.assertTrue((Path(self.tmp.name) / "jobs" / self.first["id"] / "preview-1.bin").exists())

    async def test_card_file_names_the_job(self):
        self.dev.started_job, self.dev.start_seen = None, False  # As after a bridge restart.
        self.status(gcode_state="RUNNING", gcode_file=f"/sdcard/{self.remote(self.second)}")
        self.assertEqual(await self.current(), (b"\x89PNG second", [9], {1: 3}))

    async def test_same_name_without_identity_is_no_match(self):
        self.dev.started_job, self.dev.start_seen = None, False
        self.status(gcode_state="RUNNING", gcode_file="/data/Metadata/plate_1.gcode")
        # Neither job: the bridge downloads `benchy.gcode.3mf`, which is not on the card.
        self.assertEqual(await self.current(), (None, None, None))

    async def test_unambiguous_name_after_restart(self):
        self.app[JOBS].delete(self.second["id"])
        self.dev.started_job, self.dev.start_seen = None, False
        self.status(gcode_state="RUNNING", gcode_file="/data/Metadata/plate_1.gcode")
        self.assertEqual(await self.current(), (b"\x89PNG first", [7], {1: 2}))

    async def test_print_from_elsewhere_with_the_same_name(self):
        self.status(gcode_state="FINISH")
        self.card["benchy.gcode.3mf"] = variant(b"\x89PNG orca", object_id=3)
        for file in ("benchy.gcode.3mf", "/data/Metadata/plate_1.gcode"):
            with self.subTest(file=file):
                self.status(gcode_state="RUNNING", gcode_file=file)
                self.assertEqual(await self.current(), (b"\x89PNG orca", [3], None))
                self.dev.plan_retry = None

    async def test_resliced_job_shows_what_the_card_holds(self):
        self.status(gcode_state="RUNNING", gcode_file="/data/Metadata/plate_1.gcode")
        output = self.app[JOBS].output_path(self.app[JOBS].get(self.first["id"]))
        output.write_bytes(variant(b"\x89PNG new slice", object_id=8))  # Sliced again while printing.
        downloads = []
        with self.ftps(downloads):
            self.assertEqual(await self.current(), (b"\x89PNG first", [7], {1: 2}))
        self.assertIn(f"/{self.remote(self.first)}", downloads)


class PreviewAndThumbnailCacheTests(PrintTestCase):
    """QO-8 and QO-22."""

    async def test_reslicing_replaces_the_print_preview(self):
        job = await self.upload("cube.stl", b"solid cube\nendsolid cube\n")
        await self.client.post(f"/api/jobs/{job['id']}/slice", json={"slot": 2}, headers=self.auth)
        await self.wait_state(job["id"], "sliced", "failed")
        directory = Path(self.tmp.name) / "jobs" / job["id"]
        with self.ftps(), self.starts():
            await self.print_job(job["id"], {})
            self.status(gcode_state="RUNNING")
            self.assertEqual((await self.client.get("/api/print/preview", headers=self.auth)).status, 200)
            (directory / "preview-1.bin").write_bytes(b"stale")
            (directory / "output.gcode-plate1.opv").write_bytes(b"stale")  # Older bridge versions.
            self.status(gcode_state="FAILED", print_error=STOPPED)
            await self.client.post(f"/api/jobs/{job['id']}/slice", json={"slot": 2}, headers=self.auth)
            await self.wait_state(job["id"], "sliced", "failed")
            self.assertFalse((directory / "output.gcode-plate1.opv").exists())
            await self.print_job(job["id"], {})
            self.status(gcode_state="RUNNING")
            response = await self.client.get("/api/print/preview", headers=self.auth)
            self.assertEqual(len(preview.decode(await response.read()).layers), 2)

    async def test_thumbnails_are_not_cached(self):
        job = await self.upload("a.gcode.3mf", sliced_3mf())
        response = await self.client.get(f"/api/jobs/{job['id']}/thumbnail", headers=self.auth)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        with self.ftps(), self.starts():
            await self.print_job(job["id"])
        response = await self.client.get("/api/print/thumbnail", headers=self.auth)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")


class SpoolPlanBackoffTests(PrintTestCase):
    """QO-5: no FTPS session on every status update when the print file cannot be found."""

    async def test_backoff_per_print(self):
        listings = []
        with mock.patch("bridge.server.ftps.list_dir", lambda *a, **_kw: listings.append(a) or []):
            self.status(gcode_state="RUNNING", subtask_name="ghost", gcode_file="ghost.gcode.3mf")
            with self.assertLogs("bridge.server", "INFO") as logs:
                with self.assertRaises(JobError):
                    await spool_plan(self.app, self.dev)
            self.assertEqual(len(logs.records), 1)
            for _ in range(5):
                with self.assertRaises(JobError):
                    await spool_plan(self.app, self.dev)
            self.assertEqual(len(listings), 1)
            self.dev.plan_retry["at"] = 0  # 30 s later.
            with self.assertRaises(JobError):
                await spool_plan(self.app, self.dev)
            self.assertEqual(len(listings), 2)
            self.assertAlmostEqual(self.dev.plan_retry["at"] - time.monotonic(), 60, delta=5)
            for _ in range(PLAN_ATTEMPTS + 3):
                self.dev.plan_retry["at"] = 0
                with self.assertRaises(JobError):
                    await spool_plan(self.app, self.dev)
            self.assertEqual(len(listings), PLAN_ATTEMPTS)
            # A new print starts over.
            self.status(subtask_name="other", gcode_file="other.gcode.3mf")
            with self.assertRaises(JobError):
                await spool_plan(self.app, self.dev)
            self.assertEqual(len(listings), PLAN_ATTEMPTS + 1)

    async def test_plan_waits_for_the_start_confirmation(self):
        # An older print from elsewhere with the same name is still on the card; it must not become the plan.
        self.card["teil.gcode.3mf"] = variant(object_id=5)
        job = await self.upload("teil.gcode.3mf", sliced_3mf())
        seen = []

        async def command(section, body, wait=15):
            self.status(gcode_state="PREPARE", gcode_file="/data/Metadata/plate_1.gcode", subtask_name="teil")
            try:
                await spool_plan(self.app, self.dev)
            except JobError as exc:
                seen.append(str(exc))
            return {"result": "success"}

        with self.ftps(), mock.patch.object(self.printer, "command", mock.AsyncMock(side_effect=command)):
            await self.print_job(job["id"])
        self.assertEqual(seen, ["The print is still starting."])
        self.dev.plan_retry = None
        self.assertEqual((await spool_plan(self.app, self.dev))[1], {1: 2})

    async def test_calibration_needs_no_file(self):
        with mock.patch("bridge.server.ftps.list_dir") as listing:
            self.status(gcode_state="RUNNING", subtask_name="", gcode_file="/usr/etc/print/auto_cali_for_user.gcode")
            self.assertEqual(await spool_plan(self.app, self.dev), ([], None))
            response = await self.client.get("/api/print/thumbnail", headers=self.auth)
        self.assertEqual(response.status, 404)
        listing.assert_not_called()


class CardTransferTests(PrintTestCase):
    """SEC-O15: size limit during FTPS reads, previews pruned with their archives, imported files unchanged."""

    def test_download_stops_at_the_limit(self):
        class FakeFTP:
            def retrbinary(self, cmd, callback, blocksize=8192):
                for _ in range(3):
                    callback(b"x" * 10)

        with tempfile.TemporaryDirectory() as tmp, mock.patch("bridge.ftps._connect", return_value=FakeFTP()), \
                mock.patch("bridge.ftps._close"):
            local = Path(tmp) / "f.gcode.3mf"
            with self.assertRaises(ftps.TooLarge):
                ftps.download("h", "c", "/f.gcode.3mf", local, limit=25)
            self.assertEqual(list(Path(tmp).iterdir()), [])
            ftps.download("h", "c", "/f.gcode.3mf", local, limit=30)
            self.assertEqual(local.stat().st_size, 30)

    async def test_import_size_limit(self):
        big = {"name": "big.gcode.3mf", "size": 301 * 1024 * 1024, "is_dir": False, "modified": "Nov 20 14:48"}
        with mock.patch("bridge.server.ftps.list_dir", return_value=[big]), \
                mock.patch("bridge.server.ftps.download") as download:
            response = await self.client.post("/api/sdcard/import", json={"path": "/big.gcode.3mf"}, headers=self.auth)
        self.assertEqual(response.status, 422)
        self.assertIn("larger than 300 MB", (await response.json())["error"])
        download.assert_not_called()

        # The listing claimed a small file, the transfer brings more: stopped, and no job is left.
        small = {**big, "size": 10}
        with mock.patch("bridge.server.ftps.list_dir", return_value=[small]), \
                mock.patch("bridge.server.ftps.download", side_effect=ftps.TooLarge(10)):
            response = await self.client.post("/api/sdcard/import", json={"path": "/big.gcode.3mf"}, headers=self.auth)
        self.assertEqual(response.status, 422)
        self.assertEqual(self.app[JOBS].list(), [])

    async def test_current_print_size_limit(self):
        big = {"name": "huge.gcode.3mf", "size": 301 * 1024 * 1024, "is_dir": False, "modified": "Nov 20 14:48"}
        self.status(gcode_state="RUNNING", subtask_name="huge", gcode_file="huge.gcode.3mf")
        with mock.patch("bridge.server.ftps.list_dir", return_value=[big]), \
                mock.patch("bridge.server.ftps.download") as download:
            response = await self.client.get("/api/print/thumbnail", headers=self.auth)
        self.assertEqual(response.status, 404)
        self.assertIn("larger than 300 MB", (await response.json())["error"])
        download.assert_not_called()

    def test_previews_are_pruned_with_their_archives(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            for index, name in enumerate(("old-1", "mid-2", "new-3")):
                archive = cache / f"{name}.gcode.3mf"
                archive.write_bytes(b"zip")
                (cache / f"{archive.stem}-plate1.opv").write_bytes(b"opv")
                (cache / f"{archive.stem}-plate2.opv").write_bytes(b"opv")
                time.sleep(0.01)
            (cache / "gone-4.gcode-plate1.opv").write_bytes(b"orphan")
            _prune_print_cache(cache, keep=2)
            self.assertEqual(sorted(p.name for p in cache.iterdir()), [
                "mid-2.gcode-plate1.opv", "mid-2.gcode-plate2.opv", "mid-2.gcode.3mf",
                "new-3.gcode-plate1.opv", "new-3.gcode-plate2.opv", "new-3.gcode.3mf"])

    async def test_imported_file_must_be_unchanged(self):
        self.card["teil.gcode.3mf"] = sliced_3mf()
        self.times["teil.gcode.3mf"] = "Nov 20 14:48"
        with self.ftps():
            response = await self.client.post("/api/sdcard/import", json={"path": "/teil.gcode.3mf"}, headers=self.auth)
            job = await response.json()
            self.assertEqual(response.status, 201, job)
            self.assertEqual(job["card_file"], {"size": len(sliced_3mf()), "modified": "Nov 20 14:48"})
            # Same size, but written again later (for example by OrcaSlicer under the same name).
            self.times["teil.gcode.3mf"] = "Nov 21 09:12"
            with self.starts() as cmd:
                done = await self.print_job(job["id"], expect="failed")
            self.assertIn("changed since it was imported", done["error"])
            cmd.assert_not_called()
            del self.card["teil.gcode.3mf"]
            with self.starts():
                done = await self.print_job(job["id"], expect="failed")
            self.assertIn("not on the SD card", done["error"])

    def test_card_file_comparison(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "f.gcode.3mf"
            local.write_bytes(b"x" * 10)
            job = Job(id="0" * 16, name="f", filename="f.gcode.3mf", kind="sliced", created_at=0,
                      card_file={"size": 10, "modified": "May 20 14:48"})
            entry = {"name": "f.gcode.3mf", "size": 10, "is_dir": False, "modified": "May 20 14:48"}
            self.assertTrue(_card_unchanged(job, entry, local))
            self.assertFalse(_card_unchanged(job, {**entry, "modified": "May 20 14:49"}, local))
            self.assertFalse(_card_unchanged(job, {**entry, "size": 11}, local))
            self.assertFalse(_card_unchanged(job, None, local))
            job.card_file = None  # Imported by an older bridge: size only.
            self.assertTrue(_card_unchanged(job, {**entry, "modified": "Jan 1 00:00"}, local))
        # LIST shows only the date once a file is older than six months.
        day = iso_modified("May 20 14:48")[:10]
        self.assertTrue(_same_card_time("May 20 14:48", day))
        self.assertFalse(_same_card_time("May 20 14:48", "2023-05-21"))
        self.assertFalse(_same_card_time("May 20 14:48", None))


if __name__ == "__main__":
    unittest.main()
