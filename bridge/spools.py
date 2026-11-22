"""Filament library: Bambu spools and spools with an NFC tag (OpenSpool), with remaining amounts.

Bambu spools carry a chip the AMS reads; it reports a unique spool ID, the full weight and an estimated remaining
percentage, so the bridge only records what it sees. Third-party tags are invisible to the AMS: Bambro reads the tag
when a spool is loaded and tells the bridge which spool (tag UID) sits in which slot, and the bridge keeps the books.
After every print it subtracts what the print used from each slot's spool; the app writes the current amount back to
the tag whenever the phone touches it again.

Usage per slot comes from the sliced file (grams per filament on the printed plate):
- prints sent by the bridge know their filament → slot mapping;
- prints from OrcaSlicer or Bambu Studio do not report a mapping (P1/A1), so filaments are matched to the slots
  that were active during the print (`tray_now`), by material and color; a filament that ran on several slots (AMS
  backup) is split by the time each slot was active.
Canceled or failed prints count with their progress.

Deleted spools stay as tombstones for a while so a tag that is scanned again can be recognized (see `load`).
State lives in `spools.json` in the data directory, including the print in progress, so a restart mid-print keeps
the time per slot.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

log = logging.getLogger(__name__)

ACTIVE = {"PREPARE", "RUNNING", "PAUSE"}
MAX_GRAMS = 10_000
TOMBSTONE_DAYS = 180
# A deleted spool with at most this much left was probably used up: its tag likely moved to a new spool.
NEARLY_EMPTY_GRAMS, NEARLY_EMPTY_SHARE = 20.0, 0.05
# Typical densities (g/cm³) for the remaining length; 1.75 mm filament.
DENSITY = {"PLA": 1.24, "PETG": 1.27, "ABS": 1.04, "ASA": 1.07, "TPU": 1.21, "PC": 1.20, "PA": 1.14, "PAHT": 1.14,
           "PVA": 1.23, "HIPS": 1.04, "PET": 1.38, "PPS": 1.36, "PLA-CF": 1.29, "PETG-CF": 1.29, "PA-CF": 1.20}
CROSS_SECTION_CM2 = 3.14159265 * 0.0875 ** 2
TAG_FIELDS = ("type", "brand", "subtype", "color", "nozzle_temp_min", "nozzle_temp_max")


def trays_of(status: dict) -> list[dict]:
    """All slots of the status: trays of every AMS unit plus the external spool(s)."""
    ams = status.get("ams") or {}
    trays = [tray for unit in ams.get("units") or [] for tray in unit.get("trays") or []]
    externals = ams.get("externals") or ([ams["external"]] if ams.get("external") else [])
    return trays + [tray for tray in externals if tray]


def _rgb(color: str | None) -> tuple[int, int, int] | None:
    text = (color or "").strip().lstrip("#")
    if len(text) < 6:
        return None
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        return None


def _color_distance(a: str | None, b: str | None) -> float:
    left, right = _rgb(a), _rgb(b)
    if left is None or right is None:
        return 1000.0
    return sum((x - y) ** 2 for x, y in zip(left, right)) ** 0.5


def _same_type(a: str | None, b: str | None) -> bool:
    return bool(a and b) and a.strip().upper() == b.strip().upper()


def same_filament(spool: dict, info: dict) -> bool:
    """Same material and (nearly) the same color: the tag still describes this spool."""
    return _same_type(spool.get("type"), info.get("type")) and _color_distance(spool.get("color"), info.get("color")) < 30


def length_m(grams: float | None, material: str | None) -> float | None:
    if grams is None:
        return None
    density = DENSITY.get((material or "").upper().strip(), 1.24)
    return round(grams / (density * CROSS_SECTION_CM2 * 100), 1)


def allocate(filaments: list[dict], seen: dict[int, float], trays: dict[int, dict],
             mapping: dict[int, int] | None = None, fraction: float = 1.0) -> dict[int, float]:
    """Grams used per slot.

    `filaments`: slice info of the printed plate (`id`, `type`, `color`, `used_g`); `seen`: seconds each slot was
    active; `trays`: slot → tray info (`material`, `color`); `mapping`: filament id → slot when known; `fraction`:
    share of the print that ran (1.0 when finished).
    """
    fraction = max(0.0, min(1.0, fraction))
    usage: dict[int, float] = {}

    def add(slot: int, grams: float) -> None:
        usage[slot] = usage.get(slot, 0.0) + grams

    used = [f for f in filaments if (f.get("used_g") or 0) > 0]
    if mapping:
        for item in used:
            slot = mapping.get(item["id"])
            if slot is not None:
                add(slot, item["used_g"] * fraction)
        return _rounded(usage)
    candidates = [slot for slot, seconds in seen.items() if seconds > 0]
    if not candidates:
        return {}
    chosen: dict[int, int] = {}
    free = set(candidates)
    # Match each filament to the best active slot: same material first, then the closest color.
    for item in sorted(used, key=lambda f: -f["used_g"]):
        pool = [s for s in free if _same_type(trays.get(s, {}).get("material"), item.get("type"))] or list(free)
        if not pool:
            pool = candidates
        best = min(pool, key=lambda s: (_color_distance(trays.get(s, {}).get("color"), item.get("color")), -seen[s]))
        chosen[item["id"]] = best
        free.discard(best)
    # Active slots without a filament of their own: AMS backup of a matching filament (same material and color).
    backups: dict[int, list[int]] = {}
    for slot in free:
        tray = trays.get(slot, {})
        for item in used:
            primary = trays.get(chosen.get(item["id"], -1), {})
            if (_same_type(tray.get("material"), primary.get("material"))
                    and _color_distance(tray.get("color"), primary.get("color")) < 60):
                backups.setdefault(item["id"], []).append(slot)
                break
    for item in used:
        slots = [chosen[item["id"]]] + backups.get(item["id"], []) if item["id"] in chosen else []
        total = sum(seen.get(s, 0.0) for s in slots)
        for slot in slots:
            share = seen.get(slot, 0.0) / total if total > 0 else 1 / len(slots)
            add(slot, item["used_g"] * fraction * share)
    return _rounded(usage)


def _rounded(usage: dict[int, float]) -> dict[int, float]:
    return {slot: round(grams, 1) for slot, grams in usage.items() if grams > 0}


def _grams(value: float | None) -> float | None:
    if value is None:
        return None
    grams = float(value)
    if not 0 <= grams <= MAX_GRAMS:
        raise ValueError("grams")
    return round(grams, 1)


class SpoolStore:
    """Spools by ID (tag UID, or `bambu-<uuid>`) and the slot each one is loaded in, persisted in `spools.json`."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.spools: dict[str, dict] = data.get("spools", {})
        self.printing: dict[str, dict] = data.get("printing", {})
        cutoff = time.time() - TOMBSTONE_DAYS * 86400
        self.spools = {k: v for k, v in self.spools.items() if not v.get("deleted") or v["deleted"] > cutoff}
        for key, spool in self.spools.items():
            spool.setdefault("id", key)
            spool.setdefault("kind", "tag")

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"spools": self.spools, "printing": self.printing}, handle, indent=1)
            tmp.replace(self.path)

    @staticmethod
    def normalize_uid(uid: str) -> str:
        text = "".join(ch for ch in uid if ch.isalnum()).upper()
        if not 4 <= len(text) <= 32 or any(ch not in "0123456789ABCDEF" for ch in text):
            raise ValueError("uid")
        return text

    def get(self, spool_id: str) -> dict | None:
        """A spool by ID (tag UID in any notation, or `bambu-…`); deleted spools are not returned."""
        key = spool_id if spool_id.startswith("bambu-") else None
        if key is None:
            try:
                key = self.normalize_uid(spool_id)
            except ValueError:
                return None
        spool = self.spools.get(key)
        return spool if spool is not None and not spool.get("deleted") else None

    def library(self) -> list[dict]:
        return [s for s in self.spools.values() if not s.get("deleted")]

    def at(self, printer: str, slot: int) -> dict | None:
        return next((s for s in self.library() if s.get("printer") == printer and s.get("slot") == slot), None)

    def loaded(self, printer: str) -> dict[int, dict]:
        return {s["slot"]: s for s in self.library() if s.get("printer") == printer and s.get("slot") is not None}

    def _place(self, spool: dict, printer: str | None, slot: int | None) -> None:
        if printer is not None and slot is not None:
            for other in self.library():
                if other is not spool and other.get("printer") == printer and other.get("slot") == slot:
                    other["printer"] = other["slot"] = None
        spool["printer"], spool["slot"] = printer, slot

    @staticmethod
    def _fresh(spool_id: str, kind: str, weight: float | None, remaining: float | None) -> dict:
        now = time.time()
        return {"id": spool_id, "kind": kind, "weight": weight, "remaining": remaining if remaining is not None else weight,
                "created": now, "updated": now, "last_used": None, "prints": 0, "used": 0.0, "note": None,
                "printer": None, "slot": None}

    def load(self, printer: str, slot: int, uid: str, *, weight: float | None, remaining: float | None = None,
             info: dict | None = None, choice: str = "auto") -> tuple[dict | None, dict]:
        """A tag was read in `slot`. Returns (spool, outcome); outcome `decision` is `loaded`, `new`, `restored` or
        `choose` (spool is None: the tag belongs to a deleted spool with the same filament, the user decides).

        A known spool keeps the bridge's amount (it counted the prints); a new one starts with the amount on the tag,
        else the full weight. For a deleted spool: other filament data on the tag means the tag was rewritten for a
        new spool; the same data needs a decision, suggesting "new" when the old spool was nearly empty (tag moved to
        a fresh spool) and "restore" otherwise (deleted by mistake)."""
        uid = self.normalize_uid(uid)
        info = {k: v for k, v in (info or {}).items() if k in TAG_FIELDS and v not in (None, "")}
        weight, remaining = _grams(weight), _grams(remaining)
        spool = self.spools.get(uid)
        decision = "loaded"
        if spool is not None and spool.get("deleted"):
            if not same_filament(spool, info) or choice == "new":
                spool, decision = None, "new"
            elif choice == "restore":
                spool["deleted"] = None
                decision = "restored"
            else:
                left, full = spool.get("remaining") or 0.0, spool.get("weight") or 0.0
                nearly_empty = left <= NEARLY_EMPTY_GRAMS or (full > 0 and left / full <= NEARLY_EMPTY_SHARE)
                return None, {"decision": "choose", "suggest": "new" if nearly_empty else "restore",
                              "deleted": dict(spool)}
        if spool is None:
            spool = self._fresh(uid, "tag", weight, remaining)
            self.spools[uid] = spool
            decision = "new"
        elif weight is not None:
            spool["weight"] = weight
        if spool.get("remaining") is None:
            spool["remaining"] = remaining if remaining is not None else weight
        spool.update(info)
        self._place(spool, printer, slot)
        spool["updated"] = time.time()
        self.save()
        return spool, {"decision": decision}

    def written(self, printer: str | None, slot: int | None, uid: str, *, weight: float | None,
                info: dict | None = None) -> tuple[dict, dict]:
        """A tag is (re)written. Same filament as the known spool: keep the counted amount, update the weight. Other
        filament or unknown/deleted tag: a new spool that starts full."""
        uid = self.normalize_uid(uid)
        info = {k: v for k, v in (info or {}).items() if k in TAG_FIELDS and v not in (None, "")}
        weight = _grams(weight)
        spool = self.spools.get(uid)
        if spool is not None and not spool.get("deleted") and same_filament(spool, info):
            if weight is not None:
                spool["weight"] = weight
                if spool.get("remaining") is None or spool["remaining"] > weight:
                    spool["remaining"] = weight
            decision = "updated"
        else:
            spool = self._fresh(uid, "tag", weight, weight)
            self.spools[uid] = spool
            decision = "new"
        spool.update(info)
        if slot is not None:
            self._place(spool, printer, slot)
        spool["updated"] = time.time()
        self.save()
        return spool, {"decision": decision}

    def sync_bambu(self, printer: str, tray: dict) -> bool:
        """Record a Bambu spool the AMS reports in a slot. Returns True when something changed."""
        spool_id = f"bambu-{tray['spool_uuid']}"
        spool = self.spools.get(spool_id)
        changed = False
        if spool is None or spool.get("deleted"):
            # A deleted Bambu spool that shows up again is simply back: its data comes from the chip.
            spool = self._fresh(spool_id, "bambu", None, None)
            self.spools[spool_id] = spool
            changed = True
        weight = float(tray["spool_weight"]) if tray.get("spool_weight") else spool.get("weight")
        remain = tray.get("remain")
        values = {"type": tray.get("material"), "subtype": tray.get("name"), "brand": "Bambu Lab",
                  "color": tray.get("color"), "nozzle_temp_min": tray.get("nozzle_temp_min"),
                  "nozzle_temp_max": tray.get("nozzle_temp_max"), "weight": weight,
                  "remaining_percent": remain}
        if remain is not None and weight:
            values["remaining"] = round(weight * remain / 100, 1)
        for key, value in values.items():
            if value is not None and spool.get(key) != value:
                spool[key] = value
                changed = True
        if spool.get("printer") != printer or spool.get("slot") != tray["slot"]:
            self._place(spool, printer, tray["slot"])
            changed = True
        if changed:
            spool["updated"] = time.time()
        return changed

    def unload(self, printer: str, slot: int) -> dict | None:
        spool = self.at(printer, slot)
        if spool is not None:
            spool["printer"] = spool["slot"] = None
            spool["updated"] = time.time()
            self.save()
        return spool

    def update(self, spool_id: str, *, remaining: float | None = None, note: str | None = None,
               unload: bool = False) -> dict | None:
        spool = self.get(spool_id)
        if spool is None:
            return None
        if remaining is not None:
            if spool["kind"] == "bambu":
                raise ValueError("bambu")
            spool["remaining"] = _grams(remaining)
        if note is not None:
            spool["note"] = note.strip()[:200] or None
        if unload:
            spool["printer"] = spool["slot"] = None
        spool["updated"] = time.time()
        self.save()
        return spool

    def delete(self, spool_id: str) -> bool:
        spool = self.get(spool_id)
        if spool is None:
            return False
        spool["deleted"] = time.time()
        spool["printer"] = spool["slot"] = None
        self.save()
        return True

    def consume(self, printer: str, usage: dict[int, float]) -> list[dict]:
        """Book a print: grams per slot. Tagged spools lose the grams; Bambu spools (the AMS reports their amount)
        only count in the statistics. Slots without a known spool are ignored."""
        changed = []
        now = time.time()
        for slot, grams in usage.items():
            spool = self.at(printer, slot)
            if spool is None:
                continue
            if spool["kind"] == "tag" and spool.get("remaining") is not None:
                spool["remaining"] = max(0.0, round(spool["remaining"] - grams, 1))
            spool["used"] = round((spool.get("used") or 0.0) + grams, 1)
            spool["prints"] = (spool.get("prints") or 0) + 1
            spool["last_used"] = spool["updated"] = now
            changed.append(spool)
        if changed:
            self.save()
        return changed

    @staticmethod
    def public(spool: dict) -> dict:
        """API view with derived values (share left, length)."""
        weight, remaining = spool.get("weight"), spool.get("remaining")
        percent = spool.get("remaining_percent") if spool["kind"] == "bambu" else (
            round(100 * remaining / weight) if weight and remaining is not None else None)
        return {"id": spool["id"], "kind": spool["kind"], "type": spool.get("type"), "brand": spool.get("brand"),
                "subtype": spool.get("subtype"), "color": spool.get("color"), "weight": weight,
                "remaining": remaining, "remaining_percent": percent,
                "remaining_length_m": length_m(remaining, spool.get("type")),
                "nozzle_temp_min": spool.get("nozzle_temp_min"), "nozzle_temp_max": spool.get("nozzle_temp_max"),
                "printer": spool.get("printer"), "slot": spool.get("slot"), "created": spool.get("created"),
                "updated": spool.get("updated"), "last_used": spool.get("last_used"), "prints": spool.get("prints") or 0,
                "used": spool.get("used") or 0.0, "note": spool.get("note")}


Plan = tuple[list[dict], dict[int, int] | None]


class SpoolTracker:
    """Follows one printer: records Bambu spools, unloads tagged spools when a slot empties and books each print.

    `plan` returns the plate's filaments and, for prints sent by the bridge, the filament → slot mapping. It is
    fetched once per print (it may download the file from the SD card). `changed` is called when spools changed."""

    def __init__(self, store: SpoolStore, printer_id: str, plan: Callable[[], Awaitable[Plan]],
                 changed: Callable[[], None] | None = None):
        self.store = store
        self.printer_id = printer_id
        self.plan = plan
        self.changed = changed
        self._last_at: float | None = None
        self._status: dict | None = None
        self._wake = asyncio.Event()

    def observe(self, status: dict) -> None:
        """Called by the printer on every update (synchronous); `run` does the work."""
        self._status = status
        self._wake.set()

    async def run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=60)
            except TimeoutError:
                pass
            self._wake.clear()
            if self._status is not None:
                try:
                    await self.process(self._status)
                except Exception:  # noqa: BLE001 - one failed round must not end the loop
                    log.exception("Spool tracking")

    @property
    def _print(self) -> dict | None:
        return self.store.printing.get(self.printer_id)

    async def process(self, status: dict, now: float | None = None) -> list[dict]:
        """Returns the spools whose amount changed through a print."""
        now = now or time.time()
        if not status["connection"]["connected"]:
            self._last_at = None
            return []
        if self._sync_trays(status) and self.changed:
            self.changed()
        job = status["job"]
        current = self._print
        if job["state"] in ACTIVE:
            key = f"{job.get('name')}|{job.get('file')}"
            if current is None or current.get("key") != key:
                current = {"key": key, "seen": {}, "plan": None}
                self.store.printing[self.printer_id] = current
                self.store.save()
                self._last_at = now
            if current.get("plan") is None and self.store.loaded(self.printer_id):
                await self._fetch_plan(current)
            slot = (status.get("ams") or {}).get("active_slot")
            if slot is not None and self._last_at is not None and job["state"] == "RUNNING":
                current["seen"][str(slot)] = current["seen"].get(str(slot), 0.0) + max(0.0, min(now - self._last_at, 120))
            self._last_at = now
            return []
        self._last_at = now
        if current is None or job["state"] not in ("FINISH", "FAILED", "IDLE"):
            return []
        del self.store.printing[self.printer_id]
        fraction = 1.0 if job["state"] == "FINISH" else (job.get("progress") or 0) / 100
        changed = self._book(current, status, fraction)
        self.store.save()
        if changed and self.changed:
            self.changed()
        return changed

    async def _fetch_plan(self, current: dict) -> None:
        try:
            filaments, mapping = await self.plan()
        except Exception as exc:  # noqa: BLE001 - no file on the card, FTPS hiccup: try again on the next update
            log.debug("Spool plan not available yet: %s", exc)
            return
        current["plan"] = {"filaments": filaments,
                           "mapping": {str(k): v for k, v in mapping.items()} if mapping else None}
        self.store.save()

    def _book(self, current: dict, status: dict, fraction: float) -> list[dict]:
        plan = current.get("plan")
        if not plan:
            return []
        trays = {t["slot"]: t for t in trays_of(status)}
        seen = {int(slot): seconds for slot, seconds in current.get("seen", {}).items()}
        mapping = {int(k): v for k, v in plan["mapping"].items()} if plan.get("mapping") else None
        usage = allocate(plan["filaments"], seen, trays, mapping, fraction)
        changed = self.store.consume(self.printer_id, usage)
        for spool in changed:
            log.info("Spool %s in slot %s: %s g left", spool["id"], spool["slot"], spool.get("remaining"))
        return changed

    def _sync_trays(self, status: dict) -> bool:
        changed = False
        for tray in trays_of(status):
            if tray.get("rfid") and tray.get("spool_uuid"):
                changed |= self.store.sync_bambu(self.printer_id, tray)
                continue
            # External spools have no sensor; they stay until another spool is read for that slot.
            if tray.get("empty") and tray["slot"] < 254 or tray.get("rfid"):
                spool = self.store.at(self.printer_id, tray["slot"])
                if spool is not None:
                    spool["printer"] = spool["slot"] = None
                    spool["updated"] = time.time()
                    changed = True
        if changed:
            self.store.save()
        return changed
