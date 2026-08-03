from __future__ import annotations

import inspect
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from cfdpipe.boundary_layer_smoke import RealProjectBoundaryLayerStrategy
from cfdpipe.coarse_mesh_projection import (
    evaluate_projected_element_count,
    make_projection_strategy_config,
    run_coarse_mesh_projection,
    summarize_one_layer_projection,
)


class _Mesh:
    def __init__(self) -> None:
        self.blocks: dict[tuple[int, int], tuple[list[int], list[list[int]], list[list[int]]]] = {}

    def add_block(
        self,
        dimension: int,
        entity: int,
        element_type: int,
        tags: list[int],
        nodes: list[int],
    ) -> None:
        self.blocks[(dimension, entity)] = ([element_type], [tags], [nodes])

    def getElements(self, dimension: int, entity: int):
        return self.blocks.get((dimension, entity), ([], [], []))

    @staticmethod
    def getElementProperties(element_type: int):
        properties = {
            2: ("Triangle 3", 2, 1, 3),
            4: ("Tetrahedron 4", 3, 1, 4),
            6: ("Prism 6", 3, 1, 6),
            7: ("Pyramid 5", 3, 1, 5),
        }
        return properties[element_type]


class _CountingGmsh:
    def __init__(self) -> None:
        self.model = type("Model", (), {})()
        self.model.mesh = _Mesh()


class _Logger:
    def __init__(self) -> None:
        self.started = False

    def start(self) -> None:
        self.started = True

    @staticmethod
    def get():
        return []

    def stop(self) -> None:
        self.started = False


class _LifecycleGmsh:
    __version__ = "mock"
    __file__ = __file__

    def __init__(self, *, fail_initialize_after_state_change: bool = False) -> None:
        self.initialized = False
        self.finalize_count = 0
        self.clear_count = 0
        self.fail_initialize_after_state_change = fail_initialize_after_state_change
        self.logger = _Logger()
        self.model = type("Model", (), {"add": lambda _self, _name: None})()

    def isInitialized(self) -> bool:
        return self.initialized

    def initialize(self, **_kwargs) -> None:
        self.initialized = True
        if self.fail_initialize_after_state_change:
            raise RuntimeError("partial initialize failure")

    def clear(self) -> None:
        self.clear_count += 1

    def finalize(self) -> None:
        self.finalize_count += 1
        self.initialized = False

    @staticmethod
    def write(_path: str) -> None:
        raise AssertionError("projection-only path must never call gmsh.write")


class _ProjectionStrategy:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def build(self, _gmsh, _source, _config, _staging):
        if self.fail:
            raise RuntimeError("strategy failed before projection")
        return {
            "schema": "cfdpipe.coarse_mesh_projection.v1",
            "status": "PASS",
            "scope": {
                "projection_only": True,
                "calibration_PASS_authorized": False,
                "production": False,
            },
            "mesh_written": False,
            "full_layer_subdivision_performed": False,
            "projection_gate": {"status": "PASS"},
        }


class CoarseMeshProjectionTests(unittest.TestCase):
    def test_projection_formula(self) -> None:
        result = evaluate_projected_element_count(
            source_prism_count=100,
            nonprism_type_counts={"Tetrahedron 4": 25, "Pyramid 5": 5},
            requested_layer_count=60,
            maximum_projected_3d_elements=7000,
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["projected_3d_elements"], 6030)
        self.assertEqual(result["nonprism_count"], 30)

    def test_projection_cap_is_strict_and_does_not_authorize_pass(self) -> None:
        result = evaluate_projected_element_count(
            source_prism_count=10,
            nonprism_type_counts={"Tetrahedron 4": 20},
            requested_layer_count=8,
            maximum_projected_3d_elements=100,
        )
        self.assertEqual(result["projected_3d_elements"], 100)
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["strictly_below_cap"])

    def test_counts_all_wall_columns_and_two_core_inventories(self) -> None:
        gmsh = _CountingGmsh()
        fingerprints = {
            tag: f"fingerprint-{tag:02d}" for tag in range(1, 49)
        }
        prism_by_wall = {tag: 100 + tag for tag in fingerprints}
        element_tag = 1
        node_tag = 1
        for wall_tag in fingerprints:
            gmsh.model.mesh.add_block(
                2, wall_tag, 2, [element_tag], [node_tag, node_tag + 1, node_tag + 2]
            )
            element_tag += 1
            node_tag += 3
            gmsh.model.mesh.add_block(
                3,
                prism_by_wall[wall_tag],
                6,
                [element_tag],
                [node_tag + value for value in range(6)],
            )
            element_tag += 1
            node_tag += 6
        gmsh.model.mesh.add_block(
            3,
            501,
            4,
            [element_tag, element_tag + 1, element_tag + 2],
            list(range(node_tag, node_tag + 12)),
        )
        gmsh.model.mesh.add_block(
            3,
            502,
            7,
            [element_tag + 3],
            list(range(node_tag + 12, node_tag + 17)),
        )
        result = summarize_one_layer_projection(
            gmsh,
            wall_fingerprint_by_tag=fingerprints,
            prism_by_wall=prism_by_wall,
            core_volume_tags=[501, 502],
            normalized_config={
                "projection_only": True,
                "contract_mode": "coarse_projection_only",
                "calibration_only": True,
                "production_mesh_eligible": False,
                "wall_surface_fingerprints": list(fingerprints.values()),
                "layer_count": 60,
                "projection_maximum_3d_elements": 3000,
                "construction_first_layer_height_m": 1.0e-6,
                "projection_construction_height_source": (
                    "contract.base_smoke_contract."
                    "construction_first_layer_height_m"
                ),
            },
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["wall_surface_count"], 48)
        self.assertEqual(result["wall_source_column_count"], 48)
        self.assertEqual(len(result["per_wall_surface_columns"]), 48)
        self.assertEqual(result["one_layer_3d_element_count"], 52)
        self.assertEqual(
            result["projection_gate"]["projected_3d_elements"], 4 + 48 * 60
        )
        self.assertEqual(len(result["core_inventories"]), 2)
        self.assertFalse(result["full_layer_subdivision_performed"])
        self.assertFalse(result["scope"]["calibration_PASS_authorized"])
        self.assertEqual(
            result["formal_quality_conclusion"], "NOT_EVALUATED_OR_AUTHORIZED"
        )

    def test_strategy_config_uses_base_smoke_construction_height(self) -> None:
        base_strategy = {
            "normalized_config_sha256": "0" * 64,
            "construction_first_layer_height_m": 0.077887,
            "layer_count": 60,
            "max_3d_elements": 750000,
        }
        contract = {
            "base_smoke_contract": {
                "construction_first_layer_height_m": 1.3822885636660436e-6,
                "max_3d_elements": 300000,
            },
            "boundary_layer_design": {"layer_count": 60},
        }
        with mock.patch(
            "cfdpipe.coarse_mesh_projection.make_coarse_strategy_config",
            return_value=base_strategy,
        ):
            result = make_projection_strategy_config(contract, 0.30)
        self.assertEqual(
            result["construction_first_layer_height_m"],
            contract["base_smoke_contract"]["construction_first_layer_height_m"],
        )
        self.assertNotEqual(result["construction_first_layer_height_m"], 0.077887)
        self.assertEqual(result["projection_maximum_3d_elements"], 300000)
        self.assertTrue(result["projection_only"])

    def test_lifecycle_has_no_write_or_downstream_and_finalizes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.brep"
            source.write_bytes(b"mock brep")
            gmsh = _LifecycleGmsh()
            with (
                mock.patch(
                    "cfdpipe.coarse_mesh_projection.make_projection_strategy_config",
                    return_value={"normalized_config_sha256": "1" * 64},
                ),
                mock.patch(
                    "cfdpipe.coarse_mesh_projection._validate_projection_source",
                    return_value=source,
                ),
            ):
                result = run_coarse_mesh_projection(
                    contract={},
                    characteristic_length_m=0.30,
                    strategy=_ProjectionStrategy(),
                    gmsh_module=gmsh,
                )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(gmsh.finalize_count, 1)
        self.assertEqual(result["external_commands"], [])
        self.assertFalse(result["mesh_written"])
        module_source = inspect.getsource(
            __import__("cfdpipe.coarse_mesh_projection", fromlist=["unused"])
        )
        self.assertNotIn("gmsh.write", module_source)
        self.assertNotIn("subprocess", module_source)

    def test_strategy_exception_retains_traceback_and_finalizes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.brep"
            source.write_bytes(b"mock brep")
            gmsh = _LifecycleGmsh()
            with (
                mock.patch(
                    "cfdpipe.coarse_mesh_projection.make_projection_strategy_config",
                    return_value={"normalized_config_sha256": "1" * 64},
                ),
                mock.patch(
                    "cfdpipe.coarse_mesh_projection._validate_projection_source",
                    return_value=source,
                ),
            ):
                result = run_coarse_mesh_projection(
                    contract={},
                    characteristic_length_m=0.30,
                    strategy=_ProjectionStrategy(fail=True),
                    gmsh_module=gmsh,
                )
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(gmsh.finalize_count, 1)
        self.assertIn("RuntimeError: strategy failed", result["error"]["traceback"])

    def test_partial_initialize_failure_still_finalizes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.brep"
            source.write_bytes(b"mock brep")
            gmsh = _LifecycleGmsh(fail_initialize_after_state_change=True)
            with (
                mock.patch(
                    "cfdpipe.coarse_mesh_projection.make_projection_strategy_config",
                    return_value={"normalized_config_sha256": "1" * 64},
                ),
                mock.patch(
                    "cfdpipe.coarse_mesh_projection._validate_projection_source",
                    return_value=source,
                ),
            ):
                result = run_coarse_mesh_projection(
                    contract={},
                    characteristic_length_m=0.30,
                    strategy=_ProjectionStrategy(),
                    gmsh_module=gmsh,
                )
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(gmsh.finalize_count, 1)
        self.assertTrue(result["gmsh_session"]["finalize_called"])
        self.assertIn("partial initialize failure", result["error"]["traceback"])

    def test_projection_return_precedes_full_subdivision(self) -> None:
        source = inspect.getsource(RealProjectBoundaryLayerStrategy.build)
        generate_index = source.index("gmsh.model.mesh.generate(3)")
        projection_index = source.index('if normalized_config.get("projection_only")')
        return_index = source.index("return projection", projection_index)
        subdivision_index = source.index(
            "_apply_one_layer_orientation_cone_subdivision", return_index
        )
        self.assertLess(generate_index, projection_index)
        self.assertLess(projection_index, return_index)
        self.assertLess(return_index, subdivision_index)


if __name__ == "__main__":
    unittest.main()
