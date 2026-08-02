from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cfdpipe.bridges.paraview_bridge import (
    ParaViewBridge,
    ParaViewBridgeError,
    visualization_from_manifest,
)
from cfdpipe.process import CommandResult, CommandTimeoutError


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONNECTION_OUTPUT = REPOSITORY_ROOT / "runs" / "connection"


class ParaViewBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = mock.Mock()
        CONNECTION_OUTPUT.mkdir(parents=True, exist_ok=True)
        self.temp_directory = tempfile.TemporaryDirectory(dir=CONNECTION_OUTPUT)
        self.addCleanup(self.temp_directory.cleanup)
        self.root = Path(self.temp_directory.name).resolve()
        self.script_dir = self.root / "scripts"
        self.script_dir.mkdir()
        for filename in (
            "probe_pvbatch.py",
            "inspect_solution.py",
            "smoke_postprocess.py",
        ):
            (self.script_dir / filename).write_text(
                "# pvbatch-only test fixture\n", encoding="utf-8"
            )
        self.pvbatch = self.root / "pvbatch"
        self.pvbatch.write_bytes(b"test pvbatch executable\n")

    def _result(
        self,
        *,
        name: str = "pvbatch",
        args: tuple[str, ...] = (),
        returncode: int | None = 0,
        stdout: str = "",
        stderr: str = "",
        timed_out: bool = False,
    ) -> CommandResult:
        stdout_log = self.root / f"{name}.stdout.log"
        stderr_log = self.root / f"{name}.stderr.log"
        metadata_log = self.root / f"{name}.command.json"
        stdout_log.write_text(stdout, encoding="utf-8")
        stderr_log.write_text(stderr, encoding="utf-8")
        metadata_log.write_text("{}\n", encoding="utf-8")
        return CommandResult(
            executable=str(self.pvbatch),
            args=args,
            cwd=str(self.root),
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

    def _bridge(self) -> ParaViewBridge:
        return ParaViewBridge(self.runner, self.pvbatch, self.script_dir)

    def _visualization_manifest(
        self,
        *,
        status: str = "PASS",
        visualization_file: str | None = "solution.vtu",
    ) -> Path:
        manifest = self.root / "run_manifest.json"
        payload: dict[str, object] = {"status": status}
        if visualization_file is not None:
            payload["visualization_file"] = visualization_file
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        return manifest

    def test_build_command_uses_argument_list_and_optional_offscreen_flag(self) -> None:
        command = self._bridge().build_command(
            "inspect_solution.py",
            ("--input", "solution.vtu", "--output", "inventory.json"),
            pvbatch_options=("--force-offscreen-rendering",),
        )

        self.assertEqual(
            command,
            [
                str(self.pvbatch),
                "--force-offscreen-rendering",
                str((self.script_dir / "inspect_solution.py").resolve()),
                "--input",
                "solution.vtu",
                "--output",
                "inventory.json",
            ],
        )

    def test_bridge_rejects_paraview_gui_and_pvpython_basenames(self) -> None:
        for executable in ("paraview.exe", "pvpython.exe", "pvbatch-helper.exe"):
            with self.subTest(executable=executable):
                with self.assertRaisesRegex(ParaViewBridgeError, "pvbatch only"):
                    ParaViewBridge(
                        self.runner,
                        self.root / executable,
                        self.script_dir,
                    )

    def test_inspect_compatibility_wrapper_runs_selected_script(self) -> None:
        expected = object()
        self.runner.run.return_value = expected

        result = self._bridge().inspect(
            ["--input", "solution.vtu"],
            cwd=self.root,
            dry_run=True,
        )

        self.assertIs(result, expected)
        self.runner.run.assert_called_once_with(
            str(self.pvbatch),
            args=[
                str((self.script_dir / "inspect_solution.py").resolve()),
                "--input",
                "solution.vtu",
            ],
            cwd=self.root,
            env=None,
            timeout=None,
            check=True,
            live_output=None,
            dry_run=True,
            name="paraview-inspect",
        )
        self.assertNotIn("shell", self.runner.run.call_args.kwargs)

    def test_smoke_compatibility_wrapper_selects_postprocess_script(self) -> None:
        self.runner.run.return_value = object()

        self._bridge().smoke()

        called_arguments = self.runner.run.call_args.kwargs["args"]
        self.assertEqual(
            called_arguments,
            [str((self.script_dir / "smoke_postprocess.py").resolve())],
        )

    def test_run_script_rejects_a_single_pathlike_argument(self) -> None:
        bridge = self._bridge()

        for invalid_args in ("--input", b"--input", Path("solution.vtu")):
            with self.subTest(args=invalid_args):
                with self.assertRaisesRegex(TypeError, "sequence"):
                    bridge.run_script("inspect_solution.py", invalid_args)

        self.runner.run.assert_not_called()

    def test_manifest_input_resolves_only_declared_relative_visualization(self) -> None:
        expected = self.root / "solution.vtu"
        expected.write_bytes(b"vtk output")
        manifest = self._visualization_manifest()

        actual, payload = visualization_from_manifest(manifest)

        self.assertEqual(actual, expected.resolve())
        self.assertEqual(payload["visualization_file"], "solution.vtu")

    def test_manifest_without_visualization_file_is_rejected(self) -> None:
        manifest = self._visualization_manifest(visualization_file=None)

        with self.assertRaisesRegex(
            ParaViewBridgeError, "lacks visualization_file"
        ):
            visualization_from_manifest(manifest)

    def test_manifest_must_report_pass(self) -> None:
        manifest = self._visualization_manifest(status="FAIL")

        with self.assertRaisesRegex(ParaViewBridgeError, "status is not PASS"):
            visualization_from_manifest(manifest)

    def test_manifest_rejects_nonexistent_empty_and_unsupported_input(self) -> None:
        cases = (
            ("missing.vtu", None, "does not exist"),
            ("empty.vtu", b"", "missing or empty"),
            ("solution.csv", b"not a visualization", "must be .vtk"),
        )
        for filename, contents, expected_message in cases:
            with self.subTest(filename=filename):
                if contents is not None:
                    (self.root / filename).write_bytes(contents)
                manifest = self._visualization_manifest(
                    visualization_file=filename
                )
                with self.assertRaisesRegex(
                    ParaViewBridgeError, expected_message
                ):
                    visualization_from_manifest(manifest)

    def test_run_pvbatch_uses_fixed_logs_timeout_and_no_shell_keyword(self) -> None:
        output = self.root / "inventory.json"
        output.write_text(
            json.dumps({"number_of_points": 4, "number_of_cells": 2}),
            encoding="utf-8",
        )
        expected = self._result(args=("inspect_solution.py",))
        self.runner.run.return_value = expected

        result = self._bridge().run_pvbatch(
            "inspect_solution.py",
            ("--input", "solution.vtu", "--output", str(output)),
            self.root,
            timeout_seconds=17,
            live_output=False,
        )

        self.assertIs(result, expected)
        kwargs = self.runner.run.call_args.kwargs
        self.assertEqual(self.runner.run.call_args.args, (str(self.pvbatch),))
        self.assertEqual(kwargs["timeout"], 17.0)
        self.assertFalse(kwargs["check"])
        self.assertEqual(kwargs["stdout_log"], self.root / "pvbatch.stdout.log")
        self.assertEqual(kwargs["stderr_log"], self.root / "pvbatch.stderr.log")
        self.assertEqual(kwargs["metadata_log"], self.root / "pvbatch.command.json")
        self.assertNotIn("shell", kwargs)

    def test_nonzero_return_code_preserves_original_stderr(self) -> None:
        failed = self._result(
            returncode=9,
            stderr="reader failure: unsupported data array\n",
        )
        self.runner.run.return_value = failed

        with self.assertRaises(ParaViewBridgeError) as raised:
            self._bridge().run_pvbatch(
                "inspect_solution.py", (), self.root
            )

        self.assertIs(raised.exception.result, failed)
        self.assertEqual(
            raised.exception.stderr,
            "reader failure: unsupported data array\n",
        )
        self.assertIn("reader failure", str(raised.exception))

    def test_timeout_error_is_propagated_with_result(self) -> None:
        timed_out = self._result(
            returncode=-9,
            stderr="partial pvbatch stderr\n",
            timed_out=True,
        )
        timeout_error = CommandTimeoutError(timed_out, 3.5)
        self.runner.run.side_effect = timeout_error

        with self.assertRaises(CommandTimeoutError) as raised:
            self._bridge().run_pvbatch(
                "inspect_solution.py", (), self.root, timeout_seconds=3.5
            )

        self.assertIs(raised.exception, timeout_error)
        self.assertEqual(self.runner.run.call_args.kwargs["timeout"], 3.5)

    def test_help_timeout_disables_optional_flags_but_preserves_evidence(self) -> None:
        timed_out = self._result(
            name="help-timeout",
            returncode=-9,
            timed_out=True,
        )
        self.runner.run.side_effect = CommandTimeoutError(timed_out, 10.0)

        result, option, warning = self._bridge()._run_help(
            self.root, timeout_seconds=300
        )

        self.assertIs(result, timed_out)
        self.assertIsNone(option)
        self.assertIn("no offscreen option", warning or "")
        self.assertEqual(self.runner.run.call_args.kwargs["timeout"], 10.0)

    def test_invalid_json_output_is_rejected(self) -> None:
        output = self.root / "invalid.json"
        output.write_text("{not-json", encoding="utf-8")
        self.runner.run.return_value = self._result()

        with self.assertRaisesRegex(ParaViewBridgeError, "not valid UTF-8 JSON"):
            self._bridge().run_pvbatch(
                "inspect_solution.py",
                ("--output", str(output)),
                self.root,
            )

    def test_zero_point_and_cell_json_is_rejected(self) -> None:
        output = self.root / "empty-dataset.json"
        output.write_text(
            json.dumps({"number_of_points": 0, "number_of_cells": 0}),
            encoding="utf-8",
        )
        self.runner.run.return_value = self._result()

        with self.assertRaisesRegex(
            ParaViewBridgeError, "zero points and zero cells"
        ):
            self._bridge().run_pvbatch(
                "inspect_solution.py",
                ("--output", str(output)),
                self.root,
            )

    def test_complete_manifest_workflow_passes_with_mock_pvbatch(self) -> None:
        input_path = self.root / "connection_solution.vtu"
        input_path.write_bytes(b"read-only SU2 visualization fixture")
        source_manifest = self._visualization_manifest(
            visualization_file=str(input_path)
        )
        output_dir = self.root / "postprocess"

        def run_side_effect(executable: str, **kwargs: object) -> CommandResult:
            self.assertEqual(executable, str(self.pvbatch))
            self.assertNotIn("shell", kwargs)
            arguments = [str(value) for value in kwargs.get("args", [])]
            stdout_log = Path(kwargs["stdout_log"])
            stderr_log = Path(kwargs["stderr_log"])
            metadata_log = Path(kwargs["metadata_log"])
            stdout_log.parent.mkdir(parents=True, exist_ok=True)
            stdout_log.write_text("mock pvbatch stdout\n", encoding="utf-8")
            stderr_log.write_bytes(b"")
            metadata_log.write_text("{}\n", encoding="utf-8")

            if arguments == ["--help"]:
                stdout = "usage: pvbatch [--force-offscreen-rendering]\n"
            else:
                stdout = ""
                script_name = next(
                    Path(value).name
                    for value in arguments
                    if value.endswith(".py")
                )
                if script_name == "probe_pvbatch.py":
                    output = Path(arguments[arguments.index("--output") + 1])
                    output.write_text(
                        json.dumps(
                            {
                                "status": "PASS",
                                "paraview_version": "5.13.2",
                            }
                        ),
                        encoding="utf-8",
                    )
                elif script_name == "inspect_solution.py":
                    output = Path(arguments[arguments.index("--output") + 1])
                    output.write_text(
                        json.dumps(
                            {
                                "status": "PASS",
                                "paraview_version": "5.13.2",
                                "dataset_type": "vtkUnstructuredGrid",
                                "block_count": 1,
                                "number_of_points": 994,
                                "number_of_cells": 1866,
                                "bounds": [0.0, 1.0, 0.0, 0.5, 0.0, 0.0],
                                "point_data_arrays": [
                                    {"name": "Pressure", "components": 1}
                                ],
                                "cell_data_arrays": [],
                                "field_data_arrays": [],
                            }
                        ),
                        encoding="utf-8",
                    )
                elif script_name == "smoke_postprocess.py":
                    directory = Path(
                        arguments[arguments.index("--output-directory") + 1]
                    )
                    (directory / "smoke_postprocess.json").write_text(
                        json.dumps(
                            {
                                "status": "PASS",
                                "paraview_version": "5.13.2",
                                "total_points": 994,
                                "total_cells": 1866,
                                "bounds": [0.0, 1.0, 0.0, 0.5, 0.0, 0.0],
                                "integrable_arrays": [
                                    {"name": "Pressure", "association": "point"}
                                ],
                            }
                        ),
                        encoding="utf-8",
                    )
                    (directory / "smoke_integral.csv").write_text(
                        "Pressure\n101325.0\n", encoding="utf-8"
                    )
                else:  # pragma: no cover - makes an unexpected script explicit.
                    self.fail(f"unexpected pvbatch script: {script_name}")

            return CommandResult(
                executable=executable,
                args=tuple(arguments),
                cwd=str(kwargs["cwd"]),
                start_time="2026-08-01T00:00:00Z",
                end_time="2026-08-01T00:00:01Z",
                returncode=0,
                stdout=stdout,
                stderr="",
                stdout_log=stdout_log,
                stderr_log=stderr_log,
                metadata_log=metadata_log,
                dry_run=False,
                timed_out=False,
            )

        self.runner.run.side_effect = run_side_effect

        manifest = self._bridge().inspect_manifest(
            source_manifest,
            output_dir,
            timeout_seconds=19,
            live_output=False,
        )

        self.assertEqual(manifest["status"], "PASS")
        self.assertEqual(manifest["paraview_version"], "5.13.2")
        self.assertEqual(manifest["dataset_type"], "vtkUnstructuredGrid")
        self.assertEqual(manifest["number_of_points"], 994)
        self.assertEqual(manifest["number_of_cells"], 1866)
        self.assertEqual(manifest["return_code"], 0)
        self.assertEqual(len(manifest["commands"]), 4)
        self.assertEqual(
            manifest["offscreen_option"], "--force-offscreen-rendering"
        )
        self.assertTrue((output_dir / "solution_inventory.json").is_file())
        self.assertTrue((output_dir / "smoke_postprocess.json").is_file())
        self.assertTrue((output_dir / "smoke_integral.csv").is_file())
        persisted = json.loads(
            (output_dir / "paraview_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(persisted["status"], "PASS")
        self.assertEqual(persisted["input_solution_path"], str(input_path))
        self.assertEqual(input_path.read_bytes(), b"read-only SU2 visualization fixture")

    def test_ordinary_python_package_never_imports_paraview(self) -> None:
        for source_path in (REPOSITORY_ROOT / "src" / "cfdpipe").rglob("*.py"):
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                with self.subTest(source=source_path, imports=names):
                    self.assertFalse(
                        any(name == "paraview" or name.startswith("paraview.") for name in names),
                        f"ordinary Python imports ParaView in {source_path}",
                    )


if __name__ == "__main__":
    unittest.main()
