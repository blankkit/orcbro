"""Slice through the OrcaSlicer CLI, headless under Xvfb for container thumbnail generation."""
from __future__ import annotations

from .i18n import m, message_of

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
from pathlib import Path

from .jobs import JobError, read_slice_info
from .profiles import Catalog

log = logging.getLogger(__name__)


class SliceError(Exception):
    pass


class Slicer:
    def __init__(self, command: tuple[str, ...], timeout: int = 900):
        self.command = command
        self.timeout = timeout
        self._lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def build_args(self, *, workdir: Path, model: Path, output_name: str, kind: str, plate: int,
                   settings: list[Path], filaments: list[Path], orient: bool, copies: int = 1) -> list[str]:
        args = [*self.command, "--datadir", str(workdir / "datadir"), "--outputdir", str(workdir / "out")]
        if kind == "model":
            # Orient onto the flattest stable face and arrange on the plate, as OrcaSlicer does.
            args += ["--arrange", "1", "--orient", "1" if orient else "0", "--ensure-on-bed"]
            if copies > 1:
                args += ["--clone-objects", str(copies)]
        else:
            args += ["--allow-newer-file"]
        if settings:
            args += ["--load-settings", ";".join(str(p) for p in settings)]
        if filaments:
            args += ["--load-filaments", ";".join(str(p) for p in filaments)]
        args += ["--slice", str(plate if kind == "project" else 0), "--export-3mf", output_name, str(model)]
        return args

    async def slice(self, *, catalog: Catalog, workdir: Path, model: Path, output: Path, kind: str, plate: int = 1,
                    process: str | None = None, filaments: list[str] | None = None, options: dict | None = None,
                    colors: list[str | None] | None = None, orient: bool = False, copies: int = 1) -> dict:
        """Slice `model` into `output` and return plates, time, weight and filament information."""
        async with self._lock:
            await asyncio.to_thread(shutil.rmtree, workdir, ignore_errors=True)
            (workdir / "out").mkdir(parents=True)
            (workdir / "datadir").mkdir()
            settings: list[Path] = []
            filament_paths: list[Path] = []
            if kind == "model":
                settings, filament_paths = catalog.write_settings(
                    workdir / "profiles", process=process, filaments=filaments or [], options=options or {},
                    colors=colors)
            args = self.build_args(workdir=workdir, model=model, output_name=output.name, kind=kind, plate=plate,
                                   settings=settings, filaments=filament_paths, orient=orient, copies=copies)
            env = {**os.environ, "HOME": str(workdir), "XDG_RUNTIME_DIR": str(workdir / "xdg"),
                   "LIBGL_ALWAYS_SOFTWARE": "1", "LC_ALL": "C"}
            (workdir / "xdg").mkdir(mode=0o700)
            log.info("Slice: %s", " ".join(args))
            # Own process group: xvfb-run starts Xvfb and OrcaSlicer as children, which must end with it.
            proc = await asyncio.create_subprocess_exec(
                *args, cwd=workdir, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                start_new_session=True)
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), self.timeout)
            except TimeoutError:
                await _kill(proc)
                raise SliceError(m('Slicing canceled after {p0} minutes.', p0=self.timeout // 60)) from None
            except asyncio.CancelledError:
                await _kill(proc)  # Bridge shutdown: do not leave the slicer running.
                raise
            log_text = stdout.decode(errors="replace")
            (workdir / "slicer.log").write_text(log_text, encoding="utf-8")
            produced = workdir / "out" / output.name
            report = _read_result(workdir / "out" / "result.json")
            if proc.returncode != 0 or not produced.exists():
                raise SliceError(_failure_text(report, log_text, proc.returncode))
            try:
                # Reading the result decompresses and scans every plate's G-code: keep it off the event loop.
                info = await asyncio.to_thread(_finish, produced, output, workdir)
            except JobError as exc:
                raise SliceError(message_of(exc)) from exc
            info["warnings"] = [m(w) if isinstance(w, str) else w for w in (report or {}).get("warnings", []) if w]
            info["plate"] = plate if kind == "project" else 1
            return info


def _finish(produced: Path, output: Path, workdir: Path) -> dict:
    """Move the result into place and read it; then drop the intermediate files, which are no longer needed."""
    shutil.move(produced, output)
    info = read_slice_info(output)
    shutil.rmtree(workdir / "datadir", ignore_errors=True)
    return info


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """End the slicer and everything it started."""
    with contextlib.suppress(ProcessLookupError):  # Already gone.
        os.killpg(proc.pid, signal.SIGKILL)
    await proc.wait()


def _read_result(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _failure_text(report: dict | None, log_text: str, code: int | None) -> str:
    if report:
        message = report.get("error_string") or report.get("message")
        if message:
            return m('Slicing failed: {p0}', p0=m(message))
        for plate in report.get("sliced_plates") or []:
            if plate.get("warning_message"):
                return m('Slicing failed: {p0}', p0=m(plate['warning_message']))
    lines = [line.strip() for line in log_text.splitlines() if line.strip()]
    interesting = [line for line in lines if any(w in line.lower() for w in ("error", "fail", "invalid", "outside", "empty"))]
    tail = (interesting or lines)[-3:]
    detail = " | ".join(tail)[:500] if tail else m('exit code {p0}', p0=code)
    return m('Slicing failed: {p0}', p0=detail)
