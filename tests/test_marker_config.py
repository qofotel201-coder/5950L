"""Tests for deterministic, tag-free ``cfdpipe.markers.v2`` documents."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import tempfile
import tomllib
import unittest

from cfdpipe.marker_config import (
    MarkerConfigError,
    build_marker_config,
    load_marker_config,
    render_marker_config,
    validate_marker_config_document,
    write_marker_config,
)


def _canonical_sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MarkerConfigTests(unittest.TestCase):
    """Exercise generation and strict loading without importing Gmsh."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "config" / "project.toml"
        self.source = self.root / "geometry" / "raw" / "model.step"
        self.brep = self.root / "geometry" / "derived" / "model.brep"
        self.repair = self.root / "runs" / "repair_manifest.json"
        self.rematch = self.root / "runs" / "marker_rematch.json"
        self.persistence = self.root / "runs" / "interface_persistence.json"
        self.topology = self.root / "runs" / "pipeline_topology.json"
        for path in (
            self.project,
            self.source,
            self.brep,
            self.repair,
            self.rematch,
            self.persistence,
            self.topology,
        ):
            path.parent.mkdir(parents=True, exist_ok=True)

        self.source.write_bytes(b"synthetic read-only STEP evidence\n")
        self.brep.write_bytes(b"synthetic read-only BREP evidence\n")
        self.project.write_text(
            """[project]
geometry_file = "geometry/raw/model.step"

[boundaries]
farfield_role = "front"
wall_role = "wall"
rear_outlet_roles = ["rear_1", "rear_2"]
measurement_surface_role = "measurement"
measurement_surface_is_solver_boundary = false
""",
            encoding="utf-8",
        )
        self.source_sha = _file_sha(self.source)
        self.brep_sha = _file_sha(self.brep)
        self._build_topology()
        self._build_rematch()
        self._build_repair()
        self._build_persistence()
        self.source.chmod(stat.S_IREAD)
        self.brep.chmod(stat.S_IREAD)

    def tearDown(self) -> None:
        for path in (self.source, self.brep):
            if path.exists():
                path.chmod(stat.S_IREAD | stat.S_IWRITE)
        self.temporary.cleanup()

    def _write_json(self, path: Path, value: object) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    def _surface_geometry(self, index: int) -> dict[str, object]:
        x = float(index)
        return {
            "entity_type": "Plane",
            "area_m2": round(1.0 + index / 100.0, 12),
            "centroid_m": [x, 0.25, 0.5],
            "bounding_box_m": [x, 0.0, 0.0, x + 0.1, 0.5, 1.0],
            "adjacent_volume_count": 1,
        }

    def _build_topology(self) -> None:
        surfaces: list[dict[str, object]] = []
        for index in range(55):
            geometry = self._surface_geometry(index)
            surfaces.append(
                {
                    "entity_tag_audit": index + 1,
                    "entity_type": geometry["entity_type"],
                    "area_m2": geometry["area_m2"],
                    "centroid_m": geometry["centroid_m"],
                    "bounding_box_m": geometry["bounding_box_m"],
                    "adjacent_volumes": [11],
                }
            )
        for offset in range(2):
            index = 55 + offset
            surfaces.append(
                {
                    "entity_tag_audit": index + 1,
                    "entity_type": "Plane",
                    "area_m2": 0.4 + offset,
                    "centroid_m": [float(index), 0.0, 0.0],
                    "bounding_box_m": [float(index), 0.0, 0.0, float(index), 1.0, 1.0],
                    "adjacent_volumes": [11, 22],
                }
            )
        document = {
            "surfaces": surfaces,
            "volumes": [
                {
                    "entity_tag_audit": 11,
                    "entity_type": "Volume",
                    "volume_m3": 10.0,
                    "centroid_m": [1.0, 0.0, 0.0],
                    "bounding_box_m": [0.0, -1.0, -1.0, 2.0, 1.0, 1.0],
                    "boundary_surfaces": list(range(1, 30)),
                },
                {
                    "entity_tag_audit": 22,
                    "entity_type": "Volume",
                    "volume_m3": 20.0,
                    "centroid_m": [3.0, 0.0, 0.0],
                    "bounding_box_m": [2.0, -2.0, -2.0, 6.0, 2.0, 2.0],
                    "boundary_surfaces": list(range(30, 58)),
                },
            ],
        }
        self._write_json(self.topology, document)

    def _build_rematch(self) -> None:
        role_spec = (
            ("front", "farfield", 2),
            ("wall", "wall", 48),
            ("rear_1", "rear_outlet", 3),
            ("rear_2", "rear_outlet", 2),
        )
        roles: dict[str, object] = {}
        cursor = 0
        for role, kind, count in role_spec:
            members: list[dict[str, object]] = []
            source_ids: list[str] = []
            derived_ids: list[str] = []
            for index in range(cursor, cursor + count):
                original_id = _digest(f"original-surface-{index}")
                fingerprint = {
                    "schema": "cfdpipe.derived_boundary_surface_fingerprint.v1",
                    "source_step_sha256": self.source_sha,
                    "derived_cad_sha256": self.brep_sha,
                    "original_surface_fingerprint_id": original_id,
                    "geometry": self._surface_geometry(index),
                    "classification": "external_boundary",
                }
                fingerprint_id = _canonical_sha(fingerprint)
                source_ids.append(original_id)
                derived_ids.append(fingerprint_id)
                members.append(
                    {"fingerprint_id": fingerprint_id, "fingerprint": fingerprint}
                )
            group = {
                "schema": "cfdpipe.derived_boundary_group_fingerprint.v1",
                "source_step_sha256": self.source_sha,
                "derived_cad_sha256": self.brep_sha,
                "semantic_role": role,
                "boundary_kind": kind,
                "source_confirmed_group_fingerprint_id": _digest(f"source-group-{role}"),
                "source_member_fingerprint_ids": sorted(source_ids),
                "derived_member_surface_fingerprint_ids": sorted(derived_ids),
                "member_count": count,
                "classification": "complete_external_solver_boundary_group",
            }
            roles[role] = {
                "kind": kind,
                "solver_boundary": True,
                "derived_group_fingerprint_id": _canonical_sha(group),
                "derived_group_fingerprint": group,
                "derived_member_surface_fingerprints": members,
            }
            cursor += count

        measurement = {
            "schema": "cfdpipe.derived_measurement_surface_fingerprint.v1",
            "source_step_sha256": self.source_sha,
            "derived_cad_sha256": self.brep_sha,
            "confirmed_measurement_candidate_id": _digest("measurement-candidate"),
            "derived_boundary_fingerprint_id": _digest("measurement-boundary"),
            "geometry": {
                "plane_axis": "x",
                "plane_coordinate_m": 3.77,
                "normal_unit": [1.0, 0.0, 0.0],
                "bounds_m": [3.77, -0.1, -0.1, 3.77, 0.1, 0.1],
                "area_m2": 0.0314,
                "closed": True,
            },
            "solver_boundary": False,
        }
        roles["measurement"] = {
            "kind": "measurement_surface",
            "solver_boundary": False,
            "derived_measurement_fingerprint_id": _canonical_sha(measurement),
            "derived_measurement_fingerprint": measurement,
        }
        document = {
            "schema": "cfdpipe.derived_marker_rematch.v1",
            "status": "PASS",
            "source_step_sha256": self.source_sha,
            "derived_cad_sha256": self.brep_sha,
            "identity_policy": {"runtime_entity_identifiers_in_output": False},
            "counts": {
                "confirmed_original_solver_surfaces": 55,
                "derived_external_solver_surfaces": 55,
                "solver_boundary_groups": 4,
                "measurement_surfaces": 1,
            },
            "validation": {
                "all_confirmed_original_surfaces_rematched": True,
                "all_descendant_unions_geometry_conserving": True,
                "solver_groups_complete_and_nonoverlapping": True,
                "all_solver_surfaces_external": True,
                "all_interface_surfaces_shared": True,
                "derived_catalog_fully_classified": True,
                "measurement_surface_unique": True,
                "measurement_surface_is_solver_boundary": False,
            },
            "roles": roles,
        }
        self._write_json(self.rematch, document)

    def _build_repair(self) -> None:
        self._write_json(
            self.repair,
            {
                "status": "PASS",
                "source_unchanged": True,
                "source": {"path": str(self.source), "sha256": self.source_sha},
                "pipeline_geometry": {
                    "path": str(self.brep),
                    "sha256": self.brep_sha,
                    "pipeline_eligible": True,
                    "reimport_status": "PASS",
                },
                "fragment_topology": {
                    "status": "PASS",
                    "external_surface_count": 55,
                    "shared_patch_count": 2,
                    "volume_tags_audit": [11, 22],
                },
                "marker_rematch": {
                    "status": "PASS",
                    "derived_cad_sha256": self.brep_sha,
                },
            },
        )

    def _build_persistence(self) -> None:
        validation = {
            "stable_source_identity_recomputed": True,
            "matching_uses_old_runtime_tags": False,
            "source_copy_to_derived_projection_complete": True,
            "boundary_curve_geometry_complete": True,
            "absolute_parametric_normals_aligned": True,
            "derived_oriented_boundaries_opposite": True,
            "shared_face_mapping_bijective_and_exhaustive": True,
            "no_patch_overlap": True,
        }
        self._write_json(
            self.persistence,
            {
                "schema_version": "cfdpipe.interface_persistence.v1",
                "status": "PASS",
                "source_step": {
                    "unchanged": True,
                    "before": {"sha256": self.source_sha},
                },
                "derived_brep": {
                    "unchanged": True,
                    "pipeline_eligible": True,
                    "before": {"sha256": self.brep_sha},
                },
                "project": {"sha256": _file_sha(self.project)},
                "derived_topology": {
                    "unique_surface_count": 57,
                    "external_surface_count": 55,
                    "shared_patch_count": 2,
                    "volume_tags_audit": [11, 22],
                },
                "policy": {"physical_groups_created": False},
                "validation": validation,
                "input_parameters": {
                    "surface_distance_tolerance_m": 1.0e-9,
                    "boundary_distance_tolerance_m": 1.0e-9,
                    "normal_dot_tolerance": 1.0e-8,
                },
            },
        )

    def _arguments(self) -> dict[str, object]:
        return {
            "project_path": self.project,
            "source_step_path": self.source,
            "pipeline_brep_path": self.brep,
            "repair_manifest_path": self.repair,
            "marker_rematch_path": self.rematch,
            "interface_persistence_path": self.persistence,
            "pipeline_topology_path": self.topology,
            "repository_root": self.root,
        }

    def test_build_is_tag_free_complete_and_deterministic(self) -> None:
        first = build_marker_config(**self._arguments())
        second = build_marker_config(**self._arguments())
        self.assertEqual(first, second)
        self.assertEqual(first["schema"], "cfdpipe.markers.v2")
        counts = {
            marker["semantic_role"]: marker["member_count"]
            for marker in first["solver_markers"]
        }
        self.assertEqual(
            counts, {"front": 2, "wall": 48, "rear_1": 3, "rear_2": 2}
        )
        self.assertEqual(sum(counts.values()), 55)
        self.assertEqual(first["topology"]["external_surface_count"], 55)
        self.assertEqual(len(first["fluid"]["volume_selectors"]), 2)
        self.assertEqual(first["fluid"]["dimension"], 3)
        self.assertFalse(first["measurement"]["solver_boundary"])
        self.assertFalse(first["measurement"]["create_physical_group"])
        keys: set[str] = set()

        def collect_keys(value: object) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    keys.add(str(key).casefold())
                    collect_keys(child)
            elif isinstance(value, list):
                for child in value:
                    collect_keys(child)

        collect_keys(first)
        self.assertTrue(
            {
                "entity_tag",
                "entity_tags",
                "step_name",
                "step_names",
                "runtime_tag",
                "runtime_tags",
                "gmsh_tag",
                "gmsh_tags",
            }.isdisjoint(keys)
        )

    def test_render_round_trips_exactly(self) -> None:
        document = build_marker_config(**self._arguments())
        rendered = render_marker_config(document)
        self.assertEqual(rendered, render_marker_config(document))
        self.assertEqual(tomllib.loads(rendered), document)

    def test_write_is_new_only_and_load_returns_exact_toml(self) -> None:
        output = self.root / "config" / "generated_markers.toml"
        written = write_marker_config(output, **self._arguments())
        parsed = tomllib.loads(output.read_text(encoding="utf-8"))
        loaded = load_marker_config(output, repository_root=self.root)
        self.assertEqual(written, parsed)
        self.assertEqual(loaded, parsed)
        with self.assertRaisesRegex(MarkerConfigError, "overwrite"):
            write_marker_config(output, **self._arguments())

    def test_load_rejects_tampered_payload(self) -> None:
        output = self.root / "config" / "generated_markers.toml"
        write_marker_config(output, **self._arguments())
        text = output.read_text(encoding="utf-8")
        output.write_text(text.replace("member_count = 2", "member_count = 1", 1), encoding="utf-8")
        with self.assertRaisesRegex(MarkerConfigError, "canonical payload"):
            load_marker_config(output, repository_root=self.root)

    def test_load_rejects_each_stale_provenance_hash(self) -> None:
        output = self.root / "config" / "generated_markers.toml"
        write_marker_config(output, **self._arguments())
        records = {
            "project": self.project,
            "source_step": self.source,
            "pipeline_brep": self.brep,
            "repair_manifest": self.repair,
            "marker_rematch": self.rematch,
            "interface_persistence": self.persistence,
            "pipeline_topology": self.topology,
        }
        for name, path in records.items():
            with self.subTest(name=name):
                original = path.read_bytes()
                protected = path in {self.source, self.brep}
                try:
                    if protected:
                        path.chmod(stat.S_IREAD | stat.S_IWRITE)
                    path.write_bytes(original + b" ")
                    if protected:
                        path.chmod(stat.S_IREAD)
                    with self.assertRaisesRegex(
                        MarkerConfigError, rf"provenance '{name}'.*stale"
                    ):
                        load_marker_config(output, repository_root=self.root)
                finally:
                    if protected:
                        path.chmod(stat.S_IREAD | stat.S_IWRITE)
                    path.write_bytes(original)
                    if protected:
                        path.chmod(stat.S_IREAD)

    def test_read_only_source_and_brep_are_required(self) -> None:
        self.source.chmod(stat.S_IREAD | stat.S_IWRITE)
        with self.assertRaisesRegex(MarkerConfigError, "source STEP must be read-only"):
            build_marker_config(**self._arguments())
        self.source.chmod(stat.S_IREAD)
        self.brep.chmod(stat.S_IREAD | stat.S_IWRITE)
        with self.assertRaisesRegex(MarkerConfigError, "pipeline BREP must be read-only"):
            build_marker_config(**self._arguments())

    def test_forbidden_runtime_identity_is_rejected(self) -> None:
        document = build_marker_config(**self._arguments())
        document["solver_markers"][0]["members"][0]["entity_tags"] = [99]
        with self.assertRaisesRegex(MarkerConfigError, "forbidden field"):
            validate_marker_config_document(document)

    def test_measurement_cannot_become_solver_boundary(self) -> None:
        document = json.loads(self.rematch.read_text(encoding="utf-8"))
        item = document["roles"]["measurement"]
        item["solver_boundary"] = True
        fingerprint = item["derived_measurement_fingerprint"]
        fingerprint["solver_boundary"] = True
        item["derived_measurement_fingerprint_id"] = _canonical_sha(fingerprint)
        self._write_json(self.rematch, document)
        with self.assertRaisesRegex(MarkerConfigError, "non-solver"):
            build_marker_config(**self._arguments())

    def test_external_members_must_exhaust_pipeline_topology(self) -> None:
        document = json.loads(self.topology.read_text(encoding="utf-8"))
        document["surfaces"][0]["area_m2"] = 123.0
        self._write_json(self.topology, document)
        with self.assertRaisesRegex(MarkerConfigError, "does not exhaust"):
            build_marker_config(**self._arguments())

    def test_volume_selectors_ignore_runtime_tags(self) -> None:
        first = build_marker_config(**self._arguments())
        topology = json.loads(self.topology.read_text(encoding="utf-8"))
        for index, volume in enumerate(topology["volumes"], start=1):
            volume["entity_tag_audit"] = 900 + index
        self._write_json(self.topology, topology)
        second = build_marker_config(**self._arguments())
        self.assertEqual(
            first["fluid"]["volume_selectors"], second["fluid"]["volume_selectors"]
        )
        self.assertNotEqual(
            first["provenance"]["pipeline_topology"]["sha256"],
            second["provenance"]["pipeline_topology"]["sha256"],
        )


if __name__ == "__main__":
    unittest.main()
