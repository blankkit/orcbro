import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bridge.__main__ import serve
from bridge.config import Config
from bridge.identity import BridgeIdentity, load_identity
from bridge.server import create_app
from bridge.tls import TLSState, prepare_tls


class ImmediateEvent:
    async def wait(self):
        return None

    def set(self):
        pass


class FakeLoop:
    def __init__(self):
        self.added = []
        self.removed = []

    def add_signal_handler(self, caught, callback):
        self.added.append((caught, callback))

    def remove_signal_handler(self, caught):
        self.removed.append(caught)


class FakeRunner:
    instances = []

    def __init__(self, app, access_log=None):
        self.app = app
        self.access_log = access_log
        self.setup_called = False
        self.cleanup_called = False
        self.instances.append(self)

    async def setup(self):
        self.setup_called = True

    async def cleanup(self):
        self.cleanup_called = True


class FakeSite:
    instances = []

    def __init__(self, runner, host, port, **kwargs):
        self.runner = runner
        self.host = host
        self.port = port
        self.kwargs = kwargs
        self.started = False
        self.instances.append(self)

    async def start(self):
        self.started = True


class ListenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_runner_serves_http_and_https_and_cleans_up(self):
        FakeRunner.instances.clear()
        FakeSite.instances.clear()
        loop = FakeLoop()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("bridge.__main__.load_identity", return_value=BridgeIdentity("id", "Test")), \
                mock.patch("bridge.__main__.prepare_tls", return_value=TLSState(object(), "ab" * 32)), \
                mock.patch("bridge.__main__.create_app", return_value=object()), \
                mock.patch("bridge.__main__.web.AppRunner", FakeRunner), \
                mock.patch("bridge.__main__.web.TCPSite", FakeSite), \
                mock.patch("bridge.__main__.asyncio.Event", ImmediateEvent), \
                mock.patch("bridge.__main__.asyncio.get_running_loop", return_value=loop):
            await serve(Config(data_dir=Path(tmp), port=8080, https_port=8443))
        self.assertEqual(len(FakeRunner.instances), 1)
        self.assertTrue(FakeRunner.instances[0].setup_called)
        self.assertTrue(FakeRunner.instances[0].cleanup_called)
        self.assertEqual([site.port for site in FakeSite.instances], [8080, 8443])
        self.assertTrue(all(site.started for site in FakeSite.instances))
        self.assertEqual(len(loop.added), 2)
        self.assertEqual(len(loop.removed), 2)


class ForeignDataDirectoryTests(unittest.TestCase):
    """Kubernetes volume: root owns `/data`; the bridge may write inside but cannot change its permissions."""

    def test_start_does_not_touch_data_directory_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            real_chmod = os.chmod

            def guarded(path, mode, *args, **kwargs):
                if Path(path) == data:
                    raise PermissionError(1, "Operation not permitted", str(path))
                return real_chmod(path, mode, *args, **kwargs)

            config = Config(bridge_token=None, data_dir=data, mdns=False)
            with mock.patch("os.chmod", side_effect=guarded):
                identity = load_identity(config.auth_dir / "bridge.json", config.bridge_name)
                tls_state = prepare_tls(config, identity)
                create_app(config, identity=identity, tls_state=tls_state, start_background=False)
            self.assertTrue((data / "auth" / "bridge.json").exists())
            self.assertEqual(os.stat(data / "auth").st_mode & 0o777, 0o700)
