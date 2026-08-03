"""Stable owner-free component evidence for coarse direction audits."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Any, Mapping, Sequence

from .coarse_direction_feasibility import (
    CoarseDirectionFeasibilityError,
    FINE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
    FRONTIER_PATTERN_ENDPOINT_SCHEMA,
    PATTERN_ENDPOINT_SCHEMA,
    TRIPLE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
    validate_owner_free_direction_endpoint,
)


DISCOVERY_SCHEMA = "cfdpipe.coarse_component_direction_discovery.v1"
SEARCH_SCHEMA = "cfdpipe.coarse_component_direction_search.v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FINE_REFINEMENT_SELECTION_SCHEMA = (
    "cfdpipe.coarse_schedule_frontier_fine_refinement_selection.v1"
)
_FINE_REFINEMENT_CONSERVATIVE_QUALITY_EVALUATION_CAP = 2187
_TRIPLE_REFINEMENT_SELECTION_SCHEMA = (
    "cfdpipe.coarse_schedule_frontier_triple_refinement_selection.v1"
)
_TRIPLE_REFINEMENT_CONSERVATIVE_QUALITY_EVALUATION_CAP = 3723


class CoarseComponentDirectionError(RuntimeError):
    """Owner-free component evidence is invalid or incomplete."""


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _validate_fine_quality_snapshot(
    value: Any,
    *,
    quality_fields: set[str],
    threshold_reference: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate one FINE static/prefix quality snapshot and its self-hash."""

    if not isinstance(value, Mapping) or set(value) != quality_fields:
        raise CoarseComponentDirectionError(
            "one FINE quality snapshot has an incomplete field set"
        )
    unsigned = dict(value)
    quality_sha256 = str(unsigned.pop("quality_sha256", "")).casefold()
    count_fields = (
        "nonfinite_count",
        "nonpositive_element_count",
        "prism_below_threshold_element_count",
        "core_tetra_below_gamma_count",
        "prism_element_count",
        "core_element_count",
        "core_tetra_count",
    )
    counts = {field: value.get(field) for field in count_fields}
    finite_fields = (
        "minimum_prism_scaled_jacobian_for_pass",
        "minimum_core_tetra_gamma_for_pass",
        "minimum_prism_scaled_jacobian",
        "minimum_core_tetra_gamma",
        "maximum_prism_scaled_jacobian_deficit",
        "prism_scaled_jacobian_deficit_l1",
        "prism_scaled_jacobian_deficit_l2",
        "maximum_core_tetra_gamma_deficit",
        "core_tetra_gamma_deficit_l2",
    )
    if (
        _SHA256.fullmatch(quality_sha256) is None
        or _canonical_sha256(unsigned) != quality_sha256
        or any(
            isinstance(raw, bool) or not isinstance(raw, int) or raw < 0
            for raw in counts.values()
        )
        or counts["prism_element_count"] <= 0
        or counts["core_element_count"] <= 0
        or counts["core_tetra_count"] <= 0
        or counts["core_tetra_count"] > counts["core_element_count"]
        or counts["prism_below_threshold_element_count"]
        > counts["prism_element_count"]
        or counts["core_tetra_below_gamma_count"]
        > counts["core_tetra_count"]
        or counts["nonfinite_count"]
        > counts["prism_element_count"] + counts["core_element_count"]
        or counts["nonpositive_element_count"]
        > counts["prism_element_count"] + counts["core_element_count"]
        or not isinstance(value.get("all_values_finite"), bool)
        or value["all_values_finite"] is not (counts["nonfinite_count"] == 0)
        or any(
            isinstance(value.get(field), bool)
            or not isinstance(value.get(field), (int, float))
            or not math.isfinite(float(value[field]))
            for field in finite_fields
        )
        or any(
            isinstance(threshold_reference.get(field), bool)
            or not isinstance(threshold_reference.get(field), (int, float))
            or not math.isfinite(float(threshold_reference[field]))
            for field in (
                "minimum_prism_scaled_jacobian_for_pass",
                "minimum_core_tetra_gamma_for_pass",
            )
        )
        or any(
            float(value[field]) < 0.0
            for field in (
                "maximum_prism_scaled_jacobian_deficit",
                "prism_scaled_jacobian_deficit_l1",
                "prism_scaled_jacobian_deficit_l2",
                "maximum_core_tetra_gamma_deficit",
                "core_tetra_gamma_deficit_l2",
            )
        )
        or float(value["minimum_prism_scaled_jacobian_for_pass"]).hex()
        != float(
            threshold_reference["minimum_prism_scaled_jacobian_for_pass"]
        ).hex()
        or float(value["minimum_core_tetra_gamma_for_pass"]).hex()
        != float(
            threshold_reference["minimum_core_tetra_gamma_for_pass"]
        ).hex()
        or value.get("status")
        != (
            "PASS"
            if counts["nonfinite_count"] == 0
            and counts["nonpositive_element_count"] == 0
            and counts["prism_below_threshold_element_count"] == 0
            and counts["core_tetra_below_gamma_count"] == 0
            else "FAIL"
        )
    ):
        raise CoarseComponentDirectionError(
            "one FINE quality snapshot is stale or unsafe"
        )
    return copy.deepcopy(dict(value))


def _validate_fine_objective_snapshot(
    value: Any,
    *,
    configured_sha256: Any,
    quality: Mapping[str, Any],
    objective_fields: Sequence[str],
) -> str:
    """Validate a FINE count-first objective and its field-bound hash."""

    if (
        not isinstance(value, list)
        or len(value) != len(objective_fields)
        or any(
            isinstance(raw, bool)
            or not isinstance(raw, (int, float))
            or not math.isfinite(float(raw))
            for raw in value
        )
    ):
        raise CoarseComponentDirectionError(
            "one FINE objective snapshot is incomplete"
        )
    normalized = [float(raw) for raw in value]
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
    objective_sha256 = str(configured_sha256).casefold()
    if (
        [raw.hex() for raw in normalized[:-1]]
        != [raw.hex() for raw in expected_prefix]
        or normalized[-1] < 0.0
        or _SHA256.fullmatch(objective_sha256) is None
        or objective_sha256
        != _canonical_sha256(
            {
                "quality_objective_fields": list(objective_fields),
                "objective": value,
            }
        )
    ):
        raise CoarseComponentDirectionError(
            "one FINE objective snapshot is stale"
        )
    return objective_sha256


def _validate_fine_refinement_selection(
    selection: Any,
    *,
    component_evidence: Mapping[str, Mapping[str, Any]],
    endpoint: Mapping[str, Any],
    search: Mapping[str, Any],
    pattern: Mapping[str, Any],
    quality_fields: set[str],
    triple_refinement_endpoint: bool = False,
    triple_pattern: Any = None,
) -> None:
    """Validate the FINE static gate, selected component, and prefix/tail handoff."""

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
    evaluation_fields = {
        "endpoint_maximum_quality_evaluations",
        "global_conservative_quality_evaluation_cap",
        "static_quality_evaluation_count",
        "fine_quality_evaluation_count",
        "total_quality_evaluation_count",
    }
    static_evidence_fields = {
        "interaction_component_sha256",
        "static_exit_status",
        "static_exit_quality_sha256",
        "static_exit_objective_sha256",
        "static_exit_coordinate_sha256",
        "static_exit_quality_evaluation_index",
    }
    if not isinstance(selection, Mapping) or set(selection) != selection_fields:
        raise CoarseComponentDirectionError(
            "FINE refinement selection evidence is incomplete"
        )
    gate = selection.get("gate")
    order = selection.get("order")
    evaluations = selection.get("evaluation_evidence")
    static_evidence = selection.get("component_static_evidence")
    if (
        selection.get("schema") != _FINE_REFINEMENT_SELECTION_SCHEMA
        or selection.get("status") != "PASS"
        or not isinstance(gate, Mapping)
        or set(gate) != gate_fields
        or not isinstance(order, Mapping)
        or set(order) != order_fields
        or not isinstance(evaluations, Mapping)
        or set(evaluations) != evaluation_fields
        or not isinstance(static_evidence, list)
        or any(
            not isinstance(record, Mapping)
            or set(record) != static_evidence_fields
            for record in static_evidence
        )
    ):
        raise CoarseComponentDirectionError(
            "FINE refinement selection fields are stale"
        )

    component_ids = sorted(component_evidence)
    failing_ids = sorted(
        component_id
        for component_id, evidence in component_evidence.items()
        if evidence["static_quality"]["status"] == "FAIL"
    )
    fine_performed = gate.get("fine_refinement_performed")
    if (
        gate.get("static_all_components_complete_before_fine") is not True
        or gate.get("static_search_started_from_fresh_a") is not True
        or gate.get("static_component_count") != len(component_ids)
        or gate.get("static_component_count") != search["component_count"]
        or gate.get("static_failing_component_count") != len(failing_ids)
        or len(failing_ids) not in {0, 1}
        or gate.get("maximum_refined_component_count")
        != endpoint["maximum_refined_component_count"]
        or gate.get("required_refined_component_root_count")
        != endpoint["refined_component_root_count"]
        or gate.get("required_refined_component_source_triangle_count")
        != endpoint["refined_component_source_triangle_count"]
        or not isinstance(fine_performed, bool)
        or fine_performed is not (len(failing_ids) == 1)
    ):
        raise CoarseComponentDirectionError(
            "FINE static-to-refinement gate is inconsistent"
        )

    expected_static_evidence = [
        {
            "interaction_component_sha256": component_id,
            "static_exit_status": component_evidence[component_id][
                "static_quality"
            ]["status"],
            "static_exit_quality_sha256": component_evidence[component_id][
                "static_quality_sha256"
            ],
            "static_exit_objective_sha256": component_evidence[component_id][
                "static_objective_sha256"
            ],
            "static_exit_coordinate_sha256": component_evidence[component_id][
                "static_coordinate_sha256"
            ],
            "static_exit_quality_evaluation_index": component_evidence[
                component_id
            ]["static_index"],
        }
        for component_id in component_ids
    ]
    if static_evidence != expected_static_evidence:
        raise CoarseComponentDirectionError(
            "FINE component static evidence is inconsistent"
        )

    static_order = order.get("static_component_sha256_order")
    fine_order = order.get("fine_component_sha256_order")
    indices = [
        int(component_evidence[component_id]["static_index"])
        for component_id in component_ids
    ]
    expected_static_order = sorted(
        component_ids,
        key=lambda component_id: component_evidence[component_id][
            "static_index"
        ],
    )
    last_static_index = max(indices)
    static_evaluation_count = evaluations.get("static_quality_evaluation_count")
    fine_evaluation_count = evaluations.get("fine_quality_evaluation_count")
    total_evaluation_count = evaluations.get("total_quality_evaluation_count")
    triple_evaluation_count = (
        triple_pattern.get("quality_evaluation_count")
        if triple_refinement_endpoint and isinstance(triple_pattern, Mapping)
        else 0
    )
    if (
        not isinstance(static_order, list)
        or static_order != expected_static_order
        or len(static_order) != len(set(static_order))
        or set(static_order) != set(component_ids)
        or len(indices) != len(set(indices))
        or not isinstance(fine_order, list)
        or order.get("last_static_quality_evaluation_index")
        != last_static_index
        or order.get("all_static_exits_before_first_fine_evaluation") is not True
        or order.get("tail_steps_radians")
        != list(endpoint["tangent_pattern_steps_radians"])[5:]
        or evaluations.get("endpoint_maximum_quality_evaluations")
        != endpoint["maximum_quality_evaluations"]
        or evaluations.get("global_conservative_quality_evaluation_cap")
        != _FINE_REFINEMENT_CONSERVATIVE_QUALITY_EVALUATION_CAP
        or isinstance(static_evaluation_count, bool)
        or not isinstance(static_evaluation_count, int)
        or static_evaluation_count <= 0
        or isinstance(fine_evaluation_count, bool)
        or not isinstance(fine_evaluation_count, int)
        or fine_evaluation_count < 0
        or isinstance(total_evaluation_count, bool)
        or not isinstance(total_evaluation_count, int)
        or total_evaluation_count <= 0
        or static_evaluation_count != last_static_index
        or (
            triple_refinement_endpoint
            and total_evaluation_count
            != static_evaluation_count + fine_evaluation_count
        )
        or (
            not triple_refinement_endpoint
            and total_evaluation_count != search["quality_evaluation_count"]
        )
        or total_evaluation_count
        > _FINE_REFINEMENT_CONSERVATIVE_QUALITY_EVALUATION_CAP
        or total_evaluation_count > endpoint["maximum_quality_evaluations"]
        or (
            not triple_refinement_endpoint
            and not 0
            <= total_evaluation_count
            - static_evaluation_count
            - fine_evaluation_count
            <= 6 + len(component_ids)
        )
        or isinstance(triple_evaluation_count, bool)
        or not isinstance(triple_evaluation_count, int)
        or triple_evaluation_count < 0
        or sum(
            int(evidence["record"]["quality_evaluation_count"])
            for evidence in component_evidence.values()
        )
        != (
            static_evaluation_count
            + fine_evaluation_count
            + triple_evaluation_count
        )
        or pattern.get("quality_evaluation_count") != fine_evaluation_count
        or pattern.get("refined_component_count") != len(failing_ids)
        or pattern.get("performed") is not fine_performed
    ):
        raise CoarseComponentDirectionError(
            "FINE phase order or evaluation budget is inconsistent"
        )

    for component_id in sorted(set(component_ids) - set(failing_ids)):
        evidence = component_evidence[component_id]
        record = evidence["record"]
        if (
            record["search_exit_quality_sha256"]
            != evidence["static_quality_sha256"]
            or record["search_exit_coordinate_sha256"]
            != evidence["static_coordinate_sha256"]
            or record["selected_objective"]
            != record["static_exit_objective"]
        ):
            raise CoarseComponentDirectionError(
                "one unrefined FINE component changed after its static exit"
            )

    selected_component = selection.get("selected_component")
    prefix = selection.get("prefix_checkpoint")
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
            or pattern.get("accepted_move_count") != 0
            or order.get("tail_started_from_prefix_checkpoint") is not False
            or any(order.get(field) is not None for field in nullable_order_fields)
        ):
            raise CoarseComponentDirectionError(
                "FINE zero-failure branch contains refinement evidence"
            )
        return

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
        raise CoarseComponentDirectionError(
            "FINE selected component evidence is incomplete"
        )
    selected_id = str(selected_component.get("interaction_component_sha256", ""))
    selected_evidence = component_evidence.get(selected_id)
    topology = (
        selected_evidence["topology"] if selected_evidence is not None else None
    )
    selected_roots = selected_component.get("root_coordinate_sha256")
    selected_triangles = selected_component.get("source_triangle_sha256")
    if (
        failing_ids != [selected_id]
        or fine_order != [selected_id]
        or selected_evidence is None
        or not isinstance(topology, Mapping)
        or selected_component.get("candidate_root_count")
        != endpoint["refined_component_root_count"]
        or selected_component.get("candidate_root_count")
        != topology["candidate_root_count"]
        or not isinstance(selected_roots, list)
        or selected_roots != topology["root_coordinate_sha256"]
        or selected_roots != sorted(set(selected_roots))
        or selected_component.get("source_triangle_count")
        != endpoint["refined_component_source_triangle_count"]
        or not isinstance(selected_triangles, list)
        or selected_triangles
        != selected_evidence["record"]["source_triangle_sha256"]
        or selected_triangles != sorted(set(selected_triangles))
        or selected_component.get("unique_source_triangle_edge_count") != 3
    ):
        raise CoarseComponentDirectionError(
            "FINE selected component differs from the frozen scope"
        )

    first_fine_index = order.get("first_fine_quality_evaluation_index")
    first_tail_index = order.get("first_tail_quality_evaluation_index")
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
        raise CoarseComponentDirectionError(
            "FINE prefix checkpoint evidence is incomplete"
        )
    prefix_quality = _validate_fine_quality_snapshot(
        prefix.get("selected_state_quality"),
        quality_fields=quality_fields,
        threshold_reference=selected_evidence["static_quality"],
    )
    prefix_objective_sha256 = _validate_fine_objective_snapshot(
        prefix.get("selected_state_objective"),
        configured_sha256=prefix.get("selected_state_objective_sha256"),
        quality=prefix_quality,
        objective_fields=endpoint["quality_objective"],
    )
    prefix_quality_sha256 = str(
        prefix.get("selected_state_quality_sha256", "")
    ).casefold()
    prefix_coordinate_sha256 = str(
        prefix.get("selected_state_coordinate_sha256", "")
    ).casefold()
    prefix_direction_state_sha256 = str(
        prefix.get("selected_direction_state_sha256", "")
    ).casefold()
    prefix_direction_field_sha256 = str(
        prefix.get("selected_direction_field_sha256", "")
    ).casefold()
    prefix_index = prefix.get("quality_evaluation_index")
    if (
        prefix.get("selected_component_sha256") != selected_id
        or prefix.get("prefix_steps_radians")
        != list(endpoint["tangent_pattern_steps_radians"][:5])
        or any(
            _SHA256.fullmatch(value) is None
            for value in (
                prefix_quality_sha256,
                prefix_coordinate_sha256,
                prefix_direction_state_sha256,
                prefix_direction_field_sha256,
                prefix_objective_sha256,
            )
        )
        or prefix_quality_sha256 != prefix_quality["quality_sha256"]
        or prefix_quality["prism_element_count"]
        != selected_evidence["static_quality"]["prism_element_count"]
        or prefix_quality["core_element_count"]
        != selected_evidence["static_quality"]["core_element_count"]
        or isinstance(first_fine_index, bool)
        or not isinstance(first_fine_index, int)
        or first_fine_index != static_evaluation_count + 1
        or isinstance(prefix_index, bool)
        or not isinstance(prefix_index, int)
        or prefix_index < first_fine_index
        or isinstance(first_tail_index, bool)
        or not isinstance(first_tail_index, int)
        or first_tail_index != prefix_index + 1
        or first_tail_index > static_evaluation_count + fine_evaluation_count
        or order.get("tail_start_coordinate_sha256")
        != prefix_coordinate_sha256
        or order.get("tail_start_direction_state_sha256")
        != prefix_direction_state_sha256
        or order.get("tail_start_direction_field_sha256")
        != prefix_direction_field_sha256
        or order.get("tail_start_quality_sha256") != prefix_quality_sha256
        or order.get("tail_started_from_prefix_checkpoint") is not True
    ):
        raise CoarseComponentDirectionError(
            "FINE prefix/tail continuation evidence is inconsistent"
        )


def _validate_triple_refinement_selection(
    selection: Any,
    *,
    fine_selection: Mapping[str, Any],
    component_evidence: Mapping[str, Mapping[str, Any]],
    endpoint: Mapping[str, Any],
    search: Mapping[str, Any],
    summary: Any,
    quality_fields: set[str],
) -> None:
    """Validate the atomic three-root phase emitted by the real runner."""

    selection_fields = {
        "schema",
        "status",
        "gate",
        "handoff",
        "schedule",
        "step_pass_records",
        "evaluation_evidence",
        "selected_component",
        "final_state",
    }
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
    schedule_fields = {
        "steps_radians",
        "coordinate_passes_per_step",
        "tangent_candidates_per_root_per_step",
        "atomic_cartesian_candidates_per_step_pass",
    }
    evaluation_fields = {
        "endpoint_maximum_quality_evaluations",
        "global_conservative_quality_evaluation_cap",
        "static_quality_evaluation_count",
        "fine_quality_evaluation_count",
        "through_fine_handoff_quality_evaluation_count",
        "triple_quality_evaluation_count",
        "triple_quality_evaluation_start_index_exclusive",
        "first_triple_quality_evaluation_index",
        "last_triple_quality_evaluation_index",
        "triple_quality_evaluation_end_index_inclusive",
        "final_replay_and_verification_quality_evaluation_count",
        "total_quality_evaluation_count",
    }
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
    if (
        not isinstance(selection, Mapping)
        or set(selection) != selection_fields
        or selection.get("schema") != _TRIPLE_REFINEMENT_SELECTION_SCHEMA
        or selection.get("status") != "PASS"
        or not isinstance(summary, Mapping)
        or set(summary) != summary_fields
    ):
        raise CoarseComponentDirectionError(
            "TRIPLE refinement selection evidence is incomplete"
        )

    gate = selection.get("gate")
    schedule = selection.get("schedule")
    records = selection.get("step_pass_records")
    evaluations = selection.get("evaluation_evidence")
    fine_gate = fine_selection.get("gate")
    fine_evaluations = fine_selection.get("evaluation_evidence")
    if (
        not isinstance(gate, Mapping)
        or set(gate) != gate_fields
        or not isinstance(schedule, Mapping)
        or set(schedule) != schedule_fields
        or not isinstance(records, list)
        or not isinstance(evaluations, Mapping)
        or set(evaluations) != evaluation_fields
        or not isinstance(fine_gate, Mapping)
        or not isinstance(fine_evaluations, Mapping)
    ):
        raise CoarseComponentDirectionError(
            "TRIPLE gate, schedule, or evaluation fields are stale"
        )

    failing_ids = sorted(
        component_id
        for component_id, evidence in component_evidence.items()
        if evidence["static_quality"]["status"] == "FAIL"
    )
    fine_performed = fine_gate.get("fine_refinement_performed")
    triple_performed = gate.get("triple_refinement_performed")
    selected_component = selection.get("selected_component")
    if (
        gate.get("static_all_components_complete_before_fine") is not True
        or gate.get("single_and_pair_phases_complete_before_triple") is not True
        or type(gate.get("static_failing_component_count")) is not int
        or gate.get("static_failing_component_count") != len(failing_ids)
        or len(failing_ids) not in {0, 1}
        or not isinstance(fine_performed, bool)
        or fine_performed is not (len(failing_ids) == 1)
        or gate.get("fine_refinement_performed") is not fine_performed
        or not isinstance(triple_performed, bool)
        or triple_performed is not fine_performed
        or gate.get("required_triple_component_root_count")
        != endpoint["triple_refinement_root_count"]
        or type(gate.get("required_triple_component_root_count")) is not int
        or gate.get("required_triple_component_source_triangle_count")
        != endpoint["refined_component_source_triangle_count"]
        or type(
            gate.get("required_triple_component_source_triangle_count")
        )
        is not int
        or gate.get("selected_component_shape_valid") is not True
        or selected_component != fine_selection.get("selected_component")
    ):
        raise CoarseComponentDirectionError(
            "TRIPLE static-to-fine-to-triple gate is inconsistent"
        )

    steps = endpoint["tangent_pattern_steps_radians"]
    passes = endpoint["triple_coordinate_passes_per_step"]
    root_candidate_cap = endpoint["tangent_candidates_per_root_per_step"]
    atomic_candidate_cap = endpoint[
        "atomic_triple_cartesian_candidates_per_step_pass"
    ]
    if (
        schedule.get("steps_radians") != steps
        or schedule.get("coordinate_passes_per_step") != passes
        or schedule.get("tangent_candidates_per_root_per_step")
        != root_candidate_cap
        or schedule.get("atomic_cartesian_candidates_per_step_pass")
        != atomic_candidate_cap
        or atomic_candidate_cap
        != root_candidate_cap ** endpoint["triple_refinement_root_count"]
        or summary.get("performed") is not triple_performed
        or summary.get("steps_radians") != steps
        or summary.get("coordinate_passes_per_step") != passes
        or summary.get("tangent_candidates_per_root_per_step")
        != root_candidate_cap
        or summary.get("atomic_cartesian_candidates_per_step_pass")
        != atomic_candidate_cap
        or summary.get("step_pass_records") != records
        or any(
            isinstance(summary.get(field), bool)
            or not isinstance(summary.get(field), int)
            or summary[field] < 0
            for field in (
                "quality_evaluation_count",
                "accepted_move_count",
                "rejected_margin_candidate_count",
                "refined_component_count",
            )
        )
        or summary.get("refined_component_count")
        != (1 if triple_performed else 0)
    ):
        raise CoarseComponentDirectionError(
            "TRIPLE schedule or summary differs from its endpoint"
        )

    integer_evaluation_fields = (
        "static_quality_evaluation_count",
        "fine_quality_evaluation_count",
        "through_fine_handoff_quality_evaluation_count",
        "triple_quality_evaluation_count",
        "triple_quality_evaluation_start_index_exclusive",
        "triple_quality_evaluation_end_index_inclusive",
        "final_replay_and_verification_quality_evaluation_count",
        "total_quality_evaluation_count",
    )
    if any(
        isinstance(evaluations.get(field), bool)
        or not isinstance(evaluations.get(field), int)
        or evaluations[field] < 0
        for field in integer_evaluation_fields
    ):
        raise CoarseComponentDirectionError(
            "TRIPLE evaluation counts are invalid"
        )
    static_count = evaluations["static_quality_evaluation_count"]
    fine_count = evaluations["fine_quality_evaluation_count"]
    through_fine_count = evaluations[
        "through_fine_handoff_quality_evaluation_count"
    ]
    triple_count = evaluations["triple_quality_evaluation_count"]
    triple_start = evaluations[
        "triple_quality_evaluation_start_index_exclusive"
    ]
    triple_end = evaluations[
        "triple_quality_evaluation_end_index_inclusive"
    ]
    final_count = evaluations[
        "final_replay_and_verification_quality_evaluation_count"
    ]
    total_count = evaluations["total_quality_evaluation_count"]
    if (
        static_count <= 0
        or evaluations.get("endpoint_maximum_quality_evaluations")
        != endpoint["maximum_quality_evaluations"]
        or evaluations.get("global_conservative_quality_evaluation_cap")
        != _TRIPLE_REFINEMENT_CONSERVATIVE_QUALITY_EVALUATION_CAP
        or static_count
        != fine_evaluations.get("static_quality_evaluation_count")
        or fine_count != fine_evaluations.get("fine_quality_evaluation_count")
        or through_fine_count
        != fine_evaluations.get("total_quality_evaluation_count")
        or through_fine_count != static_count + fine_count
        or triple_start != through_fine_count
        or triple_end != triple_start + triple_count
        or total_count != triple_end + final_count
        or total_count != search["quality_evaluation_count"]
        or total_count > _TRIPLE_REFINEMENT_CONSERVATIVE_QUALITY_EVALUATION_CAP
        or total_count > endpoint["maximum_quality_evaluations"]
        or final_count != 6 + len(component_evidence)
        or summary.get("quality_evaluation_count") != triple_count
        or sum(
            int(evidence["record"]["quality_evaluation_count"])
            for evidence in component_evidence.values()
        )
        != static_count + fine_count + triple_count
    ):
        raise CoarseComponentDirectionError(
            "TRIPLE phase evaluation budget is inconsistent"
        )

    first_global = evaluations.get("first_triple_quality_evaluation_index")
    last_global = evaluations.get("last_triple_quality_evaluation_index")
    if not triple_performed:
        if (
            selected_component is not None
            or selection.get("handoff") is not None
            or selection.get("final_state") is not None
            or records
            or triple_count != 0
            or first_global is not None
            or last_global is not None
            or triple_end != through_fine_count
            or summary.get("accepted_move_count") != 0
            or summary.get("rejected_margin_candidate_count") != 0
        ):
            raise CoarseComponentDirectionError(
                "TRIPLE zero-failure branch contains refinement evidence"
            )
        return

    selected_id = failing_ids[0]
    selected_evidence = component_evidence[selected_id]
    selected_roots = selected_component.get("root_coordinate_sha256")
    selected_triangles = selected_component.get("source_triangle_sha256")
    if (
        selected_component.get("interaction_component_sha256") != selected_id
        or type(selected_component.get("candidate_root_count")) is not int
        or selected_component.get("candidate_root_count") != 3
        or type(selected_component.get("source_triangle_count")) is not int
        or selected_component.get("source_triangle_count") != 1
        or type(
            selected_component.get("unique_source_triangle_edge_count")
        )
        is not int
        or selected_component.get("unique_source_triangle_edge_count") != 3
        or selected_roots
        != selected_evidence["topology"]["root_coordinate_sha256"]
        or not isinstance(selected_roots, list)
        or selected_roots != sorted(set(selected_roots))
        or len(selected_roots) != 3
        or selected_triangles
        != selected_evidence["record"]["source_triangle_sha256"]
        or not isinstance(selected_triangles, list)
        or len(selected_triangles) != 1
    ):
        raise CoarseComponentDirectionError(
            "TRIPLE selected component differs from the frozen fine component"
        )

    handoff = selection.get("handoff")
    handoff_fields = {
        "selected_component_sha256",
        "fine_exit_coordinate_sha256",
        "triple_entry_coordinate_sha256",
        "fine_exit_quality",
        "fine_exit_quality_sha256",
        "triple_entry_quality_sha256",
        "fine_exit_objective",
        "fine_exit_objective_sha256",
        "triple_entry_objective_sha256",
        "fine_exit_direction_state_sha256",
        "triple_entry_direction_state_sha256",
        "fine_exit_direction_field_sha256",
        "triple_entry_direction_field_sha256",
        "fine_exit_quality_evaluation_index",
        "triple_quality_evaluation_start_index_exclusive",
        "without_reset",
    }
    hash_suffixes = (
        "coordinate_sha256",
        "quality_sha256",
        "objective_sha256",
        "direction_state_sha256",
        "direction_field_sha256",
    )
    if not isinstance(handoff, Mapping) or set(handoff) != handoff_fields:
        raise CoarseComponentDirectionError(
            "TRIPLE fine-to-triple handoff is incomplete"
        )
    fine_exit_hashes = {
        suffix: str(handoff.get(f"fine_exit_{suffix}", "")).casefold()
        for suffix in hash_suffixes
    }
    triple_entry_hashes = {
        suffix: str(handoff.get(f"triple_entry_{suffix}", "")).casefold()
        for suffix in hash_suffixes
    }
    checkpoint = selected_evidence["record"].get("fine_exit_checkpoint")
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
        raise CoarseComponentDirectionError(
            "TRIPLE fine-exit checkpoint is incomplete"
        )
    checkpoint_quality = _validate_fine_quality_snapshot(
        checkpoint.get("selected_state_quality"),
        quality_fields=quality_fields,
        threshold_reference=selected_evidence["static_quality"],
    )
    checkpoint_objective_sha256 = _validate_fine_objective_snapshot(
        checkpoint.get("selected_state_objective"),
        configured_sha256=checkpoint.get("selected_state_objective_sha256"),
        quality=checkpoint_quality,
        objective_fields=endpoint["quality_objective"],
    )
    checkpoint_directions = checkpoint.get("selected_directions")
    direction_record_fields = {
        "root_coordinate_sha256",
        "direction_float_hex",
    }
    normalized_direction_records: list[dict[str, Any]] = []
    if not isinstance(checkpoint_directions, list):
        raise CoarseComponentDirectionError(
            "TRIPLE fine-exit direction inventory is missing"
        )
    for direction_record in checkpoint_directions:
        if (
            not isinstance(direction_record, Mapping)
            or set(direction_record) != direction_record_fields
        ):
            raise CoarseComponentDirectionError(
                "one TRIPLE fine-exit direction is incomplete"
            )
        root_id = str(direction_record.get("root_coordinate_sha256", ""))
        encoded_direction = direction_record.get("direction_float_hex")
        try:
            decoded_direction = [
                float.fromhex(raw) for raw in encoded_direction
            ] if isinstance(encoded_direction, list) else []
        except (TypeError, ValueError) as error:
            raise CoarseComponentDirectionError(
                "one TRIPLE fine-exit direction is not float-hex"
            ) from error
        if (
            _SHA256.fullmatch(root_id) is None
            or len(decoded_direction) != 3
            or any(not math.isfinite(raw) for raw in decoded_direction)
            or any(
                raw.hex() != encoded
                for raw, encoded in zip(decoded_direction, encoded_direction)
            )
            or not math.isclose(
                sum(raw * raw for raw in decoded_direction),
                1.0,
                rel_tol=1.0e-10,
                abs_tol=1.0e-10,
            )
        ):
            raise CoarseComponentDirectionError(
                "one TRIPLE fine-exit direction is stale or non-unit"
            )
        normalized_direction_records.append(copy.deepcopy(dict(direction_record)))
    checkpoint_direction_ids = [
        record["root_coordinate_sha256"]
        for record in normalized_direction_records
    ]
    expected_direction_state_sha256 = _canonical_sha256(
        {
            "encoding": "python-float-hex/root-sha/direction-v1",
            "records": normalized_direction_records,
        }
    )
    expected_direction_field_sha256 = _canonical_sha256(
        {"records": normalized_direction_records}
    )
    checkpoint_hashes = {
        "coordinate_sha256": str(
            checkpoint.get("selected_state_coordinate_sha256", "")
        ).casefold(),
        "quality_sha256": str(
            checkpoint.get("selected_state_quality_sha256", "")
        ).casefold(),
        "objective_sha256": checkpoint_objective_sha256,
        "direction_state_sha256": str(
            checkpoint.get("selected_direction_state_sha256", "")
        ).casefold(),
        "direction_field_sha256": str(
            checkpoint.get("selected_direction_field_sha256", "")
        ).casefold(),
    }
    if (
        handoff.get("selected_component_sha256") != selected_id
        or any(_SHA256.fullmatch(raw) is None for raw in fine_exit_hashes.values())
        or fine_exit_hashes != triple_entry_hashes
        or any(_SHA256.fullmatch(raw) is None for raw in checkpoint_hashes.values())
        or checkpoint_hashes != fine_exit_hashes
        or checkpoint_hashes["quality_sha256"]
        != checkpoint_quality["quality_sha256"]
        or checkpoint_hashes["direction_state_sha256"]
        != expected_direction_state_sha256
        or checkpoint_hashes["direction_field_sha256"]
        != expected_direction_field_sha256
        or checkpoint_direction_ids != selected_roots
        or checkpoint_direction_ids != sorted(set(checkpoint_direction_ids))
        or checkpoint.get("fine_exit_quality_evaluation_index")
        != through_fine_count
        or handoff.get("fine_exit_quality") != checkpoint_quality
        or handoff.get("fine_exit_objective")
        != checkpoint.get("selected_state_objective")
        or handoff.get("fine_exit_quality_evaluation_index")
        != through_fine_count
        or handoff.get("triple_quality_evaluation_start_index_exclusive")
        != through_fine_count
        or handoff.get("without_reset") is not True
    ):
        raise CoarseComponentDirectionError(
            "TRIPLE refinement reset or changed the fine handoff"
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
        "exit_coordinate_sha256",
        "entry_quality_sha256",
        "exit_quality_sha256",
        "entry_objective_sha256",
        "exit_objective_sha256",
        "entry_direction_state_sha256",
        "exit_direction_state_sha256",
        "entry_direction_field_sha256",
        "exit_direction_field_sha256",
    }
    expected_record_count = len(steps) * passes
    if len(records) != expected_record_count:
        raise CoarseComponentDirectionError(
            "TRIPLE step/pass coverage is incomplete"
        )
    previous_hashes = dict(triple_entry_hashes)
    running_index = through_fine_count
    accepted_count = 0
    rejected_count = 0
    observed_triple_count = 0
    observed_first: int | None = None
    observed_last: int | None = None
    for record_index, record in enumerate(records):
        if not isinstance(record, Mapping) or set(record) != record_fields:
            raise CoarseComponentDirectionError(
                "one TRIPLE step/pass record is incomplete"
            )
        expected_step_index = record_index // passes + 1
        expected_pass_index = record_index % passes + 1
        expected_step = float(steps[expected_step_index - 1])
        root_counts = record.get("root_candidate_counts")
        root_count_fields = {"root_coordinate_sha256", "candidate_count"}
        if (
            record.get("step_index_1_based") != expected_step_index
            or record.get("pass_index_1_based") != expected_pass_index
            or isinstance(record.get("step_radians"), bool)
            or not isinstance(record.get("step_radians"), (int, float))
            or float(record["step_radians"]).hex() != expected_step.hex()
            or record.get("step_radians_float_hex") != expected_step.hex()
            or not isinstance(root_counts, list)
            or len(root_counts) != len(selected_roots)
            or any(
                not isinstance(item, Mapping)
                or set(item) != root_count_fields
                for item in root_counts
            )
        ):
            raise CoarseComponentDirectionError(
                "one TRIPLE step/pass identity is invalid"
            )
        root_ids = [str(item["root_coordinate_sha256"]) for item in root_counts]
        candidate_counts = [item["candidate_count"] for item in root_counts]
        if (
            root_ids != selected_roots
            or any(_SHA256.fullmatch(raw) is None for raw in root_ids)
            or any(
                isinstance(raw, bool)
                or not isinstance(raw, int)
                or not 0 <= raw <= root_candidate_cap
                for raw in candidate_counts
            )
        ):
            raise CoarseComponentDirectionError(
                "one TRIPLE root candidate inventory is invalid"
            )
        atomic_count = record.get("atomic_candidate_count")
        rejected_delta = record.get("rejected_margin_candidate_count")
        quality_count = record.get("quality_evaluation_count")
        start_index = record.get("quality_evaluation_start_index_exclusive")
        end_index = record.get("quality_evaluation_end_index_inclusive")
        first_index = record.get("first_quality_evaluation_index")
        last_index = record.get("last_quality_evaluation_index")
        if any(
            isinstance(raw, bool) or not isinstance(raw, int) or raw < 0
            for raw in (atomic_count, rejected_delta, quality_count)
        ):
            raise CoarseComponentDirectionError(
                "one TRIPLE step/pass count is invalid"
            )
        entry_hashes = {
            suffix: str(record.get(f"entry_{suffix}", "")).casefold()
            for suffix in hash_suffixes
        }
        exit_hashes = {
            suffix: str(record.get(f"exit_{suffix}", "")).casefold()
            for suffix in hash_suffixes
        }
        accepted_move = record.get("accepted_move")
        if (
            record.get("candidate_upper_bound") != atomic_candidate_cap
            or atomic_count != math.prod(candidate_counts)
            or atomic_count > atomic_candidate_cap
            or quality_count != atomic_count
            or rejected_delta > len(selected_roots) * root_candidate_cap
            or start_index != running_index
            or end_index != running_index + quality_count
            or (
                quality_count > 0
                and (
                    first_index != running_index + 1
                    or last_index != end_index
                )
            )
            or (
                quality_count == 0
                and (first_index is not None or last_index is not None)
            )
            or any(
                _SHA256.fullmatch(raw) is None
                for raw in (*entry_hashes.values(), *exit_hashes.values())
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
            raise CoarseComponentDirectionError(
                "one TRIPLE step/pass transition is invalid"
            )
        if quality_count > 0:
            observed_first = (
                first_index if observed_first is None else observed_first
            )
            observed_last = last_index
        running_index = end_index
        previous_hashes = exit_hashes
        observed_triple_count += quality_count
        accepted_count += int(accepted_move)
        rejected_count += rejected_delta

    if (
        observed_triple_count != triple_count
        or running_index != triple_end
        or first_global != observed_first
        or last_global != observed_last
        or summary.get("accepted_move_count") != accepted_count
        or summary.get("rejected_margin_candidate_count") != rejected_count
    ):
        raise CoarseComponentDirectionError(
            "TRIPLE step/pass totals or evaluation indices are inconsistent"
        )

    final_state = selection.get("final_state")
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
        raise CoarseComponentDirectionError(
            "TRIPLE final-state evidence is incomplete"
        )
    final_quality = _validate_fine_quality_snapshot(
        final_state.get("selected_state_quality"),
        quality_fields=quality_fields,
        threshold_reference=selected_evidence["static_quality"],
    )
    final_objective_sha256 = _validate_fine_objective_snapshot(
        final_state.get("selected_state_objective"),
        configured_sha256=final_state.get("selected_state_objective_sha256"),
        quality=final_quality,
        objective_fields=endpoint["quality_objective"],
    )
    final_hashes = {
        "coordinate_sha256": str(
            final_state.get("selected_state_coordinate_sha256", "")
        ).casefold(),
        "quality_sha256": str(
            final_state.get("selected_state_quality_sha256", "")
        ).casefold(),
        "objective_sha256": final_objective_sha256,
        "direction_state_sha256": str(
            final_state.get("selected_direction_state_sha256", "")
        ).casefold(),
        "direction_field_sha256": str(
            final_state.get("selected_direction_field_sha256", "")
        ).casefold(),
    }
    if (
        final_state.get("selected_component_sha256") != selected_id
        or any(_SHA256.fullmatch(raw) is None for raw in final_hashes.values())
        or final_hashes != previous_hashes
        or final_hashes["quality_sha256"] != final_quality["quality_sha256"]
        or final_hashes["coordinate_sha256"]
        != selected_evidence["record"]["search_exit_coordinate_sha256"]
        or final_hashes["quality_sha256"]
        != selected_evidence["record"]["search_exit_quality_sha256"]
        or final_state.get("selected_state_objective")
        != selected_evidence["record"]["selected_objective"]
        or final_state.get("triple_quality_evaluation_end_index_inclusive")
        != triple_end
    ):
        raise CoarseComponentDirectionError(
            "TRIPLE final state differs from its component exit"
        )


def _stable_root_id(coordinate: Sequence[float]) -> str:
    if len(coordinate) != 3:
        raise CoarseComponentDirectionError("root coordinate is not 3-D")
    values = [float(value) for value in coordinate]
    if any(not math.isfinite(value) for value in values):
        raise CoarseComponentDirectionError("root coordinate is non-finite")
    payload = json.dumps(
        [round(value, 14) for value in values],
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def build_owner_free_components(
    triangles: Sequence[Sequence[int]],
    *,
    triangle_stable_ids: Mapping[tuple[int, int, int], str],
    triangle_wall_fingerprints: Mapping[tuple[int, int, int], str],
    root_coordinates: Mapping[int, Sequence[float]],
) -> tuple[list[tuple[int, ...]], list[dict[str, Any]]]:
    """Build deterministic components; sharing any root implies connectivity."""

    raw_triangles = list(triangles)
    if any(
        not isinstance(triangle, Sequence)
        or isinstance(triangle, (str, bytes))
        or len(triangle) != 3
        or any(isinstance(root, bool) or not isinstance(root, int) for root in triangle)
        for triangle in raw_triangles
    ):
        raise CoarseComponentDirectionError("bad source triangles are invalid")
    normalized = sorted(
        {tuple(sorted(int(root) for root in triangle)) for triangle in raw_triangles}
    )
    if (
        not normalized
        or len(normalized) != len(raw_triangles)
        or any(len(triangle) != 3 or len(set(triangle)) != 3 for triangle in normalized)
    ):
        raise CoarseComponentDirectionError("bad source triangles are invalid")
    if set(triangle_stable_ids) != set(normalized) or set(
        triangle_wall_fingerprints
    ) != set(normalized):
        raise CoarseComponentDirectionError(
            "component triangle stable lineage is incomplete"
        )
    remaining = set(normalized)
    raw_components: list[set[tuple[int, int, int]]] = []
    while remaining:
        seed = min(remaining)
        component = {seed}
        remaining.remove(seed)
        component_roots = set(seed)
        changed = True
        while changed:
            changed = False
            for triangle in sorted(remaining):
                if component_roots & set(triangle):
                    component.add(triangle)
                    component_roots.update(triangle)
                    remaining.remove(triangle)
                    changed = True
        raw_components.append(component)

    assembled: list[tuple[tuple[int, ...], dict[str, Any]]] = []
    seen_roots: set[int] = set()
    for raw_component in raw_components:
        roots = tuple(sorted({root for triangle in raw_component for root in triangle}))
        if seen_roots & set(roots):
            raise CoarseComponentDirectionError(
                "one runtime root belongs to multiple components"
            )
        seen_roots.update(roots)
        try:
            root_ids = sorted(_stable_root_id(root_coordinates[root]) for root in roots)
        except KeyError as error:
            raise CoarseComponentDirectionError(
                "component root coordinate is missing"
            ) from error
        triangle_ids = sorted(
            str(triangle_stable_ids[triangle]).casefold()
            for triangle in raw_component
        )
        wall_ids = sorted(
            {
                str(triangle_wall_fingerprints[triangle]).casefold()
                for triangle in raw_component
            }
        )
        if (
            len(triangle_ids) != len(set(triangle_ids))
            or any(_SHA256.fullmatch(value) is None for value in triangle_ids)
            or len(root_ids) != len(set(root_ids))
            or any(_SHA256.fullmatch(value) is None for value in root_ids)
            or any(_SHA256.fullmatch(value) is None for value in wall_ids)
        ):
            raise CoarseComponentDirectionError(
                "component stable identifiers are invalid"
            )
        stable: dict[str, Any] = {
            "source_triangle_sha256": triangle_ids,
            "wall_surface_fingerprints": wall_ids,
            "root_coordinate_sha256": root_ids,
            "triangle_count": len(triangle_ids),
            "root_count": len(root_ids),
            "connected_by_shared_root": True,
            "split_by_wall_fingerprint": False,
            "sharp_owner_used": False,
        }
        stable["component_sha256"] = _canonical_sha256(stable)
        assembled.append((roots, stable))
    assembled.sort(key=lambda item: item[1]["component_sha256"])
    return [item[0] for item in assembled], [item[1] for item in assembled]


def validate_component_direction_discovery(
    value: Any,
    *,
    expected_direction_endpoint: Mapping[str, Any],
) -> dict[str, Any]:
    """Strictly validate one owner-free direction discovery result."""

    if not isinstance(value, Mapping):
        raise CoarseComponentDirectionError(
            "component direction discovery must be a mapping"
        )
    expected_keys = {
        "schema",
        "status",
        "profile_complete",
        "audit_only",
        "audit_variant",
        "calibration_PASS_authorized",
        "production_mesh_eligible",
        "mesh_written",
        "runtime_mesh_tags_hardcoded",
        "runtime_tags_are_audit_only",
        "repair_application_performed",
        "repair_application_scope",
        "in_memory_mesh_mutation_performed",
        "final_direction_application_performed",
        "incomplete_reason",
        "observed_counts",
        "stable_observation",
        "stable_observation_sha256",
        "direction_endpoint",
        "search",
        "final_quality",
        "orientation",
        "one_layer_chebyshev",
        "full_layer_detection",
        "subdivision",
    }
    status = value.get("status")
    if (
        set(value) != expected_keys
        or value.get("schema") != DISCOVERY_SCHEMA
        or status not in {"PASS", "INCOMPLETE"}
        or value.get("profile_complete") is not (status == "PASS")
        or value.get("audit_only") is not True
        or value.get("audit_variant")
        != "owner_free_component_direction"
        or value.get("calibration_PASS_authorized") is not False
        or value.get("production_mesh_eligible") is not False
        or value.get("mesh_written") is not False
        or value.get("runtime_mesh_tags_hardcoded") is not False
        or value.get("runtime_tags_are_audit_only") is not True
        or value.get("repair_application_performed") is not True
        or value.get("in_memory_mesh_mutation_performed") is not True
        or value.get("final_direction_application_performed") is not True
    ):
        raise CoarseComponentDirectionError(
            "component direction discovery violates audit-only scope"
        )

    endpoint = value.get("direction_endpoint")
    schedule_endpoint_hash = (
        expected_direction_endpoint.get("schedule_endpoint_sha256")
        if isinstance(expected_direction_endpoint, Mapping)
        else ""
    )
    try:
        expected_endpoint = validate_owner_free_direction_endpoint(
            expected_direction_endpoint,
            expected_schedule_endpoint_sha256=str(schedule_endpoint_hash),
        )
        validated_endpoint = validate_owner_free_direction_endpoint(
            endpoint,
            expected_schedule_endpoint_sha256=str(schedule_endpoint_hash),
        )
    except CoarseDirectionFeasibilityError as error:
        raise CoarseComponentDirectionError(
            f"component direction endpoint is invalid: {error}"
        ) from error
    if validated_endpoint != expected_endpoint:
        raise CoarseComponentDirectionError(
            "component direction endpoint does not match the trusted strategy"
        )

    counts = value.get("observed_counts")
    count_fields = {
        "initial_bad_prism_count",
        "negative_volume_prism_count",
        "cone_node_count",
        "source_prism_count",
        "projected_3d_element_count",
        "initial_below_threshold_prism_count",
        "bad_source_triangle_count",
        "component_count",
        "candidate_root_count",
        "quality_evaluation_count",
        "changed_root_count",
        "final_nonpositive_element_count",
        "final_prism_below_threshold_count",
        "final_core_tetra_below_gamma_count",
        "final_nonfinite_count",
    }
    if (
        not isinstance(counts, Mapping)
        or set(counts) != count_fields
        or any(
            isinstance(raw, bool) or not isinstance(raw, int) or raw < 0
            for raw in counts.values()
        )
        or counts["source_prism_count"] <= 0
        or counts["projected_3d_element_count"] <= 0
        or counts["bad_source_triangle_count"] <= 0
        or counts["component_count"] <= 0
        or counts["candidate_root_count"] <= 0
        or counts["quality_evaluation_count"] <= 0
        or counts["quality_evaluation_count"]
        > int(endpoint["maximum_quality_evaluations"])
        or counts["component_count"] > int(endpoint["maximum_component_count"])
        or counts["candidate_root_count"]
        > int(endpoint["maximum_candidate_root_count"])
    ):
        raise CoarseComponentDirectionError(
            "component direction observed counts are invalid"
        )

    stable = value.get("stable_observation")
    stable_hash = str(value.get("stable_observation_sha256", "")).casefold()
    if (
        not isinstance(stable, Mapping)
        or set(stable)
        != {
            "wall_surface_fingerprint_inventory",
            "bad_source_triangles",
            "components",
        }
        or _SHA256.fullmatch(stable_hash) is None
        or _canonical_sha256(stable) != stable_hash
    ):
        raise CoarseComponentDirectionError(
            "component direction stable observation is stale"
        )

    wall_inventory = stable.get("wall_surface_fingerprint_inventory")
    triangles = stable.get("bad_source_triangles")
    components = stable.get("components")
    if (
        not isinstance(wall_inventory, list)
        or len(wall_inventory) != 48
        or wall_inventory != sorted(set(wall_inventory))
        or any(_SHA256.fullmatch(str(raw)) is None for raw in wall_inventory)
        or not isinstance(triangles, list)
        or len(triangles) != counts["bad_source_triangle_count"]
        or not isinstance(components, list)
        or len(components) != counts["component_count"]
    ):
        raise CoarseComponentDirectionError(
            "component direction stable inventory is incomplete"
        )
    triangle_fields = {
        "source_triangle_sha256",
        "wall_surface_fingerprint",
        "root_coordinate_sha256",
    }
    for record in triangles:
        if (
            not isinstance(record, Mapping)
            or set(record) != triangle_fields
            or _SHA256.fullmatch(str(record.get("source_triangle_sha256", "")))
            is None
            or str(record.get("wall_surface_fingerprint", ""))
            not in wall_inventory
            or not isinstance(record.get("root_coordinate_sha256"), list)
            or len(record["root_coordinate_sha256"]) != 3
            or record["root_coordinate_sha256"]
            != sorted(set(record["root_coordinate_sha256"]))
            or any(
                _SHA256.fullmatch(str(raw)) is None
                for raw in record["root_coordinate_sha256"]
            )
        ):
            raise CoarseComponentDirectionError(
                "one stable bad-triangle record is invalid"
            )
    triangle_ids_in_order = [
        str(record["source_triangle_sha256"]) for record in triangles
    ]
    if triangle_ids_in_order != sorted(set(triangle_ids_in_order)):
        raise CoarseComponentDirectionError(
            "stable bad-triangle records are duplicated or unsorted"
        )
    if any(not isinstance(component, Mapping) for component in components):
        raise CoarseComponentDirectionError("one component is not a mapping")
    if components != sorted(
        components, key=lambda item: str(item.get("component_sha256", ""))
    ):
        raise CoarseComponentDirectionError("stable components are not sorted")
    component_fields = {
        "component_sha256",
        "source_triangle_sha256",
        "wall_surface_fingerprints",
        "root_coordinate_sha256",
        "triangle_count",
        "root_count",
        "connected_by_shared_root",
        "split_by_wall_fingerprint",
        "sharp_owner_used",
    }
    component_triangle_ids: list[str] = []
    component_root_ids: list[str] = []
    triangle_to_component: dict[str, Mapping[str, Any]] = {}
    for component in components:
        unsigned = dict(component)
        component_hash = str(unsigned.pop("component_sha256", "")).casefold()
        component_triangles = component.get("source_triangle_sha256")
        component_walls = component.get("wall_surface_fingerprints")
        component_roots = component.get("root_coordinate_sha256")
        if (
            set(component) != component_fields
            or
            _SHA256.fullmatch(component_hash) is None
            or _canonical_sha256(unsigned) != component_hash
            or component.get("sharp_owner_used") is not False
            or component.get("connected_by_shared_root") is not True
            or component.get("split_by_wall_fingerprint") is not False
            or not isinstance(component_triangles, list)
            or not component_triangles
            or component_triangles != sorted(set(component_triangles))
            or any(_SHA256.fullmatch(str(raw)) is None for raw in component_triangles)
            or not isinstance(component_walls, list)
            or not component_walls
            or component_walls != sorted(set(component_walls))
            or any(str(raw) not in wall_inventory for raw in component_walls)
            or not isinstance(component_roots, list)
            or not component_roots
            or component_roots != sorted(set(component_roots))
            or any(_SHA256.fullmatch(str(raw)) is None for raw in component_roots)
            or isinstance(component.get("triangle_count"), bool)
            or component.get("triangle_count") != len(component_triangles)
            or isinstance(component.get("root_count"), bool)
            or component.get("root_count") != len(component_roots)
        ):
            raise CoarseComponentDirectionError(
                "one stable component is stale or unsafe"
            )
        component_triangle_ids.extend(str(raw) for raw in component_triangles)
        component_root_ids.extend(str(raw) for raw in component_roots)
        for triangle_id in component_triangles:
            if triangle_id in triangle_to_component:
                raise CoarseComponentDirectionError(
                    "one bad triangle belongs to multiple components"
                )
            triangle_to_component[str(triangle_id)] = component

    stable_triangle_ids = [str(record["source_triangle_sha256"]) for record in triangles]
    if (
        sorted(component_triangle_ids) != sorted(stable_triangle_ids)
        or len(component_root_ids) != len(set(component_root_ids))
        or len(component_root_ids) != counts["candidate_root_count"]
    ):
        raise CoarseComponentDirectionError(
            "component coverage does not match the stable observation"
        )
    for record in triangles:
        component = triangle_to_component[str(record["source_triangle_sha256"])]
        if (
            str(record["wall_surface_fingerprint"])
            not in component["wall_surface_fingerprints"]
            or not set(record["root_coordinate_sha256"])
            <= set(component["root_coordinate_sha256"])
        ):
            raise CoarseComponentDirectionError(
                "bad-triangle lineage does not match its component"
            )

    search = value.get("search")
    final_quality = value.get("final_quality")
    search_fields = {
        "schema",
        "status",
        "endpoint_sha256",
        "sharp_owner_used",
        "absolute_coordinate_replay",
        "aba_replay_status",
        "aba_replay",
        "monotonicity_assumed",
        "component_count",
        "candidate_root_count",
        "candidate_direction_count",
        "quality_evaluation_count",
        "coordinate_descent_passes_completed",
        "component_records",
        "changed_roots",
    }
    triple_refinement_endpoint = (
        endpoint.get("schema") == TRIPLE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA
    )
    fine_refinement_endpoint = endpoint.get("schema") in {
        FINE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
        TRIPLE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
    }
    pattern_endpoint = endpoint.get("schema") in {
        PATTERN_ENDPOINT_SCHEMA,
        FRONTIER_PATTERN_ENDPOINT_SCHEMA,
        FINE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
        TRIPLE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
    }
    if pattern_endpoint:
        search_fields.update(
            {
                "tangent_pattern_refinement",
                "abab_replay_status",
                "coordinate_replay",
                "interaction_topology",
                "final_component_recheck",
                "final_low_quality_prisms",
            }
        )
    if fine_refinement_endpoint:
        search_fields.add("fine_refinement_selection")
    if triple_refinement_endpoint:
        search_fields.update(
            {"triple_pattern_refinement", "triple_refinement_selection"}
        )
    final_quality_fields = {
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
        "quality_sha256",
    }
    if pattern_endpoint:
        final_quality_fields.update(
            {
                "maximum_prism_scaled_jacobian_deficit",
                "prism_scaled_jacobian_deficit_l1",
                "prism_scaled_jacobian_deficit_l2",
                "maximum_core_tetra_gamma_deficit",
                "core_tetra_gamma_deficit_l2",
            }
        )
    if (
        not isinstance(search, Mapping)
        or set(search) != search_fields
        or search.get("schema") != SEARCH_SCHEMA
        or search.get("status")
        != ("PASS" if status == "PASS" else "EXHAUSTED")
        or search.get("endpoint_sha256") != endpoint["endpoint_sha256"]
        or search.get("sharp_owner_used") is not False
        or search.get("absolute_coordinate_replay") is not True
        or search.get("aba_replay_status") != "PASS"
        or search.get("monotonicity_assumed") is not False
        or not isinstance(final_quality, Mapping)
        or set(final_quality) != final_quality_fields
    ):
        raise CoarseComponentDirectionError(
            "component direction search evidence is incomplete"
        )
    aba = search.get("aba_replay")
    component_records = search.get("component_records")
    changed_roots = search.get("changed_roots")
    if (
        not isinstance(aba, Mapping)
        or set(aba)
        != {
            "state_a_first_sha256",
            "state_b_sha256",
            "state_a_second_sha256",
            "state_a_reproducible",
        }
        or aba.get("state_a_reproducible") is not True
        or aba.get("state_a_first_sha256") != aba.get("state_a_second_sha256")
        or any(
            _SHA256.fullmatch(str(aba.get(field, ""))) is None
            for field in (
                "state_a_first_sha256",
                "state_b_sha256",
                "state_a_second_sha256",
            )
        )
        or search.get("component_count")
        != (
            search.get("interaction_topology", {}).get(
                "interaction_component_count"
            )
            if pattern_endpoint
            and isinstance(search.get("interaction_topology"), Mapping)
            else counts["component_count"]
        )
        or search.get("candidate_root_count") != counts["candidate_root_count"]
        or search.get("quality_evaluation_count")
        != counts["quality_evaluation_count"]
        or not isinstance(search.get("candidate_direction_count"), int)
        or isinstance(search.get("candidate_direction_count"), bool)
        or search["candidate_direction_count"] <= 0
        or not isinstance(search.get("coordinate_descent_passes_completed"), int)
        or isinstance(search.get("coordinate_descent_passes_completed"), bool)
        or not 0
        <= search["coordinate_descent_passes_completed"]
        <= int(endpoint["coordinate_descent_passes"])
        or not isinstance(component_records, list)
        or len(component_records) != search.get("component_count")
        or not isinstance(changed_roots, list)
        or len(changed_roots) != counts["changed_root_count"]
    ):
        raise CoarseComponentDirectionError(
            "component direction search counts or replay proof are invalid"
        )
    if pattern_endpoint:
        pattern = search.get("tangent_pattern_refinement")
        triple_pattern = (
            search.get("triple_pattern_refinement")
            if triple_refinement_endpoint
            else None
        )
        topology = search.get("interaction_topology")
        coordinate_replay = search.get("coordinate_replay")
        fine_selection = (
            search.get("fine_refinement_selection")
            if fine_refinement_endpoint
            else None
        )
        fine_gate = (
            fine_selection.get("gate")
            if isinstance(fine_selection, Mapping)
            else None
        )
        expected_pattern_performed = (
            fine_gate.get("fine_refinement_performed")
            if isinstance(fine_gate, Mapping)
            else None
        ) if fine_refinement_endpoint else True
        if (
            not isinstance(pattern, Mapping)
            or set(pattern)
            != {
                "performed",
                "steps_radians",
                "coordinate_passes_per_step",
                "shared_triangle_pair_moves",
                "quality_evaluation_count",
                "accepted_move_count",
                "refined_component_count",
                "rejected_margin_candidate_count",
                "minimum_required_cone_margin",
                "minimum_required_changed_direction_margin",
                "entry_minimum_cone_margin",
                "final_minimum_cone_margin",
                "final_minimum_changed_direction_margin",
            }
            or not isinstance(expected_pattern_performed, bool)
            or pattern.get("performed") is not expected_pattern_performed
            or pattern.get("steps_radians")
            != endpoint["tangent_pattern_steps_radians"]
            or pattern.get("coordinate_passes_per_step")
            != endpoint["tangent_coordinate_passes_per_step"]
            or pattern.get("shared_triangle_pair_moves")
            is not endpoint["shared_triangle_pair_moves"]
            or any(
                isinstance(pattern.get(field), bool)
                or not isinstance(pattern.get(field), int)
                or pattern[field] < 0
                for field in (
                    "quality_evaluation_count",
                    "accepted_move_count",
                    "refined_component_count",
                    "rejected_margin_candidate_count",
                )
            )
            or pattern["quality_evaluation_count"]
            > counts["quality_evaluation_count"]
            or pattern["refined_component_count"] > search["component_count"]
            or any(
                isinstance(pattern.get(field), bool)
                or not isinstance(pattern.get(field), (int, float))
                or not math.isfinite(float(pattern[field]))
                or float(pattern[field]) <= 0.0
                for field in (
                    "minimum_required_cone_margin",
                    "minimum_required_changed_direction_margin",
                    "entry_minimum_cone_margin",
                    "final_minimum_cone_margin",
                )
            )
            or pattern["minimum_required_changed_direction_margin"]
            < pattern["minimum_required_cone_margin"]
            or pattern["entry_minimum_cone_margin"]
            < pattern["minimum_required_cone_margin"]
            or pattern["final_minimum_cone_margin"]
            < pattern["minimum_required_cone_margin"]
            or (
                counts["changed_root_count"] == 0
                and pattern["final_minimum_changed_direction_margin"]
                is not None
            )
            or (
                counts["changed_root_count"] > 0
                and (
                    isinstance(
                        pattern["final_minimum_changed_direction_margin"],
                        bool,
                    )
                    or not isinstance(
                        pattern["final_minimum_changed_direction_margin"],
                        (int, float),
                    )
                    or not math.isfinite(
                        float(
                            pattern[
                                "final_minimum_changed_direction_margin"
                            ]
                        )
                    )
                    or float(
                        pattern["final_minimum_changed_direction_margin"]
                    )
                    < float(
                        pattern[
                            "minimum_required_changed_direction_margin"
                        ]
                    )
                )
            )
            or search.get("abab_replay_status") != "PASS"
            or not isinstance(coordinate_replay, Mapping)
            or set(coordinate_replay)
            != {
                "encoding",
                "candidate_root_count",
                "candidate_chain_node_count",
                "state_a_first_coordinate_sha256",
                "state_b_first_coordinate_sha256",
                "state_a_second_coordinate_sha256",
                "state_b_restored_coordinate_sha256",
                "state_a_first_quality_sha256",
                "state_b_first_quality_sha256",
                "state_a_second_quality_sha256",
                "state_b_restored_quality_sha256",
                "state_a_reproducible",
                "state_b_reproducible",
                "a_b_distinct",
            }
            or coordinate_replay.get("encoding")
            != "python-float-hex/root-sha/layer-v1"
            or coordinate_replay.get("candidate_root_count")
            != counts["candidate_root_count"]
            or isinstance(
                coordinate_replay.get("candidate_chain_node_count"), bool
            )
            or not isinstance(
                coordinate_replay.get("candidate_chain_node_count"), int
            )
            or coordinate_replay["candidate_chain_node_count"]
            <= counts["candidate_root_count"]
            or coordinate_replay.get("state_a_reproducible") is not True
            or coordinate_replay.get("state_b_reproducible") is not True
            or coordinate_replay.get("state_a_first_coordinate_sha256")
            != coordinate_replay.get("state_a_second_coordinate_sha256")
            or coordinate_replay.get("state_b_first_coordinate_sha256")
            != coordinate_replay.get("state_b_restored_coordinate_sha256")
            or coordinate_replay.get("state_a_first_quality_sha256")
            != coordinate_replay.get("state_a_second_quality_sha256")
            or coordinate_replay.get("state_b_first_quality_sha256")
            != coordinate_replay.get("state_b_restored_quality_sha256")
            or coordinate_replay.get("a_b_distinct")
            is not (counts["changed_root_count"] > 0)
            or any(
                _SHA256.fullmatch(str(coordinate_replay.get(field, "")))
                is None
                for field in (
                    "state_a_first_coordinate_sha256",
                    "state_b_first_coordinate_sha256",
                    "state_a_second_coordinate_sha256",
                    "state_b_restored_coordinate_sha256",
                    "state_a_first_quality_sha256",
                    "state_b_first_quality_sha256",
                    "state_a_second_quality_sha256",
                    "state_b_restored_quality_sha256",
                )
            )
            or coordinate_replay.get("state_a_first_quality_sha256")
            != aba.get("state_a_first_sha256")
            or coordinate_replay.get("state_b_first_quality_sha256")
            != aba.get("state_b_sha256")
            or coordinate_replay.get("state_a_second_quality_sha256")
            != aba.get("state_a_second_sha256")
        ):
            raise CoarseComponentDirectionError(
                "component tangent-pattern evidence is incomplete"
            )

        topology_fields = {
            "schema",
            "status",
            "base_component_count",
            "interaction_component_count",
            "candidate_root_count",
            "candidate_chain_node_count",
            "affected_prism_count",
            "affected_core_element_count",
            "affected_core_tetra_count",
            "cross_base_component_prism_count",
            "cross_base_component_core_count",
            "base_components_merged",
            "components",
            "interaction_topology_sha256",
        }
        topology_component_fields = {
            "root_coordinate_sha256",
            "candidate_root_count",
            "source_component_sha256",
            "source_component_count",
            "affected_prism_count",
            "affected_core_element_count",
            "affected_core_tetra_count",
            "affected_element_lineage_sha256",
            "connected_by_affected_element_cooccurrence",
            "sharp_owner_used",
            "split_by_wall_fingerprint",
            "interaction_component_sha256",
        }
        topology_unsigned = dict(topology) if isinstance(topology, Mapping) else {}
        topology_hash = str(
            topology_unsigned.pop("interaction_topology_sha256", "")
        ).casefold()
        topology_components = topology.get("components") if isinstance(topology, Mapping) else None
        base_component_ids = {
            str(component["component_sha256"]) for component in components
        }
        seen_interaction_roots: list[str] = []
        seen_source_components: list[str] = []
        interaction_component_ids: list[str] = []
        if (
            not isinstance(topology, Mapping)
            or set(topology) != topology_fields
            or topology.get("schema")
            != "cfdpipe.owner_free_interaction_topology.v1"
            or topology.get("status") != "PASS"
            or _SHA256.fullmatch(topology_hash) is None
            or _canonical_sha256(topology_unsigned) != topology_hash
            or topology.get("base_component_count") != counts["component_count"]
            or topology.get("interaction_component_count")
            != search["component_count"]
            or topology.get("candidate_root_count")
            != counts["candidate_root_count"]
            or topology.get("candidate_chain_node_count")
            != coordinate_replay["candidate_chain_node_count"]
            or not isinstance(topology_components, list)
            or len(topology_components) != search["component_count"]
        ):
            raise CoarseComponentDirectionError(
                "owner-free interaction topology is incomplete"
            )
        for component in topology_components:
            unsigned_component = dict(component) if isinstance(component, Mapping) else {}
            component_hash = str(
                unsigned_component.pop("interaction_component_sha256", "")
            ).casefold()
            roots = component.get("root_coordinate_sha256") if isinstance(component, Mapping) else None
            sources = component.get("source_component_sha256") if isinstance(component, Mapping) else None
            if (
                not isinstance(component, Mapping)
                or set(component) != topology_component_fields
                or _SHA256.fullmatch(component_hash) is None
                or _canonical_sha256(unsigned_component) != component_hash
                or not isinstance(roots, list)
                or roots != sorted(set(roots))
                or not roots
                or any(_SHA256.fullmatch(str(raw)) is None for raw in roots)
                or component.get("candidate_root_count") != len(roots)
                or not isinstance(sources, list)
                or sources != sorted(set(sources))
                or not sources
                or not set(sources) <= base_component_ids
                or component.get("source_component_count") != len(sources)
                or any(
                    isinstance(component.get(field), bool)
                    or not isinstance(component.get(field), int)
                    or component[field] < 0
                    for field in (
                        "affected_prism_count",
                        "affected_core_element_count",
                        "affected_core_tetra_count",
                    )
                )
                or component["affected_prism_count"] <= 0
                or component["affected_core_element_count"] <= 0
                or component["affected_core_tetra_count"] <= 0
                or component["affected_core_tetra_count"]
                > component["affected_core_element_count"]
                or _SHA256.fullmatch(
                    str(component.get("affected_element_lineage_sha256", ""))
                )
                is None
                or component.get("connected_by_affected_element_cooccurrence")
                is not True
                or component.get("sharp_owner_used") is not False
                or component.get("split_by_wall_fingerprint") is not False
            ):
                raise CoarseComponentDirectionError(
                    "one interaction component is stale or unsafe"
                )
            seen_interaction_roots.extend(str(raw) for raw in roots)
            seen_source_components.extend(str(raw) for raw in sources)
            interaction_component_ids.append(component_hash)
        if (
            topology_components
            != sorted(
                topology_components,
                key=lambda item: item["interaction_component_sha256"],
            )
            or sorted(seen_interaction_roots)
            != sorted(set(component_root_ids))
            or set(seen_source_components) != base_component_ids
            or interaction_component_ids != sorted(set(interaction_component_ids))
            or any(
                isinstance(topology.get(field), bool)
                or not isinstance(topology.get(field), int)
                or topology[field] < 0
                for field in (
                    "affected_prism_count",
                    "affected_core_element_count",
                    "affected_core_tetra_count",
                    "cross_base_component_prism_count",
                    "cross_base_component_core_count",
                )
            )
            or topology["affected_prism_count"]
            != sum(item["affected_prism_count"] for item in topology_components)
            or topology["affected_core_element_count"]
            != sum(item["affected_core_element_count"] for item in topology_components)
            or topology["affected_core_tetra_count"]
            != sum(item["affected_core_tetra_count"] for item in topology_components)
            or topology.get("base_components_merged")
            is not (
                topology["interaction_component_count"]
                < topology["base_component_count"]
            )
        ):
            raise CoarseComponentDirectionError(
                "interaction component coverage is inconsistent"
            )

    component_id_field = (
        "interaction_component_sha256"
        if pattern_endpoint
        else "component_sha256"
    )
    component_record_fields = {
        component_id_field,
        "candidate_root_count",
        "joint_candidate_count",
        "coordinate_candidate_count",
        "quality_evaluation_count",
        "selected_objective",
    }
    if pattern_endpoint:
        component_record_fields.update(
            {
                "search_exit_quality_sha256",
                "search_exit_coordinate_sha256",
            }
        )
    if fine_refinement_endpoint:
        component_record_fields.update(
            {
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
        )
    if component_records != sorted(
        component_records, key=lambda item: str(item.get(component_id_field, ""))
        if isinstance(item, Mapping) else ""
    ):
        raise CoarseComponentDirectionError("search component records are not sorted")
    stable_component_by_id = (
        {
            str(component["interaction_component_sha256"]): component
            for component in search["interaction_topology"]["components"]
        }
        if pattern_endpoint
        else {
            str(component["component_sha256"]): component
            for component in components
        }
    )
    component_record_ids: list[str] = []
    fine_component_evidence: dict[str, dict[str, Any]] = {}
    triple_selected_component_id = None
    if triple_refinement_endpoint:
        configured_fine_selection = search.get("fine_refinement_selection")
        configured_selected_component = (
            configured_fine_selection.get("selected_component")
            if isinstance(configured_fine_selection, Mapping)
            else None
        )
        if isinstance(configured_selected_component, Mapping):
            triple_selected_component_id = str(
                configured_selected_component.get(
                    "interaction_component_sha256", ""
                )
            )
    base_component_by_id = {
        str(component["component_sha256"]): component for component in components
    }
    for record in component_records:
        component_id = str(record.get(component_id_field, "")) if isinstance(
            record, Mapping
        ) else ""
        expected_component_record_fields = set(component_record_fields)
        if (
            triple_refinement_endpoint
            and component_id == triple_selected_component_id
        ):
            expected_component_record_fields.add("fine_exit_checkpoint")
        if (
            not isinstance(record, Mapping)
            or set(record) != expected_component_record_fields
            or component_id not in stable_component_by_id
            or record.get("candidate_root_count")
            != stable_component_by_id[component_id][
                "candidate_root_count" if pattern_endpoint else "root_count"
            ]
            or any(
                isinstance(record.get(field), bool)
                or not isinstance(record.get(field), int)
                or record[field] < 0
                for field in (
                    "joint_candidate_count",
                    "coordinate_candidate_count",
                    "quality_evaluation_count",
                )
            )
            or not isinstance(record.get("selected_objective"), list)
            or len(record["selected_objective"])
            != (len(endpoint["quality_objective"]) if pattern_endpoint else 7)
            or (
                pattern_endpoint
                and any(
                    _SHA256.fullmatch(str(record.get(field, ""))) is None
                    for field in (
                        "search_exit_quality_sha256",
                        "search_exit_coordinate_sha256",
                    )
                )
            )
            or any(
                isinstance(raw, bool)
                or not isinstance(raw, (int, float))
                or not math.isfinite(float(raw))
                for raw in record["selected_objective"]
            )
        ):
            raise CoarseComponentDirectionError(
                "one search component record is invalid"
            )
        if fine_refinement_endpoint:
            topology_record = stable_component_by_id[component_id]
            source_component_ids = topology_record["source_component_sha256"]
            expected_source_triangles = sorted(
                {
                    str(triangle_sha256)
                    for source_component_id in source_component_ids
                    for triangle_sha256 in base_component_by_id[
                        str(source_component_id)
                    ]["source_triangle_sha256"]
                }
            )
            source_triangles = record.get("source_triangle_sha256")
            static_quality = _validate_fine_quality_snapshot(
                record.get("static_exit_quality"),
                quality_fields=final_quality_fields,
                threshold_reference=final_quality,
            )
            static_objective_sha256 = _validate_fine_objective_snapshot(
                record.get("static_exit_objective"),
                configured_sha256=record.get("static_exit_objective_sha256"),
                quality=static_quality,
                objective_fields=endpoint["quality_objective"],
            )
            static_index = record.get("static_exit_quality_evaluation_index")
            static_quality_sha256 = str(
                record.get("static_exit_quality_sha256", "")
            ).casefold()
            static_coordinate_sha256 = str(
                record.get("static_exit_coordinate_sha256", "")
            ).casefold()
            if (
                isinstance(record.get("source_triangle_count"), bool)
                or not isinstance(record.get("source_triangle_count"), int)
                or record["source_triangle_count"] <= 0
                or not isinstance(source_triangles, list)
                or source_triangles != sorted(set(source_triangles))
                or source_triangles != expected_source_triangles
                or record["source_triangle_count"] != len(source_triangles)
                or any(
                    _SHA256.fullmatch(str(raw)) is None
                    for raw in source_triangles
                )
                or _SHA256.fullmatch(static_quality_sha256) is None
                or static_quality_sha256 != static_quality["quality_sha256"]
                or _SHA256.fullmatch(static_coordinate_sha256) is None
                or record.get("static_exit_status")
                != static_quality["status"]
                or static_quality["all_values_finite"] is not True
                or static_quality["nonfinite_count"] != 0
                or static_quality["nonpositive_element_count"] != 0
                or static_quality["core_tetra_below_gamma_count"] != 0
                or isinstance(static_index, bool)
                or not isinstance(static_index, int)
                or static_index <= 0
                or static_index > counts["quality_evaluation_count"]
                or record["quality_evaluation_count"] <= 0
            ):
                raise CoarseComponentDirectionError(
                    "one FINE static component record is stale or unsafe"
                )
            fine_component_evidence[component_id] = {
                "record": record,
                "topology": topology_record,
                "static_quality": static_quality,
                "static_quality_sha256": static_quality_sha256,
                "static_objective_sha256": static_objective_sha256,
                "static_coordinate_sha256": static_coordinate_sha256,
                "static_index": static_index,
            }
        component_record_ids.append(component_id)
    if set(component_record_ids) != set(stable_component_by_id):
        raise CoarseComponentDirectionError(
            "search component coverage is incomplete"
        )
    if fine_refinement_endpoint:
        _validate_fine_refinement_selection(
            search.get("fine_refinement_selection"),
            component_evidence=fine_component_evidence,
            endpoint=endpoint,
            search=search,
            pattern=pattern,
            quality_fields=final_quality_fields,
            triple_refinement_endpoint=triple_refinement_endpoint,
            triple_pattern=triple_pattern,
        )
    if triple_refinement_endpoint:
        _validate_triple_refinement_selection(
            search.get("triple_refinement_selection"),
            fine_selection=search["fine_refinement_selection"],
            component_evidence=fine_component_evidence,
            endpoint=endpoint,
            search=search,
            summary=triple_pattern,
            quality_fields=final_quality_fields,
        )
    changed_root_fields = {
        "root_coordinate_sha256",
        "original_direction",
        "selected_direction",
        "candidate_count",
        "selected_source",
        "selected_interior_weight",
    }
    changed_ids: list[str] = []
    for record in changed_roots:
        if (
            not isinstance(record, Mapping)
            or set(record) != changed_root_fields
            or _SHA256.fullmatch(str(record.get("root_coordinate_sha256", "")))
            is None
            or not isinstance(record.get("candidate_count"), int)
            or isinstance(record.get("candidate_count"), bool)
            or record["candidate_count"] <= 0
            or not isinstance(record.get("selected_source"), str)
            or not record["selected_source"]
            or record["selected_source"] not in endpoint["candidate_sources"]
            or isinstance(record.get("selected_interior_weight"), bool)
            or not isinstance(record.get("selected_interior_weight"), (int, float))
            or not math.isfinite(float(record["selected_interior_weight"]))
            or float(record["selected_interior_weight"])
            not in [float(raw) for raw in endpoint["original_direction_interior_weights"]]
        ):
            raise CoarseComponentDirectionError("one changed-root record is invalid")
        for field in ("original_direction", "selected_direction"):
            vector = record.get(field)
            if (
                not isinstance(vector, list)
                or len(vector) != 3
                or any(
                    isinstance(raw, bool)
                    or not isinstance(raw, (int, float))
                    or not math.isfinite(float(raw))
                    for raw in vector
                )
            ):
                raise CoarseComponentDirectionError(
                    "one changed-root direction is invalid"
                )
        original_direction = record["original_direction"]
        selected_direction = record["selected_direction"]
        if (
            not math.isclose(
                sum(float(raw) ** 2 for raw in original_direction),
                1.0,
                rel_tol=1.0e-10,
                abs_tol=1.0e-10,
            )
            or not math.isclose(
                sum(float(raw) ** 2 for raw in selected_direction),
                1.0,
                rel_tol=1.0e-10,
                abs_tol=1.0e-10,
            )
            or all(
                math.isclose(
                    float(left), float(right), rel_tol=0.0, abs_tol=1.0e-14
                )
                for left, right in zip(original_direction, selected_direction)
            )
        ):
            raise CoarseComponentDirectionError(
                "changed-root directions are not distinct unit vectors"
            )
        changed_ids.append(str(record["root_coordinate_sha256"]))
    if changed_ids != sorted(set(changed_ids)) or not set(changed_ids) <= set(
        component_root_ids
    ):
        raise CoarseComponentDirectionError(
            "changed-root coverage is invalid"
        )

    if pattern_endpoint:
        recheck = search.get("final_component_recheck")
        recheck_fields = {
            "schema",
            "status",
            "component_count",
            "quality_evaluation_count",
            "all_records_final_state",
            "component_records",
            "final_component_recheck_sha256",
        }
        recheck_record_fields = {
            "interaction_component_sha256",
            "selected_state_coordinate_sha256",
            "affected_prism_count",
            "affected_core_element_count",
            "affected_core_tetra_count",
            "quality",
            "final_state_objective",
            "quality_evaluation_index",
        }
        recheck_unsigned = dict(recheck) if isinstance(recheck, Mapping) else {}
        recheck_hash = str(
            recheck_unsigned.pop("final_component_recheck_sha256", "")
        ).casefold()
        recheck_records = recheck.get("component_records") if isinstance(recheck, Mapping) else None
        exit_record_by_id = {
            str(record["interaction_component_sha256"]): record
            for record in component_records
        }
        topology_component_by_id = {
            str(record["interaction_component_sha256"]): record
            for record in topology_components
        }
        if (
            not isinstance(recheck, Mapping)
            or set(recheck) != recheck_fields
            or recheck.get("schema")
            != "cfdpipe.owner_free_final_component_recheck.v1"
            or recheck.get("status") != "PASS"
            or recheck.get("component_count") != search["component_count"]
            or recheck.get("quality_evaluation_count")
            != search["component_count"]
            or recheck.get("all_records_final_state") is not True
            or _SHA256.fullmatch(recheck_hash) is None
            or _canonical_sha256(recheck_unsigned) != recheck_hash
            or not isinstance(recheck_records, list)
            or len(recheck_records) != search["component_count"]
            or recheck_records
            != sorted(
                recheck_records,
                key=lambda item: str(
                    item.get("interaction_component_sha256", "")
                )
                if isinstance(item, Mapping)
                else "",
            )
        ):
            raise CoarseComponentDirectionError(
                "final interaction-component recheck is incomplete"
            )
        recheck_ids: list[str] = []
        for record in recheck_records:
            component_sha = str(
                record.get("interaction_component_sha256", "")
            ) if isinstance(record, Mapping) else ""
            quality = record.get("quality") if isinstance(record, Mapping) else None
            unsigned_component_quality = (
                dict(quality) if isinstance(quality, Mapping) else {}
            )
            component_quality_hash = str(
                unsigned_component_quality.pop("quality_sha256", "")
            ).casefold()
            topology_record = topology_component_by_id.get(component_sha)
            if (
                not isinstance(record, Mapping)
                or set(record) != recheck_record_fields
                or component_sha not in topology_component_by_id
                or _SHA256.fullmatch(
                    str(record.get("selected_state_coordinate_sha256", ""))
                )
                is None
                or record.get("selected_state_coordinate_sha256")
                != exit_record_by_id[component_sha][
                    "search_exit_coordinate_sha256"
                ]
                or any(
                    record.get(field) != topology_record[field]
                    for field in (
                        "affected_prism_count",
                        "affected_core_element_count",
                        "affected_core_tetra_count",
                    )
                )
                or not isinstance(quality, Mapping)
                or set(quality) != final_quality_fields
                or _SHA256.fullmatch(component_quality_hash) is None
                or _canonical_sha256(unsigned_component_quality)
                != component_quality_hash
                or component_quality_hash
                != exit_record_by_id[component_sha][
                    "search_exit_quality_sha256"
                ]
                or not isinstance(record.get("final_state_objective"), list)
                or len(record["final_state_objective"])
                != len(endpoint["quality_objective"])
                or any(
                    isinstance(raw, bool)
                    or not isinstance(raw, (int, float))
                    or not math.isfinite(float(raw))
                    for raw in record["final_state_objective"]
                )
                or isinstance(record.get("quality_evaluation_index"), bool)
                or not isinstance(record.get("quality_evaluation_index"), int)
                or record["quality_evaluation_index"] <= 0
            ):
                raise CoarseComponentDirectionError(
                    "one final component recheck record is invalid"
                )
            recheck_ids.append(component_sha)
        if recheck_ids != interaction_component_ids:
            raise CoarseComponentDirectionError(
                "final component recheck coverage is incomplete"
            )

        inventory = search.get("final_low_quality_prisms")
        inventory_fields = {
            "schema",
            "status",
            "minimum_scaled_jacobian_for_pass",
            "final_low_quality_prism_count",
            "lineage_quality_query_count",
            "records",
            "final_low_quality_prism_inventory_sha256",
        }
        low_record_fields = {
            "kind",
            "wall_surface_fingerprint",
            "source_triangle_sha256",
            "root_coordinate_sha256",
            "layer_1_based",
            "prism_lineage_sha256",
            "interaction_component_sha256",
            "candidate_root_coordinate_sha256",
            "minimum_scaled_jacobian",
            "quality_class",
            "was_initial_candidate",
        }
        inventory_unsigned = dict(inventory) if isinstance(inventory, Mapping) else {}
        inventory_hash = str(
            inventory_unsigned.pop(
                "final_low_quality_prism_inventory_sha256", ""
            )
        ).casefold()
        low_records = inventory.get("records") if isinstance(inventory, Mapping) else None
        if (
            not isinstance(inventory, Mapping)
            or set(inventory) != inventory_fields
            or inventory.get("schema")
            != "cfdpipe.owner_free_low_quality_prism_inventory.v1"
            or inventory.get("status") != "PASS"
            or inventory.get("minimum_scaled_jacobian_for_pass")
            != final_quality.get("minimum_prism_scaled_jacobian_for_pass")
            or inventory.get("final_low_quality_prism_count")
            != final_quality.get("prism_below_threshold_element_count")
            or inventory.get("lineage_quality_query_count") != 1
            or _SHA256.fullmatch(inventory_hash) is None
            or _canonical_sha256(inventory_unsigned) != inventory_hash
            or not isinstance(low_records, list)
            or len(low_records)
            != inventory.get("final_low_quality_prism_count")
            or low_records
            != sorted(
                low_records,
                key=lambda item: str(item.get("prism_lineage_sha256", ""))
                if isinstance(item, Mapping)
                else "",
            )
        ):
            raise CoarseComponentDirectionError(
                "final low-quality Prism inventory is incomplete"
            )
        low_ids: list[str] = []
        for record in low_records:
            roots = record.get("root_coordinate_sha256") if isinstance(record, Mapping) else None
            candidates = record.get("candidate_root_coordinate_sha256") if isinstance(record, Mapping) else None
            stable_lineage = {
                field: record.get(field)
                for field in (
                    "kind",
                    "wall_surface_fingerprint",
                    "source_triangle_sha256",
                    "root_coordinate_sha256",
                    "layer_1_based",
                )
            } if isinstance(record, Mapping) else {}
            quality_class = record.get("quality_class") if isinstance(record, Mapping) else None
            minimum_value = record.get("minimum_scaled_jacobian") if isinstance(record, Mapping) else None
            if (
                not isinstance(record, Mapping)
                or set(record) != low_record_fields
                or record.get("kind") != "Prism6"
                or _SHA256.fullmatch(
                    str(record.get("wall_surface_fingerprint", ""))
                )
                is None
                or _SHA256.fullmatch(
                    str(record.get("source_triangle_sha256", ""))
                )
                is None
                or not isinstance(roots, list)
                or roots != sorted(set(roots))
                or len(roots) != 3
                or any(_SHA256.fullmatch(str(raw)) is None for raw in roots)
                or isinstance(record.get("layer_1_based"), bool)
                or not isinstance(record.get("layer_1_based"), int)
                or record["layer_1_based"] <= 0
                or _SHA256.fullmatch(
                    str(record.get("prism_lineage_sha256", ""))
                )
                is None
                or _canonical_sha256(stable_lineage)
                != record["prism_lineage_sha256"]
                or record.get("interaction_component_sha256")
                not in topology_component_by_id
                or not isinstance(candidates, list)
                or candidates != sorted(set(candidates))
                or not candidates
                or not set(candidates) <= set(roots)
                or quality_class not in {"BELOW_THRESHOLD", "NONFINITE"}
                or (
                    quality_class == "BELOW_THRESHOLD"
                    and (
                        isinstance(minimum_value, bool)
                        or not isinstance(minimum_value, (int, float))
                        or not math.isfinite(float(minimum_value))
                        or float(minimum_value)
                        >= float(
                            inventory["minimum_scaled_jacobian_for_pass"]
                        )
                    )
                )
                or (
                    quality_class == "NONFINITE" and minimum_value is not None
                )
                or not isinstance(record.get("was_initial_candidate"), bool)
            ):
                raise CoarseComponentDirectionError(
                    "one final low-quality Prism lineage is invalid"
                )
            low_ids.append(str(record["prism_lineage_sha256"]))
        if low_ids != sorted(set(low_ids)):
            raise CoarseComponentDirectionError(
                "final low-quality Prism lineage is not unique"
            )

    unsigned_quality = dict(final_quality)
    quality_hash = str(unsigned_quality.pop("quality_sha256", "")).casefold()
    if (
        _SHA256.fullmatch(quality_hash) is None
        or _canonical_sha256(unsigned_quality) != quality_hash
        or final_quality.get("status")
        != (
            "PASS"
            if counts["final_nonpositive_element_count"] == 0
            and counts["final_prism_below_threshold_count"] == 0
            and counts["final_core_tetra_below_gamma_count"] == 0
            and counts["final_nonfinite_count"] == 0
            else "FAIL"
        )
        or final_quality.get("nonpositive_element_count")
        != counts["final_nonpositive_element_count"]
        or final_quality.get("prism_below_threshold_element_count")
        != counts["final_prism_below_threshold_count"]
        or final_quality.get("core_tetra_below_gamma_count")
        != counts["final_core_tetra_below_gamma_count"]
        or final_quality.get("nonfinite_count") != counts["final_nonfinite_count"]
        or final_quality.get("all_values_finite")
        is not (counts["final_nonfinite_count"] == 0)
        or any(
            isinstance(final_quality.get(field), bool)
            or not isinstance(final_quality.get(field), int)
            or final_quality[field] <= 0
            for field in (
                "prism_element_count",
                "core_element_count",
                "core_tetra_count",
            )
        )
        or any(
            isinstance(final_quality.get(field), bool)
            or not isinstance(final_quality.get(field), (int, float))
            or not math.isfinite(float(final_quality[field]))
            for field in (
                "minimum_prism_scaled_jacobian_for_pass",
                "minimum_core_tetra_gamma_for_pass",
                "minimum_prism_scaled_jacobian",
                "minimum_core_tetra_gamma",
            )
        )
        or final_quality.get("prism_element_count")
        + final_quality.get("core_element_count")
        != counts["projected_3d_element_count"]
        or final_quality.get("core_tetra_count")
        > final_quality.get("core_element_count")
        or final_quality.get("prism_below_threshold_element_count")
        > final_quality.get("prism_element_count")
        or final_quality.get("core_tetra_below_gamma_count")
        > final_quality.get("core_tetra_count")
        or (
            pattern_endpoint
            and any(
                isinstance(final_quality.get(field), bool)
                or not isinstance(final_quality.get(field), (int, float))
                or not math.isfinite(float(final_quality[field]))
                or float(final_quality[field]) < 0.0
                for field in (
                    "maximum_prism_scaled_jacobian_deficit",
                    "prism_scaled_jacobian_deficit_l1",
                    "prism_scaled_jacobian_deficit_l2",
                    "maximum_core_tetra_gamma_deficit",
                    "core_tetra_gamma_deficit_l2",
                )
            )
        )
    ):
        raise CoarseComponentDirectionError(
            "final component direction quality is stale or invalid"
        )

    proof_fields = {
        "orientation": {
            "initial_quality",
            "reversed_prism_count",
            "unordered_cell_node_sets_unchanged",
        },
        "one_layer_chebyshev": {
            "violating_root_count",
            "minimum_required_margin",
            "postrepair_quality",
        },
        "full_layer_detection": {
            "initial_quality",
            "minimum_scaled_jacobian_for_repair",
            "bad_prism_count",
            "below_threshold_prism_count",
            "bad_source_triangle_count",
        },
        "subdivision": {
            "surface_schedule_binding",
            "surface_schedule_binding_sha256",
            "schedule_endpoint_sha256",
            "layer_count",
            "root_count",
            "source_prism_count",
            "projected_3d_element_count",
        },
    }
    for field, keys in proof_fields.items():
        record = value.get(field)
        if not isinstance(record, Mapping) or set(record) != keys:
            raise CoarseComponentDirectionError(
                f"component direction {field} proof is incomplete"
            )
    initial_orientation_quality = value["orientation"].get("initial_quality")
    one_layer_quality = value["one_layer_chebyshev"].get("postrepair_quality")
    full_layer_quality = value["full_layer_detection"].get("initial_quality")
    if (
        not isinstance(initial_orientation_quality, Mapping)
        or initial_orientation_quality.get("bad_element_count_union")
        != counts["initial_bad_prism_count"]
        or
        value["orientation"].get("reversed_prism_count")
        != counts["negative_volume_prism_count"]
        or value["orientation"].get("unordered_cell_node_sets_unchanged") is not True
        or value["one_layer_chebyshev"].get("violating_root_count")
        != counts["cone_node_count"]
        or not isinstance(one_layer_quality, Mapping)
        or one_layer_quality.get("bad_element_count_union") != 0
        or not isinstance(full_layer_quality, Mapping)
        or isinstance(value["full_layer_detection"].get("bad_prism_count"), bool)
        or not isinstance(
            value["full_layer_detection"].get("bad_prism_count"), int
        )
        or value["full_layer_detection"]["bad_prism_count"] < 0
        or value["full_layer_detection"].get("below_threshold_prism_count")
        != counts["initial_below_threshold_prism_count"]
        or value["full_layer_detection"].get("bad_source_triangle_count")
        != counts["bad_source_triangle_count"]
        or value["subdivision"].get("source_prism_count")
        != counts["source_prism_count"]
        or value["subdivision"].get("projected_3d_element_count")
        != counts["projected_3d_element_count"]
        or value["subdivision"].get("schedule_endpoint_sha256")
        != endpoint["schedule_endpoint_sha256"]
        or not isinstance(
            value["subdivision"].get("surface_schedule_binding"), Mapping
        )
        or value["subdivision"]["surface_schedule_binding"].get(
            "binding_sha256"
        )
        != value["subdivision"].get("surface_schedule_binding_sha256")
    ):
        raise CoarseComponentDirectionError(
            "component direction proof counts are inconsistent"
        )
    quality_pass = (
        counts["final_nonpositive_element_count"] == 0
        and counts["final_prism_below_threshold_count"] == 0
        and counts["final_core_tetra_below_gamma_count"] == 0
        and counts["final_nonfinite_count"] == 0
        and final_quality.get("status") == "PASS"
    )
    if status == "PASS":
        if (
            not quality_pass
            or value.get("repair_application_scope")
            != "IN_MEMORY_OWNER_FREE_DIRECTION_COMPLETE_AUDIT_ONLY"
            or value.get("incomplete_reason") is not None
        ):
            raise CoarseComponentDirectionError(
                "component direction PASS lacks complete quality evidence"
            )
    elif (
        quality_pass
        or value.get("repair_application_scope")
        != "IN_MEMORY_OWNER_FREE_DIRECTION_EXHAUSTED_AUDIT_ONLY"
        or value.get("incomplete_reason")
        != "OWNER_FREE_DIRECTION_SEARCH_EXHAUSTED"
    ):
        raise CoarseComponentDirectionError(
            "component direction INCOMPLETE is inconsistent"
        )

    def reject_runtime_keys(raw: Any) -> None:
        if isinstance(raw, Mapping):
            for key, nested in raw.items():
                lowered = str(key).casefold()
                if lowered.endswith("_tag") or lowered.endswith("_tags") or (
                    "audit" in lowered
                ):
                    raise CoarseComponentDirectionError(
                        "runtime tags are forbidden in stable direction evidence"
                    )
                reject_runtime_keys(nested)
        elif isinstance(raw, list):
            for nested in raw:
                reject_runtime_keys(nested)

    reject_runtime_keys(stable)
    return copy.deepcopy(dict(value))


__all__ = [
    "CoarseComponentDirectionError",
    "DISCOVERY_SCHEMA",
    "SEARCH_SCHEMA",
    "build_owner_free_components",
    "validate_component_direction_discovery",
]
