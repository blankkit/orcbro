"""Configuration from environment variables.

Printers are configured in the app and saved on the volume. `BAMBU_HOST` and `BAMBU_ACCESS_CODE`
can define an additional printer, for example from a Kubernetes secret. `BAMBU_SERIAL` is optional;
the bridge reads it from the printer certificate."""
from __future__ import annotations

from .i18n import m

import os
import shlex
import ipaddress
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .branding import PUSH_RELAY_URL


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    bridge_token: str | None = field(default=None, repr=False)
    # Optional environment printer: IP, access code, serial number, name and fixed printer profile.
    host: str | None = None
    serial: str | None = None
    access_code: str | None = field(default=None, repr=False)
    name: str = ""
    machine_profile: str | None = None
    data_dir: Path = Path("/data")
    profiles_dir: Path = Path("/opt/orca-profiles/BBL")
    filament_ids: Path = Path("/opt/orca-profiles/bambu_filament_ids.json")
    # Printer models and capabilities (`resources/printers` from OrcaSlicer).
    models_dir: Path = Path("/opt/orca-profiles/printers")
    orca_cmd: tuple[str, ...] = ("xvfb-run", "-a", "-s", "-screen 0 1280x1024x24", "/opt/orca/AppRun")
    # Printer error/HMS texts (`hms_de.json.gz`, `hms_en.json.gz`); without them only codes are available.
    hms_dir: Path = Path("/opt/hms")
    # Path where the printer finds the FTPS-uploaded file.
    # Only an explicit value overrides automatic model-specific selection.
    print_url: str | None = None
    max_upload_mb: int = 300
    keep_jobs: int = 40
    slice_timeout: int = 900
    port: int = 8080
    https_port: int = 8443
    bridge_name: str | None = None
    tls: bool = True
    tls_cert: Path | None = None
    tls_key: Path | None = None
    public_url: str | None = None
    trusted_proxies: tuple[str, ...] = ()
    mdns: bool = True
    # Relay for Live Activity pushes without an APNs key. Environment configuration defaults to the
    # public relay; BRIDGE_PUSH_RELAY=off disables it. Direct configurations, such as tests, have none.
    push_relay: str | None = None
    # Live Activities via APNs (optional; disabled if any required setting is missing).
    apns_key_file: Path | None = None
    apns_key_id: str | None = None
    apns_team_id: str | None = None
    apns_bundle_id: str = "it.blankk.bambro"

    @property
    def auth_dir(self) -> Path:
        """Private directory for identity and device credentials (0700). Leave the data directory unchanged:
        on Kubernetes volumes it belongs to root and the bridge has no permission to change it."""
        return self.data_dir / "auth"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        env = dict(os.environ if env is None else env)

        token = env.get("BRIDGE_ACCESS_TOKEN", "").strip() or None
        if token is not None and len(token) < 20:
            raise ConfigError(m('BRIDGE_ACCESS_TOKEN needs at least 20 characters.'))
        kwargs: dict = dict(bridge_token=token)
        host, code = env.get("BAMBU_HOST", "").strip(), env.get("BAMBU_ACCESS_CODE", "").strip()
        if bool(host) != bool(code):
            raise ConfigError(m('BAMBU_HOST and BAMBU_ACCESS_CODE must be set together (or omit both and set up the printer in the app).'))
        optional = {
            "BAMBU_HOST": ("host", str),
            "BAMBU_ACCESS_CODE": ("access_code", str),
            "BAMBU_SERIAL": ("serial", lambda v: v.upper()),
            "BAMBU_NAME": ("name", str),
            "BRIDGE_DATA_DIR": ("data_dir", Path),
            "BAMBU_MACHINE_PROFILE": ("machine_profile", str),
            "ORCA_PROFILES_DIR": ("profiles_dir", Path),
            "ORCA_FILAMENT_IDS": ("filament_ids", Path),
            "ORCA_PRINTERS_DIR": ("models_dir", Path),
            "ORCA_CMD": ("orca_cmd", lambda v: tuple(shlex.split(v))),
            "BAMBU_PRINT_URL": ("print_url", str),
            "BRIDGE_MAX_UPLOAD_MB": ("max_upload_mb", int),
            "BRIDGE_KEEP_JOBS": ("keep_jobs", int),
            "ORCA_SLICE_TIMEOUT": ("slice_timeout", int),
            "HMS_TEXTS_DIR": ("hms_dir", Path),
            "PORT": ("port", int),
            "BRIDGE_HTTPS_PORT": ("https_port", int),
            "BRIDGE_NAME": ("bridge_name", str),
            "BRIDGE_TLS_CERT": ("tls_cert", Path),
            "BRIDGE_TLS_KEY": ("tls_key", Path),
            "BRIDGE_PUBLIC_URL": ("public_url", str),
            "APNS_KEY_FILE": ("apns_key_file", Path),
            "APNS_KEY_ID": ("apns_key_id", str),
            "APNS_TEAM_ID": ("apns_team_id", str),
            "APNS_BUNDLE_ID": ("apns_bundle_id", str),
        }
        for key, (attr, conv) in optional.items():
            if env.get(key, "").strip():
                try:
                    kwargs[attr] = conv(env[key].strip())
                except ValueError as exc:
                    raise ConfigError(f"{key}: {exc}") from exc
        if kwargs.get("print_url") is not None and "{name}" not in kwargs["print_url"]:
            raise ConfigError(m('BAMBU_PRINT_URL must contain {name}.'))
        cert, key = kwargs.get("tls_cert"), kwargs.get("tls_key")
        if bool(cert) != bool(key):
            raise ConfigError(m('BRIDGE_TLS_CERT and BRIDGE_TLS_KEY must be set together.'))
        relay = env.get("BRIDGE_PUSH_RELAY", "").strip()
        kwargs["push_relay"] = PUSH_RELAY_URL
        if relay:
            if relay.lower() == "off":
                kwargs["push_relay"] = None
            elif not relay.startswith("https://"):
                raise ConfigError(m("BRIDGE_PUSH_RELAY must be an https address or 'off'."))
            else:
                kwargs["push_relay"] = relay.rstrip("/")
        for variable, attribute in (("BRIDGE_TLS", "tls"), ("BRIDGE_MDNS", "mdns")):
            value = env.get(variable, "").strip().lower()
            if value:
                if value not in {"on", "off"}:
                    raise ConfigError(m("{p0} must be 'on' or 'off'.", p0=variable))
                kwargs[attribute] = value == "on"
        public_url = kwargs.get("public_url")
        if public_url:
            parsed = urlsplit(public_url)
            try:
                port = parsed.port
            except ValueError as exc:
                raise ConfigError(f"BRIDGE_PUBLIC_URL: {exc}") from exc
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or port is not None and not 1 <= port <= 65535):
                raise ConfigError(m('BRIDGE_PUBLIC_URL must be an HTTPS address without a query or fragment.'))
            kwargs["public_url"] = public_url.rstrip("/")
        proxies = env.get("BRIDGE_TRUSTED_PROXIES", "").strip()
        if proxies:
            kwargs["trusted_proxies"] = tuple(item.strip() for item in proxies.split(",") if item.strip())
            try:
                for network in kwargs["trusted_proxies"]:
                    ipaddress.ip_network(network, strict=False)
            except ValueError as exc:
                raise ConfigError(f"BRIDGE_TRUSTED_PROXIES: {exc}") from exc
        for attribute, variable in (("port", "PORT"), ("https_port", "BRIDGE_HTTPS_PORT")):
            port = kwargs.get(attribute, getattr(cls, attribute))
            if not 1 <= port <= 65535:
                raise ConfigError(m('{p0} must be between 1 and 65535.', p0=variable))
        return cls(**kwargs)
