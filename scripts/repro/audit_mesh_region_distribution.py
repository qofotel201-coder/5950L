#!/usr/bin/env python3
"""Measure spatial resolution in a production MSH without modifying it."""

from __future__ import annotations

import argparse
from array import array
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _percentiles(values: Iterable[float]) -> dict[str, float | int]:
    ordered = sorted(values)
    if not ordered or not all(math.isfinite(value) for value in ordered):
        raise ValueError("resolution sample is empty or nonfinite")

    def percentile(fraction: float) -> float:
        position = fraction * (len(ordered) - 1)
        lower = int(math.floor(position))
        upper = int(math.ceil(position))
        if lower == upper:
            return float(ordered[lower])
        weight = position - lower
        return float(ordered[lower] * (1.0 - weight) + ordered[upper] * weight)

    return {
        "count": len(ordered),
        "minimum": float(ordered[0]),
        "p05": percentile(0.05),
        "p50": percentile(0.50),
        "p95": percentile(0.95),
        "maximum": float(ordered[-1]),
    }


def _subtract(left: tuple[float, float, float], right: tuple[float, float, float]) -> tuple[float, float, float]:
    return left[0] - right[0], left[1] - right[1], left[2] - right[2]


def _cross(left: tuple[float, float, float], right: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _dot(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return left[0] * right[0] + left[1] * right[1] + left[2] * right[2]


def _distance(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return math.sqrt(sum((left[axis] - right[axis]) ** 2 for axis in range(3)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mesh", type=Path, required=True)
    parser.add_argument("--family-plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    mesh = args.mesh.resolve(strict=True)
    plan_path = args.family_plan.resolve(strict=True)
    output = args.output.resolve(strict=False)
    allowed_output = (Path.cwd() / "runs" / "mesh_region_review").resolve(
        strict=False
    )
    if mesh.is_symlink() or plan_path.is_symlink() or not mesh.is_file():
        raise ValueError("mesh/plan input must be a regular non-link file")
    if output.exists() or output.parent != allowed_output:
        raise ValueError("output must be new under runs/mesh_region_review")
    output.parent.mkdir(parents=True, exist_ok=True)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    regions = plan.get("physical_regions")
    if not isinstance(regions, list) or len(regions) != 4:
        raise ValueError("family plan does not contain four physical regions")
    bounds = [[float(value) for value in item["bounds_m"]] for item in regions]
    region_names = [str(item["name"]) for item in regions]

    import gmsh  # Imported only by this explicit Gmsh audit entry point.

    gmsh.initialize(["cfdpipe-region-audit", "-nopopup"])
    try:
        gmsh.open(str(mesh))
        node_tags, raw_coordinates, _ = gmsh.model.mesh.getNodes()
        if not node_tags or len(raw_coordinates) != len(node_tags) * 3:
            raise ValueError("mesh node inventory is invalid")
        coordinates = {
            int(node): (
                float(raw_coordinates[index * 3]),
                float(raw_coordinates[index * 3 + 1]),
                float(raw_coordinates[index * 3 + 2]),
            )
            for index, node in enumerate(node_tags)
        }
        element_types, element_tags, node_connectivity = gmsh.model.mesh.getElements(3)
        samples = [array("d") for _ in regions]
        wall_normal = array("d")
        wall_tangential = array("d")
        tetra_count = 0
        prism_count = 0
        outside_count = 0
        for element_type, raw_element_tags, raw_nodes in zip(
            element_types, element_tags, node_connectivity
        ):
            properties = gmsh.model.mesh.getElementProperties(int(element_type))
            name, _dimension, _order, node_count = properties[:4]
            count = len(raw_element_tags)
            if len(raw_nodes) != count * int(node_count):
                raise ValueError("volume element connectivity is malformed")
            if name == "Tetrahedron 4":
                tetra_count += count
                for offset in range(0, len(raw_nodes), 4):
                    p0 = coordinates[int(raw_nodes[offset])]
                    p1 = coordinates[int(raw_nodes[offset + 1])]
                    p2 = coordinates[int(raw_nodes[offset + 2])]
                    p3 = coordinates[int(raw_nodes[offset + 3])]
                    centroid = tuple(
                        (p0[axis] + p1[axis] + p2[axis] + p3[axis]) / 4.0
                        for axis in range(3)
                    )
                    volume = abs(
                        _dot(_subtract(p1, p0), _cross(_subtract(p2, p0), _subtract(p3, p0)))
                    ) / 6.0
                    characteristic = volume ** (1.0 / 3.0)
                    assigned = -1
                    for index in reversed(range(4)):
                        box = bounds[index]
                        if all(
                            box[axis] <= centroid[axis] <= box[axis + 3]
                            for axis in range(3)
                        ):
                            assigned = index
                            break
                    if assigned < 0:
                        outside_count += 1
                    else:
                        samples[assigned].append(characteristic)
            elif name == "Prism 6":
                prism_count += count
                for offset in range(0, len(raw_nodes), 6):
                    points = [coordinates[int(raw_nodes[offset + index])] for index in range(6)]
                    bottom = tuple(sum(point[axis] for point in points[:3]) / 3.0 for axis in range(3))
                    top = tuple(sum(point[axis] for point in points[3:]) / 3.0 for axis in range(3))
                    wall_normal.append(_distance(bottom, top))
                    wall_tangential.append(
                        (
                            _distance(points[0], points[1])
                            + _distance(points[1], points[2])
                            + _distance(points[2], points[0])
                        )
                        / 3.0
                    )
        if tetra_count <= 0 or prism_count <= 0:
            raise ValueError("mesh does not contain both Tet4 and Prism6")
        region_statistics = {
            name: _percentiles(values)
            for name, values in zip(region_names, samples)
        }
        medians = [region_statistics[name]["p50"] for name in region_names]
        volume_order_pass = all(left > right for left, right in zip(medians, medians[1:]))
        wall_normal_stats = _percentiles(wall_normal)
        wall_dense_pass = wall_normal_stats["p50"] < region_statistics["internal_passage"]["p50"]
        report = {
            "schema": "cfdpipe.production_mesh_region_distribution.v1",
            "status": "PASS" if volume_order_pass and wall_dense_pass else "FAIL",
            "mesh": {
                "path": str(mesh),
                "size_bytes": mesh.stat().st_size,
                "sha256": _sha256(mesh),
                "node_count": len(node_tags),
                "tetrahedron_count": tetra_count,
                "prism_count": prism_count,
            },
            "family_plan_sha256": _sha256(plan_path),
            "tetra_characteristic_length_m_by_nominal_region": region_statistics,
            "tetra_centroids_outside_farfield_bounds": outside_count,
            "wall_prism_normal_thickness_m": wall_normal_stats,
            "wall_prism_tangential_edge_mean_m": _percentiles(wall_tangential),
            "checks": {
                "actual_farfield_to_internal_median_size_strictly_decreases": volume_order_pass,
                "actual_wall_normal_median_finer_than_internal_core_median": wall_dense_pass,
            },
        }
        output.write_text(
            json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        print(json.dumps({"status": report["status"], "output": str(output)}))
        return 0 if report["status"] == "PASS" else 1
    finally:
        gmsh.finalize()


if __name__ == "__main__":
    raise SystemExit(main())
