#!/usr/bin/env python3
"""Measure spatial resolution in a production MSH without modifying it."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _percentiles(np: Any, values: Any) -> dict[str, float | int]:
    if values.size == 0 or not bool(np.all(np.isfinite(values))):
        raise ValueError("resolution sample is empty or nonfinite")
    return {
        "count": int(values.size),
        "minimum": float(np.min(values)),
        "p05": float(np.percentile(values, 5)),
        "p50": float(np.percentile(values, 50)),
        "p95": float(np.percentile(values, 95)),
        "maximum": float(np.max(values)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mesh", type=Path, required=True)
    parser.add_argument("--family-plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    mesh = args.mesh.resolve(strict=True)
    plan_path = args.family_plan.resolve(strict=True)
    output = args.output.resolve(strict=False)
    if mesh.is_symlink() or plan_path.is_symlink() or not mesh.is_file():
        raise ValueError("mesh/plan input must be a regular non-link file")
    if output.exists() or output.parent != (Path.cwd() / "runs" / "mesh_region_review").resolve(strict=False):
        raise ValueError("output must be new under runs/mesh_region_review")
    output.parent.mkdir(parents=True, exist_ok=True)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    regions = plan.get("physical_regions")
    if not isinstance(regions, list) or len(regions) != 4:
        raise ValueError("family plan does not contain four physical regions")

    import gmsh  # Imported only by this explicit Gmsh audit entry point.
    import numpy as np

    gmsh.initialize(["cfdpipe-region-audit", "-nopopup"])
    try:
        gmsh.open(str(mesh))
        node_tags, raw_coordinates, _ = gmsh.model.mesh.getNodes()
        tags = np.asarray(node_tags, dtype=np.int64)
        coordinates = np.asarray(raw_coordinates, dtype=np.float64).reshape((-1, 3))
        if tags.size == 0 or coordinates.shape[0] != tags.size:
            raise ValueError("mesh node inventory is invalid")
        by_tag = np.full((int(tags.max()) + 1, 3), np.nan, dtype=np.float64)
        by_tag[tags] = coordinates
        element_types, element_tags, node_connectivity = gmsh.model.mesh.getElements(3)
        tetra_blocks: list[Any] = []
        prism_blocks: list[Any] = []
        for element_type, raw_element_tags, raw_nodes in zip(
            element_types, element_tags, node_connectivity
        ):
            properties = gmsh.model.mesh.getElementProperties(int(element_type))
            name, _dimension, _order, node_count = properties[:4]
            connectivity = np.asarray(raw_nodes, dtype=np.int64).reshape(
                (len(raw_element_tags), int(node_count))
            )
            if name == "Tetrahedron 4":
                tetra_blocks.append(connectivity)
            elif name == "Prism 6":
                prism_blocks.append(connectivity)
        if not tetra_blocks or not prism_blocks:
            raise ValueError("mesh does not contain both Tet4 and Prism6")
        tetra = np.concatenate(tetra_blocks, axis=0)
        prism = np.concatenate(prism_blocks, axis=0)
        bounds = np.asarray([item["bounds_m"] for item in regions], dtype=np.float64)
        region_names = [str(item["name"]) for item in regions]
        samples: list[list[Any]] = [[] for _ in regions]
        outside_count = 0
        chunk_size = 200_000
        for start in range(0, tetra.shape[0], chunk_size):
            connectivity = tetra[start : start + chunk_size]
            points = by_tag[connectivity]
            if not bool(np.all(np.isfinite(points))):
                raise ValueError("Tet4 references an unavailable node")
            centroid = np.mean(points, axis=1)
            six_volume = np.abs(
                np.einsum(
                    "ij,ij->i",
                    points[:, 1] - points[:, 0],
                    np.cross(points[:, 2] - points[:, 0], points[:, 3] - points[:, 0]),
                )
            )
            characteristic = np.cbrt(six_volume / 6.0)
            assigned = np.full(connectivity.shape[0], -1, dtype=np.int8)
            for index in reversed(range(4)):
                inside = np.all(
                    (centroid >= bounds[index, :3])
                    & (centroid <= bounds[index, 3:]),
                    axis=1,
                )
                assigned[(assigned < 0) & inside] = index
            outside_count += int(np.count_nonzero(assigned < 0))
            for index in range(4):
                values = characteristic[assigned == index]
                if values.size:
                    samples[index].append(values)
        region_statistics = {
            name: _percentiles(np, np.concatenate(values))
            for name, values in zip(region_names, samples)
        }
        p = by_tag[prism]
        if not bool(np.all(np.isfinite(p))):
            raise ValueError("Prism6 references an unavailable node")
        bottom_centroid = np.mean(p[:, :3], axis=1)
        top_centroid = np.mean(p[:, 3:], axis=1)
        wall_normal = np.linalg.norm(top_centroid - bottom_centroid, axis=1)
        bottom_edges = np.stack(
            (
                np.linalg.norm(p[:, 1] - p[:, 0], axis=1),
                np.linalg.norm(p[:, 2] - p[:, 1], axis=1),
                np.linalg.norm(p[:, 0] - p[:, 2], axis=1),
            ),
            axis=1,
        )
        wall_tangential = np.mean(bottom_edges, axis=1)
        medians = [region_statistics[name]["p50"] for name in region_names]
        volume_order_pass = all(left > right for left, right in zip(medians, medians[1:]))
        wall_normal_stats = _percentiles(np, wall_normal)
        wall_dense_pass = wall_normal_stats["p50"] < region_statistics["internal_passage"]["p50"]
        report = {
            "schema": "cfdpipe.production_mesh_region_distribution.v1",
            "status": "PASS" if volume_order_pass and wall_dense_pass else "FAIL",
            "mesh": {
                "path": str(mesh),
                "size_bytes": mesh.stat().st_size,
                "sha256": _sha256(mesh),
                "node_count": int(tags.size),
                "tetrahedron_count": int(tetra.shape[0]),
                "prism_count": int(prism.shape[0]),
            },
            "family_plan_sha256": _sha256(plan_path),
            "tetra_characteristic_length_m_by_nominal_region": region_statistics,
            "tetra_centroids_outside_farfield_bounds": outside_count,
            "wall_prism_normal_thickness_m": wall_normal_stats,
            "wall_prism_tangential_edge_mean_m": _percentiles(np, wall_tangential),
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
