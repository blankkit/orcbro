"""Print jobs on bridge storage: upload → optional slicing → send to the printer.

Each job has `<data>/jobs/<id>/` containing `job.json`, the input file, sliced `output.gcode.3mf`
and thumbnail. Retain the latest `keep` jobs and prune older jobs."""
from __future__ import annotations

from .i18n import m, pack, unpack

import json
import re
import secrets
import shutil
import time
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

MODEL_SUFFIXES = (".stl", ".obj", ".step", ".stp", ".3mf")


class JobError(Exception):
    pass


@dataclass
class Job:
    id: str
    name: str
    filename: str
    kind: str  # model | project | sliced
    created_at: float
    state: str = "uploaded"  # uploaded | slicing | sliced | failed | sending | sent
    error: str | None = None
    updated_at: float = field(default_factory=time.time)
    settings: dict = field(default_factory=dict)
    result: dict | None = None
    sent: dict | None = None
    progress: float | None = None
    # Already on the SD card, imported from the card view; do not upload again.
    remote_path: str | None = None
    # Accepted but unconfirmed print plan (mapping, options, timestamp); survives restarts.
    pending: dict | None = None
    # Current sending step for the app, such as uploading or starting the printer.
    step: str | None = None
    # Plates from this slice result already started, for multi-plate jobs.
    sent_plates: list[int] = field(default_factory=list)
    # SD card name of the current slice result; further plates need no upload.
    uploaded: str | None = None
    # Serial number of the printer used for slicing and printing this job.
    printer: str | None = None

    def public(self) -> dict:
        out = asdict(self)
        out["thumbnail"] = bool(self.result and any(p.get("thumbnail") for p in self.result.get("plates", [])))
        return out


_JOB_FIELDS = {f.name for f in fields(Job)}


def classify(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith(".gcode.3mf"):
        return "sliced"
    if lower.endswith(".3mf"):
        return "project"
    if lower.endswith(MODEL_SUFFIXES):
        return "model"
    raise JobError(m('Only STL, OBJ, STEP and 3MF files are accepted.'))


def display_name(filename: str) -> str:
    name = re.sub(r"(\.gcode)?\.(3mf|stl|obj|step|stp)$", "", filename, flags=re.IGNORECASE)
    return name.strip() or m('Model')


def safe_stem(name: str) -> str:
    """ASCII filename without periods, slashes or special characters."""
    ascii_name = unicodedata.normalize("NFKD", name.replace("ß", "ss")).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9_-]+", "_", ascii_name).strip("_-")[:48] or "druck"


def remote_name(name: str, job_id: str) -> str:
    """SD card filename, made unique with the job ID."""
    return f"{safe_stem(name)}_{job_id[:6]}.gcode.3mf"


# Thumbnail preference: lit slicer image, unlit slicer image, then top view.
# Files may omit any of these depending on the slicer and export method.
THUMBNAILS = ("Metadata/plate_{n}.png", "Metadata/plate_no_light_{n}.png", "Metadata/top_{n}.png")


# Extraction limits: upload limits cover only compressed size. XML, JSON and thumbnails are small;
# G-code is read line by line and its size is bounded by the archive total.
MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 20_000
MAX_UNPACKED_BYTES = 8 * 1024 ** 3


def open_archive(path) -> zipfile.ZipFile:
    """Open a 3MF; reject archives with too many entries or an excessive uncompressed size."""
    archive = zipfile.ZipFile(path)
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_ENTRIES or sum(info.file_size for info in infos) > MAX_UNPACKED_BYTES:
        archive.close()
        raise JobError(m('The uncompressed 3MF is unusually large and will not be processed.'))
    return archive


def read_member(archive: zipfile.ZipFile, name: str, limit: int = MAX_METADATA_BYTES) -> bytes:
    """Read at most `limit` bytes, even if the header claims a smaller entry size."""
    info = archive.getinfo(name)
    if info.file_size > limit:
        raise JobError(m('“{p0}” in the 3MF is too large.', p0=name))
    with archive.open(info) as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise JobError(m('“{p0}” in the 3MF is too large.', p0=name))
    return data


def plate_thumbnail(names: set[str] | list[str], plate: int) -> str | None:
    """Best available plate thumbnail member name, or None."""
    present = set(names)
    return next((t.format(n=plate) for t in THUMBNAILS if t.format(n=plate) in present), None)


def read_slice_info(path: Path) -> dict:
    """Read plates, times, weights and filaments from sliced 3MF Metadata/slice_info.config."""
    with open_archive(path) as archive:
        names = set(archive.namelist())
        if "Metadata/slice_info.config" not in names:
            raise JobError(m('The 3MF contains no G-code (not sliced).'))
        root = ET.fromstring(read_member(archive, "Metadata/slice_info.config"))
        plates = []
        for plate in root.findall("plate"):
            meta = {m.get("key"): m.get("value") for m in plate.findall("metadata")}
            index = int(meta.get("index", "1"))
            if f"Metadata/plate_{index}.gcode" not in names:
                continue
            filaments = []
            for f in plate.findall("filament"):
                filaments.append({
                    "id": int(f.get("id", "1")),
                    "type": f.get("type"),
                    "color": f.get("color"),
                    "info_idx": f.get("tray_info_idx") or None,
                    "used_g": _float(f.get("used_g")),
                    "used_m": _float(f.get("used_m")),
                })
            plates.append({
                "index": index,
                "prediction_s": _int(meta.get("prediction")),
                "weight_g": _float(meta.get("weight")),
                # OrcaSlicer sets `support_used` as soon as supports are enabled. Only the G-code reveals whether
                # any support paths are actually printed when supports are generated as needed.
                "support_used": _has_support(archive, f"Metadata/plate_{index}.gcode"),
                "support_enabled": meta.get("support_used") == "true",
                "outside": meta.get("outside") == "true",
                "filaments": filaments,
                "thumbnail": plate_thumbnail(names, index),
                "objects": [o.get("name") for o in plate.findall("object")],
            })
    if not plates:
        raise JobError(m('The 3MF contains no sliced plates.'))
    return {"plates": plates}


def _has_support(archive: zipfile.ZipFile, name: str) -> bool:
    """Does the G-code contain support paths (`; FEATURE: Support`, `Support interface`, etc.)?"""
    with archive.open(name) as handle:
        return any(line.startswith(b"; FEATURE: Support") for line in handle)


def project_filaments(path: Path) -> list[dict]:
    """Filaments in an unsliced 3MF project, from Metadata/project_settings.config."""
    try:
        with open_archive(path) as archive:
            data = json.loads(read_member(archive, "Metadata/project_settings.config"))
    except (KeyError, ValueError, zipfile.BadZipFile):
        return []
    types = data.get("filament_type") or []
    colours = data.get("filament_colour") or []
    names = data.get("filament_settings_id") or []
    return [{"id": i + 1, "type": t, "color": colours[i] if i < len(colours) else None,
             "profile": names[i] if i < len(names) else None} for i, t in enumerate(types)]


INCOMPLETE_HINT = (m('The download usually has not finished or was interrupted. Download the file again (check its size under Info in Files), then open it again.'))


def check_complete(path: Path) -> None:
    """Reject truncated files during upload rather than failing later during slicing.

    3MF is ZIP: a missing end directory indicates truncation. Binary STL headers specify the number
    of 50-byte triangles; shorter files are incomplete. Text STL ends with `endsolid`."""
    size = path.stat().st_size
    mb = f"{size / 1_000_000:.1f}"
    lower = path.name.lower()
    if lower.endswith(".3mf"):
        try:
            with open_archive(path) as archive:
                archive.namelist()
        except zipfile.BadZipFile:
            raise JobError(m('The 3MF file is incomplete: It ends after {p0} MB. {p1}', p0=mb, p1=INCOMPLETE_HINT)) from None
    elif lower.endswith(".stl") and size >= 84:
        with path.open("rb") as handle:
            head = handle.read(84)
            handle.seek(max(0, size - 1024))
            tail = handle.read()
        triangles = int.from_bytes(head[80:84], "little")
        if 84 + 50 * triangles <= size:
            return  # Complete binary STL; some programs append extra data.
        if head.lstrip().lower().startswith(b"solid"):
            if b"endsolid" in tail.lower():
                return  # Complete text STL.
        raise JobError(m('The STL file is incomplete: It ends after {p0} MB. {p1}', p0=mb, p1=INCOMPLETE_HINT))


def project_printer(path: Path) -> dict | None:
    """Target printer of a 3MF project (Bambu Studio/OrcaSlicer Metadata/project_settings.config).

    Return None for 3MF without these settings, such as PrusaSlicer, CAD or downloaded model files."""
    try:
        with open_archive(path) as archive:
            data = json.loads(read_member(archive, "Metadata/project_settings.config"))
    except (KeyError, ValueError, zipfile.BadZipFile):
        return None
    if not isinstance(data, dict):
        return None
    nozzle = data.get("nozzle_diameter")
    if isinstance(nozzle, list):
        nozzle = nozzle[0] if nozzle else None
    return {"model": data.get("printer_model"), "settings_id": data.get("printer_settings_id"), "nozzle": _float(nozzle)}


def project_plates(path: Path) -> int:
    """3MF project plate count from Metadata/model_settings.config; at least one."""
    try:
        with open_archive(path) as archive:
            root = ET.fromstring(read_member(archive, "Metadata/model_settings.config"))
    except (KeyError, ET.ParseError, zipfile.BadZipFile):
        return 1
    return max(len(root.findall("plate")), 1)


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


class JobStore:
    def __init__(self, root: Path, keep: int = 40):
        self.root = Path(root) / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.keep = keep
        self.jobs: dict[str, Job] = {}
        for meta in self.root.glob("*/job.json"):
            try:
                data = unpack(json.loads(meta.read_text(encoding="utf-8")))
                # Ignore unknown fields from newer or older bridge versions.
                job = Job(**{k: v for k, v in data.items() if k in _JOB_FIELDS})
            except (OSError, ValueError, TypeError):
                continue
            # Restart interrupts active slicing. Accepted pending prints are resumed or rejected by
            # server.resume_pending_prints according to their age.
            if job.state == "slicing" or (job.state == "sending" and not job.pending):
                job.state, job.error, job.step = "failed", m('The bridge restarted.'), None
            self.jobs[job.id] = job

    def dir(self, job_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{16}", job_id):
            raise JobError(m('Invalid job ID.'))
        return self.root / job_id

    def get(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def list(self) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)

    def create(self, filename: str, printer: str | None = None) -> Job:
        kind = classify(filename)
        job = Job(id=secrets.token_hex(8), name=display_name(filename), filename=filename, kind=kind,
                  created_at=time.time(), printer=printer)
        self.dir(job.id).mkdir(parents=True)
        self.jobs[job.id] = job
        return job

    def input_path(self, job: Job) -> Path:
        """Readable input filename, which OrcaSlicer uses as the object name on the printer display."""
        suffix = ".gcode.3mf" if job.kind == "sliced" else Path(job.filename).suffix.lower()
        return self.dir(job.id) / f"{safe_stem(job.name)}{suffix}"

    def output_path(self, job: Job) -> Path:
        return self.input_path(job) if job.kind == "sliced" else self.dir(job.id) / "output.gcode.3mf"

    def save(self, job: Job) -> None:
        job.updated_at = time.time()
        path = self.dir(job.id) / "job.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(pack(asdict(job)), indent=1), encoding="utf-8")
        tmp.replace(path)

    def delete(self, job_id: str) -> None:
        self.jobs.pop(job_id, None)
        shutil.rmtree(self.dir(job_id), ignore_errors=True)

    def prune(self) -> None:
        for job in self.list()[self.keep:]:
            if job.state not in ("slicing", "sending"):
                self.delete(job.id)

    def thumbnail(self, job: Job, plate: int | None = None) -> bytes | None:
        if not job.result:
            return None
        output = self.output_path(job)
        plate = plate or job.result.get("plate") or 1
        member = next((p["thumbnail"] for p in job.result.get("plates", []) if p["index"] == plate and p.get("thumbnail")), None)
        if not member or not output.exists():
            return None
        with open_archive(output) as archive:
            return read_member(archive, member)


def read_objects(path: Path, plate: int) -> list[dict]:
    """Skippable plate objects: `identify_id` and name from slice_info.config, plus bed-mm outlines
    from plate_N.json. Match outlines by name and order because plate_N.json uses its own IDs.
    If matching fails, omit only the outlines."""
    with open_archive(path) as archive:
        names = set(archive.namelist())
        root = ET.fromstring(read_member(archive, "Metadata/slice_info.config"))
        target = None
        for element in root.findall("plate"):
            meta = {m.get("key"): m.get("value") for m in element.findall("metadata")}
            if int(meta.get("index", "0")) == plate:
                target = element
                break
        if target is None:
            raise JobError(m('Plate {p0} is not in the file.', p0=plate))
        objects = [{"id": int(o.get("identify_id")), "name": o.get("name") or m('Object {p0}', p0=o.get('identify_id')),
                    "skipped": o.get("skipped") == "true", "bbox": None}
                   for o in target.findall("object") if (o.get("identify_id") or "").isdigit()]
        member = f"Metadata/plate_{plate}.json"
        if member in names:
            try:
                boxes = json.loads(read_member(archive, member)).get("bbox_objects") or []
            except ValueError:
                boxes = []
            pending: dict[str, list[list[float]]] = {}
            for box in boxes:
                if isinstance(box.get("bbox"), list) and len(box["bbox"]) == 4:
                    pending.setdefault(box.get("name") or "", []).append([round(float(v), 2) for v in box["bbox"]])
            for obj in objects:
                queue = pending.get(obj["name"])
                if queue:
                    obj["bbox"] = queue.pop(0)
    return objects
