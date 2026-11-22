"""mDNS announcement of the bridge's direct listener.

Addresses come from the host name and from the interface the host uses for multicast and its default route: on
Debian, Raspberry Pi OS and many Ubuntu installations the host name resolves only to 127.0.1.1. The addresses are
checked again every `REFRESH` seconds: a bridge without an address, or whose announcement failed, tries again, and
a changed address is announced anew."""
from __future__ import annotations

from .i18n import m

import asyncio
import ipaddress
import logging
import socket

from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from . import __version__
from .branding import MDNS_SERVICE_TYPE

log = logging.getLogger(__name__)

REFRESH = 60.0  # Seconds between address checks.
# Destinations that select the interface for outgoing traffic: the mDNS group, and a documentation address for IPv6
# that follows the default route. Connecting a UDP socket sends nothing.
ROUTE_TARGETS = ((socket.AF_INET, "224.0.0.251"), (socket.AF_INET6, "2001:db8::1"))


def _route_addresses() -> list[tuple[int, str]]:
    found = []
    for family, target in ROUTE_TARGETS:
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as probe:
                probe.connect((target, 5353))
                found.append((family, probe.getsockname()[0]))
        except OSError:
            continue
    return found


def _addresses() -> list[bytes]:
    """Packed addresses to announce: those of the host name and of the outgoing interface, without loopback."""
    entries: list[tuple[int, str]] = []
    try:
        entries += [(family, sockaddr[0]) for family, _kind, _proto, _name, sockaddr
                    in socket.getaddrinfo(socket.gethostname(), None, type=socket.SOCK_STREAM)]
    except OSError:
        pass
    entries += _route_addresses()
    addresses = set()
    for family, text in entries:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        try:
            address = ipaddress.ip_address(text.split("%", 1)[0])
        except ValueError:
            continue
        if not (address.is_loopback or address.is_unspecified):
            addresses.add(address.packed)
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
        self._addresses: list[bytes] = []
        self._task: asyncio.Task | None = None
        self._problem: str | None = None  # Last logged problem; repeats are not logged again.

    @property
    def announced(self) -> bool:
        return self._info is not None

    async def start(self) -> bool:
        """Announce now if a local address is known, and keep checking the addresses in the background.
        Returns whether the bridge is announced."""
        await self._refresh()
        if self._task is None:
            self._task = asyncio.create_task(self._watch())
        return self.announced

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(REFRESH)
            try:
                await self._refresh()
            except Exception as exc:  # noqa: BLE001 - discovery must never stop the bridge
                self._warn(m('mDNS announcement failed: %s'), exc)

    def _warn(self, message: str, *args) -> None:
        text = message % args if args else message
        if text != self._problem:
            log.warning(message, *args)
            self._problem = text

    async def _refresh(self) -> None:
        addresses = _addresses()
        if not addresses:
            self._warn(m('mDNS: No local address found for announcement; trying again every %.0f s'), REFRESH)
            return
        if self._info is not None and addresses == self._addresses:
            return
        if self._info is not None:
            # Announce anew rather than update: the new address may be on an interface zeroconf did not join yet.
            log.info(m('mDNS: Addresses changed; announcing again'))
            await self._unregister()
        await self._register(addresses)

    async def _register(self, addresses: list[bytes]) -> None:
        properties = {"id": self.identity.id, "version": __version__, "api": "1"}
        if self.public_url:
            # Behind a reverse proxy, the app uses this HTTPS address instead of the listener's IP and port.
            properties["url"] = self.public_url
        elif self.tls_enabled and self.fingerprint:
            properties["fp"] = self.fingerprint
        else:
            properties["tls"] = "off"
        hostname = socket.gethostname().split(".", 1)[0] or "orcbro"
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
            self._warn(m('mDNS announcement failed: %s'), exc)
            if zeroconf is not None:
                try:
                    await zeroconf.async_close()
                except Exception as close_exc:
                    log.warning(m('Could not close mDNS: %s'), close_exc)
            return
        self._zeroconf, self._info, self._addresses, self._problem = zeroconf, info, addresses, None
        log.info(m('mDNS: Announced %s on port %d'), info.name, self.port)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self._unregister()

    async def _unregister(self) -> None:
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
