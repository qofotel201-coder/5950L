"""Contract tests for the real-project, smoke-scale connection pipeline.

These tests use only temporary text fixtures and mocks.  They must never import
Gmsh or start SU2, MPI, CUDA, ParaView, or any other external program.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import stat
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import cfdpipe.pipeline as pipeline_module
from cfdpipe.toolchain import ResolvedTool


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TEST_TEMP_ROOT = REPOSITORY_ROOT / "runs" / "real_connection_test_fixtures"
REPORT_KEYS = (
    "step_to_gmsh",
    "gmsh_to_su2_mesh",
    "mesh_to_su2",
    "su2_to_visualization_file",
    "visualization_file_to_pvbatch",
    "pvbatch_to_json",
)


def _write_project(path: Path, *, rear_outlet_mode: str = "TBD_AFTER_SUPERSONIC_PILOT") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""\
[project]
name = "test_real_connection"
geometry_file = "geometry/raw/model1.step"
result_root = "runs"

[units]
solver_system = "SI"
source_geometry_length = "mm"
solver_geometry_length = "m"
altitude_input = "km"

[reference]
length_m = 5.2
area_m2 = 0.2123716634
moment_origin_m = [2.6, 0.0, 0.0]

[atmosphere]
model = "US_Standard_Atmosphere_1976"
altitude_kind = "geopotential"
viscosity_model = "Sutherland"

[physics]
solver = "RANS"
turbulence_model = "SST"
fluid_model = "STANDARD_AIR"
wall_velocity = "no_slip"
wall_thermal = "adiabatic"
target_yplus = 0.7
maximum_yplus = 1.0

[boundaries]
farfield_role = "front_conical_surface"
wall_role = "vehicle_internal_and_external_walls"
rear_outlet_roles = ["rear_outlet_1", "rear_outlet_2"]
measurement_surface_role = "turning_section_outlet"
measurement_surface_is_solver_boundary = false

[boundary_conditions.rear_outlets]
mode = "{rear_outlet_mode}"
""",
        encoding="utf-8",
    )


def _write_cases(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "case_id,mach,altitude_km,alpha_deg,beta_deg,role\n"
        "M50_H21_A8_B0,5.0,21,8,0,design\n",
        encoding="utf-8",
    )


def _write_markers(path: Path, *, omit_role: str | None = None) -> None:
    root = path.parent.parent
    step = root / "geometry" / "raw" / "model1.step"
    if not step.exists():
        step.parent.mkdir(parents=True, exist_ok=True)
        step.write_bytes(b"canonical STEP marker fixture")
    brep = (
        root
        / "geometry"
        / "derived"
        / "model1_shared_topology"
        / "model1_shared_topology.brep"
    )
    brep.parent.mkdir(parents=True, exist_ok=True)
    brep.write_bytes(b"read-only pipeline BREP fixture")
    brep.chmod(stat.S_IREAD)
    roles = [
        ("front_conical_surface", 2),
        ("vehicle_internal_and_external_walls", 48),
        ("rear_outlet_1", 3),
        ("rear_outlet_2", 2),
    ]
    if omit_role is not None:
        roles = [
            ("unexpected_solver_role" if role == omit_role else role, count)
            for role, count in roles
        ]
    solver_markers: list[dict[str, object]] = []
    for role, count in roles:
        identifiers = [
            hashlib.sha256(f"{role}:{index}".encode()).hexdigest()
            for index in range(count)
        ]
        solver_markers.append(
            {
                "physical_name": role,
                "semantic_role": role,
                "kind": "test_boundary",
                "dimension": 2,
                "solver_boundary": True,
                "create_physical_group": True,
                "group_fingerprint_id": hashlib.sha256(role.encode()).hexdigest(),
                "member_count": count,
                "member_fingerprint_ids": identifiers,
                "members": [
                    {"fingerprint_id": identifier} for identifier in identifiers
                ],
            }
        )
    payload: dict[str, object] = {
        "schema": "cfdpipe.markers.v2",
        "status": "PASS",
        "source": {
            "path": "geometry/raw/model1.step",
            "sha256": hashlib.sha256(step.read_bytes()).hexdigest(),
            "read_only": True,
        },
        "pipeline_geometry": {
            "path": "geometry/derived/model1_shared_topology/model1_shared_topology.brep",
            "sha256": hashlib.sha256(brep.read_bytes()).hexdigest(),
            "format": "brep",
            "pipeline_eligible": True,
            "read_only": True,
        },
        "topology": {
            "volume_count": 2,
            "unique_surface_count": 57,
            "external_surface_count": 55,
            "shared_surface_count": 2,
        },
        "matching": {},
        "provenance": {},
        "solver_markers": solver_markers,
        "fluid": {
            "physical_name": "fluid",
            "semantic_role": "fluid",
            "dimension": 3,
            "solver_boundary": False,
            "create_physical_group": True,
            "volume_count": 2,
            "volume_selectors": [
                {"fingerprint_id": hashlib.sha256(b"volume:1").hexdigest()},
                {"fingerprint_id": hashlib.sha256(b"volume:2").hexdigest()},
            ],
        },
        "measurement": {
            "name": "turning_section_outlet",
            "semantic_role": "turning_section_outlet",
            "solver_boundary": False,
            "create_physical_group": False,
            "postprocess_only": True,
        },
        "policy": {
            "runtime_entity_identifiers_present": False,
            "step_names_present": False,
            "solver_boundary_groups_complete": True,
            "external_surface_members_disjoint_and_exhaustive": True,
            "measurement_is_physical_group": False,
            "measurement_is_solver_boundary": False,
            "pipeline_geometry_is_brep": True,
        },
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    document = {
        **payload,
        "integrity": {
            "algorithm": "SHA-256",
            "canonicalization": "test fixture",
            "payload_sha256": hashlib.sha256(canonical).hexdigest(),
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    refinement_id = str(solver_markers[1]["member_fingerprint_ids"][0])
    (path.parent / "topology_smoke.toml").write_text(
        "\n".join(
            (
                'schema = "cfdpipe.topology_smoke.v1"',
                'status = "CONFIGURED"',
                "",
                "[policy]",
                "topology_smoke_only = true",
                "production_mesh_eligible = false",
                "runtime_tags_present = false",
                "",
                "[provenance]",
                f'markers_sha256 = "{hashlib.sha256(path.read_bytes()).hexdigest()}"',
                f'pipeline_brep_sha256 = "{hashlib.sha256(brep.read_bytes()).hexdigest()}"',
                "",
                "[local_refinement]",
                f'surface_fingerprint_ids = ["{refinement_id}"]',
                "minimum_size_m = 0.012",
                "distance_max_m = 0.08",
                "sampling = 100",
                "",
            )
        ),
        encoding="utf-8",
    )


def _write_rear_outlet_pilot_approval(root: Path) -> Path:
    config_directory = root / "config"
    project = config_directory / "project.toml"
    cases = config_directory / "cases.csv"
    markers = config_directory / "markers.toml"
    topology = config_directory / "topology_smoke.toml"
    step = root / "geometry" / "raw" / "model1.step"
    output = config_directory / "rear_outlet_pilot.toml"

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    output.write_text(
        "\n".join(
            (
                'schema = "cfdpipe.rear_outlet_pilot.v1"',
                'status = "APPROVED_FOR_CONNECTION_ONLY"',
                "",
                "[approval]",
                'authority = "project_owner"',
                'approved_at_utc = "2026-08-01T00:00:00Z"',
                'decision = "implement_recommended_provisional_supersonic_connection_pilot"',
                "",
                "[provenance]",
                f'project_sha256 = "{digest(project)}"',
                f'cases_sha256 = "{digest(cases)}"',
                f'markers_sha256 = "{digest(markers)}"',
                f'topology_smoke_sha256 = "{digest(topology)}"',
                f'source_step_sha256 = "{digest(step)}"',
                "",
                "[scope]",
                'case_id = "M50_H21_A8_B0"',
                'mesh_level = "smoke"',
                'solver = "EULER"',
                "nproc = 1",
                "max_iterations = 5",
                "gpu_enabled = false",
                "connection_only = true",
                "production_eligible = false",
                "physical_interpretation_allowed = false",
                "",
                "[rear_outlet_boundary]",
                'project_mode_required = "TBD_AFTER_SUPERSONIC_PILOT"',
                'pilot_mode = "PROVISIONAL_SUPERSONIC_PILOT"',
                'su2_option = "MARKER_SUPERSONIC_OUTLET"',
                'roles = ["rear_outlet_1", "rear_outlet_2"]',
                "boundary_mode_frozen = false",
                "allow_static_pressure = false",
                "allow_back_pressure = false",
                'validation_status = "NOT_EVALUATED_BY_CONNECTION_PILOT"',
                "",
            )
        ),
        encoding="utf-8",
    )
    return output


def _load_fixture_marker_config(
    path: Path, *, repository_root: Path | None = None
) -> dict[str, object]:
    del repository_root
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_three_dimensional_mesh(
    path: Path,
    *,
    markers: tuple[tuple[str, int], ...] = (
        ("front_conical_surface", 1),
        ("vehicle_internal_and_external_walls", 1),
        ("rear_outlet_1", 1),
        ("rear_outlet_2", 1),
    ),
    nonfinite_coordinate: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    coordinate = "NaN" if nonfinite_coordinate else "0.0"
    lines = [
        "NDIME= 3",
        "NELEM= 1",
        "10 0 1 2 3 0",
        "NPOIN= 4",
        f"{coordinate} 0.0 0.0 0",
        "1.0 0.0 0.0 1",
        "0.0 1.0 0.0 2",
        "0.0 0.0 1.0 3",
        f"NMARK= {len(markers)}",
    ]
    for marker_name, marker_elements in markers:
        lines.extend((f"MARKER_TAG= {marker_name}", f"MARKER_ELEMS= {marker_elements}"))
        lines.extend("5 0 1 2" for _ in range(marker_elements))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class ProjectMeshValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary_directory = tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT)
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.required_markers = (
            "front_conical_surface",
            "vehicle_internal_and_external_walls",
            "rear_outlet_1",
            "rear_outlet_2",
        )

    def test_three_dimensional_mesh_and_all_configured_markers_pass(self) -> None:
        mesh = self.root / "mesh.su2"
        _write_three_dimensional_mesh(mesh)

        result = pipeline_module.validate_project_su2_mesh(
            mesh,
            self.required_markers,
        )

        self.assertEqual(result["ndime"], 3)
        self.assertEqual(result["nelem"], 1)
        self.assertEqual(result["npoin"], 4)
        self.assertEqual(result["nmark"], 4)
        self.assertEqual(set(result["markers"]), set(self.required_markers))
        self.assertTrue(all(result["markers"][name] > 0 for name in self.required_markers))

    def test_missing_required_marker_fails(self) -> None:
        mesh = self.root / "missing-marker.su2"
        _write_three_dimensional_mesh(
            mesh,
            markers=(
                ("front_conical_surface", 1),
                ("vehicle_internal_and_external_walls", 1),
                ("rear_outlet_1", 1),
            ),
        )

        with self.assertRaisesRegex(
            (pipeline_module.RealConnectionError, ValueError),
            "rear_outlet_2|marker",
        ):
            pipeline_module.validate_project_su2_mesh(mesh, self.required_markers)

    def test_empty_required_marker_fails(self) -> None:
        mesh = self.root / "empty-marker.su2"
        _write_three_dimensional_mesh(
            mesh,
            markers=(
                ("front_conical_surface", 1),
                ("vehicle_internal_and_external_walls", 1),
                ("rear_outlet_1", 1),
                ("rear_outlet_2", 0),
            ),
        )

        with self.assertRaisesRegex(
            (pipeline_module.RealConnectionError, ValueError),
            "rear_outlet_2|greater than zero|empty",
        ):
            pipeline_module.validate_project_su2_mesh(mesh, self.required_markers)

    def test_nonfinite_mesh_text_fails(self) -> None:
        mesh = self.root / "nonfinite.su2"
        _write_three_dimensional_mesh(mesh, nonfinite_coordinate=True)

        with self.assertRaisesRegex(
            (pipeline_module.RealConnectionError, ValueError),
            "NaN|Inf|non-finite",
        ):
            pipeline_module.validate_project_su2_mesh(mesh, self.required_markers)

    def test_two_dimensional_smoke_mesh_is_not_accepted_as_project_mesh(self) -> None:
        mesh = self.root / "two-dimensional.su2"
        mesh.write_text(
            "NDIME= 2\nNELEM= 1\n5 0 1 2 0\n"
            "NPOIN= 3\n0 0 0\n1 0 1\n0 1 2\n"
            "NMARK= 1\nMARKER_TAG= farfield\nMARKER_ELEMS= 1\n3 0 1\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            (pipeline_module.RealConnectionError, ValueError),
            "NDIME|3",
        ):
            pipeline_module.validate_project_su2_mesh(mesh, self.required_markers)

    def test_default_three_hundred_thousand_element_cap_is_enforced(self) -> None:
        mesh = self.root / "too-large.su2"
        _write_three_dimensional_mesh(mesh)
        mesh.write_text(
            mesh.read_text(encoding="utf-8").replace(
                "NELEM= 1", "NELEM= 300001", 1
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            (pipeline_module.RealConnectionError, ValueError),
            "300000|300001|element",
        ):
            pipeline_module.validate_project_su2_mesh(mesh, self.required_markers)


class RealConnectionEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary_directory = tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT)
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.paraview = self.root / "paraview"
        self.paraview.mkdir(parents=True)

    def _paraview_manifest(self) -> dict[str, object]:
        paths = (
            self.paraview / "solution_inventory.json",
            self.paraview / "smoke_postprocess.json",
            self.paraview / "smoke_integral.csv",
        )
        paths[0].write_text("{}\n", encoding="utf-8")
        paths[1].write_text("{}\n", encoding="utf-8")
        paths[2].write_text("name,value\nDensity,1.0\n", encoding="utf-8")
        return {
            "status": "PASS",
            "output_files": [
                {
                    "path": str(path),
                    "size_bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                for path in paths
            ],
        }

    def test_exact_paraview_json_and_csv_outputs_are_hash_validated(self) -> None:
        manifest = self._paraview_manifest()

        records = pipeline_module._validate_real_paraview_outputs(
            self.root, manifest
        )

        self.assertEqual(
            [Path(str(record["path"])).name for record in records],
            [
                "solution_inventory.json",
                "smoke_postprocess.json",
                "smoke_integral.csv",
            ],
        )

    def test_missing_required_paraview_record_fails_before_link_pass(self) -> None:
        manifest = self._paraview_manifest()
        manifest["output_files"] = list(manifest["output_files"])[1:]

        with self.assertRaisesRegex(
            pipeline_module.RealConnectionError,
            "does not declare required output",
        ):
            pipeline_module._validate_real_paraview_outputs(self.root, manifest)

    def test_existing_fail_manifest_gets_its_missing_stage_log(self) -> None:
        su2 = self.root / "su2"
        su2.mkdir()
        manifest_path = su2 / "run_manifest.json"
        payload = {
            "status": "FAIL",
            "error": {"type": "RuntimeError", "message": "original stderr"},
        }
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")

        pipeline_module._write_real_not_run_manifests(self.root)

        preserved = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(preserved["status"], "FAIL")
        self.assertEqual(preserved["error"]["message"], "original stderr")
        self.assertEqual(preserved["stage"], "su2")
        self.assertTrue((su2 / "su2_stage.log").is_file())


class RealConnectionInputGateTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary_directory = tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT)
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.project = self.root / "config" / "project.toml"
        self.cases = self.root / "config" / "cases.csv"
        self.markers = self.root / "config" / "markers.toml"
        self.step = self.root / "geometry" / "raw" / "model1.step"
        _write_project(self.project)
        _write_cases(self.cases)
        marker_loader = mock.patch(
            "cfdpipe.pipeline.load_marker_config",
            side_effect=_load_fixture_marker_config,
        )
        marker_loader.start()
        self.addCleanup(marker_loader.stop)

        def restore_derived_permissions() -> None:
            derived = self.root / "geometry" / "derived"
            if derived.exists():
                for candidate in derived.rglob("*"):
                    if candidate.is_file():
                        candidate.chmod(stat.S_IREAD | stat.S_IWRITE)

        self.addCleanup(restore_derived_permissions)

    def _write_read_only_step(self) -> None:
        self.step.parent.mkdir(parents=True, exist_ok=True)
        self.step.write_bytes(b"read-only STEP fixture")
        self.step.chmod(stat.S_IREAD)

        def restore_write_permission() -> None:
            if self.step.exists():
                self.step.chmod(stat.S_IREAD | stat.S_IWRITE)

        self.addCleanup(restore_write_permission)

    def test_report_exposes_case_specific_frozen_boundary(self) -> None:
        state = {
            "links": {
                key: {"status": "PASS", "evidence": [{"current": True}]}
                for key in REPORT_KEYS
            },
            "pilot_contract": {"boundary_mode_frozen": True},
        }
        report = pipeline_module.build_real_connection_report(
            self.root,
            state,
            started_at="2026-08-03T00:00:00Z",
            ended_at="2026-08-03T00:00:01Z",
        )

        self.assertTrue(
            report["metadata"]["constraints"]["rear_outlet_boundary_frozen"]
        )

    def test_valid_sources_preserve_configured_case_and_marker_roles(self) -> None:
        self._write_read_only_step()
        _write_markers(self.markers)

        inputs = pipeline_module.load_real_connection_inputs(
            step_path=self.step,
            project_path=self.project,
            cases_path=self.cases,
            markers_path=self.markers,
            case_id="M50_H21_A8_B0",
            trusted_repository_root=self.root,
        )
        selected = pipeline_module.select_real_case(inputs)

        self.assertEqual(selected["case_id"], "M50_H21_A8_B0")
        self.assertEqual(selected["mach"], 5.0)
        self.assertEqual(selected["altitude_km"], 21.0)
        self.assertEqual(selected["alpha_deg"], 8.0)
        self.assertEqual(selected["beta_deg"], 0.0)
        self.assertEqual(
            inputs.project["boundary_conditions"]["rear_outlets"]["mode"],
            "TBD_AFTER_SUPERSONIC_PILOT",
        )
        self.assertTrue(inputs.input_records["topology_smoke_config"]["exists"])
        self.assertEqual(inputs.topology_smoke_refinement["minimum_size_m"], 0.012)
        self.assertEqual(inputs.topology_smoke_refinement["distance_max_m"], 0.08)
        self.assertEqual(len(inputs.topology_smoke_refinement["surface_fingerprint_ids"]), 1)
        roles = {
            marker["role"]
            for marker in (*inputs.markers.values(), *inputs.measurements.values())
        }
        self.assertTrue(
            {
                "front_conical_surface",
                "vehicle_internal_and_external_walls",
                "rear_outlet_1",
                "rear_outlet_2",
                "turning_section_outlet",
                "fluid",
            }.issubset(roles)
        )

    def test_runtime_gate_defers_only_missing_historical_marker_provenance(self) -> None:
        self._write_read_only_step()
        _write_markers(self.markers)
        document = json.loads(self.markers.read_text(encoding="utf-8"))
        brep = (
            self.root
            / "geometry/derived/model1_shared_topology/model1_shared_topology.brep"
        )
        document["provenance"] = {
            "project": {
                "path": "config/project.toml",
                "sha256": hashlib.sha256(self.project.read_bytes()).hexdigest(),
            },
            "source_step": {
                "path": "geometry/raw/model1.step",
                "sha256": hashlib.sha256(self.step.read_bytes()).hexdigest(),
            },
            "pipeline_brep": {
                "path": "geometry/derived/model1_shared_topology/model1_shared_topology.brep",
                "sha256": hashlib.sha256(brep.read_bytes()).hexdigest(),
            },
            "repair_manifest": {
                "path": "runs/real_connection/geometry_repair/repair_manifest.json",
                "sha256": "1" * 64,
            },
            "marker_rematch": {
                "path": "runs/real_connection/geometry_repair/marker_rematch.json",
                "sha256": "2" * 64,
            },
            "interface_persistence": {
                "path": "runs/real_connection/geometry_repair_validation/interface_persistence.json",
                "sha256": "3" * 64,
            },
            "pipeline_topology": {
                "path": "runs/real_connection/geometry_repair/pipeline_brep_topology.json",
                "sha256": "4" * 64,
            },
        }
        payload = {key: value for key, value in document.items() if key != "integrity"}
        document["integrity"]["payload_sha256"] = hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        self.markers.write_text(json.dumps(document), encoding="utf-8")
        topology_path = self.markers.with_name("topology_smoke.toml")
        topology_document = {
            "schema": "cfdpipe.topology_smoke.v1",
            "status": "CONFIGURED",
            "policy": {
                "topology_smoke_only": True,
                "production_mesh_eligible": False,
                "runtime_tags_present": False,
            },
            "provenance": {
                "markers_sha256": hashlib.sha256(self.markers.read_bytes()).hexdigest(),
                "pipeline_brep_sha256": hashlib.sha256(brep.read_bytes()).hexdigest(),
            },
            "local_refinement": {
                "surface_fingerprint_ids": [
                    document["solver_markers"][1]["members"][0]["fingerprint_id"]
                ],
                "minimum_size_m": 0.012,
                "distance_max_m": 0.08,
                "sampling": 100,
            },
        }
        project_document = pipeline_module.tomllib.loads(
            self.project.read_text(encoding="utf-8")
        )
        with (
            mock.patch(
                "cfdpipe.pipeline.load_marker_config",
                side_effect=pipeline_module.MarkerConfigError("historical evidence missing"),
            ),
            mock.patch(
                "cfdpipe.pipeline.tomllib.load",
                side_effect=[project_document, document, topology_document],
            ),
            mock.patch(
                "cfdpipe.pipeline.validate_marker_config_document",
                side_effect=lambda value: value,
            ),
        ):
            inputs = pipeline_module.load_real_connection_inputs(
                step_path=self.step,
                project_path=self.project,
                cases_path=self.cases,
                markers_path=self.markers,
                topology_smoke_path=topology_path,
                case_id="M50_H21_A8_B0",
                trusted_repository_root=self.root,
            )

        self.assertEqual(
            inputs.input_records["markers"]["validation_mode"],
            "RUNTIME_CONTRACT_WITH_HISTORICAL_PROVENANCE_DEFERRED",
        )
        self.assertEqual(
            len(inputs.input_records["markers"]["deferred_historical_provenance"]),
            4,
        )

    def test_hash_bound_connection_only_pilot_approval_passes_gate(self) -> None:
        self._write_read_only_step()
        _write_markers(self.markers)
        approval = _write_rear_outlet_pilot_approval(self.root)
        inputs = pipeline_module.load_real_connection_inputs(
            step_path=self.step,
            project_path=self.project,
            cases_path=self.cases,
            markers_path=self.markers,
            case_id="M50_H21_A8_B0",
            trusted_repository_root=self.root,
        )

        contract = pipeline_module._rear_outlet_gate(
            inputs,
            approval,
            mesh_level="smoke",
            nproc=1,
            max_iterations=5,
        )

        self.assertEqual(contract["effective_mode"], "PROVISIONAL_SUPERSONIC_PILOT")
        self.assertTrue(contract["connection_only"])
        self.assertFalse(contract["production_eligible"])
        self.assertFalse(contract["boundary_mode_frozen"])
        self.assertFalse(contract["pressure_or_backpressure_guessed"])

    def test_stale_pilot_approval_hash_is_rejected(self) -> None:
        self._write_read_only_step()
        _write_markers(self.markers)
        approval = _write_rear_outlet_pilot_approval(self.root)
        approval.write_text(
            approval.read_text(encoding="utf-8").replace(
                hashlib.sha256(self.cases.read_bytes()).hexdigest(), "0" * 64
            ),
            encoding="utf-8",
        )
        inputs = pipeline_module.load_real_connection_inputs(
            step_path=self.step,
            project_path=self.project,
            cases_path=self.cases,
            markers_path=self.markers,
            case_id="M50_H21_A8_B0",
            trusted_repository_root=self.root,
        )

        with self.assertRaisesRegex(
            pipeline_module.RealConnectionConfigurationGateError,
            "hashes do not match",
        ):
            pipeline_module._rear_outlet_gate(
                inputs,
                approval,
                mesh_level="smoke",
                nproc=1,
                max_iterations=5,
            )

    def test_stale_topology_smoke_hash_fails_input_gate(self) -> None:
        self._write_read_only_step()
        _write_markers(self.markers)
        topology_config = self.markers.with_name("topology_smoke.toml")
        current_marker_hash = hashlib.sha256(self.markers.read_bytes()).hexdigest()
        topology_config.write_text(
            topology_config.read_text(encoding="utf-8").replace(
                current_marker_hash, "0" * 64
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            pipeline_module.RealConnectionInputError,
            "STALE_TOPOLOGY_SMOKE_PROVENANCE|topology_smoke",
        ):
            pipeline_module.load_real_connection_inputs(
                step_path=self.step,
                project_path=self.project,
                cases_path=self.cases,
                markers_path=self.markers,
                case_id="M50_H21_A8_B0",
                trusted_repository_root=self.root,
            )

    def test_unknown_topology_smoke_fingerprint_fails_input_gate(self) -> None:
        self._write_read_only_step()
        _write_markers(self.markers)
        topology_config = self.markers.with_name("topology_smoke.toml")
        marker_document = json.loads(self.markers.read_text(encoding="utf-8"))
        configured_id = marker_document["solver_markers"][1][
            "member_fingerprint_ids"
        ][0]
        topology_config.write_text(
            topology_config.read_text(encoding="utf-8").replace(
                configured_id, "f" * 64
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            pipeline_module.RealConnectionInputError,
            "UNKNOWN_TOPOLOGY_SMOKE_FINGERPRINT|topology_smoke",
        ):
            pipeline_module.load_real_connection_inputs(
                step_path=self.step,
                project_path=self.project,
                cases_path=self.cases,
                markers_path=self.markers,
                case_id="M50_H21_A8_B0",
                trusted_repository_root=self.root,
            )

    def test_missing_project_required_marker_role_fails_before_meshing(self) -> None:
        self._write_read_only_step()
        _write_markers(self.markers, omit_role="rear_outlet_2")

        with self.assertRaisesRegex(
            (pipeline_module.RealConnectionError, ValueError),
            "rear_outlet_2|MISSING_REQUIRED_MARKER_ROLE",
        ):
            pipeline_module.load_real_connection_inputs(
                step_path=self.step,
                project_path=self.project,
                cases_path=self.cases,
                markers_path=self.markers,
                case_id="M50_H21_A8_B0",
                trusted_repository_root=self.root,
            )

    def test_cli_step_must_match_project_geometry_file(self) -> None:
        mismatched_step = self.step.with_name("different.step")
        mismatched_step.parent.mkdir(parents=True, exist_ok=True)
        mismatched_step.write_bytes(b"different read-only STEP fixture")
        mismatched_step.chmod(stat.S_IREAD)
        self.addCleanup(
            lambda: mismatched_step.exists()
            and mismatched_step.chmod(stat.S_IREAD | stat.S_IWRITE)
        )
        _write_markers(self.markers)

        with self.assertRaisesRegex(
            (pipeline_module.RealConnectionError, ValueError),
            "STEP_PROJECT_PATH_MISMATCH|geometry_file|match",
        ):
            pipeline_module.load_real_connection_inputs(
                step_path=mismatched_step,
                project_path=self.project,
                cases_path=self.cases,
                markers_path=self.markers,
                case_id="M50_H21_A8_B0",
                trusted_repository_root=self.root,
            )

    def test_step_outside_geometry_raw_is_rejected(self) -> None:
        outside_step = self.root / "model1.step"
        outside_step.write_bytes(b"out-of-contract STEP fixture")
        outside_step.chmod(stat.S_IREAD)
        self.addCleanup(
            lambda: outside_step.exists()
            and outside_step.chmod(stat.S_IREAD | stat.S_IWRITE)
        )
        _write_markers(self.markers)

        with self.assertRaisesRegex(
            (pipeline_module.RealConnectionError, ValueError),
            "STEP_OUTSIDE_ALLOWED_DIRECTORY|geometry.raw|geometry\\\\raw|under",
        ):
            pipeline_module.load_real_connection_inputs(
                step_path=outside_step,
                project_path=self.project,
                cases_path=self.cases,
                markers_path=self.markers,
                case_id="M50_H21_A8_B0",
                trusted_repository_root=self.root,
            )

    def test_missing_markers_are_rejected_by_input_loader(self) -> None:
        self.step.parent.mkdir(parents=True, exist_ok=True)
        self.step.write_bytes(b"read-only STEP fixture")

        with self.assertRaisesRegex(
            (pipeline_module.RealConnectionError, ValueError),
            "markers.toml|marker",
        ):
            pipeline_module.load_real_connection_inputs(
                step_path=self.step,
                project_path=self.project,
                cases_path=self.cases,
                markers_path=self.markers,
                case_id="M50_H21_A8_B0",
                trusted_repository_root=self.root,
            )

    def test_current_missing_step_and_markers_stop_before_every_bridge_and_tool(self) -> None:
        output = self.root / "runs" / "real_connection"
        scripts = self.root / "scripts" / "paraview"
        scripts.mkdir(parents=True)
        toolchain = mock.Mock()
        toolchain.allow_mnt_c_executables = False

        with (
            mock.patch("cfdpipe.pipeline.GmshBridge") as gmsh_bridge,
            mock.patch(
                "cfdpipe.pipeline.build_topology_smoke_mesh"
            ) as topology_mesh,
            mock.patch("cfdpipe.pipeline.SU2Bridge") as su2_bridge,
            mock.patch("cfdpipe.pipeline.ParaViewBridge") as paraview_bridge,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(pipeline_module.RealConnectionError):
                pipeline_module.run_real_connection(
                    step_path=self.step,
                    project_path=self.project,
                    cases_path=self.cases,
                    markers_path=self.markers,
                    case_id="M50_H21_A8_B0",
                    mesh_level="smoke",
                    nproc=1,
                    max_iterations=5,
                    output_directory=output,
                    allowed_output_root=output,
                    trusted_repository_root=self.root,
                    toolchain=toolchain,
                    paraview_script_directory=scripts,
                    timeout_seconds=300,
                    live_output=False,
                    controller_argv=("cfdpipe", "pipeline", "run"),
                )

        gmsh_bridge.assert_not_called()
        topology_mesh.assert_not_called()
        su2_bridge.assert_not_called()
        paraview_bridge.assert_not_called()
        self.assertEqual(toolchain.mock_calls, [])

        report_path = output / "real_connection_report.json"
        self.assertTrue(report_path.is_file())
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(set(REPORT_KEYS) - set(report), set())
        for key in REPORT_KEYS:
            with self.subTest(connection=key):
                self.assertEqual(report[key]["status"], "FAIL")
                self.assertIsInstance(report[key]["evidence"], list)
                self.assertTrue(report[key]["evidence"])
        self.assertEqual(report.get("overall"), "FAIL")

    def test_rear_outlet_tbd_stops_after_mesh_without_su2_or_paraview(self) -> None:
        self._write_read_only_step()
        _write_markers(self.markers)
        output = self.root / "runs" / "real_connection"
        scripts = self.root / "scripts" / "paraview"
        scripts.mkdir(parents=True)
        toolchain = mock.Mock()
        toolchain.allow_mnt_c_executables = False
        def build_mesh(
            brep_path: Path,
            mesh_directory: Path,
            marker_document: object,
            *,
            mesh_options: object,
            max_elements: int,
        ) -> dict[str, object]:
            self.assertEqual(
                Path(brep_path),
                self.root
                / "geometry"
                / "derived"
                / "model1_shared_topology"
                / "model1_shared_topology.brep",
            )
            self.assertEqual(Path(mesh_directory), output / "mesh")
            self.assertIsInstance(marker_document, dict)
            self.assertIsInstance(mesh_options, dict)
            self.assertEqual(mesh_options["local_refinement"]["minimum_size_m"], 0.012)
            self.assertEqual(
                len(mesh_options["local_refinement"]["surface_fingerprint_ids"]),
                1,
            )
            self.assertEqual(max_elements, 300_000)
            mesh_directory.mkdir(parents=True)
            msh_path = mesh_directory / "mesh.msh"
            su2_path = mesh_directory / "mesh.su2"
            msh_path.write_text("mock MSH produced through API\n", encoding="utf-8")
            _write_three_dimensional_mesh(su2_path)
            return {
                "status": "PASS",
                "outputs": {
                    "mesh.msh": {"path": str(msh_path)},
                    "mesh.su2": {"path": str(su2_path)},
                },
                "topology": {
                    "volume_count": 2,
                    "external_surface_count": 55,
                    "shared_patch_count": 2,
                },
                "mesh": {
                    "quality": {
                        "nonpositive_determinant_jacobian_count": 0,
                    },
                    "boundary_layer": {
                        "actual_layer_count": 0,
                        "coverage_fraction": 0.0,
                    },
                },
            }

        with (
            mock.patch(
                "cfdpipe.pipeline.build_topology_smoke_mesh",
                side_effect=build_mesh,
            ) as topology_mesh,
            mock.patch("cfdpipe.pipeline.SU2Bridge") as su2_bridge,
            mock.patch("cfdpipe.pipeline.ParaViewBridge") as paraview_bridge,
        ):
            with self.assertRaises(pipeline_module.RealConnectionError) as raised:
                pipeline_module.run_real_connection(
                    step_path=self.step,
                    project_path=self.project,
                    cases_path=self.cases,
                    markers_path=self.markers,
                    case_id="M50_H21_A8_B0",
                    mesh_level="smoke",
                    nproc=1,
                    max_iterations=5,
                    output_directory=output,
                    allowed_output_root=output,
                    trusted_repository_root=self.root,
                    toolchain=toolchain,
                    paraview_script_directory=scripts,
                    timeout_seconds=300,
                    live_output=False,
                    controller_argv=("cfdpipe", "pipeline", "run"),
                )

        topology_mesh.assert_called_once()
        su2_bridge.assert_not_called()
        paraview_bridge.assert_not_called()
        self.assertEqual(toolchain.mock_calls, [])
        self.assertIsNotNone(raised.exception.report_path)
        report = json.loads(
            (output / "real_connection_report.json").read_text(encoding="utf-8")
        )
        self.assertEqual(report["step_to_gmsh"]["status"], "PASS")
        self.assertEqual(report["gmsh_to_su2_mesh"]["status"], "PASS")
        for key in REPORT_KEYS[2:]:
            self.assertEqual(report[key]["status"], "FAIL")
        failure = report["metadata"]["failure"]
        self.assertEqual(failure["code"], "REAR_OUTLET_BOUNDARY_NOT_FROZEN")
        self.assertFalse((output / "su2" / "case.cfg").exists())
        config_manifest = json.loads(
            (output / "su2" / "config_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(config_manifest["status"], "FAIL")
        self.assertEqual(
            config_manifest["error"]["code"],
            "REAR_OUTLET_BOUNDARY_NOT_FROZEN",
        )

    def test_approved_pilot_runs_all_real_connection_stages_in_order(self) -> None:
        self._write_read_only_step()
        _write_markers(self.markers)
        _write_rear_outlet_pilot_approval(self.root)
        output = self.root / "runs" / "real_connection"
        scripts = self.root / "scripts" / "paraview"
        scripts.mkdir(parents=True)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        su2_executable = bin_dir / "SU2_CFD.exe"
        pvbatch_executable = bin_dir / "pvbatch.exe"
        su2_executable.write_bytes(b"SU2 fixture")
        pvbatch_executable.write_bytes(b"pvbatch fixture")
        toolchain = mock.Mock()
        toolchain.allow_mnt_c_executables = False
        toolchain.resolve.side_effect = lambda name: {
            "su2_cfd": ResolvedTool("su2_cfd", su2_executable, "config"),
            "pvbatch": ResolvedTool("pvbatch", pvbatch_executable, "config"),
        }[name]
        calls: list[str] = []

        def build_mesh(
            brep_path: Path,
            mesh_directory: Path,
            marker_document: object,
            *,
            mesh_options: object,
            max_elements: int,
        ) -> dict[str, object]:
            del brep_path, marker_document, mesh_options
            calls.append("gmsh")
            self.assertEqual(max_elements, 300_000)
            mesh_directory.mkdir(parents=True)
            msh_path = mesh_directory / "mesh.msh"
            su2_path = mesh_directory / "mesh.su2"
            msh_path.write_text("mock MSH\n", encoding="utf-8")
            _write_three_dimensional_mesh(su2_path)
            return {
                "status": "PASS",
                "outputs": {
                    "mesh.msh": {"path": str(msh_path)},
                    "mesh.su2": {"path": str(su2_path)},
                },
                "topology": {"volume_count": 2},
                "mesh": {
                    "quality": {"nonpositive_determinant_jacobian_count": 0},
                    "boundary_layer": {
                        "actual_layer_count": 0,
                        "coverage_fraction": 0.0,
                    },
                },
            }

        def prepare_case(*args: object, **kwargs: object) -> dict[str, object]:
            del args, kwargs
            calls.append("config")
            run_dir = output / "su2"
            run_dir.mkdir(parents=True)
            source = output / "mesh" / "mesh.su2"
            local_mesh = run_dir / "mesh.su2"
            local_mesh.write_bytes(source.read_bytes())
            config_path = run_dir / "case.cfg"
            config_path.write_text("SOLVER= EULER\n", encoding="utf-8")
            return {
                "status": "PASS",
                "config_path": str(config_path),
                "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
                "mesh_path": str(local_mesh),
                "mesh_sha256": hashlib.sha256(local_mesh.read_bytes()).hexdigest(),
                "run_scope": {"production_eligible": False},
            }

        def run_project_pilot(*args: object, **kwargs: object) -> dict[str, object]:
            del args, kwargs
            calls.append("su2")
            run_dir = output / "su2"
            visualization = run_dir / "connection_solution.vtu"
            visualization.write_text(
                """<?xml version="1.0"?>
<VTKFile type="UnstructuredGrid" version="0.1" byte_order="LittleEndian">
  <UnstructuredGrid><Piece NumberOfPoints="4" NumberOfCells="1">
    <PointData><DataArray type="Float32" Name="Density" format="ascii">1 1 1 1</DataArray></PointData>
    <Points><DataArray type="Float32" NumberOfComponents="3" format="ascii">0 0 0 1 0 0 0 1 0 0 0 1</DataArray></Points>
    <Cells><DataArray type="Int32" Name="connectivity" format="ascii">0 1 2 3</DataArray><DataArray type="Int32" Name="offsets" format="ascii">4</DataArray><DataArray type="UInt8" Name="types" format="ascii">10</DataArray></Cells>
  </Piece></UnstructuredGrid>
</VTKFile>
""",
                encoding="utf-8",
            )
            validation = pipeline_module.validate_visualization_file(
                visualization, expected_points=4, expected_cells=1
            )
            return {
                "status": "PASS",
                "return_code": 0,
                "iterations": 5,
                "start_time": "2026-08-01T00:00:00Z",
                "end_time": "2026-08-01T00:00:01Z",
                "visualization_file": str(visualization),
                "visualization_validation": validation,
                "production_eligible": False,
                "boundary_mode_frozen": False,
            }

        def inspect_manifest(*args: object, **kwargs: object) -> dict[str, object]:
            del args, kwargs
            calls.append("pvbatch")
            pv_dir = output / "paraview"
            pv_dir.mkdir(parents=True, exist_ok=True)
            paths = (
                pv_dir / "solution_inventory.json",
                pv_dir / "smoke_postprocess.json",
                pv_dir / "smoke_integral.csv",
            )
            for path in paths:
                path.write_text(
                    "{}\n"
                    if path.suffix == ".json"
                    else "name,value\nDensity,1.0\n",
                    encoding="utf-8",
                )
            return {
                "status": "PASS",
                "pvbatch_path": str(pvbatch_executable),
                "paraview_version": "6.2.0",
                "commands": [{"command": [str(pvbatch_executable)], "return_code": 0}],
                "input_solution_path": str(output / "su2" / "connection_solution.vtu"),
                "dataset_type": "vtkUnstructuredGrid",
                "number_of_points": 4,
                "number_of_cells": 1,
                "detected_arrays": {"point": ["Density"], "cell": [], "field": []},
                "output_files": [
                    {
                        "path": str(path),
                        "size_bytes": path.stat().st_size,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                    for path in paths
                ],
            }

        su2_instance = mock.Mock()
        su2_instance.run_project_pilot_case.side_effect = run_project_pilot
        paraview_instance = mock.Mock()
        paraview_instance.inspect_manifest.side_effect = inspect_manifest
        with (
            mock.patch(
                "cfdpipe.pipeline.build_topology_smoke_mesh",
                side_effect=build_mesh,
            ),
            mock.patch(
                "cfdpipe.pipeline.prepare_project_supersonic_pilot_case",
                side_effect=prepare_case,
            ),
            mock.patch("cfdpipe.pipeline.SU2Bridge", return_value=su2_instance),
            mock.patch(
                "cfdpipe.pipeline.ParaViewBridge", return_value=paraview_instance
            ),
        ):
            result = pipeline_module.run_real_connection(
                step_path=self.step,
                project_path=self.project,
                cases_path=self.cases,
                markers_path=self.markers,
                case_id="M50_H21_A8_B0",
                mesh_level="smoke",
                nproc=1,
                max_iterations=5,
                output_directory=output,
                allowed_output_root=output,
                trusted_repository_root=self.root,
                toolchain=toolchain,
                paraview_script_directory=scripts,
                timeout_seconds=300,
                live_output=False,
            )

        self.assertEqual(calls, ["gmsh", "config", "su2", "pvbatch"])
        self.assertEqual(result.report["overall"], "PASS")
        for key in REPORT_KEYS:
            self.assertEqual(result.report[key]["status"], "PASS")
        self.assertFalse(result.report["metadata"]["production_eligible"])
        self.assertFalse(
            result.report["metadata"]["constraints"][
                "rear_outlet_boundary_frozen"
            ]
        )
        self.assertEqual(
            result.report["metadata"]["project_readiness"],
            "BLOCKED_PENDING_PHYSICAL_BOUNDARY_VALIDATION",
        )

    def test_postprocess_measurement_is_not_a_gmsh_or_su2_marker(self) -> None:
        self._write_read_only_step()
        _write_markers(self.markers)

        inputs = pipeline_module.load_real_connection_inputs(
            step_path=self.step,
            project_path=self.project,
            cases_path=self.cases,
            markers_path=self.markers,
            case_id="M50_H21_A8_B0",
            trusted_repository_root=self.root,
        )

        gmsh_mapping = pipeline_module._project_gmsh_marker_mapping(inputs)
        required_mesh_markers = pipeline_module._project_required_mesh_markers(inputs)

        self.assertNotIn("turning_section_outlet", gmsh_mapping)
        self.assertNotIn("turning_section_outlet", required_mesh_markers)
        self.assertIn("fluid", gmsh_mapping)
        self.assertEqual(
            required_mesh_markers,
            [
                "front_conical_surface",
                "rear_outlet_1",
                "rear_outlet_2",
                "vehicle_internal_and_external_walls",
            ],
        )

    def test_gmsh_failure_preserves_step_and_stops_all_downstream_bridges(self) -> None:
        self._write_read_only_step()
        _write_markers(self.markers)
        original_step = self.step.read_bytes()
        output = self.root / "runs" / "real_connection"
        scripts = self.root / "scripts" / "paraview"
        scripts.mkdir(parents=True)
        toolchain = mock.Mock()
        toolchain.allow_mnt_c_executables = False

        with (
            mock.patch(
                "cfdpipe.pipeline.build_topology_smoke_mesh",
                side_effect=RuntimeError(
                    "Gmsh import failed with original diagnostic"
                ),
            ) as topology_mesh,
            mock.patch("cfdpipe.pipeline.SU2Bridge") as su2_bridge,
            mock.patch("cfdpipe.pipeline.ParaViewBridge") as paraview_bridge,
        ):
            with self.assertRaises(pipeline_module.RealConnectionError):
                pipeline_module.run_real_connection(
                    step_path=self.step,
                    project_path=self.project,
                    cases_path=self.cases,
                    markers_path=self.markers,
                    case_id="M50_H21_A8_B0",
                    mesh_level="smoke",
                    nproc=1,
                    max_iterations=5,
                    output_directory=output,
                    allowed_output_root=output,
                    trusted_repository_root=self.root,
                    toolchain=toolchain,
                    paraview_script_directory=scripts,
                    timeout_seconds=300,
                    live_output=False,
                )

        self.assertEqual(self.step.read_bytes(), original_step)
        topology_mesh.assert_called_once()
        su2_bridge.assert_not_called()
        paraview_bridge.assert_not_called()
        self.assertEqual(toolchain.mock_calls, [])
        report = json.loads(
            (output / "real_connection_report.json").read_text(encoding="utf-8")
        )
        self.assertEqual(report["step_to_gmsh"]["status"], "FAIL")
        for key in REPORT_KEYS[1:]:
            self.assertEqual(report[key]["status"], "FAIL")
        gmsh_manifest = json.loads(
            (output / "mesh" / "gmsh_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(gmsh_manifest["status"], "FAIL")
        self.assertEqual(
            gmsh_manifest["error"]["message"],
            "Gmsh import failed with original diagnostic",
        )


class RealConnectionArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary_directory = tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT)
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()

    def test_previous_run_is_moved_but_geometry_evidence_is_preserved(self) -> None:
        mesh = self.root / "mesh"
        mesh.mkdir()
        (mesh / "mesh.su2").write_text("old mesh evidence\n", encoding="utf-8")
        (self.root / "real_connection_report.json").write_text(
            "{}\n", encoding="utf-8"
        )
        (self.root / "real-connection-1-deadbeef00.json").write_text(
            "{}\n", encoding="utf-8"
        )
        repair = self.root / "geometry_repair"
        repair.mkdir()
        (repair / "repair_manifest.json").write_text(
            "{}\n", encoding="utf-8"
        )

        archive = pipeline_module._archive_previous_real_run(self.root)

        self.assertIsNotNone(archive)
        assert archive is not None
        self.assertFalse(mesh.exists())
        self.assertFalse((self.root / "real_connection_report.json").exists())
        self.assertTrue((archive / "mesh" / "mesh.su2").is_file())
        self.assertTrue((archive / "real_connection_report.json").is_file())
        self.assertTrue((archive / "archive_manifest.json").is_file())
        self.assertTrue((repair / "repair_manifest.json").is_file())
        self.assertIsNone(pipeline_module._archive_previous_real_run(self.root))

    def test_reparse_archive_parent_is_rejected_before_any_move(self) -> None:
        mesh = self.root / "mesh"
        mesh.mkdir()
        (mesh / "mesh.su2").write_text("old mesh evidence\n", encoding="utf-8")
        archive_parent = self.root / "run_archive"
        archive_parent.mkdir()
        original_is_reparse = pipeline_module._is_reparse

        with mock.patch(
            "cfdpipe.pipeline._is_reparse",
            side_effect=lambda path: (
                Path(path) == archive_parent or original_is_reparse(Path(path))
            ),
        ):
            with self.assertRaisesRegex(ValueError, "archive parent is unsafe"):
                pipeline_module._archive_previous_real_run(self.root)

        self.assertTrue((mesh / "mesh.su2").is_file())

    def test_partial_archive_move_is_rolled_back(self) -> None:
        input_directory = self.root / "input"
        input_directory.mkdir()
        (input_directory / "input.json").write_text("{}\n", encoding="utf-8")
        mesh = self.root / "mesh"
        mesh.mkdir()
        (mesh / "mesh.su2").write_text("old mesh evidence\n", encoding="utf-8")
        original_replace = Path.replace

        def fail_second_move(path: Path, destination: Path):
            if path == mesh:
                raise OSError("archive move sentinel")
            return original_replace(path, destination)

        with mock.patch.object(Path, "replace", fail_second_move):
            with self.assertRaisesRegex(OSError, "archive move sentinel"):
                pipeline_module._archive_previous_real_run(self.root)

        self.assertTrue((input_directory / "input.json").is_file())
        self.assertTrue((mesh / "mesh.su2").is_file())
        archive_parent = self.root / "run_archive"
        self.assertFalse(any(archive_parent.iterdir()))


if __name__ == "__main__":
    unittest.main()
