import asyncio
import datetime
import socket
import ssl
import tempfile
import unittest
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from bridge.trust import CertificateChanged, PrinterTrust, fingerprint

SERIAL = "01P00A000000000"


def make_server_context(cn: str) -> tuple[ssl.SSLContext, bytes]:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
            .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "BBL CA")]))
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=30))
            .sign(key, hashes.SHA256()))
    folder = Path(tempfile.mkdtemp())
    (folder / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (folder / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(folder / "cert.pem", folder / "key.pem")
    return context, cert.public_bytes(serialization.Encoding.DER)


class TrustTests(unittest.IsolatedAsyncioTestCase):
    async def serve(self, cn: str):
        context, der = make_server_context(cn)
        received = []

        async def handle(reader, writer):
            received.append(await reader.read(100))
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
        self.addAsyncCleanup(self.stop, server)
        return server.sockets[0].getsockname()[1], der, received

    @staticmethod
    async def stop(server):
        server.close()
        await server.wait_closed()

    async def test_first_contact_pins_then_mismatch_blocks_before_sending(self):
        port, der, received = await self.serve(SERIAL)
        remembered = []
        trust = PrinterTrust(SERIAL, None, remembered.append)
        _reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=trust.context())
        writer.write(b"zugangscode")
        await writer.drain()
        writer.close()
        self.assertEqual(remembered, [fingerprint(der)])
        self.assertEqual(trust.pin, fingerprint(der))

        other_port, _, other_received = await self.serve(SERIAL)  # Same serial number, different certificate.
        with self.assertRaises((CertificateChanged, ssl.SSLError)):
            await asyncio.open_connection("127.0.0.1", other_port, ssl=trust.context())
        await asyncio.sleep(0.05)
        self.assertNotIn(b"zugangscode", b"".join(other_received))

    async def test_first_contact_requires_matching_serial(self):
        port, _, _ = await self.serve("01P00A999999999")
        trust = PrinterTrust(SERIAL)
        with self.assertRaises((CertificateChanged, ssl.SSLError)):
            await asyncio.open_connection("127.0.0.1", port, ssl=trust.context())
        self.assertIsNone(trust.pin)

    async def test_blocking_sockets_are_checked_too(self):
        port, der, _ = await self.serve(SERIAL)

        def connect(pin):
            trust = PrinterTrust(SERIAL, pin)
            with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
                with trust.context().wrap_socket(raw) as tls:
                    return tls.version()

        self.assertIsNotNone(await asyncio.to_thread(connect, fingerprint(der)))
        with self.assertRaises((CertificateChanged, ssl.SSLError)):
            await asyncio.to_thread(connect, "00" * 32)

    async def test_resumed_session_keeps_certificate_for_the_check(self):
        """FTPS data channel: a resumed control-channel session must still undergo certificate verification."""
        port, der, _ = await self.serve(SERIAL)

        def two_connections():
            trust = PrinterTrust(SERIAL, fingerprint(der))
            context = trust.context()
            with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
                with context.wrap_socket(raw) as first:
                    first.sendall(b"x")
                    session = first.session
            with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
                with context.wrap_socket(raw, session=session) as second:
                    return second.session_reused

        self.assertTrue(await asyncio.to_thread(two_connections))
