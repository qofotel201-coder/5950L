from __future__ import annotations

import copy
import unittest

import cfdpipe.coarse_direction_replay as replay


class CoarsePhysicalScheduleHomotopyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.binding = "a" * 64
        self.first = 1.0e-6
        self.layers = 60
        self.fingerprints = ["1" * 64, "2" * 64]
        self.baseline = {
            fingerprint: self._cumulative(1.04 + 0.01 * index)
            for index, fingerprint in enumerate(self.fingerprints)
        }
        self.endpoint = replay.make_physical_schedule_homotopy_endpoint(
            baseline_binding_sha256=self.binding,
            first_layer_height_m=self.first,
            layer_count=self.layers,
        )
        self.prisms = 10
        self.core = 5
        self.pass_quality = self._quality(pass_gate=True)
        self.fail_quality = self._quality(pass_gate=False)
        self.root_id = "3" * 64
        self.approval = {
            "schema": replay.APPROVAL_SCHEMA,
            "status": "PASS",
            "local_schedule_binding_sha256": self.binding,
            "source_audit_manifests": [{"sha256": "4" * 64}],
            "candidate_root_count": 1,
            "changed_root_count": 1,
            "changed_roots": [
                {"root_coordinate_sha256": self.root_id}
            ],
            "root_contexts": [
                {"root_coordinate_sha256": self.root_id}
            ],
            "source_minimum_growth_final_quality": self.pass_quality,
        }
        self.approval["approval_sha256"] = replay._canonical_sha256(
            self.approval
        )

    def _cumulative(self, growth: float) -> list[float]:
        running = 0.0
        result: list[float] = []
        for layer in range(self.layers):
            running += self.first * growth**layer
            result.append(running)
        return result

    def _quality(self, *, pass_gate: bool) -> dict[str, object]:
        prism_minimum = 0.02 if pass_gate else 0.005
        prism_below = 0 if pass_gate else 1
        prism_deficit = max(0.0, 0.01 - prism_minimum)
        value: dict[str, object] = {
            "status": "PASS" if pass_gate else "FAIL",
            "minimum_prism_scaled_jacobian_for_pass": 0.01,
            "minimum_core_tetra_gamma_for_pass": 0.001,
            "all_values_finite": True,
            "nonfinite_count": 0,
            "nonpositive_element_count": 0,
            "prism_below_threshold_element_count": prism_below,
            "core_tetra_below_gamma_count": 0,
            "minimum_prism_scaled_jacobian": prism_minimum,
            "minimum_core_tetra_gamma": 0.002,
            "prism_element_count": self.prisms,
            "core_element_count": self.core,
            "core_tetra_count": self.core,
            "maximum_prism_scaled_jacobian_deficit": prism_deficit,
            "prism_scaled_jacobian_deficit_l1": prism_deficit,
            "prism_scaled_jacobian_deficit_l2": prism_deficit**2,
            "maximum_core_tetra_gamma_deficit": 0.0,
            "core_tetra_gamma_deficit_l2": 0.0,
        }
        value["quality_sha256"] = replay._canonical_sha256(value)
        return value

    def _discovery(self, *, incomplete: bool = False) -> dict[str, object]:
        fractions = list(self.endpoint["fractions"])
        records: list[dict[str, object]] = []
        for fraction in fractions:
            _, application = replay.interpolate_physical_schedule_homotopy(
                self.baseline,
                self.endpoint,
                fraction,
                expected_surface_fingerprints=self.fingerprints,
            )
            quality = (
                self.fail_quality
                if incomplete and fraction == 1.0
                else self.pass_quality
            )
            records.append(
                {
                    "fraction": fraction,
                    "fraction_float_hex": fraction.hex(),
                    "interpolation_evidence": application,
                    "root_schedule_sha256": replay._canonical_sha256(
                        {"fraction": fraction.hex(), "kind": "root schedule"}
                    ),
                    "coordinate_sha256": replay._canonical_sha256(
                        {"fraction": fraction.hex(), "kind": "coordinates"}
                    ),
                    "quality": copy.deepcopy(quality),
                    "low_quality_aggregate": {
                        "count": 1 if quality["status"] == "FAIL" else 0
                    },
                }
            )
        _, zero_application = replay.interpolate_physical_schedule_homotopy(
            self.baseline,
            self.endpoint,
            0.0,
            expected_surface_fingerprints=self.fingerprints,
        )
        status = "INCOMPLETE" if incomplete else "PASS"
        value: dict[str, object] = {
            "schema": replay.HOMOTOPY_DISCOVERY_SCHEMA,
            "status": status,
            "profile_complete": status == "PASS",
            "audit_only": True,
            "audit_variant": replay.HOMOTOPY_AUDIT_VARIANT,
            "calibration_PASS_authorized": False,
            "production_mesh_eligible": False,
            "mesh_written": False,
            "su2_called": False,
            "paraview_called": False,
            "runtime_mesh_tags_hardcoded": False,
            "source_bindings": {
                "direction_replay_approval_sha256": self.approval[
                    "approval_sha256"
                ],
                "source_audit_manifest_sha256": ["4" * 64],
                "local_schedule_binding_sha256": self.binding,
                "homotopy_endpoint_sha256": self.endpoint["endpoint_sha256"],
            },
            "fixed_direction_contract": {
                "candidate_root_count": 1,
                "changed_root_count": 1,
                "unchanged_root_count": 0,
                "direction_search_performed": False,
                "absolute_coordinate_replay": True,
                "changed_roots": [
                    {"root_coordinate_sha256": self.root_id}
                ],
            },
            "root_context_replay": {
                "expected_count": 1,
                "matched_count": 1,
                "missing_count": 0,
                "duplicate_count": 0,
                "unexplained_count": 0,
                "contexts": copy.deepcopy(self.approval["root_contexts"]),
            },
            "homotopy_contract": {
                "endpoint": copy.deepcopy(self.endpoint),
                "baseline_surface_schedules_sha256": zero_application[
                    "baseline_surface_schedules_sha256"
                ],
                "minimum_surface_schedules_sha256": zero_application[
                    "minimum_surface_schedules_sha256"
                ],
                "fraction_count": len(fractions),
                "ascending_fraction_float_hex": [
                    fraction.hex() for fraction in fractions
                ],
                "descending_fraction_float_hex": [
                    fraction.hex() for fraction in reversed(fractions)
                ],
                "absolute_schedule_rebuild": True,
                "ascending_descending_replay_required": True,
                "quality_monotonicity_assumed": False,
                "direction_search_performed": False,
            },
            "ascending_scan": records,
            "descending_replay": copy.deepcopy(list(reversed(records))),
            "observed_counts": {
                "projected_3d_element_count": self.prisms + self.core,
                "prism_element_count": self.prisms,
                "core_element_count": self.core,
                "core_tetra_count": self.core,
                "homotopy_fraction_count": len(fractions),
                "ascending_quality_evaluation_count": len(fractions),
                "descending_quality_evaluation_count": len(fractions),
                "searched_direction_count": 0,
            },
            "final_quality": copy.deepcopy(records[-1]["quality"]),
            "maximum_tested_pass_fraction_float_hex": (
                fractions[-2].hex() if incomplete else 1.0.hex()
            ),
            "first_tested_fail_fraction_float_hex": (
                1.0.hex() if incomplete else None
            ),
            "incomplete_reason": (
                replay.HOMOTOPY_INCOMPLETE_REASON if incomplete else None
            ),
        }
        value["discovery_sha256"] = replay._canonical_sha256(value)
        return value

    def _validate_discovery(self, value: object) -> dict[str, object]:
        return replay.validate_physical_schedule_homotopy_discovery(
            value,
            expected_endpoint=self.endpoint,
            expected_approval=self.approval,
            expected_projected_3d_elements=self.prisms + self.core,
            expected_prism_element_count=self.prisms,
            expected_core_element_count=self.core,
            expected_layer_count=self.layers,
            expected_surface_count=len(self.fingerprints),
            expected_surface_schedules=self.baseline,
            expected_minimum_prism_scaled_jacobian=0.01,
            expected_minimum_core_tetra_gamma=0.001,
        )

    def test_endpoint_is_exact_hash_bound_and_audit_only(self) -> None:
        validated = replay.validate_physical_schedule_homotopy_endpoint(
            self.endpoint,
            expected_binding_sha256=self.binding,
            expected_first_layer_height_m=self.first,
            expected_layer_count=self.layers,
        )
        self.assertEqual(
            replay.DEFAULT_PHYSICAL_SCHEDULE_HOMOTOPY_FRACTIONS,
            tuple(validated["fractions"]),
        )
        self.assertFalse(validated["quality_monotonicity_assumed"])
        self.assertFalse(validated["mesh_written"])
        tampered = copy.deepcopy(self.endpoint)
        tampered["fractions"][1] *= 2.0
        tampered["endpoint_sha256"] = replay._canonical_sha256(
            {key: item for key, item in tampered.items() if key != "endpoint_sha256"}
        )
        with self.assertRaises(replay.CoarseDirectionReplayError):
            replay.validate_physical_schedule_homotopy_endpoint(
                tampered,
                expected_binding_sha256=self.binding,
                expected_first_layer_height_m=self.first,
                expected_layer_count=self.layers,
            )

    def test_interpolation_rebuilds_absolute_convex_schedules(self) -> None:
        before = copy.deepcopy(self.baseline)
        minimum, zero = replay.interpolate_physical_schedule_homotopy(
            self.baseline,
            self.endpoint,
            0.0,
            expected_surface_fingerprints=self.fingerprints,
        )
        physical, one = replay.interpolate_physical_schedule_homotopy(
            self.baseline,
            self.endpoint,
            1.0,
            expected_surface_fingerprints=self.fingerprints,
        )
        fraction = 2.0**-4
        middle, evidence = replay.interpolate_physical_schedule_homotopy(
            self.baseline,
            self.endpoint,
            fraction,
            expected_surface_fingerprints=self.fingerprints,
        )
        self.assertEqual(before, self.baseline)
        self.assertEqual(self.baseline, physical)
        self.assertEqual(self.first, minimum[self.fingerprints[0]][0])
        self.assertEqual(self.first, middle[self.fingerprints[0]][0])
        self.assertEqual(self.layers, len(middle[self.fingerprints[0]]))
        expected = minimum[self.fingerprints[0]][-1] + fraction * (
            physical[self.fingerprints[0]][-1]
            - minimum[self.fingerprints[0]][-1]
        )
        self.assertEqual(expected, middle[self.fingerprints[0]][-1])
        self.assertEqual(0.0.hex(), zero["fraction_float_hex"])
        self.assertEqual(1.0.hex(), one["fraction_float_hex"])
        self.assertEqual(fraction.hex(), evidence["fraction_float_hex"])

    def test_unfrozen_fraction_and_nonphysical_baseline_fail_closed(self) -> None:
        with self.assertRaises(replay.CoarseDirectionReplayError):
            replay.interpolate_physical_schedule_homotopy(
                self.baseline,
                self.endpoint,
                0.3,
                expected_surface_fingerprints=self.fingerprints,
            )
        bad = copy.deepcopy(self.baseline)
        bad[self.fingerprints[0]][0] = self.first * 0.5
        with self.assertRaises(replay.CoarseDirectionReplayError):
            replay.interpolate_physical_schedule_homotopy(
                bad,
                self.endpoint,
                0.0,
                expected_surface_fingerprints=self.fingerprints,
            )

    def test_pass_and_scientific_incomplete_are_both_validated(self) -> None:
        self.assertEqual("PASS", self._validate_discovery(self._discovery())["status"])
        incomplete = self._validate_discovery(self._discovery(incomplete=True))
        self.assertEqual("INCOMPLETE", incomplete["status"])
        self.assertEqual(
            replay.HOMOTOPY_INCOMPLETE_REASON, incomplete["incomplete_reason"]
        )

    def test_reverse_replay_or_permission_tamper_is_not_incomplete(self) -> None:
        for mutate in (
            "reverse",
            "permission",
            "missing_fraction",
            "endpoint_binding",
        ):
            with self.subTest(mutate=mutate):
                value = self._discovery(incomplete=True)
                if mutate == "reverse":
                    value["descending_replay"][0]["coordinate_sha256"] = "f" * 64
                elif mutate == "permission":
                    value["mesh_written"] = True
                elif mutate == "endpoint_binding":
                    value["source_bindings"]["homotopy_endpoint_sha256"] = "e" * 64
                else:
                    value["ascending_scan"].pop(1)
                unsigned = dict(value)
                unsigned.pop("discovery_sha256")
                value["discovery_sha256"] = replay._canonical_sha256(unsigned)
                with self.assertRaises(replay.CoarseDirectionReplayError):
                    self._validate_discovery(value)


if __name__ == "__main__":
    unittest.main()
