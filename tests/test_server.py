import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from bridge.config import Config, ConfigError
from bridge.printer import Printer, PrinterError
from bridge.server import PUSH, app_token, build_mapping, create_app
from helpers import FILAMENT_IDS, PROFILES, load, project_3mf, sliced_3mf

SECRET = "s3cret-bridge-access-token"


class FakeCamera:
    active = False
    frame_at = None
    error = None

    async def snapshot(self):
        return b"\xff\xd8jpeg\xff\xd9"

    async def frames(self):
        yield b"\xff\xd8one\xff\xd9"


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(
            host="192.0.2.10", serial="01P00A000000000", access_code="12345678", bridge_token=SECRET,
            data_dir=Path(self.tmp.name), profiles_dir=PROFILES, filament_ids=FILAMENT_IDS,
            orca_cmd=(sys.executable, str(Path(__file__).parent / "fake_orca.py")))
        self.printer = Printer(self.config.host, self.config.serial, self.config.access_code, "Test")
        self.printer.handle(load("pushall_p1s.json"))
        self.printer.handle(load("version_p1s.json"))
        self.printer.connected = True
        self.printer._publish = mock.AsyncMock()  # No network traffic.
        app = create_app(self.config, printer=self.printer, camera=FakeCamera(), start_background=False)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.auth = {"Authorization": f"Bearer {app_token(SECRET)}"}

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def upload(self, name: str, data: bytes):
        form = FormData()
        form.add_field("file", data, filename=name, content_type="application/octet-stream")
        return await self.client.post("/api/jobs", data=form, headers=self.auth)

    async def wait_state(self, job_id: str, *states: str) -> dict:
        for _ in range(100):
            job = await (await self.client.get(f"/api/jobs/{job_id}", headers=self.auth)).json()
            if job["state"] in states:
                return job
            await asyncio.sleep(0.05)
        self.fail(f"Auftrag blieb in {job['state']}")

    def printer_starts(self):
        """Fake print command: report PREPARE with the submitted filename afterward."""
        async def command(section, body, wait=15):
            if body.get("command") == "project_file":
                self.printer.handle({"print": {"command": "push_status", "gcode_state": "PREPARE",
                                               "gcode_file": body["url"].rsplit("/", 1)[-1]}})
            return {"result": "success"}
        return mock.AsyncMock(side_effect=command)

    @staticmethod
    def card(uploads: dict):
        """Fake list_dir: show the files stored by the upload fake."""
        return lambda h, c, d, **_kw: [{"name": n, "size": size, "is_dir": False, "modified": None} for n, size in uploads.items()]

    @staticmethod
    def uploader(uploads: dict, calls: list | None = None):
        def upload(host, code, local, remote, progress=None, **_kw):
            if calls is not None:
                calls.append(remote)
            uploads[remote] = local.stat().st_size
            if progress:
                progress(1, 1)
        return upload

    async def test_health_and_auth(self):
        self.assertEqual((await self.client.get("/healthz")).status, 200)
        self.assertEqual((await self.client.get("/api/status")).status, 401)
        bad = {"Authorization": "Bearer falsch"}
        self.assertEqual((await self.client.get("/api/status", headers=bad)).status, 401)

    async def test_pair(self):
        with mock.patch("bridge.server.asyncio.sleep", mock.AsyncMock()):
            wrong = await self.client.post("/api/pair", json={"token": "nope"})
        self.assertEqual(wrong.status, 401)
        right = await self.client.post("/api/pair", json={"token": SECRET})
        self.assertEqual((await right.json())["app_token"], app_token(SECRET))

    async def test_status(self):
        response = await self.client.get("/api/status", headers=self.auth)
        status = await response.json()
        self.assertEqual(status["printer"]["model"], "Bambu Lab P1S")
        self.assertEqual(status["printer"]["firmware"], "01.10.00.00")
        self.assertEqual(len(status["ams"]["units"][0]["trays"]), 4)
        self.assertIn("camera", status)

    async def test_control_checks_state(self):
        response = await self.client.post("/api/control", json={"action": "pause"}, headers=self.auth)
        self.assertEqual(response.status, 409)  # FAILED cannot be paused.
        response = await self.client.post("/api/control", json={"action": "selbstzerstörung"}, headers=self.auth)
        self.assertEqual(response.status, 422)
        with mock.patch.object(self.printer, "command", mock.AsyncMock(return_value={"result": "success"})) as cmd:
            response = await self.client.post("/api/control", json={"action": "light", "value": False}, headers=self.auth)
            self.assertEqual(response.status, 200)
            section, body = cmd.call_args.args
            self.assertEqual(section, "system")
            self.assertEqual(body["led_mode"], "off")

    async def test_camera_snapshot(self):
        response = await self.client.get("/api/camera/snapshot", headers=self.auth)
        self.assertEqual(response.content_type, "image/jpeg")

    async def test_profiles(self):
        data = await (await self.client.get("/api/profiles", headers=self.auth)).json()
        self.assertEqual(data["default_quality"], "0.20mm Standard @BBL X1C")
        self.assertIn("textured_pei", [p["id"] for p in data["plates"]])

    async def test_upload_rejects_truncated_files(self):
        # Interrupted downloads: 3MF without end directory, binary STL shorter than its triangle count.
        data = project_3mf()
        for name, body in (("katze.3mf", data[: len(data) // 2]), ("fertig.gcode.3mf", sliced_3mf()[:300]),
                           ("teil.stl", b"\0" * 80 + (1000).to_bytes(4, "little") + b"\0" * 50 * 400),
                           ("text.stl", b"solid teil\n" + b"facet normal 0 0 1\n outer loop\n" * 10)):
            response = await self.upload(name, body)
            self.assertEqual(response.status, 422, name)
            self.assertIn('incomplete', (await response.json())["error"], name)
        self.assertEqual((await (await self.client.get("/api/jobs", headers=self.auth)).json())["jobs"], [])
        complete = b"\0" * 80 + (2).to_bytes(4, "little") + b"\0" * 100
        self.assertEqual((await self.upload("ganz.stl", complete)).status, 201)
        text = b"solid teil\n" + b"facet normal 0 0 1\n outer loop\n" * 10 + b"endsolid teil\n"
        self.assertEqual((await self.upload("ganz-text.stl", text)).status, 201)

    async def test_upload_rejects_unknown_type(self):
        response = await self.upload("bild.png", b"x")
        self.assertEqual(response.status, 422)

    async def test_model_slice_and_print(self):
        response = await self.upload("cube.stl", b"solid cube\nendsolid cube\n")
        self.assertEqual(response.status, 201)
        job = await response.json()
        self.assertEqual(job["kind"], "model")

        response = await self.client.post(f"/api/jobs/{job['id']}/slice", headers=self.auth,
                                          json={"slot": 2, "quality": "0.12mm Fine @BBL X1C", "support": "tree"})
        self.assertEqual(response.status, 202, await response.text())
        job = await self.wait_state(job["id"], "sliced", "failed")
        self.assertEqual(job["state"], "sliced", job.get("error"))
        self.assertEqual(job["settings"]["filaments"], ["Bambu PLA Basic @BBL P1S 0.4 nozzle"])
        self.assertTrue(job["thumbnail"])
        args = json.loads((Path(self.tmp.name) / "jobs" / job["id"] / "work" / "out" / "args.json").read_text())
        self.assertIn("--load-filaments", args)
        self.assertEqual(args[args.index("--orient") + 1], "1")  # Orient STL by default.
        self.assertNotIn("--clone-objects", args)
        preview_response = await self.client.get(f"/api/jobs/{job['id']}/preview", headers=self.auth)
        self.assertEqual(preview_response.status, 200)
        from bridge import preview
        decoded = preview.decode(await preview_response.read())
        self.assertEqual(len(decoded.layers), 2)
        thumb = await self.client.get(f"/api/jobs/{job['id']}/thumbnail", headers=self.auth)
        self.assertEqual(thumb.content_type, "image/png")

        sent, uploads = {}, {}

        def fake_upload(host, code, local, remote, progress, **_kw):
            sent.update(host=host, remote=remote, size=local.stat().st_size)
            uploads[remote] = local.stat().st_size
            progress(1, 1)

        with mock.patch("bridge.server.ftps.upload", fake_upload), \
                mock.patch("bridge.server.ftps.list_dir", self.card(uploads)), \
                mock.patch.object(self.printer, "command", self.printer_starts()) as cmd:
            response = await self.client.post(f"/api/jobs/{job['id']}/print", json={"timelapse": False}, headers=self.auth)
            self.assertEqual(response.status, 202, await response.text())
            self.assertEqual((await response.json())["state"], "sending")
            done = await self.wait_state(job["id"], "sent", "failed")
            self.assertEqual(done["state"], "sent", done.get("error"))
            section, body = cmd.call_args.args
        self.assertEqual(section, "print")
        self.assertEqual(body["command"], "project_file")
        self.assertEqual(body["param"], "Metadata/plate_1.gcode")
        self.assertEqual(body["ams_mapping"], [2])
        self.assertTrue(body["use_ams"])
        self.assertEqual(body["url"], f"file:///sdcard/{sent['remote']}")
        self.assertEqual(sent["host"], "192.0.2.10")
        job = await (await self.client.get(f"/api/jobs/{job['id']}", headers=self.auth)).json()
        self.assertEqual(job["state"], "sent")

    async def test_3mf_project_only_for_this_printer(self):
        own = await (await self.upload("eigenes.3mf", project_3mf())).json()
        self.assertEqual(own["kind"], "project")
        self.assertEqual(own["settings"]["project_plates"], 2)
        # Bambu project for a different printer/nozzle: retain geometry and use our profiles.
        for name, data in (("a1.3mf", project_3mf(printer="Bambu Lab A1")),
                           ("duese.3mf", project_3mf(nozzle="0.6")),
                           ("prusa.3mf", project_3mf(settings=False))):
            job = await (await self.upload(name, data)).json()
            self.assertEqual(job["kind"], "model", name)
            self.assertFalse(job["settings"]["orient"], name)

    async def test_foreign_3mf_slices_with_own_profiles(self):
        job = await (await self.upload("10xclip.3mf", project_3mf(settings=False))).json()
        many = await self.client.post(f"/api/jobs/{job['id']}/slice", json={"slot": 2, "copies": 3}, headers=self.auth)
        self.assertEqual(many.status, 422)
        self.assertIn("3MF", (await many.json())["error"])
        response = await self.client.post(f"/api/jobs/{job['id']}/slice", json={"slot": 2}, headers=self.auth)
        self.assertEqual(response.status, 202, await response.text())
        await self.wait_state(job["id"], "sliced", "failed")
        args = json.loads((Path(self.tmp.name) / "jobs" / job["id"] / "work" / "out" / "args.json").read_text())
        self.assertIn("--load-settings", args)
        self.assertIn("--load-filaments", args)
        self.assertEqual(args[args.index("--arrange") + 1], "1")
        self.assertEqual(args[args.index("--orient") + 1], "0")  # Retain the file's orientation.
        self.assertNotIn("--allow-newer-file", args)

    async def test_slice_copies_and_no_orient(self):
        job = await (await self.upload("cube.stl", b"solid cube\n")).json()
        bad = await self.client.post(f"/api/jobs/{job['id']}/slice", json={"slot": 2, "copies": 51}, headers=self.auth)
        self.assertEqual(bad.status, 422)
        await self.client.post(f"/api/jobs/{job['id']}/slice", json={"slot": 2, "copies": 4, "orient": False}, headers=self.auth)
        await self.wait_state(job["id"], "sliced", "failed")
        args = json.loads((Path(self.tmp.name) / "jobs" / job["id"] / "work" / "out" / "args.json").read_text())
        self.assertEqual(args[args.index("--clone-objects") + 1], "4")
        self.assertEqual(args[args.index("--orient") + 1], "0")

    async def test_slice_failure_reports_reason(self):
        job = await (await self.upload("x.stl", b"solid fail\n")).json()
        await self.client.post(f"/api/jobs/{job['id']}/slice", json={"slot": 2}, headers=self.auth)
        job = await self.wait_state(job["id"], "sliced", "failed")
        self.assertEqual(job["state"], "failed")
        self.assertIn('outside', job["error"])

    async def test_sliced_upload_and_busy_printer(self):
        response = await self.upload("fertig.gcode.3mf", sliced_3mf())
        job = await response.json()
        self.assertEqual(job["state"], "sliced")
        self.printer.handle({"print": {"command": "push_status", "gcode_state": "RUNNING"}})
        response = await self.client.post(f"/api/jobs/{job['id']}/print", json={"mapping": {"1": 2}}, headers=self.auth)
        self.assertEqual(response.status, 409)

    async def test_flow_calibration_follows_model(self):
        status = await (await self.client.get("/api/status", headers=self.auth)).json()
        self.assertEqual(status["printer"]["model"], "Bambu Lab P1S")
        self.assertIs(status["printer"]["flow_calibration"], False)
        # P1S cannot measure flow dynamics; omit the option from its print command.
        from bridge.server import JOBS
        job = await (await self.upload("fertig.gcode.3mf", sliced_3mf())).json()
        uploads = {}
        with mock.patch("bridge.server.ftps.upload", self.uploader(uploads)), \
                mock.patch("bridge.server.ftps.list_dir", self.card(uploads)), \
                mock.patch.object(self.printer, "command", self.printer_starts()) as cmd:
            response = await self.client.post(f"/api/jobs/{job['id']}/print",
                                              json={"mapping": {"1": 2}, "flow_cali": True}, headers=self.auth)
            self.assertEqual(response.status, 202)
            await self.wait_state(job["id"], "sent", "failed")
        sent = [call.args[1] for call in cmd.await_args_list if call.args[1].get("command") == "project_file"]
        self.assertIs(sent[0]["flow_cali"], False)
        self.assertIsNotNone(self.client.server.app[JOBS].get(job["id"]))

    async def test_k_values(self):
        reply = {"command": "extrusion_cali_get", "result": "success", "filaments": [
            {"cali_idx": 1, "filament_id": "GFSNL08", "name": "SUNLU PETG", "k_value": "0.034", "n_coef": "0"}]}
        with mock.patch.object(self.printer, "command", mock.AsyncMock(return_value=reply)) as cmd:
            data = await (await self.client.get("/api/ams/k-values", headers=self.auth)).json()
        self.assertEqual(cmd.await_args.args[1]["command"], "extrusion_cali_get")
        self.assertEqual((data["nozzle_diameter"], data["values"][0]["k"]), ("0.4", 0.034))
        with mock.patch.object(self.printer, "command", mock.AsyncMock(return_value=None)):
            self.assertEqual((await self.client.get("/api/ams/k-values", headers=self.auth)).status, 504)
        with mock.patch.object(self.printer, "command", mock.AsyncMock(return_value={"result": "success"})) as cmd, \
                mock.patch.object(self.printer, "pushall", mock.AsyncMock()):
            response = await self.client.post("/api/ams/k-value", json={"slot": 0, "cali_idx": 1}, headers=self.auth)
            self.assertEqual(response.status, 200)
            self.assertEqual(cmd.await_args.args[1]["command"], "extrusion_cali_sel")
            self.assertEqual((await self.client.post("/api/ams/k-value", json={"slot": 0, "cali_idx": "x"},
                                                     headers=self.auth)).status, 422)
            self.assertEqual((await self.client.post("/api/ams/k-value", json={"slot": 42, "cali_idx": 1},
                                                     headers=self.auth)).status, 422)
        # Do not modify the slot currently in use for printing.
        self.printer.handle({"print": {"command": "push_status", "gcode_state": "RUNNING"}})
        active = self.printer.status()["ams"]["active_slot"]
        response = await self.client.post("/api/ams/k-value", json={"slot": active, "cali_idx": 1}, headers=self.auth)
        self.assertEqual(response.status, 409)

    async def test_print_needs_mapping_for_sliced_file(self):
        job = await (await self.upload("fertig.gcode.3mf", sliced_3mf())).json()
        response = await self.client.post(f"/api/jobs/{job['id']}/print", json={}, headers=self.auth)
        self.assertEqual(response.status, 422)
        response = await self.client.post(f"/api/jobs/{job['id']}/print", json=[1], headers=self.auth)
        self.assertEqual(response.status, 400)
        response = await self.client.post(f"/api/jobs/{job['id']}/print", json={"plate": "x"}, headers=self.auth)
        self.assertEqual(response.status, 422)


    async def test_objects_of_bridge_print_and_skip(self):
        job = await (await self.upload("fertig.gcode.3mf", sliced_3mf(plates=((1, [(1, "PLA", "#FF0000", "GFA00", 3.7)]),)))).json()
        uploads = {}
        with mock.patch("bridge.server.ftps.upload", self.uploader(uploads)), \
                mock.patch("bridge.server.ftps.list_dir", self.card(uploads)), \
                mock.patch.object(self.printer, "command", self.printer_starts()):
            await self.client.post(f"/api/jobs/{job['id']}/print", json={"mapping": {"1": 2}}, headers=self.auth)
            await self.wait_state(job["id"], "sent", "failed")
        self.printer.handle({"print": {"command": "push_status", "gcode_state": "RUNNING", "subtask_name": "fertig",
                                       "gcode_file": "/data/Metadata/plate_1.gcode"}})
        data = await (await self.client.get("/api/print/objects", headers=self.auth)).json()
        self.assertEqual(data["plate"], 1)
        thumb = await self.client.get("/api/print/thumbnail", headers=self.auth)
        self.assertEqual(thumb.content_type, "image/png")
        self.assertEqual([o["id"] for o in data["objects"]], [1])
        # Skipping the only object would end the print and must be rejected.
        with mock.patch.object(self.printer, "command", mock.AsyncMock()) as cmd:
            response = await self.client.post("/api/control", json={"action": "skip_objects", "value": [1]}, headers=self.auth)
        self.assertEqual(response.status, 422)
        cmd.assert_not_called()

    async def test_thumbnail_of_print_from_orcaslicer(self):
        # OrcaSlicer print exists only on the card; without plate_1.png, use the unlit image.
        import io
        import zipfile
        buffer = io.BytesIO(sliced_3mf(thumbnails=False))
        with zipfile.ZipFile(buffer, "a") as archive:
            archive.writestr("Metadata/plate_no_light_1.png", b"\x89PNG ohne Licht")
            archive.writestr("Metadata/top_1.png", b"\x89PNG oben")
        name = "Cat-Set_3Color_Multi_Bambu v1_plate_1"
        card = {f"{name}.gcode.3mf": buffer.getvalue()}

        def download(host, code, remote, local, progress=None, **_kw):
            local.write_bytes(card[remote.lstrip("/")])

        self.printer.handle({"print": {"command": "push_status", "gcode_state": "RUNNING", "subtask_name": name,
                                       "gcode_file": f"{name}.gcode.3mf"}})
        with mock.patch("bridge.server.ftps.list_dir", self.card({n: len(d) for n, d in card.items()})), \
                mock.patch("bridge.server.ftps.download", download):
            thumb = await self.client.get("/api/print/thumbnail", headers=self.auth)
        self.assertEqual(thumb.status, 200)
        self.assertEqual(await thumb.read(), b"\x89PNG ohne Licht")

    async def test_set_tray(self):
        with mock.patch.object(self.printer, "command", mock.AsyncMock(return_value={"result": "success"})) as cmd:
            response = await self.client.post("/api/ams/tray", headers=self.auth,
                                              json={"slot": 0, "filament": "Generic PETG", "color": "#202020"})
            self.assertEqual(response.status, 200, await response.text())
            section, body = cmd.call_args.args
        self.assertEqual(body["command"], "ams_filament_setting")
        self.assertEqual(body["tray_type"], "PETG")
        response = await self.client.post("/api/ams/tray", headers=self.auth,
                                          json={"slot": 0, "filament": "Fremdes PLA", "color": "#202020"})
        self.assertEqual(response.status, 422)

    async def test_sdcard_import_prints_in_place(self):
        def fake_download(host, code, remote, local, *rest, **_kw):
            Path(local).write_bytes(sliced_3mf())
        listing = [{"name": "Unbenannt.gcode.3mf", "size": 10, "is_dir": False, "modified": "Nov 20 14:48"},
                   {"name": "Unbenannt_plate_1.gcode", "size": 10, "is_dir": False, "modified": "2023-08-31"},
                   {"name": "._Unbenannt.gcode.3mf", "size": 4096, "is_dir": False, "modified": "2025-06-24"}]
        with mock.patch("bridge.server.ftps.list_dir", lambda h, c, d, **_kw: listing if d == "/" else []):
            files = (await (await self.client.get("/api/sdcard", headers=self.auth)).json())["files"]
        self.assertEqual([f["path"] for f in files], ["/Unbenannt.gcode.3mf"])
        with mock.patch("bridge.server.ftps.download", fake_download):
            response = await self.client.post("/api/sdcard/import", json={"path": "/Unbenannt.gcode.3mf"}, headers=self.auth)
        job = await response.json()
        self.assertEqual(response.status, 201, job)
        self.assertEqual(job["remote_path"], "Unbenannt.gcode.3mf")
        upload = mock.Mock()
        with mock.patch("bridge.server.ftps.upload", upload), \
                mock.patch.object(self.printer, "command", self.printer_starts()) as cmd:
            response = await self.client.post(f"/api/jobs/{job['id']}/print", json={"mapping": {"1": 2}}, headers=self.auth)
            self.assertEqual(response.status, 202, await response.text())
            done = await self.wait_state(job["id"], "sent", "failed")
            self.assertEqual(done["state"], "sent", done.get("error"))
            body = cmd.call_args.args[1]
        upload.assert_not_called()
        self.assertEqual(body["url"], "file:///sdcard/Unbenannt.gcode.3mf")
        bad = await self.client.post("/api/sdcard/import", json={"path": "/../x.gcode.3mf"}, headers=self.auth)
        self.assertEqual(bad.status, 422)


    async def test_sdcard_download_streams_blocks(self):
        def retrieve(host, code, remote, sink, **_kw):
            self.assertEqual(remote, "/timelapse/video.avi")
            for block in (b"RIFF", b"-avi-", b"daten"):
                sink(block)
        with mock.patch("bridge.server.ftps.retrieve", retrieve):
            response = await self.client.get("/api/sdcard/download", params={"path": "/timelapse/video.avi"},
                                             headers=self.auth)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Type"], "video/x-msvideo")
            self.assertEqual(await response.read(), b"RIFF-avi-daten")

    async def test_sdcard_download_errors(self):
        def refused(host, code, remote, sink, **_kw):
            raise OSError('Connection refused')
        with mock.patch("bridge.server.ftps.retrieve", refused):
            response = await self.client.get("/api/sdcard/download", params={"path": "/timelapse/video.avi"},
                                             headers=self.auth)
            self.assertEqual(response.status, 502)

        def broken(host, code, remote, sink, **_kw):
            sink(b"RIFF")
            raise OSError('Printer gone')
        import aiohttp
        with mock.patch("bridge.server.ftps.retrieve", broken), self.assertLogs("bridge.server", "WARNING"):
            response = await self.client.get("/api/sdcard/download", params={"path": "/timelapse/video.avi"},
                                             headers=self.auth)
            self.assertEqual(response.status, 200)
            with self.assertRaises(aiohttp.ClientPayloadError):
                await response.read()

    async def test_sdcard_download_stops_the_transfer_when_the_app_leaves(self):
        import threading
        stopped = threading.Event()

        def endless(host, code, remote, sink, **_kw):
            try:
                while True:
                    sink(b"x" * 65536)
            except Exception:
                stopped.set()
                raise
        with mock.patch("bridge.server.ftps.retrieve", endless):
            response = await self.client.get("/api/sdcard/download", params={"path": "/timelapse/video.avi"},
                                             headers=self.auth)
            await response.content.read(65536)
            response.close()
            await asyncio.to_thread(stopped.wait, 5)
        self.assertTrue(stopped.is_set())

    async def test_live_registration_counts_iphones(self):
        for token, installation in (("aa" * 32, "AB12CD34-0000"), ("bb" * 32, "AB12CD34-0000"), ("cc" * 32, None)):
            body = {"kind": "start", "token": token, "environment": "production"}
            if installation:
                body["installation"] = installation
            response = await self.client.post("/api/live/register", json=body, headers=self.auth)
            self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await (await self.client.get("/api/live", headers=self.auth)).json())["devices"], 2)
        bad = await self.client.post("/api/live/register", headers=self.auth, json={
            "kind": "start", "token": "dd" * 32, "environment": "production", "installation": "<script>"})
        self.assertEqual(bad.status, 422)

    async def test_notification_registration(self):
        body = {"kind": "alert", "token": "ee" * 32, "environment": "production", "installation": "AB12CD34-0000",
                "events": ["finished", "paused", "finished"]}
        response = await self.client.post("/api/live/register", json=body, headers=self.auth)
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(self.client.app[PUSH].alerts["ee" * 32]["events"], ["finished", "paused"])
        self.assertEqual((await (await self.client.get("/api/live", headers=self.auth)).json())["notifications"], 1)
        for events in (["alles"], "finished", None):
            bad = await self.client.post("/api/live/register", json={**body, "events": events}, headers=self.auth)
            self.assertEqual(bad.status, 422)
        off = await self.client.post("/api/live/register", json={**body, "events": []}, headers=self.auth)
        self.assertEqual(off.status, 200)
        self.assertEqual(self.client.app[PUSH].alerts, {})

    async def test_events_and_status_without_printer(self):
        # Disconnected printers must still return status/live events to prevent app reconnect loops.
        self.printer.connected = False
        self.printer._client = None
        del self.printer._publish  # Real _publish raises when disconnected; pushall handles it.
        response = await self.client.get("/api/status", headers=self.auth)
        self.assertEqual(response.status, 200)
        stream = await self.client.get("/api/events", headers=self.auth)
        self.assertEqual(stream.status, 200)
        first = await asyncio.wait_for(stream.content.readuntil(b"\n\n"), 5)
        self.assertTrue(first.startswith(b"event: status"))
        stream.close()
        control = await self.client.post("/api/control", json={"action": "light", "value": True}, headers=self.auth)
        self.assertEqual(control.status, 409)

    async def test_unknown_state_blocks_motion_and_print(self):
        self.printer.raw.pop("gcode_state", None)
        response = await self.client.post("/api/control", json={"action": "home"}, headers=self.auth)
        self.assertEqual(response.status, 409)
        job = await (await self.upload("fertig.gcode.3mf", sliced_3mf())).json()
        response = await self.client.post(f"/api/jobs/{job['id']}/print", json={"mapping": {"1": 2}}, headers=self.auth)
        self.assertEqual(response.status, 409)

    async def test_concurrent_print_is_rejected(self):
        first = await (await self.upload("a.gcode.3mf", sliced_3mf())).json()
        second = await (await self.upload("b.gcode.3mf", sliced_3mf())).json()
        uploads = {}
        fast = self.uploader(uploads)

        def slow_upload(*args, **_kw):
            import time
            time.sleep(0.3)
            fast(*args)

        with mock.patch("bridge.server.ftps.upload", slow_upload), \
                mock.patch("bridge.server.ftps.list_dir", self.card(uploads)), \
                mock.patch.object(self.printer, "command", self.printer_starts()):
            one = await self.client.post(f"/api/jobs/{first['id']}/print", json={"mapping": {"1": 2}}, headers=self.auth)
            self.assertEqual(one.status, 202)
            two = await self.client.post(f"/api/jobs/{second['id']}/print", json={"mapping": {"1": 2}}, headers=self.auth)
            self.assertEqual(two.status, 409)
            self.assertIn('Another print', (await two.json())["error"])
            self.assertEqual((await self.wait_state(first["id"], "sent", "failed"))["state"], "sent")

    async def test_print_retries_after_upload_error(self):
        job = await (await self.upload("a.gcode.3mf", sliced_3mf())).json()
        uploads, calls = {}, []
        ok_upload = self.uploader(uploads, calls)

        def flaky_upload(*args, **kwargs):
            if not calls:
                calls.append("fehlgeschlagen")
                raise TimeoutError("The read operation timed out")
            ok_upload(*args, **kwargs)

        with mock.patch("bridge.server.PRINT_RETRY_DELAYS", (0.0, 0.0)), \
                mock.patch("bridge.server.ftps.upload", flaky_upload), \
                mock.patch("bridge.server.ftps.list_dir", self.card(uploads)), \
                mock.patch.object(self.printer, "command", self.printer_starts()) as cmd:
            response = await self.client.post(f"/api/jobs/{job['id']}/print", json={"mapping": {"1": 2}}, headers=self.auth)
            self.assertEqual(response.status, 202)
            done = await self.wait_state(job["id"], "sent", "failed")
        self.assertEqual(done["state"], "sent", done.get("error"))
        self.assertEqual(len(calls), 2)
        self.assertEqual(cmd.await_count, 1)
        self.assertIsNone(done["pending"])
        self.assertIsNone(done["step"])

    async def test_incomplete_upload_fails_after_attempts(self):
        job = await (await self.upload("a.gcode.3mf", sliced_3mf())).json()
        uploads = {}

        def short_upload(host, code, local, remote, progress=None, **_kw):
            uploads[remote] = 1

        with mock.patch("bridge.server.PRINT_RETRY_DELAYS", (0.0, 0.0)), \
                mock.patch("bridge.server.ftps.upload", short_upload), \
                mock.patch("bridge.server.ftps.list_dir", self.card(uploads)), \
                mock.patch.object(self.printer, "command", self.printer_starts()) as cmd:
            await self.client.post(f"/api/jobs/{job['id']}/print", json={"mapping": {"1": 2}}, headers=self.auth)
            done = await self.wait_state(job["id"], "sent", "failed")
        self.assertEqual(done["state"], "failed")
        self.assertIn('incomplete', done["error"])
        cmd.assert_not_called()

    async def test_late_start_confirmation_is_not_sent_twice(self):
        # Late start confirmation must not cause a second print command.
        job = await (await self.upload("a.gcode.3mf", sliced_3mf())).json()
        uploads = {}

        async def slow_report(section, body, wait=15):
            remote = body["url"].rsplit("/", 1)[-1]
            asyncio.get_running_loop().call_later(0.1, self.printer.handle, {
                "print": {"command": "push_status", "gcode_state": "RUNNING", "gcode_file": remote}})
            return {"result": "success"}

        with mock.patch("bridge.server.PRINT_RETRY_DELAYS", (0.0, 0.0)), \
                mock.patch("bridge.server.START_CONFIRM_WAIT", 0.05), \
                mock.patch("bridge.server.ftps.upload", self.uploader(uploads)), \
                mock.patch("bridge.server.ftps.list_dir", self.card(uploads)), \
                mock.patch.object(self.printer, "command", mock.AsyncMock(side_effect=slow_report)) as cmd:
            await self.client.post(f"/api/jobs/{job['id']}/print", json={"mapping": {"1": 2}}, headers=self.auth)
            done = await self.wait_state(job["id"], "sent", "failed")
        self.assertEqual(done["state"], "sent", done.get("error"))
        self.assertEqual(cmd.await_count, 1)

    async def test_printer_rejects_command(self):
        job = await (await self.upload("a.gcode.3mf", sliced_3mf())).json()
        uploads = {}
        with mock.patch("bridge.server.ftps.upload", self.uploader(uploads)), \
                mock.patch("bridge.server.ftps.list_dir", self.card(uploads)), \
                mock.patch.object(self.printer, "command", mock.AsyncMock(return_value={"result": "fail", "reason": "SD"})) as cmd:
            await self.client.post(f"/api/jobs/{job['id']}/print", json={"mapping": {"1": 2}}, headers=self.auth)
            done = await self.wait_state(job["id"], "sent", "failed")
        self.assertEqual(done["state"], "failed")
        self.assertIn("rejected the request", done["error"])
        self.assertEqual(cmd.await_count, 1)  # Do not retry rejection.

    async def test_resume_after_restart(self):
        import time as clock
        from bridge.server import JOBS, resume_pending_prints
        app = self.client.server.app
        fresh = await (await self.upload("a.gcode.3mf", sliced_3mf())).json()
        stale = await (await self.upload("b.gcode.3mf", sliced_3mf())).json()
        for job_id, age in ((fresh["id"], 30), (stale["id"], 3600)):
            job = app[JOBS].get(job_id)
            job.state = "sending"
            job.pending = {"plate": 1, "ams_mapping": [2], "use_ams": True, "remote_name": f"{job_id}.gcode.3mf",
                           "timelapse": False, "bed_leveling": True, "flow_cali": False,
                           "requested_at": clock.time() - age}
        uploads = {}
        with mock.patch("bridge.server.ftps.upload", self.uploader(uploads)), \
                mock.patch("bridge.server.ftps.list_dir", self.card(uploads)), \
                mock.patch.object(self.printer, "command", self.printer_starts()) as cmd:
            resume_pending_prints(app)
            done = await self.wait_state(fresh["id"], "sent", "failed")
        self.assertEqual(done["state"], "sent", done.get("error"))
        self.assertEqual(cmd.await_count, 1)
        old = await (await self.client.get(f"/api/jobs/{stale['id']}", headers=self.auth)).json()
        self.assertEqual(old["state"], "failed")
        self.assertIn("bridge restarted", old["error"])


    async def test_status_flags_and_texts(self):
        texts = Path(self.tmp.name) / "hms"
        texts.mkdir()
        import gzip
        with gzip.open(texts / "hms_de.json.gz", "wt", encoding="utf-8") as handle:
            json.dump({"device_error": {"0300400C": {"Die Aufgabe wurde abgebrochen.": []}}, "device_hms": {}}, handle)
        from bridge.hms import Texts
        from bridge.server import HMS
        self.printer.handle({"print": {"command": "push_status", "print_error": 0x0300400C}})
        with mock.patch.object(self.client.app[HMS], "_tables", Texts(texts)._tables):
            # Only a German table here: ask in German (English requests fall back to English tables).
            status = await (await self.client.get("/api/status", headers={**self.auth, "Accept-Language": "de"})).json()
        self.assertTrue(status["connection"]["developer_mode"])
        self.assertEqual(status["job"]["error"], {"code": "0300_400C", "value": 0x0300400C,
                                                  "text": "Die Aufgabe wurde abgebrochen.", "internal": False})
        self.assertTrue(status["options"]["ams_backup"])

    async def test_developer_mode_off_refuses_commands(self):
        self.printer.handle({"print": {"command": "push_status", "flag3": 90783}})  # Bit 4: signed commands only.
        job = await (await self.upload("fertig.gcode.3mf", sliced_3mf())).json()
        with mock.patch.object(self.printer, "command", mock.AsyncMock()) as cmd:
            for path, body in (("/api/control", {"action": "light", "value": True}),
                               ("/api/ams/tray", {"slot": 1, "filament": "Generic PLA", "color": "#FFFFFF"}),
                               ("/api/ams/k-value", {"slot": 0, "cali_idx": -1}),
                               (f"/api/jobs/{job['id']}/print", {"mapping": {"1": 2}})):
                response = await self.client.post(path, json=body, headers=self.auth)
                self.assertEqual(response.status, 409, path)
                self.assertIn("Developer Mode", (await response.json())["error"])
            response = await self.client.get("/api/ams/k-values", headers=self.auth)
            self.assertEqual(response.status, 409)
            cmd.assert_not_called()

    async def test_clear_error(self):
        response = await self.client.post("/api/control", json={"action": "clear_error"}, headers=self.auth)
        self.assertEqual(response.status, 409)  # No display message.
        self.printer.handle({"print": {"command": "push_status", "print_error": 0x0300400C}})
        calls = []

        async def command(section, body, wait=8.0):
            calls.append((section, body["command"], wait))
            if section == "system":
                raise PrinterError('Printer rejected the request: unknown')  # Missing dialog command is not an app error.
            return {"result": "success"}

        with mock.patch.object(self.printer, "command", mock.AsyncMock(side_effect=command)):
            response = await self.client.post("/api/control", json={"action": "clear_error"}, headers=self.auth)
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(calls, [("print", "clean_print_error", 8.0), ("system", "uiop", 3)])

    async def test_calibrate(self):
        self.printer.handle({"print": {"command": "push_status", "gcode_state": "IDLE"}})
        with mock.patch.object(self.printer, "command", mock.AsyncMock(return_value={"result": "success"})) as cmd:
            response = await self.client.post("/api/control", headers=self.auth, json={
                "action": "calibrate", "value": {"bed_leveling": True, "vibration": True, "motor_noise": False}})
            self.assertEqual(response.status, 200, await response.text())
            self.assertEqual(cmd.call_args.args, ("print", {"command": "calibration", "option": 6}))
        self.printer.handle({"print": {"command": "push_status", "gcode_state": "RUNNING", "subtask_name": "",
                                       "gcode_file": "/usr/etc/print/auto_cali_for_user.gcode", "stg": [13, 1, 3],
                                       "stg_cur": 1}})
        status = await (await self.client.get("/api/status", headers=self.auth)).json()
        self.assertEqual((status["job"]["name"], status["job"]["calibration"]), ('Calibration', True))
        response = await self.client.post("/api/control", headers=self.auth,
                                          json={"action": "calibrate", "value": {"vibration": True}})
        self.assertEqual(response.status, 409)  # Already running.

    async def test_option(self):
        with mock.patch.object(self.printer, "command", mock.AsyncMock(return_value={"result": "success"})) as cmd:
            response = await self.client.post("/api/control", headers=self.auth,
                                              json={"action": "option", "value": {"key": "ams_backup", "enabled": False}})
            self.assertEqual(response.status, 200, await response.text())
            self.assertEqual(cmd.call_args.args, ("print", {"command": "print_option", "auto_switch_filament": False}))

    async def test_copies_on_several_plates(self):
        job = await (await self.upload("cube.stl", b"solid cube\n")).json()
        await self.client.post(f"/api/jobs/{job['id']}/slice", json={"slot": 2, "copies": 6}, headers=self.auth)
        job = await self.wait_state(job["id"], "sliced", "failed")
        self.assertEqual([p["index"] for p in job["result"]["plates"]], [1, 2])
        self.assertEqual(job["sent_plates"], [])
        for plate in (1, 2):
            preview = await self.client.get(f"/api/jobs/{job['id']}/preview?plate={plate}", headers=self.auth)
            self.assertEqual(preview.status, 200)
        missing = await self.client.get(f"/api/jobs/{job['id']}/preview?plate=3", headers=self.auth)
        self.assertEqual(missing.status, 409)

        uploads, calls = {}, []
        with mock.patch("bridge.server.ftps.upload", self.uploader(uploads, calls)), \
                mock.patch("bridge.server.ftps.list_dir", self.card(uploads)), \
                mock.patch.object(self.printer, "command", self.printer_starts()) as cmd:
            for plate in (1, 2):
                response = await self.client.post(f"/api/jobs/{job['id']}/print", json={"plate": plate}, headers=self.auth)
                self.assertEqual(response.status, 202, await response.text())
                done = await self.wait_state(job["id"], "sent", "failed")
                self.assertEqual(done["state"], "sent", done.get("error"))
                self.assertEqual(cmd.call_args.args[1]["param"], f"Metadata/plate_{plate}.gcode")
                self.printer.handle({"print": {"command": "push_status", "gcode_state": "FINISH"}})
        self.assertEqual(done["sent_plates"], [1, 2])
        self.assertEqual(len(calls), 1)  # Second plate reuses the file already on the card.

        # Reslicing clears the printed-plates history.
        await self.client.post(f"/api/jobs/{job['id']}/slice", json={"slot": 2, "copies": 2}, headers=self.auth)
        job = await self.wait_state(job["id"], "sliced", "failed")
        self.assertEqual((job["sent_plates"], job["uploaded"]), ([], None))
        self.assertEqual(len(job["result"]["plates"]), 1)


class MappingTests(unittest.TestCase):
    def setUp(self):
        printer = Printer("h", "s", "c")
        printer.handle(load("pushall_p1s.json"))
        self.status = printer.status()

    def test_unused_filaments_get_minus_one(self):
        plate = {"filaments": [{"id": 1}, {"id": 3}]}
        mapping, use_ams = build_mapping(plate, {1: 2, 3: 0}, self.status)
        self.assertEqual(mapping, [2, -1, 0])
        self.assertTrue(use_ams)

    def test_external_spool(self):
        mapping, use_ams = build_mapping({"filaments": [{"id": 1}]}, {1: 254}, self.status)
        self.assertEqual((mapping, use_ams), ([], False))
        with self.assertRaises(Exception):
            build_mapping({"filaments": [{"id": 1}, {"id": 2}]}, {1: 254, 2: 0}, self.status)

    def test_missing_slot(self):
        with self.assertRaises(Exception):
            build_mapping({"filaments": [{"id": 1}]}, {1: 9}, self.status)


class ConfigTests(unittest.TestCase):
    def test_from_env(self):
        env = {"BAMBU_HOST": "h", "BAMBU_SERIAL": "s", "BAMBU_ACCESS_CODE": "c", "BRIDGE_ACCESS_TOKEN": "x" * 24,
               "ORCA_CMD": "/opt/orca/AppRun --foo", "PORT": "9000"}
        config = Config.from_env(env)
        self.assertEqual(config.orca_cmd, ("/opt/orca/AppRun", "--foo"))
        self.assertEqual(config.port, 9000)
        self.assertNotIn("c'", repr(config).split("serial")[1])  # Access code omitted from repr.
        with self.assertRaises(ConfigError):
            Config.from_env({**env, "BRIDGE_ACCESS_TOKEN": "kurz"})
        with self.assertRaises(ConfigError):
            Config.from_env({**env, "BAMBU_PRINT_URL": "file:///sdcard/x"})


if __name__ == "__main__":
    unittest.main()
