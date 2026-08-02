from __future__ import annotations

import json
import math
from pathlib import Path
import tomllib
import unittest

from cfdpipe.coarse_calibration_gate import (
    MINIMUM_DISK_FREE_BYTES,
    evaluate_first_point_preflight,
    evaluate_second_point_preflight,
)
from cfdpipe.coarse_mesh import normalize_coarse_mesh_config


ROOT = Path(__file__).resolve().parents[1]
GIB = 1024**3
MIB = 1024**2


def _contract() -> dict:
    document = tomllib.loads(
        (ROOT / "config" / "coarse_mesh.toml").read_text(encoding="utf-8")
    )
    return normalize_coarse_mesh_config(document, repository_root=ROOT)


def _snapshot(
    *,
    total_gib: int = 16,
    available_gib: int = 12,
    virtual_gib: int = 32,
    disk_gib: int = 100,
) -> dict[str, int]:
    return {
        "physical_total_bytes": total_gib * GIB,
        "physical_available_bytes": available_gib * GIB,
        "virtual_available_bytes": virtual_gib * GIB,
        "disk_free_bytes": disk_gib * GIB,
    }


def _worker_private_commit_evidence(
    *,
    peak_private_commit_bytes: int = 1 * GIB,
    current_private_commit_bytes: int = 128 * MIB,
    worker_limit_bytes: int = 3 * GIB,
    peak_working_set_bytes: int = 9 * GIB,
) -> dict:
    return {
        "status": "PASS",
        "worker": {
            "hard_limit_installed_before_heavy_import": True,
            "builder_called": True,
        },
        "worker_memory": {
            "hard_limit": {
                "schema": "cfdpipe.worker_memory_limit.v1",
                "status": "PASS",
                "requested_process_memory_limit_bytes": worker_limit_bytes,
                "job_object": {
                    "process_memory_limit_bytes": worker_limit_bytes,
                    "process_assigned": True,
                    "handle_retained_for_process_lifetime": True,
                },
            },
            "process_after_calibration": {
                "schema": "cfdpipe.worker_process_memory.v2",
                "status": "PASS",
                "metrics": {
                    "working_set_bytes": 100 * MIB,
                    "peak_working_set_bytes": peak_working_set_bytes,
                    "private_usage_bytes": current_private_commit_bytes,
                    "pagefile_usage_bytes": current_private_commit_bytes,
                    "peak_pagefile_usage_bytes": peak_private_commit_bytes,
                },
                "private_commit_semantics": {
                    "schema": "cfdpipe.windows_private_commit_semantics.v1",
                    "job_object_process_memory_limit_basis": (
                        "private_commit_bytes"
                    ),
                    "current_private_commit_metric": (
                        "metrics.private_usage_bytes"
                    ),
                    "current_private_commit_source": (
                        "PROCESS_MEMORY_COUNTERS_EX.PrivateUsage"
                    ),
                    "peak_private_commit_metric": (
                        "metrics.peak_pagefile_usage_bytes"
                    ),
                    "peak_private_commit_source": (
                        "PROCESS_MEMORY_COUNTERS_EX.PeakPagefileUsage"
                    ),
                    "pagefile_field_means_commit_charge_not_pagefile_residency": True,
                    "working_set_is_not_job_process_memory_limit_metric": True,
                },
            },
        },
    }


def _first_sample(
    *,
    peak_private_commit_bytes: int = 1 * GIB,
    worker_limit_bytes: int = 3 * GIB,
    peak_working_set_bytes: int = 9 * GIB,
    **overrides: object,
) -> dict:
    first_size = _contract()["calibration_characteristic_lengths_m"][0]
    sample: dict[str, object] = {
        "status": "PASS",
        "characteristic_length_m": first_size,
        "elements": 200_000,
        # Deliberately misleading legacy field: the gate must not consume it.
        "peak_working_set_bytes": peak_working_set_bytes,
        "elapsed_seconds": 100.0,
        "output_bytes": 100 * MIB,
        "worker_isolation": _worker_private_commit_evidence(
            peak_private_commit_bytes=peak_private_commit_bytes,
            worker_limit_bytes=worker_limit_bytes,
            peak_working_set_bytes=peak_working_set_bytes,
        ),
    }
    sample.update(overrides)
    return sample


class FirstCalibrationPreflightTests(unittest.TestCase):
    def test_nominal_first_point_passes_and_is_strict_json(self) -> None:
        report = evaluate_first_point_preflight(
            _contract(), _snapshot(), timeout_seconds=840.0
        )

        self.assertEqual("PASS", report["status"])
        self.assertEqual("FIRST_POINT", report["stage"])
        self.assertEqual([], report["reasons"])
        self.assertEqual(
            int(7.5 * GIB), report["requirements"]["physical_available_bytes"]
        )
        self.assertNotIn("minimum_first_available_bytes", report["policy"])
        json.dumps(report, allow_nan=False)

    def test_low_available_physical_memory_fails(self) -> None:
        report = evaluate_first_point_preflight(
            _contract(),
            _snapshot(available_gib=7, virtual_gib=64),
            timeout_seconds=600.0,
        )

        self.assertEqual("FAIL", report["status"])
        codes = {item["code"] for item in report["reasons"]}
        self.assertIn("PHYSICAL_AVAILABLE_INSUFFICIENT", codes)
        self.assertIn("PAGEFILE_NOT_ACCEPTED", codes)
        self.assertEqual(
            int(7.5 * GIB), report["requirements"]["physical_available_bytes"]
        )

    def test_total_must_cover_reserve_hard_limit_and_monitor_margin(self) -> None:
        report = evaluate_first_point_preflight(
            _contract(),
            _snapshot(total_gib=7, available_gib=7, virtual_gib=64),
            timeout_seconds=600.0,
        )

        self.assertEqual("FAIL", report["status"])
        self.assertIn(
            "PHYSICAL_TOTAL_INSUFFICIENT",
            {item["code"] for item in report["reasons"]},
        )

    def test_exact_configured_physical_requirement_passes(self) -> None:
        required = int(7.5 * GIB)
        snapshot = {
            "physical_total_bytes": required,
            "physical_available_bytes": required,
            "virtual_available_bytes": 0,
            "disk_free_bytes": 100 * GIB,
        }
        report = evaluate_first_point_preflight(
            _contract(), snapshot, timeout_seconds=600.0
        )

        self.assertEqual("PASS", report["status"])
        self.assertTrue(report["checks"]["physical_available_minimum"])

    def test_disk_floor_is_twenty_gib(self) -> None:
        report = evaluate_first_point_preflight(
            _contract(), _snapshot(disk_gib=19), timeout_seconds=600.0
        )

        self.assertEqual("FAIL", report["status"])
        self.assertEqual(
            MINIMUM_DISK_FREE_BYTES, report["requirements"]["disk_free_bytes"]
        )
        self.assertIn(
            "DISK_FREE_INSUFFICIENT",
            {item["code"] for item in report["reasons"]},
        )

    def test_requested_timeout_cannot_exceed_contract(self) -> None:
        report = evaluate_first_point_preflight(
            _contract(), _snapshot(), timeout_seconds=841.0
        )

        self.assertEqual("FAIL", report["status"])
        self.assertIn(
            "REQUESTED_TIMEOUT_EXCEEDS_CONTRACT",
            {item["code"] for item in report["reasons"]},
        )

    def test_missing_worker_limit_policy_fails_closed(self) -> None:
        contract = _contract()
        del contract["resource"]["calibration_worker_memory_limit_bytes"]
        report = evaluate_first_point_preflight(
            contract, _snapshot(), timeout_seconds=600.0
        )

        self.assertEqual("FAIL", report["status"])
        self.assertIn(
            "RESOURCE_POLICY_INVALID",
            {item["code"] for item in report["reasons"]},
        )


class SecondCalibrationPreflightTests(unittest.TestCase):
    def test_cubic_prediction_and_safety_factor_pass(self) -> None:
        sizes = _contract()["calibration_characteristic_lengths_m"]
        scale = (sizes[0] / sizes[1]) ** 3
        report = evaluate_second_point_preflight(
            _contract(),
            _snapshot(),
            _first_sample(),
            timeout_seconds=840.0,
        )

        self.assertEqual("PASS", report["status"])
        self.assertAlmostEqual(scale, report["projections"]["cubic_scale_factor"])
        self.assertEqual(
            math.ceil(200_000 * scale), report["projections"]["elements"]
        )
        self.assertEqual(
            math.ceil(math.ceil(GIB * scale) * 1.3),
            report["projections"]["peak_private_commit_with_safety_bytes"],
        )
        self.assertFalse(
            report["projections"]["working_set_used_for_limit_projection"]
        )
        self.assertEqual(
            "private_commit_bytes", report["requirements"]["worker_limit_metric"]
        )
        self.assertAlmostEqual(
            100.0 * scale * 1.3,
            report["projections"]["elapsed_with_safety_seconds"],
        )
        json.dumps(report, allow_nan=False)

    def test_second_point_predicted_timeout_fails_at_840_seconds(self) -> None:
        report = evaluate_second_point_preflight(
            _contract(),
            _snapshot(),
            _first_sample(elapsed_seconds=400.0),
            timeout_seconds=840.0,
        )

        self.assertEqual("FAIL", report["status"])
        self.assertIn(
            "PREDICTED_TIMEOUT_EXCEEDED",
            {item["code"] for item in report["reasons"]},
        )

    def test_predicted_peak_must_fit_calibration_worker_hard_limit(self) -> None:
        report = evaluate_second_point_preflight(
            _contract(),
            _snapshot(available_gib=10, virtual_gib=128),
            _first_sample(peak_private_commit_bytes=2 * GIB),
            timeout_seconds=840.0,
        )

        self.assertEqual("FAIL", report["status"])
        codes = {item["code"] for item in report["reasons"]}
        self.assertIn("PREDICTED_PEAK_EXCEEDS_WORKER_HARD_LIMIT", codes)

    def test_peak_working_set_is_not_used_for_job_limit_projection(self) -> None:
        sizes = _contract()["calibration_characteristic_lengths_m"]
        report = evaluate_second_point_preflight(
            _contract(),
            _snapshot(),
            _first_sample(
                peak_private_commit_bytes=1 * GIB,
                peak_working_set_bytes=100 * GIB,
            ),
            timeout_seconds=840.0,
        )

        self.assertEqual("PASS", report["status"])
        self.assertEqual(
            math.ceil(GIB * (sizes[0] / sizes[1]) ** 3),
            report["projections"]["peak_private_commit_bytes"],
        )

    def test_legacy_peak_working_set_without_private_commit_evidence_is_rejected(self) -> None:
        sample = _first_sample()
        del sample["worker_isolation"]

        report = evaluate_second_point_preflight(
            _contract(), _snapshot(), sample, timeout_seconds=840.0
        )

        self.assertEqual("FAIL", report["status"])
        self.assertEqual({}, report["projections"])
        self.assertIn(
            "FIRST_SAMPLE_PRIVATE_COMMIT_EVIDENCE_INVALID",
            {item["code"] for item in report["reasons"]},
        )

    def test_tampered_private_commit_semantics_are_rejected(self) -> None:
        sample = _first_sample()
        process = sample["worker_isolation"]["worker_memory"][
            "process_after_calibration"
        ]
        process["private_commit_semantics"][
            "working_set_is_not_job_process_memory_limit_metric"
        ] = False

        report = evaluate_second_point_preflight(
            _contract(), _snapshot(), sample, timeout_seconds=840.0
        )

        self.assertEqual("FAIL", report["status"])
        self.assertFalse(
            report["checks"]["first_sample_private_commit_evidence"]
        )

    def test_first_sample_job_limit_must_match_current_contract(self) -> None:
        report = evaluate_second_point_preflight(
            _contract(),
            _snapshot(),
            _first_sample(worker_limit_bytes=2 * GIB),
            timeout_seconds=840.0,
        )

        self.assertEqual("FAIL", report["status"])
        self.assertEqual({}, report["projections"])
        self.assertIn(
            "FIRST_SAMPLE_WORKER_LIMIT_MISMATCH",
            {item["code"] for item in report["reasons"]},
        )

    def test_pagefile_cannot_rescue_second_point_physical_failure(self) -> None:
        report = evaluate_second_point_preflight(
            _contract(),
            _snapshot(available_gib=7, virtual_gib=128),
            _first_sample(),
            timeout_seconds=840.0,
        )

        self.assertEqual("FAIL", report["status"])
        codes = {item["code"] for item in report["reasons"]}
        self.assertIn("PHYSICAL_AVAILABLE_INSUFFICIENT", codes)
        self.assertIn("PAGEFILE_NOT_ACCEPTED", codes)
        self.assertEqual(
            int(7.5 * GIB), report["requirements"]["physical_available_bytes"]
        )

    def test_second_point_disk_uses_twenty_gib_floor(self) -> None:
        report = evaluate_second_point_preflight(
            _contract(),
            _snapshot(disk_gib=19),
            _first_sample(),
            timeout_seconds=840.0,
        )

        self.assertEqual("FAIL", report["status"])
        self.assertEqual(
            MINIMUM_DISK_FREE_BYTES, report["requirements"]["disk_free_bytes"]
        )
        self.assertIn(
            "DISK_FREE_INSUFFICIENT",
            {item["code"] for item in report["reasons"]},
        )

    def test_fail_manifest_cannot_seed_second_prediction(self) -> None:
        report = evaluate_second_point_preflight(
            _contract(),
            _snapshot(),
            _first_sample(status="FAIL"),
            timeout_seconds=840.0,
        )

        self.assertEqual("FAIL", report["status"])
        self.assertEqual({}, report["projections"])
        self.assertIn(
            "FIRST_SAMPLE_NOT_PASS",
            {item["code"] for item in report["reasons"]},
        )

    def test_manifest_nested_counts_and_output_sizes_are_supported(self) -> None:
        first_size = _contract()["calibration_characteristic_lengths_m"][0]
        manifest = {
            "schema": "cfdpipe.coarse_mesh_calibration_manifest.v1",
            "status": "PASS",
            "calibration_only": True,
            "production_mesh_eligible": False,
            "su2_called": False,
            "paraview_called": False,
            "resource_sample_eligible": True,
            "characteristic_length_m": first_size,
            "generation_audit": {"element_count_3d": 200_000},
            "peak_working_set_bytes": 100 * GIB,
            "elapsed_seconds": 100.0,
            "worker_isolation": _worker_private_commit_evidence(),
            "outputs": {
                "mesh.msh": {"size_bytes": 60 * MIB},
                "mesh.su2": {"size_bytes": 40 * MIB},
            },
        }
        report = evaluate_second_point_preflight(
            _contract(), _snapshot(), manifest, timeout_seconds=840.0
        )

        self.assertEqual("PASS", report["status"])
        self.assertEqual(
            100 * MIB, report["inputs"]["first_sample"]["output_bytes"]
        )

    def test_predicted_element_cap_fails_closed(self) -> None:
        report = evaluate_second_point_preflight(
            _contract(),
            _snapshot(),
            _first_sample(elements=500_000),
            timeout_seconds=840.0,
        )

        self.assertEqual("FAIL", report["status"])
        self.assertIn(
            "PREDICTED_ELEMENT_CAP_EXCEEDED",
            {item["code"] for item in report["reasons"]},
        )


if __name__ == "__main__":
    unittest.main()
