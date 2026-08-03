"""Tests for the offline repository reproduction preflight."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import stat
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VERIFIER_PATH = REPOSITORY_ROOT / "scripts/repro/verify_repository.py"
REPRODUCTION_OUTPUT = REPOSITORY_ROOT / "runs" / "reproducibility"
SPEC = importlib.util.spec_from_file_location(
    "cfdpipe_repository_verifier",
    VERIFIER_PATH,
)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import bootstrap guard
    raise RuntimeError(f"Unable to load verifier from {VERIFIER_PATH}")
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)


class RepositoryPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        REPRODUCTION_OUTPUT.mkdir(parents=True, exist_ok=True)
        self._temporary_directory = tempfile.TemporaryDirectory(
            dir=REPRODUCTION_OUTPUT
        )
        self.root = Path(self._temporary_directory.name)
        self.source_bytes = b"private STEP fixture\n"
        self.brep_bytes = b"private BREP fixture\n"
        self.source_sha = hashlib.sha256(self.source_bytes).hexdigest()
        self.brep_sha = hashlib.sha256(self.brep_bytes).hexdigest()
        self._write_portable_repository(include_private_inputs=True)

    def tearDown(self) -> None:
        for relative in (
            "geometry/raw/model.step",
            "geometry/derived/model.brep",
        ):
            path = self.root / relative
            if path.exists():
                path.chmod(path.stat().st_mode | stat.S_IWRITE)
        self._temporary_directory.cleanup()

    def _make_writable(self, relative: str) -> Path:
        path = self.root / relative
        path.chmod(path.stat().st_mode | stat.S_IWRITE)
        return path

    def _remove_private(self, relative: str) -> None:
        self._make_writable(relative).unlink()

    def _write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _write_bytes(self, relative: str, value: bytes) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        return path

    def _write_portable_repository(self, *, include_private_inputs: bool) -> None:
        self._write("AGENTS.md", "# Repository policy\n")
        self._write("PLAN.md", "# Reproduction plan\n")
        self._write("README.md", "# Fixture repository\n")
        self._write(
            "pyproject.toml",
            '[project]\nname = "fixture"\nversion = "0.0.0"\n',
        )
        self._write("requirements-gmsh.txt", "gmsh==4.15.2\n")
        self._write(".gitignore", "/geometry/raw/*\n/runs/*\n")
        self._write(".gitattributes", "*.py text eol=lf\n")
        self._write(
            ".github/workflows/ci.yml",
            "name: fixture\non: [push]\njobs: {}\n",
        )
        self._write(
            "config/project.toml",
            "\n".join(
                (
                    '[project]',
                    'name = "fixture"',
                    'geometry_file = "geometry/raw/model.step"',
                    '',
                    '[production]',
                    'design_case = "CASE_1"',
                    '',
                )
            ),
        )
        self._write(
            "config/cases.csv",
            (
                "case_id,mach,altitude_km,alpha_deg,beta_deg,role\n"
                "CASE_1,0.3,0,0,0,design\n"
            ),
        )
        self._write(
            "config/markers.toml",
            "\n".join(
                (
                    'schema = "cfdpipe.markers.v2"',
                    'status = "PASS"',
                    '',
                    '[source]',
                    'path = "geometry/raw/model.step"',
                    f'sha256 = "{self.source_sha}"',
                    'read_only = true',
                    '',
                    '[pipeline_geometry]',
                    'path = "geometry/derived/model.brep"',
                    f'sha256 = "{self.brep_sha}"',
                    'read_only = true',
                    '',
                )
            ),
        )
        self._write(
            "config/tools.example.json",
            json.dumps(
                {
                    "allow_mnt_c_executables": False,
                    "tools": {
                        "gmsh": None,
                        "su2_cfd": None,
                        "su2_sol": None,
                        "mpiexec": None,
                        "pvbatch": None,
                    },
                }
            ),
        )
        self._write(
            "config/reproducibility.json",
            json.dumps(
                {
                    "schema": "cfdpipe.reproducibility.v1",
                    "baseline_status": "VERIFIED_ON_REFERENCE_WORKSTATION",
                    "platform": {
                        "operating_system": "Windows",
                        "architecture": "x86_64",
                    },
                    "verified_toolchain": VERIFIER.EXPECTED_TOOLCHAIN,
                    "private_inputs": {
                        "version_control_policy": "EXCLUDED_FROM_GIT",
                        "integrity_contract": "config/markers.toml",
                        "missing_default_preflight_status": "WARN",
                        "strict_preflight_option": "--require-private-inputs",
                    },
                    "validation_tiers": {
                        "offline_repository_preflight": (
                            "python scripts/repro/verify_repository.py"
                        ),
                        "offline_repository_preflight_strict": (
                            "python scripts/repro/verify_repository.py "
                            "--require-private-inputs"
                        ),
                        "cad_free_standard_library_tests": (
                            "python -B scripts/repro/run_cad_free_tests.py"
                        ),
                        "full_standard_library_tests": (
                            "python -m unittest discover -s tests -v"
                        ),
                        "full_tests_require_private_inputs": True,
                    },
                    "scope": {
                        "external_programs_started_by_offline_preflight": False,
                        "network_accessed_by_offline_preflight": False,
                        "production_mesh_or_cfd_included": False,
                    },
                }
            ),
        )
        self._write("src/cfdpipe/__init__.py", '"""Fixture package."""\n')
        self._write(
            "src/cfdpipe/__main__.py",
            "from .cli import main\n"
            "if __name__ == '__main__':\n"
            "    raise SystemExit(main())\n",
        )
        self._write(
            "src/cfdpipe/cli.py",
            "def main(argv=None):\n"
            "    return 0\n",
        )
        self._write(
            "src/cfdpipe/process.py",
            "import subprocess\n"
            "def launch(command):\n"
            "    return subprocess.Popen(command, shell=False)\n",
        )
        self._write(
            "scripts/repro/run_cad_free_tests.py",
            '"""Fixture CAD-free test launcher."""\n',
        )
        self._write(
            "scripts/repro/verify_repository.py",
            '"""Fixture verifier placeholder."""\n',
        )
        self._write(
            "scripts/paraview/inspect_solution.py",
            "from paraview.simple import OpenDataFile\n",
        )
        if include_private_inputs:
            source = self._write_bytes("geometry/raw/model.step", self.source_bytes)
            brep = self._write_bytes("geometry/derived/model.brep", self.brep_bytes)
            write_bits = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
            source.chmod(source.stat().st_mode & ~write_bits)
            brep.chmod(brep.stat().st_mode & ~write_bits)

    def test_complete_fixture_passes(self) -> None:
        report = VERIFIER.verify_repository(self.root)

        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["summary"]["fail"], 0)
        self.assertEqual(report["summary"]["warn"], 0)
        self.assertEqual(
            VERIFIER.render_report(report),
            VERIFIER.render_report(VERIFIER.verify_repository(self.root)),
        )

    def test_required_path_must_be_a_regular_root_contained_file(self) -> None:
        readme = self.root / "README.md"
        readme.unlink()
        readme.mkdir()

        report = VERIFIER.verify_repository(self.root)

        required = next(
            check
            for check in report["checks"]
            if check["id"] == "required_file:README.md"
        )
        self.assertEqual(required["status"], "FAIL")
        self.assertIn("regular file", required["message"])

    def test_missing_private_inputs_warn_by_default(self) -> None:
        self._remove_private("geometry/raw/model.step")
        self._remove_private("geometry/derived/model.brep")

        report = VERIFIER.verify_repository(self.root)

        self.assertEqual(report["status"], "WARN")
        self.assertEqual(report["summary"]["warn"], 2)
        self.assertEqual(report["summary"]["fail"], 0)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            return_code = VERIFIER.main(["--root", str(self.root)])
        self.assertEqual(return_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "WARN")

    def test_missing_private_inputs_fail_in_strict_mode(self) -> None:
        self._remove_private("geometry/raw/model.step")
        self._remove_private("geometry/derived/model.brep")

        report = VERIFIER.verify_repository(
            self.root,
            require_private_inputs=True,
        )

        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["summary"]["fail"], 2)

    def test_private_input_hash_mismatch_fails(self) -> None:
        source = self._make_writable("geometry/raw/model.step")
        source.write_bytes(b"changed")
        write_bits = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
        source.chmod(source.stat().st_mode & ~write_bits)

        report = VERIFIER.verify_repository(self.root)

        self.assertEqual(report["status"], "FAIL")
        source_check = next(
            check
            for check in report["checks"]
            if check["id"] == "private_source_geometry"
        )
        self.assertEqual(source_check["status"], "FAIL")
        self.assertNotEqual(
            source_check["details"]["actual_sha256"],
            source_check["details"]["expected_sha256"],
        )

    def test_prohibited_shell_gui_and_ordinary_paraview_import_fail(self) -> None:
        forbidden_sources = {
            "shell": (
                "import subprocess\n"
                "subprocess.run(['tool'], shell=True)\n"
            ),
            "gui": "import gmsh\ngmsh.fltk.run()\n",
            "paraview": "from paraview import simple\n",
        }
        expected_kinds = {
            "shell": "subprocess_shell_keyword_true",
            "gui": "gmsh_gui_run",
            "paraview": "ordinary_python_imports_paraview",
        }

        for name, source in forbidden_sources.items():
            with self.subTest(name=name):
                path = self.root / "src/cfdpipe/forbidden.py"
                path.write_text(source, encoding="utf-8")
                report = VERIFIER.verify_repository(self.root)
                policy = next(
                    check
                    for check in report["checks"]
                    if check["id"] == "python_source_policy"
                )
                self.assertEqual(report["status"], "FAIL")
                self.assertIn(
                    expected_kinds[name],
                    {item["kind"] for item in policy["details"]["violations"]},
                )
                path.unlink()

    def test_paraview_script_directory_is_the_only_import_exception(self) -> None:
        report = VERIFIER.verify_repository(self.root)

        policy = next(
            check
            for check in report["checks"]
            if check["id"] == "python_source_policy"
        )
        self.assertEqual(policy["status"], "PASS")

    def test_paraview_script_cannot_create_an_external_process(self) -> None:
        self._write(
            "scripts/paraview/inspect_solution.py",
            "import subprocess\nsubprocess.run(['program'])\n",
        )

        report = VERIFIER.verify_repository(self.root)

        policy = next(
            check
            for check in report["checks"]
            if check["id"] == "python_source_policy"
        )
        kinds = {item["kind"] for item in policy["details"]["violations"]}
        self.assertEqual(policy["status"], "FAIL")
        self.assertIn("external_process_creation_outside_command_runner", kinds)

    def test_cases_contract_requires_columns_and_unique_case_ids(self) -> None:
        self._write(
            "config/cases.csv",
            (
                "case_id,mach,altitude_km,alpha_deg,beta_deg,role\n"
                "CASE_1,0.3,0,0,0,design\n"
                "CASE_1,0.4,1,1,0,duplicate\n"
            ),
        )

        report = VERIFIER.verify_repository(self.root)

        csv_check = next(
            check
            for check in report["checks"]
            if check["id"] == "csv_parse:config/cases.csv"
        )
        self.assertEqual(csv_check["status"], "FAIL")
        self.assertIn("unique", csv_check["message"])

    def test_project_and_marker_semantic_bindings_fail_closed(self) -> None:
        self._write(
            "config/project.toml",
            "\n".join(
                (
                    '[project]',
                    'name = "fixture"',
                    'geometry_file = "geometry/raw/different.step"',
                    '',
                    '[production]',
                    'design_case = "UNKNOWN"',
                    '',
                )
            ),
        )
        markers = (self.root / "config/markers.toml").read_text(encoding="utf-8")
        self._write(
            "config/markers.toml",
            markers.replace(
                'schema = "cfdpipe.markers.v2"',
                'schema = "cfdpipe.markers.v1"',
            ),
        )

        report = VERIFIER.verify_repository(self.root)
        statuses = {check["id"]: check["status"] for check in report["checks"]}

        self.assertEqual(statuses["markers_contract"], "FAIL")
        self.assertEqual(statuses["project_marker_geometry_binding"], "FAIL")
        self.assertEqual(statuses["project_design_case"], "FAIL")

    def test_reproducibility_contract_rejects_version_or_override_drift(self) -> None:
        path = self.root / "config/reproducibility.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["verified_toolchain"]["su2_cfd"]["environment_override"] = (
            "WRONG_VARIABLE"
        )
        path.write_text(json.dumps(payload), encoding="utf-8")

        report = VERIFIER.verify_repository(self.root)

        contract = next(
            check
            for check in report["checks"]
            if check["id"] == "reproducibility_contract"
        )
        self.assertEqual(contract["status"], "FAIL")

    def test_present_private_inputs_must_be_read_only(self) -> None:
        self._make_writable("geometry/raw/model.step")

        report = VERIFIER.verify_repository(self.root)

        source_check = next(
            check
            for check in report["checks"]
            if check["id"] == "private_source_geometry"
        )
        self.assertEqual(source_check["status"], "FAIL")
        self.assertFalse(source_check["details"]["actual_read_only"])

    def test_cli_returns_nonzero_for_strict_failure_and_json_for_stdout(self) -> None:
        self._remove_private("geometry/raw/model.step")
        self._remove_private("geometry/derived/model.brep")
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            return_code = VERIFIER.main(
                [
                    "--root",
                    str(self.root),
                    "--require-private-inputs",
                ]
            )

        self.assertEqual(return_code, 1)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "FAIL")

    def test_cli_writes_report_only_below_runs(self) -> None:
        output = self.root / "runs/reproduction/preflight.json"

        return_code = VERIFIER.main(
            ["--root", str(self.root), "--output", str(output)]
        )

        self.assertEqual(return_code, 0)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["status"], "PASS")

        original_payload = output.read_bytes()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            existing_code = VERIFIER.main(
                ["--root", str(self.root), "--output", str(output)]
            )
        self.assertEqual(existing_code, 1)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "FAIL")
        self.assertEqual(output.read_bytes(), original_payload)

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rejected_code = VERIFIER.main(
                [
                    "--root",
                    str(self.root),
                    "--output",
                    str(self.root / "outside.json"),
                ]
            )
        self.assertEqual(rejected_code, 1)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "FAIL")
        self.assertFalse((self.root / "outside.json").exists())

    def test_cli_rejects_symlinked_output_ancestor_when_supported(self) -> None:
        runs = self.root / "runs"
        runs.mkdir(exist_ok=True)
        target = self.root / "actual-output"
        target.mkdir()
        link = runs / "linked"
        try:
            link.symlink_to(target, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"directory symlinks are unavailable: {exc}")

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            return_code = VERIFIER.main(
                [
                    "--root",
                    str(self.root),
                    "--output",
                    str(link / "report.json"),
                ]
            )

        self.assertEqual(return_code, 1)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "FAIL")
        self.assertFalse((target / "report.json").exists())


if __name__ == "__main__":
    unittest.main()
