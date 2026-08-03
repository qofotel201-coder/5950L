from __future__ import annotations

import copy
import unittest

from cfdpipe.coarse_schedule_feasibility import (
    CoarseScheduleFeasibilityError,
    apply_minimum_growth_endpoint,
    make_minimum_growth_endpoint,
    validate_minimum_growth_application,
    validate_minimum_growth_endpoint,
)


class CoarseScheduleFeasibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.binding_sha256 = "a" * 64
        self.first = 2.5e-7
        self.layers = 60
        self.fingerprints = [f"{index:064x}" for index in range(1, 49)]
        self.baseline = {
            fingerprint: [
                self.first * sum(1.2**power for power in range(layer))
                for layer in range(1, self.layers + 1)
            ]
            for fingerprint in self.fingerprints
        }
        self.endpoint = make_minimum_growth_endpoint(
            baseline_binding_sha256=self.binding_sha256,
            first_layer_height_m=self.first,
            layer_count=self.layers,
        )

    def test_endpoint_preserves_first_height_and_layer_count_only(self) -> None:
        candidate, evidence = apply_minimum_growth_endpoint(
            self.baseline,
            self.endpoint,
            expected_surface_fingerprints=self.fingerprints,
        )
        self.assertEqual(48, len(candidate))
        self.assertEqual(
            [self.first * index for index in range(1, self.layers + 1)],
            candidate[self.fingerprints[0]],
        )
        self.assertTrue(evidence["first_layer_height_preserved"])
        self.assertTrue(evidence["layer_count_preserved"])
        self.assertFalse(evidence["calibration_PASS_authorized"])
        self.assertFalse(evidence["production_mesh_eligible"])
        self.assertFalse(evidence["mesh_written"])
        self.assertFalse(evidence["su2_called"])
        self.assertFalse(evidence["paraview_called"])

    def test_application_does_not_mutate_the_baseline(self) -> None:
        before = copy.deepcopy(self.baseline)
        apply_minimum_growth_endpoint(
            self.baseline,
            self.endpoint,
            expected_surface_fingerprints=self.fingerprints,
        )
        self.assertEqual(before, self.baseline)

    def test_application_evidence_is_exact_and_tamper_evident(self) -> None:
        _, evidence = apply_minimum_growth_endpoint(
            self.baseline,
            self.endpoint,
            expected_surface_fingerprints=self.fingerprints,
        )
        validated = validate_minimum_growth_application(
            evidence,
            endpoint=self.endpoint,
            baseline_surface_schedules=self.baseline,
            expected_surface_fingerprints=self.fingerprints,
        )
        self.assertEqual(evidence, validated)
        tampered = copy.deepcopy(evidence)
        tampered["candidate_total_thickness_m"] *= 2.0
        with self.assertRaises(CoarseScheduleFeasibilityError):
            validate_minimum_growth_application(
                tampered,
                endpoint=self.endpoint,
                baseline_surface_schedules=self.baseline,
                expected_surface_fingerprints=self.fingerprints,
            )

    def test_endpoint_hash_scope_and_inventory_fail_closed(self) -> None:
        for field, replacement in (
            ("candidate_growth_ratio", 1.01),
            ("preserve_first_layer_height", False),
            ("mesh_written", True),
            ("su2_called", True),
        ):
            with self.subTest(field=field):
                tampered = copy.deepcopy(self.endpoint)
                tampered[field] = replacement
                with self.assertRaises(CoarseScheduleFeasibilityError):
                    validate_minimum_growth_endpoint(
                        tampered,
                        expected_binding_sha256=self.binding_sha256,
                        expected_first_layer_height_m=self.first,
                        expected_layer_count=self.layers,
                    )

        duplicate_casefold = dict(self.baseline)
        duplicate_casefold[self.fingerprints[9].upper()] = duplicate_casefold[
            self.fingerprints[9]
        ]
        with self.assertRaises(CoarseScheduleFeasibilityError):
            apply_minimum_growth_endpoint(
                duplicate_casefold,
                self.endpoint,
                expected_surface_fingerprints=self.fingerprints,
            )

    def test_nonfinite_or_changed_first_layer_is_rejected(self) -> None:
        for replacement in (float("nan"), float("inf"), 0.0):
            with self.subTest(replacement=replacement):
                schedules = copy.deepcopy(self.baseline)
                schedules[self.fingerprints[0]][0] = replacement
                with self.assertRaises(CoarseScheduleFeasibilityError):
                    apply_minimum_growth_endpoint(
                        schedules,
                        self.endpoint,
                        expected_surface_fingerprints=self.fingerprints,
                    )


if __name__ == "__main__":
    unittest.main()
