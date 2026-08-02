from __future__ import annotations

import copy
import json
import unittest

from cfdpipe.coarse_projection_gate import evaluate_projection_preflight


GIB = 1024**3
MIB = 1024**2


def _contract() -> dict:
    return {
        "schema": "cfdpipe.coarse_mesh.v1",
        "status": "CONFIGURED",
        "normalized_config_sha256": "a" * 64,
        "resource": {
            "os_physical_memory_reserve_bytes": 4 * GIB,
            "projection_worker_memory_limit_bytes": 1 * GIB,
            "worker_monitor_margin_bytes": 256 * MIB,
            "minimum_disk_free_bytes": 20 * GIB,
            "physical_memory_required": True,
            "pagefile_cannot_rescue_physical_memory_failure": True,
        },
    }


def _snapshot(**overrides: int) -> dict[str, int]:
    result = {
        "physical_total_bytes": 16 * GIB,
        "physical_available_bytes": 8 * GIB,
        "virtual_available_bytes": 64 * GIB,
        "disk_free_bytes": 100 * GIB,
    }
    result.update(overrides)
    return result


class CoarseProjectionPreflightTests(unittest.TestCase):
    def test_pass_uses_reserve_plus_hard_limit_plus_monitor_margin(self) -> None:
        required = 4 * GIB + 1 * GIB + 256 * MIB
        report = evaluate_projection_preflight(
            _contract(), _snapshot(physical_available_bytes=required)
        )

        self.assertEqual("PASS", report["status"])
        self.assertTrue(report["safe_to_launch_projection_worker"])
        self.assertTrue(report["safe_to_import_gmsh_in_worker"])
        self.assertFalse(report["pagefile_rescue_authorized"])
        self.assertEqual(required, report["requirements"]["physical_available_bytes"])
        self.assertEqual([], report["reasons"])
        json.dumps(report, allow_nan=False)

    def test_one_byte_below_physical_requirement_fails_despite_pagefile(self) -> None:
        required = 4 * GIB + 1 * GIB + 256 * MIB
        report = evaluate_projection_preflight(
            _contract(),
            _snapshot(
                physical_available_bytes=required - 1,
                virtual_available_bytes=128 * GIB,
            ),
        )

        self.assertEqual("FAIL", report["status"])
        self.assertFalse(report["safe_to_launch_projection_worker"])
        codes = {reason["code"] for reason in report["reasons"]}
        self.assertIn("PHYSICAL_AVAILABLE_INSUFFICIENT", codes)
        self.assertIn("PAGEFILE_NOT_ACCEPTED", codes)

    def test_disk_requirement_is_independent(self) -> None:
        report = evaluate_projection_preflight(
            _contract(), _snapshot(disk_free_bytes=20 * GIB - 1)
        )

        self.assertEqual("FAIL", report["status"])
        self.assertIn(
            "DISK_FREE_INSUFFICIENT",
            {reason["code"] for reason in report["reasons"]},
        )

    def test_snapshot_requires_exact_keys(self) -> None:
        for mutate in (
            lambda value: value.pop("virtual_available_bytes"),
            lambda value: value.update({"captured_at_utc": "extra"}),
        ):
            with self.subTest(mutate=mutate):
                snapshot = _snapshot()
                mutate(snapshot)
                report = evaluate_projection_preflight(_contract(), snapshot)
                self.assertEqual("FAIL", report["status"])
                self.assertFalse(report["checks"]["system_snapshot_exact_keys"])
                self.assertIn(
                    "SYSTEM_SNAPSHOT_KEYS_INVALID",
                    {reason["code"] for reason in report["reasons"]},
                )

    def test_snapshot_rejects_bool_negative_and_inconsistent_physical_values(self) -> None:
        invalid_snapshots = (
            _snapshot(physical_total_bytes=True),
            _snapshot(disk_free_bytes=-1),
            _snapshot(
                physical_total_bytes=6 * GIB,
                physical_available_bytes=7 * GIB,
            ),
        )
        for snapshot in invalid_snapshots:
            with self.subTest(snapshot=snapshot):
                report = evaluate_projection_preflight(_contract(), snapshot)
                self.assertEqual("FAIL", report["status"])
                self.assertFalse(report["safe_to_import_gmsh_in_worker"])
                json.dumps(report, allow_nan=False)

    def test_contract_resource_values_are_strict_positive_integers(self) -> None:
        for field, value in (
            ("os_physical_memory_reserve_bytes", 0),
            ("projection_worker_memory_limit_bytes", True),
            ("worker_monitor_margin_bytes", -1),
            ("minimum_disk_free_bytes", 1.5),
        ):
            with self.subTest(field=field):
                contract = _contract()
                contract["resource"][field] = value
                report = evaluate_projection_preflight(contract, _snapshot())
                self.assertEqual("FAIL", report["status"])
                self.assertIn(
                    "RESOURCE_VALUE_INVALID",
                    {reason["code"] for reason in report["reasons"]},
                )

    def test_weakened_pagefile_or_physical_policy_fails(self) -> None:
        for field in (
            "physical_memory_required",
            "pagefile_cannot_rescue_physical_memory_failure",
        ):
            with self.subTest(field=field):
                contract = _contract()
                contract["resource"][field] = False
                report = evaluate_projection_preflight(contract, _snapshot())
                self.assertEqual("FAIL", report["status"])

    def test_contract_must_have_normalized_identity(self) -> None:
        for field, value in (
            ("schema", "wrong"),
            ("status", "PASS"),
            ("normalized_config_sha256", "not-a-hash"),
        ):
            with self.subTest(field=field):
                contract = _contract()
                contract[field] = value
                report = evaluate_projection_preflight(contract, _snapshot())
                self.assertEqual("FAIL", report["status"])
                self.assertFalse(report["safe_to_launch_projection_worker"])

    def test_evaluation_does_not_mutate_inputs(self) -> None:
        contract = _contract()
        snapshot = _snapshot()
        before_contract = copy.deepcopy(contract)
        before_snapshot = copy.deepcopy(snapshot)

        evaluate_projection_preflight(contract, snapshot)

        self.assertEqual(before_contract, contract)
        self.assertEqual(before_snapshot, snapshot)


if __name__ == "__main__":
    unittest.main()
