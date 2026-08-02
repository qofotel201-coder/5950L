from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import cfdpipe.cli as cli_module


class _SuccessfulRunner:
    instances: list["_SuccessfulRunner"] = []
    manifest_factory = None

    def __init__(self, output_dir, *, live_output=False, **kwargs) -> None:
        self.output_dir = Path(output_dir)
        self.live_output = live_output
        self.kwargs = kwargs
        self.calls = []
        self.__class__.instances.append(self)

    def run(self, executable, args=(), **kwargs):
        arguments = tuple(str(value) for value in args)
        self.calls.append((str(executable), arguments, kwargs))
        output_index = arguments.index("--output") + 1
        output = Path(arguments[output_index])
        output.mkdir(parents=True)
        if self.__class__.manifest_factory is None:
            raise AssertionError("successful runner manifest factory is missing")
        manifest = self.__class__.manifest_factory(output, arguments)
        (output / "coarse_calibration_manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        return SimpleNamespace(
            returncode=0,
            timed_out=False,
            stderr="",
            executable=str(executable),
            as_metadata=lambda: {
                "executable": str(executable),
                "args": list(arguments),
                "cwd": str(kwargs["cwd"]),
                "returncode": 0,
                "stdout_log": str(self.output_dir / "worker.stdout.log"),
                "stderr_log": str(self.output_dir / "worker.stderr.log"),
                "metadata_log": str(self.output_dir / "worker.command.json"),
            },
        )


class _FailingRunner(_SuccessfulRunner):
    instances: list["_FailingRunner"] = []

    def run(self, executable, args=(), **kwargs):
        arguments = tuple(str(value) for value in args)
        self.calls.append((str(executable), arguments, kwargs))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for name, content in (
            ("worker.stdout.log", "partial worker output\n"),
            ("worker.stderr.log", "original worker stderr\n"),
            ("worker.command.json", '{"returncode": 1}\n'),
        ):
            (self.output_dir / name).write_text(content, encoding="utf-8")
        output = Path(arguments[arguments.index("--output") + 1])
        output.mkdir(parents=True)
        (output / "coarse_calibration_manifest.json").write_text(
            json.dumps(
                {
                    "status": "FAIL",
                    "error": {"message": "worker failed"},
                }
            ),
            encoding="utf-8",
        )
        result = SimpleNamespace(
            returncode=1,
            timed_out=False,
            stderr="original worker stderr\n",
            executable=str(executable),
        )
        raise cli_module.CommandExecutionError(result, "coarse worker failed")


class CoarseMeshCalibrationCliTests(unittest.TestCase):
    def setUp(self) -> None:
        _SuccessfulRunner.instances.clear()
        _FailingRunner.instances.clear()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / "src").mkdir()
        (self.root / "config").mkdir()
        self.config = self.root / "config" / "coarse_mesh.toml"
        self.config.write_text('schema = "test"\n', encoding="utf-8")
        self.config_sha256 = hashlib.sha256(self.config.read_bytes()).hexdigest()
        self.output_root = self.root / "runs" / "mesh" / "coarse"
        self.output_root.mkdir(parents=True)
        self.output = self.output_root / "calibration_025"
        self.projection_manifest = (
            self.output_root / "projection_025" / "projection_manifest.json"
        )
        self.projection_manifest.parent.mkdir()
        self.projection_manifest.write_text(
            '{"schema":"test-projection"}\n', encoding="utf-8"
        )
        self.projection_manifest_sha256 = hashlib.sha256(
            self.projection_manifest.read_bytes()
        ).hexdigest()
        self.local_schedule = (
            self.output_root
            / "local_schedule_001"
            / "boundary_layer_local_schedule.json"
        )
        self.local_schedule.parent.mkdir()
        self.local_schedule.write_text('{"status":"PASS"}\n', encoding="utf-8")
        self.local_schedule_sha256 = hashlib.sha256(
            self.local_schedule.read_bytes()
        ).hexdigest()
        self.contract = {
            "schema": "cfdpipe.coarse_mesh.v1",
            "status": "CONFIGURED",
            "normalized_config_sha256": "a" * 64,
            "calibration_characteristic_lengths_m": [0.25, 0.20],
            "calibration_maximum_3d_elements": 750_000,
            "calibration_maximum_elapsed_seconds": 840.0,
            "resource": {
                "minimum_calibration_samples": 2,
                "safety_factor": 1.3,
                "os_physical_memory_reserve_bytes": 4 * 1024**3,
                "projection_worker_memory_limit_bytes": 2 * 1024**3,
                "calibration_worker_memory_limit_bytes": 3 * 1024**3,
                "worker_monitor_margin_bytes": 512 * 1024**2,
                "minimum_disk_free_bytes": 20 * 1024**3,
                "physical_memory_required": True,
                "pagefile_cannot_rescue_physical_memory_failure": True,
            },
        }
        self.projection_evidence = {
            "schema": "cfdpipe.coarse_mesh_projection_evidence.v1",
            "status": "PASS",
            "path": str(self.projection_manifest.resolve()),
            "sha256": self.projection_manifest_sha256,
            "size_bytes": self.projection_manifest.stat().st_size,
            "output_directory": str(self.projection_manifest.parent.resolve()),
            "config_path": str(self.config.resolve()),
            "config_sha256": self.config_sha256,
            "coarse_contract_sha256": self.contract[
                "normalized_config_sha256"
            ],
            "characteristic_length_m": 0.25,
            "projected_3d_elements": 200_000,
            "strictly_below_300000": True,
            "source_sha256": "f" * 64,
            "gmsh_finalize_called": True,
            "worker_hard_limit_bytes": 2 * 1024**3,
            "worker_job_object_verified": True,
            "mesh_written": False,
            "su2_called": False,
            "paraview_called": False,
            "evidence_sha256": "e" * 64,
        }
        self.snapshot = {
            "physical_total_bytes": 16 * 1024**3,
            "physical_available_bytes": 12 * 1024**3,
            "virtual_available_bytes": 32 * 1024**3,
            "disk_free_bytes": 100 * 1024**3,
        }
        self.patches = (
            mock.patch.object(cli_module, "REPOSITORY_ROOT", self.root),
            mock.patch.object(
                cli_module, "DEFAULT_COARSE_MESH_CONFIG", self.config
            ),
            mock.patch.object(
                cli_module, "DEFAULT_COARSE_MESH_OUTPUT_ROOT", self.output_root
            ),
        )
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.normalize_mock = mock.patch.object(
            cli_module,
            "normalize_coarse_mesh_config",
            return_value=self.contract,
        ).start()
        self.addCleanup(mock.patch.stopall)
        self.snapshot_mock = mock.patch.object(
            cli_module,
            "capture_system_snapshot",
            return_value=self.snapshot,
        ).start()
        self.binding_mock = mock.patch.object(
            cli_module,
            "load_and_bind_boundary_layer_local_schedule",
            side_effect=lambda **kwargs: {
                "schema": "cfdpipe.boundary_layer_local_schedule_binding.v1",
                "status": "PASS",
                "source_plan_path": str(Path(kwargs["plan_path"]).resolve()),
                "source_plan_sha256": kwargs["plan_sha256"],
                "binding_sha256": "b" * 64,
            },
        ).start()
        self.projection_loader_mock = mock.patch.object(
            cli_module,
            "load_and_validate_projection_evidence",
            side_effect=lambda *args, **kwargs: dict(self.projection_evidence),
        ).start()
        self.projection_record_mock = mock.patch.object(
            cli_module,
            "validate_projection_evidence_record",
            side_effect=lambda record, **kwargs: dict(record),
        ).start()
        _SuccessfulRunner.manifest_factory = self._make_success_manifest
        _FailingRunner.manifest_factory = self._make_success_manifest

    def arguments(self) -> list[str]:
        return [
            "pipeline",
            "coarse-mesh-calibrate",
            "--characteristic-length",
            "0.25",
            "--output",
            str(self.output),
            "--projection-manifest",
            str(self.projection_manifest),
            "--projection-sha256",
            self.projection_manifest_sha256,
            "--local-schedule",
            str(self.local_schedule),
            "--local-schedule-sha256",
            self.local_schedule_sha256,
            "--timeout",
            "300",
            "--quiet",
        ]

    def _make_success_manifest(
        self, output: Path, arguments: tuple[str, ...]
    ) -> dict[str, object]:
        output.mkdir(parents=True, exist_ok=True)
        characteristic = float(
            arguments[arguments.index("--characteristic-length") + 1]
        )
        generated_count = (
            int(self.projection_evidence["projected_3d_elements"])
            if characteristic == 0.25
            else 250_000
        )
        output_records: dict[str, dict[str, object]] = {}
        for name, content in (("mesh.msh", b"msh\n"), ("mesh.su2", b"su2\n")):
            path = output / name
            path.write_bytes(content)
            output_records[name] = {
                "path": str(path.resolve()),
                "size_bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        first_point = characteristic == 0.25
        crosscheck = {
            "status": "PASS" if first_point else "NOT_APPLICABLE",
            "required_for_first_configured_point": first_point,
            "projected_3d_elements": self.projection_evidence[
                "projected_3d_elements"
            ],
            "generated_3d_elements": generated_count,
            "exact_match": True if first_point else None,
        }
        if not first_point:
            crosscheck["second_point_uses_first_projection_lineage_only"] = True
        element_types = {"Tetrahedron 4": generated_count}
        marker_counts = {"wall": 1}
        process_semantics = {
            "schema": "cfdpipe.windows_private_commit_semantics.v1",
            "job_object_process_memory_limit_basis": "private_commit_bytes",
            "current_private_commit_metric": "metrics.private_usage_bytes",
            "current_private_commit_source": (
                "PROCESS_MEMORY_COUNTERS_EX.PrivateUsage"
            ),
            "peak_private_commit_metric": "metrics.peak_pagefile_usage_bytes",
            "peak_private_commit_source": (
                "PROCESS_MEMORY_COUNTERS_EX.PeakPagefileUsage"
            ),
            "pagefile_field_means_commit_charge_not_pagefile_residency": True,
            "working_set_is_not_job_process_memory_limit_metric": True,
        }
        process_after_calibration = {
            "schema": "cfdpipe.worker_process_memory.v2",
            "status": "PASS",
            "private_commit_semantics": process_semantics,
            "metrics": {
                "private_usage_bytes": 64 * 1024**2,
                "pagefile_usage_bytes": 64 * 1024**2,
                "peak_pagefile_usage_bytes": 128 * 1024**2,
            },
        }
        memory_snapshots = {
            name: {"status": "PASS"}
            for name in (
                "system_before_limit",
                "process_before_limit",
                "system_after_limit",
                "process_after_limit",
                "system_after_calibration",
                "process_after_calibration",
            )
        }
        memory_snapshots["process_after_calibration"] = process_after_calibration
        schedule_binding = {
            "status": "PASS",
            "source_plan_path": str(self.local_schedule.resolve()),
            "source_plan_sha256": self.local_schedule_sha256,
            "binding_sha256": "b" * 64,
        }
        return {
            "schema": "cfdpipe.coarse_mesh_calibration_manifest.v1",
            "status": "PASS",
            "calibration_only": True,
            "production_mesh_eligible": False,
            "su2_called": False,
            "paraview_called": False,
            "external_commands": [],
            "external_stderr": None,
            "error": None,
            "source_unchanged": True,
            "coarse_contract_sha256": self.contract[
                "normalized_config_sha256"
            ],
            "output_directory": str(output.resolve()),
            "characteristic_length_m": characteristic,
            "projection_evidence": dict(self.projection_evidence),
            "projection_generation_count_crosscheck": crosscheck,
            "gmsh_sessions": {
                phase: {"finalize_called": True, "cleanup_errors": []}
                for phase in ("build", "readback")
            },
            "generation_audit": {
                "status": "PASS",
                "element_count_3d": generated_count,
                "element_types_3d": element_types,
                "marker_face_counts": marker_counts,
            },
            "readback_audit": {
                "status": "PASS",
                "element_count_3d": generated_count,
                "element_types_3d": element_types,
                "marker_face_counts": marker_counts,
            },
            "strategy_config": {
                "normalized_config_sha256": "c" * 64,
                "coarse_contract_sha256": self.contract[
                    "normalized_config_sha256"
                ],
                "local_boundary_layer_schedule": {
                    "source_plan_path": str(self.local_schedule.resolve()),
                    "source_plan_sha256": self.local_schedule_sha256,
                    "binding_sha256": "b" * 64,
                },
            },
            "strategy_config_sha256": "c" * 64,
            "outputs": output_records,
            "su2_validation": {
                "status": "PASS",
                "nelem": generated_count,
                "sha256": output_records["mesh.su2"]["sha256"],
                "file_size_bytes": output_records["mesh.su2"]["size_bytes"],
            },
            "deferred_diagnostic_finalization": {"status": "PASS"},
            "diagnostic_quality_status": "PASS",
            "requires_quality_improvement_before_production": False,
            "resource_sample_eligible": True,
            "resource_sample_eligibility": {
                "quality_gate_pass": True,
                "elapsed_within_limit": True,
                "peak_working_set_valid": True,
            },
            "elapsed_seconds": 1.0,
            "worker_isolation": {
                "schema": "cfdpipe.coarse_mesh_calibration_worker.v1",
                "status": "PASS",
                "config_sha256": self.config_sha256,
                "coarse_contract_sha256": self.contract[
                    "normalized_config_sha256"
                ],
                "projection_evidence": dict(self.projection_evidence),
                "worker_error": None,
                "worker": {
                    "hard_limit_installed_before_heavy_import": True,
                    "heavy_dependencies_imported": True,
                    "normalized_resource_matches_raw": True,
                    "local_schedule_bound_after_heavy_import": True,
                    "builder_called": True,
                    "output_directory": str(output.resolve()),
                },
                "worker_memory": {
                    "hard_limit": {
                        "status": "PASS",
                        "requested_process_memory_limit_bytes": 3 * 1024**3,
                        "job_object": {
                            "process_memory_limit_bytes": 3 * 1024**3,
                            "process_assigned": True,
                            "handle_retained_for_process_lifetime": True,
                        },
                    },
                    **memory_snapshots,
                },
                "local_schedule": {"binding": schedule_binding},
            },
        }

    def test_parser_registers_minimal_worker_command_without_tool_options(self) -> None:
        parsed = cli_module.build_parser().parse_args(self.arguments())

        self.assertEqual(
            parsed.handler.__name__, "_handle_pipeline_coarse_mesh_calibrate"
        )
        self.assertEqual(parsed.config, self.config)
        self.assertEqual(parsed.characteristic_length, 0.25)
        self.assertIsNone(parsed.first_manifest)
        self.assertEqual(self.projection_manifest, parsed.projection_manifest)
        self.assertEqual(
            self.projection_manifest_sha256, parsed.projection_sha256
        )
        self.assertEqual(self.local_schedule, parsed.local_schedule)
        self.assertEqual(
            self.local_schedule_sha256, parsed.local_schedule_sha256
        )
        self.assertEqual(parsed.output, self.output)
        for forbidden in ("tools_config", "gmsh", "su2_cfd", "pvbatch", "nproc"):
            self.assertFalse(hasattr(parsed, forbidden), forbidden)

    def test_handler_uses_absolute_current_python_and_argument_vector(self) -> None:
        with mock.patch.object(cli_module, "CommandRunner", _SuccessfulRunner):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                return_code = cli_module.main(self.arguments())

        self.assertEqual(return_code, 0)
        self.assertEqual(len(_SuccessfulRunner.instances), 1)
        runner = _SuccessfulRunner.instances[0]
        self.assertFalse(runner.live_output)
        executable, arguments, keywords = runner.calls[0]
        self.assertEqual(Path(executable), Path(sys.executable).resolve(strict=True))
        self.assertEqual(
            arguments,
            (
                "-m",
                "cfdpipe.coarse_mesh_worker",
                "--config",
                str(self.config),
                "--config-sha256",
                self.config_sha256,
                "--coarse-contract-sha256",
                self.contract["normalized_config_sha256"],
                "--characteristic-length",
                "0.25",
                "--projection-manifest",
                str(self.projection_manifest.resolve()),
                "--projection-sha256",
                self.projection_manifest_sha256,
                "--output",
                str(self.output),
                "--local-schedule",
                str(self.local_schedule),
                "--local-schedule-sha256",
                self.local_schedule_sha256,
            ),
        )
        self.assertEqual(Path(keywords["cwd"]), self.root)
        self.assertEqual(keywords["env"], {"PYTHONPATH": str(self.root / "src")})
        self.assertEqual(keywords["timeout"], 300.0)
        self.assertTrue(keywords["check"])
        self.assertNotIn("shell", keywords)
        self.assertTrue(callable(keywords["abort_check"]))
        self.assertEqual(0.5, keywords["abort_check_interval_seconds"])
        self.assertEqual(keywords["stdout_log"], "worker.stdout.log")
        self.assertEqual(keywords["stderr_log"], "worker.stderr.log")
        self.assertEqual(keywords["metadata_log"], "worker.command.json")
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "PASS")
        self.assertTrue(Path(payload["preflight"]).is_file())
        preflight = json.loads(
            Path(payload["preflight"]).read_text(encoding="utf-8")
        )
        self.assertTrue(preflight["policy"]["command_runner_constructed"])
        self.assertEqual(
            Path(payload["manifest"]),
            self.output / "coarse_calibration_manifest.json",
        )

    def test_timeout_and_paths_fail_before_constructing_runner(self) -> None:
        invalid_argument_sets = []
        for timeout in ("0", "841", "nan"):
            arguments = self.arguments()
            arguments[arguments.index("--timeout") + 1] = timeout
            invalid_argument_sets.append(arguments)
        root_output = self.arguments()
        root_output[root_output.index("--output") + 1] = str(self.output_root)
        invalid_argument_sets.append(root_output)
        escaped = self.arguments()
        escaped[escaped.index("--output") + 1] = str(
            self.output_root / ".." / "escape"
        )
        invalid_argument_sets.append(escaped)
        wrong_config = self.arguments() + ["--config", str(self.root / "wrong.toml")]
        invalid_argument_sets.append(wrong_config)

        with mock.patch.object(cli_module, "CommandRunner") as runner:
            for arguments in invalid_argument_sets:
                with self.subTest(arguments=arguments):
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr):
                        self.assertEqual(cli_module.main(arguments), 2)
            runner.assert_not_called()

    def test_nonpositive_or_nonfinite_size_fails_before_worker(self) -> None:
        with mock.patch.object(cli_module, "CommandRunner") as runner:
            for value in ("0", "-0.1", "nan", "inf"):
                arguments = self.arguments()
                arguments[arguments.index("--characteristic-length") + 1] = value
                with self.subTest(value=value):
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(cli_module.main(arguments), 2)
            runner.assert_not_called()

    def test_worker_failure_keeps_runner_logs_and_worker_manifest(self) -> None:
        with mock.patch.object(cli_module, "CommandRunner", _FailingRunner):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                return_code = cli_module.main(self.arguments())

        self.assertEqual(return_code, 1)
        runner = _FailingRunner.instances[0]
        self.assertEqual(
            runner.output_dir,
            self.output_root / "_worker_evidence" / self.output.name,
        )
        self.assertEqual(
            (runner.output_dir / "worker.stderr.log").read_text(encoding="utf-8"),
            "original worker stderr\n",
        )
        self.assertTrue((runner.output_dir / "worker.command.json").is_file())
        manifest = json.loads(
            (self.output / "coarse_calibration_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(manifest["status"], "FAIL")
        self.assertIn("original worker stderr", stderr.getvalue())
        self.assertIn(str(runner.output_dir), stderr.getvalue())

    def test_zero_return_without_strict_pass_manifest_fails_closed(self) -> None:
        class MissingManifestRunner(_SuccessfulRunner):
            instances = []

            def run(self, executable, args=(), **kwargs):
                arguments = tuple(str(value) for value in args)
                self.calls.append((str(executable), arguments, kwargs))
                return SimpleNamespace(
                    returncode=0,
                    timed_out=False,
                    stderr="",
                    executable=str(executable),
                    as_metadata=lambda: {},
                )

        with mock.patch.object(cli_module, "CommandRunner", MissingManifestRunner):
            with contextlib.redirect_stderr(io.StringIO()):
                return_code = cli_module.main(self.arguments())

        self.assertEqual(return_code, 2)

    def test_zero_return_with_old_minimal_pass_manifest_fails_closed(self) -> None:
        class MinimalManifestRunner(_SuccessfulRunner):
            instances = []

            def run(self, executable, args=(), **kwargs):
                arguments = tuple(str(value) for value in args)
                self.calls.append((str(executable), arguments, kwargs))
                output = Path(arguments[arguments.index("--output") + 1])
                output.mkdir(parents=True)
                (output / "coarse_calibration_manifest.json").write_text(
                    json.dumps(
                        {
                            "schema": (
                                "cfdpipe.coarse_mesh_calibration_manifest.v1"
                            ),
                            "status": "PASS",
                            "calibration_only": True,
                            "production_mesh_eligible": False,
                            "su2_called": False,
                            "paraview_called": False,
                        }
                    ),
                    encoding="utf-8",
                )
                return SimpleNamespace(
                    returncode=0,
                    timed_out=False,
                    stderr="",
                    executable=str(executable),
                    as_metadata=lambda: {},
                )

        with mock.patch.object(cli_module, "CommandRunner", MinimalManifestRunner):
            with contextlib.redirect_stderr(io.StringIO()):
                return_code = cli_module.main(self.arguments())

        self.assertEqual(return_code, 2)

    def test_low_memory_preflight_is_persisted_without_constructing_runner(self) -> None:
        self.snapshot_mock.return_value = {
            **self.snapshot,
            "physical_available_bytes": 7 * 1024**3,
            "virtual_available_bytes": 64 * 1024**3,
        }
        with mock.patch.object(cli_module, "CommandRunner") as runner:
            with contextlib.redirect_stderr(io.StringIO()):
                return_code = cli_module.main(self.arguments())

        self.assertEqual(2, return_code)
        runner.assert_not_called()
        report_path = (
            self.output_root
            / "_worker_evidence"
            / self.output.name
            / "calibration_preflight.json"
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual("FAIL", report["status"])
        codes = {item["code"] for item in report["reasons"]}
        self.assertIn("PHYSICAL_AVAILABLE_INSUFFICIENT", codes)
        self.assertIn("PAGEFILE_NOT_ACCEPTED", codes)
        self.assertFalse(report["policy"]["command_runner_constructed"])

    def test_unconfigured_characteristic_length_never_constructs_runner(self) -> None:
        arguments = self.arguments()
        arguments[arguments.index("--characteristic-length") + 1] = "0.22"
        with mock.patch.object(cli_module, "CommandRunner") as runner:
            with contextlib.redirect_stderr(io.StringIO()):
                return_code = cli_module.main(arguments)

        self.assertEqual(2, return_code)
        runner.assert_not_called()
        report = json.loads(
            (
                self.output_root
                / "_worker_evidence"
                / self.output.name
                / "calibration_preflight.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            "CHARACTERISTIC_LENGTH_NOT_AUTHORIZED",
            report["reasons"][0]["code"],
        )

    def test_missing_local_schedule_fails_before_resource_snapshot_and_runner(self) -> None:
        arguments = self.arguments()
        arguments[arguments.index("--local-schedule") + 1] = str(
            self.output_root / "missing" / "boundary_layer_local_schedule.json"
        )
        with mock.patch.object(cli_module, "CommandRunner") as runner:
            with contextlib.redirect_stderr(io.StringIO()):
                return_code = cli_module.main(arguments)

        self.assertEqual(2, return_code)
        runner.assert_not_called()
        self.snapshot_mock.assert_not_called()
        report = json.loads(
            (
                self.output_root
                / "_worker_evidence"
                / self.output.name
                / "calibration_preflight.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual("LOCAL_SCHEDULE", report["stage"])
        self.assertEqual("LOCAL_SCHEDULE_INVALID", report["reasons"][0]["code"])

    def _second_arguments(self, output: Path, manifest: Path | None = None) -> list[str]:
        arguments = [
            "pipeline",
            "coarse-mesh-calibrate",
            "--characteristic-length",
            "0.20",
            "--output",
            str(output),
            "--projection-manifest",
            str(self.projection_manifest),
            "--projection-sha256",
            self.projection_manifest_sha256,
            "--local-schedule",
            str(self.local_schedule),
            "--local-schedule-sha256",
            self.local_schedule_sha256,
            "--timeout",
            "840",
            "--quiet",
        ]
        if manifest is not None:
            arguments.extend(("--first-manifest", str(manifest)))
        return arguments

    def _write_first_manifest(self) -> Path:
        first_output = self.output_root / "first_pass"
        first_output.mkdir(parents=True)
        path = first_output / "coarse_calibration_manifest.json"
        manifest = self._make_success_manifest(
            first_output,
            ("--characteristic-length", "0.25"),
        )
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def test_second_point_requires_explicit_valid_first_manifest(self) -> None:
        missing_output = self.output_root / "second_missing"
        with mock.patch.object(cli_module, "CommandRunner") as runner:
            with contextlib.redirect_stderr(io.StringIO()):
                return_code = cli_module.main(
                    self._second_arguments(missing_output)
                )
        self.assertEqual(2, return_code)
        runner.assert_not_called()
        missing_report = json.loads(
            (
                self.output_root
                / "_worker_evidence"
                / missing_output.name
                / "calibration_preflight.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual("FIRST_MANIFEST_REQUIRED", missing_report["reasons"][0]["code"])

        bad_directory = self.output_root / "bad_first"
        bad_directory.mkdir(parents=True)
        bad_manifest = bad_directory / "coarse_calibration_manifest.json"
        bad_manifest.write_text("{}", encoding="utf-8")
        bad_output = self.output_root / "second_bad"
        with mock.patch.object(cli_module, "CommandRunner") as runner:
            with contextlib.redirect_stderr(io.StringIO()):
                return_code = cli_module.main(
                    self._second_arguments(bad_output, bad_manifest)
                )
        self.assertEqual(2, return_code)
        runner.assert_not_called()
        bad_report = json.loads(
            (
                self.output_root
                / "_worker_evidence"
                / bad_output.name
                / "calibration_preflight.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual("FIRST_MANIFEST_INVALID", bad_report["reasons"][0]["code"])

    def test_second_point_pass_keeps_worker_argument_vector_unchanged(self) -> None:
        first_manifest = self._write_first_manifest()
        second_output = self.output_root / "calibration_020"
        with mock.patch.object(cli_module, "CommandRunner", _SuccessfulRunner):
            with contextlib.redirect_stdout(io.StringIO()):
                return_code = cli_module.main(
                    self._second_arguments(second_output, first_manifest)
                )

        self.assertEqual(0, return_code)
        runner = _SuccessfulRunner.instances[0]
        _, arguments, _ = runner.calls[0]
        self.assertEqual(
            (
                "-m",
                "cfdpipe.coarse_mesh_worker",
                "--config",
                str(self.config),
                "--config-sha256",
                self.config_sha256,
                "--coarse-contract-sha256",
                self.contract["normalized_config_sha256"],
                "--characteristic-length",
                "0.20000000000000001",
                "--projection-manifest",
                str(self.projection_manifest.resolve()),
                "--projection-sha256",
                self.projection_manifest_sha256,
                "--output",
                str(second_output),
                "--local-schedule",
                str(self.local_schedule),
                "--local-schedule-sha256",
                self.local_schedule_sha256,
            ),
            arguments,
        )
        self.assertNotIn("--first-manifest", arguments)
        preflight = json.loads(
            (
                self.output_root
                / "_worker_evidence"
                / second_output.name
                / "calibration_preflight.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual("PASS", preflight["status"])
        self.assertEqual(
            str(first_manifest.resolve()),
            preflight["inputs"]["first_manifest_evidence"]["path"],
        )


if __name__ == "__main__":
    unittest.main()
