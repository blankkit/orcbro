"""Pairing, device tokens, rate limiting and revocation of active connections."""
from __future__ import annotations

from .i18n import m

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .branding import LEGACY_TOKEN_CONTEXT

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_PATTERN = re.compile(r"^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{8}$")
TOKEN_PATTERN = re.compile(r"^[0-9a-f]{64}$")
INVITATION_SECONDS = 600
# The first-device setup code appears only in the bridge log and expires; a restart issues a new one.
SETUP_SECONDS = 3600
# Throttling per client address: ten failures within ten minutes block that address for ten minutes.
FAILURE_WINDOW = 600
FAILURE_LIMIT = 10
BLOCK_SECONDS = 600
FAILURE_DELAY = 1.5
# Bridge-wide budget: a failed check keeps its slot for FAILURE_DELAY, which bounds the guessing rate of many
# addresses together; beyond MAX_WAITING queued checks, requests are refused without a check.
PARALLEL_CHECKS = 4
MAX_WAITING = 32
BUSY_SECONDS = 10
MAX_SOURCES = 4096
MAX_INVITATIONS = 20


class AuthError(Exception):
    """Credentials on disk are invalid or cannot be saved."""


class InvalidCode(Exception):
    pass


class RateLimited(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = max(1, retry_after)
        super().__init__(m('Try again in {p0} seconds.', p0=self.retry_after))


@dataclass
class ClientDevice:
    id: str
    name: str
    token_sha256: str
    created_at: int
    last_seen_at: int

    def public(self, *, current: bool = False) -> dict:
        result = {"id": self.id, "name": self.name, "created_at": self.created_at,
                  "last_seen_at": self.last_seen_at}
        if current:
            result["current"] = True
        return result


@dataclass(frozen=True)
class Principal:
    authentication: str
    device_id: str | None = None


@dataclass(frozen=True)
class Invitation:
    code: str
    expires_at: int
    creator_id: str | None


def legacy_app_token(secret: str) -> str:
    """Keep the token derivation unchanged for existing app versions."""
    return hmac.new(secret.encode(), LEGACY_TOKEN_CONTEXT, hashlib.sha256).hexdigest()


def normalize_code(value: str) -> str:
    code = value.strip().upper()
    if len(code) == 9 and code[4] == "-":
        code = code[:4] + code[5:]
    elif len(code) != 8:
        return ""
    return code if CODE_PATTERN.fullmatch(code) else ""


def display_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def _new_code(existing: set[str] | None = None) -> str:
    occupied = existing or set()
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        if code not in occupied:
            return code


class PairingStore:
    """Persistent device storage; codes and invitations intentionally stay in memory."""

    def __init__(self, path: Path, legacy_secret: str | None = None, *,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable] = asyncio.sleep):
        self.path = Path(path)
        self.legacy_secret = legacy_secret
        self._clock = clock
        self._sleep = sleep
        self.devices: list[ClientDevice] = []
        self.invitations: dict[str, Invitation] = {}
        self._failures: dict[str | None, deque[float]] = {}
        self._blocked_until: dict[str | None, float] = {}
        self._lock = asyncio.Lock()
        self._checks = asyncio.Semaphore(PARALLEL_CHECKS)
        self._waiting = 0
        self._active: dict[str, set[asyncio.Task]] = {}
        self._dirty_seen = False
        self._last_seen_save: dict[str, float] = {}
        self._load()
        # A fixed access key suppresses the first-time setup code. Without one, the code is valid for
        # SETUP_SECONDS after start and only while no device is paired; removing the last device does not reopen it.
        self.setup_code: str | None = None
        self.setup_expires_at = 0
        if not self.devices and not legacy_secret:
            self.setup_code = _new_code()
            self.setup_expires_at = int(self._clock()) + SETUP_SECONDS

    @property
    def setup_open(self) -> bool:
        """A setup code exists and has not expired."""
        return bool(self.setup_code) and self._clock() < self.setup_expires_at

    @property
    def setup_remaining(self) -> int:
        """Seconds the setup code stays valid, 0 without one."""
        return max(0, int(self.setup_expires_at - self._clock())) if self.setup_open else 0

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AuthError(m('Device credentials {p0} cannot be read: {p1}', p0=self.path, p1=exc)) from exc
        try:
            if raw.get("schema_version") != 1 or not isinstance(raw.get("devices"), list):
                raise ValueError(m('unknown schema'))
            loaded = []
            identifiers = set()
            for entry in raw["devices"]:
                device = ClientDevice(**entry)
                device.id = str(uuid.UUID(device.id))
                if device.id in identifiers:
                    raise ValueError(m('duplicate device ID'))
                identifiers.add(device.id)
                if not isinstance(device.name, str) or not device.name.strip() or len(device.name) > 100:
                    raise ValueError(m('invalid device name'))
                if not isinstance(device.token_sha256, str) or not TOKEN_PATTERN.fullmatch(device.token_sha256):
                    raise ValueError(m('invalid token digest'))
                if not isinstance(device.created_at, int) or not isinstance(device.last_seen_at, int):
                    raise ValueError(m('invalid timestamp'))
                loaded.append(device)
            self.devices = loaded
            os.chmod(self.path.parent, 0o700)
            os.chmod(self.path, 0o600)
        except (TypeError, KeyError, ValueError) as exc:
            raise AuthError(m('Device credentials {p0} are damaged: {p1}', p0=self.path, p1=exc)) from exc
        except OSError as exc:
            raise AuthError(m('Permissions for device credentials {p0} could not be set: {p1}', p0=self.path, p1=exc)) from exc

    def _save(self) -> None:
        data = {"schema_version": 1, "devices": [asdict(device) for device in self.devices]}
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise AuthError(m('Device credentials {p0} could not be saved: {p1}', p0=self.path, p1=exc)) from exc
        finally:
            temporary.unlink(missing_ok=True)

    def authenticate(self, token: str) -> Principal | None:
        """Check every digest without early exit and throttle last-access updates."""
        try:
            encoded = token.encode()
        except UnicodeEncodeError:
            return None  # For example, lone surrogates from JSON escapes cannot form a valid token.
        if self.legacy_secret:
            expected = legacy_app_token(self.legacy_secret)
            if hmac.compare_digest(encoded, expected.encode()):
                return Principal("legacy")
        digest = hashlib.sha256(encoded).hexdigest()
        found = None
        for device in self.devices:
            matches = hmac.compare_digest(digest, device.token_sha256)
            if matches:
                found = device
        if found is None:
            return None
        now = int(self._clock())
        changed = found.last_seen_at != now
        found.last_seen_at = now
        last_save = self._last_seen_save.get(found.id, found.created_at)
        if changed and now - last_save >= 60:
            self._save()
            self._last_seen_save[found.id] = now
            self._dirty_seen = False
        elif changed:
            self._dirty_seen = True
        return Principal("device", found.id)

    def track(self, principal: Principal) -> None:
        task = asyncio.current_task()
        if principal.device_id and task:
            self._active.setdefault(principal.device_id, set()).add(task)

    def untrack(self, principal: Principal) -> None:
        task = asyncio.current_task()
        if not principal.device_id or task is None:
            return
        tasks = self._active.get(principal.device_id)
        if tasks:
            tasks.discard(task)
            if not tasks:
                self._active.pop(principal.device_id, None)

    async def pair(self, code_value: str, device_name: str, source: str | None = None) -> tuple[str, ClientDevice]:
        """Pair with the setup code or an invitation. `source` identifies the client for throttling."""
        name = device_name.strip() if isinstance(device_name, str) else ""
        if not name or len(name) > 100:
            raise ValueError(m('device_name must be 1 to 100 characters long.'))
        code = normalize_code(code_value) if isinstance(code_value, str) else ""

        def attempt(now: float) -> tuple[str, ClientDevice] | None:
            self._expire_invitations(int(now))
            invitation = self.invitations.get(code)
            valid_setup = bool(code and self.setup_open and hmac.compare_digest(code, self.setup_code))
            valid_invitation = bool(invitation and hmac.compare_digest(code, invitation.code))
            if not (valid_setup or valid_invitation):
                return None
            token = secrets.token_urlsafe(32)
            stamp = int(now)
            device = ClientDevice(str(uuid.uuid4()), name, hashlib.sha256(token.encode()).hexdigest(), stamp, stamp)
            previous_devices = list(self.devices)
            previous_setup = self.setup_code
            previous_invitations = dict(self.invitations)
            self.devices.append(device)
            if valid_setup:
                self.setup_code = None
            else:
                self.invitations.pop(code, None)
            try:
                self._save()
            except AuthError:
                self.devices = previous_devices
                self.setup_code = previous_setup
                self.invitations = previous_invitations
                raise
            self._last_seen_save[device.id] = now
            return token, device

        return await self._checked(source, attempt)

    async def check_setup(self, code_value, source: str | None = None) -> str:
        """Verify the setup code without using it up (setup page QR code); same throttling as pairing."""
        code = normalize_code(code_value) if isinstance(code_value, str) else ""

        def attempt(_now: float) -> str | None:
            valid = bool(code and self.setup_open and hmac.compare_digest(code, self.setup_code))
            return code if valid else None

        return await self._checked(source, attempt)

    async def check_access_key(self, key, source: str | None = None) -> None:
        """Verify the fixed access key (`BRIDGE_ACCESS_TOKEN`); same throttling as codes."""
        def attempt(_now: float) -> bool | None:
            if not self.legacy_secret or not isinstance(key, str):
                return None
            try:
                return True if hmac.compare_digest(key.strip().encode(), self.legacy_secret.encode()) else None
            except UnicodeEncodeError:
                return None

        await self._checked(source, attempt)

    def retry_after(self, source: str | None) -> int | None:
        """Seconds until `source` may try again, or None if it is not blocked."""
        now = self._clock()
        until = self._blocked_until.get(source, 0.0)
        return int(until - now + 0.999) if now < until else None

    async def _checked(self, source: str | None, attempt: Callable[[float], Any]) -> Any:
        """Run one credential check: a blocked source is refused, failures are counted per source and keep a
        bridge-wide check slot for FAILURE_DELAY, so concurrent guesses cannot multiply the rate."""
        blocked = self.retry_after(source)
        if blocked is not None:
            raise RateLimited(blocked)
        if self._waiting >= MAX_WAITING:
            raise RateLimited(BUSY_SECONDS)
        self._waiting += 1
        try:
            async with self._checks:
                async with self._lock:
                    blocked = self.retry_after(source)
                    if blocked is not None:
                        raise RateLimited(blocked)
                    now = self._clock()
                    result = attempt(now)
                    if result is not None:
                        return result
                    reached_limit = self._record_failure(source, now)
                await self._sleep(FAILURE_DELAY)
        finally:
            self._waiting -= 1
        if reached_limit:
            raise RateLimited(BLOCK_SECONDS)
        raise InvalidCode()

    def _record_failure(self, source: str | None, now: float) -> bool:
        for key in [key for key, until in self._blocked_until.items() if until <= now]:
            self._blocked_until.pop(key, None)
        failures = self._failures.setdefault(source, deque())
        while failures and failures[0] <= now - FAILURE_WINDOW:
            failures.popleft()
        failures.append(now)
        if len(self._failures) > MAX_SOURCES:
            # Bounded memory: forget the source with the oldest latest failure.
            stale = [key for key, times in self._failures.items() if not times or times[-1] <= now - FAILURE_WINDOW]
            for key in stale:
                self._failures.pop(key, None)
            if len(self._failures) > MAX_SOURCES:
                oldest = min((key for key in self._failures if key != source), key=lambda k: self._failures[k][-1])
                self._failures.pop(oldest, None)
        if len(failures) >= FAILURE_LIMIT:
            self._blocked_until[source] = now + BLOCK_SECONDS
            failures.clear()
            return True
        return False

    def _expire_invitations(self, now: int) -> None:
        self.invitations = {code: invitation for code, invitation in self.invitations.items()
                            if invitation.expires_at > now}

    async def invite(self, principal: Principal) -> Invitation:
        async with self._lock:
            now = int(self._clock())
            self._expire_invitations(now)
            if len(self.invitations) >= MAX_INVITATIONS:
                raise ValueError(m('Too many open invitations. Try again later.'))
            code = _new_code(set(self.invitations) | ({self.setup_code} if self.setup_code else set()))
            invitation = Invitation(code, now + INVITATION_SECONDS, principal.device_id)
            self.invitations[code] = invitation
            return invitation

    async def remove(self, identifier: str) -> bool:
        async with self._lock:
            index = next((index for index, device in enumerate(self.devices) if device.id == identifier), None)
            if index is None:
                return False
            previous_devices = list(self.devices)
            previous_invitations = dict(self.invitations)
            self.devices.pop(index)
            self.invitations = {code: invitation for code, invitation in self.invitations.items()
                                if invitation.creator_id != identifier}
            try:
                self._save()
            except AuthError:
                self.devices = previous_devices
                self.invitations = previous_invitations
                raise
        current = asyncio.current_task()
        for task in list(self._active.pop(identifier, set())):
            if task is not current:
                task.cancel()
        return True

    def list_devices(self, principal: Principal) -> list[dict]:
        return [device.public(current=principal.device_id == device.id) for device in self.devices]

    async def close(self) -> None:
        if self._dirty_seen:
            self._save()
            self._dirty_seen = False
