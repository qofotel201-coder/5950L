from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from cfdpipe.bridges.su2_bridge import (
    SU2Bridge,
    SU2BridgeError,
    SU2Reference,
    classify_2d_quality_nan,
    parse_history_iterations,
    parse_su2_freestream_log,
    prepare_project_supersonic_pilot_case,
    project_angles_to_su2,
    render_project_outlet_diagnostic_config,
    render_project_rans_restart_config,
    render_project_supersonic_pilot_config,
    render_smoke_config,
    scan_fatal_output,
    select_new_visualization,
    snapshot_visualizations,
    validate_visualization_file,
)
from cfdpipe.atmosphere import us_standard_atmosphere_1976
from cfdpipe.bridges.gmsh_bridge import validate_su2_mesh
from cfdpipe.process import (
    CommandExecutionError,
    CommandResult,
    CommandRunner,
    CommandTimeoutError,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONNECTION_OUTPUT = REPOSITORY_ROOT / "runs" / "connection"


class SU2BridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        CONNECTION_OUTPUT.mkdir(parents=True, exist_ok=True)
        self.temporary_directory = tempfile.TemporaryDirectory(
            dir=CONNECTION_OUTPUT
        )
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.run_directory = self.root / "su2"
        self.run_directory.mkdir()
        self.su2_cfd = str((self.root / "bin" / "SU2_CFD").resolve())
        self.mpiexec = str((self.root / "mpi" / "mpiexec").resolve())
        self.runner = mock.Mock()
        self.runner.run.return_value = object()

    def _write_mesh(self, name: str = "input.su2") -> Path:
        mesh = self.root / name
        mesh.write_text(
            "\n".join(
                (
                    "NDIME= 2",
                    "NELEM= 1",
                    "5 0 1 2 0",
                    "NPOIN= 3",
                    "0.0 0.0 0",
                    "1.0 0.0 1",
                    "0.0 1.0 2",
                    "NMARK= 1",
                    "MARKER_TAG= farfield",
                    "MARKER_ELEMS= 3",
                    "3 0 1",
                    "3 1 2",
                    "3 2 0",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        return mesh

    def _reference(self) -> SU2Reference:
        content = "SOLVER= EULER\nCONV_NUM_METHOD_FLOW= ROE\n"
        supported_tokens = (
            "SOLVER",
            "EULER",
            "MACH_NUMBER",
            "AOA",
            "SIDESLIP_ANGLE",
            "MARKER_FAR",
            "MARKER_EULER",
            "MARKER_SUPERSONIC_OUTLET",
            "FREESTREAM_PRESSURE",
            "FREESTREAM_TEMPERATURE",
            "REF_AREA",
            "REF_LENGTH",
            "REF_ORIGIN_MOMENT_X",
            "REF_ORIGIN_MOMENT_Y",
            "REF_ORIGIN_MOMENT_Z",
            "INNER_ITER",
            "CONV_RESIDUAL_MINVAL",
            "CONV_NUM_METHOD_FLOW",
            "CONV_STARTITER",
            "MESH_FILENAME",
            "CONV_FILENAME",
            "VOLUME_FILENAME",
            "RESTART_FILENAME",
            "TABULAR_FORMAT",
            "CSV",
            "HISTORY_OUTPUT",
            "OUTPUT_WRT_FREQ",
            "OUTPUT_FILES",
            "RESTART",
            "RMS_RES",
            "RESTART_SOL",
            "READ_BINARY_RESTART",
            "SOLUTION_FILENAME",
            "MUSCL_FLOW",
            "MUSCL_TURB",
            "YES",
            "SURFACE_FILENAME",
            "PARAVIEW",
        )
        return SU2Reference(
            path=str((self.root / "install" / "config_template.cfg").resolve()),
            sha256="a" * 64,
            content=content,
            iteration_key="INNER_ITER",
            paraview_output="PARAVIEW",
            syntax_evidence=(),
            supported_tokens=supported_tokens,
        )

    @staticmethod
    def _rans_base_config() -> str:
        return "\n".join(
            (
                "% CFDPIPE bounded real-project first-order SST RANS smoke",
                "% diagnostic_only=true; production_eligible=false",
                "SOLVER= RANS",
                "KIND_TURB_MODEL= SST",
                "FREESTREAM_PRESSURE= 4677.87605035934",
                "MARKER_FAR= ( front_conical_surface )",
                "MARKER_HEATFLUX= ( vehicle_internal_and_external_walls, 0.0 )",
                "MARKER_SUPERSONIC_OUTLET= ( rear_outlet_1, rear_outlet_2 )",
                "MUSCL_FLOW= NO",
                "MUSCL_TURB= NO",
                "INNER_ITER= 200",
                "CONV_STARTITER= 201",
                "MESH_FILENAME= mesh.su2",
                "CONV_FILENAME= history",
                "VOLUME_FILENAME= rans_solution",
                "SURFACE_FILENAME= rans_surface",
                "RESTART_FILENAME= rans_solution_restart",
                "OUTPUT_FILES= RESTART, PARAVIEW, SURFACE_PARAVIEW",
            )
        ) + "\n"

    def _write_ascii_vtu(
        self,
        *,
        point_value: str = "1.0",
        path: Path | None = None,
    ) -> Path:
        output = (
            self.run_directory / "connection_solution.vtu"
            if path is None
            else path
        )
        output.write_text(
            """<?xml version="1.0"?>
<VTKFile type="UnstructuredGrid" version="0.1" byte_order="LittleEndian">
  <UnstructuredGrid>
    <Piece NumberOfPoints="3" NumberOfCells="1">
      <PointData><DataArray type="Float32" Name="Density" format="ascii">{value} 1.0 1.0</DataArray></PointData>
      <Points><DataArray type="Float32" NumberOfComponents="3" format="ascii">0 0 0 1 0 0 0 1 0</DataArray></Points>
      <Cells>
        <DataArray type="Int32" Name="connectivity" format="ascii">0 1 2</DataArray>
        <DataArray type="Int32" Name="offsets" format="ascii">3</DataArray>
        <DataArray type="UInt8" Name="types" format="ascii">5</DataArray>
      </Cells>
    </Piece>
  </UnstructuredGrid>
</VTKFile>
""".format(value=point_value),
            encoding="utf-8",
        )
        return output

    def _result(
        self,
        *,
        args: tuple[str, ...] = ("smoke.cfg",),
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        timed_out: bool = False,
        name: str = "solver",
        directory: Path | None = None,
        executable: str | None = None,
    ) -> CommandResult:
        result_directory = self.run_directory if directory is None else directory
        stdout_log = result_directory / f"{name}.stdout.log"
        stderr_log = result_directory / f"{name}.stderr.log"
        metadata_log = result_directory / f"{name}.command.json"
        stdout_log.write_text(stdout, encoding="utf-8")
        stderr_log.write_text(stderr, encoding="utf-8")
        metadata_log.write_text("{}\n", encoding="utf-8")
        return CommandResult(
            executable=self.su2_cfd if executable is None else executable,
            args=args,
            cwd=str(result_directory),
            start_time="2026-08-01T00:00:00Z",
            end_time="2026-08-01T00:00:01Z",
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            metadata_log=metadata_log,
            dry_run=False,
            timed_out=timed_out,
        )

    def _version_result(self) -> CommandResult:
        return self._result(
            args=("--help",),
            stdout="SU2 v8.3.0 connection test\n",
            name="version",
        )

    def _patch_discovery(self, bridge: SU2Bridge):
        return (
            mock.patch(
                "cfdpipe.bridges.su2_bridge.discover_su2_reference",
                return_value=self._reference(),
            ),
            mock.patch.object(
                bridge,
                "probe_version",
                return_value=("8.3.0", self._version_result()),
            ),
        )

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_serial_run_calls_su2_directly(self) -> None:
        bridge = SU2Bridge(self.runner, self.su2_cfd)

        result = bridge.run(
            "case.cfg",
            nproc=1,
            cwd="run-dir",
            timeout=12.0,
            dry_run=True,
        )

        self.assertIs(result, self.runner.run.return_value)
        self.runner.run.assert_called_once_with(
            self.su2_cfd,
            args=["case.cfg"],
            cwd="run-dir",
            env=None,
            timeout=12.0,
            check=True,
            live_output=None,
            dry_run=True,
            name="su2-run",
        )

    def test_parallel_run_uses_mpiexec_only_for_multiple_processes(self) -> None:
        bridge = SU2Bridge(self.runner, self.su2_cfd, self.mpiexec)

        bridge.run("case.cfg", nproc=4)

        self.runner.run.assert_called_once_with(
            self.mpiexec,
            args=["-np", "4", self.su2_cfd, "case.cfg"],
            cwd=None,
            env=None,
            timeout=None,
            check=True,
            live_output=None,
            dry_run=False,
            name="su2-run",
        )

    def test_mpi_command_construction_does_not_launch_mpi(self) -> None:
        bridge = SU2Bridge(self.runner, self.su2_cfd, self.mpiexec)

        command = bridge.build_command("smoke.cfg", nproc=3)

        self.assertEqual(
            command,
            (self.mpiexec, ["-np", "3", self.su2_cfd, "smoke.cfg"]),
        )
        self.runner.run.assert_not_called()

    def test_run_su2_uses_relative_config_and_fixed_logs(self) -> None:
        config = self.run_directory / "smoke.cfg"
        config.write_text("SOLVER= EULER\n", encoding="utf-8")
        sentinel = object()
        self.runner.run.return_value = sentinel
        bridge = SU2Bridge(self.runner, self.su2_cfd)

        result = bridge.run_su2(
            config,
            self.run_directory,
            nproc=1,
            timeout_seconds=25,
            live_output=False,
        )

        self.assertIs(result, sentinel)
        self.runner.run.assert_called_once_with(
            self.su2_cfd,
            args=["smoke.cfg"],
            cwd=self.run_directory,
            timeout=25.0,
            check=False,
            live_output=False,
            dry_run=False,
            name="su2-cfd-smoke",
            stdout_log=self.run_directory / "solver.stdout.log",
            stderr_log=self.run_directory / "solver.stderr.log",
            metadata_log=self.run_directory / "solver.command.json",
        )

    def test_invalid_process_counts_stop_before_runner(self) -> None:
        bridge = SU2Bridge(self.runner, self.su2_cfd)

        for invalid in (0, -1):
            with self.subTest(nproc=invalid):
                with self.assertRaisesRegex(ValueError, "greater than zero"):
                    bridge.run("case.cfg", nproc=invalid)

        self.runner.run.assert_not_called()

    def test_parallel_run_requires_mpiexec(self) -> None:
        bridge = SU2Bridge(self.runner, self.su2_cfd)

        with self.assertRaisesRegex(ValueError, "mpirun or mpiexec is required"):
            bridge.run("case.cfg", nproc=2)

        self.runner.run.assert_not_called()

    def test_runner_failure_is_not_swallowed(self) -> None:
        bridge = SU2Bridge(self.runner, self.su2_cfd)
        failure = RuntimeError("original stderr")
        self.runner.run.side_effect = failure

        with self.assertRaisesRegex(RuntimeError, "original stderr"):
            bridge.run("case.cfg")

    def test_smoke_config_uses_relative_mesh_marker_and_five_iterations(self) -> None:
        reference = self._reference()
        rendered = render_smoke_config(reference, 5)

        self.assertIn("SOLVER= EULER", rendered)
        self.assertIn("MACH_NUMBER= 0.3", rendered)
        self.assertIn("AOA= 0.0", rendered)
        self.assertIn("SIDESLIP_ANGLE= 0.0", rendered)
        self.assertIn("MARKER_FAR= ( farfield )", rendered)
        self.assertIn("CONV_NUM_METHOD_FLOW= ROE", rendered)
        self.assertIn("INNER_ITER= 5", rendered)
        self.assertIn("CONV_STARTITER= 6", rendered)
        self.assertIn("CONV_RESIDUAL_MINVAL= -30", rendered)
        self.assertIn("MESH_FILENAME= smoke.su2", rendered)
        self.assertIn("CONV_FILENAME= history", rendered)
        self.assertIn("VOLUME_FILENAME= connection_solution", rendered)
        self.assertIn("OUTPUT_FILES= RESTART, PARAVIEW", rendered)
        self.assertNotIn(str(self.root), rendered)

        assignment_keys = {
            match.group(1)
            for line in rendered.splitlines()
            if (match := re.match(r"^([A-Z][A-Z0-9_]*)\s*=", line))
        }
        evidenced_keys = set(reference.supported_tokens)
        self.assertLessEqual(assignment_keys, evidenced_keys)
        for prohibited in (
            "KIND_TURB_MODEL",
            "INIT_OPTION",
            "FREESTREAM_OPTION",
            "FREESTREAM_PRESSURE",
            "FREESTREAM_TEMPERATURE",
            "FLUID_MODEL",
            "GAMMA_VALUE",
            "GAS_CONSTANT",
            "REF_DIMENSIONALIZATION",
            "NUM_METHOD_GRAD",
            "CFL_NUMBER",
            "CFL_ADAPT",
            "JST_SENSOR_COEFF",
            "TIME_DISCRE_FLOW",
            "LINEAR_SOLVER",
            "LINEAR_SOLVER_PREC",
            "LINEAR_SOLVER_ERROR",
            "LINEAR_SOLVER_ITER",
            "MESH_FORMAT",
        ):
            with self.subTest(prohibited=prohibited):
                self.assertNotIn(f"{prohibited}=", rendered)

    def test_rans_restart_renderer_makes_only_evidenced_second_order_changes(
        self,
    ) -> None:
        rendered = render_project_rans_restart_config(
            self._rans_base_config(),
            self._reference(),
            solution_filename="rans_solution_restart",
            iterations=300,
        )

        self.assertIn(
            "% CFDPIPE bounded real-project second-order SST RANS restart", rendered
        )
        self.assertNotIn("first-order SST RANS smoke", rendered)
        self.assertIn("RESTART_SOL= YES", rendered)
        self.assertIn("READ_BINARY_RESTART= YES", rendered)
        self.assertIn("SOLUTION_FILENAME= rans_solution_restart", rendered)
        self.assertIn("MUSCL_FLOW= YES", rendered)
        self.assertIn("MUSCL_TURB= YES", rendered)
        self.assertIn("INNER_ITER= 300", rendered)
        self.assertIn("CONV_STARTITER= 301", rendered)
        self.assertIn("VOLUME_FILENAME= rans_second_order", rendered)
        self.assertIn("SURFACE_FILENAME= rans_second_order_surface", rendered)
        self.assertIn("RESTART_FILENAME= rans_second_order_restart", rendered)
        self.assertIn("FREESTREAM_PRESSURE= 4677.87605035934", rendered)
        self.assertIn(
            "MARKER_SUPERSONIC_OUTLET= ( rear_outlet_1, rear_outlet_2 )",
            rendered,
        )
        for key in (
            "RESTART_SOL",
            "READ_BINARY_RESTART",
            "SOLUTION_FILENAME",
            "MUSCL_FLOW",
            "MUSCL_TURB",
            "INNER_ITER",
            "CONV_STARTITER",
            "VOLUME_FILENAME",
            "SURFACE_FILENAME",
            "RESTART_FILENAME",
        ):
            with self.subTest(key=key):
                self.assertEqual(
                    1,
                    len(re.findall(rf"(?m)^\s*{key}\s*=", rendered)),
                )
        self.assertNotRegex(rendered, r"(?i)[A-Z]:[\\/]")
        self.assertNotIn("MARKER_OUTLET=", rendered)
        self.assertNotIn("ENABLE_CUDA=", rendered)

    def test_rans_restart_renderer_requires_current_install_evidence(self) -> None:
        reference = self._reference()
        for missing_token in (
            "RESTART_SOL",
            "SOLUTION_FILENAME",
            "MUSCL_FLOW",
            "MUSCL_TURB",
            "YES",
        ):
            with self.subTest(missing_token=missing_token):
                reduced = SU2Reference(
                    path=reference.path,
                    sha256=reference.sha256,
                    content=reference.content,
                    iteration_key=reference.iteration_key,
                    paraview_output=reference.paraview_output,
                    syntax_evidence=reference.syntax_evidence,
                    supported_tokens=tuple(
                        token
                        for token in reference.supported_tokens
                        if token != missing_token
                    ),
                )
                with self.assertRaisesRegex(SU2BridgeError, missing_token):
                    render_project_rans_restart_config(
                        self._rans_base_config(),
                        reduced,
                        solution_filename="rans_solution_restart",
                        iterations=200,
                    )

    def test_rans_restart_renderer_rejects_nonlocal_stems_and_bad_iterations(
        self,
    ) -> None:
        for solution_filename in (
            "../restart",
            "subdir/restart",
            r"C:\run\restart",
            "restart.dat",
            " restart",
            "restart;command",
        ):
            with self.subTest(solution_filename=solution_filename):
                with self.assertRaisesRegex(ValueError, "local filename stem"):
                    render_project_rans_restart_config(
                        self._rans_base_config(),
                        self._reference(),
                        solution_filename=solution_filename,
                        iterations=200,
                    )
        for iterations in (19, 501, True, 20.0):
            with self.subTest(iterations=iterations):
                with self.assertRaises((TypeError, ValueError)):
                    render_project_rans_restart_config(
                        self._rans_base_config(),
                        self._reference(),
                        solution_filename="rans_solution_restart",
                        iterations=iterations,  # type: ignore[arg-type]
                    )

    def test_rans_restart_renderer_requires_verified_first_order_base(self) -> None:
        base = self._rans_base_config()
        invalid_cases = {
            "wrong solver": base.replace("SOLVER= RANS", "SOLVER= EULER"),
            "wrong turbulence": base.replace(
                "KIND_TURB_MODEL= SST", "KIND_TURB_MODEL= SA"
            ),
            "already second order": base.replace("MUSCL_FLOW= NO", "MUSCL_FLOW= YES"),
            "missing outlet": base.replace(
                "MARKER_SUPERSONIC_OUTLET= ( rear_outlet_1, rear_outlet_2 )\n", ""
            ),
            "duplicate assignment": base + "MUSCL_FLOW= NO\n",
        }
        for label, candidate in invalid_cases.items():
            with self.subTest(label=label):
                with self.assertRaises(SU2BridgeError):
                    render_project_rans_restart_config(
                        candidate,
                        self._reference(),
                        solution_filename="rans_solution_restart",
                        iterations=200,
                    )

    def test_rans_restart_renderer_rejects_pressure_gpu_shell_and_paths(self) -> None:
        base = self._rans_base_config()
        forbidden_lines = (
            "MARKER_OUTLET= ( rear_outlet_1, 1000.0 )",
            "OUTLET_PRESSURE= 1000.0",
            "BACK_PRESSURE= 1000.0",
            "ENABLE_CUDA= YES",
            "SHELL= true",
            r"MESH_FILENAME= C:\absolute\mesh.su2",
            "MESH_FILENAME= /absolute/mesh.su2",
            "SOLUTION_FILENAME= $(unsafe)",
        )
        for forbidden in forbidden_lines:
            with self.subTest(forbidden=forbidden):
                candidate = base
                if forbidden.startswith("MESH_FILENAME="):
                    candidate = re.sub(
                        r"(?m)^MESH_FILENAME=.*$", lambda _match: forbidden, candidate
                    )
                else:
                    candidate += forbidden + "\n"
                with self.assertRaisesRegex(
                    SU2BridgeError, "forbidden|absolute path|shell-related"
                ):
                    render_project_rans_restart_config(
                        candidate,
                        self._reference(),
                        solution_filename="rans_solution_restart",
                        iterations=200,
                    )

    def test_project_pilot_config_uses_case_markers_and_no_pressure(self) -> None:
        rendered = render_project_supersonic_pilot_config(
            self._reference(),
            {
                "case_id": "M50_H21_A8_B0",
                "mach": "5.0",
                "alpha_deg": "8",
                "beta_deg": "0",
            },
            farfield_marker="front_conical_surface",
            wall_marker="vehicle_internal_and_external_walls",
            rear_outlet_markers=("rear_outlet_1", "rear_outlet_2"),
            max_iterations=5,
        )

        self.assertIn("SOLVER= EULER", rendered)
        self.assertIn("MACH_NUMBER= 5.0", rendered)
        self.assertIn("AOA= 0", rendered)
        self.assertIn("SIDESLIP_ANGLE= 8", rendered)
        self.assertIn("MARKER_FAR= ( front_conical_surface )", rendered)
        self.assertIn(
            "MARKER_EULER= ( vehicle_internal_and_external_walls )", rendered
        )
        self.assertIn(
            "MARKER_SUPERSONIC_OUTLET= ( rear_outlet_1, rear_outlet_2 )",
            rendered,
        )
        self.assertIn("MESH_FILENAME= mesh.su2", rendered)
        assignments = {
            match.group(1)
            for line in rendered.splitlines()
            if (match := re.match(r"^([A-Z][A-Z0-9_]*)\s*=", line))
        }
        self.assertNotIn("MARKER_OUTLET", assignments)
        self.assertFalse(any("PRESSURE" in key for key in assignments))
        self.assertNotRegex(rendered, r"(?i)[A-Z]:[\\/]")

    def test_project_angle_mapping_preserves_project_direction(self) -> None:
        design = project_angles_to_su2(8.0, 0.0)
        self.assertAlmostEqual(design.su2_aoa_deg, 0.0, places=12)
        self.assertAlmostEqual(design.su2_sideslip_angle_deg, 8.0, places=12)
        self.assertLessEqual(design.maximum_component_error, 1.0e-12)
        self.assertGreater(design.project_unit_vector_xyz[1], 0.0)
        self.assertAlmostEqual(design.project_unit_vector_xyz[2], 0.0, places=12)

        combined = project_angles_to_su2(8.0, 2.0)
        for expected, actual in zip(
            combined.project_unit_vector_xyz,
            combined.su2_unit_vector_xyz,
            strict=True,
        ):
            self.assertAlmostEqual(expected, actual, places=12)
        self.assertNotAlmostEqual(combined.su2_aoa_deg, 8.0)
        self.assertNotAlmostEqual(combined.su2_sideslip_angle_deg, 2.0)

    def test_outlet_diagnostic_config_uses_atmosphere_references_and_no_outlet_pressure(self) -> None:
        atmosphere = us_standard_atmosphere_1976(21_000.0)
        rendered, mapping = render_project_outlet_diagnostic_config(
            self._reference(),
            {
                "case_id": "M50_H21_A8_B0",
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
            max_iterations=100,
        )

        self.assertAlmostEqual(mapping.su2_aoa_deg, 0.0, places=12)
        self.assertAlmostEqual(mapping.su2_sideslip_angle_deg, 8.0, places=12)
        self.assertIn("FREESTREAM_PRESSURE= 4677.", rendered)
        self.assertIn("FREESTREAM_TEMPERATURE= 217.65", rendered)
        self.assertIn("REF_AREA= 0.2123716634", rendered)
        self.assertIn("REF_LENGTH= 5.2", rendered)
        self.assertIn("REF_ORIGIN_MOMENT_X= 2.6", rendered)
        self.assertIn("INNER_ITER= 100", rendered)
        assignments = {
            match.group(1)
            for line in rendered.splitlines()
            if (match := re.match(r"^([A-Z][A-Z0-9_]*)\s*=", line))
        }
        self.assertNotIn("MARKER_OUTLET", assignments)
        self.assertNotIn("OUTLET_PRESSURE", assignments)
        self.assertNotIn("BACK_PRESSURE", assignments)

        with self.assertRaisesRegex(SU2BridgeError, "altitude"):
            render_project_outlet_diagnostic_config(
                self._reference(),
                {
                    "mach": 5.0,
                    "altitude_km": 20.0,
                    "alpha_deg": 8.0,
                    "beta_deg": 0.0,
                },
                atmosphere,
                reference_area_m2=0.2123716634,
                reference_length_m=5.2,
                moment_origin_m=(2.6, 0.0, 0.0),
                farfield_marker="farfield",
                wall_marker="wall",
                rear_outlet_markers=("rear_1", "rear_2"),
                max_iterations=100,
            )

    def test_project_pilot_config_requires_current_install_syntax_evidence(self) -> None:
        reference = self._reference()
        reference = SU2Reference(
            path=reference.path,
            sha256=reference.sha256,
            content=reference.content,
            iteration_key=reference.iteration_key,
            paraview_output=reference.paraview_output,
            syntax_evidence=reference.syntax_evidence,
            supported_tokens=tuple(
                token
                for token in reference.supported_tokens
                if token != "MARKER_SUPERSONIC_OUTLET"
            ),
        )
        with self.assertRaisesRegex(SU2BridgeError, "MARKER_SUPERSONIC_OUTLET"):
            render_project_supersonic_pilot_config(
                reference,
                {"mach": 5.0, "alpha_deg": 8.0, "beta_deg": 0.0},
                farfield_marker="farfield",
                wall_marker="wall",
                rear_outlet_markers=("rear_1", "rear_2"),
                max_iterations=5,
            )

    def test_project_pilot_runner_records_nonproduction_success(self) -> None:
        mesh = self.run_directory / "mesh.su2"
        mesh.write_text("three-dimensional project mesh\n", encoding="utf-8")
        validation = {
            "status": "PASS",
            "ndime": 3,
            "nelem": 1,
            "npoin": 3,
            "sha256": self._sha256(mesh),
        }
        config = self.run_directory / "case.cfg"
        config.write_text(
            render_project_supersonic_pilot_config(
                self._reference(),
                {"mach": 5.0, "alpha_deg": 8.0, "beta_deg": 0.0},
                farfield_marker="farfield",
                wall_marker="wall",
                rear_outlet_markers=("rear_1", "rear_2"),
                max_iterations=5,
            ),
            encoding="utf-8",
        )
        bridge = SU2Bridge(self.runner, self.su2_cfd)

        def successful_run(*args: object, **kwargs: object) -> CommandResult:
            (self.run_directory / "history.csv").write_text(
                '"Inner_Iter","rms[Rho]"\n'
                "0,-1\n1,-2\n2,-3\n3,-4\n4,-5\n",
                encoding="utf-8",
            )
            self._write_ascii_vtu()
            return self._result(args=("case.cfg",), stdout="Exit Success (SU2_CFD)\n")

        with (
            mock.patch(
                "cfdpipe.bridges.su2_bridge.discover_su2_reference",
                return_value=self._reference(),
            ),
            mock.patch.object(
                bridge,
                "probe_version",
                return_value=("8.5.0", self._version_result()),
            ),
            mock.patch.object(bridge, "run_su2", side_effect=successful_run),
        ):
            manifest = bridge.run_project_pilot_case(
                config,
                mesh,
                self.run_directory,
                validation,
                max_iterations=5,
                live_output=False,
            )

        self.assertEqual(manifest["status"], "PASS")
        self.assertEqual(manifest["iterations"], 5)
        self.assertFalse(manifest["production_eligible"])
        self.assertFalse(manifest["boundary_mode_frozen"])
        self.assertFalse(manifest["pressure_or_backpressure_guessed"])
        self.assertEqual(manifest["return_code"], 0)
        self.assertTrue(Path(manifest["visualization_file"]).is_file())

    def test_prepare_project_pilot_refuses_to_overwrite_outputs(self) -> None:
        source = self.root / "project.su2"
        source.write_text("mesh\n", encoding="utf-8")
        validation = {
            "status": "PASS",
            "ndime": 3,
            "nelem": 1,
            "npoin": 1,
            "sha256": self._sha256(source),
        }
        (self.run_directory / "case.cfg").write_text("existing\n", encoding="utf-8")
        with (
            mock.patch(
                "cfdpipe.bridges.su2_bridge.discover_su2_reference",
                return_value=self._reference(),
            ),
            self.assertRaisesRegex(SU2BridgeError, "refuses to overwrite"),
        ):
            prepare_project_supersonic_pilot_case(
                self.su2_cfd,
                source,
                self.run_directory,
                {"mach": 5.0, "alpha_deg": 8.0, "beta_deg": 0.0},
                farfield_marker="farfield",
                wall_marker="wall",
                rear_outlet_markers=("rear_1", "rear_2"),
                max_iterations=5,
                mesh_validation=validation,
            )

    def test_history_parser_counts_quoted_csv_rows(self) -> None:
        history = self.run_directory / "history.csv"
        history.write_text(
            '"Inner_Iter","rms[Rho]"\n'
            '0,-1.0\n1,-1.1\n2,-1.2\n3,-1.3\n4,-1.4\n',
            encoding="utf-8",
        )

        self.assertEqual(parse_history_iterations(history), 5)

    def test_freestream_log_parser_preserves_direction_and_state(self) -> None:
        parsed = parse_su2_freestream_log(
            """
|       Static Pressure|       4677.88|             1|        Pa|       4677.88|
|           Temperature|        217.65|             1|         K|        217.65|
|            Velocity-X|       1464.12|             1|       m/s|       1464.12|
|            Velocity-Y|       205.778|             1|       m/s|       205.778|
|            Velocity-Z|             0|             1|       m/s|             0|
|    Velocity Magnitude|       1478.51|             1|       m/s|       1478.51|
"""
        )

        self.assertAlmostEqual(parsed["static_pressure_pa"], 4677.88)
        self.assertAlmostEqual(parsed["static_temperature_k"], 217.65)
        self.assertGreater(parsed["unit_vector_xyz"][1], 0.0)
        self.assertAlmostEqual(parsed["unit_vector_xyz"][2], 0.0)

    def test_history_parser_rejects_nonfinite_solution_values(self) -> None:
        history = self.run_directory / "history.csv"
        history.write_text(
            '"Inner_Iter","rms[Rho]"\n0,-1.0\n1,NaN\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(SU2BridgeError, "NaN or Inf"):
            parse_history_iterations(history)

    def test_visualization_selection_ignores_old_and_outside_files(self) -> None:
        old = self.run_directory / "connection_solution.vtu"
        old.write_text("old", encoding="utf-8")
        before = snapshot_visualizations(self.run_directory)
        outside = self.root / "connection_solution.pvtu"
        outside.write_text("outside", encoding="utf-8")
        generated = self.run_directory / "connection_solution.vtm"
        generated.write_text("new", encoding="utf-8")

        selected = select_new_visualization(self.run_directory, before)

        self.assertEqual(selected, generated.resolve())
        self.assertNotEqual(selected, old.resolve())
        self.assertNotEqual(selected, outside.resolve())

    def test_visualization_requires_nonempty_connection_solution_prefix(self) -> None:
        before = snapshot_visualizations(self.run_directory)
        (self.run_directory / "unrelated.vtu").write_text(
            "not this run's named solution", encoding="utf-8"
        )
        (self.run_directory / "connection_solution.vtu").write_bytes(b"")
        expected = self.run_directory / "connection_solution_volume.pvtu"
        expected.write_text("nonempty", encoding="utf-8")

        selected = select_new_visualization(self.run_directory, before)

        self.assertEqual(selected, expected.resolve())
        expected.unlink()
        self.assertIsNone(select_new_visualization(self.run_directory, before))

    def test_timeout_writes_fail_manifest_and_preserves_stderr(self) -> None:
        mesh = self._write_mesh()
        bridge = SU2Bridge(self.runner, self.su2_cfd)
        timed_out = self._result(
            returncode=-9,
            stderr="partial solver stderr before timeout\n",
            timed_out=True,
        )
        discovery, version = self._patch_discovery(bridge)

        def timeout_run(*args: object, **kwargs: object) -> CommandResult:
            timed_out.stdout_log.write_text(timed_out.stdout, encoding="utf-8")
            timed_out.stderr_log.write_text(timed_out.stderr, encoding="utf-8")
            raise CommandTimeoutError(timed_out, 0.1)

        with (
            discovery,
            version,
            mock.patch.object(
                bridge,
                "run_su2",
                side_effect=timeout_run,
            ) as run_su2,
            self.assertRaises(SU2BridgeError),
        ):
            bridge.run_smoke_case(mesh, self.run_directory, live_output=False)

        run_su2.assert_called_once()
        manifest = json.loads(
            (self.run_directory / "run_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["status"], "FAIL")
        self.assertEqual(manifest["return_code"], -9)
        self.assertTrue(manifest["timed_out"])
        self.assertEqual(
            manifest["error"]["stderr"],
            "partial solver stderr before timeout\n",
        )
        self.assertEqual(
            (self.run_directory / "solver.stderr.log").read_text(
                encoding="utf-8"
            ),
            "partial solver stderr before timeout\n",
        )

    def test_nan_timeout_is_rejected_before_discovery_or_version_probe(self) -> None:
        mesh = self._write_mesh()
        bridge = SU2Bridge(self.runner, self.su2_cfd)

        with (
            mock.patch(
                "cfdpipe.bridges.su2_bridge.discover_su2_reference"
            ) as discover,
            mock.patch.object(bridge, "probe_version") as probe_version,
            self.assertRaisesRegex(ValueError, "timeout_seconds"),
        ):
            bridge.run_smoke_case(
                mesh,
                self.run_directory,
                timeout_seconds=float("nan"),
                live_output=False,
            )

        discover.assert_not_called()
        probe_version.assert_not_called()
        self.runner.run.assert_not_called()

    def test_nonzero_return_writes_fail_manifest(self) -> None:
        mesh = self._write_mesh()
        bridge = SU2Bridge(self.runner, self.su2_cfd)
        failed = self._result(returncode=7, stderr="raw SU2 failure\n")
        discovery, version = self._patch_discovery(bridge)

        def failed_run(*args: object, **kwargs: object) -> CommandResult:
            failed.stdout_log.write_text(failed.stdout, encoding="utf-8")
            failed.stderr_log.write_text(failed.stderr, encoding="utf-8")
            return failed

        with (
            discovery,
            version,
            mock.patch.object(bridge, "run_su2", side_effect=failed_run),
            self.assertRaisesRegex(SU2BridgeError, "returned 7"),
        ):
            bridge.run_smoke_case(mesh, self.run_directory, live_output=False)

        manifest = json.loads(
            (self.run_directory / "run_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["status"], "FAIL")
        self.assertEqual(manifest["return_code"], 7)
        self.assertEqual(manifest["error"]["stderr"], "raw SU2 failure\n")
        self.assertEqual(
            (self.run_directory / "solver.stderr.log").read_text(
                encoding="utf-8"
            ),
            "raw SU2 failure\n",
        )
        self.assertIsNone(manifest["history_path"])
        self.assertIsNone(manifest["visualization_file"])

    def test_version_probe_failure_does_not_masquerade_as_solver_result(self) -> None:
        mesh = self._write_mesh()
        bridge = SU2Bridge(self.runner, self.su2_cfd)
        version_result = self._result(
            args=("--help",),
            returncode=3,
            stderr="raw version probe stderr\n",
            name="version-failure",
        )
        version_error = CommandExecutionError(version_result)

        with (
            mock.patch(
                "cfdpipe.bridges.su2_bridge.discover_su2_reference",
                return_value=self._reference(),
            ),
            mock.patch.object(
                bridge, "probe_version", side_effect=version_error
            ),
            mock.patch.object(bridge, "run_su2") as run_su2,
            self.assertRaisesRegex(SU2BridgeError, "version probe failed"),
        ):
            bridge.run_smoke_case(mesh, self.run_directory, live_output=False)

        run_su2.assert_not_called()
        manifest = json.loads(
            (self.run_directory / "run_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["status"], "FAIL")
        self.assertIsNone(manifest["return_code"])
        self.assertEqual(manifest["command"], [])
        self.assertIsNone(manifest["solver_stdout_log"])
        self.assertEqual(
            manifest["su2_version_evidence"]["command"],
            [self.su2_cfd, "--help"],
        )
        self.assertEqual(
            manifest["error"]["stderr"], "raw version probe stderr\n"
        )

    def test_zero_return_with_fatal_text_is_fail(self) -> None:
        fatal_messages = (
            "ERROR: marker setup failed",
            "fatal allocation problem",
            "residual is NaN",
            "residual is Inf",
            "residual is -inf",
            "segmentation fault",
            "cannot open mesh smoke.su2",
            "marker farfield not found",
        )
        for index, fatal_message in enumerate(fatal_messages):
            with self.subTest(message=fatal_message):
                case_directory = self.root / f"fatal-{index}"
                case_directory.mkdir()
                mesh = self._write_mesh(f"fatal-{index}.su2")
                bridge = SU2Bridge(self.runner, self.su2_cfd)
                result = self._result(
                    stdout=f"{fatal_message}\n", directory=case_directory
                )
                discovery, version = self._patch_discovery(bridge)
                with (
                    discovery,
                    version,
                    mock.patch.object(bridge, "run_su2", return_value=result),
                    self.assertRaisesRegex(SU2BridgeError, "fatal text"),
                ):
                    bridge.run_smoke_case(mesh, case_directory, live_output=False)
                manifest = json.loads(
                    (case_directory / "run_manifest.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(manifest["status"], "FAIL")
                self.assertTrue(manifest["fatal_matches"])

    def test_known_two_dimensional_quality_nan_is_recorded_but_not_fatal(
        self,
    ) -> None:
        mesh = self._write_mesh()
        bridge = SU2Bridge(self.runner, self.su2_cfd)
        diagnostic = "| Orthogonality Angle (deg.) | nan | nan |"
        result = self._result(
            stdout=(
                "Computing mesh quality statistics for the dual control volumes\n"
                f"{diagnostic}\n"
                "---------------- Begin Solver ----------------\n"
            )
        )

        def successful_run(*args: object, **kwargs: object) -> CommandResult:
            (self.run_directory / "history.csv").write_text(
                '"Inner_Iter","rms[Rho]"\n'
                '0,-1.0\n1,-1.1\n2,-1.2\n3,-1.3\n4,-1.4\n',
                encoding="utf-8",
            )
            self._write_ascii_vtu()
            return result

        discovery, version = self._patch_discovery(bridge)
        with (
            discovery,
            version,
            mock.patch.object(bridge, "run_su2", side_effect=successful_run),
        ):
            manifest = bridge.run_smoke_case(
                mesh, self.run_directory, live_output=False
            )

        self.assertEqual(manifest["status"], "PASS")
        self.assertEqual(manifest["fatal_matches"], [])
        self.assertEqual(
            manifest["diagnostic_classifications"][0]["code"],
            "SU2_2D_DUAL_ORTHOGONALITY_STAT_UNAVAILABLE",
        )
        self.assertTrue(manifest["visualization_validation"]["finite"])
        self.assertTrue(
            any(
                diagnostic in entry
                for entry in manifest["nonfatal_diagnostics"]
            )
        )

    def test_signed_two_dimensional_quality_nan_is_classified(self) -> None:
        mesh_validation = validate_su2_mesh(self._write_mesh())
        diagnostic = "| Orthogonality Angle (deg.) | -nan | -nan |"
        stdout = (
            "Computing mesh quality statistics for the dual control volumes\n"
            f"{diagnostic}\n"
            "---------------- Begin Solver ----------------\n"
        )
        classified = classify_2d_quality_nan(stdout, "", mesh_validation)
        self.assertEqual(len(classified), 1)
        self.assertEqual(
            scan_fatal_output(stdout, "", allowed_diagnostics=classified), []
        )

    def test_quality_nan_exception_does_not_hide_an_explicit_error(self) -> None:
        line = "ERROR: Orthogonality Angle (deg.) | nan | nan"
        self.assertEqual(scan_fatal_output(line, ""), [f"stdout:1: {line}"])

    def test_freestream_infinity_prose_is_not_numeric_inf(self) -> None:
        line = "No restart solution, use the values at infinity (freestream)."
        self.assertEqual(scan_fatal_output(line, ""), [])

    def test_quality_nan_is_fatal_without_classified_evidence(self) -> None:
        line = "| Orthogonality Angle (deg.) | nan | nan |"
        self.assertEqual(scan_fatal_output(line, ""), [f"stdout:1: {line}"])

    def test_quality_nan_classifier_requires_full_preprocessing_context(self) -> None:
        mesh_validation = validate_su2_mesh(self._write_mesh())
        complete = (
            "Computing mesh quality statistics for the dual control volumes\n"
            "| Orthogonality Angle (deg.) | nan | nan |\n"
            "---------------- Begin Solver ----------------\n"
        )
        classified = classify_2d_quality_nan(complete, "", mesh_validation)
        self.assertEqual(len(classified), 1)
        self.assertEqual(
            scan_fatal_output(
                complete, "", allowed_diagnostics=classified
            ),
            [],
        )
        self.assertEqual(
            classify_2d_quality_nan(
                "| Orthogonality Angle (deg.) | nan | nan |\n",
                "",
                mesh_validation,
            ),
            [],
        )
        self.assertEqual(
            classify_2d_quality_nan(complete, "NaN\n", mesh_validation),
            [],
        )

    def test_su2_sol_is_not_called_without_a_new_nonempty_restart(self) -> None:
        mesh = self._write_mesh()
        su2_sol = str((self.root / "bin" / "SU2_SOL").resolve())
        bridge = SU2Bridge(
            self.runner, self.su2_cfd, su2_sol=su2_sol
        )
        old_restart = self.run_directory / "connection_solution_restart.dat"
        old_restart.write_text("old restart", encoding="utf-8")
        result = self._result(stdout="five iterations completed\n")

        def cfd_run(*args: object, **kwargs: object) -> CommandResult:
            (self.run_directory / "history.csv").write_text(
                '"Inner_Iter","rms[Rho]"\n'
                '0,-1.0\n1,-1.1\n2,-1.2\n3,-1.3\n4,-1.4\n',
                encoding="utf-8",
            )
            (self.run_directory / "connection_solution_restart_empty.dat").write_bytes(
                b""
            )
            return result

        discovery, version = self._patch_discovery(bridge)
        with (
            discovery,
            version,
            mock.patch.object(bridge, "run_su2", side_effect=cfd_run),
            mock.patch.object(
                bridge,
                "_run_su2_sol",
                side_effect=AssertionError(
                    "SU2_SOL must not run without a new nonempty restart"
                ),
            ) as run_sol,
            self.assertRaises(SU2BridgeError),
        ):
            bridge.run_smoke_case(
                mesh, self.run_directory, live_output=False
            )

        run_sol.assert_not_called()
        manifest = json.loads(
            (self.run_directory / "run_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["status"], "FAIL")
        self.assertIsNone(manifest["visualization_file"])

    def test_su2_sol_runner_enforces_visualization_fallback_preconditions(self) -> None:
        su2_sol = str((self.root / "bin" / "SU2_SOL").resolve())
        bridge = SU2Bridge(self.runner, self.su2_cfd, su2_sol=su2_sol)
        config = self.run_directory / "su2_sol.cfg"
        config.write_text("SOLVER= EULER\n", encoding="utf-8")
        restart = self.run_directory / "connection_solution_restart.dat"
        restart.write_text("restart", encoding="utf-8")

        with self.assertRaisesRegex(SU2BridgeError, "restricted"):
            bridge._run_su2_sol(
                config,
                restart,
                self.run_directory,
                30,
                live_output=False,
                reason="manual",
            )
        self.runner.run.assert_not_called()

        sentinel = object()
        self.runner.run.return_value = sentinel
        result = bridge._run_su2_sol(
            config,
            restart,
            self.run_directory,
            30,
            live_output=False,
            reason="visualization_fallback",
        )
        self.assertIs(result, sentinel)
        self.runner.run.assert_called_once_with(
            su2_sol,
            args=["su2_sol.cfg"],
            cwd=self.run_directory,
            timeout=30,
            check=False,
            live_output=False,
            dry_run=False,
            name="su2-sol-smoke",
            stdout_log=self.run_directory / "su2_sol.stdout.log",
            stderr_log=self.run_directory / "su2_sol.stderr.log",
            metadata_log=self.run_directory / "su2_sol.command.json",
        )

    def test_su2_sol_failure_preserves_command_stderr_and_return_code(self) -> None:
        mesh = self._write_mesh()
        su2_sol = str((self.root / "bin" / "SU2_SOL").resolve())
        bridge = SU2Bridge(
            self.runner, self.su2_cfd, su2_sol=su2_sol
        )
        cfd_result = self._result(stdout="five iterations completed\n")
        sol_result = self._result(
            args=("su2_sol.cfg",),
            returncode=9,
            stderr="raw SU2_SOL stderr\n",
            name="su2_sol",
            executable=su2_sol,
        )

        def cfd_run(*args: object, **kwargs: object) -> CommandResult:
            (self.run_directory / "history.csv").write_text(
                '"Inner_Iter","rms[Rho]"\n'
                '0,-1.0\n1,-1.1\n2,-1.2\n3,-1.3\n4,-1.4\n',
                encoding="utf-8",
            )
            (self.run_directory / "connection_solution_restart.dat").write_text(
                "new restart", encoding="utf-8"
            )
            return cfd_result

        def sol_run(
            config_path: Path,
            restart_path: Path,
            run_directory: Path,
            timeout_seconds: float,
            *,
            live_output: bool,
            reason: str,
        ) -> CommandResult:
            self.assertEqual(config_path.name, "su2_sol.cfg")
            self.assertEqual(restart_path.name, "connection_solution_restart.dat")
            self.assertEqual(run_directory, self.run_directory)
            self.assertTrue(config_path.is_file())
            self.assertEqual(reason, "visualization_fallback")
            sol_result.stderr_log.write_text(
                sol_result.stderr, encoding="utf-8"
            )
            return sol_result

        discovery, version = self._patch_discovery(bridge)
        with (
            discovery,
            version,
            mock.patch.object(bridge, "run_su2", side_effect=cfd_run),
            mock.patch.object(
                bridge, "_run_su2_sol", side_effect=sol_run
            ) as run_sol,
            self.assertRaisesRegex(SU2BridgeError, "SU2_SOL returned 9"),
        ):
            bridge.run_smoke_case(
                mesh, self.run_directory, live_output=False
            )

        run_sol.assert_called_once()
        manifest = json.loads(
            (self.run_directory / "run_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["status"], "FAIL")
        self.assertEqual(manifest["su2_sol_return_code"], 9)
        self.assertEqual(manifest["su2_sol_stderr"], "raw SU2_SOL stderr\n")
        self.assertEqual(
            manifest["su2_sol_command"]["command"],
            [su2_sol, "su2_sol.cfg"],
        )
        self.assertEqual(
            Path(manifest["su2_sol_command"]["stderr_log"]),
            sol_result.stderr_log,
        )

    def test_success_writes_complete_pass_manifest(self) -> None:
        mesh = self._write_mesh()
        bridge = SU2Bridge(self.runner, self.su2_cfd)
        result = self._result(stdout="Completed solver iteration 5\n")

        def successful_run(*args: object, **kwargs: object) -> CommandResult:
            (self.run_directory / "history.csv").write_text(
                '"Inner_Iter","rms[Rho]"\n'
                '0,-1.0\n1,-1.1\n2,-1.2\n3,-1.3\n4,-1.4\n',
                encoding="utf-8",
            )
            self._write_ascii_vtu()
            return result

        discovery, version = self._patch_discovery(bridge)
        with (
            discovery,
            version,
            mock.patch.object(bridge, "run_su2", side_effect=successful_run),
        ):
            manifest = bridge.run_smoke_case(
                mesh,
                self.run_directory,
                max_iterations=5,
                nproc=1,
                timeout_seconds=300,
                live_output=False,
            )

        config = self.run_directory / "smoke.cfg"
        local_mesh = self.run_directory / "smoke.su2"
        history = self.run_directory / "history.csv"
        visualization = self.run_directory / "connection_solution.vtu"
        self.assertEqual(manifest["status"], "PASS")
        self.assertEqual(manifest["su2_cfd_path"], self.su2_cfd)
        self.assertEqual(manifest["su2_version"], "8.3.0")
        self.assertEqual(manifest["arguments"], ["smoke.cfg"])
        self.assertEqual(manifest["command"], [self.su2_cfd, "smoke.cfg"])
        self.assertEqual(manifest["cwd"], str(self.run_directory))
        self.assertEqual(manifest["config_sha256"], self._sha256(config))
        self.assertEqual(manifest["mesh_sha256"], self._sha256(local_mesh))
        self.assertEqual(manifest["return_code"], 0)
        self.assertEqual(manifest["iterations"], 5)
        self.assertEqual(manifest["history_path"], str(history.resolve()))
        self.assertEqual(
            manifest["visualization_file"], str(visualization.resolve())
        )
        self.assertEqual(
            manifest["visualization_validation"]["format"], "VTU_ASCII"
        )
        self.assertTrue(manifest["visualization_validation"]["finite"])
        self.assertRegex(manifest["start_time"], r"Z$")
        self.assertRegex(manifest["end_time"], r"Z$")
        persisted = json.loads(
            (self.run_directory / "run_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(persisted, manifest)

    def test_visualization_validator_accepts_finite_ascii_vtu(self) -> None:
        visualization = self._write_ascii_vtu()
        validation = validate_visualization_file(
            visualization, expected_points=3, expected_cells=1
        )
        self.assertEqual(validation["format"], "VTU_ASCII")
        self.assertTrue(validation["finite"])
        self.assertEqual(validation["point_count"], 3)
        self.assertEqual(validation["cell_count"], 1)

    def test_visualization_validator_rejects_nonfinite_ascii_vtu(self) -> None:
        visualization = self._write_ascii_vtu(point_value="NaN")
        with self.assertRaisesRegex(SU2BridgeError, "NaN or Inf"):
            validate_visualization_file(visualization)

    def test_visualization_validator_accepts_finite_appended_raw_vtu(self) -> None:
        visualization = self.run_directory / "connection_solution.vtu"
        values = struct.pack("<3f", 1.0, 2.0, 3.0)
        prefix = (
            b'<?xml version="1.0"?>\n'
            b'<VTKFile type="UnstructuredGrid" byte_order="LittleEndian" '
            b'header_type="UInt64"><UnstructuredGrid>'
            b'<Piece NumberOfPoints="1" NumberOfCells="0">'
            b'<PointData><DataArray type="Float32" Name="Density" '
            b'format="appended" offset="0"/></PointData>'
            b'</Piece></UnstructuredGrid><AppendedData encoding="raw">_'
        )
        visualization.write_bytes(
            prefix + struct.pack("<Q", len(values)) + values + b"</AppendedData></VTKFile>"
        )
        validation = validate_visualization_file(
            visualization, expected_points=1, expected_cells=0
        )
        self.assertEqual(validation["format"], "VTU_APPENDED_RAW")
        self.assertTrue(validation["finite"])

    def test_command_runner_passes_list_and_shell_false_to_popen(self) -> None:
        process = mock.Mock()
        process.stdout = io.BytesIO(b"")
        process.stderr = io.BytesIO(b"")
        process.returncode = 0
        process.wait.return_value = 0
        runner = CommandRunner(self.run_directory / "runner-logs")

        with mock.patch(
            "cfdpipe.process.subprocess.Popen", return_value=process
        ) as popen:
            runner.run(sys.executable, args=["--version"], cwd=self.run_directory)

        command = popen.call_args.args[0]
        self.assertIsInstance(command, list)
        self.assertEqual(command, [str(Path(sys.executable).resolve()), "--version"])
        self.assertIs(popen.call_args.kwargs["shell"], False)
        if sys.platform == "win32":
            self.assertEqual(
                popen.call_args.kwargs["creationflags"],
                subprocess.CREATE_NEW_PROCESS_GROUP,
            )


if __name__ == "__main__":
    unittest.main()
