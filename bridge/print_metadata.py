"""Additional print metadata from sliced Bambu/OrcaSlicer 3MF files.

`slice_info.config` lists only filaments used on a plate. MQTT mapping needs the complete project
list. Read nozzle assignments only when explicitly stored; never guess a side for dual nozzles."""
from __future__ import annotations

from .i18n import m

import json
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from .jobs import JobError, open_archive, read_member


NOZZLE_KEYS = ("filament_nozzle_id", "filament_extruder_id", "filament_nozzle", "filament_map")
NOZZLE_ATTRIBUTES = ("nozzle_id", "extruder_id", "nozzle", "hotend")


def read_print_metadata(path: Path, plate_index: int) -> dict:
    """Read full filament IDs, used IDs and explicitly saved nozzle IDs."""
    try:
        with open_archive(path) as archive:
            root = ET.fromstring(read_member(archive, "Metadata/slice_info.config"))
            settings = _settings(archive)
    except (KeyError, ValueError, ET.ParseError, zipfile.BadZipFile) as exc:
        raise JobError(m('Cannot read print metadata from the 3MF.')) from exc

    plates = {int(_metadata(plate).get("index", "1")): plate for plate in root.findall("plate")}
    plate = plates.get(plate_index)
    if plate is None:
        raise JobError(m('Plate not found in print metadata.'))
    all_ids = {int(item.get("id", "0")) for p in plates.values() for item in p.findall("filament")
               if str(item.get("id", "")).isdigit() and int(item.get("id", "0")) > 0}
    types = settings.get("filament_type") if isinstance(settings.get("filament_type"), list) else []
    count = max([len(types), *all_ids], default=0)
    if count <= 0 or count > 32:
        raise JobError(m('The complete filament list in the 3MF is missing or too large.'))
    nozzles = _nozzle_list(settings, count)
    used = []
    per_plate = {}
    for item in plate.findall("filament"):
        try:
            filament_id = int(item.get("id", ""))
        except ValueError:
            continue
        used.append(filament_id)
        explicit = next((item.get(key) for key in NOZZLE_ATTRIBUTES if item.get(key) is not None), None)
        if explicit is not None:
            try:
                per_plate[filament_id] = int(explicit)
            except ValueError:
                pass
    filaments = []
    for filament_id in range(1, count + 1):
        nozzle = per_plate.get(filament_id, nozzles[filament_id - 1] if filament_id <= len(nozzles) else None)
        filaments.append({"id": filament_id, "used": filament_id in used, "nozzle_id": nozzle})
    return {"filaments": filaments, "used": sorted(set(used))}


def _settings(archive: zipfile.ZipFile) -> dict:
    try:
        data = json.loads(read_member(archive, "Metadata/project_settings.config"))
    except (KeyError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _metadata(plate: ET.Element) -> dict:
    return {item.get("key"): item.get("value") for item in plate.findall("metadata")}


# `filament_map` uses Bambu Studio's one-based extruders (1 left, 2 right); printers report 0 right, 1 left.
# Not verified on hardware.
FILAMENT_MAP_TO_EXTRUDER = {1: 1, 2: 0}


def _nozzle_list(settings: dict, count: int) -> list[int | None]:
    key = next((key for key in NOZZLE_KEYS if key in settings), None)
    raw = settings[key] if key else []
    if isinstance(raw, str):
        raw = [item for item in re.split(r"[\s,;]+", raw.strip()) if item]
    if not isinstance(raw, list):
        return [None] * count
    out = []
    for value in raw[:count]:
        try:
            number = int(value)
        except (TypeError, ValueError):
            out.append(None)
            continue
        if key == "filament_map":
            out.append(FILAMENT_MAP_TO_EXTRUDER.get(number))
        else:
            out.append(number if number in (0, 1) else None)
    return out + [None] * (count - len(out))
