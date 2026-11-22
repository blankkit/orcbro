"""Relay pushes (relay/) for bridges without their own app APNs key.

The first push registers the bridge (`POST /v1/bridges`) and stores its ID/key in private
`relay.json`. The first bridge to use a push token owns it at the relay. Live Activities omit
`alert`; notifications send template values (notify.py). Both include the token's language.
A relay 410 invalidates the token, as APNs does. While the relay rotates its secret, it answers
with the bridge's new key (`x-bridge-key`); the bridge stores it and keeps its ID, so its tokens
stay bound to it."""
from __future__ import annotations

from .i18n import m

import asyncio
import json
import logging
import os
import re
from pathlib import Path

import httpx

from . import __version__
from .apns import APNsError

log = logging.getLogger(__name__)

KEY = re.compile(r"[A-Za-z0-9_-]{43}")


class RelayClient:
    def __init__(self, url: str, credentials: Path | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.url = url.rstrip("/")
        self.credentials = Path(credentials) if credentials else None
        self._client = httpx.AsyncClient(transport=transport, timeout=15,
                                         headers={"user-agent": f"orcbro/{__version__}"})
        self._auth: str | None = None
        self._lock = asyncio.Lock()

    async def send_live_activity(self, device_token: str, environment: str, payload: dict, priority: int = 5) -> None:
        aps = {key: value for key, value in payload["aps"].items() if key != "alert"}  # The relay creates the start alert.
        await self._post("/v1/live-activity", {"token": device_token, "environment": environment, "priority": priority,
                                               "language": payload.get("language", "en"), "payload": {"aps": aps}})

    async def send_notification(self, device_token: str, environment: str, item: dict) -> None:
        await self._post("/v1/notification", {"token": device_token, "environment": environment, "language": "en", **item})

    async def _post(self, path: str, body: dict) -> None:
        for attempt in range(2):
            used = await self._authorization()
            response = await self._client.post(f"{self.url}{path}", json=body, headers={"authorization": f"Bearer {used}"})
            if response.status_code == 401 and attempt == 0:
                # Incorrect key (changed relay secret or another installation's file): register again.
                await self._forget()
                continue
            renewed = response.headers.get("x-bridge-key")
            if renewed and KEY.fullmatch(renewed):
                await self._renew(used, renewed)
            break
        if response.status_code == 200:
            return
        try:
            reply = response.json()
        except ValueError:
            reply = {}
        reason = reply.get("reason") or reply.get("error") or m('unknown')
        raise APNsError(response.status_code, str(reason))

    async def _authorization(self) -> str:
        async with self._lock:
            if self._auth is None:
                self._auth = self._load() or await self._register()
            return self._auth

    def _load(self) -> str | None:
        if not self.credentials:
            return None
        try:
            data = json.loads(self.credentials.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if data.get("url") != self.url or not data.get("bridge") or not data.get("key"):
            return None
        return f"{data['bridge']}.{data['key']}"

    async def _register(self) -> str:
        response = await self._client.post(f"{self.url}/v1/bridges", json={})
        if response.status_code != 201:
            raise APNsError(response.status_code, m('Relay registration failed'))
        data = response.json()
        log.info(m('Registered with relay %s'), self.url)
        self._save(data["bridge"], data["key"])
        return f"{data['bridge']}.{data['key']}"

    def _save(self, bridge: str, key: str) -> None:
        if self.credentials:
            tmp = self.credentials.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"url": self.url, "bridge": bridge, "key": key}, handle)
            tmp.replace(self.credentials)

    async def _renew(self, used: str, key: str) -> None:
        """The relay changed its secret and sent this bridge's new key: same ID, so its tokens stay bound to it."""
        async with self._lock:
            if self._auth != used:
                return
            bridge = used.split(".", 1)[0]
            self._auth = f"{bridge}.{key}"
            try:
                self._save(bridge, key)
            except OSError as exc:
                log.warning(m('Could not save the new relay key: %s'), exc)
            log.info(m("Relay %s renewed this bridge's key"), self.url)

    async def _forget(self) -> None:
        async with self._lock:
            self._auth = None
            if self.credentials:
                self.credentials.unlink(missing_ok=True)

    async def close(self) -> None:
        await self._client.aclose()
