"""Apple Push Notification service (APNs) for Live Activities: HTTP/2 with ES256 JWT authentication.

JWTs carry Team ID and Key ID, are valid for up to an hour, and refresh after 40 minutes (Apple
rejects refreshes more frequent than 20 minutes). Xcode builds register with the sandbox;
TestFlight/App Store builds use production. The app declares its environment when registering."""
from __future__ import annotations

from .i18n import m

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import jwt

log = logging.getLogger(__name__)

HOSTS = {"production": "https://api.push.apple.com", "development": "https://api.sandbox.push.apple.com"}
TOKEN_LIFETIME = 40 * 60

# Responses that invalidate a device token and require forgetting it.
GONE_REASONS = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic", "ExpiredToken"}

_DEVICE_PATH = re.compile(r"/3/device/[^\s\"'?/]+")


class RedactDeviceTokens(logging.Filter):
    """HTTPX logs every request URL at INFO; APNs URLs contain the device token. Keep the log line, not the token."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "/3/device/" in message:
            record.msg, record.args = _DEVICE_PATH.sub("/3/device/<token>", message), None
        return True


_REDACT = RedactDeviceTokens()


class APNsError(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(f"APNs {status}: {reason}")
        self.status = status
        self.reason = reason

    @property
    def token_gone(self) -> bool:
        return self.status == 410 or self.reason in GONE_REASONS


@dataclass(frozen=True)
class APNsConfig:
    key_file: Path
    key_id: str
    team_id: str
    bundle_id: str

    @classmethod
    def load(cls, key_file: Path | None, key_id: str | None, team_id: str | None, bundle_id: str) -> APNsConfig | None:
        """Return None if configuration is incomplete; disable Live Activities while the bridge keeps running."""
        if not (key_file and key_id and team_id and Path(key_file).is_file()):
            return None
        return cls(Path(key_file), key_id.strip(), team_id.strip(), bundle_id)


class APNsClient:
    def __init__(self, config: APNsConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self._key = config.key_file.read_text(encoding="utf-8")
        self._token: str | None = None
        self._token_at = 0.0
        self._client = httpx.AsyncClient(http2=transport is None, transport=transport, timeout=15)
        if _REDACT not in logging.getLogger("httpx").filters:
            logging.getLogger("httpx").addFilter(_REDACT)

    def provider_token(self) -> str:
        now = time.time()
        if self._token is None or now - self._token_at > TOKEN_LIFETIME:
            self._token = jwt.encode({"iss": self.config.team_id, "iat": int(now)}, self._key, algorithm="ES256",
                                     headers={"kid": self.config.key_id})
            self._token_at = now
        return self._token

    async def send_live_activity(self, device_token: str, environment: str, payload: dict, priority: int = 5) -> None:
        """Send a Live Activity start/update/end push; raise `APNsError` on rejection."""
        await self._post(device_token, environment, {k: v for k, v in payload.items() if k != "language"}, {
            "apns-push-type": "liveactivity", "apns-topic": f"{self.config.bundle_id}.push-type.liveactivity",
            "apns-priority": str(priority)})

    async def send_notification(self, device_token: str, environment: str, item: dict) -> None:
        """Send a notification (notify.py) to an app device token; raise `APNsError` on rejection."""
        from .notify import apns_payload
        await self._post(device_token, environment, apns_payload(item), {
            "apns-push-type": "alert", "apns-topic": self.config.bundle_id, "apns-priority": "10"})

    async def _post(self, device_token: str, environment: str, payload: dict, headers: dict) -> None:
        host = HOSTS.get(environment)
        if host is None:
            raise APNsError(400, m('Unknown environment {p0}', p0=environment))
        response = await self._client.post(f"{host}/3/device/{device_token}", json=payload,
                                           headers={"authorization": f"bearer {self.provider_token()}", **headers})
        if response.status_code != 200:
            try:
                reason = response.json().get("reason", "")
            except ValueError:
                reason = response.text[:200]
            if reason in ("ExpiredProviderToken", "InvalidProviderToken"):
                self._token = None  # Issue a fresh token next time.
            raise APNsError(response.status_code, reason or m('unknown'))

    async def close(self) -> None:
        await self._client.aclose()
