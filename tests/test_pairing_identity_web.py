import os
import tempfile
import unittest
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec

from bridge.config import Config, ConfigError
from bridge.identity import BridgeIdentity, IdentityError, load_identity
from bridge.tls import certificate_fingerprint, prepare_tls
from bridge.web import PublicOrigin, pairing_url, public_origin, setup_page


class ConfigAndIdentityTests(unittest.TestCase):
    def test_config_allows_no_key_and_print_url_override_only(self):
        config = Config.from_env({})
        self.assertIsNone(config.bridge_token)
        self.assertIsNone(config.print_url)
        with self.assertRaises(ConfigError):
            Config.from_env({"BRIDGE_ACCESS_TOKEN": "kurz"})
        with self.assertRaises(ConfigError):
            Config.from_env({"BAMBU_PRINT_URL": "ftp:///fest"})
        config = Config.from_env({"BAMBU_PRINT_URL": "ftp:///{name}", "BRIDGE_TLS": "off",
                                  "BRIDGE_TRUSTED_PROXIES": "10.42.0.0/16", "BRIDGE_MDNS": "off"})
        self.assertEqual(config.print_url, "ftp:///{name}")
        self.assertFalse(config.tls)
        self.assertFalse(config.mdns)
        self.assertFalse(config.plain_http)

    def test_tls_off_needs_trusted_proxies_or_explicit_plain_http(self):
        with self.assertRaises(ConfigError) as refused:
            Config.from_env({"BRIDGE_TLS": "off", "BRIDGE_PUBLIC_URL": "https://orcbro.example.com"})
        self.assertIn("BRIDGE_TRUSTED_PROXIES", str(refused.exception))
        with self.assertRaises(ConfigError):
            Config.from_env({"BRIDGE_TLS": "off", "BRIDGE_PLAIN_HTTP": "yes"})
        self.assertTrue(Config.from_env({"BRIDGE_TLS": "off", "BRIDGE_PLAIN_HTTP": "on"}).plain_http)
        # The owner's deployment: TLS behind Traefik with a fixed key and serial number.
        config = Config.from_env({"BRIDGE_TLS": "off", "BRIDGE_TRUSTED_PROXIES": "10.42.0.0/16",
                                  "BRIDGE_PUBLIC_URL": "https://orcbro.example.com",
                                  "BRIDGE_ACCESS_TOKEN": "s3cret-bridge-access-token", "BAMBU_HOST": "192.0.2.10",
                                  "BAMBU_ACCESS_CODE": "12345678", "BAMBU_SERIAL": "01p00a000000000"})
        self.assertEqual((config.trusted_proxies, config.serial), (("10.42.0.0/16",), "01P00A000000000"))
        self.assertTrue(Config.from_env({}).tls)

    def test_identity_is_reused_and_override_is_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bridge.json"
            first = load_identity(path)
            second = load_identity(path, "Werkstatt")
            third = load_identity(path)
            self.assertEqual(first.id, second.id)
            self.assertEqual((second.name, third.name), ("Werkstatt", "Werkstatt"))
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_corrupt_identity_is_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bridge.json"
            path.write_text('{"schema_version":1,"id":"falsch","name":"Bridge"}')
            with self.assertRaises(IdentityError):
                load_identity(path)


class TLSTests(unittest.TestCase):
    def test_certificate_is_p256_reused_and_has_der_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=Path(tmp))
            identity = BridgeIdentity("bd92e1e8-c87d-46ee-a2c5-4d859adc8597", "Test")
            first = prepare_tls(config, identity)
            cert_path = Path(tmp) / "tls" / "bridge.crt"
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            self.assertIsInstance(cert.public_key().curve, ec.SECP256R1)
            self.assertEqual(first.fingerprint, certificate_fingerprint(cert_path))
            self.assertRegex(first.fingerprint, r"^[0-9a-f]{64}$")
            before = cert_path.read_bytes()
            second = prepare_tls(config, identity)
            self.assertEqual(second.fingerprint, first.fingerprint)
            self.assertEqual(cert_path.read_bytes(), before)
            self.assertEqual(os.stat(Path(tmp) / "tls").st_mode & 0o777, 0o700)

    def test_tls_off_has_no_fingerprint(self):
        state = prepare_tls(Config(tls=False), BridgeIdentity("id", "Test"))
        self.assertIsNone(state.context)
        self.assertIsNone(state.fingerprint)


class WebTests(unittest.TestCase):
    def test_direct_ipv4_ipv6_public_and_proxy_origins(self):
        direct = public_origin(request_host="bridge.local:8080", peer="192.0.2.4", forwarded_proto=None,
                               public_url=None, trusted_proxies=(), tls_enabled=True, https_port=8443)
        self.assertEqual(direct, PublicOrigin("https://bridge.local:8443", True))
        ipv6 = public_origin(request_host="[2001:db8::2]:8080", peer="2001:db8::1", forwarded_proto=None,
                             public_url=None, trusted_proxies=(), tls_enabled=True, https_port=8443)
        self.assertEqual(ipv6.url, "https://[2001:db8::2]:8443")
        proxy = public_origin(request_host="bridge.example:443", peer="192.0.2.4", forwarded_proto="https",
                              public_url=None, trusted_proxies=("192.0.2.0/24",), tls_enabled=True, https_port=8443)
        self.assertEqual(proxy, PublicOrigin("https://bridge.example", False))
        forged = public_origin(request_host="bridge.local:8080", peer="198.51.100.2", forwarded_proto="https",
                               public_url=None, trusted_proxies=("192.0.2.0/24",), tls_enabled=True, https_port=8443)
        self.assertEqual(forged.url, "https://bridge.local:8443")
        public = public_origin(request_host="ignoriert", peer=None, forwarded_proto=None,
                               public_url="https://bridge.example/base", trusted_proxies=(),
                               tls_enabled=False, https_port=8443)
        self.assertEqual(public, PublicOrigin("https://bridge.example/base", False))

    def test_no_tls_has_no_broken_origin(self):
        self.assertIsNone(public_origin(request_host="bridge.local", peer=None, forwarded_proto=None,
                                        public_url=None, trusted_proxies=(), tls_enabled=False, https_port=8443))

    def test_pairing_url_encodes_values_and_fingerprint_policy(self):
        url = pairing_url(PublicOrigin("https://[2001:db8::2]:8443", True), "ABCDEFGH", "Brücke & Druck", "ab" * 32)
        self.assertIn("url=https%3A%2F%2F%5B2001%3Adb8%3A%3A2%5D%3A8443", url)
        self.assertIn("code=ABCD-EFGH", url)
        self.assertIn("fp=" + "ab" * 32, url)

    def test_page_escapes_name_and_fixed_key_never_shows_code(self):
        identity = BridgeIdentity("id", "<Bridge>")
        page = setup_page(identity=identity, origin=PublicOrigin("https://bridge", True), fixed_key=True,
                          paired=False, setup_minutes=None)
        self.assertNotIn("<Bridge>", page)
        self.assertIn("&lt;Bridge&gt;", page)
        self.assertIn('fixed access key', page)

    def test_setup_page_states(self):
        identity = BridgeIdentity("id", "Bridge")
        origin = PublicOrigin("https://bridge.local:8443", True)
        plain = setup_page(identity=identity, origin=origin, fixed_key=False, paired=False, setup_minutes=42)
        self.assertIn("bridge log", plain)
        self.assertIn("42 more minutes", plain)
        self.assertIn('href="https://bridge.local:8443/"', plain)
        self.assertNotIn("<form", plain)
        self.assertNotIn("fp=", plain)
        secure = setup_page(identity=identity, origin=origin, fixed_key=False, paired=False, setup_minutes=42,
                            secure=True)
        self.assertIn('action="/setup"', secure)
        self.assertNotIn("<svg", secure)
        closed = setup_page(identity=identity, origin=origin, fixed_key=False, paired=False, setup_minutes=None)
        self.assertIn("Restart the bridge", closed)
        self.assertNotIn("<form", closed)
        paired = setup_page(identity=identity, origin=origin, fixed_key=False, paired=True, setup_minutes=None)
        self.assertIn("Pair Another Device", paired)
