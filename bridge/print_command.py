"""Pure generation of model-specific `print.project_file` payloads.

Keep the legacy branch identical to the previous command. Add fields only for models whose
firmware requires them, preserving behavior for deployed P1S bridges."""
from __future__ import annotations

from .models import LEGACY_MODELS


def build_project_file(model_id: str | None, name: str, plan: dict, *, print_url: str | None = None,
                       capabilities: dict | None = None) -> dict:
    """Build a complete print command from a validated, persisted plan."""
    common = {
        "command": "project_file",
        "param": f"Metadata/plate_{plan['plate']}.gcode",
        "project_id": "0", "profile_id": "0", "task_id": "0", "subtask_id": "0",
        "subtask_name": name,
    }
    remote_name = plan["remote_name"]
    if model_id in LEGACY_MODELS or not plan.get("new_protocol"):
        return {**common,
                "url": (print_url or "file:///sdcard/{name}").format(name=remote_name),
                "timelapse": plan["timelapse"], "bed_type": "auto",
                "bed_leveling": plan["bed_leveling"], "bed_levelling": plan["bed_leveling"],
                "flow_cali": plan["flow_cali"], "vibration_cali": False, "layer_inspect": False,
                "use_ams": plan["use_ams"], "ams_mapping": plan["ams_mapping"]}

    capabilities = capabilities or {}
    bed_leveling = bool(plan["bed_leveling"])
    flow_cali = bool(plan["flow_cali"])
    out = {**common,
           "file": remote_name,
           "url": (print_url or "ftp:///{name}").format(name=remote_name),
           "timelapse": bool(plan["timelapse"]), "bed_type": "auto",
           "bed_leveling": bed_leveling, "auto_bed_leveling": int(bed_leveling),
           "flow_cali": flow_cali, "extrude_cali_flag": int(flow_cali),
           "vibration_cali": False, "layer_inspect": False,
           "use_ams": bool(plan["use_ams"]),
           "ams_mapping": list(plan["ams_mapping"]),
           "ams_mapping2": [dict(item) for item in plan["ams_mapping2"]],
           "cfg": "0"}
    if capabilities.get("calibrations", {}).get("nozzle_offset"):
        out["nozzle_offset_cali"] = int(bool(plan.get("nozzle_offset_cali", False)))
    if capabilities.get("pa_mode"):
        out["extrude_cali_manual_mode"] = int(plan.get("extrude_cali_manual_mode", 0))
    if "nozzle_mapping" in plan:
        out["nozzle_mapping"] = list(plan["nozzle_mapping"])
    return out
