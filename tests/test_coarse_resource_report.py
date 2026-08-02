"""Tests for strict coarse calibration evidence aggregation."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from cfdpipe.coarse_resource_report import (
    CoarseResourceReportError,
    build_coarse_resource_report,
    calibration_sample_from_manifest,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CoarseResourceReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "runs/mesh/coarse").mkdir(parents=True)
        self.contract = {
            "normalized_config_sha256": "a" * 64,
            "calibration_characteristic_lengths_m": [0.25, 0.20],
            "calibration_maximum_3d_elements": 750_000,
            "target_3d_elements": 5_000_000,
            "resource": {"minimum_disk_free_bytes": 20_000},
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _manifest(self, name: str, h: float, elements: int) -> Path:
        directory = self.root / "runs/mesh/coarse" / name
        directory.mkdir()
        msh = directory / "mesh.msh"
        su2 = directory / "mesh.su2"
        msh.write_bytes(b"msh-data")
        su2.write_bytes(b"su2-data")
        document = {
            "schema": "cfdpipe.coarse_mesh_calibration_manifest.v1",
            "status": "PASS",
            "calibration_only": True,
            "production_mesh_eligible": False,
            "su2_called": False,
            "paraview_called": False,
            "external_commands": [],
            "source_unchanged": True,
            "resource_sample_eligible": True,
            "coarse_contract_sha256": "a" * 64,
            "characteristic_length_m": h,
            "peak_working_set_bytes": elements * 100,
            "elapsed_seconds": float(elements) / 1000.0,
            "gmsh_sessions": {
                phase: {"finalize_called": True, "cleanup_errors": []}
                for phase in ("build", "readback")
            },
            "generation_audit": {"element_count_3d": elements},
            "readback_audit": {"element_count_3d": elements},
            "su2_validation": {"status": "PASS", "nelem": elements},
            "outputs": {
                path.name: {
                    "path": str(path),
                    "sha256": _sha(path),
                    "size_bytes": path.stat().st_size,
                }
                for path in (msh, su2)
            },
        }
        manifest = directory / "coarse_calibration_manifest.json"
        manifest.write_text(json.dumps(document), encoding="utf-8")
        return manifest

    def test_valid_manifest_returns_resource_sample(self) -> None:
        manifest = self._manifest("one", 0.25, 100_000)
        sample = calibration_sample_from_manifest(
            manifest, contract=self.contract, repository_root=self.root
        )
        self.assertEqual(sample["elements"], 100_000)
        self.assertEqual(sample["output_bytes"], 16)
        self.assertEqual(sample["manifest_sha256"], _sha(manifest))

    def test_stale_output_or_failed_manifest_is_rejected(self) -> None:
        manifest = self._manifest("one", 0.25, 100_000)
        document = json.loads(manifest.read_text("utf-8"))
        document["status"] = "FAIL"
        manifest.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(CoarseResourceReportError):
            calibration_sample_from_manifest(
                manifest, contract=self.contract, repository_root=self.root
            )
        document["status"] = "PASS"
        manifest.write_text(json.dumps(document), encoding="utf-8")
        (manifest.parent / "mesh.su2").write_bytes(b"changed")
        with self.assertRaises(CoarseResourceReportError):
            calibration_sample_from_manifest(
                manifest, contract=self.contract, repository_root=self.root
            )

    def test_requires_every_authorized_scale_exactly_once(self) -> None:
        first = self._manifest("one", 0.25, 100_000)
        with self.assertRaises(CoarseResourceReportError):
            build_coarse_resource_report(
                contract=self.contract,
                calibration_manifests=[first],
                repository_root=self.root,
                resources={
                    "physical_total_bytes": 10**12,
                    "physical_available_bytes": 10**12,
                    "virtual_available_bytes": 10**12,
                    "disk_free_bytes": 10**12,
                },
            )

    def test_two_scales_produce_pass_and_bind_samples(self) -> None:
        first = self._manifest("one", 0.25, 100_000)
        second = self._manifest("two", 0.20, 200_000)
        report = build_coarse_resource_report(
            contract=self.contract,
            calibration_manifests=[first, second],
            repository_root=self.root,
            resources={
                "physical_total_bytes": 10**12,
                "physical_available_bytes": 10**12,
                "virtual_available_bytes": 10**12,
                "disk_free_bytes": 10**12,
            },
        )
        self.assertEqual(report["status"], "PASS")
        self.assertTrue(report["production_mesh_authorized"])
        self.assertEqual(len(report["calibration_samples"]), 2)

    def test_pagefile_does_not_rescue_low_physical_availability(self) -> None:
        first = self._manifest("one", 0.25, 100_000)
        second = self._manifest("two", 0.20, 200_000)
        report = build_coarse_resource_report(
            contract=self.contract,
            calibration_manifests=[first, second],
            repository_root=self.root,
            resources={
                "physical_total_bytes": 16 * 1024**3,
                "physical_available_bytes": 1024**3,
                "virtual_available_bytes": 100 * 1024**3,
                "disk_free_bytes": 100 * 1024**3,
            },
        )
        self.assertEqual(report["status"], "FAIL")
        self.assertFalse(report["production_mesh_authorized"])
        self.assertIn(
            "PHYSICAL_AVAILABLE_INSUFFICIENT",
            {item["code"] for item in report["resource_gate"]["reasons"]},
        )

    def test_configured_disk_floor_is_an_additional_hard_gate(self) -> None:
        first = self._manifest("one", 0.25, 100_000)
        second = self._manifest("two", 0.20, 200_000)
        contract = copy.deepcopy(self.contract)
        contract["resource"]["minimum_disk_free_bytes"] = 2 * 10**12
        report = build_coarse_resource_report(
            contract=contract,
            calibration_manifests=[first, second],
            repository_root=self.root,
            resources={
                "physical_total_bytes": 10**12,
                "physical_available_bytes": 10**12,
                "virtual_available_bytes": 10**12,
                "disk_free_bytes": 10**12,
            },
        )
        self.assertEqual(report["status"], "FAIL")
        self.assertFalse(report["resource_gate"]["checks"]["configured_minimum_disk_free"])


if __name__ == "__main__":
    unittest.main()
