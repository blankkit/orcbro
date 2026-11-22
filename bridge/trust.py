"""Pin printer certificates.

Bambu printers use a private BBL CA that operating systems do not trust. Instead of validating
the chain, pin the SHA-256 certificate fingerprint during setup or first contact after an update.
Reject any other certificate before sending the access code, preventing another device at the
printer IP, or a paired device redirecting the address, from obtaining credentials.

Validation runs inside the TLS handshake: `SSLSocket` for MQTT/FTPS, `SSLObject` for asyncio
camera connections. A failed check stops the connection before credentials are transmitted."""
from __future__ import annotations

from .i18n import m

import asyncio
import hashlib
import ssl
import threading
from collections.abc import Callable

from cryptography import x509
from cryptography.x509.oid import NameOID


class CertificateChanged(ssl.SSLError):
    """The certificate differs from the pin; do not send the access code."""


CHANGED = (m('The printer certificate has changed: A different device is responding at this IP address, or the printer was replaced or reset. The bridge will not send the access code. If this is your printer, set it up in the app with its access code entered again.'))


def fingerprint(der: bytes) -> str:
    """DER certificate SHA-256, lowercase hex as in the app."""
    return hashlib.sha256(der).hexdigest()


def common_name(der: bytes) -> str | None:
    try:
        names = x509.load_der_x509_certificate(der).subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    except ValueError:
        return None
    return str(names[0].value).strip().upper() if names else None


def base_context() -> ssl.SSLContext:
    """No chain validation (private CA), at most TLS 1.2 (P2S 01.02 otherwise hangs); allow FTPS data
    connections to close without TLS shutdown."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.options |= getattr(ssl, "OP_IGNORE_UNEXPECTED_EOF", 0)
    return context


class PrinterTrust:
    """Printer fingerprint pin. Without a pin, remember the first certificate seen via `remember`,
    but only when its serial number matches the printer."""

    def __init__(self, serial: str, pin: str | None = None, remember: Callable[[str], None] | None = None):
        self.serial = (serial or "").upper()
        self.pin = pin.lower() if pin else None
        self._remember = remember
        self._lock = threading.Lock()

    def verify(self, der: bytes | None) -> str:
        if not der:
            raise CertificateChanged(m('The printer did not present a certificate.'))
        seen = fingerprint(der)
        with self._lock:
            if self.pin is None:
                if self.serial and common_name(der) != self.serial:
                    raise CertificateChanged(CHANGED)
                self.pin = seen
                if self._remember:
                    self._remember(seen)
            elif seen != self.pin:
                raise CertificateChanged(CHANGED)
        return seen

    def context(self) -> ssl.SSLContext:
        """TLS context verifying fingerprints after handshake, for blocking sockets and asyncio."""
        context = base_context()
        trust = self

        class PinnedSocket(ssl.SSLSocket):
            def do_handshake(self, *args, **kwargs):
                super().do_handshake(*args, **kwargs)
                trust.verify(self.getpeercert(binary_form=True))

        class PinnedObject(ssl.SSLObject):
            def do_handshake(self):
                super().do_handshake()
                trust.verify(self.getpeercert(binary_form=True))

        context.sslsocket_class = PinnedSocket
        context.sslobject_class = PinnedObject
        return context

    async def check(self, host: str, port: int = 8883, timeout: float = 8) -> None:
        """Handshake only, without login, before clients such as ffmpeg that cannot verify pins themselves."""
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=self.context()), timeout)
        writer.close()
