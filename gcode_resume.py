#!/usr/bin/env python3
"""G-code resume tool — continue a failed print from a given layer height."""

from __future__ import annotations

import argparse
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass
class ResumeParams:
    stopped_z: float
    current_x: float
    current_y: float
    current_z: float
    z_lift: float = 2.0
    travel_feedrate: int = 3000
    z_feedrate: int = 300


@dataclass
class GcodeAnalysis:
    layer_z_values: list[float] = field(default_factory=list)
    min_z: float = 0.0
    max_z: float = 0.0
    min_x: float = 0.0
    max_x: float = 0.0
    min_y: float = 0.0
    max_y: float = 0.0


@dataclass
class ResumeResult:
    output_lines: list[str]
    resume_line_index: int
    resume_z: float | None
    resume_x: float | None
    resume_y: float | None
    resume_e: float | None
    hotend_temp: float | None
    bed_temp: float | None
    lines_removed: int
    warnings: list[str] = field(default_factory=list)
    analysis: GcodeAnalysis | None = None

    @property
    def warning(self) -> str | None:
        return "; ".join(self.warnings) if self.warnings else None


MOVE_RE = re.compile(r"^\s*G0*1\b.*", re.IGNORECASE)
G0_RE = re.compile(r"^\s*G0\b", re.IGNORECASE)
COMMENT_RE = re.compile(r";.*$")
PARAM_RE = re.compile(r"([XYZEF])(-?\d*\.?\d+)", re.IGNORECASE)
TEMP_HOTEND_RE = re.compile(r"^\s*M10[49]\s+S(\d+\.?\d*)", re.IGNORECASE)
TEMP_BED_RE = re.compile(r"^\s*M1[49]0\s+S(\d+\.?\d*)", re.IGNORECASE)


def _strip_comment(line: str) -> str:
    return COMMENT_RE.sub("", line).strip()


def _parse_params(line: str) -> dict[str, float]:
    return {m.group(1).upper(): float(m.group(2)) for m in PARAM_RE.finditer(line)}


def _track_state(lines: Iterable[str]) -> tuple[float, float, float, float, bool, bool]:
    x = y = z = e = 0.0
    absolute_e = True
    absolute_xyz = True

    for line in lines:
        stripped = _strip_comment(line)
        if not stripped:
            continue

        upper = stripped.upper()
        if upper.startswith("G90"):
            absolute_xyz = True
        elif upper.startswith("G91"):
            absolute_xyz = False
        elif upper.startswith("M82"):
            absolute_e = True
        elif upper.startswith("M83"):
            absolute_e = False

        if not (MOVE_RE.match(stripped) or G0_RE.match(stripped)):
            continue

        params = _parse_params(stripped)
        if "X" in params:
            x = params["X"] if absolute_xyz else x + params["X"]
        if "Y" in params:
            y = params["Y"] if absolute_xyz else y + params["Y"]
        if "Z" in params:
            z = params["Z"] if absolute_xyz else z + params["Z"]
        if "E" in params:
            e_val = params["E"]
            e = e_val if absolute_e else e + e_val

    return x, y, z, e, absolute_e, absolute_xyz


def analyze_gcode(lines: list[str]) -> GcodeAnalysis:
    x = y = z = 0.0
    absolute_xyz = True
    layer_z: set[float] = set()
    min_x = min_y = min_z = float("inf")
    max_x = max_y = max_z = float("-inf")

    for line in lines:
        stripped = _strip_comment(line)
        if not stripped:
            continue

        upper = stripped.upper()
        if upper.startswith("G90"):
            absolute_xyz = True
        elif upper.startswith("G91"):
            absolute_xyz = False

        if not (MOVE_RE.match(stripped) or G0_RE.match(stripped)):
            continue

        params = _parse_params(stripped)
        if "X" in params:
            x = params["X"] if absolute_xyz else x + params["X"]
            min_x = min(min_x, x)
            max_x = max(max_x, x)
        if "Y" in params:
            y = params["Y"] if absolute_xyz else y + params["Y"]
            min_y = min(min_y, y)
            max_y = max(max_y, y)
        if "Z" in params:
            z = params["Z"] if absolute_xyz else z + params["Z"]
            layer_z.add(round(z, 3))
            min_z = min(min_z, z)
            max_z = max(max_z, z)

    if min_z == float("inf"):
        min_z = max_z = 0.0
    if min_x == float("inf"):
        min_x = max_x = min_y = max_y = 0.0

    return GcodeAnalysis(
        layer_z_values=sorted(layer_z),
        min_z=min_z,
        max_z=max_z,
        min_x=min_x,
        max_x=max_x,
        min_y=min_y,
        max_y=max_y,
    )


def _nearest_layer(layers: list[float], target: float) -> float | None:
    if not layers:
        return None
    return min(layers, key=lambda value: abs(value - target))


def validate_params(params: ResumeParams, analysis: GcodeAnalysis) -> list[str]:
    warnings: list[str] = []

    if not analysis.layer_z_values:
        warnings.append("No Z moves found in G-code file.")
        return warnings

    if params.stopped_z > analysis.max_z + 0.05:
        warnings.append(
            f"Stopped Z ({params.stopped_z:.2f} mm) is above the file's max layer "
            f"({analysis.max_z:.2f} mm)."
        )
    elif params.stopped_z < analysis.min_z - 0.05:
        warnings.append(
            f"Stopped Z ({params.stopped_z:.2f} mm) is below the file's first layer "
            f"({analysis.min_z:.2f} mm)."
        )
    else:
        nearest = _nearest_layer(analysis.layer_z_values, params.stopped_z)
        if nearest is not None and abs(nearest - params.stopped_z) > 0.15:
            warnings.append(
                f"Stopped Z ({params.stopped_z:.2f} mm) does not match a layer in the file. "
                f"Nearest layer: {nearest:.2f} mm."
            )

    z_margin = max(15.0, (analysis.max_z - analysis.min_z) * 0.5 + 10.0)
    if params.current_z > analysis.max_z + z_margin:
        warnings.append(
            f"Current Z ({params.current_z:.2f} mm) is far above the print height "
            f"({analysis.max_z:.2f} mm). This usually means Z was homed to the top. "
            "Do not home after a failure — use M114 from before homing, or pick the stopped "
            "layer Z as your current Z if the nozzle is still at the failure height."
        )

    if params.current_z < analysis.min_z - 1.0:
        warnings.append(
            f"Current Z ({params.current_z:.2f} mm) is below the first print layer "
            f"({analysis.min_z:.2f} mm). Double-check M114."
        )

    if params.current_x < analysis.min_x - 5 or params.current_x > analysis.max_x + 5:
        warnings.append(
            f"Current X ({params.current_x:.2f} mm) is outside the print bounds "
            f"({analysis.min_x:.2f}–{analysis.max_x:.2f} mm)."
        )
    if params.current_y < analysis.min_y - 5 or params.current_y > analysis.max_y + 5:
        warnings.append(
            f"Current Y ({params.current_y:.2f} mm) is outside the print bounds "
            f"({analysis.min_y:.2f}–{analysis.max_y:.2f} mm)."
        )

    return warnings


def _extract_temps(lines: Iterable[str]) -> tuple[float | None, float | None]:
    hotend: float | None = None
    bed: float | None = None
    for line in lines:
        if hotend is None:
            m = TEMP_HOTEND_RE.match(line)
            if m:
                hotend = float(m.group(1))
        if bed is None:
            m = TEMP_BED_RE.match(line)
            if m:
                bed = float(m.group(1))
        if hotend is not None and bed is not None:
            break
    return hotend, bed


def find_resume_index(
    lines: list[str],
    stopped_z: float,
    require_extrusion: bool = True,
) -> tuple[int, float | None, float | None, float | None, float | None, str | None]:
    """Return index, resume_z, resume_x, resume_y, resume_e, and optional note."""
    x = y = z = e = 0.0
    absolute_e = True
    absolute_xyz = True
    note: str | None = None

    candidates: list[tuple[int, float, float, float, float, bool]] = []

    for idx, line in enumerate(lines):
        stripped = _strip_comment(line)
        if not stripped:
            continue

        upper = stripped.upper()
        if upper.startswith("G90"):
            absolute_xyz = True
        elif upper.startswith("G91"):
            absolute_xyz = False
        elif upper.startswith("M82"):
            absolute_e = True
        elif upper.startswith("M83"):
            absolute_e = False

        if not (MOVE_RE.match(stripped) or G0_RE.match(stripped)):
            continue

        params = _parse_params(stripped)
        if "X" in params:
            x = params["X"] if absolute_xyz else x + params["X"]
        if "Y" in params:
            y = params["Y"] if absolute_xyz else y + params["Y"]
        if "Z" in params:
            z = params["Z"] if absolute_xyz else z + params["Z"]

        is_extrusion = False
        if "E" in params:
            e_val = params["E"]
            if absolute_e:
                is_extrusion = e_val > e + 1e-9
                e = e_val
            else:
                is_extrusion = e_val > 1e-9

        if z <= stopped_z + 1e-6:
            continue

        candidates.append((idx, z, x, y, e, is_extrusion))

    if not candidates:
        return (
            len(lines),
            None,
            None,
            None,
            None,
            f"No moves found above Z={stopped_z}. "
            "Check that stopped Z is below the last printed layer.",
        )

    if require_extrusion:
        extrusion_candidates = [c for c in candidates if c[5]]
        if extrusion_candidates:
            idx, rz, rx, ry, re, _ = extrusion_candidates[0]
            return idx, rz, rx, ry, re, note

        note = (
            "No extrusion moves found above stopped Z; "
            "resuming at first travel move instead."
        )

    idx, rz, rx, ry, re, _ = candidates[0]
    return idx, rz, rx, ry, re, note


def build_resume_header(
    params: ResumeParams,
    hotend_temp: float | None,
    bed_temp: float | None,
    resume_x: float | None,
    resume_y: float | None,
    resume_z: float | None,
) -> list[str]:
    """Build a viewer-friendly resume header using explicit moves (no G92)."""
    header: list[str] = [
        "; --- G-code Resume (Gcode Reprint Tool) ---",
        "; Uses explicit positioning in slicer coordinates (no G92).",
        f"; Reported nozzle position: X{params.current_x:.3f} Y{params.current_y:.3f} Z{params.current_z:.3f}",
        "G90 ; absolute positioning",
        "M83 ; relative extrusion",
    ]

    if bed_temp is not None:
        header.append(f"M140 S{bed_temp:.0f} ; set bed temperature")
        header.append(f"M190 S{bed_temp:.0f} ; wait for bed")
    if hotend_temp is not None:
        header.append(f"M104 S{hotend_temp:.0f} ; set hotend temperature")
        header.append(f"M109 S{hotend_temp:.0f} ; wait for hotend")

    safe_z = params.current_z + params.z_lift
    if resume_z is not None:
        safe_z = max(safe_z, resume_z + params.z_lift)

    header.extend(
        [
            (
                f"G0 X{params.current_x:.3f} Y{params.current_y:.3f} "
                f"Z{params.current_z:.3f} ; move to reported nozzle position"
            ),
            f"G1 Z{safe_z:.3f} F{params.z_feedrate} ; safe Z lift before travel",
        ]
    )

    if resume_x is not None and resume_y is not None:
        header.append(
            f"G1 X{resume_x:.3f} Y{resume_y:.3f} F{params.travel_feedrate} ; move to resume XY"
        )
    if resume_z is not None:
        header.append(f"G1 Z{resume_z:.3f} F{params.z_feedrate} ; lower to resume layer")

    header.extend(
        [
            "G92 E0 ; reset extruder for clean resume",
            "; --- Original G-code continues below ---",
        ]
    )
    return header


MAX_PREVIEW_SEGMENTS = 25000


def _classify_segment(
    x1: float,
    y1: float,
    z1: float,
    x2: float,
    y2: float,
    z2: float,
    is_extrusion: bool,
) -> str:
    if is_extrusion:
        return "extrusion"
    if abs(x2 - x1) < 1e-6 and abs(y2 - y1) < 1e-6 and abs(z2 - z1) > 1e-6:
        return "z_hop"
    return "travel"


def parse_gcode_paths(
    lines: list[str],
    stopped_z: float | None = None,
    resume_line_index: int | None = None,
    initial_x: float = 0.0,
    initial_y: float = 0.0,
    initial_z: float = 0.0,
    initial_e: float = 0.0,
    initial_absolute_xyz: bool = True,
    initial_absolute_e: bool = True,
) -> dict:
    """Parse G-code moves into segments for path preview.

    Returns segments, per-layer point lists, bounds, and final machine state.
    """
    x, y, z, e = initial_x, initial_y, initial_z, initial_e
    absolute_e = initial_absolute_e
    absolute_xyz = initial_absolute_xyz
    segments: list[dict] = []
    layer_points: dict[float, list[list[float]]] = {}
    min_x = min_y = min_z = float("inf")
    max_x = max_y = max_z = float("-inf")

    def _track_bounds(px: float, py: float, pz: float) -> None:
        nonlocal min_x, max_x, min_y, max_y, min_z, max_z
        min_x = min(min_x, px)
        max_x = max(max_x, px)
        min_y = min(min_y, py)
        max_y = max(max_y, py)
        min_z = min(min_z, pz)
        max_z = max(max_z, pz)

    def _add_layer_point(lz: float, px: float, py: float, pz: float) -> None:
        key = round(lz, 3)
        layer_points.setdefault(key, []).append([round(px, 3), round(py, 3), round(pz, 3)])

    for idx, line in enumerate(lines):
        stripped = _strip_comment(line)
        if not stripped:
            continue

        upper = stripped.upper()
        if upper.startswith("G90"):
            absolute_xyz = True
        elif upper.startswith("G91"):
            absolute_xyz = False
        elif upper.startswith("M82"):
            absolute_e = True
        elif upper.startswith("M83"):
            absolute_e = False

        if not (MOVE_RE.match(stripped) or G0_RE.match(stripped)):
            continue

        prev_x, prev_y, prev_z, prev_e = x, y, z, e
        params = _parse_params(stripped)
        if "X" in params:
            x = params["X"] if absolute_xyz else x + params["X"]
        if "Y" in params:
            y = params["Y"] if absolute_xyz else y + params["Y"]
        if "Z" in params:
            x_before_z = x
            y_before_z = y
            z = params["Z"] if absolute_xyz else z + params["Z"]
            _add_layer_point(z, x_before_z, y_before_z, z)

        e_delta = 0.0
        is_extrusion = False
        if "E" in params:
            e_val = params["E"]
            if absolute_e:
                is_extrusion = e_val > e + 1e-9
                e_delta = e_val - e
                e = e_val
            else:
                is_extrusion = e_val > 1e-9
                e_delta = e_val
                e += e_val

        if abs(x - prev_x) < 1e-9 and abs(y - prev_y) < 1e-9 and abs(z - prev_z) < 1e-9:
            continue

        seg_type = _classify_segment(prev_x, prev_y, prev_z, x, y, z, is_extrusion)
        _track_bounds(prev_x, prev_y, prev_z)
        _track_bounds(x, y, z)

        segments.append(
            {
                "x1": round(prev_x, 3),
                "y1": round(prev_y, 3),
                "z1": round(prev_z, 3),
                "x2": round(x, 3),
                "y2": round(y, 3),
                "z2": round(z, 3),
                "e_delta": round(e_delta, 5),
                "type": seg_type,
                "line": idx,
            }
        )

    if min_z == float("inf"):
        min_x = max_x = min_y = max_y = min_z = max_z = 0.0

    layers = [
        {"z": z_val, "points": pts}
        for z_val, pts in sorted(layer_points.items(), key=lambda item: item[0])
    ]

    return {
        "segments": segments,
        "layers": layers,
        "bounds": {
            "min_x": round(min_x, 3),
            "max_x": round(max_x, 3),
            "min_y": round(min_y, 3),
            "max_y": round(max_y, 3),
            "min_z": round(min_z, 3),
            "max_z": round(max_z, 3),
        },
        "final_position": {"x": round(x, 3), "y": round(y, 3), "z": round(z, 3), "e": round(e, 5)},
        "absolute_xyz": absolute_xyz,
        "absolute_e": absolute_e,
        "stopped_z": stopped_z,
        "resume_line_index": resume_line_index,
    }


def _segment_max_z(seg: dict) -> float:
    return max(seg["z1"], seg["z2"])


def _segment_end(seg: dict) -> tuple[float, float, float]:
    return seg["x2"], seg["y2"], seg["z2"]


def _segment_start(seg: dict) -> tuple[float, float, float]:
    return seg["x1"], seg["y1"], seg["z1"]


def _xy_distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _line_crosses_rect(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    min_x: float,
    max_x: float,
    min_y: float,
    max_y: float,
) -> bool:
    """Simple check: segment endpoints or midpoint lie inside printed XY bounds."""
    mid_x = (x1 + x2) / 2
    mid_y = (y1 + y2) / 2

    def inside(px: float, py: float) -> bool:
        return min_x <= px <= max_x and min_y <= py <= max_y

    return inside(x1, y1) or inside(x2, y2) or inside(mid_x, mid_y)


def analyze_resume_join(
    segments: list[dict],
    stopped_z: float,
    resume_idx: int,
    resume_header_segments: list[dict] | None = None,
    continuation_segments: list[dict] | None = None,
    current_position: tuple[float, float, float] | None = None,
    printed_bounds: dict | None = None,
    resume_z: float | None = None,
) -> dict:
    """Analyze gap and warnings between last printed extrusion and resume start."""
    resume_header_segments = resume_header_segments or []
    continuation_segments = continuation_segments or []

    last_printed_point: dict | None = None
    last_printed_seg: dict | None = None
    for seg in segments:
        if seg["type"] != "extrusion":
            continue
        if _segment_max_z(seg) <= stopped_z + 1e-6:
            last_printed_seg = seg
            last_printed_point = {
                "x": seg["x2"],
                "y": seg["y2"],
                "z": seg["z2"],
                "line": seg["line"] + 1,
            }

    first_resume_extrusion_point: dict | None = None
    for seg in continuation_segments:
        if seg["type"] == "extrusion":
            first_resume_extrusion_point = {
                "x": seg["x1"],
                "y": seg["y1"],
                "z": seg["z1"],
                "line": seg["line"] + 1,
            }
            break

    gap_mm = 0.0
    if last_printed_point and first_resume_extrusion_point:
        gap_mm = _xy_distance(
            (last_printed_point["x"], last_printed_point["y"]),
            (first_resume_extrusion_point["x"], first_resume_extrusion_point["y"]),
        )

    z_mismatch = 0.0
    if first_resume_extrusion_point:
        target_z = resume_z if resume_z is not None else stopped_z
        z_mismatch = abs(first_resume_extrusion_point["z"] - target_z)

    warnings: list[str] = []
    if gap_mm > 0.5:
        warnings.append(
            f"Join gap is {gap_mm:.2f} mm in XY — resume may leave empty space before extrusion continues."
        )
    if first_resume_extrusion_point and first_resume_extrusion_point["z"] < stopped_z - 0.05:
        warnings.append(
            f"Resume extrusion starts at Z {first_resume_extrusion_point['z']:.2f} mm, "
            f"below stopped Z ({stopped_z:.2f} mm)."
        )
    elif first_resume_extrusion_point is not None and resume_z is not None and z_mismatch > 0.15:
        warnings.append(
            f"First extrusion Z ({first_resume_extrusion_point['z']:.2f} mm) differs from "
            f"resume target Z ({resume_z:.2f} mm) by {z_mismatch:.2f} mm."
        )

    travel_crosses_print = False
    if printed_bounds and resume_header_segments and current_position:
        bx = printed_bounds
        for seg in resume_header_segments:
            if seg["type"] != "travel":
                continue
            if _line_crosses_rect(
                seg["x1"],
                seg["y1"],
                seg["x2"],
                seg["y2"],
                bx["min_x"],
                bx["max_x"],
                bx["min_y"],
                bx["max_y"],
            ):
                travel_crosses_print = True
                break

    if travel_crosses_print:
        warnings.append(
            "Resume travel path may cross over already-printed area — verify nozzle height is sufficient."
        )

    return {
        "last_printed_point": last_printed_point,
        "first_resume_extrusion_point": first_resume_extrusion_point,
        "gap_mm": round(gap_mm, 3),
        "z_mismatch_mm": round(z_mismatch, 3),
        "travel_crosses_print": travel_crosses_print,
        "warnings": warnings,
        "current_position": (
            {"x": current_position[0], "y": current_position[1], "z": current_position[2]}
            if current_position
            else None
        ),
    }


def _cap_segments(segments: list[dict], limit: int = MAX_PREVIEW_SEGMENTS) -> tuple[list[dict], bool]:
    if len(segments) <= limit:
        return segments, False
    step = max(1, len(segments) // limit)
    return [segments[i] for i in range(0, len(segments), step)], True


def _state_at_line(lines: list[str], line_index: int) -> tuple[float, float, float, float, bool, bool]:
    if line_index <= 0:
        return 0.0, 0.0, 0.0, 0.0, True, True
    partial = parse_gcode_paths(lines[:line_index])
    pos = partial["final_position"]
    return (
        pos["x"],
        pos["y"],
        pos["z"],
        pos["e"],
        partial["absolute_xyz"],
        partial["absolute_e"],
    )


def build_path_preview(
    original_lines: list[str],
    params: ResumeParams,
    result: ResumeResult,
) -> dict:
    """Build preview payload for API / visualization."""
    full_parse = parse_gcode_paths(original_lines, params.stopped_z, result.resume_line_index)
    all_segments = full_parse["segments"]

    header_end = len(result.output_lines) - (len(original_lines) - result.resume_line_index)
    header_lines = result.output_lines[:header_end]
    header_parse = parse_gcode_paths(
        header_lines,
        initial_x=params.current_x,
        initial_y=params.current_y,
        initial_z=params.current_z,
        initial_absolute_xyz=True,
        initial_absolute_e=False,
    )
    resume_segments = header_parse["segments"]

    cx, cy, cz, ce, abs_xyz, abs_e = _state_at_line(original_lines, result.resume_line_index)
    continuation_lines = original_lines[result.resume_line_index :]
    continuation_parse = parse_gcode_paths(
        continuation_lines,
        initial_x=cx,
        initial_y=cy,
        initial_z=cz,
        initial_e=ce,
        initial_absolute_xyz=abs_xyz,
        initial_absolute_e=abs_e,
    )
    continuation_segments = continuation_parse["segments"]
    for seg in continuation_segments:
        seg["line"] = seg["line"] + result.resume_line_index

    printed_segments = [
        seg
        for seg in all_segments
        if seg["type"] == "extrusion" and _segment_max_z(seg) <= params.stopped_z + 1e-6
    ]

    printed_bounds = full_parse["bounds"]
    if printed_segments:
        printed_bounds = {
            "min_x": min(min(s["x1"], s["x2"]) for s in printed_segments),
            "max_x": max(max(s["x1"], s["x2"]) for s in printed_segments),
            "min_y": min(min(s["y1"], s["y2"]) for s in printed_segments),
            "max_y": max(max(s["y1"], s["y2"]) for s in printed_segments),
            "min_z": min(min(s["z1"], s["z2"]) for s in printed_segments),
            "max_z": max(max(s["z1"], s["z2"]) for s in printed_segments),
        }

    join_info = analyze_resume_join(
        all_segments,
        params.stopped_z,
        result.resume_line_index,
        resume_header_segments=resume_segments,
        continuation_segments=continuation_segments,
        current_position=(params.current_x, params.current_y, params.current_z),
        printed_bounds=printed_bounds,
        resume_z=result.resume_z,
    )

    capped_all, sampled = _cap_segments(all_segments)
    capped_printed, _ = _cap_segments(printed_segments, limit=MAX_PREVIEW_SEGMENTS // 2)
    capped_resume, _ = _cap_segments(resume_segments, limit=500)
    capped_continuation, _ = _cap_segments(continuation_segments, limit=MAX_PREVIEW_SEGMENTS // 2)

    return {
        "segments": capped_all,
        "printed_segments": capped_printed,
        "resume_segments": capped_resume,
        "continuation_segments": capped_continuation,
        "join_info": join_info,
        "bounds": full_parse["bounds"],
        "layers": full_parse["layers"],
        "sampled": sampled,
        "stopped_z": params.stopped_z,
        "resume_line_index": result.resume_line_index,
    }


def generate_resume_gcode(
    lines: list[str],
    params: ResumeParams,
    require_extrusion: bool = True,
) -> ResumeResult:
    analysis = analyze_gcode(lines)
    warnings = validate_params(params, analysis)

    hotend_temp, bed_temp = _extract_temps(lines)
    resume_idx, resume_z, resume_x, resume_y, resume_e, note = find_resume_index(
        lines, params.stopped_z, require_extrusion=require_extrusion
    )
    if note:
        warnings.append(note)

    header = build_resume_header(
        params, hotend_temp, bed_temp, resume_x, resume_y, resume_z
    )

    output = header + lines[resume_idx:]
    warnings = [w for w in warnings if w]

    return ResumeResult(
        output_lines=output,
        resume_line_index=resume_idx,
        resume_z=resume_z,
        resume_x=resume_x,
        resume_y=resume_y,
        resume_e=resume_e,
        hotend_temp=hotend_temp,
        bed_temp=bed_temp,
        lines_removed=resume_idx,
        warnings=warnings,
        analysis=analysis,
    )


def process_file(
    input_path: Path,
    output_path: Path,
    params: ResumeParams,
    require_extrusion: bool = True,
) -> ResumeResult:
    lines = input_path.read_text(encoding="utf-8", errors="replace").splitlines()
    result = generate_resume_gcode(lines, params, require_extrusion=require_extrusion)
    output_path.write_text("\n".join(result.output_lines) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resume a failed 3D print from a given Z height."
    )
    parser.add_argument("input", type=Path, help="Original G-code file")
    parser.add_argument("output", type=Path, help="Output resumed G-code file")
    parser.add_argument(
        "--stopped-z",
        type=float,
        required=True,
        help="Z height of the last successfully printed layer (mm)",
    )
    parser.add_argument("--current-x", type=float, required=True, help="Current nozzle X (mm)")
    parser.add_argument("--current-y", type=float, required=True, help="Current nozzle Y (mm)")
    parser.add_argument("--current-z", type=float, required=True, help="Current nozzle Z (mm)")
    parser.add_argument("--z-lift", type=float, default=2.0, help="Safe Z lift before travel (mm)")
    parser.add_argument(
        "--include-travel",
        action="store_true",
        help="Resume at first Z move above stopped Z, even without extrusion",
    )

    args = parser.parse_args()
    params = ResumeParams(
        stopped_z=args.stopped_z,
        current_x=args.current_x,
        current_y=args.current_y,
        current_z=args.current_z,
        z_lift=args.z_lift,
    )

    result = process_file(
        args.input,
        args.output,
        params,
        require_extrusion=not args.include_travel,
    )

    print(f"Resumed at line {result.resume_line_index + 1}")
    if result.resume_z is not None:
        print(
            f"Resume position: X={result.resume_x:.3f} Y={result.resume_y:.3f} "
            f"Z={result.resume_z:.3f}"
        )
    print(f"Removed {result.lines_removed} lines from start")
    if result.hotend_temp:
        print(f"Hotend temp: {result.hotend_temp:.0f}°C")
    if result.bed_temp:
        print(f"Bed temp: {result.bed_temp:.0f}°C")
    for warning in result.warnings:
        print(f"Warning: {warning}")


if __name__ == "__main__":
    main()
