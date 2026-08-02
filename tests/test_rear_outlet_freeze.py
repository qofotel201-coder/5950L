from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from cfdpipe.rear_outlet_freeze import evaluate_rear_outlet_freeze


HASHES = {
    "source_step": "1" * 64,
    "project": "2" * 64,
    "cases": "3" * 64,
    "markers": "4" * 64,
    "topology": "5" * 64,
    "euler_mesh": "6" * 64,
    "rans_mesh": "7" * 64,
}
OUTLETS = ["rear_outlet_1", "rear_outlet_2"]


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class EvidenceFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.records: list[dict[str, str]] = []
        self.manifests: dict[str, dict] = {}
        self.diagnostics: dict[str, dict] = {}
        self.paths: dict[str, tuple[Path, Path]] = {}

    def _outlet_payload(self, stage: str, offset: float) -> dict:
        mesh_sha = HASHES["euler_mesh"] if stage == "euler_supplement" else HASHES["rans_mesh"]
        markers = {}
        for index, marker in enumerate(OUTLETS, start=1):
            markers[marker] = {
                "area_m2": float(index),
                "face_count": index * 10,
                "normal_mach_vertex_min": 1.30 + offset + index * 0.01,
                "normal_mach_vertex_max": 1.70 + offset + index * 0.01,
                "normal_mach_face_centroid_min": 1.35 + offset + index * 0.01,
                "normal_mach_face_centroid_max": 1.65 + offset + index * 0.01,
                "normal_mach_area_weighted_mean": 1.50 + offset + index * 0.01,
                "net_mass_flow_kg_s": index * 10.0 * (1.0 + offset),
                "subsonic_normal_area_fraction": 0.0,
                "nonpositive_normal_mach_area_fraction": 0.0,
                "backflow_area_fraction": 0.0,
                "reverse_mass_flow_kg_s": 0.0,
            }
        payload = {
            "computation_status": "PASS",
            "mesh": {
                "sha256": mesh_sha,
                "point_count": 100,
                "marker_count": 4,
            },
            "topology": {
                "coverage_complete": True,
                "owner_errors": 0,
                "degenerate_faces": 0,
                "exterior_face_count": 100,
                "marker_face_count": 100,
            },
            "outlet_markers": list(OUTLETS),
            "markers": markers,
            "mass_balance": {"relative_global_imbalance": 0.001},
        }
        if stage == "euler_supplement":
            payload["mesh"]["tetrahedron_count"] = 200
        else:
            payload["mesh"]["volume_element_count"] = 200
            for values in payload["markers"].values():
                values["normal_mach_evaluated"] = True
        return payload

    def _diagnostic(self, stage: str, offset: float) -> dict:
        payload = self._outlet_payload(stage, offset)
        if stage == "euler_supplement":
            return {"status": "PASS", "production_eligible": False, **payload}
        return {
            "status": "PASS",
            "production_eligible": False,
            "diagnostic_only": True,
            "yplus": {"status": "PASS"},
            "outlets_and_mass_balance": payload,
        }

    def _common_rans_inputs(self, order: int) -> dict:
        return {
            "selected_case": {"case_id": "M50_H21_A8_B0"},
            "source_records": {
                "step": {"sha256": HASHES["source_step"]},
                "project": {"sha256": HASHES["project"]},
                "cases": {"sha256": HASHES["cases"]},
                "markers": {"sha256": HASHES["markers"]},
                "topology_smoke_config": {"sha256": HASHES["topology"]},
            },
            "mesh": {"sha256": HASHES["rans_mesh"]},
            "rans_config": {
                "normalized": {
                    "numerics": {"spatial_order": order},
                    "boundary": {
                        "rear_outlet_roles": list(OUTLETS),
                        "su2_rear_outlet_option": "MARKER_SUPERSONIC_OUTLET",
                        "allow_static_outlet_pressure": False,
                        "allow_back_pressure": False,
                    },
                }
            },
        }

    def add(
        self,
        evidence_id: str,
        stage: str,
        *,
        offset: float,
        restart_from: str = "",
    ) -> None:
        diagnostics = self._diagnostic(stage, offset)
        diagnostics_path = self.root / f"{evidence_id}.diagnostics.json"
        diagnostics_path.write_text(
            json.dumps(diagnostics, sort_keys=True), encoding="utf-8"
        )
        diagnostics_sha = file_sha256(diagnostics_path)
        if stage == "euler_supplement":
            manifest = {
                "status": "PASS",
                "diagnostic_gate": "PASS",
                "production_eligible": False,
                "inputs": {
                    "selected_case": {"case_id": "M50_H21_A8_B0"},
                    "source_step": {"sha256": HASHES["source_step"]},
                    "project": {"sha256": HASHES["project"]},
                    "cases": {"sha256": HASHES["cases"]},
                    "markers": {"sha256": HASHES["markers"]},
                    "topology_smoke": {"sha256": HASHES["topology"]},
                    "mesh": {"sha256": HASHES["euler_mesh"]},
                },
                "preparation": {
                    "markers": {"rear_outlets": list(OUTLETS)},
                    "scope": {"outlet_pressure_or_backpressure_set": False},
                },
                "su2": {"status": "PASS", "return_code": 0},
                "paraview": {"diagnostics_sha256": diagnostics_sha},
            }
        else:
            order = 1 if stage == "rans_first_order" else 2
            restart_output_sha = hashlib.sha256(
                f"{evidence_id}-restart".encode("utf-8")
            ).hexdigest()
            manifest = {
                "schema": "cfdpipe.rans_smoke_report.v1",
                "status": "PASS",
                "startup_gate": "PASS",
                "yplus_gate": "PASS",
                "production_eligible": False,
                "inputs": self._common_rans_inputs(order),
                "su2": {
                    "status": "PASS",
                    "return_code": 0,
                    "restart_sha256": restart_output_sha,
                },
                "paraview": {"diagnostics_sha256": diagnostics_sha},
            }
            if stage == "rans_second_order_restart":
                source_manifest_path, _ = self.paths[restart_from]
                source_manifest = self.manifests[restart_from]
                manifest["restart_lineage"] = {
                    "used": True,
                    "source_manifest_sha256": file_sha256(source_manifest_path),
                    "source_su2_manifest_sha256": "a" * 64,
                    "input_restart_sha256": source_manifest["su2"]["restart_sha256"],
                }
                manifest["spatial_order"] = 2
                manifest["restart_used"] = True
        manifest_path = self.root / f"{evidence_id}.manifest.json"
        manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
        self.paths[evidence_id] = (manifest_path, diagnostics_path)
        self.manifests[evidence_id] = manifest
        self.diagnostics[evidence_id] = diagnostics
        self.records.append(
            {
                "id": evidence_id,
                "stage": stage,
                "manifest_path": manifest_path.name,
                "manifest_sha256": file_sha256(manifest_path),
                "diagnostics_path": diagnostics_path.name,
                "diagnostics_sha256": diagnostics_sha,
                "restart_from": restart_from,
            }
        )

    def rewrite_record_files(self, evidence_id: str) -> None:
        manifest_path, diagnostics_path = self.paths[evidence_id]
        diagnostics_path.write_text(
            json.dumps(self.diagnostics[evidence_id], sort_keys=True), encoding="utf-8"
        )
        diagnostics_sha = file_sha256(diagnostics_path)
        self.manifests[evidence_id]["paraview"]["diagnostics_sha256"] = diagnostics_sha
        manifest_path.write_text(
            json.dumps(self.manifests[evidence_id], sort_keys=True), encoding="utf-8"
        )
        record = next(record for record in self.records if record["id"] == evidence_id)
        record["diagnostics_sha256"] = diagnostics_sha
        record["manifest_sha256"] = file_sha256(manifest_path)

    def write_contract(self, name: str = "freeze.toml") -> Path:
        lines = [
            'schema = "cfdpipe.rear_outlet_freeze.v1"',
            'status = "FROZEN_FOR_DESIGN_CASE_ONLY"',
            "",
            "[scope]",
            'case_id = "M50_H21_A8_B0"',
            "production_eligible = false",
            "",
            "[provenance]",
            f'source_step_sha256 = "{HASHES["source_step"]}"',
            f'project_sha256 = "{HASHES["project"]}"',
            f'cases_sha256 = "{HASHES["cases"]}"',
            f'markers_sha256 = "{HASHES["markers"]}"',
            f'topology_smoke_sha256 = "{HASHES["topology"]}"',
            f'euler_mesh_sha256 = "{HASHES["euler_mesh"]}"',
            f'rans_mesh_sha256 = "{HASHES["rans_mesh"]}"',
            "",
            "[boundary]",
            'rear_outlet_markers = ["rear_outlet_1", "rear_outlet_2"]',
            'mode = "supersonic_outlet"',
            'su2_option = "MARKER_SUPERSONIC_OUTLET"',
            "allow_static_pressure = false",
            "allow_back_pressure = false",
            "boundary_mode_frozen = true",
            "",
            "[criteria]",
            "sonic_normal_mach = 1.0",
            "minimum_vertex_normal_mach_margin = 0.20",
            "minimum_face_centroid_normal_mach_margin = 0.20",
            "maximum_relative_global_mass_imbalance = 0.01",
            "maximum_relative_drift_vertex_normal_mach = 0.05",
            "maximum_relative_drift_face_centroid_normal_mach = 0.05",
            "maximum_relative_drift_area_weighted_normal_mach = 0.05",
            "maximum_relative_drift_net_mass_flow = 0.05",
            "require_zero_subsonic_area_fraction = true",
            "require_zero_nonpositive_normal_mach_area_fraction = true",
            "require_zero_backflow_area_fraction = true",
            "require_zero_reverse_mass_flow = true",
            "",
            "[evidence]",
        ]
        for record in self.records:
            lines.extend(
                [
                    "",
                    "[[evidence.records]]",
                    f'id = "{record["id"]}"',
                    f'stage = "{record["stage"]}"',
                    f'manifest_path = "{record["manifest_path"]}"',
                    f'manifest_sha256 = "{record["manifest_sha256"]}"',
                    f'diagnostics_path = "{record["diagnostics_path"]}"',
                    f'diagnostics_sha256 = "{record["diagnostics_sha256"]}"',
                    f'restart_from = "{record["restart_from"]}"',
                ]
            )
        path = self.root / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


class RearOutletFreezeTests(unittest.TestCase):
    def make_complete_fixture(self, root: Path) -> EvidenceFixture:
        fixture = EvidenceFixture(root)
        fixture.add("euler", "euler_supplement", offset=0.0)
        fixture.add("rans_fo_1", "rans_first_order", offset=0.0)
        fixture.add("rans_fo_2", "rans_first_order", offset=0.005)
        fixture.add(
            "rans_so_1",
            "rans_second_order_restart",
            offset=0.01,
            restart_from="rans_fo_2",
        )
        return fixture

    def test_complete_evidence_passes_and_freezes_boundary_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self.make_complete_fixture(root)
            contract = fixture.write_contract()
            output = root / "result" / "freeze_report.json"

            report = evaluate_rear_outlet_freeze(contract, output)

            self.assertEqual("PASS", report["status"])
            self.assertTrue(report["boundary_mode_frozen"])
            self.assertFalse(report["production_eligible"])
            self.assertEqual([], report["errors"])
            self.assertTrue(report["checks"]["second_order_restart_lineage"])
            self.assertTrue(report["checks"]["rans_relative_drift"])
            self.assertEqual(report, json.loads(output.read_text(encoding="utf-8")))

    def test_missing_required_stage_returns_fail_without_throwing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = EvidenceFixture(root)
            fixture.add("euler", "euler_supplement", offset=0.0)
            fixture.add("rans_fo_1", "rans_first_order", offset=0.0)
            fixture.add("rans_fo_2", "rans_first_order", offset=0.005)

            report = evaluate_rear_outlet_freeze(fixture.write_contract())

            self.assertEqual("FAIL", report["status"])
            self.assertFalse(report["boundary_mode_frozen"])
            self.assertIn(
                "EVIDENCE_STAGE_COUNT_FAILED",
                {error["code"] for error in report["errors"]},
            )

    def test_bad_physics_accumulates_all_gate_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self.make_complete_fixture(root)
            payload = fixture.diagnostics["rans_so_1"]["outlets_and_mass_balance"]
            marker = payload["markers"]["rear_outlet_1"]
            marker["normal_mach_vertex_min"] = 1.01
            marker["normal_mach_face_centroid_min"] = 1.02
            marker["subsonic_normal_area_fraction"] = 0.1
            marker["nonpositive_normal_mach_area_fraction"] = 0.1
            marker["backflow_area_fraction"] = 0.2
            marker["reverse_mass_flow_kg_s"] = 1.0
            payload["mass_balance"]["relative_global_imbalance"] = 0.2
            fixture.rewrite_record_files("rans_so_1")

            report = evaluate_rear_outlet_freeze(fixture.write_contract())

            codes = [error["code"] for error in report["errors"]]
            self.assertEqual("FAIL", report["status"])
            self.assertGreaterEqual(codes.count("NORMAL_MACH_MARGIN_FAILED"), 2)
            self.assertGreaterEqual(codes.count("ZERO_FLOW_CRITERION_FAILED"), 4)
            self.assertIn("MASS_BALANCE_FAILED", codes)
            self.assertIn("RANS_RELATIVE_DRIFT_FAILED", codes)

    def test_hash_case_marker_and_topology_mismatches_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self.make_complete_fixture(root)
            manifest = fixture.manifests["rans_fo_1"]
            manifest["inputs"]["selected_case"]["case_id"] = "wrong"
            manifest["inputs"]["source_records"]["project"]["sha256"] = "8" * 64
            diagnostic = fixture.diagnostics["rans_fo_1"]["outlets_and_mass_balance"]
            diagnostic["outlet_markers"] = ["rear_outlet_1", "wrong"]
            diagnostic["topology"]["coverage_complete"] = False
            fixture.rewrite_record_files("rans_fo_1")

            report = evaluate_rear_outlet_freeze(fixture.write_contract())

            codes = {error["code"] for error in report["errors"]}
            self.assertEqual("FAIL", report["status"])
            self.assertTrue(
                {"CASE_ID_MISMATCH", "INPUT_HASH_MISMATCH", "MARKER_MISMATCH", "TOPOLOGY_INVALID"}
                <= codes
            )

    def test_euler_equivalent_mach_evidence_and_mesh_count_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self.make_complete_fixture(root)
            diagnostic = fixture.diagnostics["euler"]
            del diagnostic["mesh"]["tetrahedron_count"]
            del diagnostic["markers"]["rear_outlet_1"]["normal_mach_vertex_max"]
            fixture.rewrite_record_files("euler")
            output = root / "fail.json"

            report = evaluate_rear_outlet_freeze(fixture.write_contract(), output)

            self.assertEqual("FAIL", report["status"])
            codes = {error["code"] for error in report["errors"]}
            self.assertIn("MESH_METADATA_INVALID", codes)
            self.assertIn("METRIC_INVALID", codes)
            self.assertIsNone(report["evidence"][0]["checkpoint"]["mesh"]["volume_element_count"])
            self.assertEqual(report, json.loads(output.read_text(encoding="utf-8")))

    def test_nonfinite_json_is_a_data_gate_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self.make_complete_fixture(root)
            _, diagnostics_path = fixture.paths["rans_fo_1"]
            text = diagnostics_path.read_text(encoding="utf-8")
            diagnostics_path.write_text(
                text.replace('"relative_global_imbalance": 0.001', '"relative_global_imbalance": NaN'),
                encoding="utf-8",
            )
            record = next(record for record in fixture.records if record["id"] == "rans_fo_1")
            record["diagnostics_sha256"] = file_sha256(diagnostics_path)

            report = evaluate_rear_outlet_freeze(fixture.write_contract())

            self.assertEqual("FAIL", report["status"])
            self.assertIn(
                "EVIDENCE_JSON_INVALID", {error["code"] for error in report["errors"]}
            )

    def test_broken_restart_lineage_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self.make_complete_fixture(root)
            fixture.manifests["rans_so_1"]["restart_lineage"]["input_restart_sha256"] = "9" * 64
            fixture.rewrite_record_files("rans_so_1")

            report = evaluate_rear_outlet_freeze(fixture.write_contract())

            self.assertEqual("FAIL", report["status"])
            self.assertFalse(report["checks"]["second_order_restart_lineage"])
            self.assertIn(
                "RESTART_LINEAGE_MISMATCH",
                {error["code"] for error in report["errors"]},
            )

    def test_missing_or_malformed_contract_writes_fail_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "failure.json"
            report = evaluate_rear_outlet_freeze(root / "missing.toml", output)
            self.assertEqual("FAIL", report["status"])
            self.assertTrue(output.is_file())
            malformed = root / "bad.toml"
            malformed.write_text("not = [valid", encoding="utf-8")
            report = evaluate_rear_outlet_freeze(malformed)
            self.assertEqual("FAIL", report["status"])
            self.assertEqual("CONTRACT_READ_FAILED", report["errors"][0]["code"])

    def test_invalid_caller_types_are_programming_errors(self) -> None:
        with self.assertRaises(TypeError):
            evaluate_rear_outlet_freeze(123)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            evaluate_rear_outlet_freeze("contract.toml", output_path=object())  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
