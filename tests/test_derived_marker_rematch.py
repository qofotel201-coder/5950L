"""Tests for fail-closed post-repair marker rematching."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from cfdpipe.derived_marker_rematch import (
    DerivedMarkerRematchError,
    rematch_derived_markers,
)


def _canonical_sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DerivedMarkerRematchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source_step_sha = _digest("original-step")
        self.derived_cad_sha = _digest("derived-cad")
        self.roles = {
            "front_conical_surface": "farfield",
            "vehicle_internal_and_external_walls": "wall",
            "rear_outlet_1": "rear_outlet",
            "rear_outlet_2": "rear_outlet",
        }
        self.original = {
            "status": "PASS",
            "surfaces": [
                self._surface(tag, float(tag), [tag]) for tag in range(1, 7)
            ],
        }
        self.group_ids = {role: _digest(f"group:{role}") for role in self.roles}
        self.member_ids = {
            role: _digest(f"source-surface:{index}")
            for index, role in enumerate(self.roles, start=1)
        }
        expected_measurement_payload = {
            "schema": "cfdpipe.measurement_surface_fingerprint.v1",
            "source_sha256": self.source_step_sha,
            "plane_axis": "x",
            "plane_coordinate_m": 3.77,
            "normal_unit": [1.0, 0.0, 0.0],
            "bounds_m": [3.77, -0.06, -0.06, 3.77, 0.06, 0.06],
            "area_m2": 0.0113,
        }
        self.confirmation = {
            "schema_version": 1,
            "document_kind": "cfdpipe_marker_confirmation_validation",
            "status": "PASS",
            "confirmation": {"sha256": _digest("confirmed-document")},
            "validation": {
                "status": "PASS",
                "marker_configuration_ready": True,
                "markers_toml_written": False,
                "source_step_sha256": self.source_step_sha,
                "current_evidence": {
                    "surface_catalog_sha256": _canonical_sha(self.original)
                },
                "roles": {},
                "human_confirmation": {
                    "status": "PASS",
                    "gmsh_tags_are_matching_criteria": False,
                    "stable_group_fingerprint_ids_by_role": dict(self.group_ids),
                },
                "policy": {
                    "gmsh_tags_are_audit_only": True,
                    "every_selected_fingerprint_uniquely_matched": True,
                    "all_solver_boundaries_require_complete_surface_groups": True,
                    "duplicate_solver_marker_assignment_forbidden": True,
                    "measurement_surface_is_solver_boundary": False,
                },
            },
        }
        for index, (role, kind) in enumerate(self.roles.items(), start=1):
            self.confirmation["validation"]["roles"][role] = {
                "kind": kind,
                "solver_boundary": True,
                "group_fingerprint_id": self.group_ids[role],
                "complete_member_count": 1,
                "matched_entities": [
                    {
                        "fingerprint_id": self.member_ids[role],
                        "audit": {
                            "gmsh_surface_entity_tag": index,
                            "tag_is_matching_criterion": False,
                        },
                    }
                ],
            }
        measurement_hash = _canonical_sha(expected_measurement_payload)
        self.confirmation["validation"]["roles"]["turning_section_outlet"] = {
            "kind": "measurement_surface",
            "solver_boundary": False,
            "measurement_candidate": {
                "candidate_id": f"measurement_loop_{measurement_hash[:16]}",
                "role": "turning_section_outlet",
                "solver_boundary": False,
                "definition": {
                    "origin_m": [3.77, 0.0, 0.0],
                    "normal": [1.0, 0.0, 0.0],
                    "boundary_loop_fingerprint": {
                        "payload": expected_measurement_payload,
                        "sha256": measurement_hash,
                    },
                },
            },
        }
        self.lineage = {
            "status": "PASS",
            "source_step_sha256": self.source_step_sha,
            "derived_cad_sha256": self.derived_cad_sha,
            "external_surface_lineage": [],
            "interface_lineages": [
                {
                    "status": "PASS",
                    "classification": "shared_interface",
                    "original_surface_tags_audit": [5, 6],
                    "descendant_surface_tags_audit": [201],
                }
            ],
        }
        for index, role in enumerate(self.roles, start=1):
            self.lineage["external_surface_lineage"].append(
                {
                    "status": "PASS",
                    "classification": "external_boundary",
                    "original_surface_tag_audit": index,
                    "original_fingerprint_id": self.member_ids[role],
                    "semantic_role": role,
                    "semantic_group_fingerprint_id": self.group_ids[role],
                    "match_mode": "one_to_one",
                    "descendant_surface_tags_audit": [100 + index],
                    "coverage": {"status": "PASS"},
                }
            )
        self.derived = {
            "status": "PASS",
            "derived_cad_sha256": self.derived_cad_sha,
            "surfaces": [
                self._surface(100 + index, float(index), [10])
                for index in range(1, 5)
            ]
            + [self._surface(201, 5.0, [10, 20])],
        }
        derived_measurement_payload = dict(expected_measurement_payload)
        derived_measurement_payload["source_sha256"] = self.derived_cad_sha
        derived_measurement_hash = _canonical_sha(derived_measurement_payload)
        self.measurement = {
            "status": "PASS",
            "derived_cad_sha256": self.derived_cad_sha,
            "loops": [
                {
                    "plane_axis": "x",
                    "plane_coordinate_m": 3.77,
                    "normal_unit": [1.0, 0.0, 0.0],
                    "bounds_m": [3.77, -0.06, -0.06, 3.77, 0.06, 0.06],
                    "area_m2": 0.0113,
                    "closed": True,
                    "solver_boundary": False,
                    "fingerprint": {
                        "payload": derived_measurement_payload,
                        "sha256": derived_measurement_hash,
                    },
                }
            ],
            "classification": {
                "geometry_status": "CONFIRMED_GEOMETRY",
                "selected_fingerprint_sha256": derived_measurement_hash,
            },
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _surface(tag: int, coordinate: float, volumes: list[int]) -> dict:
        return {
            "entity_tag": tag,
            "area_m2": 1.0,
            "centroid_m": [coordinate, 0.0, 0.0],
            "bounding_box_m": [
                coordinate,
                -0.5,
                -0.5,
                coordinate,
                0.5,
                0.5,
            ],
            "entity_type": "Plane",
            "adjacent_volumes": volumes,
        }

    def _run(self, filename: str = "rematch.json") -> dict:
        return rematch_derived_markers(
            self.confirmation,
            self.original,
            self.lineage,
            self.derived,
            self.measurement,
            self.root / filename,
        )

    def test_complete_rematch_writes_tag_free_stable_groups(self) -> None:
        output = self.root / "rematch.json"
        result = self._run()

        self.assertEqual(result["status"], "PASS")
        self.assertTrue(output.is_file())
        self.assertEqual(len(result["roles"]), 5)
        self.assertFalse(
            result["roles"]["turning_section_outlet"]["solver_boundary"]
        )
        for role in self.roles:
            group = result["roles"][role]
            self.assertTrue(len(group["derived_group_fingerprint_id"]) == 64)
            self.assertEqual(
                group["derived_group_fingerprint"][
                    "source_confirmed_group_fingerprint_id"
                ],
                self.group_ids[role],
            )

        def assert_no_tag_keys(value: object) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    lowered = key.lower()
                    self.assertNotEqual(lowered, "tag")
                    self.assertFalse(lowered.endswith("_tag"))
                    self.assertFalse(lowered.endswith("_tags"))
                    self.assertNotIn("entity_tag", lowered)
                    self.assertNotIn("runtime_tag", lowered)
                    assert_no_tag_keys(child)
            elif isinstance(value, list):
                for child in value:
                    assert_no_tag_keys(child)

        assert_no_tag_keys(json.loads(output.read_text(encoding="utf-8")))

    def test_output_is_new_only(self) -> None:
        self._run()
        with self.assertRaisesRegex(DerivedMarkerRematchError, "overwrite"):
            self._run()

    def test_accepts_repair_catalog_fingerprint_contract_without_top_status(self) -> None:
        self.derived.pop("status")
        self.derived.pop("derived_cad_sha256")
        for surface in self.derived["surfaces"]:
            payload = {
                "schema": "cfdpipe.derived_surface_geometry.v1",
                "derived_cad_sha256": self.derived_cad_sha,
                "entity_type": surface["entity_type"],
                "area_m2": surface["area_m2"],
                "centroid_m": surface["centroid_m"],
                "bounding_box_m": surface["bounding_box_m"],
                "boundary_curve_count": 4,
                "adjacent_volume_count": len(surface["adjacent_volumes"]),
            }
            surface["surface_fingerprint"] = payload
            surface["surface_fingerprint_id"] = _canonical_sha(payload)
        for entry in self.lineage["external_surface_lineage"]:
            entry["classification"] = "external_solver_boundary"
            entry["match_mode"] = "one_to_one_geometry_and_volume_lineage"

        result = self._run("repair-contract.json")

        self.assertEqual(result["status"], "PASS")

    def test_missing_confirmed_lineage_fails_without_output(self) -> None:
        self.lineage["external_surface_lineage"].pop()
        output = self.root / "missing.json"
        with self.assertRaisesRegex(DerivedMarkerRematchError, "cover every"):
            self._run(output.name)
        self.assertFalse(output.exists())

    def test_overlapping_solver_descendants_fail_closed(self) -> None:
        self.lineage["external_surface_lineage"][1][
            "descendant_surface_tags_audit"
        ] = [101]
        with self.assertRaisesRegex(DerivedMarkerRematchError, "overlap"):
            self._run("overlap.json")

    def test_nonexternal_solver_descendant_fails_closed(self) -> None:
        self.derived["surfaces"][0]["adjacent_volumes"] = [10, 20]
        with self.assertRaisesRegex(DerivedMarkerRematchError, "not an external"):
            self._run("nonexternal.json")

    def test_incomplete_descendant_union_fails_geometry_check(self) -> None:
        self.derived["surfaces"][0]["area_m2"] = 0.75
        with self.assertRaisesRegex(DerivedMarkerRematchError, "area is incomplete"):
            self._run("incomplete.json")

    def test_interface_must_be_shared_and_exhaust_original_remainder(self) -> None:
        self.lineage["interface_lineages"][0]["classification"] = "external"
        with self.assertRaisesRegex(DerivedMarkerRematchError, "shared_interface"):
            self._run("bad-interface.json")

        self.lineage["interface_lineages"][0]["classification"] = "shared_interface"
        self.lineage["interface_lineages"][0]["original_surface_tags_audit"] = [4, 6]
        with self.assertRaisesRegex(DerivedMarkerRematchError, "non-solver original"):
            self._run("wrong-interface-pair.json")

    def test_measurement_must_rematch_uniquely_and_remain_non_solver(self) -> None:
        duplicate = copy.deepcopy(self.measurement["loops"][0])
        duplicate["fingerprint"]["payload"]["area_m2"] = 0.0113
        duplicate["fingerprint"]["sha256"] = _canonical_sha(
            duplicate["fingerprint"]["payload"]
        )
        self.measurement["loops"].append(duplicate)
        with self.assertRaisesRegex(DerivedMarkerRematchError, "uniquely"):
            self._run("ambiguous-measurement.json")

        self.measurement["loops"].pop()
        self.measurement["loops"][0]["solver_boundary"] = True
        with self.assertRaisesRegex(DerivedMarkerRematchError, "solver boundary"):
            self._run("solver-measurement.json")

    def test_stale_original_catalog_hash_fails_closed(self) -> None:
        self.original["surfaces"][0]["area_m2"] = 2.0
        with self.assertRaisesRegex(DerivedMarkerRematchError, "hash disagrees"):
            self._run("stale-catalog.json")


if __name__ == "__main__":
    unittest.main()
