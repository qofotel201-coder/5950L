from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from cfdpipe import cli as cli_module
from cfdpipe.boundary_layer_trial import (
    BoundaryLayerTrialError,
    build_boundary_layer_trial,
    calculate_boundary_layer_schedule,
    determine_inward_height_sign,
    inspect_prism_layer_mesh,
    normalize_boundary_layer_trial_config,
    probe_inward_direction,
    reconcile_serialized_mesh_audit,
)


_FP = "a" * 64
_HASHES = {
    "project": "1" * 64,
    "cases": "2" * 64,
    "markers": "3" * 64,
    "pipeline_geometry": "4" * 64,
}


def _project() -> dict:
    return {
        "physics": {
            "solver": "RANS",
            "turbulence_model": "SST",
            "target_yplus": 0.7,
            "maximum_yplus": 1.0,
        },
        "mesh": {
            "minimum_prism_layers": 15,
            "initial_growth_ratio": 1.2,
        },
        "reference": {"length_m": 5.2},
        "atmosphere": {
            "model": "US_Standard_Atmosphere_1976",
            "altitude_kind": "geopotential",
            "viscosity_model": "Sutherland",
        },
        "production": {"design_case": "M50_H21_A8_B0"},
    }


def _case() -> dict:
    return {
        "case_id": "M50_H21_A8_B0",
        "mach": 5.0,
        "altitude_km": 21.0,
        "alpha_deg": 8.0,
        "beta_deg": 0.0,
    }


def _markers() -> dict:
    return {
        "solver_markers": [
            {
                "physical_name": "wall",
                "kind": "wall",
                "members": [
                    {
                        "fingerprint_id": _FP,
                        "area_m2": 2.0,
                        "entity_type": "Plane",
                        "centroid_m": [0.5, 0.0, 0.0],
                        "bounding_box_m": [0.0, 0.0, 0.0, 1.0, 1.0, 0.0],
                        "adjacent_volume_count": 1,
                    },
                    {
                        "fingerprint_id": "b" * 64,
                        "area_m2": 3.0,
                        "entity_type": "Plane",
                        "centroid_m": [0.5, 0.0, 1.0],
                        "bounding_box_m": [0.0, 0.0, 1.0, 1.0, 1.0, 1.0],
                        "adjacent_volume_count": 1,
                    },
                ],
            }
        ]
    }


def _records(pipeline_hash: str = _HASHES["pipeline_geometry"]) -> dict:
    return {
        "project": {"sha256": _HASHES["project"]},
        "cases": {"sha256": _HASHES["cases"]},
        "markers": {"sha256": _HASHES["markers"]},
        "pipeline_geometry": {"sha256": pipeline_hash},
    }


def _document() -> dict:
    return {
        "schema": "cfdpipe.boundary_layer_trial.v1",
        "status": "CONFIGURED",
        "policy": {
            "local_wall_patch_only": True,
            "production_mesh_eligible": False,
            "run_su2": False,
            "run_paraview": False,
            "runtime_tags_present": False,
            "max_3d_elements": 300000,
        },
        "provenance": {
            "project_sha256": _HASHES["project"],
            "cases_sha256": _HASHES["cases"],
            "markers_sha256": _HASHES["markers"],
            "pipeline_brep_sha256": _HASHES["pipeline_geometry"],
        },
        "selection": {
            "case_id": "M50_H21_A8_B0",
            "surface_fingerprint_id": _FP,
            "required_marker_kind": "wall",
        },
        "estimator": {
            "skin_friction_correlation": (
                "turbulent_flat_plate_cf_0p026_re_x_minus_one_seventh"
            ),
            "height_safety_factor": 0.5,
            "target_yplus_source": "project.physics.target_yplus",
            "reference_length_source": "project.reference.length_m",
        },
        "mesh": {
            "surface_size_m": 0.05,
            "layer_count_source": "project.mesh.minimum_prism_layers",
            "growth_ratio_source": "project.mesh.initial_growth_ratio",
            "element_order": 1,
            "recombine": True,
        },
    }


def _normalized() -> dict:
    return normalize_boundary_layer_trial_config(
        _document(),
        project=_project(),
        selected_case=_case(),
        marker_config=_markers(),
        input_records=_records(),
    )


class _DirectionModel:
    def getParametrizationBounds(self, dimension: int, tag: int):
        return [0.0, 0.0], [1.0, 1.0]

    def getValue(self, dimension: int, tag: int, parameters):
        return [float(parameters[0]), float(parameters[1]), 0.0]

    def getNormal(self, tag: int, parameters):
        return [0.0, 0.0, 1.0]

    def isInside(self, dimension: int, tag: int, coordinates, parametric=False):
        if dimension == 2:
            return 1
        return int(float(coordinates[2]) < 0.0)


class _DirectionGmsh:
    def __init__(self) -> None:
        self.model = _DirectionModel()


class _PrismMesh:
    def __init__(self, prism_count: int = 30, quality: float = 0.5) -> None:
        self.prism_count = prism_count
        self.quality = quality

    def getNodes(self):
        tags = list(range(1, 65))
        coordinates = [float(value) for tag in tags for value in (tag, 0, 0)]
        return tags, coordinates, []

    def getElements(self, dimension: int, tag: int = -1):
        if dimension == 2:
            return [2], [[1, 2]], [[1, 2, 3, 3, 2, 4]]
        if dimension == 3:
            tags = list(range(100, 100 + self.prism_count))
            nodes = [
                node
                for index in range(self.prism_count)
                for node in (
                    1 + index % 10,
                    2 + index % 10,
                    3 + index % 10,
                    21 + index % 10,
                    22 + index % 10,
                    23 + index % 10,
                )
            ]
            return [6], [tags], [nodes]
        return [], [], []

    def getElementProperties(self, element_type: int):
        if element_type == 2:
            return "Triangle 3", 2, 1, 3, [], 3
        if element_type == 6:
            return "Prism 6", 3, 1, 6, [], 6
        raise AssertionError(element_type)

    def getElementQualities(self, element_tags, quality_name="minSICN"):
        return [self.quality for _ in element_tags]


class _PrismGmsh:
    def __init__(self, prism_count: int = 30, quality: float = 0.5) -> None:
        self.model = type("Model", (), {})()
        self.model.mesh = _PrismMesh(prism_count, quality)


class _Logger:
    def start(self) -> None:
        pass

    def get(self):
        return ["Info : fake boundary-layer trial"]

    def stop(self) -> None:
        pass


class _Option:
    def setString(self, name: str, value: str) -> None:
        pass


class _Occ:
    def importShapes(self, path: str):
        return [(3, 1)]

    def synchronize(self) -> None:
        pass


class _EarlyFailureModel:
    def __init__(self) -> None:
        self.occ = _Occ()

    def add(self, name: str) -> None:
        pass


class _EarlyFailureGmsh:
    __version__ = "4.15.2-fake"
    __file__ = __file__

    def __init__(self) -> None:
        self.initialized = False
        self.finalized = False
        self.logger = _Logger()
        self.option = _Option()
        self.model = _EarlyFailureModel()

    def isInitialized(self) -> bool:
        return self.initialized

    def initialize(self, readConfigFiles=False) -> None:
        self.initialized = True

    def finalize(self) -> None:
        self.finalized = True
        self.initialized = False


class BoundaryLayerTrialTests(unittest.TestCase):
    def test_hash_bound_config_selects_exactly_one_wall_member(self) -> None:
        normalized = _normalized()

        self.assertEqual(normalized["surface_fingerprint_id"], _FP)
        self.assertEqual(normalized["wall_physical_name"], "wall")
        self.assertEqual(normalized["selected_area_m2"], 2.0)
        self.assertEqual(normalized["whole_wall_area_m2"], 5.0)
        self.assertEqual(normalized["layer_count"], 15)

    def test_stale_hash_or_nonwall_selection_fails_closed(self) -> None:
        stale = _document()
        stale["provenance"]["markers_sha256"] = "f" * 64
        with self.assertRaisesRegex(BoundaryLayerTrialError, "stale"):
            normalize_boundary_layer_trial_config(
                stale,
                project=_project(),
                selected_case=_case(),
                marker_config=_markers(),
                input_records=_records(),
            )

        nonwall = _markers()
        nonwall["solver_markers"][0]["kind"] = "farfield"
        with self.assertRaisesRegex(BoundaryLayerTrialError, "wall member"):
            normalize_boundary_layer_trial_config(
                _document(),
                project=_project(),
                selected_case=_case(),
                marker_config=nonwall,
                input_records=_records(),
            )

    def test_schedule_uses_design_case_atmosphere_and_project_layer_policy(self) -> None:
        schedule = calculate_boundary_layer_schedule(_normalized(), _case())

        self.assertEqual(schedule["layer_count"], 15)
        self.assertEqual(schedule["growth_ratio"], 1.2)
        self.assertEqual(len(schedule["layer_thicknesses_m"]), 15)
        self.assertEqual(len(schedule["cumulative_heights_m"]), 15)
        self.assertAlmostEqual(schedule["atmosphere"]["static_temperature_k"], 217.65)
        self.assertAlmostEqual(schedule["estimated_yplus_after_safety_factor"], 0.35)
        self.assertLess(
            schedule["first_layer_height_m"],
            schedule["unsafetied_first_layer_height_m"],
        )
        self.assertGreater(schedule["total_thickness_m"], 0.0)

    def test_oriented_catalog_and_bilateral_inside_samples_prove_inward_side(self) -> None:
        catalog = {
            "surfaces": [{"entity_tag": 10, "adjacent_volumes": [20]}],
            "volumes": [
                {
                    "entity_tag": 20,
                    "oriented_boundary_surfaces": [
                        {"surface_tag": 10, "orientation": 1}
                    ],
                }
            ],
        }
        direction = determine_inward_height_sign(
            _DirectionGmsh(), catalog, 10, probe_distance_m=1.0e-4
        )
        self.assertEqual(direction["inward_height_sign"], -1)
        self.assertFalse(direction["oriented_boundary_sign_used_for_direction"])
        self.assertEqual(direction["bilateral_sign_attempts"]["1"]["status"], "FAIL")
        evidence = probe_inward_direction(
            _DirectionGmsh(),
            surface_tag=10,
            volume_tag=20,
            height_sign=-1,
            probe_distance_m=1.0e-4,
        )
        self.assertEqual(evidence["status"], "PASS")
        self.assertEqual(evidence["decisive_inside_sample_count"], 5)
        with self.assertRaisesRegex(BoundaryLayerTrialError, "do not prove"):
            probe_inward_direction(
                _DirectionGmsh(),
                surface_tag=10,
                volume_tag=20,
                height_sign=1,
                probe_distance_m=1.0e-4,
            )

        catalog["volumes"][0]["oriented_boundary_surfaces"][0]["orientation"] = -1
        reversed_audit = determine_inward_height_sign(
            _DirectionGmsh(), catalog, 10, probe_distance_m=1.0e-4
        )
        self.assertEqual(reversed_audit["inward_height_sign"], -1)

    def test_prism_audit_proves_fifteen_layers_and_positive_quality(self) -> None:
        result = inspect_prism_layer_mesh(
            _PrismGmsh(),
            base_surface_tag=10,
            requested_layers=15,
            max_elements=300000,
        )

        self.assertEqual(result["base_triangle_count"], 2)
        self.assertEqual(result["element_count_3d"], 30)
        self.assertEqual(result["actual_layer_count"], 15)
        self.assertEqual(result["selected_patch_coverage_fraction"], 1.0)
        self.assertEqual(result["element_types_3d"], {"Prism 6": 30})

    def test_prism_audit_rejects_missing_column_cap_and_nonpositive_quality(self) -> None:
        with self.assertRaisesRegex(BoundaryLayerTrialError, "complete column"):
            inspect_prism_layer_mesh(
                _PrismGmsh(prism_count=29),
                base_surface_tag=10,
                requested_layers=15,
                max_elements=300000,
            )
        with self.assertRaisesRegex(BoundaryLayerTrialError, "exceeds cap"):
            inspect_prism_layer_mesh(
                _PrismGmsh(),
                base_surface_tag=10,
                requested_layers=15,
                max_elements=29,
            )
        with self.assertRaisesRegex(BoundaryLayerTrialError, "non-positive"):
            inspect_prism_layer_mesh(
                _PrismGmsh(quality=0.0),
                base_surface_tag=10,
                requested_layers=15,
                max_elements=300000,
            )

    def test_written_mesh_readback_is_authoritative_without_hiding_topology_change(self) -> None:
        generation = inspect_prism_layer_mesh(
            _PrismGmsh(),
            base_surface_tag=10,
            requested_layers=15,
            max_elements=300000,
        )
        serialized = dict(generation)
        serialized["node_count"] = 50

        result = reconcile_serialized_mesh_audit(generation, serialized)

        self.assertEqual(result["node_count"], 50)
        self.assertEqual(result["generation_node_count_before_write"], 64)
        self.assertEqual(result["node_count_delta_from_in_memory_model"], 14)
        self.assertTrue(result["serialized_file_readback"])
        self.assertEqual(
            result["node_count_source"], "fresh_gmsh_readback_of_written_msh"
        )

        changed = dict(serialized)
        changed["element_count_3d"] = 29
        with self.assertRaisesRegex(BoundaryLayerTrialError, "changed audited"):
            reconcile_serialized_mesh_audit(generation, changed)

        expanded = dict(serialized)
        expanded["node_count"] = 65
        with self.assertRaisesRegex(BoundaryLayerTrialError, "more nodes"):
            reconcile_serialized_mesh_audit(generation, expanded)

    def test_exception_after_initialize_still_finalizes_and_preserves_traceback(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        brep = root / "trial.brep"
        brep.write_bytes(b"read-only trial geometry")
        os.chmod(brep, stat.S_IREAD)
        self.addCleanup(os.chmod, brep, stat.S_IREAD | stat.S_IWRITE)
        brep_hash = hashlib.sha256(brep.read_bytes()).hexdigest()
        records = _records(brep_hash)
        document = _document()
        document["provenance"]["pipeline_brep_sha256"] = brep_hash
        config = root / "trial.toml"
        config.write_text(
            "\n".join(
                [
                    'schema = "cfdpipe.boundary_layer_trial.v1"',
                    'status = "CONFIGURED"',
                    "",
                    "[policy]",
                    "local_wall_patch_only = true",
                    "production_mesh_eligible = false",
                    "run_su2 = false",
                    "run_paraview = false",
                    "runtime_tags_present = false",
                    "max_3d_elements = 300000",
                    "",
                    "[provenance]",
                    f'project_sha256 = "{_HASHES["project"]}"',
                    f'cases_sha256 = "{_HASHES["cases"]}"',
                    f'markers_sha256 = "{_HASHES["markers"]}"',
                    f'pipeline_brep_sha256 = "{brep_hash}"',
                    "",
                    "[selection]",
                    'case_id = "M50_H21_A8_B0"',
                    f'surface_fingerprint_id = "{_FP}"',
                    'required_marker_kind = "wall"',
                    "",
                    "[estimator]",
                    "skin_friction_correlation = "
                    '"turbulent_flat_plate_cf_0p026_re_x_minus_one_seventh"',
                    "height_safety_factor = 0.5",
                    'target_yplus_source = "project.physics.target_yplus"',
                    'reference_length_source = "project.reference.length_m"',
                    "",
                    "[mesh]",
                    "surface_size_m = 0.05",
                    'layer_count_source = "project.mesh.minimum_prism_layers"',
                    'growth_ratio_source = "project.mesh.initial_growth_ratio"',
                    "element_order = 1",
                    "recombine = true",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        gmsh = _EarlyFailureGmsh()
        output = root / "failed"

        with mock.patch(
            "cfdpipe.boundary_layer_trial._collect_model",
            side_effect=RuntimeError("collect sentinel"),
        ), self.assertRaisesRegex(BoundaryLayerTrialError, "collect sentinel"):
            build_boundary_layer_trial(
                brep,
                output,
                project=_project(),
                selected_case=_case(),
                marker_config=_markers(),
                trial_config_path=config,
                input_records=records,
                gmsh_module=gmsh,
            )

        self.assertTrue(gmsh.finalized)
        manifest = json.loads(
            (output / "boundary_layer_trial_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(manifest["status"], "FAIL")
        self.assertTrue(manifest["gmsh_finalize_called"])
        self.assertIn("RuntimeError: collect sentinel", manifest["error"]["traceback"])

    def test_cli_command_does_not_resolve_or_call_any_external_solver(self) -> None:
        parsed = cli_module.build_parser().parse_args(
            [
                "pipeline",
                "boundary-layer-trial",
                "--step",
                "geometry/raw/model1.step",
                "--project",
                "config/project.toml",
                "--cases",
                "config/cases.csv",
                "--markers",
                "config/markers.toml",
                "--case",
                "M50_H21_A8_B0",
            ]
        )
        fake_inputs = type(
            "Inputs",
            (),
            {
                "pipeline_geometry_path": Path("derived.brep"),
                "project": _project(),
                "marker_document": _markers(),
                "input_records": _records(),
            },
        )()
        with mock.patch.object(
            cli_module, "load_real_connection_inputs", return_value=fake_inputs
        ), mock.patch.object(
            cli_module, "select_real_case", return_value=_case()
        ), mock.patch.object(
            cli_module,
            "build_boundary_layer_trial",
            return_value={"status": "PASS"},
        ) as build, mock.patch.object(
            cli_module,
            "_toolchain",
            side_effect=AssertionError("external toolchain must not be resolved"),
        ):
            self.assertEqual(parsed.handler(parsed), 0)
        build.assert_called_once()


if __name__ == "__main__":
    unittest.main()
