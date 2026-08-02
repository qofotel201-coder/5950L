"""Tests for the public ``python -m cfdpipe`` command skeleton."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import cfdpipe.cli as cli_module
from cfdpipe.cli import (
    DEFAULT_GEOMETRY_INSPECTION_OUTPUT_DIR,
    DEFAULT_GEOMETRY_REVIEW_OUTPUT_DIR,
    DEFAULT_GMSH_OUTPUT_DIR,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_PARAVIEW_OUTPUT_DIR,
    DEFAULT_SU2_OUTPUT_DIR,
    build_parser,
)
from cfdpipe.toolchain import ToolResolutionError


class CliTests(unittest.TestCase):
    @staticmethod
    def _real_pipeline_arguments() -> list[str]:
        return [
            "pipeline",
            "run",
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
            "--mesh-level",
            "smoke",
            "--nproc",
            "1",
            "--max-iterations",
            "5",
            "--output",
            "runs/real_connection",
        ]

    @staticmethod
    def _outlet_check_arguments() -> list[str]:
        return [
            "pipeline",
            "outlet-check",
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
            "--mesh",
            "runs/real_connection/mesh/mesh.su2",
            "--mesh-manifest",
            "runs/real_connection/mesh/mesh_manifest.json",
            "--output",
            "runs/outlet_validation/unit_gate",
        ]

    @staticmethod
    def _boundary_layer_smoke_arguments() -> list[str]:
        return [
            "pipeline",
            "boundary-layer-smoke",
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
            "--output",
            "runs/boundary_layer_smoke/unit_cli",
        ]

    @staticmethod
    def _rans_smoke_arguments() -> list[str]:
        return [
            "pipeline",
            "rans-smoke",
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
            "--output",
            "runs/rans_smoke/unit_restart",
        ]

    @staticmethod
    def _rear_outlet_freeze_arguments() -> list[str]:
        return [
            "pipeline",
            "rear-outlet-freeze-check",
            "--output",
            "runs/rear_outlet_freeze/unit_cli",
        ]

    def test_all_required_leaf_commands_are_registered(self) -> None:
        parser = build_parser()
        cases = (
            (["tools", "check"], "_handle_tools_check"),
            (["gmsh", "smoke"], "_handle_gmsh_smoke"),
            (
                ["gmsh", "catalog", "--step", "placeholder.step"],
                "_handle_gmsh_catalog",
            ),
            (
                ["gmsh", "review", "--step", "placeholder.step"],
                "_handle_gmsh_review",
            ),
            (
                ["gmsh", "repair", "--step", "geometry/raw/model1.step"],
                "_handle_gmsh_repair",
            ),
            (
                [
                    "gmsh",
                    "validate-repair",
                    "--step",
                    "geometry/raw/model1.step",
                ],
                "_handle_gmsh_validate_repair",
            ),
            (["gmsh", "markers"], "_handle_gmsh_markers"),
            (["gmsh", "step", "placeholder.step"], "_handle_gmsh_step"),
            (["su2", "smoke", "--mesh", "smoke.su2"], "_handle_su2_smoke"),
            (["su2", "run", "case.cfg"], "_handle_su2_run"),
            (
                [
                    "paraview",
                    "inspect",
                    "--manifest",
                    "runs/connection/su2/run_manifest.json",
                ],
                "_handle_paraview_inspect",
            ),
            (["connect-test"], "_handle_connect_test"),
            (self._real_pipeline_arguments(), "_handle_pipeline_run"),
            (
                self._outlet_check_arguments(),
                "_handle_pipeline_outlet_check",
            ),
            (
                self._boundary_layer_smoke_arguments(),
                "_handle_pipeline_boundary_layer_smoke",
            ),
            (
                self._rear_outlet_freeze_arguments(),
                "_handle_pipeline_rear_outlet_freeze_check",
            ),
        )

        for arguments, expected_handler in cases:
            with self.subTest(arguments=arguments):
                parsed = parser.parse_args(arguments)
                self.assertEqual(parsed.handler.__name__, expected_handler)
                if hasattr(parsed, "output_dir"):
                    if arguments[:2] == ["gmsh", "smoke"]:
                        expected_output = DEFAULT_GMSH_OUTPUT_DIR
                    elif arguments[:2] == ["su2", "smoke"]:
                        expected_output = DEFAULT_SU2_OUTPUT_DIR
                    elif arguments[:2] == ["paraview", "inspect"]:
                        expected_output = DEFAULT_PARAVIEW_OUTPUT_DIR
                    elif arguments[:2] == ["pipeline", "run"]:
                        expected_output = (
                            cli_module.REPOSITORY_ROOT / "runs" / "real_connection"
                        )
                    else:
                        expected_output = DEFAULT_OUTPUT_DIR
                    self.assertEqual(parsed.output_dir, expected_output)

    def test_rear_outlet_freeze_parser_uses_default_config_without_tool_options(
        self,
    ) -> None:
        parsed = build_parser().parse_args(self._rear_outlet_freeze_arguments())
        self.assertEqual(
            parsed.handler.__name__, "_handle_pipeline_rear_outlet_freeze_check"
        )
        self.assertEqual(
            parsed.config, cli_module.DEFAULT_REAR_OUTLET_FREEZE_CONFIG
        )
        self.assertEqual(parsed.output, Path("runs/rear_outlet_freeze/unit_cli"))
        for forbidden in ("tools_config", "su2_cfd", "pvbatch", "nproc", "timeout"):
            self.assertFalse(hasattr(parsed, forbidden))

    def test_rear_outlet_freeze_cli_forwards_fixed_report_path_and_passes(self) -> None:
        expected_config = cli_module.DEFAULT_REAR_OUTLET_FREEZE_CONFIG.resolve()
        expected_report = (
            cli_module.DEFAULT_REAR_OUTLET_FREEZE_OUTPUT_ROOT
            / "unit_cli"
            / "rear_outlet_freeze_report.json"
        ).resolve()
        with (
            mock.patch(
                "cfdpipe.cli.evaluate_rear_outlet_freeze",
                return_value={"status": "PASS", "errors": []},
            ) as evaluator,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return_code = cli_module.main(self._rear_outlet_freeze_arguments())
        self.assertEqual(return_code, 0)
        evaluator.assert_called_once_with(expected_config, expected_report)

    def test_rear_outlet_freeze_cli_preserves_fail_report_and_returns_nonzero(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve()
            output_root = repository / "runs" / "rear_outlet_freeze"
            output = output_root / "failed_evidence"
            missing_config = repository / "config" / "missing.toml"
            arguments = [
                "pipeline",
                "rear-outlet-freeze-check",
                "--config",
                str(missing_config),
                "--output",
                str(output),
            ]
            with (
                mock.patch.object(cli_module, "REPOSITORY_ROOT", repository),
                mock.patch.object(
                    cli_module,
                    "DEFAULT_REAR_OUTLET_FREEZE_OUTPUT_ROOT",
                    output_root,
                ),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                return_code = cli_module.main(arguments)

            report_path = output / "rear_outlet_freeze_report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(return_code, 1)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["errors"][0]["code"], "CONTRACT_FILE_MISSING")

    def test_rear_outlet_freeze_cli_rejects_output_escape_before_evaluator(
        self,
    ) -> None:
        unsafe_outputs = (
            "runs/rear_outlet_freeze",
            "runs/not-rear-outlet-freeze/unit_cli",
            "runs/rear_outlet_freeze/../escaped",
        )
        for output in unsafe_outputs:
            with self.subTest(output=output):
                arguments = [
                    "pipeline",
                    "rear-outlet-freeze-check",
                    "--output",
                    output,
                ]
                with (
                    mock.patch(
                        "cfdpipe.cli.evaluate_rear_outlet_freeze"
                    ) as evaluator,
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    return_code = cli_module.main(arguments)
                self.assertEqual(return_code, 2)
                evaluator.assert_not_called()

    def test_rear_outlet_freeze_cli_refuses_to_overwrite_existing_report(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve()
            output_root = repository / "runs" / "rear_outlet_freeze"
            output = output_root / "existing"
            output.mkdir(parents=True)
            report_path = output / "rear_outlet_freeze_report.json"
            original = '{"status":"PASS"}\n'
            report_path.write_text(original, encoding="utf-8")
            arguments = [
                "pipeline",
                "rear-outlet-freeze-check",
                "--output",
                str(output),
            ]
            with (
                mock.patch.object(cli_module, "REPOSITORY_ROOT", repository),
                mock.patch.object(
                    cli_module,
                    "DEFAULT_REAR_OUTLET_FREEZE_OUTPUT_ROOT",
                    output_root,
                ),
                mock.patch(
                    "cfdpipe.cli.evaluate_rear_outlet_freeze"
                ) as evaluator,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                return_code = cli_module.main(arguments)

            self.assertEqual(return_code, 2)
            evaluator.assert_not_called()
            self.assertEqual(original, report_path.read_text(encoding="utf-8"))

    def test_boundary_layer_smoke_parser_has_no_toolchain_or_downstream_options(self) -> None:
        parsed = build_parser().parse_args(self._boundary_layer_smoke_arguments())
        self.assertEqual(
            parsed.handler.__name__, "_handle_pipeline_boundary_layer_smoke"
        )
        self.assertEqual(parsed.case_id, "M50_H21_A8_B0")
        self.assertEqual(
            parsed.smoke_config, cli_module.DEFAULT_BOUNDARY_LAYER_SMOKE_CONFIG
        )
        self.assertEqual(
            parsed.schedule_config, cli_module.DEFAULT_BOUNDARY_LAYER_TRIAL_CONFIG
        )
        for forbidden in ("tools_config", "su2_cfd", "pvbatch", "nproc", "timeout"):
            self.assertFalse(hasattr(parsed, forbidden))

    def test_rans_restart_cli_requires_explicit_manifest(self) -> None:
        arguments = [*self._rans_smoke_arguments(), "--spatial-order", "2"]
        with (
            mock.patch("cfdpipe.cli.Toolchain") as toolchain_type,
            mock.patch("cfdpipe.cli.run_rans_smoke_validation") as runner,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return_code = cli_module.main(arguments)
        self.assertEqual(return_code, 2)
        toolchain_type.assert_not_called()
        runner.assert_not_called()

    def test_rans_restart_cli_forwards_order_and_absolute_manifest(self) -> None:
        arguments = [
            *self._rans_smoke_arguments(),
            "--spatial-order",
            "2",
            "--restart-manifest",
            "runs/rans_smoke/source/su2/run_manifest.json",
            "--max-iterations",
            "200",
            "--quiet",
        ]
        toolchain = mock.Mock()
        with (
            mock.patch("cfdpipe.cli.Toolchain", return_value=toolchain),
            mock.patch(
                "cfdpipe.cli.run_rans_smoke_validation",
                return_value={"status": "PASS"},
            ) as runner,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return_code = cli_module.main(arguments)
        self.assertEqual(return_code, 0)
        kwargs = runner.call_args.kwargs
        self.assertEqual(kwargs["spatial_order"], 2)
        self.assertEqual(kwargs["max_iterations"], 200)
        self.assertEqual(
            kwargs["restart_manifest_path"],
            (
                cli_module.REPOSITORY_ROOT
                / "runs"
                / "rans_smoke"
                / "source"
                / "su2"
                / "run_manifest.json"
            ),
        )
        self.assertIs(kwargs["toolchain"], toolchain)

    def test_boundary_layer_smoke_config_binds_current_inputs_and_48_stable_members(self) -> None:
        document, _ = cli_module._read_cfdpipe_config(
            cli_module.DEFAULT_BOUNDARY_LAYER_SMOKE_CONFIG, "smoke config"
        )
        provenance = document["provenance"]
        paths = {
            "project_sha256": cli_module.REPOSITORY_ROOT / "config" / "project.toml",
            "cases_sha256": cli_module.REPOSITORY_ROOT / "config" / "cases.csv",
            "markers_sha256": cli_module.REPOSITORY_ROOT / "config" / "markers.toml",
            "topology_smoke_sha256": cli_module.DEFAULT_TOPOLOGY_SMOKE_CONFIG,
            "pipeline_brep_sha256": (
                cli_module.DEFAULT_DERIVED_GEOMETRY_DIR
                / "model1_shared_topology.brep"
            ),
        }
        for field, path in paths.items():
            with path.open("rb") as stream:
                actual = hashlib.sha256(stream.read()).hexdigest()
            self.assertEqual(provenance[field], actual)
        fingerprints = document["selection"]["wall_surface_fingerprints"]
        self.assertEqual(len(fingerprints), 48)
        self.assertEqual(len(set(fingerprints)), 48)
        self.assertTrue(all(len(value) == 64 for value in fingerprints))
        self.assertNotIn("tag", document["selection"])
        self.assertEqual(document["mesh"]["normal_mode"], "implicit")
        self.assertFalse(document["policy"]["run_su2"])
        self.assertFalse(document["policy"]["run_paraview"])

    def test_boundary_layer_smoke_handler_reuses_real_strategy_and_builder(self) -> None:
        topology_hash = cli_module._read_cfdpipe_config(
            cli_module.DEFAULT_TOPOLOGY_SMOKE_CONFIG, "topology"
        )[1]
        smoke_document = cli_module._read_cfdpipe_config(
            cli_module.DEFAULT_BOUNDARY_LAYER_SMOKE_CONFIG, "smoke config"
        )[0]
        markers_hash = "a249c79b9167c782cfa8dbde8632b0fc6e9ca61bda84ae76f475eccf3722fa8e"
        inputs = SimpleNamespace(
            pipeline_geometry_path=Path("C:/repository/pipeline.brep"),
            project={"mesh": {"minimum_prism_layers": 15, "initial_growth_ratio": 1.2}},
            marker_document={"solver_markers": []},
            input_records={
                "markers": {"sha256": markers_hash},
                "topology_smoke_config": {"sha256": topology_hash},
            },
        )
        schedule = {
            "first_layer_height_m": smoke_document["mesh"]["first_layer_height_m"],
            "total_thickness_m": smoke_document["mesh"][
                "direction_probe_distance_m"
            ],
        }
        normalized = {"schema": "cfdpipe.boundary_layer_smoke.v2"}
        strategy = mock.Mock()
        manifest = {"status": "PASS"}
        root = cli_module.DEFAULT_BOUNDARY_LAYER_SMOKE_OUTPUT_ROOT
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            expected_output = (Path(temporary) / "unit_cli").resolve()
            arguments = self._boundary_layer_smoke_arguments()
            arguments[arguments.index("--output") + 1] = str(expected_output)
            with (
                mock.patch("cfdpipe.cli.load_real_connection_inputs", return_value=inputs),
                mock.patch("cfdpipe.cli.select_real_case", return_value={"case_id": "M50_H21_A8_B0"}),
                mock.patch("cfdpipe.cli.normalize_boundary_layer_trial_config", return_value={}) as normalize_trial,
                mock.patch("cfdpipe.cli.calculate_boundary_layer_schedule", return_value=schedule),
                mock.patch("cfdpipe.cli.normalize_boundary_layer_smoke_config", return_value=normalized) as normalize_smoke,
                mock.patch("cfdpipe.cli.RealProjectBoundaryLayerStrategy", return_value=strategy) as strategy_type,
                mock.patch("cfdpipe.cli.build_boundary_layer_smoke", return_value=manifest) as builder,
                mock.patch("cfdpipe.cli.Toolchain") as toolchain_type,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                return_code = cli_module.main(arguments)

        self.assertEqual(return_code, 0)
        toolchain_type.assert_not_called()
        normalize_trial.assert_called_once()
        normalize_smoke.assert_called_once()
        strategy_type.assert_called_once()
        builder.assert_called_once()
        call = builder.call_args
        self.assertEqual(call.args[0], Path("C:/repository/pipeline.brep"))
        self.assertEqual(call.args[1], expected_output)
        self.assertIs(call.args[2], normalized)
        self.assertIs(call.kwargs["strategy"], strategy)
        evidence = call.kwargs["run_evidence"]
        self.assertEqual(evidence["boundary_layer_schedule"], schedule)
        self.assertFalse(evidence["downstream_programs_called"])
        self.assertIn("smoke", evidence["configuration_files"])

    def test_boundary_layer_smoke_rejects_existing_or_outside_output_before_inputs(self) -> None:
        outside = self._boundary_layer_smoke_arguments()
        outside[outside.index("--output") + 1] = "runs/not-boundary-layer-smoke"
        with (
            mock.patch("cfdpipe.cli.load_real_connection_inputs") as loader,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(cli_module.main(outside), 2)
        loader.assert_not_called()

    def test_boundary_layer_smoke_safe_preflight_failure_writes_new_evidence(self) -> None:
        root = cli_module.DEFAULT_BOUNDARY_LAYER_SMOKE_OUTPUT_ROOT
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            output = Path(temporary) / "failed_preflight"
            arguments = self._boundary_layer_smoke_arguments()
            arguments[arguments.index("--output") + 1] = str(output)
            with (
                mock.patch(
                    "cfdpipe.cli.load_real_connection_inputs",
                    side_effect=ValueError("injected stale input hash"),
                ),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                return_code = cli_module.main(arguments)
            manifest = json.loads(
                (output / "preflight_manifest.json").read_text(encoding="utf-8")
            )
            log_text = (output / "preflight.log").read_text(encoding="utf-8")
        self.assertEqual(return_code, 2)
        self.assertEqual(manifest["status"], "FAIL")
        self.assertFalse(manifest["gmsh_initialized"])
        self.assertIn("injected stale input hash", manifest["error"]["message"])
        self.assertIn("Traceback", log_text)

    def test_outlet_check_handler_forwards_bounded_serial_inputs(self) -> None:
        arguments = [
            *self._outlet_check_arguments(),
            "--max-iterations",
            "120",
            "--timeout",
            "45",
            "--quiet",
        ]
        toolchain = mock.Mock()
        report = {"status": "PASS", "diagnostic_gate": "PASS"}
        with (
            mock.patch("cfdpipe.cli.Toolchain", return_value=toolchain),
            mock.patch(
                "cfdpipe.cli.run_outlet_validation", return_value=report
            ) as run_outlet_validation,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return_code = cli_module.main(arguments)

        self.assertEqual(return_code, 0)
        repository_root = cli_module.REPOSITORY_ROOT.resolve()
        run_outlet_validation.assert_called_once_with(
            step_path=repository_root / "geometry" / "raw" / "model1.step",
            project_path=repository_root / "config" / "project.toml",
            cases_path=repository_root / "config" / "cases.csv",
            markers_path=repository_root / "config" / "markers.toml",
            topology_smoke_path=cli_module.DEFAULT_TOPOLOGY_SMOKE_CONFIG,
            case_id="M50_H21_A8_B0",
            source_mesh_path=(
                repository_root / "runs" / "real_connection" / "mesh" / "mesh.su2"
            ),
            mesh_manifest_path=(
                repository_root
                / "runs"
                / "real_connection"
                / "mesh"
                / "mesh_manifest.json"
            ),
            output_directory=(
                repository_root / "runs" / "outlet_validation" / "unit_gate"
            ),
            trusted_repository_root=cli_module.REPOSITORY_ROOT,
            toolchain=toolchain,
            paraview_script_directory=cli_module.DEFAULT_PARAVIEW_SCRIPTS,
            max_iterations=120,
            timeout_seconds=45.0,
            live_output=False,
            controller_argv=["cfdpipe", *arguments],
        )

    def test_real_pipeline_accepts_the_exact_required_form(self) -> None:
        parsed = build_parser().parse_args(self._real_pipeline_arguments())

        self.assertEqual(parsed.step, Path("geometry/raw/model1.step"))
        self.assertEqual(parsed.project, Path("config/project.toml"))
        self.assertEqual(parsed.cases, Path("config/cases.csv"))
        self.assertEqual(parsed.markers, Path("config/markers.toml"))
        self.assertEqual(
            parsed.topology_smoke_config,
            cli_module.DEFAULT_TOPOLOGY_SMOKE_CONFIG,
        )
        self.assertEqual(parsed.case_id, "M50_H21_A8_B0")
        self.assertEqual(parsed.mesh_level, "smoke")
        self.assertEqual(parsed.nproc, 1)
        self.assertEqual(parsed.max_iterations, 5)
        self.assertEqual(parsed.output, Path("runs/real_connection"))
        self.assertEqual(parsed.handler.__name__, "_handle_pipeline_run")

    def test_real_pipeline_handler_forwards_all_inputs_and_runtime_limits(self) -> None:
        arguments = [
            *self._real_pipeline_arguments(),
            "--timeout",
            "73.5",
            "--quiet",
            "--tools-config",
            "config/real-tools.json",
        ]
        toolchain = mock.Mock()
        result = SimpleNamespace(report={"overall": "PASS"})

        with (
            mock.patch("cfdpipe.cli.Toolchain", return_value=toolchain) as toolchain_type,
            mock.patch(
                "cfdpipe.cli.run_real_connection", return_value=result
            ) as run_real_connection,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return_code = cli_module.main(arguments)

        self.assertEqual(return_code, 0)
        toolchain_type.assert_called_once_with(
            config_path=Path("config/real-tools.json"),
            cli_overrides={},
        )
        repository_root = cli_module.REPOSITORY_ROOT.resolve()
        output_root = (repository_root / "runs" / "real_connection").resolve()
        run_real_connection.assert_called_once_with(
            step_path=(repository_root / "geometry" / "raw" / "model1.step"),
            project_path=(repository_root / "config" / "project.toml"),
            cases_path=(repository_root / "config" / "cases.csv"),
            markers_path=(repository_root / "config" / "markers.toml"),
            topology_smoke_path=cli_module.DEFAULT_TOPOLOGY_SMOKE_CONFIG,
            case_id="M50_H21_A8_B0",
            mesh_level="smoke",
            nproc=1,
            max_iterations=5,
            output_directory=output_root,
            allowed_output_root=output_root,
            trusted_repository_root=cli_module.REPOSITORY_ROOT,
            toolchain=toolchain,
            paraview_script_directory=cli_module.DEFAULT_PARAVIEW_SCRIPTS,
            smoke_characteristic_length_m=0.25,
            facet_overlap_angle_tolerance_degrees=0.1,
            gmsh_algorithm_3d=1,
            timeout_seconds=73.5,
            live_output=False,
            controller_argv=["cfdpipe", *arguments],
        )

    def test_real_pipeline_rejects_unsafe_limits_before_toolchain_or_runner(self) -> None:
        cases = (
            (["--nproc", "2"], "nproc"),
            (["--max-iterations", "0"], "iterations-zero"),
            (["--max-iterations", "6"], "iterations-over-cap"),
            (["--smoke-characteristic-length", "0"], "smoke-size-zero"),
            (["--smoke-characteristic-length", "nan"], "smoke-size-nan"),
            (["--facet-overlap-angle-tolerance", "0"], "facet-angle-zero"),
            (["--facet-overlap-angle-tolerance", "0.11"], "facet-angle-loose"),
            (["--timeout", "nan"], "timeout"),
        )
        for replacement, label in cases:
            with self.subTest(case=label):
                arguments = self._real_pipeline_arguments()
                option = replacement[0]
                if option in arguments:
                    option_index = arguments.index(option)
                    arguments[option_index + 1] = replacement[1]
                else:
                    arguments.extend(replacement)
                with (
                    mock.patch("cfdpipe.cli.Toolchain") as toolchain_type,
                    mock.patch("cfdpipe.cli.run_real_connection") as runner,
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    return_code = cli_module.main(arguments)

                self.assertEqual(return_code, 2)
                toolchain_type.assert_not_called()
                runner.assert_not_called()

    def test_real_pipeline_rejects_non_smoke_mesh_level(self) -> None:
        arguments = self._real_pipeline_arguments()
        arguments[arguments.index("--mesh-level") + 1] = "medium"

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                build_parser().parse_args(arguments)

        self.assertEqual(raised.exception.code, 2)

    def test_real_pipeline_rejects_output_outside_fixed_root_before_toolchain(self) -> None:
        arguments = self._real_pipeline_arguments()
        arguments[arguments.index("--output") + 1] = "runs/not-real-connection"

        with (
            mock.patch("cfdpipe.cli.Toolchain") as toolchain_type,
            mock.patch("cfdpipe.cli.run_real_connection") as runner,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return_code = cli_module.main(arguments)

        self.assertEqual(return_code, 2)
        toolchain_type.assert_not_called()
        runner.assert_not_called()

    def test_gmsh_smoke_accepts_the_required_output_form(self) -> None:
        parser = build_parser()

        parsed = parser.parse_args(
            ["gmsh", "smoke", "--output", "runs/connection/gmsh"]
        )

        self.assertEqual(parsed.output, Path("runs/connection/gmsh"))
        self.assertEqual(parsed.handler.__name__, "_handle_gmsh_smoke")

    def test_gmsh_catalog_accepts_and_forwards_only_the_fixed_output(self) -> None:
        DEFAULT_GEOMETRY_INSPECTION_OUTPUT_DIR.parent.mkdir(
            parents=True, exist_ok=True
        )
        with tempfile.TemporaryDirectory(
            prefix="catalog_cli_",
            dir=DEFAULT_GEOMETRY_INSPECTION_OUTPUT_DIR.parent,
        ) as temporary:
            source = Path(temporary) / "model.step"
            source.write_text("ISO-10303-21;", encoding="ascii")
            bridge = mock.Mock()
            bridge.inspect_step_geometry.return_value = {
                "status": "PASS",
                "classification_status": "UNCLASSIFIED",
            }
            arguments = [
                "gmsh",
                "catalog",
                "--step",
                str(source),
                "--output",
                str(DEFAULT_GEOMETRY_INSPECTION_OUTPUT_DIR),
            ]

            with (
                mock.patch("cfdpipe.cli.GmshBridge", return_value=bridge),
                mock.patch("cfdpipe.cli.Toolchain") as toolchain_type,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                return_code = cli_module.main(arguments)

            self.assertEqual(return_code, 0)
            toolchain_type.assert_not_called()
            bridge.inspect_step_geometry.assert_called_once_with(
                source.resolve(),
                DEFAULT_GEOMETRY_INSPECTION_OUTPUT_DIR.resolve(),
                allowed_output_root=DEFAULT_GEOMETRY_INSPECTION_OUTPUT_DIR.resolve(),
            )

    def test_gmsh_catalog_rejects_output_escape_before_bridge(self) -> None:
        DEFAULT_GEOMETRY_INSPECTION_OUTPUT_DIR.parent.mkdir(
            parents=True, exist_ok=True
        )
        with tempfile.TemporaryDirectory(
            prefix="catalog_cli_",
            dir=DEFAULT_GEOMETRY_INSPECTION_OUTPUT_DIR.parent,
        ) as temporary:
            source = Path(temporary) / "model.step"
            source.write_text("ISO-10303-21;", encoding="ascii")
            outside = DEFAULT_GEOMETRY_INSPECTION_OUTPUT_DIR.parent / "not_catalog"

            with (
                mock.patch("cfdpipe.cli.GmshBridge") as bridge_type,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                return_code = cli_module.main(
                    [
                        "gmsh",
                        "catalog",
                        "--step",
                        str(source),
                        "--output",
                        str(outside),
                    ]
                )

            self.assertEqual(return_code, 2)
            bridge_type.assert_not_called()

    def test_gmsh_review_forwards_bounded_diagnostic_options(self) -> None:
        DEFAULT_GEOMETRY_REVIEW_OUTPUT_DIR.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="review_cli_",
            dir=DEFAULT_GEOMETRY_REVIEW_OUTPUT_DIR.parent,
        ) as temporary:
            source = Path(temporary) / "model.step"
            source.write_text("ISO-10303-21;", encoding="ascii")
            arguments = [
                "gmsh",
                "review",
                "--step",
                str(source),
                "--project",
                str(cli_module.REPOSITORY_ROOT / "config" / "project.toml"),
                "--output",
                str(DEFAULT_GEOMETRY_REVIEW_OUTPUT_DIR),
                "--mesh-size",
                "0.25",
                "--max-triangles",
                "90000",
                "--timeout",
                "44",
                "--quiet",
            ]
            toolchain = mock.Mock()
            with (
                mock.patch("cfdpipe.cli.Toolchain", return_value=toolchain),
                mock.patch(
                    "cfdpipe.cli.run_geometry_evidence_review",
                    return_value={"status": "PASS", "next_stage_allowed": False},
                ) as review,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                return_code = cli_module.main(arguments)

        self.assertEqual(return_code, 0)
        review.assert_called_once_with(
            step_path=source.resolve(),
            project_path=(
                cli_module.REPOSITORY_ROOT / "config" / "project.toml"
            ).resolve(),
            output_directory=DEFAULT_GEOMETRY_REVIEW_OUTPUT_DIR.resolve(),
            allowed_output_root=DEFAULT_GEOMETRY_REVIEW_OUTPUT_DIR.resolve(),
            toolchain=toolchain,
            timeout_seconds=44.0,
            live_output=False,
            mesh_size_m=0.25,
            max_triangles=90000,
            controller_argv=["cfdpipe", *arguments],
        )

    def test_gmsh_review_rejects_unsafe_output_and_limits_before_toolchain(self) -> None:
        DEFAULT_GEOMETRY_REVIEW_OUTPUT_DIR.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="review_cli_limits_",
            dir=DEFAULT_GEOMETRY_REVIEW_OUTPUT_DIR.parent,
        ) as temporary:
            source = Path(temporary) / "model.step"
            source.write_text("ISO-10303-21;", encoding="ascii")
            cases = (
                (["--output", "runs/not_geometry_review"], "output"),
                (["--max-triangles", "100001"], "triangle-limit"),
                (["--mesh-size", "nan"], "mesh-size"),
                (["--timeout", "0"], "timeout"),
            )
            for extra, label in cases:
                with self.subTest(case=label):
                    arguments = ["gmsh", "review", "--step", str(source), *extra]
                    with (
                        mock.patch("cfdpipe.cli.Toolchain") as toolchain_type,
                        mock.patch("cfdpipe.cli.run_geometry_evidence_review") as review,
                        contextlib.redirect_stdout(io.StringIO()),
                        contextlib.redirect_stderr(io.StringIO()),
                    ):
                        return_code = cli_module.main(arguments)
                    self.assertEqual(return_code, 2)
                    toolchain_type.assert_not_called()
                    review.assert_not_called()

    def test_su2_smoke_accepts_the_required_connection_form(self) -> None:
        parser = build_parser()

        parsed = parser.parse_args(
            [
                "su2",
                "smoke",
                "--mesh",
                "runs/connection/gmsh/smoke.su2",
                "--output",
                "runs/connection/su2",
                "--nproc",
                "1",
                "--max-iterations",
                "5",
                "--timeout",
                "300",
            ]
        )

        self.assertEqual(parsed.mesh, Path("runs/connection/gmsh/smoke.su2"))
        self.assertEqual(parsed.output, Path("runs/connection/su2"))
        self.assertEqual(parsed.nproc, 1)
        self.assertEqual(parsed.max_iterations, 5)
        self.assertEqual(parsed.timeout, 300.0)
        self.assertEqual(parsed.handler.__name__, "_handle_su2_smoke")

    def test_paraview_inspect_accepts_exact_manifest_output_form(self) -> None:
        parser = build_parser()

        parsed = parser.parse_args(
            [
                "paraview",
                "inspect",
                "--manifest",
                "runs/connection/su2/run_manifest.json",
                "--output",
                "runs/connection/paraview",
            ]
        )

        self.assertEqual(
            parsed.manifest,
            Path("runs/connection/su2/run_manifest.json"),
        )
        self.assertEqual(parsed.output, Path("runs/connection/paraview"))
        self.assertEqual(parsed.timeout, 300.0)
        self.assertEqual(parsed.handler.__name__, "_handle_paraview_inspect")

    def test_connect_test_accepts_exact_required_form(self) -> None:
        parser = build_parser()

        parsed = parser.parse_args(
            [
                "connect-test",
                "--output",
                "runs/connection",
                "--clean",
                "--nproc",
                "1",
                "--timeout",
                "300",
            ]
        )

        self.assertEqual(parsed.output, Path("runs/connection"))
        self.assertTrue(parsed.clean)
        self.assertEqual(parsed.nproc, 1)
        self.assertEqual(parsed.timeout, 300.0)
        self.assertFalse(parsed.quiet)
        self.assertFalse(hasattr(parsed, "dry_run"))
        self.assertEqual(parsed.handler.__name__, "_handle_connect_test")

    def test_connect_test_rejects_dry_run_option(self) -> None:
        parser = build_parser()

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                parser.parse_args(["connect-test", "--dry-run"])

        self.assertEqual(raised.exception.code, 2)

    def test_connect_test_rejects_nonserial_nproc_before_runner(self) -> None:
        with (
            mock.patch("cfdpipe.cli.Toolchain") as toolchain_type,
            mock.patch("cfdpipe.cli.run_connect_test") as run_connect_test,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return_code = cli_module.main(
                [
                    "connect-test",
                    "--output",
                    str(DEFAULT_OUTPUT_DIR),
                    "--nproc",
                    "2",
                ]
            )

        self.assertEqual(return_code, 2)
        toolchain_type.assert_not_called()
        run_connect_test.assert_not_called()

    def test_connect_test_rejects_nan_timeout_before_runner(self) -> None:
        with (
            mock.patch("cfdpipe.cli.Toolchain") as toolchain_type,
            mock.patch("cfdpipe.cli.run_connect_test") as run_connect_test,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return_code = cli_module.main(
                [
                    "connect-test",
                    "--output",
                    str(DEFAULT_OUTPUT_DIR),
                    "--timeout",
                    "nan",
                ]
            )

        self.assertEqual(return_code, 2)
        toolchain_type.assert_not_called()
        run_connect_test.assert_not_called()

    def test_connect_test_rejects_output_outside_root_before_runner(self) -> None:
        outside = DEFAULT_OUTPUT_DIR.parent / "outside-connection"
        with (
            mock.patch("cfdpipe.cli.Toolchain") as toolchain_type,
            mock.patch("cfdpipe.cli.run_connect_test") as run_connect_test,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return_code = cli_module.main(
                [
                    "connect-test",
                    "--output",
                    str(outside),
                ]
            )

        self.assertEqual(return_code, 2)
        toolchain_type.assert_not_called()
        run_connect_test.assert_not_called()

    def test_connect_test_handler_forwards_options_and_all_tool_overrides(self) -> None:
        tools_config = Path("config/connect-test-tools.json")
        overrides = {
            "gmsh": Path("C:/tools/gmsh.exe"),
            "su2_cfd": Path("C:/tools/SU2_CFD.exe"),
            "su2_sol": Path("C:/tools/SU2_SOL.exe"),
            "mpiexec": Path("C:/tools/mpiexec.exe"),
            "pvbatch": Path("C:/tools/pvbatch.exe"),
        }
        arguments = [
            "connect-test",
            "--output",
            "runs/connection",
            "--clean",
            "--nproc",
            "1",
            "--timeout",
            "73.5",
            "--quiet",
            "--tools-config",
            str(tools_config),
        ]
        for name, path in overrides.items():
            arguments.extend(["--" + name.replace("_", "-"), str(path)])

        toolchain = mock.Mock()
        result = SimpleNamespace(report={"overall": "PASS"})
        with (
            mock.patch(
                "cfdpipe.cli.Toolchain", return_value=toolchain
            ) as toolchain_type,
            mock.patch(
                "cfdpipe.cli.run_connect_test", return_value=result
            ) as run_connect_test,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return_code = cli_module.main(arguments)

        self.assertEqual(return_code, 0)
        toolchain_type.assert_called_once_with(
            config_path=tools_config,
            cli_overrides=overrides,
        )
        expected_root = DEFAULT_OUTPUT_DIR.resolve()
        run_connect_test.assert_called_once_with(
            output_directory=expected_root,
            allowed_output_root=expected_root,
            trusted_repository_root=cli_module.REPOSITORY_ROOT,
            toolchain=toolchain,
            paraview_script_directory=cli_module.DEFAULT_PARAVIEW_SCRIPTS,
            clean=True,
            nproc=1,
            timeout_seconds=73.5,
            live_output=False,
            controller_argv=["cfdpipe", *arguments],
        )

    def test_paraview_inspect_handler_resolves_only_pvbatch_and_runs_manifest(self) -> None:
        DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=DEFAULT_OUTPUT_DIR) as temporary:
            output = Path(temporary).resolve()
            source_manifest = Path("runs/connection/su2/run_manifest.json")
            toolchain = mock.Mock()
            resolved = SimpleNamespace(
                path=Path("C:/ParaView/bin/pvbatch.exe"), source="cli"
            )
            toolchain.resolve.return_value = resolved
            bridge = mock.Mock()
            bridge.inspect_manifest.return_value = {"status": "PASS"}
            runner = mock.Mock()

            with (
                mock.patch("cfdpipe.cli.Toolchain", return_value=toolchain),
                mock.patch("cfdpipe.cli._runner", return_value=runner),
                mock.patch(
                    "cfdpipe.cli.ParaViewBridge", return_value=bridge
                ) as bridge_type,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                return_code = cli_module.main(
                    [
                        "paraview",
                        "inspect",
                        "--manifest",
                        str(source_manifest),
                        "--output",
                        str(output),
                        "--timeout",
                        "27",
                        "--quiet",
                    ]
                )

            self.assertEqual(return_code, 0)
            toolchain.resolve.assert_called_once_with("pvbatch")
            bridge_type.assert_called_once_with(
                runner,
                pvbatch=resolved.path,
                script_dir=cli_module.DEFAULT_PARAVIEW_SCRIPTS,
                pvbatch_source="cli",
            )
            bridge.inspect_manifest.assert_called_once_with(
                source_manifest,
                output,
                timeout_seconds=27.0,
                live_output=False,
            )

    def test_serial_su2_smoke_allows_missing_mpi_and_su2_sol(self) -> None:
        DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=DEFAULT_OUTPUT_DIR) as temporary:
            output = Path(temporary).resolve()
            toolchain = mock.Mock()

            def resolve(name: str):
                if name == "su2_cfd":
                    return SimpleNamespace(path=Path("C:/SU2/bin/SU2_CFD.exe"))
                raise ToolResolutionError(f"optional tool is absent: {name}")

            toolchain.resolve.side_effect = resolve
            toolchain.resolve_optional.return_value = None
            bridge = mock.Mock()
            bridge.run_smoke_case.return_value = {"status": "PASS"}
            with (
                mock.patch("cfdpipe.cli.Toolchain", return_value=toolchain),
                mock.patch("cfdpipe.cli._runner", return_value=mock.Mock()),
                mock.patch("cfdpipe.cli.SU2Bridge", return_value=bridge) as bridge_type,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                return_code = cli_module.main(
                    [
                        "su2",
                        "smoke",
                        "--mesh",
                        "runs/connection/gmsh/smoke.su2",
                        "--output",
                        str(output),
                        "--nproc",
                        "1",
                        "--max-iterations",
                        "5",
                        "--timeout",
                        "300",
                    ]
                )

            self.assertEqual(return_code, 0)
            resolved_names = [call.args[0] for call in toolchain.resolve.call_args_list]
            self.assertIn("su2_cfd", resolved_names)
            self.assertNotIn("mpiexec", resolved_names)
            constructor = bridge_type.call_args
            self.assertEqual(
                constructor.kwargs["su2_cfd"], Path("C:/SU2/bin/SU2_CFD.exe")
            )
            self.assertIsNone(constructor.kwargs["mpiexec"])
            self.assertIsNone(constructor.kwargs["su2_sol"])
            bridge.run_smoke_case.assert_called_once()

    def test_nan_timeout_is_rejected_before_tool_resolution(self) -> None:
        DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=DEFAULT_OUTPUT_DIR) as temporary:
            output = Path(temporary).resolve()
            with (
                mock.patch("cfdpipe.cli.Toolchain") as toolchain_type,
                mock.patch("cfdpipe.cli.SU2Bridge") as bridge_type,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                return_code = cli_module.main(
                    [
                        "su2",
                        "smoke",
                        "--mesh",
                        "runs/connection/gmsh/smoke.su2",
                        "--output",
                        str(output),
                        "--timeout",
                        "nan",
                    ]
                )

            self.assertEqual(return_code, 2)
            toolchain_type.assert_not_called()
            bridge_type.assert_not_called()

    def test_cli_does_not_allow_connection_output_outside_fixed_directory(self) -> None:
        parser = build_parser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                parser.parse_args(
                    ["su2", "smoke", "--output-dir", "outside-connection"]
                )
        self.assertEqual(raised.exception.code, 2)

    def test_module_entrypoint_delegates_to_cli_main(self) -> None:
        with mock.patch("cfdpipe.cli.main", return_value=0) as main:
            with self.assertRaises(SystemExit) as raised:
                runpy.run_module("cfdpipe.__main__", run_name="__main__")

        main.assert_called_once_with()
        self.assertEqual(raised.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
