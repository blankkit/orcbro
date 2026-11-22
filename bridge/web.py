"""Local setup page, public URL and QR code."""
from __future__ import annotations

from .i18n import t, render, current_language

import html
import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

import segno

from . import __version__
from .auth import display_code
from .branding import PAIRING_SCHEME

HOST_PATTERN = re.compile(r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)$")


class URLConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class PublicOrigin:
    url: str
    include_fingerprint: bool


def _trusted(peer: str | None, networks: tuple[str, ...]) -> bool:
    if not peer:
        return False
    try:
        address = ipaddress.ip_address(peer)
        return any(address in ipaddress.ip_network(network, strict=False) for network in networks)
    except ValueError:
        return False


def _request_hostname(host_header: str) -> str:
    try:
        parsed = urlsplit(f"//{host_header}")
        hostname = parsed.hostname
        _ = parsed.port  # Also validates nonnumeric ports.
        if not hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise ValueError
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            if not HOST_PATTERN.fullmatch(hostname) or ".." in hostname:
                raise ValueError
        return hostname
    except (ValueError, UnicodeError) as exc:
        raise URLConfigurationError(t("The HTTP host is invalid.")) from exc


def public_origin(*, request_host: str, peer: str | None, forwarded_proto: str | None,
                  public_url: str | None, trusted_proxies: tuple[str, ...], tls_enabled: bool,
                  https_port: int) -> PublicOrigin | None:
    if public_url:
        return PublicOrigin(public_url.rstrip("/"), False)
    hostname = _request_hostname(request_host)
    if _trusted(peer, trusted_proxies) and forwarded_proto and forwarded_proto.lower() == "https":
        # The proxy must forward the external Host header unchanged.
        parsed = urlsplit(f"//{request_host}")
        port = f":{parsed.port}" if parsed.port and parsed.port != 443 else ""
        shown = f"[{hostname}]" if ":" in hostname else hostname
        return PublicOrigin(f"https://{shown}{port}", False)
    if not tls_enabled:
        return None
    shown = f"[{hostname}]" if ":" in hostname else hostname
    port = "" if https_port == 443 else f":{https_port}"
    return PublicOrigin(f"https://{shown}{port}", True)


def pairing_url(origin: PublicOrigin, code: str, name: str, fingerprint: str | None) -> str:
    parameters = {"url": origin.url, "code": display_code(code), "name": name}
    if origin.include_fingerprint and fingerprint:
        parameters["fp"] = fingerprint
    return f"{PAIRING_SCHEME}://pair?{urlencode(parameters)}"


# Match Bambro: light/dark system appearance, orange accent, rounded cards and pill buttons.
# Keep everything inline because the page CSP disallows external resources.
PAGE_STYLE = """
:root{color-scheme:light dark;--canvas:#F2F2F5;--panel:#FFFFFF;--edge:#E4E4E9;--face:#EBEBF0;--text:#121214;
--muted:#6B6B73;--accent:#CC321B;--ok:#1F9D55;--error:#C0262D}
@media (prefers-color-scheme:dark){:root{--canvas:#0B0B0D;--panel:#1A1A1E;--edge:#26262B;--face:#2A2A2F;
--text:#F2F2F5;--muted:#9A9AA3;--accent:#FF5A3C;--ok:#3DD17A;--error:#FF6B6B}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;font:17px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif;
color:var(--text);background:radial-gradient(120% 60% at 50% -10%,color-mix(in srgb,var(--accent) 22%,transparent),
transparent 60%),var(--canvas);-webkit-font-smoothing:antialiased}
main{max-width:34rem;margin:0 auto;padding:3rem 1rem 4rem}
.eyebrow{font-size:.75rem;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);margin:0}
h1{font-size:2.4rem;line-height:1.1;margin:.2rem 0 1.5rem;letter-spacing:-.02em}
.card{background:var(--panel);border:.5px solid var(--edge);border-radius:26px;padding:1.25rem 1.25rem 1.4rem;
margin:0 0 1rem}
.label{font-size:.75rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--accent);margin:0 0 .6rem}
p{margin:.5rem 0}.muted{color:var(--muted)}.small{font-size:.9rem}
.qr{display:block;background:#fff;border-radius:22px;padding:14px;width:min(16rem,100%);margin:.8rem auto .4rem}
.qr svg{display:block;width:100%;height:auto}
.hint{text-align:center;color:var(--muted);font-size:.9rem;margin:.2rem 0 .8rem}
.code{text-align:center;font:600 1.4rem ui-monospace,"SF Mono",Menlo,monospace;letter-spacing:.08em}
label{display:block;font-size:.9rem;color:var(--muted);margin:.8rem 0 .35rem}
input{width:100%;font:inherit;color:var(--text);background:var(--face);border:0;border-radius:999px;padding:.8rem 1.1rem;
outline:none}input:focus{box-shadow:0 0 0 2px var(--accent)}
button,.button{display:block;width:100%;font-family:inherit;font-size:1.05rem;font-weight:600;line-height:1.2;color:#fff;background:var(--accent);
border:0;border-radius:999px;padding:1rem;margin-top:.9rem;text-align:center;text-decoration:none;cursor:pointer}
.secondary{background:var(--face);color:var(--text)}
.error{color:var(--error);font-weight:600}
ul.printers{list-style:none;margin:0;padding:0}
ul.printers li{display:flex;align-items:center;gap:.75rem;padding:.7rem 0;border-top:.5px solid var(--edge)}
ul.printers li:first-child{border-top:0}
.dot{width:.6rem;height:.6rem;border-radius:50%;background:var(--muted);flex:none}.dot.on{background:var(--ok)}
.name{font-weight:600}.grow{flex:1;min-width:0}
code{font:.8rem ui-monospace,"SF Mono",Menlo,monospace;overflow-wrap:anywhere;color:var(--muted)}
a{color:var(--accent)}
"""


def _document(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="{current_language()}"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="color-scheme" content="light dark">'
            f"<title>{html.escape(title)}</title><style>{PAGE_STYLE}</style></head><body><main>{body}</main></body></html>")


def _header(identity) -> str:
    return (f'<p class="eyebrow">Orcbro {html.escape(__version__)}</p>'
            f"<h1>{html.escape(identity.name)}</h1>")


def _qr(link: str) -> str:
    svg = segno.make(link, error="m").svg_inline(scale=4, omitsize=True, dark="#000", light="#fff")
    return (f'<a class="qr" href="{html.escape(link, quote=True)}" aria-label="{t("Open in Bambro")}">{svg}</a>'
            f'<p class="hint">{t("On your iPhone, tap the QR code to open Bambro and pair.<br>Otherwise, choose “Scan Bridge QR Code” in Bambro.")}</p>')


def _key_source() -> str:
    return t("The bridge operator sets the access key: It is the value of <code>BRIDGE_ACCESS_TOKEN</code> in the bridge configuration (such as <code>docker-compose.yml</code> or a Kubernetes secret).")


def invite_form(error: str | None = None) -> str:
    """Access-key invitation form, offered only over HTTPS."""
    message = f'<p class="error">{html.escape(render(error))}</p>' if error else ""
    return (f'<section class="card"><p class="label">{t("Invite a Device")}</p>'
            f'<p>{t("Use the access key to create an invitation. Bambro then pairs using a QR code, and the key stays on this device.")}</p>' + message +
            f'<form method="post" action="/invite"><label for="key">{t("Access Key")}</label>'
            '<input id="key" type="password" name="key" autocomplete="current-password" required>'
            f'<button type="submit">{t("Create Invitation")}</button></form>'
            f'<p class="muted small">{_key_source()}</p></section>')


def invitation_page(*, identity, link: str, code: str, minutes: int) -> str:
    body = (_header(identity) +
            f'<section class="card"><p class="label">{t("Invitation")}</p>'
            f'<p>{t("Valid for {p0} minutes, for one device.", p0=minutes)}</p>{_qr(link)}'
            f'<p class="code">{html.escape(display_code(code))}</p>'
            f'<p class="muted small" style="text-align:center">{t("Enter this code if the QR code does not work.")}</p>'
            f'<a class="button secondary" href="/">{t("Back")}</a></section>')
    return _document(identity.name, body)


def setup_page(*, identity, printers: list[dict], setup_code: str | None,
               origin: PublicOrigin | None, fingerprint: str | None, fixed_key: bool,
               can_invite: bool = False, invite_error: str | None = None) -> str:
    printer_items = "".join(
        '<li><span class="dot{}"></span><span class="grow"><span class="name">{}</span><br>'
        '<span class="muted small">{}</span></span><span class="muted small">{}</span></li>'.format(
            " on" if printer.get("connected") else "",
            html.escape(str(printer.get("name") or "Bambu Lab")),
            html.escape(str(printer.get("model") or t("model unknown"))),
            t("connected") if printer.get("connected") else t("not connected"))
        for printer in printers)
    printer_section = (f'<ul class="printers">{printer_items}</ul>' if printer_items
                       else f'<p class="muted">{t("No printer configured yet. Set one up in Bambro after pairing.")}</p>')
    if fixed_key:
        pairing = (f'<section class="card"><p class="label">{t("Pair")}</p>'
                   f'<p>{t("A fixed access key is configured. New devices can pair with this key or an invitation.")}</p>')
        if can_invite:
            pairing += "</section>" + invite_form(invite_error)
        elif origin:
            pairing += (f'<p>{t("QR code invitations are available at the HTTPS address.")}</p>'
                        f'<a class="button" href="{html.escape(origin.url, quote=True)}/">{html.escape(origin.url)}</a>'
                        '</section>')
        else:
            pairing += f'<p class="muted small">{_key_source()}</p></section>'
    elif setup_code and origin:
        link = pairing_url(origin, setup_code, identity.name, fingerprint)
        pairing = (f'<section class="card"><p class="label">{t("Pair Bambro")}</p>'
                   f'<p>{t("One-time setup code for the first device.")}</p>' + _qr(link) +
                   f'<p class="code">{display_code(setup_code)}</p>'
                   f'<p class="muted small" style="text-align:center">{t("Certificate Fingerprint")}<br>'
                   f'<code>{html.escape(fingerprint or t("not applicable"))}</code></p></section>')
    elif setup_code:
        pairing = (f'<section class="card"><p class="label">{t("Pair Bambro")}</p>'
                   f'<p>{t("Pairing requires HTTPS. Enable <code>BRIDGE_TLS</code> or set <code>BRIDGE_PUBLIC_URL</code> to the external HTTPS address.")}</p></section>')
    else:
        pairing = (f'<section class="card"><p class="label">{t("More Devices")}</p>'
                   f'<p>{t("Invite more iPhones in Bambro on an already paired device: Settings → Bridge → Devices → Pair Another Device. The QR code for the new device appears there.")}</p></section>')
    body = (_header(identity) + pairing +
            f'<section class="card"><p class="label">{t("Printer")}</p>{printer_section}</section>')
    return _document(identity.name, body)
