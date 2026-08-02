from __future__ import annotations

import copy
import hashlib
import json
import unittest

from cfdpipe.coarse_component_direction import (
    CoarseComponentDirectionError,
    DISCOVERY_SCHEMA,
    SEARCH_SCHEMA,
    build_owner_free_components,
    validate_component_direction_discovery,
)
from cfdpipe.coarse_direction_feasibility import (
    make_owner_free_direction_endpoint,
    make_owner_free_direction_frontier_fine_refinement_pattern_endpoint,
    make_owner_free_direction_frontier_pattern_endpoint,
    make_owner_free_direction_frontier_triple_refinement_pattern_endpoint,
    make_owner_free_direction_pattern_endpoint,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _canonical(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()


class CoarseComponentDirectionTests(unittest.TestCase):
    def test_shared_root_connects_across_wall_fingerprints_deterministically(self) -> None:
        triangles = [(1, 2, 3), (3, 4, 5), (8, 9, 10)]
        ids = {triangle: _sha(f"triangle-{index}") for index, triangle in enumerate(triangles)}
        walls = {
            triangles[0]: _sha("wall-a"),
            triangles[1]: _sha("wall-b"),
            triangles[2]: _sha("wall-c"),
        }
        coordinates = {
            root: (float(root), float(root % 2), float(root % 3))
            for root in {value for triangle in triangles for value in triangle}
        }
        runtime, stable = build_owner_free_components(
            list(reversed(triangles)),
            triangle_stable_ids=ids,
            triangle_wall_fingerprints=walls,
            root_coordinates=coordinates,
        )
        replay_runtime, replay_stable = build_owner_free_components(
            triangles,
            triangle_stable_ids=ids,
            triangle_wall_fingerprints=walls,
            root_coordinates=coordinates,
        )
        self.assertEqual(runtime, replay_runtime)
        self.assertEqual(stable, replay_stable)
        self.assertEqual(2, len(runtime))
        connected = next(component for component in runtime if 3 in component)
        self.assertEqual((1, 2, 3, 4, 5), connected)
        all_roots = [root for component in runtime for root in component]
        self.assertEqual(len(all_roots), len(set(all_roots)))
        connected_record = stable[runtime.index(connected)]
        self.assertEqual(2, len(connected_record["wall_surface_fingerprints"]))
        self.assertFalse(connected_record["split_by_wall_fingerprint"])
        self.assertFalse(connected_record["sharp_owner_used"])

    def test_component_builder_rejects_non_triangle_and_non_integer_roots(self) -> None:
        for triangle in ((1, 1, 2, 3), (1, 2, True), (1, 2, 3.0)):
            with self.subTest(triangle=triangle):
                with self.assertRaises(CoarseComponentDirectionError):
                    build_owner_free_components(
                        [triangle],
                        triangle_stable_ids={},
                        triangle_wall_fingerprints={},
                        root_coordinates={},
                    )

    def _discovery(self):
        endpoint = make_owner_free_direction_endpoint(
            schedule_endpoint_sha256="a" * 64
        )
        triangle = (1, 2, 3)
        runtime, components = build_owner_free_components(
            [triangle],
            triangle_stable_ids={triangle: _sha("triangle")},
            triangle_wall_fingerprints={triangle: _sha("wall-0")},
            root_coordinates={
                1: (0.0, 0.0, 0.0),
                2: (1.0, 0.0, 0.0),
                3: (0.0, 1.0, 0.0),
            },
        )
        self.assertEqual(1, len(runtime))
        walls = sorted(_sha(f"wall-{index}") for index in range(48))
        triangle_record = {
            "source_triangle_sha256": _sha("triangle"),
            "wall_surface_fingerprint": _sha("wall-0"),
            "root_coordinate_sha256": components[0][
                "root_coordinate_sha256"
            ],
        }
        stable = {
            "wall_surface_fingerprint_inventory": walls,
            "bad_source_triangles": [triangle_record],
            "components": components,
        }
        quality_unsigned = {
            "status": "PASS",
            "minimum_prism_scaled_jacobian_for_pass": 0.01,
            "minimum_core_tetra_gamma_for_pass": 0.001,
            "all_values_finite": True,
            "nonfinite_count": 0,
            "nonpositive_element_count": 0,
            "prism_below_threshold_element_count": 0,
            "core_tetra_below_gamma_count": 0,
            "minimum_prism_scaled_jacobian": 0.02,
            "minimum_core_tetra_gamma": 0.01,
            "prism_element_count": 60,
            "core_element_count": 10,
            "core_tetra_count": 10,
        }
        quality = {
            **quality_unsigned,
            "quality_sha256": _canonical(quality_unsigned),
        }
        counts = {
            "initial_bad_prism_count": 14,
            "negative_volume_prism_count": 8,
            "cone_node_count": 4,
            "source_prism_count": 1,
            "projected_3d_element_count": 70,
            "initial_below_threshold_prism_count": 8,
            "bad_source_triangle_count": 1,
            "component_count": 1,
            "candidate_root_count": 3,
            "quality_evaluation_count": 5,
            "changed_root_count": 0,
            "final_nonpositive_element_count": 0,
            "final_prism_below_threshold_count": 0,
            "final_core_tetra_below_gamma_count": 0,
            "final_nonfinite_count": 0,
        }
        replay_hash = _sha("quality-a")
        discovery = {
            "schema": DISCOVERY_SCHEMA,
            "status": "PASS",
            "profile_complete": True,
            "audit_only": True,
            "audit_variant": "owner_free_component_direction",
            "calibration_PASS_authorized": False,
            "production_mesh_eligible": False,
            "mesh_written": False,
            "runtime_mesh_tags_hardcoded": False,
            "runtime_tags_are_audit_only": True,
            "repair_application_performed": True,
            "repair_application_scope": (
                "IN_MEMORY_OWNER_FREE_DIRECTION_COMPLETE_AUDIT_ONLY"
            ),
            "in_memory_mesh_mutation_performed": True,
            "final_direction_application_performed": True,
            "incomplete_reason": None,
            "observed_counts": counts,
            "stable_observation": stable,
            "stable_observation_sha256": _canonical(stable),
            "direction_endpoint": endpoint,
            "search": {
                "schema": SEARCH_SCHEMA,
                "status": "PASS",
                "endpoint_sha256": endpoint["endpoint_sha256"],
                "sharp_owner_used": False,
                "absolute_coordinate_replay": True,
                "aba_replay_status": "PASS",
                "aba_replay": {
                    "state_a_first_sha256": replay_hash,
                    "state_b_sha256": _sha("quality-b"),
                    "state_a_second_sha256": replay_hash,
                    "state_a_reproducible": True,
                },
                "monotonicity_assumed": False,
                "component_count": 1,
                "candidate_root_count": 3,
                "candidate_direction_count": 12,
                "quality_evaluation_count": 5,
                "coordinate_descent_passes_completed": 2,
                "component_records": [
                    {
                        "component_sha256": components[0]["component_sha256"],
                        "candidate_root_count": 3,
                        "joint_candidate_count": 2,
                        "coordinate_candidate_count": 1,
                        "quality_evaluation_count": 1,
                        "selected_objective": [0, 0, 0, 0, -0.02, -0.01, 0],
                    }
                ],
                "changed_roots": [],
            },
            "final_quality": quality,
            "orientation": {
                "initial_quality": {"bad_element_count_union": 14},
                "reversed_prism_count": 8,
                "unordered_cell_node_sets_unchanged": True,
            },
            "one_layer_chebyshev": {
                "violating_root_count": 4,
                "minimum_required_margin": 0.001,
                "postrepair_quality": {"bad_element_count_union": 0},
            },
            "full_layer_detection": {
                "initial_quality": {"bad_element_count_union": 0},
                "minimum_scaled_jacobian_for_repair": 0.01,
                "bad_prism_count": 0,
                "below_threshold_prism_count": 8,
                "bad_source_triangle_count": 1,
            },
            "subdivision": {
                "surface_schedule_binding": {
                    "binding_sha256": _sha("binding")
                },
                "surface_schedule_binding_sha256": _sha("binding"),
                "schedule_endpoint_sha256": "a" * 64,
                "layer_count": 60,
                "root_count": 100,
                "source_prism_count": 1,
                "projected_3d_element_count": 70,
            },
        }
        return endpoint, discovery

    def test_discovery_validator_accepts_complete_hash_bound_pass(self) -> None:
        endpoint, discovery = self._discovery()
        self.assertEqual(
            discovery,
            validate_component_direction_discovery(
                discovery,
                expected_direction_endpoint=endpoint,
            ),
        )

    def _pattern_discovery(self):
        _old_endpoint, discovery = self._discovery()
        endpoint = make_owner_free_direction_pattern_endpoint(
            schedule_endpoint_sha256="a" * 64
        )
        discovery["direction_endpoint"] = endpoint
        quality_unsigned = dict(discovery["final_quality"])
        quality_unsigned.pop("quality_sha256")
        quality_unsigned.update(
            {
                "maximum_prism_scaled_jacobian_deficit": 0.0,
                "prism_scaled_jacobian_deficit_l1": 0.0,
                "prism_scaled_jacobian_deficit_l2": 0.0,
                "maximum_core_tetra_gamma_deficit": 0.0,
                "core_tetra_gamma_deficit_l2": 0.0,
            }
        )
        quality = {
            **quality_unsigned,
            "quality_sha256": _canonical(quality_unsigned),
        }
        discovery["final_quality"] = quality
        discovery["observed_counts"]["quality_evaluation_count"] = 10

        base_component = discovery["stable_observation"]["components"][0]
        interaction_unsigned = {
            "root_coordinate_sha256": base_component[
                "root_coordinate_sha256"
            ],
            "candidate_root_count": 3,
            "source_component_sha256": [base_component["component_sha256"]],
            "source_component_count": 1,
            "affected_prism_count": 60,
            "affected_core_element_count": 10,
            "affected_core_tetra_count": 10,
            "affected_element_lineage_sha256": _sha("affected-elements"),
            "connected_by_affected_element_cooccurrence": True,
            "sharp_owner_used": False,
            "split_by_wall_fingerprint": False,
        }
        interaction = {
            **interaction_unsigned,
            "interaction_component_sha256": _canonical(
                interaction_unsigned
            ),
        }
        topology_unsigned = {
            "schema": "cfdpipe.owner_free_interaction_topology.v1",
            "status": "PASS",
            "base_component_count": 1,
            "interaction_component_count": 1,
            "candidate_root_count": 3,
            "candidate_chain_node_count": 183,
            "affected_prism_count": 60,
            "affected_core_element_count": 10,
            "affected_core_tetra_count": 10,
            "cross_base_component_prism_count": 0,
            "cross_base_component_core_count": 0,
            "base_components_merged": False,
            "components": [interaction],
        }
        topology = {
            **topology_unsigned,
            "interaction_topology_sha256": _canonical(topology_unsigned),
        }
        coordinate_hash = _sha("coordinates")
        quality_hash = quality["quality_sha256"]
        selected_objective = [
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            -0.02,
            -0.01,
            0.0,
        ]
        component_record = {
            "interaction_component_sha256": interaction[
                "interaction_component_sha256"
            ],
            "candidate_root_count": 3,
            "joint_candidate_count": 2,
            "coordinate_candidate_count": 1,
            "quality_evaluation_count": 2,
            "selected_objective": selected_objective,
            "search_exit_quality_sha256": quality_hash,
            "search_exit_coordinate_sha256": coordinate_hash,
        }
        recheck_record = {
            "interaction_component_sha256": interaction[
                "interaction_component_sha256"
            ],
            "selected_state_coordinate_sha256": coordinate_hash,
            "affected_prism_count": 60,
            "affected_core_element_count": 10,
            "affected_core_tetra_count": 10,
            "quality": quality,
            "final_state_objective": selected_objective,
            "quality_evaluation_index": 8,
        }
        recheck_unsigned = {
            "schema": "cfdpipe.owner_free_final_component_recheck.v1",
            "status": "PASS",
            "component_count": 1,
            "quality_evaluation_count": 1,
            "all_records_final_state": True,
            "component_records": [recheck_record],
        }
        recheck = {
            **recheck_unsigned,
            "final_component_recheck_sha256": _canonical(recheck_unsigned),
        }
        inventory_unsigned = {
            "schema": "cfdpipe.owner_free_low_quality_prism_inventory.v1",
            "status": "PASS",
            "minimum_scaled_jacobian_for_pass": 0.01,
            "final_low_quality_prism_count": 0,
            "lineage_quality_query_count": 1,
            "records": [],
        }
        inventory = {
            **inventory_unsigned,
            "final_low_quality_prism_inventory_sha256": _canonical(
                inventory_unsigned
            ),
        }
        replay_hash = _sha("quality-a")
        discovery["search"] = {
            "schema": SEARCH_SCHEMA,
            "status": "PASS",
            "endpoint_sha256": endpoint["endpoint_sha256"],
            "sharp_owner_used": False,
            "absolute_coordinate_replay": True,
            "aba_replay_status": "PASS",
            "aba_replay": {
                "state_a_first_sha256": replay_hash,
                "state_b_sha256": quality_hash,
                "state_a_second_sha256": replay_hash,
                "state_a_reproducible": True,
            },
            "monotonicity_assumed": False,
            "component_count": 1,
            "candidate_root_count": 3,
            "candidate_direction_count": 12,
            "quality_evaluation_count": 10,
            "coordinate_descent_passes_completed": 2,
            "component_records": [component_record],
            "changed_roots": [],
            "tangent_pattern_refinement": {
                "performed": True,
                "steps_radians": endpoint["tangent_pattern_steps_radians"],
                "coordinate_passes_per_step": endpoint[
                    "tangent_coordinate_passes_per_step"
                ],
                "shared_triangle_pair_moves": True,
                "quality_evaluation_count": 0,
                "accepted_move_count": 0,
                "refined_component_count": 0,
                "rejected_margin_candidate_count": 0,
                "minimum_required_cone_margin": 1.0e-5,
                "minimum_required_changed_direction_margin": 1.0e-4,
                "entry_minimum_cone_margin": 2.0e-5,
                "final_minimum_cone_margin": 2.0e-5,
                "final_minimum_changed_direction_margin": None,
            },
            "abab_replay_status": "PASS",
            "coordinate_replay": {
                "encoding": "python-float-hex/root-sha/layer-v1",
                "candidate_root_count": 3,
                "candidate_chain_node_count": 183,
                "state_a_first_coordinate_sha256": coordinate_hash,
                "state_b_first_coordinate_sha256": coordinate_hash,
                "state_a_second_coordinate_sha256": coordinate_hash,
                "state_b_restored_coordinate_sha256": coordinate_hash,
                "state_a_first_quality_sha256": replay_hash,
                "state_b_first_quality_sha256": quality_hash,
                "state_a_second_quality_sha256": replay_hash,
                "state_b_restored_quality_sha256": quality_hash,
                "state_a_reproducible": True,
                "state_b_reproducible": True,
                "a_b_distinct": False,
            },
            "interaction_topology": topology,
            "final_component_recheck": recheck,
            "final_low_quality_prisms": inventory,
        }
        return endpoint, discovery

    def _fine_pattern_discovery(self):
        _pattern_endpoint, discovery = self._pattern_discovery()
        endpoint = make_owner_free_direction_frontier_fine_refinement_pattern_endpoint(
            schedule_endpoint_sha256="a" * 64
        )
        discovery["direction_endpoint"] = endpoint
        search = discovery["search"]
        search["endpoint_sha256"] = endpoint["endpoint_sha256"]
        search["quality_evaluation_count"] = 9
        discovery["observed_counts"]["quality_evaluation_count"] = 9
        refinement = search["tangent_pattern_refinement"]
        refinement.update(
            {
                "performed": False,
                "steps_radians": endpoint["tangent_pattern_steps_radians"],
                "quality_evaluation_count": 0,
                "accepted_move_count": 0,
                "refined_component_count": 0,
            }
        )

        record = search["component_records"][0]
        interaction_id = record["interaction_component_sha256"]
        source_triangles = discovery["stable_observation"]["components"][0][
            "source_triangle_sha256"
        ]
        static_quality = copy.deepcopy(discovery["final_quality"])
        static_objective = list(record["selected_objective"])
        static_coordinate_sha256 = record["search_exit_coordinate_sha256"]
        static_objective_sha256 = _canonical(
            {
                "quality_objective_fields": endpoint["quality_objective"],
                "objective": static_objective,
            }
        )
        record.update(
            {
                "source_triangle_count": len(source_triangles),
                "source_triangle_sha256": source_triangles,
                "static_exit_quality": static_quality,
                "static_exit_quality_sha256": static_quality["quality_sha256"],
                "static_exit_objective": static_objective,
                "static_exit_objective_sha256": static_objective_sha256,
                "static_exit_coordinate_sha256": static_coordinate_sha256,
                "static_exit_status": "PASS",
                "static_exit_quality_evaluation_index": 2,
            }
        )
        static_evidence = {
            "interaction_component_sha256": interaction_id,
            "static_exit_status": "PASS",
            "static_exit_quality_sha256": static_quality["quality_sha256"],
            "static_exit_objective_sha256": static_objective_sha256,
            "static_exit_coordinate_sha256": static_coordinate_sha256,
            "static_exit_quality_evaluation_index": 2,
        }
        search["fine_refinement_selection"] = {
            "schema": (
                "cfdpipe.coarse_schedule_frontier_fine_refinement_selection.v1"
            ),
            "status": "PASS",
            "gate": {
                "static_all_components_complete_before_fine": True,
                "static_search_started_from_fresh_a": True,
                "static_component_count": 1,
                "static_failing_component_count": 0,
                "maximum_refined_component_count": 1,
                "required_refined_component_root_count": 3,
                "required_refined_component_source_triangle_count": 1,
                "fine_refinement_performed": False,
            },
            "order": {
                "static_component_sha256_order": [interaction_id],
                "fine_component_sha256_order": [],
                "last_static_quality_evaluation_index": 2,
                "first_fine_quality_evaluation_index": None,
                "all_static_exits_before_first_fine_evaluation": True,
                "tail_steps_radians": endpoint[
                    "tangent_pattern_steps_radians"
                ][5:],
                "tail_start_coordinate_sha256": None,
                "tail_start_direction_state_sha256": None,
                "tail_start_direction_field_sha256": None,
                "tail_start_quality_sha256": None,
                "tail_started_from_prefix_checkpoint": False,
                "first_tail_quality_evaluation_index": None,
            },
            "evaluation_evidence": {
                "endpoint_maximum_quality_evaluations": 4096,
                "global_conservative_quality_evaluation_cap": 2187,
                "static_quality_evaluation_count": 2,
                "fine_quality_evaluation_count": 0,
                "total_quality_evaluation_count": 9,
            },
            "selected_component": None,
            "prefix_checkpoint": None,
            "component_static_evidence": [static_evidence],
        }
        return endpoint, discovery

    def _triple_zero_failure_discovery(self):
        _fine_endpoint, discovery = self._fine_pattern_discovery()
        endpoint = (
            make_owner_free_direction_frontier_triple_refinement_pattern_endpoint(
                schedule_endpoint_sha256="a" * 64
            )
        )
        discovery["direction_endpoint"] = endpoint
        search = discovery["search"]
        search["endpoint_sha256"] = endpoint["endpoint_sha256"]
        fine = search["fine_refinement_selection"]
        fine["evaluation_evidence"]["total_quality_evaluation_count"] = 2
        schedule = {
            "steps_radians": endpoint["tangent_pattern_steps_radians"],
            "coordinate_passes_per_step": endpoint[
                "triple_coordinate_passes_per_step"
            ],
            "tangent_candidates_per_root_per_step": endpoint[
                "tangent_candidates_per_root_per_step"
            ],
            "atomic_cartesian_candidates_per_step_pass": endpoint[
                "atomic_triple_cartesian_candidates_per_step_pass"
            ],
        }
        evaluations = {
            "endpoint_maximum_quality_evaluations": 4096,
            "global_conservative_quality_evaluation_cap": 3723,
            "static_quality_evaluation_count": 2,
            "fine_quality_evaluation_count": 0,
            "through_fine_handoff_quality_evaluation_count": 2,
            "triple_quality_evaluation_count": 0,
            "triple_quality_evaluation_start_index_exclusive": 2,
            "first_triple_quality_evaluation_index": None,
            "last_triple_quality_evaluation_index": None,
            "triple_quality_evaluation_end_index_inclusive": 2,
            "final_replay_and_verification_quality_evaluation_count": 7,
            "total_quality_evaluation_count": 9,
        }
        triple_records: list[dict] = []
        search["triple_pattern_refinement"] = {
            "performed": False,
            **schedule,
            "quality_evaluation_count": 0,
            "accepted_move_count": 0,
            "rejected_margin_candidate_count": 0,
            "refined_component_count": 0,
            "step_pass_records": triple_records,
        }
        search["triple_refinement_selection"] = {
            "schema": (
                "cfdpipe.coarse_schedule_frontier_"
                "triple_refinement_selection.v1"
            ),
            "status": "PASS",
            "gate": {
                "static_all_components_complete_before_fine": True,
                "single_and_pair_phases_complete_before_triple": True,
                "static_failing_component_count": 0,
                "fine_refinement_performed": False,
                "triple_refinement_performed": False,
                "required_triple_component_root_count": 3,
                "required_triple_component_source_triangle_count": 1,
                "selected_component_shape_valid": True,
            },
            "handoff": None,
            "schedule": schedule,
            "step_pass_records": triple_records,
            "evaluation_evidence": evaluations,
            "selected_component": None,
            "final_state": None,
        }
        return endpoint, discovery

    def _triple_performed_discovery(self):
        endpoint, discovery = self._triple_zero_failure_discovery()
        search = discovery["search"]
        record = search["component_records"][0]
        interaction_id = record["interaction_component_sha256"]
        roots = search["interaction_topology"]["components"][0][
            "root_coordinate_sha256"
        ]

        static_unsigned = dict(discovery["final_quality"])
        static_unsigned.pop("quality_sha256")
        static_unsigned.update(
            {
                "status": "FAIL",
                "prism_below_threshold_element_count": 1,
                "minimum_prism_scaled_jacobian": 0.005,
                "maximum_prism_scaled_jacobian_deficit": 0.005,
                "prism_scaled_jacobian_deficit_l1": 0.005,
                "prism_scaled_jacobian_deficit_l2": 0.005,
            }
        )
        static_quality = {
            **static_unsigned,
            "quality_sha256": _canonical(static_unsigned),
        }
        static_objective = [
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.005,
            0.005,
            0.005,
            -0.005,
            -0.01,
            0.0,
        ]
        static_objective_sha256 = _canonical(
            {
                "quality_objective_fields": endpoint["quality_objective"],
                "objective": static_objective,
            }
        )
        record.update(
            {
                "static_exit_quality": static_quality,
                "static_exit_quality_sha256": static_quality["quality_sha256"],
                "static_exit_objective": static_objective,
                "static_exit_objective_sha256": static_objective_sha256,
                "static_exit_status": "FAIL",
                "quality_evaluation_count": 28,
            }
        )
        final_quality = discovery["final_quality"]
        final_objective = list(record["selected_objective"])
        final_objective_sha256 = _canonical(
            {
                "quality_objective_fields": endpoint["quality_objective"],
                "objective": final_objective,
            }
        )
        selected_directions = [
            {
                "root_coordinate_sha256": root,
                "direction_float_hex": [
                    float(value).hex() for value in direction
                ],
            }
            for root, direction in zip(
                roots,
                ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]),
            )
        ]
        direction_state_sha256 = _canonical(
            {
                "encoding": "python-float-hex/root-sha/direction-v1",
                "records": selected_directions,
            }
        )
        direction_field_sha256 = _canonical({"records": selected_directions})
        fine_coordinate_sha256 = _sha("triple-fine-exit-coordinate")
        checkpoint = {
            "selected_state_coordinate_sha256": fine_coordinate_sha256,
            "selected_state_quality": final_quality,
            "selected_state_quality_sha256": final_quality["quality_sha256"],
            "selected_state_objective": final_objective,
            "selected_state_objective_sha256": final_objective_sha256,
            "selected_direction_state_sha256": direction_state_sha256,
            "selected_direction_field_sha256": direction_field_sha256,
            "selected_directions": selected_directions,
            "fine_exit_quality_evaluation_index": 4,
        }
        record["fine_exit_checkpoint"] = checkpoint
        record["search_exit_coordinate_sha256"] = fine_coordinate_sha256
        record["search_exit_quality_sha256"] = final_quality["quality_sha256"]

        fine = search["fine_refinement_selection"]
        fine["gate"].update(
            {
                "static_failing_component_count": 1,
                "fine_refinement_performed": True,
            }
        )
        fine["component_static_evidence"][0].update(
            {
                "static_exit_status": "FAIL",
                "static_exit_quality_sha256": static_quality["quality_sha256"],
                "static_exit_objective_sha256": static_objective_sha256,
            }
        )
        selected_component = {
            "interaction_component_sha256": interaction_id,
            "candidate_root_count": 3,
            "root_coordinate_sha256": roots,
            "source_triangle_count": 1,
            "source_triangle_sha256": record["source_triangle_sha256"],
            "unique_source_triangle_edge_count": 3,
        }
        fine["selected_component"] = selected_component
        prefix_coordinate = _sha("fine-prefix-coordinate")
        prefix_direction_state = _sha("fine-prefix-direction-state")
        prefix_direction_field = _sha("fine-prefix-direction-field")
        fine["prefix_checkpoint"] = {
            "selected_component_sha256": interaction_id,
            "prefix_steps_radians": endpoint["tangent_pattern_steps_radians"][:5],
            "selected_state_coordinate_sha256": prefix_coordinate,
            "selected_state_quality": final_quality,
            "selected_state_quality_sha256": final_quality["quality_sha256"],
            "selected_state_objective": final_objective,
            "selected_state_objective_sha256": final_objective_sha256,
            "selected_direction_state_sha256": prefix_direction_state,
            "selected_direction_field_sha256": prefix_direction_field,
            "quality_evaluation_index": 3,
        }
        fine["order"].update(
            {
                "fine_component_sha256_order": [interaction_id],
                "first_fine_quality_evaluation_index": 3,
                "tail_start_coordinate_sha256": prefix_coordinate,
                "tail_start_direction_state_sha256": prefix_direction_state,
                "tail_start_direction_field_sha256": prefix_direction_field,
                "tail_start_quality_sha256": final_quality["quality_sha256"],
                "tail_started_from_prefix_checkpoint": True,
                "first_tail_quality_evaluation_index": 4,
            }
        )
        fine["evaluation_evidence"].update(
            {
                "fine_quality_evaluation_count": 2,
                "total_quality_evaluation_count": 4,
            }
        )
        search["tangent_pattern_refinement"].update(
            {
                "performed": True,
                "quality_evaluation_count": 2,
                "accepted_move_count": 1,
                "refined_component_count": 1,
                "final_minimum_changed_direction_margin": 0.001,
            }
        )

        hash_suffix_values = {
            "coordinate_sha256": fine_coordinate_sha256,
            "quality_sha256": final_quality["quality_sha256"],
            "objective_sha256": final_objective_sha256,
            "direction_state_sha256": direction_state_sha256,
            "direction_field_sha256": direction_field_sha256,
        }
        step_records = []
        running_index = 4
        for step_index, step in enumerate(
            endpoint["tangent_pattern_steps_radians"], start=1
        ):
            for pass_index in range(1, 3):
                step_records.append(
                    {
                        "step_index_1_based": step_index,
                        "pass_index_1_based": pass_index,
                        "step_radians": step,
                        "step_radians_float_hex": float(step).hex(),
                        "root_candidate_counts": [
                            {
                                "root_coordinate_sha256": root,
                                "candidate_count": 1,
                            }
                            for root in roots
                        ],
                        "candidate_upper_bound": 64,
                        "atomic_candidate_count": 1,
                        "rejected_margin_candidate_count": 0,
                        "quality_evaluation_start_index_exclusive": running_index,
                        "first_quality_evaluation_index": running_index + 1,
                        "last_quality_evaluation_index": running_index + 1,
                        "quality_evaluation_end_index_inclusive": running_index + 1,
                        "quality_evaluation_count": 1,
                        "accepted_move": False,
                        **{
                            f"entry_{key}": value
                            for key, value in hash_suffix_values.items()
                        },
                        **{
                            f"exit_{key}": value
                            for key, value in hash_suffix_values.items()
                        },
                    }
                )
                running_index += 1

        handoff = {
            "selected_component_sha256": interaction_id,
            "fine_exit_quality": final_quality,
            "fine_exit_objective": final_objective,
            "fine_exit_quality_evaluation_index": 4,
            "triple_quality_evaluation_start_index_exclusive": 4,
            "without_reset": True,
        }
        for suffix, value in hash_suffix_values.items():
            handoff[f"fine_exit_{suffix}"] = value
            handoff[f"triple_entry_{suffix}"] = value

        triple = search["triple_refinement_selection"]
        triple["gate"].update(
            {
                "static_failing_component_count": 1,
                "fine_refinement_performed": True,
                "triple_refinement_performed": True,
            }
        )
        triple["selected_component"] = selected_component
        triple["handoff"] = handoff
        triple["step_pass_records"] = step_records
        triple["evaluation_evidence"].update(
            {
                "fine_quality_evaluation_count": 2,
                "through_fine_handoff_quality_evaluation_count": 4,
                "triple_quality_evaluation_count": 24,
                "triple_quality_evaluation_start_index_exclusive": 4,
                "first_triple_quality_evaluation_index": 5,
                "last_triple_quality_evaluation_index": 28,
                "triple_quality_evaluation_end_index_inclusive": 28,
                "final_replay_and_verification_quality_evaluation_count": 7,
                "total_quality_evaluation_count": 35,
            }
        )
        triple["final_state"] = {
            "selected_component_sha256": interaction_id,
            "selected_state_coordinate_sha256": fine_coordinate_sha256,
            "selected_state_quality": final_quality,
            "selected_state_quality_sha256": final_quality["quality_sha256"],
            "selected_state_objective": final_objective,
            "selected_state_objective_sha256": final_objective_sha256,
            "selected_direction_state_sha256": direction_state_sha256,
            "selected_direction_field_sha256": direction_field_sha256,
            "triple_quality_evaluation_end_index_inclusive": 28,
        }
        search["triple_pattern_refinement"].update(
            {
                "performed": True,
                "quality_evaluation_count": 24,
                "refined_component_count": 1,
                "step_pass_records": step_records,
            }
        )
        search["quality_evaluation_count"] = 35
        discovery["observed_counts"].update(
            {"quality_evaluation_count": 35, "changed_root_count": 3}
        )
        search["changed_roots"] = [
            {
                "root_coordinate_sha256": root,
                "original_direction": [0.0, 0.0, -1.0],
                "selected_direction": [float(value) for value in direction],
                "candidate_count": 1,
                "selected_source": "tangent_pattern",
                "selected_interior_weight": 0.5,
            }
            for root, direction in zip(
                roots,
                ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]),
            )
        ]
        replay = search["coordinate_replay"]
        replay.update(
            {
                "state_b_first_coordinate_sha256": fine_coordinate_sha256,
                "state_b_restored_coordinate_sha256": fine_coordinate_sha256,
                "a_b_distinct": True,
            }
        )
        recheck = search["final_component_recheck"]
        recheck["component_records"][0][
            "selected_state_coordinate_sha256"
        ] = fine_coordinate_sha256
        recheck_unsigned = dict(recheck)
        recheck_unsigned.pop("final_component_recheck_sha256")
        recheck["final_component_recheck_sha256"] = _canonical(recheck_unsigned)
        return endpoint, discovery

    def test_pattern_discovery_requires_interaction_and_abab_evidence(self) -> None:
        endpoint, discovery = self._pattern_discovery()
        self.assertEqual(
            discovery,
            validate_component_direction_discovery(
                discovery,
                expected_direction_endpoint=endpoint,
            ),
        )
        for mutation in (
            lambda value: value["search"]["coordinate_replay"].update(
                {"state_b_reproducible": False}
            ),
            lambda value: value["search"]["interaction_topology"].update(
                {"affected_prism_count": 59}
            ),
            lambda value: value["search"][
                "tangent_pattern_refinement"
            ].update({"final_minimum_cone_margin": 1.0e-6}),
            lambda value: value["search"][
                "final_component_recheck"
            ].update({"all_records_final_state": False}),
        ):
            with self.subTest(mutation=mutation):
                tampered = copy.deepcopy(discovery)
                mutation(tampered)
                with self.assertRaises(CoarseComponentDirectionError):
                    validate_component_direction_discovery(
                        tampered,
                        expected_direction_endpoint=endpoint,
                    )

    def test_frontier_pattern_variant_requires_pattern_evidence(self) -> None:
        _pattern_endpoint, discovery = self._pattern_discovery()
        endpoint = make_owner_free_direction_frontier_pattern_endpoint(
            schedule_endpoint_sha256="a" * 64
        )
        discovery["direction_endpoint"] = endpoint
        discovery["search"]["endpoint_sha256"] = endpoint["endpoint_sha256"]
        discovery["search"]["tangent_pattern_refinement"][
            "steps_radians"
        ] = endpoint["tangent_pattern_steps_radians"]
        self.assertEqual(
            discovery,
            validate_component_direction_discovery(
                discovery,
                expected_direction_endpoint=endpoint,
            ),
        )

        missing_pattern_proof = copy.deepcopy(discovery)
        missing_pattern_proof["search"].pop("coordinate_replay")
        with self.assertRaises(CoarseComponentDirectionError):
            validate_component_direction_discovery(
                missing_pattern_proof,
                expected_direction_endpoint=endpoint,
            )

    def test_fine_pattern_variant_requires_static_selection_evidence(self) -> None:
        endpoint, discovery = self._fine_pattern_discovery()
        self.assertEqual(
            discovery,
            validate_component_direction_discovery(
                discovery,
                expected_direction_endpoint=endpoint,
            ),
        )

        mutations = (
            lambda value: value["search"].pop("fine_refinement_selection"),
            lambda value: value["search"]["component_records"][0].update(
                {"static_exit_quality_sha256": "f" * 64}
            ),
            lambda value: value["search"]["component_records"][0].update(
                {"source_triangle_sha256": [_sha("other-triangle")]}
            ),
            lambda value: value["search"]["fine_refinement_selection"][
                "evaluation_evidence"
            ].update({"fine_quality_evaluation_count": 1}),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                tampered = copy.deepcopy(discovery)
                mutation(tampered)
                with self.assertRaises(CoarseComponentDirectionError):
                    validate_component_direction_discovery(
                        tampered,
                        expected_direction_endpoint=endpoint,
                    )

        _pattern_endpoint, stale = self._pattern_discovery()
        stale["direction_endpoint"] = endpoint
        stale["search"]["endpoint_sha256"] = endpoint["endpoint_sha256"]
        stale["search"]["tangent_pattern_refinement"][
            "steps_radians"
        ] = endpoint["tangent_pattern_steps_radians"]
        with self.assertRaises(CoarseComponentDirectionError):
            validate_component_direction_discovery(
                stale,
                expected_direction_endpoint=endpoint,
            )

        frontier_endpoint, frontier = self._pattern_discovery()
        frontier["search"]["fine_refinement_selection"] = {}
        with self.assertRaises(CoarseComponentDirectionError):
            validate_component_direction_discovery(
                frontier,
                expected_direction_endpoint=frontier_endpoint,
            )

    def test_triple_zero_failure_branch_forbids_all_refinement_evidence(self) -> None:
        endpoint, discovery = self._triple_zero_failure_discovery()
        self.assertEqual(
            discovery,
            validate_component_direction_discovery(
                discovery,
                expected_direction_endpoint=endpoint,
            ),
        )
        mutations = (
            lambda value: value["search"]["triple_pattern_refinement"].update(
                {"performed": True}
            ),
            lambda value: value["search"]["triple_refinement_selection"].update(
                {"handoff": {}}
            ),
            lambda value: value["search"]["component_records"][0].update(
                {"fine_exit_checkpoint": {}}
            ),
            lambda value: value["search"]["triple_refinement_selection"][
                "evaluation_evidence"
            ].update({"triple_quality_evaluation_count": 1}),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                tampered = copy.deepcopy(discovery)
                mutation(tampered)
                with self.assertRaises(CoarseComponentDirectionError):
                    validate_component_direction_discovery(
                        tampered,
                        expected_direction_endpoint=endpoint,
                    )

    def test_triple_refinement_binds_handoff_records_budget_and_final_state(self) -> None:
        endpoint, discovery = self._triple_performed_discovery()
        self.assertEqual(
            discovery,
            validate_component_direction_discovery(
                discovery,
                expected_direction_endpoint=endpoint,
            ),
        )
        mutations = (
            lambda value: value["search"]["component_records"][0][
                "fine_exit_checkpoint"
            ].update({"selected_state_quality_sha256": "f" * 64}),
            lambda value: value["search"]["component_records"][0][
                "fine_exit_checkpoint"
            ]["selected_directions"][0].update(
                {"direction_float_hex": ["0x0.0p+0"] * 3}
            ),
            lambda value: value["search"]["triple_refinement_selection"][
                "handoff"
            ].update({"triple_entry_quality_sha256": "e" * 64}),
            lambda value: value["search"]["triple_refinement_selection"][
                "step_pass_records"
            ][0].update({"step_index_1_based": 2}),
            lambda value: value["search"]["triple_refinement_selection"][
                "step_pass_records"
            ][0]["root_candidate_counts"][0].update({"candidate_count": 5}),
            lambda value: value["search"]["triple_refinement_selection"][
                "step_pass_records"
            ][0].update({"candidate_upper_bound": 65}),
            lambda value: value["search"]["triple_refinement_selection"][
                "step_pass_records"
            ].pop(),
            lambda value: value["search"]["triple_refinement_selection"][
                "step_pass_records"
            ][1].update({"quality_evaluation_start_index_exclusive": 4}),
            lambda value: value["search"]["triple_refinement_selection"][
                "step_pass_records"
            ][0].update({"exit_coordinate_sha256": _sha("changed-exit")}),
            lambda value: value["search"]["triple_refinement_selection"][
                "evaluation_evidence"
            ].update(
                {
                    "total_quality_evaluation_count": 3724,
                    "final_replay_and_verification_quality_evaluation_count": 3696,
                }
            ),
            lambda value: value["search"]["triple_refinement_selection"][
                "final_state"
            ].update({"selected_state_quality_sha256": "d" * 64}),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                tampered = copy.deepcopy(discovery)
                mutation(tampered)
                with self.assertRaises(CoarseComponentDirectionError):
                    validate_component_direction_discovery(
                        tampered,
                        expected_direction_endpoint=endpoint,
                    )

    def test_discovery_validator_rejects_untrusted_or_incomplete_proof(self) -> None:
        endpoint, discovery = self._discovery()
        tampered_endpoint = make_owner_free_direction_endpoint(
            schedule_endpoint_sha256="b" * 64
        )
        with self.assertRaises(CoarseComponentDirectionError):
            validate_component_direction_discovery(
                discovery,
                expected_direction_endpoint=tampered_endpoint,
            )
        for mutation in (
            lambda value: value.update({"orientation": None}),
            lambda value: value["search"].update({"sharp_owner_used": True}),
            lambda value: value["observed_counts"].update(
                {"component_count": 17}
            ),
            lambda value: value["final_quality"].update(
                {"minimum_prism_scaled_jacobian": 0.0}
            ),
        ):
            with self.subTest(mutation=mutation):
                tampered = copy.deepcopy(discovery)
                mutation(tampered)
                with self.assertRaises(CoarseComponentDirectionError):
                    validate_component_direction_discovery(
                        tampered,
                        expected_direction_endpoint=endpoint,
                    )


if __name__ == "__main__":
    unittest.main()
