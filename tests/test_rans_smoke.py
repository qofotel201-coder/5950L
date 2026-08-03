from __future__ import annotations

import copy
import csv
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import tomllib
import unittest

from cfdpipe.atmosphere import us_standard_atmosphere_1976
from cfdpipe.bridges import su2_bridge
from cfdpipe.bridges.su2_bridge import (
    SU2Reference,
    render_project_rans_smoke_config,
)
from cfdpipe.outlet_diagnostics import SU2TopologyMesh
from cfdpipe.rans_diagnostics import (
    RANSDiagnosticError,
    summarize_measurement_slice,
    summarize_rans_history,
    summarize_wall_yplus,
)
from cfdpipe.rans_smoke_pipeline import (
    RANSSmokePipelineError,
    _validate_binary_restart,
    _validate_regenerated_mesh_lineage,
    _validated_restart_source,
    _validate_rans_config,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _reference() -> SU2Reference:
    supported = {
        *su2_bridge._REQUIRED_SYNTAX_TOKENS,
        *su2_bridge._PROJECT_PILOT_SYNTAX_TOKENS,
        *su2_bridge._PROJECT_DIAGNOSTIC_SYNTAX_TOKENS,
        *su2_bridge._PROJECT_RANS_SYNTAX_TOKENS,
        "INNER_ITER",
        "PARAVIEW",
    }
    return SU2Reference(
        path="installed.zip::configFlow.cfg",
        sha256="a" * 64,
        content="SOLVER= EULER\n",
        iteration_key="INNER_ITER",
        paraview_output="PARAVIEW",
        syntax_evidence=(),
        supported_tokens=tuple(sorted(supported)),
    )


class RANSSmokeTests(unittest.TestCase):
    def test_regenerated_mesh_lineage_requires_all_frozen_inputs(self) -> None:
        hashes = {
            name: hashlib.sha256(name.encode("ascii")).hexdigest()
            for name in (
                "project",
                "cases",
                "markers",
                "topology",
                "step",
                "brep",
                "smoke",
                "trial",
            )
        }
        input_records = {
            "project": {"sha256": hashes["project"]},
            "cases": {"sha256": hashes["cases"]},
            "markers": {"sha256": hashes["markers"]},
            "topology_smoke_config": {"sha256": hashes["topology"]},
            "step": {"sha256": hashes["step"]},
            "pipeline_geometry": {"sha256": hashes["brep"]},
        }
        source = {"sha256": hashes["brep"], "read_only": True}
        manifest = {
            "source_unchanged": True,
            "source_before": source,
            "source_after": dict(source),
            "normalized_config": {
                "provenance": {
                    "project_sha256": hashes["project"],
                    "cases_sha256": hashes["cases"],
                    "markers_sha256": hashes["markers"],
                    "topology_smoke_sha256": hashes["topology"],
                    "pipeline_brep_sha256": hashes["brep"],
                }
            },
            "run_evidence": {
                "downstream_programs_called": False,
                "configuration_files": {
                    "smoke": {"sha256": hashes["smoke"]},
                    "schedule": {"sha256": hashes["trial"]},
                    "topology_smoke": {"sha256": hashes["topology"]},
                },
                "input_records": {
                    name: {"sha256": value["sha256"]}
                    for name, value in input_records.items()
                },
            },
        }
        provenance = {
            "boundary_layer_smoke_sha256": hashes["smoke"],
            "boundary_layer_trial_sha256": hashes["trial"],
            "topology_smoke_sha256": hashes["topology"],
        }

        _validate_regenerated_mesh_lineage(
            manifest, provenance=provenance, input_records=input_records
        )
        tampered = copy.deepcopy(manifest)
        tampered["run_evidence"]["input_records"]["pipeline_geometry"][
            "sha256"
        ] = "0" * 64
        with self.assertRaisesRegex(RANSSmokePipelineError, "input lineage"):
            _validate_regenerated_mesh_lineage(
                tampered, provenance=provenance, input_records=input_records
            )

    @staticmethod
    def _write_restart(path: Path, *, point_count: int = 2) -> None:
        fields = (
            "x",
            "y",
            "z",
            "Density",
            "Momentum_x",
            "Momentum_y",
            "Momentum_z",
            "Energy",
            "Turb_Kin_Energy",
            "Omega",
        )
        payload = bytearray(struct.pack("<5i", 535532, len(fields), point_count, 0, 0))
        for field in fields:
            encoded = field.encode("ascii")
            payload.extend(encoded + b"\0" * (33 - len(encoded)))
        payload.extend(struct.pack(f"<{point_count * len(fields)}d", *([1.0] * (point_count * len(fields)))))
        path.write_bytes(payload)

    def test_binary_restart_validation_checks_layout_points_and_finiteness(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            restart = Path(raw) / "restart.dat"
            self._write_restart(restart)
            validation = _validate_binary_restart(restart, expected_points=2)
            self.assertEqual(validation["magic"], 535532)
            self.assertEqual(validation["point_count"], 2)
            self.assertTrue(validation["finite"])
            with self.assertRaisesRegex(RANSSmokePipelineError, "point counts"):
                _validate_binary_restart(restart, expected_points=3)
            content = bytearray(restart.read_bytes())
            content[-8:] = struct.pack("<d", float("nan"))
            restart.write_bytes(content)
            with self.assertRaisesRegex(RANSSmokePipelineError, "NaN or Inf"):
                _validate_binary_restart(restart, expected_points=2)

    def test_restart_source_requires_explicit_pass_report_and_hash(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run = root / "runs" / "rans_smoke" / "source"
            su2 = run / "su2"
            su2.mkdir(parents=True)
            restart = su2 / "rans_solution_restart.dat"
            self._write_restart(restart)
            restart_sha = hashlib.sha256(restart.read_bytes()).hexdigest()
            config = su2 / "case.cfg"
            config.write_text(
                "\n".join(
                    (
                        "SOLVER= RANS",
                        "KIND_TURB_MODEL= SST",
                        "MUSCL_FLOW= NO",
                        "MUSCL_TURB= NO",
                        "MARKER_SUPERSONIC_OUTLET= ( rear_outlet_1, rear_outlet_2 )",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            mesh_sha = "a" * 64
            manifest = {
                "status": "PASS",
                "solver_model": "RANS",
                "turbulence_model": "SST",
                "mesh_sha256": mesh_sha,
                "return_code": 0,
                "timed_out": False,
                "fatal_matches": [],
                "iterations": 200,
                "restart_file": str(restart.resolve()),
                "restart_sha256": restart_sha,
                "config_path": str(config.resolve()),
                "preparation": {
                    "selected_case": {"case_id": "M50_H21_A8_B0"},
                    "nproc": 1,
                    "gpu_enabled": False,
                    "boundary_mode_frozen": False,
                    "outlet_pressure_or_backpressure_set": False,
                },
            }
            manifest_path = su2 / "run_manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            report = {
                "status": "PASS",
                "startup_gate": "PASS",
                "yplus_gate": "PASS",
                "diagnostic_only": True,
                "production_eligible": False,
                "su2": {"restart_sha256": restart_sha, "mesh_sha256": mesh_sha},
            }
            (run / "rans_smoke_report.json").write_text(
                json.dumps(report), encoding="utf-8"
            )
            record = _validated_restart_source(
                manifest_path,
                repository_root=root,
                expected_mesh_sha256=mesh_sha,
                expected_case_id="M50_H21_A8_B0",
            )
            self.assertEqual(record["restart_sha256"], restart_sha)
            restart.write_bytes(restart.read_bytes() + b"x")
            with self.assertRaisesRegex(RANSSmokePipelineError, "SHA256"):
                _validated_restart_source(
                    manifest_path,
                    repository_root=root,
                    expected_mesh_sha256=mesh_sha,
                    expected_case_id="M50_H21_A8_B0",
                )

    @staticmethod
    def _repository_config() -> tuple[dict[str, object], dict[str, object]]:
        project = tomllib.loads(
            (REPOSITORY_ROOT / "config" / "project.toml").read_text(
                encoding="utf-8"
            )
        )
        document = tomllib.loads(
            (REPOSITORY_ROOT / "config" / "rans_smoke.toml").read_text(
                encoding="utf-8"
            )
        )
        return project, document

    def test_repository_rans_config_matches_project(self) -> None:
        project, document = self._repository_config()
        normalized = _validate_rans_config(document, project=project, max_iterations=20)
        self.assertEqual(normalized["scope"]["nproc"], 1)
        self.assertFalse(normalized["scope"]["gpu_enabled"])
        self.assertEqual(normalized["yplus"]["maximum"], 1.0)

    def test_rans_config_rejects_non_smoke_mesh_level(self) -> None:
        project, document = self._repository_config()
        candidate = copy.deepcopy(document)
        candidate["scope"]["mesh_level"] = "medium"
        with self.assertRaisesRegex(
            RANSSmokePipelineError, "bounded serial SST"
        ):
            _validate_rans_config(candidate, project=project, max_iterations=20)

    def test_rans_config_rejects_3d_element_limit_above_gate(self) -> None:
        project, document = self._repository_config()
        candidate = copy.deepcopy(document)
        candidate["scope"]["maximum_3d_elements"] = 300_001
        with self.assertRaisesRegex(
            RANSSmokePipelineError, "bounded serial SST"
        ):
            _validate_rans_config(candidate, project=project, max_iterations=20)

    def test_rans_config_rejects_unapproved_rear_outlet_mode(self) -> None:
        project, document = self._repository_config()
        candidate = copy.deepcopy(document)
        candidate["boundary_contract"]["rear_outlet_mode"] = "PRESSURE_OUTLET"
        with self.assertRaisesRegex(
            RANSSmokePipelineError, "boundary contract"
        ):
            _validate_rans_config(candidate, project=project, max_iterations=20)

    def test_rans_config_requires_every_iteration_output(self) -> None:
        project, document = self._repository_config()
        candidate = copy.deepcopy(document)
        candidate["output"]["write_frequency"] = 5
        with self.assertRaisesRegex(
            RANSSmokePipelineError, "output contract"
        ):
            _validate_rans_config(candidate, project=project, max_iterations=20)

    def test_rans_renderer_is_first_order_no_slip_adiabatic(self) -> None:
        atmosphere = us_standard_atmosphere_1976(21_000.0)
        reference = _reference()
        rendered, mapping = render_project_rans_smoke_config(
            reference,
            {
                "mach": 5.0,
                "altitude_km": 21.0,
                "alpha_deg": 8.0,
                "beta_deg": 0.0,
            },
            atmosphere,
            reference_area_m2=0.2123716634,
            reference_length_m=5.2,
            moment_origin_m=(2.6, 0.0, 0.0),
            farfield_marker="front_conical_surface",
            wall_marker="vehicle_internal_and_external_walls",
            rear_outlet_markers=("rear_outlet_1", "rear_outlet_2"),
            max_iterations=20,
            cfl_number=0.05,
            gradient_method="GREEN_GAUSS",
            flow_convective_method="ROE",
            turbulence_convective_method="SCALAR_UPWIND",
            flow_time_discretization="EULER_IMPLICIT",
            turbulence_time_discretization="EULER_IMPLICIT",
            linear_solver="FGMRES",
            linear_solver_preconditioner="ILU",
            linear_solver_error=0.001,
            linear_solver_iterations=10,
            freestream_turbulence_intensity=0.01,
            freestream_turbulent_to_laminar_viscosity_ratio=10.0,
            volume_fields=(
                "COORDINATES",
                "SOLUTION",
                "PRIMITIVE",
            ),
            output_files=("RESTART", "PARAVIEW", "SURFACE_PARAVIEW"),
        )
        self.assertIn("SOLVER= RANS", rendered)
        self.assertIn("KIND_TURB_MODEL= SST", rendered)
        self.assertIn(
            "MARKER_HEATFLUX= ( vehicle_internal_and_external_walls, 0.0 )",
            rendered,
        )
        self.assertIn("MUSCL_FLOW= NO", rendered)
        self.assertIn("MUSCL_TURB= NO", rendered)
        self.assertIn("VOLUME_OUTPUT= COORDINATES, SOLUTION, PRIMITIVE", rendered)
        self.assertNotIn("SKIN_FRICTION-X", rendered)
        self.assertFalse(
            {"SKIN_FRICTION-X", "SKIN_FRICTION-Y", "SKIN_FRICTION-Z"}
            & set(reference.supported_tokens)
        )
        self.assertNotIn("MARKER_EULER=", rendered)
        self.assertNotIn("MARKER_OUTLET=", rendered)
        self.assertNotIn("BACK_PRESSURE=", rendered)
        self.assertNotIn("ENABLE_CUDA=", rendered)
        self.assertEqual(mapping.su2_aoa_deg, 0.0)
        self.assertAlmostEqual(mapping.su2_sideslip_angle_deg, 8.0)

    def _mesh(self) -> SU2TopologyMesh:
        return SU2TopologyMesh(
            path=Path("fixture.su2"),
            sha256="b" * 64,
            points={
                0: (0.0, 0.0, 0.0),
                1: (1.0, 0.0, 0.0),
                2: (0.0, 1.0, 0.0),
                3: (0.0, 0.0, 1.0),
            },
            tetrahedra=((0, 1, 2, 3),),
            prisms=(),
            markers={
                "wall": ((0, 1, 2),),
                "far": ((0, 1, 3), (0, 2, 3), (1, 2, 3)),
            },
        )

    def test_wall_yplus_pass_and_fail(self) -> None:
        points = tuple(self._mesh().points[index] for index in range(4))
        passed = summarize_wall_yplus(
            self._mesh(),
            solution_points=points,
            yplus=(0.2, 0.4, 0.8, 0.0),
            wall_marker="wall",
            target=0.7,
            maximum=1.0,
        )
        self.assertEqual(passed["status"], "PASS")
        self.assertEqual(passed["maximum"], 0.8)
        failed = summarize_wall_yplus(
            self._mesh(),
            solution_points=points,
            yplus=(0.2, 1.2, 0.8, 0.0),
            wall_marker="wall",
            target=0.7,
            maximum=1.0,
        )
        self.assertEqual(failed["status"], "FAIL")
        self.assertGreater(failed["above_maximum_area_fraction"], 0.0)
        equality = summarize_wall_yplus(
            self._mesh(),
            solution_points=points,
            yplus=(0.2, 1.0, 0.8, 0.0),
            wall_marker="wall",
            target=0.7,
            maximum=1.0,
        )
        self.assertEqual(equality["status"], "FAIL")
        self.assertEqual(equality["at_or_above_maximum_point_count"], 1)

    def test_wall_yplus_all_zero_is_rejected(self) -> None:
        points = tuple(self._mesh().points[index] for index in range(4))
        with self.assertRaisesRegex(RANSDiagnosticError, "all wall Y_Plus"):
            summarize_wall_yplus(
                self._mesh(),
                solution_points=points,
                yplus=(0.0, 0.0, 0.0, 0.0),
                wall_marker="wall",
                target=0.7,
                maximum=1.0,
            )

    def test_history_requires_and_scales_six_components(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "history.csv"
            headers = [
                "Inner_Iter",
                "rms[Rho]",
                "CFx",
                "CFy",
                "CFz",
                "CMx",
                "CMy",
                "CMz",
            ]
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=headers)
                writer.writeheader()
                writer.writerow(
                    {
                        "Inner_Iter": 0,
                        "rms[Rho]": -1.0,
                        "CFx": 1.0,
                        "CFy": 2.0,
                        "CFz": 3.0,
                        "CMx": 4.0,
                        "CMy": 5.0,
                        "CMz": 6.0,
                    }
                )
            summary = summarize_rans_history(
                path,
                freestream_density_kg_m3=2.0,
                freestream_velocity_m_s=3.0,
                reference_area_m2=4.0,
                reference_length_m=5.0,
                moment_origin_m=(2.6, 0.0, 0.0),
            )
            self.assertEqual(summary["dimensional_loads"]["Fx_N"], 36.0)
            self.assertEqual(summary["dimensional_loads"]["Mx_N_m"], 720.0)
            self.assertFalse(summary["convergence_claimed"])

    def test_measurement_uses_conservative_momentum_and_splits_backflow(self) -> None:
        summary = summarize_measurement_slice(
            points=((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
            cells=((0, 1, 2),),
            density=(100.0, 100.0, 100.0),
            momentum=((-1.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
            mach=(0.3, 0.3, 0.3),
            pressure=(100000.0, 100000.0, 100000.0),
            temperature=(300.0, 300.0, 300.0),
            name="section",
            plane_coordinate_m=0.0,
            normal_unit=(1.0, 0.0, 0.0),
            bounds_m=(0.0, 0.0, 0.0, 0.0, 1.0, 1.0),
            expected_area_m2=0.5,
            maximum_relative_area_error=0.01,
            minimum_selected_cell_count=1,
            specific_heat_ratio=1.4,
            freestream_static_pressure_pa=100000.0,
            freestream_mach=0.3,
        )

        self.assertEqual(summary["status"], "PASS")
        self.assertEqual(summary["mass_flux_source"], "Momentum dot confirmed unit normal")
        self.assertGreater(summary["forward_mass_flow_kg_s"], 0.0)
        self.assertGreater(summary["reverse_mass_flow_kg_s"], 0.0)
        self.assertAlmostEqual(
            summary["forward_mass_flow_kg_s"]
            - summary["reverse_mass_flow_kg_s"],
            summary["net_mass_flow_kg_s"],
            places=12,
        )
        self.assertAlmostEqual(summary["mass_weighted_mach"], 0.3, places=12)

    def test_measurement_requires_one_matching_connected_component(self) -> None:
        common = {
            "density": (1.0,) * 6,
            "momentum": ((1.0, 0.0, 0.0),) * 6,
            "mach": (0.3,) * 6,
            "pressure": (100000.0,) * 6,
            "temperature": (300.0,) * 6,
            "name": "section",
            "plane_coordinate_m": 0.0,
            "normal_unit": (1.0, 0.0, 0.0),
            "bounds_m": (0.0, 0.0, 0.0, 0.0, 3.0, 1.0),
            "expected_area_m2": 0.5,
            "maximum_relative_area_error": 0.01,
            "minimum_selected_cell_count": 1,
            "specific_heat_ratio": 1.4,
            "freestream_static_pressure_pa": 100000.0,
            "freestream_mach": 0.3,
        }
        with self.assertRaisesRegex(RANSDiagnosticError, "match uniquely"):
            summarize_measurement_slice(
                points=(
                    (0.0, 0.0, 0.0),
                    (0.0, 1.0, 0.0),
                    (0.0, 0.0, 1.0),
                    (0.0, 2.0, 0.0),
                    (0.0, 3.0, 0.0),
                    (0.0, 2.0, 1.0),
                ),
                cells=((0, 1, 2), (3, 4, 5)),
                **common,
            )

    def test_measurement_zero_flux_vertex_has_zero_reverse_contribution(self) -> None:
        summary = summarize_measurement_slice(
            points=((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
            cells=((0, 1, 2),),
            density=(1.0, 1.0, 1.0),
            momentum=((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
            mach=(0.3, 0.3, 0.3),
            pressure=(100000.0,) * 3,
            temperature=(300.0,) * 3,
            name="section",
            plane_coordinate_m=0.0,
            normal_unit=(1.0, 0.0, 0.0),
            bounds_m=(0.0, 0.0, 0.0, 0.0, 1.0, 1.0),
            expected_area_m2=0.5,
            maximum_relative_area_error=0.01,
            minimum_selected_cell_count=1,
            specific_heat_ratio=1.4,
            freestream_static_pressure_pa=100000.0,
            freestream_mach=0.3,
        )
        self.assertEqual(summary["reverse_mass_flow_kg_s"], 0.0)
        self.assertAlmostEqual(
            summary["forward_mass_flow_kg_s"],
            summary["net_mass_flow_kg_s"],
            places=12,
        )

    def test_measurement_rejects_nonfinite_or_inverted_contract(self) -> None:
        with self.assertRaisesRegex(RANSDiagnosticError, "bounds are inverted"):
            summarize_measurement_slice(
                points=((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
                cells=((0, 1, 2),),
                density=(1.0, 1.0, 1.0),
                momentum=((1.0, 0.0, 0.0),) * 3,
                mach=(0.3, 0.3, 0.3),
                pressure=(100000.0,) * 3,
                temperature=(300.0,) * 3,
                name="section",
                plane_coordinate_m=0.0,
                normal_unit=(1.0, 0.0, 0.0),
                bounds_m=(0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
                expected_area_m2=0.5,
                maximum_relative_area_error=0.01,
                minimum_selected_cell_count=1,
                specific_heat_ratio=1.4,
                freestream_static_pressure_pa=100000.0,
                freestream_mach=0.3,
            )


if __name__ == "__main__":
    unittest.main()
