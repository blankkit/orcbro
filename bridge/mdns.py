"""mDNS announcement of the bridge's direct listener."""
from __future__ import annotations

from .i18n import m

import asyncio
import logging
import socket

from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from . import __version__
from .branding import MDNS_SERVICE_TYPE

log = logging.getLogger(__name__)


def _addresses() -> list[bytes]:
    addresses = set()
    try:
        entries = socket.getaddrinfo(socket.gethostname(), None, type=socket.SOCK_STREAM)
    except OSError:
        return []
    for family, _kind, _proto, _name, sockaddr in entries:
        try:
            if family == socket.AF_INET:
                packed = socket.inet_pton(socket.AF_INET, sockaddr[0])
            elif family == socket.AF_INET6:
                packed = socket.inet_pton(socket.AF_INET6, sockaddr[0])
            else:
                continue
            if not sockaddr[0].startswith("127.") and sockaddr[0] != "::1":
                addresses.add(packed)
        except OSError:
            continue
    return sorted(addresses)


class MDNSAdvertiser:
    def __init__(self, *, identity, port: int, fingerprint: str | None, tls_enabled: bool,
                 public_url: str | None = None, timeout: float = 5.0, zeroconf_factory=AsyncZeroconf):
        self.identity = identity
        self.public_url = public_url
        self.port = port
        self.fingerprint = fingerprint
        self.tls_enabled = tls_enabled
        self.timeout = timeout
        self._factory = zeroconf_factory
        self._zeroconf = None
        self._info = None

    async def start(self) -> None:
        addresses = _addresses()
        if not addresses:
            log.warning(m('mDNS: No local address found for announcement'))
            return
        properties = {"id": self.identity.id, "version": __version__, "api": "1"}
        if self.public_url:
            # Behind a reverse proxy, the app uses this HTTPS address instead of the listener's IP and port.
            properties["url"] = self.public_url
        elif self.tls_enabled and self.fingerprint:
            properties["fp"] = self.fingerprint
        else:
            properties["tls"] = "off"
        hostname = socket.gethostname().split(".", 1)[0] or "bambu-bridge"
        zeroconf = None
        try:
            info = ServiceInfo(
                MDNS_SERVICE_TYPE,
                f"{self.identity.name}.{MDNS_SERVICE_TYPE}",
                addresses=addresses,
                port=self.port,
                properties=properties,
                server=f"{hostname}.local.",
            )
            zeroconf = self._factory(ip_version=IPVersion.All)
            await asyncio.wait_for(zeroconf.async_register_service(info, allow_name_change=True), self.timeout)
        except Exception as exc:  # Discovery failures must not prevent service startup.
            log.warning(m('mDNS announcement failed: %s'), exc)
            if zeroconf is not None:
                try:
                    await zeroconf.async_close()
                except Exception as close_exc:
                    log.warning(m('Could not close mDNS: %s'), close_exc)
            return
        self._zeroconf, self._info = zeroconf, info
        log.info(m('mDNS: Announced %s on port %d'), info.name, self.port)

    async def stop(self) -> None:
        if self._zeroconf is None:
            return
        try:
            await asyncio.wait_for(self._zeroconf.async_unregister_service(self._info), self.timeout)
        except Exception as exc:
            log.warning(m('mDNS unregistration failed: %s'), exc)
        finally:
            try:
                await self._zeroconf.async_close()
            except Exception as exc:
                log.warning(m('Could not close mDNS: %s'), exc)
            self._zeroconf = self._info = None
