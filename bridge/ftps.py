"""Upload to the printer SD card using implicit-TLS FTPS on port 990.

The printer requires the data connection to reuse the control connection's TLS session.
`ftplib` does not do this automatically, hence the small subclass."""
from __future__ import annotations

import ftplib
import ssl
import time
from collections.abc import Callable
from pathlib import Path

from .trust import base_context


class ImplicitFTPS(ftplib.FTP_TLS):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size

    def storbinary(self, cmd, fp, blocksize=8192, callback=None, rest=None):
        """Like `FTP_TLS.storbinary`, without TLS shutdown (`unwrap`) on the data connection.

        The printer does not answer close_notify after uploads. `unwrap()` times out despite a
        complete SD card file, causing print start to fail. Closing TCP directly yields an immediate 226."""
        self.voidcmd("TYPE I")
        with self.transfercmd(cmd, rest) as conn:
            while buf := fp.read(blocksize):
                conn.sendall(buf)
                if callback:
                    callback(buf)
        return self.voidresp()


def _connect(host: str, access_code: str, timeout: float, context: ssl.SSLContext | None = None) -> ImplicitFTPS:
    # Use the pinned certificate (`PrinterTrust.context()`), or no verification in tests. Printers sometimes
    # close data connections without TLS shutdown; `base_context` permits this.
    ftp = ImplicitFTPS(context=context or base_context())
    ftp.connect(host, 990, timeout=timeout)
    ftp.login("bblp", access_code)
    ftp.prot_p()
    return ftp


def upload(host: str, access_code: str, local: Path, remote_name: str,
           progress: Callable[[int, int], None] | None = None, timeout: float = 60,
           context: ssl.SSLContext | None = None) -> None:
    """Upload `local` as `/<remote_name>`; blocking, call via asyncio `to_thread`. An exception raised by
    `progress` aborts the upload."""
    total = local.stat().st_size
    sent = 0

    def chunk(block: bytes) -> None:
        nonlocal sent
        sent += len(block)
        if progress:
            progress(sent, total)

    ftp = _connect(host, access_code, timeout, context)
    try:
        with local.open("rb") as handle:
            ftp.storbinary(f"STOR /{remote_name}", handle, blocksize=64 * 1024, callback=chunk)
    finally:
        _close(ftp)


def _close(ftp: ImplicitFTPS) -> None:
    try:
        ftp.quit()
    except (OSError, EOFError, ftplib.Error):
        ftp.close()


def list_dir(host: str, access_code: str, directory: str = "/", timeout: float = 30,
             context: ssl.SSLContext | None = None) -> list[dict]:
    """SD card directory files: name, size and modification time when reported.

    The printer FTP server sometimes drops connections, especially concurrent thumbnail, preview
    and object requests. Retry once in that case."""
    for attempt in (1, 2):
        try:
            return _list_dir_once(host, access_code, directory, timeout, context)
        except (OSError, EOFError, ftplib.error_temp, ftplib.error_reply):
            if attempt == 2:
                raise
            time.sleep(1)
    return []


def _list_dir_once(host: str, access_code: str, directory: str, timeout: float,
                   context: ssl.SSLContext | None = None) -> list[dict]:
    ftp = _connect(host, access_code, timeout, context)
    try:
        lines: list[str] = []
        try:
            ftp.retrlines(f"LIST {directory}", lines.append)
        except ftplib.error_perm:
            return []
        return [entry for entry in (parse_list_line(line) for line in lines) if entry]
    finally:
        _close(ftp)


_MONTHS = {m: i + 1 for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}


def parse_list_line(line: str) -> dict | None:
    """Unix-style LIST line → {name, size, is_dir, modified}. Unknown formats return None."""
    parts = line.split(None, 8)
    if len(parts) < 9 or parts[0][0] not in "-d":
        return None
    try:
        size = int(parts[4])
    except ValueError:
        return None
    month, day, year_or_time = parts[5], parts[6], parts[7]
    modified = None
    if month in _MONTHS and day.isdigit():
        if ":" in year_or_time:
            modified = f"{month} {day} {year_or_time}"
        else:
            modified = f"{year_or_time}-{_MONTHS[month]:02d}-{int(day):02d}"
    return {"name": parts[8], "size": size, "is_dir": parts[0][0] == "d", "modified": modified}


class TooLarge(Exception):
    """The SD card file is larger than allowed; the download was stopped."""

    def __init__(self, limit: int):
        super().__init__(f"more than {limit} bytes")
        self.limit = limit


def download(host: str, access_code: str, remote_path: str, local: Path, timeout: float = 60,
             context: ssl.SSLContext | None = None, limit: int | None = None) -> None:
    """Download an SD card file; blocking, call via asyncio `to_thread`.

    With `limit`, stop with `TooLarge` as soon as more bytes arrive, before they reach the disk."""
    ftp = _connect(host, access_code, timeout, context)
    received = 0
    tmp = local.with_suffix(local.suffix + ".part")
    try:
        with tmp.open("wb") as handle:
            def chunk(block: bytes) -> None:
                nonlocal received
                received += len(block)
                if limit is not None and received > limit:
                    raise TooLarge(limit)
                handle.write(block)
            ftp.retrbinary(f"RETR {remote_path}", chunk, blocksize=64 * 1024)
        tmp.replace(local)
    finally:
        tmp.unlink(missing_ok=True)
        _close(ftp)


def retrieve(host: str, access_code: str, remote_path: str, sink: Callable[[bytes], None], timeout: float = 60,
             context: ssl.SSLContext | None = None) -> None:
    """Stream an SD card file to `sink` in blocks without a temporary file; call via `to_thread`.

    This lets the app display progress while receiving large timelapses. If `sink` raises, abort."""
    ftp = _connect(host, access_code, timeout, context)
    try:
        ftp.retrbinary(f"RETR {remote_path}", sink, blocksize=64 * 1024)
    finally:
        _close(ftp)


def delete_path(host: str, access_code: str, remote_path: str, timeout: float = 30,
                context: ssl.SSLContext | None = None) -> None:
    ftp = _connect(host, access_code, timeout, context)
    try:
        ftp.delete(remote_path)
    finally:
        _close(ftp)
