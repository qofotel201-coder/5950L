from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import cfdpipe.boundary_layer_clearance as clearance_module
import cfdpipe.cli as cli_module


class BoundaryLayerClearanceCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / "config").mkdir()
        self.config = self.root / "config" / "coarse_mesh.toml"
        self.markers = self.root / "config" / "markers.toml"
        self.config.write_text('schema = "test"\n', encoding="utf-8")
        self.markers.write_text('schema = "test"\n', encoding="utf-8")
        self.output_root = self.root / "runs" / "mesh" / "coarse"
        self.output = self.output_root / "clearance_001"
        self.contract = {
            "pipeline_brep_path": str(self.root / "geometry" / "pipeline.brep")
        }
        self.fake_gmsh = SimpleNamespace(__version__="fake", __file__=__file__)
        self.audit_pass = {
            "schema": "cfdpipe.boundary_layer_clearance.v1",
            "status": "PASS",
            "execution_status": "PASS",
            "clearance_requirement_status": "PASS",
            "scope": {
                "mesh_generated": False,
                "su2_called": False,
                "paraview_called": False,
            },
        }
        self.root_patch = mock.patch.object(cli_module, "REPOSITORY_ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def arguments(self, output: Path | None = None) -> list[str]:
        return [
            "pipeline",
            "boundary-layer-clearance",
            "--config",
            str(self.config),
            "--markers",
            str(self.markers),
            "--output",
            str(self.output if output is None else output),
        ]

    def _run(
        self,
        *,
        audit_result: dict[str, object] | None = None,
        normalize_side_effect: BaseException | None = None,
    ) -> tuple[int, str, str, mock.Mock, mock.Mock]:
        normalizer = mock.Mock(
            return_value=self.contract,
            side_effect=normalize_side_effect,
        )
        auditor = mock.Mock(
            return_value=self.audit_pass if audit_result is None else audit_result
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.dict(sys.modules, {"gmsh": self.fake_gmsh}),
            mock.patch(
                "cfdpipe.coarse_mesh.normalize_coarse_mesh_config", normalizer
            ),
            mock.patch.object(
                clearance_module, "audit_boundary_layer_clearance", auditor
            ),
            mock.patch.object(cli_module, "CommandRunner") as runner,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            return_code = cli_module.main(self.arguments())
        runner.assert_not_called()
        return return_code, stdout.getvalue(), stderr.getvalue(), normalizer, auditor

    def test_parser_registers_read_only_command_without_tool_options(self) -> None:
        parsed = cli_module.build_parser().parse_args(self.arguments())

        self.assertEqual(
            parsed.handler.__name__,
            "_handle_pipeline_boundary_layer_clearance",
        )
        self.assertEqual(parsed.config, self.config)
        self.assertEqual(parsed.markers, self.markers)
        self.assertEqual(parsed.output, self.output)
        for forbidden in (
            "tools_config",
            "gmsh",
            "su2_cfd",
            "pvbatch",
            "nproc",
            "timeout",
        ):
            self.assertFalse(hasattr(parsed, forbidden), forbidden)

    def test_pass_writes_one_strict_json_report_and_returns_zero(self) -> None:
        return_code, stdout, stderr, normalizer, auditor = self._run()

        self.assertEqual(return_code, 0)
        self.assertEqual(stderr, "")
        report_path = self.output / "boundary_layer_clearance.json"
        report = json.loads(
            report_path.read_text(encoding="utf-8"),
            parse_constant=lambda value: self.fail(f"non-finite JSON: {value}"),
        )
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(
            Path(report["command_inputs"]["output_directory"]), self.output
        )
        self.assertEqual(json.loads(stdout)["status"], "PASS")
        normalizer.assert_called_once()
        self.assertEqual(normalizer.call_args.kwargs["repository_root"], self.root)
        auditor.assert_called_once_with(
            gmsh_module=self.fake_gmsh,
            pipeline_brep=self.contract["pipeline_brep_path"],
            coarse_contract=self.contract,
            marker_config={"schema": "test"},
        )

    def test_audit_fail_is_persisted_and_returns_nonzero(self) -> None:
        failure = {
            **self.audit_pass,
            "status": "FAIL",
            "execution_status": "PASS",
            "clearance_requirement_status": "FAIL",
            "failure_reasons": ["insufficient sampled clearance"],
        }
        return_code, stdout, stderr, _, _ = self._run(audit_result=failure)

        self.assertEqual(return_code, 1)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout)["status"], "FAIL")
        saved = json.loads(
            (self.output / "boundary_layer_clearance.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(saved["clearance_requirement_status"], "FAIL")
        self.assertEqual(saved["failure_reasons"], ["insufficient sampled clearance"])

    def test_preflight_exception_keeps_full_traceback_without_importing_audit(self) -> None:
        return_code, stdout, stderr, _, auditor = self._run(
            normalize_side_effect=RuntimeError("normalization exploded")
        )

        self.assertEqual(return_code, 1)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout)["status"], "FAIL")
        auditor.assert_not_called()
        saved = json.loads(
            (self.output / "boundary_layer_clearance.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(saved["error"]["type"], "RuntimeError")
        self.assertIn("normalization exploded", saved["error"]["traceback"])
        self.assertFalse(saved["gmsh_session"]["initialize_attempted"])

    def test_non_finite_audit_result_is_replaced_by_strict_failure_json(self) -> None:
        invalid = {**self.audit_pass, "minimum_clearance_lower_bound_m": float("nan")}
        return_code, _, _, _, _ = self._run(audit_result=invalid)

        self.assertEqual(return_code, 1)
        text = (self.output / "boundary_layer_clearance.json").read_text(
            encoding="utf-8"
        )
        saved = json.loads(
            text,
            parse_constant=lambda value: self.fail(f"non-finite JSON: {value}"),
        )
        self.assertEqual(saved["status"], "FAIL")
        self.assertEqual(saved["error"]["type"], "ValueError")

    def test_existing_or_escaping_output_is_rejected_without_overwrite(self) -> None:
        self.output.mkdir(parents=True)
        sentinel = self.output / "sentinel.txt"
        sentinel.write_text("keep\n", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli_module.main(self.arguments()), 1)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")
        self.assertFalse((self.output / "boundary_layer_clearance.json").exists())

        escaped = self.output_root / ".." / "escape"
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli_module.main(self.arguments(escaped)), 1)
        self.assertFalse((self.root / "runs" / "mesh" / "escape").exists())


if __name__ == "__main__":
    unittest.main()
