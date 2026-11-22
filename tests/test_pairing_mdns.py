import asyncio
import socket
import unittest
from unittest import mock

from bridge.identity import BridgeIdentity
from bridge.mdns import MDNSAdvertiser


class FakeZeroconf:
    def __init__(self, **_kwargs):
        self.registered = None
        self.unregistered = None
        self.closed = False

    async def async_register_service(self, info, allow_name_change=False):
        self.registered = (info, allow_name_change)
        if allow_name_change:
            info.name = "Test-2._orcbro._tcp.local."

    async def async_unregister_service(self, info):
        self.unregistered = info

    async def async_close(self):
        self.closed = True


class MDNSTests(unittest.IsolatedAsyncioTestCase):
    async def test_registers_direct_listener_and_unregisters(self):
        created = []

        def factory(**kwargs):
            instance = FakeZeroconf(**kwargs)
            created.append(instance)
            return instance

        identity = BridgeIdentity("bd92e1e8-c87d-46ee-a2c5-4d859adc8597", "Test")
        advertiser = MDNSAdvertiser(identity=identity, port=8443, fingerprint="ab" * 32,
                                    tls_enabled=True, zeroconf_factory=factory)
        with mock.patch("bridge.mdns._addresses", return_value=[socket.inet_aton("192.0.2.10")]):
            await advertiser.start()
        info, allow_change = created[0].registered
        self.assertTrue(allow_change)
        self.assertEqual(info.port, 8443)
        self.assertEqual(info.properties[b"id"], identity.id.encode())
        self.assertEqual(info.properties[b"fp"], ("ab" * 32).encode())
        await advertiser.stop()
        self.assertIs(created[0].unregistered, info)
        self.assertTrue(created[0].closed)

    async def test_no_address_starts_no_discovery(self):
        factory = mock.Mock()
        advertiser = MDNSAdvertiser(identity=BridgeIdentity("id", "Test"), port=8080,
                                    fingerprint=None, tls_enabled=False, zeroconf_factory=factory)
        with mock.patch("bridge.mdns._addresses", return_value=[]):
            await advertiser.start()
        factory.assert_not_called()

    async def test_registration_timeout_is_nonfatal_and_closes(self):
        instance = FakeZeroconf()

        async def slow(_info, allow_name_change=False):
            await asyncio.Event().wait()

        instance.async_register_service = slow
        advertiser = MDNSAdvertiser(identity=BridgeIdentity("id", "Test"), port=8080,
                                    fingerprint=None, tls_enabled=False, timeout=0.001,
                                    zeroconf_factory=lambda **_kwargs: instance)
        with mock.patch("bridge.mdns._addresses", return_value=[socket.inet_aton("192.0.2.10")]):
            await advertiser.start()
        self.assertTrue(instance.closed)


class PublicURLAnnouncementTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_url_is_announced_instead_of_tls_off(self):
        instance = FakeZeroconf()
        advertiser = MDNSAdvertiser(identity=BridgeIdentity("id", "Test"), port=8080, fingerprint=None,
                                    tls_enabled=False, public_url="https://orcbro.example.com",
                                    zeroconf_factory=lambda **_kwargs: instance)
        with mock.patch("bridge.mdns._addresses", return_value=[socket.inet_aton("192.0.2.10")]):
            await advertiser.start()
        info, _ = instance.registered
        self.assertEqual(info.properties[b"url"], b"https://orcbro.example.com")
        self.assertNotIn(b"tls", info.properties)
        self.assertNotIn(b"fp", info.properties)
