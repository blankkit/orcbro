"""Announcement only: `python -m bridge announce` advertises a bridge via Bonjour on the home
network when the bridge cannot access LAN multicast (Kubernetes, Docker without host networking,
or behind a reverse proxy).

Run as a second container with host networking; no data or printer required. Set
`BRIDGE_PUBLIC_URL` to the bridge HTTPS URL and optionally `BRIDGE_NAME`; the app uses that URL."""
from __future__ import annotations

from .i18n import m

import asyncio
import hashlib
import logging
import signal
from dataclasses import dataclass
from urllib.parse import urlsplit

from .config import ConfigError
from .mdns import MDNSAdvertiser

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Announced:
    id: str
    name: str


def settings(env: dict[str, str]) -> tuple[Announced, str, int]:
    """Validate environment and return (name and ID, public URL, port)."""
    url = (env.get("BRIDGE_PUBLIC_URL") or "").strip().rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query or parsed.fragment:
        raise ConfigError(m('BRIDGE_PUBLIC_URL must be the HTTPS address of the bridge, e.g. https://orcbro.example.com.'))
    name = (env.get("BRIDGE_NAME") or "Orcbro").strip()[:63] or "Orcbro"
    # Stable ID per URL lets the app recognize the same bridge after a restart.
    identifier = "url-" + hashlib.sha256(url.encode()).hexdigest()[:16]
    return Announced(identifier, name), url, parsed.port or 443


async def run(env: dict[str, str]) -> None:
    identity, url, port = settings(env)
    advertiser = MDNSAdvertiser(identity=identity, port=port, fingerprint=None, tls_enabled=False, public_url=url)
    # Without a local address yet, the advertiser keeps trying and logs the announcement once it succeeds.
    if await advertiser.start():
        log.info(m('Announcing %s: %s'), identity.name, url)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for caught in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(caught, stopped.set)
        except NotImplementedError:
            pass
    try:
        await stopped.wait()
    finally:
        await advertiser.stop()
