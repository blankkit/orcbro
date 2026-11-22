import ssl
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from bridge import __version__
from bridge.config import Config
from bridge.identity import BridgeIdentity
from bridge.server import AUTH, app_token, create_app
from bridge.tls import TLSState
from helpers import FILAMENT_IDS, MODELS, PROFILES


class PairingEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(data_dir=Path(self.tmp.name), profiles_dir=PROFILES, filament_ids=FILAMENT_IDS,
                             models_dir=MODELS, orca_cmd=(sys.executable, "-c", "pass"), tls=False, mdns=False)
        app = create_app(self.config, identity=BridgeIdentity("bd92e1e8-c87d-46ee-a2c5-4d859adc8597", "Test"),
                         tls_state=TLSState(None, None), start_background=False)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_pair_manage_and_self_delete(self):
        code = self.client.server.app[AUTH].setup_code
        response = await self.client.post("/api/pair", json={"code": code, "device_name": "Telefon"})
        paired = await response.json()
        self.assertEqual(response.status, 201)
        headers = {"Authorization": f"Bearer {paired['app_token']}"}
        listed = await (await self.client.get("/api/devices", headers=headers)).json()
        self.assertEqual(listed["authentication"], "device")
        self.assertTrue(listed["devices"][0]["current"])
        invitation = await self.client.post("/api/pairing", headers=headers)
        self.assertEqual(invitation.status, 201)
        deleted = await self.client.delete(f"/api/devices/{paired['device_id']}", headers=headers)
        self.assertEqual(deleted.status, 204)

    async def test_fixed_key_keeps_legacy_response_and_hides_code(self):
        await self.client.close()
        config = Config(bridge_token="s3cret-bridge-access-token", data_dir=Path(self.tmp.name),
                        profiles_dir=PROFILES, filament_ids=FILAMENT_IDS, models_dir=MODELS, mdns=False)
        app = create_app(config, start_background=False)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        response = await self.client.post("/api/pair", json={"token": config.bridge_token})
        self.assertEqual(await response.json(), {"app_token": app_token(config.bridge_token), "bridge_version": __version__})
        page = await (await self.client.get("/")).text()
        self.assertIn('fixed access key', page)
        self.assertIsNone(app[AUTH].setup_code)

    async def test_web_invitation_with_fixed_key_over_https_only(self):
        await self.client.close()
        config = Config(bridge_token="s3cret-bridge-access-token", data_dir=Path(self.tmp.name),
                        profiles_dir=PROFILES, filament_ids=FILAMENT_IDS, models_dir=MODELS, mdns=False, tls=False,
                        public_url="https://orcbro.example.com", trusted_proxies=("127.0.0.0/8",))
        app = create_app(config, start_background=False)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        # HTTPS through the trusted proxy.
        public = {"Host": "orcbro.example.com", "X-Forwarded-Proto": "https"}
        # Direct HTTP shows an HTTPS link, without a form; the Host header alone is insufficient.
        for headers in ({}, {"Host": "orcbro.example.com"}):
            plain = await (await self.client.get("/", headers=headers)).text()
            self.assertNotIn("name=\"key\"", plain)
            self.assertIn("https://orcbro.example.com", plain)
            refused = await self.client.post("/invite", data={"key": config.bridge_token}, headers=headers)
            self.assertEqual(refused.status, 403)
        # Public HTTPS supports the form, incorrect-key feedback, then a QR invitation.
        page = await (await self.client.get("/", headers=public)).text()
        self.assertIn("name=\"key\"", page)
        wrong = await self.client.post("/invite", data={"key": "falsch"}, headers=public)
        self.assertEqual(wrong.status, 401)
        self.assertIn('Incorrect access key', await wrong.text())
        response = await self.client.post("/invite", data={"key": config.bridge_token}, headers=public)
        body = await response.text()
        self.assertEqual(response.status, 200)
        self.assertIn("bambro://pair?url=https%3A%2F%2Forcbro.example.com", body)
        self.assertIn("<svg", body)
        self.assertNotIn(config.bridge_token, body)
        invitation = next(iter(app[AUTH].invitations))
        paired = await self.client.post("/api/pair", json={"code": invitation, "device_name": "iPhone"}, headers=public)
        self.assertEqual(paired.status, 201)

    async def test_setup_page_shows_neither_code_nor_printers(self):
        from types import SimpleNamespace
        from bridge.server import DEVICES
        printer = SimpleNamespace(status=lambda: {"connection": {"connected": True}})
        device = SimpleNamespace(name="Werkstatt", model="Bambu Lab P1S", printer=printer)
        code = self.client.server.app[AUTH].setup_code
        with unittest.mock.patch.object(self.client.server.app[DEVICES], "list", return_value=[device]):
            response = await self.client.get("/")
            page = await response.text()
        self.assertEqual(response.status, 200)
        self.assertNotIn("Werkstatt", page)
        self.assertNotIn(code[:4], page)
        self.assertIn("bridge log", page)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    """Built-in TLS blocks HTTP API access; reverse-proxy mode accepts only trusted proxy peers."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def start(self, **kwargs):
        config = Config(bridge_token="s3cret-bridge-access-token", data_dir=Path(self.tmp.name), profiles_dir=PROFILES,
                        filament_ids=FILAMENT_IDS, models_dir=MODELS, mdns=False, **kwargs)
        tls = TLSState(ssl.create_default_context(), "ab" * 32) if kwargs.get("tls", True) else TLSState(None, None)
        app = create_app(config, identity=BridgeIdentity("bd92e1e8-c87d-46ee-a2c5-4d859adc8597", "Test"),
                         tls_state=tls, start_background=False)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        return {"Authorization": f"Bearer {app_token(config.bridge_token)}"}

    async def test_http_port_with_own_tls_serves_only_the_setup_page(self):
        auth = await self.start(tls=True)
        self.assertEqual((await self.client.get("/")).status, 200)
        self.assertEqual((await self.client.get("/healthz")).status, 200)
        api = await self.client.get("/api/printers", headers=auth)
        self.assertEqual(api.status, 403)
        self.assertIn("HTTPS", (await api.json())["error"])
        self.assertEqual((await self.client.post("/api/pair", json={"token": "x"})).status, 403)

    async def test_reverse_proxy_mode_accepts_api_only_from_trusted_proxies_over_https(self):
        https = {"X-Forwarded-Proto": "https"}
        auth = await self.start(tls=False, trusted_proxies=("192.0.2.0/24",))
        self.assertEqual((await self.client.get("/api/printers", headers={**auth, **https})).status, 403)
        await self.client.close()
        auth = await self.start(tls=False, trusted_proxies=("127.0.0.0/8",))
        self.assertEqual((await self.client.get("/api/printers", headers={**auth, **https})).status, 200)
        # The trusted proxy itself forwarding plain HTTP (or nothing) is refused too.
        for headers in ({}, {"X-Forwarded-Proto": "http"}):
            response = await self.client.get("/api/printers", headers={**auth, **headers})
            self.assertEqual(response.status, 403)
            self.assertIn("HTTPS", (await response.json())["error"])
        self.assertEqual((await self.client.post("/api/pair", json={"token": "x"})).status, 403)
