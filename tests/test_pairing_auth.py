import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from bridge.auth import (AuthError, InvalidCode, PairingStore, Principal, RateLimited,
                         legacy_app_token, normalize_code)


class PairingStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "auth.json"
        self.now = 1_795_251_600.0

    def tearDown(self):
        self.tmp.cleanup()

    def store(self, secret=None):
        async def no_wait(_seconds):
            return None
        return PairingStore(self.path, secret, clock=lambda: self.now, sleep=no_wait)

    def test_legacy_vector_and_no_empty_secret(self):
        self.assertEqual(legacy_app_token("s3cret-bridge-access-token"),
                         "a9057d904d4cde92ee1ef9775d37b07448fff73b4476ea7a722c7c7b14bd6486")
        self.assertIsNone(self.store().authenticate(legacy_app_token("")))

    def test_fixed_key_suppresses_setup_code(self):
        self.assertIsNone(self.store("s3cret-bridge-access-token").setup_code)
        self.assertIsNotNone(self.store().setup_code)

    async def test_code_normalization_and_one_time_pairing(self):
        store = self.store()
        shown = f"{store.setup_code[:4]}-{store.setup_code[4:]}".lower()
        token, device = await store.pair(shown, " Mein iPhone ")
        self.assertEqual(device.name, "Mein iPhone")
        self.assertIsNone(store.setup_code)
        principal = store.authenticate(token)
        self.assertEqual(principal, Principal("device", device.id))
        with self.assertRaises(InvalidCode):
            await store.pair(shown, "Noch eins")
        saved = self.path.read_text(encoding="utf-8")
        self.assertNotIn(token, saved)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertEqual(normalize_code(shown), shown.replace("-", "").upper())

    async def test_same_code_cannot_pair_concurrently(self):
        store = self.store()
        code = store.setup_code

        async def attempt(name):
            try:
                await store.pair(code, name)
                return "ok"
            except InvalidCode:
                return "invalid"

        self.assertCountEqual(await asyncio.gather(attempt("Eins"), attempt("Zwei")), ["ok", "invalid"])
        self.assertEqual(len(store.devices), 1)

    async def test_tenth_parallel_failure_blocks_without_extending(self):
        store = self.store()

        async def wrong():
            try:
                await store.pair("AAAA-AAAA", "Telefon")
                return "ok"
            except InvalidCode:
                return "invalid"
            except RateLimited as exc:
                return exc.retry_after

        results = await asyncio.gather(*(wrong() for _ in range(10)))
        self.assertEqual(results.count("invalid"), 9)
        self.assertEqual(results.count(600), 1)
        self.now += 100
        with self.assertRaises(RateLimited) as blocked:
            await store.pair(store.setup_code, "Richtig")
        self.assertEqual(blocked.exception.retry_after, 500)
        self.now += 500
        token, _device = await store.pair(store.setup_code, "Richtig")
        self.assertTrue(token)

    async def test_invitation_expiry_and_creator_deletion(self):
        store = self.store()
        first_token, first = await store.pair(store.setup_code, "Erstes")
        principal = store.authenticate(first_token)
        invitation = await store.invite(principal)
        self.now = invitation.expires_at
        with self.assertRaises(InvalidCode):
            await store.pair(invitation.code, "Zu spät")
        self.now += 1
        invitation = await store.invite(principal)
        self.assertTrue(await store.remove(first.id))
        with self.assertRaises(InvalidCode):
            await store.pair(invitation.code, "Ohne Einlader")
        # Removing the last device does not reopen pairing; only a restart issues a new setup code.
        self.assertIsNone(store.setup_code)
        self.assertFalse(store.setup_open)
        self.assertIsNotNone(self.store().setup_code)

    async def test_legacy_principal_can_invite_and_has_no_current_device(self):
        secret = "s3cret-bridge-access-token"
        store = self.store(secret)
        principal = store.authenticate(legacy_app_token(secret))
        invitation = await store.invite(principal)
        token, device = await store.pair(invitation.code, "Telefon")
        self.assertEqual(store.authenticate(token).device_id, device.id)
        self.assertNotIn("current", store.list_devices(principal)[0])

    async def test_corrupt_file_is_fatal(self):
        self.path.write_text("{kaputt", encoding="utf-8")
        with self.assertRaises(AuthError):
            self.store()

    async def test_last_seen_is_persisted_at_most_once_per_minute_and_on_close(self):
        store = self.store()
        token, _device = await store.pair(store.setup_code, "Telefon")
        writes = 0
        original = store._save

        def counted():
            nonlocal writes
            writes += 1
            original()

        store._save = counted
        store.authenticate(token)
        self.now += 30
        store.authenticate(token)
        self.assertEqual(writes, 0)
        await store.close()
        self.assertEqual(writes, 1)

    async def test_storage_failure_does_not_consume_code(self):
        store = self.store()
        code = store.setup_code

        def failed():
            raise AuthError("voll")

        store._save = failed
        with self.assertRaises(AuthError):
            await store.pair(code, "Telefon")
        self.assertEqual(store.setup_code, code)
        self.assertEqual(store.devices, [])

    async def test_restart_keeps_devices_but_not_invitations(self):
        store = self.store()
        token, device = await store.pair(store.setup_code, "Telefon")
        await store.invite(store.authenticate(token))
        again = self.store()
        self.assertEqual([entry.id for entry in again.devices], [device.id])
        self.assertEqual(again.invitations, {})
        self.assertIsNone(again.setup_code)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], 1)

    async def test_removal_cancels_active_connection(self):
        store = self.store()
        token, device = await store.pair(store.setup_code, "Telefon")
        started = asyncio.Event()

        async def connection():
            principal = store.authenticate(token)
            store.track(principal)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                store.untrack(principal)

        task = asyncio.create_task(connection())
        await started.wait()
        self.assertTrue(await store.remove(device.id))
        with self.assertRaises(asyncio.CancelledError):
            await task
