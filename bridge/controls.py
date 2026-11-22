"""Controls: app action + printer status → MQTT commands or a reason for rejection.

Network-free logic keeps every rule testable:
- Movement, extrusion and filament changes require no running print.
- Temperatures and fans can change during printing, within fixed limits.
- Objects can be skipped only while printing, only known IDs, and never all objects.
- Print options and message acknowledgment are always allowed, as in Bambu Studio.
- Calibration requires no running print because the printer moves, heats and vibrates.
- Drying requires a heated AMS (AMS 2 Pro or AMS HT), remote firmware support, the AMS temperature
  limits, and temperatures below the loaded filament's heat distortion temperature.
Commands follow the LAN Only mode protocol used by OrcaSlicer and Bambu Studio."""
from __future__ import annotations

from .i18n import m, join_messages

from dataclasses import dataclass

from .state import EXTERNAL_SLOT

# Exclude UNKNOWN: do not move until the printer has reported its state.
IDLE = {"IDLE", "FINISH", "FAILED"}
PRINTING = {"RUNNING", "PAUSE", "PREPARE"}

NOZZLE_MAX = 300
BED_MAX = 100
FANS = {"part": 1, "aux": 2, "chamber": 3}
AXES = {"X": (100, 3000), "Y": (100, 3000), "Z": (50, 600)}  # Maximum move in mm, feed rate in mm/min.
MIN_EXTRUDE_TEMP = 170


@dataclass
class ControlError(Exception):
    status: int
    message: str


Command = tuple[str, dict]


def gcode(*lines: str) -> Command:
    return ("print", {"command": "gcode_line", "param": "".join(f"{line} \n" for line in lines)})


def build(action: str, value, status: dict, objects: list[dict] | None = None,
          drying_suggestion: dict | None = None) -> list[Command]:
    state = status["job"]["state"]
    capabilities = status.get("printer", {}).get("capabilities") or {}

    def need(states: set[str], message: str) -> None:
        if state not in states:
            raise ControlError(409, message)

    def need_homed() -> None:
        # Without homing, toolhead and bed positions are unknown: no movement, extrusion or filament changes
        # (which move to the wiper). Unknown homing status also counts as not homed.
        homed = status.get("homed")
        if not homed or not all(homed.values()):
            raise ControlError(409, m('Home the printer first: The axis positions are uncertain.'))

    if action == "pause":
        need({"RUNNING"}, m('You can only pause while printing.'))
        return [("print", {"command": "pause"})]
    if action == "resume":
        need({"PAUSE"}, m('You can only resume a paused print.'))
        return [("print", {"command": "resume"})]
    if action == "stop":
        need(PRINTING, m('No print is running.'))
        return [("print", {"command": "stop"})]
    if action == "light":
        if not isinstance(value, bool):
            raise ControlError(422, m('Light: value must be true or false.'))
        command = {"command": "ledctrl", "led_node": "chamber_light", "led_mode": "on" if value else "off",
                   "led_on_time": 500, "led_off_time": 500, "loop_times": 0, "interval_time": 0}
        commands = [("system", command)]
        if capabilities.get("second_chamber_light"):
            commands.append(("system", {**command, "led_node": "chamber_light2"}))
        return commands
    if action == "speed":
        if value not in (1, 2, 3, 4):
            raise ControlError(422, m('Speed: 1 (Silent) to 4 (Turbo).'))
        need({"RUNNING", "PAUSE"}, m('You can only change the speed while printing.'))
        return [("print", {"command": "print_speed", "param": str(value)})]
    if action == "temperature":
        heater, target = _get(value, "heater"), _get(value, "target")
        limits = capabilities.get("temperature_limits") or {}
        configured = limits.get(heater)
        limit = configured[1] if isinstance(configured, list) and len(configured) == 2 else {
            "nozzle": NOZZLE_MAX, "bed": BED_MAX}.get(heater)
        if heater == "chamber":
            # set_ctt and M141/M145 are not sufficiently verified across active air duct modes.
            raise ControlError(409, m('The bridge cannot safely control the chamber temperature yet.'))
        if limit is None:
            raise ControlError(422, m('Heater: nozzle or bed.'))
        if not isinstance(target, int) or not 0 <= target <= limit:
            raise ControlError(422, m('Temperature: 0 to {p0} °C.', p0=limit))
        if heater == "nozzle" and capabilities.get("dual_nozzle"):
            extruder = _get(value, "extruder")
            if extruder not in (0, 1):
                raise ControlError(422, m('With two nozzles, extruder 0 (right) or 1 (left) is required.'))
            return [("print", {"command": "set_nozzle_temp", "extruder_index": extruder, "target_temp": target})]
        if heater == "bed" and capabilities.get("mqtt_bed_temperature"):
            return [("print", {"command": "set_bed_temp", "temp": target})]
        return [gcode(f"{'M104' if heater == 'nozzle' else 'M140'} S{target}")]
    if action == "fan":
        fan, percent = _get(value, "fan"), _get(value, "percent")
        if fan not in FANS:
            raise ControlError(422, m('Fan: part, aux or chamber.'))
        if not isinstance(percent, int) or not 0 <= percent <= 100:
            raise ControlError(422, m('Fan: 0 to 100%.'))
        return [gcode(f"M106 P{FANS[fan]} S{round(percent * 255 / 100)}")]
    if action == "home":
        need(IDLE, m('You can only home the printer when no print is running.'))
        return [("print", {"command": "back_to_center"})] if capabilities.get("mqtt_homing") else [gcode("G28")]
    if action == "move":
        need(IDLE, m('You can only move the axes when no print is running.'))
        need_homed()
        axis, distance = str(_get(value, "axis") or "").upper(), _get(value, "distance")
        if axis not in AXES:
            raise ControlError(422, m('Axis: X, Y or Z.'))
        limit, feed = AXES[axis]
        if not isinstance(distance, (int, float)) or isinstance(distance, bool) or not 0 < abs(distance) <= limit:
            raise ControlError(422, m('Distance on {p0}: Up to {p1} mm.', p0=axis, p1=limit))
        if capabilities.get("architecture") == "i3" and axis in ("Y", "Z"):
            distance = -distance
        # Move relatively, keep software endstops enabled, then restore the reference mode.
        return [gcode("M211 S", "M211 X1 Y1 Z1", "M1002 push_ref_mode", "G91",
                      f"G1 {axis}{distance:.1f} F{feed}", "M1002 pop_ref_mode", "M211 R")]
    if action == "extrude":
        need(IDLE, m('You can only extrude when no print is running.'))
        need_homed()
        distance = _get(value, "distance") if capabilities.get("dual_nozzle") else value
        if not isinstance(distance, (int, float)) or isinstance(distance, bool) or not 0 < abs(distance) <= 50:
            raise ControlError(422, m('Extrusion: Up to 50 mm forward or backward.'))
        extruder = _get(value, "extruder") if capabilities.get("dual_nozzle") else None
        if capabilities.get("dual_nozzle") and extruder not in (0, 1):
            raise ControlError(422, m('With two nozzles, extruder 0 (right) or 1 (left) is required.'))
        nozzle = (next((e.get("temperature") for e in status.get("extruders", {}).get("items", [])
                        if e.get("id") == extruder), None) if extruder is not None else status["temperatures"].get("nozzle")) or 0
        if nozzle < MIN_EXTRUDE_TEMP:
            raise ControlError(409, m('Nozzle too cold ({p0} °C). Heat it to at least {p1} °C first.', p0=int(nozzle), p1=MIN_EXTRUDE_TEMP))
        if extruder is not None:
            return [("print", {"command": "set_extrusion_length", "extruder_index": extruder,
                                "length": distance})]
        return [gcode("M83", f"G0 E{distance:.1f} F300")]
    if action == "load":
        need(IDLE, m('You can only load filament when no print is running.'))
        need_homed()
        tray = find_tray(status, value)
        if tray is None or tray["empty"] or tray["slot"] == EXTERNAL_SLOT:
            raise ControlError(422, m('Slot not found, empty or external spool (insert filament by hand).'))
        current = find_tray(status, status["ams"].get("active_slot"))
        return [("print", {"command": "ams_change_filament", "ams_id": tray["ams"], "slot_id": tray["tray"],
                           "target": tray["slot"], "curr_temp": load_temp(current), "tar_temp": load_temp(tray)})]
    if action == "unload":
        need(IDLE, m('You can only unload filament when no print is running.'))
        need_homed()
        current = find_tray(status, status["ams"].get("active_slot"))
        if current is None:
            raise ControlError(409, m('No filament is loaded.'))
        ams_id = 255 if current["slot"] == EXTERNAL_SLOT else current["ams"]
        temp = load_temp(current)
        return [("print", {"command": "ams_change_filament", "ams_id": ams_id, "slot_id": 255, "target": 255,
                           "curr_temp": temp, "tar_temp": temp})]
    if action == "skip_objects":
        need({"RUNNING", "PAUSE"}, m('You can only skip objects while printing.'))
        if not isinstance(value, list) or not value or not all(isinstance(i, int) for i in value):
            raise ControlError(422, m('Expected a list of object IDs.'))
        if objects is None:
            raise ControlError(409, m('The objects in the current print are unknown.'))
        known = {o["id"] for o in objects}
        unknown = sorted(set(value) - known)
        if unknown:
            raise ControlError(422, m('Unknown objects: {p0}.', p0=', '.join(map(str, unknown))))
        already = set(status["job"].get("skipped_objects") or []) | {o["id"] for o in objects if o.get("skipped")}
        remaining = known - already - set(value)
        if not remaining:
            raise ControlError(422, m('At least one object must keep printing. Otherwise, cancel the print.'))
        return [("print", {"command": "skip_objects", "obj_list": sorted(set(value) - already)})]
    if action == "dry":
        return [dry(value, status, drying_suggestion)]
    if action == "dry_stop":
        return [dry_stop(value, status)]
    if action == "option":
        return [print_option(value, status)]
    if action == "calibrate":
        need(IDLE, m('You can only calibrate when no print is running.'))
        return [calibration(value, status)]
    raise ControlError(422, m('Unknown action.'))


# App print options → status keys (`options`).
OPTIONS = ("auto_recovery", "ams_backup", "rfid_on_startup", "rfid_on_insert", "remain_estimate")
AMS_SETTINGS = ("rfid_on_startup", "rfid_on_insert", "remain_estimate")


DRYING_ACTIVE = {"checking", "drying", "error", "heat_out_of_control", "stopping"}
DRY_BLOCKED_TEXT = {
    "task_occupied": m('The printer is busy with a task.'),
    "insufficient_power": m('Insufficient power (connect the AMS power adapter).'),
    "needs_plugin_power": m('Insufficient power: The AMS needs its own power adapter.'),
    "ams_busy": m('The AMS is busy.'),
    "filament_at_outlet": m('Filament is still in the AMS outlet; unload it first.'),
    "filament_at_outlet_manual_unload": m('Filament is in the AMS outlet; pull it out by hand.'),
    "initiating": m('Drying is being prepared.'),
    "not_in_2d_mode": m('Drying is unavailable in laser or cutting mode.'),
    "drying_in_progress": m('Drying is already in progress.'),
    "upgrading": m('A firmware update is in progress.'),
}


def drying_unit(status: dict, ams_id) -> dict:
    if not isinstance(ams_id, int) or isinstance(ams_id, bool):
        raise ControlError(422, m('Drying: ams_id is missing.'))
    unit = next((u for u in status["ams"]["units"] if u["id"] == ams_id), None)
    if unit is None:
        raise ControlError(422, m('AMS not found.'))
    if not unit.get("drying"):
        raise ControlError(409, m('This AMS cannot dry filament (AMS 2 Pro and AMS HT only).'))
    if not unit["drying"]["supported"]:
        raise ControlError(409, m('The printer firmware does not support starting drying remotely.'))
    return unit


def dry(value, status: dict, suggestion: dict | None) -> Command:
    """Start drying: {ams_id, temperature, hours, rotate_tray}. Filament type and cooling temperature
    come from bridge presets (`suggestion`), rather than the app."""
    if not isinstance(value, dict):
        raise ControlError(422, m('Drying: value must be an object.'))
    unit = drying_unit(status, value.get("ams_id"))
    info = unit["drying"]
    if info.get("status") in DRYING_ACTIVE:
        raise ControlError(409, m('This AMS is already drying.'))
    if info.get("blocked"):
        reasons = join_messages(" ", (DRY_BLOCKED_TEXT.get(r, m('Reason {p0}.', p0=r)) for r in info["blocked"]))
        raise ControlError(409, m('Drying is currently unavailable. {p0}', p0=reasons))
    if suggestion is None:
        raise ControlError(409, m('No drying presets for this AMS.'))
    temperature, hours = value.get("temperature"), value.get("hours")
    low, high = suggestion["temperature_range"][0], suggestion["max_temperature"]
    if not isinstance(temperature, int) or isinstance(temperature, bool) or not low <= temperature <= high:
        raise ControlError(422, m('Drying: Temperature {p0} to {p1} °C.', p0=low, p1=high))
    first, last = suggestion["hours_range"]
    if not isinstance(hours, int) or isinstance(hours, bool) or not first <= hours <= last:
        raise ControlError(422, m('Drying: Duration {p0} to {p1} hours.', p0=first, p1=last))
    rotate = value.get("rotate_tray", False)
    if not isinstance(rotate, bool):
        raise ControlError(422, m('Drying: rotate_tray must be true or false.'))
    return ("print", {"command": "ams_filament_drying", "ams_id": unit["id"], "mode": 1,
                      "filament": suggestion.get("filament_type") or "", "temp": temperature, "duration": hours,
                      "humidity": 0, "rotate_tray": rotate, "cooling_temp": int(suggestion["cooling_temperature"]),
                      "close_power_conflict": False})


def dry_stop(value, status: dict) -> Command:
    unit = drying_unit(status, value.get("ams_id") if isinstance(value, dict) else None)
    return ("print", {"command": "ams_filament_drying", "ams_id": unit["id"], "mode": 0, "filament": "",
                      "temp": 0, "duration": 0, "humidity": 0, "rotate_tray": False, "cooling_temp": 0,
                      "close_power_conflict": False})


def print_option(value, status: dict) -> Command:
    """Toggle a print option: {"key": <OPTIONS>, "enabled": bool}.

    The AMS has one command for RFID reading and remaining filament (`ams_user_setting`), so send
    the two unchanged values as last reported by the printer."""
    key, enabled = _get(value, "key"), _get(value, "enabled")
    if key not in OPTIONS:
        raise ControlError(422, m('Unknown option.'))
    if not isinstance(enabled, bool):
        raise ControlError(422, m('enabled must be true or false.'))
    if key == "auto_recovery":
        # `option` is the older firmware bitmask (bit 0 = recovery after lost steps).
        return ("print", {"command": "print_option", "option": int(enabled), "auto_recovery": enabled})
    if not status["ams"]["units"]:
        raise ControlError(409, m('No AMS connected.'))
    if key == "ams_backup":
        return ("print", {"command": "print_option", "auto_switch_filament": enabled})
    current = status.get("options") or {}
    wanted = {k: current.get(k) for k in AMS_SETTINGS} | {key: enabled}
    if any(v is None for v in wanted.values()):
        raise ControlError(409, m('The printer has not reported its AMS settings yet.'))
    return ("print", {"command": "ams_user_setting", "ams_id": -1, "startup_read_option": wanted["rfid_on_startup"],
                      "tray_read_option": wanted["rfid_on_insert"], "calibrate_remain_flag": wanted["remain_estimate"]})


# Calibration steps → `option` bits, as in Bambu Studio/OrcaSlicer's `command_start_calibration`.
CALIBRATION_STEPS = {"bed_leveling": 1 << 1, "vibration": 1 << 2, "motor_noise": 1 << 3}


def calibration(value, status: dict) -> Command:
    """Start calibration: {"bed_leveling", "vibration", "motor_noise"}, each true/false; at least one step."""
    if not isinstance(value, dict) or set(value) - set(CALIBRATION_STEPS) \
            or not all(isinstance(v, bool) for v in value.values()):
        raise ControlError(422, m('Steps: bed_leveling, vibration, motor_noise (true/false).'))
    if value.get("motor_noise") and (status.get("capabilities") or {}).get("motor_noise_calibration") is False:
        raise ControlError(409, m('The printer does not support motor noise calibration.'))
    option = sum(bit for key, bit in CALIBRATION_STEPS.items() if value.get(key))
    if not option:
        raise ControlError(422, m('Select at least one step.'))
    return ("print", {"command": "calibration", "option": option})


def clear_error(status: dict) -> tuple[Command, Command]:
    """Acknowledge a display message using `clean_print_error` (Bambu Studio's OK), then close the dialog
    with `uiop`. Both include the message code to avoid dismissing a newer message."""
    value = status["job"].get("print_error") or 0
    if value <= 0:
        raise ControlError(409, m('The printer is not showing a message.'))
    return (("print", {"command": "clean_print_error", "subtask_id": status["job"].get("subtask_id") or "",
                       "print_error": value}),
            ("system", {"command": "uiop", "name": "print_error", "action": "close", "source": 1, "type": "dialog",
                        "err": f"{value:08X}"}))


def tray_setting(slot: int, info: dict, color: str, source: dict | None = None) -> Command:
    """Assign filament and color to an AMS slot or external spool: `ams_filament_setting`."""
    hex_color = color.lstrip("#").upper()
    if len(hex_color) == 6:
        hex_color += "FF"
    if len(hex_color) != 8 or any(c not in "0123456789ABCDEF" for c in hex_color):
        raise ControlError(422, m('Color as #RRGGBB.'))
    if source and source.get("ams_id") not in (0, 1, 2, 3, 255):
        raise ControlError(409, m('This AMS slot cannot be changed safely with the verified write protocol.'))
    if source:
        ams_id, slot_id = source.get("ams_id"), source.get("slot_id")
        tray_id = 254 if ams_id == 255 else slot_id
    elif slot == EXTERNAL_SLOT:
        ams_id, slot_id, tray_id = 255, 0, 254
    elif isinstance(slot, int) and 0 <= slot < 64:
        ams_id, slot_id, tray_id = slot // 4, slot % 4, slot % 4
    else:
        raise ControlError(422, m('Invalid slot.'))
    return ("print", {
        "command": "ams_filament_setting", "ams_id": ams_id, "slot_id": slot_id, "tray_id": tray_id,
        "tray_info_idx": info.get("bambu_id") or "", "setting_id": "", "tray_color": hex_color,
        "nozzle_temp_min": info.get("nozzle_temp_min") or 190, "nozzle_temp_max": info.get("nozzle_temp_max") or 240,
        "tray_type": info.get("type") or "PLA",
    })


def nozzle_text(diameter) -> str:
    """Nozzle diameter in the format expected by K value commands: "0.4"."""
    try:
        value = float(diameter)
    except (TypeError, ValueError):
        raise ControlError(409, m('Nozzle diameter unknown.')) from None
    if not 0.1 <= value <= 1.2:
        raise ControlError(409, m('Nozzle diameter unknown.'))
    return f"{value:g}"


def k_values_query(diameter) -> Command:
    """Query the printer's K value table for the installed nozzle (`extrusion_cali_get`, all filaments)."""
    return ("print", {"command": "extrusion_cali_get", "filament_id": "", "nozzle_diameter": nozzle_text(diameter)})


def k_values(reply: dict | None) -> list[dict]:
    """`extrusion_cali_get` response → [{"cali_idx", "filament_id", "setting_id", "name", "k", "n"}].

    Printers return numbers as strings or numbers; omit incomplete entries, as OrcaSlicer does."""
    out = []
    for entry in (reply or {}).get("filaments") or []:
        if not isinstance(entry, dict):
            continue
        try:
            item = {"cali_idx": int(entry["cali_idx"]), "filament_id": str(entry["filament_id"]),
                    "setting_id": entry.get("setting_id") or None, "name": str(entry.get("name") or ""),
                    "k": float(entry["k_value"]), "n": float(entry.get("n_coef") or 0)}
        except (KeyError, TypeError, ValueError):
            continue
        if item["cali_idx"] >= 0 and 0 <= item["k"] < 2:
            out.append(item)
    return sorted(out, key=lambda e: (e["filament_id"], e["name"].lower(), e["cali_idx"]))


def k_select(slot, tray: dict, cali_idx, diameter) -> Command:
    """Select a K value table entry for a slot (`extrusion_cali_sel`); -1 uses the profile default.

    IDs follow OrcaSlicer: AMS `tray_id` is the slot across all units; a single-nozzle printer's
    external spool uses `ams_id` 255 and `tray_id` 254."""
    if not isinstance(cali_idx, int) or isinstance(cali_idx, bool) or cali_idx < -1:
        raise ControlError(422, m('cali_idx: Entry number or -1 for the default.'))
    filament_id = tray.get("info_idx")
    if not filament_id:
        raise ControlError(422, m('The slot has no filament profile; set the filament first.'))
    source = tray.get("source") or {}
    if source and source.get("ams_id") not in (0, 1, 2, 3, 255):
        raise ControlError(409, m('K value selection is not verified for this AMS slot.'))
    if source:
        ams_id, slot_id = source.get("ams_id"), source.get("slot_id")
        tray_id = 254 if ams_id == 255 else (ams_id * 4 + slot_id)
    elif slot == EXTERNAL_SLOT:
        ams_id, slot_id, tray_id = 255, 0, 254
    elif isinstance(slot, int) and 0 <= slot < 64:
        ams_id, slot_id, tray_id = slot // 4, slot % 4, slot
    else:
        raise ControlError(422, m('Invalid slot.'))
    return ("print", {"command": "extrusion_cali_sel", "tray_id": tray_id, "ams_id": ams_id, "slot_id": slot_id,
                      "cali_idx": cali_idx, "filament_id": filament_id, "nozzle_diameter": nozzle_text(diameter)})


def find_tray(status: dict, slot) -> dict | None:
    if not isinstance(slot, int) or isinstance(slot, bool):
        return None
    ams = status["ams"]
    externals = ams.get("externals") or ([ams["external"]] if ams.get("external") else [])
    trays = [t for unit in ams["units"] for t in unit["trays"]] + externals
    return next((t for t in trays if t["slot"] == slot), None)


def load_temp(tray: dict | None) -> int:
    """Filament-change nozzle temperature: midpoint of the spool's range, or 220 °C."""
    if not tray or not tray.get("nozzle_temp_min") or not tray.get("nozzle_temp_max"):
        return 220
    return max(190, min(280, (tray["nozzle_temp_min"] + tray["nozzle_temp_max"]) // 2))


def _get(value, key):
    return value.get(key) if isinstance(value, dict) else None
