"""Synthetic tests for conservative marker review."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from cfdpipe.marker_review import MarkerReviewError, build_marker_review


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MarkerReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.allowed = self.root / "runs" / "real_connection"
        self.allowed.mkdir(parents=True)
        self.evidence = self.allowed / "evidence"
        self.evidence.mkdir()
        self.output = self.allowed / "marker_review"
        self.project = self.allowed / "project.toml"
        self.project.write_text(
            """[boundaries]
farfield_role = "front_conical_surface"
wall_role = "vehicle_internal_and_external_walls"
rear_outlet_roles = ["rear_outlet_1", "rear_outlet_2"]
measurement_surface_role = "turning_section_outlet"
measurement_surface_is_solver_boundary = false
""",
            encoding="utf-8",
        )
        self.catalog = self.evidence / "surface_catalog.csv"
        self.types = self.evidence / "surface_types.json"
        self.render_input = self.evidence / "diagnostic.vtk"
        self.render_input.write_text("vtk evidence", encoding="utf-8")
        self.groups = self.evidence / "groups.json"
        self.groups.write_text('{"groups": []}\n', encoding="utf-8")
        self.image = self.evidence / "all.png"
        self.image.write_bytes(b"synthetic image")
        self.render = self.evidence / "geometry_render_manifest.json"
        self._write_catalog()
        self._write_types()
        self._write_render()
        self.topology = {
            "source_path": str(self.root / "model.step"),
            "source_sha256": "a" * 64,
            "solids": [
                {
                    "id": 10,
                    "type": "BREP_WITH_VOIDS",
                    "name": "large",
                    "face_count": 3,
                    "shell_components": [
                        {"root_shell_id": 101, "role_in_solid": "outer_shell", "face_count": 2},
                        {"root_shell_id": 102, "role_in_solid": "void_shell", "face_count": 1},
                    ],
                },
                {
                    "id": 20,
                    "type": "MANIFOLD_SOLID_BREP",
                    "name": "small",
                    "face_count": 2,
                    "shell_components": [
                        {"root_shell_id": 201, "role_in_solid": "boundary_shell", "face_count": 2}
                    ],
                },
            ],
        }
        self.mapping = {
            "status": "MATCHED",
            "source_sha256": "a" * 64,
            "matches": [
                {"solid_id": 10, "volume_entity_tag": 1, "face_count": 3},
                {"solid_id": 20, "volume_entity_tag": 2, "face_count": 2},
            ],
        }

    def _write_catalog(self, *, names: dict[int, str] | None = None) -> None:
        names = names or {}
        rows = [
            (1, [1], [10, 11], [0.0, 0.0, 0.0, 1.0, 1.0, 1.0]),
            (2, [1], [11, 12], [1.0, 0.0, 0.0, 2.0, 1.0, 1.0]),
            (3, [1], [30], [3.0, 0.0, 0.0, 3.0, 1.0, 1.0]),
            (4, [2], [40, 41], [0.0, 0.0, 0.0, 1.0, 1.0, 1.0]),
            (5, [2], [41, 42], [4.0, 0.0, 0.0, 4.0, 1.0, 1.0]),
        ]
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
            for tag, owners, curves, bounds in rows:
                writer.writerow(
                    {
                        "entity_tag": tag,
                        "step_name": names.get(tag, ""),
                        "area_m2": 1.0,
                        "centroid_m": json.dumps(
                            [
                                (bounds[0] + bounds[3]) / 2,
                                (bounds[1] + bounds[4]) / 2,
                                (bounds[2] + bounds[5]) / 2,
                            ]
                        ),
                        "bounding_box_m": json.dumps(bounds),
                        "normal_samples": json.dumps([[1.0, 0.0, 0.0]]),
                        "adjacent_volumes": json.dumps(owners),
                        "boundary_curves": json.dumps(curves),
                    }
                )

    def _write_types(self, *, status: str = "PASS") -> None:
        surfaces = []
        for tag in range(1, 6):
            surfaces.append(
                {
                    "entity_tag": tag,
                    "entity_type": "Plane" if tag in {3, 5} else "BSpline surface",
                    "adjacent_volumes": [1] if tag <= 3 else [2],
                }
            )
        self.types.write_text(
            json.dumps(
                {
                    "status": status,
                    "surface_catalog_sha256": _sha(self.catalog),
                    "surfaces": surfaces,
                    "distance_evidence": {
                        "surface_pairs": [
                            {
                                "entity_a": [2, 1],
                                "entity_b": [2, 4],
                                "distance_m": 0.0,
                                "prescreen": {
                                    "surface_a": 1,
                                    "surface_b": 4,
                                    "relative_area_difference": 0.0,
                                    "centroid_distance_m": 0.0,
                                    "bbox_gap_m": 0.0,
                                },
                            }
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )

    def _write_render(self, *, status: str = "PASS", bad_hash: bool = False) -> None:
        self.render.write_text(
            json.dumps(
                {
                    "status": status,
                    "input_file": str(self.render_input),
                    "input_sha256": "0" * 64 if bad_hash else _sha(self.render_input),
                    "groups_file": str(self.groups),
                    "groups_sha256": _sha(self.groups),
                    "images": [
                        {"path": str(self.image), "sha256": _sha(self.image)}
                    ],
                }
            ),
            encoding="utf-8",
        )

    def _build(self, **overrides: object) -> dict:
        arguments = {
            "project_path": self.project,
            "surface_catalog_path": self.catalog,
            "surface_types_path": self.types,
            "step_topology": self.topology,
            "solid_volume_mapping": self.mapping,
            "render_manifest_path": self.render,
            "output_directory": self.output,
            "allowed_output_root": self.allowed,
        }
        arguments.update(overrides)
        return build_marker_review(**arguments)

    def test_builds_connected_components_and_step_shell_correspondence(self) -> None:
        report = self._build()

        component_tags = [item["surface_tags"] for item in report["shell_components"]]
        self.assertEqual(component_tags, [[1, 2], [3], [4, 5]])
        correspondence = report["step_shell_correspondence"]
        self.assertEqual(len(correspondence), 3)
        self.assertTrue(all(item["status"] == "CONFIRMED" for item in correspondence))
        self.assertTrue(all(item["cfd_role_inference"] == "none" for item in correspondence))
        self.assertEqual(len(report["contact_candidates"]), 1)
        self.assertEqual(report["contact_candidates"][0]["status"], "CANDIDATE")
        self.assertTrue((self.output / "marker_review.json").is_file())
        self.assertTrue((self.output / "marker_review.md").is_file())
        self.assertIn("marker_review.json", report["outputs"])
        self.assertEqual(_sha(self.output / "marker_review.json"), report["outputs"]["marker_review.json"]["sha256"])

    def test_missing_measurement_and_unnamed_surfaces_are_not_confirmed(self) -> None:
        report = self._build()

        measurement = report["roles"]["turning_section_outlet"]
        self.assertEqual(measurement["status"], "MISSING")
        self.assertIn("no surface has two", measurement["basis"])
        self.assertTrue(
            all(item["status"] != "CONFIRMED" for item in report["roles"].values())
        )
        self.assertFalse(report["marker_configuration_ready"])
        self.assertTrue(report["uncovered_exposed_shells"])

    def test_axial_planes_remain_ambiguous_and_never_write_markers(self) -> None:
        report = self._build()

        self.assertGreaterEqual(len(report["axial_plane_candidates"]), 2)
        self.assertEqual(report["roles"]["rear_outlet_1"]["status"], "AMBIGUOUS")
        self.assertEqual(report["roles"]["rear_outlet_2"]["status"], "AMBIGUOUS")
        self.assertEqual(report["roles"]["rear_outlet_1"]["surface_tags"], report["roles"]["rear_outlet_2"]["surface_tags"])
        self.assertFalse((self.output / "markers.toml").exists())
        self.assertFalse((self.root / "config" / "markers.toml").exists())

    def test_geometrically_axial_bspline_is_not_discarded_by_type_name(self) -> None:
        report = self._build()

        by_tag = {
            item["surface_tag"]: item for item in report["axial_plane_candidates"]
        }
        self.assertIn(3, by_tag)
        self.assertEqual(by_tag[3]["entity_type"], "Plane")
        self.assertIn(5, by_tag)
        self.assertEqual(by_tag[5]["entity_type"], "Plane")

        # Make the thin axial surface a B-spline: the geometric evidence, not
        # the OCC label, must keep it in the review set.
        payload = json.loads(self.types.read_text(encoding="utf-8"))
        for surface in payload["surfaces"]:
            if surface["entity_tag"] == 3:
                surface["entity_type"] = "BSpline surface"
        self.types.write_text(json.dumps(payload), encoding="utf-8")
        second_output = self.allowed / "marker_review_bspline"
        report = self._build(output_directory=second_output)
        by_tag = {
            item["surface_tag"]: item for item in report["axial_plane_candidates"]
        }
        self.assertIn(3, by_tag)
        self.assertEqual(by_tag[3]["entity_type"], "BSpline surface")

    def test_zero_distance_edge_intersection_is_not_a_contact_candidate(self) -> None:
        payload = json.loads(self.types.read_text(encoding="utf-8"))
        payload["distance_evidence"]["surface_pairs"].append(
            {
                "entity_a": [2, 2],
                "entity_b": [2, 5],
                "distance_m": 0.0,
                "prescreen": {
                    "surface_a": 2,
                    "surface_b": 5,
                    "relative_area_difference": 0.0,
                    "centroid_distance_m": 1.25,
                    "bbox_gap_m": 0.0,
                },
            }
        )
        self.types.write_text(json.dumps(payload), encoding="utf-8")

        report = self._build()

        self.assertEqual(
            [(item["surface_a"], item["surface_b"]) for item in report["contact_candidates"]],
            [(1, 4)],
        )

    def test_unique_exact_persistent_name_can_confirm_only_that_role(self) -> None:
        self._write_catalog(names={1: "front_conical_surface"})
        self._write_types()

        report = self._build()

        self.assertEqual(report["roles"]["front_conical_surface"]["status"], "CONFIRMED")
        self.assertEqual(report["roles"]["front_conical_surface"]["surface_tags"], [1])
        self.assertFalse(report["marker_configuration_ready"])

    def test_outside_path_and_failed_upstream_status_fail_without_output(self) -> None:
        outside = self.root / "outside.csv"
        outside.write_bytes(self.catalog.read_bytes())
        with self.assertRaisesRegex(MarkerReviewError, "outside"):
            self._build(surface_catalog_path=outside)
        self.assertFalse(self.output.exists())

        self._write_types(status="FAIL")
        with self.assertRaisesRegex(MarkerReviewError, "not PASS"):
            self._build()
        self.assertFalse(self.output.exists())

    def test_render_dependency_hash_mismatch_fails_without_output(self) -> None:
        self._write_render(bad_hash=True)
        with self.assertRaisesRegex(MarkerReviewError, "SHA256"):
            self._build()
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
