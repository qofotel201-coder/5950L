"""Focused tests for the real end-to-end connection-test orchestrator."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from cfdpipe.pipeline import (
    CONNECTION_KEYS,
    ConnectionTestError,
    ConnectionTestRunner,
    build_connection_report,
    check_connection_tools,
    clean_connection_artifacts,
    write_ownership_ledger,
)
from cfdpipe.toolchain import ResolvedTool


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONNECTION_OUTPUT = REPOSITORY_ROOT / "runs" / "connection"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _command_record(
    executable: Path,
    arguments: list[str],
    cwd: Path,
    *,
    returncode: int = 0,
    timed_out: bool = False,
) -> dict[str, object]:
    command = [str(executable), *arguments]
    return {
        "executable": str(executable),
        "args": list(arguments),
        "command": command,
        "cwd": str(cwd),
        "start_time": "2026-08-01T00:00:01+00:00",
        "end_time": "2026-08-01T00:00:02+00:00",
        "returncode": returncode,
        "dry_run": False,
        "timed_out": timed_out,
        "interrupted": False,
        "output_complete": True,
        "runner_error": None,
    }


def _file_record(path: Path) -> dict[str, object]:
    return {
        "path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


class ConnectPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        CONNECTION_OUTPUT.mkdir(parents=True, exist_ok=True)
        self.temporary_directory = tempfile.TemporaryDirectory(
            dir=CONNECTION_OUTPUT
        )
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()

    def test_clean_preserves_unknown_files_pip_install_and_discovery(self) -> None:
        managed = self.root / "gmsh" / "smoke.su2"
        pip_install = self.root / "gmsh" / "pip_install.log"
        discovery = self.root / "su2" / "discovery" / "reference.cfg"
        unknown = self.root / "keep-me.txt"
        for path, content in (
            (managed, "managed mesh"),
            (pip_install, "bootstrap provenance"),
            (discovery, "discovery evidence"),
            (unknown, "user file"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        result = clean_connection_artifacts(
            self.root,
            allowed_root=self.root,
            trusted_root=self.root,
        )

        self.assertEqual(result["status"], "PASS")
        self.assertIn("gmsh/smoke.su2", result["removed"])
        self.assertFalse(managed.exists())
        self.assertEqual(pip_install.read_text(encoding="utf-8"), "bootstrap provenance")
        self.assertEqual(discovery.read_text(encoding="utf-8"), "discovery evidence")
        self.assertEqual(unknown.read_text(encoding="utf-8"), "user file")
        self.assertTrue(result["unknown_files_preserved"])

    def test_ledger_hash_change_fails_full_preflight_without_partial_delete(self) -> None:
        first = self.root / "gmsh" / "smoke.su2"
        changed = self.root / "su2" / "history.csv"
        first.parent.mkdir(parents=True, exist_ok=True)
        changed.parent.mkdir(parents=True, exist_ok=True)
        first.write_text("first managed artifact", encoding="utf-8")
        changed.write_text("original history", encoding="utf-8")
        ledger = write_ownership_ledger(
            self.root,
            (first, changed),
            trusted_root=self.root,
        )
        changed.write_text("history changed after ledger", encoding="utf-8")

        with self.assertRaisesRegex(
            ValueError, "owned artifact changed since ledger creation"
        ):
            clean_connection_artifacts(
                self.root,
                allowed_root=self.root,
                trusted_root=self.root,
            )

        self.assertEqual(first.read_text(encoding="utf-8"), "first managed artifact")
        self.assertEqual(
            changed.read_text(encoding="utf-8"),
            "history changed after ledger",
        )
        self.assertTrue(ledger.is_file())
        self.assertFalse((self.root / "clean_manifest.json").exists())

    def test_clean_rejects_tampered_ledger_contract_before_deletion(self) -> None:
        cases = (
            (
                "schema",
                lambda payload, _root: payload.update(schema_version=2),
                "schema_version",
            ),
            (
                "root",
                lambda payload, root: payload.update(root=str(root / "elsewhere")),
                "root does not match",
            ),
            (
                "namespace",
                lambda payload, _root: payload["files"][0].update(
                    path="user_notes.txt"
                ),
                "outside managed namespace",
            ),
            (
                "duplicate",
                lambda payload, _root: payload["files"].append(
                    dict(payload["files"][0])
                ),
                "duplicate ownership ledger path",
            ),
        )
        for name, tamper, expected_error in cases:
            with self.subTest(case=name):
                case_root = self.root / name
                managed = case_root / "gmsh" / "smoke.su2"
                managed.parent.mkdir(parents=True)
                managed.write_text(f"managed {name}", encoding="utf-8")
                ledger = write_ownership_ledger(
                    case_root,
                    (managed,),
                    trusted_root=self.root,
                )
                payload = json.loads(ledger.read_text(encoding="utf-8"))
                tamper(payload, case_root)
                _write_json(ledger, payload)

                with self.assertRaisesRegex(ValueError, expected_error):
                    clean_connection_artifacts(
                        case_root,
                        allowed_root=case_root,
                        trusted_root=self.root,
                    )

                self.assertEqual(
                    managed.read_text(encoding="utf-8"),
                    f"managed {name}",
                )

    def test_clean_rejects_reparse_in_trusted_ancestor_chain(self) -> None:
        trusted = self.root / "repository"
        unsafe_ancestor = trusted / "runs"
        output = unsafe_ancestor / "connection"
        output.mkdir(parents=True)
        sentinel = output / "keep-me.txt"
        sentinel.write_text("preserve", encoding="utf-8")

        with mock.patch(
            "cfdpipe.pipeline._is_reparse",
            side_effect=lambda path: Path(path) == unsafe_ancestor,
        ):
            with self.assertRaisesRegex(ValueError, "unsafe directory"):
                clean_connection_artifacts(
                    output,
                    allowed_root=output,
                    trusted_root=trusted,
                )

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")

    def test_clean_rejects_managed_symlink_when_platform_allows_it(self) -> None:
        target = self.root / "preserved-target.txt"
        link = self.root / "gmsh" / "smoke.su2"
        target.write_text("do not follow", encoding="utf-8")
        link.parent.mkdir(parents=True, exist_ok=True)
        simulated_reparse = False
        try:
            os.symlink(target, link)
        except (NotImplementedError, OSError):
            # Windows commonly requires an unavailable privilege for symlink
            # creation.  Exercise the same lstat/reparse refusal without
            # weakening the suite to a platform skip.
            link.write_text("simulated reparse", encoding="utf-8")
            simulated_reparse = True

        if simulated_reparse:
            with mock.patch(
                "cfdpipe.pipeline._is_reparse",
                side_effect=lambda path: Path(path) == link,
            ):
                with self.assertRaisesRegex(ValueError, "link|reparse"):
                    clean_connection_artifacts(
                        self.root,
                        allowed_root=self.root,
                        trusted_root=self.root,
                    )
        else:
            with self.assertRaisesRegex(ValueError, "link|reparse"):
                clean_connection_artifacts(
                    self.root,
                    allowed_root=self.root,
                    trusted_root=self.root,
                )

        self.assertEqual(target.read_text(encoding="utf-8"), "do not follow")
        self.assertTrue(link.is_symlink() or simulated_reparse)

    def test_tool_check_allows_both_optional_tools_to_be_missing(self) -> None:
        module_path = self.root / "gmsh.py"
        module_path.write_text("# fake gmsh module", encoding="utf-8")
        executable_paths: dict[str, Path] = {}
        for name in ("gmsh", "su2_cfd", "pvbatch"):
            path = self.root / f"{name}.exe"
            path.write_bytes(name.encode("ascii"))
            executable_paths[name] = path

        toolchain = mock.Mock()
        toolchain.resolve.side_effect = lambda name: ResolvedTool(
            name=name,
            path=executable_paths[name],
            source="test",
        )
        toolchain.resolve_optional.return_value = None
        gmsh_module = SimpleNamespace(__file__=str(module_path), __version__="4.15.2")

        with mock.patch(
            "cfdpipe.pipeline.importlib.import_module",
            return_value=gmsh_module,
        ):
            manifest, resolved = check_connection_tools(
                toolchain,
                self.root / "tools",
                controller_argv=("-m", "cfdpipe", "connect-test"),
            )

        self.assertEqual(manifest["status"], "PASS")
        self.assertEqual(manifest["warnings"], [])
        for name in ("su2_sol", "mpiexec"):
            self.assertIsNone(resolved[name])
            self.assertEqual(
                manifest["tools"][name]["status"],
                "MISSING_OPTIONAL",
            )
            self.assertFalse(manifest["tools"][name]["required"])
        self.assertEqual(
            [call.args[0] for call in toolchain.resolve.call_args_list],
            ["gmsh", "su2_cfd", "pvbatch"],
        )
        self.assertEqual(
            [call.args[0] for call in toolchain.resolve_optional.call_args_list],
            ["su2_sol", "mpiexec"],
        )
        persisted = json.loads(
            (self.root / "tools" / "toolchain_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(persisted["status"], "PASS")

    def test_runner_stops_after_gmsh_failure_and_writes_fail_report(self) -> None:
        scripts = self.root / "scripts"
        scripts.mkdir()
        toolchain = mock.Mock()
        toolchain.allow_mnt_c_executables = False
        tools_manifest = {
            "status": "PASS",
            "started_at": "2026-08-01T00:00:00+00:00",
            "ended_at": "2026-08-01T00:00:01+00:00",
            "controller": {
                "python_executable": sys.executable,
                "python_version": "test",
                "argv": [],
                "sha256": "0" * 64,
            },
            "gmsh_python": {
                "status": "PASS",
                "version": "4.15.2",
                "module_path": str(self.root / "gmsh.py"),
                "module_sha256": "1" * 64,
            },
            "warnings": [],
            "error": None,
        }
        gmsh_instance = mock.Mock()
        gmsh_instance.smoke.side_effect = RuntimeError("gmsh stage exploded")

        with (
            mock.patch(
                "cfdpipe.pipeline.check_connection_tools",
                return_value=(tools_manifest, {}),
            ) as tool_check,
            mock.patch(
                "cfdpipe.pipeline.GmshBridge",
                return_value=gmsh_instance,
            ) as gmsh_bridge,
            mock.patch("cfdpipe.pipeline.SU2Bridge") as su2_bridge,
            mock.patch("cfdpipe.pipeline.ParaViewBridge") as paraview_bridge,
        ):
            runner = ConnectionTestRunner(
                output_directory=self.root,
                allowed_output_root=self.root,
                trusted_repository_root=self.root,
                toolchain=toolchain,
                paraview_script_directory=scripts,
                timeout_seconds=7,
                live_output=False,
            )
            with self.assertRaises(ConnectionTestError) as raised:
                runner.run(clean=False)

        tool_check.assert_called_once()
        gmsh_bridge.assert_called_once()
        gmsh_instance.smoke.assert_called_once_with(
            output_directory=self.root / "gmsh",
            timeout=7.0,
        )
        su2_bridge.assert_not_called()
        paraview_bridge.assert_not_called()
        self.assertIsNotNone(raised.exception.report_path)
        report_path = raised.exception.report_path
        self.assertIsNotNone(report_path)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["overall"], "FAIL")
        self.assertEqual(
            report["metadata"]["stage_attempted"],
            {
                "tools": True,
                "gmsh": True,
                "su2": False,
                "paraview": False,
            },
        )
        self.assertEqual(report["python_to_gmsh"]["status"], "FAIL")
        self.assertEqual(
            report["python_to_su2"]["evidence"][0]["execution_state"],
            "NOT_RUN_DUE_TO_UPSTREAM_FAILURE",
        )

    def test_complete_fixture_passes_with_nonzero_noncritical_pvbatch_help(self) -> None:
        start = "2026-08-01T00:00:00+00:00"
        end = "2026-08-01T00:00:10+00:00"
        tools_dir = self.root / "tools"
        gmsh_dir = self.root / "gmsh"
        su2_dir = self.root / "su2"
        paraview_dir = self.root / "paraview"
        scripts_dir = self.root / "scripts"
        for directory in (tools_dir, gmsh_dir, su2_dir, paraview_dir, scripts_dir):
            directory.mkdir(parents=True, exist_ok=True)

        gmsh_module = tools_dir / "gmsh.py"
        gmsh_cli = tools_dir / "gmsh.exe"
        su2_cfd = tools_dir / "SU2_CFD.exe"
        pvbatch = tools_dir / "pvbatch.exe"
        for path, content in (
            (gmsh_module, b"gmsh module"),
            (gmsh_cli, b"gmsh executable"),
            (su2_cfd, b"su2 executable"),
            (pvbatch, b"pvbatch executable"),
        ):
            path.write_bytes(content)

        tools_manifest = {
            "status": "PASS",
            "started_at": "2026-08-01T00:00:01+00:00",
            "ended_at": "2026-08-01T00:00:02+00:00",
            "controller": {
                "python_executable": str(Path(sys.executable).resolve()),
                "python_version": "3.test",
                "argv": ["-m", "cfdpipe", "connect-test"],
                "sha256": _sha256(Path(sys.executable).resolve()),
            },
            "gmsh_python": {
                "status": "PASS",
                "version": "4.15.2",
                "module_path": str(gmsh_module.resolve()),
                "module_sha256": _sha256(gmsh_module),
            },
            "warnings": [],
            "error": None,
        }
        _write_json(tools_dir / "toolchain_manifest.json", tools_manifest)

        smoke_msh = gmsh_dir / "smoke.msh"
        smoke_su2 = gmsh_dir / "smoke.su2"
        smoke_msh.write_text("MSH fixture", encoding="utf-8")
        smoke_su2.write_text("SU2 fixture", encoding="utf-8")
        (gmsh_dir / "gmsh.log").write_text(
            "\n".join(
                (
                    "gmsh_version: 4.15.2",
                    f"python_module_path: {gmsh_module.resolve()}",
                    "Info : Writing 'smoke.su2'...",
                    "Info : Writing 1 elements",
                    "Info : Done writing 'smoke.su2'",
                    "status: PASS",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        gmsh_command = _command_record(gmsh_cli, ["--version"], gmsh_dir)
        gmsh_manifest = {
            "status": "PASS",
            "started_at": "2026-08-01T00:00:02+00:00",
            "ended_at": "2026-08-01T00:00:03+00:00",
            "gmsh_version": "4.15.2",
            "python_module_path": str(gmsh_module.resolve()),
            "gmsh_cli_path": str(gmsh_cli.resolve()),
            "gmsh_cli_version": "4.15.2",
            "gmsh_cli_command": gmsh_command,
            "node_count": 3,
            "element_count": 1,
            "markers": [
                {"name": "farfield", "dimension": 1},
                {"name": "fluid", "dimension": 2},
            ],
            "output_sha256": {
                "smoke.msh": _sha256(smoke_msh),
                "smoke.su2": _sha256(smoke_su2),
            },
            "su2_validation": {
                "ndime": 2,
                "nelem": 1,
                "npoin": 3,
                "nmark": 1,
                "markers": {"farfield": 3},
                "quality": {"status": "PASS", "finite": True},
            },
            "error": None,
        }
        _write_json(gmsh_dir / "gmsh_manifest.json", gmsh_manifest)

        local_mesh = su2_dir / "smoke.su2"
        local_mesh.write_bytes(smoke_su2.read_bytes())
        config = su2_dir / "smoke.cfg"
        history = su2_dir / "history.csv"
        visualization = su2_dir / "connection_solution.vtu"
        solver_stdout = su2_dir / "solver.stdout.log"
        solver_stderr = su2_dir / "solver.stderr.log"
        solver_command_path = su2_dir / "solver.command.json"
        config.write_text("SOLVER= EULER\n", encoding="utf-8")
        history.write_text(
            '"Inner_Iter","rms[Rho]"\n0,-1\n1,-2\n2,-3\n3,-4\n4,-5\n',
            encoding="utf-8",
        )
        visualization.write_bytes(b"valid finite VTU fixture")
        solver_stdout.write_text(
            "\n".join(
                (
                    "Input mesh file name: smoke.su2",
                    "3 grid points.",
                    "1 volume elements.",
                    "3 boundary elements in index 0 (Marker = farfield).",
                    "Maximum number of iterations reached (ITER = 5) before convergence.",
                    "Writing PARAVIEW output.",
                    "Exit Success (SU2_CFD)",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        solver_stderr.write_bytes(b"")
        solver_command = _command_record(su2_cfd, ["smoke.cfg"], su2_dir)
        _write_json(solver_command_path, solver_command)
        version_command = _command_record(su2_cfd, ["--help"], su2_dir)
        mesh_sha = _sha256(local_mesh)
        visualization_sha = _sha256(visualization)
        su2_manifest = {
            "status": "PASS",
            "su2_cfd_path": str(su2_cfd.resolve()),
            "su2_sol_path": None,
            "mpiexec_path": None,
            "su2_version": "8.5.0",
            "su2_version_evidence": version_command,
            "command": [str(su2_cfd.resolve()), "smoke.cfg"],
            "arguments": ["smoke.cfg"],
            "cwd": str(su2_dir.resolve()),
            "config_path": str(config.resolve()),
            "config_sha256": _sha256(config),
            "mesh_path": str(local_mesh.resolve()),
            "mesh_sha256": mesh_sha,
            "source_mesh_path": str(smoke_su2.resolve()),
            "source_mesh_sha256": _sha256(smoke_su2),
            "mesh_validation": {"npoin": 3, "nelem": 1},
            "return_code": 0,
            "requested_iterations": 5,
            "iterations": 5,
            "history_path": str(history.resolve()),
            "visualization_file": str(visualization.resolve()),
            "visualization_validation": {
                "path": str(visualization.resolve()),
                "sha256": visualization_sha,
                "finite": True,
                "point_count": 3,
                "cell_count": 1,
            },
            "solver_stdout_log": str(solver_stdout.resolve()),
            "solver_stderr_log": str(solver_stderr.resolve()),
            "solver_command_log": str(solver_command_path.resolve()),
            "su2_sol_command": None,
            "su2_sol_return_code": None,
            "fatal_matches": [],
            "nonfatal_diagnostics": [],
            "diagnostic_classifications": [],
            "timed_out": False,
            "start_time": "2026-08-01T00:00:03+00:00",
            "end_time": "2026-08-01T00:00:05+00:00",
            "error": None,
        }
        _write_json(su2_dir / "run_manifest.json", su2_manifest)

        probe_script = scripts_dir / "probe_pvbatch.py"
        inspect_script = scripts_dir / "inspect_solution.py"
        postprocess_script = scripts_dir / "smoke_postprocess.py"
        for script in (probe_script, inspect_script, postprocess_script):
            script.write_text("# fixture", encoding="utf-8")
        probe_json = paraview_dir / "pvbatch_probe.json"
        inventory_json = paraview_dir / "solution_inventory.json"
        postprocess_json = paraview_dir / "smoke_postprocess.json"
        integral_csv = paraview_dir / "smoke_integral.csv"
        _write_json(
            probe_json,
            {
                "status": "PASS",
                "paraview_version": "6.2",
                "python_executable": str(pvbatch.resolve()),
            },
        )
        _write_json(
            inventory_json,
            {
                "status": "PASS",
                "paraview_version": "6.2",
                "input_file": str(visualization.resolve()),
                "dataset_type": "Unstructured Grid",
                "number_of_points": 3,
                "number_of_cells": 1,
                "point_data_arrays": [],
                "cell_data_arrays": [],
                "field_data_arrays": [],
            },
        )
        _write_json(
            postprocess_json,
            {
                "status": "PASS",
                "paraview_version": "6.2",
                "input_file": str(visualization.resolve()),
                "total_points": 3,
                "total_cells": 1,
                "bounds": [0, 1, 0, 1, 0, 0],
                "integrable_arrays": [],
            },
        )
        integral_csv.write_text('"Area"\n1.0\n', encoding="utf-8")

        help_command = _command_record(
            pvbatch,
            ["--help"],
            paraview_dir,
            returncode=123,
            timed_out=True,
        )
        probe_command = _command_record(
            pvbatch,
            [str(probe_script.resolve()), "--output", str(probe_json.resolve())],
            paraview_dir,
        )
        inspect_command = _command_record(
            pvbatch,
            [
                str(inspect_script.resolve()),
                "--input",
                str(visualization.resolve()),
                "--output",
                str(inventory_json.resolve()),
            ],
            paraview_dir,
        )
        postprocess_command = _command_record(
            pvbatch,
            [
                str(postprocess_script.resolve()),
                "--input",
                str(visualization.resolve()),
                "--output-directory",
                str(paraview_dir.resolve()),
            ],
            paraview_dir,
        )
        paraview_manifest = {
            "status": "PASS",
            "started_at": "2026-08-01T00:00:05+00:00",
            "ended_at": "2026-08-01T00:00:09+00:00",
            "pvbatch_path": str(pvbatch.resolve()),
            "pvbatch_sha256": _sha256(pvbatch),
            "paraview_version": "6.2",
            "input_solution_path": str(visualization.resolve()),
            "input_solution_sha256": visualization_sha,
            "commands": [
                help_command,
                probe_command,
                inspect_command,
                postprocess_command,
            ],
            "return_code": 0,
            "detected_arrays": {"point": [], "cell": [], "field": []},
            "output_files": [
                _file_record(probe_json),
                _file_record(inventory_json),
                _file_record(postprocess_json),
                _file_record(integral_csv),
            ],
            "warnings": [
                "pvbatch help capability probe timed out; no offscreen option was used"
            ],
            "error": None,
        }
        _write_json(paraview_dir / "paraview_manifest.json", paraview_manifest)

        state = {
            "attempted": {
                "tools": True,
                "gmsh": True,
                "su2": True,
                "paraview": True,
            },
            "manifests": {
                "tools": tools_manifest,
                "gmsh": gmsh_manifest,
                "su2": su2_manifest,
                "paraview": paraview_manifest,
            },
        }

        report = build_connection_report(
            self.root,
            state,
            connection_started_at=start,
            connection_ended_at=end,
        )

        self.assertEqual(report["overall"], "PASS")
        for key in CONNECTION_KEYS:
            with self.subTest(connection=key):
                self.assertEqual(report[key]["status"], "PASS")
                self.assertEqual(report[key]["evidence"][0]["failed_checks"], [])
        command_evidence = report["python_to_pvbatch"]["evidence"][0]["commands"]
        reported_help = next(
            command for command in command_evidence if command["args"] == ["--help"]
        )
        self.assertEqual(reported_help["returncode"], 123)
        self.assertTrue(reported_help["timed_out"])
        self.assertFalse(reported_help["critical"])
        self.assertIn(
            "pvbatch help capability probe timed out; no offscreen option was used",
            report["metadata"]["warnings"],
        )


if __name__ == "__main__":
    unittest.main()
