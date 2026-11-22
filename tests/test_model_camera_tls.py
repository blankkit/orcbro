import asyncio
import ssl
import unittest
from unittest import mock

from bridge import ftps
from bridge.camera import Camera
from bridge.camera_rtsp import MjpegParser, RtspCamera, stream_url
from bridge.probe import _tls
from bridge.trust import PrinterTrust


class CameraSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_model_never_probes_port_6000(self):
        camera = Camera("192.0.2.10", "code")
        self.assertFalse(camera.active)
        self.assertIn('unknown', camera.error)
        with self.assertRaises(RuntimeError):
            await camera.snapshot()
        camera.set_mode("jpeg6000")
        self.assertEqual(camera.mode, "jpeg6000")
        camera.set_mode("rtsps", disabled=True)
        await asyncio.sleep(0)
        self.assertEqual(camera.mode, "disabled")
        self.assertIn("LAN Liveview", camera.error)

    def test_fragmented_mjpeg_and_bounded_garbage(self):
        parser = MjpegParser(limit=20)
        self.assertEqual(parser.feed(b"rauschen\xff"), [])
        self.assertEqual(parser.feed(b"\xd8abc"), [])
        self.assertEqual(parser.feed(b"def\xff\xd9\xff\xd8x\xff"), [b"\xff\xd8abcdef\xff\xd9"])
        self.assertEqual(parser.feed(b"\xd9"), [b"\xff\xd8x\xff\xd9"])
        with self.assertRaises(ValueError):
            MjpegParser(limit=5).feed(b"\xff\xd8" + b"x" * 10)

    def test_url_uses_configured_host_and_escapes_access_code(self):
        url = stream_url("2001:db8::10", "a:b@c")
        self.assertEqual(url, "rtsps://bblp:a%3Ab%40c@[2001:db8::10]:322/streaming/live/1")

    async def test_process_is_terminated_then_killed_after_timeout(self):
        process = mock.Mock(returncode=None)
        process.wait = mock.AsyncMock(return_value=None)
        camera = RtspCamera("host", "code")
        camera._process = process
        async def timeout(awaitable, _seconds):
            awaitable.close()
            raise TimeoutError
        with mock.patch("bridge.camera_rtsp.asyncio.wait_for", timeout):
            await camera._stop_process()
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()


class TlsLimitTests(unittest.TestCase):
    def test_mqtt_and_probe_cap_tls_at_12(self):
        self.assertEqual(PrinterTrust("01P00A000000000").context().maximum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(_tls().maximum_version, ssl.TLSVersion.TLSv1_2)

    def test_ftps_keeps_session_reuse_and_caps_tls(self):
        fake = mock.Mock()
        with mock.patch.object(ftps, "ImplicitFTPS", return_value=fake) as constructor:
            ftps._connect("host", "code", 1)
        context = constructor.call_args.kwargs["context"]
        self.assertEqual(context.maximum_version, ssl.TLSVersion.TLSv1_2)
        fake.connect.assert_called_once_with("host", 990, timeout=1)
        fake.login.assert_called_once_with("bblp", "code")
        fake.prot_p.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
