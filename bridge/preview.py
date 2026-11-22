"""Slicing preview: plate G-code → layers of extrusion paths, encoded compactly for the app.

Reads OrcaSlicer/Bambu Studio comments (`; FEATURE:`, `; CHANGE_LAYER`, `; Z_HEIGHT:`,
`; LAYER_HEIGHT:`, or `;LAYER_CHANGE`/`;Z:`), G0/G1 moves and G2/G3 arcs. Extrusion uses positive E
(relative, M83) or increasing E (absolute, M82). Discard everything before the first layer
(the purge line in start G-code).

OPV1 format (little endian):
    4s  Magic b"OPV1"
    H   Version (1)
    ff  Bed width and depth in mm
    H   Path type count, each: B length + UTF-8 name
    I   Layer count, each: f Z, f layer height, I path count,
        each path: B type index, I point count, then HH points (x, y in 0.01 mm)"""
from __future__ import annotations

from .i18n import m

import io
import math
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

MAGIC = b"OPV1"
VERSION = 1
MAX_POINTS = 2_000_000
UNIT = 100.0  # 0.01 mm
# Bounds while building, before `reduce`: paths are split at MAX_PATH_POINTS; above MAX_BUILD_POINTS, the layers read
# so far are simplified with the next tolerance; the format allows 255 path types. SIMPLIFY_WORK caps the
# Douglas-Peucker steps per preview (adversarial paths make it quadratic).
TOLERANCES = (0.02, 0.05, 0.1, 0.2, 0.4)
MAX_BUILD_POINTS = 2 * MAX_POINTS
MAX_PATH_POINTS = 20_000
MAX_FEATURES = 255
MAX_FEATURE_NAME = 64
SIMPLIFY_WORK = 400_000_000

_WORD = re.compile(r"([XYZEIJF])(-?\d*\.?\d+)")


@dataclass
class Layer:
    z: float
    height: float
    paths: list[tuple[int, list[tuple[float, float]]]] = field(default_factory=list)


class TooComplex(ValueError):
    """The G-code exceeds the preview's work or size budget."""


class Budget:
    """Remaining simplification steps for one preview."""

    def __init__(self, steps: int = SIMPLIFY_WORK):
        self.steps = steps

    def spend(self, steps: int) -> None:
        self.steps -= steps
        if self.steps < 0:
            raise TooComplex(m('The G-code is too complex for a preview.'))


@dataclass
class Preview:
    bed: tuple[float, float]
    features: list[str]
    layers: list[Layer]
    # Index into TOLERANCES already applied while building, -1 for none.
    simplified: int = -1

    @property
    def point_count(self) -> int:
        return sum(len(points) for layer in self.layers for _, points in layer.paths)


def parse_gcode(lines, bed: tuple[float, float] = (256.0, 256.0), budget: Budget | None = None) -> Preview:
    """Build the preview. Memory stays bounded: see MAX_BUILD_POINTS and the other limits above."""
    budget = budget or Budget()
    features: list[str] = []
    feature_index: dict[str, int] = {}
    layers: list[Layer] = []
    absolute_xyz, absolute_e = True, False
    x = y = z = e = 0.0
    current: list[tuple[float, float]] | None = None
    current_feature = -1
    points = 0
    level = -1

    def feature_id(name: str) -> int:
        name = name.strip()[:MAX_FEATURE_NAME] or m('Unknown')
        if name not in feature_index:
            if len(features) >= MAX_FEATURES:
                return 0  # The first type is "Unknown".
            feature_index[name] = len(features)
            features.append(name)
        return feature_index[name]

    def close_path() -> None:
        nonlocal current, points
        if current is not None and len(current) >= 2 and layers:
            path = simplify(current, TOLERANCES[level], budget) if level >= 0 else current
            layers[-1].paths.append((current_feature, path))
            points += len(path)
            if points > MAX_BUILD_POINTS:
                tighten()
        current = None

    def tighten() -> None:
        nonlocal level, points
        while points > MAX_BUILD_POINTS:
            level += 1
            if level >= len(TOLERANCES):
                raise TooComplex(m('The G-code is too complex for a preview.'))
            for layer in layers:
                layer.paths = [(f, simplify(path, TOLERANCES[level], budget)) for f, path in layer.paths]
            points = sum(len(path) for layer in layers for _, path in layer.paths)

    def new_layer(layer_z: float | None) -> None:
        close_path()
        layer_z = layer_z if layer_z is not None else z
        # Provisional height from the Z delta; Bambu's later `; LAYER_HEIGHT:` overrides it.
        height = layer_z - layers[-1].z if layers else layer_z
        layer = Layer(z=layer_z, height=round(max(height, 0.0), 4))
        if layers and not layers[-1].paths:
            layers[-1] = layer  # Layers without paths are dropped anyway; do not collect them.
        else:
            layers.append(layer)

    feature = feature_id(m('Unknown'))
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith(";"):
            comment = line.lstrip("; ").strip()
            upper = comment.upper()
            value = comment.split(":", 1)[1] if ":" in comment else ""
            if upper.startswith(("FEATURE:", "TYPE:")):
                close_path()
                feature = feature_id(value)
            elif upper.startswith(("Z_HEIGHT:", "Z:")):
                # Bambu/OrcaSlicer for BBL: `; Z_HEIGHT: 0.4`; generic OrcaSlicer: `;Z:0.4`.
                new_layer(_float(value))
            elif upper.startswith(("LAYER_HEIGHT:", "HEIGHT:")) and layers:
                height = _float(value)
                if height:
                    layers[-1].height = height
            continue
        code, _, _comment = line.partition(";")
        parts = code.split(None, 1)
        if not parts:
            continue
        command = parts[0].upper()
        if command in ("G90", "G91"):
            absolute_xyz = command == "G90"
            continue
        if command in ("M82", "M83"):
            absolute_e = command == "M82"
            continue
        if command == "G92":
            words = dict((k, float(v)) for k, v in _WORD.findall(parts[1].upper() if len(parts) > 1 else ""))
            if "E" in words:
                e = words["E"]
            continue
        if command not in ("G0", "G1", "G2", "G3"):
            continue
        words = dict((k, float(v)) for k, v in _WORD.findall(parts[1].upper() if len(parts) > 1 else ""))
        nx = (words["X"] if absolute_xyz else x + words["X"]) if "X" in words else x
        ny = (words["Y"] if absolute_xyz else y + words["Y"]) if "Y" in words else y
        nz = (words["Z"] if absolute_xyz else z + words["Z"]) if "Z" in words else z
        extruding = False
        if "E" in words:
            delta = words["E"] - e if absolute_e else words["E"]
            if absolute_e:
                e = words["E"]
            extruding = delta > 1e-6
        moved_xy = nx != x or ny != y
        if nz != z:
            close_path()
        if extruding and moved_xy and layers:
            if current is None or current_feature != feature:
                close_path()
                current = [(x, y)]
                current_feature = feature
            if command in ("G2", "G3") and ("I" in words or "J" in words):
                current.extend(_arc(x, y, nx, ny, words.get("I", 0.0), words.get("J", 0.0), command == "G2"))
            else:
                current.append((nx, ny))
            if len(current) >= MAX_PATH_POINTS:
                last = current[-1]
                close_path()
                current, current_feature = [last], feature
        elif moved_xy:
            close_path()
        x, y, z = nx, ny, nz
    close_path()
    layers = [layer for layer in layers if layer.paths]
    used = sorted({f for layer in layers for f, _ in layer.paths})
    remap = {old: new for new, old in enumerate(used)}
    for layer in layers:
        layer.paths = [(remap[f], path) for f, path in layer.paths]
    return Preview(bed=bed, features=[features[i] for i in used], layers=layers, simplified=level)


def _float(text: str) -> float | None:
    try:
        return float(text.strip())
    except ValueError:
        return None


def _arc(x0: float, y0: float, x1: float, y1: float, i: float, j: float, clockwise: bool) -> list[tuple[float, float]]:
    cx, cy = x0 + i, y0 + j
    radius = math.hypot(i, j)
    start = math.atan2(y0 - cy, x0 - cx)
    end = math.atan2(y1 - cy, x1 - cx)
    sweep = end - start
    if clockwise and sweep >= 0:
        sweep -= 2 * math.pi
    elif not clockwise and sweep <= 0:
        sweep += 2 * math.pi
    steps = max(2, min(64, int(abs(sweep) * radius / 0.5)))
    points = [(cx + radius * math.cos(start + sweep * k / steps), cy + radius * math.sin(start + sweep * k / steps))
              for k in range(1, steps)]
    points.append((x1, y1))
    return points


def simplify(points: list[tuple[float, float]], tolerance: float,
             budget: Budget | None = None) -> list[tuple[float, float]]:
    """Iterative Douglas-Peucker simplification, preserving both endpoints."""
    if len(points) < 3 or tolerance <= 0:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    tol2 = tolerance * tolerance
    while stack:
        first, last = stack.pop()
        if budget is not None:
            budget.spend(last - first)
        ax, ay = points[first]
        bx, by = points[last]
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        worst, index = -1.0, -1
        for k in range(first + 1, last):
            px, py = points[k]
            if length2 == 0:
                d2 = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
                d2 = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if d2 > worst:
                worst, index = d2, k
        if worst > tol2:
            keep[index] = True
            stack.append((first, index))
            stack.append((index, last))
    return [p for p, k in zip(points, keep) if k]


def reduce(preview: Preview, limit: int = MAX_POINTS, budget: Budget | None = None) -> Preview:
    """Simplify paths until at most `limit` points remain, continuing from tolerances applied while building."""
    if preview.simplified >= 0 and preview.point_count <= limit:
        return preview
    for level in range(max(preview.simplified, 0), len(TOLERANCES)):
        for layer in preview.layers:
            layer.paths = [(f, simplify(points, TOLERANCES[level], budget)) for f, points in layer.paths]
        preview.simplified = level
        if preview.point_count <= limit:
            break
    return preview


def encode(preview: Preview) -> bytes:
    out = io.BytesIO()
    out.write(MAGIC)
    out.write(struct.pack("<Hff", VERSION, *preview.bed))
    out.write(struct.pack("<H", len(preview.features)))
    for name in preview.features:
        data = name.encode()[:255]
        out.write(struct.pack("<B", len(data)) + data)
    out.write(struct.pack("<I", len(preview.layers)))
    for layer in preview.layers:
        out.write(struct.pack("<ffI", layer.z, layer.height, len(layer.paths)))
        for feature, points in layer.paths:
            out.write(struct.pack("<BI", feature, len(points)))
            flat = []
            for px, py in points:
                flat.append(min(65535, max(0, round(px * UNIT))))
                flat.append(min(65535, max(0, round(py * UNIT))))
            out.write(struct.pack(f"<{len(flat)}H", *flat))
    return out.getvalue()


def decode(data: bytes) -> Preview:
    """Counterpart to `encode`, used by tests."""
    view = memoryview(data)
    if bytes(view[:4]) != MAGIC:
        raise ValueError(m('not OPV1'))
    offset = 4
    version, bed_w, bed_d = struct.unpack_from("<Hff", view, offset)
    offset += 10
    (count,) = struct.unpack_from("<H", view, offset)
    offset += 2
    features = []
    for _ in range(count):
        length = view[offset]
        features.append(bytes(view[offset + 1:offset + 1 + length]).decode())
        offset += 1 + length
    (layer_count,) = struct.unpack_from("<I", view, offset)
    offset += 4
    layers = []
    for _ in range(layer_count):
        z, height, paths = struct.unpack_from("<ffI", view, offset)
        offset += 12
        layer = Layer(z=z, height=height)
        for _ in range(paths):
            feature, points = struct.unpack_from("<BI", view, offset)
            offset += 5
            flat = struct.unpack_from(f"<{points * 2}H", view, offset)
            offset += points * 4
            layer.paths.append((feature, [(flat[k] / UNIT, flat[k + 1] / UNIT) for k in range(0, len(flat), 2)]))
        layers.append(layer)
    return Preview(bed=(bed_w, bed_d), features=features, layers=layers)


def build_from_3mf(path: Path, plate: int, bed: tuple[float, float]) -> bytes:
    """Read Metadata/plate_N.gcode from a sliced 3MF and return an OPV1 preview."""
    from .jobs import gcode_lines, open_archive  # Late import keeps preview independent of jobs.

    budget = Budget()
    with open_archive(path) as archive:
        lines = (line.decode("utf-8", "replace") for line in gcode_lines(archive, f"Metadata/plate_{plate}.gcode"))
        preview = parse_gcode(lines, bed, budget)
    return encode(reduce(preview, budget=budget))
