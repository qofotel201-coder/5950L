"""Fail-closed contract for the first physical-schedule direction frontier.

The contract authorizes an in-memory, audit-only direction search at the
first sampled homotopy failure.  It never authorizes a mesh write, solver
execution, post-processing, calibration PASS, or production use.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Any, Mapping

from .coarse_direction_feasibility import (
    COLLAR_REFINEMENT_ENDPOINT_MODE,
    COLLAR_REFINEMENT_ENDPOINT_SCHEMA,
    FINE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
    FRONTIER_PATTERN_ENDPOINT_SCHEMA,
    TRIPLE_REFINEMENT_PATTERN_ENDPOINT_MODE,
    TRIPLE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
    make_owner_free_direction_frontier_collar_refinement_endpoint,
    make_owner_free_direction_frontier_fine_refinement_pattern_endpoint,
    make_owner_free_direction_frontier_pattern_endpoint,
    make_owner_free_direction_frontier_triple_refinement_pattern_endpoint,
)


REQUEST = "physical-schedule-first-frontier-direction-audit"
ENDPOINT_SCHEMA = "cfdpipe.coarse_schedule_frontier_direction_endpoint.v5"
DISCOVERY_SCHEMA = "cfdpipe.coarse_schedule_frontier_direction_continuation.v5"

_MODE = "previous_pass_to_first_fail_fixed_schedule_direction_continuation_v5"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PREVIOUS_FRACTION = 0.0
_TARGET_FRACTION = 2.0**-12
_MINIMUM_PRISM_SCALED_JACOBIAN_FOR_PASS = 0.01
_MINIMUM_CORE_TETRA_GAMMA_FOR_PASS = 0.001
_CAPS = {
    "maximum_candidate_root_count": 16,
    "maximum_component_count": 3,
    "maximum_quality_evaluations": 4096,
    "maximum_pair_candidates_per_edge_step": 16,
}
_OBSERVED_REAL_SCOPE = {
    "component_count": 3,
    "candidate_root_count": 12,
    "source_triangle_count": 5,
}
_FINE_REFINEMENT_STEPS_RADIANS = tuple(
    2.0**-exponent for exponent in range(1, 13)
)
_FINE_REFINEMENT_PREFIX_STEPS_RADIANS = (
    _FINE_REFINEMENT_STEPS_RADIANS[:5]
)
_FINE_REFINEMENT_TAIL_STEPS_RADIANS = (
    _FINE_REFINEMENT_STEPS_RADIANS[5:]
)
_COLLAR_RING_WIDTHS = (1, 2, 4, 8, 16, 32, 64)
# coarse_direction_feasibility intentionally leaves the objective embedded in
# the versioned child factory.  Keep an exact local tuple so the parent can
# freeze and validate the child semantics without accepting a generic pattern
# endpoint that happens to have the same evaluation cap.
_FRONTIER_PATTERN_QUALITY_OBJECTIVE = (
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
)
_DIRECTION_SEARCH_CONTRACT = {
    "endpoint_schema": TRIPLE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
    "endpoint_mode": TRIPLE_REFINEMENT_PATTERN_ENDPOINT_MODE,
    "quality_objective": list(_FRONTIER_PATTERN_QUALITY_OBJECTIVE),
    "tangent_pattern_steps_radians": list(
        _FINE_REFINEMENT_STEPS_RADIANS
    ),
    "maximum_refined_component_count": 1,
    "refined_component_root_count": 3,
    "refined_component_source_triangle_count": 1,
    "static_all_components_complete_before_fine": True,
    "single_and_pair_phases_complete_before_triple": True,
    "triple_refinement_enabled": True,
    "triple_refinement_root_count": 3,
    "tangent_candidates_per_root_per_step": 4,
    "atomic_triple_cartesian_candidates_per_step_pass": 64,
    "triple_coordinate_passes_per_step": 2,
}


def _collar_refinement_contract() -> dict[str, Any]:
    """Extract every hash-independent semantic field from the collar child."""

    child = make_owner_free_direction_frontier_collar_refinement_endpoint(
        schedule_endpoint_sha256="0" * 64
    )
    return {
        key: copy.deepcopy(value)
        for key, value in child.items()
        if not key.endswith("_sha256")
    }


_COLLAR_REFINEMENT_CONTRACT = _collar_refinement_contract()
_QUALITY_EVALUATION_BUDGET_ALGORITHM_KNOBS = {
    "static_component_baseline_evaluation_count": 2,
    "component_joint_target_count": 3,
    "maximum_fixed_target_count_per_root": 6,
    "original_direction_interior_weights": [0.015625, 0.0625, 0.25, 0.5],
    "coordinate_descent_passes": 2,
    "source_triangle_vertex_count": 3,
    "fine_tangent_pattern_steps_radians": list(
        _FINE_REFINEMENT_STEPS_RADIANS
    ),
    "fine_tangent_coordinate_passes_per_step": 2,
    "fine_tangent_candidate_count_per_root_step": 4,
    "maximum_pair_candidates_per_edge_step": 16,
    "maximum_unique_edge_count_per_source_triangle": 3,
    "maximum_refined_component_count": 1,
    "refined_component_root_count": 3,
    "refined_component_source_triangle_count": 1,
    "triple_tangent_pattern_steps_radians": list(
        _FINE_REFINEMENT_STEPS_RADIANS
    ),
    "triple_tangent_coordinate_passes_per_step": 2,
    "triple_tangent_candidate_count_per_root_step": 4,
    "triple_atomic_cartesian_candidates_per_step_pass": 64,
    "triple_refined_component_count": 1,
    "triple_refined_component_root_count": 3,
    "collar_ring_widths": list(_COLLAR_RING_WIDTHS),
    "collar_quality_evaluation_count_per_candidate": 1,
    "maximum_collar_candidates": len(_COLLAR_RING_WIDTHS),
    "fixed_replay_and_global_verification_reserve": 6,
    "final_component_recheck_count_per_component": 1,
}
_QUALITY_EVALUATION_BUDGET_PROOF_SCHEMA = (
    "cfdpipe.coarse_schedule_frontier_direction_evaluation_budget_proof.v4"
)
_FINE_REFINEMENT_SELECTION_SCHEMA = (
    "cfdpipe.coarse_schedule_frontier_fine_refinement_selection.v1"
)
_TRIPLE_REFINEMENT_SELECTION_SCHEMA = (
    "cfdpipe.coarse_schedule_frontier_triple_refinement_selection.v1"
)
_COLLAR_REFINEMENT_SELECTION_SCHEMA = (
    "cfdpipe.coarse_schedule_frontier_collar_refinement_selection.v1"
)
_TARGET_OBSERVATION_FIELDS = {
    "low_quality_prism_count",
    "unique_source_triangle_count",
    "wall_count",
    "core_tetra_below_gamma_count",
    "nonpositive_element_count",
}
_HOMOTOPY_DISCOVERY_SCHEMA = (
    "cfdpipe.coarse_physical_schedule_fixed_direction_homotopy_discovery.v1"
)
_QUALITY_FIELDS = {
    "status",
    "minimum_prism_scaled_jacobian_for_pass",
    "minimum_core_tetra_gamma_for_pass",
    "all_values_finite",
    "nonfinite_count",
    "nonpositive_element_count",
    "prism_below_threshold_element_count",
    "core_tetra_below_gamma_count",
    "minimum_prism_scaled_jacobian",
    "minimum_core_tetra_gamma",
    "prism_element_count",
    "core_element_count",
    "core_tetra_count",
    "maximum_prism_scaled_jacobian_deficit",
    "prism_scaled_jacobian_deficit_l1",
    "prism_scaled_jacobian_deficit_l2",
    "maximum_core_tetra_gamma_deficit",
    "core_tetra_gamma_deficit_l2",
    "quality_sha256",
}


class CoarseScheduleContinuationError(RuntimeError):
    """The first-frontier direction endpoint is malformed or stale."""


def _canonical_sha256(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise CoarseScheduleContinuationError(
            "frontier direction endpoint is not canonically serializable"
        ) from error
    return hashlib.sha256(payload).hexdigest()


def _sha256(value: Any, label: str) -> str:
    result = str(value).casefold()
    if _SHA256.fullmatch(result) is None:
        raise CoarseScheduleContinuationError(f"{label} must be a SHA-256 digest")
    return result


def _positive_finite(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise CoarseScheduleContinuationError(f"{label} must be positive and finite")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise CoarseScheduleContinuationError(
            f"{label} must be positive and finite"
        ) from error
    if not math.isfinite(result) or result <= 0.0:
        raise CoarseScheduleContinuationError(f"{label} must be positive and finite")
    return result


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CoarseScheduleContinuationError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CoarseScheduleContinuationError(
            f"{label} must be a non-negative integer"
        )
    return value


def _target_observation(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != _TARGET_OBSERVATION_FIELDS:
        raise CoarseScheduleContinuationError(
            "target observation has an incomplete field set"
        )
    result = {
        field: _nonnegative_int(value[field], f"target observation {field}")
        for field in sorted(_TARGET_OBSERVATION_FIELDS)
    }
    low_quality_count = result["low_quality_prism_count"]
    triangle_count = result["unique_source_triangle_count"]
    wall_count = result["wall_count"]
    if (
        low_quality_count <= 0
        or triangle_count <= 0
        or wall_count <= 0
        or triangle_count > low_quality_count
        or wall_count > triangle_count
        or result["core_tetra_below_gamma_count"] != 0
        or result["nonpositive_element_count"] != 0
    ):
        raise CoarseScheduleContinuationError(
            "target observation does not describe the frozen prism-only first frontier"
        )
    return result


def _quality_evaluation_bound_components() -> dict[str, int]:
    """Derive the v5 four-phase bound from the exact frozen loop knobs."""

    knobs = _QUALITY_EVALUATION_BUDGET_ALGORITHM_KNOBS
    component_count = _OBSERVED_REAL_SCOPE["component_count"]
    candidate_root_count = _OBSERVED_REAL_SCOPE["candidate_root_count"]
    source_triangle_count = _OBSERVED_REAL_SCOPE["source_triangle_count"]
    weight_count = len(knobs["original_direction_interior_weights"])

    static_component_evaluations = (
        knobs["static_component_baseline_evaluation_count"]
        + knobs["component_joint_target_count"] * weight_count
    )
    static_root_evaluations = (
        knobs["maximum_fixed_target_count_per_root"]
        * weight_count
        * knobs["coordinate_descent_passes"]
    )
    static_triangle_evaluations = (
        knobs["source_triangle_vertex_count"]
        * weight_count
        * knobs["coordinate_descent_passes"]
    )
    static_search = (
        static_component_evaluations * component_count
        + static_root_evaluations * candidate_root_count
        + static_triangle_evaluations * source_triangle_count
    )

    refined_edge_count = (
        knobs["refined_component_source_triangle_count"]
        * knobs["maximum_unique_edge_count_per_source_triangle"]
    )
    fine_candidates_per_pass = (
        knobs["fine_tangent_candidate_count_per_root_step"]
        * knobs["refined_component_root_count"]
        + knobs["maximum_pair_candidates_per_edge_step"]
        * refined_edge_count
    )
    fine_refinement = (
        len(knobs["fine_tangent_pattern_steps_radians"])
        * knobs["fine_tangent_coordinate_passes_per_step"]
        * fine_candidates_per_pass
        * knobs["maximum_refined_component_count"]
    )
    derived_atomic_triple_candidates = (
        knobs["triple_tangent_candidate_count_per_root_step"]
        ** knobs["triple_refined_component_root_count"]
    )
    if (
        derived_atomic_triple_candidates
        != knobs["triple_atomic_cartesian_candidates_per_step_pass"]
    ):
        raise CoarseScheduleContinuationError(
            "triple Cartesian candidate cap is not derived from the root scope"
        )
    triple_refinement = (
        len(knobs["triple_tangent_pattern_steps_radians"])
        * knobs["triple_tangent_coordinate_passes_per_step"]
        * derived_atomic_triple_candidates
        * knobs["triple_refined_component_count"]
    )
    collar_refinement = (
        len(knobs["collar_ring_widths"])
        * knobs["collar_quality_evaluation_count_per_candidate"]
    )
    if (
        tuple(knobs["collar_ring_widths"]) != _COLLAR_RING_WIDTHS
        or len(knobs["collar_ring_widths"])
        != knobs["maximum_collar_candidates"]
    ):
        raise CoarseScheduleContinuationError(
            "collar ring schedule differs from the frozen candidate scope"
        )
    replay_and_recheck = (
        knobs["fixed_replay_and_global_verification_reserve"]
        + knobs["final_component_recheck_count_per_component"]
        * component_count
    )
    return {
        "static_search_quality_evaluation_upper_bound": static_search,
        "fine_refinement_quality_evaluation_upper_bound": fine_refinement,
        "triple_refinement_quality_evaluation_upper_bound": (
            triple_refinement
        ),
        "collar_refinement_quality_evaluation_upper_bound": (
            collar_refinement
        ),
        "replay_and_recheck_quality_evaluation_upper_bound": (
            replay_and_recheck
        ),
        "quality_evaluation_upper_bound": (
            static_search
            + fine_refinement
            + triple_refinement
            + collar_refinement
            + replay_and_recheck
        ),
    }


def _quality_evaluation_budget_proof() -> dict[str, Any]:
    """Freeze and self-hash the loop-derived v5 collar budget proof."""

    actual_scope = copy.deepcopy(_OBSERVED_REAL_SCOPE)
    actual_scope["refined_component_count"] = (
        _QUALITY_EVALUATION_BUDGET_ALGORITHM_KNOBS[
            "maximum_refined_component_count"
        ]
    )
    actual_scope["refined_component_root_count"] = (
        _QUALITY_EVALUATION_BUDGET_ALGORITHM_KNOBS[
            "refined_component_root_count"
        ]
    )
    actual_scope["refined_component_source_triangle_count"] = (
        _QUALITY_EVALUATION_BUDGET_ALGORITHM_KNOBS[
            "refined_component_source_triangle_count"
        ]
    )
    actual_scope["refined_component_unique_edge_count"] = (
        actual_scope["refined_component_source_triangle_count"]
        * _QUALITY_EVALUATION_BUDGET_ALGORITHM_KNOBS[
            "maximum_unique_edge_count_per_source_triangle"
        ]
    )
    actual_scope["triple_refined_component_count"] = (
        _QUALITY_EVALUATION_BUDGET_ALGORITHM_KNOBS[
            "triple_refined_component_count"
        ]
    )
    actual_scope["triple_refined_component_root_count"] = (
        _QUALITY_EVALUATION_BUDGET_ALGORITHM_KNOBS[
            "triple_refined_component_root_count"
        ]
    )
    actual_scope["collar_candidate_count"] = len(_COLLAR_RING_WIDTHS)
    actual_scope.update(_quality_evaluation_bound_components())
    maximum_evaluations = _CAPS["maximum_quality_evaluations"]
    headroom = maximum_evaluations - actual_scope[
        "quality_evaluation_upper_bound"
    ]
    if (
        actual_scope["static_search_quality_evaluation_upper_bound"] != 738
        or actual_scope[
            "fine_refinement_quality_evaluation_upper_bound"
        ]
        != 1440
        or actual_scope[
            "triple_refinement_quality_evaluation_upper_bound"
        ]
        != 1536
        or actual_scope[
            "collar_refinement_quality_evaluation_upper_bound"
        ]
        != 7
        or actual_scope[
            "replay_and_recheck_quality_evaluation_upper_bound"
        ]
        != 9
        or actual_scope["quality_evaluation_upper_bound"] != 3730
        or headroom != 366
        or headroom <= 0
    ):
        raise CoarseScheduleContinuationError(
            "frontier direction evaluation budget proof is stale or over budget"
        )
    proof: dict[str, Any] = {
        "schema": _QUALITY_EVALUATION_BUDGET_PROOF_SCHEMA,
        "status": "PASS",
        "bound_formula": "B=STATIC+FINE+TRIPLE+COLLAR+TAIL",
        "static_search_bound_formula": (
            "STATIC=(2+3*4)*C+(6*4*2)*R+(3*4*2)*T"
        ),
        "fine_refinement_bound_formula": (
            "FINE=12*2*(4*3+16*3)*1"
        ),
        "triple_refinement_bound_formula": "TRIPLE=12*2*(4^3)*1",
        "collar_refinement_bound_formula": "COLLAR=7*1",
        "replay_and_recheck_bound_formula": "TAIL=6+1*C",
        "symbols": {
            "C": "component_count",
            "R": "candidate_root_count",
            "T": "source_triangle_count",
        },
        "algorithm_knobs": copy.deepcopy(
            _QUALITY_EVALUATION_BUDGET_ALGORITHM_KNOBS
        ),
        "actual_scope": actual_scope,
        "maximum_quality_evaluations": maximum_evaluations,
        "headroom": headroom,
        "within_cap": True,
    }
    proof["proof_sha256"] = _canonical_sha256(proof)
    return proof


def make_frontier_direction_endpoint(
    *,
    homotopy_manifest_sha256: str,
    homotopy_discovery_sha256: str,
    homotopy_endpoint_sha256: str,
    direction_replay_approval_sha256: str,
    local_schedule_binding_sha256: str,
    previous_quality_sha256: str,
    target_quality_sha256: str,
    target_low_quality_aggregate_sha256: str,
    first_layer_height_m: float,
    layer_count: int,
    target_observation: Mapping[str, Any],
) -> dict[str, Any]:
    """Create the self-hashed, audit-only first-frontier endpoint.

    The target observation is supplied by a previously validated homotopy
    discovery.  Version 5 admits only the observed five-triangle frontier,
    freezes its measured component/root scope, and permits atomic Cartesian
    three-root refinement followed by a seven-ring outer-tail schedule collar
    only for the sole residual 3-root/1-triangle component.
    """

    source_bindings = {
        "direction_replay_approval_sha256": _sha256(
            direction_replay_approval_sha256,
            "direction replay approval SHA-256",
        ),
        "homotopy_discovery_sha256": _sha256(
            homotopy_discovery_sha256, "homotopy discovery SHA-256"
        ),
        "homotopy_endpoint_sha256": _sha256(
            homotopy_endpoint_sha256, "homotopy endpoint SHA-256"
        ),
        "homotopy_manifest_sha256": _sha256(
            homotopy_manifest_sha256, "homotopy manifest SHA-256"
        ),
        "local_schedule_binding_sha256": _sha256(
            local_schedule_binding_sha256, "local schedule binding SHA-256"
        ),
    }
    previous_quality = _sha256(
        previous_quality_sha256, "previous quality SHA-256"
    )
    target_quality = _sha256(target_quality_sha256, "target quality SHA-256")
    target_aggregate = _sha256(
        target_low_quality_aggregate_sha256,
        "target low-quality aggregate SHA-256",
    )
    first_layer_height = _positive_finite(
        first_layer_height_m, "first layer height"
    )
    layers = _positive_int(layer_count, "layer count")
    observation = _target_observation(target_observation)
    if (
        observation["unique_source_triangle_count"]
        != _OBSERVED_REAL_SCOPE["source_triangle_count"]
    ):
        raise CoarseScheduleContinuationError(
            "target observation differs from the frozen real frontier scope"
        )

    endpoint: dict[str, Any] = {
        "schema": ENDPOINT_SCHEMA,
        "status": "AUDIT_ONLY",
        "request": REQUEST,
        "mode": _MODE,
        "source_bindings": source_bindings,
        "previous_fraction_float_hex": _PREVIOUS_FRACTION.hex(),
        "target_fraction_float_hex": _TARGET_FRACTION.hex(),
        "previous_quality_sha256": previous_quality,
        "target_quality_sha256": target_quality,
        "target_low_quality_aggregate_sha256": target_aggregate,
        "target_observation": observation,
        "quality_thresholds": {
            "minimum_core_tetra_gamma_for_pass": (
                _MINIMUM_CORE_TETRA_GAMMA_FOR_PASS
            ),
            "minimum_prism_scaled_jacobian_for_pass": (
                _MINIMUM_PRISM_SCALED_JACOBIAN_FOR_PASS
            ),
        },
        "caps": copy.deepcopy(_CAPS),
        "observed_real_scope": copy.deepcopy(_OBSERVED_REAL_SCOPE),
        "direction_search_contract": copy.deepcopy(_DIRECTION_SEARCH_CONTRACT),
        "collar_refinement_contract": copy.deepcopy(
            _COLLAR_REFINEMENT_CONTRACT
        ),
        "quality_evaluation_budget_proof": (
            _quality_evaluation_budget_proof()
        ),
        "first_layer_height_m": first_layer_height,
        "layer_count": layers,
        "preserve_first_layer_height": True,
        "preserve_layer_count": True,
        "absolute_coordinate_replay_required": True,
        "abab_replay_required": True,
        "target_frontier_only": True,
        "direction_search_performed": False,
        "audit_only": True,
        "calibration_PASS_authorized": False,
        "production_mesh_eligible": False,
        "mesh_write_authorized": False,
        "mesh_written": False,
        "su2_called": False,
        "paraview_called": False,
    }
    endpoint["endpoint_sha256"] = _canonical_sha256(endpoint)
    return endpoint


def validate_frontier_direction_endpoint(
    value: Any,
    *,
    homotopy_manifest_sha256: str,
    homotopy_discovery_sha256: str,
    homotopy_endpoint_sha256: str,
    direction_replay_approval_sha256: str,
    local_schedule_binding_sha256: str,
    previous_quality_sha256: str,
    target_quality_sha256: str,
    target_low_quality_aggregate_sha256: str,
    first_layer_height_m: float,
    layer_count: int,
    target_observation: Mapping[str, Any],
) -> dict[str, Any]:
    """Recreate the expected endpoint and require byte-semantic equality."""

    if not isinstance(value, Mapping):
        raise CoarseScheduleContinuationError(
            "frontier direction endpoint must be an object"
        )
    expected = make_frontier_direction_endpoint(
        homotopy_manifest_sha256=homotopy_manifest_sha256,
        homotopy_discovery_sha256=homotopy_discovery_sha256,
        homotopy_endpoint_sha256=homotopy_endpoint_sha256,
        direction_replay_approval_sha256=direction_replay_approval_sha256,
        local_schedule_binding_sha256=local_schedule_binding_sha256,
        previous_quality_sha256=previous_quality_sha256,
        target_quality_sha256=target_quality_sha256,
        target_low_quality_aggregate_sha256=(
            target_low_quality_aggregate_sha256
        ),
        first_layer_height_m=first_layer_height_m,
        layer_count=layer_count,
        target_observation=target_observation,
    )
    if set(value) != set(expected) or dict(value) != expected:
        raise CoarseScheduleContinuationError(
            "frontier direction endpoint is stale or unsafe"
        )
    return copy.deepcopy(expected)


def _homotopy_frontier_inputs(
    homotopy_discovery: Any,
    *,
    homotopy_endpoint_sha256: str,
    direction_replay_approval_sha256: str,
    local_schedule_binding_sha256: str,
    minimum_prism_scaled_jacobian_for_pass: float,
    minimum_core_tetra_gamma_for_pass: float,
) -> dict[str, Any]:
    """Extract the two frozen frontier states from validated homotopy evidence."""

    if not isinstance(homotopy_discovery, Mapping):
        raise CoarseScheduleContinuationError(
            "homotopy discovery must be an object"
        )
    unsigned = dict(homotopy_discovery)
    configured_hash = _sha256(
        unsigned.pop("discovery_sha256", ""), "homotopy discovery SHA-256"
    )
    if (
        homotopy_discovery.get("schema") != _HOMOTOPY_DISCOVERY_SCHEMA
        or _canonical_sha256(unsigned) != configured_hash
    ):
        raise CoarseScheduleContinuationError(
            "homotopy discovery schema or self-hash is stale"
        )

    expected_endpoint = _sha256(
        homotopy_endpoint_sha256, "homotopy endpoint SHA-256"
    )
    expected_approval = _sha256(
        direction_replay_approval_sha256, "direction replay approval SHA-256"
    )
    expected_binding = _sha256(
        local_schedule_binding_sha256, "local schedule binding SHA-256"
    )
    bindings = homotopy_discovery.get("source_bindings")
    if (
        not isinstance(bindings, Mapping)
        or bindings.get("homotopy_endpoint_sha256") != expected_endpoint
        or bindings.get("direction_replay_approval_sha256") != expected_approval
        or bindings.get("local_schedule_binding_sha256") != expected_binding
        or homotopy_discovery.get("maximum_tested_pass_fraction_float_hex")
        != _PREVIOUS_FRACTION.hex()
        or homotopy_discovery.get("first_tested_fail_fraction_float_hex")
        != _TARGET_FRACTION.hex()
    ):
        raise CoarseScheduleContinuationError(
            "homotopy frontier lineage differs from the frozen first frontier"
        )

    prism_threshold = _positive_finite(
        minimum_prism_scaled_jacobian_for_pass,
        "minimum prism scaled Jacobian for PASS",
    )
    core_threshold = _positive_finite(
        minimum_core_tetra_gamma_for_pass,
        "minimum core tetra gamma for PASS",
    )
    if (
        prism_threshold.hex()
        != _MINIMUM_PRISM_SCALED_JACOBIAN_FOR_PASS.hex()
        or core_threshold.hex() != _MINIMUM_CORE_TETRA_GAMMA_FOR_PASS.hex()
    ):
        raise CoarseScheduleContinuationError(
            "frontier quality thresholds are not the frozen project thresholds"
        )

    scan = homotopy_discovery.get("ascending_scan")
    if not isinstance(scan, list):
        raise CoarseScheduleContinuationError(
            "homotopy ascending scan is missing"
        )
    states: dict[str, Mapping[str, Any]] = {}
    for record in scan:
        if not isinstance(record, Mapping):
            raise CoarseScheduleContinuationError(
                "homotopy ascending scan contains a non-object record"
            )
        fraction = record.get("fraction_float_hex")
        if fraction in {_PREVIOUS_FRACTION.hex(), _TARGET_FRACTION.hex()}:
            if fraction in states:
                raise CoarseScheduleContinuationError(
                    "homotopy first-frontier state is duplicated"
                )
            states[str(fraction)] = record
    if set(states) != {_PREVIOUS_FRACTION.hex(), _TARGET_FRACTION.hex()}:
        raise CoarseScheduleContinuationError(
            "homotopy first-frontier states are incomplete"
        )

    previous = states[_PREVIOUS_FRACTION.hex()]
    target = states[_TARGET_FRACTION.hex()]
    previous_quality = previous.get("quality")
    target_quality = target.get("quality")
    target_aggregate = target.get("low_quality_aggregate")
    if not all(
        isinstance(item, Mapping)
        for item in (previous_quality, target_quality, target_aggregate)
    ):
        raise CoarseScheduleContinuationError(
            "homotopy first-frontier quality evidence is incomplete"
        )

    previous_quality_hash = _sha256(
        previous_quality.get("quality_sha256"), "previous quality SHA-256"
    )
    target_quality_hash = _sha256(
        target_quality.get("quality_sha256"), "target quality SHA-256"
    )
    target_aggregate_hash = _sha256(
        target_aggregate.get("aggregate_sha256"),
        "target low-quality aggregate SHA-256",
    )
    if (
        previous_quality.get("status") != "PASS"
        or target_quality.get("status") != "FAIL"
        or float(previous_quality.get("minimum_prism_scaled_jacobian_for_pass"))
        .hex()
        != prism_threshold.hex()
        or float(previous_quality.get("minimum_core_tetra_gamma_for_pass")).hex()
        != core_threshold.hex()
        or float(target_quality.get("minimum_prism_scaled_jacobian_for_pass"))
        .hex()
        != prism_threshold.hex()
        or float(target_quality.get("minimum_core_tetra_gamma_for_pass")).hex()
        != core_threshold.hex()
    ):
        raise CoarseScheduleContinuationError(
            "homotopy first-frontier quality status or thresholds differ"
        )

    observation = _target_observation(
        {
            "low_quality_prism_count": target_quality.get(
                "prism_below_threshold_element_count"
            ),
            "unique_source_triangle_count": target_aggregate.get(
                "unique_source_triangle_count"
            ),
            "wall_count": target_aggregate.get("wall_count"),
            "core_tetra_below_gamma_count": target_quality.get(
                "core_tetra_below_gamma_count"
            ),
            "nonpositive_element_count": target_quality.get(
                "nonpositive_element_count"
            ),
        }
    )
    if (
        _nonnegative_int(target_aggregate.get("count"), "target aggregate count")
        != observation["low_quality_prism_count"]
        or _nonnegative_int(
            previous_quality.get("prism_below_threshold_element_count"),
            "previous low-quality prism count",
        )
        != 0
        or _nonnegative_int(
            previous_quality.get("core_tetra_below_gamma_count"),
            "previous low-quality core tetra count",
        )
        != 0
        or _nonnegative_int(
            previous_quality.get("nonpositive_element_count"),
            "previous nonpositive element count",
        )
        != 0
    ):
        raise CoarseScheduleContinuationError(
            "homotopy first-frontier counts are inconsistent"
        )
    return {
        "homotopy_discovery_sha256": configured_hash,
        "previous_quality_sha256": previous_quality_hash,
        "target_quality_sha256": target_quality_hash,
        "target_low_quality_aggregate_sha256": target_aggregate_hash,
        "target_observation": observation,
    }


def make_schedule_frontier_direction_endpoint(
    *,
    homotopy_manifest_sha256: str,
    homotopy_discovery: Mapping[str, Any],
    homotopy_endpoint_sha256: str,
    direction_replay_approval_sha256: str,
    local_schedule_binding_sha256: str,
    minimum_prism_scaled_jacobian_for_pass: float,
    minimum_core_tetra_gamma_for_pass: float,
    layer_count: int,
    first_layer_height_m: float,
) -> dict[str, Any]:
    """Compatibility factory deriving the eleven inputs from homotopy evidence."""

    inputs = _homotopy_frontier_inputs(
        homotopy_discovery,
        homotopy_endpoint_sha256=homotopy_endpoint_sha256,
        direction_replay_approval_sha256=direction_replay_approval_sha256,
        local_schedule_binding_sha256=local_schedule_binding_sha256,
        minimum_prism_scaled_jacobian_for_pass=(
            minimum_prism_scaled_jacobian_for_pass
        ),
        minimum_core_tetra_gamma_for_pass=minimum_core_tetra_gamma_for_pass,
    )
    return make_frontier_direction_endpoint(
        homotopy_manifest_sha256=homotopy_manifest_sha256,
        homotopy_discovery_sha256=inputs["homotopy_discovery_sha256"],
        homotopy_endpoint_sha256=homotopy_endpoint_sha256,
        direction_replay_approval_sha256=direction_replay_approval_sha256,
        local_schedule_binding_sha256=local_schedule_binding_sha256,
        previous_quality_sha256=inputs["previous_quality_sha256"],
        target_quality_sha256=inputs["target_quality_sha256"],
        target_low_quality_aggregate_sha256=(
            inputs["target_low_quality_aggregate_sha256"]
        ),
        first_layer_height_m=first_layer_height_m,
        layer_count=layer_count,
        target_observation=inputs["target_observation"],
    )


def validate_schedule_frontier_direction_endpoint(
    value: Any,
    **arguments: Any,
) -> dict[str, Any]:
    """Strictly recreate the endpoint from validated homotopy evidence."""

    expected = make_schedule_frontier_direction_endpoint(**arguments)
    if not isinstance(value, Mapping) or set(value) != set(expected) or dict(
        value
    ) != expected:
        raise CoarseScheduleContinuationError(
            "frontier direction endpoint is stale or unsafe"
        )
    return copy.deepcopy(expected)


def _validate_endpoint_integrity(value: Any) -> dict[str, Any]:
    """Validate a self-contained endpoint before using it as an expectation."""

    if not isinstance(value, Mapping):
        raise CoarseScheduleContinuationError(
            "expected frontier direction endpoint must be an object"
        )
    bindings = value.get("source_bindings")
    observation = value.get("target_observation")
    if not isinstance(bindings, Mapping) or not isinstance(observation, Mapping):
        raise CoarseScheduleContinuationError(
            "expected frontier direction endpoint lineage is incomplete"
        )
    try:
        return validate_frontier_direction_endpoint(
            value,
            homotopy_manifest_sha256=bindings["homotopy_manifest_sha256"],
            homotopy_discovery_sha256=bindings["homotopy_discovery_sha256"],
            homotopy_endpoint_sha256=bindings["homotopy_endpoint_sha256"],
            direction_replay_approval_sha256=bindings[
                "direction_replay_approval_sha256"
            ],
            local_schedule_binding_sha256=bindings[
                "local_schedule_binding_sha256"
            ],
            previous_quality_sha256=value["previous_quality_sha256"],
            target_quality_sha256=value["target_quality_sha256"],
            target_low_quality_aggregate_sha256=value[
                "target_low_quality_aggregate_sha256"
            ],
            first_layer_height_m=value["first_layer_height_m"],
            layer_count=value["layer_count"],
            target_observation=observation,
        )
    except KeyError as error:
        raise CoarseScheduleContinuationError(
            "expected frontier direction endpoint lineage is incomplete"
        ) from error


def _expected_frontier_pattern_endpoint(
    endpoint: Mapping[str, Any],
) -> dict[str, Any]:
    """Reconstruct and cross-check the v4 triple-refinement child endpoint."""

    child = (
        make_owner_free_direction_frontier_triple_refinement_pattern_endpoint(
            schedule_endpoint_sha256=str(endpoint["endpoint_sha256"])
        )
    )
    contract = endpoint["direction_search_contract"]
    caps = endpoint["caps"]
    proof = endpoint["quality_evaluation_budget_proof"]
    unsigned_proof = dict(proof)
    proof_sha256 = _sha256(
        unsigned_proof.pop("proof_sha256", ""),
        "frontier quality-evaluation budget proof SHA-256",
    )
    knobs = proof["algorithm_knobs"]
    expected_proof = _quality_evaluation_budget_proof()
    source_frontier_child = make_owner_free_direction_frontier_pattern_endpoint(
        schedule_endpoint_sha256=str(endpoint["endpoint_sha256"])
    )
    source_fine_child = (
        make_owner_free_direction_frontier_fine_refinement_pattern_endpoint(
            schedule_endpoint_sha256=str(endpoint["endpoint_sha256"])
        )
    )
    if (
        proof.get("schema") != _QUALITY_EVALUATION_BUDGET_PROOF_SCHEMA
        or proof.get("status") != "PASS"
        or proof.get("within_cap") is not True
        or _canonical_sha256(unsigned_proof) != proof_sha256
        or proof != expected_proof
        or child.get("schema") != contract["endpoint_schema"]
        or child.get("schema") != TRIPLE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA
        or child.get("mode") != contract["endpoint_mode"]
        or child.get("mode") != TRIPLE_REFINEMENT_PATTERN_ENDPOINT_MODE
        or child.get("quality_objective") != contract["quality_objective"]
        or tuple(child.get("quality_objective", ()))
        != _FRONTIER_PATTERN_QUALITY_OBJECTIVE
        or child.get("tangent_pattern_steps_radians")
        != contract["tangent_pattern_steps_radians"]
        or tuple(child.get("tangent_pattern_steps_radians", ()))
        != _FINE_REFINEMENT_STEPS_RADIANS
        or child.get("maximum_refined_component_count")
        != contract["maximum_refined_component_count"]
        or child.get("refined_component_root_count")
        != contract["refined_component_root_count"]
        or child.get("refined_component_source_triangle_count")
        != contract["refined_component_source_triangle_count"]
        or contract.get("static_all_components_complete_before_fine")
        is not True
        or child.get("source_frontier_pattern_endpoint_schema")
        != FRONTIER_PATTERN_ENDPOINT_SCHEMA
        or child.get("source_frontier_pattern_endpoint_sha256")
        != source_frontier_child["endpoint_sha256"]
        or child.get("source_fine_refinement_pattern_endpoint_schema")
        != FINE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA
        or child.get("source_fine_refinement_pattern_endpoint_sha256")
        != source_fine_child["endpoint_sha256"]
        or child.get(
            "static_component_searches_complete_before_fine_refinement"
        )
        is not True
        or child.get(
            "single_and_pair_phases_complete_before_triple_refinement"
        )
        is not True
        or child.get("triple_refinement_enabled") is not True
        or child.get("triple_refinement_root_count")
        != contract["triple_refinement_root_count"]
        or child.get("tangent_candidates_per_root_per_step")
        != contract["tangent_candidates_per_root_per_step"]
        or child.get("atomic_triple_cartesian_candidates_per_step_pass")
        != contract["atomic_triple_cartesian_candidates_per_step_pass"]
        or child.get("triple_coordinate_passes_per_step")
        != contract["triple_coordinate_passes_per_step"]
        or contract.get("single_and_pair_phases_complete_before_triple")
        is not True
        or contract.get("triple_refinement_enabled") is not True
        or child.get("maximum_component_count")
        != caps["maximum_component_count"]
        or child.get("maximum_candidate_root_count")
        != caps["maximum_candidate_root_count"]
        or child.get("maximum_quality_evaluations")
        != caps["maximum_quality_evaluations"]
        or child.get("maximum_pair_candidates_per_edge_step")
        != caps["maximum_pair_candidates_per_edge_step"]
        or child.get("maximum_quality_evaluations")
        != proof["maximum_quality_evaluations"]
        or child.get("original_direction_interior_weights")
        != knobs["original_direction_interior_weights"]
        or child.get("coordinate_descent_passes")
        != knobs["coordinate_descent_passes"]
        or child.get("tangent_pattern_steps_radians")
        != knobs["fine_tangent_pattern_steps_radians"]
        or child.get("tangent_coordinate_passes_per_step")
        != knobs["fine_tangent_coordinate_passes_per_step"]
        or child.get("maximum_pair_candidates_per_edge_step")
        != knobs["maximum_pair_candidates_per_edge_step"]
        or child.get("maximum_refined_component_count")
        != knobs["maximum_refined_component_count"]
        or child.get("refined_component_root_count")
        != knobs["refined_component_root_count"]
        or child.get("refined_component_source_triangle_count")
        != knobs["refined_component_source_triangle_count"]
        or child.get("tangent_pattern_steps_radians")
        != knobs["triple_tangent_pattern_steps_radians"]
        or child.get("triple_coordinate_passes_per_step")
        != knobs["triple_tangent_coordinate_passes_per_step"]
        or child.get("tangent_candidates_per_root_per_step")
        != knobs["triple_tangent_candidate_count_per_root_step"]
        or child.get("atomic_triple_cartesian_candidates_per_step_pass")
        != knobs["triple_atomic_cartesian_candidates_per_step_pass"]
        or child.get("triple_refinement_root_count")
        != knobs["triple_refined_component_root_count"]
    ):
        raise CoarseScheduleContinuationError(
            "frontier triple-refinement child differs from the frozen v4 contract"
        )
    return child


def _expected_frontier_collar_endpoint(
    endpoint: Mapping[str, Any],
) -> dict[str, Any]:
    """Reconstruct and cross-check the v5 outer-tail collar child."""

    child = make_owner_free_direction_frontier_collar_refinement_endpoint(
        schedule_endpoint_sha256=str(endpoint["endpoint_sha256"])
    )
    contract = endpoint.get("collar_refinement_contract")
    invariant_child = {
        key: copy.deepcopy(value)
        for key, value in child.items()
        if not key.endswith("_sha256")
    }
    triple_child = _expected_frontier_pattern_endpoint(endpoint)
    proof_scope = endpoint["quality_evaluation_budget_proof"]["actual_scope"]
    if (
        contract != _COLLAR_REFINEMENT_CONTRACT
        or invariant_child != contract
        or child.get("schema") != COLLAR_REFINEMENT_ENDPOINT_SCHEMA
        or child.get("mode") != COLLAR_REFINEMENT_ENDPOINT_MODE
        or child.get("source_triple_refinement_pattern_endpoint_schema")
        != TRIPLE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA
        or child.get("source_triple_refinement_pattern_endpoint_sha256")
        != triple_child["endpoint_sha256"]
        or child.get("quality_objective")
        != list(_FRONTIER_PATTERN_QUALITY_OBJECTIVE)
        or child.get("collar_ring_widths") != list(_COLLAR_RING_WIDTHS)
        or child.get("maximum_collar_candidates") != len(_COLLAR_RING_WIDTHS)
        or proof_scope.get(
            "collar_refinement_quality_evaluation_upper_bound"
        )
        != len(_COLLAR_RING_WIDTHS)
    ):
        raise CoarseScheduleContinuationError(
            "frontier collar child differs from the frozen v5 contract"
        )
    return child


def _validate_quality_snapshot(
    value: Any,
    *,
    label: str,
    expected_endpoint: Mapping[str, Any],
    expected_prism_element_count: int | None,
    expected_core_element_count: int | None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _QUALITY_FIELDS:
        raise CoarseScheduleContinuationError(
            f"{label} has an incomplete field set"
        )
    unsigned = dict(value)
    quality_sha256 = _sha256(
        unsigned.pop("quality_sha256", ""), f"{label} SHA-256"
    )
    if _canonical_sha256(unsigned) != quality_sha256:
        raise CoarseScheduleContinuationError(f"{label} self-hash is stale")

    thresholds = expected_endpoint["quality_thresholds"]
    prism_threshold = _positive_finite(
        value.get("minimum_prism_scaled_jacobian_for_pass"),
        f"{label} prism threshold",
    )
    core_threshold = _positive_finite(
        value.get("minimum_core_tetra_gamma_for_pass"),
        f"{label} core threshold",
    )
    if (
        prism_threshold.hex()
        != float(thresholds["minimum_prism_scaled_jacobian_for_pass"]).hex()
        or core_threshold.hex()
        != float(thresholds["minimum_core_tetra_gamma_for_pass"]).hex()
    ):
        raise CoarseScheduleContinuationError(f"{label} thresholds differ")

    count_fields = (
        "nonfinite_count",
        "nonpositive_element_count",
        "prism_below_threshold_element_count",
        "core_tetra_below_gamma_count",
        "prism_element_count",
        "core_element_count",
        "core_tetra_count",
    )
    counts = {
        field: _nonnegative_int(value.get(field), f"{label} {field}")
        for field in count_fields
    }
    if (expected_prism_element_count is None) is not (
        expected_core_element_count is None
    ):
        raise CoarseScheduleContinuationError(
            f"{label} expected inventory is incomplete"
        )
    prism_inventory = (
        counts["prism_element_count"]
        if expected_prism_element_count is None
        else expected_prism_element_count
    )
    core_inventory = (
        counts["core_element_count"]
        if expected_core_element_count is None
        else expected_core_element_count
    )
    if (
        (expected_prism_element_count is not None
         and counts["prism_element_count"] != expected_prism_element_count)
        or (expected_core_element_count is not None
            and counts["core_element_count"] != expected_core_element_count)
        or counts["core_tetra_count"] != core_inventory
        or prism_inventory + core_inventory <= 0
        or counts["prism_below_threshold_element_count"] > prism_inventory
        or counts["core_tetra_below_gamma_count"] > core_inventory
        or counts["nonfinite_count"] > prism_inventory + core_inventory
        or counts["nonpositive_element_count"]
        > prism_inventory + core_inventory
    ):
        raise CoarseScheduleContinuationError(f"{label} counts are inconsistent")
    if (
        not isinstance(value.get("all_values_finite"), bool)
        or value["all_values_finite"] is not (counts["nonfinite_count"] == 0)
    ):
        raise CoarseScheduleContinuationError(
            f"{label} finite flag/count disagree"
        )
    numeric_fields = (
        "maximum_prism_scaled_jacobian_deficit",
        "prism_scaled_jacobian_deficit_l1",
        "prism_scaled_jacobian_deficit_l2",
        "maximum_core_tetra_gamma_deficit",
        "core_tetra_gamma_deficit_l2",
    )
    try:
        minimum_prism = float(value.get("minimum_prism_scaled_jacobian"))
        minimum_core = float(value.get("minimum_core_tetra_gamma"))
        numeric_values = [float(value.get(field)) for field in numeric_fields]
    except (TypeError, ValueError) as error:
        raise CoarseScheduleContinuationError(
            f"{label} contains an invalid quality metric"
        ) from error
    if (
        not math.isfinite(minimum_prism)
        or not math.isfinite(minimum_core)
        or any(not math.isfinite(item) or item < 0.0 for item in numeric_values)
    ):
        raise CoarseScheduleContinuationError(
            f"{label} contains an invalid quality metric"
        )
    computed_status = (
        "PASS"
        if counts["nonfinite_count"] == 0
        and counts["nonpositive_element_count"] == 0
        and counts["prism_below_threshold_element_count"] == 0
        and counts["core_tetra_below_gamma_count"] == 0
        else "FAIL"
    )
    if value.get("status") != computed_status:
        raise CoarseScheduleContinuationError(f"{label} status is inconsistent")
    return copy.deepcopy(dict(value))


def _validate_objective_snapshot(
    value: Any,
    *,
    configured_sha256: Any,
    quality: Mapping[str, Any],
    label: str,
) -> tuple[list[float], str]:
    """Validate a count-first objective and its field-bound canonical hash."""

    if not isinstance(value, list) or len(value) != len(
        _FRONTIER_PATTERN_QUALITY_OBJECTIVE
    ):
        raise CoarseScheduleContinuationError(
            f"{label} has an incomplete objective"
        )
    if any(
        isinstance(item, bool)
        or not isinstance(item, (int, float))
        or not math.isfinite(float(item))
        for item in value
    ):
        raise CoarseScheduleContinuationError(
            f"{label} contains a non-finite objective value"
        )
    normalized = [float(item) for item in value]
    expected_prefix = [
        float(quality["nonfinite_count"]),
        float(quality["nonpositive_element_count"]),
        float(quality["core_tetra_below_gamma_count"]),
        float(quality["maximum_core_tetra_gamma_deficit"]),
        float(quality["core_tetra_gamma_deficit_l2"]),
        float(quality["prism_below_threshold_element_count"]),
        float(quality["maximum_prism_scaled_jacobian_deficit"]),
        float(quality["prism_scaled_jacobian_deficit_l2"]),
        float(quality["prism_scaled_jacobian_deficit_l1"]),
        -float(quality["minimum_prism_scaled_jacobian"]),
        -float(quality["minimum_core_tetra_gamma"]),
    ]
    if [item.hex() for item in normalized[:-1]] != [
        item.hex() for item in expected_prefix
    ] or normalized[-1] < 0.0:
        raise CoarseScheduleContinuationError(
            f"{label} differs from its quality snapshot"
        )
    objective_sha256 = _sha256(configured_sha256, f"{label} SHA-256")
    expected_sha256 = _canonical_sha256(
        {
            "quality_objective_fields": list(
                _FRONTIER_PATTERN_QUALITY_OBJECTIVE
            ),
            "objective": value,
        }
    )
    if objective_sha256 != expected_sha256:
        raise CoarseScheduleContinuationError(f"{label} self-hash is stale")
    return normalized, objective_sha256


def _canonical_unit_hex(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or len(value) != 3:
        raise CoarseScheduleContinuationError(
            f"{label} must contain three binary64 values"
        )
    try:
        decoded = [float.fromhex(str(item)) for item in value]
    except ValueError as error:
        raise CoarseScheduleContinuationError(
            f"{label} is not canonical binary64 hex"
        ) from error
    if any(not math.isfinite(item) for item in decoded):
        raise CoarseScheduleContinuationError(f"{label} is non-finite")
    norm = math.sqrt(math.fsum(item * item for item in decoded))
    if (
        [item.hex() for item in decoded] != value
        or not math.isclose(norm, 1.0, rel_tol=1.0e-12, abs_tol=1.0e-12)
    ):
        raise CoarseScheduleContinuationError(
            f"{label} is not a canonical unit vector"
        )
    return list(value)


def _validate_static_and_fine_pattern_evidence(
    *,
    pattern_search: Mapping[str, Any],
    selection: Any,
    endpoint: Mapping[str, Any],
    expected_pattern_endpoint: Mapping[str, Any],
    stable_component_records: list[Mapping[str, Any]],
    total_quality_evaluation_count: int,
) -> dict[str, Any]:
    """Validate the fresh-A static gate and single-component fine phase."""

    search_records = pattern_search.get("component_records")
    if not isinstance(search_records, list) or len(search_records) != len(
        stable_component_records
    ):
        raise CoarseScheduleContinuationError(
            "continuation pattern component evidence is incomplete"
        )
    search_component_fields = {
        "candidate_root_count",
        "coordinate_candidate_count",
        "interaction_component_sha256",
        "joint_candidate_count",
        "quality_evaluation_count",
        "search_exit_coordinate_sha256",
        "search_exit_quality_sha256",
        "selected_objective",
        "source_triangle_count",
        "source_triangle_sha256",
        "static_exit_quality",
        "static_exit_quality_sha256",
        "static_exit_objective",
        "static_exit_objective_sha256",
        "static_exit_coordinate_sha256",
        "static_exit_status",
        "static_exit_quality_evaluation_index",
    }
    fine_exit_checkpoint_field = "fine_exit_checkpoint"
    stable_by_triangles: dict[tuple[str, ...], Mapping[str, Any]] = {}
    for record in stable_component_records:
        key = tuple(str(item) for item in record["source_triangle_sha256"])
        if key in stable_by_triangles:
            raise CoarseScheduleContinuationError(
                "continuation stable component triangle sets are ambiguous"
            )
        stable_by_triangles[key] = record

    search_by_id: dict[str, dict[str, Any]] = {}
    matched_triangle_sets: set[tuple[str, ...]] = set()
    for record in search_records:
        if (
            not isinstance(record, Mapping)
            or frozenset(record)
            not in {
                frozenset(search_component_fields),
                frozenset(search_component_fields | {fine_exit_checkpoint_field}),
            }
        ):
            raise CoarseScheduleContinuationError(
                "one continuation pattern component record is incomplete"
            )
        component_id = _sha256(
            record["interaction_component_sha256"],
            "continuation interaction component SHA-256",
        )
        roots = _positive_int(
            record["candidate_root_count"],
            "continuation pattern component root count",
        )
        source_count = _positive_int(
            record["source_triangle_count"],
            "continuation pattern source triangle count",
        )
        source_triangles = record["source_triangle_sha256"]
        if (
            component_id in search_by_id
            or not isinstance(source_triangles, list)
            or source_triangles != sorted(set(source_triangles))
            or len(source_triangles) != source_count
            or any(
                _SHA256.fullmatch(str(item)) is None
                for item in source_triangles
            )
        ):
            raise CoarseScheduleContinuationError(
                "one continuation pattern component identity is stale"
            )
        triangle_key = tuple(str(item) for item in source_triangles)
        stable_record = stable_by_triangles.get(triangle_key)
        if (
            stable_record is None
            or triangle_key in matched_triangle_sets
            or roots != stable_record["root_count"]
        ):
            raise CoarseScheduleContinuationError(
                "continuation pattern component differs from stable topology"
            )
        matched_triangle_sets.add(triangle_key)

        static_quality = _validate_quality_snapshot(
            record["static_exit_quality"],
            label="continuation static component quality",
            expected_endpoint=endpoint,
            expected_prism_element_count=None,
            expected_core_element_count=None,
        )
        static_quality_sha256 = _sha256(
            record["static_exit_quality_sha256"],
            "continuation static component quality SHA-256",
        )
        _, static_objective_sha256 = _validate_objective_snapshot(
            record["static_exit_objective"],
            configured_sha256=record["static_exit_objective_sha256"],
            quality=static_quality,
            label="continuation static component objective",
        )
        static_coordinate_sha256 = _sha256(
            record["static_exit_coordinate_sha256"],
            "continuation static component coordinate SHA-256",
        )
        static_index = _positive_int(
            record["static_exit_quality_evaluation_index"],
            "continuation static component evaluation index",
        )
        selected_objective = record["selected_objective"]
        if (
            static_quality_sha256 != static_quality["quality_sha256"]
            or record["static_exit_status"] != static_quality["status"]
            or static_quality["all_values_finite"] is not True
            or static_quality["nonfinite_count"] != 0
            or static_quality["nonpositive_element_count"] != 0
            or static_quality["core_tetra_below_gamma_count"] != 0
            or not isinstance(selected_objective, list)
            or len(selected_objective)
            != len(_FRONTIER_PATTERN_QUALITY_OBJECTIVE)
            or any(
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                or not math.isfinite(float(item))
                for item in selected_objective
            )
        ):
            raise CoarseScheduleContinuationError(
                "one continuation static component exit is unsafe"
            )
        search_exit_coordinate_sha256 = _sha256(
            record["search_exit_coordinate_sha256"],
            "continuation search-exit coordinate SHA-256",
        )
        search_exit_quality_sha256 = _sha256(
            record["search_exit_quality_sha256"],
            "continuation search-exit quality SHA-256",
        )
        component_evaluations = _positive_int(
            record["quality_evaluation_count"],
            "continuation pattern component evaluation count",
        )
        _nonnegative_int(
            record["coordinate_candidate_count"],
            "continuation coordinate candidate count",
        )
        _nonnegative_int(
            record["joint_candidate_count"],
            "continuation joint candidate count",
        )
        search_by_id[component_id] = {
            "record": record,
            "stable_record": stable_record,
            "static_quality": static_quality,
            "static_quality_sha256": static_quality_sha256,
            "static_objective_sha256": static_objective_sha256,
            "static_coordinate_sha256": static_coordinate_sha256,
            "static_index": static_index,
            "component_evaluations": component_evaluations,
            "search_exit_coordinate_sha256": search_exit_coordinate_sha256,
            "search_exit_quality_sha256": search_exit_quality_sha256,
            "fine_exit_checkpoint": record.get(
                fine_exit_checkpoint_field
            ),
        }
    if matched_triangle_sets != set(stable_by_triangles):
        raise CoarseScheduleContinuationError(
            "continuation pattern components do not cover stable topology"
        )
    if [
        str(record["interaction_component_sha256"])
        for record in search_records
    ] != sorted(search_by_id):
        raise CoarseScheduleContinuationError(
            "continuation pattern component records are not canonical"
        )

    selection_fields = {
        "schema",
        "status",
        "gate",
        "order",
        "evaluation_evidence",
        "selected_component",
        "prefix_checkpoint",
        "component_static_evidence",
    }
    if not isinstance(selection, Mapping) or set(selection) != selection_fields:
        raise CoarseScheduleContinuationError(
            "continuation fine-refinement selection is incomplete"
        )
    gate = selection["gate"]
    order = selection["order"]
    evaluations = selection["evaluation_evidence"]
    static_evidence = selection["component_static_evidence"]
    if (
        selection.get("schema") != _FINE_REFINEMENT_SELECTION_SCHEMA
        or selection.get("status") != "PASS"
        or not isinstance(gate, Mapping)
        or not isinstance(order, Mapping)
        or not isinstance(evaluations, Mapping)
        or not isinstance(static_evidence, list)
    ):
        raise CoarseScheduleContinuationError(
            "continuation fine-refinement selection gate is invalid"
        )

    gate_fields = {
        "static_all_components_complete_before_fine",
        "static_search_started_from_fresh_a",
        "static_component_count",
        "static_failing_component_count",
        "maximum_refined_component_count",
        "required_refined_component_root_count",
        "required_refined_component_source_triangle_count",
        "fine_refinement_performed",
    }
    failing_ids = sorted(
        component_id
        for component_id, evidence in search_by_id.items()
        if evidence["static_quality"]["status"] == "FAIL"
    )
    passing_ids = sorted(set(search_by_id) - set(failing_ids))
    fine_performed = gate.get("fine_refinement_performed")
    contract = endpoint["direction_search_contract"]
    if (
        set(gate) != gate_fields
        or gate.get("static_all_components_complete_before_fine") is not True
        or gate.get("static_search_started_from_fresh_a") is not True
        or gate.get("static_component_count") != len(search_by_id)
        or gate.get("static_component_count")
        != _OBSERVED_REAL_SCOPE["component_count"]
        or gate.get("static_failing_component_count") != len(failing_ids)
        or len(failing_ids) not in {0, 1}
        or gate.get("maximum_refined_component_count")
        != contract["maximum_refined_component_count"]
        or gate.get("required_refined_component_root_count")
        != contract["refined_component_root_count"]
        or gate.get("required_refined_component_source_triangle_count")
        != contract["refined_component_source_triangle_count"]
        or not isinstance(fine_performed, bool)
        or fine_performed is not (len(failing_ids) == 1)
        or len(passing_ids) != len(search_by_id) - len(failing_ids)
    ):
        raise CoarseScheduleContinuationError(
            "continuation static-to-fine gate is stale or unsafe"
        )

    static_evidence_fields = {
        "interaction_component_sha256",
        "static_exit_status",
        "static_exit_quality_sha256",
        "static_exit_objective_sha256",
        "static_exit_coordinate_sha256",
        "static_exit_quality_evaluation_index",
    }
    expected_static_evidence: list[dict[str, Any]] = []
    for component_id in sorted(search_by_id):
        evidence = search_by_id[component_id]
        expected_static_evidence.append(
            {
                "interaction_component_sha256": component_id,
                "static_exit_status": evidence["static_quality"]["status"],
                "static_exit_quality_sha256": evidence[
                    "static_quality_sha256"
                ],
                "static_exit_objective_sha256": evidence[
                    "static_objective_sha256"
                ],
                "static_exit_coordinate_sha256": evidence[
                    "static_coordinate_sha256"
                ],
                "static_exit_quality_evaluation_index": evidence[
                    "static_index"
                ],
            }
        )
    if (
        any(
            not isinstance(record, Mapping)
            or set(record) != static_evidence_fields
            for record in static_evidence
        )
        or static_evidence != expected_static_evidence
    ):
        raise CoarseScheduleContinuationError(
            "continuation component static evidence is stale"
        )

    order_fields = {
        "static_component_sha256_order",
        "fine_component_sha256_order",
        "last_static_quality_evaluation_index",
        "first_fine_quality_evaluation_index",
        "all_static_exits_before_first_fine_evaluation",
        "tail_steps_radians",
        "tail_start_coordinate_sha256",
        "tail_start_direction_state_sha256",
        "tail_start_direction_field_sha256",
        "tail_start_quality_sha256",
        "tail_started_from_prefix_checkpoint",
        "first_tail_quality_evaluation_index",
    }
    static_order = order.get("static_component_sha256_order")
    fine_order = order.get("fine_component_sha256_order")
    static_ids_by_index = [
        component_id
        for component_id, _evidence in sorted(
            search_by_id.items(), key=lambda item: item[1]["static_index"]
        )
    ]
    last_static_index = max(
        evidence["static_index"] for evidence in search_by_id.values()
    )
    static_indices = [
        evidence["static_index"] for evidence in search_by_id.values()
    ]
    if (
        set(order) != order_fields
        or static_order != static_ids_by_index
        or not isinstance(static_order, list)
        or len(static_order) != len(set(static_order))
        or set(static_order) != set(search_by_id)
        or len(static_indices) != len(set(static_indices))
        or not isinstance(fine_order, list)
        or order.get("last_static_quality_evaluation_index")
        != last_static_index
        or order.get("all_static_exits_before_first_fine_evaluation")
        is not True
        or order.get("tail_steps_radians")
        != list(_FINE_REFINEMENT_TAIL_STEPS_RADIANS)
    ):
        raise CoarseScheduleContinuationError(
            "continuation static/fine evaluation order is invalid"
        )

    evaluation_fields = {
        "endpoint_maximum_quality_evaluations",
        "global_conservative_quality_evaluation_cap",
        "static_quality_evaluation_count",
        "fine_quality_evaluation_count",
        "total_quality_evaluation_count",
    }
    if set(evaluations) != evaluation_fields:
        raise CoarseScheduleContinuationError(
            "continuation phase evaluation evidence is incomplete"
        )
    static_evaluation_count = _positive_int(
        evaluations["static_quality_evaluation_count"],
        "continuation static evaluation count",
    )
    fine_evaluation_count = _nonnegative_int(
        evaluations["fine_quality_evaluation_count"],
        "continuation fine evaluation count",
    )
    selection_total = _positive_int(
        evaluations["total_quality_evaluation_count"],
        "continuation total evaluation count",
    )
    proof_scope = endpoint["quality_evaluation_budget_proof"]["actual_scope"]
    if (
        evaluations.get("endpoint_maximum_quality_evaluations")
        != endpoint["caps"]["maximum_quality_evaluations"]
        or evaluations.get("global_conservative_quality_evaluation_cap")
        != (
            proof_scope["static_search_quality_evaluation_upper_bound"]
            + proof_scope["fine_refinement_quality_evaluation_upper_bound"]
            + proof_scope[
                "replay_and_recheck_quality_evaluation_upper_bound"
            ]
        )
        or selection_total != static_evaluation_count + fine_evaluation_count
        or selection_total > total_quality_evaluation_count
        or selection_total > (
            proof_scope["static_search_quality_evaluation_upper_bound"]
            + proof_scope["fine_refinement_quality_evaluation_upper_bound"]
        )
        or static_evaluation_count
        > proof_scope["static_search_quality_evaluation_upper_bound"]
        or fine_evaluation_count
        > proof_scope["fine_refinement_quality_evaluation_upper_bound"]
        or last_static_index != static_evaluation_count
    ):
        raise CoarseScheduleContinuationError(
            "continuation phase evaluation budget is invalid"
        )

    refinement = pattern_search.get("tangent_pattern_refinement")
    if not isinstance(refinement, Mapping) or (
        refinement.get("performed") is not fine_performed
        or refinement.get("steps_radians")
        != expected_pattern_endpoint["tangent_pattern_steps_radians"]
        or refinement.get("coordinate_passes_per_step")
        != expected_pattern_endpoint["tangent_coordinate_passes_per_step"]
        or refinement.get("shared_triangle_pair_moves") is not True
        or refinement.get("quality_evaluation_count")
        != fine_evaluation_count
        or refinement.get("refined_component_count") != len(failing_ids)
        or refinement.get("refined_component_count")
        > expected_pattern_endpoint["maximum_refined_component_count"]
    ):
        raise CoarseScheduleContinuationError(
            "continuation fine-refinement execution differs from its endpoint"
        )

    selected_component = selection["selected_component"]
    prefix = selection["prefix_checkpoint"]
    if not fine_performed:
        nullable_order_fields = (
            "first_fine_quality_evaluation_index",
            "tail_start_coordinate_sha256",
            "tail_start_direction_state_sha256",
            "tail_start_direction_field_sha256",
            "tail_start_quality_sha256",
            "first_tail_quality_evaluation_index",
        )
        if (
            selected_component is not None
            or prefix is not None
            or fine_order != []
            or fine_evaluation_count != 0
            or order.get("tail_started_from_prefix_checkpoint") is not False
            or any(order.get(field) is not None for field in nullable_order_fields)
        ):
            raise CoarseScheduleContinuationError(
                "continuation zero-failure branch performed fine refinement"
            )
        return {
            "fine_performed": False,
            "failing_ids": [],
            "selected_component": None,
            "selected_id": None,
            "selected_evidence": None,
            "static_evaluation_count": static_evaluation_count,
            "fine_evaluation_count": 0,
            "fine_handoff_evaluation_count": selection_total,
        }

    selected_fields = {
        "interaction_component_sha256",
        "candidate_root_count",
        "root_coordinate_sha256",
        "source_triangle_count",
        "source_triangle_sha256",
        "unique_source_triangle_edge_count",
    }
    if not isinstance(selected_component, Mapping) or set(
        selected_component
    ) != selected_fields:
        raise CoarseScheduleContinuationError(
            "continuation selected fine component is incomplete"
        )
    selected_id = _sha256(
        selected_component["interaction_component_sha256"],
        "continuation selected component SHA-256",
    )
    selected_evidence = search_by_id.get(selected_id)
    selected_roots = selected_component["root_coordinate_sha256"]
    selected_triangles = selected_component["source_triangle_sha256"]
    stable_selected = (
        None if selected_evidence is None else selected_evidence["stable_record"]
    )
    if (
        failing_ids != [selected_id]
        or fine_order != [selected_id]
        or selected_evidence is None
        or stable_selected is None
        or selected_component.get("candidate_root_count")
        != contract["refined_component_root_count"]
        or selected_component.get("candidate_root_count")
        != stable_selected["root_count"]
        or not isinstance(selected_roots, list)
        or selected_roots != stable_selected["root_coordinate_sha256"]
        or selected_roots != sorted(set(selected_roots))
        or len(selected_roots) != contract["refined_component_root_count"]
        or selected_component.get("source_triangle_count")
        != contract["refined_component_source_triangle_count"]
        or not isinstance(selected_triangles, list)
        or selected_triangles != stable_selected["source_triangle_sha256"]
        or selected_triangles != sorted(set(selected_triangles))
        or len(selected_triangles)
        != contract["refined_component_source_triangle_count"]
        or selected_component.get("unique_source_triangle_edge_count") != 3
    ):
        raise CoarseScheduleContinuationError(
            "continuation selected component is outside the frozen fine policy"
        )

    first_fine_index = _positive_int(
        order.get("first_fine_quality_evaluation_index"),
        "continuation first fine evaluation index",
    )
    if first_fine_index != last_static_index + 1:
        raise CoarseScheduleContinuationError(
            "continuation fine refinement did not begin after every static exit"
        )
    prefix_fields = {
        "selected_component_sha256",
        "prefix_steps_radians",
        "selected_state_coordinate_sha256",
        "selected_state_quality",
        "selected_state_quality_sha256",
        "selected_state_objective",
        "selected_state_objective_sha256",
        "selected_direction_state_sha256",
        "selected_direction_field_sha256",
        "quality_evaluation_index",
    }
    if not isinstance(prefix, Mapping) or set(prefix) != prefix_fields:
        raise CoarseScheduleContinuationError(
            "continuation fine prefix checkpoint is incomplete"
        )
    prefix_quality = _validate_quality_snapshot(
        prefix["selected_state_quality"],
        label="continuation fine prefix quality",
        expected_endpoint=endpoint,
        expected_prism_element_count=None,
        expected_core_element_count=None,
    )
    prefix_quality_sha256 = _sha256(
        prefix["selected_state_quality_sha256"],
        "continuation fine prefix quality SHA-256",
    )
    _validate_objective_snapshot(
        prefix["selected_state_objective"],
        configured_sha256=prefix["selected_state_objective_sha256"],
        quality=prefix_quality,
        label="continuation fine prefix objective",
    )
    prefix_coordinate_sha256 = _sha256(
        prefix["selected_state_coordinate_sha256"],
        "continuation fine prefix coordinate SHA-256",
    )
    prefix_direction_state_sha256 = _sha256(
        prefix["selected_direction_state_sha256"],
        "continuation fine prefix direction-state SHA-256",
    )
    prefix_direction_field_sha256 = _sha256(
        prefix["selected_direction_field_sha256"],
        "continuation fine prefix direction-field SHA-256",
    )
    prefix_index = _positive_int(
        prefix["quality_evaluation_index"],
        "continuation fine prefix evaluation index",
    )
    first_tail_index = _positive_int(
        order.get("first_tail_quality_evaluation_index"),
        "continuation first tail evaluation index",
    )
    if (
        prefix.get("selected_component_sha256") != selected_id
        or prefix.get("prefix_steps_radians")
        != list(_FINE_REFINEMENT_PREFIX_STEPS_RADIANS)
        or prefix_quality_sha256 != prefix_quality["quality_sha256"]
        or prefix_quality["all_values_finite"] is not True
        or prefix_quality["nonfinite_count"] != 0
        or any(
            prefix_quality[field]
            != selected_evidence["static_quality"][field]
            for field in (
                "prism_element_count",
                "core_element_count",
                "core_tetra_count",
            )
        )
        or not first_fine_index <= prefix_index < first_tail_index
        or first_tail_index > static_evaluation_count + fine_evaluation_count
        or order.get("tail_started_from_prefix_checkpoint") is not True
        or _sha256(
            order.get("tail_start_coordinate_sha256"),
            "continuation tail-start coordinate SHA-256",
        )
        != prefix_coordinate_sha256
        or _sha256(
            order.get("tail_start_direction_state_sha256"),
            "continuation tail-start direction-state SHA-256",
        )
        != prefix_direction_state_sha256
        or _sha256(
            order.get("tail_start_direction_field_sha256"),
            "continuation tail-start direction-field SHA-256",
        )
        != prefix_direction_field_sha256
        or _sha256(
            order.get("tail_start_quality_sha256"),
            "continuation tail-start quality SHA-256",
        )
        != prefix_quality_sha256
    ):
        raise CoarseScheduleContinuationError(
            "continuation fine prefix/tail continuity is invalid"
        )
    return {
        "fine_performed": True,
        "failing_ids": failing_ids,
        "selected_component": copy.deepcopy(dict(selected_component)),
        "selected_id": selected_id,
        "selected_evidence": selected_evidence,
        "static_evaluation_count": static_evaluation_count,
        "fine_evaluation_count": fine_evaluation_count,
        "fine_handoff_evaluation_count": selection_total,
    }


def _validate_triple_pattern_evidence(
    *,
    pattern_search: Mapping[str, Any],
    selection: Any,
    endpoint: Mapping[str, Any],
    expected_pattern_endpoint: Mapping[str, Any],
    fine_context: Mapping[str, Any],
    total_quality_evaluation_count: int,
) -> None:
    """Validate the atomic three-root phase and its fine-to-triple handoff."""

    selection_fields = {
        "schema",
        "status",
        "gate",
        "selected_component",
        "handoff",
        "schedule",
        "step_pass_records",
        "evaluation_evidence",
        "final_state",
    }
    if not isinstance(selection, Mapping) or set(selection) != selection_fields:
        raise CoarseScheduleContinuationError(
            "continuation triple-refinement selection is incomplete"
        )
    gate = selection["gate"]
    schedule = selection["schedule"]
    records = selection["step_pass_records"]
    evaluations = selection["evaluation_evidence"]
    if (
        selection.get("schema") != _TRIPLE_REFINEMENT_SELECTION_SCHEMA
        or selection.get("status") != "PASS"
        or not isinstance(gate, Mapping)
        or not isinstance(schedule, Mapping)
        or not isinstance(records, list)
        or not isinstance(evaluations, Mapping)
    ):
        raise CoarseScheduleContinuationError(
            "continuation triple-refinement selection is invalid"
        )

    fine_performed = fine_context.get("fine_performed") is True
    triple_performed = fine_performed
    contract = endpoint["direction_search_contract"]
    gate_fields = {
        "static_all_components_complete_before_fine",
        "single_and_pair_phases_complete_before_triple",
        "static_failing_component_count",
        "fine_refinement_performed",
        "triple_refinement_performed",
        "required_triple_component_root_count",
        "required_triple_component_source_triangle_count",
        "selected_component_shape_valid",
    }
    if (
        set(gate) != gate_fields
        or gate.get("static_all_components_complete_before_fine") is not True
        or gate.get("single_and_pair_phases_complete_before_triple") is not True
        or gate.get("fine_refinement_performed") is not fine_performed
        or gate.get("triple_refinement_performed") is not triple_performed
        or gate.get("static_failing_component_count")
        != (1 if fine_performed else 0)
        or gate.get("required_triple_component_root_count")
        != contract["triple_refinement_root_count"]
        or gate.get("required_triple_component_source_triangle_count")
        != contract["refined_component_source_triangle_count"]
        or gate.get("selected_component_shape_valid") is not True
    ):
        raise CoarseScheduleContinuationError(
            "continuation fine-to-triple gate is stale or unsafe"
        )

    schedule_fields = {
        "steps_radians",
        "coordinate_passes_per_step",
        "tangent_candidates_per_root_per_step",
        "atomic_cartesian_candidates_per_step_pass",
    }
    if (
        set(schedule) != schedule_fields
        or schedule.get("steps_radians")
        != list(_FINE_REFINEMENT_STEPS_RADIANS)
        or schedule.get("steps_radians")
        != expected_pattern_endpoint["tangent_pattern_steps_radians"]
        or schedule.get("coordinate_passes_per_step")
        != expected_pattern_endpoint["triple_coordinate_passes_per_step"]
        or schedule.get("tangent_candidates_per_root_per_step")
        != expected_pattern_endpoint["tangent_candidates_per_root_per_step"]
        or schedule.get("atomic_cartesian_candidates_per_step_pass")
        != expected_pattern_endpoint[
            "atomic_triple_cartesian_candidates_per_step_pass"
        ]
        or schedule["atomic_cartesian_candidates_per_step_pass"]
        != schedule["tangent_candidates_per_root_per_step"]
        ** contract["triple_refinement_root_count"]
    ):
        raise CoarseScheduleContinuationError(
            "continuation triple-refinement schedule differs from its endpoint"
        )

    selected_component = selection["selected_component"]
    handoff = selection["handoff"]
    final_state = selection["final_state"]
    expected_record_count = (
        len(_FINE_REFINEMENT_STEPS_RADIANS)
        * schedule["coordinate_passes_per_step"]
        if triple_performed
        else 0
    )
    if len(records) != expected_record_count:
        raise CoarseScheduleContinuationError(
            "continuation triple-refinement step/pass count is incomplete"
        )

    evaluation_fields = {
        "endpoint_maximum_quality_evaluations",
        "global_conservative_quality_evaluation_cap",
        "static_quality_evaluation_count",
        "fine_quality_evaluation_count",
        "through_fine_handoff_quality_evaluation_count",
        "triple_quality_evaluation_count",
        "final_replay_and_verification_quality_evaluation_count",
        "total_quality_evaluation_count",
        "triple_quality_evaluation_start_index_exclusive",
        "first_triple_quality_evaluation_index",
        "last_triple_quality_evaluation_index",
        "triple_quality_evaluation_end_index_inclusive",
    }
    if set(evaluations) != evaluation_fields:
        raise CoarseScheduleContinuationError(
            "continuation triple evaluation evidence is incomplete"
        )
    static_count = _positive_int(
        evaluations["static_quality_evaluation_count"],
        "continuation triple-evidence static evaluation count",
    )
    fine_count = _nonnegative_int(
        evaluations["fine_quality_evaluation_count"],
        "continuation triple-evidence fine evaluation count",
    )
    triple_count = _nonnegative_int(
        evaluations["triple_quality_evaluation_count"],
        "continuation triple evaluation count",
    )
    final_count = _nonnegative_int(
        evaluations[
            "final_replay_and_verification_quality_evaluation_count"
        ],
        "continuation final replay/verification evaluation count",
    )
    global_total = _positive_int(
        evaluations["total_quality_evaluation_count"],
        "continuation global evaluation count",
    )
    proof_scope = endpoint["quality_evaluation_budget_proof"]["actual_scope"]
    pattern_conservative_bound = (
        proof_scope["quality_evaluation_upper_bound"]
        - proof_scope["collar_refinement_quality_evaluation_upper_bound"]
    )
    fine_handoff_count = _positive_int(
        fine_context["fine_handoff_evaluation_count"],
        "continuation fine handoff evaluation count",
    )
    through_fine_count = evaluations.get(
        "through_fine_handoff_quality_evaluation_count"
    )
    triple_start_exclusive = evaluations.get(
        "triple_quality_evaluation_start_index_exclusive"
    )
    first_triple_index = evaluations.get(
        "first_triple_quality_evaluation_index"
    )
    last_triple_index = evaluations.get(
        "last_triple_quality_evaluation_index"
    )
    triple_end_inclusive = evaluations.get(
        "triple_quality_evaluation_end_index_inclusive"
    )
    if (
        evaluations.get("endpoint_maximum_quality_evaluations")
        != endpoint["caps"]["maximum_quality_evaluations"]
        or evaluations.get("global_conservative_quality_evaluation_cap")
        != pattern_conservative_bound
        or static_count != fine_context["static_evaluation_count"]
        or fine_count != fine_context["fine_evaluation_count"]
        or static_count + fine_count != fine_handoff_count
        or through_fine_count != fine_handoff_count
        or triple_count
        > proof_scope["triple_refinement_quality_evaluation_upper_bound"]
        or final_count
        != proof_scope["replay_and_recheck_quality_evaluation_upper_bound"]
        or global_total
        != static_count + fine_count + triple_count + final_count
        or global_total != total_quality_evaluation_count
        or global_total > pattern_conservative_bound
        or global_total > endpoint["caps"]["maximum_quality_evaluations"]
        or triple_start_exclusive != fine_handoff_count
    ):
        raise CoarseScheduleContinuationError(
            "continuation triple/global evaluation budget is invalid"
        )

    summary = pattern_search.get("triple_pattern_refinement")
    summary_fields = {
        "performed",
        "steps_radians",
        "coordinate_passes_per_step",
        "tangent_candidates_per_root_per_step",
        "atomic_cartesian_candidates_per_step_pass",
        "quality_evaluation_count",
        "accepted_move_count",
        "rejected_margin_candidate_count",
        "refined_component_count",
        "step_pass_records",
    }
    if not isinstance(summary, Mapping) or set(summary) != summary_fields:
        raise CoarseScheduleContinuationError(
            "continuation triple-refinement summary is incomplete"
        )
    if (
        summary.get("performed") is not triple_performed
        or summary.get("steps_radians") != schedule["steps_radians"]
        or summary.get("coordinate_passes_per_step")
        != schedule["coordinate_passes_per_step"]
        or summary.get("tangent_candidates_per_root_per_step")
        != schedule["tangent_candidates_per_root_per_step"]
        or summary.get("atomic_cartesian_candidates_per_step_pass")
        != schedule["atomic_cartesian_candidates_per_step_pass"]
        or summary.get("quality_evaluation_count") != triple_count
        or summary.get("refined_component_count")
        != (1 if triple_performed else 0)
        or summary.get("step_pass_records") != records
    ):
        raise CoarseScheduleContinuationError(
            "continuation triple-refinement summary diverges from selection evidence"
        )

    if not triple_performed:
        if (
            selected_component is not None
            or handoff is not None
            or final_state is not None
            or records
            or triple_count != 0
            or summary.get("accepted_move_count") != 0
            or summary.get("rejected_margin_candidate_count") != 0
            or first_triple_index is not None
            or last_triple_index is not None
        or triple_end_inclusive != fine_handoff_count
        ):
            raise CoarseScheduleContinuationError(
                "continuation zero-failure branch performed triple refinement"
            )
        return

    if selected_component != fine_context["selected_component"]:
        raise CoarseScheduleContinuationError(
            "continuation triple component differs from the fine component"
        )
    selected_id = str(fine_context["selected_id"])
    selected_evidence = fine_context["selected_evidence"]
    handoff_fields = {
        "selected_component_sha256",
        "fine_exit_coordinate_sha256",
        "fine_exit_quality_sha256",
        "fine_exit_quality",
        "fine_exit_objective_sha256",
        "fine_exit_objective",
        "fine_exit_direction_state_sha256",
        "fine_exit_direction_field_sha256",
        "triple_entry_coordinate_sha256",
        "triple_entry_quality_sha256",
        "triple_entry_objective_sha256",
        "triple_entry_direction_state_sha256",
        "triple_entry_direction_field_sha256",
        "fine_exit_quality_evaluation_index",
        "triple_quality_evaluation_start_index_exclusive",
        "without_reset",
    }
    if not isinstance(handoff, Mapping) or set(handoff) != handoff_fields:
        raise CoarseScheduleContinuationError(
            "continuation fine-to-triple handoff is incomplete"
        )
    hash_suffixes = (
        "coordinate_sha256",
        "quality_sha256",
        "objective_sha256",
        "direction_state_sha256",
        "direction_field_sha256",
    )
    fine_handoff_hashes = {
        suffix: _sha256(
            handoff.get(f"fine_exit_{suffix}"),
            f"continuation fine-exit {suffix}",
        )
        for suffix in hash_suffixes
    }
    triple_entry_hashes = {
        suffix: _sha256(
            handoff.get(f"triple_entry_{suffix}"),
            f"continuation triple-entry {suffix}",
        )
        for suffix in hash_suffixes
    }
    handoff_quality = _validate_quality_snapshot(
        handoff["fine_exit_quality"],
        label="continuation fine-exit handoff quality",
        expected_endpoint=endpoint,
        expected_prism_element_count=None,
        expected_core_element_count=None,
    )
    handoff_quality_sha256 = _sha256(
        handoff["fine_exit_quality_sha256"],
        "continuation fine-exit handoff quality SHA-256",
    )
    _handoff_objective, handoff_objective_sha256 = (
        _validate_objective_snapshot(
            handoff["fine_exit_objective"],
            configured_sha256=handoff["fine_exit_objective_sha256"],
            quality=handoff_quality,
            label="continuation fine-exit handoff objective",
        )
    )
    if (
        fine_handoff_hashes != triple_entry_hashes
        or handoff_quality_sha256 != handoff_quality["quality_sha256"]
        or fine_handoff_hashes["quality_sha256"]
        != handoff_quality_sha256
        or fine_handoff_hashes["objective_sha256"]
        != handoff_objective_sha256
        or handoff.get("selected_component_sha256") != selected_id
        or handoff.get("without_reset") is not True
        or handoff.get("fine_exit_quality_evaluation_index")
        != fine_handoff_count
        or handoff.get("triple_quality_evaluation_start_index_exclusive")
        != fine_handoff_count
        or first_triple_index != fine_handoff_count + 1
        or triple_end_inclusive != fine_handoff_count + triple_count
        or last_triple_index != triple_end_inclusive
    ):
        raise CoarseScheduleContinuationError(
            "continuation fine-to-triple handoff reset or skipped an evaluation"
        )

    checkpoint = selected_evidence.get("fine_exit_checkpoint")
    checkpoint_fields = {
        "selected_state_coordinate_sha256",
        "selected_state_quality",
        "selected_state_quality_sha256",
        "selected_state_objective",
        "selected_state_objective_sha256",
        "selected_direction_state_sha256",
        "selected_direction_field_sha256",
        "selected_directions",
        "fine_exit_quality_evaluation_index",
    }
    if not isinstance(checkpoint, Mapping) or set(checkpoint) != checkpoint_fields:
        raise CoarseScheduleContinuationError(
            "continuation fine-exit component checkpoint is incomplete"
        )
    checkpoint_quality = _validate_quality_snapshot(
        checkpoint["selected_state_quality"],
        label="continuation component fine-exit quality",
        expected_endpoint=endpoint,
        expected_prism_element_count=None,
        expected_core_element_count=None,
    )
    checkpoint_quality_sha256 = _sha256(
        checkpoint["selected_state_quality_sha256"],
        "continuation component fine-exit quality SHA-256",
    )
    _checkpoint_objective, checkpoint_objective_sha256 = (
        _validate_objective_snapshot(
            checkpoint["selected_state_objective"],
            configured_sha256=checkpoint[
                "selected_state_objective_sha256"
            ],
            quality=checkpoint_quality,
            label="continuation component fine-exit objective",
        )
    )
    checkpoint_hashes = {
        "coordinate_sha256": _sha256(
            checkpoint["selected_state_coordinate_sha256"],
            "continuation component fine-exit coordinate SHA-256",
        ),
        "quality_sha256": checkpoint_quality_sha256,
        "objective_sha256": checkpoint_objective_sha256,
        "direction_state_sha256": _sha256(
            checkpoint["selected_direction_state_sha256"],
            "continuation component fine-exit direction-state SHA-256",
        ),
        "direction_field_sha256": _sha256(
            checkpoint["selected_direction_field_sha256"],
            "continuation component fine-exit direction-field SHA-256",
        ),
    }
    selected_directions = checkpoint["selected_directions"]
    direction_record_fields = {
        "root_coordinate_sha256",
        "direction_float_hex",
    }
    if (
        not isinstance(selected_directions, list)
        or len(selected_directions)
        != contract["triple_refinement_root_count"]
        or any(
            not isinstance(record, Mapping)
            or set(record) != direction_record_fields
            for record in selected_directions
        )
    ):
        raise CoarseScheduleContinuationError(
            "continuation fine-exit selected directions are incomplete"
        )
    normalized_directions: list[dict[str, Any]] = []
    for record in selected_directions:
        normalized_directions.append(
            {
                "root_coordinate_sha256": _sha256(
                    record["root_coordinate_sha256"],
                    "continuation fine-exit direction root SHA-256",
                ),
                "direction_float_hex": _canonical_unit_hex(
                    record["direction_float_hex"],
                    "continuation fine-exit direction",
                ),
            }
        )
    if (
        normalized_directions != selected_directions
        or [
            record["root_coordinate_sha256"]
            for record in normalized_directions
        ]
        != selected_component["root_coordinate_sha256"]
        or checkpoint_hashes["direction_state_sha256"]
        != _canonical_sha256(
            {
                "encoding": "python-float-hex/root-sha/direction-v1",
                "records": normalized_directions,
            }
        )
        or checkpoint_hashes["direction_field_sha256"]
        != _canonical_sha256({"records": normalized_directions})
    ):
        raise CoarseScheduleContinuationError(
            "continuation fine-exit direction hashes are stale"
        )
    checkpoint_count = sum(
        int(evidence["fine_exit_checkpoint"] is not None)
        for evidence in (
            fine_context["selected_evidence"],
        )
    )
    all_checkpoint_count = sum(
        int(record.get("fine_exit_checkpoint") is not None)
        for record in pattern_search["component_records"]
    )
    if (
        checkpoint_quality_sha256 != checkpoint_quality["quality_sha256"]
        or checkpoint_hashes != fine_handoff_hashes
        or checkpoint["selected_state_quality"]
        != handoff["fine_exit_quality"]
        or checkpoint["selected_state_objective"]
        != handoff["fine_exit_objective"]
        or checkpoint.get("fine_exit_quality_evaluation_index")
        != fine_handoff_count
        or checkpoint_count != 1
        or all_checkpoint_count != 1
    ):
        raise CoarseScheduleContinuationError(
            "continuation fine-exit checkpoint is not anchored to the handoff"
        )

    record_fields = {
        "step_index_1_based",
        "pass_index_1_based",
        "step_radians",
        "step_radians_float_hex",
        "root_candidate_counts",
        "candidate_upper_bound",
        "atomic_candidate_count",
        "rejected_margin_candidate_count",
        "quality_evaluation_start_index_exclusive",
        "first_quality_evaluation_index",
        "last_quality_evaluation_index",
        "quality_evaluation_end_index_inclusive",
        "quality_evaluation_count",
        "accepted_move",
        "entry_coordinate_sha256",
        "entry_quality_sha256",
        "entry_objective_sha256",
        "entry_direction_state_sha256",
        "entry_direction_field_sha256",
        "exit_coordinate_sha256",
        "exit_quality_sha256",
        "exit_objective_sha256",
        "exit_direction_state_sha256",
        "exit_direction_field_sha256",
    }
    previous_hashes = dict(triple_entry_hashes)
    running_index = fine_handoff_count
    accepted_count = 0
    rejected_count = 0
    observed_triple_count = 0
    for record_index, record in enumerate(records):
        if not isinstance(record, Mapping) or set(record) != record_fields:
            raise CoarseScheduleContinuationError(
                "one continuation triple step/pass record is incomplete"
            )
        step_index = record_index // schedule["coordinate_passes_per_step"] + 1
        pass_index = record_index % schedule["coordinate_passes_per_step"] + 1
        step = _FINE_REFINEMENT_STEPS_RADIANS[step_index - 1]
        root_count_records = record["root_candidate_counts"]
        if (
            record.get("step_index_1_based") != step_index
            or record.get("pass_index_1_based") != pass_index
            or not isinstance(record.get("step_radians"), float)
            or record.get("step_radians").hex() != step.hex()
            or record.get("step_radians_float_hex") != step.hex()
            or not isinstance(root_count_records, list)
            or len(root_count_records)
            != contract["triple_refinement_root_count"]
        ):
            raise CoarseScheduleContinuationError(
                "one continuation triple step/pass identity is invalid"
            )
        selected_root_ids = selected_component["root_coordinate_sha256"]
        root_count_fields = {"root_coordinate_sha256", "candidate_count"}
        if any(
            not isinstance(item, Mapping) or set(item) != root_count_fields
            for item in root_count_records
        ):
            raise CoarseScheduleContinuationError(
                "one continuation triple root candidate record is incomplete"
            )
        record_root_ids = [
            _sha256(
                item["root_coordinate_sha256"],
                "continuation triple candidate root SHA-256",
            )
            for item in root_count_records
        ]
        normalized_root_counts = [
            _nonnegative_int(
                item["candidate_count"],
                "continuation triple root candidate count",
            )
            for item in root_count_records
        ]
        if any(
            item > schedule["tangent_candidates_per_root_per_step"]
            for item in normalized_root_counts
        ) or record_root_ids != selected_root_ids:
            raise CoarseScheduleContinuationError(
                "one continuation triple root candidate count exceeds its cap"
            )
        candidate_upper_bound = math.prod(normalized_root_counts)
        atomic_count = _nonnegative_int(
            record["atomic_candidate_count"],
            "continuation atomic triple candidate count",
        )
        rejected_delta = _nonnegative_int(
            record["rejected_margin_candidate_count"],
            "continuation rejected-margin candidate count",
        )
        quality_count = _nonnegative_int(
            record["quality_evaluation_count"],
            "continuation triple step/pass evaluation count",
        )
        record_start = record["quality_evaluation_start_index_exclusive"]
        record_end = record["quality_evaluation_end_index_inclusive"]
        first_index = record["first_quality_evaluation_index"]
        last_index = record["last_quality_evaluation_index"]
        entry_hashes = {
            suffix: _sha256(
                record.get(f"entry_{suffix}"),
                f"continuation triple entry {suffix}",
            )
            for suffix in hash_suffixes
        }
        exit_hashes = {
            suffix: _sha256(
                record.get(f"exit_{suffix}"),
                f"continuation triple exit {suffix}",
            )
            for suffix in hash_suffixes
        }
        accepted_move = record.get("accepted_move")
        if (
            record.get("candidate_upper_bound")
            != schedule["atomic_cartesian_candidates_per_step_pass"]
            or candidate_upper_bound
            > schedule["atomic_cartesian_candidates_per_step_pass"]
            or atomic_count != candidate_upper_bound
            or quality_count != atomic_count
            or record_start != running_index
            or record_end != running_index + quality_count
            or (
                quality_count > 0
                and (
                    first_index != running_index + 1
                    or last_index != record_end
                )
            )
            or (
                quality_count == 0
                and (first_index is not None or last_index is not None)
            )
            or entry_hashes != previous_hashes
            or not isinstance(accepted_move, bool)
            or (not accepted_move and exit_hashes != entry_hashes)
            or (
                accepted_move
                and exit_hashes["coordinate_sha256"]
                == entry_hashes["coordinate_sha256"]
                and exit_hashes["direction_field_sha256"]
                == entry_hashes["direction_field_sha256"]
            )
        ):
            raise CoarseScheduleContinuationError(
                "one continuation triple step/pass transition is invalid"
            )
        running_index = record_end
        previous_hashes = exit_hashes
        observed_triple_count += quality_count
        rejected_count += rejected_delta
        accepted_count += int(accepted_move)
    if (
        observed_triple_count != triple_count
        or running_index != triple_end_inclusive
        or summary.get("accepted_move_count") != accepted_count
        or summary.get("rejected_margin_candidate_count") != rejected_count
    ):
        raise CoarseScheduleContinuationError(
            "continuation triple step/pass totals are inconsistent"
        )

    final_fields = {
        "selected_component_sha256",
        "selected_state_coordinate_sha256",
        "selected_state_quality",
        "selected_state_quality_sha256",
        "selected_state_objective",
        "selected_state_objective_sha256",
        "selected_direction_state_sha256",
        "selected_direction_field_sha256",
        "triple_quality_evaluation_end_index_inclusive",
    }
    if not isinstance(final_state, Mapping) or set(final_state) != final_fields:
        raise CoarseScheduleContinuationError(
            "continuation triple final state is incomplete"
        )
    final_quality = _validate_quality_snapshot(
        final_state["selected_state_quality"],
        label="continuation triple final component quality",
        expected_endpoint=endpoint,
        expected_prism_element_count=None,
        expected_core_element_count=None,
    )
    final_quality_sha256 = _sha256(
        final_state["selected_state_quality_sha256"],
        "continuation triple final component quality SHA-256",
    )
    _final_objective, final_objective_sha256 = _validate_objective_snapshot(
        final_state["selected_state_objective"],
        configured_sha256=final_state[
            "selected_state_objective_sha256"
        ],
        quality=final_quality,
        label="continuation triple final component objective",
    )
    final_hashes = {
        "coordinate_sha256": _sha256(
            final_state["selected_state_coordinate_sha256"],
            "continuation triple final coordinate SHA-256",
        ),
        "quality_sha256": final_quality_sha256,
        "objective_sha256": final_objective_sha256,
        "direction_state_sha256": _sha256(
            final_state["selected_direction_state_sha256"],
            "continuation triple final direction-state SHA-256",
        ),
        "direction_field_sha256": _sha256(
            final_state["selected_direction_field_sha256"],
            "continuation triple final direction-field SHA-256",
        ),
    }
    expected_final_objective_sha256 = _canonical_sha256(
        {
            "quality_objective_fields": list(
                _FRONTIER_PATTERN_QUALITY_OBJECTIVE
            ),
            "objective": selected_evidence["record"]["selected_objective"],
        }
    )
    if (
        final_state.get("selected_component_sha256") != selected_id
        or final_state.get(
            "triple_quality_evaluation_end_index_inclusive"
        )
        != triple_end_inclusive
        or final_quality_sha256 != final_quality["quality_sha256"]
        or final_hashes != previous_hashes
        or final_hashes["coordinate_sha256"]
        != selected_evidence["search_exit_coordinate_sha256"]
        or final_hashes["quality_sha256"]
        != selected_evidence["search_exit_quality_sha256"]
        or final_hashes["objective_sha256"]
        != expected_final_objective_sha256
    ):
        raise CoarseScheduleContinuationError(
            "continuation triple final state diverges from component exit"
        )

    component_evaluation_total = sum(
        _positive_int(
            record["quality_evaluation_count"],
            "continuation component evaluation count",
        )
        for record in pattern_search["component_records"]
    )
    if component_evaluation_total != static_count + fine_count + triple_count:
        raise CoarseScheduleContinuationError(
            "continuation component evaluations omit or duplicate a phase"
        )


def _canonical_nonnegative_float_hex(value: Any, label: str) -> float:
    try:
        decoded = float.fromhex(str(value))
    except ValueError as error:
        raise CoarseScheduleContinuationError(
            f"{label} is not canonical binary64 hex"
        ) from error
    if (
        not math.isfinite(decoded)
        or decoded < 0.0
        or decoded.hex() != value
    ):
        raise CoarseScheduleContinuationError(
            f"{label} is not canonical non-negative binary64 hex"
        )
    return decoded


def _stable_sha256_list(value: Any, label: str, *, nonempty: bool) -> list[str]:
    if (
        not isinstance(value, list)
        or (nonempty and not value)
        or value != sorted(set(value))
        or any(_SHA256.fullmatch(str(item)) is None for item in value)
    ):
        raise CoarseScheduleContinuationError(
            f"{label} must be a sorted unique stable SHA-256 list"
        )
    return [str(item) for item in value]


def _validate_collar_low_quality_aggregate(
    value: Any,
    *,
    expected_bad_count: int,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CoarseScheduleContinuationError(f"{label} must be an object")
    unsigned = dict(value)
    digest = _sha256(
        unsigned.pop("aggregate_sha256", ""), f"{label} SHA-256"
    )
    if (
        _canonical_sha256(unsigned) != digest
        or _nonnegative_int(value.get("count"), f"{label} count")
        != expected_bad_count
        or _nonnegative_int(
            value.get("unique_source_triangle_count"),
            f"{label} source-triangle count",
        )
        > expected_bad_count
        or _nonnegative_int(value.get("wall_count"), f"{label} wall count")
        > value.get("unique_source_triangle_count")
    ):
        raise CoarseScheduleContinuationError(
            f"{label} is stale or inconsistent"
        )
    return copy.deepcopy(dict(value))


def _validate_collar_graph(
    value: Any,
    *,
    gate: Mapping[str, Any],
    collar_endpoint: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, int]]:
    graph_fields = {
        "scope",
        "wall_surface_fingerprint",
        "source_triangle_count",
        "root_count",
        "edge_count",
        "seed_root_coordinate_sha256",
        "root_coordinate_sha256",
        "source_triangle_sha256",
        "triangle_records",
        "edge_records",
        "distance_records",
        "graph_sha256",
        "distances_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != graph_fields:
        raise CoarseScheduleContinuationError(
            "continuation collar stable graph has an incomplete field set"
        )
    roots = _stable_sha256_list(
        value["root_coordinate_sha256"],
        "continuation collar graph roots",
        nonempty=True,
    )
    seeds = _stable_sha256_list(
        value["seed_root_coordinate_sha256"],
        "continuation collar graph seed roots",
        nonempty=True,
    )
    triangles = _stable_sha256_list(
        value["source_triangle_sha256"],
        "continuation collar graph source triangles",
        nonempty=True,
    )
    triangle_records = value["triangle_records"]
    if not isinstance(triangle_records, list) or not triangle_records:
        raise CoarseScheduleContinuationError(
            "continuation collar graph triangle records are incomplete"
        )
    normalized_triangle_records: list[dict[str, Any]] = []
    derived_roots: set[str] = set()
    derived_edges: set[tuple[str, str]] = set()
    for record in triangle_records:
        if not isinstance(record, Mapping) or set(record) != {
            "source_triangle_sha256",
            "wall_surface_fingerprint",
            "root_coordinate_sha256",
        }:
            raise CoarseScheduleContinuationError(
                "one continuation collar graph triangle is incomplete"
            )
        triangle = _sha256(
            record["source_triangle_sha256"],
            "continuation collar graph triangle SHA-256",
        )
        wall = _sha256(
            record["wall_surface_fingerprint"],
            "continuation collar graph triangle wall fingerprint",
        )
        triangle_roots = _stable_sha256_list(
            record["root_coordinate_sha256"],
            "continuation collar graph triangle roots",
            nonempty=True,
        )
        if len(triangle_roots) != 3 or wall != gate["target_wall_surface_fingerprint"]:
            raise CoarseScheduleContinuationError(
                "one continuation collar graph triangle is outside the target wall"
            )
        normalized_triangle_records.append(
            {
                "source_triangle_sha256": triangle,
                "wall_surface_fingerprint": wall,
                "root_coordinate_sha256": triangle_roots,
            }
        )
        derived_roots.update(triangle_roots)
        derived_edges.update(
            tuple(sorted((triangle_roots[left], triangle_roots[right])))
            for left, right in ((0, 1), (0, 2), (1, 2))
        )
    expected_triangle_records = sorted(
        normalized_triangle_records,
        key=lambda item: (
            item["source_triangle_sha256"],
            item["wall_surface_fingerprint"],
            item["root_coordinate_sha256"],
        ),
    )
    if (
        triangle_records != expected_triangle_records
        or len({item["source_triangle_sha256"] for item in triangle_records})
        != len(triangle_records)
        or sorted(derived_roots) != roots
        or [item["source_triangle_sha256"] for item in triangle_records]
        != triangles
    ):
        raise CoarseScheduleContinuationError(
            "continuation collar graph roots/triangles are not derived from triangle records"
        )
    edge_records = value["edge_records"]
    if not isinstance(edge_records, list):
        raise CoarseScheduleContinuationError(
            "continuation collar graph edge records must be a list"
        )
    edges: list[tuple[str, str]] = []
    edge_hashes: list[str] = []
    for record in edge_records:
        if not isinstance(record, Mapping) or set(record) != {
            "edge_sha256",
            "root_coordinate_sha256",
        }:
            raise CoarseScheduleContinuationError(
                "one continuation collar graph edge is incomplete"
            )
        edge_roots = _stable_sha256_list(
            record["root_coordinate_sha256"],
            "continuation collar graph edge roots",
            nonempty=True,
        )
        edge_digest = _sha256(
            record["edge_sha256"], "continuation collar graph edge SHA-256"
        )
        if (
            len(edge_roots) != 2
            or not set(edge_roots) <= set(roots)
            or edge_digest
            != _canonical_sha256({"root_coordinate_sha256": edge_roots})
        ):
            raise CoarseScheduleContinuationError(
                "one continuation collar graph edge is stale"
            )
        edges.append((edge_roots[0], edge_roots[1]))
        edge_hashes.append(edge_digest)
    if edge_hashes != sorted(set(edge_hashes)):
        raise CoarseScheduleContinuationError(
            "continuation collar graph edges are duplicated or unordered"
        )
    expected_edge_records = []
    for edge_roots in sorted(derived_edges):
        unsigned_edge = {"root_coordinate_sha256": list(edge_roots)}
        expected_edge_records.append(
            {
                "edge_sha256": _canonical_sha256(unsigned_edge),
                **unsigned_edge,
            }
        )
    expected_edge_records.sort(key=lambda item: item["edge_sha256"])
    if edge_records != expected_edge_records:
        raise CoarseScheduleContinuationError(
            "continuation collar graph edges are not triangle-derived"
        )

    distance_records = value["distance_records"]
    if not isinstance(distance_records, list) or not distance_records:
        raise CoarseScheduleContinuationError(
            "continuation collar graph distances are incomplete"
        )
    distance_by_root: dict[str, int] = {}
    for expected_distance, record in enumerate(distance_records):
        if not isinstance(record, Mapping) or set(record) != {
            "distance",
            "root_coordinate_sha256",
        }:
            raise CoarseScheduleContinuationError(
                "one continuation collar graph distance record is incomplete"
            )
        distance = _nonnegative_int(
            record["distance"], "continuation collar graph distance"
        )
        distance_roots = _stable_sha256_list(
            record["root_coordinate_sha256"],
            "continuation collar graph distance roots",
            nonempty=True,
        )
        if distance != expected_distance:
            raise CoarseScheduleContinuationError(
                "continuation collar graph distances are not contiguous"
            )
        for root in distance_roots:
            if root in distance_by_root:
                raise CoarseScheduleContinuationError(
                    "continuation collar graph distance roots are duplicated"
                )
            distance_by_root[root] = distance
    if (
        sorted(distance_by_root) != roots
        or sorted(root for root, distance in distance_by_root.items() if distance == 0)
        != seeds
        or any(abs(distance_by_root[left] - distance_by_root[right]) > 1
               for left, right in edges)
        or any(
            distance > 0
            and not any(
                root in edge
                and distance_by_root[edge[0] if edge[1] == root else edge[1]]
                == distance - 1
                for edge in edges
                if root in edge
            )
            for root, distance in distance_by_root.items()
        )
    ):
        raise CoarseScheduleContinuationError(
            "continuation collar graph distances do not describe a stable BFS"
        )
    graph_digest = _sha256(
        value["graph_sha256"], "continuation collar stable graph SHA-256"
    )
    distances_digest = _sha256(
        value["distances_sha256"],
        "continuation collar stable graph distances SHA-256",
    )
    expected_graph_digest = _canonical_sha256(
        {
            "scope": value["scope"],
            "wall_surface_fingerprint": value[
                "wall_surface_fingerprint"
            ],
            "root_coordinate_sha256": roots,
            "source_triangle_sha256": triangles,
            "triangle_records": triangle_records,
            "edge_records": edge_records,
        }
    )
    if (
        value.get("scope") != collar_endpoint["stable_root_graph_scope"]
        or value.get("wall_surface_fingerprint")
        != gate["target_wall_surface_fingerprint"]
        or value.get("source_triangle_count") != len(triangles)
        or value.get("root_count") != len(roots)
        or value.get("edge_count") != len(edges)
        or seeds != gate["seed_root_coordinate_sha256"]
        or not set(seeds) <= set(roots)
        or gate["target_source_triangle_sha256"] not in triangles
        or graph_digest != expected_graph_digest
        or distances_digest != _canonical_sha256(distance_records)
    ):
        raise CoarseScheduleContinuationError(
            "continuation collar stable graph differs from the frozen gate"
        )
    return copy.deepcopy(dict(value)), distance_by_root


def _validate_collar_bad_records(
    value: Any,
    *,
    configured_sha256: Any,
    label: str,
) -> tuple[list[dict[str, Any]], str]:
    if not isinstance(value, list):
        raise CoarseScheduleContinuationError(f"{label} must be a list")
    normalized: list[dict[str, Any]] = []
    for record in value:
        fields = {
            "prism_element_sha256",
            "source_triangle_sha256",
            "wall_surface_fingerprint",
            "root_coordinate_sha256",
            "layer_index_1_based",
        }
        if not isinstance(record, Mapping) or set(record) != fields:
            raise CoarseScheduleContinuationError(
                f"one {label} record is incomplete"
            )
        roots = _stable_sha256_list(
            record["root_coordinate_sha256"],
            f"{label} roots",
            nonempty=True,
        )
        unsigned = {
            "source_triangle_sha256": _sha256(
                record["source_triangle_sha256"], f"{label} triangle"
            ),
            "wall_surface_fingerprint": _sha256(
                record["wall_surface_fingerprint"], f"{label} wall"
            ),
            "root_coordinate_sha256": roots,
            "layer_index_1_based": _positive_int(
                record["layer_index_1_based"], f"{label} layer"
            ),
        }
        prism_digest = _sha256(
            record["prism_element_sha256"], f"{label} Prism SHA-256"
        )
        if len(roots) != 3 or prism_digest != _canonical_sha256(unsigned):
            raise CoarseScheduleContinuationError(
                f"one {label} Prism identity is stale"
            )
        normalized.append(
            {"prism_element_sha256": prism_digest, **unsigned}
        )
    normalized.sort(key=lambda item: item["prism_element_sha256"])
    digest = _sha256(configured_sha256, f"{label} records SHA-256")
    if (
        normalized != value
        or len({item["prism_element_sha256"] for item in normalized})
        != len(normalized)
        or digest != _canonical_sha256(normalized)
    ):
        raise CoarseScheduleContinuationError(f"{label} records are stale")
    return copy.deepcopy(normalized), digest


def _validate_collar_triangle_records(
    value: Any,
    *,
    configured_sha256: Any,
    label: str,
) -> tuple[list[dict[str, Any]], str]:
    if not isinstance(value, list) or not value:
        raise CoarseScheduleContinuationError(f"{label} must be non-empty")
    normalized: list[dict[str, Any]] = []
    for record in value:
        if not isinstance(record, Mapping) or set(record) != {
            "source_triangle_sha256",
            "wall_surface_fingerprint",
            "root_coordinate_sha256",
        }:
            raise CoarseScheduleContinuationError(
                f"one {label} record is incomplete"
            )
        roots = _stable_sha256_list(
            record["root_coordinate_sha256"], f"{label} roots", nonempty=True
        )
        if len(roots) != 3:
            raise CoarseScheduleContinuationError(
                f"one {label} record is not a triangle"
            )
        normalized.append(
            {
                "source_triangle_sha256": _sha256(
                    record["source_triangle_sha256"], f"{label} triangle"
                ),
                "wall_surface_fingerprint": _sha256(
                    record["wall_surface_fingerprint"], f"{label} wall"
                ),
                "root_coordinate_sha256": roots,
            }
        )
    normalized.sort(
        key=lambda item: (
            item["source_triangle_sha256"],
            item["wall_surface_fingerprint"],
            item["root_coordinate_sha256"],
        )
    )
    digest = _sha256(configured_sha256, f"{label} records SHA-256")
    if (
        normalized != value
        or len({item["source_triangle_sha256"] for item in normalized})
        != len(normalized)
        or digest != _canonical_sha256(normalized)
    ):
        raise CoarseScheduleContinuationError(f"{label} records are stale")
    return copy.deepcopy(normalized), digest


def _validate_collar_chain_node_records(
    value: Any,
    *,
    configured_sha256: Any,
    label: str,
) -> tuple[list[dict[str, Any]], str]:
    if not isinstance(value, list) or not value:
        raise CoarseScheduleContinuationError(f"{label} must be non-empty")
    normalized: list[dict[str, Any]] = []
    for record in value:
        if not isinstance(record, Mapping) or set(record) != {
            "chain_node_sha256",
            "root_coordinate_sha256",
            "layer_index_1_based",
        }:
            raise CoarseScheduleContinuationError(
                f"one {label} record is incomplete"
            )
        unsigned = {
            "root_coordinate_sha256": _sha256(
                record["root_coordinate_sha256"], f"{label} root"
            ),
            "layer_index_1_based": _positive_int(
                record["layer_index_1_based"], f"{label} layer"
            ),
        }
        node_digest = _sha256(
            record["chain_node_sha256"], f"{label} node SHA-256"
        )
        if node_digest != _canonical_sha256(unsigned):
            raise CoarseScheduleContinuationError(
                f"one {label} node identity is stale"
            )
        normalized.append({"chain_node_sha256": node_digest, **unsigned})
    normalized.sort(key=lambda item: item["chain_node_sha256"])
    digest = _sha256(configured_sha256, f"{label} records SHA-256")
    if (
        normalized != value
        or len({item["chain_node_sha256"] for item in normalized})
        != len(normalized)
        or digest != _canonical_sha256(normalized)
    ):
        raise CoarseScheduleContinuationError(f"{label} records are stale")
    return copy.deepcopy(normalized), digest


def _validate_collar_core_records(
    value: Any,
    *,
    configured_sha256: Any,
    label: str,
) -> tuple[list[dict[str, Any]], str]:
    if not isinstance(value, list) or not value:
        raise CoarseScheduleContinuationError(f"{label} must be non-empty")
    normalized: list[dict[str, Any]] = []
    for record in value:
        if not isinstance(record, Mapping) or set(record) != {
            "core_element_sha256",
            "element_type",
            "node_sha256",
        }:
            raise CoarseScheduleContinuationError(
                f"one {label} record is incomplete"
            )
        nodes = _stable_sha256_list(
            record["node_sha256"], f"{label} nodes", nonempty=True
        )
        unsigned = {"element_type": record["element_type"], "node_sha256": nodes}
        core_digest = _sha256(
            record["core_element_sha256"], f"{label} element SHA-256"
        )
        if (
            record["element_type"] != "Tetrahedron 4"
            or len(nodes) != 4
            or core_digest != _canonical_sha256(unsigned)
        ):
            raise CoarseScheduleContinuationError(
                f"one {label} element identity is stale"
            )
        normalized.append({"core_element_sha256": core_digest, **unsigned})
    normalized.sort(key=lambda item: item["core_element_sha256"])
    digest = _sha256(configured_sha256, f"{label} records SHA-256")
    if (
        normalized != value
        or len({item["core_element_sha256"] for item in normalized})
        != len(normalized)
        or digest != _canonical_sha256(normalized)
    ):
        raise CoarseScheduleContinuationError(f"{label} records are stale")
    return copy.deepcopy(normalized), digest


def _cumulative_float_hex(
    value: Any,
    *,
    layer_count: int,
    label: str,
) -> tuple[list[str], list[float]]:
    if not isinstance(value, list) or len(value) != layer_count:
        raise CoarseScheduleContinuationError(
            f"{label} must contain the frozen layer count"
        )
    try:
        decoded = [float.fromhex(str(item)) for item in value]
    except ValueError as error:
        raise CoarseScheduleContinuationError(
            f"{label} is not canonical binary64 hex"
        ) from error
    if (
        any(not math.isfinite(item) or item <= 0.0 for item in decoded)
        or [item.hex() for item in decoded] != value
        or any(right <= left for left, right in zip(decoded, decoded[1:]))
    ):
        raise CoarseScheduleContinuationError(
            f"{label} is not a strictly increasing finite schedule"
        )
    return list(value), decoded


def _validate_collar_refinement_selection(
    value: Any,
    *,
    endpoint: Mapping[str, Any],
    collar_endpoint: Mapping[str, Any],
    pattern_search: Mapping[str, Any],
    triple_selection: Mapping[str, Any],
    stable_component_records: list[Mapping[str, Any]],
    pattern_quality_evaluation_count: int,
    frontier_target_schedule_sha256: str,
    expected_prism_element_count: int,
    expected_core_element_count: int,
) -> dict[str, Any]:
    """Validate the isolated v5 collar evidence after the complete v4 search."""

    fields = {
        "schema",
        "status",
        "gate",
        "handoff",
        "schedule",
        "graph",
        "ring_widths",
        "candidate_records",
        "evaluation_evidence",
        "selection",
        "final_state",
        "preservation",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise CoarseScheduleContinuationError(
            "continuation collar selection has an incomplete field set"
        )
    if value.get("schema") != _COLLAR_REFINEMENT_SELECTION_SCHEMA:
        raise CoarseScheduleContinuationError(
            "continuation collar selection schema is stale"
        )
    ring_widths = value.get("ring_widths")
    candidate_records = value.get("candidate_records")
    if (
        ring_widths != list(_COLLAR_RING_WIDTHS)
        or ring_widths != collar_endpoint["collar_ring_widths"]
        or not isinstance(candidate_records, list)
    ):
        raise CoarseScheduleContinuationError(
            "continuation collar ring schedule differs from its child"
        )

    gate_fields = {
        "eligible",
        "reason",
        "triple_status",
        "triple_accepted_move_count",
        "residual_component_count",
        "residual_source_triangle_count",
        "residual_root_count",
        "residual_wall_count",
        "residual_prism_count",
        "residual_bad_records",
        "residual_bad_records_sha256",
        "minimum_bad_layer_1_based",
        "maximum_bad_layer_1_based",
        "outer_tail_contiguous",
        "layer_count",
        "frozen_prefix_last_layer_1_based",
        "minimum_frozen_prefix_layer_count",
        "core_tetra_below_gamma_count",
        "nonpositive_element_count",
        "nonfinite_count",
        "target_wall_surface_fingerprint",
        "target_source_triangle_sha256",
        "seed_root_coordinate_sha256",
    }
    gate = value.get("gate")
    if not isinstance(gate, Mapping) or set(gate) != gate_fields:
        raise CoarseScheduleContinuationError(
            "continuation collar gate has an incomplete field set"
        )
    if not isinstance(gate.get("eligible"), bool) or not isinstance(
        gate.get("outer_tail_contiguous"), bool
    ):
        raise CoarseScheduleContinuationError(
            "continuation collar gate flags are invalid"
        )
    count_fields = (
        "triple_accepted_move_count",
        "residual_component_count",
        "residual_source_triangle_count",
        "residual_root_count",
        "residual_wall_count",
        "residual_prism_count",
        "core_tetra_below_gamma_count",
        "nonpositive_element_count",
        "nonfinite_count",
    )
    gate_counts = {
        field: _nonnegative_int(gate[field], f"continuation collar gate {field}")
        for field in count_fields
    }
    residual_bad_records, _residual_bad_records_sha256 = (
        _validate_collar_bad_records(
            gate["residual_bad_records"],
            configured_sha256=gate["residual_bad_records_sha256"],
            label="continuation collar residual bad lineage",
        )
    )
    if (
        gate.get("triple_status") != pattern_search.get("status")
        or gate_counts["triple_accepted_move_count"]
        != pattern_search.get("triple_pattern_refinement", {}).get(
            "accepted_move_count"
        )
        or gate.get("layer_count") != endpoint["layer_count"]
        or gate.get("minimum_frozen_prefix_layer_count")
        != collar_endpoint["minimum_frozen_inner_layer_count"]
    ):
        raise CoarseScheduleContinuationError(
            "continuation collar gate diverges from the completed triple search"
        )

    minimum_bad = gate.get("minimum_bad_layer_1_based")
    maximum_bad = gate.get("maximum_bad_layer_1_based")
    frozen_prefix = gate.get("frozen_prefix_last_layer_1_based")
    target_wall = gate.get("target_wall_surface_fingerprint")
    target_triangle = gate.get("target_source_triangle_sha256")
    seed_roots_raw = gate.get("seed_root_coordinate_sha256")
    selected_component = triple_selection.get("selected_component")
    triple_final = triple_selection.get("final_state")
    selected_roots: list[str] = []
    selected_triangles: list[str] = []
    selected_walls: list[str] = []
    if isinstance(selected_component, Mapping):
        selected_roots = _stable_sha256_list(
            selected_component.get("root_coordinate_sha256"),
            "continuation collar selected roots",
            nonempty=True,
        )
        selected_triangles = _stable_sha256_list(
            selected_component.get("source_triangle_sha256"),
            "continuation collar selected source triangles",
            nonempty=True,
        )
        matching_stable = [
            record
            for record in stable_component_records
            if set(selected_roots)
            <= set(record.get("root_coordinate_sha256", []))
            and set(selected_triangles)
            <= set(record.get("source_triangle_sha256", []))
        ]
        if len(matching_stable) != 1:
            raise CoarseScheduleContinuationError(
                "continuation collar target has no unique stable component"
            )
        selected_walls = list(
            matching_stable[0]["wall_surface_fingerprints"]
        )
    if seed_roots_raw is None:
        seed_roots: list[str] = []
    else:
        seed_roots = _stable_sha256_list(
            seed_roots_raw,
            "continuation collar gate seed roots",
            nonempty=True,
        )
    triple_gate_quality: dict[str, Any] | None = None
    if isinstance(triple_final, Mapping):
        triple_gate_quality = _validate_quality_snapshot(
            triple_final.get("selected_state_quality"),
            label="continuation collar gate triple-final quality",
            expected_endpoint=endpoint,
            expected_prism_element_count=None,
            expected_core_element_count=None,
        )
        if (
            gate_counts["residual_prism_count"]
            != triple_gate_quality["prism_below_threshold_element_count"]
            or gate_counts["core_tetra_below_gamma_count"]
            != triple_gate_quality["core_tetra_below_gamma_count"]
            or gate_counts["nonpositive_element_count"]
            != triple_gate_quality["nonpositive_element_count"]
            or gate_counts["nonfinite_count"]
            != triple_gate_quality["nonfinite_count"]
        ):
            raise CoarseScheduleContinuationError(
                "continuation collar gate is not derived from triple-final quality"
            )
    for item, label in (
        (target_wall, "continuation collar target wall fingerprint"),
        (target_triangle, "continuation collar target triangle SHA-256"),
    ):
        if item is not None:
            _sha256(item, label)

    has_residual = gate_counts["residual_prism_count"] > 0
    if has_residual:
        if any(type(item) is not int for item in (minimum_bad, maximum_bad, frozen_prefix)):
            raise CoarseScheduleContinuationError(
                "continuation collar residual layer indices are invalid"
            )
        minimum_bad = _positive_int(
            minimum_bad, "continuation collar minimum bad layer"
        )
        maximum_bad = _positive_int(
            maximum_bad, "continuation collar maximum bad layer"
        )
        frozen_prefix = _nonnegative_int(
            frozen_prefix, "continuation collar frozen prefix"
        )
        if frozen_prefix != minimum_bad - 1:
            raise CoarseScheduleContinuationError(
                "continuation collar K does not equal min_bad_layer-1"
            )
    elif any(
        item is not None
        for item in (
            minimum_bad,
            maximum_bad,
            frozen_prefix,
            target_wall,
            target_triangle,
            seed_roots_raw,
        )
    ):
        raise CoarseScheduleContinuationError(
            "continuation collar empty residual carries fabricated scope"
        )

    residual_shape_valid = (
        gate_counts["residual_component_count"] == 1
        and gate_counts["residual_source_triangle_count"]
        == collar_endpoint["collar_component_source_triangle_count"]
        and gate_counts["residual_root_count"]
        == collar_endpoint["collar_component_root_count"]
        and gate_counts["residual_wall_count"] == 1
        and has_residual
        and len(selected_roots) == gate_counts["residual_root_count"]
        and len(selected_triangles)
        == gate_counts["residual_source_triangle_count"]
        and len(selected_walls) == gate_counts["residual_wall_count"]
        and seed_roots == selected_roots
        and target_triangle == selected_triangles[0]
        and target_wall == selected_walls[0]
        and len(residual_bad_records)
        == gate_counts["residual_prism_count"]
        and {
            item["source_triangle_sha256"] for item in residual_bad_records
        }
        == set(selected_triangles)
        and {
            item["wall_surface_fingerprint"] for item in residual_bad_records
        }
        == set(selected_walls)
        and all(
            item["root_coordinate_sha256"] == selected_roots
            for item in residual_bad_records
        )
    )
    quality_gate_valid = (
        gate_counts["core_tetra_below_gamma_count"] == 0
        and gate_counts["nonpositive_element_count"] == 0
        and gate_counts["nonfinite_count"] == 0
    )
    tail_valid = (
        has_residual
        and gate.get("outer_tail_contiguous") is True
        and maximum_bad == endpoint["layer_count"]
        and gate_counts["residual_prism_count"]
        == maximum_bad - minimum_bad + 1
        and sorted(
            item["layer_index_1_based"] for item in residual_bad_records
        )
        == list(range(minimum_bad, maximum_bad + 1))
    )
    prefix_valid = (
        has_residual
        and frozen_prefix
        >= collar_endpoint["minimum_frozen_inner_layer_count"]
        and frozen_prefix < endpoint["layer_count"]
    )
    triple_exhausted = pattern_search.get("status") == "EXHAUSTED"
    expected_eligible = (
        triple_exhausted
        and residual_shape_valid
        and quality_gate_valid
        and tail_valid
        and prefix_valid
    )
    if pattern_search.get("status") == "PASS":
        expected_reason = "TRIPLE_ALREADY_PASS"
    elif not quality_gate_valid:
        expected_reason = "RESIDUAL_QUALITY_GATE_FAILED"
    elif not triple_exhausted or not residual_shape_valid:
        expected_reason = "RESIDUAL_SCOPE_NOT_ELIGIBLE"
    elif not tail_valid:
        expected_reason = "RESIDUAL_NOT_CONTIGUOUS_OUTER_TAIL"
    elif not prefix_valid:
        expected_reason = "FROZEN_PREFIX_TOO_SHORT"
    else:
        expected_reason = "ELIGIBLE_CONTIGUOUS_OUTER_TAIL"
    if (
        gate["eligible"] is not expected_eligible
        or gate.get("reason") != expected_reason
    ):
        raise CoarseScheduleContinuationError(
            "continuation collar eligibility or fail-closed reason is stale"
        )

    evaluation_fields = {
        "pattern_quality_evaluation_count",
        "collar_quality_evaluation_start_index_exclusive",
        "collar_quality_evaluation_count",
        "collar_quality_evaluation_end_index_inclusive",
        "maximum_collar_quality_evaluations",
        "total_quality_evaluation_count",
        "maximum_total_quality_evaluations",
        "within_cap",
    }
    evaluations = value.get("evaluation_evidence")
    if not isinstance(evaluations, Mapping) or set(evaluations) != evaluation_fields:
        raise CoarseScheduleContinuationError(
            "continuation collar evaluation evidence is incomplete"
        )
    collar_count = _nonnegative_int(
        evaluations["collar_quality_evaluation_count"],
        "continuation collar evaluation count",
    )
    total_count = _nonnegative_int(
        evaluations["total_quality_evaluation_count"],
        "continuation total evaluation count",
    )
    proof_scope = endpoint["quality_evaluation_budget_proof"]["actual_scope"]
    if (
        evaluations.get("pattern_quality_evaluation_count")
        != pattern_quality_evaluation_count
        or evaluations.get("collar_quality_evaluation_start_index_exclusive")
        != pattern_quality_evaluation_count
        or evaluations.get("collar_quality_evaluation_end_index_inclusive")
        != pattern_quality_evaluation_count + collar_count
        or total_count != pattern_quality_evaluation_count + collar_count
        or evaluations.get("maximum_collar_quality_evaluations")
        != proof_scope["collar_refinement_quality_evaluation_upper_bound"]
        or evaluations.get("maximum_total_quality_evaluations")
        != endpoint["caps"]["maximum_quality_evaluations"]
        or evaluations.get("within_cap") is not True
        or total_count > proof_scope["quality_evaluation_upper_bound"]
        or total_count > endpoint["caps"]["maximum_quality_evaluations"]
    ):
        raise CoarseScheduleContinuationError(
            "continuation collar evaluation indices or budget are stale"
        )

    if not expected_eligible:
        if (
            value.get("status") != "SKIPPED"
            or candidate_records
            or collar_count != 0
            or any(
                value.get(field) is not None
                for field in (
                    "handoff",
                    "schedule",
                    "graph",
                    "selection",
                    "final_state",
                    "preservation",
                )
            )
        ):
            raise CoarseScheduleContinuationError(
                "continuation collar ran despite a closed eligibility gate"
            )
        return {
            "status": "SKIPPED",
            "quality_evaluation_count": 0,
            "total_quality_evaluation_count": total_count,
            "selected_record": None,
            "final_state": None,
        }

    if value.get("status") not in {"PASS", "EXHAUSTED"}:
        raise CoarseScheduleContinuationError(
            "eligible continuation collar has an invalid status"
        )
    if collar_count != len(_COLLAR_RING_WIDTHS) or len(candidate_records) != collar_count:
        raise CoarseScheduleContinuationError(
            "eligible continuation collar did not evaluate all seven rings"
        )

    handoff_fields = {
        "without_reset_from_triple_final",
        "entry_schedule_sha256",
        "entry_direction_field_sha256",
        "entry_coordinate_sha256",
        "entry_quality_sha256",
        "entry_objective",
        "entry_objective_sha256",
    }
    handoff = value.get("handoff")
    if (
        not isinstance(handoff, Mapping)
        or set(handoff) != handoff_fields
        or handoff.get("without_reset_from_triple_final") is not True
        or not isinstance(triple_final, Mapping)
    ):
        raise CoarseScheduleContinuationError(
            "continuation collar triple handoff is incomplete"
        )
    triple_quality = _validate_quality_snapshot(
        triple_final["selected_state_quality"],
        label="continuation collar triple-entry quality",
        expected_endpoint=endpoint,
        expected_prism_element_count=None,
        expected_core_element_count=None,
    )
    _entry_objective, entry_objective_sha256 = _validate_objective_snapshot(
        handoff["entry_objective"],
        configured_sha256=handoff["entry_objective_sha256"],
        quality=triple_quality,
        label="continuation collar triple-entry objective",
    )
    entry_hashes = {
        field: _sha256(
            handoff[field], f"continuation collar handoff {field}"
        )
        for field in (
            "entry_schedule_sha256",
            "entry_direction_field_sha256",
            "entry_coordinate_sha256",
            "entry_quality_sha256",
            "entry_objective_sha256",
        )
    }
    if (
        entry_hashes["entry_direction_field_sha256"]
        != triple_final["selected_direction_field_sha256"]
        or entry_hashes["entry_coordinate_sha256"]
        != triple_final["selected_state_coordinate_sha256"]
        or entry_hashes["entry_quality_sha256"]
        != triple_quality["quality_sha256"]
        or entry_hashes["entry_objective_sha256"]
        != entry_objective_sha256
        or handoff["entry_objective"]
        != triple_final["selected_state_objective"]
        or entry_hashes["entry_schedule_sha256"]
        != frontier_target_schedule_sha256
    ):
        raise CoarseScheduleContinuationError(
            "continuation collar did not start from the triple-final state"
        )

    schedule_fields = {
        "formula",
        "minimum_growth_formula",
        "layer_count",
        "first_layer_height_float_hex",
        "frozen_prefix_last_layer_1_based",
        "frozen_prefix_preserved",
        "directions_frozen",
        "connectivity_frozen",
        "no_amplification",
        "strictly_increasing",
    }
    schedule = value.get("schedule")
    if not isinstance(schedule, Mapping) or set(schedule) != schedule_fields:
        raise CoarseScheduleContinuationError(
            "continuation collar schedule evidence is incomplete"
        )
    try:
        first_height = float.fromhex(str(schedule["first_layer_height_float_hex"]))
    except ValueError as error:
        raise CoarseScheduleContinuationError(
            "continuation collar first layer is not binary64 hex"
        ) from error
    if (
        schedule.get("formula")
        != collar_endpoint["outer_tail_cumulative_formula"]
        or schedule.get("minimum_growth_formula")
        != collar_endpoint["minimum_growth_cumulative_formula"]
        or schedule.get("layer_count") != endpoint["layer_count"]
        or schedule.get("frozen_prefix_last_layer_1_based") != frozen_prefix
        or first_height.hex() != schedule["first_layer_height_float_hex"]
        or first_height.hex() != float(endpoint["first_layer_height_m"]).hex()
        or any(
            schedule.get(field) is not True
            for field in (
                "frozen_prefix_preserved",
                "directions_frozen",
                "connectivity_frozen",
                "no_amplification",
                "strictly_increasing",
            )
        )
    ):
        raise CoarseScheduleContinuationError(
            "continuation collar schedule violates the frozen child contract"
        )

    _graph, distance_by_root = _validate_collar_graph(
        value.get("graph"), gate=gate, collar_endpoint=collar_endpoint
    )

    preservation_fields = {
        "entry_inner_prefix_coordinate_sha256",
        "final_inner_prefix_coordinate_sha256",
        "inner_prefix_reproducible",
        "entry_non_target_coordinate_sha256",
        "final_non_target_coordinate_sha256",
        "non_target_reproducible",
        "entry_connectivity_sha256",
        "final_connectivity_sha256",
        "connectivity_reproducible",
    }
    preservation = value.get("preservation")
    if not isinstance(preservation, Mapping) or set(preservation) != preservation_fields:
        raise CoarseScheduleContinuationError(
            "continuation collar preservation evidence is incomplete"
        )
    preservation_hashes = {
        field: _sha256(
            preservation[field], f"continuation collar preservation {field}"
        )
        for field in preservation_fields
        if field.endswith("_sha256")
    }
    if (
        preservation_hashes["entry_inner_prefix_coordinate_sha256"]
        != preservation_hashes["final_inner_prefix_coordinate_sha256"]
        or preservation_hashes["entry_non_target_coordinate_sha256"]
        != preservation_hashes["final_non_target_coordinate_sha256"]
        or preservation_hashes["entry_connectivity_sha256"]
        != preservation_hashes["final_connectivity_sha256"]
        or any(
            preservation.get(field) is not True
            for field in (
                "inner_prefix_reproducible",
                "non_target_reproducible",
                "connectivity_reproducible",
            )
        )
    ):
        raise CoarseScheduleContinuationError(
            "continuation collar changed frozen coordinates or connectivity"
        )

    candidate_fields = {
        "candidate_index_1_based",
        "ring_width",
        "affected_root_count",
        "affected_triangle_count",
        "affected_prism_element_count",
        "affected_core_element_count",
        "affected_chain_node_count",
        "affected_root_sha256",
        "affected_triangle_sha256",
        "affected_element_sha256",
        "affected_triangle_records",
        "affected_triangle_records_sha256",
        "affected_chain_node_records",
        "affected_chain_node_records_sha256",
        "affected_prism_records",
        "affected_prism_records_sha256",
        "affected_core_records",
        "affected_core_records_sha256",
        "entry_schedule_sha256",
        "entry_direction_field_sha256",
        "entry_coordinate_sha256",
        "entry_quality_sha256",
        "entry_objective_sha256",
        "exit_schedule_sha256",
        "exit_coordinate_sha256",
        "exit_quality_sha256",
        "exit_objective_sha256",
        "direction_field_sha256",
        "exit_direction_field_sha256",
        "schedule_sha256",
        "schedule_records",
        "schedule_records_sha256",
        "collar_schedule_proof_sha256",
        "coordinate_sha256",
        "coordinate_reduction_l2_float_hex",
        "quality_evaluation_index",
        "quality",
        "quality_sha256",
        "low_quality_aggregate",
        "low_quality_records",
        "low_quality_records_sha256",
        "objective",
        "objective_sha256",
        "new_bad_lineage_count",
        "lineage_contained",
        "inner_prefix_changed_node_count",
        "outside_closure_changed_node_count",
        "base_root_moved_count",
        "connectivity_sha256",
        "selection_eligible",
        "unsafe_reasons",
        "status",
    }
    normalized_candidates: list[dict[str, Any]] = []
    previous_closure = (0, 0, 0, 0, 0)
    for index, (width, record) in enumerate(
        zip(_COLLAR_RING_WIDTHS, candidate_records), start=1
    ):
        if not isinstance(record, Mapping) or set(record) != candidate_fields:
            raise CoarseScheduleContinuationError(
                "one continuation collar candidate is incomplete"
            )
        closure = (
            _positive_int(
                record["affected_root_count"],
                "continuation collar candidate affected root count",
            ),
            _positive_int(
                record["affected_triangle_count"],
                "continuation collar candidate affected triangle count",
            ),
            _positive_int(
                record["affected_prism_element_count"],
                "continuation collar candidate affected Prism count",
            ),
            _positive_int(
                record["affected_core_element_count"],
                "continuation collar candidate affected core count",
            ),
            _positive_int(
                record["affected_chain_node_count"],
                "continuation collar candidate affected chain-node count",
            ),
        )
        affected_roots = sorted(
            root for root, distance in distance_by_root.items() if distance < width
        )
        configured_hashes = {
            field: _sha256(
                record[field], f"continuation collar candidate {field}"
            )
            for field in candidate_fields
            if field.endswith("_sha256")
        }
        quality = _validate_quality_snapshot(
            record["quality"],
            label=f"continuation collar ring {width} quality",
            expected_endpoint=endpoint,
            expected_prism_element_count=expected_prism_element_count,
            expected_core_element_count=expected_core_element_count,
        )
        objective, objective_sha256 = _validate_objective_snapshot(
            record["objective"],
            configured_sha256=record["objective_sha256"],
            quality=quality,
            label=f"continuation collar ring {width} objective",
        )
        reduction = _canonical_nonnegative_float_hex(
            record["coordinate_reduction_l2_float_hex"],
            f"continuation collar ring {width} coordinate reduction",
        )
        low_quality_aggregate = _validate_collar_low_quality_aggregate(
            record["low_quality_aggregate"],
            expected_bad_count=quality[
                "prism_below_threshold_element_count"
            ],
            label=f"continuation collar ring {width} low-quality aggregate",
        )
        low_quality_records, low_quality_records_sha256 = (
            _validate_collar_bad_records(
                record["low_quality_records"],
                configured_sha256=record["low_quality_records_sha256"],
                label=f"continuation collar ring {width} bad lineage",
            )
        )

        schedule_records = record["schedule_records"]
        if not isinstance(schedule_records, list):
            raise CoarseScheduleContinuationError(
                "one continuation collar candidate schedule proof is not a list"
            )
        normalized_schedule_records: list[dict[str, Any]] = []
        for schedule_record in schedule_records:
            if not isinstance(schedule_record, Mapping) or set(schedule_record) != {
                "root_coordinate_sha256",
                "distance",
                "spatial_weight_float_hex",
                "entry_cumulative_distance_float_hex",
                "exit_cumulative_distance_float_hex",
            }:
                raise CoarseScheduleContinuationError(
                    "one continuation collar root schedule proof is incomplete"
                )
            root = _sha256(
                schedule_record["root_coordinate_sha256"],
                "continuation collar schedule root",
            )
            distance = _nonnegative_int(
                schedule_record["distance"],
                "continuation collar schedule graph distance",
            )
            weight = _canonical_nonnegative_float_hex(
                schedule_record["spatial_weight_float_hex"],
                "continuation collar spatial weight",
            )
            entry_hex, entry_schedule = _cumulative_float_hex(
                schedule_record["entry_cumulative_distance_float_hex"],
                layer_count=endpoint["layer_count"],
                label="continuation collar entry cumulative schedule",
            )
            exit_hex, exit_schedule = _cumulative_float_hex(
                schedule_record["exit_cumulative_distance_float_hex"],
                layer_count=endpoint["layer_count"],
                label="continuation collar exit cumulative schedule",
            )
            expected_weight = max(0.0, 1.0 - distance / width)
            if (
                root not in distance_by_root
                or distance_by_root[root] != distance
                or weight.hex() != expected_weight.hex()
                or entry_schedule[0].hex() != first_height.hex()
                or exit_schedule[0].hex() != first_height.hex()
            ):
                raise CoarseScheduleContinuationError(
                    "one continuation collar root schedule has stale graph/first-layer data"
                )
            a_k = frozen_prefix * first_height
            b_k = entry_schedule[frozen_prefix - 1]
            for layer_index, (b_value, c_value) in enumerate(
                zip(entry_schedule, exit_schedule), start=1
            ):
                a_value = layer_index * first_height
                if layer_index <= frozen_prefix:
                    formula_valid = c_value.hex() == b_value.hex()
                else:
                    expected_c = (
                        b_k
                        + (a_value - a_k)
                        + (1.0 - weight)
                        * ((b_value - b_k) - (a_value - a_k))
                    )
                    formula_valid = math.isclose(
                        c_value,
                        expected_c,
                        rel_tol=1.0e-13,
                        abs_tol=1.0e-18,
                    )
                tolerance = max(1.0e-18, abs(b_value) * 1.0e-13)
                if (
                    not formula_valid
                    or c_value < a_value - tolerance
                    or c_value > b_value + tolerance
                    or (
                        layer_index > 1
                        and c_value - exit_schedule[layer_index - 2]
                        < first_height - tolerance
                    )
                ):
                    raise CoarseScheduleContinuationError(
                        "one continuation collar root schedule violates C/A/B bounds"
                    )
            normalized_schedule_records.append(
                {
                    "root_coordinate_sha256": root,
                    "distance": distance,
                    "spatial_weight_float_hex": weight.hex(),
                    "entry_cumulative_distance_float_hex": entry_hex,
                    "exit_cumulative_distance_float_hex": exit_hex,
                }
            )
        normalized_schedule_records.sort(
            key=lambda item: item["root_coordinate_sha256"]
        )
        expected_schedule_proof_sha256 = _canonical_sha256(
            {
                "frozen_prefix_last_layer_1_based": frozen_prefix,
                "ring_width": width,
                "schedule_records": normalized_schedule_records,
            }
        )
        if (
            schedule_records != normalized_schedule_records
            or [item["root_coordinate_sha256"] for item in schedule_records]
            != affected_roots
            or configured_hashes["schedule_records_sha256"]
            != _canonical_sha256(schedule_records)
            or configured_hashes["collar_schedule_proof_sha256"]
            != expected_schedule_proof_sha256
        ):
            raise CoarseScheduleContinuationError(
                "one continuation collar schedule proof is stale or incomplete"
            )

        affected_triangle_records, affected_triangle_records_sha256 = (
            _validate_collar_triangle_records(
                record["affected_triangle_records"],
                configured_sha256=record[
                    "affected_triangle_records_sha256"
                ],
                label=f"continuation collar ring {width} triangle closure",
            )
        )
        affected_chain_records, affected_chain_records_sha256 = (
            _validate_collar_chain_node_records(
                record["affected_chain_node_records"],
                configured_sha256=record[
                    "affected_chain_node_records_sha256"
                ],
                label=f"continuation collar ring {width} chain-node closure",
            )
        )
        affected_prism_records, affected_prism_records_sha256 = (
            _validate_collar_bad_records(
                record["affected_prism_records"],
                configured_sha256=record["affected_prism_records_sha256"],
                label=f"continuation collar ring {width} Prism closure",
            )
        )
        affected_core_records, affected_core_records_sha256 = (
            _validate_collar_core_records(
                record["affected_core_records"],
                configured_sha256=record["affected_core_records_sha256"],
                label=f"continuation collar ring {width} core closure",
            )
        )
        chain_node_ids = {
            item["chain_node_sha256"] for item in affected_chain_records
        }
        triangle_keys = {
            (
                item["source_triangle_sha256"],
                item["wall_surface_fingerprint"],
            ): item["root_coordinate_sha256"]
            for item in affected_triangle_records
        }
        expected_chain_keys = {
            (root, layer)
            for root in affected_roots
            for layer in range(frozen_prefix + 1, endpoint["layer_count"] + 1)
        }
        observed_chain_keys = {
            (item["root_coordinate_sha256"], item["layer_index_1_based"])
            for item in affected_chain_records
        }
        expected_prism_keys = {
            (triangle, wall, layer)
            for triangle, wall in triangle_keys
            for layer in range(1, endpoint["layer_count"] + 1)
        }
        observed_prism_keys = {
            (
                item["source_triangle_sha256"],
                item["wall_surface_fingerprint"],
                item["layer_index_1_based"],
            )
            for item in affected_prism_records
        }
        residual_bad_ids = {
            item["prism_element_sha256"] for item in residual_bad_records
        }
        candidate_bad_ids = {
            item["prism_element_sha256"] for item in low_quality_records
        }
        affected_element_ids = sorted(
            [item["prism_element_sha256"] for item in affected_prism_records]
            + [item["core_element_sha256"] for item in affected_core_records]
        )
        new_bad_lineage_count = _nonnegative_int(
            record["new_bad_lineage_count"],
            "continuation collar new bad lineage count",
        )
        inner_prefix_changed = _nonnegative_int(
            record["inner_prefix_changed_node_count"],
            "continuation collar candidate inner-prefix changed count",
        )
        outside_closure_changed = _nonnegative_int(
            record["outside_closure_changed_node_count"],
            "continuation collar candidate outside-closure changed count",
        )
        base_root_moved = _nonnegative_int(
            record["base_root_moved_count"],
            "continuation collar candidate base-root moved count",
        )
        safety_checks = {
            "lineage_contained": (
                candidate_bad_ids <= residual_bad_ids
                and new_bad_lineage_count == 0
                and record.get("lineage_contained") is True
            ),
            "core_quality": quality["core_tetra_below_gamma_count"] == 0,
            "positive_elements": quality["nonpositive_element_count"] == 0,
            "finite_quality": quality["nonfinite_count"] == 0,
            "coordinate_preservation": (
                inner_prefix_changed == 0
                and outside_closure_changed == 0
                and base_root_moved == 0
            ),
        }
        selection_eligible = all(safety_checks.values())
        unsafe_reasons = sorted(
            name for name, passed in safety_checks.items() if not passed
        )
        expected_candidate_status = (
            quality["status"] if selection_eligible else "UNSAFE"
        )
        if (
            record.get("candidate_index_1_based") != index
            or record.get("ring_width") != width
            or closure[0] != len(affected_roots)
            or configured_hashes["affected_root_sha256"]
            != _canonical_sha256(affected_roots)
            or closure[1] != len(affected_triangle_records)
            or closure[2] != len(affected_prism_records)
            or closure[3] != len(affected_core_records)
            or closure[4] != len(affected_chain_records)
            or affected_triangle_records_sha256
            != configured_hashes["affected_triangle_records_sha256"]
            or affected_chain_records_sha256
            != configured_hashes["affected_chain_node_records_sha256"]
            or affected_prism_records_sha256
            != configured_hashes["affected_prism_records_sha256"]
            or affected_core_records_sha256
            != configured_hashes["affected_core_records_sha256"]
            or configured_hashes["affected_triangle_sha256"]
            != _canonical_sha256(
                sorted(item["source_triangle_sha256"] for item in affected_triangle_records)
            )
            or configured_hashes["affected_element_sha256"]
            != _canonical_sha256(affected_element_ids)
            or any(
                not set(item["root_coordinate_sha256"]) & set(affected_roots)
                for item in affected_triangle_records
            )
            or not set(affected_roots) <= {
                root
                for item in affected_triangle_records
                for root in item["root_coordinate_sha256"]
            }
            or observed_chain_keys != expected_chain_keys
            or observed_prism_keys != expected_prism_keys
            or any(
                item["root_coordinate_sha256"]
                != triangle_keys.get(
                    (
                        item["source_triangle_sha256"],
                        item["wall_surface_fingerprint"],
                    )
                )
                for item in affected_prism_records
            )
            or any(
                not set(item["node_sha256"]) & chain_node_ids
                for item in affected_core_records
            )
            or len(low_quality_records)
            != quality["prism_below_threshold_element_count"]
            or low_quality_records_sha256
            != configured_hashes["low_quality_records_sha256"]
            or low_quality_aggregate.get("records_sha256")
            != low_quality_records_sha256
            or any(current < previous for current, previous in zip(closure, previous_closure))
            or record.get("quality_evaluation_index")
            != pattern_quality_evaluation_count + index
            or any(
                configured_hashes[field] != entry_hashes[field]
                for field in (
                    "entry_schedule_sha256",
                    "entry_direction_field_sha256",
                    "entry_coordinate_sha256",
                    "entry_quality_sha256",
                    "entry_objective_sha256",
                )
            )
            or configured_hashes["schedule_sha256"]
            != configured_hashes["exit_schedule_sha256"]
            or configured_hashes["coordinate_sha256"]
            != configured_hashes["exit_coordinate_sha256"]
            or configured_hashes["quality_sha256"]
            != configured_hashes["exit_quality_sha256"]
            or configured_hashes["objective_sha256"]
            != configured_hashes["exit_objective_sha256"]
            or configured_hashes["quality_sha256"] != quality["quality_sha256"]
            or configured_hashes["objective_sha256"] != objective_sha256
            or configured_hashes["direction_field_sha256"]
            != entry_hashes["entry_direction_field_sha256"]
            or configured_hashes["exit_direction_field_sha256"]
            != entry_hashes["entry_direction_field_sha256"]
            or objective[-1].hex() != (0.0).hex()
            or configured_hashes["connectivity_sha256"]
            != preservation_hashes["entry_connectivity_sha256"]
            or record.get("selection_eligible") is not selection_eligible
            or record.get("unsafe_reasons") != unsafe_reasons
            or record.get("status") != expected_candidate_status
        ):
            raise CoarseScheduleContinuationError(
                "one continuation collar candidate violates its closure or S0"
            )
        previous_closure = closure
        normalized_candidates.append(
            {
                "record": record,
                "quality": quality,
                "objective": objective,
                "closure": closure,
                "reduction": reduction,
                "hashes": configured_hashes,
            }
        )

    safe_candidates = [
        item
        for item in normalized_candidates
        if item["record"]["selection_eligible"] is True
    ]
    if not safe_candidates:
        raise CoarseScheduleContinuationError(
            "continuation collar has no safe selectable candidate"
        )
    passing = [
        item
        for item in safe_candidates
        if item["quality"]["status"] == "PASS"
    ]
    if passing:
        expected_basis = (
            "passing_candidate_minimum_affected_closure_then_coordinate_change"
        )
        ranked = min(
            passing,
            key=lambda item: (
                *item["closure"],
                item["reduction"],
                item["record"]["ring_width"],
                item["record"]["candidate_index_1_based"],
                item["hashes"]["schedule_sha256"],
                item["hashes"]["coordinate_sha256"],
            ),
        )
        expected_status = "PASS"
    else:
        expected_basis = (
            "count_first_quality_then_minimum_affected_closure_then_coordinate_change"
        )
        ranked = min(
            safe_candidates,
            key=lambda item: (
                *item["objective"],
                *item["closure"],
                item["reduction"],
                item["record"]["ring_width"],
                item["record"]["candidate_index_1_based"],
                item["hashes"]["schedule_sha256"],
                item["hashes"]["coordinate_sha256"],
            ),
        )
        expected_status = "EXHAUSTED"
    if value.get("status") != expected_status:
        raise CoarseScheduleContinuationError(
            "continuation collar status disagrees with its seven candidates"
        )

    selection_fields = {
        "selection_basis",
        "pass_candidate_count",
        "selected_candidate_index_1_based",
        "selected_ring_width",
        "selected_status",
        "selected_schedule_sha256",
        "selected_coordinate_sha256",
        "selected_quality_sha256",
        "selected_quality",
        "selected_affected_root_count",
        "selected_affected_triangle_count",
        "selected_affected_prism_element_count",
        "selected_affected_core_element_count",
        "selected_affected_chain_node_count",
        "selected_coordinate_reduction_l2_float_hex",
    }
    selected = value.get("selection")
    if not isinstance(selected, Mapping) or set(selected) != selection_fields:
        raise CoarseScheduleContinuationError(
            "continuation collar selected candidate summary is incomplete"
        )
    selected_quality = _validate_quality_snapshot(
        selected["selected_quality"],
        label="continuation collar selected quality",
        expected_endpoint=endpoint,
        expected_prism_element_count=expected_prism_element_count,
        expected_core_element_count=expected_core_element_count,
    )
    ranked_record = ranked["record"]
    if (
        selected.get("selection_basis") != expected_basis
        or selected.get("pass_candidate_count") != len(passing)
        or selected.get("selected_candidate_index_1_based")
        != ranked_record["candidate_index_1_based"]
        or selected.get("selected_ring_width") != ranked_record["ring_width"]
        or selected.get("selected_status") != ranked_record["status"]
        or selected.get("selected_schedule_sha256")
        != ranked["hashes"]["schedule_sha256"]
        or selected.get("selected_coordinate_sha256")
        != ranked["hashes"]["coordinate_sha256"]
        or selected.get("selected_quality_sha256")
        != ranked["hashes"]["quality_sha256"]
        or selected_quality != ranked["quality"]
        or tuple(
            selected.get(field)
            for field in (
                "selected_affected_root_count",
                "selected_affected_triangle_count",
                "selected_affected_prism_element_count",
                "selected_affected_core_element_count",
                "selected_affected_chain_node_count",
            )
        )
        != ranked["closure"]
        or selected.get("selected_coordinate_reduction_l2_float_hex")
        != ranked_record["coordinate_reduction_l2_float_hex"]
    ):
        raise CoarseScheduleContinuationError(
            "continuation collar selected candidate is not the deterministic rank winner"
        )

    final_fields = {
        "schedule_sha256",
        "direction_field_sha256",
        "coordinate_sha256",
        "quality_sha256",
        "quality",
    }
    final_state = value.get("final_state")
    if not isinstance(final_state, Mapping) or set(final_state) != final_fields:
        raise CoarseScheduleContinuationError(
            "continuation collar final state is incomplete"
        )
    final_quality = _validate_quality_snapshot(
        final_state["quality"],
        label="continuation collar final quality",
        expected_endpoint=endpoint,
        expected_prism_element_count=expected_prism_element_count,
        expected_core_element_count=expected_core_element_count,
    )
    if (
        final_state.get("schedule_sha256")
        != ranked["hashes"]["schedule_sha256"]
        or final_state.get("direction_field_sha256")
        != entry_hashes["entry_direction_field_sha256"]
        or final_state.get("coordinate_sha256")
        != ranked["hashes"]["coordinate_sha256"]
        or final_state.get("quality_sha256")
        != ranked["hashes"]["quality_sha256"]
        or final_quality != ranked["quality"]
    ):
        raise CoarseScheduleContinuationError(
            "continuation collar final state differs from its selected candidate"
        )
    return {
        "status": expected_status,
        "quality_evaluation_count": collar_count,
        "total_quality_evaluation_count": total_count,
        "selected_record": copy.deepcopy(dict(ranked_record)),
        "final_state": copy.deepcopy(dict(final_state)),
    }


def validate_schedule_frontier_direction_continuation(
    value: Any,
    *,
    expected_endpoint: Mapping[str, Any],
    expected_projected_3d_elements: int,
    expected_prism_element_count: int,
    expected_core_element_count: int,
) -> dict[str, Any]:
    """Strict compatibility validator for continuation discovery evidence."""

    endpoint = _validate_endpoint_integrity(expected_endpoint)
    projected_count = _positive_int(
        expected_projected_3d_elements, "expected projected element count"
    )
    prism_count = _positive_int(
        expected_prism_element_count, "expected Prism6 element count"
    )
    core_count = _positive_int(
        expected_core_element_count, "expected core element count"
    )
    if prism_count + core_count != projected_count:
        raise CoarseScheduleContinuationError(
            "expected continuation element inventories differ"
        )

    top_fields = {
        "schema",
        "status",
        "profile_complete",
        "request",
        "audit_only",
        "audit_variant",
        "calibration_PASS_authorized",
        "production_mesh_eligible",
        "mesh_write_authorized",
        "mesh_written",
        "su2_called",
        "paraview_called",
        "runtime_mesh_tags_hardcoded",
        "source_bindings",
        "endpoint",
        "fixed_direction_input",
        "frontier_input",
        "components",
        "pattern_search",
        "fine_refinement_selection",
        "triple_refinement_selection",
        "collar_refinement_selection",
        "selected_direction_field",
        "cross_schedule_replay",
        "final_quality",
        "observed_counts",
        "incomplete_reason",
        "discovery_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != top_fields:
        raise CoarseScheduleContinuationError(
            "continuation discovery has an incomplete field set"
        )
    mapping_fields = (
        "fixed_direction_input",
        "frontier_input",
        "components",
        "pattern_search",
        "fine_refinement_selection",
        "triple_refinement_selection",
        "collar_refinement_selection",
        "selected_direction_field",
        "cross_schedule_replay",
        "final_quality",
        "observed_counts",
    )
    if (
        value.get("schema") != DISCOVERY_SCHEMA
        or value.get("request") != REQUEST
        or value.get("audit_variant")
        != "physical_schedule_first_frontier_direction_continuation"
        or value.get("audit_only") is not True
        or value.get("calibration_PASS_authorized") is not False
        or value.get("production_mesh_eligible") is not False
        or value.get("mesh_write_authorized") is not False
        or value.get("mesh_written") is not False
        or value.get("su2_called") is not False
        or value.get("paraview_called") is not False
        or value.get("runtime_mesh_tags_hardcoded") is not False
        or value.get("source_bindings") != endpoint["source_bindings"]
        or value.get("endpoint") != endpoint
        or any(not isinstance(value.get(field), Mapping) for field in mapping_fields)
    ):
        raise CoarseScheduleContinuationError(
            "continuation discovery scope or source endpoint is invalid"
        )

    frontier = value["frontier_input"]
    previous_state = frontier.get("previous_state")
    target_state = frontier.get("target_state")
    if not isinstance(previous_state, Mapping) or not isinstance(
        target_state, Mapping
    ):
        raise CoarseScheduleContinuationError(
            "continuation frontier states are incomplete"
        )
    previous_quality = _validate_quality_snapshot(
        previous_state.get("quality"),
        label="continuation previous quality",
        expected_endpoint=endpoint,
        expected_prism_element_count=prism_count,
        expected_core_element_count=core_count,
    )
    target_quality = _validate_quality_snapshot(
        target_state.get("quality"),
        label="continuation target input quality",
        expected_endpoint=endpoint,
        expected_prism_element_count=prism_count,
        expected_core_element_count=core_count,
    )
    frontier_target_schedule_sha256 = _sha256(
        target_state.get("schedule_sha256"),
        "continuation target schedule SHA-256",
    )
    aggregate = target_state.get("low_quality_aggregate")
    if not isinstance(aggregate, Mapping):
        raise CoarseScheduleContinuationError(
            "continuation target low-quality aggregate is missing"
        )
    aggregate_unsigned = dict(aggregate)
    aggregate_sha256 = _sha256(
        aggregate_unsigned.pop("aggregate_sha256", ""),
        "continuation target aggregate SHA-256",
    )
    observation = endpoint["target_observation"]
    if (
        previous_state.get("fraction_float_hex")
        != endpoint["previous_fraction_float_hex"]
        or target_state.get("fraction_float_hex")
        != endpoint["target_fraction_float_hex"]
        or previous_quality["quality_sha256"]
        != endpoint["previous_quality_sha256"]
        or target_quality["quality_sha256"] != endpoint["target_quality_sha256"]
        or aggregate_sha256 != endpoint["target_low_quality_aggregate_sha256"]
        or _canonical_sha256(aggregate_unsigned) != aggregate_sha256
        or aggregate.get("count") != observation["low_quality_prism_count"]
        or aggregate.get("unique_source_triangle_count")
        != observation["unique_source_triangle_count"]
        or aggregate.get("wall_count") != observation["wall_count"]
    ):
        raise CoarseScheduleContinuationError(
            "continuation initial frontier lineage differs from the endpoint"
        )

    components = value["components"]
    caps = endpoint["caps"]
    observed_scope = endpoint["observed_real_scope"]
    budget_proof = endpoint["quality_evaluation_budget_proof"]
    component_records = components.get("records")
    if (
        components.get("maximum_component_count")
        != caps["maximum_component_count"]
        or components.get("maximum_candidate_root_count")
        != caps["maximum_candidate_root_count"]
        or components.get("within_caps") is not True
        or components.get("connected_by_shared_root") is not True
        or components.get("split_by_wall_fingerprint") is not False
        or not isinstance(component_records, list)
        or len(component_records) != components.get("component_count")
        or components.get("component_count")
        != observed_scope["component_count"]
        or components.get("candidate_root_count")
        != observed_scope["candidate_root_count"]
    ):
        raise CoarseScheduleContinuationError(
            "continuation component caps differ from the endpoint"
        )
    component_ids: list[str] = []
    component_root_ids: list[str] = []
    component_triangle_ids: list[str] = []
    component_wall_ids: set[str] = set()
    component_fields = {
        "source_triangle_sha256",
        "wall_surface_fingerprints",
        "root_coordinate_sha256",
        "triangle_count",
        "root_count",
        "connected_by_shared_root",
        "split_by_wall_fingerprint",
        "sharp_owner_used",
        "component_sha256",
    }
    for record in component_records:
        if not isinstance(record, Mapping) or set(record) != component_fields:
            raise CoarseScheduleContinuationError(
                "one continuation component record is incomplete"
            )
        unsigned_component = dict(record)
        component_sha256 = _sha256(
            unsigned_component.pop("component_sha256", ""),
            "continuation component SHA-256",
        )
        roots = record.get("root_coordinate_sha256")
        triangles = record.get("source_triangle_sha256")
        walls = record.get("wall_surface_fingerprints")
        if (
            _canonical_sha256(unsigned_component) != component_sha256
            or not isinstance(roots, list)
            or not roots
            or roots != sorted(set(roots))
            or any(_SHA256.fullmatch(str(item)) is None for item in roots)
            or not isinstance(triangles, list)
            or not triangles
            or triangles != sorted(set(triangles))
            or any(_SHA256.fullmatch(str(item)) is None for item in triangles)
            or not isinstance(walls, list)
            or not walls
            or walls != sorted(set(walls))
            or any(_SHA256.fullmatch(str(item)) is None for item in walls)
            or record.get("triangle_count") != len(triangles)
            or record.get("root_count") != len(roots)
            or record.get("connected_by_shared_root") is not True
            or record.get("split_by_wall_fingerprint") is not False
            or record.get("sharp_owner_used") is not False
        ):
            raise CoarseScheduleContinuationError(
                "one continuation component record is stale"
            )
        component_ids.append(component_sha256)
        component_root_ids.extend(str(item) for item in roots)
        component_triangle_ids.extend(str(item) for item in triangles)
        component_wall_ids.update(str(item) for item in walls)
    if (
        component_ids != sorted(set(component_ids))
        or len(component_root_ids) != len(set(component_root_ids))
        or len(component_triangle_ids) != len(set(component_triangle_ids))
        or len(component_root_ids) != components.get("candidate_root_count")
        or len(component_triangle_ids)
        != observed_scope["source_triangle_count"]
        or len(component_triangle_ids)
        != observation["unique_source_triangle_count"]
        or len(component_wall_ids) != observation["wall_count"]
    ):
        raise CoarseScheduleContinuationError(
            "continuation component records do not cover the frozen frontier"
        )

    selected = value["selected_direction_field"]
    selected_roots = selected.get("changed_roots")
    changed_count = _nonnegative_int(
        selected.get("changed_root_count"), "continuation changed root count"
    )
    if not isinstance(selected_roots, list) or len(selected_roots) != changed_count:
        raise CoarseScheduleContinuationError(
            "continuation selected root count is inconsistent"
        )
    root_ids: list[str] = []
    for record in selected_roots:
        if not isinstance(record, Mapping) or set(record) != {
            "root_coordinate_sha256",
            "incoming_direction_float_hex",
            "selected_direction_float_hex",
        }:
            raise CoarseScheduleContinuationError(
                "one continuation selected root is invalid"
            )
        root_id = _sha256(
            record["root_coordinate_sha256"],
            "continuation stable root SHA-256",
        )
        incoming = _canonical_unit_hex(
            record["incoming_direction_float_hex"], "incoming direction"
        )
        chosen = _canonical_unit_hex(
            record["selected_direction_float_hex"], "selected direction"
        )
        if incoming == chosen:
            raise CoarseScheduleContinuationError(
                "one continuation changed root did not change direction"
            )
        root_ids.append(root_id)
    if root_ids != sorted(set(root_ids)):
        raise CoarseScheduleContinuationError(
            "continuation stable selected roots are duplicated or unsorted"
        )
    if not set(root_ids) <= set(component_root_ids):
        raise CoarseScheduleContinuationError(
            "continuation selected root is outside the frontier components"
        )

    final_quality = _validate_quality_snapshot(
        value["final_quality"],
        label="continuation final quality",
        expected_endpoint=endpoint,
        expected_prism_element_count=prism_count,
        expected_core_element_count=core_count,
    )
    count_fields = {
        "projected_3d_element_count",
        "prism_element_count",
        "core_element_count",
        "core_tetra_count",
        "target_low_quality_prism_count",
        "target_unique_source_triangle_count",
        "target_wall_count",
        "component_count",
        "candidate_root_count",
        "pattern_quality_evaluation_count",
        "collar_quality_evaluation_count",
        "quality_evaluation_count",
        "changed_root_count",
        "final_nonpositive_element_count",
        "final_prism_below_threshold_count",
        "final_core_tetra_below_gamma_count",
        "final_nonfinite_count",
    }
    counts = value["observed_counts"]
    if set(counts) != count_fields:
        raise CoarseScheduleContinuationError(
            "continuation observed count fields are incomplete"
        )
    normalized_counts = {
        field: _nonnegative_int(counts[field], f"continuation {field}")
        for field in count_fields
    }
    if (
        normalized_counts["projected_3d_element_count"] != projected_count
        or normalized_counts["prism_element_count"] != prism_count
        or normalized_counts["core_element_count"] != core_count
        or normalized_counts["core_tetra_count"] != core_count
        or normalized_counts["target_low_quality_prism_count"]
        != observation["low_quality_prism_count"]
        or normalized_counts["target_unique_source_triangle_count"]
        != observation["unique_source_triangle_count"]
        or normalized_counts["target_wall_count"] != observation["wall_count"]
        or normalized_counts["component_count"]
        != components.get("component_count")
        or normalized_counts["component_count"]
        != observed_scope["component_count"]
        or normalized_counts["candidate_root_count"]
        != components.get("candidate_root_count")
        or normalized_counts["candidate_root_count"]
        != observed_scope["candidate_root_count"]
        or normalized_counts["component_count"]
        > caps["maximum_component_count"]
        or normalized_counts["candidate_root_count"]
        > caps["maximum_candidate_root_count"]
        or normalized_counts["quality_evaluation_count"]
        > caps["maximum_quality_evaluations"]
        or normalized_counts["quality_evaluation_count"]
        > budget_proof["actual_scope"]["quality_evaluation_upper_bound"]
        or normalized_counts["pattern_quality_evaluation_count"]
        + normalized_counts["collar_quality_evaluation_count"]
        != normalized_counts["quality_evaluation_count"]
        or normalized_counts["pattern_quality_evaluation_count"]
        > (
            budget_proof["actual_scope"]["quality_evaluation_upper_bound"]
            - budget_proof["actual_scope"][
                "collar_refinement_quality_evaluation_upper_bound"
            ]
        )
        or normalized_counts["collar_quality_evaluation_count"]
        > budget_proof["actual_scope"][
            "collar_refinement_quality_evaluation_upper_bound"
        ]
        or normalized_counts["changed_root_count"] != changed_count
        or normalized_counts["final_nonpositive_element_count"]
        != final_quality["nonpositive_element_count"]
        or normalized_counts["final_prism_below_threshold_count"]
        != final_quality["prism_below_threshold_element_count"]
        or normalized_counts["final_core_tetra_below_gamma_count"]
        != final_quality["core_tetra_below_gamma_count"]
        or normalized_counts["final_nonfinite_count"]
        != final_quality["nonfinite_count"]
    ):
        raise CoarseScheduleContinuationError(
            "continuation observations, caps, or final quality disagree"
        )

    pattern_search = value["pattern_search"]
    expected_pattern_endpoint = _expected_frontier_pattern_endpoint(endpoint)
    pattern_changed_roots = pattern_search.get("changed_roots")
    if (
        pattern_search.get("endpoint_sha256")
        != expected_pattern_endpoint["endpoint_sha256"]
        or pattern_search.get("component_count")
        != normalized_counts["component_count"]
        or pattern_search.get("candidate_root_count")
        != normalized_counts["candidate_root_count"]
        or pattern_search.get("quality_evaluation_count")
        != normalized_counts["pattern_quality_evaluation_count"]
        or not isinstance(pattern_changed_roots, list)
        or len(pattern_changed_roots) != normalized_counts["changed_root_count"]
        or pattern_search.get("status") not in {"PASS", "EXHAUSTED"}
        or expected_pattern_endpoint["maximum_quality_evaluations"]
        < normalized_counts["pattern_quality_evaluation_count"]
        or (
            budget_proof["actual_scope"]["quality_evaluation_upper_bound"]
            - budget_proof["actual_scope"][
                "collar_refinement_quality_evaluation_upper_bound"
            ]
        )
        < normalized_counts["pattern_quality_evaluation_count"]
        or expected_pattern_endpoint["maximum_pair_candidates_per_edge_step"]
        != caps["maximum_pair_candidates_per_edge_step"]
    ):
        raise CoarseScheduleContinuationError(
            "continuation pattern search differs from its endpoint or counts"
        )
    search_selection = pattern_search.get("fine_refinement_selection")
    if (
        not isinstance(search_selection, Mapping)
        or search_selection != value["fine_refinement_selection"]
    ):
        raise CoarseScheduleContinuationError(
            "continuation fine-refinement selection copy diverges from search evidence"
        )
    fine_context = _validate_static_and_fine_pattern_evidence(
        pattern_search=pattern_search,
        selection=search_selection,
        endpoint=endpoint,
        expected_pattern_endpoint=expected_pattern_endpoint,
        stable_component_records=component_records,
        total_quality_evaluation_count=normalized_counts[
            "pattern_quality_evaluation_count"
        ],
    )
    triple_selection = pattern_search.get("triple_refinement_selection")
    if (
        not isinstance(triple_selection, Mapping)
        or triple_selection != value["triple_refinement_selection"]
    ):
        raise CoarseScheduleContinuationError(
            "continuation triple-refinement selection copy diverges from search evidence"
        )
    _validate_triple_pattern_evidence(
        pattern_search=pattern_search,
        selection=triple_selection,
        endpoint=endpoint,
        expected_pattern_endpoint=expected_pattern_endpoint,
        fine_context=fine_context,
        total_quality_evaluation_count=normalized_counts[
            "pattern_quality_evaluation_count"
        ],
    )
    collar_endpoint = _expected_frontier_collar_endpoint(endpoint)
    collar_context = _validate_collar_refinement_selection(
        value["collar_refinement_selection"],
        endpoint=endpoint,
        collar_endpoint=collar_endpoint,
        pattern_search=pattern_search,
        triple_selection=triple_selection,
        stable_component_records=component_records,
        pattern_quality_evaluation_count=normalized_counts[
            "pattern_quality_evaluation_count"
        ],
        frontier_target_schedule_sha256=frontier_target_schedule_sha256,
        expected_prism_element_count=prism_count,
        expected_core_element_count=core_count,
    )
    if (
        collar_context["quality_evaluation_count"]
        != normalized_counts["collar_quality_evaluation_count"]
        or collar_context["total_quality_evaluation_count"]
        != normalized_counts["quality_evaluation_count"]
    ):
        raise CoarseScheduleContinuationError(
            "continuation collar and top-level evaluation counts diverge"
        )
    pattern_root_ids: list[str] = []
    for record in pattern_changed_roots:
        if not isinstance(record, Mapping):
            raise CoarseScheduleContinuationError(
                "one continuation pattern changed root is invalid"
            )
        pattern_root_ids.append(
            _sha256(
                record.get("root_coordinate_sha256"),
                "continuation pattern changed-root SHA-256",
            )
        )
    if (
        pattern_root_ids != sorted(set(pattern_root_ids))
        or pattern_root_ids != root_ids
    ):
        raise CoarseScheduleContinuationError(
            "continuation pattern and selected changed roots differ"
        )

    replay = value["cross_schedule_replay"]
    replay_hash_fields = (
        "state_a_first_coordinate_sha256",
        "state_b_first_coordinate_sha256",
        "state_a_second_coordinate_sha256",
        "state_b_second_coordinate_sha256",
        "state_a_first_quality_sha256",
        "state_b_first_quality_sha256",
        "state_a_second_quality_sha256",
        "state_b_second_quality_sha256",
        "non_target_coordinate_sha256",
        "selected_schedule_sha256",
        "selected_coordinate_sha256",
        "selected_quality_sha256",
    )
    if (
        replay.get("sequence_fraction_float_hex")
        != [
            endpoint["previous_fraction_float_hex"],
            endpoint["target_fraction_float_hex"],
            endpoint["previous_fraction_float_hex"],
            endpoint["target_fraction_float_hex"],
        ]
        or any(
            _SHA256.fullmatch(str(replay.get(field, ""))) is None
            for field in replay_hash_fields
        )
        or replay.get("state_a_first_coordinate_sha256")
        != replay.get("state_a_second_coordinate_sha256")
        or replay.get("state_b_first_coordinate_sha256")
        != replay.get("state_b_second_coordinate_sha256")
        or replay.get("state_a_first_coordinate_sha256")
        == replay.get("state_b_first_coordinate_sha256")
        or replay.get("state_a_first_quality_sha256")
        != replay.get("state_a_second_quality_sha256")
        or replay.get("state_b_first_quality_sha256")
        != replay.get("state_b_second_quality_sha256")
        or replay.get("state_b_second_quality_sha256")
        != final_quality["quality_sha256"]
        or replay.get("selected_coordinate_sha256")
        != replay.get("state_b_first_coordinate_sha256")
        or replay.get("selected_coordinate_sha256")
        != replay.get("state_b_second_coordinate_sha256")
        or replay.get("selected_quality_sha256")
        != replay.get("state_b_first_quality_sha256")
        or replay.get("selected_quality_sha256")
        != replay.get("state_b_second_quality_sha256")
        or replay.get("state_a_reproducible") is not True
        or replay.get("state_b_reproducible") is not True
        or replay.get("base_root_moved_count") != 0
        or replay.get("non_target_node_changed_count") != 0
    ):
        raise CoarseScheduleContinuationError(
            "continuation A-B-A-B replay evidence is invalid"
        )

    collar_final = collar_context["final_state"]
    if collar_final is not None and (
        replay.get("selected_schedule_sha256")
        != collar_final["schedule_sha256"]
        or replay.get("selected_coordinate_sha256")
        != collar_final["coordinate_sha256"]
        or replay.get("selected_quality_sha256")
        != collar_final["quality_sha256"]
        or final_quality != collar_final["quality"]
    ):
        raise CoarseScheduleContinuationError(
            "continuation A-B-A-B replay differs from the collar final state"
        )

    passed = final_quality["status"] == "PASS"
    expected_passed = (
        collar_context["status"] == "PASS"
        or (
            collar_context["status"] == "SKIPPED"
            and pattern_search.get("status") == "PASS"
        )
    )
    if (
        passed is not expected_passed
        or (
            collar_context["status"] == "EXHAUSTED"
            and pattern_search.get("status") != "EXHAUSTED"
        )
        or (
            collar_context["status"] == "PASS"
            and pattern_search.get("status") != "EXHAUSTED"
        )
        or
        value.get("status") != ("PASS" if passed else "INCOMPLETE")
        or value.get("profile_complete") is not passed
        or replay.get("both_states_pass") is not passed
        or (passed and value.get("incomplete_reason") is not None)
        or (
            not passed
            and (
                not isinstance(value.get("incomplete_reason"), str)
                or not value["incomplete_reason"]
            )
        )
    ):
        raise CoarseScheduleContinuationError(
            "continuation PASS/INCOMPLETE status is inconsistent"
        )
    unsigned = dict(value)
    discovery_sha256 = _sha256(
        unsigned.pop("discovery_sha256", ""), "continuation discovery SHA-256"
    )
    if _canonical_sha256(unsigned) != discovery_sha256:
        raise CoarseScheduleContinuationError(
            "continuation discovery self-hash is stale"
        )
    return copy.deepcopy(dict(value))


__all__ = [
    "DISCOVERY_SCHEMA",
    "ENDPOINT_SCHEMA",
    "REQUEST",
    "CoarseScheduleContinuationError",
    "make_frontier_direction_endpoint",
    "make_schedule_frontier_direction_endpoint",
    "validate_frontier_direction_endpoint",
    "validate_schedule_frontier_direction_continuation",
    "validate_schedule_frontier_direction_endpoint",
]
