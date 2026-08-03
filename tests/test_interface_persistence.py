"""Tests for the post-fragment interface-persistence quality gate."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import cfdpipe.interface_persistence as persistence
from cfdpipe.interface_persistence import (
    InterfacePersistenceError,
    _boundary_matching_across_models,
    _new_output_path,
    _select_unique_matches,
    _validate_repair_documents,
)


class InterfacePersistenceSelectionTests(unittest.TestCase):
    def test_exact_full_coverage_bijection_is_selected(self) -> None:
        rows = [
            {
                "interface_pair_fingerprint_id": "pair-a",
                "derived_shared_surface_tag_audit": 91,
                "status": "FULL_COVERAGE",
            },
            {
                "interface_pair_fingerprint_id": "pair-a",
                "derived_shared_surface_tag_audit": 92,
                "status": "NOT_A_MATCH",
            },
            {
                "interface_pair_fingerprint_id": "pair-b",
                "derived_shared_surface_tag_audit": 91,
                "status": "NOT_A_MATCH",
            },
            {
                "interface_pair_fingerprint_id": "pair-b",
                "derived_shared_surface_tag_audit": 92,
                "status": "FULL_COVERAGE",
            },
        ]
        self.assertEqual(
            _select_unique_matches(rows, ["pair-a", "pair-b"], [91, 92]),
            {"pair-a": 91, "pair-b": 92},
        )

    def test_zero_multiple_reused_or_unexhausted_matches_fail_closed(self) -> None:
        cases = (
            (
                [
                    {
                        "interface_pair_fingerprint_id": "a",
                        "derived_shared_surface_tag_audit": 1,
                        "status": "NOT_A_MATCH",
                    }
                ],
                ["a"],
                [1],
            ),
            (
                [
                    {
                        "interface_pair_fingerprint_id": "a",
                        "derived_shared_surface_tag_audit": 1,
                        "status": "FULL_COVERAGE",
                    },
                    {
                        "interface_pair_fingerprint_id": "a",
                        "derived_shared_surface_tag_audit": 2,
                        "status": "FULL_COVERAGE",
                    },
                ],
                ["a"],
                [1, 2],
            ),
            (
                [
                    {
                        "interface_pair_fingerprint_id": "a",
                        "derived_shared_surface_tag_audit": 1,
                        "status": "FULL_COVERAGE",
                    },
                    {
                        "interface_pair_fingerprint_id": "b",
                        "derived_shared_surface_tag_audit": 1,
                        "status": "FULL_COVERAGE",
                    },
                ],
                ["a", "b"],
                [1, 2],
            ),
        )
        for rows, fingerprints, tags in cases:
            with self.subTest(rows=rows):
                with self.assertRaises(InterfacePersistenceError):
                    _select_unique_matches(rows, fingerprints, tags)


class InterfacePersistenceBoundaryTests(unittest.TestCase):
    def test_boundary_matching_requires_mutual_bijective_sampled_geometry(self) -> None:
        first = {
            1: {"points": [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]], "length_m": 1.0},
            2: {"points": [[0.0, 1.0, 0.0], [1.0, 1.0, 0.0]], "length_m": 1.0},
        }
        second = {
            7: {"points": [[1.0, 0.0, 0.0], [0.0, 0.0, 0.0]], "length_m": 1.0},
            8: {"points": [[1.0, 1.0, 0.0], [0.0, 1.0, 0.0]], "length_m": 1.0},
        }
        with mock.patch.object(
            persistence, "_curve_inventory", side_effect=[first, second]
        ):
            result = _boundary_matching_across_models(
                object(), "source", 10, "derived", 20, 5
            )
        self.assertTrue(result["bijective"])
        self.assertEqual(result["maximum_sample_hausdorff_m"], 0.0)
        self.assertTrue(all(item["mutual_nearest"] for item in result["matches"]))

    def test_boundary_curve_count_change_is_not_accepted(self) -> None:
        with mock.patch.object(
            persistence,
            "_curve_inventory",
            side_effect=[
                {1: {"points": [[0.0, 0.0, 0.0]], "length_m": 1.0}},
                {
                    7: {"points": [[0.0, 0.0, 0.0]], "length_m": 1.0},
                    8: {"points": [[1.0, 0.0, 0.0]], "length_m": 1.0},
                },
            ],
        ):
            result = _boundary_matching_across_models(
                object(), "source", 10, "derived", 20, 5
            )
        self.assertFalse(result["bijective"])


class InterfacePersistencePolicyTests(unittest.TestCase):
    def test_output_is_json_new_only_and_not_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            valid = _new_output_path(root / "evidence" / "proof.json")
            self.assertEqual(valid.name, "proof.json")
            valid.parent.mkdir(parents=True)
            valid.write_text("{}", encoding="utf-8")
            with self.assertRaises(InterfacePersistenceError):
                _new_output_path(valid)
            with self.assertRaises(InterfacePersistenceError):
                _new_output_path(root / "config" / "proof.json")
            with self.assertRaises(InterfacePersistenceError):
                _new_output_path(root / "evidence" / "proof.txt")

    def test_current_project_and_pipeline_hashes_are_mandatory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.step"
            derived = root / "derived.brep"
            project = root / "project.toml"
            manifest_path = root / "manifest.json"
            shared_path = root / "shared.json"
            source.write_bytes(b"source")
            derived.write_bytes(b"derived")
            project.write_bytes(b"project")
            manifest_path.write_text(
                json.dumps(
                    {
                        "status": "PASS",
                        "source_unchanged": True,
                        "source": {"sha256": persistence._sha256(source)},
                        "pipeline_geometry": {
                            "path": str(derived.resolve()),
                            "sha256": persistence._sha256(derived),
                            "pipeline_eligible": True,
                        },
                        "reimport_validation": {
                            "brep": {"status": "PASS", "pipeline_eligible": True}
                        },
                    }
                ),
                encoding="utf-8",
            )
            shared_path.write_text(
                json.dumps(
                    {
                        "status": "PASS",
                        "pipeline_geometry_format": "brep",
                        "brep_reimport": {"status": "PASS", "shared_patch_count": 2},
                    }
                ),
                encoding="utf-8",
            )
            dependencies = {"source_project_sha256": "stale"}
            with self.assertRaisesRegex(
                InterfacePersistenceError, "project.toml"
            ):
                _validate_repair_documents(
                    source=source.resolve(),
                    derived=derived.resolve(),
                    project=project.resolve(),
                    repair_manifest_path=manifest_path.resolve(),
                    shared_interface_path=shared_path.resolve(),
                    dependencies=dependencies,
                )

    def test_source_has_no_mesh_cad_write_gui_or_process_calls(self) -> None:
        source = inspect.getsource(persistence).casefold()
        for forbidden in (
            "mesh.generate(",
            "gmsh.write(",
            "fltk.run(",
            "subprocess.",
            "shell=true",
            "addphysicalgroup(",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)
        self.assertIn("_run_owned_session", source)
        self.assertIn('"finalize_succeeded": true', source)


if __name__ == "__main__":
    unittest.main()
