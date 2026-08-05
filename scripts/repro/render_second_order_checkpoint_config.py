#!/usr/bin/env python3
"""Render a bounded second-order checkpoint from an accepted first-order cfg."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--transition", action="store_true")
    parser.add_argument(
        "--full-output",
        action="store_true",
        help="Write ParaView volume/surface output for a diagnostic checkpoint",
    )
    parser.add_argument("--cfl", choices=("0.001", "0.005", "0.01", "0.05", "0.1", "0.5", "1.0"), default="1.0")
    args = parser.parse_args()
    text = args.base.read_text(encoding="utf-8")
    assignments = {}
    for line in text.splitlines():
        match = re.match(r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$", line.split("%", 1)[0])
        if match:
            assignments[match.group(1)] = match.group(2)
    expected = {"SOLVER": "RANS", "KIND_TURB_MODEL": "SST", "SST_OPTIONS": "V2003m",
                "MUSCL_FLOW": "NO", "MUSCL_TURB": "NO"}
    if any(assignments.get(key) != value for key, value in expected.items()):
        raise ValueError("base config is not the frozen accepted first-order contract")
    replaced = {"MUSCL_FLOW", "MUSCL_TURB", "RAMP_MUSCL", "RAMP_MUSCL_COEFF",
                "RAMP_MUSCL_POWER", "KIND_MUSCL_RAMP", "CONV_STARTITER"}
    replaced.add("OUTPUT_FILES")
    replaced.add("CFL_NUMBER")
    lines = []
    for line in text.splitlines():
        match = re.match(r"^\s*([A-Z][A-Z0-9_]*)\s*=", line.split("%", 1)[0])
        if match and match.group(1) in replaced:
            continue
        lines.append(line)
    lines.extend((f"CFL_NUMBER= {args.cfl}", "MUSCL_FLOW= YES", "MUSCL_TURB= YES"))
    if args.transition:
        lines.extend(("RAMP_MUSCL= YES", "RAMP_MUSCL_COEFF= (0.0, 10.0, 500.0)",
                      "RAMP_MUSCL_POWER= 1.0", "KIND_MUSCL_RAMP= SMOOTH_FUNCTION"))
    else:
        lines.append("RAMP_MUSCL= NO")
    lines.append("CONV_STARTITER= 501")
    lines.append(
        "OUTPUT_FILES= RESTART, PARAVIEW, SURFACE_PARAVIEW"
        if args.full_output
        else "OUTPUT_FILES= RESTART"
    )
    rendered = "\n".join(lines) + "\n"
    for frozen in ("SST_OPTIONS= V2003m", "CONV_NUM_METHOD_FLOW= ROE",
                   "MARKER_SUPERSONIC_OUTLET= ( rear_outlet_1, rear_outlet_2 )"):
        if frozen not in rendered:
            raise ValueError(f"frozen contract missing: {frozen}")
    args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
