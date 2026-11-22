"""Bambu printer models from OrcaSlicer (`resources/printers/<model_id>.json`).

Each model file contains a display name (`display_name`, e.g. Bambu Lab P1S, matching profile
`printer_model`) and capabilities. Firmware layers start at `00.00.00.00`; higher versions add or
replace values from that firmware onward. OrcaSlicer updates add new models without a table here."""
from __future__ import annotations

from .i18n import m

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

LEGACY_MODELS = frozenset({"BL-P001", "BL-P002", "C13", "C11", "C12", "N1", "N2S"})
NEW_PRINT_MODELS = frozenset({"N7", "N9", "O1D", "O1E", "O1S", "O1C", "O1C2", "N6"})
DUAL_NOZZLE_MODELS = frozenset({"O1D", "O1E", "O1C", "O1C2", "N6"})
JPEG_CAMERA_MODELS = frozenset({"C11", "C12", "N1", "N2S", "N9"})
RTSPS_CAMERA_MODELS = frozenset({"BL-P001", "BL-P002", "C13", "N7", "O1D", "O1E", "O1S", "O1C", "O1C2", "N6"})
CHAMBER_SENSOR_MODELS = frozenset({"BL-P001", "BL-P002", "C13", "N7", "O1D", "O1E", "O1S", "O1C", "O1C2", "N6"})
CHAMBER_HEATER_MODELS = frozenset({"C13", "O1D", "O1E", "O1S", "O1C", "O1C2", "N6"})


def _version(text: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError:
        return (0,)


def _merge(base: dict, extra: dict) -> None:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


@dataclass(frozen=True)
class Model:
    model_id: str
    name: str
    layers: dict = field(repr=False, default_factory=dict)  # Firmware version → values.

    def info(self, firmware: str | None = None) -> dict:
        """Capabilities for a firmware version, or the newest known version if omitted."""
        limit = _version(firmware) if firmware else None
        out: dict = {}
        for version in sorted(self.layers, key=_version):
            if limit is not None and _version(version) > limit:
                break
            _merge(out, json.loads(json.dumps(self.layers[version])))
        return out

    def baseline(self) -> dict:
        """Only the safe baseline; unknown firmware does not inherit later capability guarantees."""
        base = self.layers.get("00.00.00.00", {})
        return json.loads(json.dumps(base))

    def capabilities(self, firmware: str | None = None, raw: dict | None = None) -> dict:
        """Stable app capabilities from model metadata and explicitly verified firmware bits."""
        data = self.info(firmware) if firmware else self.baseline()
        settings = data.get("print") or {}
        raw = raw or {}
        dual = self.model_id in DUAL_NOZZLE_MODELS
        chamber_sensor = self.model_id in CHAMBER_SENSOR_MODELS
        chamber_heater = self.model_id in CHAMBER_HEATER_MODELS and bool(
            settings.get("support_chamber_temp_edit"))
        nozzle_range = _range(settings.get("nozzle_temp_range"))
        bed_range = _range(settings.get("bed_temp_range"))
        if bed_range is None and settings.get("bed_temperature_limit") is not None:
            bed_range = [0, int(settings["bed_temperature_limit"])]
        chamber_range = _range(settings.get("support_chamber_temp_edit_range")) if chamber_heater else None
        fun, fun2 = _hex(raw.get("fun")), _hex(raw.get("fun2"))
        new_generation = self.model_id in NEW_PRINT_MODELS
        home = _integer(raw.get("home_flag"))
        motor_noise = bool(fun >> 10 & 1) if fun is not None else (
            bool(home >> 21 & 1) if home is not None else bool(settings.get("support_motor_noise_cali")))
        nozzles = ([{"id": 0, "name": m('Right')}, {"id": 1, "name": m('Left')}] if dual
                   else [{"id": 0, "name": m('Nozzle')}])
        return {
            "enclosed": bool(data.get("printer_is_enclosed")),
            "architecture": data.get("printer_arch"),
            "series": data.get("printer_series"),
            "temperature_limits": {"nozzle": nozzle_range, "bed": bed_range, "chamber": chamber_range},
            "chamber_sensor": chamber_sensor,
            "chamber_heater": chamber_heater,
            "camera": ("jpeg6000" if self.model_id in JPEG_CAMERA_MODELS else
                       "rtsps" if self.model_id in RTSPS_CAMERA_MODELS else None),
            "dual_nozzle": dual,
            "nozzles": nozzles,
            "new_print_protocol": self.model_id in NEW_PRINT_MODELS,
            "second_chamber_light": self.model_id.startswith("O1"),
            "calibrations": {
                "bed_leveling": bool(settings.get("support_bed_leveling")),
                "vibration": True,
                "motor_noise": motor_noise,
                "nozzle_offset": bool(settings.get("support_nozzle_offset_calibration")),
            },
            # New generation only: X1 also reports `fun`, but retains the established G-code behavior.
            "mqtt_homing": bool(new_generation and fun is not None and fun >> 32 & 1),
            "mqtt_axis": bool(new_generation and fun is not None and fun >> 38 & 1),
            "mqtt_bed_temperature": bool(new_generation and fun is not None and fun >> 39 & 1),
            "part_skip": bool(fun is not None and fun >> 49 & 1),
            "hotend_rack": bool(fun is not None and fun >> 60 & 1),
            "pa_mode": bool(fun2 is not None and fun2 >> 3 & 1),
        }


class Models:
    def __init__(self, directory: Path | None):
        self.by_id: dict[str, Model] = {}
        self.by_name: dict[str, Model] = {}
        self.by_type: dict[str, Model] = {}
        if directory is None or not Path(directory).is_dir():
            if directory is not None:
                log.warning(m('Printer models missing at %s'), directory)
            return
        for path in sorted(Path(directory).glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            base = data.get("00.00.00.00") if isinstance(data, dict) else None
            if not isinstance(base, dict) or not base.get("display_name"):
                continue  # For example, bambu_filament_ids.json.
            layers = {k: v for k, v in data.items() if isinstance(v, dict)}
            model = Model(base.get("model_id") or path.stem, base["display_name"], layers)
            self.by_id[model.model_id] = model
            # Multiple files per model (H2C: O1C, O1C2): the first name mapping wins.
            self.by_name.setdefault(model.name, model)
            self.by_type[str(base.get("printer_type") or model.model_id)] = model
            for alias in base.get("subseries") or []:
                self.by_type[str(alias)] = model

        # Firmware uses both spellings; OrcaSlicer names the model without a hyphen.
        x1c = self.by_id.get("BL-P001")
        if x1c:
            for alias in ("Bambu Lab X1-Carbon", "X1 Carbon", "X1-Carbon"):
                self.by_name.setdefault(alias, x1c)

    def find(self, name: str | None = None, model_id: str | None = None) -> Model | None:
        if model_id:
            clean_id = model_id.removesuffix("-V2")
            if clean_id in self.by_id:
                return self.by_id[clean_id]
            if model_id in self.by_type:
                return self.by_type[model_id]
        if name:
            clean = name.strip()
            return self.by_name.get(clean) or self.by_name.get(f"Bambu Lab {clean}") or self.by_type.get(clean)
        return None

    def names(self) -> list[str]:
        return sorted({model.name for model in self.by_id.values()})


def _range(value) -> list[int] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    try:
        return [int(value[0]), int(value[1])]
    except (TypeError, ValueError):
        return None


def _integer(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _hex(value) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None
