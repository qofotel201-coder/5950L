"""Mock-only tests for stable-fingerprint BREP topology-smoke meshing."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from cfdpipe.topology_smoke_mesh import (
    TopologySmokeMeshError,
    build_topology_smoke_mesh,
)


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


class _Logger:
    def __init__(self, owner: "_FakeGmsh") -> None:
        self.owner = owner

    def start(self) -> None:
        self.owner.calls.append(("logger.start",))

    def get(self) -> list[str]:
        return list(self.owner.logger_messages)

    def stop(self) -> None:
        self.owner.calls.append(("logger.stop",))


class _Option:
    def __init__(self, owner: "_FakeGmsh") -> None:
        self.owner = owner

    def setString(self, name: str, value: str) -> None:
        self.owner.calls.append(("setString", name, value))

    def setNumber(self, name: str, value: float) -> None:
        self.owner.calls.append(("setNumber", name, value))


class _Occ:
    def __init__(self, owner: "_FakeGmsh") -> None:
        self.owner = owner

    def importShapes(self, path: str) -> list[tuple[int, int]]:
        self.owner.calls.append(("importShapes", path))
        return [(3, tag) for tag in self.owner.volume_tags]

    def synchronize(self) -> None:
        self.owner.calls.append(("synchronize",))

    def getMass(self, dimension: int, tag: int) -> float:
        record = (
            self.owner.surfaces[tag]
            if dimension == 2
            else self.owner.volumes[tag]
        )
        return float(record["mass"])

    def getCenterOfMass(self, dimension: int, tag: int) -> list[float]:
        record = (
            self.owner.surfaces[tag]
            if dimension == 2
            else self.owner.volumes[tag]
        )
        return list(record["centroid"])

    def getBoundingBox(self, dimension: int, tag: int) -> list[float]:
        record = (
            self.owner.surfaces[tag]
            if dimension == 2
            else self.owner.volumes[tag]
        )
        return list(record["bounds"])


class _Field:
    def __init__(self, owner: "_FakeGmsh") -> None:
        self.owner = owner
        self.next_tag = 1

    def add(self, field_type: str) -> int:
        tag = self.next_tag
        self.next_tag += 1
        self.owner.calls.append(("field.add", field_type, tag))
        return tag

    def setNumbers(self, tag: int, option: str, values: list[int]) -> None:
        self.owner.calls.append(("field.setNumbers", tag, option, list(values)))

    def setNumber(self, tag: int, option: str, value: float) -> None:
        self.owner.calls.append(("field.setNumber", tag, option, value))

    def setAsBackgroundMesh(self, tag: int) -> None:
        self.owner.calls.append(("field.setAsBackgroundMesh", tag))


class _Mesh:
    def __init__(self, owner: "_FakeGmsh") -> None:
        self.owner = owner
        self.field = _Field(owner)

    def generate(self, dimension: int) -> None:
        self.owner.calls.append(("generate", dimension))
        if self.owner.fail_generate:
            raise RuntimeError("generate sentinel")

    def getLastEntityError(self):
        return self.owner.last_entity_errors

    def getLastNodeError(self):
        return self.owner.last_node_errors

    def getNodes(self):
        coordinates = [
            coordinate
            for node in self.owner.node_tags
            for coordinate in (float(node), float(node % 3), float(node % 5))
        ]
        return list(self.owner.node_tags), coordinates, []

    def getElements(self, dimension: int = -1, tag: int = -1):
        if dimension == 3:
            if tag == -1:
                connectivity = {
                    element_tag: nodes
                    for volume in self.owner.volume_tetrahedra.values()
                    for element_tag, nodes in volume.items()
                }
            else:
                connectivity = self.owner.volume_tetrahedra.get(int(tag), {})
            elements = sorted(connectivity)
            return [4], [elements], [
                [node for element in elements for node in connectivity[element]]
            ]
        if dimension == 2:
            if tag == -1:
                connectivity = {
                    element_tag: nodes
                    for surface in self.owner.surface_triangles.values()
                    for element_tag, nodes in surface.items()
                }
            else:
                connectivity = self.owner.surface_triangles.get(int(tag), {})
            elements = sorted(connectivity)
            return [2], [elements], [
                [node for element in elements for node in connectivity[element]]
            ]
        return [], [], []

    def getElementProperties(self, element_type: int):
        if element_type == 4:
            return "Tetrahedron 4", 3, 1, 4, [], 4
        if element_type == 2:
            return "Triangle 3", 2, 1, 3, [], 3
        raise AssertionError(f"unexpected element type {element_type}")

    def getElementQualities(self, element_tags, quality_name="minSICN", task=0, numTasks=1):
        value = self.owner.quality_values[quality_name]
        return [value for _ in element_tags]


class _Model:
    def __init__(self, owner: "_FakeGmsh") -> None:
        self.owner = owner
        self.occ = _Occ(owner)
        self.mesh = _Mesh(owner)

    def add(self, name: str) -> None:
        self.owner.calls.append(("model.add", name))

    def getEntities(self, dimension: int):
        if dimension == 2:
            return [(2, tag) for tag in sorted(self.owner.surfaces)]
        if dimension == 3:
            return [(3, tag) for tag in self.owner.volume_tags]
        return []

    def getEntityName(self, dimension: int, tag: int) -> str:
        return ""

    def getType(self, dimension: int, tag: int) -> str:
        return "Plane" if dimension == 2 else "Volume"

    def getAdjacencies(self, dimension: int, tag: int):
        if dimension == 2:
            return (
                list(self.owner.surfaces[tag]["adjacent"]),
                list(self.owner.surface_curves[tag]),
            )
        return [], list(self.owner.volumes[tag]["boundary"])

    def getBoundary(self, dimtags, combined=False, oriented=True, recursive=False):
        volume = int(dimtags[0][1])
        result = []
        for surface in self.owner.volumes[volume]["boundary"]:
            sign = 1
            if surface in self.owner.shared_tags and volume == self.owner.volume_tags[0]:
                sign = -1
            result.append((2, sign * surface))
        return result

    def addPhysicalGroup(self, dimension: int, tags: list[int]) -> int:
        physical = len(self.owner.physical_groups) + 1
        self.owner.physical_groups[physical] = {
            "dimension": dimension,
            "tags": list(tags),
            "name": None,
        }
        return physical

    def setPhysicalName(self, dimension: int, physical: int, name: str) -> None:
        self.owner.physical_groups[physical]["name"] = name


class _FakeGmsh:
    __version__ = "4.15.2-test"
    __file__ = __file__

    def __init__(self, *, reverse_tags: bool = True) -> None:
        self.calls: list[tuple] = []
        self.initialized = False
        self.fail_generate = False
        self.invalid_su2_node = False
        self.logger_messages = ["Info : mock topology-smoke"]
        self.last_entity_errors = []
        self.last_node_errors = []
        self.quality_values = {
            "minDetJac": 0.25,
            "minSICN": 0.5,
            "volume": 0.125,
        }
        identities = list(range(1, 56))
        live_tags = list(range(1001, 1056))
        if reverse_tags:
            live_tags.reverse()
        self.identity_to_tag = dict(zip(identities, live_tags, strict=True))
        self.shared_tags = [3001, 3002]
        self.volume_tags = [4001, 4002]
        self.surfaces: dict[int, dict[str, object]] = {}
        for identity, tag in self.identity_to_tag.items():
            adjacent = [self.volume_tags[0] if identity <= 51 else self.volume_tags[1]]
            self.surfaces[tag] = {
                "mass": float(identity),
                "centroid": [float(identity), 0.0, 0.0],
                "bounds": [float(identity), 0.0, 0.0, float(identity) + 0.5, 1.0, 1.0],
                "adjacent": adjacent,
            }
        for offset, tag in enumerate(self.shared_tags):
            self.surfaces[tag] = {
                "mass": 500.0 + offset,
                "centroid": [0.0, float(offset), 0.0],
                "bounds": [0.0, float(offset), 0.0, 1.0, float(offset) + 1.0, 1.0],
                "adjacent": list(self.volume_tags),
            }
        self.surface_curves = {
            tag: [tag * 10 + 1, tag * 10 + 2]
            for tag in self.surfaces
        }
        first_boundary = [self.identity_to_tag[value] for value in range(1, 52)] + self.shared_tags
        second_boundary = [self.identity_to_tag[value] for value in range(52, 56)] + self.shared_tags
        self.volumes = {
            self.volume_tags[0]: {
                "mass": 100.0,
                "centroid": [10.0, 0.0, 0.0],
                "bounds": [0.0, 0.0, 0.0, 10.0, 1.0, 1.0],
                "boundary": first_boundary,
            },
            self.volume_tags[1]: {
                "mass": 200.0,
                "centroid": [20.0, 0.0, 0.0],
                "bounds": [10.0, 0.0, 0.0, 20.0, 1.0, 1.0],
                "boundary": second_boundary,
            },
        }
        first_sequence = list(range(1, 31))
        second_sequence = [1, 2, 3, 28, 29, 30]
        first_tetrahedra = {
            index + 1: tuple(first_sequence[index : index + 4])
            for index in range(27)
        }
        second_tetrahedra = {
            28 + index: tuple(second_sequence[index : index + 4])
            for index in range(3)
        }
        self.volume_tetrahedra = {
            self.volume_tags[0]: first_tetrahedra,
            self.volume_tags[1]: second_tetrahedra,
        }
        self.element_count = sum(
            len(elements) for elements in self.volume_tetrahedra.values()
        )
        self.node_tags = sorted(
            {
                node
                for elements in self.volume_tetrahedra.values()
                for connectivity in elements.values()
                for node in connectivity
            }
        )

        face_owners: dict[tuple[int, int, int], list[int]] = {}
        for volume_tag, elements in self.volume_tetrahedra.items():
            for nodes in elements.values():
                faces = (
                    (nodes[0], nodes[1], nodes[2]),
                    (nodes[0], nodes[1], nodes[3]),
                    (nodes[0], nodes[2], nodes[3]),
                    (nodes[1], nodes[2], nodes[3]),
                )
                for face in faces:
                    face_owners.setdefault(tuple(sorted(face)), []).append(volume_tag)
        external_by_volume = {
            volume_tag: sorted(
                face
                for face, owners in face_owners.items()
                if owners == [volume_tag]
            )
            for volume_tag in self.volume_tags
        }
        shared_faces = sorted(
            face
            for face, owners in face_owners.items()
            if len(owners) == 2 and set(owners) == set(self.volume_tags)
        )
        assert len(external_by_volume[self.volume_tags[0]]) == 54
        assert len(external_by_volume[self.volume_tags[1]]) == 6
        assert shared_faces == [(1, 2, 3), (28, 29, 30)]

        surface_faces: dict[int, list[tuple[int, int, int]]] = {}

        def distribute(
            identities_for_volume: list[int], faces: list[tuple[int, int, int]]
        ) -> None:
            for offset, face in enumerate(faces):
                identity = identities_for_volume[offset % len(identities_for_volume)]
                surface_faces.setdefault(self.identity_to_tag[identity], []).append(face)

        distribute(list(range(1, 52)), external_by_volume[self.volume_tags[0]])
        distribute(list(range(52, 56)), external_by_volume[self.volume_tags[1]])
        for tag, face in zip(self.shared_tags, shared_faces, strict=True):
            surface_faces[tag] = [face]
        self.surface_triangles: dict[int, dict[int, tuple[int, int, int]]] = {}
        next_element_tag = 10_001
        for surface_tag in sorted(surface_faces):
            self.surface_triangles[surface_tag] = {}
            for face in surface_faces[surface_tag]:
                self.surface_triangles[surface_tag][next_element_tag] = face
                next_element_tag += 1
        self.physical_groups: dict[int, dict[str, object]] = {}
        self.model = _Model(self)
        self.option = _Option(self)
        self.logger = _Logger(self)

    def isInitialized(self) -> bool:
        return self.initialized

    def initialize(self, *, readConfigFiles: bool = True) -> None:
        self.calls.append(("initialize", readConfigFiles))
        self.initialized = True

    def finalize(self) -> None:
        self.calls.append(("finalize",))
        self.initialized = False

    def write(self, path_value: str) -> None:
        path = Path(path_value)
        self.calls.append(("write", path.suffix.casefold()))
        if path.suffix.casefold() == ".msh":
            path.write_text("mock MSH\n", encoding="utf-8")
            return
        surface_names = sorted(
            str(group["name"])
            for group in self.physical_groups.values()
            if group["dimension"] == 2
        )
        node_indices = {node: index for index, node in enumerate(self.node_tags)}
        tetrahedra = {
            element_tag: nodes
            for volume in self.volume_tetrahedra.values()
            for element_tag, nodes in volume.items()
        }
        lines = [
            "NDIME= 3",
            f"NELEM= {self.element_count}",
        ]
        for element_tag in sorted(tetrahedra):
            nodes = tetrahedra[element_tag]
            lines.append(
                "10 "
                + " ".join(str(node_indices[node]) for node in nodes)
                + f" {element_tag - 1}"
            )
        lines.append(f"NPOIN= {len(self.node_tags)}")
        for node in self.node_tags:
            index = node_indices[node]
            lines.append(f"{float(node)} {float(node % 3)} {float(node % 5)} {index}")
        lines.append(f"NMARK= {len(surface_names)}")
        groups_by_name = {
            str(group["name"]): group
            for group in self.physical_groups.values()
            if group["dimension"] == 2
        }
        for name in surface_names:
            group = groups_by_name[name]
            triangles = [
                (element_tag, nodes)
                for surface_tag in group["tags"]
                for element_tag, nodes in self.surface_triangles[int(surface_tag)].items()
            ]
            lines.extend((f"MARKER_TAG= {name}", f"MARKER_ELEMS= {len(triangles)}"))
            for element_tag, nodes in triangles:
                indices = [node_indices[node] for node in nodes]
                if self.invalid_su2_node:
                    indices[0] = len(self.node_tags) + 99
                lines.append(
                    "5 " + " ".join(str(index) for index in indices) + f" {element_tag}"
                )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _surface_geometry(identity: int) -> dict[str, object]:
    return {
        "area_m2": float(identity),
        "centroid_m": [float(identity), 0.0, 0.0],
        "bounding_box_m": [
            float(identity),
            0.0,
            0.0,
            float(identity) + 0.5,
            1.0,
            1.0,
        ],
        "entity_type": "Plane",
        "adjacent_volume_count": 1,
    }


def _marker_config(brep: Path) -> dict[str, object]:
    derived_hash = hashlib.sha256(brep.read_bytes()).hexdigest()
    groups = (
        ("front_conical_surface", range(1, 3), "farfield"),
        ("vehicle_internal_and_external_walls", range(3, 51), "wall"),
        ("rear_outlet_1", range(51, 54), "rear_outlet"),
        ("rear_outlet_2", range(54, 56), "rear_outlet"),
    )
    markers: list[dict[str, object]] = []
    for name, identities, kind in groups:
        members: list[dict[str, object]] = []
        member_ids: list[str] = []
        for identity in identities:
            member_payload = {
                "original_surface_fingerprint_id": hashlib.sha256(
                    f"source-{identity}".encode()
                ).hexdigest(),
                **_surface_geometry(identity),
            }
            fingerprint_id = _canonical_sha256(member_payload)
            member_ids.append(fingerprint_id)
            members.append({"fingerprint_id": fingerprint_id, **member_payload})
        markers.append(
            {
                "physical_name": name,
                "semantic_role": name,
                "kind": kind,
                "dimension": 2,
                "solver_boundary": True,
                "create_physical_group": True,
                "group_fingerprint_id": _canonical_sha256(
                    {
                        "physical_name": name,
                        "member_fingerprint_ids": sorted(member_ids),
                    }
                ),
                "member_count": len(members),
                "member_fingerprint_ids": sorted(member_ids),
                "members": members,
            }
        )
    volume_definitions = (
        (100.0, [10.0, 0.0, 0.0], [0.0, 0.0, 0.0, 10.0, 1.0, 1.0], 53),
        (200.0, [20.0, 0.0, 0.0], [10.0, 0.0, 0.0, 20.0, 1.0, 1.0], 6),
    )
    volume_selectors = []
    for mass, centroid, bounds, boundary_count in volume_definitions:
        selector = {
            "schema": "cfdpipe.volume_geometry_selector.v1",
            "derived_cad_sha256": derived_hash,
            "entity_type": "Volume",
            "volume_m3": mass,
            "centroid_m": centroid,
            "bounding_box_m": bounds,
            "boundary_surface_count": boundary_count,
        }
        volume_selectors.append(
            {"fingerprint_id": _canonical_sha256(selector), **selector}
        )
    payload = {
        "schema": "cfdpipe.markers.v2",
        "status": "PASS",
        "source": {
            "path": "geometry/raw/source.step",
            "sha256": "a" * 64,
            "read_only": True,
        },
        "pipeline_geometry": {
            "path": str(brep),
            "sha256": derived_hash,
            "format": "brep",
            "pipeline_eligible": True,
            "read_only": True,
        },
        "topology": {
            "volume_count": 2,
            "unique_surface_count": 57,
            "external_surface_count": 55,
            "shared_surface_count": 2,
        },
        "matching": {
            "surface_area_relative_tolerance": 2.0e-8,
            "surface_area_absolute_tolerance_m2": 2.0e-9,
            "coordinate_absolute_tolerance_m": 2.0e-7,
            "interface_surface_distance_tolerance_m": 1.0e-7,
            "interface_boundary_distance_tolerance_m": 1.0e-7,
            "interface_normal_dot_tolerance": 1.0e-7,
            "unique_match_required": True,
            "runtime_tags_are_matching_criteria": False,
        },
        "provenance": {},
        "solver_markers": markers,
        "fluid": {
            "physical_name": "fluid",
            "semantic_role": "fluid",
            "dimension": 3,
            "solver_boundary": False,
            "create_physical_group": True,
            "volume_count": 2,
            "selector_policy": "canonical_volume_geometry_unique_match",
            "volume_selectors": volume_selectors,
        },
        "measurement": {
            "name": "turning_section_outlet",
            "semantic_role": "turning_section_outlet",
            "kind": "measurement_surface",
            "dimension": 2,
            "solver_boundary": False,
            "create_physical_group": False,
            "postprocess_only": True,
            "fingerprint_id": "b" * 64,
            "confirmed_measurement_candidate_id": "turning-section-outlet",
            "derived_boundary_fingerprint_id": "c" * 64,
            "geometry": {
                "plane_axis": "x",
                "plane_coordinate_m": 3.77,
                "normal_unit": [1.0, 0.0, 0.0],
                "bounds_m": [3.77, -1.0, -1.0, 3.77, 1.0, 1.0],
                "area_m2": 1.0,
                "closed": True,
            },
        },
        "policy": {
            "runtime_entity_identifiers_present": False,
            "step_names_present": False,
            "solver_boundary_groups_complete": True,
            "external_surface_members_disjoint_and_exhaustive": True,
            "measurement_is_physical_group": False,
            "measurement_is_solver_boundary": False,
            "pipeline_geometry_is_brep": True,
        },
    }
    return {
        **payload,
        "integrity": {
            "algorithm": "SHA-256",
            "canonicalization": "UTF-8 JSON, sorted keys, compact separators, integrity excluded",
            "payload_sha256": _canonical_sha256(payload),
        },
    }


def _rehash_surface_groups(config: dict[str, object]) -> None:
    markers = config["solver_markers"]
    assert isinstance(markers, list)
    for raw in markers:
        assert isinstance(raw, dict)
        members = raw["members"]
        assert isinstance(members, list)
        ids = []
        for member in members:
            assert isinstance(member, dict)
            member["fingerprint_id"] = _canonical_sha256(
                {key: value for key, value in member.items() if key != "fingerprint_id"}
            )
            ids.append(member["fingerprint_id"])
        raw["member_fingerprint_ids"] = sorted(ids)
        raw["group_fingerprint_id"] = _canonical_sha256(
            {
                "physical_name": raw["physical_name"],
                "member_fingerprint_ids": sorted(ids),
            }
        )
    payload = {key: value for key, value in config.items() if key != "integrity"}
    integrity = config["integrity"]
    assert isinstance(integrity, dict)
    integrity["payload_sha256"] = _canonical_sha256(payload)


class TopologySmokeMeshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.brep = self.root / "derived.brep"
        self.brep.write_bytes(b"repaired BREP fixture")
        os.chmod(self.brep, stat.S_IREAD)
        self.addCleanup(os.chmod, self.brep, stat.S_IREAD | stat.S_IWRITE)
        self.config = _marker_config(self.brep)

    def _run(self, gmsh: _FakeGmsh, name: str = "mesh", **kwargs):
        mesh_options = kwargs.pop(
            "mesh_options", {"characteristic_length_m": 0.25}
        )
        return build_topology_smoke_mesh(
            self.brep,
            self.root / name,
            self.config,
            mesh_options=mesh_options,
            gmsh_module=gmsh,
            **kwargs,
        )

    def test_tag_reordering_is_resolved_from_fingerprints_and_outputs_publish(self) -> None:
        gmsh = _FakeGmsh(reverse_tags=True)

        manifest = self._run(gmsh)

        self.assertEqual(manifest["status"], "PASS")
        self.assertTrue(manifest["source_unchanged"])
        self.assertTrue(manifest["finalize_called"])
        self.assertEqual(manifest["mesh"]["element_count_3d"], 30)
        self.assertEqual(manifest["topology"]["shared_patch_count"], 2)
        proof = manifest["mesh"]["connectivity_proof"]
        self.assertEqual(proof["external_boundary_face_count"], 60)
        self.assertEqual(proof["marker_triangle_face_count"], 60)
        self.assertEqual(proof["cross_volume_face_count"], 2)
        self.assertEqual(proof["shared_surface_triangle_face_count"], 2)
        self.assertTrue(proof["each_shared_face_has_one_owner_per_fluid_volume"])
        resolved = manifest["resolved_physical_groups_audit"]
        self.assertEqual(resolved["fluid"]["entity_tags"], gmsh.volume_tags)
        all_surface_tags = {
            tag
            for group in resolved.values()
            if group["dimension"] == 2
            for tag in group["entity_tags"]
        }
        self.assertEqual(len(all_surface_tags), 55)
        self.assertFalse(all_surface_tags.intersection(gmsh.shared_tags))
        self.assertIn(("initialize", False), gmsh.calls)
        self.assertEqual(gmsh.calls[-1], ("finalize",))
        output = self.root / "mesh"
        self.assertEqual(
            {path.name for path in output.iterdir()},
            {
                "mesh.msh",
                "mesh.su2",
                "gmsh_topology_smoke.log",
                "topology_smoke_manifest.json",
            },
        )

    def test_local_refinement_resolves_stable_fingerprints_after_tag_reordering(self) -> None:
        gmsh = _FakeGmsh(reverse_tags=True)
        members = self.config["solver_markers"][1]["members"]
        fingerprints = [members[0]["fingerprint_id"], members[5]["fingerprint_id"]]

        manifest = self._run(
            gmsh,
            "refined",
            mesh_options={
                "characteristic_length_m": 0.25,
                "local_refinement": {
                    "surface_fingerprint_ids": fingerprints,
                    "minimum_size_m": 0.012,
                    "distance_max_m": 0.08,
                    "sampling": 100,
                },
            },
        )

        audit = manifest["mesh_options"]["local_refinement"]
        expected_surfaces = sorted(
            [gmsh.identity_to_tag[3], gmsh.identity_to_tag[8]]
        )
        expected_curves = sorted(
            curve
            for surface in expected_surfaces
            for curve in gmsh.surface_curves[surface]
        )
        self.assertEqual(audit["resolved_surface_tags_audit"], expected_surfaces)
        self.assertEqual(audit["resolved_curve_tags_audit"], expected_curves)
        self.assertFalse(audit["runtime_tags_are_matching_criteria"])
        self.assertIn(("field.add", "Distance", 1), gmsh.calls)
        self.assertIn(
            ("field.setNumbers", 1, "CurvesList", expected_curves), gmsh.calls
        )
        self.assertIn(("field.setAsBackgroundMesh", 2), gmsh.calls)
        self.assertIn(("setNumber", "Mesh.MeshSizeMin", 0.012), gmsh.calls)

    def test_unknown_local_refinement_fingerprint_fails_before_generate(self) -> None:
        gmsh = _FakeGmsh()

        with self.assertRaisesRegex(TopologySmokeMeshError, "zero matches"):
            self._run(
                gmsh,
                "unknown-refinement",
                mesh_options={
                    "characteristic_length_m": 0.25,
                    "local_refinement": {
                        "surface_fingerprint_ids": ["f" * 64],
                        "minimum_size_m": 0.012,
                        "distance_max_m": 0.08,
                        "sampling": 100,
                    },
                },
            )

        self.assertNotIn(("generate", 3), gmsh.calls)
        self.assertEqual(gmsh.calls[-1], ("finalize",))

    def test_zero_surface_match_fails_before_meshing_and_publishes_only_evidence(self) -> None:
        gmsh = _FakeGmsh()
        markers = self.config["solver_markers"]
        member = markers[0]["members"][0]
        member["area_m2"] = 9999.0
        _rehash_surface_groups(self.config)

        with self.assertRaisesRegex(TopologySmokeMeshError, "zero matches"):
            self._run(gmsh, "zero")

        self.assertNotIn(("generate", 3), gmsh.calls)
        self.assertEqual(gmsh.calls[-1], ("finalize",))
        output = self.root / "zero"
        self.assertEqual(
            {path.name for path in output.iterdir()},
            {"gmsh_topology_smoke.log", "topology_smoke_manifest.json"},
        )
        failure = json.loads(
            (output / "topology_smoke_manifest.json").read_text(encoding="utf-8")
        )
        self.assertIn("zero matches", failure["error"]["traceback"])

    def test_multiple_surface_match_fails_global_uniqueness(self) -> None:
        gmsh = _FakeGmsh()
        first_tag = gmsh.identity_to_tag[1]
        second_tag = gmsh.identity_to_tag[2]
        gmsh.surfaces[second_tag] = {
            **gmsh.surfaces[second_tag],
            "mass": gmsh.surfaces[first_tag]["mass"],
            "centroid": list(gmsh.surfaces[first_tag]["centroid"]),
            "bounds": list(gmsh.surfaces[first_tag]["bounds"]),
        }
        markers = self.config["solver_markers"]
        members = markers[0]["members"]
        for key in (
            "entity_type",
            "area_m2",
            "centroid_m",
            "bounding_box_m",
            "adjacent_volume_count",
        ):
            members[1][key] = members[0][key]
        members[1]["original_surface_fingerprint_id"] = "d" * 64
        _rehash_surface_groups(self.config)

        with self.assertRaisesRegex(TopologySmokeMeshError, "ambiguous"):
            self._run(gmsh, "multiple")

        self.assertNotIn(("generate", 3), gmsh.calls)
        self.assertEqual(gmsh.calls[-1], ("finalize",))

    def test_element_cap_is_checked_before_any_mesh_write(self) -> None:
        gmsh = _FakeGmsh()

        with self.assertRaisesRegex(TopologySmokeMeshError, "exceeds cap"):
            self._run(gmsh, "cap", max_elements=29)

        self.assertFalse(any(call[0] == "write" for call in gmsh.calls))
        self.assertEqual(gmsh.calls[-1], ("finalize",))

    def test_nonpositive_quality_is_rejected_before_any_mesh_write(self) -> None:
        gmsh = _FakeGmsh()
        gmsh.quality_values["minDetJac"] = 0.0

        with self.assertRaisesRegex(TopologySmokeMeshError, "non-positive"):
            self._run(gmsh, "quality")

        self.assertFalse(any(call[0] == "write" for call in gmsh.calls))
        self.assertEqual(gmsh.calls[-1], ("finalize",))

    def test_generate_exception_still_finalizes_and_retains_original_traceback(self) -> None:
        gmsh = _FakeGmsh()
        gmsh.fail_generate = True

        with self.assertRaisesRegex(TopologySmokeMeshError, "generate sentinel"):
            self._run(gmsh, "exception")

        self.assertEqual(gmsh.calls[-1], ("finalize",))
        failure = json.loads(
            (self.root / "exception" / "topology_smoke_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(failure["status"], "FAIL")
        self.assertEqual(failure["error"]["message"], "generate sentinel")
        self.assertIn("RuntimeError: generate sentinel", failure["error"]["traceback"])

    def test_cad_failure_maps_runtime_tags_to_stable_fingerprints(self) -> None:
        class _ArrayLike:
            def __init__(self, values) -> None:
                self.values = values

            def __iter__(self):
                return iter(self.values)

        gmsh = _FakeGmsh()
        first_tag = gmsh.identity_to_tag[3]
        second_tag = gmsh.identity_to_tag[4]
        gmsh.fail_generate = True
        gmsh.last_entity_errors = _ArrayLike(
            [_ArrayLike([2, second_tag]), _ArrayLike([2, first_tag])]
        )
        gmsh.last_node_errors = _ArrayLike([45, 283])
        gmsh.logger_messages = [
            "Info: Found two nearly self-intersecting facets (dihedral angle  1.70589E-03).",
            f"Info:   1st: [283, 45, 211] #{first_tag}",
            f"Info:   2nd: [283, 45, 341] #{second_tag}",
            "Info: The dihedral angle between them is 0.0977404 degree.",
            f"Error: Invalid boundary mesh (overlapping facets) on surface {first_tag} surface {second_tag}",
        ]

        with self.assertRaisesRegex(TopologySmokeMeshError, "generate sentinel"):
            self._run(gmsh, "cad-failure")

        output = self.root / "cad-failure"
        self.assertEqual(
            {path.name for path in output.iterdir()},
            {
                "gmsh_topology_smoke.log",
                "topology_smoke_manifest.json",
                "cad_repair_request.json",
            },
        )
        failure = json.loads(
            (output / "topology_smoke_manifest.json").read_text(encoding="utf-8")
        )
        diagnostics = failure["mesh_failure_diagnostics"]
        self.assertTrue(diagnostics["fatal"])
        self.assertEqual(diagnostics["localization_status"], "RESOLVED")
        condition = diagnostics["conditions"][0]
        self.assertEqual(condition["dihedral_angle_degrees"], 0.0977404)
        self.assertEqual(
            condition["runtime_surface_tags_audit"], sorted([first_tag, second_tag])
        )
        self.assertEqual(
            {surface["physical_name"] for surface in condition["stable_surfaces"]},
            {"vehicle_internal_and_external_walls"},
        )
        expected_fingerprints = {
            member["fingerprint_id"]
            for marker in self.config["solver_markers"]
            for member in marker["members"]
            if member["area_m2"] in {3.0, 4.0}
        }
        self.assertEqual(
            set(diagnostics["stable_surface_fingerprints"]), expected_fingerprints
        )
        self.assertEqual(
            failure["last_mesh_error_entities"],
            [[2, min(first_tag, second_tag)], [2, max(first_tag, second_tag)]],
        )
        self.assertEqual(failure["last_mesh_error_nodes"], [45, 283])
        request = json.loads(
            (output / "cad_repair_request.json").read_text(encoding="utf-8")
        )
        self.assertEqual(request["status"], "CAD_REPAIR_REQUIRED")
        self.assertFalse(request["production_mesh_eligible"])
        self.assertTrue(request["constraints"]["runtime_tags_are_audit_only"])

    def test_fatal_cad_logger_prevents_publishing_an_apparent_success(self) -> None:
        gmsh = _FakeGmsh()
        first_tag = gmsh.identity_to_tag[3]
        second_tag = gmsh.identity_to_tag[4]
        gmsh.logger_messages = [
            f"Error: Invalid boundary mesh (overlapping facets) on surface {first_tag} surface {second_tag}"
        ]

        with self.assertRaisesRegex(TopologySmokeMeshError, "fatal CAD boundary"):
            self._run(gmsh, "fatal-logger")

        output = self.root / "fatal-logger"
        self.assertEqual(
            {path.name for path in output.iterdir()},
            {
                "gmsh_topology_smoke.log",
                "topology_smoke_manifest.json",
                "cad_repair_request.json",
            },
        )
        self.assertFalse((output / "mesh.msh").exists())
        self.assertFalse((output / "mesh.su2").exists())

    def test_bad_entity_diagnostic_does_not_hide_valid_node_diagnostic(self) -> None:
        gmsh = _FakeGmsh()
        gmsh.fail_generate = True
        gmsh.last_entity_errors = [[2]]
        gmsh.last_node_errors = [91, 92]

        with self.assertRaisesRegex(TopologySmokeMeshError, "generate sentinel"):
            self._run(gmsh, "independent-diagnostics")

        failure = json.loads(
            (
                self.root
                / "independent-diagnostics"
                / "topology_smoke_manifest.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(failure["last_mesh_error_nodes"], [91, 92])
        self.assertIn(
            "getLastEntityError",
            {item.get("context") for item in failure["secondary_errors"]},
        )

    def test_shared_surface_triangle_requires_one_tetrahedron_in_each_volume(self) -> None:
        gmsh = _FakeGmsh()
        shared_element = next(iter(gmsh.surface_triangles[gmsh.shared_tags[0]]))
        external_surface = gmsh.identity_to_tag[1]
        external_face = next(iter(gmsh.surface_triangles[external_surface].values()))
        gmsh.surface_triangles[gmsh.shared_tags[0]][shared_element] = external_face

        with self.assertRaisesRegex(
            TopologySmokeMeshError, "shared interface|exactly exhaust"
        ):
            self._run(gmsh, "bad-shared")

        self.assertFalse(any(call[0] == "write" for call in gmsh.calls))
        self.assertEqual(gmsh.calls[-1], ("finalize",))

    def test_su2_parser_rejects_marker_node_outside_npoin(self) -> None:
        gmsh = _FakeGmsh()
        gmsh.invalid_su2_node = True

        with self.assertRaisesRegex(TopologySmokeMeshError, "invalid node index"):
            self._run(gmsh, "bad-su2")

        self.assertEqual(gmsh.calls[-1], ("finalize",))
        output = self.root / "bad-su2"
        self.assertEqual(
            {path.name for path in output.iterdir()},
            {"gmsh_topology_smoke.log", "topology_smoke_manifest.json"},
        )


if __name__ == "__main__":
    unittest.main()
