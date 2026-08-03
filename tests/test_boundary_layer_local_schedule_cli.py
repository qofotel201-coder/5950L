from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import cfdpipe.boundary_layer_local_schedule as local_module
import cfdpipe.cli as cli_module


class BoundaryLayerLocalScheduleCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / "config").mkdir()
        self.config = self.root / "config" / "coarse_mesh.toml"
        self.config.write_text('schema = "test"\n', encoding="utf-8")
        self.output_root = self.root / "runs" / "mesh" / "coarse"
        self.clearance_directory = self.output_root / "clearance_001"
        self.clearance_directory.mkdir(parents=True)
        self.clearance = self.clearance_directory / "boundary_layer_clearance.json"
        self.clearance.write_text('{"status":"FAIL"}\n', encoding="utf-8")
        self.clearance_sha256 = hashlib.sha256(
            self.clearance.read_bytes()
        ).hexdigest()
        self.output = self.output_root / "local_plan_001"
        self.contract = {"schema": "normalized"}
        self.plan_pass = {
            "schema": "cfdpipe.boundary_layer_local_schedule.v1",
            "status": "PASS",
            "planning_only": True,
            "extrusion_authorized": False,
            "mesh_generated": False,
            "su2_called": False,
            "paraview_called": False,
            "production_mesh_eligible": False,
        }
        self.root_patch = mock.patch.object(cli_module, "REPOSITORY_ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def arguments(
        self,
        *,
        output: Path | None = None,
        clearance: Path | None = None,
    ) -> list[str]:
        return [
            "pipeline",
            "boundary-layer-local-plan",
            "--config",
            str(self.config),
            "--clearance-report",
            str(self.clearance if clearance is None else clearance),
            "--clearance-sha256",
            self.clearance_sha256,
            "--output",
            str(self.output if output is None else output),
        ]

    def _run(
        self,
        *,
        plan_result: dict | None = None,
        plan_side_effect: BaseException | None = None,
        arguments: list[str] | None = None,
    ) -> tuple[int, str, str, mock.Mock, mock.Mock]:
        normalizer = mock.Mock(return_value=self.contract)
        planner = mock.Mock(
            return_value=self.plan_pass if plan_result is None else plan_result,
            side_effect=plan_side_effect,
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch(
                "cfdpipe.coarse_mesh.normalize_coarse_mesh_config", normalizer
            ),
            mock.patch.object(
                local_module, "plan_local_boundary_layer_schedules", planner
            ),
            mock.patch.object(cli_module, "CommandRunner") as runner,
            mock.patch.dict(sys.modules, {"gmsh": None}),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            return_code = cli_module.main(
                self.arguments() if arguments is None else arguments
            )
        runner.assert_not_called()
        return return_code, stdout.getvalue(), stderr.getvalue(), normalizer, planner

    def test_parser_registers_pure_python_command_without_tool_options(self) -> None:
        parsed = cli_module.build_parser().parse_args(self.arguments())

        self.assertEqual(
            parsed.handler.__name__, "_handle_pipeline_boundary_layer_local_plan"
        )
        self.assertEqual(parsed.config, self.config)
        self.assertEqual(parsed.clearance_report, self.clearance)
        self.assertEqual(parsed.clearance_sha256, self.clearance_sha256)
        self.assertEqual(parsed.output, self.output)
        for forbidden in (
            "tools_config",
            "gmsh",
            "su2_cfd",
            "pvbatch",
            "nproc",
            "timeout",
            "quiet",
        ):
            self.assertFalse(hasattr(parsed, forbidden), forbidden)

    def test_pass_writes_exact_strict_json_and_returns_zero(self) -> None:
        return_code, stdout, stderr, normalizer, planner = self._run()

        self.assertEqual(return_code, 0)
        self.assertEqual(stderr, "")
        path = self.output / "boundary_layer_local_schedule.json"
        saved = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda value: self.fail(f"non-finite JSON: {value}"),
        )
        self.assertEqual(saved["status"], "PASS")
        self.assertEqual(Path(saved["report_path"]), path)
        self.assertEqual(
            Path(saved["command_inputs"]["clearance_report_path"]), self.clearance
        )
        self.assertEqual(
            saved["command_inputs"]["clearance_report_sha256"],
            self.clearance_sha256,
        )
        self.assertEqual(json.loads(stdout)["status"], "PASS")
        normalizer.assert_called_once_with(
            {"schema": "test"}, repository_root=self.root
        )
        planner.assert_called_once_with(
            coarse_contract=self.contract,
            clearance_report_path=self.clearance,
            clearance_report_sha256=self.clearance_sha256,
        )

    def test_engineering_fail_is_saved_and_returns_nonzero(self) -> None:
        failure = {**self.plan_pass, "status": "FAIL", "failure_reasons": ["thin"]}
        return_code, stdout, stderr, _, _ = self._run(plan_result=failure)

        self.assertEqual(return_code, 1)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout)["status"], "FAIL")
        saved = json.loads(
            (self.output / "boundary_layer_local_schedule.json").read_text("utf-8")
        )
        self.assertEqual(saved["failure_reasons"], ["thin"])

    def test_planner_exception_preserves_traceback_and_returns_nonzero(self) -> None:
        return_code, stdout, stderr, _, _ = self._run(
            plan_side_effect=RuntimeError("local planning exploded")
        )

        self.assertEqual(return_code, 1)
        self.assertEqual(stderr, "")
        summary = json.loads(stdout)
        self.assertEqual(summary["status"], "FAIL")
        disk = json.loads(
            (self.output / "boundary_layer_local_schedule.json").read_text("utf-8")
        )
        self.assertEqual(disk["error"]["type"], "RuntimeError")
        self.assertIn("local planning exploded", disk["error"]["traceback"])

    def test_nonfinite_planner_result_becomes_strict_failure_json(self) -> None:
        invalid = {**self.plan_pass, "minimum": float("nan")}
        return_code, _, _, _, _ = self._run(plan_result=invalid)

        self.assertEqual(return_code, 1)
        text = (self.output / "boundary_layer_local_schedule.json").read_text(
            encoding="utf-8"
        )
        saved = json.loads(
            text,
            parse_constant=lambda value: self.fail(f"non-finite JSON: {value}"),
        )
        self.assertEqual(saved["status"], "FAIL")
        self.assertEqual(saved["error"]["type"], "ValueError")

    def test_existing_or_escaping_output_is_rejected_without_overwrite(self) -> None:
        self.output.mkdir()
        sentinel = self.output / "sentinel.txt"
        sentinel.write_text("keep\n", encoding="utf-8")
        code, _, stderr, _, planner = self._run()
        self.assertEqual(code, 1)
        self.assertIn("must be new", stderr)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")
        self.assertFalse(
            (self.output / "boundary_layer_local_schedule.json").exists()
        )
        planner.assert_not_called()

        escaped = self.output_root / ".." / "escape"
        code, _, stderr, _, planner = self._run(
            arguments=self.arguments(output=escaped)
        )
        self.assertEqual(code, 1)
        self.assertIn("must not contain '..'", stderr)
        self.assertFalse((self.root / "runs" / "mesh" / "escape").exists())
        planner.assert_not_called()

    def test_report_outside_coarse_root_writes_fail_without_planning(self) -> None:
        outside = self.root / "outside.json"
        outside.write_text("{}\n", encoding="utf-8")
        code, stdout, stderr, normalizer, planner = self._run(
            arguments=self.arguments(clearance=outside)
        )

        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout)["status"], "FAIL")
        saved = json.loads(
            (self.output / "boundary_layer_local_schedule.json").read_text("utf-8")
        )
        self.assertIn("below", saved["error"]["message"])
        normalizer.assert_not_called()
        planner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
