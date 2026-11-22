"""Probe a printer before setup: reachable, Bambu identity, access code and model.

Users need not enter a serial number: the MQTT TLS certificate on port 8883 carries it as the
Common Name, issued by BBL CA (BBL Device CA on older printers)."""
from __future__ import annotations

from .i18n import m, message_of

import asyncio
import ipaddress
import json
import re
import secrets
import socket
import ssl
import time

import aiomqtt
from cryptography import x509
from cryptography.x509.oid import NameOID

from .printer import login_refused
from .state import normalize
from .trust import PrinterTrust, base_context, fingerprint

SERIAL = re.compile(r"^[0-9A-Z]{10,20}$")


class ProbeError(Exception):
    """Setup failure: machine-readable `code` for the app and a readable message."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _tls() -> ssl.SSLContext:
    # Self-signed certificate without a matching hostname; P2S 01.02 does not answer TLS 1.3 (trust.py).
    return base_context()


def serial_from_certificate(der: bytes) -> str:
    """Printer certificate serial number; raise `ProbeError` if not a Bambu certificate."""
    try:
        cert = x509.load_der_x509_certificate(der)
    except ValueError as exc:
        raise ProbeError("not_bambu", m('The device did not provide a readable certificate. Is it a Bambu printer?')) from exc
    issuer = " ".join(a.value for a in cert.issuer if isinstance(a.value, str))
    names = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    serial = str(names[0].value).strip().upper() if names else ""
    if "BBL" not in issuer or not SERIAL.match(serial):
        raise ProbeError("not_bambu", m('The device at this address is not a Bambu printer (or LAN Only mode is off).'))
    return serial


async def read_serial(host: str, timeout: float = 8) -> str:
    return (await read_identity(host, timeout))[0]


async def read_identity(host: str, timeout: float = 8) -> tuple[str, str]:
    """Certificate serial number and fingerprint, without login."""
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, 8883, ssl=_tls()), timeout)
    except (OSError, TimeoutError, ssl.SSLError) as exc:
        raise ProbeError("unreachable", m('No printer is responding at {p0}. Check the IP address: The printer must be on and on the same network, with LAN Only mode enabled.', p0=host)) from exc
    try:
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
    finally:
        writer.close()
    if not der:
        raise ProbeError("not_bambu", m('The device at this address did not provide a certificate.'))
    return serial_from_certificate(der), fingerprint(der)


def local_address(address: str) -> bool:
    """Printer addresses the app may probe: private IPv4, unique local IPv6 and link-local addresses.
    Loopback, public, multicast and unspecified addresses are excluded, so a paired device cannot use the bridge
    to test TLS ports elsewhere."""
    try:
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if parsed.version == 6 and parsed.ipv4_mapped:
        parsed = parsed.ipv4_mapped
    if parsed.is_loopback or parsed.is_multicast or parsed.is_unspecified or parsed.is_reserved:
        return False
    return parsed.is_link_local or parsed.is_private


async def resolve_local(host: str) -> str:
    """Resolve `host` once and return a local address to connect to (see `local_address`). Connecting to that
    address, not the name, keeps a second DNS answer from redirecting the connection."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, 8883, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise ProbeError("unreachable", m('No printer is responding at {p0}. Check the IP address: The printer must be on and on the same network, with LAN Only mode enabled.', p0=host)) from exc
    for _family, _type, _proto, _name, sockaddr in infos:
        if local_address(str(sockaddr[0])):
            return str(sockaddr[0])
    raise ProbeError("not_local", m('{p0} is not an address on the local network. Enter the printer’s local IP address (for example 192.168.x.x).', p0=host))


def _mqtt_error(exc: Exception, host: str) -> ProbeError:
    text = message_of(exc)
    if login_refused(text):
        return ProbeError("access_code", m('Incorrect access code. Find it on the printer under Settings → WLAN (or Network). It changes when the printer switches Wi-Fi networks.'))
    return ProbeError("unreachable", m('Could not connect to {p0}: {p1}', p0=host, p1=text or exc.__class__.__name__))


async def probe(host: str, access_code: str, serial: str | None = None, timeout: float = 12, *,
                pin: str | None = None) -> dict:
    """Log in and query version and status without changing printer settings.

    Only local addresses are probed (`resolve_local`). Before login, the certificate must match the expected
    `serial` and optional saved `pin`. Bind login to that certificate and return its fingerprint with the result."""
    address = await resolve_local(host)
    found, seen = await read_identity(address)
    expected = (serial or "").strip().upper()
    if expected and found != expected:
        raise ProbeError("other_printer", m('A different printer is responding at this address.'))
    if pin and seen != pin.lower():
        raise ProbeError("certificate_changed", m('A device with a different certificate is responding at this address. The bridge will not send the saved access code; enter the access code again for a new address.'))
    serial = found
    trust = PrinterTrust(serial, seen)
    version: dict = {}
    raw: dict = {}
    try:
        async with aiomqtt.Client(hostname=address, port=8883, username="bblp", password=access_code,
                                  tls_context=trust.context(), identifier=f"orcbro-probe-{secrets.token_hex(4)}",
                                  keepalive=15, timeout=timeout) as client:
            await client.subscribe(f"device/{serial}/report")
            await client.publish(f"device/{serial}/request", json.dumps(
                {"info": {"sequence_id": "1", "command": "get_version"}}))
            await client.publish(f"device/{serial}/request", json.dumps(
                {"pushing": {"sequence_id": "2", "command": "pushall"}}))
            async with asyncio.timeout(timeout):
                async for message in client.messages:
                    try:
                        payload = json.loads(message.payload)
                    except (TypeError, ValueError):
                        continue
                    info, report = payload.get("info"), payload.get("print")
                    if isinstance(info, dict) and info.get("command") == "get_version":
                        version = info
                    if isinstance(report, dict) and report.get("command") == "push_status":
                        raw.update(report)
                    # Version plus full status with nozzle data is enough; do not wait until the deadline.
                    if version and "nozzle_diameter" in raw:
                        break
    except TimeoutError:
        if not version and not raw:
            raise ProbeError("timeout", m('The printer accepted the login but did not respond. Is the serial number correct?')) from None
    except aiomqtt.MqttError as exc:
        raise _mqtt_error(exc, host) from exc
    ota = next((m for m in version.get("module", []) if m.get("name") == "ota"), {})
    project = next((m.get("project_name") for m in version.get("module", []) if m.get("project_name")), None)
    status = normalize(raw, connected=True, last_message_at=time.time(), printer={})
    return {
        "serial": serial,
        "fingerprint": seen,
        "host": host,
        "model": ota.get("product_name") or raw.get("printer_type") or project or None,
        "firmware": ota.get("sw_ver") or None,
        "nozzle_diameter": status["nozzle"]["diameter"],
        "developer_mode": status["connection"]["developer_mode"],
    }
