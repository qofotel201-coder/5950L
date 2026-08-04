"""Strict CAD-free review of a frozen production mesh-region contract."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence


class MeshRegionReviewError(ValueError):
    """Raised when a mesh-family authorization input fails closed."""


_REGION_NAMES = (
    "farfield",
    "transition",
    "near_body_core",
    "internal_passage",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MeshRegionReviewError(f"{label} is not numeric")
    result = float(value)
    if not math.isfinite(result):
        raise MeshRegionReviewError(f"{label} is not finite")
    return result


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MeshRegionReviewError(f"{label} is not a positive integer")
    return value


def _nested(inner: Sequence[float], outer: Sequence[float]) -> bool:
    return all(
        inner[axis] >= outer[axis]
        and inner[axis + 3] <= outer[axis + 3]
        for axis in range(3)
    )


def review_mesh_regions(
    *,
    plan: Mapping[str, Any],
    coarse_result: Mapping[str, Any],
    project: Mapping[str, Any],
    plan_sha256: str,
    coarse_result_sha256: str,
) -> dict[str, Any]:
    """Return a strict authorization report or raise on the first bad contract."""

    if plan.get("schema") != "cfdpipe.production_mesh_family_plan.v1":
        raise MeshRegionReviewError("mesh-family plan schema is invalid")
    if plan.get("status") != "COARSE_REGIONS_FROZEN_MEDIUM_AUTHORIZED":
        raise MeshRegionReviewError("mesh-family plan is not in the authorization state")
    family = plan.get("mesh_family")
    levels = plan.get("levels")
    raw_regions = plan.get("physical_regions")
    if not isinstance(family, Mapping) or not isinstance(levels, Mapping):
        raise MeshRegionReviewError("mesh-family plan structure is incomplete")
    if not isinstance(raw_regions, list) or len(raw_regions) != 4:
        raise MeshRegionReviewError("exactly four physical regions are required")
    if [item.get("name") for item in raw_regions if isinstance(item, Mapping)] != list(
        _REGION_NAMES
    ):
        raise MeshRegionReviewError("physical region order or names changed")

    factors = family.get("level_size_factors")
    if factors != [1.0, 0.794, 0.63]:
        raise MeshRegionReviewError("mesh-family scale factors changed")
    if family.get("boundary_layer_count") != 60:
        raise MeshRegionReviewError("the frozen 60-layer boundary layer changed")
    first_layer = _number(family.get("first_layer_height_m"), "first-layer height")
    growth = _number(family.get("growth_ratio"), "growth ratio")
    wall_tangential = _number(
        family.get("wall_tangential_size_m"), "wall tangential size"
    )
    if first_layer <= 0.0 or growth <= 1.0 or wall_tangential != 0.4:
        raise MeshRegionReviewError("frozen wall resolution parameters changed")
    implementation = family.get("sizing_implementation")
    if implementation != {
        "dimension_3_only": True,
        "region_composition": "minimum",
        "transition_interpolation": "linear_distance_to_box",
        "wall_surface_triangulation_preserved": True,
    }:
        raise MeshRegionReviewError("volume sizing implementation is not frozen")

    regions: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_regions):
        if not isinstance(raw, Mapping):
            raise MeshRegionReviewError("physical region is not an object")
        bounds_raw = raw.get("bounds_m")
        if not isinstance(bounds_raw, list) or len(bounds_raw) != 6:
            raise MeshRegionReviewError("physical region bounds are invalid")
        bounds = [_number(value, f"region {index} bound") for value in bounds_raw]
        if any(bounds[axis] >= bounds[axis + 3] for axis in range(3)):
            raise MeshRegionReviewError("physical region bounds are empty")
        size = _number(raw.get("coarse_size_m"), f"region {index} size")
        transition = _number(
            raw.get("transition_width_m"), f"region {index} transition"
        )
        if size <= 0.0 or transition <= 0.0:
            raise MeshRegionReviewError("physical region size/transition is not positive")
        regions.append(
            {
                "name": _REGION_NAMES[index],
                "bounds_m": bounds,
                "coarse_size_m": size,
                "medium_size_m": size * 0.794,
                "transition_width_m": transition,
            }
        )
    sizes = [item["coarse_size_m"] for item in regions]
    if not all(left > right for left, right in zip(sizes, sizes[1:])):
        raise MeshRegionReviewError("farfield-to-passage sizes are not strictly refined")
    if not all(_nested(regions[index + 1]["bounds_m"], regions[index]["bounds_m"])
               for index in range(3)):
        raise MeshRegionReviewError("physical region bounds are not nested")

    medium = levels.get("medium")
    if not isinstance(medium, Mapping) or medium.get("build_authorized") is not True:
        raise MeshRegionReviewError("medium mesh is not explicitly authorized")
    project_mesh = project.get("mesh")
    if not isinstance(project_mesh, Mapping):
        raise MeshRegionReviewError("project mesh contract is missing")
    target = _positive_integer(medium.get("target_cells"), "medium target")
    minimum = _positive_integer(medium.get("minimum_cells"), "medium minimum")
    maximum = _positive_integer(medium.get("maximum_cells"), "medium maximum")
    if (target, minimum, maximum) != (
        project_mesh.get("medium_target_cells"),
        project_mesh.get("medium_min_cells"),
        project_mesh.get("medium_max_cells"),
    ) or not minimum <= target <= maximum:
        raise MeshRegionReviewError("medium cell contract differs from project.toml")

    required_result = {
        "status": "PASS",
        "boundary_layer_count": 60,
        "prism_below_threshold_element_count": 0,
        "negative_volume_count": 0,
        "nonpositive_jacobian_count": 0,
        "nonfinite_quality_count": 0,
        "core_below_gamma_count": 0,
        "marker_omission_count": 0,
        "shared_interface_error_count": 0,
        "conformal_status": "PASS",
        "su2_text_audit": "PASS",
    }
    for key, expected in required_result.items():
        if coarse_result.get(key) != expected:
            raise MeshRegionReviewError(f"coarse result gate {key} is not PASS")
    coarse_count = _positive_integer(
        coarse_result.get("element_count_3d"), "coarse element count"
    )
    target_range = coarse_result.get("target_element_range")
    if target_range != [4_750_000, 5_250_000] or not target_range[0] <= coarse_count <= target_range[1]:
        raise MeshRegionReviewError("coarse element count is outside its frozen range")
    minimum_sj = _number(
        coarse_result.get("minimum_prism_scaled_jacobian"),
        "minimum prism Scaled Jacobian",
    )
    if minimum_sj < 0.01:
        raise MeshRegionReviewError("coarse prism Scaled Jacobian gate failed")
    for name in ("mesh_msh", "mesh_su2"):
        record = coarse_result.get(name)
        if (
            not isinstance(record, Mapping)
            or _positive_integer(record.get("size_bytes"), f"{name} size") <= 0
            or not isinstance(record.get("sha256"), str)
            or len(record["sha256"]) != 64
        ):
            raise MeshRegionReviewError(f"{name} evidence is incomplete")
    bundle = coarse_result.get("evidence_bundle")
    if not isinstance(bundle, Mapping) or bundle.get("local_verification") != "PASS":
        raise MeshRegionReviewError("coarse evidence bundle was not locally verified")

    prism_count = _positive_integer(
        coarse_result.get("prism_element_count"), "coarse prism count"
    )
    core_count = coarse_count - prism_count
    projected_medium = round(core_count / (0.794**3) + prism_count)
    if not minimum <= projected_medium <= maximum:
        raise MeshRegionReviewError("medium cubic count projection is outside its contract")
    return {
        "schema": "cfdpipe.production_mesh_region_review.v1",
        "status": "PASS",
        "coarse_regions_frozen": True,
        "customer_requirements": {
            "farfield_coarsest": "PASS",
            "wall_normal_resolution_dense": "PASS",
            "internal_passage_finest": "PASS",
            "near_body_core_refined": "PASS",
            "smooth_transitions": "PASS",
        },
        "input_bindings": {
            "family_plan_sha256": plan_sha256,
            "coarse_result_sha256": coarse_result_sha256,
            "coarse_mesh_msh_sha256": coarse_result["mesh_msh"]["sha256"],
            "coarse_mesh_su2_sha256": coarse_result["mesh_su2"]["sha256"],
            "coarse_evidence_bundle_sha256": bundle.get("sha256"),
        },
        "frozen_wall": {
            "tangential_size_m": wall_tangential,
            "first_layer_height_m": first_layer,
            "growth_ratio": growth,
            "layer_count": 60,
        },
        "regions": regions,
        "coarse_quality": {
            "element_count_3d": coarse_count,
            "minimum_prism_scaled_jacobian": minimum_sj,
            "all_hard_gate_failure_counts": 0,
        },
        "medium_authorization": {
            "status": "AUTHORIZED_NOT_BUILT",
            "target_cells": target,
            "minimum_cells": minimum,
            "maximum_cells": maximum,
            "uniform_size_factor": 0.794,
            "cubic_projection_cells": projected_medium,
            "output_root": "runs/mesh/medium",
            "rans_allowed": False,
            "fine_mesh_allowed": False,
        },
    }


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise MeshRegionReviewError(f"JSON input is not a regular file: {path}")
    value = json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=lambda token: (_ for _ in ()).throw(
            MeshRegionReviewError(f"JSON input contains {token}")
        ),
    )
    if not isinstance(value, dict):
        raise MeshRegionReviewError("JSON input is not an object")
    return value
