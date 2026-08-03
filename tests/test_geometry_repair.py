from __future__ import annotations

from pathlib import Path
import unittest

from cfdpipe.geometry_repair import (
    GeometryRepairError,
    _assess_nonconformal_step_roundtrip,
    _assign_interface_lineages,
    _resolve_input_volumes,
    _run_owned_session,
    _validate_shared_topology,
)


def _surface(tag: int, adjacent: list[int]) -> dict[str, object]:
    return {
        "entity_tag": tag,
        "entity_type": "Plane",
        "area_m2": 1.0,
        "centroid_m": [float(tag), 0.0, 0.0],
        "bounding_box_m": [float(tag), 0.0, 0.0, float(tag), 1.0, 1.0],
        "adjacent_volumes": adjacent,
    }


def _shared_catalog(*, equal_orientation: bool = False) -> dict[str, object]:
    external = [_surface(tag, [10 if tag <= 28 else 20]) for tag in range(1, 56)]
    shared = [_surface(56, [10, 20]), _surface(57, [10, 20])]
    first = [
        {"surface_tag": tag, "orientation": 1}
        for tag in list(range(1, 29)) + [56, 57]
    ]
    second = [
        {"surface_tag": tag, "orientation": 1}
        for tag in range(29, 56)
    ] + [
        {
            "surface_tag": tag,
            "orientation": 1 if equal_orientation else -1,
        }
        for tag in (56, 57)
    ]
    return {
        "surfaces": external + shared,
        "volumes": [
            {"entity_tag": 10, "oriented_boundary_surfaces": first},
            {"entity_tag": 20, "oriented_boundary_surfaces": second},
        ],
    }


class _Model:
    def add(self, name: str) -> None:
        self.name = name


class _Session:
    __version__ = "test"

    def __init__(self) -> None:
        self.model = _Model()
        self.initialized = False
        self.finalized = False

    def isInitialized(self) -> int:
        return int(self.initialized and not self.finalized)

    def initialize(self) -> None:
        self.initialized = True

    def finalize(self) -> None:
        self.finalized = True


class GeometryRepairTests(unittest.TestCase):
    def test_shared_topology_requires_two_oppositely_oriented_interfaces(self) -> None:
        result = _validate_shared_topology(_shared_catalog())
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["external_surface_count"], 55)
        self.assertEqual(result["shared_patch_count"], 2)

    def test_equal_shared_orientation_fails_closed(self) -> None:
        with self.assertRaisesRegex(GeometryRepairError, "equal orientation"):
            _validate_shared_topology(_shared_catalog(equal_orientation=True))

    def test_interface_lineage_accepts_measured_occ_property_difference(self) -> None:
        first = {
            **_surface(1, [1]),
            "area_m2": 14.809181844508327,
            "centroid_m": [3.0283120030864317, 0.0, -0.6162883256396205],
            "bounding_box_m": [-0.1711972163, -1.2488864886, -2.4977728772, 5.2000001, 1.2488864886, 0.0],
        }
        duplicate = {
            **first,
            "entity_tag": 54,
            "area_m2": 14.809200432442363,
            "centroid_m": [3.0283120030864312, 0.0, -0.6163017617840341],
        }
        descendant = {**first, "entity_tag": 7, "adjacent_volumes": [1, 2]}
        result = _assign_interface_lineages(
            {"surfaces": [first, duplicate]},
            {"surfaces": [descendant]},
            [[1, 54]],
            [7],
        )
        self.assertEqual(result[0]["descendant_surface_tags_audit"], [7])
        self.assertTrue(result[0]["all_descendants_have_two_adjacent_volumes"])

    def test_volume_resolution_uses_geometry_not_old_entity_tag(self) -> None:
        current = {
            "volumes": [
                {
                    "entity_tag": 91,
                    "volume_m3": 2.0,
                    "centroid_m": [2.0, 0.0, 0.0],
                    "bounding_box_m": [1.0, 0.0, 0.0, 3.0, 1.0, 1.0],
                    "boundary_surfaces": [1, 2],
                },
                {
                    "entity_tag": 37,
                    "volume_m3": 1.0,
                    "centroid_m": [0.5, 0.0, 0.0],
                    "bounding_box_m": [0.0, 0.0, 0.0, 1.0, 1.0, 1.0],
                    "boundary_surfaces": [3],
                },
            ]
        }
        references = [
            {
                "solid_id": 1,
                "geometry": {
                    "face_count": 1,
                    "volume_m3": 1.0,
                    "centroid_m": [0.5, 0.0, 0.0],
                    "bounding_box_m": [0.0, 0.0, 0.0, 1.0, 1.0, 1.0],
                },
            },
            {
                "solid_id": 2,
                "geometry": {
                    "face_count": 2,
                    "volume_m3": 2.0,
                    "centroid_m": [2.0, 0.0, 0.0],
                    "bounding_box_m": [1.0, 0.0, 0.0, 3.0, 1.0, 1.0],
                },
            },
        ]
        result = _resolve_input_volumes(current, references)
        self.assertEqual([item["current_volume_tag_audit"] for item in result], [37, 91])

    def test_owned_session_finalizes_after_action_exception(self) -> None:
        session = _Session()

        def fail() -> None:
            raise RuntimeError("primary failure")

        with self.assertRaisesRegex(RuntimeError, "primary failure"):
            _run_owned_session(session, "test", fail, [])
        self.assertTrue(session.finalized)

    def test_step_roundtrip_duplication_is_proven_but_not_pipeline_eligible(self) -> None:
        volumes = [
            {
                "entity_tag": 1,
                "volume_m3": 1.0,
                "centroid_m": [0.0, 0.0, 0.0],
                "bounding_box_m": [0.0, 0.0, 0.0, 1.0, 1.0, 1.0],
            },
            {
                "entity_tag": 2,
                "volume_m3": 2.0,
                "centroid_m": [2.0, 0.0, 0.0],
                "bounding_box_m": [1.0, 0.0, 0.0, 3.0, 1.0, 1.0],
            },
        ]
        external = _surface(1, [1])
        shared = _surface(2, [1, 2])
        current_volumes = [
            {**volumes[0], "entity_tag": 101},
            {**volumes[1], "entity_tag": 202},
        ]
        current_surfaces = [
            {**external, "entity_tag": 10, "adjacent_volumes": [101]},
            {**shared, "entity_tag": 20, "adjacent_volumes": [101]},
            {**shared, "entity_tag": 21, "adjacent_volumes": [202]},
        ]

        result = _assess_nonconformal_step_roundtrip(
            {"volumes": volumes, "surfaces": [external, shared]},
            {"volumes": current_volumes, "surfaces": current_surfaces},
            strict_match_error=GeometryRepairError("no shared topology"),
        )

        self.assertEqual(result["status"], "NONCONFORMAL_AFTER_STEP_ROUNDTRIP")
        self.assertFalse(result["pipeline_eligible"])
        self.assertEqual(result["surface_mapping_audit"]["2"], [20, 21])
        self.assertEqual(result["topology"]["shared_surface_count"], 0)

    def test_repair_source_contains_no_forbidden_geometry_or_gui_calls(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "cfdpipe"
            / "geometry_repair.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            ".removeAllDuplicates(",
            ".healShapes(",
            ".mesh.generate(",
            ".addPhysicalGroup(",
            ".fltk.run(",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
