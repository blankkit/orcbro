"""Printer status: merge partial printer updates and normalize them for the app.

After `pushall`, P1/A1 printers send only changed fields. Merge updates into raw state (`merge`),
then expose a stable, flat app view (`normalize`)."""
from __future__ import annotations

from .i18n import m

import re
import time
from typing import Any

from .models import DUAL_NOZZLE_MODELS, CHAMBER_SENSOR_MODELS

# mc_print_stage/stg_cur → readable stage. Protocol values with our own text.
STAGES: dict[int, str] = {
    0: m('Printing'),
    1: m('Auto bed leveling'),
    2: m('Preheating the bed'),
    3: m('Vibration compensation'),
    4: m('Changing filament'),
    5: m('Paused (M400)'),
    6: m('Paused: Out of filament'),
    7: m('Heating the nozzle'),
    8: m('Calibrating extrusion'),
    9: m('Scanning the bed'),
    10: m('Inspecting the first layer'),
    11: m('Detecting the build plate'),
    12: m('Calibrating Micro-Lidar'),
    13: m('Homing the toolhead'),
    14: m('Cleaning the nozzle tip'),
    15: m('Checking the extruder temperature'),
    16: m('Paused by the user'),
    17: m('Paused: Front cover fell off'),
    18: m('Calibrating Micro-Lidar'),
    19: m('Calibrating extrusion flow'),
    20: m('Paused: Nozzle temperature fault'),
    21: m('Paused: Bed temperature fault'),
    22: m('Unloading filament'),
    23: m('Paused: Lost steps'),
    24: m('Loading filament'),
    25: m('Calibrating motor noise'),
    26: m('Paused: AMS disconnected'),
    27: m('Paused: Hotend fan speed too low'),
    28: m('Paused: Chamber temperature fault'),
    29: m('Cooling the chamber'),
    30: m('Paused by G-code'),
    31: m('Motor noise demonstration'),
    32: m('Paused: Nozzle covered in filament'),
    33: m('Paused: Cutter error'),
    34: m('Paused: First layer error'),
    35: m('Paused: Nozzle clogged'),
    40: m('High-temperature bed leveling'),
    47: m('Bed leveling, phase 1'),
    48: m('Bed leveling, phase 2'),
    50: m('Cooling the bed'),
    54: m('Waiting for the bed temperature'),
}

# Calibration started like Bambu Studio (`print.calibration`, `option` bitmask), running the printer's
# auto_cali_for_user.gcode file.
CALIBRATION_FILE = "auto_cali_for_user"

GCODE_STATES = {"IDLE", "PREPARE", "RUNNING", "PAUSE", "FINISH", "FAILED", "SLICING", "INIT", "OFFLINE"}

SPEED_LEVELS = {1: m('Silent'), 2: "Standard", 3: "Sport", 4: "Turbo"}

# External spool's global slot index (vt_tray).
EXTERNAL_SLOT = 254

# Authorization checks since P1 firmware 01.08.02 require signed commands; Developer Mode disables them.
# `fun` bit 29 reports the requirement. P1S omits `fun`, using `flag3` bit 4 (bit 16: checks available).
# Values follow ha-bambulab and were verified on P1S.
FUN_SIGNATURE_REQUIRED = 0x20000000
FLAG3_SIGNATURE_REQUIRED = 0x10
# MQTT command verification failed HMS: the printer rejected an unsigned command.
HMS_COMMAND_REJECTED = "0500_0500_0001_0007"


def merge(base: dict, update: dict) -> dict:
    """Recursively merge `update` into `base` in place and return `base`.

    Merge object lists by `id` (AMS units/slots), because printers send changed entries only.
    All other lists replace the previous value. A bare AMS slot (`{"id"}`, at most with `state`) means the slot was
    emptied or holds a spool without known filament: it replaces the old entry, so the previous spool's fields do
    not linger (the P1 series reports a removed spool this way)."""
    for key, value in update.items():
        old = base.get(key)
        if isinstance(value, dict) and isinstance(old, dict):
            merge(old, value)
        elif _is_id_list(value) and _is_id_list(old):
            by_id = {str(item.get("id")): item for item in old}
            merged = []
            for item in value:
                prior = by_id.pop(str(item.get("id")), None)
                bare = key == "tray" and set(item) <= {"id", "state"}
                merged.append(merge(prior, item) if prior is not None and not bare else item)
            # Preserve entries missing from a partial update, appending them in their previous order.
            merged.extend(by_id.values())
            merged.sort(key=lambda item: _sort_id(item.get("id")))
            base[key] = merged
        else:
            base[key] = value
    return base


def _hex(value: Any) -> int | None:
    try:
        return int(str(value), 16) if value is not None else None
    except ValueError:
        return None


def _present(tray: dict, exist_bits: int | None, slots: int) -> dict:
    """Whether a spool physically sits in the slot (`tray_exist_bits`, bit ams * 4 + slot of 4-slot units). With
    `present` and no `material` the AMS knows the spool but not its filament (Bambu Studio shows "?"). Unknown for
    other units and printers without the bits."""
    ams_id, tray_id = tray.get("ams"), tray.get("tray")
    known = exist_bits is not None and slots == 4 and isinstance(ams_id, int) and ams_id < 128 and isinstance(tray_id, int)
    tray["present"] = bool(exist_bits >> (ams_id * 4 + tray_id) & 1) if known else None
    return tray


def _is_id_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(i, dict) and "id" in i for i in value)


def _sort_id(value: Any) -> tuple[int, str]:
    try:
        return (int(value), "")
    except (TypeError, ValueError):
        return (1 << 30, str(value))


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def color(value: Any) -> str | None:
    """Printer `RRGGBBAA` → `#RRGGBBAA`; empty or invalid colors return None."""
    if not isinstance(value, str) or len(value) not in (6, 8):
        return None
    try:
        int(value, 16)
    except ValueError:
        return None
    return "#" + value.upper()


def fan_percent(value: Any) -> int | None:
    """Printer fan level 0–15 → percent, rounded in tens as in Bambu Studio."""
    raw = _int(value)
    if raw is None:
        return None
    return int(round(raw / 15 * 10)) * 10


def humidity_letter(level: int | None) -> str | None:
    """AMS humidity level 1–5 → displayed letter. 5 is driest (A), 1 is most humid (E)."""
    if level is None or not 1 <= level <= 5:
        return None
    return "ABCDE"[5 - level]


def homed_axes(flag) -> dict | None:
    """Homed axes: `home_flag` bits 0 = X, 1 = Y, 2 = Z; higher bits indicate other capabilities.
    None until reported, treated as not homed."""
    value = _int(flag)
    if value is None:
        return None
    return {"x": bool(value & 1), "y": bool(value >> 1 & 1), "z": bool(value >> 2 & 1)}


def hms_code(entry: dict) -> str | None:
    """HMS (`attr`, `code`) → readable `XXXX_XXXX_XXXX_XXXX` as in Bambu Studio and the wiki."""
    attr, code = _int(entry.get("attr")), _int(entry.get("code"))
    if attr is None or code is None:
        return None
    return f"{attr >> 16:04X}_{attr & 0xFFFF:04X}_{code >> 16:04X}_{code & 0xFFFF:04X}"


HMS_SEVERITY = {1: "fatal", 2: "serious", 3: "common", 4: "info"}


def developer_mode(p: dict, hms: list[dict]) -> bool | None:
    """Does the printer accept unsigned commands? True means Developer Mode or firmware without
    authorization checks; False means bridge commands are rejected; None means not reported.

    Prefer `fun` and `flag3`. Use command-rejected HMS only when neither is available: the HMS
    message can remain after Developer Mode is enabled and would otherwise block all commands."""
    fun = p.get("fun")
    if isinstance(fun, str) and fun.strip():
        try:
            return not int(fun, 16) & FUN_SIGNATURE_REQUIRED
        except ValueError:
            pass
    flag3 = _int(p.get("flag3"))
    if flag3 is not None:
        return not flag3 & FLAG3_SIGNATURE_REQUIRED
    if any(entry["code"] == HMS_COMMAND_REJECTED for entry in hms):
        return False
    return None


def print_error(p: dict) -> dict | None:
    """Numeric `print_error` → Bambu Studio code (`0300_400C`); 0 means no message."""
    value = _int(p.get("print_error"), 0) or 0
    if value <= 0:
        return None
    raw = f"{value:08X}"
    return {"code": f"{raw[:4]}_{raw[4:]}", "value": value}


def print_options(p: dict, has_ams: bool) -> dict:
    """Print options as in Bambu Studio; None until reported by the printer.

    `home_flag` bit 4: recover after lost steps; bit 7: estimate spool remainder; bit 10: AMS
    backup with an equivalent spool. RFID flags live in AMS `power_on_flag` and `insert_flag`."""
    home = _int(p.get("home_flag"))
    ams = p.get("ams") or {}

    def bit(n: int) -> bool | None:
        return None if home is None else bool(home >> n & 1)

    def flag(key: str) -> bool | None:
        value = ams.get(key)
        return value if isinstance(value, bool) else None

    remain = flag("calibrate_remain_flag")
    return {
        "auto_recovery": bit(4),
        "ams_backup": bit(10) if has_ams else None,
        "rfid_on_startup": flag("power_on_flag") if has_ams else None,
        "rfid_on_insert": flag("insert_flag") if has_ams else None,
        "remain_estimate": (remain if remain is not None else bit(7)) if has_ams else None,
    }


def stage_plan(p: dict) -> list[dict]:
    """Planned calibration/preparation stages (`stg`): ordered [{"code", "text"}]."""
    out = []
    for item in p.get("stg") or []:
        # Usually numbers; older firmware sends objects containing the stage number as a value.
        for value in (item.values() if isinstance(item, dict) else [item]):
            code = _int(value)
            if code is not None:
                out.append({"code": code, "text": STAGES.get(code)})
    return out


def plate_index(p: dict) -> int | None:
    """Current print plate: `plate_idx`, otherwise parse `gcode_file` (…/plate_2.gcode)."""
    value = _int(p.get("plate_idx"))
    if value and value > 0:
        return value
    match = re.search(r"plate_(\d+)\.gcode", str(p.get("gcode_file") or ""))
    return int(match.group(1)) if match else None


AMS_TYPES = {1: "ams", 2: "ams_lite", 3: "ams_2_pro", 4: "ams_ht", 5: "ams_lite_mixed"}

# AMS 2 Pro/AMS HT drying, following OrcaSlicer DevFilaSystem.cpp/h: `info` bits 4–7 = state,
# bits 22–23 = phase; `dry_time` = remaining minutes; `dry_setting` = settings;
# `dry_sf_reason` = blocking reasons. Remote drying requires `fun2` bit 5.
DRY_LIMITS = {3: (45, 65), 4: (45, 85)}  # °C by AMS type, matching OrcaSlicer's drying dialog.
DRY_HOURS = (1, 24)
DRY_STATUS = {0: "off", 1: "checking", 2: "drying", 3: "cooling", 4: "stopping", 5: "error",
              6: "heat_out_of_control", 7: "testing"}
DRY_PHASE = {1: "heating", 2: "dehumidifying"}
DRY_BLOCKED = {0: "task_occupied", 1: "insufficient_power", 2: "ams_busy", 3: "filament_at_outlet",
               4: "initiating", 5: "not_in_2d_mode", 6: "drying_in_progress", 7: "upgrading",
               8: "needs_plugin_power", 10: "filament_at_outlet_manual_unload"}


def remote_drying(p: dict) -> bool:
    try:
        return bool(int(str(p.get("fun2")), 16) >> 5 & 1)
    except (TypeError, ValueError):
        return False


def drying(unit: dict, type_id: int | None, remote: bool) -> dict | None:
    """Unit drying state; None for unheated units (AMS, AMS lite)."""
    if type_id not in DRY_LIMITS:
        return None
    low, high = DRY_LIMITS[type_id]
    try:
        info = int(str(unit.get("info")), 16)
    except (TypeError, ValueError):
        info = None
    status = DRY_STATUS.get(info >> 4 & 0xF) if remote and info is not None else None
    settings = unit.get("dry_setting") if isinstance(unit.get("dry_setting"), dict) else {}
    temperature, hours = _int(settings.get("dry_temperature")), _int(settings.get("dry_duration"))
    remaining = _int(unit.get("dry_time"))
    reasons = unit.get("dry_sf_reason") if isinstance(unit.get("dry_sf_reason"), list) else []
    return {
        "supported": remote,
        "status": status,
        "phase": DRY_PHASE.get(info >> 22 & 0x3) if remote and info is not None and status == "drying" else None,
        "remaining_minutes": remaining if remaining and remaining > 0 else None,
        "settings": ({"filament": settings.get("dry_filament") or None, "temperature": temperature, "hours": hours}
                     if temperature is not None and temperature >= 0 else None),
        "blocked": [DRY_BLOCKED.get(code, f"code_{code}") for code in (_int(r) for r in reasons) if code is not None],
        "temperature_range": [low, high],
        "hours_range": list(DRY_HOURS),
    }


def _ams_info(value) -> tuple[int | None, int | None, int | None]:
    """`ams.info` → type, fixed extruder binding and track-switch input."""
    try:
        info = int(str(value), 16)
    except (TypeError, ValueError):
        return None, None, None
    binding = info >> 8 & 0xF
    return info & 0xF, (binding if binding in (0, 1) else None), (info >> 24 & 0xF if binding == 0xE else None)


def _public_slot(ams_id: int, tray_id: int, *, dual: bool) -> int:
    if ams_id == 16:
        return 24 + tray_id
    if 128 <= ams_id <= 135:
        return ams_id + tray_id
    if ams_id == 254:
        return 254
    if ams_id == 255:
        return 255 if dual else EXTERNAL_SLOT
    return ams_id * 4 + tray_id


def _tray(raw: dict, ams_id: int | None, *, dual: bool = False, extruder_id: int | None = None) -> dict:
    tray_id = _int(raw.get("id"), 0) or 0
    source_ams = 255 if ams_id is None else ams_id
    source_tray = 0 if ams_id is None else tray_id
    slot = _public_slot(source_ams, source_tray, dual=dual)
    material = raw.get("tray_type") or None
    remain = _int(raw.get("remain"))
    colors = [c for c in (color(c) for c in raw.get("cols") or []) if c]
    spool_uuid = str(raw.get("tray_uuid") or "").upper()
    spool_weight = _int(raw.get("tray_weight"))
    return {
        "slot": slot,
        "ams": ams_id,
        "tray": tray_id if ams_id is not None else None,
        "source": {"ams_id": source_ams, "slot_id": source_tray},
        "extruder_id": extruder_id,
        "empty": not material,
        "material": material,
        "name": raw.get("tray_sub_brands") or material,
        "info_idx": raw.get("tray_info_idx") or None,
        "color": color(raw.get("tray_color")),
        "colors": colors,
        # `ctype` 0 = gradient, 1 = segments, 2 = single color (Bambu Studio `DevFilaColorType`).
        "gradient": len(colors) > 1 and _int(raw.get("ctype")) == 0,
        # -1 = unknown (no Bambu spool chip or remainder estimation disabled).
        "remain": remain if remain is not None and remain >= 0 else None,
        "nozzle_temp_min": _int(raw.get("nozzle_temp_min")),
        "nozzle_temp_max": _int(raw.get("nozzle_temp_max")),
        "k": _num(raw.get("k")),
        # Selected printer K value entry; -1 uses the filament profile default.
        "cali_idx": _int(raw.get("cali_idx")),
        "rfid": bool(raw.get("tag_uid")) and set(str(raw.get("tag_uid"))) != {"0"},
        # Bambu spools: unique spool ID and filament weight of the full spool (grams), from the chip.
        "spool_uuid": spool_uuid if spool_uuid and set(spool_uuid) != {"0"} else None,
        "spool_weight": spool_weight if spool_weight and spool_weight > 0 else None,
        "present": None,  # Set for AMS slots from `tray_exist_bits`.
    }


def packed_temperature(value) -> tuple[float | None, float | None]:
    """Packed 16-bit temperatures → actual/target; zero remains a valid reading."""
    number = _int(value)
    if number is None or number < 0:
        return None, None
    return float(number & 0xFFFF), float(number >> 16 & 0xFFFF)


def _model_id(printer: dict) -> str | None:
    model_id = printer.get("model_id")
    if model_id:
        return str(model_id)
    return MODEL_IDS_BY_NAME.get(printer.get("model"))


def model_id_from_name(name: str | None) -> str | None:
    """Product name → model ID even without OrcaSlicer data (Bambu Lab P1S → C12)."""
    return MODEL_IDS_BY_NAME.get(name)


MODEL_IDS_BY_NAME = {
    "Bambu Lab X1 Carbon": "BL-P001", "Bambu Lab X1-Carbon": "BL-P001", "Bambu Lab X1": "BL-P002",
    "Bambu Lab X1E": "C13", "Bambu Lab P1P": "C11", "Bambu Lab P1S": "C12",
    "Bambu Lab P2S": "N7", "Bambu Lab A1 mini": "N1", "Bambu Lab A1": "N2S", "Bambu Lab A2L": "N9",
    "Bambu Lab H2D": "O1D", "Bambu Lab H2D Pro": "O1E", "Bambu Lab H2S": "O1S",
    "Bambu Lab H2C": "O1C", "Bambu Lab X2D": "N6",
}


def normalize(raw: dict, *, connected: bool, last_message_at: float | None, printer: dict) -> dict:
    """Raw `print` object → stable app status."""
    p = raw
    state = p.get("gcode_state") or ("OFFLINE" if not connected else "UNKNOWN")
    stage_code = _int(p.get("stg_cur"))
    stage = STAGES.get(stage_code) if stage_code not in (None, -1, 255) else None
    if state != "RUNNING" and stage_code == 0:
        stage = None

    ams_block = p.get("ams") or {}
    model_id = _model_id(printer)
    dual = model_id in DUAL_NOZZLE_MODELS
    remote = remote_drying(p)
    units = []
    exist_bits = _hex(ams_block.get("tray_exist_bits"))
    for unit in ams_block.get("ams") or []:
        ams_id = _int(unit.get("id"), 0) or 0
        type_id, extruder_id, track_switch = _ams_info(unit.get("info"))
        level = _int(unit.get("humidity"))
        if type_id in (2, 5):
            level = None  # AMS lite has no humidity sensor.
        units.append({
            "id": ams_id,
            "type": AMS_TYPES.get(type_id),
            "extruder_id": extruder_id,
            "track_switch": track_switch,
            "humidity_level": level,
            "humidity_letter": humidity_letter(level),
            "humidity_percent": None if type_id in (2, 5) else _int(unit.get("humidity_raw")),
            "temperature": None if type_id in (2, 5) else _num(unit.get("temp")),
            "drying": drying(unit, type_id, remote),
            "trays": [_present(_tray(t, ams_id, dual=dual, extruder_id=extruder_id), exist_bits, len(unit.get("tray")))
                      for t in unit.get("tray") or []],
        })
    external_items = []
    virtual = ams_block.get("vir_slot") or p.get("vir_slot") or []
    for item in virtual:
        if not isinstance(item, dict):
            continue
        ams_id = _int(item.get("id"))
        if ams_id in (254, 255):
            external_items.append(_tray({**item, "id": 0}, ams_id, dual=dual,
                                        extruder_id=1 if ams_id == 254 else 0))
    if not external_items and isinstance(p.get("vt_tray"), dict):
        external_items.append(_tray(p["vt_tray"], None, dual=dual, extruder_id=0))
    external_items.sort(key=lambda item: item["slot"])
    external = next((item for item in external_items if item["slot"] in (EXTERNAL_SLOT, 255)), None)
    tray_now = _int(ams_block.get("tray_now"))
    # 255 = nothing loaded, 254 = external spool; otherwise a global slot index.
    active_slot = None if tray_now in (None, 255) else tray_now

    device = p.get("device") if isinstance(p.get("device"), dict) else {}
    extruder = device.get("extruder") if isinstance(device.get("extruder"), dict) else {}
    extruder_state = _int(extruder.get("state"))
    active_extruder = (extruder_state >> 4 & 0xF) if extruder_state is not None else (0 if not dual else None)
    if active_extruder == 15:
        active_extruder = None
    extruders = []
    extruder_raw = {}
    for item in extruder.get("info") or []:
        if not isinstance(item, dict) or _int(item.get("id")) not in (0, 1):
            continue
        item_id = _int(item.get("id"))
        extruder_raw[item_id] = item
        current, target = packed_temperature(item.get("temp"))
        extruders.append({"id": item_id, "name": m('Right') if item_id == 0 else m('Left'),
                          "temperature": current, "target": target})
    if not extruders:
        extruders = [{"id": 0, "name": m('Nozzle'), "temperature": _num(p.get("nozzle_temper")),
                      "target": _num(p.get("nozzle_target_temper"))}]
    active_item = next((item for item in extruders if item["id"] == active_extruder), None)
    loaded = _int((extruder_raw.get(active_extruder) or {}).get("snow"))
    if loaded is not None and loaded != 0xFFFF:
        loaded_ams, loaded_tray = loaded >> 8 & 0xFF, loaded & 0xFF
        if loaded_tray != 0xFF:
            active_slot = _public_slot(loaded_ams, loaded_tray, dual=dual)

    bed_current, bed_target = None, None
    if "bed_temp" in device:
        bed_current, bed_target = packed_temperature(device.get("bed_temp"))
    bed_info = device.get("bed") if isinstance(device.get("bed"), dict) else {}
    if bed_current is None and isinstance(bed_info.get("info"), dict):
        bed_current, bed_target = packed_temperature(bed_info["info"].get("temp"))
    if bed_current is None:
        bed_current, bed_target = _num(p.get("bed_temper")), _num(p.get("bed_target_temper"))

    chamber_current, chamber_target = None, None
    ctc = device.get("ctc") if isinstance(device.get("ctc"), dict) else {}
    if isinstance(ctc.get("info"), dict):
        chamber_current, chamber_target = packed_temperature(ctc["info"].get("temp"))
    if chamber_current is None and model_id in CHAMBER_SENSOR_MODELS:
        chamber_current, chamber_target = _num(p.get("chamber_temper")), _num(p.get("ctt"))

    hms = []
    for entry in p.get("hms") or []:
        code = hms_code(entry)
        if code:
            severity = (_int(entry.get("code"), 0) or 0) >> 16
            hms.append({
                "code": code,
                "severity": HMS_SEVERITY.get(severity, "unknown"),
                "url": f"https://wiki.bambulab.com/en/x1/troubleshooting/hmscode/{code}",
            })

    lights = {item.get("node"): item.get("mode") == "on" for item in p.get("lights_report") or [] if isinstance(item, dict)}
    speed = _int(p.get("spd_lvl"))
    remaining = _int(p.get("mc_remaining_time"))
    progress = _int(p.get("mc_percent"))
    active = state in ("PREPARE", "RUNNING", "PAUSE")
    # Last job was calibration; remains set until the next print starts.
    calibrating = CALIBRATION_FILE in str(p.get("gcode_file") or "")
    home = _int(p.get("home_flag"))

    return {
        "printer": printer,
        "connection": {
            "connected": connected,
            "last_message_at": last_message_at,
            "wifi_signal": p.get("wifi_signal"),
            "sdcard": bool(p.get("sdcard")),
            "developer_mode": developer_mode(p, hms),
        },
        "job": {
            "state": state,
            "active": active,
            "name": m('Calibration') if calibrating else (p.get("subtask_name") or None),
            "file": p.get("gcode_file") or None,
            "progress": progress,
            "remaining_minutes": remaining if active else None,
            "eta": (time.time() + remaining * 60) if active and remaining else None,
            "layer": _int(p.get("layer_num")),
            "total_layers": _int(p.get("total_layer_num")),
            "stage": stage,
            "stage_code": stage_code,
            # Calibration has no file or objects; `stages` lists its planned steps.
            "calibration": calibrating,
            "stages": stage_plan(p) if active else [],
            "print_error": _int(p.get("print_error"), 0),
            # Readable display error code; hms.Texts adds the text.
            "error": print_error(p),
            "subtask_id": str(p["subtask_id"]) if p.get("subtask_id") not in (None, "") else None,
            "prepare_percent": _int(p.get("gcode_file_prepare_percent")),
            "plate_index": plate_index(p),
            "skipped_objects": [i for i in (_int(v) for v in p.get("s_obj") or []) if i is not None],
        },
        "temperatures": {
            "nozzle": active_item["temperature"] if active_item else _num(p.get("nozzle_temper")),
            "nozzle_target": active_item["target"] if active_item else _num(p.get("nozzle_target_temper")),
            "bed": bed_current,
            "bed_target": bed_target,
            # P1/P1S/A1 report constant 5 °C despite having no chamber sensor.
            "chamber": chamber_current,
            "chamber_target": chamber_target,
        },
        "extruders": {"active": active_extruder, "items": extruders},
        "fans": {
            "part": fan_percent(p.get("cooling_fan_speed")),
            "aux": fan_percent(p.get("big_fan1_speed")),
            "chamber": fan_percent(p.get("big_fan2_speed")),
            "heatbreak": fan_percent(p.get("heatbreak_fan_speed")),
        },
        "speed": {"level": speed, "name": SPEED_LEVELS.get(speed or 0), "percent": _int(p.get("spd_mag"))},
        "lights": {"chamber": lights.get("chamber_light"), "chamber2": lights.get("chamber_light2")},
        "ams": {"units": units, "external": external, "externals": external_items, "active_slot": active_slot},
        "hms": hms,
        "nozzle": _nozzle(p, device, active_extruder),
        "homed": homed_axes(p.get("home_flag")),
        "options": print_options(p, bool(units)),
        # Motor noise calibration: `home_flag` bit 21 (as in Bambu Studio); unknown until reported.
        "capabilities": {"motor_noise_calibration": None if home is None else bool(home >> 21 & 1)},
    }


def _nozzle(p: dict, device: dict, active: int | None) -> dict:
    diameter, kind = _num(p.get("nozzle_diameter")), p.get("nozzle_type")
    block = device.get("nozzle") if isinstance(device.get("nozzle"), dict) else {}
    if diameter is None:
        selected = next((item for item in block.get("info") or []
                         if isinstance(item, dict) and _int(item.get("id")) == active), None)
        if selected:
            diameter, kind = _num(selected.get("diameter")), selected.get("type") or kind
    return {"diameter": diameter, "type": kind}
