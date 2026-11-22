"""Bridge TLS certificate and fingerprint pinned by the app."""
from __future__ import annotations

from .i18n import m

import datetime
import hashlib
import os
import ssl
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .config import Config
from .identity import BridgeIdentity


class TLSError(Exception):
    pass


@dataclass(frozen=True)
class TLSState:
    context: ssl.SSLContext | None
    fingerprint: str | None

    @property
    def enabled(self) -> bool:
        return self.context is not None


def certificate_fingerprint(path: Path) -> str:
    try:
        certificate = x509.load_pem_x509_certificate(Path(path).read_bytes())
    except (OSError, ValueError) as exc:
        raise TLSError(m('Cannot read TLS certificate {p0}: {p1}', p0=path, p1=exc)) from exc
    return hashlib.sha256(certificate.public_bytes(serialization.Encoding.DER)).hexdigest()


def _generate(cert_path: Path, key_path: Path, identity: BridgeIdentity) -> None:
    directory = cert_path.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    private_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, identity.name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(private_key, hashes.SHA256())
    )
    _write_private(key_path, private_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    _write_private(cert_path, certificate.public_bytes(serialization.Encoding.PEM))


def _write_private(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_tls(config: Config, identity: BridgeIdentity) -> TLSState:
    if not config.tls:
        return TLSState(None, None)
    if config.tls_cert and config.tls_key:
        cert_path, key_path = config.tls_cert, config.tls_key
    else:
        directory = config.data_dir / "tls"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        cert_path, key_path = directory / "bridge.crt", directory / "bridge.key"
        if cert_path.exists() != key_path.exists():
            raise TLSError(m('TLS files at {p0} are incomplete.', p0=directory))
        if not cert_path.exists():
            try:
                _generate(cert_path, key_path, identity)
            except OSError as exc:
                raise TLSError(m('Could not create TLS certificate: {p0}', p0=exc)) from exc
        else:
            os.chmod(cert_path, 0o600)
            os.chmod(key_path, 0o600)
    fingerprint = certificate_fingerprint(cert_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(cert_path, key_path)
    except (OSError, ssl.SSLError) as exc:
        raise TLSError(m('TLS certificate and key do not match: {p0}', p0=exc)) from exc
    return TLSState(context, fingerprint)
