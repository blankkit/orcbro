"""Catalog of OrcaSlicer system profiles (resources/profiles/BBL).

Profiles inherit (`inherits`) and include G-code templates (`include`), so the CLI receives
fully resolved flat profiles. Select only profiles compatible with the configured printer
(`compatible_printers`) and map AMS slots to filament profiles by filament ID.

SUNLU and Polymaker profiles within BBL apply directly. OrcaFilamentLibrary and Creality/Anycubic
printer folders do not enable their products for Bambu printers. Port their material values
(temperature, fans, flow, density) onto Bambu Generic profiles of the same material. Retain
Bambu's printer-specific start G-code, pressure advance and retraction, and cap volumetric flow
at the Bambu profile's limit."""
from __future__ import annotations

from .i18n import m

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Metadata excluded when including a template (`include`).
_META = {"name", "type", "from", "instantiation", "setting_id", "include", "inherits"}

# App plate ID → OrcaSlicer `curr_bed_type` value.
BED_TYPES = {
    "textured_pei": "Textured PEI Plate",
    "smooth_pei": "High Temp Plate",
    "engineering": "Engineering Plate",
    "cool": "Cool Plate",
    "cool_supertack": "Supertack Plate",
}
BED_LABELS = {
    "textured_pei": m('Textured PEI Plate'),
    "smooth_pei": m('Smooth PEI Plate (High Temp)'),
    "engineering": m('Engineering Plate'),
    "cool": "Cool Plate",
    "cool_supertack": "Cool Plate SuperTack",
}

SUPPORT_TYPES = {"off": None, "tree": "tree(auto)", "normal": "normal(auto)"}

# Port product profiles from vendor directories beside BBL. Prefer fast CoreXY printer variants
# closest to the P1S; None selects profiles without a printer association.
VENDOR_SOURCES: dict[str, tuple[str, ...] | None] = {
    "OrcaFilamentLibrary": None,
    "Creality": ("K1C", "K1 ", "K1", "K2"),
    "Anycubic": ("Kobra S1", "Kobra 3 0.4"),
}

# Material values inherited from a ported vendor profile.
MATERIAL_KEYS = (
    "filament_type", "filament_density", "filament_diameter", "filament_cost", "filament_flow_ratio",
    "filament_soluble", "filament_is_support", "filament_shrink", "temperature_vitrification", "required_nozzle_HRC",
    "nozzle_temperature", "nozzle_temperature_initial_layer", "nozzle_temperature_range_low",
    "nozzle_temperature_range_high",
    "fan_min_speed", "fan_max_speed", "fan_cooling_layer_time", "slow_down_layer_time", "slow_down_min_speed",
    "close_fan_the_first_x_layers", "full_fan_speed_layer", "overhang_fan_speed", "overhang_fan_threshold",
    "reduce_fan_stop_start_freq",
)
# Copy plate temperatures only when positive; 0 means the plate is unsuitable.
PLATE_KEYS = tuple(f"{plate}_temp{suffix}" for plate in ("hot_plate", "textured_plate", "cool_plate", "eng_plate",
                                                          "supertack_plate", "textured_cool_plate")
                   for suffix in ("", "_initial_layer"))


class ProfileError(Exception):
    """Missing or incompatible profile; return the readable message to the app."""


@dataclass(frozen=True)
class Choice:
    name: str
    label: str


class Catalog:
    def __init__(self, root: Path, machine: str, filament_ids: Path | None = None):
        self.root = Path(root)
        self.machine_name = machine
        # Bambu Lab P1S 0.4 nozzle → P1S: name suffix for ported profiles (SUNLU PLA+ @SUNLU P1S).
        self.short_name = re.sub(r"\s+\d+(\.\d+)? nozzle$", "", re.sub(r"^Bambu Lab ", "", machine))
        self._raw: dict[tuple[str, str], dict] = {}
        self._cache: dict[tuple[str, str], dict] = {}
        self._flow_calibration: dict[str, bool | None] = {}
        for kind in ("machine", "process", "filament"):
            # Vendors such as SUNLU and Polymaker have subdirectories (filament/SUNLU/…).
            for name, data in _load_folder(self.root / kind).items():
                self._raw[(kind, name)] = data
        if ("machine", machine) not in self._raw:
            raise ProfileError(m('Printer profile “{p0}” is missing at {p1}.', p0=machine, p1=self.root))
        # AMS Bambu ID (e.g. GFA00) → OrcaSlicer ID (e.g. OFoiVqVM).
        self.bambu_to_orca: dict[str, str] = {}
        if filament_ids and filament_ids.exists():
            table = json.loads(filament_ids.read_text(encoding="utf-8")).get("filaments", {})
            self.bambu_to_orca = {v["bambu_id"]: k for k, v in table.items() if isinstance(v, dict) and v.get("bambu_id")}
        self.orca_to_bambu = {orca: bambu for bambu, orca in self.bambu_to_orca.items()}
        self.processes = self._compatible("process")
        self.filaments = self._compatible("filament")
        self._bambu_filaments = list(self.filaments)
        self._add_vendor_profiles()

    # --- Resolution ---

    def resolve(self, kind: str, name: str, _depth: int = 0) -> dict:
        key = (kind, name)
        if key in self._cache:
            return dict(self._cache[key])
        if _depth > 20:
            raise ProfileError(m('Profile inheritance is too deep at “{p0}”.', p0=name))
        data = self._raw.get(key)
        if data is None:
            raise ProfileError(m('Profile “{p0}” ({p1}) is missing.', p0=name, p1=kind))
        out: dict[str, Any] = {}
        if data.get("inherits"):
            out.update(self.resolve(kind, data["inherits"], _depth + 1))
        for include in data.get("include") or []:
            template = self._raw.get((kind, include))
            if template is None:
                raise ProfileError(m('Template “{p0}” for “{p1}” is missing.', p0=include, p1=name))
            out.update({k: v for k, v in template.items() if k not in _META})
        out.update({k: v for k, v in data.items() if k not in ("inherits", "include")})
        self._cache[key] = out
        return dict(out)

    def flow_calibration(self, model: str | None) -> bool | None:
        """Does the printer measure flow dynamics (K factor) before printing, making `flow_cali` effective?

        Its profile's start G-code must check `extrude_cali_flag` to run measurement (X1 Lidar;
        A1/H2/P2S force sensors). P1 lacks a sensor and does not check that flag. Identify by
        reported model (`printer_model`), preferring the configured profile. Return None for an
        unknown model or no matching profile with start G-code."""
        if not model:
            return None
        if model not in self._flow_calibration:
            names = [self.machine_name] + sorted(n for k, n in self._raw if k == "machine" and n != self.machine_name)
            found = None
            for name in names:
                try:
                    profile = self.resolve("machine", name)
                except ProfileError:
                    continue
                gcode = profile.get("machine_start_gcode")
                if profile.get("printer_model") == model and isinstance(gcode, str) and gcode:
                    found = "extrude_cali_flag" in gcode
                    break
            self._flow_calibration[model] = found
        return self._flow_calibration[model]

    def flat(self, kind: str, name: str) -> dict:
        """Fully resolved profile as loaded by the CLI."""
        out = self.resolve(kind, name)
        out["from"] = "system"
        out["inherits"] = ""
        return out

    def _add_vendor_profiles(self) -> None:
        """Port vendor profiles from directories beside BBL; skip missing directories (see module docs)."""
        labels = {_label(name) for name in self.filaments}
        added = 0
        for folder_name, prefer in VENDOR_SOURCES.items():
            folder = self.root.parent / folder_name / "filament"
            if not folder.is_dir():
                continue
            tree = _load_folder(folder)
            for label, name in sorted(_pick_products(tree, prefer).items()):
                if label in labels or label.lower().startswith("generic"):
                    continue
                try:
                    product = _resolve_tree(tree, name)
                except ProfileError:
                    continue
                vendor = _first(product.get("filament_vendor")) or ""
                if not vendor or vendor.lower() in ("generic", "bambu", "bambu lab"):
                    continue
                profile = self._transfer(product, f"{label} @{vendor} {self.short_name}")
                if profile is None:
                    continue
                self._raw[("filament", profile["name"])] = profile
                self.filaments.append(profile["name"])
                labels.add(label)
                added += 1
        self.filaments.sort()
        if added:
            log.info(m('Imported %d vendor profiles for %s'), added, self.machine_name)

    def _transfer(self, product: dict, name: str) -> dict | None:
        """Vendor profile for this printer: Bambu Generic material profile plus vendor material values."""
        base_name = self._generic(_first(product.get("filament_type")), self._bambu_filaments)
        if base_name is None:
            return None
        out = self.resolve("filament", base_name)
        for key in MATERIAL_KEYS:
            if key in product:
                out[key] = _shaped_like(out.get(key), product[key])
        for key in PLATE_KEYS:
            if (_first_float(product.get(key)) or 0) > 0:
                out[key] = _shaped_like(out.get(key), product[key])
        # Vendor hotends differ; never exceed the Bambu profile's volumetric flow limit.
        theirs = _first_float(product.get("filament_max_volumetric_speed"))
        ours = _first_float(out.get("filament_max_volumetric_speed"))
        if theirs and ours and theirs < ours:
            out["filament_max_volumetric_speed"] = [_number(theirs)]
        out.update({
            "name": name, "inherits": "", "from": "system", "instantiation": "true", "setting_id": "",
            "filament_settings_id": [name], "compatible_printers": [self.machine_name],
            "filament_vendor": product.get("filament_vendor"),
            "filament_id": product.get("filament_id") or out.get("filament_id"),
        })
        return out

    def _compatible(self, kind: str) -> list[str]:
        names = []
        for (k, name), data in self._raw.items():
            if k != kind or data.get("instantiation") != "true":
                continue
            try:
                resolved = self.resolve(kind, name)
            except ProfileError:
                continue
            if self.machine_name in (resolved.get("compatible_printers") or []):
                names.append(name)
        return sorted(names)

    # --- App choices ---

    def quality_choices(self) -> list[dict]:
        out = []
        for name in self.processes:
            p = self.resolve("process", name)
            label = re.sub(r"\s*@.*$", "", name)
            out.append({"name": name, "label": label, "layer_height": _first_float(p.get("layer_height"))})
        out.sort(key=lambda c: (c["layer_height"] or 0, c["label"]))
        return out

    def default_process(self) -> str:
        machine = self.resolve("machine", self.machine_name)
        wanted = machine.get("default_print_profile")
        if wanted in self.processes:
            return wanted
        if not self.processes:
            raise ProfileError(m('No process profile matches the printer.'))
        return self.processes[0]

    def filament_info(self, name: str) -> dict:
        if name not in self.filaments:
            raise ProfileError(m('Filament “{p0}” is not compatible with the printer.', p0=name))
        f = self.resolve("filament", name)
        orca_id = f.get("filament_id")
        return {
            "name": name,
            "label": re.sub(r"\s*@.*$", "", name),
            "type": _first(f.get("filament_type")),
            "vendor": _first(f.get("filament_vendor")),
            "filament_id": orca_id,
            # ID stored by the AMS (tray_info_idx): Bambu IDs such as GFA00, otherwise the OrcaSlicer ID.
            "bambu_id": self.orca_to_bambu.get(orca_id or "", orca_id),
            "nozzle_temp_min": _first_int(f.get("nozzle_temperature_range_low")),
            "nozzle_temp_max": _first_int(f.get("nozzle_temperature_range_high")),
        }

    def drying_preset(self, name: str) -> dict | None:
        """Filament drying presets for AMS 2 Pro and AMS HT, following OrcaSlicer's DevUtilBackend.cpp:
        temperature and hours per AMS type, idle/printing; softening point as cooling target;
        heat distortion temperature as upper limit. None if no preset exists."""
        try:
            f = self.resolve("filament", name)
        except ProfileError:
            return None
        temps, hours = _floats(f.get("filament_dev_ams_drying_temperature")), _floats(f.get("filament_dev_ams_drying_time"))
        if len(temps) < 4 or len(hours) < 4:
            return None
        return {
            "filament": name,
            "type": _first(f.get("filament_type")),
            "ams_2_pro": {"idle": {"temperature": temps[0], "hours": hours[0]},
                          "printing": {"temperature": temps[2], "hours": hours[2]}},
            "ams_ht": {"idle": {"temperature": temps[1], "hours": hours[1]},
                       "printing": {"temperature": temps[3], "hours": hours[3]}},
            "softening_temperature": _first_float(f.get("filament_dev_drying_softening_temperature")),
            "heat_distortion_temperature": _first_float(f.get("filament_dev_ams_drying_heat_distortion_temperature")),
        }

    def filament_choices(self) -> list[dict]:
        return [self.filament_info(name) for name in self.filaments]

    def bed_size(self) -> list[float]:
        """Printable area in mm from the printer profile's `printable_area`."""
        points = self.resolve("machine", self.machine_name).get("printable_area") or []
        xs, ys = [], []
        for point in points:
            try:
                x, y = (float(v) for v in str(point).split("x"))
            except ValueError:
                continue
            xs.append(x)
            ys.append(y)
        return [max(xs), max(ys)] if xs and ys else [256.0, 256.0]

    def filament_for_tray(self, info_idx: str | None, material: str | None) -> str:
        """AMS slot filament profile: prefer filament ID, otherwise `Generic <material>`."""
        orca_id = self.bambu_to_orca.get(info_idx or "", info_idx)
        if orca_id:
            for name in self.filaments:
                if self.resolve("filament", name).get("filament_id") == orca_id:
                    return name
        found = self._generic(material, self._bambu_filaments)
        if found:
            return found
        raise ProfileError(m('No filament profile found for {p0} ({p1}).', p0=material or m('unknown material'), p1=info_idx or m('no ID')))

    def _generic(self, material: str | None, names: list[str]) -> str | None:
        """Bambu Generic profile for a material, otherwise its most general profile."""
        if not material:
            return None
        base = f"generic {material.lower()}"
        # Generic PETG or Generic PETG @BBL …, excluding Generic PETG HF ….
        generic = sorted((n for n in names if n.lower() == base or n.lower().startswith(base + " @")), key=len)
        if generic:
            return generic[0]
        by_type = [n for n in names if (_first(self.resolve("filament", n).get("filament_type")) or "").lower() == material.lower()]
        if by_type:
            return sorted(by_type, key=lambda n: (not n.lower().startswith("generic"), len(n)))[0]
        return None

    # --- CLI settings ---

    def write_settings(self, directory: Path, *, process: str | None, filaments: list[str], options: dict,
                       colors: list[str | None] | None = None) -> tuple[list[Path], list[Path]]:
        """Write machine, process and filament profiles for a slice job and return their paths."""
        directory.mkdir(parents=True, exist_ok=True)
        process = process or self.default_process()
        if process not in self.processes:
            raise ProfileError(m('Quality “{p0}” is not compatible with the printer.', p0=process))
        machine = self.flat("machine", self.machine_name)
        proc = self.flat("process", process)
        apply_options(proc, options)
        paths = [directory / "machine.json", directory / "process.json"]
        paths[0].write_text(json.dumps(machine, indent=1), encoding="utf-8")
        paths[1].write_text(json.dumps(proc, indent=1), encoding="utf-8")
        filament_paths = []
        for i, name in enumerate(filaments):
            if name not in self.filaments:
                raise ProfileError(m('Filament “{p0}” is not compatible with the printer.', p0=name))
            path = directory / f"filament_{i + 1}.json"
            flat = self.flat("filament", name)
            color = colors[i] if colors and i < len(colors) else None
            if color and re.fullmatch(r"#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", color):
                # Actual spool color, included in the 3MF and shown on the printer display.
                flat["filament_colour"] = [color[:7].upper()]
            path.write_text(json.dumps(flat, indent=1), encoding="utf-8")
            filament_paths.append(path)
        return paths, filament_paths


def apply_options(proc: dict, options: dict) -> None:
    """Apply app settings to a flat process profile; reject unknown values."""
    bed = options.get("plate")
    if bed is not None:
        if bed not in BED_TYPES:
            raise ProfileError(m('Unknown build plate “{p0}”.', p0=bed))
        proc["curr_bed_type"] = BED_TYPES[bed]
    support = options.get("support")
    if support is not None:
        if support not in SUPPORT_TYPES:
            raise ProfileError(m('Unknown support structure “{p0}”.', p0=support))
        proc["enable_support"] = "0" if support == "off" else "1"
        if SUPPORT_TYPES[support]:
            proc["support_type"] = SUPPORT_TYPES[support]
    infill = options.get("infill")
    if infill is not None:
        if not isinstance(infill, (int, float)) or not 0 <= infill <= 100:
            raise ProfileError(m('Infill must be between 0 and 100%.'))
        proc["sparse_infill_density"] = f"{int(infill)}%"
    walls = options.get("walls")
    if walls is not None:
        if not isinstance(walls, int) or not 1 <= walls <= 10:
            raise ProfileError(m('Walls: 1 to 10.'))
        proc["wall_loops"] = str(walls)
    brim = options.get("brim")
    if brim is not None:
        proc["brim_type"] = "auto_brim" if brim else "no_brim"


def machine_profiles(root: Path) -> dict[tuple[str, str], str]:
    """Profiles by (model, nozzle): (Bambu Lab P1S, 0.4) → Bambu Lab P1S 0.4 nozzle."""
    tree = _load_folder(Path(root) / "machine")
    out: dict[tuple[str, str], str] = {}
    for name, data in tree.items():
        if data.get("instantiation") != "true":
            continue
        try:
            resolved = _resolve_tree(tree, name)
        except ProfileError:
            continue
        model, variant = resolved.get("printer_model"), resolved.get("printer_variant")
        if isinstance(model, str) and isinstance(variant, str):
            out.setdefault((model, variant), name)
    return out


def nozzle_variant(diameter: float | None) -> str:
    """Reported nozzle diameter (0.4, 0.6, etc.) → profile variant ("0.4")."""
    return f"{float(diameter):.1f}" if diameter else "0.4"


def _load_folder(folder: Path) -> dict[str, dict]:
    """All profiles in a directory and its subdirectories, indexed by name."""
    out: dict[str, dict] = {}
    for path in sorted(folder.rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning(m('Cannot read profile %s: %s'), path, exc)
            continue
        if isinstance(data, dict) and data.get("name"):
            out[data["name"]] = data
    return out


def _resolve_tree(tree: dict[str, dict], name: str, depth: int = 0) -> dict:
    """Resolve inheritance within a vendor directory."""
    if depth > 20:
        raise ProfileError(m('Profile inheritance is too deep at “{p0}”.', p0=name))
    data = tree.get(name)
    if data is None:
        raise ProfileError(m('Profile “{p0}” is missing.', p0=name))
    out = _resolve_tree(tree, data["inherits"], depth + 1) if data.get("inherits") else {}
    out.update({k: v for k, v in data.items() if k != "inherits"})
    return out


def _pick_products(tree: dict[str, dict], prefer: tuple[str, ...] | None) -> dict[str, str]:
    """One instantiable variant per product: first preferred printer variant, otherwise shortest name."""
    variants: dict[str, list[str]] = {}
    for name, data in tree.items():
        if data.get("instantiation") == "true":
            variants.setdefault(_label(name), []).append(name)

    def rank(name: str) -> tuple:
        hit = next((i for i, p in enumerate(prefer or ()) if p in name), len(prefer or ()))
        return hit, len(name), name

    return {label: min(names, key=rank) for label, names in variants.items()}


def _label(name: str) -> str:
    return re.sub(r"\s*@.*$", "", name)


def _shaped_like(template: Any, value: Any) -> Any:
    """Match the template's value shape; some vendors use scalars where Bambu expects lists."""
    if isinstance(template, list) and not isinstance(value, list):
        return [value]
    if template is not None and not isinstance(template, list) and isinstance(value, list):
        return value[0] if value else template
    return value


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _floats(value: Any) -> list[float]:
    items = value if isinstance(value, list) else ([] if value is None else str(value).split(","))
    out = []
    for item in items:
        try:
            out.append(float(item))
        except (TypeError, ValueError):
            return []
    return out


def _first(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _first_int(value: Any) -> int | None:
    try:
        return int(float(_first(value)))
    except (TypeError, ValueError):
        return None


def _first_float(value: Any) -> float | None:
    try:
        return float(_first(value))
    except (TypeError, ValueError):
        return None
