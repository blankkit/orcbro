import socket
import unittest
from unittest import mock

from bridge.announce import settings
from bridge.config import ConfigError
from bridge.mdns import MDNSAdvertiser


class AnnounceTests(unittest.IsolatedAsyncioTestCase):
    def test_settings_require_https_url(self):
        identity, url, port = settings({"BRIDGE_PUBLIC_URL": "https://orcbro.example.com/", "BRIDGE_NAME": "Werkstatt"})
        self.assertEqual((identity.name, url, port), ("Werkstatt", "https://orcbro.example.com", 443))
        self.assertEqual(identity.id, settings({"BRIDGE_PUBLIC_URL": "https://orcbro.example.com"})[0].id)
        self.assertEqual(settings({"BRIDGE_PUBLIC_URL": "https://h.example:8443"})[2], 8443)
        for bad in ("", "http://orcbro.example.com", "https://", "https://h.example/pfad"):
            with self.assertRaises(ConfigError):
                settings({"BRIDGE_PUBLIC_URL": bad})

    async def test_announces_url(self):
        registered = []

        class Fake:
            def __init__(self, **_kwargs):
                pass

            async def async_register_service(self, info, allow_name_change=False):
                registered.append(info)

        identity, url, port = settings({"BRIDGE_PUBLIC_URL": "https://orcbro.example.com"})
        advertiser = MDNSAdvertiser(identity=identity, port=port, fingerprint=None, tls_enabled=False, public_url=url,
                                    zeroconf_factory=Fake)
        with mock.patch("bridge.mdns._addresses", return_value=[socket.inet_aton("192.0.2.10")]):
            await advertiser.start()
        self.assertEqual(registered[0].properties[b"url"], b"https://orcbro.example.com")
        self.assertEqual(registered[0].port, 443)
