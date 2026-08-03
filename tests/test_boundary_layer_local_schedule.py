from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import tempfile
import tomllib
import unittest

from cfdpipe.boundary_layer_local_schedule import (
    BoundaryLayerLocalScheduleError,
    plan_local_boundary_layer_schedules,
    solve_growth_ratio,
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


def _synthetic_report(contract: dict, clearance_m: float = 0.02) -> dict:
    required = contract["boundary_layer_design"]["total_thickness_m"]
    source = Path(contract["pipeline_brep_path"])
    source_record = {
        "path": str(source),
        "sha256": contract["provenance"]["pipeline_brep_sha256"],
        "size_bytes": source.stat().st_size,
        "read_only": True,
    }
    surfaces = []
    for fingerprint in contract["wall_surface_fingerprints"]:
        samples = []
        for index in range(5):
            lower = clearance_m + index * 1.0e-6
            samples.append(
                {
                    "surface_fingerprint_id": fingerprint,
                    "label": f"sample-{index}",
                    "clearance_lower_bound_m": lower,
                    "clearance_upper_bound_m": lower + 1.0e-8,
                    "required_total_thickness_m": required,
                    "conservative_clearance_margin_m": lower - required,
                    "required_total_thickness_clear": lower >= required,
                }
            )
        surface_pass = clearance_m >= required
        surfaces.append(
            {
                "surface_fingerprint_id": fingerprint,
                "sample_count": 5,
                "minimum_clearance_lower_bound_m": clearance_m,
                "minimum_conservative_margin_m": clearance_m - required,
                "required_total_thickness_status": (
                    "PASS" if surface_pass else "FAIL"
                ),
                "samples": samples,
            }
        )
    uniform_status = "PASS" if clearance_m >= required else "FAIL"
    return {
        "schema": "cfdpipe.boundary_layer_clearance.v1",
        "status": uniform_status,
        "execution_status": "PASS",
        "clearance_requirement_status": uniform_status,
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
        "gmsh_session": {
            "initialize_called": True,
            "finalize_attempted": True,
            "finalize_called": True,
            "cleanup_errors": [],
        },
        "coarse_contract_sha256": contract[
            "clearance_evidence_contract_sha256"
        ],
        "source_before": source_record,
        "source_after": copy.deepcopy(source_record),
        "source_unchanged": True,
        "required_total_thickness_m": required,
        "surface_count": 48,
        "sample_count": 240,
        "surfaces": surfaces,
        "minimum_clearance_lower_bound_m": clearance_m,
        "error": None,
        "secondary_errors": [],
    }


def _write_report(directory: Path, report: dict, *, allow_nan: bool = False) -> tuple[Path, str]:
    path = directory / "clearance.json"
    path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=allow_nan),
        encoding="utf-8",
    )
    return path, _sha256(path)


class GrowthRatioTests(unittest.TestCase):
    def test_bisection_reconstructs_endpoints_and_interior(self) -> None:
        first = 2.0e-6
        count = 60
        minimum = first * count
        maximum = math.fsum(first * 1.2**index for index in range(count))
        self.assertEqual(
            solve_growth_ratio(
                first_layer_height_m=first,
                layer_count=count,
                target_total_thickness_m=minimum,
                maximum_growth_ratio=1.2,
            ),
            1.0,
        )
        self.assertEqual(
            solve_growth_ratio(
                first_layer_height_m=first,
                layer_count=count,
                target_total_thickness_m=maximum,
                maximum_growth_ratio=1.2,
            ),
            1.2,
        )
        target = 0.5 * (minimum + maximum)
        ratio = solve_growth_ratio(
            first_layer_height_m=first,
            layer_count=count,
            target_total_thickness_m=target,
            maximum_growth_ratio=1.2,
        )
        reconstructed = math.fsum(first * ratio**index for index in range(count))
        self.assertGreater(ratio, 1.0)
        self.assertLess(ratio, 1.2)
        self.assertAlmostEqual(reconstructed, target, places=12)

    def test_bisection_rejects_clearance_below_fixed_stack(self) -> None:
        with self.assertRaisesRegex(
            BoundaryLayerLocalScheduleError, "fixed first height and layer count"
        ):
            solve_growth_ratio(
                first_layer_height_m=1.0e-6,
                layer_count=60,
                target_total_thickness_m=5.0e-5,
                maximum_growth_ratio=1.2,
            )

    def test_bisection_never_accepts_fewer_than_fifteen_layers(self) -> None:
        with self.assertRaisesRegex(BoundaryLayerLocalScheduleError, "15 layers"):
            solve_growth_ratio(
                first_layer_height_m=1.0e-6,
                layer_count=14,
                target_total_thickness_m=2.0e-5,
                maximum_growth_ratio=1.2,
            )


class LocalSchedulePlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = _contract()

    def _plan(self, report: dict) -> dict:
        with tempfile.TemporaryDirectory() as raw:
            path, digest = _write_report(Path(raw), report)
            return plan_local_boundary_layer_schedules(
                coarse_contract=self.contract,
                clearance_report_path=path,
                clearance_report_sha256=digest,
            )

    def test_uniform_failure_yields_48_clearance_aware_plans(self) -> None:
        # Unit behavior must remain reproducible when the repository contract
        # evolves and correctly makes older on-disk audits stale.  Actual
        # geometry evidence is exercised by the CLI quality gate, while this
        # test uses a report bound to the contract under test.
        result = self._plan(_synthetic_report(self.contract, 0.0129781875))

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["surface_count"], 48)
        self.assertEqual(result["sample_count"], 240)
        self.assertEqual(result["pass_surface_count"], 48)
        self.assertEqual(result["fail_surface_count"], 0)
        self.assertEqual(
            result["clearance_report"]["uniform_clearance_requirement_status"],
            "FAIL",
        )
        self.assertEqual(
            result["clearance_report"]["contract_binding"],
            "preserved_pre_local_policy_clearance_contract",
        )
        self.assertTrue(result["planning_only"])
        self.assertFalse(result["extrusion_authorized"])
        self.assertFalse(result["extrusion_feasibility_claimed"])
        self.assertTrue(result["requires_builder_integration_and_mesh_quality_gate"])
        self.assertFalse(result["mesh_generated"])
        self.assertFalse(result["production_mesh_eligible"])
        first = self.contract["boundary_layer_design"]["first_layer_height_m"]
        global_total = self.contract["boundary_layer_design"]["total_thickness_m"]
        for schedule in result["schedules"]:
            self.assertEqual(schedule["layer_count"], 60)
            self.assertEqual(schedule["first_layer_height_m"], first)
            self.assertGreaterEqual(schedule["local_growth_ratio"], 1.0)
            self.assertLessEqual(schedule["local_growth_ratio"], 1.2)
            expected = min(
                global_total,
                schedule["minimum_clearance_lower_bound_m"] * 0.40,
            )
            self.assertAlmostEqual(schedule["total_thickness_m"], expected, places=12)
            self.assertAlmostEqual(
                schedule["recommended_local_core_max_m"],
                schedule["last_layer_height_m"] * 10.0,
                places=14,
            )
            self.assertGreater(schedule["two_sided_core_clearance_margin_m"], 0.0)
            self.assertEqual(len(schedule["surface_clearance_evidence_sha256"]), 64)
            self.assertEqual(len(schedule["schedule_evidence_sha256"]), 64)
        json.dumps(result, allow_nan=False)

    def test_even_unit_growth_infeasible_returns_engineering_fail(self) -> None:
        first = self.contract["boundary_layer_design"]["first_layer_height_m"]
        minimum_total = first * 60
        report = _synthetic_report(
            self.contract,
            clearance_m=0.5 * minimum_total / 0.40,
        )
        result = self._plan(report)

        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["pass_surface_count"], 0)
        self.assertEqual(result["fail_surface_count"], 48)
        self.assertEqual(len(result["failure_reasons"]), 48)
        self.assertIsNone(result["schedules"][0]["local_growth_ratio"])

    def test_report_path_and_sha256_are_caller_bound(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path, digest = _write_report(
                Path(raw), _synthetic_report(self.contract)
            )
            with self.assertRaisesRegex(
                BoundaryLayerLocalScheduleError, "does not match the file"
            ):
                plan_local_boundary_layer_schedules(
                    coarse_contract=self.contract,
                    clearance_report_path=path,
                    clearance_report_sha256="0" * 64,
                )
            self.assertNotEqual(digest, "0" * 64)

    def test_fresh_report_bound_to_current_normalized_contract_is_accepted(self) -> None:
        report = _synthetic_report(self.contract)
        report["coarse_contract_sha256"] = self.contract[
            "normalized_config_sha256"
        ]
        result = self._plan(report)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(
            result["clearance_report"]["contract_binding"], "current_contract"
        )

    def test_execution_finalize_and_source_evidence_fail_closed(self) -> None:
        cases = (
            (lambda value: value.__setitem__("execution_status", "FAIL"), "execution"),
            (
                lambda value: value["gmsh_session"].__setitem__(
                    "finalize_called", False
                ),
                "finalize",
            ),
            (
                lambda value: value["source_after"].__setitem__("sha256", "0" * 64),
                "source",
            ),
        )
        for mutate, expected in cases:
            with self.subTest(expected=expected):
                report = _synthetic_report(self.contract)
                mutate(report)
                with self.assertRaisesRegex(BoundaryLayerLocalScheduleError, expected):
                    self._plan(report)

    def test_fingerprint_and_sample_cardinality_fail_closed(self) -> None:
        for mutate, expected in (
            (lambda value: value["surfaces"].pop(), "48 surfaces"),
            (lambda value: value["surfaces"][0]["samples"].pop(), "five samples"),
            (
                lambda value: value["surfaces"][0].__setitem__(
                    "surface_fingerprint_id", "f" * 64
                ),
                "fingerprints",
            ),
        ):
            with self.subTest(expected=expected):
                report = _synthetic_report(self.contract)
                mutate(report)
                with self.assertRaisesRegex(BoundaryLayerLocalScheduleError, expected):
                    self._plan(report)

    def test_nonfinite_json_is_rejected_before_planning(self) -> None:
        report = _synthetic_report(self.contract)
        report["surfaces"][0]["samples"][0]["clearance_lower_bound_m"] = math.nan
        with tempfile.TemporaryDirectory() as raw:
            path, digest = _write_report(Path(raw), report, allow_nan=True)
            with self.assertRaisesRegex(
                BoundaryLayerLocalScheduleError, "non-finite JSON constant"
            ):
                plan_local_boundary_layer_schedules(
                    coarse_contract=self.contract,
                    clearance_report_path=path,
                    clearance_report_sha256=digest,
                )

    def test_stale_normalized_contract_is_rejected(self) -> None:
        contract = copy.deepcopy(self.contract)
        contract["boundary_layer_maximum_clearance_fraction"] = 0.39
        report = _synthetic_report(self.contract)
        with tempfile.TemporaryDirectory() as raw:
            path, digest = _write_report(Path(raw), report)
            with self.assertRaisesRegex(
                BoundaryLayerLocalScheduleError, "contract hash is stale"
            ):
                plan_local_boundary_layer_schedules(
                    coarse_contract=contract,
                    clearance_report_path=path,
                    clearance_report_sha256=digest,
                )


if __name__ == "__main__":
    unittest.main()
