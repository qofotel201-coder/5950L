"""Contract tests for read-only STEP geometry inspection through Gmsh.

The suite uses a deterministic in-memory stand-in for the public Gmsh API.  It
must never generate a mesh, write through Gmsh, create physical groups, or start
an external executable.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path
import stat
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock

from cfdpipe.bridges import gmsh_bridge


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
REAL_CONNECTION_ROOT = (REPOSITORY_ROOT / "runs" / "real_connection").resolve()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_cell(row: dict[str, str], name: str) -> object:
    value = row.get(name)
    if value is None:
        raise AssertionError(f"catalog is missing required column {name!r}")
    return json.loads(value)


def make_geometry_gmsh(
    *,
    operation_error: BaseException | None = None,
) -> ModuleType:
    """Return a minimal deterministic fake for Gmsh geometry-query APIs."""

    gmsh = ModuleType("gmsh")
    gmsh.__version__ = "4.15.2-test"
    gmsh.__file__ = str((REAL_CONNECTION_ROOT / "fake_sdk" / "gmsh.py").resolve())
    gmsh.timeline = []

    gmsh.initialize = mock.Mock(
        side_effect=lambda: gmsh.timeline.append(("initialize",))
    )
    gmsh.finalize = mock.Mock(
        side_effect=lambda: gmsh.timeline.append(("finalize",))
    )
    gmsh.write = mock.Mock()
    gmsh.fltk = SimpleNamespace(run=mock.Mock())

    def set_string(name: str, value: str) -> None:
        gmsh.timeline.append(("setString", str(name), str(value)))

    gmsh.option = SimpleNamespace(
        setString=mock.Mock(side_effect=set_string),
        setNumber=mock.Mock(),
    )
    gmsh.logger = SimpleNamespace(
        start=mock.Mock(),
        get=mock.Mock(
            return_value=[
                "Info : imported one volume",
                "Info : inspected two surfaces without meshing",
            ]
        ),
        stop=mock.Mock(),
    )

    def import_shapes(path: str, *args: object, **kwargs: object):
        del args, kwargs
        gmsh.timeline.append(("importShapes", str(path)))
        return [(3, 101)]

    def synchronize() -> None:
        gmsh.timeline.append(("occ.synchronize",))

    def get_mass(dimension: int, tag: int) -> float:
        if operation_error is not None and dimension == 2 and tag == 11:
            raise operation_error
        return {
            (2, 11): 2.0,
            (2, 12): 3.0,
            (3, 101): 4.0,
        }[(dimension, tag)]

    def get_center_of_mass(dimension: int, tag: int):
        return {
            (2, 11): (0.0, 0.5, 0.5),
            (2, 12): (1.0, 0.5, 0.5),
            (3, 101): (0.5, 0.5, 0.5),
        }[(dimension, tag)]

    def get_bounding_box(dimension: int, tag: int):
        return {
            (2, 11): (0.0, 0.0, 0.0, 0.0, 1.0, 1.0),
            (2, 12): (1.0, 0.0, 0.0, 1.0, 1.0, 1.0),
            (3, 101): (0.0, 0.0, 0.0, 1.0, 1.0, 1.0),
        }[(dimension, tag)]

    occ = SimpleNamespace(
        importShapes=mock.Mock(side_effect=import_shapes),
        synchronize=mock.Mock(side_effect=synchronize),
        getMass=mock.Mock(side_effect=get_mass),
        getCenterOfMass=mock.Mock(side_effect=get_center_of_mass),
        getBoundingBox=mock.Mock(side_effect=get_bounding_box),
    )

    def get_entities(dimension: int = -1):
        return {
            2: [(2, 11), (2, 12)],
            3: [(3, 101)],
            -1: [(2, 11), (2, 12), (3, 101)],
        }.get(dimension, [])

    def get_entity_name(dimension: int, tag: int) -> str:
        return {
            (2, 11): "STEP_FRONT",
            (2, 12): "STEP_WALL",
            (3, 101): "STEP_FLUID_VOLUME",
        }.get((dimension, tag), "")

    def get_adjacencies(dimension: int, tag: int):
        if dimension == 2 and tag in {11, 12}:
            return ([101], [201, 202])
        return ([], [])

    def parametrization_bounds(dimension: int, tag: int):
        del tag
        if dimension != 2:
            return ([], [])
        return ([0.0, 0.0], [1.0, 1.0])

    def normals(tag: int, parameters: list[float]):
        sample_count = max(1, len(parameters) // 2)
        normal = (1.0, 0.0, 0.0) if tag == 12 else (-1.0, 0.0, 0.0)
        return list(normal) * sample_count

    def derivatives(dimension: int, tag: int, parameters: list[float]):
        del dimension, tag
        sample_count = max(1, len(parameters) // 2)
        return [1.0, 0.0, 0.0, 0.0, 1.0, 0.0] * sample_count

    mesh = SimpleNamespace(generate=mock.Mock())
    model = SimpleNamespace(
        add=mock.Mock(),
        occ=occ,
        mesh=mesh,
        getEntities=mock.Mock(side_effect=get_entities),
        getEntityName=mock.Mock(side_effect=get_entity_name),
        getBoundingBox=mock.Mock(side_effect=get_bounding_box),
        getAdjacencies=mock.Mock(side_effect=get_adjacencies),
        getParametrizationBounds=mock.Mock(side_effect=parametrization_bounds),
        getNormal=mock.Mock(side_effect=normals),
        getDerivative=mock.Mock(side_effect=derivatives),
        addPhysicalGroup=mock.Mock(),
        setPhysicalName=mock.Mock(),
    )
    gmsh.model = model
    return gmsh


class GeometryInspectionTests(unittest.TestCase):
    def setUp(self) -> None:
        REAL_CONNECTION_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="geometry_inspection_",
            dir=REAL_CONNECTION_ROOT,
        )
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.source = self.root / "source" / "model.step"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b"ISO-10303-21; mock read-only STEP; END-ISO-10303-21;")
        self.output = self.root / "geometry"

    def _make_source_read_only(self) -> None:
        self.source.chmod(stat.S_IREAD)

        def restore_permission() -> None:
            if self.source.exists():
                self.source.chmod(stat.S_IREAD | stat.S_IWRITE)

        self.addCleanup(restore_permission)

    @staticmethod
    def _assert_no_model_mutation(gmsh: ModuleType) -> None:
        gmsh.model.mesh.generate.assert_not_called()
        gmsh.write.assert_not_called()
        gmsh.model.addPhysicalGroup.assert_not_called()
        gmsh.model.setPhysicalName.assert_not_called()
        gmsh.fltk.run.assert_not_called()

    def test_inspection_lifecycle_units_catalogs_and_no_meshing(self) -> None:
        self._make_source_read_only()
        original_hash = _sha256(self.source)
        gmsh = make_geometry_gmsh()

        with mock.patch("cfdpipe.bridges.gmsh_bridge._import_gmsh", return_value=gmsh):
            manifest = gmsh_bridge.GmshBridge().inspect_step_geometry(
                self.source,
                self.output,
                allowed_output_root=self.output,
            )

        gmsh.initialize.assert_called_once_with()
        gmsh.finalize.assert_called_once_with()
        event_names = [event[0] for event in gmsh.timeline]
        self.assertLess(event_names.index("initialize"), event_names.index("setString"))
        self.assertLess(event_names.index("setString"), event_names.index("importShapes"))
        self.assertLess(
            event_names.index("importShapes"), event_names.index("occ.synchronize")
        )
        self.assertLess(event_names.index("occ.synchronize"), event_names.index("finalize"))
        gmsh.option.setString.assert_any_call("Geometry.OCCTargetUnit", "M")
        gmsh.model.occ.importShapes.assert_called_once_with(str(self.source.resolve()))
        gmsh.model.occ.synchronize.assert_called_once_with()
        self._assert_no_model_mutation(gmsh)

        expected_files = (
            "surface_catalog.csv",
            "volume_catalog.csv",
            "marker_candidates.json",
            "geometry_manifest.json",
            "gmsh_geometry.log",
        )
        for filename in expected_files:
            with self.subTest(output=filename):
                path = self.output / filename
                self.assertTrue(path.is_file())
                self.assertGreater(path.stat().st_size, 0)

        with (self.output / "surface_catalog.csv").open(
            "r", encoding="utf-8-sig", newline=""
        ) as stream:
            surfaces = list(csv.DictReader(stream))
        self.assertEqual(len(surfaces), 2)
        self.assertEqual({int(row["entity_tag"]) for row in surfaces}, {11, 12})
        self.assertEqual(
            {row["step_name"] for row in surfaces},
            {"STEP_FRONT", "STEP_WALL"},
        )
        for row in surfaces:
            with self.subTest(surface=row["entity_tag"]):
                self.assertGreater(float(row["area_m2"]), 0.0)
                centroid = _json_cell(row, "centroid_m")
                bounds = _json_cell(row, "bounding_box_m")
                normal_samples = _json_cell(row, "normal_samples")
                adjacent_volumes = _json_cell(row, "adjacent_volumes")
                self.assertEqual(len(centroid), 3)
                self.assertEqual(len(bounds), 6)
                self.assertTrue(normal_samples)
                self.assertTrue(
                    all(
                        len(sample) == 3
                        and all(math.isfinite(float(value)) for value in sample)
                        for sample in normal_samples
                    )
                )
                self.assertEqual(adjacent_volumes, [101])
                self.assertEqual(row["inferred_boundary_role"], "unclassified")
                self.assertEqual(float(row["recognition_confidence"]), 0.0)

        with (self.output / "volume_catalog.csv").open(
            "r", encoding="utf-8-sig", newline=""
        ) as stream:
            volumes = list(csv.DictReader(stream))
        self.assertEqual(len(volumes), 1)
        self.assertEqual(int(volumes[0]["entity_tag"]), 101)
        self.assertEqual(volumes[0]["step_name"], "STEP_FLUID_VOLUME")
        self.assertGreater(float(volumes[0]["volume_m3"]), 0.0)
        self.assertEqual(len(_json_cell(volumes[0], "centroid_m")), 3)
        self.assertEqual(len(_json_cell(volumes[0], "bounding_box_m")), 6)

        candidates = json.loads(
            (self.output / "marker_candidates.json").read_text(encoding="utf-8")
        )
        self.assertEqual(candidates["status"], "UNCLASSIFIED")
        self.assertEqual(len(candidates["surfaces"]), 2)
        self.assertTrue(
            all(
                item["inferred_boundary_role"] == "unclassified"
                and float(item["recognition_confidence"]) == 0.0
                for item in candidates["surfaces"]
            )
        )

        persisted = json.loads(
            (self.output / "geometry_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest, persisted)
        self.assertEqual(persisted["status"], "PASS")
        self.assertEqual(persisted["source_step_path"], str(self.source.resolve()))
        self.assertEqual(persisted["source_sha256_before"], original_hash)
        self.assertEqual(persisted["source_sha256_after"], original_hash)
        self.assertTrue(persisted["source_read_only"])
        self.assertEqual(persisted["surface_count"], 2)
        self.assertEqual(persisted["volume_count"], 1)
        self.assertEqual(persisted["gmsh_version"], "4.15.2-test")
        self.assertEqual(persisted["gmsh_module_path"], gmsh.__file__)
        for filename in expected_files[:3]:
            record = persisted["outputs"][filename]
            self.assertEqual(record["path"], str((self.output / filename).resolve()))
            self.assertEqual(record["sha256"], _sha256(self.output / filename))
        self.assertEqual(_sha256(self.source), original_hash)
        geometry_log = (self.output / "gmsh_geometry.log").read_text(encoding="utf-8")
        self.assertIn("status: PASS", geometry_log)
        self.assertIn("inspected two surfaces without meshing", geometry_log)

    def test_geometry_query_exception_finalizes_and_persists_full_failure(self) -> None:
        self._make_source_read_only()
        original_hash = _sha256(self.source)
        gmsh = make_geometry_gmsh(
            operation_error=RuntimeError("original OCC geometry diagnostic")
        )

        with mock.patch("cfdpipe.bridges.gmsh_bridge._import_gmsh", return_value=gmsh):
            with self.assertRaises(gmsh_bridge.GmshBridgeError):
                gmsh_bridge.GmshBridge().inspect_step_geometry(
                    self.source,
                    self.output,
                    allowed_output_root=self.output,
                )

        gmsh.initialize.assert_called_once_with()
        gmsh.finalize.assert_called_once_with()
        self._assert_no_model_mutation(gmsh)
        self.assertEqual(_sha256(self.source), original_hash)
        manifest_path = self.output / "geometry_manifest.json"
        log_path = self.output / "gmsh_geometry.log"
        self.assertTrue(manifest_path.is_file())
        self.assertTrue(log_path.is_file())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "FAIL")
        self.assertEqual(manifest["source_sha256_before"], original_hash)
        self.assertEqual(manifest["source_sha256_after"], original_hash)
        self.assertEqual(manifest["error"]["type"], "RuntimeError")
        self.assertEqual(
            manifest["error"]["message"], "original OCC geometry diagnostic"
        )
        self.assertIn("Traceback", manifest["error"]["traceback"])
        self.assertIn(
            "original OCC geometry diagnostic", manifest["error"]["traceback"]
        )
        log_text = log_path.read_text(encoding="utf-8")
        self.assertIn("status: FAIL", log_text)
        self.assertIn("original OCC geometry diagnostic", log_text)

    def test_writable_source_is_rejected_before_importing_gmsh(self) -> None:
        self.source.chmod(stat.S_IREAD | stat.S_IWRITE)

        with mock.patch("cfdpipe.bridges.gmsh_bridge._import_gmsh") as import_gmsh:
            with self.assertRaises((ValueError, gmsh_bridge.GmshBridgeError)):
                gmsh_bridge.GmshBridge().inspect_step_geometry(
                    self.source,
                    self.output,
                    allowed_output_root=self.output,
                )

        import_gmsh.assert_not_called()

    def test_output_escape_is_rejected_before_importing_gmsh(self) -> None:
        self._make_source_read_only()
        authorized = self.root / "authorized"
        escaped = self.root / "outside" / "geometry"

        with mock.patch("cfdpipe.bridges.gmsh_bridge._import_gmsh") as import_gmsh:
            with self.assertRaises((ValueError, gmsh_bridge.GmshBridgeError)):
                gmsh_bridge.GmshBridge().inspect_step_geometry(
                    self.source,
                    escaped,
                    allowed_output_root=authorized,
                )

        import_gmsh.assert_not_called()
        self.assertFalse(escaped.exists())


if __name__ == "__main__":
    unittest.main()
