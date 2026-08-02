from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from cfdpipe.boundary_semantic_candidates import (
    BoundarySemanticCandidateError,
    build_boundary_semantic_candidates,
)


class BoundarySemanticCandidatesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.catalog = self.root / "surface_catalog.csv"
        self.types = self.root / "surface_types.json"
        self.topology = self.root / "step_topology.json"
        self.project = self.root / "project.toml"
        self.interface = self.root / "interface_completeness.json"
        self.source_sha = "a" * 64
        self._write_fixture()

    @staticmethod
    def _surface(
        tag: int,
        *,
        volume: int,
        curves: list[int],
        normal_x: float,
        centroid: list[float],
        area: float = 1.0,
        entity_type: str = "Cone",
    ) -> dict[str, object]:
        return {
            "tag": tag,
            "volume": volume,
            "curves": curves,
            "normal_x": normal_x,
            "centroid": centroid,
            "area": area,
            "type": entity_type,
        }

    def _rows(self) -> list[dict[str, object]]:
        return [
            self._surface(1, volume=1, curves=[1, 2], normal_x=-0.3, centroid=[0.0, 0.0, -1.0]),
            self._surface(2, volume=1, curves=[3, 4], normal_x=0.6, centroid=[2.0, 1.0, 0.0]),
            self._surface(3, volume=1, curves=[2, 5], normal_x=-0.3, centroid=[0.0, 0.0, 1.0]),
            self._surface(4, volume=1, curves=[4, 6], normal_x=1.0, centroid=[3.0, 0.0, 0.0], entity_type="Plane"),
            self._surface(5, volume=1, curves=[3, 6], normal_x=0.6, centroid=[2.0, -1.0, 0.0]),
            self._surface(6, volume=1, curves=[7, 8], normal_x=-1.0, centroid=[1.0, 0.0, -0.2], area=0.01, entity_type="BSpline surface"),
            self._surface(7, volume=1, curves=[7, 9], normal_x=-1.0, centroid=[1.0, 0.0, 0.2], area=0.01, entity_type="BSpline surface"),
            self._surface(54, volume=2, curves=[101, 102], normal_x=0.3, centroid=[0.0, 0.0, -1.0]),
            self._surface(55, volume=2, curves=[103, 104, 106], normal_x=-0.5, centroid=[-1.0, 0.0, -2.0]),
            self._surface(56, volume=2, curves=[101, 105, 106], normal_x=0.9, centroid=[0.0, 2.0, 0.0]),
            self._surface(57, volume=2, curves=[103, 107, 109], normal_x=-0.5, centroid=[-1.0, 0.0, 2.0]),
            self._surface(58, volume=2, curves=[102, 108], normal_x=0.3, centroid=[0.0, 0.0, 1.0]),
            self._surface(59, volume=2, curves=[108, 105, 109], normal_x=0.9, centroid=[0.0, -2.0, 0.0]),
        ]

    def _write_fixture(self, *, offset_tags: int = 0, offset_curves: int = 0) -> None:
        rows = self._rows()
        tag_map = {int(row["tag"]): int(row["tag"]) + offset_tags for row in rows}
        with self.catalog.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=[
                    "entity_tag",
                    "step_name",
                    "area_m2",
                    "centroid_m",
                    "bounding_box_m",
                    "normal_samples",
                    "adjacent_volumes",
                    "boundary_curves",
                ],
            )
            writer.writeheader()
            for row in rows:
                centroid = list(row["centroid"])
                writer.writerow(
                    {
                        "entity_tag": tag_map[int(row["tag"])],
                        "step_name": "",
                        "area_m2": row["area"],
                        "centroid_m": json.dumps(centroid),
                        "bounding_box_m": json.dumps(
                            [
                                centroid[0] - (1.0e-7 if int(row["tag"]) in {4, 6, 7} else 0.1),
                                centroid[1] - 0.1,
                                centroid[2] - 0.1,
                                centroid[0] + (1.0e-7 if int(row["tag"]) in {4, 6, 7} else 0.1),
                                centroid[1] + 0.1,
                                centroid[2] + 0.1,
                            ]
                        ),
                        "normal_samples": json.dumps(
                            [[row["normal_x"], 0.0, 0.0]] * 3
                        ),
                        "adjacent_volumes": json.dumps([row["volume"]]),
                        "boundary_curves": json.dumps(
                            [int(value) + offset_curves for value in row["curves"]]
                        ),
                    }
                )
        self.types.write_text(
            json.dumps(
                {
                    "status": "PASS",
                    "surfaces": [
                        {
                            "entity_tag": tag_map[int(row["tag"])],
                            "entity_type": row["type"],
                        }
                        for row in rows
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.topology.write_text(
            json.dumps(
                {
                    "source_sha256": self.source_sha,
                    "solids": [
                        {"id": 100, "type": "BREP_WITH_VOIDS", "name": ""},
                        {"id": 200, "type": "MANIFOLD_SOLID_BREP", "name": "outer"},
                    ],
                    "shell_component_mapping": [
                        {
                            "status": "MATCHED",
                            "volume_entity_tag": 1,
                            "solid_id": 100,
                            "root_shell_id": 1000,
                            "role_in_solid": "outer_shell",
                            "surface_tags": [tag_map[tag] for tag in [1, 2, 3, 4, 5]],
                        },
                        {
                            "status": "MATCHED",
                            "volume_entity_tag": 1,
                            "solid_id": 100,
                            "root_shell_id": 1001,
                            "role_in_solid": "void_shell",
                            "surface_tags": [tag_map[tag] for tag in [6, 7]],
                        },
                        {
                            "status": "MATCHED",
                            "volume_entity_tag": 2,
                            "solid_id": 200,
                            "root_shell_id": 2000,
                            "role_in_solid": "boundary_shell",
                            "surface_tags": [tag_map[tag] for tag in [54, 55, 56, 57, 58, 59]],
                        },
                    ],
                    "strong_contact_candidates": [
                        {
                            "surface_tags": [tag_map[1], tag_map[54]],
                            "relative_area_difference": 0.0,
                            "centroid_distance_m": 0.0,
                            "bbox_gap_m": 0.0,
                            "occ_minimum_distance_m": 0.0,
                        },
                        {
                            "surface_tags": [tag_map[3], tag_map[58]],
                            "relative_area_difference": 0.0,
                            "centroid_distance_m": 0.0,
                            "bbox_gap_m": 0.0,
                            "occ_minimum_distance_m": 0.0,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.project.write_text(
            '[axes]\nx_positive = "nose_to_tail"\n', encoding="utf-8"
        )
        self.interface.write_text(
            json.dumps(
                {
                    "schema_version": "cfdpipe.interface_coincidence_probe.v1",
                    "status": "PASS",
                    "source_unchanged": True,
                    "source_step_sha256": self.source_sha,
                    "pairs": [
                        {
                            "surface_tags": [tag_map[first], tag_map[second]],
                            "status": "FULL_GEOMETRIC_COINCIDENCE",
                            "bidirectional_surface_projection": {
                                "direction_count": 2,
                                "sample_count_per_direction": 25,
                                "maximum_distance_m": 1.0e-14,
                                "distance_tolerance_m": 1.0e-10,
                            },
                            "matched_parametric_normal_opposition": {
                                "opposite": True,
                                "maximum_dot_product": -0.999999999,
                            },
                            "boundary_curve_matching": {
                                "bijective": True,
                                "curve_count_per_surface": 2,
                                "maximum_sample_hausdorff_m": 1.0e-14,
                                "distance_tolerance_m": 1.0e-10,
                            },
                        }
                        for first, second in ((1, 54), (3, 58))
                    ],
                }
            ),
            encoding="utf-8",
        )

    def _build(self, output: Path | None = None) -> dict[str, object]:
        return build_boundary_semantic_candidates(
            self.catalog,
            self.types,
            self.topology,
            output,
            project_path=self.project,
            interface_completeness_path=self.interface,
        )

    def test_complete_groups_are_derived_without_tag_semantics(self) -> None:
        report = self._build()
        groups = report["geometric_candidates"]
        self.assertEqual(
            [item["audit"]["current_surface_tags"] for item in groups["upstream_outer_groups"]],
            [[55, 57]],
        )
        self.assertEqual(
            sorted(item["audit"]["current_surface_tags"] for item in groups["downstream_outer_groups"]),
            [[2, 4, 5], [56, 59]],
        )
        self.assertEqual(
            [item["audit"]["current_surface_tags"] for item in groups["void_shell_boundary_groups"]],
            [[6, 7]],
        )
        self.assertEqual(
            [item["audit"]["current_surface_tags"] for item in groups["near_coincident_interface_pairs"]],
            [[1, 54], [3, 58]],
        )
        self.assertFalse(report["marker_configuration_ready"])
        self.assertFalse(report["business_semantics_asserted"])
        self.assertTrue(report["partition_audit"]["all_surfaces_assigned_once"])
        self.assertEqual(
            [item["audit"]["current_surface_tags"] for item in report["axial_surface_observations"]],
            [[6, 7], [4]],
        )
        self.assertEqual(
            [item["step_shell_role"] for item in report["axial_surface_observations"]],
            ["void_shell", "outer_shell"],
        )
        downstream_classes = {
            item["fingerprint"]["geometric_class"]
            for item in groups["downstream_outer_groups"]
        }
        self.assertEqual(
            downstream_classes,
            {
                "positive_x_continuation_from_interface",
                "maximum_x_interface_edge_closure",
            },
        )
        self.assertEqual(
            groups["upstream_outer_groups"][0]["fingerprint"]["geometric_class"],
            "lower_x_outer_shell_before_interface_edge",
        )
        self.assertTrue(
            all(
                item["full_surface_coincidence_claimed"]
                for item in groups["near_coincident_interface_pairs"]
            )
        )
        self.assertTrue(
            report["policy"][
                "parametric_gmsh_normals_are_not_treated_as_outward_normals"
            ]
        )

    def test_group_fingerprints_survive_runtime_tag_and_curve_reordering(self) -> None:
        first = self._build()
        first_ids = sorted(
            item["group_fingerprint_id"]
            for groups in first["geometric_candidates"].values()
            for item in groups
            if "group_fingerprint_id" in item
        )
        self._write_fixture(offset_tags=500, offset_curves=9000)
        second = self._build()
        second_ids = sorted(
            item["group_fingerprint_id"]
            for groups in second["geometric_candidates"].values()
            for item in groups
            if "group_fingerprint_id" in item
        )
        self.assertEqual(first_ids, second_ids)

    def test_numbering_contract_requires_complete_group_mapping_not_z(self) -> None:
        report = self._build()
        contract = report["rear_outlet_numbering_contract"]
        self.assertEqual(contract["strategy"], "explicit_group_fingerprint_mapping")
        self.assertEqual(contract["forbidden_inference"], "single_surface_centroid_z")
        self.assertEqual(len(contract["candidate_group_fingerprint_ids"]), 2)
        self.assertEqual(contract["status"], "PENDING_HUMAN_CONFIRMATION")

    def test_output_is_json_new_only_and_never_config(self) -> None:
        output = self.root / "runs" / "boundary_candidates.json"
        report = self._build(output)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), report)
        with self.assertRaisesRegex(BoundarySemanticCandidateError, "overwrite"):
            self._build(output)
        with self.assertRaisesRegex(BoundarySemanticCandidateError, "config"):
            self._build(self.root / "config" / "boundary_candidates.json")

    def test_missing_contact_evidence_fails_closed(self) -> None:
        document = json.loads(self.topology.read_text(encoding="utf-8"))
        document["strong_contact_candidates"] = []
        self.topology.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(
            BoundarySemanticCandidateError, "no strong cross-volume contact"
        ):
            self._build()

    def test_incomplete_interface_probe_fails_closed(self) -> None:
        document = json.loads(self.interface.read_text(encoding="utf-8"))
        document["pairs"][0]["boundary_curve_matching"]["bijective"] = False
        self.interface.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(
            BoundarySemanticCandidateError, "not bijectively matched"
        ):
            self._build()

    def test_project_must_freeze_positive_x_as_nose_to_tail(self) -> None:
        self.project.write_text(
            '[axes]\nx_positive = "tail_to_nose"\n', encoding="utf-8"
        )
        with self.assertRaisesRegex(
            BoundarySemanticCandidateError, "nose_to_tail"
        ):
            self._build()

    def test_accepts_windows_utf8_bom_catalog(self) -> None:
        text = self.catalog.read_text(encoding="utf-8")
        self.catalog.write_text(text, encoding="utf-8-sig")

        report = self._build()

        self.assertEqual(report["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
