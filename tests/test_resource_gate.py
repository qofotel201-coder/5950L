from __future__ import annotations

import json
import math
import unittest

from cfdpipe.resource_gate import (
    OS_RESERVE_BYTES,
    SAFETY_FACTOR,
    evaluate_coarse_resource_gate,
)


GIB = 1024**3
MIB = 1024**2


class CoarseResourceGateTests(unittest.TestCase):
    def sample(
        self,
        elements: int,
        peak_gib: float,
        elapsed_seconds: float,
        output_mib: float,
    ) -> dict[str, int | float]:
        return {
            "elements": elements,
            "peak_working_set_bytes": int(peak_gib * GIB),
            "elapsed_seconds": elapsed_seconds,
            "output_bytes": int(output_mib * MIB),
        }

    def evaluate(self, samples, **overrides):
        arguments = {
            "target_elements": 500_000,
            "physical_total_bytes": 64 * GIB,
            "physical_available_bytes": 48 * GIB,
            "virtual_available_bytes": 80 * GIB,
            "disk_free_bytes": 100 * GIB,
            "maximum_elapsed_seconds": 1_000.0,
        }
        arguments.update(overrides)
        return evaluate_coarse_resource_gate(samples, **arguments)

    def test_two_samples_produce_a_json_safe_pass_report(self) -> None:
        samples = [
            self.sample(100_000, 1.0, 100.0, 100.0),
            self.sample(200_000, 1.8, 230.0, 210.0),
        ]

        report = self.evaluate(samples)

        self.assertEqual("cfdpipe.coarse_resource_gate.v1", report["schema"])
        self.assertEqual("PASS", report["status"])
        self.assertEqual([], report["reasons"])
        self.assertEqual(4 * GIB, OS_RESERVE_BYTES)
        self.assertEqual(1.3, SAFETY_FACTOR)
        self.assertEqual(OS_RESERVE_BYTES, report["policy"]["os_reserve_bytes"])
        self.assertFalse(report["policy"]["pagefile_can_substitute_physical"])
        self.assertTrue(all(report["checks"].values()))
        for metric in report["projections"].values():
            self.assertGreaterEqual(metric["upper_bound"], metric["linear_upper"])
            self.assertGreaterEqual(metric["upper_bound"], metric["power_upper"])
        json.dumps(report, allow_nan=False)

    def test_superlinear_observation_controls_power_upper_bound(self) -> None:
        samples = [
            self.sample(100_000, 1.0, 100.0, 100.0),
            self.sample(200_000, 4.0, 400.0, 400.0),
        ]

        report = self.evaluate(
            samples,
            target_elements=400_000,
            physical_total_bytes=256 * GIB,
            physical_available_bytes=200 * GIB,
            maximum_elapsed_seconds=2_500.0,
        )

        memory = report["projections"]["peak_working_set_bytes"]
        self.assertAlmostEqual(2.0, memory["power_exponent"], places=12)
        self.assertGreater(memory["power_upper"], memory["linear_upper"])
        self.assertEqual(memory["power_upper"], memory["upper_bound"])
        self.assertGreaterEqual(
            report["requirements"]["elapsed_seconds"],
            report["projections"]["elapsed_seconds"]["upper_bound"],
        )
        self.assertEqual("PASS", report["status"])

    def test_pagefile_cannot_rescue_failed_physical_memory(self) -> None:
        samples = [
            self.sample(100_000, 1.0, 20.0, 50.0),
            self.sample(200_000, 2.0, 40.0, 100.0),
        ]

        report = self.evaluate(
            samples,
            target_elements=5_000_000,
            physical_total_bytes=16 * GIB,
            physical_available_bytes=12 * GIB,
            virtual_available_bytes=256 * GIB,
            disk_free_bytes=500 * GIB,
            maximum_elapsed_seconds=10_000.0,
        )

        self.assertEqual("FAIL", report["status"])
        self.assertFalse(report["checks"]["physical_total"])
        self.assertFalse(report["checks"]["physical_available"])
        self.assertTrue(report["checks"]["virtual_available"])
        codes = {reason["code"] for reason in report["reasons"]}
        self.assertIn("PHYSICAL_TOTAL_INSUFFICIENT", codes)
        self.assertIn("PHYSICAL_AVAILABLE_INSUFFICIENT", codes)
        self.assertIn("PAGEFILE_NOT_ACCEPTED", codes)

    def test_disk_and_elapsed_budgets_are_independent_fail_gates(self) -> None:
        samples = [
            self.sample(100_000, 0.2, 100.0, 200.0),
            self.sample(200_000, 0.4, 200.0, 400.0),
        ]

        report = self.evaluate(
            samples,
            disk_free_bytes=500 * MIB,
            maximum_elapsed_seconds=300.0,
        )

        self.assertEqual("FAIL", report["status"])
        self.assertFalse(report["checks"]["disk_free"])
        self.assertFalse(report["checks"]["elapsed_budget"])
        codes = {reason["code"] for reason in report["reasons"]}
        self.assertIn("DISK_FREE_INSUFFICIENT", codes)
        self.assertIn("ELAPSED_BUDGET_EXCEEDED", codes)

    def test_missing_elapsed_budget_is_reported_but_not_silently_passed(self) -> None:
        samples = [
            self.sample(100_000, 0.2, 10.0, 10.0),
            self.sample(200_000, 0.4, 20.0, 20.0),
        ]

        report = self.evaluate(samples, maximum_elapsed_seconds=None)

        self.assertEqual("PASS", report["status"])
        self.assertIsNone(report["checks"]["elapsed_budget"])
        self.assertEqual("NOT_GATED", report["time_gate_status"])
        self.assertIn("TIME_NOT_GATED", {item["code"] for item in report["warnings"]})

    def test_fewer_than_two_or_duplicate_scale_samples_fail_closed(self) -> None:
        one = [self.sample(100_000, 1.0, 10.0, 10.0)]
        duplicate = [
            self.sample(100_000, 1.0, 10.0, 10.0),
            self.sample(100_000, 1.1, 11.0, 11.0),
        ]

        one_report = self.evaluate(one)
        duplicate_report = self.evaluate(duplicate)

        self.assertEqual("FAIL", one_report["status"])
        self.assertEqual("FAIL", duplicate_report["status"])
        self.assertIn(
            "CALIBRATION_SAMPLE_COUNT",
            {reason["code"] for reason in one_report["reasons"]},
        )
        self.assertIn(
            "CALIBRATION_SCALE_COUNT",
            {reason["code"] for reason in duplicate_report["reasons"]},
        )

    def test_invalid_and_nonfinite_inputs_return_serializable_fail(self) -> None:
        samples = [
            self.sample(100_000, 1.0, math.nan, 10.0),
            {
                "elements": 200_000,
                "peak_working_set_bytes": -1,
                "elapsed_seconds": 20.0,
                "output_bytes": 20 * MIB,
            },
        ]

        report = self.evaluate(samples, physical_available_bytes=65 * GIB)

        self.assertEqual("FAIL", report["status"])
        codes = {reason["code"] for reason in report["reasons"]}
        self.assertIn("CALIBRATION_VALUE_INVALID", codes)
        self.assertIn("PHYSICAL_AVAILABLE_INVALID", codes)
        json.dumps(report, allow_nan=False)

    def test_target_must_be_an_extrapolation_not_below_largest_sample(self) -> None:
        samples = [
            self.sample(100_000, 1.0, 10.0, 10.0),
            self.sample(200_000, 2.0, 20.0, 20.0),
        ]

        report = self.evaluate(samples, target_elements=150_000)

        self.assertEqual("FAIL", report["status"])
        self.assertIn(
            "TARGET_BELOW_CALIBRATION_MAX",
            {reason["code"] for reason in report["reasons"]},
        )


if __name__ == "__main__":
    unittest.main()
