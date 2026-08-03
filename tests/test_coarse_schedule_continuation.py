from __future__ import annotations

import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from cfdpipe.coarse_schedule_continuation import (
    DISCOVERY_SCHEMA,
    ENDPOINT_SCHEMA,
    REQUEST,
    CoarseScheduleContinuationError,
    make_frontier_direction_endpoint,
    make_schedule_frontier_direction_endpoint,
    validate_frontier_direction_endpoint,
    validate_schedule_frontier_direction_continuation,
    validate_schedule_frontier_direction_endpoint,
)
from cfdpipe.coarse_direction_feasibility import (
    COLLAR_REFINEMENT_ENDPOINT_MODE,
    COLLAR_REFINEMENT_ENDPOINT_SCHEMA,
    FINE_REFINEMENT_PATTERN_ENDPOINT_MODE,
    FINE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
    TRIPLE_REFINEMENT_PATTERN_ENDPOINT_MODE,
    TRIPLE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
    make_owner_free_direction_frontier_collar_refinement_endpoint,
    make_owner_free_direction_frontier_fine_refinement_pattern_endpoint,
    make_owner_free_direction_frontier_pattern_endpoint,
    make_owner_free_direction_frontier_triple_refinement_pattern_endpoint,
    make_owner_free_direction_pattern_endpoint,
)


def _canonical_sha256(value):
    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


class FrontierDirectionEndpointTests(unittest.TestCase):
    def setUp(self):
        self.observation = {
            "low_quality_prism_count": 37,
            "unique_source_triangle_count": 5,
            "wall_count": 2,
            "core_tetra_below_gamma_count": 0,
            "nonpositive_element_count": 0,
        }
        self.arguments = {
            "homotopy_manifest_sha256": "1" * 64,
            "homotopy_discovery_sha256": "2" * 64,
            "homotopy_endpoint_sha256": "3" * 64,
            "direction_replay_approval_sha256": "4" * 64,
            "local_schedule_binding_sha256": "5" * 64,
            "previous_quality_sha256": "6" * 64,
            "target_quality_sha256": "7" * 64,
            "target_low_quality_aggregate_sha256": "8" * 64,
            "first_layer_height_m": 2.7645771273320875e-7,
            "layer_count": 60,
            "target_observation": self.observation,
        }

    def test_constants_are_frozen(self):
        self.assertEqual(
            REQUEST, "physical-schedule-first-frontier-direction-audit"
        )
        self.assertEqual(
            ENDPOINT_SCHEMA,
            "cfdpipe.coarse_schedule_frontier_direction_endpoint.v5",
        )
        self.assertEqual(
            DISCOVERY_SCHEMA,
            "cfdpipe.coarse_schedule_frontier_direction_continuation.v5",
        )

    def test_factory_freezes_frontier_thresholds_caps_and_permissions(self):
        endpoint = make_frontier_direction_endpoint(**self.arguments)
        self.assertEqual(endpoint["schema"], ENDPOINT_SCHEMA)
        self.assertEqual(endpoint["request"], REQUEST)
        self.assertEqual(endpoint["previous_fraction_float_hex"], (0.0).hex())
        self.assertEqual(endpoint["target_fraction_float_hex"], (2.0**-12).hex())
        self.assertEqual(
            endpoint["quality_thresholds"],
            {
                "minimum_core_tetra_gamma_for_pass": 0.001,
                "minimum_prism_scaled_jacobian_for_pass": 0.01,
            },
        )
        self.assertEqual(
            endpoint["caps"],
            {
                "maximum_candidate_root_count": 16,
                "maximum_component_count": 3,
                "maximum_quality_evaluations": 4096,
                "maximum_pair_candidates_per_edge_step": 16,
            },
        )
        self.assertEqual(
            endpoint["observed_real_scope"],
            {
                "component_count": 3,
                "candidate_root_count": 12,
                "source_triangle_count": 5,
            },
        )
        self.assertEqual(
            endpoint["direction_search_contract"]["endpoint_schema"],
            TRIPLE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
        )
        self.assertEqual(
            endpoint["direction_search_contract"]["endpoint_mode"],
            TRIPLE_REFINEMENT_PATTERN_ENDPOINT_MODE,
        )
        child = (
            make_owner_free_direction_frontier_triple_refinement_pattern_endpoint(
                schedule_endpoint_sha256=endpoint["endpoint_sha256"]
            )
        )
        collar_child = (
            make_owner_free_direction_frontier_collar_refinement_endpoint(
                schedule_endpoint_sha256=endpoint["endpoint_sha256"]
            )
        )
        self.assertEqual(
            endpoint["collar_refinement_contract"]["schema"],
            COLLAR_REFINEMENT_ENDPOINT_SCHEMA,
        )
        self.assertEqual(
            endpoint["collar_refinement_contract"]["mode"],
            COLLAR_REFINEMENT_ENDPOINT_MODE,
        )
        self.assertEqual(collar_child["collar_ring_widths"], [1, 2, 4, 8, 16, 32, 64])
        self.assertEqual(collar_child["maximum_collar_candidates"], 7)
        self.assertEqual(
            collar_child["source_triple_refinement_pattern_endpoint_sha256"],
            child["endpoint_sha256"],
        )
        fine_child = (
            make_owner_free_direction_frontier_fine_refinement_pattern_endpoint(
                schedule_endpoint_sha256=endpoint["endpoint_sha256"]
            )
        )
        self.assertEqual(
            child["source_fine_refinement_pattern_endpoint_schema"],
            FINE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
        )
        self.assertEqual(
            child["source_fine_refinement_pattern_endpoint_sha256"],
            fine_child["endpoint_sha256"],
        )
        self.assertEqual(
            endpoint["direction_search_contract"]["quality_objective"],
            [
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
        )
        self.assertEqual(
            endpoint["direction_search_contract"][
                "tangent_pattern_steps_radians"
            ],
            [2.0**-exponent for exponent in range(1, 13)],
        )
        self.assertEqual(
            {
                key: endpoint["direction_search_contract"][key]
                for key in (
                    "maximum_refined_component_count",
                    "refined_component_root_count",
                    "refined_component_source_triangle_count",
                    "static_all_components_complete_before_fine",
                    "single_and_pair_phases_complete_before_triple",
                    "triple_refinement_enabled",
                    "triple_refinement_root_count",
                    "tangent_candidates_per_root_per_step",
                    "atomic_triple_cartesian_candidates_per_step_pass",
                    "triple_coordinate_passes_per_step",
                )
            },
            {
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
            },
        )
        proof = endpoint["quality_evaluation_budget_proof"]
        self.assertEqual(
            proof["schema"],
            "cfdpipe.coarse_schedule_frontier_direction_evaluation_budget_proof.v4",
        )
        self.assertEqual(proof["status"], "PASS")
        self.assertIs(proof["within_cap"], True)
        self.assertEqual(
            proof["bound_formula"], "B=STATIC+FINE+TRIPLE+COLLAR+TAIL"
        )
        self.assertEqual(
            proof["actual_scope"][
                "static_search_quality_evaluation_upper_bound"
            ],
            738,
        )
        self.assertEqual(
            proof["actual_scope"][
                "fine_refinement_quality_evaluation_upper_bound"
            ],
            1440,
        )
        self.assertEqual(
            proof["actual_scope"][
                "triple_refinement_quality_evaluation_upper_bound"
            ],
            1536,
        )
        self.assertEqual(
            proof["actual_scope"][
                "collar_refinement_quality_evaluation_upper_bound"
            ],
            7,
        )
        self.assertEqual(
            proof["actual_scope"][
                "replay_and_recheck_quality_evaluation_upper_bound"
            ],
            9,
        )
        self.assertEqual(
            proof["actual_scope"]["quality_evaluation_upper_bound"], 3730
        )
        self.assertEqual(proof["headroom"], 366)
        self.assertEqual(proof["maximum_quality_evaluations"], 4096)
        self.assertEqual(
            proof["algorithm_knobs"],
            {
                "static_component_baseline_evaluation_count": 2,
                "component_joint_target_count": 3,
                "maximum_fixed_target_count_per_root": 6,
                "original_direction_interior_weights": [
                    0.015625,
                    0.0625,
                    0.25,
                    0.5,
                ],
                "coordinate_descent_passes": 2,
                "source_triangle_vertex_count": 3,
                "fine_tangent_pattern_steps_radians": [
                    2.0**-exponent for exponent in range(1, 13)
                ],
                "fine_tangent_coordinate_passes_per_step": 2,
                "fine_tangent_candidate_count_per_root_step": 4,
                "maximum_pair_candidates_per_edge_step": 16,
                "maximum_unique_edge_count_per_source_triangle": 3,
                "maximum_refined_component_count": 1,
                "refined_component_root_count": 3,
                "refined_component_source_triangle_count": 1,
                "triple_tangent_pattern_steps_radians": [
                    2.0**-exponent for exponent in range(1, 13)
                ],
                "triple_tangent_coordinate_passes_per_step": 2,
                "triple_tangent_candidate_count_per_root_step": 4,
                "triple_atomic_cartesian_candidates_per_step_pass": 64,
                "triple_refined_component_count": 1,
                "triple_refined_component_root_count": 3,
                "collar_ring_widths": [1, 2, 4, 8, 16, 32, 64],
                "collar_quality_evaluation_count_per_candidate": 1,
                "maximum_collar_candidates": 7,
                "fixed_replay_and_global_verification_reserve": 6,
                "final_component_recheck_count_per_component": 1,
            },
        )
        unsigned_proof = dict(proof)
        proof_sha256 = unsigned_proof.pop("proof_sha256")
        self.assertEqual(proof_sha256, _canonical_sha256(unsigned_proof))
        for field in (
            "calibration_PASS_authorized",
            "production_mesh_eligible",
            "mesh_write_authorized",
            "mesh_written",
            "su2_called",
            "paraview_called",
            "direction_search_performed",
        ):
            self.assertIs(endpoint[field], False)
        unsigned = dict(endpoint)
        configured_hash = unsigned.pop("endpoint_sha256")
        self.assertEqual(configured_hash, _canonical_sha256(unsigned))
        self.assertEqual(endpoint["target_observation"], self.observation)
        self.assertEqual(
            endpoint["mode"],
            "previous_pass_to_first_fail_fixed_schedule_direction_continuation_v5",
        )

    def test_real_source_triangle_scope_is_frozen_in_v4(self):
        changed = copy.deepcopy(self.arguments)
        changed["target_observation"] = {
            **self.observation,
            "low_quality_prism_count": 41,
            "unique_source_triangle_count": 6,
        }
        with self.assertRaises(CoarseScheduleContinuationError):
            make_frontier_direction_endpoint(**changed)

    def test_factory_rejects_bad_lineage_and_observation(self):
        for field in (
            "homotopy_manifest_sha256",
            "homotopy_discovery_sha256",
            "homotopy_endpoint_sha256",
            "direction_replay_approval_sha256",
            "local_schedule_binding_sha256",
            "previous_quality_sha256",
            "target_quality_sha256",
            "target_low_quality_aggregate_sha256",
        ):
            with self.subTest(field=field):
                arguments = copy.deepcopy(self.arguments)
                arguments[field] = "not-a-digest"
                with self.assertRaises(CoarseScheduleContinuationError):
                    make_frontier_direction_endpoint(**arguments)
        for observation in (
            {**self.observation, "extra": 1},
            {key: value for key, value in self.observation.items() if key != "wall_count"},
            {**self.observation, "low_quality_prism_count": True},
            {**self.observation, "low_quality_prism_count": 0},
            {**self.observation, "unique_source_triangle_count": 38},
            {**self.observation, "wall_count": 6},
            {**self.observation, "core_tetra_below_gamma_count": 1},
            {**self.observation, "nonpositive_element_count": 1},
        ):
            with self.subTest(observation=observation):
                arguments = copy.deepcopy(self.arguments)
                arguments["target_observation"] = observation
                with self.assertRaises(CoarseScheduleContinuationError):
                    make_frontier_direction_endpoint(**arguments)

    def test_validator_exactly_recreates_endpoint(self):
        endpoint = make_frontier_direction_endpoint(**self.arguments)
        validated = validate_frontier_direction_endpoint(
            endpoint, **self.arguments
        )
        self.assertEqual(validated, endpoint)
        self.assertIsNot(validated, endpoint)
        for field, value in (
            ("target_fraction_float_hex", (2.0**-11).hex()),
            ("mesh_write_authorized", True),
            ("endpoint_sha256", "9" * 64),
        ):
            with self.subTest(field=field):
                tampered = copy.deepcopy(endpoint)
                tampered[field] = value
                with self.assertRaises(CoarseScheduleContinuationError):
                    validate_frontier_direction_endpoint(
                        tampered, **self.arguments
                    )

    def test_rehashed_child_scope_or_budget_tamper_fails_closed(self):
        endpoint = make_frontier_direction_endpoint(**self.arguments)

        def swap_count_first_objective(item):
            objective = item["direction_search_contract"]["quality_objective"]
            objective[5], objective[6] = objective[6], objective[5]

        for mutate in (
            swap_count_first_objective,
            lambda item: item["observed_real_scope"].update(
                {"candidate_root_count": 11}
            ),
            lambda item: item["quality_evaluation_budget_proof"].update(
                {"headroom": 118}
            ),
            lambda item: item["quality_evaluation_budget_proof"][
                "algorithm_knobs"
            ].update({"coordinate_descent_passes": 3}),
            lambda item: item["direction_search_contract"].update(
                {"maximum_refined_component_count": 2}
            ),
            lambda item: item["direction_search_contract"][
                "tangent_pattern_steps_radians"
            ].pop(),
            lambda item: item["collar_refinement_contract"][
                "collar_ring_widths"
            ].pop(),
        ):
            with self.subTest(mutate=mutate):
                tampered = copy.deepcopy(endpoint)
                mutate(tampered)
                tampered_proof = tampered[
                    "quality_evaluation_budget_proof"
                ]
                tampered_proof["proof_sha256"] = _canonical_sha256(
                    {
                        key: value
                        for key, value in tampered_proof.items()
                        if key != "proof_sha256"
                    }
                )
                tampered["endpoint_sha256"] = _canonical_sha256(
                    {
                        key: value
                        for key, value in tampered.items()
                        if key != "endpoint_sha256"
                    }
                )
                with self.assertRaises(CoarseScheduleContinuationError):
                    validate_frontier_direction_endpoint(
                        tampered, **self.arguments
                    )

    def test_homotopy_compatibility_factory_derives_source_values(self):
        previous_quality = {
            "status": "PASS",
            "quality_sha256": "6" * 64,
            "minimum_prism_scaled_jacobian_for_pass": 0.01,
            "minimum_core_tetra_gamma_for_pass": 0.001,
            "prism_below_threshold_element_count": 0,
            "core_tetra_below_gamma_count": 0,
            "nonpositive_element_count": 0,
        }
        target_quality = {
            "status": "FAIL",
            "quality_sha256": "7" * 64,
            "minimum_prism_scaled_jacobian_for_pass": 0.01,
            "minimum_core_tetra_gamma_for_pass": 0.001,
            "prism_below_threshold_element_count": 37,
            "core_tetra_below_gamma_count": 0,
            "nonpositive_element_count": 0,
        }
        target_aggregate = {
            "aggregate_sha256": "8" * 64,
            "count": 37,
            "unique_source_triangle_count": 5,
            "wall_count": 2,
        }
        discovery = {
            "schema": (
                "cfdpipe.coarse_physical_schedule_fixed_direction_homotopy_discovery.v1"
            ),
            "status": "INCOMPLETE",
            "maximum_tested_pass_fraction_float_hex": (0.0).hex(),
            "first_tested_fail_fraction_float_hex": (2.0**-12).hex(),
            "source_bindings": {
                "homotopy_endpoint_sha256": "3" * 64,
                "direction_replay_approval_sha256": "4" * 64,
                "local_schedule_binding_sha256": "5" * 64,
            },
            "ascending_scan": [
                {
                    "fraction_float_hex": (0.0).hex(),
                    "quality": previous_quality,
                },
                {
                    "fraction_float_hex": (2.0**-12).hex(),
                    "quality": target_quality,
                    "low_quality_aggregate": target_aggregate,
                },
            ],
        }
        discovery["discovery_sha256"] = _canonical_sha256(discovery)
        arguments = {
            "homotopy_manifest_sha256": "1" * 64,
            "homotopy_discovery": discovery,
            "homotopy_endpoint_sha256": "3" * 64,
            "direction_replay_approval_sha256": "4" * 64,
            "local_schedule_binding_sha256": "5" * 64,
            "minimum_prism_scaled_jacobian_for_pass": 0.01,
            "minimum_core_tetra_gamma_for_pass": 0.001,
            "layer_count": 60,
            "first_layer_height_m": 2.7645771273320875e-7,
        }
        endpoint = make_schedule_frontier_direction_endpoint(**arguments)
        self.assertEqual(
            endpoint["source_bindings"]["homotopy_discovery_sha256"],
            discovery["discovery_sha256"],
        )
        self.assertEqual(endpoint["target_observation"], self.observation)
        self.assertEqual(
            validate_schedule_frontier_direction_endpoint(
                endpoint, **arguments
            ),
            endpoint,
        )
        tampered = copy.deepcopy(discovery)
        tampered["first_tested_fail_fraction_float_hex"] = (2.0**-11).hex()
        tampered["discovery_sha256"] = _canonical_sha256(
            {key: value for key, value in tampered.items() if key != "discovery_sha256"}
        )
        with self.assertRaises(CoarseScheduleContinuationError):
            make_schedule_frontier_direction_endpoint(
                **{**arguments, "homotopy_discovery": tampered}
            )


class FrontierDirectionDiscoveryCompatibilityTests(unittest.TestCase):
    @staticmethod
    def _quality(
        *,
        status,
        prism_bad,
        core_bad=0,
        nonpositive=0,
        prism_count=280320,
        core_count=13739,
        minimum_prism=None,
    ):
        if minimum_prism is None:
            minimum_prism = 0.011 if not prism_bad else 0.002
        value = {
            "status": status,
            "minimum_prism_scaled_jacobian_for_pass": 0.01,
            "minimum_core_tetra_gamma_for_pass": 0.001,
            "all_values_finite": True,
            "nonfinite_count": 0,
            "nonpositive_element_count": nonpositive,
            "prism_below_threshold_element_count": prism_bad,
            "core_tetra_below_gamma_count": core_bad,
            "minimum_prism_scaled_jacobian": minimum_prism,
            "minimum_core_tetra_gamma": 0.0011,
            "prism_element_count": prism_count,
            "core_element_count": core_count,
            "core_tetra_count": core_count,
            "maximum_prism_scaled_jacobian_deficit": (
                0.0 if not prism_bad else 0.01 - minimum_prism
            ),
            "prism_scaled_jacobian_deficit_l1": (
                0.0 if not prism_bad else 0.001 * prism_bad
            ),
            "prism_scaled_jacobian_deficit_l2": (
                0.0 if not prism_bad else 0.0001 * prism_bad
            ),
            "maximum_core_tetra_gamma_deficit": 0.0,
            "core_tetra_gamma_deficit_l2": 0.0,
        }
        value["quality_sha256"] = _canonical_sha256(value)
        return value

    @staticmethod
    def _objective(quality, *, direction_penalty=0.0):
        objective = [
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
            float(direction_penalty),
        ]
        objective_sha256 = _canonical_sha256(
            {
                "quality_objective_fields": [
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
                "objective": objective,
            }
        )
        return objective, objective_sha256

    def _evidence(self):
        previous_quality = self._quality(status="PASS", prism_bad=0)
        target_quality = self._quality(status="FAIL", prism_bad=37)
        aggregate = {
            "count": 37,
            "unique_source_triangle_count": 5,
            "wall_count": 2,
            "layer_count": 21,
            "by_wall_and_layer": [],
            "by_wall": [],
            "by_layer": [],
            "records_sha256": "9" * 64,
        }
        aggregate["aggregate_sha256"] = _canonical_sha256(aggregate)
        endpoint = make_frontier_direction_endpoint(
            homotopy_manifest_sha256="1" * 64,
            homotopy_discovery_sha256="2" * 64,
            homotopy_endpoint_sha256="3" * 64,
            direction_replay_approval_sha256="4" * 64,
            local_schedule_binding_sha256="5" * 64,
            previous_quality_sha256=previous_quality["quality_sha256"],
            target_quality_sha256=target_quality["quality_sha256"],
            target_low_quality_aggregate_sha256=aggregate["aggregate_sha256"],
            first_layer_height_m=2.7645771273320875e-7,
            layer_count=60,
            target_observation={
                "low_quality_prism_count": 37,
                "unique_source_triangle_count": 5,
                "wall_count": 2,
                "core_tetra_below_gamma_count": 0,
                "nonpositive_element_count": 0,
            },
        )
        roots = sorted(
            hashlib.sha256(f"root-{index}".encode()).hexdigest()
            for index in range(12)
        )
        triangles = sorted(
            hashlib.sha256(f"triangle-{index}".encode()).hexdigest()
            for index in range(5)
        )
        walls = sorted(
            hashlib.sha256(f"wall-{index}".encode()).hexdigest()
            for index in range(2)
        )
        component_records = []
        for component_roots, component_triangles, component_walls in (
            (roots[:5], triangles[:2], walls[:1]),
            (roots[5:9], triangles[2:4], walls[1:]),
            (roots[9:], triangles[4:], walls[1:]),
        ):
            record = {
                "source_triangle_sha256": component_triangles,
                "wall_surface_fingerprints": component_walls,
                "root_coordinate_sha256": component_roots,
                "triangle_count": len(component_triangles),
                "root_count": len(component_roots),
                "connected_by_shared_root": True,
                "split_by_wall_fingerprint": False,
                "sharp_owner_used": False,
            }
            record["component_sha256"] = _canonical_sha256(record)
            component_records.append(record)
        component_records.sort(key=lambda item: item["component_sha256"])
        pattern_endpoint = (
            make_owner_free_direction_frontier_triple_refinement_pattern_endpoint(
                schedule_endpoint_sha256=endpoint["endpoint_sha256"]
            )
        )
        pattern_components = []
        for stable in component_records:
            interaction_id = hashlib.sha256(
                (
                    "interaction-" + stable["component_sha256"]
                ).encode()
            ).hexdigest()
            failing = stable["root_count"] == 3
            static_quality = self._quality(
                status="FAIL" if failing else "PASS",
                prism_bad=21 if failing else 0,
                prism_count=960 if failing else 1140,
                core_count=34 if failing else 25,
                minimum_prism=0.0032 if failing else 0.014,
            )
            static_objective, static_objective_sha256 = self._objective(
                static_quality, direction_penalty=0.04 if failing else 0.2
            )
            search_exit_quality = (
                self._quality(
                    status="FAIL",
                    prism_bad=9,
                    prism_count=960,
                    core_count=34,
                    minimum_prism=0.0045,
                )
                if failing
                else static_quality
            )
            selected_objective, _unused = self._objective(
                search_exit_quality,
                direction_penalty=0.03 if failing else 0.2,
            )
            pattern_components.append(
                {
                    "interaction_component_sha256": interaction_id,
                    "stable": stable,
                    "failing": failing,
                    "static_quality": static_quality,
                    "static_objective": static_objective,
                    "static_objective_sha256": static_objective_sha256,
                    "search_exit_quality": search_exit_quality,
                    "selected_objective": selected_objective,
                }
            )
        pattern_components.sort(
            key=lambda item: item["interaction_component_sha256"]
        )
        static_counts = [220, 230, 240]
        static_evaluation_count = sum(static_counts)
        fine_evaluation_count = 360
        triple_evaluation_count = 24 * 4
        final_evaluation_count = 9
        fine_handoff_evaluation_count = (
            static_evaluation_count + fine_evaluation_count
        )
        total_evaluation_count = (
            fine_handoff_evaluation_count
            + triple_evaluation_count
            + final_evaluation_count
        )
        running_static_index = 0
        search_component_records = []
        for item, static_count in zip(pattern_components, static_counts):
            running_static_index += static_count
            stable = item["stable"]
            refined_count = (
                fine_evaluation_count + triple_evaluation_count
                if item["failing"]
                else 0
            )
            search_component_records.append(
                {
                    "candidate_root_count": stable["root_count"],
                    "coordinate_candidate_count": static_count - 12,
                    "interaction_component_sha256": item[
                        "interaction_component_sha256"
                    ],
                    "joint_candidate_count": 12,
                    "quality_evaluation_count": static_count + refined_count,
                    "search_exit_coordinate_sha256": hashlib.sha256(
                        (
                            "search-exit-coordinate-"
                            + item["interaction_component_sha256"]
                        ).encode()
                    ).hexdigest(),
                    "search_exit_quality_sha256": item[
                        "search_exit_quality"
                    ]["quality_sha256"],
                    "selected_objective": item["selected_objective"],
                    "source_triangle_count": stable["triangle_count"],
                    "source_triangle_sha256": stable[
                        "source_triangle_sha256"
                    ],
                    "static_exit_quality": item["static_quality"],
                    "static_exit_quality_sha256": item["static_quality"][
                        "quality_sha256"
                    ],
                    "static_exit_objective": item["static_objective"],
                    "static_exit_objective_sha256": item[
                        "static_objective_sha256"
                    ],
                    "static_exit_coordinate_sha256": hashlib.sha256(
                        (
                            "static-exit-coordinate-"
                            + item["interaction_component_sha256"]
                        ).encode()
                    ).hexdigest(),
                    "static_exit_status": item["static_quality"]["status"],
                    "static_exit_quality_evaluation_index": (
                        running_static_index
                    ),
                }
            )
        failing_component = next(
            item for item in pattern_components if item["failing"]
        )
        failing_id = failing_component["interaction_component_sha256"]
        selected_stable = failing_component["stable"]
        prefix_quality = self._quality(
            status="FAIL",
            prism_bad=13,
            prism_count=960,
            core_count=34,
            minimum_prism=0.00367627702055587,
        )
        prefix_objective, prefix_objective_sha256 = self._objective(
            prefix_quality, direction_penalty=0.035
        )
        prefix_coordinate_sha256 = hashlib.sha256(
            b"fine-prefix-coordinate"
        ).hexdigest()
        prefix_direction_state_sha256 = hashlib.sha256(
            b"fine-prefix-direction-state"
        ).hexdigest()
        prefix_direction_field_sha256 = hashlib.sha256(
            b"fine-prefix-direction-field"
        ).hexdigest()
        component_static_evidence = [
            {
                "interaction_component_sha256": record[
                    "interaction_component_sha256"
                ],
                "static_exit_status": record["static_exit_status"],
                "static_exit_quality_sha256": record[
                    "static_exit_quality_sha256"
                ],
                "static_exit_objective_sha256": record[
                    "static_exit_objective_sha256"
                ],
                "static_exit_coordinate_sha256": record[
                    "static_exit_coordinate_sha256"
                ],
                "static_exit_quality_evaluation_index": record[
                    "static_exit_quality_evaluation_index"
                ],
            }
            for record in search_component_records
        ]
        final_quality = self._quality(
            status="FAIL",
            prism_bad=9,
            minimum_prism=0.0045,
        )
        selected_search_record = next(
            record
            for record in search_component_records
            if record["interaction_component_sha256"] == failing_id
        )
        triple_coordinate_sha256 = selected_search_record[
            "search_exit_coordinate_sha256"
        ]
        triple_quality_sha256 = selected_search_record[
            "search_exit_quality_sha256"
        ]
        triple_component_quality = failing_component["search_exit_quality"]
        triple_component_objective = selected_search_record[
            "selected_objective"
        ]
        triple_objective_sha256 = _canonical_sha256(
            {
                "quality_objective_fields": [
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
                "objective": triple_component_objective,
            }
        )
        triple_selected_directions = [
            {
                "root_coordinate_sha256": root_id,
                "direction_float_hex": [
                    (0.0).hex(),
                    (0.0).hex(),
                    (1.0).hex(),
                ],
            }
            for root_id in selected_stable["root_coordinate_sha256"]
        ]
        triple_direction_state_sha256 = _canonical_sha256(
            {
                "encoding": "python-float-hex/root-sha/direction-v1",
                "records": triple_selected_directions,
            }
        )
        triple_direction_field_sha256 = _canonical_sha256(
            {"records": triple_selected_directions}
        )
        triple_state_hashes = {
            "coordinate_sha256": triple_coordinate_sha256,
            "quality_sha256": triple_quality_sha256,
            "objective_sha256": triple_objective_sha256,
            "direction_state_sha256": triple_direction_state_sha256,
            "direction_field_sha256": triple_direction_field_sha256,
        }
        selected_search_record["fine_exit_checkpoint"] = {
            "selected_state_coordinate_sha256": triple_coordinate_sha256,
            "selected_state_quality": triple_component_quality,
            "selected_state_quality_sha256": triple_quality_sha256,
            "selected_state_objective": triple_component_objective,
            "selected_state_objective_sha256": triple_objective_sha256,
            "selected_direction_state_sha256": (
                triple_direction_state_sha256
            ),
            "selected_direction_field_sha256": (
                triple_direction_field_sha256
            ),
            "selected_directions": triple_selected_directions,
            "fine_exit_quality_evaluation_index": (
                fine_handoff_evaluation_count
            ),
        }
        triple_records = []
        triple_running_index = fine_handoff_evaluation_count
        for step_index, step in enumerate(
            [2.0**-exponent for exponent in range(1, 13)], start=1
        ):
            for pass_index in range(1, 3):
                entry = dict(triple_state_hashes)
                triple_records.append(
                    {
                        "step_index_1_based": step_index,
                        "pass_index_1_based": pass_index,
                        "step_radians": step,
                        "step_radians_float_hex": step.hex(),
                        "root_candidate_counts": [
                            {
                                "root_coordinate_sha256": root_id,
                                "candidate_count": count,
                            }
                            for root_id, count in zip(
                                selected_stable["root_coordinate_sha256"],
                                [2, 2, 1],
                            )
                        ],
                        "candidate_upper_bound": 64,
                        "atomic_candidate_count": 4,
                        "rejected_margin_candidate_count": 0,
                        "quality_evaluation_start_index_exclusive": (
                            triple_running_index
                        ),
                        "first_quality_evaluation_index": (
                            triple_running_index + 1
                        ),
                        "last_quality_evaluation_index": (
                            triple_running_index + 4
                        ),
                        "quality_evaluation_end_index_inclusive": (
                            triple_running_index + 4
                        ),
                        "quality_evaluation_count": 4,
                        "accepted_move": False,
                        **{
                            f"entry_{key}": value
                            for key, value in entry.items()
                        },
                        **{
                            f"exit_{key}": value
                            for key, value in entry.items()
                        },
                    }
                )
                triple_running_index += 4
        triple_selection = {
            "schema": (
                "cfdpipe.coarse_schedule_frontier_triple_refinement_selection.v1"
            ),
            "status": "PASS",
            "gate": {
                "static_all_components_complete_before_fine": True,
                "single_and_pair_phases_complete_before_triple": True,
                "static_failing_component_count": 1,
                "fine_refinement_performed": True,
                "triple_refinement_performed": True,
                "required_triple_component_root_count": 3,
                "required_triple_component_source_triangle_count": 1,
                "selected_component_shape_valid": True,
            },
            "selected_component": {
                "interaction_component_sha256": failing_id,
                "candidate_root_count": 3,
                "root_coordinate_sha256": selected_stable[
                    "root_coordinate_sha256"
                ],
                "source_triangle_count": 1,
                "source_triangle_sha256": selected_stable[
                    "source_triangle_sha256"
                ],
                "unique_source_triangle_edge_count": 3,
            },
            "handoff": {
                "selected_component_sha256": failing_id,
                **{
                    f"fine_exit_{key}": value
                    for key, value in triple_state_hashes.items()
                },
                "fine_exit_quality": triple_component_quality,
                "fine_exit_objective": triple_component_objective,
                **{
                    f"triple_entry_{key}": value
                    for key, value in triple_state_hashes.items()
                },
                "fine_exit_quality_evaluation_index": (
                    fine_handoff_evaluation_count
                ),
                "triple_quality_evaluation_start_index_exclusive": (
                    fine_handoff_evaluation_count
                ),
                "without_reset": True,
            },
            "schedule": {
                "steps_radians": [
                    2.0**-exponent for exponent in range(1, 13)
                ],
                "coordinate_passes_per_step": 2,
                "tangent_candidates_per_root_per_step": 4,
                "atomic_cartesian_candidates_per_step_pass": 64,
            },
            "step_pass_records": triple_records,
            "evaluation_evidence": {
                "endpoint_maximum_quality_evaluations": 4096,
                "global_conservative_quality_evaluation_cap": 3723,
                "static_quality_evaluation_count": static_evaluation_count,
                "fine_quality_evaluation_count": fine_evaluation_count,
                "through_fine_handoff_quality_evaluation_count": (
                    fine_handoff_evaluation_count
                ),
                "triple_quality_evaluation_count": triple_evaluation_count,
                "final_replay_and_verification_quality_evaluation_count": (
                    final_evaluation_count
                ),
                "total_quality_evaluation_count": total_evaluation_count,
                "triple_quality_evaluation_start_index_exclusive": (
                    fine_handoff_evaluation_count
                ),
                "first_triple_quality_evaluation_index": (
                    fine_handoff_evaluation_count + 1
                ),
                "last_triple_quality_evaluation_index": (
                    fine_handoff_evaluation_count
                    + triple_evaluation_count
                ),
                "triple_quality_evaluation_end_index_inclusive": (
                    fine_handoff_evaluation_count
                    + triple_evaluation_count
                ),
            },
            "final_state": {
                "selected_component_sha256": failing_id,
                "selected_state_coordinate_sha256": (
                    triple_coordinate_sha256
                ),
                "selected_state_quality": triple_component_quality,
                "selected_state_quality_sha256": triple_quality_sha256,
                "selected_state_objective": triple_component_objective,
                "selected_state_objective_sha256": (
                    triple_objective_sha256
                ),
                "selected_direction_state_sha256": (
                    triple_direction_state_sha256
                ),
                "selected_direction_field_sha256": (
                    triple_direction_field_sha256
                ),
                "triple_quality_evaluation_end_index_inclusive": (
                    fine_handoff_evaluation_count
                    + triple_evaluation_count
                ),
            },
        }
        collar_ring_widths = [1, 2, 4, 8, 16, 32, 64]
        collar_entry_schedule_sha256 = hashlib.sha256(
            b"collar-entry-schedule"
        ).hexdigest()
        collar_connectivity_sha256 = hashlib.sha256(
            b"collar-connectivity"
        ).hexdigest()
        collar_inner_sha256 = hashlib.sha256(
            b"collar-frozen-inner-prefix"
        ).hexdigest()
        collar_non_target_sha256 = hashlib.sha256(
            b"collar-non-target"
        ).hexdigest()
        collar_edges = []
        for left_index, right_index in ((0, 1), (0, 2), (1, 2)):
            edge = {
                "root_coordinate_sha256": sorted(
                    [
                        selected_stable["root_coordinate_sha256"][left_index],
                        selected_stable["root_coordinate_sha256"][right_index],
                    ]
                )
            }
            edge["edge_sha256"] = _canonical_sha256(edge)
            collar_edges.append(edge)
        collar_edges.sort(key=lambda item: item["edge_sha256"])
        collar_distance_records = [
            {
                "distance": 0,
                "root_coordinate_sha256": selected_stable[
                    "root_coordinate_sha256"
                ],
            }
        ]
        collar_graph = {
            "scope": "target_wall_source_triangle_edge_graph",
            "wall_surface_fingerprint": selected_stable[
                "wall_surface_fingerprints"
            ][0],
            "source_triangle_count": 1,
            "root_count": 3,
            "edge_count": 3,
            "seed_root_coordinate_sha256": selected_stable[
                "root_coordinate_sha256"
            ],
            "root_coordinate_sha256": selected_stable[
                "root_coordinate_sha256"
            ],
            "source_triangle_sha256": selected_stable[
                "source_triangle_sha256"
            ],
            "triangle_records": [
                {
                    "source_triangle_sha256": selected_stable[
                        "source_triangle_sha256"
                    ][0],
                    "wall_surface_fingerprint": selected_stable[
                        "wall_surface_fingerprints"
                    ][0],
                    "root_coordinate_sha256": selected_stable[
                        "root_coordinate_sha256"
                    ],
                }
            ],
            "edge_records": collar_edges,
            "distance_records": collar_distance_records,
        }
        collar_graph["graph_sha256"] = _canonical_sha256(
            {
                key: collar_graph[key]
                for key in (
                    "scope",
                    "wall_surface_fingerprint",
                    "root_coordinate_sha256",
                    "source_triangle_sha256",
                    "triangle_records",
                    "edge_records",
                )
            }
        )
        collar_graph["distances_sha256"] = _canonical_sha256(
            collar_distance_records
        )
        collar_handoff = {
            "without_reset_from_triple_final": True,
            "entry_schedule_sha256": collar_entry_schedule_sha256,
            "entry_direction_field_sha256": triple_direction_field_sha256,
            "entry_coordinate_sha256": triple_coordinate_sha256,
            "entry_quality_sha256": triple_quality_sha256,
            "entry_objective": triple_component_objective,
            "entry_objective_sha256": triple_objective_sha256,
        }
        collar_bad_records = []
        for layer_index in range(52, 61):
            unsigned_bad = {
                "source_triangle_sha256": selected_stable[
                    "source_triangle_sha256"
                ][0],
                "wall_surface_fingerprint": selected_stable[
                    "wall_surface_fingerprints"
                ][0],
                "root_coordinate_sha256": selected_stable[
                    "root_coordinate_sha256"
                ],
                "layer_index_1_based": layer_index,
            }
            collar_bad_records.append(
                {
                    "prism_element_sha256": _canonical_sha256(unsigned_bad),
                    **unsigned_bad,
                }
            )
        collar_bad_records.sort(
            key=lambda item: item["prism_element_sha256"]
        )
        collar_bad_records_sha256 = _canonical_sha256(collar_bad_records)
        collar_prism_records = []
        for layer_index in range(1, 61):
            unsigned_prism = {
                "source_triangle_sha256": selected_stable[
                    "source_triangle_sha256"
                ][0],
                "wall_surface_fingerprint": selected_stable[
                    "wall_surface_fingerprints"
                ][0],
                "root_coordinate_sha256": selected_stable[
                    "root_coordinate_sha256"
                ],
                "layer_index_1_based": layer_index,
            }
            collar_prism_records.append(
                {
                    "prism_element_sha256": _canonical_sha256(
                        unsigned_prism
                    ),
                    **unsigned_prism,
                }
            )
        collar_prism_records.sort(
            key=lambda item: item["prism_element_sha256"]
        )
        collar_prism_records_sha256 = _canonical_sha256(
            collar_prism_records
        )
        collar_triangle_records = collar_graph["triangle_records"]
        collar_triangle_records_sha256 = _canonical_sha256(
            collar_triangle_records
        )
        collar_chain_records = []
        for root_id in selected_stable["root_coordinate_sha256"]:
            for layer_index in range(52, 61):
                unsigned_node = {
                    "root_coordinate_sha256": root_id,
                    "layer_index_1_based": layer_index,
                }
                collar_chain_records.append(
                    {
                        "chain_node_sha256": _canonical_sha256(unsigned_node),
                        **unsigned_node,
                    }
                )
        collar_chain_records.sort(
            key=lambda item: item["chain_node_sha256"]
        )
        collar_chain_records_sha256 = _canonical_sha256(
            collar_chain_records
        )
        outer_node_ids = sorted(
            item["chain_node_sha256"]
            for item in collar_chain_records
            if item["layer_index_1_based"] == 60
        )
        core_center_sha256 = hashlib.sha256(b"collar-core-center").hexdigest()
        core_nodes = sorted(outer_node_ids + [core_center_sha256])
        unsigned_core = {
            "element_type": "Tetrahedron 4",
            "node_sha256": core_nodes,
        }
        collar_core_records = [
            {
                "core_element_sha256": _canonical_sha256(unsigned_core),
                **unsigned_core,
            }
        ]
        collar_core_records_sha256 = _canonical_sha256(collar_core_records)
        collar_affected_element_sha256 = _canonical_sha256(
            sorted(
                [
                    item["prism_element_sha256"]
                    for item in collar_prism_records
                ]
                + [
                    item["core_element_sha256"]
                    for item in collar_core_records
                ]
            )
        )
        first_height = 2.7645771273320875e-7
        entry_cumulative = [
            first_height * (layer + 0.01 * layer * (layer - 1))
            for layer in range(1, 61)
        ]
        collar_candidates = []
        for candidate_index, (ring_width, prism_bad) in enumerate(
            zip(collar_ring_widths, range(8, 1, -1)), start=1
        ):
            candidate_quality = self._quality(
                status="FAIL",
                prism_bad=prism_bad,
                minimum_prism=0.0045 + candidate_index * 0.0001,
            )
            candidate_objective, candidate_objective_sha256 = self._objective(
                candidate_quality, direction_penalty=0.0
            )
            schedule_sha256 = hashlib.sha256(
                f"collar-schedule-{ring_width}".encode()
            ).hexdigest()
            coordinate_sha256 = hashlib.sha256(
                f"collar-coordinate-{ring_width}".encode()
            ).hexdigest()
            schedule_records = []
            for root_id in selected_stable["root_coordinate_sha256"]:
                exit_cumulative = []
                a_k = 51 * first_height
                b_k = entry_cumulative[50]
                for layer_index, b_value in enumerate(
                    entry_cumulative, start=1
                ):
                    a_value = layer_index * first_height
                    c_value = (
                        b_value
                        if layer_index <= 51
                        else b_k + (a_value - a_k)
                    )
                    exit_cumulative.append(c_value)
                schedule_records.append(
                    {
                        "root_coordinate_sha256": root_id,
                        "distance": 0,
                        "spatial_weight_float_hex": (1.0).hex(),
                        "entry_cumulative_distance_float_hex": [
                            item.hex() for item in entry_cumulative
                        ],
                        "exit_cumulative_distance_float_hex": [
                            item.hex() for item in exit_cumulative
                        ],
                    }
                )
            schedule_records.sort(
                key=lambda item: item["root_coordinate_sha256"]
            )
            schedule_records_sha256 = _canonical_sha256(schedule_records)
            schedule_proof_sha256 = _canonical_sha256(
                {
                    "frozen_prefix_last_layer_1_based": 51,
                    "ring_width": ring_width,
                    "schedule_records": schedule_records,
                }
            )
            low_quality_records = collar_bad_records[:prism_bad]
            low_quality_records = sorted(
                low_quality_records,
                key=lambda item: item["prism_element_sha256"],
            )
            low_quality_records_sha256 = _canonical_sha256(
                low_quality_records
            )
            low_quality_aggregate = {
                "count": prism_bad,
                "unique_source_triangle_count": 1,
                "wall_count": 1,
                "records_sha256": low_quality_records_sha256,
            }
            low_quality_aggregate["aggregate_sha256"] = _canonical_sha256(
                low_quality_aggregate
            )
            collar_candidates.append(
                {
                    "candidate_index_1_based": candidate_index,
                    "ring_width": ring_width,
                    "affected_root_count": 3,
                    "affected_triangle_count": 1,
                    "affected_prism_element_count": 60,
                    "affected_core_element_count": 1,
                    "affected_chain_node_count": 27,
                    "affected_root_sha256": _canonical_sha256(
                        selected_stable["root_coordinate_sha256"]
                    ),
                    "affected_triangle_sha256": _canonical_sha256(
                        selected_stable["source_triangle_sha256"]
                    ),
                    "affected_element_sha256": (
                        collar_affected_element_sha256
                    ),
                    "affected_triangle_records": collar_triangle_records,
                    "affected_triangle_records_sha256": (
                        collar_triangle_records_sha256
                    ),
                    "affected_chain_node_records": collar_chain_records,
                    "affected_chain_node_records_sha256": (
                        collar_chain_records_sha256
                    ),
                    "affected_prism_records": collar_prism_records,
                    "affected_prism_records_sha256": (
                        collar_prism_records_sha256
                    ),
                    "affected_core_records": collar_core_records,
                    "affected_core_records_sha256": (
                        collar_core_records_sha256
                    ),
                    "entry_schedule_sha256": collar_entry_schedule_sha256,
                    "entry_direction_field_sha256": (
                        triple_direction_field_sha256
                    ),
                    "entry_coordinate_sha256": triple_coordinate_sha256,
                    "entry_quality_sha256": triple_quality_sha256,
                    "entry_objective_sha256": triple_objective_sha256,
                    "exit_schedule_sha256": schedule_sha256,
                    "exit_coordinate_sha256": coordinate_sha256,
                    "exit_quality_sha256": candidate_quality[
                        "quality_sha256"
                    ],
                    "exit_objective_sha256": candidate_objective_sha256,
                    "direction_field_sha256": (
                        triple_direction_field_sha256
                    ),
                    "exit_direction_field_sha256": (
                        triple_direction_field_sha256
                    ),
                    "schedule_sha256": schedule_sha256,
                    "schedule_records": schedule_records,
                    "schedule_records_sha256": schedule_records_sha256,
                    "collar_schedule_proof_sha256": (
                        schedule_proof_sha256
                    ),
                    "coordinate_sha256": coordinate_sha256,
                    "coordinate_reduction_l2_float_hex": float(
                        candidate_index
                    ).hex(),
                    "quality_evaluation_index": (
                        total_evaluation_count + candidate_index
                    ),
                    "quality": candidate_quality,
                    "quality_sha256": candidate_quality["quality_sha256"],
                    "low_quality_aggregate": low_quality_aggregate,
                    "low_quality_records": low_quality_records,
                    "low_quality_records_sha256": (
                        low_quality_records_sha256
                    ),
                    "objective": candidate_objective,
                    "objective_sha256": candidate_objective_sha256,
                    "new_bad_lineage_count": 0,
                    "lineage_contained": True,
                    "inner_prefix_changed_node_count": 0,
                    "outside_closure_changed_node_count": 0,
                    "base_root_moved_count": 0,
                    "connectivity_sha256": collar_connectivity_sha256,
                    "status": "FAIL",
                }
            )
        collar_selected = collar_candidates[-1]
        collar_final_quality = collar_selected["quality"]
        collar_selection = {
            "schema": (
                "cfdpipe.coarse_schedule_frontier_collar_refinement_selection.v1"
            ),
            "status": "EXHAUSTED",
            "gate": {
                "eligible": True,
                "reason": "ELIGIBLE_CONTIGUOUS_OUTER_TAIL",
                "triple_status": "EXHAUSTED",
                "triple_accepted_move_count": 0,
                "residual_component_count": 1,
                "residual_source_triangle_count": 1,
                "residual_root_count": 3,
                "residual_wall_count": 1,
                "residual_prism_count": 9,
                "residual_bad_records": collar_bad_records,
                "residual_bad_records_sha256": (
                    collar_bad_records_sha256
                ),
                "minimum_bad_layer_1_based": 52,
                "maximum_bad_layer_1_based": 60,
                "outer_tail_contiguous": True,
                "layer_count": 60,
                "frozen_prefix_last_layer_1_based": 51,
                "minimum_frozen_prefix_layer_count": 15,
                "core_tetra_below_gamma_count": 0,
                "nonpositive_element_count": 0,
                "nonfinite_count": 0,
                "target_wall_surface_fingerprint": selected_stable[
                    "wall_surface_fingerprints"
                ][0],
                "target_source_triangle_sha256": selected_stable[
                    "source_triangle_sha256"
                ][0],
                "seed_root_coordinate_sha256": selected_stable[
                    "root_coordinate_sha256"
                ],
            },
            "handoff": collar_handoff,
            "schedule": {
                "formula": (
                    "C_i=B_K+(A_i-A_K)+(1-w)*"
                    "((B_i-B_K)-(A_i-A_K))"
                ),
                "minimum_growth_formula": "A_i=i*h1",
                "layer_count": 60,
                "first_layer_height_float_hex": (
                    2.7645771273320875e-7
                ).hex(),
                "frozen_prefix_last_layer_1_based": 51,
                "frozen_prefix_preserved": True,
                "directions_frozen": True,
                "connectivity_frozen": True,
                "no_amplification": True,
                "strictly_increasing": True,
            },
            "graph": collar_graph,
            "ring_widths": collar_ring_widths,
            "candidate_records": collar_candidates,
            "evaluation_evidence": {
                "pattern_quality_evaluation_count": total_evaluation_count,
                "collar_quality_evaluation_start_index_exclusive": (
                    total_evaluation_count
                ),
                "collar_quality_evaluation_count": 7,
                "collar_quality_evaluation_end_index_inclusive": (
                    total_evaluation_count + 7
                ),
                "maximum_collar_quality_evaluations": 7,
                "total_quality_evaluation_count": (
                    total_evaluation_count + 7
                ),
                "maximum_total_quality_evaluations": 4096,
                "within_cap": True,
            },
            "selection": {
                "selection_basis": (
                    "count_first_quality_then_minimum_affected_closure_then_"
                    "coordinate_change"
                ),
                "pass_candidate_count": 0,
                "selected_candidate_index_1_based": 7,
                "selected_ring_width": 64,
                "selected_status": "FAIL",
                "selected_schedule_sha256": collar_selected[
                    "schedule_sha256"
                ],
                "selected_coordinate_sha256": collar_selected[
                    "coordinate_sha256"
                ],
                "selected_quality_sha256": collar_selected[
                    "quality_sha256"
                ],
                "selected_quality": collar_final_quality,
                "selected_affected_root_count": 3,
                "selected_affected_triangle_count": 1,
                "selected_affected_prism_element_count": 60,
                "selected_affected_core_element_count": 1,
                "selected_affected_chain_node_count": 27,
                "selected_coordinate_reduction_l2_float_hex": (7.0).hex(),
            },
            "final_state": {
                "schedule_sha256": collar_selected["schedule_sha256"],
                "direction_field_sha256": triple_direction_field_sha256,
                "coordinate_sha256": collar_selected["coordinate_sha256"],
                "quality_sha256": collar_selected["quality_sha256"],
                "quality": collar_final_quality,
            },
            "preservation": {
                "entry_inner_prefix_coordinate_sha256": collar_inner_sha256,
                "final_inner_prefix_coordinate_sha256": collar_inner_sha256,
                "inner_prefix_reproducible": True,
                "entry_non_target_coordinate_sha256": (
                    collar_non_target_sha256
                ),
                "final_non_target_coordinate_sha256": (
                    collar_non_target_sha256
                ),
                "non_target_reproducible": True,
                "entry_connectivity_sha256": collar_connectivity_sha256,
                "final_connectivity_sha256": collar_connectivity_sha256,
                "connectivity_reproducible": True,
            },
        }
        evidence = {
            "schema": DISCOVERY_SCHEMA,
            "status": "INCOMPLETE",
            "profile_complete": False,
            "request": REQUEST,
            "audit_only": True,
            "audit_variant": "physical_schedule_first_frontier_direction_continuation",
            "calibration_PASS_authorized": False,
            "production_mesh_eligible": False,
            "mesh_write_authorized": False,
            "mesh_written": False,
            "su2_called": False,
            "paraview_called": False,
            "runtime_mesh_tags_hardcoded": False,
            "source_bindings": endpoint["source_bindings"],
            "endpoint": endpoint,
            "fixed_direction_input": {},
            "frontier_input": {
                "previous_state": {
                    "fraction_float_hex": endpoint["previous_fraction_float_hex"],
                    "quality": previous_quality,
                },
                "target_state": {
                    "fraction_float_hex": endpoint["target_fraction_float_hex"],
                    "schedule_sha256": collar_entry_schedule_sha256,
                    "quality": target_quality,
                    "low_quality_aggregate": aggregate,
                },
            },
            "components": {
                "component_count": 3,
                "candidate_root_count": 12,
                "maximum_component_count": 3,
                "maximum_candidate_root_count": 16,
                "within_caps": True,
                "connected_by_shared_root": True,
                "split_by_wall_fingerprint": False,
                "records": component_records,
            },
            "pattern_search": {
                "endpoint_sha256": pattern_endpoint["endpoint_sha256"],
                "status": "EXHAUSTED",
                "component_count": 3,
                "candidate_root_count": 12,
                "quality_evaluation_count": total_evaluation_count,
                "component_records": search_component_records,
                "tangent_pattern_refinement": {
                    "performed": True,
                    "steps_radians": [
                        2.0**-exponent for exponent in range(1, 13)
                    ],
                    "coordinate_passes_per_step": 2,
                    "shared_triangle_pair_moves": True,
                    "quality_evaluation_count": fine_evaluation_count,
                    "refined_component_count": 1,
                },
                "triple_pattern_refinement": {
                    "performed": True,
                    "steps_radians": [
                        2.0**-exponent for exponent in range(1, 13)
                    ],
                    "coordinate_passes_per_step": 2,
                    "tangent_candidates_per_root_per_step": 4,
                    "atomic_cartesian_candidates_per_step_pass": 64,
                    "quality_evaluation_count": triple_evaluation_count,
                    "accepted_move_count": 0,
                    "rejected_margin_candidate_count": 0,
                    "refined_component_count": 1,
                    "step_pass_records": triple_records,
                },
                "triple_refinement_selection": triple_selection,
                "changed_roots": [],
            },
            "fine_refinement_selection": {
                "schema": (
                    "cfdpipe.coarse_schedule_frontier_fine_refinement_selection.v1"
                ),
                "status": "PASS",
                "gate": {
                    "static_all_components_complete_before_fine": True,
                    "static_search_started_from_fresh_a": True,
                    "static_component_count": 3,
                    "static_failing_component_count": 1,
                    "maximum_refined_component_count": 1,
                    "required_refined_component_root_count": 3,
                    "required_refined_component_source_triangle_count": 1,
                    "fine_refinement_performed": True,
                },
                "order": {
                    "static_component_sha256_order": [
                        item["interaction_component_sha256"]
                        for item in pattern_components
                    ],
                    "fine_component_sha256_order": [failing_id],
                    "last_static_quality_evaluation_index": (
                        static_evaluation_count
                    ),
                    "first_fine_quality_evaluation_index": (
                        static_evaluation_count + 1
                    ),
                    "all_static_exits_before_first_fine_evaluation": True,
                    "tail_steps_radians": [
                        2.0**-exponent for exponent in range(6, 13)
                    ],
                    "tail_start_coordinate_sha256": prefix_coordinate_sha256,
                    "tail_start_direction_state_sha256": (
                        prefix_direction_state_sha256
                    ),
                    "tail_start_direction_field_sha256": (
                        prefix_direction_field_sha256
                    ),
                    "tail_start_quality_sha256": prefix_quality[
                        "quality_sha256"
                    ],
                    "tail_started_from_prefix_checkpoint": True,
                    "first_tail_quality_evaluation_index": (
                        static_evaluation_count + 181
                    ),
                },
                "evaluation_evidence": {
                    "endpoint_maximum_quality_evaluations": 4096,
                    "global_conservative_quality_evaluation_cap": 2187,
                    "static_quality_evaluation_count": (
                        static_evaluation_count
                    ),
                    "fine_quality_evaluation_count": fine_evaluation_count,
                    "total_quality_evaluation_count": (
                        fine_handoff_evaluation_count
                    ),
                },
                "selected_component": {
                    "interaction_component_sha256": failing_id,
                    "candidate_root_count": 3,
                    "root_coordinate_sha256": selected_stable[
                        "root_coordinate_sha256"
                    ],
                    "source_triangle_count": 1,
                    "source_triangle_sha256": selected_stable[
                        "source_triangle_sha256"
                    ],
                    "unique_source_triangle_edge_count": 3,
                },
                "prefix_checkpoint": {
                    "selected_component_sha256": failing_id,
                    "prefix_steps_radians": [
                        2.0**-exponent for exponent in range(1, 6)
                    ],
                    "selected_state_coordinate_sha256": (
                        prefix_coordinate_sha256
                    ),
                    "selected_state_quality": prefix_quality,
                    "selected_state_quality_sha256": prefix_quality[
                        "quality_sha256"
                    ],
                    "selected_state_objective": prefix_objective,
                    "selected_state_objective_sha256": (
                        prefix_objective_sha256
                    ),
                    "selected_direction_state_sha256": (
                        prefix_direction_state_sha256
                    ),
                    "selected_direction_field_sha256": (
                        prefix_direction_field_sha256
                    ),
                    "quality_evaluation_index": (
                        static_evaluation_count + 180
                    ),
                },
                "component_static_evidence": component_static_evidence,
            },
            "triple_refinement_selection": triple_selection,
            "collar_refinement_selection": collar_selection,
            "selected_direction_field": {
                "changed_root_count": 0,
                "changed_roots": [],
            },
            "cross_schedule_replay": {
                "sequence_fraction_float_hex": [
                    endpoint["previous_fraction_float_hex"],
                    endpoint["target_fraction_float_hex"],
                    endpoint["previous_fraction_float_hex"],
                    endpoint["target_fraction_float_hex"],
                ],
                "state_a_first_coordinate_sha256": "a" * 64,
                "state_b_first_coordinate_sha256": collar_selected[
                    "coordinate_sha256"
                ],
                "state_a_second_coordinate_sha256": "a" * 64,
                "state_b_second_coordinate_sha256": collar_selected[
                    "coordinate_sha256"
                ],
                "state_a_first_quality_sha256": previous_quality["quality_sha256"],
                "state_b_first_quality_sha256": collar_final_quality[
                    "quality_sha256"
                ],
                "state_a_second_quality_sha256": previous_quality["quality_sha256"],
                "state_b_second_quality_sha256": collar_final_quality[
                    "quality_sha256"
                ],
                "state_a_reproducible": True,
                "state_b_reproducible": True,
                "both_states_pass": False,
                "base_root_moved_count": 0,
                "non_target_node_changed_count": 0,
                "non_target_coordinate_sha256": collar_non_target_sha256,
                "selected_schedule_sha256": collar_selected[
                    "schedule_sha256"
                ],
                "selected_coordinate_sha256": collar_selected[
                    "coordinate_sha256"
                ],
                "selected_quality_sha256": collar_final_quality[
                    "quality_sha256"
                ],
            },
            "final_quality": collar_final_quality,
            "observed_counts": {
                "projected_3d_element_count": 294059,
                "prism_element_count": 280320,
                "core_element_count": 13739,
                "core_tetra_count": 13739,
                "target_low_quality_prism_count": 37,
                "target_unique_source_triangle_count": 5,
                "target_wall_count": 2,
                "component_count": 3,
                "candidate_root_count": 12,
                "pattern_quality_evaluation_count": total_evaluation_count,
                "collar_quality_evaluation_count": 7,
                "quality_evaluation_count": total_evaluation_count + 7,
                "changed_root_count": 0,
                "final_nonpositive_element_count": 0,
                "final_prism_below_threshold_count": 2,
                "final_core_tetra_below_gamma_count": 0,
                "final_nonfinite_count": 0,
            },
            "incomplete_reason": "NO_PASS_WITHIN_FROZEN_COLLAR_CAPS",
        }
        evidence["pattern_search"]["fine_refinement_selection"] = copy.deepcopy(
            evidence["fine_refinement_selection"]
        )
        evidence["discovery_sha256"] = _canonical_sha256(evidence)
        return endpoint, evidence

    def test_discovery_compatibility_validator_accepts_scientific_incomplete(self):
        endpoint, evidence = self._evidence()
        validated = validate_schedule_frontier_direction_continuation(
            evidence,
            expected_endpoint=endpoint,
            expected_projected_3d_elements=294059,
            expected_prism_element_count=280320,
            expected_core_element_count=13739,
        )
        self.assertEqual(validated, evidence)

    def test_collar_rejects_rehashed_graph_schedule_closure_and_lineage_tampering(self):
        endpoint, evidence = self._evidence()

        def rehash_discovery(item):
            item["discovery_sha256"] = _canonical_sha256(
                {
                    key: value
                    for key, value in item.items()
                    if key != "discovery_sha256"
                }
            )

        def stale_graph(item):
            graph = item["collar_refinement_selection"]["graph"]
            graph["edge_records"].pop()
            graph["edge_count"] -= 1
            graph["graph_sha256"] = _canonical_sha256(
                {
                    key: graph[key]
                    for key in (
                        "scope",
                        "wall_surface_fingerprint",
                        "root_coordinate_sha256",
                        "source_triangle_sha256",
                        "triangle_records",
                        "edge_records",
                    )
                }
            )

        def gapped_residual(item):
            gate = item["collar_refinement_selection"]["gate"]
            records = gate["residual_bad_records"]
            unsigned = {
                key: value
                for key, value in records[0].items()
                if key != "prism_element_sha256"
            }
            unsigned["layer_index_1_based"] = 51
            records[0] = {
                "prism_element_sha256": _canonical_sha256(unsigned),
                **unsigned,
            }
            records.sort(key=lambda record: record["prism_element_sha256"])
            gate["residual_bad_records_sha256"] = _canonical_sha256(records)

        def stale_schedule_handoff(item):
            item["collar_refinement_selection"]["handoff"][
                "entry_schedule_sha256"
            ] = "f" * 64

        def broken_schedule_formula(item):
            record = item["collar_refinement_selection"]["candidate_records"][0]
            root_schedule = record["schedule_records"][0]
            root_schedule["exit_cumulative_distance_float_hex"][-1] = (
                root_schedule["entry_cumulative_distance_float_hex"][-1]
            )
            record["schedule_records_sha256"] = _canonical_sha256(
                record["schedule_records"]
            )
            record["collar_schedule_proof_sha256"] = _canonical_sha256(
                {
                    "frozen_prefix_last_layer_1_based": 51,
                    "ring_width": record["ring_width"],
                    "schedule_records": record["schedule_records"],
                }
            )

        def changed_direction(item):
            item["collar_refinement_selection"]["candidate_records"][0][
                "exit_direction_field_sha256"
            ] = "e" * 64

        def omitted_core_closure(item):
            record = item["collar_refinement_selection"]["candidate_records"][0]
            record["affected_core_records"] = []
            record["affected_core_records_sha256"] = _canonical_sha256([])
            record["affected_core_element_count"] = 0

        def introduced_bad_lineage(item):
            record = item["collar_refinement_selection"]["candidate_records"][0]
            bad_records = record["low_quality_records"]
            unsigned = {
                key: value
                for key, value in bad_records[0].items()
                if key != "prism_element_sha256"
            }
            unsigned["layer_index_1_based"] = 51
            bad_records[0] = {
                "prism_element_sha256": _canonical_sha256(unsigned),
                **unsigned,
            }
            bad_records.sort(
                key=lambda candidate: candidate["prism_element_sha256"]
            )
            digest = _canonical_sha256(bad_records)
            record["low_quality_records_sha256"] = digest
            aggregate = record["low_quality_aggregate"]
            aggregate["records_sha256"] = digest
            aggregate["aggregate_sha256"] = _canonical_sha256(
                {
                    key: value
                    for key, value in aggregate.items()
                    if key != "aggregate_sha256"
                }
            )

        def unsafe_core_quality(item):
            record = item["collar_refinement_selection"]["candidate_records"][0]
            quality = self._quality(
                status="FAIL",
                prism_bad=8,
                core_bad=1,
                minimum_prism=0.0046,
            )
            objective, objective_sha256 = self._objective(
                quality, direction_penalty=0.0
            )
            record.update(
                {
                    "quality": quality,
                    "quality_sha256": quality["quality_sha256"],
                    "exit_quality_sha256": quality["quality_sha256"],
                    "objective": objective,
                    "objective_sha256": objective_sha256,
                    "exit_objective_sha256": objective_sha256,
                }
            )

        mutations = (
            stale_graph,
            gapped_residual,
            stale_schedule_handoff,
            broken_schedule_formula,
            changed_direction,
            omitted_core_closure,
            introduced_bad_lineage,
            unsafe_core_quality,
            lambda item: item["collar_refinement_selection"][
                "candidate_records"
            ][1].update(
                {
                    "quality_evaluation_index": item[
                        "collar_refinement_selection"
                    ]["candidate_records"][0]["quality_evaluation_index"]
                }
            ),
            lambda item: item["cross_schedule_replay"].update(
                {"selected_schedule_sha256": "d" * 64}
            ),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                tampered = copy.deepcopy(evidence)
                mutate(tampered)
                rehash_discovery(tampered)
                with self.assertRaises(CoarseScheduleContinuationError):
                    validate_schedule_frontier_direction_continuation(
                        tampered,
                        expected_endpoint=endpoint,
                        expected_projected_3d_elements=294059,
                        expected_prism_element_count=280320,
                        expected_core_element_count=13739,
                    )

    def test_actual_runner_triple_evidence_matches_the_v4_contract(self):
        from cfdpipe.coarse_schedule_continuation import (
            _expected_frontier_pattern_endpoint,
            _validate_static_and_fine_pattern_evidence,
            _validate_triple_pattern_evidence,
        )
        from tests.test_boundary_layer_smoke import BoundaryLayerSmokeTests

        endpoint, _evidence = self._evidence()
        run = BoundaryLayerSmokeTests("_run_fine_search")._run_fine_search(
            failing_prism_tags={102}, triple=True
        )
        self.assertIsNone(run["error"])
        search, _quality, _selected = run["result"]
        child = _expected_frontier_pattern_endpoint(endpoint)
        context = _validate_static_and_fine_pattern_evidence(
            pattern_search=search,
            selection=search["fine_refinement_selection"],
            endpoint=endpoint,
            expected_pattern_endpoint=child,
            stable_component_records=run["fixture"]["stable_components"],
            total_quality_evaluation_count=search[
                "quality_evaluation_count"
            ],
        )
        _validate_triple_pattern_evidence(
            pattern_search=search,
            selection=search["triple_refinement_selection"],
            endpoint=endpoint,
            expected_pattern_endpoint=child,
            fine_context=context,
            total_quality_evaluation_count=search[
                "quality_evaluation_count"
            ],
        )
        self.assertEqual(
            search["triple_refinement_selection"]["evaluation_evidence"][
                "triple_quality_evaluation_count"
            ],
            1536,
        )

    def test_actual_collar_runner_selection_validates_in_complete_discovery(self):
        import cfdpipe.boundary_layer_smoke as smoke
        from tests.test_boundary_layer_smoke import _HomotopyGmsh

        endpoint, evidence = self._evidence()
        search = copy.deepcopy(evidence["pattern_search"])
        triple = search["triple_refinement_selection"]
        selected = triple["selected_component"]
        selected_roots = list(selected["root_coordinate_sha256"])
        selected_triangle = selected["source_triangle_sha256"][0]
        stable_component = next(
            record
            for record in evidence["components"]["records"]
            if record["root_coordinate_sha256"] == selected_roots
            and record["source_triangle_sha256"]
            == selected["source_triangle_sha256"]
        )
        wall = stable_component["wall_surface_fingerprints"][0]
        raw_bad_records = [
            {
                "interaction_component_sha256": selected[
                    "interaction_component_sha256"
                ],
                "source_triangle_sha256": selected_triangle,
                "wall_surface_fingerprint": wall,
                "root_coordinate_sha256": selected_roots,
                "layer_1_based": layer,
            }
            for layer in range(52, 61)
        ]
        search["final_low_quality_prisms"] = {"records": raw_bad_records}

        roots = [1, 2, 3]
        origins = {
            1: (0.0, 0.0, 0.0),
            2: (1.0, 0.0, 0.0),
            3: (0.0, 1.0, 0.0),
        }
        first_height = float(endpoint["first_layer_height_m"])
        baseline = [
            first_height * layer * (1.0 + 0.01 * (layer - 1))
            for layer in range(1, 61)
        ]
        target_schedules = {root: list(baseline) for root in roots}
        directions = {root: (0.0, 0.0, 1.0) for root in roots}
        chains = {
            root: [root] + [root * 1000 + layer for layer in range(1, 61)]
            for root in roots
        }
        coordinates = dict(origins)
        chain_origin = {}
        for root in roots:
            chain_origin[root] = (root, 0)
            for layer, node in enumerate(chains[root][1:], start=1):
                coordinates[node] = (
                    origins[root][0],
                    origins[root][1],
                    baseline[layer - 1],
                )
                chain_origin[node] = (root, layer)
        fixed_core_node = 9999
        coordinates[fixed_core_node] = (0.25, 0.25, baseline[-1] + first_height)
        prism_records = []
        for layer in range(1, 61):
            prism_records.append(
                {
                    "tag": 10000 + layer,
                    "entity": 7,
                    "type": "Prism 6",
                    "nodes": [
                        *(chains[root][layer - 1] for root in roots),
                        *(chains[root][layer] for root in roots),
                    ],
                }
            )
        core_records = [
            {
                "tag": 20001,
                "entity": 8,
                "type": "Tetrahedron 4",
                "nodes": [
                    *(chains[root][-1] for root in roots),
                    fixed_core_node,
                ],
            }
        ]
        gmsh = _HomotopyGmsh(
            coordinates,
            [record["tag"] for record in prism_records],
            core_records[0]["tag"],
        )

        real_root_id = smoke._stable_root_id
        real_triangle_id = smoke._stable_triangle_id
        real_canonical_hash = smoke._canonical_hash
        root_id_by_coordinate = {
            tuple(origins[root]): selected_roots[index]
            for index, root in enumerate(roots)
        }

        def stable_root_id(coordinate):
            return root_id_by_coordinate.get(
                tuple(float(value) for value in coordinate),
                real_root_id(coordinate),
            )

        def stable_triangle_id(runtime_roots, runtime_coordinates):
            if set(int(value) for value in runtime_roots) == set(roots):
                return selected_triangle
            return real_triangle_id(runtime_roots, runtime_coordinates)

        triple_coordinate_sha256 = triple["final_state"][
            "selected_state_coordinate_sha256"
        ]

        def canonical_hash(value):
            if (
                isinstance(value, dict)
                and value.get("encoding")
                == "python-float-hex/root-sha/layer-v1"
                and isinstance(value.get("records"), list)
            ):
                return triple_coordinate_sha256
            return real_canonical_hash(value)

        def apply_state(schedules, selected_directions):
            for root in roots:
                origin = origins[root]
                direction = selected_directions[root]
                for layer, node in enumerate(chains[root][1:], start=1):
                    distance = float(schedules[root][layer - 1])
                    parameters = gmsh.model.mesh.getNode(node)[1]
                    gmsh.model.mesh.setNode(
                        node,
                        [
                            origin[axis] + float(direction[axis]) * distance
                            for axis in range(3)
                        ],
                        parameters,
                    )
            current = dict(gmsh.model.mesh.coordinates)
            return {
                "root_schedule_sha256": smoke._homotopy_root_schedule_sha256(
                    roots=roots,
                    root_coordinates=origins,
                    root_schedules=schedules,
                ),
                "direction_field_sha256": smoke._stable_direction_field_sha256(
                    roots=roots,
                    root_coordinates=origins,
                    directions=selected_directions,
                ),
                "coordinate_sha256": smoke._frontier_collar_coordinate_sha256(
                    current,
                    roots=roots,
                    root_coordinates=origins,
                    chains=chains,
                ),
            }

        candidate_quality = self._quality(status="FAIL", prism_bad=9)

        def low_lineage(*_args, **_kwargs):
            low_records = [
                {
                    "source_triangle_sha256": selected_triangle,
                    "wall_surface_fingerprint": wall,
                    "layer": layer,
                }
                for layer in range(52, 61)
            ]
            aggregate = {
                "count": 9,
                "unique_source_triangle_count": 1,
                "wall_count": 1,
                "layer_count": 9,
                "by_wall_and_layer": [],
                "by_wall": [],
                "by_layer": [],
                "records_sha256": "0" * 64,
            }
            aggregate["aggregate_sha256"] = real_canonical_hash(aggregate)
            return [(1, 2, 3)], low_records, aggregate

        collar_endpoint = (
            make_owner_free_direction_frontier_collar_refinement_endpoint(
                schedule_endpoint_sha256=endpoint["endpoint_sha256"]
            )
        )
        with patch.object(smoke, "_stable_root_id", side_effect=stable_root_id), patch.object(
            smoke, "_stable_triangle_id", side_effect=stable_triangle_id
        ), patch.object(smoke, "_canonical_hash", side_effect=canonical_hash), patch.object(
            smoke,
            "_owner_free_quality_snapshot",
            side_effect=lambda *_args, **_kwargs: copy.deepcopy(candidate_quality),
        ), patch.object(
            smoke,
            "_stable_low_quality_prism_lineage",
            side_effect=low_lineage,
        ):
            actual_selection, _schedules, _application, final_quality = (
                smoke._run_frontier_schedule_collar(
                    gmsh,
                    endpoint=collar_endpoint,
                    search=search,
                    search_quality=triple["final_state"][
                        "selected_state_quality"
                    ],
                    roots=roots,
                    root_coordinates=origins,
                    chains=chains,
                    target_schedules=target_schedules,
                    selected_directions=directions,
                    source_triangles=[(1, 2, 3)],
                    source_triangle_wall_fingerprints={(1, 2, 3): wall},
                    chain_origin=chain_origin,
                    subdivided_prisms=prism_records,
                    core_volume_records=core_records,
                    prism_volume_fingerprints={7: wall},
                    prism_element_tags=[
                        record["tag"] for record in prism_records
                    ],
                    core_element_tags=[20001],
                    core_tetra_element_tags=[20001],
                    minimum_prism_scaled_jacobian=0.01,
                    minimum_core_tetra_gamma=0.001,
                    apply_state=apply_state,
                    non_target_nodes=[fixed_core_node],
                )
            )

        self.assertEqual(actual_selection["status"], "EXHAUSTED")
        self.assertEqual(len(actual_selection["candidate_records"]), 7)
        evidence["pattern_search"] = search
        evidence["collar_refinement_selection"] = actual_selection
        evidence["frontier_input"]["target_state"]["schedule_sha256"] = (
            actual_selection["handoff"]["entry_schedule_sha256"]
        )
        selected_state = actual_selection["final_state"]
        replay = evidence["cross_schedule_replay"]
        replay.update(
            {
                "state_b_first_coordinate_sha256": selected_state[
                    "coordinate_sha256"
                ],
                "state_b_second_coordinate_sha256": selected_state[
                    "coordinate_sha256"
                ],
                "state_b_first_quality_sha256": selected_state[
                    "quality_sha256"
                ],
                "state_b_second_quality_sha256": selected_state[
                    "quality_sha256"
                ],
                "non_target_coordinate_sha256": actual_selection[
                    "preservation"
                ]["final_non_target_coordinate_sha256"],
                "selected_schedule_sha256": selected_state["schedule_sha256"],
                "selected_coordinate_sha256": selected_state[
                    "coordinate_sha256"
                ],
                "selected_quality_sha256": selected_state["quality_sha256"],
            }
        )
        evidence["final_quality"] = final_quality
        counts = evidence["observed_counts"]
        counts["collar_quality_evaluation_count"] = 7
        counts["quality_evaluation_count"] = (
            counts["pattern_quality_evaluation_count"] + 7
        )
        counts["final_prism_below_threshold_count"] = 9
        evidence.pop("discovery_sha256")
        evidence["discovery_sha256"] = _canonical_sha256(evidence)

        validated = validate_schedule_frontier_direction_continuation(
            evidence,
            expected_endpoint=endpoint,
            expected_projected_3d_elements=294059,
            expected_prism_element_count=280320,
            expected_core_element_count=13739,
        )
        self.assertEqual(validated, evidence)

    def test_discovery_requires_frontier_child_and_actual_budget_bound(self):
        endpoint, evidence = self._evidence()
        for stale_endpoint in (
            make_owner_free_direction_pattern_endpoint(
                schedule_endpoint_sha256=endpoint["endpoint_sha256"]
            ),
            make_owner_free_direction_frontier_pattern_endpoint(
                schedule_endpoint_sha256=endpoint["endpoint_sha256"]
            ),
            make_owner_free_direction_frontier_fine_refinement_pattern_endpoint(
                schedule_endpoint_sha256=endpoint["endpoint_sha256"]
            ),
        ):
            with self.subTest(schema=stale_endpoint["schema"]):
                stale_child = copy.deepcopy(evidence)
                stale_child["pattern_search"]["endpoint_sha256"] = (
                    stale_endpoint["endpoint_sha256"]
                )
                stale_child["discovery_sha256"] = _canonical_sha256(
                    {
                        key: value
                        for key, value in stale_child.items()
                        if key != "discovery_sha256"
                    }
                )
                with self.assertRaises(CoarseScheduleContinuationError):
                    validate_schedule_frontier_direction_continuation(
                        stale_child,
                        expected_endpoint=endpoint,
                        expected_projected_3d_elements=294059,
                        expected_prism_element_count=280320,
                        expected_core_element_count=13739,
                    )

        over_actual_bound = copy.deepcopy(evidence)
        over_actual_bound["pattern_search"]["quality_evaluation_count"] = 3724
        over_actual_bound["observed_counts"]["quality_evaluation_count"] = 3724
        over_actual_bound["discovery_sha256"] = _canonical_sha256(
            {
                key: value
                for key, value in over_actual_bound.items()
                if key != "discovery_sha256"
            }
        )
        with self.assertRaises(CoarseScheduleContinuationError):
            validate_schedule_frontier_direction_continuation(
                over_actual_bound,
                expected_endpoint=endpoint,
                expected_projected_3d_elements=294059,
                expected_prism_element_count=280320,
                expected_core_element_count=13739,
            )

    def test_discovery_accepts_static_all_pass_without_fine_refinement(self):
        endpoint, evidence = self._evidence()
        failing_record = next(
            record
            for record in evidence["pattern_search"]["component_records"]
            if record["static_exit_status"] == "FAIL"
        )
        local_pass = self._quality(
            status="PASS",
            prism_bad=0,
            prism_count=failing_record["static_exit_quality"][
                "prism_element_count"
            ],
            core_count=failing_record["static_exit_quality"][
                "core_element_count"
            ],
            minimum_prism=0.012,
        )
        local_objective, local_objective_sha256 = self._objective(
            local_pass, direction_penalty=0.03
        )
        failing_record.update(
            {
                "quality_evaluation_count": 240,
                "search_exit_quality_sha256": local_pass["quality_sha256"],
                "selected_objective": local_objective,
                "static_exit_quality": local_pass,
                "static_exit_quality_sha256": local_pass["quality_sha256"],
                "static_exit_objective": local_objective,
                "static_exit_objective_sha256": local_objective_sha256,
                "static_exit_status": "PASS",
            }
        )
        failing_record.pop("fine_exit_checkpoint")
        selection = evidence["fine_refinement_selection"]
        selected_id = selection["selected_component"][
            "interaction_component_sha256"
        ]
        static_copy = next(
            record
            for record in selection["component_static_evidence"]
            if record["interaction_component_sha256"] == selected_id
        )
        static_copy.update(
            {
                "static_exit_status": "PASS",
                "static_exit_quality_sha256": local_pass["quality_sha256"],
                "static_exit_objective_sha256": local_objective_sha256,
            }
        )
        selection["gate"].update(
            {
                "static_failing_component_count": 0,
                "fine_refinement_performed": False,
            }
        )
        selection["order"].update(
            {
                "fine_component_sha256_order": [],
                "first_fine_quality_evaluation_index": None,
                "tail_start_coordinate_sha256": None,
                "tail_start_direction_state_sha256": None,
                "tail_start_direction_field_sha256": None,
                "tail_start_quality_sha256": None,
                "tail_started_from_prefix_checkpoint": False,
                "first_tail_quality_evaluation_index": None,
            }
        )
        selection["evaluation_evidence"].update(
            {
                "fine_quality_evaluation_count": 0,
                "total_quality_evaluation_count": 690,
            }
        )
        selection["selected_component"] = None
        selection["prefix_checkpoint"] = None
        evidence["pattern_search"]["fine_refinement_selection"] = copy.deepcopy(
            selection
        )
        evidence["pattern_search"].update(
            {"status": "PASS", "quality_evaluation_count": 699}
        )
        evidence["pattern_search"]["tangent_pattern_refinement"].update(
            {
                "performed": False,
                "quality_evaluation_count": 0,
                "refined_component_count": 0,
            }
        )
        triple_selection = evidence["triple_refinement_selection"]
        triple_selection["gate"].update(
            {
                "static_failing_component_count": 0,
                "fine_refinement_performed": False,
                "triple_refinement_performed": False,
            }
        )
        triple_selection.update(
            {
                "selected_component": None,
                "handoff": None,
                "step_pass_records": [],
                "final_state": None,
            }
        )
        triple_selection["evaluation_evidence"].update(
            {
                "fine_quality_evaluation_count": 0,
                "through_fine_handoff_quality_evaluation_count": 690,
                "triple_quality_evaluation_count": 0,
                "triple_quality_evaluation_start_index_exclusive": 690,
                "first_triple_quality_evaluation_index": None,
                "last_triple_quality_evaluation_index": None,
                "triple_quality_evaluation_end_index_inclusive": 690,
                "total_quality_evaluation_count": 699,
            }
        )
        evidence["pattern_search"]["triple_refinement_selection"] = (
            copy.deepcopy(triple_selection)
        )
        evidence["pattern_search"]["triple_pattern_refinement"].update(
            {
                "performed": False,
                "quality_evaluation_count": 0,
                "accepted_move_count": 0,
                "rejected_margin_candidate_count": 0,
                "refined_component_count": 0,
                "step_pass_records": [],
            }
        )
        skipped_gate = copy.deepcopy(
            evidence["collar_refinement_selection"]["gate"]
        )
        skipped_gate.update(
            {
                "eligible": False,
                "reason": "TRIPLE_ALREADY_PASS",
                "triple_status": "PASS",
                "triple_accepted_move_count": 0,
                "residual_component_count": 0,
                "residual_source_triangle_count": 0,
                "residual_root_count": 0,
                "residual_wall_count": 0,
                "residual_prism_count": 0,
                "residual_bad_records": [],
                "residual_bad_records_sha256": _canonical_sha256([]),
                "minimum_bad_layer_1_based": None,
                "maximum_bad_layer_1_based": None,
                "outer_tail_contiguous": False,
                "frozen_prefix_last_layer_1_based": None,
                "core_tetra_below_gamma_count": 0,
                "nonpositive_element_count": 0,
                "nonfinite_count": 0,
                "target_wall_surface_fingerprint": None,
                "target_source_triangle_sha256": None,
                "seed_root_coordinate_sha256": None,
            }
        )
        evidence["collar_refinement_selection"] = {
            "schema": (
                "cfdpipe.coarse_schedule_frontier_collar_refinement_selection.v1"
            ),
            "status": "SKIPPED",
            "gate": skipped_gate,
            "handoff": None,
            "schedule": None,
            "graph": None,
            "ring_widths": [1, 2, 4, 8, 16, 32, 64],
            "candidate_records": [],
            "evaluation_evidence": {
                "pattern_quality_evaluation_count": 699,
                "collar_quality_evaluation_start_index_exclusive": 699,
                "collar_quality_evaluation_count": 0,
                "collar_quality_evaluation_end_index_inclusive": 699,
                "maximum_collar_quality_evaluations": 7,
                "total_quality_evaluation_count": 699,
                "maximum_total_quality_evaluations": 4096,
                "within_cap": True,
            },
            "selection": None,
            "final_state": None,
            "preservation": None,
        }
        final_pass = self._quality(status="PASS", prism_bad=0)
        evidence.update(
            {
                "status": "PASS",
                "profile_complete": True,
                "final_quality": final_pass,
                "incomplete_reason": None,
            }
        )
        evidence["cross_schedule_replay"].update(
            {
                "state_b_first_quality_sha256": final_pass["quality_sha256"],
                "state_b_second_quality_sha256": final_pass["quality_sha256"],
                "selected_quality_sha256": final_pass["quality_sha256"],
                "both_states_pass": True,
            }
        )
        evidence["observed_counts"].update(
            {
                "pattern_quality_evaluation_count": 699,
                "collar_quality_evaluation_count": 0,
                "quality_evaluation_count": 699,
                "final_prism_below_threshold_count": 0,
            }
        )
        evidence["discovery_sha256"] = _canonical_sha256(
            {
                key: value
                for key, value in evidence.items()
                if key != "discovery_sha256"
            }
        )
        self.assertEqual(
            validate_schedule_frontier_direction_continuation(
                evidence,
                expected_endpoint=endpoint,
                expected_projected_3d_elements=294059,
                expected_prism_element_count=280320,
                expected_core_element_count=13739,
            ),
            evidence,
        )

    def test_discovery_rejects_static_gate_and_selected_component_tampering(self):
        endpoint, evidence = self._evidence()

        def selection_mutation(mutator):
            def mutate(item):
                mutator(item["fine_refinement_selection"])
                item["pattern_search"]["fine_refinement_selection"] = (
                    copy.deepcopy(item["fine_refinement_selection"])
                )

            return mutate

        mutations = (
            lambda item: item["pattern_search"][
                "fine_refinement_selection"
            ]["gate"].update(
                {"static_all_components_complete_before_fine": False}
            ),
            selection_mutation(
                lambda selection: selection["gate"].update(
                    {"static_search_started_from_fresh_a": False}
                )
            ),
            selection_mutation(
                lambda selection: selection["selected_component"].update(
                    {"unique_source_triangle_edge_count": 2}
                )
            ),
            selection_mutation(
                lambda selection: selection["gate"].update(
                    {"maximum_refined_component_count": 2}
                )
            ),
            selection_mutation(
                lambda selection: selection["order"].update(
                    {
                        "first_fine_quality_evaluation_index": selection[
                            "order"
                        ]["last_static_quality_evaluation_index"]
                    }
                )
            ),
            lambda item: item["pattern_search"][
                "tangent_pattern_refinement"
            ].update({"coordinate_passes_per_step": 3}),
            lambda item: item["pattern_search"]["component_records"][0].update(
                {"static_exit_coordinate_sha256": "f" * 64}
            ),
            lambda item: item["pattern_search"]["component_records"][0].update(
                {
                    "source_triangle_count": item["pattern_search"][
                        "component_records"
                    ][0]["source_triangle_count"]
                    + 1
                }
            ),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                tampered = copy.deepcopy(evidence)
                mutate(tampered)
                tampered["discovery_sha256"] = _canonical_sha256(
                    {
                        key: value
                        for key, value in tampered.items()
                        if key != "discovery_sha256"
                    }
                )
                with self.assertRaises(CoarseScheduleContinuationError):
                    validate_schedule_frontier_direction_continuation(
                        tampered,
                        expected_endpoint=endpoint,
                        expected_projected_3d_elements=294059,
                        expected_prism_element_count=280320,
                        expected_core_element_count=13739,
                    )

    def test_discovery_rejects_prefix_and_tail_discontinuity(self):
        endpoint, evidence = self._evidence()

        def mutate_both(item, mutator):
            mutator(item["fine_refinement_selection"])
            item["pattern_search"]["fine_refinement_selection"] = copy.deepcopy(
                item["fine_refinement_selection"]
            )

        mutations = (
            lambda item: mutate_both(
                item,
                lambda selection: selection["prefix_checkpoint"].update(
                    {"prefix_steps_radians": [0.5, 0.25]}
                ),
            ),
            lambda item: mutate_both(
                item,
                lambda selection: selection["order"].update(
                    {"tail_steps_radians": [2.0**-6]}
                ),
            ),
            lambda item: mutate_both(
                item,
                lambda selection: selection["order"].update(
                    {"tail_start_coordinate_sha256": "f" * 64}
                ),
            ),
            lambda item: mutate_both(
                item,
                lambda selection: selection["order"].update(
                    {"tail_start_direction_field_sha256": "f" * 64}
                ),
            ),
            lambda item: mutate_both(
                item,
                lambda selection: selection["order"].update(
                    {"tail_start_quality_sha256": "f" * 64}
                ),
            ),
            lambda item: mutate_both(
                item,
                lambda selection: selection["order"].update(
                    {
                        "first_tail_quality_evaluation_index": selection[
                            "prefix_checkpoint"
                        ]["quality_evaluation_index"]
                    }
                ),
            ),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                tampered = copy.deepcopy(evidence)
                mutate(tampered)
                tampered["discovery_sha256"] = _canonical_sha256(
                    {
                        key: value
                        for key, value in tampered.items()
                        if key != "discovery_sha256"
                    }
                )
                with self.assertRaises(CoarseScheduleContinuationError):
                    validate_schedule_frontier_direction_continuation(
                        tampered,
                        expected_endpoint=endpoint,
                        expected_projected_3d_elements=294059,
                        expected_prism_element_count=280320,
                        expected_core_element_count=13739,
                    )

    def test_discovery_rejects_triple_handoff_checkpoint_and_record_tampering(self):
        endpoint, evidence = self._evidence()

        def mutate_triple(item, mutator, *, copy_records=False):
            selection = item["triple_refinement_selection"]
            mutator(selection)
            item["pattern_search"]["triple_refinement_selection"] = (
                copy.deepcopy(selection)
            )
            if copy_records:
                item["pattern_search"]["triple_pattern_refinement"][
                    "step_pass_records"
                ] = copy.deepcopy(selection["step_pass_records"])

        mutations = (
            lambda item: mutate_triple(
                item,
                lambda selection: selection["handoff"].pop(
                    "fine_exit_quality"
                ),
            ),
            lambda item: mutate_triple(
                item,
                lambda selection: selection["handoff"][
                    "fine_exit_quality"
                ].update({"minimum_prism_scaled_jacobian": 0.009}),
            ),
            lambda item: next(
                record
                for record in item["pattern_search"]["component_records"]
                if "fine_exit_checkpoint" in record
            )["fine_exit_checkpoint"].update(
                {"selected_state_coordinate_sha256": "f" * 64}
            ),
            lambda item: mutate_triple(
                item,
                lambda selection: selection["step_pass_records"][0][
                    "root_candidate_counts"
                ][0].update({"candidate_count": 5}),
                copy_records=True,
            ),
            lambda item: mutate_triple(
                item,
                lambda selection: selection["step_pass_records"][0].update(
                    {"candidate_upper_bound": 63}
                ),
                copy_records=True,
            ),
            lambda item: mutate_triple(
                item,
                lambda selection: selection["step_pass_records"][1].update(
                    {
                        "quality_evaluation_start_index_exclusive": (
                            selection["step_pass_records"][1][
                                "quality_evaluation_start_index_exclusive"
                            ]
                            + 1
                        )
                    }
                ),
                copy_records=True,
            ),
            lambda item: mutate_triple(
                item,
                lambda selection: selection["final_state"][
                    "selected_state_objective"
                ].__setitem__(5, 8.0),
            ),
            lambda item: next(
                record
                for record in item["pattern_search"]["component_records"]
                if "fine_exit_checkpoint" in record
            )["fine_exit_checkpoint"].update({"extra": True}),
            lambda item: next(
                record
                for record in item["pattern_search"]["component_records"]
                if "fine_exit_checkpoint" in record
            )["fine_exit_checkpoint"]["selected_directions"][0].update(
                {
                    "direction_float_hex": [
                        (1.0).hex(),
                        (0.0).hex(),
                        (0.0).hex(),
                    ]
                }
            ),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                tampered = copy.deepcopy(evidence)
                mutate(tampered)
                tampered["discovery_sha256"] = _canonical_sha256(
                    {
                        key: value
                        for key, value in tampered.items()
                        if key != "discovery_sha256"
                    }
                )
                with self.assertRaises(CoarseScheduleContinuationError):
                    validate_schedule_frontier_direction_continuation(
                        tampered,
                        expected_endpoint=endpoint,
                        expected_projected_3d_elements=294059,
                        expected_prism_element_count=280320,
                        expected_core_element_count=13739,
                    )

    def test_discovery_rejects_nonobserved_component_or_root_scope(self):
        endpoint, evidence = self._evidence()
        for field, stale_value in (
            ("component_count", 2),
            ("candidate_root_count", 11),
        ):
            with self.subTest(field=field):
                stale_scope = copy.deepcopy(evidence)
                stale_scope["components"][field] = stale_value
                stale_scope["pattern_search"][field] = stale_value
                stale_scope["observed_counts"][field] = stale_value
                stale_scope["discovery_sha256"] = _canonical_sha256(
                    {
                        key: value
                        for key, value in stale_scope.items()
                        if key != "discovery_sha256"
                    }
                )
                with self.assertRaises(CoarseScheduleContinuationError):
                    validate_schedule_frontier_direction_continuation(
                        stale_scope,
                        expected_endpoint=endpoint,
                        expected_projected_3d_elements=294059,
                        expected_prism_element_count=280320,
                        expected_core_element_count=13739,
                    )

    def test_discovery_compatibility_validator_rejects_lineage_and_cap_tampering(self):
        endpoint, evidence = self._evidence()

        def mismatch_changed_root_identity(item):
            selected_root = item["components"]["records"][0][
                "root_coordinate_sha256"
            ][0]
            pattern_root = item["components"]["records"][0][
                "root_coordinate_sha256"
            ][1]
            item["selected_direction_field"]["changed_root_count"] = 1
            item["selected_direction_field"]["changed_roots"] = [
                {
                    "root_coordinate_sha256": selected_root,
                    "incoming_direction_float_hex": [
                        "0x1.0000000000000p+0",
                        "0x0.0p+0",
                        "0x0.0p+0",
                    ],
                    "selected_direction_float_hex": [
                        "0x0.0p+0",
                        "0x1.0000000000000p+0",
                        "0x0.0p+0",
                    ],
                }
            ]
            item["pattern_search"]["changed_roots"] = [
                {"root_coordinate_sha256": pattern_root}
            ]
            item["observed_counts"]["changed_root_count"] = 1

        for mutate in (
            lambda item: item["source_bindings"].update(
                {"homotopy_manifest_sha256": "f" * 64}
            ),
            lambda item: item["observed_counts"].update(
                {"candidate_root_count": 17}
            ),
            lambda item: item["cross_schedule_replay"].update(
                {"state_b_second_coordinate_sha256": "d" * 64}
            ),
            lambda item: item["cross_schedule_replay"].update(
                {
                    "sequence_fraction_float_hex": list(
                        reversed(
                            item["cross_schedule_replay"][
                                "sequence_fraction_float_hex"
                            ]
                        )
                    )
                }
            ),
            lambda item: item["cross_schedule_replay"].update(
                {"state_a_reproducible": False}
            ),
            lambda item: item["cross_schedule_replay"].update(
                {"both_states_pass": True}
            ),
            lambda item: item["components"]["records"].pop(),
            lambda item: item["pattern_search"].update(
                {"endpoint_sha256": "e" * 64}
            ),
            lambda item: item["pattern_search"].update(
                {"component_count": 1}
            ),
            lambda item: item["pattern_search"].update(
                {"candidate_root_count": 9}
            ),
            lambda item: item["pattern_search"].update(
                {"quality_evaluation_count": 4095}
            ),
            lambda item: item["pattern_search"]["changed_roots"].append(
                {"root_coordinate_sha256": "a" * 64}
            ),
            mismatch_changed_root_identity,
            lambda item: item.update({"mesh_written": True}),
        ):
            with self.subTest(mutate=mutate):
                tampered = copy.deepcopy(evidence)
                mutate(tampered)
                tampered["discovery_sha256"] = _canonical_sha256(
                    {
                        key: value
                        for key, value in tampered.items()
                        if key != "discovery_sha256"
                    }
                )
                with self.assertRaises(CoarseScheduleContinuationError):
                    validate_schedule_frontier_direction_continuation(
                        tampered,
                        expected_endpoint=endpoint,
                        expected_projected_3d_elements=294059,
                        expected_prism_element_count=280320,
                        expected_core_element_count=13739,
                    )


if __name__ == "__main__":
    unittest.main()
