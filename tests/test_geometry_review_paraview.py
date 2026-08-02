"""Tests for the ordinary-Python to pvbatch geometry-review bridge."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from cfdpipe.bridges.geometry_review_paraview import (
    GeometryReviewParaViewBridge,
    GeometryReviewParaViewError,
)
from cfdpipe.process import CommandResult


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
REAL_CONNECTION_ROOT = REPOSITORY_ROOT / "runs" / "real_connection"
SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "paraview" / "render_geometry_review.py"
VIEWS = ("isometric", "XY", "XZ", "YZ")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class GeometryReviewParaViewTests(unittest.TestCase):
    def setUp(self) -> None:
        REAL_CONNECTION_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="geometry_review_pv_",
            dir=REAL_CONNECTION_ROOT,
        )
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.input_path = self.root / "geometry" / "inspection.vtk"
        self.groups_path = self.root / "geometry" / "review_groups.json"
        self.output = self.root / "review"
        self.input_path.parent.mkdir(parents=True)
        self.input_path.write_bytes(b"# vtk DataFile Version 3.0\nmock geometry\n")
        self.groups_payload = {
            "groups": {
                "nose": {"surface_tags": [11, 12]},
                "walls": {"surface_tags": [21]},
            }
        }
        self.groups_path.write_text(
            json.dumps(self.groups_payload), encoding="utf-8"
        )
        self.pvbatch = self.root / "pvbatch.exe"
        self.pvbatch.write_bytes(b"explicit test pvbatch")
        self.runner = mock.Mock()

    def _bridge(self) -> GeometryReviewParaViewBridge:
        return GeometryReviewParaViewBridge(
            self.runner,
            self.pvbatch,
            SCRIPT_PATH,
        )

    def _result(
        self,
        *,
        returncode: int | None = 0,
        stdout: str = "",
        stderr: str = "",
        timed_out: bool = False,
    ) -> CommandResult:
        self.output.mkdir(parents=True, exist_ok=True)
        stdout_log = self.output / "pvbatch.stdout.log"
        stderr_log = self.output / "pvbatch.stderr.log"
        command_log = self.output / "pvbatch.command.json"
        stdout_log.write_text(stdout, encoding="utf-8")
        stderr_log.write_text(stderr, encoding="utf-8")
        command_log.write_text("{}\n", encoding="utf-8")
        return CommandResult(
            executable=str(self.pvbatch.resolve()),
            args=(),
            cwd=str(self.output),
            start_time="2026-08-01T00:00:00Z",
            end_time="2026-08-01T00:00:01Z",
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            metadata_log=command_log,
            dry_run=False,
            timed_out=timed_out,
        )

    def _write_success_outputs(self) -> dict[str, object]:
        self.output.mkdir(parents=True, exist_ok=True)
        images: list[dict[str, object]] = []
        for group in ("all", "nose", "walls"):
            for view in VIEWS:
                image_path = self.output / f"{group}__{view}.png"
                image_path.write_bytes(f"mock {group} {view} image".encode("utf-8"))
                images.append(
                    {
                        "group": group,
                        "view": view,
                        "path": str(image_path.resolve()),
                        "size_bytes": image_path.stat().st_size,
                        "sha256": _sha256(image_path),
                    }
                )
        manifest: dict[str, object] = {
            "status": "PASS",
            "paraview_version": "6.2",
            "input_file": str(self.input_path.resolve()),
            "input_sha256": _sha256(self.input_path),
            "groups_file": str(self.groups_path.resolve()),
            "groups_sha256": _sha256(self.groups_path),
            "dataset_type": "Unstructured Grid",
            "number_of_points": 24,
            "number_of_cells": 12,
            "bounds": [0.0, 1.0, 0.0, 1.0, 0.0, 1.0],
            "available_cell_arrays": ["gmsh:physical", "gmsh:geometrical"],
            "tag_array": "gmsh:geometrical",
            "groups": [
                {"name": "all", "surface_tags": [11, 12, 21]},
                {"name": "nose", "surface_tags": [11, 12]},
                {"name": "walls", "surface_tags": [21]},
            ],
            "images": images,
            "error": None,
        }
        (self.output / "geometry_render_manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        return manifest

    def test_build_command_is_an_explicit_pvbatch_argument_list(self) -> None:
        command = self._bridge().build_command(
            self.input_path,
            self.groups_path,
            self.output,
        )

        self.assertEqual(
            command,
            [
                str(self.pvbatch.resolve()),
                str(SCRIPT_PATH.resolve()),
                "--input",
                str(self.input_path),
                "--groups",
                str(self.groups_path),
                "--output-directory",
                str(self.output),
            ],
        )

    def test_success_uses_fixed_logs_without_shell_and_validates_every_image(self) -> None:
        expected_manifest: dict[str, object] = {}

        def run_side_effect(executable: str, **kwargs: object) -> CommandResult:
            self.assertEqual(executable, str(self.pvbatch.resolve()))
            self.assertNotIn("shell", kwargs)
            expected_manifest.update(self._write_success_outputs())
            return self._result(stdout="headless geometry render complete\n")

        self.runner.run.side_effect = run_side_effect

        manifest = self._bridge().run(
            self.input_path,
            self.groups_path,
            self.output,
            allowed_output_root=self.root,
            timeout_seconds=19,
            live_output=False,
        )

        self.assertEqual(manifest, expected_manifest)
        self.assertEqual(manifest["status"], "PASS")
        self.assertEqual(manifest["tag_array"], "gmsh:geometrical")
        self.assertEqual(len(manifest["images"]), 12)
        self.runner.run.assert_called_once()
        call = self.runner.run.call_args
        self.assertEqual(call.args, (str(self.pvbatch.resolve()),))
        self.assertEqual(
            call.kwargs["args"],
            [
                str(SCRIPT_PATH.resolve()),
                "--input",
                str(self.input_path.resolve()),
                "--groups",
                str(self.groups_path.resolve()),
                "--output-directory",
                str(self.output.resolve()),
            ],
        )
        self.assertEqual(call.kwargs["cwd"], self.output.resolve())
        self.assertEqual(call.kwargs["timeout"], 19.0)
        self.assertFalse(call.kwargs["check"])
        self.assertFalse(call.kwargs["live_output"])
        self.assertFalse(call.kwargs["dry_run"])
        self.assertEqual(call.kwargs["stdout_log"], self.output / "pvbatch.stdout.log")
        self.assertEqual(call.kwargs["stderr_log"], self.output / "pvbatch.stderr.log")
        self.assertEqual(call.kwargs["metadata_log"], self.output / "pvbatch.command.json")
        self.assertNotIn("shell", call.kwargs)

    def test_nonzero_return_preserves_original_stderr(self) -> None:
        failed = self._result(
            returncode=7,
            stderr="Threshold property unavailable on this ParaView build\n",
        )
        self.runner.run.return_value = failed

        with self.assertRaises(GeometryReviewParaViewError) as raised:
            self._bridge().run(
                self.input_path,
                self.groups_path,
                self.output,
                allowed_output_root=self.root,
            )

        self.assertIs(raised.exception.result, failed)
        self.assertEqual(raised.exception.returncode, 7)
        self.assertEqual(raised.exception.stderr, failed.stderr)
        self.assertIn("Threshold property unavailable", str(raised.exception))

    def test_zero_return_without_new_manifest_fails(self) -> None:
        self.runner.run.return_value = self._result()

        with self.assertRaisesRegex(
            GeometryReviewParaViewError,
            "did not create geometry_render_manifest",
        ):
            self._bridge().run(
                self.input_path,
                self.groups_path,
                self.output,
                allowed_output_root=self.root,
            )

    def test_invalid_manifest_json_fails(self) -> None:
        failed_result: CommandResult | None = None

        def run_side_effect(*args: object, **kwargs: object) -> CommandResult:
            nonlocal failed_result
            del args, kwargs
            self.output.mkdir(parents=True, exist_ok=True)
            (self.output / "geometry_render_manifest.json").write_text(
                "{not-json", encoding="utf-8"
            )
            failed_result = self._result(
                stderr="pvbatch wrote an invalid geometry manifest\n"
            )
            return failed_result

        self.runner.run.side_effect = run_side_effect

        with self.assertRaisesRegex(
            GeometryReviewParaViewError,
            "not valid UTF-8 JSON",
        ) as raised:
            self._bridge().run(
                self.input_path,
                self.groups_path,
                self.output,
                allowed_output_root=self.root,
            )
        self.assertIs(raised.exception.result, failed_result)
        self.assertEqual(
            raised.exception.stderr,
            "pvbatch wrote an invalid geometry manifest\n",
        )

    def test_manifest_missing_one_declared_image_fails(self) -> None:
        def run_side_effect(*args: object, **kwargs: object) -> CommandResult:
            del args, kwargs
            manifest = self._write_success_outputs()
            missing = Path(manifest["images"][0]["path"])
            missing.unlink()
            return self._result()

        self.runner.run.side_effect = run_side_effect

        with self.assertRaisesRegex(
            GeometryReviewParaViewError,
            "image is missing",
        ):
            self._bridge().run(
                self.input_path,
                self.groups_path,
                self.output,
                allowed_output_root=self.root,
            )

    def test_stale_png_not_declared_by_current_manifest_fails(self) -> None:
        def run_side_effect(*args: object, **kwargs: object) -> CommandResult:
            del args, kwargs
            self._write_success_outputs()
            (self.output / "stale_previous_group__XY.png").write_bytes(
                b"stale generated evidence"
            )
            return self._result()

        self.runner.run.side_effect = run_side_effect

        with self.assertRaisesRegex(
            GeometryReviewParaViewError,
            "PNG inventory does not exactly match",
        ):
            self._bridge().run(
                self.input_path,
                self.groups_path,
                self.output,
                allowed_output_root=self.root,
            )

    def test_manifest_status_counts_tag_array_and_groups_are_validated(self) -> None:
        mutations = (
            ("status", lambda payload: payload.update(status="FAIL"), "status"),
            (
                "points",
                lambda payload: payload.update(number_of_points=0),
                "number_of_points",
            ),
            ("tag", lambda payload: payload.update(tag_array=None), "tag_array"),
            (
                "input_hash",
                lambda payload: payload.update(input_sha256="0" * 64),
                "input_sha256",
            ),
            (
                "groups_hash",
                lambda payload: payload.update(groups_sha256="0" * 64),
                "groups_sha256",
            ),
            (
                "all_tags",
                lambda payload: payload["groups"][0].update(surface_tags=[999]),
                "all group tags",
            ),
            (
                "groups",
                lambda payload: payload.update(groups=payload["groups"][:-1]),
                "groups do not match",
            ),
        )
        for name, mutate, expected in mutations:
            with self.subTest(case=name):
                case_output = self.root / f"review-{name}"
                self.output = case_output

                def run_side_effect(*args: object, **kwargs: object) -> CommandResult:
                    del args, kwargs
                    payload = self._write_success_outputs()
                    mutate(payload)
                    (self.output / "geometry_render_manifest.json").write_text(
                        json.dumps(payload), encoding="utf-8"
                    )
                    return self._result()

                self.runner.reset_mock()
                self.runner.run.side_effect = run_side_effect
                with self.assertRaisesRegex(GeometryReviewParaViewError, expected):
                    self._bridge().run(
                        self.input_path,
                        self.groups_path,
                        case_output,
                        allowed_output_root=self.root,
                    )

    def test_input_groups_and_output_must_all_stay_under_allowed_root(self) -> None:
        outside_root = REAL_CONNECTION_ROOT / "outside_geometry_review_fixture"
        outside_root.mkdir(parents=True, exist_ok=True)
        self.addCleanup(
            lambda: outside_root.rmdir()
            if outside_root.exists() and not any(outside_root.iterdir())
            else None
        )
        outside_input = outside_root / "outside.vtk"
        outside_groups = outside_root / "outside.json"
        outside_input.write_bytes(b"outside vtk")
        outside_groups.write_text(json.dumps(self.groups_payload), encoding="utf-8")
        self.addCleanup(lambda: outside_input.unlink(missing_ok=True))
        self.addCleanup(lambda: outside_groups.unlink(missing_ok=True))

        cases = (
            (outside_input, self.groups_path, self.output, "VTK input"),
            (self.input_path, outside_groups, self.output, "groups JSON"),
            (self.input_path, self.groups_path, outside_root / "output", "output"),
        )
        for input_path, groups_path, output, expected in cases:
            with self.subTest(case=expected):
                self.runner.reset_mock()
                with self.assertRaisesRegex(
                    GeometryReviewParaViewError,
                    expected,
                ):
                    self._bridge().run(
                        input_path,
                        groups_path,
                        output,
                        allowed_output_root=self.root,
                    )
                self.runner.run.assert_not_called()

    def test_ordinary_bridge_has_no_paraview_import_and_script_has_required_calls(self) -> None:
        bridge_path = (
            REPOSITORY_ROOT
            / "src"
            / "cfdpipe"
            / "bridges"
            / "geometry_review_paraview.py"
        )
        bridge_tree = ast.parse(bridge_path.read_text(encoding="utf-8"))
        imports: list[str] = []
        for node in ast.walk(bridge_tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        self.assertFalse(
            any(name == "paraview" or name.startswith("paraview.") for name in imports)
        )

        script_tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        script_imports = [
            node.module or ""
            for node in ast.walk(script_tree)
            if isinstance(node, ast.ImportFrom)
        ]
        self.assertIn("paraview", script_imports)
        for required_call in (
            "OpenDataFile",
            "UpdatePipeline",
            "Threshold",
            "SaveScreenshot",
        ):
            self.assertIn(required_call, source)
        self.assertIn("group_bounds = _finite_bounds(proxy_information)", source)


if __name__ == "__main__":
    unittest.main()
