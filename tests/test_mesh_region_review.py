from __future__ import annotations

import copy
import json
from pathlib import Path
import tomllib
from types import SimpleNamespace
import unittest

from cfdpipe.boundary_layer_smoke import RealProjectBoundaryLayerStrategy
from cfdpipe.mesh_region_review import MeshRegionReviewError, review_mesh_regions
from cfdpipe.production_coarse_worker import _level_contract, _regions


ROOT = Path(__file__).resolve().parents[1]


def _inputs() -> tuple[dict, dict, dict]:
    plan = json.loads(
        (ROOT / "reports/production_mesh_family_plan.json").read_text("utf-8")
    )
    result = json.loads(
        (ROOT / "reports/autodl_production_coarse_mesh_result.json").read_text(
            "utf-8"
        )
    )
    project = tomllib.loads((ROOT / "config/project.toml").read_text("utf-8"))
    return plan, result, project


class MeshRegionReviewTests(unittest.TestCase):
    def test_runtime_size_callback_preserves_surfaces_and_smoothly_refines(self) -> None:
        plan, _result, _project = _inputs()
        regions = _regions(plan, 1.0, "coarse")
        mesh = SimpleNamespace(setSizeCallback=lambda callback: setattr(mesh, "callback", callback))
        gmsh = SimpleNamespace(model=SimpleNamespace(mesh=mesh))
        evidence = RealProjectBoundaryLayerStrategy._configure_production_volume_sizes(
            gmsh, {"production_volume_regions": regions}
        )
        callback = mesh.callback
        incoming = 0.5
        self.assertEqual(callback(2, 1, 0.0, 0.0, 0.0, incoming), incoming)
        samples = [
            callback(3, 1, -0.5, 3.5, 7.0, incoming),
            callback(3, 1, 5.5, 1.4, 1.4, incoming),
            callback(3, 1, 5.3, 0.7, 0.7, incoming),
            callback(3, 1, 2.0, 0.0, 0.0, incoming),
        ]
        self.assertTrue(all(left > right for left, right in zip(samples, samples[1:])))
        farfield = regions[0]
        xmax = farfield["bounds_m"][3]
        width = farfield["transition_width_m"]
        at_edge = callback(3, 1, xmax, 3.5, 7.0, incoming)
        halfway = callback(3, 1, xmax + width / 2.0, 3.5, 7.0, incoming)
        outside = callback(3, 1, xmax + width, 3.5, 7.0, incoming)
        self.assertAlmostEqual(at_edge, farfield["size_m"])
        self.assertAlmostEqual(halfway, (farfield["size_m"] + incoming) / 2.0)
        self.assertAlmostEqual(outside, incoming)
        self.assertTrue(evidence["dimension_3_only"])
        self.assertTrue(evidence["wall_surface_triangulation_preserved"])

    def test_frozen_regions_authorize_medium_without_authorizing_rans(self) -> None:
        plan, result, project = _inputs()
        report = review_mesh_regions(
            plan=plan,
            coarse_result=result,
            project=project,
            plan_sha256="a" * 64,
            coarse_result_sha256="b" * 64,
        )
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(
            report["medium_authorization"]["status"], "AUTHORIZED_NOT_BUILT"
        )
        self.assertFalse(report["medium_authorization"]["rans_allowed"])
        self.assertGreater(
            report["regions"][0]["coarse_size_m"],
            report["regions"][-1]["coarse_size_m"],
        )

    def test_non_nested_or_non_refining_regions_fail_closed(self) -> None:
        plan, result, project = _inputs()
        bad = copy.deepcopy(plan)
        bad["physical_regions"][2]["coarse_size_m"] = 0.1
        with self.assertRaisesRegex(MeshRegionReviewError, "strictly refined"):
            review_mesh_regions(
                plan=bad,
                coarse_result=result,
                project=project,
                plan_sha256="a" * 64,
                coarse_result_sha256="b" * 64,
            )
        bad = copy.deepcopy(plan)
        bad["physical_regions"][3]["bounds_m"][0] = -1.0
        with self.assertRaisesRegex(MeshRegionReviewError, "not nested"):
            review_mesh_regions(
                plan=bad,
                coarse_result=result,
                project=project,
                plan_sha256="a" * 64,
                coarse_result_sha256="b" * 64,
            )

    def test_failed_coarse_quality_cannot_authorize_medium(self) -> None:
        plan, result, project = _inputs()
        bad = copy.deepcopy(result)
        bad["marker_omission_count"] = 1
        with self.assertRaisesRegex(MeshRegionReviewError, "marker_omission_count"):
            review_mesh_regions(
                plan=plan,
                coarse_result=bad,
                project=project,
                plan_sha256="a" * 64,
                coarse_result_sha256="b" * 64,
            )

    def test_medium_worker_uses_authorized_range_and_uniform_factor(self) -> None:
        plan, _result, _project = _inputs()
        contract = _level_contract(plan, "medium")
        self.assertEqual(
            contract,
            {
                "target_cells": 10_000_000,
                "minimum_cells": 8_000_000,
                "maximum_cells": 12_000_000,
            },
        )
        coarse = _regions(plan, 1.0, "coarse")
        medium = _regions(plan, 1.0, "medium")
        self.assertEqual(
            [item["bounds_m"] for item in medium],
            [item["bounds_m"] for item in coarse],
        )
        for coarse_region, medium_region in zip(coarse, medium):
            self.assertAlmostEqual(
                medium_region["size_m"], coarse_region["size_m"] * 0.794
            )

    def test_medium_worker_rejects_revoked_authorization(self) -> None:
        plan, _result, _project = _inputs()
        plan["levels"]["medium"]["build_authorized"] = False
        with self.assertRaisesRegex(ValueError, "not authorized"):
            _regions(plan, 1.0, "medium")


if __name__ == "__main__":
    unittest.main()
