"""RTSPS camera through one shared ffmpeg process → MJPEG."""
from __future__ import annotations

from .i18n import m

import asyncio
import logging
import time
from urllib.parse import quote

from .camera import IDLE_STOP, MAX_FRAME
from .trust import CHANGED, CertificateChanged

log = logging.getLogger(__name__)


class MjpegParser:
    """Extract JPEGs from arbitrarily fragmented ffmpeg output while bounding the buffer."""

    def __init__(self, limit: int = MAX_FRAME * 2):
        self.buffer = bytearray()
        self.limit = limit

    def feed(self, chunk: bytes) -> list[bytes]:
        if chunk:
            self.buffer.extend(chunk)
        frames = []
        while True:
            start = self.buffer.find(b"\xff\xd8")
            if start < 0:
                if len(self.buffer) > 1:
                    del self.buffer[:-1]
                break
            if start:
                del self.buffer[:start]
            end = self.buffer.find(b"\xff\xd9", 2)
            if end < 0:
                if len(self.buffer) > self.limit:
                    self.buffer.clear()
                    raise ValueError(m('Camera image exceeds the size limit.'))
                break
            frame = bytes(self.buffer[:end + 2])
            del self.buffer[:end + 2]
            if len(frame) <= MAX_FRAME:
                frames.append(frame)
        return frames


RTSPS_PORT = 322


def stream_url(host: str, access_code: str) -> str:
    """RTSPS URL with configured host; encode the access code only as part of the URL."""
    address = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"rtsps://bblp:{quote(access_code, safe='')}@{address}:{RTSPS_PORT}/streaming/live/1"


class RtspCamera:
    def __init__(self, host: str, access_code: str, trust=None):
        self.host = host
        self._code = access_code
        # ffmpeg cannot check the pin: before each start, a TLS handshake on the RTSPS port itself must present
        # the pinned printer certificate. ffmpeg then opens its own connection with the access code.
        self.trust = trust
        self.frame: bytes | None = None
        self.frame_at: float | None = None
        self.error: str | None = None
        self._viewers = 0
        self._last_viewer = 0.0
        self._task: asyncio.Task | None = None
        self._new = asyncio.Condition()
        self._process = None

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    def _ensure_running(self) -> None:
        self._last_viewer = time.monotonic()
        if not self.active:
            self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown errors can be ignored
                pass
        await self._stop_process()

    async def snapshot(self, timeout: float = 12.0) -> bytes:
        self._ensure_running()
        if self.frame and self.frame_at and time.time() - self.frame_at < 3:
            return self.frame
        async with self._new:
            await asyncio.wait_for(self._new.wait(), timeout)
        assert self.frame is not None
        return self.frame

    async def frames(self):
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
        while self._viewers > 0 or time.monotonic() - self._last_viewer < IDLE_STOP:
            parser = MjpegParser()
            try:
                if self.trust is not None:
                    await self.trust.check(self.host, RTSPS_PORT)
                self._process = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-nostdin", "-loglevel", "error", "-rtsp_transport", "tcp", "-tls_verify", "0",
                    "-i", stream_url(self.host, self._code), "-an", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                log.info(m('RTSPS camera connected'))
                while self._viewers > 0 or time.monotonic() - self._last_viewer < IDLE_STOP:
                    chunk = await asyncio.wait_for(self._process.stdout.read(64 * 1024), 15)
                    if not chunk:
                        raise EOFError(m('ffmpeg exited'))
                    for frame in parser.feed(chunk):
                        self.frame, self.frame_at, self.error = frame, time.time(), None
                        backoff = 2
                        async with self._new:
                            self._new.notify_all()
            except asyncio.CancelledError:
                raise
            except CertificateChanged:
                self.error = CHANGED
                log.warning(m('RTSPS camera: the certificate on the camera port does not match the printer; ffmpeg was not started'))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
            except Exception:  # Never log the access code or ffmpeg output.
                self.error = m('No camera image. Check LAN Liveview on the printer.')
                log.warning(m('RTSPS camera unavailable; check LAN Liveview'))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
            finally:
                await self._stop_process()
        log.info(m('RTSPS camera disconnected with no viewers'))

    async def _stop_process(self) -> None:
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 3)
        except TimeoutError:
            process.kill()
            await process.wait()
