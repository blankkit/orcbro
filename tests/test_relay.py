"""Live Activity pushes through the relay for bridges without their own APNs key."""
import json
import stat
import tempfile
import unittest
from pathlib import Path

import httpx

from bridge.apns import APNsError
from bridge.branding import PUSH_RELAY_URL
from bridge.config import Config, ConfigError
from bridge.relay import RelayClient

SECRET = "s3cret-bridge-access-token"


class RelayClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.credentials = Path(self.tmp.name) / "relay.json"

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, replies, seen):
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/v1/bridges":
                return httpx.Response(201, json={"bridge": "b" * 32, "key": "k" * 43})
            return next(replies)
        return RelayClient("https://relay.example/", self.credentials, transport=httpx.MockTransport(handler))

    async def test_request_and_errors(self):
        seen = []
        replies = iter([httpx.Response(200, json={"ok": True}),
                        httpx.Response(410, json={"error": "APNs lehnt ab", "reason": "BadDeviceToken"}),
                        httpx.Response(429, json={"error": "Zu viele Anfragen"}),
                        httpx.Response(403, json={"error": "anderer Bridge", "reason": "TokenBoundElsewhere"})])
        client = self.client(replies, seen)
        payload = {"aps": {"event": "start", "timestamp": 1, "content-state": {"progress": 3},
                           "alert": {"title": 'Print Started', "body": "x"}}}
        await client.send_live_activity("ab" * 32, "production", payload, 10)
        # Register once and save the key readable only by the bridge.
        self.assertEqual(seen[0].url.path, "/v1/bridges")
        self.assertEqual(stat.S_IMODE(self.credentials.stat().st_mode), 0o600)
        self.assertEqual(str(seen[1].url), "https://relay.example/v1/live-activity")
        self.assertEqual(seen[1].headers["authorization"], f"Bearer {'b' * 32}.{'k' * 43}")
        # The relay creates its template start alert; the bridge omits the alert text.
        self.assertEqual(json.loads(seen[1].content), {"token": "ab" * 32, "environment": "production", "priority": 10, "language": "en",
                                                       "payload": {"aps": {"event": "start", "timestamp": 1,
                                                                           "content-state": {"progress": 3}}}})
        with self.assertRaises(APNsError) as gone:
            await client.send_live_activity("ab" * 32, "production", payload)
        self.assertTrue(gone.exception.token_gone)
        with self.assertRaises(APNsError) as limited:
            await client.send_live_activity("ab" * 32, "production", payload)
        self.assertFalse(limited.exception.token_gone)
        with self.assertRaises(APNsError) as bound:
            await client.send_notification("ab" * 32, "production", {"template": "finished", "job": "x", "printer": "y"})
        self.assertEqual(bound.exception.reason, "TokenBoundElsewhere")
        self.assertEqual(sum(1 for r in seen if r.url.path == "/v1/bridges"), 1)
        await client.close()

    async def test_stored_key_is_reused_and_renewed_after_401(self):
        self.credentials.write_text(json.dumps({"url": "https://relay.example", "bridge": "a" * 32, "key": "o" * 43}))
        seen = []
        replies = iter([httpx.Response(401, json={"reason": "UnknownBridge"}), httpx.Response(200, json={"ok": True})])
        client = self.client(replies, seen)
        await client.send_notification("ab" * 32, "development", {"template": "finished", "job": "x", "printer": "y"})
        self.assertEqual([r.url.path for r in seen], ["/v1/notification", "/v1/bridges", "/v1/notification"])
        self.assertTrue(seen[0].headers["authorization"].startswith("Bearer " + "a" * 32))
        self.assertTrue(seen[2].headers["authorization"].startswith("Bearer " + "b" * 32))
        self.assertEqual(json.loads(seen[2].content), {"token": "ab" * 32, "environment": "development", "language": "en",
                                                       "template": "finished", "job": "x", "printer": "y"})
        self.assertEqual(json.loads(self.credentials.read_text())["bridge"], "b" * 32)
        await client.close()

    async def test_explicit_german_language_is_forwarded(self):
        seen = []
        replies = iter([httpx.Response(200, json={"ok": True}), httpx.Response(200, json={"ok": True})])
        client = self.client(replies, seen)
        payload = {"language": "de", "aps": {"event": "start", "content-state": {"stage": "Druckt"}}}
        try:
            await client.send_live_activity("ab" * 32, "production", payload)
            await client.send_notification("ab" * 32, "production",
                                           {"language": "de", "template": "finished", "job": "part", "printer": "P1S"})
        finally:
            await client.close()
        live, notification = [json.loads(request.content) for request in seen[1:]]
        self.assertEqual(live["language"], "de")
        self.assertEqual(notification["language"], "de")
        self.assertNotIn("language", live["payload"])
        self.assertEqual(live["payload"]["aps"]["content-state"]["stage"], "Druckt")


class RelayConfigTests(unittest.TestCase):
    def test_default_override_and_off(self):
        self.assertEqual(Config.from_env({"BRIDGE_ACCESS_TOKEN": SECRET}).push_relay, PUSH_RELAY_URL)
        self.assertEqual(Config.from_env({"BRIDGE_PUSH_RELAY": "https://relay.example/"}).push_relay,
                         "https://relay.example")
        self.assertIsNone(Config.from_env({"BRIDGE_PUSH_RELAY": "off"}).push_relay)
        self.assertIsNone(Config(bridge_token=SECRET).push_relay)  # Tests and directly constructed configurations.
        with self.assertRaises(ConfigError):
            Config.from_env({"BRIDGE_PUSH_RELAY": "http://relay.example"})


if __name__ == "__main__":
    unittest.main()
