import zipfile
from unittest import mock
import tempfile
import unittest
from pathlib import Path

from bridge.camera import HEADER, auth_packet, is_jpeg, parse_header
from bridge.jobs import (JobError, JobStore, classify, display_name, project_filaments, project_plates,
                         read_slice_info, remote_name)
from helpers import project_3mf, sliced_3mf


class JobFileTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify("Würfel.STL"), "model")
        self.assertEqual(classify("a.step"), "model")
        self.assertEqual(classify("projekt.3mf"), "project")
        self.assertEqual(classify("fertig.gcode.3mf"), "sliced")
        with self.assertRaises(JobError):
            classify("bild.png")

    def test_names(self):
        self.assertEqual(display_name("Pen Holder.gcode.3mf"), "Pen Holder")
        name = remote_name("Größe – Test/../x", "0123456789abcdef")
        self.assertTrue(name.endswith("_012345.gcode.3mf"))
        self.assertRegex(name, r"^[A-Za-z0-9._-]+$")
        self.assertTrue(name.startswith("Grosse_Test_x_"))

    def test_read_slice_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.gcode.3mf"
            path.write_bytes(sliced_3mf(plates=((1, [(1, "PLA", "#FF0000", "GFA00", 3.7)]),
                                                (2, [(1, "PLA", "#FF0000", "GFA00", 1.0), (3, "PETG", "#000000", "GFG99", 2.0)]))))
            info = read_slice_info(path)
            self.assertEqual([p["index"] for p in info["plates"]], [1, 2])
            self.assertEqual(info["plates"][0]["prediction_s"], 909)
            self.assertEqual(info["plates"][1]["filaments"][1]["id"], 3)
            self.assertEqual(info["plates"][0]["thumbnail"], "Metadata/plate_1.png")

    def test_thumbnail_fallbacks(self):
        from bridge.jobs import plate_thumbnail
        self.assertEqual(plate_thumbnail(["Metadata/plate_2.png", "Metadata/plate_no_light_2.png"], 2), "Metadata/plate_2.png")
        self.assertEqual(plate_thumbnail(["Metadata/top_1.png", "Metadata/plate_no_light_1.png"], 1),
                         "Metadata/plate_no_light_1.png")
        self.assertEqual(plate_thumbnail(["Metadata/top_3.png"], 3), "Metadata/top_3.png")
        self.assertIsNone(plate_thumbnail(["Metadata/plate_1.png"], 2))

    def test_support_used_means_supports_in_gcode(self):
        # OrcaSlicer sets support_used when enabled; actual support use comes from G-code.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.gcode.3mf"
            path.write_bytes(sliced_3mf(support=True))
            plate = read_slice_info(path)["plates"][0]
            self.assertTrue(plate["support_used"])
            self.assertTrue(plate["support_enabled"])
            path.write_bytes(sliced_3mf(support=False))
            plate = read_slice_info(path)["plates"][0]
            self.assertFalse(plate["support_used"])
            self.assertFalse(plate["support_enabled"])

    def test_unsliced_3mf_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.3mf"
            path.write_bytes(project_3mf())
            with self.assertRaises(JobError):
                read_slice_info(path)
            self.assertEqual([f["type"] for f in project_filaments(path)], ["PLA", "PETG"])
            self.assertEqual(project_plates(path), 2)

    def test_store_roundtrip_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp), keep=2)
            job = store.create("cube.stl")
            job.state = "slicing"
            store.save(job)
            reopened = JobStore(Path(tmp), keep=2)
            self.assertEqual(reopened.get(job.id).state, "failed")
            for i in range(3):
                reopened.save(reopened.create(f"m{i}.stl"))
            reopened.prune()
            self.assertEqual(len(reopened.list()), 2)
            self.assertNotIn(job.id, [j.id for j in reopened.list()])  # Prune the oldest job.
            with self.assertRaises(JobError):
                store.dir("../../etc")


class CameraProtocolTests(unittest.TestCase):
    def test_auth_packet_layout(self):
        packet = auth_packet("12345678")
        self.assertEqual(len(packet), 80)
        self.assertEqual(HEADER.unpack(packet[:16]), (0x40, 0x3000, 0, 0))
        self.assertEqual(packet[16:20], b"bblp")
        self.assertEqual(packet[48:56], b"12345678")
        self.assertEqual(packet[56:80], b"\0" * 24)

    def test_header_bounds(self):
        self.assertEqual(parse_header(HEADER.pack(90792, 0, 1, 0)), 90792)
        with self.assertRaises(ValueError):
            parse_header(HEADER.pack(0, 0, 1, 0))
        with self.assertRaises(ValueError):
            parse_header(HEADER.pack(10**9, 0, 1, 0))

    def test_is_jpeg(self):
        self.assertTrue(is_jpeg(b"\xff\xd8abc\xff\xd9"))
        self.assertFalse(is_jpeg(b"\x89PNG"))


class ArchiveLimitTests(unittest.TestCase):
    """Bound uncompressed metadata/archive sizes even when the compressed file is small."""

    def write(self, entries: dict[str, bytes]) -> Path:
        folder = Path(tempfile.mkdtemp())
        path = folder / "bombe.3mf"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        return path

    def test_oversized_metadata_is_refused(self):
        from bridge import jobs
        path = self.write({"Metadata/slice_info.config": b"<config>" + b" " * 5000 + b"</config>",
                           "Metadata/plate_1.gcode": b"G1\n"})
        with mock.patch.object(jobs, "MAX_METADATA_BYTES", 1024):
            with self.assertRaises(jobs.JobError):
                jobs.read_slice_info(path)

    def test_archives_with_too_many_entries_are_refused(self):
        from bridge import jobs
        path = self.write({f"x/{i}": b"" for i in range(10)})
        with mock.patch.object(jobs, "MAX_ARCHIVE_ENTRIES", 5):
            with self.assertRaises(jobs.JobError):
                jobs.open_archive(path)


if __name__ == "__main__":
    unittest.main()
