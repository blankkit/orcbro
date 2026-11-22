"""New spools without a chip: set the slot to Generic <material> in neutral gray.

When a spool without a Bambu chip goes into an AMS slot, the P1 series applies the slot's previous filament and color
again (the AMS keeps the last settings for third-party filament). A new spool is rarely the same filament, so the
bridge sets such a slot to the Generic profile of the previous material (Generic PLA when it is unknown) in neutral
gray, once, and flags it (`new_spool` on the tray) until the filament is set for the slot or a tag is read for it.

An insertion is a slot whose presence (`tray_exist_bits`) goes from empty to present while the bridge watches; units
without presence bits are left alone. The AMS first reads the spool (`tray_reading_bits`) and the firmware then
reports the old setting again, so the bridge waits until the AMS no longer reads the slot and its report (filament,
color, chip) has not changed for `SETTLE` seconds. A slot that still reports no filament waits until `BARE_WAIT`
seconds after the insertion, and a printer that does not report reading bits until `READ_WAIT`, the time the AMS
needs to turn a spool. `CAP` seconds after the insertion the bridge decides anyway; if the AMS is still reading the
slot then, it leaves it alone.

Nothing is sent for Bambu spools (chip); when the filament was set or a tag was read for the slot after the insertion
or shortly before it (`PLACE_GRACE`, as for the spool tracker); when the slot settles on a filament other than its
last setting (set on the printer or in Bambu Studio meanwhile); or when a print is running from the slot.
Re-inserting the same spool counts as a new one: the AMS cannot tell the two apart.

`NewSpools` is the network-free state machine (also the source of Bambro's direct-mode fixtures); `NewSpoolWatcher`
runs it for one printer and sends the setting with `controls.tray_setting`."""
from __future__ import annotations

from .i18n import m, message_of

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from . import controls
from .printer import PrinterError
from .profiles import Catalog, ProfileError
from .spools import PLACE_GRACE

log = logging.getLogger(__name__)

# Neutral gray for a reset slot; Bambro uses the same value in direct mode.
GRAY = "#8E8E93"
# Material when the slot's previous filament is unknown.
FALLBACK_MATERIAL = "PLA"
SETTLE = 5.0
BARE_WAIT = 30.0
READ_WAIT = 20.0
CAP = 60.0
FIRE, WAIT = "fire", "wait"


def reading_slots(raw: dict) -> set[int] | None:
    """Slots the AMS is reading (`tray_reading_bits`, bit ams * 4 + slot like `tray_exist_bits`); None when the
    printer does not report them."""
    ams = raw.get("ams") if isinstance(raw.get("ams"), dict) else {}
    try:
        bits = int(str(ams["tray_reading_bits"]), 16) if ams.get("tray_reading_bits") is not None else None
    except ValueError:
        bits = None
    if bits is None:
        return None
    return {bit for bit in range(bits.bit_length()) if bits >> bit & 1}


def _rgb(color) -> str:
    return str(color or "").lstrip("#").upper()[:6]


def _bambu(tray: dict) -> bool:
    return bool(tray.get("rfid") or tray.get("spool_uuid"))


def _setting(tray: dict) -> tuple | None:
    """The slot's filament setting (material, filament ID, color); None while it reports none."""
    return (tray["material"], tray.get("info_idx"), _rgb(tray.get("color"))) if tray.get("material") else None


def _label(slot: int) -> str:
    """A1 … D4, as on the AMS."""
    return f"{'ABCD'[slot // 4] if slot // 4 < 4 else slot // 4 + 1}{slot % 4 + 1}"


class NewSpools:
    """Insertions and flagged slots of one printer, without network or clock: `update` takes the normalized status
    and the time, and returns the slots due for the Generic setting now (`slot`, `material` or None, `tray`). The
    caller sends it and reports it with `sent`."""

    def __init__(self):
        self._present: dict[int, bool] = {}
        # Last filament setting each slot reported: the one the firmware applies again to a chipless spool.
        self._last: dict[int, tuple] = {}
        self._pending: dict[int, dict] = {}
        self._flags: dict[int, dict] = {}
        self._set: dict[int, float] = {}
        self._now = 0.0

    def forget(self) -> None:
        """The setting is off: no insertions are followed and no slot is flagged."""
        self._present.clear()
        self._pending.clear()
        self._flags.clear()

    def user_set(self, slot: int, now: float) -> None:
        """The filament was set for the slot, or a tag was read for it: nothing is sent and its flag ends."""
        self._set[slot] = now
        self._pending.pop(slot, None)
        self._flags.pop(slot, None)

    def set_since(self, slot: int, when: float) -> bool:
        """The filament was set for the slot at or after `when`."""
        return slot in self._set and self._set[slot] >= when

    def sent(self, slot: int, info_idx: str | None) -> None:
        """The Generic setting went to the printer: the slot stays flagged until it shows another filament."""
        self._flags[slot] = {"info_idx": info_idx or None, "confirmed": False}

    def flagged(self, slot: int) -> bool:
        return slot in self._flags

    def annotate(self, status: dict) -> None:
        """`new_spool` on every slot of the status."""
        ams = status.get("ams") or {}
        externals = ams.get("externals") or ([ams["external"]] if ams.get("external") else [])
        for unit in ams.get("units") or []:
            for tray in unit.get("trays") or []:
                tray["new_spool"] = tray["slot"] in self._flags
        for tray in externals:
            if tray:
                tray["new_spool"] = False

    def deadline(self) -> float | None:
        """Next time a waiting slot can become due without a new report."""
        times = [t for p in self._pending.values()
                 for t in (p["changed"] + SETTLE, p["since"] + BARE_WAIT, p["since"] + READ_WAIT, p["since"] + CAP)
                 if t > self._now]
        return min(times) if times else None

    def update(self, status: dict, now: float, *, reading: set[int] | None = None,
               placed: Callable[[int], float | None] | None = None) -> list[dict]:
        self._now = now
        if not status["connection"]["connected"]:
            # Reports may be missed while disconnected: start over without counting the next one as an insertion.
            self._present.clear()
            self._pending.clear()
            return []
        job, ams = status["job"], status.get("ams") or {}
        printing = ams.get("active_slot") if job.get("active") else None
        due, seen = [], set()
        for unit in ams.get("units") or []:
            for tray in unit.get("trays") or []:
                slot = tray["slot"]
                seen.add(slot)
                setting, previous = _setting(tray), self._last.get(slot)
                if setting:
                    self._last[slot] = setting
                present = tray.get("present")
                if present is None:
                    self._present.pop(slot, None)
                    self._pending.pop(slot, None)
                    self._flags.pop(slot, None)
                    continue
                before = self._present.get(slot)
                self._present[slot] = present
                if not present:
                    self._pending.pop(slot, None)
                    self._flags.pop(slot, None)
                    continue
                self._follow_flag(slot, tray)
                signature = (tray.get("material"), _rgb(tray.get("color")), tray.get("info_idx"), tray.get("name"),
                             _bambu(tray), reading is not None and slot in reading)
                if before is False:
                    self._flags.pop(slot, None)
                    self._pending[slot] = {"since": now, "changed": now, "signature": signature, "previous": previous}
                    log.debug("New spool in slot %s: waiting for the AMS", _label(slot))
                pending = self._pending.get(slot)
                if pending is None:
                    continue
                if signature != pending["signature"]:
                    pending["signature"], pending["changed"] = signature, now
                decision = self._decide(slot, tray, pending, now, reading, printing, placed)
                if decision == WAIT:
                    continue
                del self._pending[slot]
                if decision == FIRE:
                    known = setting or pending["previous"]
                    due.append({"slot": slot, "material": known[0] if known else None, "tray": tray})
                else:
                    log.info(m('New spool in slot %s: left as it is (%s)'), _label(slot), decision)
        for slot in set(self._pending) - seen:
            del self._pending[slot]
        for slot in set(self._flags) - seen:
            del self._flags[slot]
        return due

    def _follow_flag(self, slot: int, tray: dict) -> None:
        """A flag ends with a Bambu spool or, once the printer showed the Generic setting, any other filament (set on
        the printer or in Bambu Studio)."""
        flag = self._flags.get(slot)
        if flag is None:
            return
        ours = tray.get("info_idx") == flag["info_idx"] and _rgb(tray.get("color")) == _rgb(GRAY)
        if _bambu(tray) or (flag["confirmed"] and not ours):
            del self._flags[slot]
        elif ours:
            flag["confirmed"] = True

    def _decide(self, slot: int, tray: dict, pending: dict, now: float, reading: set[int] | None,
                printing: int | None, placed: Callable[[int], float | None] | None) -> str:
        """`FIRE`, `WAIT`, or why the slot is left alone."""
        if _bambu(tray):
            return "Bambu spool"
        if printing == slot:
            return "printing from this slot"
        earliest = pending["since"] - PLACE_GRACE
        for reason, when in (("filament set", self._set.get(slot)), ("tag read", placed(slot) if placed else None)):
            if when is not None and when >= earliest:
                return reason
        age = now - pending["since"]
        busy = reading is not None and slot in reading
        if age < CAP:
            if busy or now - pending["changed"] < SETTLE:
                return WAIT
            if not tray.get("material") and age < BARE_WAIT:
                return WAIT
            if reading is None and age < READ_WAIT:
                return WAIT
        elif busy:
            return "still reading"
        # The firmware applies the slot's last setting again; any other filament was set on the printer or in Bambu
        # Studio after the insertion.
        setting = _setting(tray)
        if setting and pending["previous"] and setting != pending["previous"]:
            return "filament set on the printer"
        return FIRE


def generic_info(catalog: Catalog, material: str | None) -> dict:
    """Profile for a reset slot: the Generic profile of the material, else its most general one, else Generic PLA."""
    name = catalog.generic_for(material) or catalog.generic_for(FALLBACK_MATERIAL)
    if name is None:
        raise ProfileError(m('No filament profile found for {p0} ({p1}).', p0=material or FALLBACK_MATERIAL,
                             p1=m('no ID')))
    return catalog.filament_info(name)


class NewSpoolWatcher:
    """Runs `NewSpools` for one printer: on every printer update and when a waiting slot is due. `catalog` loads the
    printer's profiles, `placed` tells when a tag was last read for a slot, `changed` runs after a Generic setting."""

    def __init__(self, printer, catalog: Callable[[], Awaitable[Catalog]], *, enabled: bool = True,
                 placed: Callable[[int], float | None] | None = None, changed: Callable[[], None] | None = None,
                 clock: Callable[[], float] = time.time):
        self.printer = printer
        self.catalog = catalog
        self.enabled = enabled
        self.placed = placed
        self.changed = changed
        self.clock = clock
        self.state = NewSpools()
        self._wake = asyncio.Event()

    def observe(self) -> None:
        self._wake.set()

    def user_set(self, slot: int) -> None:
        self.state.user_set(slot, self.clock())

    def annotate(self, status: dict) -> None:
        self.state.annotate(status)

    async def run(self) -> None:
        while True:
            deadline = self.state.deadline()
            timeout = None if deadline is None else max(0.05, deadline - self.clock())
            try:
                await asyncio.wait_for(self._wake.wait(), timeout)
            except TimeoutError:
                pass
            self._wake.clear()
            try:
                await self.process()
            except Exception:  # noqa: BLE001 - one failed round must not end the loop
                log.exception("New spool check")

    async def process(self) -> list[int]:
        """Follow the printer's slots and send the Generic setting to every slot due now; returns those slots."""
        if not self.enabled:
            self.state.forget()
            return []
        status = self.printer.status()
        due = self.state.update(status, self.clock(), reading=reading_slots(self.printer.raw), placed=self.placed)
        done = []
        for item in due:
            if await self._reset(item, status):
                done.append(item["slot"])
        if done:
            if self.changed:
                self.changed()
            await self.printer.pushall(force=True)
        return done

    async def _reset(self, item: dict, status: dict) -> bool:
        slot, tray = item["slot"], item["tray"]
        decided = self.clock()
        if status["connection"].get("developer_mode") is False:
            log.debug("New spool in slot %s: the printer does not accept commands", _label(slot))
            return False
        try:
            info = generic_info(await self.catalog(), item["material"])
        except ProfileError as exc:
            log.warning(m('New spool in slot %s: no Generic profile (%s)'), _label(slot), exc)
            return False
        if self.state.set_since(slot, decided):
            return False  # The filament was set while the profiles loaded.
        if tray.get("info_idx") == info["bambu_id"] and _rgb(tray.get("color")) == _rgb(GRAY):
            self.state.sent(slot, info["bambu_id"])
            log.info(m('New spool in slot %s: already %s in gray'), _label(slot), info["label"])
            return True
        capabilities = (status.get("printer") or {}).get("capabilities") or {}
        try:
            section, command = controls.tray_setting(slot, info, GRAY, tray.get("source"),
                                                     multi_color=bool(capabilities.get("multi_color")))
            await self.printer.command(section, command)
        except (controls.ControlError, PrinterError) as exc:
            log.warning(m('New spool in slot %s: could not set %s: %s'), _label(slot), info["label"],
                        exc.message if isinstance(exc, controls.ControlError) else message_of(exc))
            return False
        if self.state.set_since(slot, decided):
            return False  # The filament was set while the printer answered: that setting came later and stays.
        self.state.sent(slot, info["bambu_id"])
        log.info(m('New spool in slot %s (previously %s): set to %s in gray (%s)'), _label(slot),
                 item["material"] or m('unknown'), info["label"], GRAY)
        return True
