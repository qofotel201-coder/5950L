from __future__ import annotations

import copy
import hashlib
import json
import unittest

from cfdpipe.coarse_direction_feasibility import (
    COLLAR_REFINEMENT_ENDPOINT_MODE,
    COLLAR_REFINEMENT_ENDPOINT_SCHEMA,
    CoarseDirectionFeasibilityError,
    FINE_REFINEMENT_PATTERN_ENDPOINT_MODE,
    FINE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
    FRONTIER_PATTERN_ENDPOINT_MODE,
    FRONTIER_PATTERN_ENDPOINT_SCHEMA,
    PATTERN_ENDPOINT_MODE,
    PATTERN_ENDPOINT_SCHEMA,
    TRIPLE_REFINEMENT_PATTERN_ENDPOINT_MODE,
    TRIPLE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
    make_owner_free_direction_endpoint,
    make_owner_free_direction_frontier_collar_refinement_endpoint,
    make_owner_free_direction_frontier_fine_refinement_pattern_endpoint,
    make_owner_free_direction_frontier_pattern_endpoint,
    make_owner_free_direction_frontier_triple_refinement_pattern_endpoint,
    make_owner_free_direction_pattern_endpoint,
    validate_owner_free_direction_endpoint,
)


def _self_hash(endpoint: dict[str, object]) -> str:
    unsigned = copy.deepcopy(endpoint)
    unsigned.pop("endpoint_sha256")
    payload = json.dumps(
        unsigned,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


class CoarseDirectionFeasibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schedule_hash = "a" * 64
        self.endpoint = make_owner_free_direction_endpoint(
            schedule_endpoint_sha256=self.schedule_hash
        )

    def test_endpoint_is_owner_free_bounded_and_audit_only(self) -> None:
        validated = validate_owner_free_direction_endpoint(
            self.endpoint,
            expected_schedule_endpoint_sha256=self.schedule_hash,
        )
        self.assertEqual(self.endpoint, validated)
        self.assertFalse(validated["sharp_owner_used"])
        self.assertFalse(validated["monotonicity_assumed"])
        self.assertTrue(validated["absolute_coordinate_replay_required"])
        self.assertTrue(validated["aba_replay_required"])
        self.assertLessEqual(validated["maximum_quality_evaluations"], 4096)
        for field in (
            "calibration_PASS_authorized",
            "production_mesh_eligible",
            "mesh_written",
            "su2_called",
            "paraview_called",
        ):
            self.assertFalse(validated[field])

    def test_tamper_or_stale_schedule_hash_fails_closed(self) -> None:
        for field, replacement in (
            ("sharp_owner_used", True),
            ("monotonicity_assumed", True),
            ("maximum_quality_evaluations", 4097),
            ("original_direction_interior_weights", [0.0]),
        ):
            with self.subTest(field=field):
                tampered = copy.deepcopy(self.endpoint)
                tampered[field] = replacement
                with self.assertRaises(CoarseDirectionFeasibilityError):
                    validate_owner_free_direction_endpoint(
                        tampered,
                        expected_schedule_endpoint_sha256=self.schedule_hash,
                    )
        with self.assertRaises(CoarseDirectionFeasibilityError):
            validate_owner_free_direction_endpoint(
                self.endpoint,
                expected_schedule_endpoint_sha256="b" * 64,
            )

    def test_pattern_endpoint_requires_interaction_replay_and_strict_merit(self) -> None:
        endpoint = make_owner_free_direction_pattern_endpoint(
            schedule_endpoint_sha256=self.schedule_hash
        )
        self.assertEqual(endpoint["schema"], PATTERN_ENDPOINT_SCHEMA)
        self.assertEqual(endpoint["mode"], PATTERN_ENDPOINT_MODE)
        self.assertEqual(endpoint["maximum_component_count"], 16)
        self.assertEqual(endpoint["maximum_candidate_root_count"], 64)
        self.assertEqual(
            endpoint["endpoint_sha256"],
            "7fad138b1bbd9bbbb7800741efb68f729de4974d6eaf5bd03ac4d94788dba472",
        )
        self.assertEqual(
            endpoint,
            validate_owner_free_direction_endpoint(
                endpoint,
                expected_schedule_endpoint_sha256=self.schedule_hash,
            ),
        )
        self.assertTrue(endpoint["interaction_components_required"])
        self.assertTrue(endpoint["final_low_quality_lineage_required"])
        self.assertTrue(endpoint["final_component_recheck_required"])
        self.assertTrue(endpoint["coordinate_hash_replay_required"])
        self.assertTrue(endpoint["abab_replay_required"])
        self.assertFalse(endpoint["aba_replay_required"])
        self.assertLess(
            endpoint["quality_objective"].index(
                "core_tetra_below_minimum_gamma_count"
            ),
            endpoint["quality_objective"].index(
                "prism_below_minimum_scaled_jacobian_count"
            ),
        )
        tampered = copy.deepcopy(endpoint)
        tampered["minimum_changed_direction_margin_source"] = "hardcoded"
        with self.assertRaises(CoarseDirectionFeasibilityError):
            validate_owner_free_direction_endpoint(
                tampered,
                expected_schedule_endpoint_sha256=self.schedule_hash,
            )

    def test_frontier_pattern_endpoint_is_count_first_self_hashed_and_audit_only(
        self,
    ) -> None:
        endpoint = make_owner_free_direction_frontier_pattern_endpoint(
            schedule_endpoint_sha256=self.schedule_hash
        )
        self.assertEqual(endpoint["schema"], FRONTIER_PATTERN_ENDPOINT_SCHEMA)
        self.assertEqual(endpoint["mode"], FRONTIER_PATTERN_ENDPOINT_MODE)
        self.assertEqual(
            endpoint["source_pattern_endpoint_schema"], PATTERN_ENDPOINT_SCHEMA
        )
        self.assertEqual(
            endpoint["source_pattern_endpoint_sha256"],
            make_owner_free_direction_pattern_endpoint(
                schedule_endpoint_sha256=self.schedule_hash
            )["endpoint_sha256"],
        )
        self.assertEqual(endpoint["maximum_component_count"], 3)
        self.assertEqual(endpoint["maximum_candidate_root_count"], 16)
        self.assertEqual(endpoint["maximum_quality_evaluations"], 4096)
        self.assertEqual(endpoint["maximum_pair_candidates_per_edge_step"], 16)
        self.assertEqual(
            endpoint["endpoint_sha256"],
            "5ee51757b92a7bebf9eb4b5da3e9bba3b5b1e452b6c852cabc5e3e6211e8fc72",
        )
        self.assertEqual(endpoint["endpoint_sha256"], _self_hash(endpoint))
        self.assertEqual(
            endpoint["quality_objective"],
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
            endpoint,
            validate_owner_free_direction_endpoint(
                endpoint,
                expected_schedule_endpoint_sha256=self.schedule_hash,
            ),
        )
        for field in (
            "calibration_PASS_authorized",
            "production_mesh_eligible",
            "mesh_written",
            "su2_called",
            "paraview_called",
        ):
            self.assertFalse(endpoint[field])

    def test_fine_refinement_endpoint_inherits_frontier_and_freezes_scope(
        self,
    ) -> None:
        endpoint = (
            make_owner_free_direction_frontier_fine_refinement_pattern_endpoint(
                schedule_endpoint_sha256=self.schedule_hash
            )
        )
        frontier = make_owner_free_direction_frontier_pattern_endpoint(
            schedule_endpoint_sha256=self.schedule_hash
        )
        self.assertEqual(
            endpoint["schema"], FINE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA
        )
        self.assertEqual(endpoint["mode"], FINE_REFINEMENT_PATTERN_ENDPOINT_MODE)
        self.assertEqual(
            endpoint["source_frontier_pattern_endpoint_schema"],
            FRONTIER_PATTERN_ENDPOINT_SCHEMA,
        )
        self.assertEqual(
            endpoint["source_frontier_pattern_endpoint_sha256"],
            frontier["endpoint_sha256"],
        )
        self.assertEqual(
            endpoint["quality_objective"], frontier["quality_objective"]
        )
        self.assertEqual(
            endpoint["tangent_pattern_steps_radians"],
            [2.0**-exponent for exponent in range(1, 13)],
        )
        self.assertEqual(endpoint["maximum_component_count"], 3)
        self.assertEqual(endpoint["maximum_candidate_root_count"], 16)
        self.assertEqual(endpoint["maximum_quality_evaluations"], 4096)
        self.assertEqual(endpoint["maximum_pair_candidates_per_edge_step"], 16)
        self.assertEqual(endpoint["maximum_refined_component_count"], 1)
        self.assertEqual(endpoint["refined_component_root_count"], 3)
        self.assertEqual(endpoint["refined_component_source_triangle_count"], 1)
        self.assertEqual(endpoint["endpoint_sha256"], _self_hash(endpoint))
        self.assertEqual(endpoint["status"], "AUDIT_ONLY")
        self.assertEqual(
            endpoint,
            validate_owner_free_direction_endpoint(
                endpoint,
                expected_schedule_endpoint_sha256=self.schedule_hash,
            ),
        )
        for field in (
            "calibration_PASS_authorized",
            "production_mesh_eligible",
            "mesh_written",
            "su2_called",
            "paraview_called",
        ):
            self.assertFalse(endpoint[field])

    def test_fine_refinement_tamper_and_unknown_schema_fail_closed(self) -> None:
        endpoint = (
            make_owner_free_direction_frontier_fine_refinement_pattern_endpoint(
                schedule_endpoint_sha256=self.schedule_hash
            )
        )
        for field, replacement in (
            ("maximum_refined_component_count", 2),
            ("maximum_refined_component_count", True),
            ("refined_component_root_count", 4),
            ("refined_component_source_triangle_count", 2),
            ("refined_component_source_triangle_count", True),
            ("tangent_pattern_steps_radians", [0.25, 0.5]),
        ):
            with self.subTest(field=field):
                tampered = copy.deepcopy(endpoint)
                tampered[field] = replacement
                tampered["endpoint_sha256"] = _self_hash(tampered)
                with self.assertRaises(CoarseDirectionFeasibilityError):
                    validate_owner_free_direction_endpoint(
                        tampered,
                        expected_schedule_endpoint_sha256=self.schedule_hash,
                    )

        unknown = copy.deepcopy(endpoint)
        unknown["schema"] = (
            "cfdpipe.coarse_direction_frontier_fine_refinement_pattern_endpoint.v2"
        )
        unknown["endpoint_sha256"] = _self_hash(unknown)
        with self.assertRaises(CoarseDirectionFeasibilityError):
            validate_owner_free_direction_endpoint(
                unknown,
                expected_schedule_endpoint_sha256=self.schedule_hash,
            )

    def test_triple_refinement_endpoint_inherits_fine_and_freezes_atomic_scope(
        self,
    ) -> None:
        endpoint = (
            make_owner_free_direction_frontier_triple_refinement_pattern_endpoint(
                schedule_endpoint_sha256=self.schedule_hash
            )
        )
        fine = (
            make_owner_free_direction_frontier_fine_refinement_pattern_endpoint(
                schedule_endpoint_sha256=self.schedule_hash
            )
        )
        self.assertEqual(
            endpoint["schema"], TRIPLE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA
        )
        self.assertEqual(
            endpoint["mode"], TRIPLE_REFINEMENT_PATTERN_ENDPOINT_MODE
        )
        self.assertEqual(
            endpoint["source_fine_refinement_pattern_endpoint_schema"],
            FINE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
        )
        self.assertEqual(
            endpoint["source_fine_refinement_pattern_endpoint_sha256"],
            fine["endpoint_sha256"],
        )
        self.assertEqual(endpoint["quality_objective"], fine["quality_objective"])
        self.assertEqual(
            endpoint["tangent_pattern_steps_radians"],
            [2.0**-exponent for exponent in range(1, 13)],
        )
        self.assertEqual(endpoint["maximum_refined_component_count"], 1)
        self.assertEqual(endpoint["refined_component_root_count"], 3)
        self.assertEqual(endpoint["refined_component_source_triangle_count"], 1)
        self.assertTrue(
            endpoint[
                "static_component_searches_complete_before_fine_refinement"
            ]
        )
        self.assertTrue(
            endpoint[
                "single_and_pair_phases_complete_before_triple_refinement"
            ]
        )
        self.assertTrue(endpoint["triple_refinement_enabled"])
        self.assertEqual(endpoint["triple_refinement_root_count"], 3)
        self.assertEqual(endpoint["tangent_candidates_per_root_per_step"], 4)
        self.assertEqual(
            endpoint["atomic_triple_cartesian_candidates_per_step_pass"],
            4**3,
        )
        self.assertEqual(endpoint["triple_coordinate_passes_per_step"], 2)
        self.assertEqual(endpoint["endpoint_sha256"], _self_hash(endpoint))
        self.assertEqual(
            endpoint,
            validate_owner_free_direction_endpoint(
                endpoint,
                expected_schedule_endpoint_sha256=self.schedule_hash,
            ),
        )
        for field in (
            "calibration_PASS_authorized",
            "production_mesh_eligible",
            "mesh_written",
            "su2_called",
            "paraview_called",
        ):
            self.assertFalse(endpoint[field])

    def test_triple_refinement_tamper_and_unknown_schema_fail_closed(self) -> None:
        endpoint = (
            make_owner_free_direction_frontier_triple_refinement_pattern_endpoint(
                schedule_endpoint_sha256=self.schedule_hash
            )
        )
        for field, replacement in (
            ("source_fine_refinement_pattern_endpoint_sha256", "b" * 64),
            (
                "static_component_searches_complete_before_fine_refinement",
                False,
            ),
            (
                "single_and_pair_phases_complete_before_triple_refinement",
                False,
            ),
            ("triple_refinement_enabled", False),
            ("triple_refinement_root_count", 2),
            ("triple_refinement_root_count", True),
            ("tangent_candidates_per_root_per_step", 3),
            ("tangent_candidates_per_root_per_step", True),
            ("atomic_triple_cartesian_candidates_per_step_pass", 63),
            ("atomic_triple_cartesian_candidates_per_step_pass", True),
            ("triple_coordinate_passes_per_step", 1),
            ("triple_coordinate_passes_per_step", True),
            ("maximum_refined_component_count", True),
            ("refined_component_root_count", 4),
            ("refined_component_root_count", True),
            ("refined_component_source_triangle_count", 2),
            ("refined_component_source_triangle_count", True),
            ("tangent_pattern_steps_radians", [2.0**-1, 2.0**-3]),
        ):
            with self.subTest(field=field):
                tampered = copy.deepcopy(endpoint)
                tampered[field] = replacement
                tampered["endpoint_sha256"] = _self_hash(tampered)
                with self.assertRaises(CoarseDirectionFeasibilityError):
                    validate_owner_free_direction_endpoint(
                        tampered,
                        expected_schedule_endpoint_sha256=self.schedule_hash,
                    )

        unknown = copy.deepcopy(endpoint)
        unknown["schema"] = (
            "cfdpipe.coarse_direction_frontier_triple_refinement_pattern_endpoint.v2"
        )
        unknown["endpoint_sha256"] = _self_hash(unknown)
        with self.assertRaises(CoarseDirectionFeasibilityError):
            validate_owner_free_direction_endpoint(
                unknown,
                expected_schedule_endpoint_sha256=self.schedule_hash,
            )

    def test_collar_endpoint_inherits_triple_and_freezes_outer_tail(self) -> None:
        endpoint = make_owner_free_direction_frontier_collar_refinement_endpoint(
            schedule_endpoint_sha256=self.schedule_hash
        )
        triple = (
            make_owner_free_direction_frontier_triple_refinement_pattern_endpoint(
                schedule_endpoint_sha256=self.schedule_hash
            )
        )
        self.assertEqual(endpoint["schema"], COLLAR_REFINEMENT_ENDPOINT_SCHEMA)
        self.assertEqual(endpoint["mode"], COLLAR_REFINEMENT_ENDPOINT_MODE)
        self.assertEqual(
            endpoint["source_triple_refinement_pattern_endpoint_schema"],
            TRIPLE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
        )
        self.assertEqual(
            endpoint["source_triple_refinement_pattern_endpoint_sha256"],
            triple["endpoint_sha256"],
        )
        self.assertTrue(endpoint["triple_refinement_exhausted_before_collar"])
        self.assertEqual(
            endpoint["required_triple_incomplete_reason"],
            "FIRST_FRONTIER_DIRECTION_SEARCH_EXHAUSTED",
        )
        self.assertEqual(endpoint["maximum_collar_component_count"], 1)
        self.assertEqual(endpoint["collar_component_source_triangle_count"], 1)
        self.assertEqual(endpoint["collar_component_root_count"], 3)
        self.assertTrue(endpoint["continuous_outer_tail_required"])
        self.assertGreaterEqual(endpoint["minimum_frozen_inner_layer_count"], 15)
        self.assertEqual(endpoint["layer_count"], 60)
        self.assertEqual(endpoint["collar_ring_widths"], [1, 2, 4, 8, 16, 32, 64])
        self.assertEqual(endpoint["maximum_collar_candidates"], 7)
        self.assertEqual(endpoint["spatial_weight_formula"], "w=max(0,1-d/W)")
        self.assertEqual(endpoint["outer_tail_start_rule"], "K=min_bad_layer-1")
        self.assertEqual(endpoint["minimum_growth_cumulative_formula"], "A_i=i*h1")
        self.assertEqual(
            endpoint["outer_tail_cumulative_formula"],
            "C_i=B_K+(A_i-A_K)+(1-w)*((B_i-B_K)-(A_i-A_K))",
        )
        self.assertFalse(endpoint["schedule_amplification_allowed"])
        self.assertEqual(endpoint["schedule_bounds_required"], "A_i<=C_i<=B_i")
        self.assertTrue(endpoint["global_quality_evaluation_per_candidate"])
        self.assertFalse(endpoint["layer_reduction_allowed"])
        self.assertFalse(endpoint["runtime_mesh_tags_hardcoded"])
        self.assertEqual(endpoint["endpoint_sha256"], _self_hash(endpoint))
        self.assertEqual(
            endpoint,
            validate_owner_free_direction_endpoint(
                endpoint,
                expected_schedule_endpoint_sha256=self.schedule_hash,
            ),
        )
        for field in (
            "calibration_PASS_authorized",
            "production_mesh_eligible",
            "mesh_written",
            "su2_called",
            "paraview_called",
        ):
            self.assertFalse(endpoint[field])

    def test_collar_tamper_and_unknown_schema_fail_closed(self) -> None:
        endpoint = make_owner_free_direction_frontier_collar_refinement_endpoint(
            schedule_endpoint_sha256=self.schedule_hash
        )
        for field, replacement in (
            ("source_triple_refinement_pattern_endpoint_sha256", "b" * 64),
            ("triple_refinement_exhausted_before_collar", False),
            ("maximum_collar_component_count", True),
            ("collar_component_root_count", 4),
            ("collar_component_source_triangle_count", True),
            ("continuous_outer_tail_required", False),
            ("minimum_frozen_inner_layer_count", 14),
            ("layer_count", 59),
            ("preserve_first_layer_height", False),
            ("preserve_direction_field", False),
            ("preserve_connectivity", False),
            ("layer_reduction_allowed", True),
            ("stable_root_graph_wall_restricted", False),
            ("runtime_mesh_tags_hardcoded", True),
            ("collar_ring_widths", [True, 2, 4, 8, 16, 32, 64]),
            ("collar_ring_widths", [1, 2, 4, 8, 16, 32]),
            ("maximum_collar_candidates", 8),
            ("spatial_weight_formula", "w=max(0,1-d/(W+1))"),
            ("outer_tail_start_rule", "K=min_bad_layer"),
            ("minimum_growth_cumulative_formula", "A_i=(i-1)*h1"),
            (
                "outer_tail_cumulative_formula",
                "C_i=B_K+(1-w)*(B_i-B_K)",
            ),
            ("candidate_rebuilt_from_same_absolute_state", False),
            ("global_quality_evaluation_per_candidate", False),
            ("schedule_amplification_allowed", True),
            ("schedule_bounds_required", "C_i<=B_i"),
            ("strictly_increasing_schedule_required", False),
            ("affected_closure_hash_required", False),
            ("connectivity_hash_required", False),
            ("passing_candidate_objective", ["coordinate_change_penalty"]),
        ):
            with self.subTest(field=field, replacement=replacement):
                tampered = copy.deepcopy(endpoint)
                tampered[field] = replacement
                tampered["endpoint_sha256"] = _self_hash(tampered)
                with self.assertRaises(CoarseDirectionFeasibilityError):
                    validate_owner_free_direction_endpoint(
                        tampered,
                        expected_schedule_endpoint_sha256=self.schedule_hash,
                    )

        missing = copy.deepcopy(endpoint)
        missing.pop("stable_graph_hash_required")
        missing["endpoint_sha256"] = _self_hash(missing)
        with self.assertRaises(CoarseDirectionFeasibilityError):
            validate_owner_free_direction_endpoint(
                missing,
                expected_schedule_endpoint_sha256=self.schedule_hash,
            )

        unknown = copy.deepcopy(endpoint)
        unknown["schema"] = (
            "cfdpipe.coarse_direction_frontier_collar_refinement_endpoint.v2"
        )
        unknown["endpoint_sha256"] = _self_hash(unknown)
        with self.assertRaises(CoarseDirectionFeasibilityError):
            validate_owner_free_direction_endpoint(
                unknown,
                expected_schedule_endpoint_sha256=self.schedule_hash,
            )

    def test_triple_refinement_does_not_change_legacy_endpoint_hashes(self) -> None:
        factories_and_hashes = (
            (
                make_owner_free_direction_endpoint,
                "9f235f53aebed9ede949d8a10f16c9ef81bb6b2663780712ca8fbec649280480",
            ),
            (
                make_owner_free_direction_pattern_endpoint,
                "7fad138b1bbd9bbbb7800741efb68f729de4974d6eaf5bd03ac4d94788dba472",
            ),
            (
                make_owner_free_direction_frontier_pattern_endpoint,
                "5ee51757b92a7bebf9eb4b5da3e9bba3b5b1e452b6c852cabc5e3e6211e8fc72",
            ),
            (
                make_owner_free_direction_frontier_fine_refinement_pattern_endpoint,
                "45c4e75f9accaeee24464f051871905d794731c398ffe69d65fdf937942f2635",
            ),
            (
                make_owner_free_direction_frontier_triple_refinement_pattern_endpoint,
                "193dec3655524a28bcf46c527f2b15d5e7673aca231b9274d51cbeb7fce52793",
            ),
        )
        for factory, expected_hash in factories_and_hashes:
            with self.subTest(factory=factory.__name__):
                self.assertEqual(
                    factory(schedule_endpoint_sha256=self.schedule_hash)[
                        "endpoint_sha256"
                    ],
                    expected_hash,
                )

    def test_frontier_pattern_tamper_and_unknown_schema_fail_closed(self) -> None:
        endpoint = make_owner_free_direction_frontier_pattern_endpoint(
            schedule_endpoint_sha256=self.schedule_hash
        )
        tampered = copy.deepcopy(endpoint)
        objective = tampered["quality_objective"]
        objective[5], objective[6] = objective[6], objective[5]
        tampered["endpoint_sha256"] = _self_hash(tampered)
        with self.assertRaises(CoarseDirectionFeasibilityError):
            validate_owner_free_direction_endpoint(
                tampered,
                expected_schedule_endpoint_sha256=self.schedule_hash,
            )

        for factory in (
            make_owner_free_direction_pattern_endpoint,
            make_owner_free_direction_frontier_pattern_endpoint,
        ):
            with self.subTest(factory=factory.__name__):
                unsafe = factory(schedule_endpoint_sha256=self.schedule_hash)
                unsafe["tangent_pattern_steps_radians"] = [0.25, 0.5]
                unsafe["endpoint_sha256"] = _self_hash(unsafe)
                with self.assertRaises(CoarseDirectionFeasibilityError):
                    validate_owner_free_direction_endpoint(
                        unsafe,
                        expected_schedule_endpoint_sha256=self.schedule_hash,
                    )

        unknown = copy.deepcopy(endpoint)
        unknown["schema"] = "cfdpipe.coarse_direction_frontier_pattern_endpoint.v2"
        unknown["endpoint_sha256"] = _self_hash(unknown)
        with self.assertRaises(CoarseDirectionFeasibilityError):
            validate_owner_free_direction_endpoint(
                unknown,
                expected_schedule_endpoint_sha256=self.schedule_hash,
            )


if __name__ == "__main__":
    unittest.main()
