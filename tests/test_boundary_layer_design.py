from __future__ import annotations

import math
import unittest

from cfdpipe.boundary_layer_design import (
    BoundaryLayerDesignError,
    design_boundary_layer_stack,
)


def _project() -> dict:
    return {
        "atmosphere": {
            "model": "US_Standard_Atmosphere_1976",
            "altitude_kind": "geopotential",
            "viscosity_model": "Sutherland",
        },
        "reference": {"length_m": 5.2},
        "mesh": {
            "minimum_prism_layers": 15,
            "target_prism_layers": 20,
            "initial_growth_ratio": 1.2,
        },
    }


def _case() -> dict:
    return {"case_id": "M50_H21_A8_B0", "mach": 5.0, "altitude_km": 21.0}


def _policy() -> dict:
    return {
        "method": "turbulent_flat_plate_delta99_0p37_re_x_minus_one_fifth",
        "thickness_safety_factor": 1.2,
        "maximum_layer_count": 80,
        "maximum_core_to_last_layer_ratio": 10.0,
    }


class BoundaryLayerDesignTests(unittest.TestCase):
    def test_design_expands_stack_to_cover_planning_delta99(self) -> None:
        result = design_boundary_layer_stack(
            project=_project(),
            selected_case=_case(),
            first_layer_height_m=2.7645771273320875e-7,
            estimated_yplus=0.7,
            core_size_m=0.07162,
            policy=_policy(),
        )

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["layer_count"], 60)
        self.assertGreaterEqual(
            result["total_thickness_m"], result["required_total_thickness_m"]
        )
        self.assertGreaterEqual(result["layer_count"], 20)
        self.assertLessEqual(result["core_to_last_layer_ratio"], 10.0)
        self.assertTrue(result["requires_solution_based_validation"])
        self.assertTrue(all(math.isfinite(v) for v in result["finite_audit_values"]))

    def test_design_fails_when_layer_cap_cannot_cover_outer_thickness(self) -> None:
        policy = _policy()
        policy["maximum_layer_count"] = 20
        with self.assertRaisesRegex(BoundaryLayerDesignError, "layer cap"):
            design_boundary_layer_stack(
                project=_project(),
                selected_case=_case(),
                first_layer_height_m=2.7645771273320875e-7,
                estimated_yplus=0.7,
                core_size_m=0.07162,
                policy=policy,
            )

    def test_design_fails_for_unsafe_core_transition(self) -> None:
        with self.assertRaisesRegex(BoundaryLayerDesignError, "core-to-last-layer"):
            design_boundary_layer_stack(
                project=_project(),
                selected_case=_case(),
                first_layer_height_m=2.7645771273320875e-7,
                estimated_yplus=0.7,
                core_size_m=0.5,
                policy=_policy(),
            )

    def test_design_rejects_unsupported_method_and_nonfinite_input(self) -> None:
        policy = _policy()
        policy["method"] = "guessed"
        with self.assertRaisesRegex(BoundaryLayerDesignError, "method"):
            design_boundary_layer_stack(
                project=_project(),
                selected_case=_case(),
                first_layer_height_m=2.7645771273320875e-7,
                estimated_yplus=0.7,
                core_size_m=0.07162,
                policy=policy,
            )
        with self.assertRaisesRegex(BoundaryLayerDesignError, "finite"):
            design_boundary_layer_stack(
                project=_project(),
                selected_case=_case(),
                first_layer_height_m=float("nan"),
                estimated_yplus=0.7,
                core_size_m=0.07162,
                policy=_policy(),
            )


if __name__ == "__main__":
    unittest.main()
