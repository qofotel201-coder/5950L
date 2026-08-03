from __future__ import annotations

import copy
import unittest

from cfdpipe.coarse_schedule_invariant import (
    CoarseScheduleInvariantError,
    prove_growth_only_infeasible,
)


def _manifest() -> dict:
    fingerprints = [f"{index:064x}" for index in range(1, 49)]
    triangle = "f" * 64
    first = 2.5e-7
    schedules = {
        fingerprint: {
            "surface_fingerprint_id": fingerprint,
            "cumulative_heights_m": [first, first * 2.2],
        }
        for fingerprint in fingerprints
    }
    discovery = {
        "status": "INCOMPLETE",
        "full_layer_detection": {
            "minimum_scaled_jacobian_for_repair": 0.01,
            "bad_records": [
                {
                    "layer": 1,
                    "min_sj": 0.001,
                    "below_configured_min_sj": True,
                    "strict_nonpositive_quality": False,
                    "source_triangle_sha256": triangle,
                }
            ],
            "triangle_evidence": [
                {
                    "source_triangle_sha256": triangle,
                    "wall_surface_fingerprint": fingerprints[0],
                }
            ],
        },
    }
    return {
        "schema": "cfdpipe.coarse_repair_audit_manifest.v1",
        "status": "INCOMPLETE",
        "audit_only": True,
        "calibration_PASS_authorized": False,
        "production_mesh_eligible": False,
        "mesh_written": False,
        "su2_called": False,
        "paraview_called": False,
        "external_commands": [],
        "source_unchanged": True,
        "coarse_contract_sha256": "e" * 64,
        "gmsh_session": {
            "initialize_called": True,
            "clear_called": True,
            "finalize_called": True,
            "cleanup_errors": [],
            "write_guard": {
                "installed": True,
                "write_attempt_count": 0,
                "blocked_paths": [],
            },
            "diagnostic_audit": {
                "status": "PASS",
                "fatal_messages": [],
            },
        },
        "repair_discovery": discovery,
        "local_schedule_binding": {
            "schema": "cfdpipe.boundary_layer_local_schedule_binding.v1",
            "status": "PASS",
            "binding_sha256": "b" * 64,
            "first_layer_height_m": first,
            "layer_count": 2,
            "surface_schedules": schedules,
        },
        "strategy_config": {
            "contract_mode": "coarse_repair_audit_only",
            "repair_audit_only": True,
            "quality_improvement": {
                "minimum_prism_scaled_jacobian": 0.01,
            },
        },
    }


class CoarseScheduleInvariantTests(unittest.TestCase):
    def test_first_layer_failure_proves_only_the_fixed_direction_family(self) -> None:
        manifest = _manifest()
        report = prove_growth_only_infeasible(
            manifest,
            source_manifest_path="audit.json",
            source_manifest_sha256="a" * 64,
            discovery_validator=lambda value: copy.deepcopy(value),
        )
        self.assertEqual("PASS", report["status"])
        self.assertEqual("SCHEDULE_ONLY_PROVEN_INFEASIBLE", report["conclusion"])
        self.assertEqual(1, report["first_layer_failure_count"])
        self.assertFalse(
            report["proof_scope"][
                "geometry_aware_direction_or_topology_change_in_scope"
            ]
        )
        self.assertEqual(
            "GEOMETRY_AWARE_DIRECTION_OR_TOPOLOGY_AUDIT",
            report["required_next_stage"],
        )

    def test_no_layer_one_failure_or_unsafe_lifecycle_fails_closed(self) -> None:
        for mutate in (
            lambda value: value["repair_discovery"]["full_layer_detection"][
                "bad_records"
            ][0].update({"layer": 2}),
            lambda value: value["gmsh_session"]["write_guard"].update(
                {"write_attempt_count": 1}
            ),
            lambda value: value.update({"source_unchanged": False}),
        ):
            with self.subTest(mutate=mutate):
                manifest = _manifest()
                mutate(manifest)
                with self.assertRaises(CoarseScheduleInvariantError):
                    prove_growth_only_infeasible(
                        manifest,
                        source_manifest_path="audit.json",
                        source_manifest_sha256="a" * 64,
                        discovery_validator=lambda value: copy.deepcopy(value),
                    )


if __name__ == "__main__":
    unittest.main()
