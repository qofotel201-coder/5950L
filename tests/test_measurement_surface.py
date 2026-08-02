"""Pure-algorithm and mock-only tests for measurement-surface discovery."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from cfdpipe.measurement_surface import (
    MeasurementSurfaceError,
    classify_branch_merge_candidates,
    enumerate_coordinate_plane_loops,
    inspect_measurement_surfaces,
)


SOURCE_HASH = "a" * 64


def _circle_curve(
    x: float,
    y: float,
    z: float,
    radius: float,
    runtime_tag: int,
    adjacent_surfaces: tuple[int, int],
    *,
    sample_count: int = 65,
) -> dict[str, object]:
    samples = [
        (
            x,
            y + radius * math.cos(2.0 * math.pi * index / (sample_count - 1)),
            z + radius * math.sin(2.0 * math.pi * index / (sample_count - 1)),
        )
        for index in range(sample_count)
    ]
    return {
        "runtime_curve_tag": runtime_tag,
        "length_m": 2.0 * math.pi * radius,
        "samples_m": samples,
        "adjacent_surface_tags": list(adjacent_surfaces),
    }


def _surface_pair(
    x: float,
    y: float,
    z: float,
    radius: float,
    upstream_tag: int,
    downstream_tag: int,
) -> dict[int, list[float]]:
    return {
        upstream_tag: [
            x - 0.5,
            y - radius,
            z - radius,
            x,
            y + radius,
            z + radius,
        ],
        downstream_tag: [
            x,
            y - radius,
            z - radius,
            x + 0.5,
            y + radius,
            z + radius,
        ],
    }


class MeasurementSurfaceAlgorithmTests(unittest.TestCase):
    def test_two_branches_then_first_single_section_is_geometry_only_candidate(self) -> None:
        curves = [
            _circle_curve(1.0, -0.25, 0.0, 0.08, 10, (101, 102)),
            _circle_curve(1.0, 0.25, 0.0, 0.08, 20, (103, 104)),
            # A geometrically closed but unoccupied reference loop must not
            # interrupt the fluid-section sequence.
            _circle_curve(1.5, 0.0, 0.0, 0.40, 30, (105, 106)),
            _circle_curve(2.0, 0.0, 0.0, 0.10, 40, (107, 108)),
            _circle_curve(3.0, 0.0, 0.0, 0.12, 50, (109, 110)),
        ]
        bounds: dict[int, list[float]] = {}
        for values in (
            _surface_pair(1.0, -0.25, 0.0, 0.08, 101, 102),
            _surface_pair(1.0, 0.25, 0.0, 0.08, 103, 104),
            _surface_pair(1.5, 0.0, 0.0, 0.40, 105, 106),
            _surface_pair(2.0, 0.0, 0.0, 0.10, 107, 108),
            _surface_pair(3.0, 0.0, 0.0, 0.12, 109, 110),
        ):
            bounds.update(values)

        def membership(point):
            return [] if math.isclose(point[0], 1.5, abs_tol=1.0e-9) else [901]

        loops = enumerate_coordinate_plane_loops(
            curves,
            surface_bounds=bounds,
            source_sha256=SOURCE_HASH,
            volume_membership=membership,
        )
        classification = classify_branch_merge_candidates(loops)

        self.assertEqual(classification["geometry_status"], "CONFIRMED_GEOMETRY")
        self.assertEqual(classification["role_status"], "CANDIDATE_ROLE")
        self.assertFalse(classification["business_role_asserted"])
        self.assertEqual(len(classification["transition_candidates"]), 1)
        selected = next(
            loop
            for loop in loops
            if loop["fingerprint"]["sha256"]
            == classification["selected_fingerprint_sha256"]
        )
        # The test intentionally uses x=2.0: selection comes from topology,
        # not a project-specific coordinate literal.
        self.assertAlmostEqual(selected["plane_coordinate_m"], 2.0)
        self.assertEqual(selected["geometry_status"], "CONFIRMED_GEOMETRY")
        self.assertEqual(selected["role_status"], "CANDIDATE_ROLE")
        self.assertEqual(selected["normal_unit"], [1.0, 0.0, 0.0])
        self.assertAlmostEqual(selected["centroid_m"][0], 2.0)
        self.assertAlmostEqual(selected["perimeter_m"], 2.0 * math.pi * 0.10)
        self.assertAlmostEqual(
            selected["area_m2"], math.pi * 0.10**2, delta=1.0e-4
        )
        self.assertGreater(selected["circularity"], 0.99)
        self.assertLessEqual(selected["planarity_residual_m"], 1.0e-12)
        self.assertEqual(
            selected["adjacency_evidence"]["upstream_surface_count"], 1
        )
        self.assertEqual(
            selected["adjacency_evidence"]["downstream_surface_count"], 1
        )
        self.assertEqual(
            selected["topology_evidence"]["upstream_connected_loop_count"], 2
        )
        reference_loop = next(
            loop for loop in loops if math.isclose(loop["plane_coordinate_m"], 1.5)
        )
        self.assertFalse(reference_loop["occupancy_evidence"]["qualified"])

    def test_micrometre_bspline_waviness_does_not_drop_one_branch(self) -> None:
        first = _circle_curve(1.0, -0.25, 0.0, 0.08, 10, (101, 102))
        second = _circle_curve(1.0, 0.25, 0.0, 0.08, 20, (103, 104))
        # This mirrors the imported STEP evidence: an intended x-station
        # B-spline can vary by about 1.4 micrometres peak-to-peak while its
        # endpoints still lie on the same section plane.
        samples = second["samples_m"]
        second["samples_m"] = [
            (
                point[0]
                + 0.7e-6
                * math.sin(2.0 * math.pi * index / (len(samples) - 1)),
                point[1],
                point[2],
            )
            for index, point in enumerate(samples)
        ]
        downstream = _circle_curve(2.0, 0.0, 0.0, 0.10, 30, (105, 106))
        bounds: dict[int, list[float]] = {}
        for values in (
            _surface_pair(1.0, -0.25, 0.0, 0.08, 101, 102),
            _surface_pair(1.0, 0.25, 0.0, 0.08, 103, 104),
            _surface_pair(2.0, 0.0, 0.0, 0.10, 105, 106),
        ):
            bounds.update(values)

        loops = enumerate_coordinate_plane_loops(
            [first, second, downstream],
            surface_bounds=bounds,
            source_sha256=SOURCE_HASH,
            volume_membership=lambda point: [901],
        )
        branch_loops = [
            loop for loop in loops if math.isclose(loop["plane_coordinate_m"], 1.0)
        ]
        classification = classify_branch_merge_candidates(loops)

        self.assertEqual(len(branch_loops), 2)
        self.assertGreater(
            max(loop["planarity_residual_m"] for loop in branch_loops), 1.0e-8
        )
        self.assertLessEqual(
            max(loop["planarity_residual_m"] for loop in branch_loops), 2.0e-6
        )
        self.assertEqual(classification["geometry_status"], "CONFIRMED_GEOMETRY")
        selected = next(
            loop
            for loop in loops
            if loop["fingerprint"]["sha256"]
            == classification["selected_fingerprint_sha256"]
        )
        self.assertAlmostEqual(selected["plane_coordinate_m"], 2.0)

    def test_waviness_above_approximate_plane_tolerance_is_rejected(self) -> None:
        curve = _circle_curve(1.0, 0.0, 0.0, 0.08, 10, (101, 102))
        samples = curve["samples_m"]
        curve["samples_m"] = [
            (
                point[0]
                + 2.0e-6
                * math.sin(2.0 * math.pi * index / (len(samples) - 1)),
                point[1],
                point[2],
            )
            for index, point in enumerate(samples)
        ]

        loops = enumerate_coordinate_plane_loops(
            [curve],
            surface_bounds=_surface_pair(1.0, 0.0, 0.0, 0.08, 101, 102),
            source_sha256=SOURCE_HASH,
            volume_membership=lambda point: [901],
        )

        self.assertEqual(loops, [])

    def test_fingerprint_is_stable_when_runtime_tags_are_reordered(self) -> None:
        first = _circle_curve(2.5, 0.0, 0.0, 0.2, 7, (11, 12))
        second = _circle_curve(2.5, 0.0, 0.0, 0.2, 700, (91, 92))
        first_loop = enumerate_coordinate_plane_loops(
            [first],
            surface_bounds=_surface_pair(2.5, 0.0, 0.0, 0.2, 11, 12),
            source_sha256=SOURCE_HASH,
            volume_membership=lambda point: [301],
        )[0]
        second_loop = enumerate_coordinate_plane_loops(
            [second],
            surface_bounds=_surface_pair(2.5, 0.0, 0.0, 0.2, 91, 92),
            source_sha256=SOURCE_HASH,
            volume_membership=lambda point: [999],
        )[0]

        self.assertNotEqual(first_loop["runtime_curve_tags"], second_loop["runtime_curve_tags"])
        self.assertEqual(
            first_loop["fingerprint"]["sha256"],
            second_loop["fingerprint"]["sha256"],
        )
        serialized = json.dumps(first_loop["fingerprint"]["payload"], sort_keys=True)
        self.assertNotIn("runtime_", serialized)
        self.assertNotIn("surface_tags", serialized)
        self.assertNotIn("volume_tag", serialized)

    def test_open_and_nonplanar_curves_are_not_fabricated_as_loops(self) -> None:
        open_curve = {
            "runtime_curve_tag": 1,
            "length_m": 1.0,
            "samples_m": [(0.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
            "adjacent_surface_tags": [],
        }
        nonplanar_closed = {
            "runtime_curve_tag": 2,
            "length_m": 4.0,
            "samples_m": [
                (0.0, 0.0, 0.0),
                (1.0, 0.0, 1.0),
                (1.0, 1.0, 0.0),
                (0.0, 1.0, 1.0),
                (0.0, 0.0, 0.0),
            ],
            "adjacent_surface_tags": [],
        }
        loops = enumerate_coordinate_plane_loops(
            [open_curve, nonplanar_closed],
            surface_bounds={},
            source_sha256=SOURCE_HASH,
            volume_membership=lambda point: [1],
        )
        self.assertEqual(loops, [])

    def test_multiple_two_to_one_transitions_are_not_declared_unique(self) -> None:
        def eligible_loop(station: float, suffix: str) -> dict[str, object]:
            return {
                "plane_axis": "x",
                "plane_coordinate_m": station,
                "occupancy_evidence": {"qualified": True},
                "adjacency_evidence": {
                    "upstream_surface_count": 1,
                    "downstream_surface_count": 1,
                    "spanning_surface_count": 0,
                },
                "fingerprint": {"sha256": (suffix * 64)[:64]},
                "geometry_status": "DISCOVERED_GEOMETRY",
                "role_status": "UNASSIGNED",
                "topology_evidence": None,
            }

        loops = [
            eligible_loop(1.0, "a"),
            eligible_loop(1.0, "b"),
            eligible_loop(2.0, "c"),
            eligible_loop(3.0, "d"),
            eligible_loop(3.0, "e"),
            eligible_loop(4.0, "f"),
        ]
        result = classify_branch_merge_candidates(loops)  # type: ignore[arg-type]
        self.assertEqual(result["geometry_status"], "AMBIGUOUS_GEOMETRY")
        self.assertEqual(result["role_status"], "UNRESOLVED_ROLE")
        self.assertIsNone(result["selected_fingerprint_sha256"])
        self.assertEqual(len(result["transition_candidates"]), 2)
        self.assertTrue(
            all(loop["geometry_status"] == "DISCOVERED_GEOMETRY" for loop in loops)
        )

    def test_nonfinite_samples_fail_closed(self) -> None:
        curve = _circle_curve(1.0, 0.0, 0.0, 0.1, 1, (2, 3))
        curve["samples_m"][2] = (1.0, math.nan, 0.0)  # type: ignore[index]
        with self.assertRaisesRegex(MeasurementSurfaceError, "NaN or Inf"):
            enumerate_coordinate_plane_loops(
                [curve],
                surface_bounds={},
                source_sha256=SOURCE_HASH,
            )


class _FakeOcc:
    def __init__(self, owner: "_FakeGmsh") -> None:
        self.owner = owner
        self.importShapes = mock.Mock(side_effect=self._import_shapes)
        self.synchronize = mock.Mock(side_effect=self._synchronize)
        self.getMass = mock.Mock(side_effect=self._get_mass)
        self.getBoundingBox = mock.Mock(side_effect=self._get_bounds)
        self.fragment = mock.Mock()
        self.addWire = mock.Mock()
        self.addPlaneSurface = mock.Mock()

    def _import_shapes(self, path: str):
        self.owner.timeline.append(("importShapes", path))
        return [(3, 501)]

    def _synchronize(self) -> None:
        self.owner.timeline.append(("synchronize",))

    def _get_mass(self, dimension: int, tag: int) -> float:
        if dimension != 1:
            raise AssertionError("only curve mass is expected")
        return 2.0 * math.pi * self.owner.curves[tag][3]

    def _get_bounds(self, dimension: int, tag: int):
        if dimension != 2:
            raise AssertionError("only surface bounds are expected")
        return self.owner.surface_bounds[tag]


class _FakeModel:
    def __init__(self, owner: "_FakeGmsh") -> None:
        self.owner = owner
        self.occ = _FakeOcc(owner)
        self.mesh = SimpleNamespace(generate=mock.Mock())
        self.add = mock.Mock(side_effect=self._add)
        self.getEntities = mock.Mock(side_effect=self._get_entities)
        self.getParametrizationBounds = mock.Mock(return_value=([0.0], [2.0 * math.pi]))
        self.getValue = mock.Mock(side_effect=self._get_value)
        self.getAdjacencies = mock.Mock(side_effect=self._get_adjacencies)
        self.isInside = mock.Mock(return_value=1)
        self.addPhysicalGroup = mock.Mock()
        self.setPhysicalName = mock.Mock()

    def _add(self, name: str) -> None:
        self.owner.timeline.append(("model.add", name))

    def _get_entities(self, dimension: int):
        if dimension == 1:
            return [(1, tag) for tag in sorted(self.owner.curves)]
        if dimension == 2:
            return [(2, tag) for tag in sorted(self.owner.surface_bounds)]
        if dimension == 3:
            return [(3, 501)]
        return []

    def _get_value(self, dimension: int, tag: int, parameters):
        if dimension != 1:
            raise AssertionError("only curve samples are expected")
        x, y, z, radius, _ = self.owner.curves[tag]
        values: list[float] = []
        for parameter in parameters:
            values.extend(
                [
                    x,
                    y + radius * math.cos(parameter),
                    z + radius * math.sin(parameter),
                ]
            )
        return values

    def _get_adjacencies(self, dimension: int, tag: int):
        if dimension != 1:
            raise AssertionError("only curve adjacency is expected")
        return list(self.owner.curves[tag][4]), []


class _FakeOption:
    def __init__(self, owner: "_FakeGmsh") -> None:
        self.owner = owner
        self.setString = mock.Mock(side_effect=self._set_string)

    def _set_string(self, name: str, value: str) -> None:
        self.owner.timeline.append(("setString", name, value))


class _FakeGmsh:
    __version__ = "4.15.2-test"
    __file__ = __file__

    def __init__(self) -> None:
        self.timeline: list[tuple[object, ...]] = []
        self.curves = {
            10: (1.0, -0.25, 0.0, 0.08, (101, 102)),
            20: (1.0, 0.25, 0.0, 0.08, (103, 104)),
            30: (2.0, 0.0, 0.0, 0.10, (105, 106)),
        }
        self.surface_bounds: dict[int, list[float]] = {}
        for values in (
            _surface_pair(1.0, -0.25, 0.0, 0.08, 101, 102),
            _surface_pair(1.0, 0.25, 0.0, 0.08, 103, 104),
            _surface_pair(2.0, 0.0, 0.0, 0.10, 105, 106),
        ):
            self.surface_bounds.update(values)
        self.model = _FakeModel(self)
        self.option = _FakeOption(self)
        self.initialize = mock.Mock(side_effect=lambda: self.timeline.append(("initialize",)))
        self.finalize = mock.Mock(side_effect=lambda: self.timeline.append(("finalize",)))
        self.write = mock.Mock()
        self.fltk = SimpleNamespace(run=mock.Mock())


class MeasurementSurfaceGmshLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.step = self.root / "input.step"
        self.step.write_text("mock STEP\n", encoding="utf-8")
        self._make_read_only(self.step)

    def tearDown(self) -> None:
        if self.step.exists():
            self.step.chmod(stat.S_IRUSR | stat.S_IWUSR)
        self.temporary.cleanup()

    @staticmethod
    def _make_read_only(path: Path) -> None:
        if os.name == "nt":
            path.chmod(stat.S_IREAD)
        else:
            path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

    def _inspect(self, gmsh: _FakeGmsh, *, source: Path | None = None, **kwargs):
        with mock.patch.dict(sys.modules, {"gmsh": gmsh}):
            return inspect_measurement_surfaces(
                self.step if source is None else source,
                curve_sample_count=17,
                **kwargs,
            )

    def test_read_only_occ_lifecycle_and_prohibited_calls(self) -> None:
        gmsh = _FakeGmsh()

        report = self._inspect(gmsh)

        self.assertEqual(report["status"], "PASS")
        self.assertTrue(report["source_unchanged"])
        self.assertEqual(
            report["source_sha256_before"], report["source_sha256_after"]
        )
        self.assertTrue(report["source_read_only_before"])
        self.assertTrue(report["source_read_only_after"])
        self.assertEqual(report["target_geometry_unit"], "M")
        self.assertEqual(
            report["discovery_parameters"]["plane_tolerance_m"], 2.0e-6
        )
        self.assertEqual(
            report["classification"]["geometry_status"], "CONFIRMED_GEOMETRY"
        )
        self.assertEqual(report["classification"]["role_status"], "CANDIDATE_ROLE")
        self.assertFalse(report["classification"]["business_role_asserted"])

        gmsh.initialize.assert_called_once_with()
        gmsh.finalize.assert_called_once_with()
        gmsh.option.setString.assert_called_once_with("Geometry.OCCTargetUnit", "M")
        gmsh.model.occ.importShapes.assert_called_once_with(str(self.step.resolve()))
        gmsh.model.occ.synchronize.assert_called_once_with()
        event_names = [entry[0] for entry in gmsh.timeline]
        self.assertLess(event_names.index("initialize"), event_names.index("setString"))
        self.assertLess(event_names.index("setString"), event_names.index("importShapes"))
        self.assertLess(event_names.index("importShapes"), event_names.index("synchronize"))
        self.assertLess(event_names.index("synchronize"), event_names.index("finalize"))

        gmsh.model.mesh.generate.assert_not_called()
        gmsh.write.assert_not_called()
        gmsh.model.addPhysicalGroup.assert_not_called()
        gmsh.model.setPhysicalName.assert_not_called()
        gmsh.model.occ.fragment.assert_not_called()
        gmsh.model.occ.addWire.assert_not_called()
        gmsh.model.occ.addPlaneSurface.assert_not_called()
        gmsh.fltk.run.assert_not_called()
        self.assertEqual(
            report["operations"],
            {
                "mesh_generated": False,
                "file_written_by_gmsh": False,
                "physical_groups_created": False,
                "source_geometry_modified": False,
            },
        )

    def test_read_only_brep_uses_the_same_headless_occ_lifecycle(self) -> None:
        brep = self.root / "derived.brep"
        brep.write_text("mock BREP\n", encoding="utf-8")
        self._make_read_only(brep)
        self.addCleanup(
            lambda: brep.exists()
            and brep.chmod(stat.S_IRUSR | stat.S_IWUSR)
        )
        gmsh = _FakeGmsh()

        report = self._inspect(gmsh, source=brep)

        self.assertEqual(report["source_cad_format"], "brep")
        self.assertEqual(report["source_cad_path"], str(brep.resolve()))
        self.assertEqual(report["derived_cad_sha256"], report["source_sha256_before"])
        gmsh.model.occ.importShapes.assert_called_once_with(str(brep.resolve()))
        gmsh.finalize.assert_called_once_with()

    def test_query_exception_still_finalizes(self) -> None:
        gmsh = _FakeGmsh()
        gmsh.model.getValue.side_effect = RuntimeError("sampling sentinel")

        with self.assertRaisesRegex(RuntimeError, "sampling sentinel"):
            self._inspect(gmsh)

        gmsh.finalize.assert_called_once_with()
        gmsh.model.mesh.generate.assert_not_called()
        gmsh.write.assert_not_called()

    def test_explicit_json_output_is_new_and_parseable(self) -> None:
        gmsh = _FakeGmsh()
        output = self.root / "measurement_surface_candidates.json"

        report = self._inspect(gmsh, output_path=output)

        self.assertTrue(output.is_file())
        self.assertEqual(
            json.loads(output.read_text(encoding="utf-8")), report
        )
        with self.assertRaisesRegex(MeasurementSurfaceError, "overwrite"):
            self._inspect(_FakeGmsh(), output_path=output)

    def test_changed_read_only_state_is_detected_after_finalize(self) -> None:
        gmsh = _FakeGmsh()

        def make_source_writable() -> None:
            self.step.chmod(stat.S_IRUSR | stat.S_IWUSR)
            gmsh.timeline.append(("finalize",))

        gmsh.finalize.side_effect = make_source_writable
        with self.assertRaisesRegex(MeasurementSurfaceError, "read-only state changed"):
            self._inspect(gmsh)
        gmsh.finalize.assert_called_once_with()

    def test_changed_source_hash_is_detected_even_if_read_only_is_restored(self) -> None:
        gmsh = _FakeGmsh()

        def alter_source_and_restore_read_only() -> None:
            self.step.chmod(stat.S_IRUSR | stat.S_IWUSR)
            self.step.write_text("modified mock STEP\n", encoding="utf-8")
            self._make_read_only(self.step)
            gmsh.timeline.append(("finalize",))

        gmsh.finalize.side_effect = alter_source_and_restore_read_only
        with self.assertRaisesRegex(MeasurementSurfaceError, "hash, size"):
            self._inspect(gmsh)
        gmsh.finalize.assert_called_once_with()

    def test_writable_source_is_rejected_before_gmsh_initialize(self) -> None:
        self.step.chmod(stat.S_IRUSR | stat.S_IWUSR)
        gmsh = _FakeGmsh()
        with self.assertRaisesRegex(ValueError, "must be read-only"):
            self._inspect(gmsh)
        gmsh.initialize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
