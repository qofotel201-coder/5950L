"""Regression tests for fail-fast connection-stage orchestration."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from cfdpipe import pipeline as pipeline_module
from cfdpipe.pipeline import (
    Pipeline,
    PipelineStage,
    PipelineStageError,
    RealConnectionInputs,
)


CONNECTION_OUTPUT = Path(__file__).resolve().parents[1] / "runs" / "connection"


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        CONNECTION_OUTPUT.mkdir(parents=True, exist_ok=True)
        self.temporary_directory = tempfile.TemporaryDirectory(
            dir=CONNECTION_OUTPUT
        )
        self.addCleanup(self.temporary_directory.cleanup)
        self.output_dir = Path(self.temporary_directory.name)

    def _freeze_inputs(
        self,
        *,
        case_id: str = "M50_H21_A8_B0",
        create_freeze: bool = True,
    ) -> tuple[RealConnectionInputs, Path]:
        root = (self.output_dir / "repository").resolve()
        config = root / "config"
        config.mkdir(parents=True)
        freeze = config / "rear_outlet_freeze.toml"
        if create_freeze:
            freeze.write_text("case-specific evidence contract\n", encoding="utf-8")
        inputs = RealConnectionInputs(
            repository_root=root,
            step_path=root / "geometry" / "raw" / "model1.step",
            pipeline_geometry_path=root / "geometry" / "derived" / "model1.brep",
            project_path=config / "project.toml",
            cases_path=config / "cases.csv",
            markers_path=config / "markers.toml",
            project={
                "boundary_conditions": {
                    "rear_outlets": {"mode": "TBD_AFTER_SUPERSONIC_PILOT"}
                },
                "boundaries": {
                    "rear_outlet_roles": ["rear_outlet_1", "rear_outlet_2"]
                },
            },
            cases=(),
            markers={},
            measurements={},
            marker_document={},
            requested_case_id=case_id,
            input_records={},
            topology_smoke_path=config / "topology_smoke.toml",
            topology_smoke_refinement={},
        )
        return inputs, freeze

    @staticmethod
    def _passing_freeze_evaluation(
        case_id: str = "M50_H21_A8_B0",
    ) -> dict[str, object]:
        return {
            "schema": "cfdpipe.rear_outlet_freeze_report.v1",
            "status": "PASS",
            "boundary_mode_frozen": True,
            "production_eligible": False,
            "scope": {"case_id": case_id},
            "boundary": {
                "rear_outlet_markers": ["rear_outlet_1", "rear_outlet_2"],
                "mode": "supersonic_outlet",
                "su2_option": "MARKER_SUPERSONIC_OUTLET",
                "allow_static_pressure": False,
                "allow_back_pressure": False,
                "boundary_mode_frozen": True,
            },
            "errors": [],
        }

    def test_stages_run_in_order_and_write_a_completion_record(self) -> None:
        calls: list[str] = []
        stages = (
            PipelineStage("first", lambda: calls.append("first")),
            PipelineStage("second", lambda: calls.append("second")),
        )

        result = Pipeline(self.output_dir).run(stages, name="test-success")

        self.assertEqual(calls, ["first", "second"])
        payload = json.loads(result.log_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(
            [record["name"] for record in payload["stages"]],
            ["first", "second"],
        )

    def test_failure_stops_downstream_and_retains_original_cause(self) -> None:
        calls: list[str] = []
        original = RuntimeError("original stderr remains available")

        def fail() -> None:
            calls.append("fail")
            raise original

        stages = (
            PipelineStage("first", lambda: calls.append("first")),
            PipelineStage("fail", fail),
            PipelineStage("downstream", lambda: calls.append("downstream")),
        )

        with self.assertRaises(PipelineStageError) as raised:
            Pipeline(self.output_dir).run(stages, name="test-failure")

        self.assertEqual(calls, ["first", "fail"])
        self.assertIs(raised.exception.cause, original)
        self.assertIs(raised.exception.__cause__, original)
        payload = json.loads(
            raised.exception.log_path.read_text(encoding="utf-8")
        )
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(
            [record["name"] for record in payload["stages"]],
            ["first", "fail"],
        )
        self.assertEqual(
            payload["stages"][-1]["error_message"],
            "original stderr remains available",
        )

    def test_case_specific_freeze_pass_authorizes_only_supersonic_marker(self) -> None:
        inputs, freeze = self._freeze_inputs()
        evaluation = self._passing_freeze_evaluation()
        with mock.patch.object(
            pipeline_module,
            "evaluate_rear_outlet_freeze",
            return_value=evaluation,
        ) as evaluate, mock.patch.object(
            pipeline_module, "_rear_outlet_pilot_gate"
        ) as pilot:
            contract = pipeline_module._rear_outlet_gate(
                inputs,
                inputs.repository_root / "config" / "rear_outlet_pilot.toml",
                mesh_level="smoke",
                nproc=1,
                max_iterations=5,
            )

        evaluate.assert_called_once_with(freeze)
        pilot.assert_not_called()
        self.assertEqual("PASS", contract["status"])
        self.assertEqual("supersonic_outlet", contract["effective_mode"])
        self.assertEqual("MARKER_SUPERSONIC_OUTLET", contract["su2_option"])
        self.assertTrue(contract["boundary_mode_frozen"])
        self.assertTrue(contract["case_specific"])
        self.assertFalse(contract["production_eligible"])
        self.assertFalse(contract["pressure_or_backpressure_guessed"])
        self.assertEqual("TBD_AFTER_SUPERSONIC_PILOT", contract["project_configured_mode"])

    def test_existing_failed_freeze_fails_closed_without_pilot_fallback(self) -> None:
        inputs, _freeze = self._freeze_inputs()
        evaluation = self._passing_freeze_evaluation()
        evaluation.update(
            {
                "status": "FAIL",
                "boundary_mode_frozen": False,
                "errors": [{"code": "RANS_RELATIVE_DRIFT_FAILED"}],
            }
        )
        with mock.patch.object(
            pipeline_module,
            "evaluate_rear_outlet_freeze",
            return_value=evaluation,
        ), mock.patch.object(
            pipeline_module, "_rear_outlet_pilot_gate"
        ) as pilot:
            with self.assertRaises(
                pipeline_module.RealConnectionConfigurationGateError
            ) as raised:
                pipeline_module._rear_outlet_gate(
                    inputs,
                    inputs.repository_root / "config" / "rear_outlet_pilot.toml",
                    mesh_level="smoke",
                    nproc=1,
                    max_iterations=5,
                )

        pilot.assert_not_called()
        self.assertEqual("REAR_OUTLET_FREEZE_EVIDENCE_FAILED", raised.exception.code)
        self.assertIn("RANS_RELATIVE_DRIFT_FAILED", str(raised.exception))

    def test_design_case_freeze_cannot_be_borrowed_by_another_case(self) -> None:
        inputs, _freeze = self._freeze_inputs(case_id="M35_H15_A3_B0")
        with mock.patch.object(
            pipeline_module,
            "evaluate_rear_outlet_freeze",
            return_value=self._passing_freeze_evaluation("M50_H21_A8_B0"),
        ), mock.patch.object(
            pipeline_module, "_rear_outlet_pilot_gate"
        ) as pilot:
            with self.assertRaises(
                pipeline_module.RealConnectionConfigurationGateError
            ) as raised:
                pipeline_module._rear_outlet_gate(
                    inputs,
                    inputs.repository_root / "config" / "rear_outlet_pilot.toml",
                    mesh_level="smoke",
                    nproc=1,
                    max_iterations=5,
                )

        pilot.assert_not_called()
        self.assertEqual("REAR_OUTLET_FREEZE_CASE_MISMATCH", raised.exception.code)

    def test_pass_report_must_still_be_frozen_and_nonproduction(self) -> None:
        inputs, _freeze = self._freeze_inputs()
        cases = (
            ("boundary_mode_frozen", False, "REAR_OUTLET_FREEZE_NOT_FROZEN"),
            ("production_eligible", True, "INVALID_REAR_OUTLET_FREEZE_SCOPE"),
        )
        for key, value, expected_code in cases:
            with self.subTest(key=key):
                evaluation = self._passing_freeze_evaluation()
                evaluation[key] = value
                with mock.patch.object(
                    pipeline_module,
                    "evaluate_rear_outlet_freeze",
                    return_value=evaluation,
                ):
                    with self.assertRaises(
                        pipeline_module.RealConnectionConfigurationGateError
                    ) as raised:
                        pipeline_module._rear_outlet_gate(
                            inputs,
                            inputs.repository_root
                            / "config"
                            / "rear_outlet_pilot.toml",
                            mesh_level="smoke",
                            nproc=1,
                            max_iterations=5,
                        )
                self.assertEqual(expected_code, raised.exception.code)

    def test_missing_freeze_preserves_legacy_pilot_fallback(self) -> None:
        inputs, _freeze = self._freeze_inputs(create_freeze=False)
        expected = {"status": "PASS", "effective_mode": "legacy-pilot"}
        pilot_path = inputs.repository_root / "config" / "rear_outlet_pilot.toml"
        with mock.patch.object(
            pipeline_module, "_rear_outlet_pilot_gate", return_value=expected
        ) as pilot, mock.patch.object(
            pipeline_module, "evaluate_rear_outlet_freeze"
        ) as evaluate:
            contract = pipeline_module._rear_outlet_gate(
                inputs,
                pilot_path,
                mesh_level="smoke",
                nproc=1,
                max_iterations=5,
            )

        self.assertIs(contract, expected)
        pilot.assert_called_once_with(
            inputs,
            pilot_path,
            mesh_level="smoke",
            nproc=1,
            max_iterations=5,
        )
        evaluate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
