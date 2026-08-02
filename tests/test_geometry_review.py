"""Mock-only tests for the diagnostic STEP surface-mesh review."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from cfdpipe.geometry_review import (
    GeometryReviewError,
    build_diagnostic_surface_mesh,
)


class _FakeLogger:
    def __init__(self) -> None:
        self.start = mock.Mock()
        self.get = mock.Mock(return_value=["fake Gmsh diagnostic log"])
        self.stop = mock.Mock()


class _FakeOption:
    def __init__(self, timeline: list[tuple[object, ...]]) -> None:
        self._timeline = timeline
        self.setString = mock.Mock(side_effect=self._set_string)
        self.setNumber = mock.Mock(side_effect=self._set_number)

    def _set_string(self, name: str, value: str) -> None:
        self._timeline.append(("setString", name, value))

    def _set_number(self, name: str, value: float) -> None:
        self._timeline.append(("setNumber", name, value))


class _FakeOcc:
    def __init__(self, timeline: list[tuple[object, ...]]) -> None:
        self._timeline = timeline
        self.importShapes = mock.Mock(side_effect=self._import_shapes)
        self.synchronize = mock.Mock(
            side_effect=lambda: self._timeline.append(("synchronize",))
        )
        self.getMass = mock.Mock(side_effect=lambda dim, tag: 1.0)
        self.getCenterOfMass = mock.Mock(side_effect=self._center)
        self.getBoundingBox = mock.Mock(side_effect=self._bounds)
        self.getDistance = mock.Mock(side_effect=self._distance)
        self.fragment = mock.Mock()
        self.fuse = mock.Mock()
        self.removeAllDuplicates = mock.Mock()

    def _import_shapes(self, path: str) -> list[tuple[int, int]]:
        self._timeline.append(("importShapes", path))
        return [(3, 101), (3, 102)]

    @staticmethod
    def _center(dimension: int, tag: int) -> list[float]:
        if dimension == 2:
            return [0.5, 0.5, 0.0 if tag == 11 else 0.01]
        return [0.5, 0.5, float(tag - 101)]

    @staticmethod
    def _bounds(dimension: int, tag: int) -> list[float]:
        if dimension == 2 and tag == 11:
            return [0.0, 0.0, 0.0, 1.0, 1.0, 0.0]
        if dimension == 2 and tag == 12:
            return [0.0, 0.0, 0.01, 1.0, 1.0, 0.01]
        return [0.0, 0.0, 0.0, 1.0, 1.0, 1.0]

    @staticmethod
    def _distance(
        dimension_a: int,
        tag_a: int,
        dimension_b: int,
        tag_b: int,
    ) -> tuple[float, list[float], list[float]]:
        distance = 0.01 if dimension_a == 2 else 0.25
        return distance, [0.0, 0.0, 0.0], [0.0, 0.0, distance]


class _FakeMesh:
    def __init__(
        self,
        timeline: list[tuple[object, ...]],
        *,
        triangle_count: int = 2,
        volume_element_count: int = 0,
        generate_error: BaseException | None = None,
    ) -> None:
        self._timeline = timeline
        self.triangle_count = triangle_count
        self.volume_element_count = volume_element_count
        self.generate_error = generate_error
        self.setSize = mock.Mock()
        self.generate = mock.Mock(side_effect=self._generate)
        self.getNodes = mock.Mock(
            return_value=(
                [1, 2, 3],
                [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0],
                [],
            )
        )
        self.getElements = mock.Mock(side_effect=self._get_elements)
        self.getElementProperties = mock.Mock(
            return_value=("Triangle 3", 2, 1, 3, [], 3)
        )

    def _generate(self, dimension: int) -> None:
        self._timeline.append(("generate", dimension))
        if self.generate_error is not None:
            raise self.generate_error

    def _get_elements(self, dimension: int):
        if dimension == 3:
            if self.volume_element_count:
                return [4], [list(range(1, self.volume_element_count + 1))], [[1, 2, 3, 1]]
            return [], [], []
        if dimension == 2:
            return (
                [2],
                [list(range(1, self.triangle_count + 1))],
                [[1, 2, 3] * self.triangle_count],
            )
        return [], [], []


class _FakeModel:
    def __init__(
        self,
        timeline: list[tuple[object, ...]],
        *,
        triangle_count: int = 2,
        volume_element_count: int = 0,
        generate_error: BaseException | None = None,
    ) -> None:
        self._timeline = timeline
        self.occ = _FakeOcc(timeline)
        self.mesh = _FakeMesh(
            timeline,
            triangle_count=triangle_count,
            volume_element_count=volume_element_count,
            generate_error=generate_error,
        )
        self.add = mock.Mock(
            side_effect=lambda name: self._timeline.append(("model.add", name))
        )
        self.getEntities = mock.Mock(side_effect=self._get_entities)
        self.getType = mock.Mock(side_effect=self._get_type)
        self.getAdjacencies = mock.Mock(side_effect=self._get_adjacencies)
        self.getBoundary = mock.Mock(side_effect=self._get_boundary)
        self._next_physical_tag = 200
        self.addPhysicalGroup = mock.Mock(side_effect=self._add_physical_group)
        self.setPhysicalName = mock.Mock()

    @staticmethod
    def _get_entities(dimension: int):
        return {
            0: [(0, 1), (0, 2), (0, 3)],
            2: [(2, 11), (2, 12)],
            3: [(3, 101), (3, 102)],
        }.get(dimension, [])

    @staticmethod
    def _get_type(dimension: int, tag: int) -> str:
        return "Plane" if dimension == 2 else "Volume"

    @staticmethod
    def _get_adjacencies(dimension: int, tag: int):
        if dimension == 2:
            return ([101] if tag == 11 else [102]), ([1, 2] if tag == 11 else [3, 4])
        if dimension == 3:
            return [], ([11] if tag == 101 else [12])
        return [], []

    @staticmethod
    def _get_boundary(entities, *, oriented: bool, recursive: bool):
        tag = int(entities[0][1])
        return [(2, 11 if tag == 101 else 12)]

    def _add_physical_group(self, dimension: int, tags: list[int]) -> int:
        self._next_physical_tag += 1
        return self._next_physical_tag


class _FakeGmsh:
    __version__ = "4.15.2-test"
    __file__ = __file__

    def __init__(
        self,
        *,
        triangle_count: int = 2,
        volume_element_count: int = 0,
        generate_error: BaseException | None = None,
    ) -> None:
        self.timeline: list[tuple[object, ...]] = []
        self.model = _FakeModel(
            self.timeline,
            triangle_count=triangle_count,
            volume_element_count=volume_element_count,
            generate_error=generate_error,
        )
        self.option = _FakeOption(self.timeline)
        self.logger = _FakeLogger()
        self.initialize = mock.Mock(
            side_effect=lambda: self.timeline.append(("initialize",))
        )
        self.finalize = mock.Mock(
            side_effect=lambda: self.timeline.append(("finalize",))
        )
        self.write = mock.Mock(side_effect=self._write)
        self.fltk = SimpleNamespace(run=mock.Mock())

    def _write(self, path: str) -> None:
        self.timeline.append(("write", path))
        Path(path).write_text("ASCII diagnostic mesh\n", encoding="ascii")


class GeometryReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.step = self.root / "input.step"
        self.step.write_text("mock read-only STEP\n", encoding="utf-8")
        self._make_read_only(self.step)
        self.output_root = self.root / "allowed"
        self.output = self.output_root / "review"

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

    def _run(self, gmsh: _FakeGmsh, **kwargs):
        with mock.patch.dict(sys.modules, {"gmsh": gmsh}):
            return build_diagnostic_surface_mesh(
                self.step,
                self.output,
                allowed_output_root=self.output_root,
                **kwargs,
            )

    def test_builds_named_surface_only_mesh_and_records_geometry(self) -> None:
        gmsh = _FakeGmsh()

        manifest = self._run(gmsh)

        self.assertEqual(manifest["status"], "PASS")
        self.assertEqual(manifest["mesh"]["triangle_count"], 2)
        self.assertEqual(manifest["mesh"]["volume_element_count"], 0)
        names = [entry[0][2] for entry in gmsh.model.setPhysicalName.call_args_list]
        self.assertEqual(
            names,
            ["diagnostic_surface_11", "diagnostic_surface_12"],
        )
        for call, tag in zip(gmsh.model.addPhysicalGroup.call_args_list, (11, 12)):
            self.assertEqual(call.args, (2, [tag]))
        gmsh.model.mesh.generate.assert_called_once_with(2)
        self.assertTrue(any(call.args == (3,) for call in gmsh.model.mesh.getElements.call_args_list))
        self.assertEqual(
            {Path(call.args[0]).suffix.lower() for call in gmsh.write.call_args_list},
            {".msh", ".vtk"},
        )
        self.assertFalse((self.output / "diagnostic_surfaces.su2").exists())
        gmsh.fltk.run.assert_not_called()
        gmsh.model.occ.fragment.assert_not_called()
        gmsh.model.occ.fuse.assert_not_called()
        gmsh.model.occ.removeAllDuplicates.assert_not_called()
        gmsh.finalize.assert_called_once_with()

        types = json.loads(
            (self.output / "surface_types.json").read_text(encoding="utf-8")
        )
        self.assertTrue(
            {
                "entity_tag",
                "entity_type",
                "adjacent_volumes",
                "area",
                "centroid",
                "bounds",
                "area_m2",
                "centroid_m",
                "bounds_m",
            }.issubset(types["surfaces"][0])
        )
        self.assertEqual(types["volumes"][0]["boundary_surfaces"], [11])
        self.assertEqual(types["volumes"][0]["face_count"], 1)
        self.assertEqual(types["surfaces"][0]["boundary_curves"], [1, 2])
        self.assertEqual(len(types["distance_evidence"]["volume_pairs"]), 1)
        self.assertEqual(len(types["distance_evidence"]["surface_pairs"]), 1)
        gmsh.model.occ.getDistance.assert_any_call(3, 101, 3, 102)
        gmsh.model.occ.getDistance.assert_any_call(2, 11, 2, 12)
        self.assertTrue(manifest["source_unchanged"])
        for name in (
            "diagnostic_surfaces.msh",
            "diagnostic_surfaces.vtk",
            "surface_types.json",
            "gmsh_review.log",
        ):
            self.assertRegex(manifest["outputs"][name]["sha256"], r"^[0-9a-f]{64}$")

    def test_rejects_any_three_dimensional_mesh_elements(self) -> None:
        gmsh = _FakeGmsh(volume_element_count=1)

        with self.assertRaisesRegex(GeometryReviewError, "3D elements"):
            self._run(gmsh)

        gmsh.finalize.assert_called_once_with()
        gmsh.write.assert_not_called()
        failure = json.loads(
            (self.output / "geometry_review_mesh_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(failure["status"], "FAIL")

    def test_enforces_requested_triangle_limit(self) -> None:
        gmsh = _FakeGmsh(triangle_count=2)

        with self.assertRaisesRegex(GeometryReviewError, "outside"):
            self._run(gmsh, max_triangles=1)

        gmsh.finalize.assert_called_once_with()
        gmsh.write.assert_not_called()

    def test_operation_exception_keeps_traceback_and_finalizes(self) -> None:
        gmsh = _FakeGmsh(generate_error=RuntimeError("mesh sentinel"))

        with self.assertRaisesRegex(GeometryReviewError, "mesh sentinel"):
            self._run(gmsh)

        gmsh.finalize.assert_called_once_with()
        failure = json.loads(
            (self.output / "geometry_review_mesh_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("Traceback (most recent call last)", failure["error"]["traceback"])
        self.assertIn("RuntimeError: mesh sentinel", failure["error"]["traceback"])
        log = (self.output / "gmsh_review.log").read_text(encoding="utf-8")
        self.assertIn("RuntimeError: mesh sentinel", log)
        gmsh.fltk.run.assert_not_called()

    def test_missing_distance_api_records_warning_without_fabrication(self) -> None:
        gmsh = _FakeGmsh()
        gmsh.model.occ.getDistance = None

        manifest = self._run(gmsh)

        self.assertEqual(manifest["distance_evidence"]["capability"], "UNAVAILABLE")
        self.assertEqual(manifest["distance_evidence"]["volume_pairs"], [])
        self.assertEqual(manifest["distance_evidence"]["surface_pairs"], [])
        self.assertTrue(any("unavailable" in item for item in manifest["warnings"]))

    def test_gmsh_415_flat_seven_value_distance_result_is_supported(self) -> None:
        gmsh = _FakeGmsh()
        gmsh.model.occ.getDistance = mock.Mock(
            return_value=(0.125, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
        )
        manifest = self._run(gmsh)
        record = manifest["distance_evidence"]["volume_pairs"][0]
        self.assertEqual(record["distance_m"], 0.125)
        self.assertEqual(record["nearest_point_a_m"], [1.0, 2.0, 3.0])
        self.assertEqual(record["nearest_point_b_m"], [4.0, 5.0, 6.0])

    def test_logger_warnings_are_retained_in_manifest_and_surface_types(self) -> None:
        gmsh = _FakeGmsh()
        gmsh.logger.get.return_value = [
            "Warning: diagnostic surface warning sentinel"
        ]

        manifest = self._run(gmsh)
        types = json.loads(
            (self.output / "surface_types.json").read_text(encoding="utf-8")
        )

        self.assertEqual(manifest["diagnostic_quality_status"], "WARN")
        self.assertFalse(manifest["production_mesh_eligible"])
        self.assertEqual(types["warnings"], manifest["warnings"])
        self.assertIn("warning sentinel", manifest["warnings"][0])

    def test_output_escape_is_rejected_before_gmsh_initialization(self) -> None:
        gmsh = _FakeGmsh()
        escaped = self.root / "escaped"

        with mock.patch.dict(sys.modules, {"gmsh": gmsh}):
            with self.assertRaisesRegex(ValueError, "escapes"):
                build_diagnostic_surface_mesh(
                    self.step,
                    escaped,
                    allowed_output_root=self.output_root,
                )

        gmsh.initialize.assert_not_called()
        self.assertFalse(escaped.exists())

    def test_writable_source_is_rejected_before_output_or_import(self) -> None:
        self.step.chmod(stat.S_IRUSR | stat.S_IWUSR)
        gmsh = _FakeGmsh()

        with mock.patch.dict(sys.modules, {"gmsh": gmsh}):
            with self.assertRaisesRegex(ValueError, "read-only"):
                build_diagnostic_surface_mesh(
                    self.step,
                    self.output,
                    allowed_output_root=self.output_root,
                )

        gmsh.initialize.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_reparse_source_is_rejected_before_gmsh_initialization(self) -> None:
        gmsh = _FakeGmsh()

        with (
            mock.patch.dict(sys.modules, {"gmsh": gmsh}),
            mock.patch(
                "cfdpipe.geometry_review._existing_path_has_reparse_component",
                return_value=True,
            ),
        ):
            with self.assertRaisesRegex(ValueError, "reparse"):
                build_diagnostic_surface_mesh(
                    self.step,
                    self.output,
                    allowed_output_root=self.output_root,
                )

        gmsh.initialize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
