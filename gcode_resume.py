#!/usr/bin/env python3
"""G-code resume tool — continue a failed print from a given layer height."""

from __future__ import annotations

import argparse
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
