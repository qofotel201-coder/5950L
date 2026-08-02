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


GIB = 1024**3
MIB = 1024**2


class _SuccessfulRunner:
    instances: list["_SuccessfulRunner"] = []

    def __init__(self, output_dir, **kwargs) -> None:
        self.output_dir = Path(output_dir)
        self.kwargs = kwargs
        self.calls = []
        self.__class__.instances.append(self)

    def run(self, executable, args=(), **kwargs):
        arguments = tuple(str(value) for value in args)
        self.calls.append((str(executable), arguments, kwargs))
        output = Path(arguments[arguments.index("--output") + 1])
        output.mkdir()
        (output / "projection_manifest.json").write_text(
            json.dumps(
                {
                    "schema": "cfdpipe.coarse_mesh_projection_manifest.v1",
                    "status": "PASS",
                    "projection_only": True,
                    "calibration_PASS_authorized": False,
                    "production": False,
                    "mesh_written": False,
                    "su2_called": False,
                    "paraview_called": False,
                    "external_commands": [],
                    "source_unchanged": True,
                    "gmsh_session": {
                        "finalize_called": True,
                        "cleanup_errors": [],
                    },
                    "worker": {
                        "hard_limit_installed_before_heavy_import": True,
                    },
                    "projection": {
                        "projection_gate": {
                            "status": "PASS",
                            "strictly_below_cap": True,
                            "projected_3d_elements": 275_000,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(
            returncode=0,
            stderr="",
            as_metadata=lambda: {
                "executable": str(executable),
                "args": list(arguments),
                "cwd": str(kwargs["cwd"]),
                "returncode": 0,
            },
        )


class CoarseProjectionCliTests(unittest.TestCase):
    def setUp(self) -> None:
        _SuccessfulRunner.instances.clear()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / "src").mkdir()
        (self.root / "config").mkdir()
        self.config = self.root / "config" / "coarse_mesh.toml"
        self.config.write_text('schema = "test"\n', encoding="utf-8")
        self.output_root = self.root / "runs" / "mesh" / "coarse"
        self.output_root.mkdir(parents=True)
        self.output = self.output_root / "projection_030"
        self.contract = {
            "schema": "cfdpipe.coarse_mesh.v1",
            "status": "CONFIGURED",
            "normalized_config_sha256": "a" * 64,
            "calibration_characteristic_lengths_m": [0.30, 0.25],
            "resource": {
                "os_physical_memory_reserve_bytes": 4 * GIB,
                "projection_worker_memory_limit_bytes": 2 * GIB,
                "worker_monitor_margin_bytes": 512 * MIB,
                "minimum_disk_free_bytes": 20 * GIB,
                "physical_memory_required": True,
                "pagefile_cannot_rescue_physical_memory_failure": True,
            },
        }
        self.snapshot = {
            "physical_total_bytes": 16 * GIB,
            "physical_available_bytes": 10 * GIB,
            "virtual_available_bytes": 32 * GIB,
            "disk_free_bytes": 100 * GIB,
            "disk_path": str(self.root),
        }
        patches = (
            mock.patch.object(cli_module, "REPOSITORY_ROOT", self.root),
            mock.patch.object(
                cli_module, "DEFAULT_COARSE_MESH_CONFIG", self.config
            ),
            mock.patch.object(
                cli_module, "DEFAULT_COARSE_MESH_OUTPUT_ROOT", self.output_root
            ),
            mock.patch.object(
                cli_module,
                "normalize_coarse_mesh_config",
                return_value=self.contract,
            ),
            mock.patch.object(
                cli_module, "capture_system_snapshot", return_value=self.snapshot
            ),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.config_sha256 = hashlib.sha256(self.config.read_bytes()).hexdigest()
        self.projection_validation_mock = mock.patch.object(
            cli_module,
            "load_and_validate_projection_evidence",
            return_value={
                "status": "PASS",
                "projected_3d_elements": 275_000,
            },
        ).start()
        self.addCleanup(mock.patch.stopall)

    def arguments(self) -> list[str]:
        return [
            "pipeline",
            "coarse-mesh-project",
            "--config",
            str(self.config),
            "--characteristic-length",
            "0.30",
            "--output",
            str(self.output),
            "--timeout",
            "300",
            "--quiet",
        ]

    def test_parser_exposes_only_projection_inputs(self) -> None:
        parsed = cli_module.build_parser().parse_args(self.arguments())
        self.assertEqual(
            "_handle_pipeline_coarse_mesh_project", parsed.handler.__name__
        )
        self.assertEqual(0.30, parsed.characteristic_length)
        for forbidden in ("gmsh", "su2_cfd", "pvbatch", "nproc"):
            self.assertFalse(hasattr(parsed, forbidden), forbidden)

    def test_pass_launches_absolute_python_argument_vector_without_shell(self) -> None:
        with mock.patch.object(cli_module, "CommandRunner", _SuccessfulRunner):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = cli_module.main(self.arguments())

        self.assertEqual(0, code)
        runner = _SuccessfulRunner.instances[0]
        executable, arguments, keywords = runner.calls[0]
        self.assertEqual(Path(sys.executable).resolve(), Path(executable))
        self.assertEqual(
            (
                "-m",
                "cfdpipe.coarse_projection_worker",
                "--config",
                str(self.config),
                "--config-sha256",
                self.config_sha256,
                "--coarse-contract-sha256",
                self.contract["normalized_config_sha256"],
                "--characteristic-length",
                "0.29999999999999999",
                "--output",
                str(self.output),
            ),
            arguments,
        )
        self.assertNotIn("shell", keywords)
        self.assertEqual({"PYTHONPATH": str(self.root / "src")}, keywords["env"])
        self.assertTrue(callable(keywords["abort_check"]))
        self.assertEqual(0.5, keywords["abort_check_interval_seconds"])
        payload = json.loads(stdout.getvalue())
        self.assertEqual("PASS", payload["status"])
        self.assertEqual(275_000, payload["projected_3d_elements"])
        preflight = json.loads(Path(payload["preflight"]).read_text(encoding="utf-8"))
        self.assertTrue(preflight["controller"]["command_runner_constructed"])

    def test_low_physical_memory_persists_fail_and_never_constructs_runner(self) -> None:
        cli_module.capture_system_snapshot.return_value = {  # type: ignore[attr-defined]
            **self.snapshot,
            "physical_available_bytes": 6 * GIB,
            "virtual_available_bytes": 128 * GIB,
        }
        with mock.patch.object(cli_module, "CommandRunner") as runner:
            with contextlib.redirect_stderr(io.StringIO()):
                code = cli_module.main(self.arguments())

        self.assertEqual(2, code)
        runner.assert_not_called()
        report = json.loads(
            (
                self.output_root
                / "_worker_evidence"
                / self.output.name
                / "projection_preflight.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual("FAIL", report["status"])
        codes = {record["code"] for record in report["reasons"]}
        self.assertIn("PHYSICAL_AVAILABLE_INSUFFICIENT", codes)
        self.assertIn("PAGEFILE_NOT_ACCEPTED", codes)
        self.assertFalse(self.output.exists())

    def test_runtime_monitor_protects_reserve_plus_margin(self) -> None:
        monitor = cli_module._make_physical_memory_abort_check(self.contract)
        with mock.patch.object(
            cli_module,
            "capture_system_physical_memory",
            return_value={
                "status": "PASS",
                "metrics": {"physical_available_bytes": 4 * GIB},
            },
        ):
            reason = monitor()
        self.assertIsInstance(reason, str)
        self.assertIn(str(4 * GIB + 512 * MIB), reason)

        with mock.patch.object(
            cli_module,
            "capture_system_physical_memory",
            return_value={
                "status": "PASS",
                "metrics": {"physical_available_bytes": 5 * GIB},
            },
        ):
            self.assertIsNone(monitor())

    def test_bad_path_size_and_timeout_fail_before_runner(self) -> None:
        cases: list[list[str]] = []
        for option, value in (
            ("--characteristic-length", "nan"),
            ("--characteristic-length", "0"),
            ("--timeout", "0"),
            ("--timeout", "841"),
        ):
            arguments = self.arguments()
            arguments[arguments.index(option) + 1] = value
            cases.append(arguments)
        nested = self.arguments()
        nested[nested.index("--output") + 1] = str(
            self.output_root / "nested" / "projection"
        )
        cases.append(nested)

        with mock.patch.object(cli_module, "CommandRunner") as runner:
            for arguments in cases:
                with self.subTest(arguments=arguments):
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(2, cli_module.main(arguments))
            runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
