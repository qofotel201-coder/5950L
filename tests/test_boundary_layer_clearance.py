from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import tomllib
import unittest
from unittest import mock

from cfdpipe.boundary_layer_clearance import (
    BoundaryLayerClearanceError,
    audit_boundary_layer_clearance,
    probe_normal_clearance,
)
from cfdpipe.coarse_mesh import _make_clearance_evidence_contract


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_hash(value: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()


class _Occ:
    def __init__(self, owner: "_FakeGmsh") -> None:
        self.owner = owner

    def importShapes(self, path: str) -> list[tuple[int, int]]:
        self.owner.imported_paths.append(path)
        if self.owner.import_error is not None:
            raise self.owner.import_error
        return [(3, 7)]

    def synchronize(self) -> None:
        self.owner.synchronize_calls += 1


class _Model:
    def __init__(self, owner: "_FakeGmsh", exit_distance: float = 2.0) -> None:
        self.owner = owner
        self.occ = _Occ(owner)
        self.exit_distance = exit_distance
        self.inside_queries: list[tuple[int, int, tuple[float, ...]]] = []

    def add(self, name: str) -> None:
        self.owner.model_names.append(name)

    def isInside(
        self,
        dimension: int,
        tag: int,
        coordinates: list[float],
        parametric: bool = False,
    ) -> int:
        del parametric
        point = tuple(float(value) for value in coordinates)
        self.inside_queries.append((dimension, tag, point))
        if dimension != 3 or tag != 7:
            raise AssertionError("clearance probes must query only the adjacent volume")
        return int(0.0 < point[0] < self.exit_distance)

    @property
    def mesh(self) -> object:
        raise AssertionError("clearance audit must not access gmsh.model.mesh")


class _Option:
    def __init__(self, owner: "_FakeGmsh") -> None:
        self.owner = owner

    def setString(self, name: str, value: str) -> None:
        self.owner.string_options.append((name, value))


class _FakeGmsh:
    __version__ = "fake-4.15"
    __file__ = __file__

    def __init__(
        self,
        *,
        exit_distance: float = 2.0,
        initialize_error: BaseException | None = None,
        import_error: BaseException | None = None,
    ) -> None:
        self.model = _Model(self, exit_distance)
        self.option = _Option(self)
        self.initialize_error = initialize_error
        self.import_error = import_error
        self.initialized = False
        self.initialize_calls = 0
        self.finalize_calls = 0
        self.imported_paths: list[str] = []
        self.model_names: list[str] = []
        self.string_options: list[tuple[str, str]] = []
        self.synchronize_calls = 0
        self.write_calls = 0

    def isInitialized(self) -> bool:
        return self.initialized

    def initialize(self, *, readConfigFiles: bool = True) -> None:
        if readConfigFiles:
            raise AssertionError("audit must disable ambient Gmsh configuration")
        self.initialize_calls += 1
        self.initialized = True
        if self.initialize_error is not None:
            raise self.initialize_error

    def finalize(self) -> None:
        self.finalize_calls += 1
        self.initialized = False

    def write(self, path: str) -> None:
        del path
        self.write_calls += 1
        raise AssertionError("clearance audit must not call gmsh.write")


def _fingerprints() -> list[str]:
    return [f"{index:064x}" for index in range(1, 49)]


def _contract(
    source: Path,
    marker_path: Path,
    marker_config: dict[str, object],
    *,
    required_thickness: float = 1.0,
) -> dict[str, object]:
    fingerprints = _fingerprints()
    clearance_contract = _make_clearance_evidence_contract(
        pipeline_brep_path=source.resolve(),
        pipeline_brep_sha256=_sha256(source),
        markers_path=marker_path.resolve(),
        markers_sha256=_sha256(marker_path),
        marker_pipeline_geometry_sha256=_sha256(source),
        coordinate_absolute_tolerance_m=1.0e-4,
        required_total_thickness_m=required_thickness,
        wall_physical_name="wall",
        wall_surface_fingerprints=fingerprints,
        direction_probe_distance_m=0.1,
    )
    value: dict[str, object] = {
        "schema": "cfdpipe.coarse_mesh.v1",
        "status": "CONFIGURED",
        "pipeline_brep_path": str(source.resolve()),
        "provenance": {
            "pipeline_brep_sha256": _sha256(source),
            "markers_sha256": _sha256(marker_path),
        },
        "boundary_layer_design": {
            "status": "PASS",
            "total_thickness_m": required_thickness,
        },
        "wall_surface_fingerprints": fingerprints,
        "base_smoke_contract": {
            "wall_physical_name": "wall",
            "direction_probe_distance_m": 0.1,
        },
        "clearance_evidence_contract": clearance_contract,
        "clearance_evidence_contract_sha256": _canonical_hash(
            clearance_contract
        ),
    }
    value["normalized_config_sha256"] = _canonical_hash(value)
    return value


def _marker_config(source: Path) -> dict[str, object]:
    return {
        "pipeline_geometry": {
            "path": str(source),
            "sha256": _sha256(source),
            "format": "brep",
            "pipeline_eligible": True,
            "read_only": True,
        },
        "matching": {"coordinate_absolute_tolerance_m": 1.0e-4},
    }


def _catalog() -> dict[str, object]:
    surfaces = [
        {
            "entity_tag": index,
            "adjacent_volumes": [7],
        }
        for index in range(1, 49)
    ]
    return {
        "surfaces": surfaces,
        "volumes": [
            {
                "entity_tag": 7,
                "bounding_box_m": [0.0, -1.0, -1.0, 2.0, 1.0, 1.0],
            }
        ],
    }


def _resolved() -> dict[str, object]:
    return {
        "surface_diagnostic_index": {
            tag: {
                "fingerprint_id": fingerprint,
                "physical_name": "wall",
            }
            for tag, fingerprint in enumerate(_fingerprints(), start=1)
        }
    }


def _direction_audit(
    gmsh: object,
    catalog: dict[str, object],
    surface_tag: int,
    *,
    probe_distances_m: list[float],
) -> dict[str, object]:
    del gmsh, catalog
    epsilon = min(probe_distances_m)
    return {
        "status": "PASS",
        "surface_tag_audit": surface_tag,
        "adjacent_volume_tag_audit": 7,
        "inward_height_sign": 1,
        "samples": [
            {
                "label": f"sample_{index}",
                "parametric_coordinates": [0.1 * index, 0.2 * index],
                "point_m": [0.0, 0.0, 0.0],
                "surface_unit_normal": [1.0, 0.0, 0.0],
                "resolved_height_sign": 1,
                "tests": [
                    {
                        "epsilon_m": epsilon,
                        "plus_inside_count": 1,
                        "minus_inside_count": 0,
                    }
                ],
            }
            for index in range(1, 6)
        ],
    }


class NormalClearanceProbeTests(unittest.TestCase):
    def test_doubling_and_bisection_bracket_the_first_detected_exit(self) -> None:
        gmsh = _FakeGmsh(exit_distance=2.0)
        result = probe_normal_clearance(
            gmsh,
            volume_tag=7,
            point_m=[0.0, 0.0, 0.0],
            inward_unit_normal=[1.0, 0.0, 0.0],
            initial_inside_distance_m=0.125,
            maximum_distance_m=8.0,
            absolute_tolerance_m=1.0e-7,
        )

        self.assertEqual(result["status"], "PASS")
        self.assertLessEqual(result["clearance_lower_bound_m"], 2.0)
        self.assertGreaterEqual(result["clearance_upper_bound_m"], 2.0)
        self.assertLessEqual(result["bracket_width_m"], 1.0e-7)
        self.assertGreater(result["expansion_iterations"], 0)
        self.assertGreater(result["bisection_iterations"], 0)
        self.assertTrue(all(item[0:2] == (3, 7) for item in gmsh.model.inside_queries))

    def test_probe_fails_when_initial_point_is_outside_or_exit_is_unbracketed(self) -> None:
        with self.assertRaisesRegex(BoundaryLayerClearanceError, "initial"):
            probe_normal_clearance(
                _FakeGmsh(exit_distance=0.05),
                volume_tag=7,
                point_m=[0.0, 0.0, 0.0],
                inward_unit_normal=[1.0, 0.0, 0.0],
                initial_inside_distance_m=0.1,
                maximum_distance_m=1.0,
                absolute_tolerance_m=1.0e-6,
            )
        with self.assertRaisesRegex(BoundaryLayerClearanceError, "bracket"):
            probe_normal_clearance(
                _FakeGmsh(exit_distance=100.0),
                volume_tag=7,
                point_m=[0.0, 0.0, 0.0],
                inward_unit_normal=[1.0, 0.0, 0.0],
                initial_inside_distance_m=0.1,
                maximum_distance_m=1.0,
                absolute_tolerance_m=1.0e-6,
            )


class BoundaryLayerClearanceLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name) / "pipeline.brep"
        self.source.write_bytes(b"read-only-brep\n")
        os.chmod(self.source, stat.S_IREAD)
        self.addCleanup(os.chmod, self.source, stat.S_IREAD | stat.S_IWRITE)
        self.markers = Path(self.temporary.name) / "markers.toml"
        self.markers.write_text(
            "[pipeline_geometry]\n"
            f'sha256 = "{_sha256(self.source)}"\n'
            "[matching]\n"
            "coordinate_absolute_tolerance_m = 0.0001\n",
            encoding="utf-8",
        )
        self.marker_config = tomllib.loads(self.markers.read_text("utf-8"))

    def _run(
        self,
        gmsh: _FakeGmsh,
        *,
        required_thickness: float = 1.0,
        direction_side_effect: BaseException | None = None,
    ) -> dict[str, object]:
        direction = (
            mock.Mock(side_effect=direction_side_effect)
            if direction_side_effect is not None
            else _direction_audit
        )
        with (
            mock.patch(
                "cfdpipe.boundary_layer_clearance._collect_model",
                return_value=_catalog(),
            ),
            mock.patch(
                "cfdpipe.boundary_layer_clearance._normalize_contract",
                return_value={"normalized": True},
            ),
            mock.patch(
                "cfdpipe.boundary_layer_clearance._resolve_entities",
                return_value=_resolved(),
            ),
            mock.patch(
                "cfdpipe.boundary_layer_clearance._audit_wall_inward_direction",
                direction,
            ),
        ):
            return audit_boundary_layer_clearance(
                gmsh_module=gmsh,
                pipeline_brep=self.source,
                coarse_contract=_contract(
                    self.source,
                    self.markers,
                    self.marker_config,
                    required_thickness=required_thickness,
                ),
                marker_config=self.marker_config,
            )

    def test_read_only_audit_covers_48_surfaces_and_240_samples(self) -> None:
        gmsh = _FakeGmsh(exit_distance=2.0)
        result = self._run(gmsh, required_thickness=1.0)

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["execution_status"], "PASS")
        self.assertEqual(result["clearance_requirement_status"], "PASS")
        self.assertEqual(result["surface_count"], 48)
        self.assertEqual(result["sample_count"], 240)
        self.assertEqual(len(result["surfaces"]), 48)
        self.assertGreater(result["minimum_clearance_lower_bound_m"], 1.0)
        self.assertTrue(result["source_unchanged"])
        self.assertTrue(result["gmsh_session"]["initialize_called"])
        self.assertTrue(result["gmsh_session"]["finalize_called"])
        self.assertEqual(gmsh.initialize_calls, 1)
        self.assertEqual(gmsh.finalize_calls, 1)
        self.assertEqual(gmsh.write_calls, 0)
        self.assertEqual(gmsh.string_options, [("Geometry.OCCTargetUnit", "M")])
        self.assertFalse(result["scope"]["mesh_generated"])
        self.assertFalse(result["scope"]["extrusion_feasibility_claimed"])

    def test_insufficient_clearance_fails_requirement_without_fabricating_exception(self) -> None:
        result = self._run(_FakeGmsh(exit_distance=2.0), required_thickness=3.0)

        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["execution_status"], "PASS")
        self.assertEqual(result["clearance_requirement_status"], "FAIL")
        self.assertIsNone(result["error"])
        self.assertTrue(result["failure_reasons"])

    def test_direction_exception_keeps_traceback_and_still_finalizes(self) -> None:
        gmsh = _FakeGmsh()
        result = self._run(
            gmsh,
            direction_side_effect=RuntimeError("direction exploded"),
        )

        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["execution_status"], "FAIL")
        self.assertEqual(result["error"]["type"], "RuntimeError")
        self.assertIn("direction exploded", result["error"]["traceback"])
        self.assertEqual(result["gmsh_module_path"], str(Path(__file__).resolve()))
        self.assertTrue(result["gmsh_session"]["finalize_called"])
        self.assertEqual(gmsh.finalize_calls, 1)
        self.assertTrue(result["source_unchanged"])

    def test_partial_initialize_failure_is_detected_and_finalized(self) -> None:
        gmsh = _FakeGmsh(initialize_error=RuntimeError("initialize exploded"))
        result = self._run(gmsh)

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("initialize exploded", result["error"]["traceback"])
        self.assertTrue(result["gmsh_session"]["initialize_attempted"])
        self.assertFalse(result["gmsh_session"]["initialize_called"])
        self.assertTrue(result["gmsh_session"]["finalize_attempted"])
        self.assertTrue(result["gmsh_session"]["finalize_called"])
        self.assertEqual(gmsh.finalize_calls, 1)


if __name__ == "__main__":
    unittest.main()
