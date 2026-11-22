"""Persistent bridge identity."""
from __future__ import annotations

from .i18n import m

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path


class IdentityError(Exception):
    """Identity could not be loaded or saved securely."""


@dataclass(frozen=True)
class BridgeIdentity:
    id: str
    name: str


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def load_identity(path: Path, name_override: str | None = None) -> BridgeIdentity:
    """Load the identity or create it on first launch; existing corrupt identity data is fatal."""
    path = Path(path)
    override = name_override.strip() if name_override else None
    if override is not None and not override:
        override = None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        identity = BridgeIdentity(str(uuid.uuid4()), override or "Orcbro")
        try:
            _write(path, {"schema_version": 1, "id": identity.id, "name": identity.name})
        except OSError as exc:
            raise IdentityError(m('Could not save bridge identity {p0}: {p1}', p0=path, p1=exc)) from exc
        return identity
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IdentityError(m('Cannot read bridge identity {p0}: {p1}', p0=path, p1=exc)) from exc

    try:
        if raw.get("schema_version") != 1:
            raise ValueError(m('unknown schema_version'))
        identifier = str(uuid.UUID(raw["id"]))
        stored_name = raw["name"]
        if not isinstance(stored_name, str) or not stored_name.strip():
            raise ValueError(m('name is missing'))
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise IdentityError(m('Bridge identity {p0} is damaged: {p1}', p0=path, p1=exc)) from exc

    identity = BridgeIdentity(identifier, override or stored_name.strip())
    if override and identity.name != stored_name:
        try:
            _write(path, {"schema_version": 1, "id": identity.id, "name": identity.name})
        except OSError as exc:
            raise IdentityError(m('Could not update bridge identity {p0}: {p1}', p0=path, p1=exc)) from exc
    else:
        try:
            os.chmod(path.parent, 0o700)
            os.chmod(path, 0o600)
        except OSError as exc:
            raise IdentityError(m('Could not set permissions for bridge identity {p0}: {p1}', p0=path, p1=exc)) from exc
    return identity
