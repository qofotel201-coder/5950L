"""Tests for the fail-closed human marker confirmation layer."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from cfdpipe.marker_confirmation import (
    MarkerConfirmationError,
    build_marker_confirmation_template,
    solidify_marker_confirmation,
    validate_marker_confirmation,
    write_marker_confirmation_validation,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_json(value: object) -> object:
    return json.loads(json.dumps(value))


def _payload_sha(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class MarkerConfirmationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.evidence = self.root / "runs" / "real_connection" / "geometry_review"
        self.evidence.mkdir(parents=True)
        self.project = self.root / "project.toml"
        self.project.write_text(
            """[boundaries]
farfield_role = "front_conical_surface"
wall_role = "vehicle_internal_and_external_walls"
rear_outlet_roles = ["rear_outlet_1", "rear_outlet_2"]
measurement_surface_role = "turning_section_outlet"
measurement_surface_is_solver_boundary = false
""",
            encoding="utf-8",
        )
        self.catalog = self.evidence / "surface_catalog.csv"
        self.types = self.evidence / "surface_types.json"
        self.review = self.evidence / "marker_review.json"
        self.template_path = self.evidence / "marker_confirmation_template.json"
        self.step_sha = "a" * 64
        self.rows = [
            {
                "tag": 1,
                "area": 10.0,
                "centroid": [0.0, 0.2, 0.0],
                "bounds": [-1.0, 0.0, -1.0, 1.0, 0.4, 1.0],
                "volume": 1,
                "curves": [10, 11],
                "type": "BSpline surface",
            },
            {
                "tag": 2,
                "area": 11.0,
                "centroid": [0.0, -0.2, 0.0],
                "bounds": [-1.0, -0.4, -1.0, 1.0, 0.0, 1.0],
                "volume": 1,
                "curves": [11, 12],
                "type": "BSpline surface",
            },
            {
                "tag": 3,
                "area": 20.0,
                "centroid": [2.0, 0.2, 0.0],
                "bounds": [0.0, 0.0, -2.0, 4.0, 0.4, 2.0],
                "volume": 2,
                "curves": [30, 31],
                "type": "Cone",
            },
            {
                "tag": 4,
                "area": 21.0,
                "centroid": [2.0, -0.2, 0.0],
                "bounds": [0.0, -0.4, -2.0, 4.0, 0.0, 2.0],
                "volume": 2,
                "curves": [31, 32],
                "type": "Cone",
            },
            {
                "tag": 5,
                "area": 1.0,
                "centroid": [5.2, 0.2, 0.0],
                "bounds": [5.2, 0.1, -0.1, 5.2, 0.3, 0.1],
                "volume": 3,
                "curves": [50, 51],
                "type": "Plane",
            },
            {
                "tag": 6,
                "area": 1.2,
                "centroid": [5.2, -0.2, 0.0],
                "bounds": [5.2, -0.3, -0.1, 5.2, -0.1, 0.1],
                "volume": 3,
                "curves": [51, 52],
                "type": "Plane",
            },
            {
                "tag": 7,
                "area": 2.0,
                "centroid": [6.0, 0.0, 0.0],
                "bounds": [5.9, -0.2, -0.2, 6.1, 0.2, 0.2],
                "volume": 4,
                "curves": [70, 71],
                "type": "BSpline surface",
            },
            {
                "tag": 8,
                "area": 2.1,
                "centroid": [6.0, 0.25, 0.0],
                "bounds": [5.9, 0.15, -0.2, 6.1, 0.35, 0.2],
                "volume": 4,
                "curves": [71, 72],
                "type": "BSpline surface",
            },
            {
                "tag": 9,
                "area": 2.2,
                "centroid": [6.0, -0.25, 0.0],
                "bounds": [5.9, -0.35, -0.2, 6.1, -0.15, 0.2],
                "volume": 4,
                "curves": [72, 73],
                "type": "BSpline surface",
            },
        ]
        self._write_evidence(self.rows)
        self.measurements = [
            {
                "candidate_id": "turning_plane_customer_1",
                "role": "turning_section_outlet",
                "solver_boundary": False,
                "definition": {
                    "kind": "plane",
                    "origin_m": [2.6, 0.0, 0.0],
                    "normal": [1.0, 0.0, 0.0],
                },
                "source_sha256": "b" * 64,
            }
        ]
        self.boundary_groups = self._semantic_boundary_report()

    def _write_catalog(self, rows: list[dict[str, object]]) -> None:
        with self.catalog.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=[
                    "entity_tag",
                    "area_m2",
                    "centroid_m",
                    "bounding_box_m",
                    "adjacent_volumes",
                    "boundary_curves",
                ],
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        "entity_tag": row["tag"],
                        "area_m2": row["area"],
                        "centroid_m": json.dumps(row["centroid"]),
                        "bounding_box_m": json.dumps(row["bounds"]),
                        "adjacent_volumes": json.dumps([row["volume"]]),
                        "boundary_curves": json.dumps(row["curves"]),
                    }
                )

    def _write_types(self, rows: list[dict[str, object]]) -> None:
        self.types.write_text(
            json.dumps(
                {
                    "status": "PASS",
                    "surface_catalog_sha256": _sha(self.catalog),
                    "surfaces": [
                        {
                            "entity_tag": row["tag"],
                            "entity_type": row["type"],
                        }
                        for row in rows
                    ],
                }
            ),
            encoding="utf-8",
        )

    def _write_review(self, rows: list[dict[str, object]]) -> None:
        tags = [int(row["tag"]) for row in rows]
        surface_groups = [tags[:2], tags[2:4], tags[4:6], tags[6:]]
        role_candidates = {
            # All legacy role candidates are deliberately incomplete or
            # wrong.  Independent complete-group evidence must replace them.
            "front_conical_surface": [surface_groups[0][0]],
            "vehicle_internal_and_external_walls": [surface_groups[1][0]],
            "rear_outlet_1": [surface_groups[2][0]],
            "rear_outlet_2": [surface_groups[2][0]],
            "turning_section_outlet": [],
        }
        required_roles = [
            {"role": "front_conical_surface", "kind": "farfield"},
            {
                "role": "vehicle_internal_and_external_walls",
                "kind": "wall",
            },
            {"role": "rear_outlet_1", "kind": "rear_outlet"},
            {"role": "rear_outlet_2", "kind": "rear_outlet"},
            {
                "role": "turning_section_outlet",
                "kind": "measurement_surface",
            },
        ]
        kinds = {item["role"]: item["kind"] for item in required_roles}
        payload = {
            "status": "AMBIGUOUS",
            "marker_configuration_ready": False,
            "markers_toml_written": False,
            "measurement_surface_is_solver_boundary": False,
            "required_roles": required_roles,
            "roles": {
                role: {
                    "status": "MISSING" if not candidates else "AMBIGUOUS",
                    "kind": kinds[role],
                    "surface_tags": candidates,
                }
                for role, candidates in role_candidates.items()
            },
            "shell_components": [
                {
                    "component_id": f"volume_{index}_shell_1",
                    "volume_entity_tag": index,
                    "surface_tags": group,
                    "surface_count": len(group),
                }
                for index, group in enumerate(surface_groups, start=1)
            ],
            "step_shell_correspondence": [
                {
                    "status": "CONFIRMED",
                    "solid_id": 100 + index,
                    "root_shell_id": 200 + index,
                    "step_role_in_solid": (
                        "void_shell" if index == 2 else "boundary_shell"
                    ),
                    "face_count": len(group),
                    "candidate_catalog_component_ids": [
                        f"volume_{index}_shell_1"
                    ],
                }
                for index, group in enumerate(surface_groups, start=1)
            ],
            "inputs": {
                "project": {"path": str(self.project), "sha256": _sha(self.project)},
                "surface_catalog": {
                    "path": str(self.catalog),
                    "sha256": _sha(self.catalog),
                },
                "surface_types": {
                    "path": str(self.types),
                    "sha256": _sha(self.types),
                },
                "step_topology": {"source_sha256": self.step_sha},
            },
        }
        self.review.write_text(json.dumps(payload), encoding="utf-8")

    def _write_evidence(self, rows: list[dict[str, object]]) -> None:
        self._write_catalog(rows)
        self._write_types(rows)
        self._write_review(rows)

    def _semantic_group(
        self,
        tags: list[int],
        *,
        geometric_class: str,
        shell_index: int,
    ) -> dict[str, object]:
        rows = {int(row["tag"]): row for row in self.rows}
        shell = {
            "step_solid_id": 100 + shell_index,
            "step_solid_type": "MANIFOLD_SOLID_BREP",
            "step_solid_name": f"solid_{shell_index}",
            "step_root_shell_id": 200 + shell_index,
            "step_shell_role": (
                "void_shell" if shell_index == 2 else "boundary_shell"
            ),
        }
        member_rows: list[tuple[str, int, dict[str, object]]] = []
        for tag in tags:
            row = rows[tag]
            fingerprint = {
                "schema": "cfdpipe.boundary_surface_fingerprint.v1",
                "source_step_sha256": self.step_sha,
                "source_project_sha256": _sha(self.project),
                "solid_shell": shell,
                "entity_type": row["type"],
                "area_m2": row["area"],
                "centroid_m": row["centroid"],
                "bounding_box_m": row["bounds"],
                "boundary_curve_count": len(row["curves"]),
            }
            member_rows.append((_payload_sha(fingerprint), tag, fingerprint))
        member_rows.sort(key=lambda item: item[0])
        member_ids = [item[0] for item in member_rows]
        id_by_tag = {tag: member_id for member_id, tag, _ in member_rows}
        edges: list[dict[str, object]] = []
        for index, first in enumerate(tags):
            first_curves = set(rows[first]["curves"])
            for second in tags[index + 1 :]:
                shared = len(first_curves.intersection(rows[second]["curves"]))
                if shared:
                    edges.append(
                        {
                            "member_fingerprint_ids": sorted(
                                [id_by_tag[first], id_by_tag[second]]
                            ),
                            "shared_boundary_curve_count": shared,
                        }
                    )
        edges.sort(key=lambda item: tuple(item["member_fingerprint_ids"]))
        curve_counts: dict[int, int] = {}
        for tag in tags:
            for curve in rows[tag]["curves"]:
                curve_counts[int(curve)] = curve_counts.get(int(curve), 0) + 1
        areas = [float(rows[tag]["area"]) for tag in tags]
        total_area = sum(areas)
        type_counts: dict[str, int] = {}
        for tag in tags:
            name = str(rows[tag]["type"])
            type_counts[name] = type_counts.get(name, 0) + 1
        fingerprint = {
            "schema": "cfdpipe.boundary_group_fingerprint.v1",
            "source_step_sha256": self.step_sha,
            "source_project_sha256": _sha(self.project),
            "geometric_class": geometric_class,
            "solid_shell": shell,
            "member_surface_fingerprint_ids": member_ids,
            "member_count": len(tags),
            "entity_type_histogram": dict(sorted(type_counts.items())),
            "total_area_m2": total_area,
            "area_weighted_centroid_m": [
                sum(
                    float(rows[tag]["area"])
                    * float(rows[tag]["centroid"][axis])
                    for tag in tags
                )
                / total_area
                for axis in range(3)
            ],
            "bounding_box_m": [
                min(float(rows[tag]["bounds"][axis]) for tag in tags)
                for axis in range(3)
            ]
            + [
                max(float(rows[tag]["bounds"][axis]) for tag in tags)
                for axis in range(3, 6)
            ],
            "connectivity_edges": edges,
            "external_boundary_curve_count": sum(
                count == 1 for count in curve_counts.values()
            ),
            "internal_shared_curve_multiplicities": sorted(
                count for count in curve_counts.values() if count > 1
            ),
        }
        return {
            "geometry_status": "CONFIRMED_GEOMETRY",
            "business_role_status": "CANDIDATE_ROLE",
            "group_fingerprint_id": _payload_sha(fingerprint),
            "fingerprint": fingerprint,
            "members": [
                {
                    "surface_fingerprint_id": member_id,
                    "fingerprint": member_fingerprint,
                    "audit": {
                        "gmsh_surface_entity_tag": tag,
                        "tag_is_matching_criterion": False,
                        "boundary_curve_tags": list(rows[tag]["curves"]),
                        "boundary_curve_tags_are_matching_criteria": False,
                    },
                }
                for member_id, tag, member_fingerprint in member_rows
            ],
            "audit": {
                "current_surface_tags": sorted(tags),
                "current_volume_entity_tag": shell_index,
                "gmsh_tags_are_matching_criteria": False,
            },
        }

    def _semantic_boundary_report(self) -> dict[str, object]:
        farfield = self._semantic_group(
            [1, 2],
            geometric_class="lower_x_outer_shell_before_interface_edge",
            shell_index=1,
        )
        wall = self._semantic_group(
            [3, 4], geometric_class="void_shell_boundary", shell_index=2
        )
        rear_groups = [
            self._semantic_group(
                [5, 6],
                geometric_class="positive_x_continuation_from_interface",
                shell_index=3,
            ),
            self._semantic_group(
                [7, 8, 9],
                geometric_class="maximum_x_interface_edge_closure",
                shell_index=4,
            ),
        ]
        rear_ids = sorted(
            str(group["group_fingerprint_id"]) for group in rear_groups
        )
        return {
            "schema_version": "cfdpipe.boundary_semantic_candidates.v1",
            "status": "PASS",
            "classification_status": "HUMAN_CONFIRMATION_REQUIRED",
            "marker_configuration_ready": False,
            "business_semantics_asserted": False,
            "source": {
                "step_sha256": self.step_sha,
                "project_sha256": _sha(self.project),
            },
            "geometric_candidates": {
                "upstream_outer_groups": [farfield],
                "downstream_outer_groups": rear_groups,
                "transverse_or_mixed_outer_groups": [],
                "void_shell_boundary_groups": [wall],
                "near_coincident_interface_pairs": [],
            },
            "role_hypotheses": {
                "farfield": {
                    "status": "CANDIDATE",
                    "candidate_group_fingerprint_ids": [
                        farfield["group_fingerprint_id"]
                    ],
                },
                "wall": {
                    "status": "CANDIDATE",
                    "candidate_group_fingerprint_ids": [
                        wall["group_fingerprint_id"]
                    ],
                },
                "rear_outlets": {
                    "status": "CANDIDATE_NUMBERING_PENDING",
                    "candidate_group_fingerprint_ids": rear_ids,
                },
            },
            "rear_outlet_numbering_contract": {
                "status": "PENDING_HUMAN_CONFIRMATION",
                "strategy": "explicit_group_fingerprint_mapping",
                "required_role_count": 2,
                "candidate_group_fingerprint_ids": rear_ids,
                "forbidden_inference": "single_surface_centroid_z",
            },
            "partition_audit": {
                "surface_count": len(self.rows),
                "assigned_surface_count": len(self.rows),
                "all_surfaces_assigned_once": True,
                "current_interface_surface_tags": [],
                "current_group_surface_tags": sorted(
                    int(row["tag"]) for row in self.rows
                ),
                "gmsh_tags_are_matching_criteria": False,
            },
        }

    def _build(self) -> dict[str, object]:
        return self._build_to_new_path(self.template_path.name)

    def _build_to_new_path(self, name: str) -> dict[str, object]:
        return build_marker_confirmation_template(
            self.review,
            self.measurements,
            self.evidence / name,
            boundary_group_candidates=self.boundary_groups,
        )

    def _completed(self, template: dict[str, object]) -> dict[str, object]:
        document = _copy_json(template)
        assert isinstance(document, dict)
        document["status"] = "CONFIRMED_BY_HUMAN"
        groups_by_kind: dict[str, list[str]] = {}
        for item in document["boundary_group_candidates"]:
            groups_by_kind.setdefault(item["candidate_role_kind"], []).append(
                item["group_fingerprint_id"]
            )
        selections = {
            "front_conical_surface": groups_by_kind["farfield"],
            "vehicle_internal_and_external_walls": groups_by_kind["wall"],
        }
        confirmations = document["confirmations"]
        assert isinstance(confirmations, dict)
        for role, decision in confirmations.items():
            decision["status"] = "CONFIRMED"
            decision["confirmed_by"] = "geometry_owner"
            decision["decision_basis"] = "approved against controlled review images"
            if role in selections:
                decision["selected_group_fingerprint_ids"] = selections[role]
        measurement = confirmations["turning_section_outlet"]
        measurement["selected_measurement_candidate_id"] = (
            "turning_plane_customer_1"
        )
        assignment = document["rear_outlet_group_assignment"]
        assignment.update(
            {
                "status": "CONFIRMED",
                "role_to_group_fingerprint": {
                    "rear_outlet_1": groups_by_kind["rear_outlet"][0],
                    "rear_outlet_2": groups_by_kind["rear_outlet"][1],
                },
                "confirmed_by": "geometry_owner",
                "decision_basis": "customer explicitly mapped both complete groups",
            }
        )
        return document

    def _recommended_tag_mapping(self) -> dict[str, list[int]]:
        return {
            "front_conical_surface": [1, 2],
            "vehicle_internal_and_external_walls": [3, 4],
            "rear_outlet_1": [5, 6],
            "rear_outlet_2": [7, 8, 9],
        }

    def _solidify(
        self,
        template: object,
        output_name: str,
        *,
        tags: object | None = None,
        measurement_id: str = "turning_plane_customer_1",
        confirmed_by: str = "project_owner",
        confirmed_at_utc: str = "2026-08-01T08:00:00Z",
    ) -> dict[str, object]:
        return solidify_marker_confirmation(
            template,
            self.review,
            self.evidence / output_name,
            selected_surface_tags_by_role=(
                self._recommended_tag_mapping() if tags is None else tags
            ),
            selected_measurement_candidate_id=measurement_id,
            confirmed_by=confirmed_by,
            user_decision_summary="User explicitly confirmed the recommended mapping.",
            confirmed_at_utc=confirmed_at_utc,
        )

    def test_template_keeps_tags_audit_only_and_records_full_fingerprint(self) -> None:
        template = self._build()

        self.assertTrue(self.template_path.is_file())
        self.assertFalse((self.root / "config" / "markers.toml").exists())
        self.assertEqual(template["source"]["step_sha256"], self.step_sha)
        entity = template["entities"][0]
        fingerprint = entity["fingerprint"]
        self.assertNotIn("entity_tag", fingerprint)
        self.assertNotIn("boundary_curve_tags", fingerprint)
        self.assertIn("gmsh_surface_entity_tag", entity["audit"])
        self.assertFalse(entity["audit"]["tag_is_matching_criterion"])
        self.assertEqual(fingerprint["solid_shell"]["step_solid_id"], 101)
        self.assertEqual(
            fingerprint["solid_shell"]["step_shell_role"], "boundary_shell"
        )
        for field in (
            "area_m2",
            "centroid_m",
            "bounding_box_m",
            "entity_type",
            "boundary_loop_signature",
        ):
            self.assertIn(field, fingerprint)
        self.assertFalse(
            fingerprint["boundary_loop_signature"]["loop_partition_available"]
        )
        groups = template["boundary_group_candidates"]
        self.assertEqual(len(groups), 4)
        self.assertEqual(
            sorted(group["candidate_role_kind"] for group in groups),
            ["farfield", "rear_outlet", "rear_outlet", "wall"],
        )
        for group in groups:
            stable = group["group_fingerprint"]
            self.assertNotIn("gmsh_surface_entity_tags", stable)
            self.assertNotIn("surface_tags", stable)
            self.assertIn("members", stable)
            self.assertIn("topology", stable)
            self.assertIn("geometry", stable)
            self.assertGreaterEqual(stable["member_count"], 2)
            self.assertIn("gmsh_surface_entity_tags", group["audit"])
            self.assertFalse(group["audit"]["tag_is_matching_criterion"])
        for role in (
            "front_conical_surface",
            "vehicle_internal_and_external_walls",
            "rear_outlet_1",
            "rear_outlet_2",
        ):
            self.assertEqual(
                template["confirmations"][role]["candidate_fingerprint_ids"], []
            )

    def test_valid_complete_confirmation_uniquely_resolves(self) -> None:
        confirmation = self._completed(self._build())

        result = validate_marker_confirmation(confirmation, self.review)

        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["marker_configuration_ready"])
        self.assertFalse(result["markers_toml_written"])
        self.assertFalse(
            result["roles"]["turning_section_outlet"]["solver_boundary"]
        )
        self.assertTrue(result["policy"]["gmsh_tags_are_audit_only"])
        self.assertTrue(
            result["policy"]["all_solver_boundaries_require_complete_surface_groups"]
        )
        self.assertEqual(
            sorted(
                result["roles"][role]["complete_member_count"]
                for role in (
                    "front_conical_surface",
                    "vehicle_internal_and_external_walls",
                    "rear_outlet_1",
                    "rear_outlet_2",
                )
            ),
            [2, 2, 2, 3],
        )

    def test_accepts_measurement_discovery_loop_evidence_without_asserting_role(self) -> None:
        fingerprint_payload = {
            "schema": "cfdpipe.measurement_surface_fingerprint.v1",
            "source_sha256": self.step_sha,
            "plane_axis": "x",
            "plane_coordinate_m": 2.6,
            "centroid_m": [2.6, 0.0, 0.0],
            "normal_unit": [1.0, 0.0, 0.0],
            "bounds_m": [2.6, -0.1, -0.1, 2.6, 0.1, 0.1],
            "curve_count": 1,
            "curve_length_spectrum_m": [0.5],
            "perimeter_m": 0.5,
            "area_m2": 0.01,
            "circularity": 0.5,
            "planarity_residual_m": 0.0,
            "adjacency_signature": {
                "incident_surface_count": 2,
                "upstream_surface_count": 1,
                "downstream_surface_count": 1,
                "spanning_surface_count": 0,
                "coplanar_surface_count": 0,
                "unknown_surface_count": 0,
            },
            "occupancy_signature": {
                "available": True,
                "qualified": True,
                "inside_fraction": 1.0,
                "dominant_volume_fraction": 1.0,
                "occupying_volume_count": 1,
            },
        }
        fingerprint_sha = _payload_sha(fingerprint_payload)
        discovery = {
            "status": "PASS",
            "source_unchanged": True,
            "source_sha256_before": self.step_sha,
            "source_sha256_after": self.step_sha,
            "loops": [
                {
                    "centroid_m": [2.6, 0.0, 0.0],
                    "normal_unit": [1.0, 0.0, 0.0],
                    "geometry_status": "CONFIRMED_GEOMETRY",
                    "role_status": "CANDIDATE_ROLE",
                    "fingerprint": {
                        "payload": fingerprint_payload,
                        "sha256": fingerprint_sha,
                    },
                }
            ],
            "classification": {
                "business_role_asserted": False,
                "selected_fingerprint_sha256": fingerprint_sha,
                "transition_candidates": [
                    {"candidate_fingerprint_sha256": fingerprint_sha}
                ],
            },
        }
        output = self.evidence / "measurement_discovery_confirmation.json"

        template = build_marker_confirmation_template(
            self.review,
            discovery,
            output,
            boundary_group_candidates=self.boundary_groups,
        )

        candidate = template["measurement_candidates"][0]
        self.assertFalse(candidate["solver_boundary"])
        self.assertEqual(candidate["definition"]["kind"], "plane_loop")
        self.assertEqual(
            candidate["definition"]["role_status"], "CANDIDATE_ROLE"
        )
        self.assertEqual(
            candidate["definition"]["boundary_loop_fingerprint"]["sha256"],
            fingerprint_sha,
        )

    def test_tag_reordering_still_matches_by_geometry_and_topology(self) -> None:
        confirmation = self._completed(self._build())
        reordered = []
        for row in self.rows:
            item = dict(row)
            item["tag"] = int(row["tag"]) + 100
            item["curves"] = [int(value) + 1000 for value in row["curves"]]
            reordered.append(item)
        self._write_evidence(reordered)

        result = validate_marker_confirmation(confirmation, self.review)

        farfield = result["roles"]["front_conical_surface"]["matched_entities"]
        self.assertEqual(farfield[0]["audit"]["gmsh_surface_entity_tag"], 101)

    def test_zero_and_multiple_fingerprint_matches_fail_closed(self) -> None:
        confirmation = self._completed(self._build())
        changed = _copy_json(self.rows)
        changed[0]["centroid"] = [100.0, 0.0, 0.0]
        changed[0]["bounds"] = [99.0, -1.0, -1.0, 101.0, 1.0, 1.0]
        self._write_evidence(changed)
        with self.assertRaisesRegex(MarkerConfirmationError, "zero matches"):
            validate_marker_confirmation(confirmation, self.review)

        duplicated = _copy_json(self.rows)
        for field in ("area", "centroid", "bounds", "type"):
            duplicated[1][field] = _copy_json(duplicated[0][field])
        self._write_evidence(duplicated)
        with self.assertRaisesRegex(MarkerConfirmationError, "multiple matches"):
            validate_marker_confirmation(confirmation, self.review)

    def test_missing_role_and_wrong_group_assignment_are_rejected(self) -> None:
        confirmation = self._completed(self._build())
        missing = _copy_json(confirmation)
        del missing["confirmations"]["vehicle_internal_and_external_walls"]
        with self.assertRaisesRegex(MarkerConfirmationError, "missing required roles"):
            validate_marker_confirmation(missing, self.review)

        duplicate = _copy_json(confirmation)
        duplicate["confirmations"]["vehicle_internal_and_external_walls"][
            "selected_group_fingerprint_ids"
        ] = duplicate["confirmations"]["front_conical_surface"][
            "selected_group_fingerprint_ids"
        ]
        with self.assertRaisesRegex(MarkerConfirmationError, "exactly one complete group"):
            validate_marker_confirmation(duplicate, self.review)

    def test_independent_complete_group_evidence_is_required(self) -> None:
        with self.assertRaisesRegex(MarkerConfirmationError, "evidence is required"):
            build_marker_confirmation_template(
                self.review,
                self.measurements,
                self.evidence / "missing_groups.json",
            )

        incomplete = _copy_json(self.boundary_groups)
        incomplete["geometric_candidates"]["void_shell_boundary_groups"] = []
        with self.assertRaisesRegex(MarkerConfirmationError, "wall complete-group"):
            build_marker_confirmation_template(
                self.review,
                self.measurements,
                self.evidence / "incomplete_groups.json",
                boundary_group_candidates=incomplete,
            )

        overlap = _copy_json(self.boundary_groups)
        rear_groups = overlap["geometric_candidates"]["downstream_outer_groups"]
        duplicate_group = _copy_json(rear_groups[0])
        duplicate_group["fingerprint"]["geometric_class"] = (
            "maximum_x_interface_edge_closure"
        )
        duplicate_group["group_fingerprint_id"] = _payload_sha(
            duplicate_group["fingerprint"]
        )
        rear_groups[1] = duplicate_group
        rear_ids = sorted(group["group_fingerprint_id"] for group in rear_groups)
        overlap["role_hypotheses"]["rear_outlets"][
            "candidate_group_fingerprint_ids"
        ] = rear_ids
        overlap["rear_outlet_numbering_contract"][
            "candidate_group_fingerprint_ids"
        ] = rear_ids
        with self.assertRaisesRegex(MarkerConfirmationError, "overlap"):
            build_marker_confirmation_template(
                self.review,
                self.measurements,
                self.evidence / "overlap_groups.json",
                boundary_group_candidates=overlap,
            )

    def test_boundary_semantic_report_path_is_recorded(self) -> None:
        evidence_path = self.evidence / "boundary_semantic_candidates.json"
        evidence_path.write_text(
            json.dumps(self.boundary_groups, ensure_ascii=False), encoding="utf-8"
        )
        template = build_marker_confirmation_template(
            self.review,
            self.measurements,
            self.evidence / "path_based_template.json",
            boundary_group_candidates=evidence_path,
        )

        provenance = template["boundary_group_evidence"]
        self.assertEqual(provenance["path"], str(evidence_path.resolve()))
        self.assertEqual(provenance["sha256"], _sha(evidence_path))
        self.assertEqual(
            provenance["source_schema_version"],
            "cfdpipe.boundary_semantic_candidates.v1",
        )
        result = validate_marker_confirmation(self._completed(template), self.review)
        self.assertEqual(result["status"], "PASS")

        tampered = self._completed(_copy_json(template))
        farfield = next(
            group
            for group in tampered["boundary_group_candidates"]
            if group["candidate_role_kind"] == "farfield"
        )
        farfield["candidate_role_kind"] = "wall"
        with self.assertRaisesRegex(MarkerConfirmationError, "canonical evidence"):
            validate_marker_confirmation(tampered, self.review)

    def test_rear_group_numbering_is_one_to_one_without_z_inference(self) -> None:
        confirmation = self._completed(self._build())
        mapping = confirmation["rear_outlet_group_assignment"][
            "role_to_group_fingerprint"
        ]
        mapping["rear_outlet_2"] = mapping["rear_outlet_1"]
        with self.assertRaisesRegex(MarkerConfirmationError, "one-to-one"):
            validate_marker_confirmation(confirmation, self.review)

        swapped = self._completed(_copy_json(self._build_to_new_path("swap.json")))
        swapped_mapping = swapped["rear_outlet_group_assignment"][
            "role_to_group_fingerprint"
        ]
        first = swapped_mapping["rear_outlet_1"]
        swapped_mapping["rear_outlet_1"] = swapped_mapping["rear_outlet_2"]
        swapped_mapping["rear_outlet_2"] = first
        result = validate_marker_confirmation(swapped, self.review)
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["policy"]["rear_outlet_z_sign_inference_forbidden"])

    def test_solidify_writes_stable_confirmed_mapping_and_validates(self) -> None:
        self._build()
        output = self.evidence / "marker_confirmation_completed.json"

        document = self._solidify(
            self.template_path, output.name
        )

        self.assertTrue(output.is_file())
        self.assertEqual(
            json.loads(output.read_text(encoding="utf-8")), document
        )
        self.assertEqual(document["status"], "CONFIRMED_BY_HUMAN")
        self.assertFalse(document["markers_toml_written"])
        human = document["human_confirmation"]
        self.assertEqual(human["schema_version"], 1)
        self.assertEqual(human["confirmed_by"], "project_owner")
        self.assertEqual(human["confirmed_at_utc"], "2026-08-01T08:00:00Z")
        self.assertEqual(
            human["decision_basis"],
            "User explicitly confirmed the recommended mapping.",
        )
        self.assertEqual(
            human["original_confirmation_summary"], human["decision_basis"]
        )
        stable = human["stable_group_fingerprint_ids_by_role"]
        self.assertEqual(set(stable), set(self._recommended_tag_mapping()))
        self.assertTrue(all(len(group_id) == 64 for group_id in stable.values()))
        self.assertFalse(human["audit"]["gmsh_tags_are_matching_criteria"])
        self.assertEqual(document["solidification_validation"]["status"], "PASS")
        validation = validate_marker_confirmation(document, self.review)
        self.assertEqual(validation["status"], "PASS")
        self.assertEqual(validation["human_confirmation"]["status"], "PASS")
        self.assertFalse((self.root / "config" / "markers.toml").exists())

    def test_solidify_rejects_partial_and_wrong_audit_tag_sets(self) -> None:
        template = self._build()
        failures = (
            ("partial", [5]),
            ("wrong", [999]),
        )
        for name, rear_tags in failures:
            with self.subTest(name=name):
                tags = self._recommended_tag_mapping()
                tags["rear_outlet_1"] = rear_tags
                output = self.evidence / f"{name}_completed.json"
                with self.assertRaisesRegex(
                    MarkerConfirmationError, "exactly one complete candidate group"
                ):
                    self._solidify(template, output.name, tags=tags)
                self.assertFalse(output.exists())

    def test_solidify_rejects_duplicate_group_selection(self) -> None:
        template = self._build()
        tags = self._recommended_tag_mapping()
        tags["rear_outlet_2"] = list(tags["rear_outlet_1"])
        output = self.evidence / "duplicate_group_completed.json"

        with self.assertRaisesRegex(MarkerConfirmationError, "multiple solver roles"):
            self._solidify(template, output.name, tags=tags)

        self.assertFalse(output.exists())

    def test_solidify_rejects_wrong_measurement_candidate(self) -> None:
        template = self._build()
        output = self.evidence / "wrong_measurement_completed.json"

        with self.assertRaisesRegex(MarkerConfirmationError, "measurement candidate"):
            self._solidify(
                template,
                output.name,
                measurement_id="not_a_template_candidate",
            )

        self.assertFalse(output.exists())

    def test_solidify_rejects_overwrite_and_invalid_metadata(self) -> None:
        template = self._build()
        output = self.evidence / "existing_completed.json"
        output.write_text("preserve-me", encoding="utf-8")

        with self.assertRaisesRegex(MarkerConfirmationError, "overwrite"):
            self._solidify(template, output.name)
        self.assertEqual(output.read_text(encoding="utf-8"), "preserve-me")

        invalid_output = self.evidence / "invalid_metadata_completed.json"
        with self.assertRaisesRegex(MarkerConfirmationError, "UTC offset"):
            self._solidify(
                template,
                invalid_output.name,
                confirmed_at_utc="2026-08-01T08:00:00+08:00",
            )
        self.assertFalse(invalid_output.exists())

    def test_validation_report_records_saved_confirmation_provenance(self) -> None:
        self._build()
        confirmation_path = self.evidence / "confirmed_for_report.json"
        self._solidify(self.template_path, confirmation_path.name)
        report_path = self.evidence / "confirmation_validation.json"

        report = write_marker_confirmation_validation(
            confirmation_path,
            self.review,
            report_path,
            validated_at_utc="2026-08-01T08:05:00Z",
        )

        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["validation"]["status"], "PASS")
        self.assertEqual(report["confirmation"]["sha256"], _sha(confirmation_path))
        self.assertEqual(
            report["current_marker_review"]["sha256"], _sha(self.review)
        )
        self.assertEqual(
            json.loads(report_path.read_text(encoding="utf-8")), report
        )

    def test_validation_failure_does_not_write_a_pass_report(self) -> None:
        self._build()
        confirmation_path = self.evidence / "tampered_confirmation.json"
        document = self._solidify(self.template_path, confirmation_path.name)
        document["confirmations"]["rear_outlet_2"]["status"] = "PENDING"
        confirmation_path.write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8"
        )
        report_path = self.evidence / "must_not_exist_validation.json"

        with self.assertRaisesRegex(MarkerConfirmationError, "not CONFIRMED"):
            write_marker_confirmation_validation(
                confirmation_path,
                self.review,
                report_path,
                validated_at_utc="2026-08-01T08:05:00Z",
            )

        self.assertFalse(report_path.exists())

    def test_measurement_must_remain_non_solver_boundary(self) -> None:
        confirmation = self._completed(self._build())
        confirmation["confirmations"]["turning_section_outlet"][
            "solver_boundary"
        ] = True
        with self.assertRaisesRegex(MarkerConfirmationError, "solver_boundary=false"):
            validate_marker_confirmation(confirmation, self.review)

        invalid_candidates = _copy_json(self.measurements)
        invalid_candidates[0]["solver_boundary"] = True
        other_output = self.evidence / "invalid_measurement_template.json"
        with self.assertRaisesRegex(MarkerConfirmationError, "solver_boundary=false"):
            build_marker_confirmation_template(
                self.review,
                invalid_candidates,
                other_output,
                boundary_group_candidates=self.boundary_groups,
            )

    def test_template_refuses_config_and_existing_output(self) -> None:
        config_output = self.root / "config" / "marker_confirmation.json"
        with self.assertRaisesRegex(MarkerConfirmationError, "config"):
            build_marker_confirmation_template(
                self.review,
                self.measurements,
                config_output,
                boundary_group_candidates=self.boundary_groups,
            )

        self._build()
        with self.assertRaisesRegex(MarkerConfirmationError, "overwrite"):
            self._build()


if __name__ == "__main__":
    unittest.main()
