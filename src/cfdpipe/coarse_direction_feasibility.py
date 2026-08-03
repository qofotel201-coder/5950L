"""Hash-bound owner-free direction-search contracts for coarse audit runs."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Any, Mapping


ENDPOINT_SCHEMA = "cfdpipe.coarse_direction_feasibility_endpoint.v1"
ENDPOINT_MODE = "owner_free_component_direction_exhaustive_v1"
PATTERN_ENDPOINT_SCHEMA = "cfdpipe.coarse_direction_pattern_endpoint.v1"
PATTERN_ENDPOINT_MODE = "owner_free_interaction_component_tangent_pattern_v1"
FRONTIER_PATTERN_ENDPOINT_SCHEMA = (
    "cfdpipe.coarse_direction_frontier_pattern_endpoint.v1"
)
FRONTIER_PATTERN_ENDPOINT_MODE = (
    "owner_free_frontier_interaction_component_count_first_tangent_pattern_v1"
)
FINE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA = (
    "cfdpipe.coarse_direction_frontier_fine_refinement_pattern_endpoint.v1"
)
FINE_REFINEMENT_PATTERN_ENDPOINT_MODE = (
    "owner_free_frontier_single_component_fine_tangent_pattern_v1"
)
TRIPLE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA = (
    "cfdpipe.coarse_direction_frontier_triple_refinement_pattern_endpoint.v1"
)
TRIPLE_REFINEMENT_PATTERN_ENDPOINT_MODE = (
    "owner_free_frontier_single_component_fine_tangent_triple_pattern_v1"
)
COLLAR_REFINEMENT_ENDPOINT_SCHEMA = (
    "cfdpipe.coarse_direction_frontier_collar_refinement_endpoint.v1"
)
COLLAR_REFINEMENT_ENDPOINT_MODE = (
    "owner_free_frontier_single_component_root_schedule_collar_v1"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CoarseDirectionFeasibilityError(RuntimeError):
    """The owner-free direction endpoint is unsafe or stale."""


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def make_owner_free_direction_endpoint(
    *, schedule_endpoint_sha256: str,
) -> dict[str, Any]:
    """Create the one permitted deterministic owner-free audit endpoint."""

    schedule_hash = str(schedule_endpoint_sha256).casefold()
    if _SHA256.fullmatch(schedule_hash) is None:
        raise CoarseDirectionFeasibilityError(
            "schedule endpoint SHA-256 is invalid"
        )
    endpoint: dict[str, Any] = {
        "schema": ENDPOINT_SCHEMA,
        "status": "AUDIT_ONLY",
        "mode": ENDPOINT_MODE,
        "schedule_endpoint_sha256": schedule_hash,
        "candidate_sources": [
            "component_chebyshev",
            "component_mean_bad_triangle_normal",
            "component_mean_original_direction",
            "incident_chebyshev",
            "incident_mean_normal",
            "local_bad_triangle_normal",
            "neighbour_mean_original_direction",
        ],
        "original_direction_interior_weights": [
            0.015625,
            0.0625,
            0.25,
            0.5,
        ],
        "coordinate_descent_passes": 2,
        "maximum_component_count": 16,
        "maximum_candidate_root_count": 64,
        "maximum_quality_evaluations": 4096,
        "absolute_coordinate_replay_required": True,
        "aba_replay_required": True,
        "monotonicity_assumed": False,
        "sharp_owner_used": False,
        "quality_objective": [
            "nonfinite_count",
            "nonpositive_element_count",
            "prism_below_minimum_scaled_jacobian_count",
            "core_tetra_below_minimum_gamma_count",
            "negative_minimum_prism_scaled_jacobian",
            "negative_minimum_core_tetra_gamma",
            "direction_change_penalty",
        ],
        "calibration_PASS_authorized": False,
        "production_mesh_eligible": False,
        "mesh_written": False,
        "su2_called": False,
        "paraview_called": False,
    }
    endpoint["endpoint_sha256"] = _canonical_sha256(endpoint)
    return endpoint


def make_owner_free_direction_pattern_endpoint(
    *, schedule_endpoint_sha256: str,
) -> dict[str, Any]:
    """Create the bounded tangent/pair refinement endpoint without changing v1."""

    endpoint = make_owner_free_direction_endpoint(
        schedule_endpoint_sha256=schedule_endpoint_sha256
    )
    endpoint.pop("endpoint_sha256")
    endpoint.update(
        {
            "schema": PATTERN_ENDPOINT_SCHEMA,
            "mode": PATTERN_ENDPOINT_MODE,
            "candidate_sources": [
                *endpoint["candidate_sources"],
                "source_triangle_edge_pair_pattern",
                "tangent_pattern",
            ],
            "quality_objective": [
                "nonfinite_count",
                "nonpositive_element_count",
                "core_tetra_below_minimum_gamma_count",
                "maximum_core_tetra_gamma_deficit",
                "core_tetra_gamma_deficit_l2",
                "maximum_prism_scaled_jacobian_deficit",
                "prism_scaled_jacobian_deficit_l2",
                "prism_scaled_jacobian_deficit_l1",
                "prism_below_minimum_scaled_jacobian_count",
                "negative_minimum_prism_scaled_jacobian",
                "negative_minimum_core_tetra_gamma",
                "direction_change_penalty",
            ],
            "tangent_pattern_steps_radians": [
                0.5,
                0.25,
                0.125,
                0.0625,
                0.03125,
            ],
            "tangent_coordinate_passes_per_step": 2,
            "tangent_interior_weight": 0.015625,
            "shared_triangle_pair_moves": True,
            "maximum_pair_candidates_per_edge_step": 16,
            "minimum_cone_margin_source": (
                "post_mesh_repair.minimum_cone_margin"
            ),
            "minimum_changed_direction_margin_source": (
                "post_mesh_repair.minimum_smoothing_margin"
            ),
            "quality_threshold_contract_binding_required": True,
            "interaction_components_required": True,
            "final_low_quality_lineage_required": True,
            "final_component_recheck_required": True,
            "coordinate_hash_replay_required": True,
            "aba_replay_required": False,
            "abab_replay_required": True,
        }
    )
    endpoint["endpoint_sha256"] = _canonical_sha256(endpoint)
    return endpoint


def make_owner_free_direction_frontier_pattern_endpoint(
    *, schedule_endpoint_sha256: str,
) -> dict[str, Any]:
    """Create the continuation-only count-first tangent-pattern endpoint."""

    endpoint = make_owner_free_direction_pattern_endpoint(
        schedule_endpoint_sha256=schedule_endpoint_sha256
    )
    source_pattern_endpoint_sha256 = endpoint["endpoint_sha256"]
    endpoint.pop("endpoint_sha256")
    endpoint.update(
        {
            "schema": FRONTIER_PATTERN_ENDPOINT_SCHEMA,
            "mode": FRONTIER_PATTERN_ENDPOINT_MODE,
            "source_pattern_endpoint_schema": PATTERN_ENDPOINT_SCHEMA,
            "source_pattern_endpoint_sha256": source_pattern_endpoint_sha256,
            "maximum_component_count": 3,
            "maximum_candidate_root_count": 16,
            "quality_objective": [
                "nonfinite_count",
                "nonpositive_element_count",
                "core_tetra_below_minimum_gamma_count",
                "maximum_core_tetra_gamma_deficit",
                "core_tetra_gamma_deficit_l2",
                "prism_below_minimum_scaled_jacobian_count",
                "maximum_prism_scaled_jacobian_deficit",
                "prism_scaled_jacobian_deficit_l2",
                "prism_scaled_jacobian_deficit_l1",
                "negative_minimum_prism_scaled_jacobian",
                "negative_minimum_core_tetra_gamma",
                "direction_change_penalty",
            ],
        }
    )
    endpoint["endpoint_sha256"] = _canonical_sha256(endpoint)
    return endpoint


def make_owner_free_direction_frontier_fine_refinement_pattern_endpoint(
    *, schedule_endpoint_sha256: str,
) -> dict[str, Any]:
    """Create the single-component fine-refinement frontier endpoint."""

    endpoint = make_owner_free_direction_frontier_pattern_endpoint(
        schedule_endpoint_sha256=schedule_endpoint_sha256
    )
    source_frontier_pattern_endpoint_sha256 = endpoint["endpoint_sha256"]
    endpoint.pop("endpoint_sha256")
    endpoint.update(
        {
            "schema": FINE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
            "mode": FINE_REFINEMENT_PATTERN_ENDPOINT_MODE,
            "source_frontier_pattern_endpoint_schema": (
                FRONTIER_PATTERN_ENDPOINT_SCHEMA
            ),
            "source_frontier_pattern_endpoint_sha256": (
                source_frontier_pattern_endpoint_sha256
            ),
            "tangent_pattern_steps_radians": [
                2.0**-exponent for exponent in range(1, 13)
            ],
            "maximum_refined_component_count": 1,
            "refined_component_root_count": 3,
            "refined_component_source_triangle_count": 1,
        }
    )
    endpoint["endpoint_sha256"] = _canonical_sha256(endpoint)
    return endpoint


def make_owner_free_direction_frontier_triple_refinement_pattern_endpoint(
    *, schedule_endpoint_sha256: str,
) -> dict[str, Any]:
    """Create the atomic three-root Cartesian fine-refinement endpoint."""

    endpoint = (
        make_owner_free_direction_frontier_fine_refinement_pattern_endpoint(
            schedule_endpoint_sha256=schedule_endpoint_sha256
        )
    )
    source_fine_refinement_pattern_endpoint_sha256 = endpoint[
        "endpoint_sha256"
    ]
    endpoint.pop("endpoint_sha256")
    endpoint.update(
        {
            "schema": TRIPLE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
            "mode": TRIPLE_REFINEMENT_PATTERN_ENDPOINT_MODE,
            "source_fine_refinement_pattern_endpoint_schema": (
                FINE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA
            ),
            "source_fine_refinement_pattern_endpoint_sha256": (
                source_fine_refinement_pattern_endpoint_sha256
            ),
            "static_component_searches_complete_before_fine_refinement": True,
            "single_and_pair_phases_complete_before_triple_refinement": True,
            "triple_refinement_enabled": True,
            "triple_refinement_root_count": 3,
            "tangent_candidates_per_root_per_step": 4,
            "atomic_triple_cartesian_candidates_per_step_pass": 64,
            "triple_coordinate_passes_per_step": 2,
        }
    )
    endpoint["endpoint_sha256"] = _canonical_sha256(endpoint)
    return endpoint


def make_owner_free_direction_frontier_collar_refinement_endpoint(
    *, schedule_endpoint_sha256: str,
) -> dict[str, Any]:
    """Create the seven-candidate outer-tail root-schedule collar endpoint.

    The child preserves the complete triple-refinement lineage but authorizes
    no additional direction or topology change.  It can be consumed only
    after the sole three-root/one-triangle triple search has been exhausted.
    """

    endpoint = (
        make_owner_free_direction_frontier_triple_refinement_pattern_endpoint(
            schedule_endpoint_sha256=schedule_endpoint_sha256
        )
    )
    source_triple_refinement_pattern_endpoint_sha256 = endpoint[
        "endpoint_sha256"
    ]
    endpoint.pop("endpoint_sha256")
    endpoint.update(
        {
            "schema": COLLAR_REFINEMENT_ENDPOINT_SCHEMA,
            "mode": COLLAR_REFINEMENT_ENDPOINT_MODE,
            "source_triple_refinement_pattern_endpoint_schema": (
                TRIPLE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA
            ),
            "source_triple_refinement_pattern_endpoint_sha256": (
                source_triple_refinement_pattern_endpoint_sha256
            ),
            "triple_refinement_exhausted_before_collar": True,
            "required_triple_incomplete_reason": (
                "FIRST_FRONTIER_DIRECTION_SEARCH_EXHAUSTED"
            ),
            "maximum_collar_component_count": 1,
            "collar_component_root_count": 3,
            "collar_component_source_triangle_count": 1,
            "continuous_outer_tail_required": True,
            "minimum_frozen_inner_layer_count": 15,
            "layer_count": 60,
            "preserve_first_layer_height": True,
            "preserve_layer_count": True,
            "preserve_direction_field": True,
            "preserve_prism_topology": True,
            "preserve_connectivity": True,
            "preserve_non_candidate_coordinates": True,
            "layer_reduction_allowed": False,
            "stable_root_graph_required": True,
            "stable_root_graph_wall_restricted": True,
            "stable_root_graph_scope": (
                "target_wall_source_triangle_edge_graph"
            ),
            "stable_root_identity": "stable_root_coordinate_sha256",
            "stable_root_graph_target_roots_distance_zero": True,
            "runtime_mesh_tags_hardcoded": False,
            "collar_ring_widths": [1, 2, 4, 8, 16, 32, 64],
            "maximum_collar_candidates": 7,
            "spatial_weight_formula": "w=max(0,1-d/W)",
            "outer_tail_start_rule": "K=min_bad_layer-1",
            "minimum_growth_cumulative_formula": "A_i=i*h1",
            "frozen_inner_schedule_rule": "C_i=B_i for i<=K",
            "outer_tail_formula_applies_only_when": "i>K",
            "outer_tail_cumulative_formula": (
                "C_i=B_K+(A_i-A_K)+(1-w)*"
                "((B_i-B_K)-(A_i-A_K))"
            ),
            "candidate_rebuilt_from_same_absolute_state": True,
            "global_quality_evaluation_per_candidate": True,
            "schedule_amplification_allowed": False,
            "schedule_bounds_required": "A_i<=C_i<=B_i",
            "strictly_increasing_schedule_required": True,
            "first_layer_float_hex_replay_required": True,
            "stable_graph_hash_required": True,
            "stable_distance_hash_required": True,
            "affected_closure_hash_required": True,
            "frozen_inner_coordinate_hash_required": True,
            "non_target_coordinate_hash_required": True,
            "connectivity_hash_required": True,
            "passing_candidate_objective": [
                "affected_closure_size",
                "coordinate_change_penalty",
            ],
        }
    )
    endpoint["endpoint_sha256"] = _canonical_sha256(endpoint)
    return endpoint


def validate_owner_free_direction_endpoint(
    value: Any,
    *,
    expected_schedule_endpoint_sha256: str,
) -> dict[str, Any]:
    """Validate the exact endpoint schema and return a defensive copy."""

    if not isinstance(value, Mapping):
        raise CoarseDirectionFeasibilityError(
            "owner-free direction endpoint must be a mapping"
        )
    schema = value.get("schema")
    if schema == ENDPOINT_SCHEMA:
        expected = make_owner_free_direction_endpoint(
            schedule_endpoint_sha256=expected_schedule_endpoint_sha256
        )
    elif schema == PATTERN_ENDPOINT_SCHEMA:
        expected = make_owner_free_direction_pattern_endpoint(
            schedule_endpoint_sha256=expected_schedule_endpoint_sha256
        )
    elif schema == FRONTIER_PATTERN_ENDPOINT_SCHEMA:
        expected = make_owner_free_direction_frontier_pattern_endpoint(
            schedule_endpoint_sha256=expected_schedule_endpoint_sha256
        )
    elif schema == FINE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA:
        expected = (
            make_owner_free_direction_frontier_fine_refinement_pattern_endpoint(
                schedule_endpoint_sha256=expected_schedule_endpoint_sha256
            )
        )
    elif schema == TRIPLE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA:
        expected = (
            make_owner_free_direction_frontier_triple_refinement_pattern_endpoint(
                schedule_endpoint_sha256=expected_schedule_endpoint_sha256
            )
        )
    elif schema == COLLAR_REFINEMENT_ENDPOINT_SCHEMA:
        expected = make_owner_free_direction_frontier_collar_refinement_endpoint(
            schedule_endpoint_sha256=expected_schedule_endpoint_sha256
        )
    else:
        raise CoarseDirectionFeasibilityError(
            "owner-free direction endpoint schema is unsupported"
        )
    if set(value) != set(expected):
        raise CoarseDirectionFeasibilityError(
            "owner-free direction endpoint schema is incomplete"
        )
    unsigned = dict(value)
    configured_hash = str(unsigned.pop("endpoint_sha256", "")).casefold()
    if (
        _SHA256.fullmatch(configured_hash) is None
        or _canonical_sha256(unsigned) != configured_hash
        or dict(value) != expected
        or any(
            isinstance(raw, bool)
            or not isinstance(raw, (int, float))
            or not math.isfinite(float(raw))
            or not 0.0 < float(raw) < 1.0
            for raw in value.get("original_direction_interior_weights", [])
        )
    ):
        raise CoarseDirectionFeasibilityError(
            "owner-free direction endpoint is stale or unsafe"
        )
    if schema in {
        PATTERN_ENDPOINT_SCHEMA,
        FRONTIER_PATTERN_ENDPOINT_SCHEMA,
        FINE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
        TRIPLE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
        COLLAR_REFINEMENT_ENDPOINT_SCHEMA,
    } and (
        value.get("shared_triangle_pair_moves") is not True
        or value.get("tangent_pattern_steps_radians")
        != sorted(value.get("tangent_pattern_steps_radians", []), reverse=True)
        or any(
            isinstance(raw, bool)
            or not isinstance(raw, (int, float))
            or not math.isfinite(float(raw))
            or not 0.0 < float(raw) <= 0.5
            for raw in value.get("tangent_pattern_steps_radians", [])
        )
    ):
        raise CoarseDirectionFeasibilityError(
            "owner-free tangent-pattern endpoint is stale or unsafe"
        )
    if schema in {
        FINE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
        TRIPLE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
        COLLAR_REFINEMENT_ENDPOINT_SCHEMA,
    } and any(
        type(value.get(field)) is not int
        for field in (
            "maximum_refined_component_count",
            "refined_component_root_count",
            "refined_component_source_triangle_count",
        )
    ):
        raise CoarseDirectionFeasibilityError(
            "owner-free fine-refinement endpoint is stale or unsafe"
        )
    if schema in {
        TRIPLE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
        COLLAR_REFINEMENT_ENDPOINT_SCHEMA,
    } and (
        value.get("source_fine_refinement_pattern_endpoint_schema")
        != FINE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA
        or value.get("static_component_searches_complete_before_fine_refinement")
        is not True
        or value.get("single_and_pair_phases_complete_before_triple_refinement")
        is not True
        or value.get("triple_refinement_enabled") is not True
        or type(value.get("triple_refinement_root_count")) is not int
        or value.get("triple_refinement_root_count") != 3
        or type(value.get("tangent_candidates_per_root_per_step")) is not int
        or value.get("tangent_candidates_per_root_per_step") != 4
        or type(
            value.get("atomic_triple_cartesian_candidates_per_step_pass")
        )
        is not int
        or value.get("atomic_triple_cartesian_candidates_per_step_pass")
        != value.get("tangent_candidates_per_root_per_step")
        ** value.get("triple_refinement_root_count")
        or type(value.get("triple_coordinate_passes_per_step")) is not int
        or value.get("triple_coordinate_passes_per_step") != 2
        or type(value.get("maximum_refined_component_count")) is not int
        or value.get("maximum_refined_component_count") != 1
        or type(value.get("refined_component_root_count")) is not int
        or value.get("refined_component_root_count")
        != value.get("triple_refinement_root_count")
        or type(value.get("refined_component_source_triangle_count")) is not int
        or value.get("refined_component_source_triangle_count") != 1
        or value.get("tangent_pattern_steps_radians")
        != [2.0**-exponent for exponent in range(1, 13)]
    ):
        raise CoarseDirectionFeasibilityError(
            "owner-free triple-refinement endpoint is stale or unsafe"
        )
    if schema == COLLAR_REFINEMENT_ENDPOINT_SCHEMA and (
        value.get("source_triple_refinement_pattern_endpoint_schema")
        != TRIPLE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA
        or value.get("triple_refinement_exhausted_before_collar") is not True
        or value.get("required_triple_incomplete_reason")
        != "FIRST_FRONTIER_DIRECTION_SEARCH_EXHAUSTED"
        or type(value.get("maximum_collar_component_count")) is not int
        or value.get("maximum_collar_component_count") != 1
        or type(value.get("collar_component_root_count")) is not int
        or value.get("collar_component_root_count") != 3
        or type(value.get("collar_component_source_triangle_count")) is not int
        or value.get("collar_component_source_triangle_count") != 1
        or value.get("continuous_outer_tail_required") is not True
        or type(value.get("minimum_frozen_inner_layer_count")) is not int
        or value.get("minimum_frozen_inner_layer_count") < 15
        or type(value.get("layer_count")) is not int
        or value.get("layer_count") != 60
        or value.get("minimum_frozen_inner_layer_count")
        >= value.get("layer_count")
        or any(
            value.get(field) is not True
            for field in (
                "preserve_first_layer_height",
                "preserve_layer_count",
                "preserve_direction_field",
                "preserve_prism_topology",
                "preserve_connectivity",
                "preserve_non_candidate_coordinates",
                "stable_root_graph_required",
                "stable_root_graph_wall_restricted",
                "stable_root_graph_target_roots_distance_zero",
                "candidate_rebuilt_from_same_absolute_state",
                "global_quality_evaluation_per_candidate",
                "strictly_increasing_schedule_required",
                "first_layer_float_hex_replay_required",
                "stable_graph_hash_required",
                "stable_distance_hash_required",
                "affected_closure_hash_required",
                "frozen_inner_coordinate_hash_required",
                "non_target_coordinate_hash_required",
                "connectivity_hash_required",
            )
        )
        or value.get("layer_reduction_allowed") is not False
        or value.get("runtime_mesh_tags_hardcoded") is not False
        or value.get("schedule_amplification_allowed") is not False
        or value.get("stable_root_graph_scope")
        != "target_wall_source_triangle_edge_graph"
        or value.get("stable_root_identity") != "stable_root_coordinate_sha256"
        or not isinstance(value.get("collar_ring_widths"), list)
        or any(type(width) is not int for width in value["collar_ring_widths"])
        or value.get("collar_ring_widths") != [1, 2, 4, 8, 16, 32, 64]
        or type(value.get("maximum_collar_candidates")) is not int
        or value.get("maximum_collar_candidates")
        != len(value.get("collar_ring_widths", []))
        or value.get("spatial_weight_formula") != "w=max(0,1-d/W)"
        or value.get("outer_tail_start_rule") != "K=min_bad_layer-1"
        or value.get("minimum_growth_cumulative_formula") != "A_i=i*h1"
        or value.get("frozen_inner_schedule_rule") != "C_i=B_i for i<=K"
        or value.get("outer_tail_formula_applies_only_when") != "i>K"
        or value.get("outer_tail_cumulative_formula")
        != (
            "C_i=B_K+(A_i-A_K)+(1-w)*"
            "((B_i-B_K)-(A_i-A_K))"
        )
        or value.get("schedule_bounds_required") != "A_i<=C_i<=B_i"
        or value.get("passing_candidate_objective")
        != ["affected_closure_size", "coordinate_change_penalty"]
    ):
        raise CoarseDirectionFeasibilityError(
            "owner-free collar-refinement endpoint is stale or unsafe"
        )
    return copy.deepcopy(dict(value))


__all__ = [
    "CoarseDirectionFeasibilityError",
    "COLLAR_REFINEMENT_ENDPOINT_MODE",
    "COLLAR_REFINEMENT_ENDPOINT_SCHEMA",
    "ENDPOINT_MODE",
    "ENDPOINT_SCHEMA",
    "FINE_REFINEMENT_PATTERN_ENDPOINT_MODE",
    "FINE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA",
    "FRONTIER_PATTERN_ENDPOINT_MODE",
    "FRONTIER_PATTERN_ENDPOINT_SCHEMA",
    "PATTERN_ENDPOINT_MODE",
    "PATTERN_ENDPOINT_SCHEMA",
    "TRIPLE_REFINEMENT_PATTERN_ENDPOINT_MODE",
    "TRIPLE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA",
    "make_owner_free_direction_endpoint",
    "make_owner_free_direction_frontier_collar_refinement_endpoint",
    "make_owner_free_direction_frontier_fine_refinement_pattern_endpoint",
    "make_owner_free_direction_frontier_pattern_endpoint",
    "make_owner_free_direction_frontier_triple_refinement_pattern_endpoint",
    "make_owner_free_direction_pattern_endpoint",
    "validate_owner_free_direction_endpoint",
]
