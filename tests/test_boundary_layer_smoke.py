from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import json
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cfdpipe.boundary_layer_smoke import (
    BoundaryLayerSmokeError,
    RealProjectBoundaryLayerStrategy,
    _apply_absolute_fraction_chain_state,
    _call_audit,
    _apply_one_layer_orientation_cone_subdivision,
    _chebyshev_cone_direction,
    _closest_feasible_cone_direction,
    _derive_prism_columns,
    _derive_internal_role_faces,
    _expected_repair_audit_schema,
    _finalize_deferred_gmsh_log_diagnostics,
    _frontier_collar_candidate_schedules,
    _frontier_collar_graph,
    _frontier_component_precheck,
    _gate_subdivision_element_cap,
    _group_repair_source_triangles,
    _configure_quality_improvement_fields,
    _core_tetra_quality_by_region,
    _prism_column_quality_summary,
    _rebuild_owner_free_interaction_topology,
    _nodes_from_gmsh,
    _one_layer_prism_topology,
    _owner_free_quality_objective,
    _owner_free_quality_snapshot,
    _repair_layer_schedule,
    _reject_fatal_gmsh_log,
    _resolve_fixed_direction_replay_consensus_subgraph,
    _run_physical_schedule_homotopy_scan,
    _run_physical_schedule_first_frontier_direction_continuation,
    _run_owner_free_component_direction_search,
    _stable_root_id,
    _stable_direction_field_sha256,
    _stable_low_quality_prism_lineage,
    _stable_root_schedule_assignments,
    _stable_triangle_id,
    _validated_local_surface_schedules,
    _volume_elements_from_gmsh,
    audit_mixed_mesh,
    build_boundary_layer_smoke,
    normalize_boundary_layer_smoke_config,
    parse_su2_mixed_mesh,
)
from cfdpipe.coarse_component_direction import build_owner_free_components
from cfdpipe.coarse_direction_feasibility import (
    FINE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
    FRONTIER_PATTERN_ENDPOINT_SCHEMA,
    TRIPLE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
    make_owner_free_direction_frontier_collar_refinement_endpoint,
    make_owner_free_direction_endpoint,
    make_owner_free_direction_frontier_fine_refinement_pattern_endpoint,
    make_owner_free_direction_frontier_pattern_endpoint,
    make_owner_free_direction_frontier_triple_refinement_pattern_endpoint,
    make_owner_free_direction_pattern_endpoint,
)
from cfdpipe.coarse_direction_replay import make_physical_schedule_homotopy_endpoint
from cfdpipe.coarse_direction_replay import HOMOTOPY_DISCOVERY_SCHEMA
from cfdpipe.coarse_schedule_continuation import (
    DISCOVERY_SCHEMA as CONTINUATION_DISCOVERY_SCHEMA,
    ENDPOINT_SCHEMA as CONTINUATION_ENDPOINT_SCHEMA,
    REQUEST as CONTINUATION_REQUEST,
)


_HASHES = {
    "project": "1" * 64,
    "cases": "2" * 64,
    "markers": "3" * 64,
    "topology_smoke": "4" * 64,
    "pipeline_geometry": "5" * 64,
}
_FPS = [f"{index:064x}" for index in range(1, 49)]


def _project() -> dict:
    return {"mesh": {"minimum_prism_layers": 15, "initial_growth_ratio": 1.2}}


def _markers() -> dict:
    return {
        "solver_markers": [
            {
                "physical_name": "farfield",
                "kind": "farfield",
                "members": [{"fingerprint_id": "a" * 64}],
            },
            {
                "physical_name": "wall",
                "kind": "wall",
                "members": [
                    {"fingerprint_id": value, "area_m2": 1.0} for value in _FPS
                ],
            },
        ]
    }


def _records(pipeline_hash: str = _HASHES["pipeline_geometry"]) -> dict:
    return {
        name: {"sha256": value}
        for name, value in {**_HASHES, "pipeline_geometry": pipeline_hash}.items()
    }


def _document() -> dict:
    return {
        "schema": "cfdpipe.boundary_layer_smoke.v2",
        "status": "CONFIGURED",
        "policy": {
            "smoke_only": True,
            "production_mesh_eligible": False,
            "run_su2": False,
            "run_paraview": False,
            "runtime_tags_present": False,
            "max_3d_elements": 300000,
        },
        "provenance": {
            "project_sha256": _HASHES["project"],
            "cases_sha256": _HASHES["cases"],
            "markers_sha256": _HASHES["markers"],
            "topology_smoke_sha256": _HASHES["topology_smoke"],
            "pipeline_brep_sha256": _HASHES["pipeline_geometry"],
        },
        "selection": {
            "wall_physical_name": "wall",
            "wall_surface_fingerprints": list(reversed(_FPS)),
        },
        "mesh": {
            "layer_count_source": "project.mesh.minimum_prism_layers",
            "growth_ratio_source": "project.mesh.initial_growth_ratio",
            "element_order": 1,
            "allowed_3d_element_types": [
                "Tetrahedron 4",
                "Prism 6",
                "Pyramid 5",
            ],
            "normal_mode": "implicit",
            "first_layer_height_m": 1.382288564e-6,
            "construction_first_layer_height_m": 2.764577128e-6,
            "characteristic_length_m": 0.25,
            "direction_probe_distance_m": 1.0e-5,
            "recombine": True,
        },
        "post_mesh_repair": {
            "mode": "one_layer_orientation_cone_subdivision_v1",
            "expected_initial_bad_prism_count": 13,
            "expected_negative_volume_prism_count": 6,
            "expected_cone_node_count": 15,
            "expected_full_layer_bad_prism_count": 34,
            "expected_initial_below_threshold_prism_count": 34,
            "expected_bad_source_triangle_count": 6,
            "expected_smoothing_group_count": 4,
            "expected_smoothed_root_count": 10,
            "smoothing_interior_weight": 0.02,
            "minimum_smoothing_margin": 1.0e-4,
            "minimum_cone_margin": 1.0e-5,
            "height_tolerance_m": 1.0e-12,
            "approved_ambiguous_owners": [],
        },
        "quality_improvement": {
            "mode": "stable_surface_threshold_min_v1",
            "base_refinement_source": (
                "config/topology_smoke.toml.local_refinement"
            ),
            "base_excluded_surface_fingerprints": [_FPS[0]],
            "additional_refinement_groups": [
                {
                    "name": "restored_region",
                    "surface_fingerprint_ids": [_FPS[0]],
                    "minimum_size_m": 0.04,
                    "distance_max_m": 0.08,
                    "sampling": 100,
                },
                {
                    "name": "core_interface_regions",
                    "surface_fingerprint_ids": [_FPS[1], _FPS[2]],
                    "minimum_size_m": 0.05,
                    "distance_max_m": 0.08,
                    "sampling": 100,
                },
            ],
            "minimum_core_tetra_gamma": 1.0e-3,
            "maximum_core_tetra_below_gamma_count": 0,
            "minimum_prism_scaled_jacobian": 1.0e-2,
            "maximum_prism_below_threshold_element_count": 0,
            "maximum_prism_below_threshold_column_count": 0,
            "core_tetra_meshing_algorithm": 10,
            "core_tetra_meshing_algorithm_name": "HXT",
            "scoped_optimizer_policy": "disabled_no_safe_entity_scope",
            "scoped_optimizer_evidence": (
                "Gmsh 4.15.2: optimization of specified model entities is not interfaced"
            ),
        },
    }


def _normalized() -> dict:
    return normalize_boundary_layer_smoke_config(
        _document(),
        project=_project(),
        marker_config=_markers(),
        input_records=_records(),
    )


def _refresh_normalized_hash(config: dict) -> None:
    unsigned = copy.deepcopy(config)
    unsigned.pop("normalized_config_sha256", None)
    payload = json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    config["normalized_config_sha256"] = hashlib.sha256(payload).hexdigest()


def _mixed_payload() -> dict:
    # Three disconnected, valid linear cells are sufficient for the pure type
    # and ownership audit.  Every exterior face is assigned to one marker.
    nodes = {
        index: (float(index), float(index % 3), float(index % 5))
        for index in range(1, 16)
    }
    elements = [
        {"tag": 1, "type": "Prism 6", "nodes": [1, 2, 3, 4, 5, 6], "region": "fluid_1"},
        {"tag": 2, "type": "Tetrahedron 4", "nodes": [7, 8, 9, 10], "region": "fluid_1"},
        {"tag": 3, "type": "Pyramid 5", "nodes": [11, 12, 13, 14, 15], "region": "fluid_2"},
        # Two tetrahedra share face 7/8/9 and prove the distinct-region interface.
        {"tag": 4, "type": "Tetrahedron 4", "nodes": [7, 8, 9, 11], "region": "fluid_2"},
    ]
    # Derive exterior faces with the module's public contract expressed as raw
    # connectivity.  The shared 7/8/9 face is excluded from markers.
    all_faces = [
        [7, 8, 10], [8, 9, 10], [9, 7, 10],
        [11, 14, 13, 12], [11, 12, 15], [12, 13, 15], [13, 14, 15], [14, 11, 15],
        [7, 8, 11], [8, 9, 11], [9, 7, 11],
    ]
    quality = {
        tag: {
            "volume": 0.1,
            "minDetJac": 0.01,
            "minSJ": 0.5,
            "minSICN": 0.4,
        }
        for tag in range(1, 5)
    }
    # The pure layer audit requires every prism in one 15-layer column.  Expand
    # the single prism into 15 tags while preserving the other mixed types.
    prism_template = elements.pop(0)
    prism_nodes = []
    for layer in range(15):
        base = 20 + layer * 6
        nodes.update({base + i: (float(base + i), 0.0, float(layer)) for i in range(6)})
        tag = 100 + layer
        prism_nodes.append(tag)
        elements.append({"tag": tag, "type": "Prism 6", "nodes": [base + i for i in range(6)], "region": "fluid_1"})
        quality[tag] = dict(quality[1])
        all_faces.extend([
            [base, base + 1, base + 2], [base + 3, base + 4, base + 5],
            [base, base + 3, base + 4, base + 1],
            [base + 1, base + 4, base + 5, base + 2],
            [base + 2, base + 5, base + 3, base],
        ])
    quality.pop(1)
    return {
        "nodes": nodes,
        "elements": elements,
        "marker_faces": {"farfield": all_faces[:8], "wall": all_faces[8:]},
        "shared_interface_faces": [[7, 8, 9]],
        "quality": quality,
        "wall_columns": {_FPS[0]: [prism_nodes]},
        "collar_faces": {fp: [[1, 2, 3]] for fp in _FPS[1:]},
    }


class _Logger:
    def start(self): pass
    def get(self): return ["Info : fake"]
    def stop(self): pass


class _Model:
    def add(self, name): pass


class _FailingGmsh:
    __version__ = "4.15.2-fake"
    __file__ = __file__

    def __init__(self):
        self.initialized = False
        self.finalized = False
        self.logger = _Logger()
        self.model = _Model()

    def isInitialized(self): return self.initialized
    def initialize(self, readConfigFiles=False): self.initialized = True
    def finalize(self):
        self.finalized = True
        self.initialized = False


class _FailingStrategy:
    def build(self, gmsh, source, config, staging):
        raise RuntimeError("injected core reconstruction failed")

    def readback(self, gmsh, mesh_path, config):
        raise AssertionError("readback must not run")

    def evidence(self, phase):
        if phase == "build":
            return {"direction_audit": {"surface": "complete traceback path"}}
        return {}


class _ExtrudeGeo:
    def __init__(self):
        self.calls = []

    def extrudeBoundaryLayer(self, dimtags, num_elements, heights, recombine):
        self.calls.append((dimtags, num_elements, heights, recombine))
        return [(2, 1001), (3, 2001)]


class _ExtrudeModel:
    def __init__(self):
        self.geo = _ExtrudeGeo()


class _ExtrudeGmsh:
    def __init__(self):
        self.model = _ExtrudeModel()


class _OfficialFaceMesh:
    def __init__(self):
        self.face_calls = []

    def getElements(self, dimension, entity):
        self.request = (dimension, entity)
        return [4], [[101]], [[1, 2, 3, 4]]

    def getElementProperties(self, element_type):
        return "Tetrahedron 4", 3, 1, 4, [], 4

    def getElementFaceNodes(self, element_type, face_nodes, entity, primary=False):
        self.face_calls.append((element_type, face_nodes, entity, primary))
        if face_nodes == 3:
            return [1, 3, 2, 1, 2, 4, 2, 3, 4, 3, 1, 4]
        return []


class _OfficialFaceModel:
    def __init__(self):
        self.mesh = _OfficialFaceMesh()


class _OfficialFaceGmsh:
    def __init__(self):
        self.model = _OfficialFaceModel()


class _OneLayerTopologyMesh:
    def __init__(self, *, nonbijective=False):
        self.nonbijective = nonbijective

    def getElements(self, dimension, entity):
        if (dimension, entity) == (3, 7):
            # Deliberately interleave base/top nodes: the topology helper must
            # not assume that raw slots 0:3 and 3:6 define the two triangles.
            return [6], [[101]], [[4, 1, 5, 2, 6, 3]]
        if (dimension, entity) == (2, 10):
            return [2], [[201]], [[1, 2, 3]]
        if (dimension, entity) == (2, 11):
            return [2], [[202]], [[4, 5, 6]]
        return [], [], []

    def getElementProperties(self, element_type):
        if element_type == 6:
            return "Prism 6", 3, 1, 6, [], 6
        if element_type == 2:
            return "Triangle 3", 2, 1, 3, [], 3
        raise AssertionError(f"unexpected element type {element_type}")

    def getElementFaceNodes(self, element_type, face_nodes, entity, primary=False):
        if not primary or element_type != 6 or entity != 7:
            raise AssertionError("official primary Prism6 faces were not requested")
        if face_nodes == 3:
            return [1, 2, 3, 4, 5, 6]
        if not self.nonbijective:
            return [
                1, 4, 5, 2,
                2, 5, 6, 3,
                3, 6, 4, 1,
            ]
        return [
            1, 4, 5, 2,
            1, 5, 6, 3,
            2, 4, 6, 3,
        ]


class _OneLayerTopologyGmsh:
    def __init__(self, *, nonbijective=False):
        self.model = SimpleNamespace(
            mesh=_OneLayerTopologyMesh(nonbijective=nonbijective)
        )


class _CapMesh(_OfficialFaceMesh):
    def getElements(self, dimension, entity):
        return [4], [[101, 102]], [[1, 2, 3, 4, 1, 2, 3, 5]]

    def getElementFaceNodes(self, *args, **kwargs):
        raise AssertionError("face extraction must not run after cap failure")


class _CapGmsh:
    def __init__(self):
        self.model = SimpleNamespace(mesh=_CapMesh())


class _ReadbackPhysicalModel:
    def getEntities(self, dimension):
        return [(3, 1), (3, 2), (3, 3)] if dimension == 3 else []

    def getPhysicalGroups(self):
        return []


class _ReadbackPhysicalGmsh:
    def __init__(self):
        self.model = _ReadbackPhysicalModel()


class _GlobalNodeMesh:
    def __init__(self, duplicate=False):
        self.duplicate = duplicate
        self.called = False

    def getNodes(self):
        self.called = True
        if self.duplicate:
            return [1, 1], [0.0, 0.0, 0.0, 1.0, 0.0, 0.0], []
        return [1, 2], [0.0, 0.0, 0.0, 1.0, 0.0, 0.0], []


class _GlobalNodeGmsh:
    def __init__(self, duplicate=False):
        self.model = SimpleNamespace(mesh=_GlobalNodeMesh(duplicate))


class _FieldApi:
    def __init__(self):
        self.add_calls = []
        self.number_lists = []
        self.background = []

    def add(self, kind):
        self.add_calls.append(kind)
        return 90 + len(self.add_calls)

    def setNumbers(self, tag, name, values):
        self.number_lists.append((tag, name, list(values)))

    def setAsBackgroundMesh(self, tag):
        self.background.append(tag)


class _FieldGmsh:
    def __init__(self):
        self.field = _FieldApi()
        self.model = SimpleNamespace(mesh=SimpleNamespace(field=self.field))


class _QualityMesh:
    def __init__(self, *, low_core=False, low_prism=False):
        self.low_core = low_core
        self.low_prism = low_prism

    def getNodes(self):
        coordinates = []
        for tag in range(1, 9):
            coordinates.extend((float(tag), float(tag % 3), float(tag % 5)))
        return list(range(1, 9)), coordinates, []

    def getElements(self, dimension, entity):
        if dimension == 3 and entity == 1:
            return [4], [[101]], [[1, 2, 3, 4]]
        if dimension == 3 and entity == 2:
            return [4], [[102]], [[5, 6, 7, 8]]
        return [], [], []

    def getElementProperties(self, element_type):
        if element_type != 4:
            raise AssertionError("unexpected quality-test element type")
        return "Tetrahedron 4", 3, 1, 4, [], 4

    def getElementQualities(self, tags, metric):
        values = []
        for tag in tags:
            if metric == "gamma":
                values.append(
                    5.0e-4 if self.low_core and int(tag) == 101 else 0.2
                )
            elif metric == "minSJ" and int(tag) >= 201:
                values.append(
                    5.0e-3 if self.low_prism and int(tag) == 201 else 0.2
                )
            else:
                values.append(0.2)
        return values


class _QualityGmsh:
    def __init__(self, *, low_core=False, low_prism=False):
        self.model = SimpleNamespace(
            mesh=_QualityMesh(low_core=low_core, low_prism=low_prism)
        )


class _HomotopyMesh:
    def __init__(self, coordinates, prism_tags, core_tag):
        self.coordinates = {
            int(tag): tuple(float(value) for value in values)
            for tag, values in coordinates.items()
        }
        self.parameters = {int(tag): [float(tag) / 1000.0] for tag in coordinates}
        self.prism_tags = {int(tag) for tag in prism_tags}
        self.core_tag = int(core_tag)
        self.get_node_count = 0
        self.set_node_count = 0

    def getNode(self, tag):
        self.get_node_count += 1
        return self.coordinates[int(tag)], list(self.parameters[int(tag)])

    def setNode(self, tag, coordinates, parameters):
        self.set_node_count += 1
        self.coordinates[int(tag)] = tuple(float(value) for value in coordinates)
        self.parameters[int(tag)] = [float(value) for value in parameters]

    def getNodes(self):
        tags = sorted(self.coordinates)
        flattened = [
            value for tag in tags for value in self.coordinates[int(tag)]
        ]
        return tags, flattened, []

    def getElementQualities(self, tags, metric):
        # The full physical endpoint has a 0.29 m outer height; the growth=1
        # endpoint has 0.15 m.  Only the former intentionally fails minSJ.
        outer_height = max(value[2] for value in self.coordinates.values())
        low = outer_height > 0.25
        values = []
        for tag in tags:
            tag = int(tag)
            if metric == "gamma":
                values.append(0.002)
            elif metric == "minSJ" and tag in self.prism_tags:
                values.append(0.005 if low and tag == max(self.prism_tags) else 0.02)
            else:
                values.append(0.02)
        return values


class _HomotopyGmsh:
    def __init__(self, coordinates, prism_tags, core_tag):
        self.model = SimpleNamespace(
            mesh=_HomotopyMesh(coordinates, prism_tags, core_tag)
        )


class _FineSearchMesh(_HomotopyMesh):
    def __init__(self, coordinates, prism_tags, core_tag, failing_prism_tags):
        super().__init__(coordinates, prism_tags, core_tag)
        self.failing_prism_tags = {int(value) for value in failing_prism_tags}

    def getElementQualities(self, tags, metric):
        if metric == "minSJ":
            return [
                0.005 if int(tag) in self.failing_prism_tags else 0.02
                for tag in tags
            ]
        if metric == "gamma":
            return [0.002 for _tag in tags]
        return [0.02 for _tag in tags]


class _FineSearchGmsh:
    def __init__(self, coordinates, prism_tags, core_tag, failing_prism_tags):
        self.model = SimpleNamespace(
            mesh=_FineSearchMesh(
                coordinates,
                prism_tags,
                core_tag,
                failing_prism_tags,
            )
        )


class BoundaryLayerSmokeTests(unittest.TestCase):
    def test_production_replay_uses_coarse_mixed_mesh_audit_contract(self) -> None:
        payload = {
            name: {}
            for name in (
                "nodes",
                "elements",
                "marker_faces",
                "shared_interface_faces",
                "quality",
                "wall_columns",
                "collar_faces",
                "wall_base_faces",
                "prism_base_faces",
                "collar_base_faces",
                "role_faces",
            )
        }
        config = {
            "wall_surface_fingerprints": [],
            "solver_marker_names": [],
            "layer_count": 60,
            "max_3d_elements": 5_500_000,
            "contract_mode": "coarse_repair_audit_only",
            "production_replay_after_audit": True,
        }
        with patch(
            "cfdpipe.boundary_layer_smoke.audit_mixed_mesh",
            return_value={"status": "PASS"},
        ) as mocked:
            self.assertEqual(_call_audit(payload, config), {"status": "PASS"})
        self.assertEqual(mocked.call_args.kwargs["contract_mode"], "coarse_calibration")

    def test_uniform_smoke_schedule_covers_every_stable_wall(self) -> None:
        config = {
            "smoke_only": True,
            "production_mesh_eligible": False,
            "su2_called": False,
            "paraview_called": False,
            "wall_surface_fingerprints": list(_FPS),
            "layer_count": 15,
            "first_layer_height_m": 0.01,
            "growth_ratio": 1.2,
        }

        schedules, evidence = _validated_local_surface_schedules(config)

        self.assertEqual(set(_FPS), set(schedules))
        self.assertTrue(all(len(values) == 15 for values in schedules.values()))
        self.assertTrue(all(values[0] == 0.01 for values in schedules.values()))
        self.assertEqual("PASS", evidence["status"])
        self.assertEqual("uniform_design_stack", evidence["schedule_mode"])
        self.assertEqual(48, evidence["surface_count"])

        unsafe = {**config, "production_mesh_eligible": True}
        with self.assertRaisesRegex(
            BoundaryLayerSmokeError, "complete smoke safety scope"
        ):
            _validated_local_surface_schedules(unsafe)

    @staticmethod
    def _fine_quality_snapshot(*, failing_prism_tags, **kwargs):
        prism_tags = [int(value) for value in kwargs["prism_element_tags"]]
        core_tags = [int(value) for value in kwargs["core_element_tags"]]
        tetra_tags = [
            int(value) for value in kwargs["core_tetra_element_tags"]
        ]
        below_count = sum(tag in failing_prism_tags for tag in prism_tags)
        minimum_sj = 0.005 if below_count else 0.02
        prism_deficit = max(0.0, 0.01 - minimum_sj)
        unsigned = {
            "status": "FAIL" if below_count else "PASS",
            "minimum_prism_scaled_jacobian_for_pass": 0.01,
            "minimum_core_tetra_gamma_for_pass": 0.001,
            "all_values_finite": True,
            "nonfinite_count": 0,
            "nonpositive_element_count": 0,
            "prism_below_threshold_element_count": below_count,
            "core_tetra_below_gamma_count": 0,
            "minimum_prism_scaled_jacobian": minimum_sj,
            "minimum_core_tetra_gamma": 0.002,
            "prism_element_count": len(prism_tags),
            "core_element_count": len(core_tags),
            "core_tetra_count": len(tetra_tags),
            "maximum_prism_scaled_jacobian_deficit": prism_deficit,
            "prism_scaled_jacobian_deficit_l1": prism_deficit * below_count,
            "prism_scaled_jacobian_deficit_l2": (
                prism_deficit * prism_deficit * below_count
            ),
            "maximum_core_tetra_gamma_deficit": 0.0,
            "core_tetra_gamma_deficit_l2": 0.0,
        }
        payload = json.dumps(
            unsigned,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
        return {
            **unsigned,
            "quality_sha256": hashlib.sha256(payload).hexdigest(),
        }

    @staticmethod
    def _fine_search_fixture(*, linked_components=False):
        if linked_components:
            triangles = [(1, 2, 3), (3, 4, 5), (6, 7, 8)]
        else:
            # Frozen continuation scope: C=3, R=12, T=5.  The third
            # component contains three shared-root triangles and six roots.
            triangles = [
                (1, 2, 3),
                (4, 5, 6),
                (7, 8, 9),
                (9, 10, 11),
                (7, 11, 12),
            ]
        roots = sorted({root for triangle in triangles for root in triangle})
        origins = {
            root: (float(root), float(root % 3), 0.0) for root in roots
        }
        chains = {root: [root, root + 100] for root in roots}
        coordinates = {
            **origins,
            **{
                root + 100: (
                    origins[root][0],
                    origins[root][1],
                    0.1,
                )
                for root in roots
            },
        }
        subdivided_prisms = []
        core_volume_records = []
        prism_volume_fingerprints = {}
        triangle_walls = {}
        for index, triangle in enumerate(triangles, start=1):
            wall = f"{index + 20:064x}"
            prism_tag = 100 + index
            entity = 700 + index
            fixed_node = 1000 + index
            coordinates[fixed_node] = (float(index), -1.0, 0.2)
            subdivided_prisms.append(
                {
                    "tag": prism_tag,
                    "entity": entity,
                    "type": "Prism 6",
                    "nodes": [
                        *triangle,
                        *(root + 100 for root in triangle),
                    ],
                }
            )
            core_volume_records.append(
                {
                    "tag": 200 + index,
                    "entity": 800 + index,
                    "type": "Tetrahedron 4",
                    "nodes": [
                        *(root + 100 for root in triangle),
                        fixed_node,
                    ],
                }
            )
            prism_volume_fingerprints[entity] = wall
            triangle_walls[triangle] = wall
        triangle_ids = {
            triangle: _stable_triangle_id(triangle, origins)
            for triangle in triangles
        }
        components, stable_components = build_owner_free_components(
            triangles,
            triangle_stable_ids=triangle_ids,
            triangle_wall_fingerprints=triangle_walls,
            root_coordinates=origins,
        )
        return {
            "triangles": triangles,
            "roots": roots,
            "origins": origins,
            "chains": chains,
            "coordinates": coordinates,
            "subdivided_prisms": subdivided_prisms,
            "core_volume_records": core_volume_records,
            "prism_volume_fingerprints": prism_volume_fingerprints,
            "triangle_walls": triangle_walls,
            "triangle_ids": triangle_ids,
            "components": components,
            "stable_components": stable_components,
        }

    def _run_fine_search(
        self,
        *,
        failing_prism_tags,
        linked_components=False,
        triple=False,
    ):
        fixture = self._fine_search_fixture(
            linked_components=linked_components
        )
        gmsh = _FineSearchGmsh(
            fixture["coordinates"],
            [record["tag"] for record in fixture["subdivided_prisms"]],
            fixture["core_volume_records"][0]["tag"],
            failing_prism_tags,
        )
        endpoint_factory = (
            make_owner_free_direction_frontier_triple_refinement_pattern_endpoint
            if triple
            else make_owner_free_direction_frontier_fine_refinement_pattern_endpoint
        )
        endpoint = endpoint_factory(schedule_endpoint_sha256="f" * 64)
        quality_calls = []
        quality_coordinate_states = []

        def quality_side_effect(_gmsh, **kwargs):
            quality_calls.append(
                tuple(sorted(int(value) for value in kwargs["prism_element_tags"]))
            )
            quality_coordinate_states.append(
                dict(gmsh.model.mesh.coordinates)
            )
            return self._fine_quality_snapshot(
                failing_prism_tags={int(value) for value in failing_prism_tags},
                **kwargs,
            )

        tangent_call_quality_counts = []
        real_cos = __import__("math").cos

        def cos_side_effect(value):
            tangent_call_quality_counts.append(len(quality_calls))
            return real_cos(value)

        result = None
        captured_error = None
        with patch(
            "cfdpipe.boundary_layer_smoke._owner_free_quality_snapshot",
            side_effect=quality_side_effect,
        ), patch(
            "cfdpipe.boundary_layer_smoke.math.cos",
            side_effect=cos_side_effect,
        ) as cos_mock:
            try:
                result = _run_owner_free_component_direction_search(
                    gmsh,
                    components=fixture["components"],
                    stable_components=fixture["stable_components"],
                    bad_source_triangles=fixture["triangles"],
                    triangle_stable_ids=fixture["triangle_ids"],
                    triangle_wall_fingerprints=fixture["triangle_walls"],
                    prism_volume_fingerprints=fixture[
                        "prism_volume_fingerprints"
                    ],
                    source_triangle_normals={
                        triangle: (0.0, 0.0, 1.0)
                        for triangle in fixture["triangles"]
                    },
                    incident_normals={
                        root: [(0.0, 0.0, 1.0)]
                        for root in fixture["roots"]
                    },
                    original_directions={
                        root: (0.0, 0.0, 1.0)
                        for root in fixture["roots"]
                    },
                    root_coordinates=fixture["origins"],
                    chains=fixture["chains"],
                    root_cumulative_heights={
                        root: [0.1] for root in fixture["roots"]
                    },
                    subdivided_prisms=fixture["subdivided_prisms"],
                    core_volume_records=fixture["core_volume_records"],
                    minimum_prism_scaled_jacobian=0.01,
                    minimum_core_tetra_gamma=0.001,
                    minimum_cone_margin=1.0e-5,
                    minimum_changed_direction_margin=1.0e-4,
                    endpoint=endpoint,
                )
            except BoundaryLayerSmokeError as error:
                captured_error = error
        return {
            "result": result,
            "error": captured_error,
            "fixture": fixture,
            "endpoint": endpoint,
            "quality_calls": quality_calls,
            "quality_coordinate_states": quality_coordinate_states,
            "tangent_call_quality_counts": tangent_call_quality_counts,
            "cos_call_count": cos_mock.call_count,
        }

    def test_fine_two_phase_zero_failures_skips_all_tangent_calls(self):
        run = self._run_fine_search(failing_prism_tags=set())
        self.assertIsNone(run["error"])
        search, final_quality, _selected = run["result"]
        selection = search["fine_refinement_selection"]
        self.assertEqual(search["endpoint_sha256"], run["endpoint"]["endpoint_sha256"])
        self.assertEqual(final_quality["status"], "PASS")
        self.assertEqual(selection["gate"]["static_failing_component_count"], 0)
        self.assertFalse(selection["gate"]["fine_refinement_performed"])
        self.assertIsNone(selection["selected_component"])
        self.assertIsNone(selection["prefix_checkpoint"])
        self.assertEqual(selection["order"]["fine_component_sha256_order"], [])
        self.assertEqual(run["cos_call_count"], 0)
        self.assertFalse(search["tangent_pattern_refinement"]["performed"])
        self.assertEqual(
            search["tangent_pattern_refinement"]["steps_radians"],
            [2.0**-exponent for exponent in range(1, 13)],
        )
        self.assertEqual(
            search["tangent_pattern_refinement"]["quality_evaluation_count"],
            0,
        )
        self.assertEqual(len(search["component_records"]), 3)
        self.assertTrue(
            all(
                record["static_exit_status"] == "PASS"
                for record in search["component_records"]
            )
        )

    def test_fine_two_phase_orders_all_static_exits_before_single_component_fine(self):
        run = self._run_fine_search(failing_prism_tags={102})
        self.assertIsNone(run["error"])
        search, final_quality, _selected = run["result"]
        selection = search["fine_refinement_selection"]
        static_indices = [
            record["static_exit_quality_evaluation_index"]
            for record in search["component_records"]
        ]
        first_fine = selection["order"][
            "first_fine_quality_evaluation_index"
        ]
        self.assertEqual(final_quality["status"], "FAIL")
        self.assertEqual(selection["gate"]["static_failing_component_count"], 1)
        self.assertTrue(selection["gate"]["fine_refinement_performed"])
        self.assertTrue(selection["gate"]["static_search_started_from_fresh_a"])
        self.assertTrue(
            selection["gate"]["static_all_components_complete_before_fine"]
        )
        self.assertTrue(all(index < first_fine for index in static_indices))
        self.assertEqual(
            selection["order"]["last_static_quality_evaluation_index"],
            max(static_indices),
        )
        self.assertGreater(run["cos_call_count"], 0)
        self.assertEqual(
            run["tangent_call_quality_counts"][0],
            selection["evaluation_evidence"]["static_quality_evaluation_count"],
        )
        selected = selection["selected_component"]
        self.assertEqual(selected["candidate_root_count"], 3)
        self.assertEqual(selected["source_triangle_count"], 1)
        self.assertEqual(selected["unique_source_triangle_edge_count"], 3)
        self.assertEqual(
            selection["order"]["fine_component_sha256_order"],
            [selected["interaction_component_sha256"]],
        )
        prefix = selection["prefix_checkpoint"]
        self.assertEqual(
            prefix["prefix_steps_radians"],
            [2.0**-exponent for exponent in range(1, 6)],
        )
        self.assertEqual(
            selection["order"]["tail_steps_radians"],
            [2.0**-exponent for exponent in range(6, 13)],
        )
        self.assertTrue(
            selection["order"]["tail_started_from_prefix_checkpoint"]
        )
        for order_field, prefix_field in (
            (
                "tail_start_coordinate_sha256",
                "selected_state_coordinate_sha256",
            ),
            (
                "tail_start_direction_state_sha256",
                "selected_direction_state_sha256",
            ),
            (
                "tail_start_direction_field_sha256",
                "selected_direction_field_sha256",
            ),
            ("tail_start_quality_sha256", "selected_state_quality_sha256"),
        ):
            self.assertEqual(
                selection["order"][order_field], prefix[prefix_field]
            )
        evaluations = selection["evaluation_evidence"]
        self.assertEqual(search["component_count"], 3)
        self.assertEqual(search["candidate_root_count"], 12)
        self.assertEqual(
            sum(
                record["source_triangle_count"]
                for record in search["component_records"]
            ),
            5,
        )
        self.assertEqual(evaluations["endpoint_maximum_quality_evaluations"], 4096)
        self.assertEqual(
            evaluations["global_conservative_quality_evaluation_cap"], 2187
        )
        self.assertLessEqual(evaluations["total_quality_evaluation_count"], 2187)
        self.assertLessEqual(evaluations["static_quality_evaluation_count"], 738)
        self.assertLessEqual(evaluations["fine_quality_evaluation_count"], 1440)
        self.assertEqual(
            evaluations["total_quality_evaluation_count"]
            - evaluations["static_quality_evaluation_count"]
            - evaluations["fine_quality_evaluation_count"],
            9,
        )
        self.assertEqual(
            evaluations["total_quality_evaluation_count"],
            search["quality_evaluation_count"],
        )

    def test_fine_two_phase_multiple_static_failures_stop_before_tangent(self):
        run = self._run_fine_search(failing_prism_tags={101, 102})
        self.assertIsNone(run["result"])
        self.assertIsInstance(run["error"], BoundaryLayerSmokeError)
        self.assertIn("static failure gate", str(run["error"]))
        self.assertEqual(run["cos_call_count"], 0)
        observed_local_components = {
            tags for tags in run["quality_calls"] if len(tags) in {1, 3}
        }
        self.assertTrue(
            {(101,), (102,), (103, 104, 105)} <= observed_local_components
        )

    def test_fine_two_phase_wrong_shape_stops_before_tangent(self):
        run = self._run_fine_search(
            failing_prism_tags={101}, linked_components=True
        )
        self.assertIsNone(run["result"])
        self.assertIsInstance(run["error"], BoundaryLayerSmokeError)
        self.assertIn("wrong shape", str(run["error"]))
        self.assertEqual(run["cos_call_count"], 0)
        self.assertIn((101, 102), run["quality_calls"])
        self.assertIn((103,), run["quality_calls"])

    def test_triple_zero_failures_skips_fine_and_atomic_refinement(self):
        run = self._run_fine_search(
            failing_prism_tags=set(), triple=True
        )
        self.assertIsNone(run["error"])
        search, final_quality, _selected = run["result"]
        selection = search["triple_refinement_selection"]
        summary = search["triple_pattern_refinement"]
        self.assertEqual(final_quality["status"], "PASS")
        self.assertFalse(
            selection["gate"]["fine_refinement_performed"]
        )
        self.assertFalse(
            selection["gate"]["triple_refinement_performed"]
        )
        self.assertIsNone(selection["handoff"])
        self.assertIsNone(selection["selected_component"])
        self.assertIsNone(selection["final_state"])
        self.assertEqual(selection["step_pass_records"], [])
        self.assertFalse(summary["performed"])
        self.assertEqual(summary["quality_evaluation_count"], 0)
        self.assertEqual(summary["accepted_move_count"], 0)
        fine_total = search["fine_refinement_selection"][
            "evaluation_evidence"
        ]["total_quality_evaluation_count"]
        total = selection["evaluation_evidence"][
            "total_quality_evaluation_count"
        ]
        self.assertEqual(
            fine_total,
            selection["evaluation_evidence"][
                "through_fine_handoff_quality_evaluation_count"
            ],
        )
        self.assertEqual(total - fine_total, 9)

    def test_triple_refinement_is_atomic_continuous_and_bounded(self):
        run = self._run_fine_search(
            failing_prism_tags={102}, triple=True
        )
        self.assertIsNone(run["error"])
        search, final_quality, _selected = run["result"]
        self.assertEqual(final_quality["status"], "FAIL")
        self.assertEqual(
            run["endpoint"]["schema"],
            TRIPLE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
        )
        selection = search["triple_refinement_selection"]
        summary = search["triple_pattern_refinement"]
        handoff = selection["handoff"]
        evaluations = selection["evaluation_evidence"]
        records = selection["step_pass_records"]
        self.assertTrue(
            selection["gate"]["single_and_pair_phases_complete_before_triple"]
        )
        self.assertTrue(
            selection["gate"]["triple_refinement_performed"]
        )
        self.assertTrue(handoff["without_reset"])
        for suffix in (
            "coordinate_sha256",
            "quality_sha256",
            "objective_sha256",
            "direction_state_sha256",
            "direction_field_sha256",
        ):
            self.assertEqual(
                handoff[f"fine_exit_{suffix}"],
                handoff[f"triple_entry_{suffix}"],
            )
        self.assertEqual(
            handoff["fine_exit_quality"]["quality_sha256"],
            handoff["fine_exit_quality_sha256"],
        )
        expected_objective_hash = hashlib.sha256(
            json.dumps(
                {
                    "quality_objective_fields": run["endpoint"][
                        "quality_objective"
                    ],
                    "objective": handoff["fine_exit_objective"],
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("ascii")
        ).hexdigest()
        self.assertEqual(
            handoff["fine_exit_objective_sha256"],
            expected_objective_hash,
        )
        selected_record = next(
            record
            for record in search["component_records"]
            if record["interaction_component_sha256"]
            == handoff["selected_component_sha256"]
        )
        fine_checkpoint = selected_record["fine_exit_checkpoint"]
        self.assertEqual(
            set(fine_checkpoint),
            {
                "selected_state_coordinate_sha256",
                "selected_state_quality",
                "selected_state_quality_sha256",
                "selected_state_objective",
                "selected_state_objective_sha256",
                "selected_direction_state_sha256",
                "selected_direction_field_sha256",
                "selected_directions",
                "fine_exit_quality_evaluation_index",
            },
        )
        self.assertEqual(
            fine_checkpoint["selected_state_quality_sha256"],
            handoff["fine_exit_quality_sha256"],
        )
        self.assertEqual(
            fine_checkpoint["selected_state_objective_sha256"],
            handoff["fine_exit_objective_sha256"],
        )
        self.assertEqual(len(fine_checkpoint["selected_directions"]), 3)
        self.assertEqual(
            hashlib.sha256(
                json.dumps(
                    {"records": fine_checkpoint["selected_directions"]},
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("ascii")
            ).hexdigest(),
            fine_checkpoint["selected_direction_field_sha256"],
        )
        runner_source = inspect.getsource(
            _run_owner_free_component_direction_search
        )
        self.assertNotIn(
            "source_triangle_triple_tangent_pattern", runner_source
        )
        self.assertIn('"tangent_pattern"', runner_source)
        self.assertIn("tangent_pattern", run["endpoint"]["candidate_sources"])
        self.assertEqual(
            handoff["fine_exit_quality_evaluation_index"],
            handoff["triple_quality_evaluation_start_index_exclusive"],
        )
        self.assertEqual(
            evaluations["first_triple_quality_evaluation_index"],
            handoff["fine_exit_quality_evaluation_index"] + 1,
        )
        self.assertEqual(len(records), 24)
        self.assertEqual(summary["step_pass_records"], records)
        self.assertEqual(summary["quality_evaluation_count"], 1536)
        self.assertEqual(
            evaluations["triple_quality_evaluation_count"], 1536
        )
        self.assertLessEqual(
            evaluations["total_quality_evaluation_count"], 3723
        )
        previous_end = handoff[
            "triple_quality_evaluation_start_index_exclusive"
        ]
        previous_exit = None
        for expected_index, record in enumerate(records):
            self.assertEqual(
                record["step_index_1_based"], expected_index // 2 + 1
            )
            self.assertEqual(
                record["pass_index_1_based"], expected_index % 2 + 1
            )
            self.assertEqual(
                record["quality_evaluation_start_index_exclusive"],
                previous_end,
            )
            self.assertEqual(record["candidate_upper_bound"], 64)
            self.assertEqual(record["atomic_candidate_count"], 64)
            self.assertEqual(record["quality_evaluation_count"], 64)
            self.assertEqual(
                record["atomic_candidate_count"],
                __import__("math").prod(
                    item["candidate_count"]
                    for item in record["root_candidate_counts"]
                ),
            )
            self.assertTrue(
                all(
                    item["candidate_count"] <= 4
                    for item in record["root_candidate_counts"]
                )
            )
            if previous_exit is not None:
                self.assertEqual(
                    record["entry_coordinate_sha256"], previous_exit
                )
            previous_exit = record["exit_coordinate_sha256"]
            previous_end = record[
                "quality_evaluation_end_index_inclusive"
            ]
        self.assertEqual(
            previous_end,
            evaluations["triple_quality_evaluation_end_index_inclusive"],
        )
        self.assertEqual(
            selection["final_state"][
                "selected_state_coordinate_sha256"
            ],
            records[-1]["exit_coordinate_sha256"],
        )

        # The first atomic pass starts from the unmodified fine state in this
        # constant-quality fixture.  Every evaluated triple candidate changes
        # all three roots before the single quality call; no one-/two-root
        # partial candidate is ever evaluated by the triple phase.
        selected_root_hashes = set(
            selection["selected_component"]["root_coordinate_sha256"]
        )
        fixture = run["fixture"]
        selected_roots = [
            root
            for root in fixture["roots"]
            if _stable_root_id(fixture["origins"][root])
            in selected_root_hashes
        ]
        first_record = records[0]
        first_index = first_record["first_quality_evaluation_index"]
        last_index = first_record["last_quality_evaluation_index"]
        for state in run["quality_coordinate_states"][
            first_index - 1 : last_index
        ]:
            for root in selected_roots:
                baseline = (
                    fixture["origins"][root][0],
                    fixture["origins"][root][1],
                    0.1,
                )
                self.assertNotEqual(state[root + 100], baseline)

    def test_triple_scope_failures_stop_before_any_tangent_evaluation(self):
        multiple = self._run_fine_search(
            failing_prism_tags={101, 102}, triple=True
        )
        self.assertIsNone(multiple["result"])
        self.assertIn("static failure gate", str(multiple["error"]))
        self.assertEqual(multiple["cos_call_count"], 0)

        wrong_shape = self._run_fine_search(
            failing_prism_tags={101}, linked_components=True, triple=True
        )
        self.assertIsNone(wrong_shape["result"])
        self.assertIn("wrong shape", str(wrong_shape["error"]))
        self.assertEqual(wrong_shape["cos_call_count"], 0)

    def test_fine_endpoint_remains_count_first_and_old_endpoints_are_unchanged(self):
        schedule_hash = "9" * 64
        base = make_owner_free_direction_endpoint(
            schedule_endpoint_sha256=schedule_hash
        )
        generic = make_owner_free_direction_pattern_endpoint(
            schedule_endpoint_sha256=schedule_hash
        )
        frontier = make_owner_free_direction_frontier_pattern_endpoint(
            schedule_endpoint_sha256=schedule_hash
        )
        fine = (
            make_owner_free_direction_frontier_fine_refinement_pattern_endpoint(
                schedule_endpoint_sha256=schedule_hash
            )
        )
        self.assertNotIn("tangent_pattern_steps_radians", base)
        self.assertEqual(
            generic["tangent_pattern_steps_radians"],
            [0.5, 0.25, 0.125, 0.0625, 0.03125],
        )
        self.assertEqual(
            frontier["tangent_pattern_steps_radians"],
            generic["tangent_pattern_steps_radians"],
        )
        self.assertNotIn("maximum_refined_component_count", frontier)
        self.assertEqual(fine["schema"], FINE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA)
        self.assertLess(
            fine["quality_objective"].index(
                "prism_below_minimum_scaled_jacobian_count"
            ),
            fine["quality_objective"].index(
                "maximum_prism_scaled_jacobian_deficit"
            ),
        )

    def test_frontier_component_precheck_is_small_disjoint_and_bounded(self):
        caps = {
            "maximum_component_count": 2,
            "maximum_candidate_root_count": 16,
        }
        evidence = _frontier_component_precheck(
            [tuple(range(1, 9)), tuple(range(9, 17))],
            caps=caps,
        )
        self.assertTrue(evidence["within_caps"])
        self.assertEqual(evidence["component_count"], 2)
        self.assertEqual(evidence["candidate_root_count"], 16)
        with self.assertRaisesRegex(
            BoundaryLayerSmokeError,
            "component_count=1 maximum_component_count=2 "
            "candidate_root_count=17 maximum_candidate_root_count=16",
        ):
            _frontier_component_precheck(
                [tuple(range(1, 18))],
                caps=caps,
            )
        with self.assertRaisesRegex(
            BoundaryLayerSmokeError,
            "component_count=3 maximum_component_count=2 "
            "candidate_root_count=9 maximum_candidate_root_count=16",
        ):
            _frontier_component_precheck(
                [(1, 2, 3), (4, 5, 6), (7, 8, 9)],
                caps=caps,
            )
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "overlap"):
            _frontier_component_precheck(
                [(1, 2, 3), (3, 4, 5)],
                caps=caps,
            )

    def test_frontier_collar_graph_is_wall_restricted_and_bfs_stable(self):
        coordinates = {
            1: (0.0, 0.0, 0.0),
            2: (1.0, 0.0, 0.0),
            3: (0.0, 1.0, 0.0),
            4: (1.0, 1.0, 0.0),
            5: (2.0, 1.0, 0.0),
            6: (1.0, 2.0, 0.0),
        }
        target_wall = "a" * 64
        other_wall = "b" * 64
        first = (1, 2, 3)
        second = (2, 3, 4)
        cross_wall = (4, 5, 6)
        graph, distances = _frontier_collar_graph(
            target_wall_surface_fingerprint=target_wall,
            seed_root_coordinate_sha256=sorted(
                _stable_root_id(coordinates[root]) for root in first
            ),
            source_triangles=[first, second, cross_wall],
            source_triangle_wall_fingerprints={
                first: target_wall,
                second: target_wall,
                cross_wall: other_wall,
            },
            root_coordinates=coordinates,
        )
        self.assertEqual(graph["source_triangle_count"], 2)
        self.assertEqual(graph["root_count"], 4)
        self.assertEqual(distances[4], 1)
        self.assertNotIn(_stable_root_id(coordinates[5]), graph["root_coordinate_sha256"])
        self.assertEqual(
            graph["distance_records"],
            [
                {
                    "distance": 0,
                    "root_coordinate_sha256": sorted(
                        _stable_root_id(coordinates[root]) for root in first
                    ),
                },
                {
                    "distance": 1,
                    "root_coordinate_sha256": [
                        _stable_root_id(coordinates[4])
                    ],
                },
            ],
        )

    def test_frontier_collar_schedule_freezes_prefix_and_obeys_formula(self):
        roots = [1, 2, 3, 4]
        first_height = 1.0e-5
        baseline = [
            first_height * (layer + 0.01 * layer * (layer - 1))
            for layer in range(1, 61)
        ]
        target_schedules = {root: list(baseline) for root in roots}
        schedules, affected, reduction = _frontier_collar_candidate_schedules(
            roots=roots,
            target_schedules=target_schedules,
            distances={1: 0, 2: 0, 3: 0, 4: 1},
            ring_width=2,
            frozen_prefix_last_layer_1_based=15,
        )
        self.assertEqual(affected, roots)
        self.assertGreater(reduction, 0.0)
        for root in roots:
            self.assertEqual(schedules[root][:15], baseline[:15])
            self.assertEqual(schedules[root][0].hex(), baseline[0].hex())
            weight = 1.0 if root != 4 else 0.5
            layer = 60
            a_i = layer * first_height
            a_k = 15 * first_height
            b_k = baseline[14]
            expected = (
                b_k
                + (a_i - a_k)
                + (1.0 - weight)
                * ((baseline[59] - b_k) - (a_i - a_k))
            )
            self.assertAlmostEqual(schedules[root][59], expected, places=18)
            self.assertTrue(
                all(
                    right > left
                    for left, right in zip(
                        schedules[root], schedules[root][1:]
                    )
                )
            )
            self.assertTrue(
                all(
                    layer * first_height <= value <= baseline[layer - 1]
                    for layer, value in enumerate(schedules[root], 1)
                )
            )

    def test_frontier_runner_replays_previous_target_previous_target(self):
        roots = (1, 2, 3)
        origins = {
            1: (0.0, 0.0, 0.0),
            2: (1.0, 0.0, 0.0),
            3: (0.0, 1.0, 0.0),
        }
        chains = {1: [1, 11], 2: [2, 12], 3: [3, 13]}
        coordinates = {
            **origins,
            11: (0.0, 0.0, 0.1),
            12: (1.0, 0.0, 0.1),
            13: (0.0, 1.0, 0.1),
            99: (9.0, 9.0, 9.0),
        }
        gmsh = _HomotopyGmsh(coordinates, [101], 201)
        directions = {root: (0.0, 0.0, 1.0) for root in roots}
        prism = {
            "tag": 101,
            "entity": 7,
            "type": "Prism 6",
            "nodes": [1, 2, 3, 11, 12, 13],
        }
        core = {
            "tag": 201,
            "entity": 8,
            "type": "Tetrahedron 4",
            "nodes": [11, 12, 13, 99],
        }
        triangle = roots
        triangle_id = _stable_triangle_id(triangle, origins)
        wall = "a" * 64

        def quality(status, digest, prism_below=0):
            return {
                "status": status,
                "quality_sha256": digest,
                "prism_below_threshold_element_count": prism_below,
                "core_tetra_below_gamma_count": 0,
                "nonpositive_element_count": 0,
                "nonfinite_count": 0,
            }

        previous = quality("PASS", "1" * 64)
        target = quality("FAIL", "2" * 64, prism_below=1)
        selected_previous = quality("PASS", "3" * 64)
        selected_target = quality("PASS", "4" * 64)
        endpoint = {
            "schema": CONTINUATION_ENDPOINT_SCHEMA,
            "request": CONTINUATION_REQUEST,
            "source_bindings": {
                "direction_replay_approval_sha256": "5" * 64,
                "homotopy_discovery_sha256": "6" * 64,
                "homotopy_endpoint_sha256": "7" * 64,
                "homotopy_manifest_sha256": "8" * 64,
                "local_schedule_binding_sha256": "9" * 64,
            },
            "endpoint_sha256": "a" * 64,
            "previous_fraction_float_hex": (0.0).hex(),
            "target_fraction_float_hex": (2.0**-12).hex(),
            "previous_quality_sha256": previous["quality_sha256"],
            "target_quality_sha256": target["quality_sha256"],
            "target_low_quality_aggregate_sha256": "b" * 64,
            "target_observation": {
                "low_quality_prism_count": 1,
                "unique_source_triangle_count": 1,
                "wall_count": 1,
                "core_tetra_below_gamma_count": 0,
                "nonpositive_element_count": 0,
            },
            "quality_thresholds": {
                "minimum_prism_scaled_jacobian_for_pass": 0.01,
                "minimum_core_tetra_gamma_for_pass": 0.001,
            },
            "caps": {
                "maximum_candidate_root_count": 16,
                "maximum_component_count": 2,
                "maximum_quality_evaluations": 4096,
                "maximum_pair_candidates_per_edge_step": 16,
            },
        }
        collar_endpoint = (
            make_owner_free_direction_frontier_collar_refinement_endpoint(
                schedule_endpoint_sha256=endpoint["endpoint_sha256"]
            )
        )
        endpoint["collar_refinement_contract"] = {
            key: value
            for key, value in collar_endpoint.items()
            if not key.endswith("_sha256")
        }
        fixed = {
            "directions": directions,
            "direction_field_sha256": "c" * 64,
            "changed_roots": [],
            "changed_root_count": 0,
            "candidate_root_count": 3,
            "root_context_replay": {"status": "PASS"},
            "interaction_topology_sha256": "d" * 64,
        }
        low_record = {
            "source_triangle_sha256": triangle_id,
            "wall_surface_fingerprint": wall,
            "layer": 1,
            "minimum_scaled_jacobian_float_hex": (0.005).hex(),
        }
        aggregate = {
            "count": 1,
            "unique_source_triangle_count": 1,
            "wall_count": 1,
            "layer_count": 1,
            "by_wall_and_layer": [],
            "by_wall": [],
            "by_layer": [],
            "records_sha256": "e" * 64,
            "aggregate_sha256": "b" * 64,
        }
        fine_selection = {
            "schema": (
                "cfdpipe.coarse_schedule_frontier_fine_refinement_selection.v1"
            ),
            "status": "PASS",
        }
        triple_selection = {
            "schema": (
                "cfdpipe.coarse_schedule_frontier_"
                "triple_refinement_selection.v1"
            ),
            "status": "PASS",
        }
        search = {
            "schema": "mock-pattern-search",
            "status": "PASS",
            "quality_evaluation_count": 9,
            "changed_roots": [],
            "fine_refinement_selection": fine_selection,
            "triple_refinement_selection": triple_selection,
        }
        schedule_evidence = {
            "schedule_direction_continuation_endpoint": endpoint,
            "physical_schedule_homotopy_endpoint": {"schema": "homotopy"},
            "authoritative_physical_surface_schedules": {wall: [0.2]},
        }
        interpolation_side_effect = [
            ({wall: [0.1]}, {"fraction_float_hex": (0.0).hex()}),
            ({wall: [0.2]}, {"fraction_float_hex": (2.0**-12).hex()}),
        ]
        assignment_side_effect = [
            ({root: [0.1] for root in roots}, {"status": "PASS"}),
            ({root: [0.2] for root in roots}, {"status": "PASS"}),
        ]
        qualities = [
            previous,
            target,
            selected_previous,
            selected_target,
            selected_previous,
            selected_target,
        ]
        skipped_collar = {
            "status": "SKIPPED",
            "evaluation_evidence": {
                "collar_quality_evaluation_count": 0,
            },
        }
        with patch(
            "cfdpipe.boundary_layer_smoke."
            "_prepare_fixed_approved_direction_field",
            return_value=fixed,
        ), patch(
            "cfdpipe.boundary_layer_smoke."
            "interpolate_physical_schedule_homotopy",
            side_effect=interpolation_side_effect,
        ), patch(
            "cfdpipe.boundary_layer_smoke._assign_root_local_schedules",
            side_effect=assignment_side_effect,
        ), patch(
            "cfdpipe.boundary_layer_smoke._owner_free_quality_snapshot",
            side_effect=qualities,
        ), patch(
            "cfdpipe.boundary_layer_smoke._stable_low_quality_prism_lineage",
            return_value=([triangle], [low_record], aggregate),
        ), patch(
            "cfdpipe.boundary_layer_smoke."
            "_run_frontier_schedule_collar",
            return_value=(
                skipped_collar,
                {root: [0.2] for root in roots},
                None,
                selected_target,
            ),
        ), patch(
            "cfdpipe.boundary_layer_smoke."
            "_run_owner_free_component_direction_search",
            return_value=(search, selected_target, directions),
        ) as search_mock:
            result = (
                _run_physical_schedule_first_frontier_direction_continuation(
                    gmsh,
                    approval={"approval_sha256": "5" * 64},
                    schedule_evidence=schedule_evidence,
                    root_coordinates=origins,
                    runtime_directions=directions,
                    chains=chains,
                    minimum_growth_bad_source_triangles=[triangle],
                    source_triangles=[triangle],
                    source_triangle_wall_fingerprints={triangle: wall},
                    source_triangle_normals={triangle: (0.0, 0.0, 1.0)},
                    incident_normals={
                        root: [(0.0, 0.0, 1.0)] for root in roots
                    },
                    source_prisms=[
                        {
                            "tag": 1,
                            "entity": 7,
                            "type": "Prism 6",
                            "base_face": triangle,
                        }
                    ],
                    chain_origin={
                        1: (1, 0), 2: (2, 0), 3: (3, 0),
                        11: (1, 1), 12: (2, 1), 13: (3, 1),
                    },
                    subdivided_prisms=[prism],
                    core_volume_records=[core],
                    prism_volume_fingerprints={7: wall},
                    minimum_prism_scaled_jacobian=0.01,
                    minimum_core_tetra_gamma=0.001,
                    minimum_cone_margin=1.0e-5,
                    minimum_changed_direction_margin=1.0e-4,
                )
            )
        self.assertEqual(result["schema"], CONTINUATION_DISCOVERY_SCHEMA)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(
            result["cross_schedule_replay"]["sequence_fraction_float_hex"],
            [(0.0).hex(), (2.0**-12).hex(), (0.0).hex(), (2.0**-12).hex()],
        )
        self.assertTrue(result["cross_schedule_replay"]["both_states_pass"])
        self.assertEqual(result["observed_counts"]["candidate_root_count"], 3)
        self.assertEqual(result["observed_counts"]["component_count"], 1)
        passed_endpoint = search_mock.call_args.kwargs["endpoint"]
        self.assertEqual(
            passed_endpoint["schema"], TRIPLE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA
        )
        self.assertLess(
            passed_endpoint["quality_objective"].index(
                "prism_below_minimum_scaled_jacobian_count"
            ),
            passed_endpoint["quality_objective"].index(
                "maximum_prism_scaled_jacobian_deficit"
            ),
        )
        self.assertFalse(result["mesh_written"])
        self.assertFalse(result["su2_called"])
        self.assertFalse(result["paraview_called"])
        self.assertEqual(result["fine_refinement_selection"], fine_selection)
        self.assertEqual(
            result["fine_refinement_selection"],
            result["pattern_search"]["fine_refinement_selection"],
        )
        self.assertEqual(
            result["triple_refinement_selection"], triple_selection
        )
        self.assertEqual(
            result["triple_refinement_selection"],
            result["pattern_search"]["triple_refinement_selection"],
        )
        self.assertEqual(result["collar_refinement_selection"], skipped_collar)
        self.assertEqual(
            result["observed_counts"]["pattern_quality_evaluation_count"], 9
        )
        self.assertEqual(
            result["observed_counts"]["collar_quality_evaluation_count"], 0
        )

    def test_continuation_absolute_fraction_state_rebuilds_from_roots(self):
        roots = [1, 2, 3]
        origins = {
            1: (0.0, 0.0, 0.0),
            2: (1.0, 0.0, 0.0),
            3: (0.0, 1.0, 0.0),
        }
        chains = {
            1: [1, 11, 21],
            2: [2, 12, 22],
            3: [3, 13, 23],
        }
        coordinates = {
            **origins,
            11: (99.0, 0.0, 0.0),
            21: (99.0, 0.0, 0.0),
            12: (99.0, 0.0, 0.0),
            22: (99.0, 0.0, 0.0),
            13: (99.0, 0.0, 0.0),
            23: (99.0, 0.0, 0.0),
            999: (7.0, 8.0, 9.0),
        }
        gmsh = _HomotopyGmsh(coordinates, [100], 200)
        schedules = {root: [0.1, 0.3] for root in roots}
        directions = {
            1: (0.0, 0.0, 1.0),
            2: (0.0, 1.0, 0.0),
            3: (1.0, 0.0, 0.0),
        }
        parameters = {
            node: list(gmsh.model.mesh.parameters[node])
            for chain in chains.values()
            for node in chain[1:]
        }
        evidence = _apply_absolute_fraction_chain_state(
            gmsh,
            roots=roots,
            root_coordinates=origins,
            chains=chains,
            root_schedules=schedules,
            directions=directions,
            parametric_coordinates_by_node=parameters,
        )
        self.assertEqual(gmsh.model.mesh.coordinates[11], (0.0, 0.0, 0.1))
        self.assertEqual(gmsh.model.mesh.coordinates[22], (1.0, 0.3, 0.0))
        self.assertEqual(gmsh.model.mesh.coordinates[23], (0.3, 1.0, 0.0))
        self.assertEqual(gmsh.model.mesh.coordinates[999], (7.0, 8.0, 9.0))
        self.assertEqual(evidence["first_layer_height_float_hex"], (0.1).hex())
        self.assertEqual(evidence["updated_chain_node_count"], 6)
        self.assertTrue(evidence["absolute_origin_rebuild"])
        self.assertEqual(
            evidence["direction_field_sha256"],
            _stable_direction_field_sha256(
                roots=list(reversed(roots)),
                root_coordinates=origins,
                directions=directions,
            ),
        )

    def test_continuation_low_prism_lineage_matches_homotopy_aggregate(self):
        class _Mesh:
            @staticmethod
            def getElementQualities(tags, metric):
                self_values = {101: 0.005, 102: 0.02, 103: float("nan")}
                self.assertEqual(metric, "minSJ")
                return [self_values[int(tag)] for tag in tags]

        roots = (1, 2, 3)
        coordinates = {
            1: (0.0, 0.0, 0.0),
            2: (1.0, 0.0, 0.0),
            3: (0.0, 1.0, 0.0),
        }
        chain_origin = {
            1: (1, 0), 2: (2, 0), 3: (3, 0),
            11: (1, 1), 12: (2, 1), 13: (3, 1),
            21: (1, 2), 22: (2, 2), 23: (3, 2),
            31: (1, 3), 32: (2, 3), 33: (3, 3),
        }
        prisms = [
            {"tag": 101, "entity": 7, "type": "Prism 6", "nodes": [1, 2, 3, 11, 12, 13]},
            {"tag": 102, "entity": 7, "type": "Prism 6", "nodes": [11, 12, 13, 21, 22, 23]},
            {"tag": 103, "entity": 7, "type": "Prism 6", "nodes": [21, 22, 23, 31, 32, 33]},
        ]
        gmsh = SimpleNamespace(model=SimpleNamespace(mesh=_Mesh()))
        runtime, records, aggregate = _stable_low_quality_prism_lineage(
            gmsh,
            prism_element_tags=[101, 102, 103],
            subdivided_prisms=prisms,
            chain_origin=chain_origin,
            root_coordinates=coordinates,
            prism_volume_fingerprints={7: "a" * 64},
            minimum_prism_scaled_jacobian=0.01,
        )
        self.assertEqual(runtime, [roots])
        self.assertEqual([record["layer"] for record in records], [1, 3])
        self.assertEqual(aggregate["count"], 2)
        self.assertEqual(aggregate["unique_source_triangle_count"], 1)
        self.assertEqual(aggregate["wall_count"], 1)
        self.assertEqual(aggregate["by_layer"], [
            {"layer": 1, "count": 1, "unique_source_triangle_count": 1},
            {"layer": 3, "count": 1, "unique_source_triangle_count": 1},
        ])
        self.assertEqual(
            aggregate["records_sha256"],
            hashlib.sha256(
                json.dumps(
                    {"records": records},
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("ascii")
            ).hexdigest(),
        )

    def test_type_specific_hard_quality_summaries_pass_and_expose_failures(self):
        passing = _QualityGmsh()
        core = _core_tetra_quality_by_region(
            passing,
            wall_adjacent_core_volume_tag=1,
            other_core_volume_tag=2,
            minimum_gamma=1.0e-3,
        )
        self.assertEqual(core["status"], "PASS")
        self.assertEqual(core["below_gamma_count"], 0)
        prism = _prism_column_quality_summary(
            passing,
            columns={_FPS[0]: [[201, 202]]},
            prism_base_faces={_FPS[0]: [[1, 2, 3]]},
            minimum_scaled_jacobian=1.0e-2,
        )
        self.assertEqual(prism["status"], "PASS")
        self.assertEqual(prism["below_threshold_column_count"], 0)

        low_core = _core_tetra_quality_by_region(
            _QualityGmsh(low_core=True),
            wall_adjacent_core_volume_tag=1,
            other_core_volume_tag=2,
            minimum_gamma=1.0e-3,
        )
        self.assertEqual(low_core["status"], "WARN")
        self.assertEqual(low_core["below_gamma_count"], 1)
        low_prism = _prism_column_quality_summary(
            _QualityGmsh(low_prism=True),
            columns={_FPS[0]: [[201, 202]]},
            prism_base_faces={_FPS[0]: [[1, 2, 3]]},
            minimum_scaled_jacobian=1.0e-2,
        )
        self.assertEqual(low_prism["status"], "WARN")
        self.assertEqual(low_prism["below_threshold_element_count"], 1)
        self.assertEqual(low_prism["below_threshold_column_count"], 1)

    def test_quality_improvement_fields_are_tag_free_and_combined_by_min(self):
        gmsh = _FieldGmsh()
        calls = []

        def configure(_gmsh, _resolved, contract, *, characteristic_length):
            calls.append((copy.deepcopy(contract), characteristic_length))
            return {
                **dict(contract),
                "threshold_field_tag_audit": 10 + len(calls),
            }

        quality = _normalized()["quality_improvement"]
        result = _configure_quality_improvement_fields(
            gmsh,
            {},
            {
                "surface_fingerprint_ids": [_FPS[0], _FPS[3], _FPS[4]],
                "minimum_size_m": 0.012,
                "distance_max_m": 0.08,
                "sampling": 100,
            },
            quality,
            characteristic_length=0.25,
            configure_local_refinement=configure,
        )
        self.assertEqual(
            calls[0][0]["surface_fingerprint_ids"], [_FPS[3], _FPS[4]]
        )
        self.assertEqual(len(calls), 3)
        self.assertEqual(gmsh.field.add_calls, ["Min"])
        self.assertEqual(
            gmsh.field.number_lists,
            [(91, "FieldsList", [11, 12, 13])],
        )
        self.assertEqual(gmsh.field.background, [91])
        self.assertFalse(result["runtime_tags_are_matching_criteria"])

    def test_chebyshev_cone_covers_single_pair_and_triple_active_cases(self):
        single_margin, single, single_candidates = _chebyshev_cone_direction(
            [(1.0, 0.0, 0.0)]
        )
        self.assertAlmostEqual(single_margin, 1.0)
        self.assertEqual(single, (1.0, 0.0, 0.0))
        self.assertEqual(single_candidates, 2)

        pair_margin, pair, _ = _chebyshev_cone_direction(
            [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0)]
        )
        self.assertAlmostEqual(pair_margin, 2.0**-0.5)
        self.assertAlmostEqual(pair[0], 2.0**-0.5)
        self.assertAlmostEqual(pair[1], 2.0**-0.5)
        self.assertAlmostEqual(pair[2], 0.0)

        triple_margin, triple, _ = _chebyshev_cone_direction(
            [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)]
        )
        self.assertAlmostEqual(triple_margin, 3.0**-0.5)
        for component in triple:
            self.assertAlmostEqual(component, 3.0**-0.5)

    def test_chebyshev_cone_is_deterministic_and_exposes_an_empty_halfspace(self):
        normals = [(0.0, 1.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)]
        first = _chebyshev_cone_direction(normals)
        second = _chebyshev_cone_direction(list(reversed(normals)))
        self.assertEqual(first, second)
        margin, _direction, _count = _chebyshev_cone_direction(
            [(1.0, 0.0, 0.0), (-1.0, 0.0, 0.0)]
        )
        self.assertAlmostEqual(margin, 0.0)

    def test_quality_selected_triangles_use_unique_direct_and_one_ring_groups(self):
        groups, evidence = _group_repair_source_triangles(
            [(1, 2, 3), (2, 3, 4), (8, 9, 10)],
            [1, 8],
        )
        self.assertEqual(groups, {1: {2, 3, 4}, 8: {9, 10}})
        self.assertEqual(
            evidence[(2, 3, 4)]["assignment_method"],
            "shares_edge_with_direct_sharp_root_triangle",
        )
        preferred_groups, preferred_evidence = _group_repair_source_triangles(
            [(1, 2, 3)],
            [1, 2],
            preferred_roots=[2],
        )
        # Root 1 is also a dynamically detected sharp root.  Even though root 2
        # is the unique strict-failure owner, root 1 must remain protected and
        # only the ordinary neighbour may be smoothed.
        self.assertEqual(preferred_groups, {2: {3}})
        self.assertEqual(
            preferred_evidence[(1, 2, 3)]["assignment_method"],
            "contains_unique_strict_failure_preferred_root",
        )
        self.assertEqual(
            preferred_evidence[(1, 2, 3)][
                "protected_other_sharp_root_tags_audit"
            ],
            [1],
        )

    def test_quality_selected_one_ring_cannot_cross_stable_wall_groups(self):
        triangles = [(1, 2, 3), (2, 3, 4)]
        labels = {
            (1, 2, 3): "wall-group-a",
            (2, 3, 4): "wall-group-b",
        }
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "no unique one-ring"):
            _group_repair_source_triangles(
                triangles,
                [1],
                triangle_group_labels=labels,
            )

    def test_ambiguous_triangle_uses_only_hash_bound_prior_strict_owner(self):
        triangle_id = "a" * 64
        root_one_id = "b" * 64
        root_two_id = "c" * 64
        wall_id = "d" * 64
        groups, evidence = _group_repair_source_triangles(
            [(1, 2, 3)],
            [1, 2],
            triangle_group_labels={(1, 2, 3): wall_id},
            triangle_stable_ids={(1, 2, 3): triangle_id},
            root_stable_ids={1: root_one_id, 2: root_two_id},
            approved_ambiguous_owner_stable_ids={
                (triangle_id, wall_id): root_two_id
            },
        )
        self.assertEqual(groups, {2: {3}})
        self.assertEqual(
            evidence[(1, 2, 3)]["assignment_method"],
            "matches_hash_bound_prior_strict_failure_owner",
        )
        self.assertEqual(
            evidence[(1, 2, 3)]["protected_other_sharp_root_tags_audit"],
            [1],
        )
        with self.assertRaisesRegex(
            BoundaryLayerSmokeError, "was not used exactly once"
        ):
            _group_repair_source_triangles(
                [(1, 2, 3)],
                [1],
                triangle_group_labels={(1, 2, 3): wall_id},
                triangle_stable_ids={(1, 2, 3): triangle_id},
                root_stable_ids={1: root_one_id},
                approved_ambiguous_owner_stable_ids={
                    (triangle_id, wall_id): root_one_id
                },
            )
        with self.assertRaisesRegex(
            BoundaryLayerSmokeError, "incomplete or invalid"
        ):
            _group_repair_source_triangles(
                [(1, 2, 3), (4, 5, 6)],
                [1, 4],
                triangle_group_labels={
                    (1, 2, 3): wall_id,
                    (4, 5, 6): wall_id,
                },
                triangle_stable_ids={
                    (1, 2, 3): triangle_id,
                    (4, 5, 6): triangle_id,
                },
                root_stable_ids={1: root_one_id, 4: root_two_id},
                approved_ambiguous_owner_stable_ids={
                    (triangle_id, wall_id): root_one_id
                },
            )

    def test_quality_selected_triangle_grouping_rejects_ambiguity_and_disconnect(self):
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "multiple sharp"):
            _group_repair_source_triangles([(1, 2, 3)], [1, 2])
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "no unique one-ring"):
            _group_repair_source_triangles([(1, 2, 3), (4, 5, 6)], [1])

    def test_incident_cone_projection_is_complete_deterministic_and_strict(self):
        normals = [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0)]
        first = _closest_feasible_cone_direction(
            (-1.0, 1.0, 0.0),
            normals,
            (1.0, 1.0, 0.0),
            interior_weight=0.02,
        )
        second = _closest_feasible_cone_direction(
            (-1.0, 1.0, 0.0),
            list(reversed(normals)),
            (1.0, 1.0, 0.0),
            interior_weight=0.02,
        )
        self.assertEqual(first, second)
        direction, margin, alignment, candidate_count = first
        self.assertGreater(margin, 0.0)
        self.assertGreater(alignment, 0.0)
        self.assertGreaterEqual(candidate_count, 4)
        self.assertGreater(direction[0], 0.0)

    def test_global_node_table_uses_unique_no_argument_api(self):
        gmsh = _GlobalNodeGmsh()
        self.assertEqual(
            _nodes_from_gmsh(gmsh),
            {1: (0.0, 0.0, 0.0), 2: (1.0, 0.0, 0.0)},
        )
        self.assertTrue(gmsh.model.mesh.called)
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "node table"):
            _nodes_from_gmsh(_GlobalNodeGmsh(duplicate=True))

    def test_one_layer_pairing_uses_official_faces_not_raw_node_slots(self):
        result = _one_layer_prism_topology(
            _OneLayerTopologyGmsh(),
            prism_volume_tags=[7],
            wall_surface_tags=[10],
            top_surface_tags=[11],
            max_elements=100,
        )
        self.assertEqual(result["base_to_top"], {1: 4, 2: 5, 3: 6})
        self.assertEqual(result["records"][0]["nodes"], (4, 1, 5, 2, 6, 3))
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "bijective"):
            _one_layer_prism_topology(
                _OneLayerTopologyGmsh(nonbijective=True),
                prism_volume_tags=[7],
                wall_surface_tags=[10],
                top_surface_tags=[11],
                max_elements=100,
            )

    def test_repair_schedule_has_exact_configured_15_layer_geometry(self):
        config = _normalized()
        schedule = _repair_layer_schedule(config)
        self.assertEqual(len(schedule), 15)
        self.assertAlmostEqual(schedule[0], config["first_layer_height_m"])
        self.assertTrue(all(right > left for left, right in zip(schedule, schedule[1:])))
        expected_total = config["first_layer_height_m"] * sum(
            config["growth_ratio"]**index for index in range(15)
        )
        self.assertAlmostEqual(schedule[-1], expected_total)

    def test_subdivision_cap_is_gated_before_any_discrete_mesh_mutation(self):
        self.assertEqual(
            _gate_subdivision_element_cap(
                source_prism_count=10,
                nonprism_type_counts={"Tetrahedron 4": 5},
                layer_count=15,
                max_3d_elements=155,
            ),
            155,
        )
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "exceed"):
            _gate_subdivision_element_cap(
                source_prism_count=10,
                nonprism_type_counts={"Tetrahedron 4": 6},
                layer_count=15,
                max_3d_elements=155,
            )
        source = inspect.getsource(_apply_one_layer_orientation_cone_subdivision)
        self.assertLess(
            source.index("_gate_subdivision_element_cap("),
            source.index("gmsh.model.mesh.addNodes("),
        )

    def test_exact_collar_base_does_not_exempt_other_missing_wall_faces(self):
        prism = {
            "tag": 1,
            "type": "Prism 6",
            "faces": [
                [1, 2, 3],
                [4, 5, 6],
                [1, 4, 5, 2],
                [2, 5, 6, 3],
                [3, 6, 4, 1],
            ],
        }
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "no unique first prism"):
            _derive_prism_columns(
                [prism],
                {_FPS[0]: [[7, 8, 9], [10, 11, 12]]},
                {_FPS[0]: [[7, 8, 9]]},
            )

    def test_interface_role_audit_rejects_prism_top_without_core_owner(self):
        payload = _mixed_payload()
        for element in payload["elements"]:
            if element["type"] == "Prism 6":
                element["region"] = "boundary_layer"
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "prism top"):
            audit_mixed_mesh(
                **payload,
                wall_fingerprints=_FPS,
                expected_marker_names={"farfield", "wall"},
                collar_base_faces={},
                role_faces={
                    "wall_base": [[20, 21, 22]],
                    "prism_core_top": [[20, 21, 22]],
                    "prism_prism_lateral": [],
                    "transition": [],
                },
            )

    def test_internal_role_faces_strip_canonical_arity_prefix(self):
        elements = [
            {
                "type": "Prism 6",
                "faces": [[1, 2, 3], [4, 5, 6], [1, 4, 5, 2]],
            },
            {
                "type": "Prism 6",
                "faces": [[6, 5, 4], [10, 11, 12], [5, 4, 1, 2]],
            },
            {
                "type": "Pyramid 5",
                "faces": [[20, 21, 22], [30, 31, 32]],
            },
            {
                "type": "Tetrahedron 4",
                "faces": [[22, 20, 21]],
            },
        ]
        lateral, transition = _derive_internal_role_faces(elements)
        self.assertEqual(lateral, [[1, 2, 4, 5]])
        self.assertEqual(transition, [[20, 21, 22]])
        self.assertTrue(all(len(face) in {3, 4} for face in lateral + transition))

    def test_readback_rejects_missing_named_physical_groups_before_mesh_audit(self):
        strategy = RealProjectBoundaryLayerStrategy(_markers(), {"local_refinement": {}})
        strategy._readback_plan = {
            "core1_tag": 1,
            "core2_tag": 2,
            "prism_volume_tags": [3],
            "marker_surface_tags": {"wall": [10], "farfield": [11]},
            "fluid_physical_name": "fluid",
        }
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "physical groups"):
            strategy.readback(
                _ReadbackPhysicalGmsh(), Path("mesh.msh"), _normalized()
            )

    def test_fatal_gmsh_logger_is_a_fail_gate(self):
        manifest = {
            "gmsh_sessions": {
                "build": {"logger_messages": ["Error : inverted element"]}
            }
        }
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "fatal diagnostics"):
            _reject_fatal_gmsh_log(manifest, "build")

    def test_ill_shaped_warning_is_deferred_until_complete_final_evidence(self):
        message = "Warning : 11 ill-shaped tets are still in the mesh"
        manifest = {
            "production_mesh_eligible": False,
            "su2_called": False,
            "paraview_called": False,
            "external_commands": [],
            "source_unchanged": True,
            "gmsh_sessions": {
                "build": {
                    "logger_messages": [message],
                    "finalize_called": True,
                    "cleanup_errors": [],
                },
                "readback": {
                    "logger_messages": ["Info : No ill-shaped tets in the mesh :-)"],
                    "finalize_called": True,
                    "cleanup_errors": [],
                },
            },
        }
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "fatal diagnostics"):
            _reject_fatal_gmsh_log(manifest, "build")
        pending = _reject_fatal_gmsh_log(
            manifest, "build", defer_ill_shaped=True
        )
        self.assertEqual(pending["status"], "PENDING")
        self.assertEqual(pending["pending_diagnostics"][0]["raw_line"], message)
        self.assertEqual(_reject_fatal_gmsh_log(manifest, "readback")["status"], "PASS")

        audit = {
            "status": "PASS",
            "node_count": 8,
            "element_count_3d": 2,
            "element_types_3d": {"Tetrahedron 4": 2},
            "external_face_count": 4,
            "marker_face_counts": {"wall": 4},
            "shared_interface_face_count": 1,
            "shared_interface_region_pairs": {"fluid_1|fluid_2": 1},
            "requested_layer_count": 15,
            "layer_audit": {"column_count": 1},
            "quality": {
                "volume": 1.0e-12,
                "minDetJac": 2.0e-13,
                "minSJ": 3.0e-4,
                "minSICN": 4.0e-8,
                "nonpositive_volume_count": 0,
                "nonpositive_jacobian_count": 0,
                "nonfinite_count": 0,
            },
        }
        with tempfile.TemporaryDirectory() as raw:
            msh = Path(raw) / "mesh.msh"
            su2 = Path(raw) / "mesh.su2"
            msh.write_text("msh", encoding="ascii")
            su2.write_text("su2", encoding="ascii")
            result = _finalize_deferred_gmsh_log_diagnostics(
                manifest,
                audit,
                copy.deepcopy(audit),
                {
                    "status": "PASS",
                    "nelem": 2,
                    "marker_element_counts": {"wall": 4},
                },
                msh,
                su2,
            )
        self.assertEqual(result["status"], "WARN")
        classified = result["classified_nonfatal_diagnostics"][0]
        self.assertEqual(classified["ill_shaped_tetrahedron_count"], 11)
        self.assertEqual(classified["disposition"], "WARN_AFTER_COMPLETE_SMOKE_GATE")
        self.assertEqual(manifest["diagnostic_quality_status"], "WARN")
        self.assertTrue(manifest["requires_quality_improvement_before_production"])

    def test_deferred_warning_is_resolved_only_by_build_and_readback_hard_gate(self):
        manifest = {
            "production_mesh_eligible": False,
            "su2_called": False,
            "paraview_called": False,
            "external_commands": [],
            "source_unchanged": True,
            "gmsh_sessions": {
                "build": {
                    "logger_messages": [
                        "Warning : 2 ill-shaped tets are still in the mesh"
                    ],
                    "finalize_called": True,
                    "cleanup_errors": [],
                },
                "readback": {
                    "logger_messages": [],
                    "finalize_called": True,
                    "cleanup_errors": [],
                },
            },
        }
        _reject_fatal_gmsh_log(manifest, "build", defer_ill_shaped=True)
        _reject_fatal_gmsh_log(manifest, "readback")
        hard_gate = {
            "quality_improvement": {
                "status": "PASS",
                "core_tetra": {
                    "status": "PASS",
                    "below_gamma_count": 0,
                    "regions": {
                        "wall_adjacent_core": {
                            "element_count": 10,
                            "bad_element_count_union": 0,
                            "below_threshold_count": 0,
                        },
                        "other_core": {
                            "element_count": 20,
                            "bad_element_count_union": 0,
                            "below_threshold_count": 0,
                        },
                    },
                },
                "prism_columns": {
                    "status": "PASS",
                    "element_count": 15,
                    "column_count": 1,
                    "below_threshold_element_count": 0,
                    "below_threshold_column_count": 0,
                },
            }
        }
        manifest["strategy_evidence"] = {
            "build": copy.deepcopy(hard_gate),
            "readback": copy.deepcopy(hard_gate),
        }
        audit = {
            "status": "PASS",
            "node_count": 8,
            "element_count_3d": 2,
            "element_types_3d": {"Tetrahedron 4": 2},
            "external_face_count": 4,
            "marker_face_counts": {"wall": 4},
            "shared_interface_face_count": 1,
            "shared_interface_region_pairs": {"fluid_1|fluid_2": 1},
            "requested_layer_count": 15,
            "layer_audit": {"column_count": 1},
            "quality": {
                "volume": 1.0e-12,
                "minDetJac": 2.0e-13,
                "minSJ": 1.1e-2,
                "minSICN": 4.0e-8,
                "nonpositive_volume_count": 0,
                "nonpositive_jacobian_count": 0,
                "nonfinite_count": 0,
            },
        }
        with tempfile.TemporaryDirectory() as raw:
            msh = Path(raw) / "mesh.msh"
            su2 = Path(raw) / "mesh.su2"
            msh.write_text("msh", encoding="ascii")
            su2.write_text("su2", encoding="ascii")
            result = _finalize_deferred_gmsh_log_diagnostics(
                manifest,
                audit,
                copy.deepcopy(audit),
                {
                    "status": "PASS",
                    "nelem": 2,
                    "marker_element_counts": {"wall": 4},
                },
                msh,
                su2,
            )
        self.assertEqual(result["status"], "PASS")
        classified = result["classified_nonfatal_diagnostics"][0]
        self.assertEqual(
            classified["disposition"],
            "PASS_AFTER_BUILD_AND_FRESH_READBACK_HARD_GATE",
        )
        self.assertEqual(manifest["diagnostic_quality_status"], "PASS")
        self.assertFalse(manifest["requires_quality_improvement_before_production"])

    def test_deferred_warning_rejects_incomplete_quality_or_unknown_warning(self):
        unknown = {"gmsh_sessions": {"build": {"logger_messages": ["Warning : unknown"]}}}
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "fatal diagnostics"):
            _reject_fatal_gmsh_log(unknown, "build", defer_ill_shaped=True)

        manifest = {
            "production_mesh_eligible": False,
            "su2_called": False,
            "paraview_called": False,
            "external_commands": [],
            "source_unchanged": True,
            "gmsh_sessions": {
                "build": {
                    "logger_messages": [
                        "Warning : 1 ill-shaped tet is still in the mesh"
                    ],
                    "finalize_called": True,
                    "cleanup_errors": [],
                },
                "readback": {
                    "logger_messages": [],
                    "finalize_called": True,
                    "cleanup_errors": [],
                },
            },
        }
        _reject_fatal_gmsh_log(manifest, "build", defer_ill_shaped=True)
        _reject_fatal_gmsh_log(manifest, "readback")
        bad_audit = {
            "status": "PASS",
            "quality": {
                "volume": 1.0,
                "minDetJac": 1.0,
                "minSJ": 0.0,
                "minSICN": 1.0,
                "nonpositive_volume_count": 0,
                "nonpositive_jacobian_count": 0,
                "nonfinite_count": 0,
            },
        }
        with tempfile.TemporaryDirectory() as raw:
            msh = Path(raw) / "mesh.msh"
            su2 = Path(raw) / "mesh.su2"
            msh.write_text("msh", encoding="ascii")
            su2.write_text("su2", encoding="ascii")
            with self.assertRaisesRegex(BoundaryLayerSmokeError, "complete final"):
                _finalize_deferred_gmsh_log_diagnostics(
                    manifest,
                    bad_audit,
                    copy.deepcopy(bad_audit),
                    {"status": "PASS", "nelem": None, "marker_element_counts": None},
                    msh,
                    su2,
                )

    def test_element_cap_stops_before_official_faces_and_quality(self):
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "exceeds"):
            _volume_elements_from_gmsh(
                _CapGmsh(), {7: "fluid_core_1"}, max_elements=1
            )

    def test_real_strategy_dispatches_implicit_and_fails_closed_without_repair_hook(self):
        strategy = RealProjectBoundaryLayerStrategy(_markers(), {"local_refinement": {}})
        gmsh = _ExtrudeGmsh()
        config = _normalized()
        result = strategy._extrude(
            gmsh,
            [10],
            {10: _FPS[0]},
            -1,
            config,
        )
        self.assertEqual(result["extruded_dimtags"], [(2, 1001), (3, 2001)])
        dimtags, num_elements, heights, recombine = gmsh.model.geo.calls[0]
        self.assertEqual(dimtags, [(2, 10)])
        self.assertEqual(num_elements, [1])
        self.assertEqual(len(heights), 1)
        self.assertAlmostEqual(
            abs(heights[0]), config["construction_first_layer_height_m"]
        )
        self.assertEqual(
            result["final_physical_first_layer_height_m"],
            config["first_layer_height_m"],
        )
        self.assertTrue(all(value < 0.0 for value in heights))
        self.assertTrue(recombine)

        scalar = copy.deepcopy(config)
        scalar["normal_mode"] = "scalar"
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "no approved"):
            strategy._extrude(gmsh, [10], {10: _FPS[0]}, -1, scalar)

    def test_real_strategy_uses_official_primary_element_faces_and_real_quality_names(self):
        gmsh = _OfficialFaceGmsh()
        records = _volume_elements_from_gmsh(
            gmsh, {7: "fluid_core_1"}, max_elements=300000
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(len(records[0]["faces"]), 4)
        self.assertTrue(all(call[3] is True for call in gmsh.model.mesh.face_calls))
        source = inspect.getsource(__import__(
            "cfdpipe.boundary_layer_smoke", fromlist=["boundary_layer_smoke"]
        ))
        self.assertIn('("minDetJac", "minSJ", "minSICN", "volume")', source)
        self.assertNotIn("max_nonorthogonality", source)
        self.assertNotRegex(source, r"(?<![A-Za-z0-9_])(?:832|1108|1183|1333|1444|4698)(?![A-Za-z0-9_])")
        parsed = ast.parse(source)
        add_calls = [
            node
            for node in ast.walk(parsed)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "addElementsByType"
        ]
        self.assertGreaterEqual(len(add_calls), 3)
        self.assertTrue(
            all(
                len(call.args) > 1
                and not isinstance(call.args[1], ast.Constant)
                for call in add_calls
            )
        )

    def test_normalization_binds_five_hashes_and_all_48_walls(self):
        result = _normalized()
        self.assertEqual(result["schema"], "cfdpipe.boundary_layer_smoke.v2")
        self.assertEqual(result["wall_member_count"], 48)
        self.assertEqual(result["wall_surface_fingerprints"], sorted(_FPS))
        self.assertEqual(result["layer_count"], 15)
        self.assertFalse(result["su2_called"])
        self.assertFalse(result["paraview_called"])
        self.assertRegex(result["normalized_config_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            result["post_mesh_repair"]["expected_full_layer_bad_prism_count"],
            34,
        )
        self.assertEqual(
            result["quality_improvement"]["minimum_core_tetra_gamma"], 1.0e-3
        )
        self.assertEqual(
            result["quality_improvement"][
                "maximum_prism_below_threshold_column_count"
            ],
            0,
        )

    def test_normalization_rejects_incomplete_or_unsafe_subdivision_repair(self):
        incomplete = _document()
        incomplete["post_mesh_repair"].pop("expected_smoothed_root_count")
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "repair is incomplete"):
            normalize_boundary_layer_smoke_config(
                incomplete,
                project=_project(),
                marker_config=_markers(),
                input_records=_records(),
            )

        unsafe_construction = _document()
        unsafe_construction["mesh"]["construction_first_layer_height_m"] = 1.0e-9
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "mesh policy is unsafe"):
            normalize_boundary_layer_smoke_config(
                unsafe_construction,
                project=_project(),
                marker_config=_markers(),
                input_records=_records(),
            )

    def test_normalization_rejects_relaxed_or_unrestored_quality_contract(self):
        relaxed = _document()
        relaxed["quality_improvement"][
            "maximum_core_tetra_below_gamma_count"
        ] = 1
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "may not be relaxed"):
            normalize_boundary_layer_smoke_config(
                relaxed,
                project=_project(),
                marker_config=_markers(),
                input_records=_records(),
            )
        unrestored = _document()
        unrestored["quality_improvement"]["additional_refinement_groups"][0][
            "surface_fingerprint_ids"
        ] = [_FPS[5]]
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "must be restored"):
            normalize_boundary_layer_smoke_config(
                unrestored,
                project=_project(),
                marker_config=_markers(),
                input_records=_records(),
            )
        unsafe = _document()
        unsafe["post_mesh_repair"]["smoothing_interior_weight"] = 1.0
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "repair is unsafe"):
            normalize_boundary_layer_smoke_config(
                unsafe,
                project=_project(),
                marker_config=_markers(),
                input_records=_records(),
            )

    def test_normalization_rejects_stale_hash_missing_wall_and_unsafe_downstream(self):
        stale = _document()
        stale["provenance"]["topology_smoke_sha256"] = "f" * 64
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "stale"):
            normalize_boundary_layer_smoke_config(
                stale, project=_project(), marker_config=_markers(), input_records=_records()
            )
        incomplete = _document()
        incomplete["selection"]["wall_surface_fingerprints"].pop()
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "all 48"):
            normalize_boundary_layer_smoke_config(
                incomplete, project=_project(), marker_config=_markers(), input_records=_records()
            )
        unsafe = _document()
        unsafe["policy"]["run_su2"] = True
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "unsafe"):
            normalize_boundary_layer_smoke_config(
                unsafe, project=_project(), marker_config=_markers(), input_records=_records()
            )

    def test_mixed_mesh_audit_accepts_only_allowed_linear_types_and_15_layers(self):
        payload = _mixed_payload()
        result = audit_mixed_mesh(
            **payload,
            wall_fingerprints=_FPS,
            expected_marker_names={"farfield", "wall"},
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["element_types_3d"]["Prism 6"], 15)
        self.assertEqual(result["element_types_3d"]["Pyramid 5"], 1)
        self.assertEqual(result["element_types_3d"]["Tetrahedron 4"], 2)
        self.assertEqual(result["shared_interface_face_count"], 1)
        self.assertEqual(result["layer_audit"]["column_count"], 1)

    def test_mixed_mesh_audit_rejects_bad_marker_shared_interface_and_quality(self):
        bad_marker = _mixed_payload()
        duplicate = bad_marker["marker_faces"]["farfield"][0]
        bad_marker["marker_faces"]["wall"].append(duplicate)
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "multiple markers"):
            audit_mixed_mesh(
                **bad_marker, wall_fingerprints=_FPS, expected_marker_names={"farfield", "wall"}
            )

        bad_interface = _mixed_payload()
        bad_interface["shared_interface_faces"] = [[20, 21, 22]]
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "marked as a boundary"):
            audit_mixed_mesh(
                **bad_interface, wall_fingerprints=_FPS, expected_marker_names={"farfield", "wall"}
            )

        bad_quality = _mixed_payload()
        bad_quality["quality"][2]["minSICN"] = 0.0
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "non-positive"):
            audit_mixed_mesh(
                **bad_quality, wall_fingerprints=_FPS, expected_marker_names={"farfield", "wall"}
            )

    def test_mixed_mesh_audit_rejects_missing_layer_and_element_cap(self):
        missing = _mixed_payload()
        missing["wall_columns"][_FPS[0]][0].pop()
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "exactly 15"):
            audit_mixed_mesh(
                **missing, wall_fingerprints=_FPS, expected_marker_names={"farfield", "wall"}
            )
        capped = _mixed_payload()
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "exceeds"):
            audit_mixed_mesh(
                **capped,
                wall_fingerprints=_FPS,
                expected_marker_names={"farfield", "wall"},
                max_elements=1,
            )

    def test_su2_mixed_text_parser(self):
        text = """NDIME= 3
NELEM= 3
10 0 1 2 3 0
13 0 1 2 4 5 6 1
14 0 1 2 3 7 2
NPOIN= 8
0 0 0 0
1 0 0 1
0 1 0 2
0 0 1 3
1 1 0 4
1 0 1 5
0 1 1 6
1 1 1 7
NMARK= 2
MARKER_TAG= farfield
MARKER_ELEMS= 1
5 0 1 2
MARKER_TAG= wall
MARKER_ELEMS= 1
9 0 1 4 2
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "mesh.su2"
            path.write_text(text, encoding="utf-8")
            result = parse_su2_mixed_mesh(path, expected_markers={"farfield", "wall"})
        self.assertEqual(result["nelem"], 3)
        self.assertEqual(result["volume_element_types"]["Prism 6"], 1)
        self.assertEqual(result["volume_element_types"]["Pyramid 5"], 1)

    def test_su2_parser_rejects_nan_disallowed_type_and_marker_mismatch(self):
        base = "NDIME= 3\nNELEM= 1\n10 0 1 2 3\nNPOIN= 4\n0 0 0\n1 0 0\n0 1 0\n0 0 1\nNMARK= 1\nMARKER_TAG= wall\nMARKER_ELEMS= 1\n5 0 1 2\n"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "mesh.su2"
            path.write_text(base.replace("0 0 0", "NaN 0 0", 1), encoding="utf-8")
            with self.assertRaisesRegex(BoundaryLayerSmokeError, "NaN"):
                parse_su2_mixed_mesh(path, expected_markers={"wall"})
            path.write_text(base.replace("10 0 1 2 3", "12 0 1 2 3 4 5 6 7"), encoding="utf-8")
            with self.assertRaisesRegex(BoundaryLayerSmokeError, "disallowed"):
                parse_su2_mixed_mesh(path, expected_markers={"wall"})
            path.write_text(base, encoding="utf-8")
            with self.assertRaisesRegex(BoundaryLayerSmokeError, "markers"):
                parse_su2_mixed_mesh(path, expected_markers={"farfield"})

    def test_builder_preserves_traceback_and_finalizes_after_strategy_failure(self):
        gmsh = _FailingGmsh()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.brep"
            source.write_bytes(b"brep")
            source.chmod(stat.S_IREAD)
            normalized = _normalized()
            normalized["provenance"]["pipeline_brep_sha256"] = hashlib.sha256(b"brep").hexdigest()
            _refresh_normalized_hash(normalized)
            output = root / "result"
            with self.assertRaisesRegex(BoundaryLayerSmokeError, "injected core"):
                build_boundary_layer_smoke(
                    source,
                    output,
                    normalized,
                    strategy=_FailingStrategy(),
                    gmsh_module=gmsh,
                    run_evidence={"controller_argv": ["cfdpipe", "pipeline"]},
                )
            manifest = json.loads(
                (output / "boundary_layer_smoke_manifest.json").read_text(encoding="utf-8")
            )
            log_text = (output / "gmsh_boundary_layer_smoke.log").read_text(
                encoding="utf-8"
            )
            source.chmod(stat.S_IWRITE | stat.S_IREAD)
        self.assertTrue(gmsh.finalized)
        self.assertTrue(manifest["gmsh_sessions"]["build"]["finalize_called"])
        self.assertEqual(manifest["status"], "FAIL")
        self.assertIn("Traceback", manifest["error"]["traceback"])
        self.assertFalse(manifest["su2_called"])
        self.assertFalse(manifest["paraview_called"])
        self.assertEqual(
            manifest["run_evidence"]["controller_argv"], ["cfdpipe", "pipeline"]
        )
        self.assertEqual(
            manifest["normalized_config_sha256"], normalized["normalized_config_sha256"]
        )
        self.assertIn("[gmsh:build] Info : fake", log_text)
        self.assertIn(
            "direction_audit", manifest["strategy_evidence"]["build"]
        )

    def test_owner_free_pattern_quality_uses_continuous_deficits(self):
        class _Mesh:
            @staticmethod
            def getElementQualities(tags, metric):
                if metric == "gamma":
                    return [0.0005 for _tag in tags]
                if metric == "minSJ":
                    return [0.005 if tag == 1 else 0.02 for tag in tags]
                return [0.02 for _tag in tags]

        gmsh = SimpleNamespace(
            model=SimpleNamespace(mesh=_Mesh())
        )
        quality = _owner_free_quality_snapshot(
            gmsh,
            prism_element_tags=[1],
            core_element_tags=[2],
            core_tetra_element_tags=[2],
            minimum_prism_scaled_jacobian=0.01,
            minimum_core_tetra_gamma=0.001,
            include_deficit_metrics=True,
        )
        self.assertAlmostEqual(
            quality["maximum_prism_scaled_jacobian_deficit"], 0.005
        )
        self.assertAlmostEqual(
            quality["prism_scaled_jacobian_deficit_l1"], 0.005
        )
        self.assertAlmostEqual(
            quality["prism_scaled_jacobian_deficit_l2"], 0.000025
        )
        self.assertAlmostEqual(
            quality["maximum_core_tetra_gamma_deficit"], 0.0005
        )

    def test_owner_free_pattern_objective_never_trades_core_for_prisms(self):
        safe = {
            "nonfinite_count": 0,
            "nonpositive_element_count": 0,
            "core_tetra_below_gamma_count": 0,
            "maximum_core_tetra_gamma_deficit": 0.0,
            "core_tetra_gamma_deficit_l2": 0.0,
            "maximum_prism_scaled_jacobian_deficit": 0.009,
            "prism_scaled_jacobian_deficit_l2": 0.01,
            "prism_scaled_jacobian_deficit_l1": 0.1,
            "prism_below_threshold_element_count": 20,
            "minimum_prism_scaled_jacobian": 0.001,
            "minimum_core_tetra_gamma": 0.0011,
        }
        unsafe = {
            **safe,
            "core_tetra_below_gamma_count": 1,
            "maximum_core_tetra_gamma_deficit": 0.0001,
            "core_tetra_gamma_deficit_l2": 1.0e-8,
            "maximum_prism_scaled_jacobian_deficit": 0.0,
            "prism_scaled_jacobian_deficit_l2": 0.0,
            "prism_scaled_jacobian_deficit_l1": 0.0,
            "prism_below_threshold_element_count": 0,
            "minimum_prism_scaled_jacobian": 0.02,
            "minimum_core_tetra_gamma": 0.0009,
        }
        safe_rank = _owner_free_quality_objective(
            safe,
            direction_change_penalty=0.0,
            continuous_deficit_merit=True,
        )
        unsafe_rank = _owner_free_quality_objective(
            unsafe,
            direction_change_penalty=0.0,
            continuous_deficit_merit=True,
        )
        self.assertLess(safe_rank, unsafe_rank)

    def test_owner_free_endpoint_declared_objectives_preserve_old_orders(self):
        quality = {
            "nonfinite_count": 1,
            "nonpositive_element_count": 2,
            "core_tetra_below_gamma_count": 3,
            "maximum_core_tetra_gamma_deficit": 4.0,
            "core_tetra_gamma_deficit_l2": 5.0,
            "maximum_prism_scaled_jacobian_deficit": 6.0,
            "prism_scaled_jacobian_deficit_l2": 7.0,
            "prism_scaled_jacobian_deficit_l1": 8.0,
            "prism_below_threshold_element_count": 9,
            "minimum_prism_scaled_jacobian": 10.0,
            "minimum_core_tetra_gamma": 11.0,
        }
        schedule_hash = "a" * 64
        base_endpoint = make_owner_free_direction_endpoint(
            schedule_endpoint_sha256=schedule_hash
        )
        pattern_endpoint = make_owner_free_direction_pattern_endpoint(
            schedule_endpoint_sha256=schedule_hash
        )
        base_legacy = _owner_free_quality_objective(
            quality, direction_change_penalty=12.0
        )
        base_declared = _owner_free_quality_objective(
            quality,
            direction_change_penalty=12.0,
            quality_objective=base_endpoint["quality_objective"],
        )
        pattern_legacy = _owner_free_quality_objective(
            quality,
            direction_change_penalty=12.0,
            continuous_deficit_merit=True,
        )
        pattern_declared = _owner_free_quality_objective(
            quality,
            direction_change_penalty=12.0,
            quality_objective=pattern_endpoint["quality_objective"],
        )
        self.assertEqual(base_declared, base_legacy)
        self.assertEqual(
            base_declared, (1.0, 2.0, 9.0, 3.0, -10.0, -11.0, 12.0)
        )
        self.assertEqual(pattern_declared, pattern_legacy)
        self.assertEqual(
            pattern_declared,
            (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, -10.0, -11.0, 12.0),
        )

    def test_frontier_objective_is_prism_count_first_after_core_safety(self):
        objective = make_owner_free_direction_frontier_pattern_endpoint(
            schedule_endpoint_sha256="b" * 64
        )["quality_objective"]
        fewer_prisms = {
            "nonfinite_count": 0,
            "nonpositive_element_count": 0,
            "core_tetra_below_gamma_count": 0,
            "maximum_core_tetra_gamma_deficit": 0.0,
            "core_tetra_gamma_deficit_l2": 0.0,
            "prism_below_threshold_element_count": 1,
            "maximum_prism_scaled_jacobian_deficit": 0.009,
            "prism_scaled_jacobian_deficit_l2": 0.01,
            "prism_scaled_jacobian_deficit_l1": 0.1,
            "minimum_prism_scaled_jacobian": 0.001,
            "minimum_core_tetra_gamma": 0.002,
        }
        improved_maximum_but_more_prisms = {
            **fewer_prisms,
            "prism_below_threshold_element_count": 2,
            "maximum_prism_scaled_jacobian_deficit": 0.001,
            "prism_scaled_jacobian_deficit_l2": 1.0e-6,
            "prism_scaled_jacobian_deficit_l1": 0.002,
            "minimum_prism_scaled_jacobian": 0.009,
        }
        fewer_rank = _owner_free_quality_objective(
            fewer_prisms,
            direction_change_penalty=0.0,
            quality_objective=objective,
        )
        improved_rank = _owner_free_quality_objective(
            improved_maximum_but_more_prisms,
            direction_change_penalty=0.0,
            quality_objective=objective,
        )
        self.assertLess(fewer_rank, improved_rank)

        for safety_field in (
            "nonfinite_count",
            "nonpositive_element_count",
            "core_tetra_below_gamma_count",
        ):
            unsafe = {
                **fewer_prisms,
                safety_field: 1,
                "prism_below_threshold_element_count": 0,
                "maximum_prism_scaled_jacobian_deficit": 0.0,
                "prism_scaled_jacobian_deficit_l2": 0.0,
                "prism_scaled_jacobian_deficit_l1": 0.0,
                "minimum_prism_scaled_jacobian": 0.02,
            }
            with self.subTest(safety_field=safety_field):
                unsafe_rank = _owner_free_quality_objective(
                    unsafe,
                    direction_change_penalty=0.0,
                    quality_objective=objective,
                )
                self.assertLess(fewer_rank, unsafe_rank)

    def test_owner_free_declared_objective_rejects_unknown_duplicate_and_incomplete(self):
        quality = {
            "nonfinite_count": 0,
            "nonpositive_element_count": 0,
            "core_tetra_below_gamma_count": 0,
            "maximum_core_tetra_gamma_deficit": 0.0,
            "core_tetra_gamma_deficit_l2": 0.0,
            "prism_below_threshold_element_count": 0,
            "maximum_prism_scaled_jacobian_deficit": 0.0,
            "prism_scaled_jacobian_deficit_l2": 0.0,
            "prism_scaled_jacobian_deficit_l1": 0.0,
            "minimum_prism_scaled_jacobian": 0.02,
            "minimum_core_tetra_gamma": 0.002,
        }
        declared = make_owner_free_direction_frontier_pattern_endpoint(
            schedule_endpoint_sha256="c" * 64
        )["quality_objective"]
        cases = {
            "unknown": [*declared[:-1], "not_a_quality_field"],
            "duplicates": [*declared[:-1], declared[0]],
            "incomplete": declared[:-1],
        }
        for expected, candidate in cases.items():
            with self.subTest(expected=expected), self.assertRaisesRegex(
                BoundaryLayerSmokeError, expected
            ):
                _owner_free_quality_objective(
                    quality,
                    direction_change_penalty=0.0,
                    quality_objective=candidate,
                )

    def test_frontier_schema_runs_pattern_interaction_branch(self):
        roots = (1, 2, 3)
        origins = {
            1: (0.0, 0.0, 0.0),
            2: (1.0, 0.0, 0.0),
            3: (0.0, 1.0, 0.0),
        }
        chains = {1: [1, 11], 2: [2, 12], 3: [3, 13]}
        coordinates = {
            **origins,
            11: (0.0, 0.0, 0.1),
            12: (1.0, 0.0, 0.1),
            13: (0.0, 1.0, 0.1),
            99: (9.0, 9.0, 0.1),
        }
        gmsh = _HomotopyGmsh(coordinates, [101], 201)
        triangle = roots
        wall = "d" * 64
        triangle_ids = {triangle: _stable_triangle_id(triangle, origins)}
        runtime_components, stable_components = build_owner_free_components(
            [triangle],
            triangle_stable_ids=triangle_ids,
            triangle_wall_fingerprints={triangle: wall},
            root_coordinates=origins,
        )
        quality = {
            "status": "PASS",
            "quality_sha256": "e" * 64,
            "nonfinite_count": 0,
            "nonpositive_element_count": 0,
            "core_tetra_below_gamma_count": 0,
            "maximum_core_tetra_gamma_deficit": 0.0,
            "core_tetra_gamma_deficit_l2": 0.0,
            "prism_below_threshold_element_count": 0,
            "maximum_prism_scaled_jacobian_deficit": 0.0,
            "prism_scaled_jacobian_deficit_l2": 0.0,
            "prism_scaled_jacobian_deficit_l1": 0.0,
            "minimum_prism_scaled_jacobian": 0.02,
            "minimum_core_tetra_gamma": 0.002,
            "prism_element_count": 1,
            "core_element_count": 1,
            "core_tetra_count": 1,
        }
        endpoint = make_owner_free_direction_frontier_pattern_endpoint(
            schedule_endpoint_sha256="f" * 64
        )
        with patch(
            "cfdpipe.boundary_layer_smoke._owner_free_quality_snapshot",
            return_value=quality,
        ) as quality_mock:
            search, final_quality, _selected = (
                _run_owner_free_component_direction_search(
                    gmsh,
                    components=runtime_components,
                    stable_components=stable_components,
                    bad_source_triangles=[triangle],
                    triangle_stable_ids=triangle_ids,
                    triangle_wall_fingerprints={triangle: wall},
                    prism_volume_fingerprints={7: wall},
                    source_triangle_normals={triangle: (0.0, 0.0, 1.0)},
                    incident_normals={
                        root: [(0.0, 0.0, 1.0)] for root in roots
                    },
                    original_directions={
                        root: (0.0, 0.0, 1.0) for root in roots
                    },
                    root_coordinates=origins,
                    chains=chains,
                    root_cumulative_heights={root: [0.1] for root in roots},
                    subdivided_prisms=[
                        {
                            "tag": 101,
                            "entity": 7,
                            "type": "Prism 6",
                            "nodes": [1, 2, 3, 11, 12, 13],
                        }
                    ],
                    core_volume_records=[
                        {
                            "tag": 201,
                            "entity": 8,
                            "type": "Tetrahedron 4",
                            "nodes": [11, 12, 13, 99],
                        }
                    ],
                    minimum_prism_scaled_jacobian=0.01,
                    minimum_core_tetra_gamma=0.001,
                    minimum_cone_margin=1.0e-5,
                    minimum_changed_direction_margin=1.0e-4,
                    endpoint=endpoint,
                )
            )
        self.assertEqual(final_quality["status"], "PASS")
        self.assertEqual(search["endpoint_sha256"], endpoint["endpoint_sha256"])
        self.assertIn("interaction_topology", search)
        self.assertIn("final_component_recheck", search)
        self.assertEqual(search["abab_replay_status"], "PASS")
        self.assertTrue(
            all(
                call.kwargs["include_deficit_metrics"]
                for call in quality_mock.call_args_list
            )
        )

    def test_physical_replay_freshly_rebuilds_base_and_interaction_lineage(self):
        class _Mesh:
            def __init__(self, coordinates):
                self.coordinates = coordinates

            def getNode(self, node):
                return (self.coordinates[int(node)], [])

        root_coordinates = {
            1: (0.0, 0.0, 0.0),
            2: (1.0, 0.0, 0.0),
            3: (0.0, 1.0, 0.0),
            4: (2.0, 0.0, 0.0),
            5: (3.0, 0.0, 0.0),
            6: (2.0, 1.0, 0.0),
        }
        chains = {root: [root, root + 10] for root in root_coordinates}
        coordinates = {
            **root_coordinates,
            **{
                root + 10: (value[0], value[1], 0.1)
                for root, value in root_coordinates.items()
            },
            101: (9.0, 0.0, 0.0),
            102: (9.0, 1.0, 0.0),
            103: (9.0, 2.0, 0.0),
            104: (9.0, 3.0, 0.0),
        }
        gmsh = SimpleNamespace(
            model=SimpleNamespace(mesh=_Mesh(coordinates))
        )
        bad_triangles = [(1, 2, 3), (4, 5, 6)]
        triangle_ids = {
            triangle: hashlib.sha256(repr(triangle).encode("ascii")).hexdigest()
            for triangle in bad_triangles
        }
        triangle_walls = {
            bad_triangles[0]: "a" * 64,
            bad_triangles[1]: "b" * 64,
        }
        components, stable_components = build_owner_free_components(
            bad_triangles,
            triangle_stable_ids=triangle_ids,
            triangle_wall_fingerprints=triangle_walls,
            root_coordinates=root_coordinates,
        )
        prisms = [
            {
                "tag": 201,
                "entity": 301,
                "type": "Prism 6",
                "nodes": [1, 2, 3, 11, 12, 13],
            },
            {
                "tag": 202,
                "entity": 302,
                "type": "Prism 6",
                "nodes": [4, 5, 6, 14, 15, 16],
            },
        ]
        core = [
            {
                "tag": 401,
                "type": "Tetrahedron 4",
                "nodes": [11, 12, 13, 101],
            },
            {
                "tag": 402,
                "type": "Tetrahedron 4",
                "nodes": [14, 15, 16, 102],
            },
            {
                "tag": 403,
                "type": "Tetrahedron 4",
                "nodes": [13, 14, 103, 104],
            },
        ]
        common = {
            "base_runtime_components": components,
            "base_stable_components": stable_components,
            "root_coordinates": root_coordinates,
            "chains": chains,
            "subdivided_prisms": prisms,
            "core_volume_records": core,
            "prism_volume_fingerprints": {301: "a" * 64, 302: "b" * 64},
        }
        first = _rebuild_owner_free_interaction_topology(gmsh, **common)
        self.assertEqual(len(components), 2)
        self.assertEqual(
            first["interaction_topology"]["interaction_component_count"], 1
        )
        self.assertTrue(
            first["interaction_topology"]["base_components_merged"]
        )
        self.assertEqual(
            set(first["base_component_by_root"]), set(root_coordinates)
        )
        self.assertEqual(
            len(set(first["interaction_component_by_root"].values())), 1
        )

        # A fixed core-node coordinate participates in stable element lineage;
        # changing it must produce a freshly different interaction digest.
        coordinates[104] = (9.0, 3.5, 0.0)
        second = _rebuild_owner_free_interaction_topology(gmsh, **common)
        self.assertNotEqual(
            first["interaction_topology"]["interaction_topology_sha256"],
            second["interaction_topology"]["interaction_topology_sha256"],
        )

    def test_physical_replay_resolves_only_the_approved_source_subgraph(self):
        root_coordinates = {
            1: (0.0, 0.0, 0.0),
            2: (1.0, 0.0, 0.0),
            3: (0.0, 1.0, 0.0),
            4: (2.0, 0.0, 0.0),
        }
        chains = {root: [root, root + 10] for root in root_coordinates}
        approved_triangle = (1, 2, 3)
        physical_only_bad_triangle = (2, 3, 4)
        approved_triangle_id = _stable_triangle_id(
            approved_triangle, root_coordinates
        )
        contexts = {
            _stable_root_id(root_coordinates[root]): {
                "root_coordinate_sha256": _stable_root_id(
                    root_coordinates[root]
                ),
                "incident_bad_source_triangle_sha256": [
                    approved_triangle_id
                ],
            }
            for root in approved_triangle
        }
        result = _resolve_fixed_direction_replay_consensus_subgraph(
            expected_contexts=contexts,
            expected_candidate_root_count=3,
            root_coordinates=root_coordinates,
            chains=chains,
            source_triangles=[approved_triangle, physical_only_bad_triangle],
            current_bad_source_triangles=[
                approved_triangle,
                physical_only_bad_triangle,
            ],
            source_triangle_wall_fingerprints={
                approved_triangle: "a" * 64,
                physical_only_bad_triangle: "b" * 64,
            },
        )
        self.assertEqual(result["current_bad_source_triangle_count"], 2)
        self.assertEqual(set(result["candidate_roots"]), {1, 2, 3})
        self.assertNotIn(4, result["candidate_roots"])
        self.assertEqual(
            result["runtime_triangle_by_stable"],
            {approved_triangle_id: approved_triangle},
        )
        self.assertEqual(len(result["base_runtime_components"]), 1)

        with self.assertRaisesRegex(BoundaryLayerSmokeError, "source subset"):
            _resolve_fixed_direction_replay_consensus_subgraph(
                expected_contexts=contexts,
                expected_candidate_root_count=3,
                root_coordinates=root_coordinates,
                chains=chains,
                source_triangles=[approved_triangle],
                current_bad_source_triangles=[physical_only_bad_triangle],
                source_triangle_wall_fingerprints={
                    approved_triangle: "a" * 64,
                },
            )

    def test_physical_replay_root_schedule_assignments_are_stable_and_complete(self):
        roots = [7, 8, 9]
        coordinates = {
            7: (0.0, 0.0, 0.0),
            8: (1.0, 0.0, 0.0),
            9: (0.0, 1.0, 0.0),
        }
        chains = {root: [root, root + 10, root + 20] for root in roots}
        schedules = {
            7: [0.1, 0.3],
            8: [0.1, 0.25],
            9: [0.1, 0.2],
        }
        triangle = (7, 8, 9)
        other_incident_triangle = (7, 70, 71)
        all_source_triangles = [triangle, other_incident_triangle]
        all_source_walls = {
            triangle: "c" * 64,
            other_incident_triangle: "d" * 64,
        }
        evidence = _stable_root_schedule_assignments(
            candidate_roots=list(reversed(roots)),
            root_coordinates=coordinates,
            root_cumulative_heights=schedules,
            chains=chains,
            source_triangles=all_source_triangles,
            source_triangle_wall_fingerprints=all_source_walls,
        )
        records = evidence["root_schedule_assignments"]
        self.assertEqual(evidence["root_schedule_assignment_count"], 3)
        self.assertEqual(
            [record["root_coordinate_sha256"] for record in records],
            sorted(record["root_coordinate_sha256"] for record in records),
        )
        self.assertTrue(
            all(
                set(record)
                == {
                    "root_coordinate_sha256",
                    "incident_wall_surface_fingerprints",
                    "cumulative_heights_sha256",
                    "selected_total_thickness_float_hex",
                }
                for record in records
            )
        )
        root_seven_record = next(
            record
            for record in records
            if record["selected_total_thickness_float_hex"] == float(0.3).hex()
        )
        self.assertEqual(
            root_seven_record["incident_wall_surface_fingerprints"],
            ["c" * 64, "d" * 64],
        )
        expected_hash = hashlib.sha256(
            json.dumps(
                {"root_schedule_assignments": records},
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("ascii")
        ).hexdigest()
        self.assertEqual(
            evidence["root_schedule_assignments_sha256"], expected_hash
        )
        changed = copy.deepcopy(schedules)
        changed[7][-1] = 0.31
        changed_evidence = _stable_root_schedule_assignments(
            candidate_roots=roots,
            root_coordinates=coordinates,
            root_cumulative_heights=changed,
            chains=chains,
            source_triangles=all_source_triangles,
            source_triangle_wall_fingerprints=all_source_walls,
        )
        self.assertNotEqual(
            evidence["root_schedule_assignments_sha256"],
            changed_evidence["root_schedule_assignments_sha256"],
        )
        invalid = copy.deepcopy(schedules)
        invalid[8] = [0.1, 0.1]
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "non-monotone"):
            _stable_root_schedule_assignments(
                candidate_roots=roots,
                root_coordinates=coordinates,
                root_cumulative_heights=invalid,
                chains=chains,
                source_triangles=all_source_triangles,
                source_triangle_wall_fingerprints=all_source_walls,
            )

    def test_physical_schedule_homotopy_scan_is_absolute_and_reproducible(self):
        roots = (1, 2, 3)
        origins = {
            1: (0.0, 0.0, 0.0),
            2: (1.0, 0.0, 0.0),
            3: (0.0, 1.0, 0.0),
        }
        chains = {
            root: [root]
            + [1000 + root * 100 + layer for layer in range(1, 16)]
            for root in roots
        }
        coordinates = dict(origins)
        for root in roots:
            origin = origins[root]
            for layer, node in enumerate(chains[root][1:], start=1):
                coordinates[node] = (origin[0], origin[1], 0.01 * layer)
        # Regression: the production mapping contains non-root mesh nodes.
        coordinates[999] = (9.0, 9.0, 0.0)
        prism_tags = [200 + layer for layer in range(15)]
        subdivided_prisms = [
            {
                "tag": prism_tags[layer],
                "entity": 7,
                "type": "Prism 6",
                "nodes": [
                    *(chains[root][layer] for root in roots),
                    *(chains[root][layer + 1] for root in roots),
                ],
            }
            for layer in range(15)
        ]
        chain_origin = {
            int(node): (root, layer)
            for root in roots
            for layer, node in enumerate(chains[root])
        }
        core_tag = 9000
        core_records = [
            {
                "tag": core_tag,
                "entity": 8,
                "type": "Tetrahedron 4",
                "nodes": [1, 2, 3, 999],
            }
        ]
        gmsh = _HomotopyGmsh(coordinates, prism_tags, core_tag)
        anchor = _owner_free_quality_snapshot(
            gmsh,
            prism_element_tags=prism_tags,
            core_element_tags=[core_tag],
            core_tetra_element_tags=[core_tag],
            minimum_prism_scaled_jacobian=0.01,
            minimum_core_tetra_gamma=0.001,
            include_deficit_metrics=True,
        )
        self.assertEqual(anchor["status"], "PASS")
        fingerprint = "a" * 64
        endpoint = make_physical_schedule_homotopy_endpoint(
            baseline_binding_sha256="b" * 64,
            first_layer_height_m=0.01,
            layer_count=15,
        )
        result = _run_physical_schedule_homotopy_scan(
            gmsh,
            endpoint=endpoint,
            authoritative_surface_schedules={
                fingerprint: [0.01 + 0.02 * index for index in range(15)]
            },
            expected_surface_fingerprints=[fingerprint],
            source_prisms=[
                {
                    "tag": 100,
                    "entity": 7,
                    "type": "Prism 6",
                    "base_face": roots,
                }
            ],
            prism_volume_fingerprints={7: fingerprint},
            root_coordinates={**origins, 999: coordinates[999]},
            selected_directions={root: (0.0, 0.0, 1.0) for root in roots},
            chains=chains,
            chain_origin=chain_origin,
            subdivided_prisms=subdivided_prisms,
            core_volume_records=core_records,
            minimum_prism_scaled_jacobian=0.01,
            minimum_core_tetra_gamma=0.001,
            expected_minimum_growth_quality_sha256=anchor["quality_sha256"],
        )
        fraction_count = len(endpoint["fractions"])
        self.assertEqual(len(result["ascending_scan"]), fraction_count)
        self.assertEqual(len(result["descending_replay"]), fraction_count)
        self.assertEqual(result["final_quality"]["status"], "FAIL")
        self.assertEqual(
            result["maximum_tested_pass_fraction_float_hex"], (0.5).hex()
        )
        self.assertEqual(result["first_tested_fail_fraction_float_hex"], (1.0).hex())
        self.assertEqual(
            result["ascending_scan"][-1]["low_quality_aggregate"]["by_layer"],
            [{"layer": 15, "count": 1, "unique_source_triangle_count": 1}],
        )
        ascending = {
            record["fraction_float_hex"]: record
            for record in result["ascending_scan"]
        }
        for reverse in result["descending_replay"]:
            forward = ascending[reverse["fraction_float_hex"]]
            self.assertEqual(reverse["root_schedule_sha256"], forward["root_schedule_sha256"])
            self.assertEqual(reverse["coordinate_sha256"], forward["coordinate_sha256"])
            self.assertEqual(reverse["quality"], forward["quality"])
            self.assertEqual(
                reverse["low_quality_aggregate"], forward["low_quality_aggregate"]
            )
        updated_node_count = sum(len(chain) - 1 for chain in chains.values())
        self.assertEqual(gmsh.model.mesh.get_node_count, updated_node_count)
        self.assertEqual(
            gmsh.model.mesh.set_node_count,
            updated_node_count * fraction_count * 2,
        )
        self.assertEqual(gmsh.model.mesh.coordinates[999], (9.0, 9.0, 0.0))
        for root in roots:
            self.assertEqual(
                gmsh.model.mesh.coordinates[chains[root][1]][2].hex(),
                float(0.01).hex(),
            )

    def test_local_schedule_validation_accepts_only_scoped_homotopy_and_replay(self):
        physical_values = [0.01 + 0.02 * index for index in range(15)]
        raw_binding = {
            "schema": "cfdpipe.boundary_layer_local_schedule_binding.v1",
            "status": "PASS",
            "source_plan_sha256": "c" * 64,
            "layer_count": 15,
            "minimum_layer_count": 15,
            "first_layer_height_m": 0.01,
            "limited_surface_count": 0,
            "runtime_entity_tags_present": False,
            "surface_schedules": {
                fingerprint: {
                    "surface_fingerprint_id": fingerprint,
                    "cumulative_heights_m": list(physical_values),
                    "total_thickness_m": physical_values[-1],
                }
                for fingerprint in _FPS
            },
        }
        binding_hash = hashlib.sha256(
            json.dumps(
                raw_binding,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("ascii")
        ).hexdigest()
        bound = {**raw_binding, "binding_sha256": binding_hash}
        endpoint = make_physical_schedule_homotopy_endpoint(
            baseline_binding_sha256=binding_hash,
            first_layer_height_m=0.01,
            layer_count=15,
        )
        config = {
            "contract_mode": "coarse_repair_audit_only",
            "smoke_only": False,
            "calibration_only": False,
            "repair_audit_only": True,
            "projection_only": False,
            "calibration_PASS_authorized": False,
            "production_mesh_eligible": False,
            "mesh_written": False,
            "su2_called": False,
            "paraview_called": False,
            "layer_count": 15,
            "first_layer_height_m": 0.01,
            "wall_surface_fingerprints": list(_FPS),
            "local_boundary_layer_schedule": bound,
            "owner_free_direction_replay_approval": {"placeholder": True},
            "schedule_feasibility_endpoint": endpoint,
            "audit_purpose": "physical_local_schedule_fixed_direction_homotopy",
            "fixed_direction_replay_only": True,
            "combined_endpoint_only": True,
            "coarse_contract_sha256": "d" * 64,
            "characteristic_length_m": 1.0,
            "quality_improvement": {
                "minimum_prism_scaled_jacobian": 0.01,
                "minimum_core_tetra_gamma": 0.001,
            },
        }
        with patch(
            "cfdpipe.boundary_layer_smoke."
            "validate_boundary_layer_schedule_binding_for_strategy",
            return_value=bound,
        ), patch(
            "cfdpipe.boundary_layer_smoke."
            "_validated_direction_replay_approval_for_config",
            return_value={"approval_sha256": "e" * 64},
        ):
            schedules, evidence = _validated_local_surface_schedules(config)
        self.assertEqual(set(schedules), set(_FPS))
        self.assertTrue(all(values[0] == 0.01 for values in schedules.values()))
        self.assertTrue(all(values[-1] == 0.15 for values in schedules.values()))
        self.assertTrue(evidence["minimum_growth_schedule_used"])
        self.assertEqual(
            evidence["physical_schedule_homotopy_endpoint"], endpoint
        )
        self.assertEqual(
            evidence["schedule_feasibility_application"]["fraction_float_hex"],
            (0.0).hex(),
        )
        self.assertEqual(
            evidence["authoritative_physical_surface_schedules"][_FPS[0]],
            physical_values,
        )

        continuation = {
            "schema": CONTINUATION_ENDPOINT_SCHEMA,
            "source_bindings": {
                "homotopy_manifest_sha256": "1" * 64,
                "homotopy_discovery_sha256": "2" * 64,
                "homotopy_endpoint_sha256": endpoint["endpoint_sha256"],
                "direction_replay_approval_sha256": "e" * 64,
                "local_schedule_binding_sha256": binding_hash,
            },
            "previous_quality_sha256": "3" * 64,
            "target_quality_sha256": "4" * 64,
            "target_low_quality_aggregate_sha256": "5" * 64,
            "target_observation": {
                "low_quality_prism_count": 37,
                "unique_source_triangle_count": 5,
                "wall_count": 2,
                "core_tetra_below_gamma_count": 0,
                "nonpositive_element_count": 0,
            },
        }
        continuation_config = dict(config)
        continuation_config.update(
            {
                "schedule_direction_continuation_endpoint": continuation,
                "audit_purpose": (
                    "physical_schedule_first_frontier_direction_continuation"
                ),
                "first_frontier_direction_continuation_only": True,
            }
        )
        with patch(
            "cfdpipe.boundary_layer_smoke."
            "validate_boundary_layer_schedule_binding_for_strategy",
            return_value=bound,
        ), patch(
            "cfdpipe.boundary_layer_smoke."
            "_validated_direction_replay_approval_for_config",
            return_value={"approval_sha256": "e" * 64},
        ), patch(
            "cfdpipe.boundary_layer_smoke.validate_frontier_direction_endpoint",
            return_value=continuation,
        ):
            continuation_schedules, continuation_evidence = (
                _validated_local_surface_schedules(continuation_config)
            )
        self.assertEqual(continuation_schedules, schedules)
        self.assertEqual(
            continuation_evidence["physical_schedule_homotopy_endpoint"],
            endpoint,
        )
        self.assertEqual(
            continuation_evidence["owner_free_direction_replay_approval"],
            {"approval_sha256": "e" * 64},
        )
        self.assertEqual(
            continuation_evidence[
                "schedule_direction_continuation_endpoint"
            ],
            continuation,
        )
        missing_continuation_scope = dict(continuation_config)
        missing_continuation_scope[
            "first_frontier_direction_continuation_only"
        ] = False
        with patch(
            "cfdpipe.boundary_layer_smoke."
            "validate_boundary_layer_schedule_binding_for_strategy",
            return_value=bound,
        ):
            with self.assertRaisesRegex(BoundaryLayerSmokeError, "scope"):
                _validated_local_surface_schedules(
                    missing_continuation_scope
                )

        unsafe = dict(config)
        unsafe["combined_endpoint_only"] = False
        with patch(
            "cfdpipe.boundary_layer_smoke."
            "validate_boundary_layer_schedule_binding_for_strategy",
            return_value=bound,
        ):
            with self.assertRaisesRegex(BoundaryLayerSmokeError, "scope"):
                _validated_local_surface_schedules(unsafe)

    def test_post_hook_schema_distinguishes_fixed_replay_from_homotopy(self):
        replay_approval = {"schema": "approval-placeholder"}
        self.assertEqual(
            _expected_repair_audit_schema(
                {"owner_free_direction_replay_approval": replay_approval}
            ),
            "cfdpipe.coarse_physical_schedule_fixed_direction_discovery.v1",
        )
        self.assertEqual(
            _expected_repair_audit_schema(
                {
                    "owner_free_direction_replay_approval": replay_approval,
                    "physical_schedule_homotopy_endpoint": {
                        "schema": "endpoint-placeholder"
                    },
                }
            ),
            HOMOTOPY_DISCOVERY_SCHEMA,
        )
        self.assertEqual(
            _expected_repair_audit_schema(
                {
                    "owner_free_direction_replay_approval": replay_approval,
                    "physical_schedule_homotopy_endpoint": {
                        "schema": "endpoint-placeholder"
                    },
                    "schedule_direction_continuation_endpoint": {
                        "schema": "continuation-placeholder"
                    },
                }
            ),
            CONTINUATION_DISCOVERY_SCHEMA,
        )


if __name__ == "__main__":
    unittest.main()
