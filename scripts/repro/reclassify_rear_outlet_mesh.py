#!/usr/bin/env python3
"""Split a planar rear outlet from adjacent conical farfield faces in SU2 mesh."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import tomllib


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
    parser.add_argument("--contract", required=True, type=Path)
    args = parser.parse_args()
    source = args.input.resolve(strict=True)
    output = args.output.resolve()
    report = args.report.resolve()
    contract_path = args.contract.resolve(strict=True)
    with contract_path.open("rb") as stream:
        contract = tomllib.load(stream)
    if contract.get("schema") != "cfdpipe.rear_outlet_marker_correction.v1" or contract.get("status") != "APPROVED":
        raise ValueError("marker correction contract is not approved")
    source_contract = contract.get("source_mesh", {})
    output_contract = contract.get("output_mesh", {})
    classification = contract.get("classification", {})
    policy = contract.get("policy", {})
    required_policy = ("source_geometry_unchanged", "boundary_conditions_unchanged", "volume_topology_unchanged", "runtime_gmsh_tags_forbidden", "old_solution_is_diagnostic_only", "independent_first_order_restart_required")
    if not all(policy.get(key) is True for key in required_policy):
        raise ValueError("marker correction safety policy is incomplete")
    source_sha = digest(source)
    if source_sha != source_contract.get("sha256"):
        raise ValueError("source mesh SHA-256 does not match correction contract")
    outlet = classification.get("outlet_marker")
    farfield = classification.get("farfield_marker")
    plane_x = classification.get("plane_x_m")
    tolerance = classification.get("tolerance_m")
    if not isinstance(outlet, str) or not isinstance(farfield, str):
        raise ValueError("contract marker names are invalid")
    if source.is_symlink() or output.exists() or report.exists():
        raise ValueError("input must be regular and outputs must be new")
    if output.parent != report.parent or not output.parent.is_dir():
        raise ValueError("output and report must share an existing directory")
    if not isinstance(plane_x, (int, float)) or not isinstance(tolerance, (int, float)) or not math.isfinite(plane_x) or not 0.0 < tolerance < 1.0e-3:
        raise ValueError("plane/tolerance is invalid")

    npoin, order, markers, nodes = scan(source, outlet)
    if farfield not in markers:
        raise ValueError("farfield marker is absent")
    coordinates = selected_coordinates(source, npoin, nodes)
    planar: list[str] = []
    moved: list[str] = []
    moved_x: list[float] = []
    for record in markers[outlet]:
        ids = [int(value) for value in record.split()[1:4]]
        xs = [coordinates[index][0] for index in ids]
        if max(abs(value - plane_x) for value in xs) <= tolerance:
            planar.append(record)
        else:
            if max(xs) > plane_x + tolerance:
                raise ValueError("nonplanar outlet face crosses the outlet plane")
            if sum(xs) / 3.0 >= plane_x - tolerance:
                raise ValueError("nonplanar outlet face centroid is not upstream")
            moved.append(record)
            moved_x.extend(xs)
    if not planar or not moved:
        raise ValueError("repair requires both planar and nonplanar outlet faces")
    if len(planar) != classification.get("planar_outlet_faces") or len(moved) != classification.get("moved_to_farfield_faces") or len(planar) + len(moved) != classification.get("original_outlet_faces"):
        raise ValueError("classified face counts do not match correction contract")
    markers[farfield].extend(moved)
    markers[outlet] = planar

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
        "contract": {"path": str(contract_path), "sha256": digest(contract_path)},
        "input": {"path": str(source), "sha256": source_sha, "size_bytes": source.stat().st_size},
        "output": {"path": str(output), "sha256": digest(output), "size_bytes": output.stat().st_size},
        "classification": {"outlet": outlet, "farfield": farfield, "plane_x_m": plane_x, "tolerance_m": tolerance},
        "counts": {"original_outlet_faces": len(planar) + len(moved), "planar_outlet_faces": len(planar), "moved_to_farfield_faces": len(moved), "farfield_faces_after": len(markers[farfield])},
        "moved_node_x_range_m": [min(moved_x), max(moved_x)],
        "volume_and_point_records_unchanged": True,
    }
    if payload["output"]["sha256"] != output_contract.get("sha256"):
        output.unlink()
        raise ValueError("derived mesh SHA-256 does not match correction contract")
    report.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
