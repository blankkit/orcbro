"""P1/A1 printer cameras (port 6000, TLS): individual JPEG frames, about 1–2 per second.

The client sends an 80-byte login packet (16-byte header, 32-byte username, 32-byte access code).
Each returned frame has a 16-byte header (uint32 LE length, then 12 bytes) followed by JPEG data.
Connect only while viewers are present and distribute each frame to all viewers."""
from __future__ import annotations

from .i18n import m, message_of

import asyncio
import logging
import struct
import time

from .trust import PrinterTrust, base_context

log = logging.getLogger(__name__)

HEADER = struct.Struct("<IIII")
MAX_FRAME = 4 * 1024 * 1024
IDLE_STOP = 20  # Seconds without viewers before disconnecting.


def auth_packet(access_code: str, user: str = "bblp") -> bytes:
    return HEADER.pack(0x40, 0x3000, 0, 0) + user.encode().ljust(32, b"\0") + access_code.encode().ljust(32, b"\0")


def parse_header(header: bytes) -> int:
    size, _itrack, _flags, _reserved = HEADER.unpack(header)
    if not 0 < size <= MAX_FRAME:
        raise ValueError(m('implausible image size {p0}', p0=size))
    return size


def is_jpeg(data: bytes) -> bool:
    return data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9"


class JpegCamera:
    def __init__(self, host: str, access_code: str, port: int = 6000, trust: PrinterTrust | None = None):
        self.host = host
        self.port = port
        self.trust = trust
        self._code = access_code
        self.frame: bytes | None = None
        self.frame_at: float | None = None
        self.error: str | None = None
        self._viewers = 0
        self._last_viewer = 0.0
        self._task: asyncio.Task | None = None
        self._new = asyncio.Condition()

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    def _ensure_running(self) -> None:
        self._last_viewer = time.monotonic()
        if not self.active:
            self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        """Close the camera connection when the printer is removed or reconfigured."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown errors can be ignored
                pass

    async def snapshot(self, timeout: float = 12.0) -> bytes:
        """Current frame; start the camera if needed and wait for a fresh frame."""
        self._ensure_running()
        if self.frame and self.frame_at and time.time() - self.frame_at < 3:
            return self.frame
        async with self._new:
            await asyncio.wait_for(self._new.wait(), timeout)
        assert self.frame is not None
        return self.frame

    async def frames(self):
        """Asynchronous frame stream for one MJPEG viewer."""
        self._viewers += 1
        try:
            self._ensure_running()
            if self.frame:
                yield self.frame
            while True:
                async with self._new:
                    await asyncio.wait_for(self._new.wait(), 30)
                self._last_viewer = time.monotonic()
                if self.frame:
                    yield self.frame
        finally:
            self._viewers -= 1
            self._last_viewer = time.monotonic()

    async def _run(self) -> None:
        backoff = 2
        # Verify the MQTT certificate before sending the access code.
        context = self.trust.context() if self.trust else base_context()
        while self._viewers > 0 or time.monotonic() - self._last_viewer < IDLE_STOP:
            writer = None
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port, ssl=context), 10)
                writer.write(auth_packet(self._code))
                await writer.drain()
                log.info(m('Camera connected'))
                while self._viewers > 0 or time.monotonic() - self._last_viewer < IDLE_STOP:
                    size = parse_header(await asyncio.wait_for(reader.readexactly(HEADER.size), 15))
                    data = await asyncio.wait_for(reader.readexactly(size), 15)
                    if not is_jpeg(data):
                        continue
                    self.frame, self.frame_at, self.error = data, time.time(), None
                    backoff = 2
                    async with self._new:
                        self._new.notify_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.error = _describe(exc)
                log.warning(m('Camera: %s'), self.error)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
            finally:
                if writer is not None:
                    writer.close()
        log.info(m('Camera disconnected with no viewers'))


def _describe(exc: Exception) -> str:
    if isinstance(exc, asyncio.IncompleteReadError) and not exc.partial:
        return m('The camera closed the connection (incorrect access code or Liveview is off).')
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return m('Camera not responding.')
    return message_of(exc) or exc.__class__.__name__


class Camera:
    """Stable camera facade; select a backend only after resolving the model safely."""

    def __init__(self, host: str, access_code: str, trust: PrinterTrust | None = None):
        self.host = host
        self._code = access_code
        self.trust = trust
        self.mode: str | None = None
        self._backend = None
        self._selection_error: str | None = m('Printer model is still unknown; camera remains off.')

    @property
    def available(self) -> bool:
        """A transport is selected: model known and camera enabled."""
        return self._backend is not None

    def set_mode(self, mode: str | None, *, disabled: bool = False) -> None:
        wanted = "disabled" if disabled else mode
        if wanted == self.mode:
            return
        old = self._backend
        self._backend = None
        self.mode = wanted
        if old is not None:
            try:
                asyncio.get_running_loop().create_task(old.close())
            except RuntimeError:
                pass
        if wanted == "jpeg6000":
            self._backend = JpegCamera(self.host, self._code, trust=self.trust)
            self._selection_error = None
        elif wanted == "rtsps":
            from .camera_rtsp import RtspCamera
            self._backend = RtspCamera(self.host, self._code, trust=self.trust)
            self._selection_error = None
        elif wanted == "disabled":
            self._selection_error = m('LAN Liveview is disabled on the printer.')
        else:
            self._selection_error = m('No secure camera transport is known for this printer model.')

    @property
    def active(self) -> bool:
        return bool(self._backend and self._backend.active)

    @property
    def frame(self) -> bytes | None:
        return self._backend.frame if self._backend else None

    @property
    def frame_at(self) -> float | None:
        return self._backend.frame_at if self._backend else None

    @property
    def error(self) -> str | None:
        return self._backend.error if self._backend else self._selection_error

    async def close(self) -> None:
        if self._backend:
            await self._backend.close()

    async def snapshot(self, timeout: float = 12.0) -> bytes:
        if not self._backend:
            raise RuntimeError(self.error)
        return await self._backend.snapshot(timeout)

    async def frames(self):
        if not self._backend:
            raise RuntimeError(self.error)
        async for frame in self._backend.frames():
            yield frame
