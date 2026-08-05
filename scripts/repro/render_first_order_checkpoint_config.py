#!/usr/bin/env python3
"""Render an independent or manifest-restarted first-order RANS checkpoint."""

from __future__ import annotations

import argparse
from pathlib import Path
import re


def assignments(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(
            r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$",
            line.split("%", 1)[0],
        )
        if match:
            if match.group(1) in result:
                raise ValueError(f"duplicate assignment: {match.group(1)}")
            result[match.group(1)] = match.group(2)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--mesh", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--restart",
        action="store_true",
        help="Enable restart input; omit only for the independent first segment",
    )
    parser.add_argument(
        "--full-output",
        action="store_true",
        help="Write ParaView volume/surface output for a diagnostic checkpoint",
    )
    parser.add_argument(
        "--cfl",
        choices=("0.001", "0.005", "0.01", "0.05", "0.1", "0.5", "1.0"),
        default="0.5",
    )
    args = parser.parse_args()
    mesh = args.mesh.resolve(strict=True)
    if not mesh.is_file() or mesh.is_symlink():
        raise ValueError("mesh must be a non-symlink regular file")
    text = args.base.read_text(encoding="utf-8")
    values = assignments(text)
    expected = {
        "SOLVER": "RANS",
        "KIND_TURB_MODEL": "SST",
        "SST_OPTIONS": "V2003m",
        "MUSCL_FLOW": "NO",
        "MUSCL_TURB": "NO",
        "CONV_NUM_METHOD_FLOW": "ROE",
        "MARKER_SUPERSONIC_OUTLET": "( rear_outlet_1, rear_outlet_2 )",
    }
    for name, expected_value in expected.items():
        if values.get(name) != expected_value:
            raise ValueError(f"frozen first-order contract mismatch: {name}")
    replaced = {
        "MESH_FILENAME",
        "RESTART_SOL",
        "SOLUTION_FILENAME",
        "RESTART_FILENAME",
        "VOLUME_FILENAME",
        "SURFACE_FILENAME",
        "CFL_NUMBER",
        "ITER",
        "CONV_STARTITER",
        "RAMP_MUSCL",
        "RAMP_MUSCL_COEFF",
        "RAMP_MUSCL_POWER",
        "KIND_MUSCL_RAMP",
        "OUTPUT_FILES",
    }
    lines: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^\s*([A-Z][A-Z0-9_]*)\s*=", line.split("%", 1)[0])
        if match and match.group(1) in replaced:
            continue
        lines.append(line)
    lines.extend(
        (
            f"MESH_FILENAME= {mesh}",
            f"RESTART_SOL= {'YES' if args.restart else 'NO'}",
            "SOLUTION_FILENAME= restart_input",
            "RESTART_FILENAME= restart",
            "VOLUME_FILENAME= solution",
            "SURFACE_FILENAME= surface_solution",
            f"CFL_NUMBER= {args.cfl}",
            "ITER= 500",
            "CONV_STARTITER= 501",
            "RAMP_MUSCL= NO",
            (
                "OUTPUT_FILES= RESTART, PARAVIEW, SURFACE_PARAVIEW"
                if args.full_output
                else "OUTPUT_FILES= RESTART"
            ),
        )
    )
    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
