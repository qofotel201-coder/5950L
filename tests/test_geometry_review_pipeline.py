"""Pure unit tests for fail-closed geometry-evidence orchestration helpers."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from cfdpipe.geometry_review_pipeline import (
    GeometryEvidenceReviewError,
    _diagnostic_groups,
    _strong_contact_pairs,
    _surface_components,
    run_geometry_evidence_review,
)


class GeometryReviewPipelineHelperTests(unittest.TestCase):
    def test_surface_components_use_shared_boundary_curves(self) -> None:
        surfaces = [
            {"entity_tag": 1, "boundary_curves": [10, 11]},
            {"entity_tag": 2, "boundary_curves": [11, 12]},
            {"entity_tag": 3, "boundary_curves": [30]},
            {"entity_tag": 4, "boundary_curves": []},
        ]
        volumes = [
            {"entity_tag": 101, "boundary_surfaces": [4, 3, 2, 1]},
        ]

        components = _surface_components(surfaces, volumes)

        self.assertEqual(
            components,
            [
                {
                    "volume_entity_tag": 101,
                    "component_index": 1,
                    "surface_tags": [3],
                    "face_count": 1,
                },
                {
                    "volume_entity_tag": 101,
                    "component_index": 2,
                    "surface_tags": [4],
                    "face_count": 1,
                },
                {
                    "volume_entity_tag": 101,
                    "component_index": 3,
                    "surface_tags": [1, 2],
                    "face_count": 2,
                },
            ],
        )

    def test_strong_contact_pairs_exclude_edge_only_and_invalid_evidence(self) -> None:
        manifest = {
            "distance_evidence": {
                "surface_pairs": [
                    {
                        "distance_m": 0.0,
                        "prescreen": {
                            "surface_a": 1,
                            "surface_b": 54,
                            "relative_area_difference": 1.3e-6,
                            "centroid_distance_m": 1.4e-5,
                            "bbox_gap_m": 0.0,
                        },
                    },
                    {
                        # The faces touch at an edge, but their centroids prove
                        # that they are not a full near-coincident pair.
                        "distance_m": 0.0,
                        "prescreen": {
                            "surface_a": 1,
                            "surface_b": 58,
                            "relative_area_difference": 1.3e-6,
                            "centroid_distance_m": 1.23,
                            "bbox_gap_m": 0.0,
                        },
                    },
                    {
                        "distance_m": 1.0e-4,
                        "prescreen": {
                            "surface_a": 2,
                            "surface_b": 55,
                            "relative_area_difference": 0.0,
                            "centroid_distance_m": 0.0,
                            "bbox_gap_m": 0.0,
                        },
                    },
                    {
                        "distance_m": float("nan"),
                        "prescreen": {
                            "surface_a": 3,
                            "surface_b": 56,
                            "relative_area_difference": 0.0,
                            "centroid_distance_m": 0.0,
                            "bbox_gap_m": 0.0,
                        },
                    },
                    {"distance_m": 0.0},
                ]
            }
        }

        contacts = _strong_contact_pairs(manifest)

        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]["surface_tags"], [1, 54])
        self.assertEqual(
            contacts[0]["classification"],
            "strong_near_coincident_contact_candidate",
        )
        self.assertFalse(contacts[0]["full_surface_coincidence_proven"])

    def test_diagnostic_groups_are_geometric_candidates_not_cfd_markers(self) -> None:
        surfaces = [
            {"entity_tag": 1, "area_m2": 100.0, "bounds_m": [0, 0, 0, 1, 1, 1]},
            {
                "entity_tag": 2,
                "area_m2": 5.0,
                "bounds_m": [2.0, 0, 0, 2.000001, 1, 1],
            },
            {"entity_tag": 3, "area_m2": 10.0, "bounds_m": [0, 0, 0, 1, 1, 1]},
            {"entity_tag": 4, "area_m2": 1.0, "bounds_m": [0, 0, 0, 1, 1, 1]},
        ]
        volumes = [{"entity_tag": 10, "boundary_surfaces": [1, 2, 3, 4]}]
        components = [
            {
                "volume_entity_tag": 10,
                "component_index": 1,
                "surface_tags": [1, 2],
                "face_count": 2,
            },
            {
                "volume_entity_tag": 10,
                "component_index": 2,
                "surface_tags": [3, 4],
                "face_count": 2,
            },
        ]
        contacts = [{"surface_tags": [1, 3]}]

        result = _diagnostic_groups(surfaces, volumes, components, contacts)
        groups = result["groups"]

        self.assertEqual(groups["volume_10_all"]["surface_tags"], [1, 2, 3, 4])
        self.assertEqual(groups["contact_candidate_1"]["surface_tags"], [1, 3])
        self.assertEqual(groups["axial_planar_candidates"]["surface_tags"], [2])
        self.assertEqual(
            groups["assembly_exposed_surface_candidates"]["surface_tags"],
            [2, 4],
        )
        self.assertEqual(groups["surface_2"]["surface_tags"], [2])
        forbidden = ("farfield", "wall", "outlet", "fluid")
        self.assertFalse(
            any(token in name.lower() for name in groups for token in forbidden)
        )


class GeometryReviewPipelineValidationTests(unittest.TestCase):
    def test_invalid_step_suffix_fails_before_any_tool_or_parser_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.txt"
            project = root / "project.toml"
            output = root / "review"
            source.write_text("not a STEP file\n", encoding="utf-8")
            project.write_text("[project]\nname='test'\n", encoding="utf-8")
            toolchain = mock.Mock()

            with (
                mock.patch(
                    "cfdpipe.geometry_review_pipeline.parse_step_topology"
                ) as parse_topology,
                mock.patch(
                    "cfdpipe.geometry_review_pipeline.GmshBridge"
                ) as gmsh_bridge,
                mock.patch(
                    "cfdpipe.geometry_review_pipeline.build_diagnostic_surface_mesh"
                ) as build_mesh,
                mock.patch(
                    "cfdpipe.geometry_review_pipeline.run_geometry_review"
                ) as render,
            ):
                with self.assertRaises(GeometryEvidenceReviewError):
                    run_geometry_evidence_review(
                        step_path=source,
                        project_path=project,
                        output_directory=output,
                        allowed_output_root=output,
                        toolchain=toolchain,
                        live_output=False,
                    )

            parse_topology.assert_not_called()
            gmsh_bridge.assert_not_called()
            build_mesh.assert_not_called()
            render.assert_not_called()
            toolchain.resolve.assert_not_called()
            manifest_path = output / "geometry_evidence_manifest.json"
            self.assertTrue(manifest_path.is_file())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "FAIL")
            self.assertEqual(manifest["classification_status"], "FAILED")
            self.assertFalse(manifest["next_stage_allowed"])
            self.assertIn("invalid STEP source", manifest["error"]["message"])
            self.assertEqual(set(output.iterdir()), {manifest_path})


if __name__ == "__main__":
    unittest.main()
