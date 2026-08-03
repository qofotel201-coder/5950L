from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import tempfile
import tomllib
import unittest

from cfdpipe.boundary_layer_clearance_revalidation import (
    BoundaryLayerClearanceRevalidationError,
    derive_revalidated_clearance_report,
)
from cfdpipe.boundary_layer_local_schedule import (
    plan_local_boundary_layer_schedules,
)
from cfdpipe.coarse_mesh import normalize_coarse_mesh_config


ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _contract() -> dict:
    document = tomllib.loads(
        (ROOT / "config" / "coarse_mesh.toml").read_text(encoding="utf-8")
    )
    return normalize_coarse_mesh_config(document, repository_root=ROOT)


def _report(contract: dict, path: Path, clearance_m: float = 0.02) -> dict:
    required = float(contract["boundary_layer_design"]["total_thickness_m"])
    tolerance = required * 1.0e-6
    width = tolerance * 0.5
    source = Path(contract["pipeline_brep_path"])
    source_record = {
        "path": str(source),
        "sha256": contract["provenance"]["pipeline_brep_sha256"],
        "size_bytes": source.stat().st_size,
        "read_only": True,
    }
    surfaces = []
    for surface_index, fingerprint in enumerate(
        contract["wall_surface_fingerprints"],
        start=1,
    ):
        samples = []
        direction_samples = []
        for sample_index in range(5):
            lower = clearance_m + sample_index * 1.0e-6
            upper = lower + width
            label = f"sample-{sample_index}"
            samples.append(
                {
                    "surface_fingerprint_id": fingerprint,
                    "surface_tag_audit": surface_index,
                    "adjacent_volume_tag_audit": 1,
                    "label": label,
                    "status": "PASS",
                    "method": (
                        "doubling_then_bisection_first_detected_"
                        "inside_to_outside"
                    ),
                    "first_exit_guaranteed_for_nonconvex_volume": False,
                    "clearance_lower_bound_m": lower,
                    "clearance_upper_bound_m": upper,
                    "bracket_width_m": width,
                    "required_total_thickness_m": required,
                    "conservative_clearance_margin_m": lower - required,
                    "required_total_thickness_clear": lower >= required,
                }
            )
            direction_samples.append({"label": label})
        surface_status = "PASS" if clearance_m >= required else "FAIL"
        surfaces.append(
            {
                "surface_fingerprint_id": fingerprint,
                "surface_tag_audit": surface_index,
                "adjacent_volume_tag_audit": 1,
                "direction_audit": {
                    "status": "PASS",
                    "samples": direction_samples,
                },
                "sample_count": 5,
                "minimum_clearance_lower_bound_m": clearance_m,
                "minimum_conservative_margin_m": clearance_m - required,
                "required_total_thickness_status": surface_status,
                "samples": samples,
            }
        )
    status = "PASS" if clearance_m >= required else "FAIL"
    return {
        "schema": "cfdpipe.boundary_layer_clearance.v1",
        "status": status,
        "execution_status": "PASS",
        "clearance_requirement_status": status,
        "started_at_utc": "2026-08-02T01:02:03Z",
        "ended_at_utc": "2026-08-02T01:03:03Z",
        "scope": {
            "read_only_geometry_audit": True,
            "mesh_generated": False,
            "gmsh_write_called": False,
            "gui_started": False,
            "su2_called": False,
            "paraview_called": False,
            "extrusion_feasibility_claimed": False,
            "production_mesh_eligible": False,
        },
        "gmsh_version": "4.test",
        "gmsh_module_path": "C:/test/gmsh.py",
        "gmsh_session": {
            "fresh_session_required": True,
            "initialize_attempted": True,
            "initialize_called": True,
            "finalize_attempted": True,
            "finalize_called": True,
            "logger_messages": [],
            "cleanup_errors": [],
        },
        "coarse_contract_sha256": "f" * 64,
        "source_before": source_record,
        "source_after": copy.deepcopy(source_record),
        "source_unchanged": True,
        "required_total_thickness_m": required,
        "absolute_tolerance_m": tolerance,
        "surface_count": 48,
        "sample_count": 240,
        "surfaces": surfaces,
        "minimum_clearance_lower_bound_m": clearance_m,
        "minimum_clearance_location": {
            "surface_fingerprint_id": contract["wall_surface_fingerprints"][0],
            "surface_tag_audit": 1,
            "sample_label": "sample-0",
        },
        "failure_reasons": (
            []
            if status == "PASS"
            else ["uniform stack exceeds at least one sampled clearance"]
        ),
        "error": None,
        "secondary_errors": [],
        "command_inputs": {
            "markers_sha256": contract["provenance"]["markers_sha256"],
        },
        "report_path": str(path.resolve()),
    }


def _write(path: Path, report: dict, *, allow_nan: bool = False) -> str:
    path.write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=allow_nan,
        ),
        encoding="utf-8",
    )
    return _sha256(path)


class ClearanceRevalidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = _contract()

    def test_derivation_preserves_lineage_and_is_accepted_by_local_planner(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            source = directory / "legacy.json"
            source_digest = _write(source, _report(self.contract, source))
            original_bytes = source.read_bytes()

            derived = derive_revalidated_clearance_report(
                coarse_contract=self.contract,
                source_report_path=source,
                source_report_sha256=source_digest,
            )

            self.assertEqual(source.read_bytes(), original_bytes)
            self.assertEqual(
                derived["coarse_contract_sha256"],
                self.contract["clearance_evidence_contract_sha256"],
            )
            lineage = derived["revalidation"]
            self.assertEqual(lineage["status"], "PASS")
            self.assertEqual(lineage["original_report_path"], str(source.resolve()))
            self.assertEqual(lineage["original_report_sha256"], source_digest)
            self.assertEqual(lineage["original_coarse_contract_sha256"], "f" * 64)
            self.assertEqual(lineage["validated_surface_count"], 48)
            self.assertEqual(lineage["validated_sample_count"], 240)
            self.assertFalse(lineage["gmsh_called_during_revalidation"])

            derived_path = directory / "derived.json"
            derived_digest = _write(derived_path, derived)
            plan = plan_local_boundary_layer_schedules(
                coarse_contract=self.contract,
                clearance_report_path=derived_path,
                clearance_report_sha256=derived_digest,
            )
            self.assertEqual(plan["status"], "PASS")
            self.assertEqual(plan["surface_count"], 48)

    def test_lifecycle_source_counts_and_required_thickness_fail_closed(self) -> None:
        mutations = (
            (
                lambda value: value["gmsh_session"].__setitem__(
                    "finalize_called", False
                ),
                "finalize",
            ),
            (
                lambda value: value["source_after"].__setitem__(
                    "sha256", "0" * 64
                ),
                "source",
            ),
            (lambda value: value["surfaces"].pop(), "48 surfaces"),
            (
                lambda value: value.__setitem__(
                    "required_total_thickness_m", 0.01
                ),
                "required total thickness",
            ),
        )
        for mutation, expected in mutations:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as raw:
                path = Path(raw) / "legacy.json"
                report = _report(self.contract, path)
                mutation(report)
                digest = _write(path, report)
                with self.assertRaisesRegex(
                    BoundaryLayerClearanceRevalidationError,
                    expected,
                ):
                    derive_revalidated_clearance_report(
                        coarse_contract=self.contract,
                        source_report_path=path,
                        source_report_sha256=digest,
                    )

    def test_caller_hash_and_nonfinite_json_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "legacy.json"
            report = _report(self.contract, path)
            digest = _write(path, report)
            with self.assertRaisesRegex(
                BoundaryLayerClearanceRevalidationError,
                "does not match",
            ):
                derive_revalidated_clearance_report(
                    coarse_contract=self.contract,
                    source_report_path=path,
                    source_report_sha256="0" * 64,
                )
            self.assertNotEqual(digest, "0" * 64)

            report["surfaces"][0]["samples"][0][
                "clearance_lower_bound_m"
            ] = math.nan
            nan_digest = _write(path, report, allow_nan=True)
            with self.assertRaisesRegex(
                BoundaryLayerClearanceRevalidationError,
                "non-finite",
            ):
                derive_revalidated_clearance_report(
                    coarse_contract=self.contract,
                    source_report_path=path,
                    source_report_sha256=nan_digest,
                )


if __name__ == "__main__":
    unittest.main()
