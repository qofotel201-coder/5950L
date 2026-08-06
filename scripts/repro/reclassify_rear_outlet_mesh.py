#!/usr/bin/env python3
"""Split a planar rear outlet from adjacent conical farfield faces in SU2 mesh."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def assignment(line: str, expected: str) -> str:
    if "=" not in line:
        raise ValueError(f"missing {expected}")
    key, value = (part.strip() for part in line.split("=", 1))
    if key != expected or not value:
        raise ValueError(f"expected {expected}, got {key}")
    return value


def scan(path: Path, outlet: str) -> tuple[int, list[str], dict[str, list[str]], set[int]]:
    with path.open("r", encoding="utf-8") as stream:
        lines = iter(stream)
        if int(assignment(next(lines), "NDIME")) != 3:
            raise ValueError("mesh must be three-dimensional")
        nelem = int(assignment(next(lines), "NELEM"))
        for _ in range(nelem):
            next(lines)
        npoin = int(assignment(next(lines), "NPOIN"))
        for _ in range(npoin):
            next(lines)
        nmark = int(assignment(next(lines), "NMARK"))
        order: list[str] = []
        markers: dict[str, list[str]] = {}
        for _ in range(nmark):
            name = assignment(next(lines), "MARKER_TAG")
            count = int(assignment(next(lines), "MARKER_ELEMS"))
            if name in markers:
                raise ValueError(f"duplicate marker {name}")
            order.append(name)
            markers[name] = [next(lines).rstrip("\n") for _ in range(count)]
    if outlet not in markers:
        raise ValueError(f"outlet marker {outlet} is absent")
    nodes: set[int] = set()
    for record in markers[outlet]:
        fields = record.split()
        if len(fields) not in (4, 5) or fields[0] != "5":
            raise ValueError("rear outlet must contain only Triangle 3 records")
        nodes.update(int(value) for value in fields[1:4])
    return npoin, order, markers, nodes


def selected_coordinates(path: Path, npoin: int, selected: set[int]) -> dict[int, tuple[float, float, float]]:
    coordinates: dict[int, tuple[float, float, float]] = {}
    with path.open("r", encoding="utf-8") as stream:
        lines = iter(stream)
        next(lines)
        nelem = int(assignment(next(lines), "NELEM"))
        for _ in range(nelem):
            next(lines)
        declared = int(assignment(next(lines), "NPOIN"))
        if declared != npoin:
            raise ValueError("NPOIN changed between scans")
        for index in range(npoin):
            fields = next(lines).split()
            if index in selected:
                xyz = tuple(float(value) for value in fields[:3])
                if len(xyz) != 3 or any(not math.isfinite(value) for value in xyz):
                    raise ValueError("outlet node has invalid coordinates")
                coordinates[index] = xyz
    if set(coordinates) != selected:
        raise ValueError("not all outlet node coordinates were resolved")
    return coordinates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--outlet", default="rear_outlet_1")
    parser.add_argument("--farfield", default="front_conical_surface")
    parser.add_argument("--plane-x", required=True, type=float)
    parser.add_argument("--tolerance", default=2.0e-7, type=float)
    args = parser.parse_args()
    source = args.input.resolve(strict=True)
    output = args.output.resolve()
    report = args.report.resolve()
    if source.is_symlink() or output.exists() or report.exists():
        raise ValueError("input must be regular and outputs must be new")
    if output.parent != report.parent or not output.parent.is_dir():
        raise ValueError("output and report must share an existing directory")
    if not math.isfinite(args.plane_x) or not 0.0 < args.tolerance < 1.0e-3:
        raise ValueError("plane/tolerance is invalid")

    npoin, order, markers, nodes = scan(source, args.outlet)
    if args.farfield not in markers:
        raise ValueError("farfield marker is absent")
    coordinates = selected_coordinates(source, npoin, nodes)
    planar: list[str] = []
    moved: list[str] = []
    moved_x: list[float] = []
    for record in markers[args.outlet]:
        ids = [int(value) for value in record.split()[1:4]]
        xs = [coordinates[index][0] for index in ids]
        if max(abs(value - args.plane_x) for value in xs) <= args.tolerance:
            planar.append(record)
        else:
            if max(xs) > args.plane_x + args.tolerance:
                raise ValueError("nonplanar outlet face crosses the outlet plane")
            if sum(xs) / 3.0 >= args.plane_x - args.tolerance:
                raise ValueError("nonplanar outlet face centroid is not upstream")
            moved.append(record)
            moved_x.extend(xs)
    if not planar or not moved:
        raise ValueError("repair requires both planar and nonplanar outlet faces")
    markers[args.farfield].extend(moved)
    markers[args.outlet] = planar

    temporary = output.with_name(output.name + ".partial")
    if temporary.exists():
        raise ValueError("partial output already exists")
    with source.open("r", encoding="utf-8") as src, temporary.open("w", encoding="utf-8", newline="\n") as dst:
        for line in src:
            if line.strip().startswith("NMARK"):
                dst.write(f"NMARK= {len(order)}\n")
                break
            dst.write(line)
        for name in order:
            dst.write(f"MARKER_TAG= {name}\n")
            dst.write(f"MARKER_ELEMS= {len(markers[name])}\n")
            for record in markers[name]:
                dst.write(record + "\n")
    temporary.replace(output)
    payload = {
        "schema": "cfdpipe.rear_outlet_mesh_reclassification.v1",
        "status": "PASS",
        "input": {"path": str(source), "sha256": digest(source), "size_bytes": source.stat().st_size},
        "output": {"path": str(output), "sha256": digest(output), "size_bytes": output.stat().st_size},
        "contract": {"outlet": args.outlet, "farfield": args.farfield, "plane_x_m": args.plane_x, "tolerance_m": args.tolerance},
        "counts": {"original_outlet_faces": len(planar) + len(moved), "planar_outlet_faces": len(planar), "moved_to_farfield_faces": len(moved), "farfield_faces_after": len(markers[args.farfield])},
        "moved_node_x_range_m": [min(moved_x), max(moved_x)],
        "volume_and_point_records_unchanged": True,
    }
    report.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
