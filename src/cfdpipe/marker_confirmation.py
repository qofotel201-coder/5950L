"""Auditable human confirmation for geometry marker semantics.

This module deliberately sits between :mod:`cfdpipe.marker_review` and the
future formal ``config/markers.toml`` writer.  It can create a JSON document
for human review and validate an edited copy, but it never writes a production
marker configuration and never invokes Gmsh or another external program.

Gmsh entity and curve tags are retained only below ``audit``.  A confirmed
selection is re-matched by an unchanged STEP source hash, STEP solid/shell
membership, geometry, OCC entity type and a tag-independent boundary-loop
incidence signature.  A selection is valid only when that fingerprint has
exactly one match in the current evidence catalogue.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tomllib
from typing import Any, Mapping, Sequence


PathLike = str | os.PathLike[str]
_SHA256_LENGTH = 64
_SCHEMA_VERSION = 2
_DOCUMENT_KIND = "cfdpipe_marker_human_confirmation"
_BOUNDARY_SEMANTIC_SCHEMA = "cfdpipe.boundary_semantic_candidates.v1"
_BOUNDARY_GROUP_SCHEMA = "cfdpipe.boundary_group_fingerprint.v1"
_BOUNDARY_SURFACE_SCHEMA = "cfdpipe.boundary_surface_fingerprint.v1"
_DEFAULT_TOLERANCES: dict[str, float] = {
    "area_relative": 1.0e-8,
    "area_absolute_m2": 1.0e-10,
    "coordinate_absolute_m": 1.0e-7,
}


class MarkerConfirmationError(ValueError):
    """Raised when a confirmation document cannot be trusted."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _SHA256_LENGTH
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MarkerConfirmationError(f"cannot parse {label}: {error}") from error
    if not isinstance(value, dict):
        raise MarkerConfirmationError(f"{label} must contain a JSON object")
    return value


def _evidence_file(
    marker_review_path: Path,
    inputs: Mapping[str, Any],
    key: str,
) -> tuple[Path, str]:
    evidence = inputs.get(key)
    if not isinstance(evidence, Mapping):
        raise MarkerConfirmationError(f"marker review is missing {key} evidence")
    raw_path = evidence.get("path")
    declared_hash = evidence.get("sha256")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise MarkerConfirmationError(f"marker review {key} path is invalid")
    if not _is_sha256(declared_hash):
        raise MarkerConfirmationError(f"marker review {key} SHA256 is invalid")
    path = Path(raw_path)
    if not path.is_absolute():
        path = marker_review_path.parent / path
    path = path.resolve()
    if not path.is_file():
        raise MarkerConfirmationError(f"marker review {key} file does not exist: {path}")
    actual_hash = _sha256(path)
    if actual_hash.casefold() != str(declared_hash).casefold():
        raise MarkerConfirmationError(f"marker review {key} SHA256 mismatch")
    return path, actual_hash


def _finite_float(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise MarkerConfirmationError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise MarkerConfirmationError(f"{label} must be a finite number") from error
    if not math.isfinite(number):
        raise MarkerConfirmationError(f"{label} must be a finite number")
    return number


def _json_number_vector(
    value: object,
    *,
    length: int,
    label: str,
) -> list[float]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise MarkerConfirmationError(f"{label} is not valid JSON") from error
    if not isinstance(value, list) or len(value) != length:
        raise MarkerConfirmationError(f"{label} must contain {length} numbers")
    return [_finite_float(item, label) for item in value]


def _json_integer_list(value: object, label: str) -> list[int]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise MarkerConfirmationError(f"{label} is not valid JSON") from error
    if not isinstance(value, list):
        raise MarkerConfirmationError(f"{label} must contain a JSON list")
    result: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise MarkerConfirmationError(f"{label} must contain integers")
        result.append(item)
    if len(result) != len(set(result)):
        raise MarkerConfirmationError(f"{label} contains duplicate ids")
    return result


def _load_surface_catalog(path: Path) -> list[dict[str, Any]]:
    required = {
        "entity_tag",
        "area_m2",
        "centroid_m",
        "bounding_box_m",
        "adjacent_volumes",
        "boundary_curves",
    }
    try:
        stream = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as error:
        raise MarkerConfirmationError(f"cannot open surface catalog: {error}") from error
    with stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            missing = sorted(required - set(reader.fieldnames or ()))
            raise MarkerConfirmationError(
                f"surface catalog is missing required columns: {missing}"
            )
        surfaces: list[dict[str, Any]] = []
        seen: set[int] = set()
        for row_number, row in enumerate(reader, start=2):
            try:
                tag = int(row["entity_tag"])
            except (TypeError, ValueError) as error:
                raise MarkerConfirmationError(
                    f"surface catalog row {row_number} has an invalid entity tag"
                ) from error
            if tag <= 0 or tag in seen:
                raise MarkerConfirmationError(
                    f"surface catalog has invalid or duplicate entity tag {tag}"
                )
            seen.add(tag)
            area = _finite_float(row["area_m2"], f"surface {tag} area")
            if area <= 0.0:
                raise MarkerConfirmationError(f"surface {tag} area must be positive")
            surfaces.append(
                {
                    "entity_tag": tag,
                    "area_m2": area,
                    "centroid_m": _json_number_vector(
                        row["centroid_m"],
                        length=3,
                        label=f"surface {tag} centroid",
                    ),
                    "bounding_box_m": _json_number_vector(
                        row["bounding_box_m"],
                        length=6,
                        label=f"surface {tag} bounding box",
                    ),
                    "adjacent_volumes": _json_integer_list(
                        row["adjacent_volumes"],
                        f"surface {tag} adjacent volumes",
                    ),
                    "boundary_curves": _json_integer_list(
                        row["boundary_curves"],
                        f"surface {tag} boundary curves",
                    ),
                }
            )
    if not surfaces:
        raise MarkerConfirmationError("surface catalog contains no surfaces")
    return sorted(surfaces, key=lambda item: int(item["entity_tag"]))


def _load_surface_types(
    path: Path,
    catalog_tags: set[int],
    catalog_sha256: str,
) -> dict[int, str]:
    payload = _load_json(path, "surface types")
    if payload.get("status") != "PASS":
        raise MarkerConfirmationError("surface types evidence is not PASS")
    declared_catalog_hash = payload.get("surface_catalog_sha256")
    if declared_catalog_hash is not None and (
        not _is_sha256(declared_catalog_hash)
        or str(declared_catalog_hash).casefold() != catalog_sha256.casefold()
    ):
        raise MarkerConfirmationError(
            "surface types surface_catalog_sha256 mismatch"
        )
    raw_surfaces = payload.get("surfaces")
    if not isinstance(raw_surfaces, list):
        raise MarkerConfirmationError("surface types evidence has no surface list")
    types: dict[int, str] = {}
    for item in raw_surfaces:
        if not isinstance(item, Mapping):
            raise MarkerConfirmationError("surface types contains an invalid entry")
        tag = item.get("entity_tag")
        entity_type = item.get("entity_type")
        if isinstance(tag, bool) or not isinstance(tag, int) or tag <= 0:
            raise MarkerConfirmationError("surface types contains an invalid entity tag")
        if tag in types:
            raise MarkerConfirmationError(f"surface types repeats entity tag {tag}")
        if not isinstance(entity_type, str) or not entity_type.strip():
            raise MarkerConfirmationError(f"surface {tag} has no entity type")
        types[tag] = entity_type.strip()
    if set(types) != catalog_tags:
        missing = sorted(catalog_tags - set(types))
        extra = sorted(set(types) - catalog_tags)
        raise MarkerConfirmationError(
            f"surface type/catalog tag mismatch; missing={missing}, extra={extra}"
        )
    return types


def _required_roles(review: Mapping[str, Any]) -> list[dict[str, str]]:
    raw_roles = review.get("required_roles")
    if not isinstance(raw_roles, list) or not raw_roles:
        raise MarkerConfirmationError("marker review has no required roles")
    roles: list[dict[str, str]] = []
    names: set[str] = set()
    for item in raw_roles:
        if not isinstance(item, Mapping):
            raise MarkerConfirmationError("marker review has an invalid required role")
        role = item.get("role")
        kind = item.get("kind")
        if not isinstance(role, str) or not role.strip():
            raise MarkerConfirmationError("marker review has an empty required role")
        if not isinstance(kind, str) or not kind.strip():
            raise MarkerConfirmationError(f"required role {role!r} has no kind")
        role = role.strip()
        kind = kind.strip()
        if role in names:
            raise MarkerConfirmationError(f"marker review repeats role {role!r}")
        names.add(role)
        roles.append({"role": role, "kind": kind})
    if sum(item["kind"] == "measurement_surface" for item in roles) != 1:
        raise MarkerConfirmationError(
            "marker review must declare exactly one measurement_surface role"
        )
    return roles


def _project_roles(project_path: Path) -> tuple[list[dict[str, str]], bool]:
    try:
        payload = tomllib.loads(project_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise MarkerConfirmationError(f"cannot parse project TOML: {error}") from error
    boundaries = payload.get("boundaries")
    if not isinstance(boundaries, Mapping):
        raise MarkerConfirmationError("project TOML is missing [boundaries]")

    def required_string(key: str) -> str:
        value = boundaries.get(key)
        if not isinstance(value, str) or not value.strip():
            raise MarkerConfirmationError(
                f"project boundary field {key!r} must be a nonempty string"
            )
        return value.strip()

    rear = boundaries.get("rear_outlet_roles")
    if (
        not isinstance(rear, list)
        or len(rear) != 2
        or any(not isinstance(item, str) or not item.strip() for item in rear)
    ):
        raise MarkerConfirmationError(
            "project rear_outlet_roles must contain exactly two nonempty names"
        )
    roles = [
        {"role": required_string("farfield_role"), "kind": "farfield"},
        {"role": required_string("wall_role"), "kind": "wall"},
        *[
            {"role": str(item).strip(), "kind": "rear_outlet"}
            for item in rear
        ],
        {
            "role": required_string("measurement_surface_role"),
            "kind": "measurement_surface",
        },
    ]
    if len({item["role"] for item in roles}) != len(roles):
        raise MarkerConfirmationError("project boundary role names must be unique")
    solver_boundary = boundaries.get("measurement_surface_is_solver_boundary")
    if not isinstance(solver_boundary, bool):
        raise MarkerConfirmationError(
            "project measurement_surface_is_solver_boundary must be boolean"
        )
    return roles, solver_boundary


def _component_shell_map(review: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw_components = review.get("shell_components")
    raw_correspondence = review.get("step_shell_correspondence")
    if not isinstance(raw_components, list) or not isinstance(raw_correspondence, list):
        raise MarkerConfirmationError("marker review lacks shell correspondence evidence")

    correspondence: dict[str, dict[str, Any]] = {}
    for item in raw_correspondence:
        if not isinstance(item, Mapping) or item.get("status") != "CONFIRMED":
            continue
        candidates = item.get("candidate_catalog_component_ids")
        if not isinstance(candidates, list) or len(candidates) != 1:
            continue
        component_id = candidates[0]
        if not isinstance(component_id, str) or not component_id:
            continue
        if component_id in correspondence:
            raise MarkerConfirmationError(
                f"shell component {component_id!r} has multiple STEP correspondences"
            )
        solid_id = item.get("solid_id")
        root_shell_id = item.get("root_shell_id")
        face_count = item.get("face_count")
        shell_role = item.get("step_role_in_solid")
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (solid_id, root_shell_id, face_count)
        ) or not isinstance(shell_role, str):
            raise MarkerConfirmationError(
                f"shell component {component_id!r} correspondence is incomplete"
            )
        correspondence[component_id] = {
            "step_solid_id": solid_id,
            "step_root_shell_id": root_shell_id,
            "step_shell_role": shell_role,
            "shell_face_count": face_count,
        }

    result: dict[str, dict[str, Any]] = {}
    for component in raw_components:
        if not isinstance(component, Mapping):
            raise MarkerConfirmationError("marker review has an invalid shell component")
        component_id = component.get("component_id")
        tags = component.get("surface_tags")
        volume_tag = component.get("volume_entity_tag")
        if (
            not isinstance(component_id, str)
            or not component_id
            or not isinstance(tags, list)
            or isinstance(volume_tag, bool)
            or not isinstance(volume_tag, int)
        ):
            raise MarkerConfirmationError("marker review shell component is incomplete")
        shell = correspondence.get(component_id)
        if shell is None:
            raise MarkerConfirmationError(
                f"shell component {component_id!r} lacks a unique confirmed STEP shell"
            )
        for tag in tags:
            if isinstance(tag, bool) or not isinstance(tag, int):
                raise MarkerConfirmationError(
                    f"shell component {component_id!r} has an invalid surface tag"
                )
            if str(tag) in result:
                raise MarkerConfirmationError(
                    f"surface {tag} belongs to multiple catalog shell components"
                )
            result[str(tag)] = {
                "component_id": component_id,
                "gmsh_volume_entity_tag": volume_tag,
                "solid_shell": dict(shell),
            }
    return result


def _boundary_loop_signatures(
    surfaces: Sequence[Mapping[str, Any]],
) -> dict[int, dict[str, Any]]:
    curve_owners: dict[int, set[int]] = {}
    by_tag = {int(surface["entity_tag"]): surface for surface in surfaces}
    for surface in surfaces:
        tag = int(surface["entity_tag"])
        for curve in surface["boundary_curves"]:
            curve_owners.setdefault(int(curve), set()).add(tag)

    signatures: dict[int, dict[str, Any]] = {}
    for tag, surface in by_tag.items():
        curves = [int(value) for value in surface["boundary_curves"]]
        neighbor_counts: dict[int, int] = {}
        incidences: list[int] = []
        for curve in curves:
            owners = curve_owners[curve]
            incidences.append(len(owners))
            for neighbor in owners - {tag}:
                neighbor_counts[neighbor] = neighbor_counts.get(neighbor, 0) + 1
        signatures[tag] = {
            "curve_count": len(curves),
            "curve_incidence_multiset": sorted(incidences),
            "neighbor_surface_count": len(neighbor_counts),
            "neighbor_shared_curve_count_multiset": sorted(neighbor_counts.values()),
            "loop_partition_available": False,
            "basis": (
                "tag-independent boundary-curve incidence signature; the source "
                "catalog does not encode separate wire/loop partitions"
            ),
        }
    return signatures


def _build_entity_records(
    review: Mapping[str, Any],
    surfaces: Sequence[Mapping[str, Any]],
    surface_types: Mapping[int, str],
    *,
    step_sha256: str,
    project_sha256: str,
    catalog_sha256: str,
    require_unique_fingerprints: bool,
) -> list[dict[str, Any]]:
    shell_by_surface = _component_shell_map(review)
    loop_signatures = _boundary_loop_signatures(surfaces)
    records: list[dict[str, Any]] = []
    ids: set[str] = set()
    for surface in surfaces:
        tag = int(surface["entity_tag"])
        membership = shell_by_surface.get(str(tag))
        if membership is None:
            raise MarkerConfirmationError(
                f"surface {tag} has no unique solid/shell correspondence"
            )
        fingerprint = {
            "source_step_sha256": step_sha256,
            "source_project_sha256": project_sha256,
            "solid_shell": dict(membership["solid_shell"]),
            "area_m2": float(surface["area_m2"]),
            "centroid_m": list(surface["centroid_m"]),
            "bounding_box_m": list(surface["bounding_box_m"]),
            "entity_type": surface_types[tag],
            "boundary_loop_signature": loop_signatures[tag],
        }
        fingerprint_id = _canonical_sha256(fingerprint)
        if require_unique_fingerprints and fingerprint_id in ids:
            raise MarkerConfirmationError(
                "source evidence contains multiple surfaces with one stable fingerprint"
            )
        ids.add(fingerprint_id)
        records.append(
            {
                "fingerprint_id": fingerprint_id,
                "fingerprint": fingerprint,
                "audit": {
                    "gmsh_surface_entity_tag": tag,
                    "gmsh_volume_entity_tag": membership["gmsh_volume_entity_tag"],
                    "catalog_component_id": membership["component_id"],
                    "gmsh_boundary_curve_tags": list(surface["boundary_curves"]),
                    "catalog_sha256": catalog_sha256,
                    "tag_is_matching_criterion": False,
                },
            }
        )
    return records


def _group_topology_signature(
    members: Sequence[Mapping[str, Any]],
    all_entities: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a tag-independent surface-group connectivity signature."""

    all_curve_owners: dict[int, set[int]] = {}
    all_curves_by_tag: dict[int, set[int]] = {}
    for entity in all_entities:
        audit = entity.get("audit")
        if not isinstance(audit, Mapping):
            raise MarkerConfirmationError("entity audit evidence is invalid")
        tag = audit.get("gmsh_surface_entity_tag")
        curves = audit.get("gmsh_boundary_curve_tags")
        if isinstance(tag, bool) or not isinstance(tag, int) or not isinstance(
            curves, list
        ):
            raise MarkerConfirmationError("entity curve audit evidence is invalid")
        curve_set = {int(curve) for curve in curves}
        all_curves_by_tag[tag] = curve_set
        for curve in curve_set:
            all_curve_owners.setdefault(curve, set()).add(tag)

    member_tags = {
        int(entity["audit"]["gmsh_surface_entity_tag"]) for entity in members
    }
    graph = {tag: set() for tag in member_tags}
    pair_shared_counts: list[int] = []
    member_records: list[dict[str, Any]] = []
    for tag in sorted(member_tags):
        neighbor_shared_counts: list[int] = []
        external_neighbors: set[int] = set()
        group_incidences: list[int] = []
        global_incidences: list[int] = []
        for other in sorted(member_tags - {tag}):
            shared = len(all_curves_by_tag[tag] & all_curves_by_tag[other])
            if shared:
                graph[tag].add(other)
                neighbor_shared_counts.append(shared)
                if tag < other:
                    pair_shared_counts.append(shared)
        for curve in all_curves_by_tag[tag]:
            owners = all_curve_owners[curve]
            group_incidences.append(len(owners & member_tags))
            global_incidences.append(len(owners))
            external_neighbors.update(owners - member_tags)
        member_records.append(
            {
                "curve_count": len(all_curves_by_tag[tag]),
                "group_neighbor_count": len(graph[tag]),
                "shared_curve_count_spectrum": sorted(neighbor_shared_counts),
                "group_curve_incidence_spectrum": sorted(group_incidences),
                "global_curve_incidence_spectrum": sorted(global_incidences),
                "external_neighbor_count": len(external_neighbors),
            }
        )

    unseen = set(member_tags)
    component_sizes: list[int] = []
    while unseen:
        stack = [min(unseen)]
        found: set[int] = set()
        while stack:
            tag = stack.pop()
            if tag in found:
                continue
            found.add(tag)
            unseen.discard(tag)
            stack.extend(graph[tag] - found)
        component_sizes.append(len(found))

    group_curves = {
        curve
        for tag in member_tags
        for curve in all_curves_by_tag[tag]
    }
    group_curve_incidences = [
        len(all_curve_owners[curve] & member_tags) for curve in group_curves
    ]
    return {
        "connected_component_count": len(component_sizes),
        "connected_component_member_count_spectrum": sorted(component_sizes),
        "member_topology_spectrum": sorted(
            member_records,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        ),
        "pair_shared_curve_count_spectrum": sorted(pair_shared_counts),
        "unique_group_curve_count": len(group_curves),
        "group_curve_incidence_spectrum": sorted(group_curve_incidences),
        "group_internal_curve_count": sum(
            incidence > 1 for incidence in group_curve_incidences
        ),
        "group_boundary_curve_count": sum(
            incidence == 1 for incidence in group_curve_incidences
        ),
        "basis": (
            "tag-independent member connectivity and boundary-curve incidence "
            "spectra; runtime curve and surface ids are excluded"
        ),
    }


def _group_geometry_signature(
    members: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    areas = [float(item["fingerprint"]["area_m2"]) for item in members]
    total_area = sum(areas)
    if not math.isfinite(total_area) or total_area <= 0.0:
        raise MarkerConfirmationError("surface group has invalid total area")
    centroids = [item["fingerprint"]["centroid_m"] for item in members]
    bounds = [item["fingerprint"]["bounding_box_m"] for item in members]
    weighted_centroid = [
        sum(area * float(centroid[index]) for area, centroid in zip(areas, centroids))
        / total_area
        for index in range(3)
    ]
    entity_type_counts: dict[str, int] = {}
    shell_counts: dict[str, tuple[Mapping[str, Any], int]] = {}
    for member in members:
        fingerprint = member["fingerprint"]
        entity_type = str(fingerprint["entity_type"])
        entity_type_counts[entity_type] = entity_type_counts.get(entity_type, 0) + 1
        shell = fingerprint["solid_shell"]
        key = json.dumps(shell, sort_keys=True, separators=(",", ":"))
        previous = shell_counts.get(key)
        shell_counts[key] = (shell, 1 if previous is None else previous[1] + 1)
    return {
        "total_area_m2": total_area,
        "member_area_spectrum_m2": sorted(areas),
        "area_weighted_centroid_m": weighted_centroid,
        "bounding_box_m": [
            min(float(value[index]) for value in bounds) for index in range(3)
        ]
        + [
            max(float(value[index]) for value in bounds) for index in range(3, 6)
        ],
        "entity_type_counts": [
            {"entity_type": name, "member_count": count}
            for name, count in sorted(entity_type_counts.items())
        ],
        "solid_shell_member_counts": [
            {"solid_shell": dict(shell), "member_count": count}
            for _, (shell, count) in sorted(shell_counts.items())
        ],
    }


def _build_group_fingerprint(
    members: Sequence[Mapping[str, Any]],
    all_entities: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if len(members) < 2:
        raise MarkerConfirmationError(
            "rear boundary group must contain at least two complete member surfaces"
        )
    member_ids = [str(item["fingerprint_id"]) for item in members]
    if len(member_ids) != len(set(member_ids)):
        raise MarkerConfirmationError("surface group repeats a member fingerprint")
    source_steps = {
        str(item["fingerprint"]["source_step_sha256"]) for item in members
    }
    source_projects = {
        str(item["fingerprint"]["source_project_sha256"]) for item in members
    }
    if len(source_steps) != 1 or len(source_projects) != 1:
        raise MarkerConfirmationError("surface group members have mixed source evidence")
    ordered = sorted(members, key=lambda item: str(item["fingerprint_id"]))
    return {
        "source_step_sha256": next(iter(source_steps)),
        "source_project_sha256": next(iter(source_projects)),
        "member_count": len(ordered),
        "members": [
            {
                "fingerprint_id": item["fingerprint_id"],
                "fingerprint": item["fingerprint"],
            }
            for item in ordered
        ],
        "topology": _group_topology_signature(ordered, all_entities),
        "geometry": _group_geometry_signature(ordered),
    }


def _semantic_surface_matches_context(
    raw_member: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    expected_step_sha256: str,
    expected_project_sha256: str,
) -> None:
    """Cross-check one independent semantic member against import evidence.

    Runtime tags are used only to perform this one-time evidence join.  The
    normalized fingerprint produced afterwards contains no runtime surface or
    curve ids.
    """

    member_id = raw_member.get("surface_fingerprint_id")
    fingerprint = raw_member.get("fingerprint")
    audit = raw_member.get("audit")
    if (
        not isinstance(member_id, str)
        or not _is_sha256(member_id)
        or not isinstance(fingerprint, Mapping)
        or _canonical_sha256(fingerprint) != member_id
        or not isinstance(audit, Mapping)
    ):
        raise MarkerConfirmationError(
            "boundary semantic group has an invalid member fingerprint"
        )
    if (
        fingerprint.get("schema") != _BOUNDARY_SURFACE_SCHEMA
        or fingerprint.get("source_step_sha256") != expected_step_sha256
        or fingerprint.get("source_project_sha256") != expected_project_sha256
    ):
        raise MarkerConfirmationError(
            "boundary semantic member source or schema is stale"
        )
    if (
        audit.get("tag_is_matching_criterion") is not False
        or audit.get("boundary_curve_tags_are_matching_criteria") is not False
    ):
        raise MarkerConfirmationError(
            "boundary semantic member must keep Gmsh ids as audit evidence only"
        )

    tag = audit.get("gmsh_surface_entity_tag")
    current_audit = current.get("audit")
    current_fingerprint = current.get("fingerprint")
    if (
        isinstance(tag, bool)
        or not isinstance(tag, int)
        or not isinstance(current_audit, Mapping)
        or current_audit.get("gmsh_surface_entity_tag") != tag
        or not isinstance(current_fingerprint, Mapping)
    ):
        raise MarkerConfirmationError(
            "boundary semantic member audit does not match current import evidence"
        )

    semantic_shell = fingerprint.get("solid_shell")
    current_shell = current_fingerprint.get("solid_shell")
    if not isinstance(semantic_shell, Mapping) or not isinstance(
        current_shell, Mapping
    ):
        raise MarkerConfirmationError("boundary semantic member shell is invalid")
    for field in (
        "step_solid_id",
        "step_root_shell_id",
        "step_shell_role",
    ):
        if semantic_shell.get(field) != current_shell.get(field):
            raise MarkerConfirmationError(
                "boundary semantic member solid/shell evidence is stale"
            )
    if fingerprint.get("entity_type") != current_fingerprint.get("entity_type"):
        raise MarkerConfirmationError(
            "boundary semantic member entity type is stale"
        )

    area = _finite_float(fingerprint.get("area_m2"), "semantic member area")
    current_area = _finite_float(
        current_fingerprint.get("area_m2"), "current member area"
    )
    area_tolerance = max(
        _DEFAULT_TOLERANCES["area_absolute_m2"],
        _DEFAULT_TOLERANCES["area_relative"] * max(abs(area), abs(current_area)),
    )
    if abs(area - current_area) > area_tolerance:
        raise MarkerConfirmationError("boundary semantic member area is stale")
    coordinate_tolerance = _DEFAULT_TOLERANCES["coordinate_absolute_m"]
    for field, length in (("centroid_m", 3), ("bounding_box_m", 6)):
        expected = _json_number_vector(
            fingerprint.get(field), length=length, label=f"semantic member {field}"
        )
        actual = _json_number_vector(
            current_fingerprint.get(field),
            length=length,
            label=f"current member {field}",
        )
        if any(abs(first - second) > coordinate_tolerance for first, second in zip(expected, actual)):
            raise MarkerConfirmationError(
                f"boundary semantic member {field} is stale"
            )

    current_curves = current_audit.get("gmsh_boundary_curve_tags")
    raw_curves = audit.get("boundary_curve_tags")
    if (
        not isinstance(current_curves, list)
        or not isinstance(raw_curves, list)
        or any(
            isinstance(curve, bool) or not isinstance(curve, int)
            for curve in raw_curves
        )
        or len(raw_curves) != len(set(raw_curves))
        or sorted(raw_curves) != sorted(current_curves)
    ):
        raise MarkerConfirmationError(
            "boundary semantic member curve audit is stale"
        )
    curve_count = fingerprint.get("boundary_curve_count")
    if (
        isinstance(curve_count, bool)
        or not isinstance(curve_count, int)
        or curve_count != len(raw_curves)
    ):
        raise MarkerConfirmationError(
            "boundary semantic member boundary-curve count is invalid"
        )


def _semantic_group_tags(
    raw_group: Mapping[str, Any],
    *,
    expected_geometric_classes: set[str],
    expected_step_sha256: str,
    expected_project_sha256: str,
    records_by_tag: Mapping[int, Mapping[str, Any]],
) -> list[int]:
    """Validate an independent complete group and return audit-only tags."""

    if (
        raw_group.get("geometry_status") != "CONFIRMED_GEOMETRY"
        or raw_group.get("business_role_status") != "CANDIDATE_ROLE"
    ):
        raise MarkerConfirmationError(
            "boundary semantic group is not confirmed geometry-only evidence"
        )
    group_id = raw_group.get("group_fingerprint_id")
    fingerprint = raw_group.get("fingerprint")
    raw_members = raw_group.get("members")
    audit = raw_group.get("audit")
    if (
        not isinstance(group_id, str)
        or not _is_sha256(group_id)
        or not isinstance(fingerprint, Mapping)
        or _canonical_sha256(fingerprint) != group_id
        or not isinstance(raw_members, list)
        or len(raw_members) < 2
        or not isinstance(audit, Mapping)
    ):
        raise MarkerConfirmationError(
            "boundary semantic group fingerprint or membership is invalid"
        )
    if (
        fingerprint.get("schema") != _BOUNDARY_GROUP_SCHEMA
        or fingerprint.get("source_step_sha256") != expected_step_sha256
        or fingerprint.get("source_project_sha256") != expected_project_sha256
        or fingerprint.get("geometric_class") not in expected_geometric_classes
        or audit.get("gmsh_tags_are_matching_criteria") is not False
    ):
        raise MarkerConfirmationError(
            "boundary group has the wrong geometric class or source STEP"
        )
    member_count = fingerprint.get("member_count")
    if (
        isinstance(member_count, bool)
        or not isinstance(member_count, int)
        or member_count != len(raw_members)
    ):
        raise MarkerConfirmationError(
            "boundary semantic group has incomplete membership"
        )

    tags: list[int] = []
    member_ids: list[str] = []
    member_fingerprints: list[Mapping[str, Any]] = []
    curves_by_tag: dict[int, set[int]] = {}
    for raw_member in raw_members:
        if not isinstance(raw_member, Mapping):
            raise MarkerConfirmationError(
                "boundary semantic group contains an invalid member"
            )
        member_audit = raw_member.get("audit")
        if not isinstance(member_audit, Mapping):
            raise MarkerConfirmationError(
                "boundary semantic group member lacks audit evidence"
            )
        tag = member_audit.get("gmsh_surface_entity_tag")
        if isinstance(tag, bool) or not isinstance(tag, int):
            raise MarkerConfirmationError(
                "boundary semantic group member has an invalid audit tag"
            )
        current = records_by_tag.get(tag)
        if current is None:
            raise MarkerConfirmationError(
                f"boundary semantic group references unknown surface {tag}"
            )
        _semantic_surface_matches_context(
            raw_member,
            current,
            expected_step_sha256=expected_step_sha256,
            expected_project_sha256=expected_project_sha256,
        )
        tags.append(tag)
        member_ids.append(str(raw_member["surface_fingerprint_id"]))
        member_fingerprints.append(raw_member["fingerprint"])
        curves_by_tag[tag] = set(member_audit["boundary_curve_tags"])
    if len(tags) != len(set(tags)) or len(member_ids) != len(set(member_ids)):
        raise MarkerConfirmationError(
            "boundary semantic group repeats a member surface"
        )
    audit_tags = audit.get("current_surface_tags")
    if (
        not isinstance(audit_tags, list)
        or any(isinstance(tag, bool) or not isinstance(tag, int) for tag in audit_tags)
        or sorted(audit_tags) != sorted(tags)
    ):
        raise MarkerConfirmationError(
            "boundary semantic group audit membership is inconsistent"
        )
    declared_member_ids = fingerprint.get("member_surface_fingerprint_ids")
    if declared_member_ids != sorted(member_ids):
        raise MarkerConfirmationError(
            "boundary semantic group member fingerprint list is inconsistent"
        )

    group_shell = fingerprint.get("solid_shell")
    if not isinstance(group_shell, Mapping) or any(
        member.get("solid_shell") != group_shell for member in member_fingerprints
    ):
        raise MarkerConfirmationError(
            "boundary semantic group members do not share one solid/shell fingerprint"
        )
    type_counts: dict[str, int] = {}
    for member in member_fingerprints:
        name = str(member.get("entity_type"))
        type_counts[name] = type_counts.get(name, 0) + 1
    if fingerprint.get("entity_type_histogram") != dict(sorted(type_counts.items())):
        raise MarkerConfirmationError(
            "boundary semantic group entity-type histogram is inconsistent"
        )

    areas = [
        _finite_float(member.get("area_m2"), "boundary semantic group member area")
        for member in member_fingerprints
    ]
    total_area = sum(areas)
    declared_total = _finite_float(
        fingerprint.get("total_area_m2"), "boundary semantic group total area"
    )
    area_tolerance = max(
        _DEFAULT_TOLERANCES["area_absolute_m2"],
        _DEFAULT_TOLERANCES["area_relative"]
        * max(abs(total_area), abs(declared_total)),
    )
    if total_area <= 0.0 or abs(total_area - declared_total) > area_tolerance:
        raise MarkerConfirmationError(
            "boundary semantic group total area is inconsistent"
        )
    centroids = [
        _json_number_vector(
            member.get("centroid_m"), length=3, label="semantic member centroid"
        )
        for member in member_fingerprints
    ]
    bounds = [
        _json_number_vector(
            member.get("bounding_box_m"),
            length=6,
            label="semantic member bounding box",
        )
        for member in member_fingerprints
    ]
    expected_centroid = [
        sum(area * centroid[axis] for area, centroid in zip(areas, centroids))
        / total_area
        for axis in range(3)
    ]
    expected_bounds = [
        min(bound[axis] for bound in bounds) for axis in range(3)
    ] + [max(bound[axis] for bound in bounds) for axis in range(3, 6)]
    declared_centroid = _json_number_vector(
        fingerprint.get("area_weighted_centroid_m"),
        length=3,
        label="boundary semantic group centroid",
    )
    declared_bounds = _json_number_vector(
        fingerprint.get("bounding_box_m"),
        length=6,
        label="boundary semantic group bounding box",
    )
    coordinate_tolerance = _DEFAULT_TOLERANCES["coordinate_absolute_m"]
    if any(
        abs(first - second) > coordinate_tolerance
        for first, second in zip(expected_centroid, declared_centroid)
    ) or any(
        abs(first - second) > coordinate_tolerance
        for first, second in zip(expected_bounds, declared_bounds)
    ):
        raise MarkerConfirmationError(
            "boundary semantic group aggregate geometry is inconsistent"
        )

    id_by_tag = dict(zip(tags, member_ids))
    edges: list[dict[str, Any]] = []
    for index, first in enumerate(tags):
        for second in tags[index + 1 :]:
            shared = len(curves_by_tag[first] & curves_by_tag[second])
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
    for curves in curves_by_tag.values():
        for curve in curves:
            curve_counts[curve] = curve_counts.get(curve, 0) + 1
    if (
        fingerprint.get("connectivity_edges") != edges
        or fingerprint.get("external_boundary_curve_count")
        != sum(count == 1 for count in curve_counts.values())
        or fingerprint.get("internal_shared_curve_multiplicities")
        != sorted(count for count in curve_counts.values() if count > 1)
    ):
        raise MarkerConfirmationError(
            "boundary semantic group connectivity fingerprint is inconsistent"
        )
    return sorted(tags)


def _normalize_boundary_semantic_report(
    payload: Mapping[str, Any],
    *,
    context: Mapping[str, Any],
    evidence_path: Path | None,
    evidence_sha: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if (
        payload.get("business_semantics_asserted") is not False
        or payload.get("marker_configuration_ready") is not False
    ):
        raise MarkerConfirmationError(
            "boundary semantic evidence must not pre-assign business roles"
        )
    source = payload.get("source")
    source_step = source.get("step_sha256") if isinstance(source, Mapping) else None
    source_project = (
        source.get("project_sha256") if isinstance(source, Mapping) else None
    )
    if (
        not _is_sha256(source_step)
        or str(source_step).lower() != context["source"]["step_sha256"]
        or not _is_sha256(source_project)
        or str(source_project).lower() != context["source"]["project_sha256"]
    ):
        raise MarkerConfirmationError(
            "boundary semantic source hashes do not match marker review"
        )
    hypotheses = payload.get("role_hypotheses")
    geometric = payload.get("geometric_candidates")
    if not isinstance(hypotheses, Mapping) or not isinstance(geometric, Mapping):
        raise MarkerConfirmationError(
            "boundary semantic report lacks complete group hypotheses"
        )

    group_specs = (
        (
            "farfield",
            "upstream_outer_groups",
            {"lower_x_outer_shell_before_interface_edge"},
            "CANDIDATE",
            1,
        ),
        (
            "wall",
            "void_shell_boundary_groups",
            {"void_shell_boundary"},
            "CANDIDATE",
            1,
        ),
        (
            "rear_outlet",
            "downstream_outer_groups",
            {
                "positive_x_continuation_from_interface",
                "maximum_x_interface_edge_closure",
            },
            "CANDIDATE_NUMBERING_PENDING",
            2,
        ),
    )
    raw_groups_by_kind: dict[str, list[Mapping[str, Any]]] = {}
    independent_ids_by_kind: dict[str, list[str]] = {}
    for candidate_kind, collection_name, geometric_classes, status, count in group_specs:
        raw_collection = geometric.get(collection_name)
        hypothesis_name = "rear_outlets" if candidate_kind == "rear_outlet" else candidate_kind
        hypothesis = hypotheses.get(hypothesis_name)
        if (
            not isinstance(raw_collection, list)
            or len(raw_collection) != count
            or any(not isinstance(item, Mapping) for item in raw_collection)
            or not isinstance(hypothesis, Mapping)
            or hypothesis.get("status") != status
        ):
            raise MarkerConfirmationError(
                f"boundary semantic {candidate_kind} complete-group evidence is missing"
            )
        raw_ids = [str(item.get("group_fingerprint_id")) for item in raw_collection]
        hypothesis_ids = hypothesis.get("candidate_group_fingerprint_ids")
        if (
            len(set(raw_ids)) != count
            or any(not _is_sha256(group_id) for group_id in raw_ids)
            or not isinstance(hypothesis_ids, list)
            or set(hypothesis_ids) != set(raw_ids)
        ):
            raise MarkerConfirmationError(
                f"boundary semantic {candidate_kind} group hypothesis is inconsistent"
            )
        if any(not isinstance(item.get("fingerprint"), Mapping) for item in raw_collection):
            raise MarkerConfirmationError(
                f"boundary semantic {candidate_kind} group fingerprint is missing"
            )
        actual_classes = {
            item["fingerprint"].get("geometric_class") for item in raw_collection
        }
        if actual_classes != geometric_classes:
            raise MarkerConfirmationError(
                f"boundary semantic {candidate_kind} geometric classes are incomplete"
            )
        raw_groups_by_kind[candidate_kind] = list(raw_collection)
        independent_ids_by_kind[candidate_kind] = raw_ids

    contract = payload.get("rear_outlet_numbering_contract")
    if (
        not isinstance(contract, Mapping)
        or contract.get("status") != "PENDING_HUMAN_CONFIRMATION"
        or contract.get("strategy") != "explicit_group_fingerprint_mapping"
        or contract.get("required_role_count") != 2
        or contract.get("forbidden_inference") != "single_surface_centroid_z"
    ):
        raise MarkerConfirmationError(
            "boundary semantic report lacks the explicit rear group mapping contract"
        )
    contract_ids = contract.get("candidate_group_fingerprint_ids")
    if (
        not isinstance(contract_ids, list)
        or len(contract_ids) != 2
        or len(set(contract_ids)) != 2
        or any(not _is_sha256(group_id) for group_id in contract_ids)
    ):
        raise MarkerConfirmationError(
            "exactly two complete rear group fingerprints are required"
        )
    if set(independent_ids_by_kind["rear_outlet"]) != set(contract_ids):
        raise MarkerConfirmationError(
            "downstream groups do not match the rear numbering contract"
        )

    records_by_tag = {
        int(item["audit"]["gmsh_surface_entity_tag"]): item
        for item in context["records"]
    }
    groups: list[dict[str, Any]] = []
    used_tags: set[int] = set()
    normalized_ids: set[str] = set()
    classes_by_kind = {
        candidate_kind: geometric_classes
        for candidate_kind, _, geometric_classes, _, _ in group_specs
    }
    for candidate_kind in ("farfield", "wall", "rear_outlet"):
        for raw in raw_groups_by_kind[candidate_kind]:
            tags = _semantic_group_tags(
                raw,
                expected_geometric_classes=classes_by_kind[candidate_kind],
                expected_step_sha256=str(source_step).lower(),
                expected_project_sha256=str(source_project).lower(),
                records_by_tag=records_by_tag,
            )
            overlap = sorted(used_tags & set(tags))
            if overlap:
                raise MarkerConfirmationError(
                    f"boundary group candidates overlap on surfaces {overlap}"
                )
            used_tags.update(tags)
            members = [records_by_tag[tag] for tag in tags]
            fingerprint = _build_group_fingerprint(members, context["records"])
            normalized_id = _canonical_sha256(fingerprint)
            if normalized_id in normalized_ids:
                raise MarkerConfirmationError(
                    "boundary semantic evidence contains duplicate group fingerprints"
                )
            normalized_ids.add(normalized_id)
            independent_id = str(raw["group_fingerprint_id"])
            geometric_class = str(raw["fingerprint"]["geometric_class"])
            groups.append(
                {
                    "candidate_id": f"{candidate_kind}_group_{independent_id[:16]}",
                    "kind": "solver_boundary_surface_group",
                    "candidate_role_kind": candidate_kind,
                    "geometric_class": geometric_class,
                    "business_role_asserted": False,
                    "group_fingerprint_id": normalized_id,
                    "group_fingerprint": fingerprint,
                    "completeness": {
                        "status": "COMPLETE",
                        "expected_member_count": len(tags),
                        "basis": (
                            "independent boundary-semantic evidence identified one "
                            f"complete connected {geometric_class} group"
                        ),
                    },
                    "audit": {
                        "gmsh_surface_entity_tags": tags,
                        "independent_group_fingerprint_id": independent_id,
                        "boundary_group_evidence_sha256": evidence_sha,
                        "tag_is_matching_criterion": False,
                    },
                }
            )
    unresolved = geometric.get("transverse_or_mixed_outer_groups")
    if unresolved != []:
        raise MarkerConfirmationError(
            "boundary semantic report contains unresolved non-interface surfaces"
        )
    interfaces = geometric.get("near_coincident_interface_pairs")
    if not isinstance(interfaces, list):
        raise MarkerConfirmationError(
            "boundary semantic report lacks interface partition evidence"
        )
    interface_tags: set[int] = set()
    for interface in interfaces:
        audit = interface.get("audit") if isinstance(interface, Mapping) else None
        tags = audit.get("current_surface_tags") if isinstance(audit, Mapping) else None
        if (
            not isinstance(tags, list)
            or len(tags) != 2
            or audit.get("gmsh_tags_are_matching_criteria") is not False
            or any(isinstance(tag, bool) or not isinstance(tag, int) for tag in tags)
            or len(set(tags)) != 2
            or any(tag not in records_by_tag for tag in tags)
            or used_tags.intersection(tags)
            or interface_tags.intersection(tags)
        ):
            raise MarkerConfirmationError(
                "boundary semantic interface partition is invalid or overlapping"
            )
        interface_tags.update(tags)
    if used_tags | interface_tags != set(records_by_tag):
        raise MarkerConfirmationError(
            "boundary semantic groups and interfaces do not cover all surfaces"
        )
    partition = payload.get("partition_audit")
    partition_interface_tags = (
        partition.get("current_interface_surface_tags")
        if isinstance(partition, Mapping)
        else None
    )
    partition_group_tags = (
        partition.get("current_group_surface_tags")
        if isinstance(partition, Mapping)
        else None
    )
    if (
        not isinstance(partition, Mapping)
        or not isinstance(partition_interface_tags, list)
        or not isinstance(partition_group_tags, list)
        or any(
            isinstance(tag, bool) or not isinstance(tag, int)
            for tag in [*partition_interface_tags, *partition_group_tags]
        )
        or partition.get("all_surfaces_assigned_once") is not True
        or partition.get("surface_count") != len(records_by_tag)
        or partition.get("assigned_surface_count") != len(records_by_tag)
        or partition.get("gmsh_tags_are_matching_criteria") is not False
        or sorted(partition_interface_tags) != sorted(interface_tags)
        or sorted(partition_group_tags) != sorted(used_tags)
    ):
        raise MarkerConfirmationError(
            "boundary semantic partition audit is missing or inconsistent"
        )
    return groups, {
        "path": None if evidence_path is None else str(evidence_path),
        "sha256": evidence_sha,
        "source_step_sha256": str(source_step).lower(),
        "source_project_sha256": str(source_project).lower(),
        "evidence_kind": "independent_boundary_group_candidates",
        "source_schema_version": _BOUNDARY_SEMANTIC_SCHEMA,
        "independent_group_fingerprint_ids_by_candidate_kind": {
            key: sorted(value) for key, value in independent_ids_by_kind.items()
        },
        "business_role_asserted": False,
    }


def _normalize_boundary_group_evidence(
    evidence: object,
    *,
    context: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    evidence_path: Path | None = None
    if isinstance(evidence, (str, os.PathLike)):
        evidence_path = Path(evidence).resolve()
        payload = _load_json(evidence_path, "boundary group candidates")
        evidence_sha = _sha256(evidence_path)
    elif isinstance(evidence, Mapping):
        payload = json.loads(json.dumps(evidence, ensure_ascii=False, allow_nan=False))
        evidence_sha = _canonical_sha256(payload)
    else:
        raise MarkerConfirmationError(
            "independent boundary group candidate evidence is required"
        )
    if payload.get("status") != "PASS":
        raise MarkerConfirmationError("boundary group candidate evidence is not PASS")
    if payload.get("schema_version") != _BOUNDARY_SEMANTIC_SCHEMA:
        raise MarkerConfirmationError(
            "boundary group evidence must use the boundary semantic candidate schema"
        )
    return _normalize_boundary_semantic_report(
        payload,
        context=context,
        evidence_path=evidence_path,
        evidence_sha=evidence_sha,
    )


def _boundary_source_group_contract(path: Path) -> dict[str, dict[str, str]]:
    """Read the tag-independent group/kind contract from canonical evidence."""

    payload = _load_json(path, "boundary semantic candidates")
    if (
        payload.get("schema_version") != _BOUNDARY_SEMANTIC_SCHEMA
        or payload.get("status") != "PASS"
        or payload.get("business_semantics_asserted") is not False
    ):
        raise MarkerConfirmationError(
            "boundary semantic source contract is invalid"
        )
    geometric = payload.get("geometric_candidates")
    if not isinstance(geometric, Mapping):
        raise MarkerConfirmationError(
            "boundary semantic source contract lacks geometric candidates"
        )
    collections = (
        ("farfield", "upstream_outer_groups"),
        ("wall", "void_shell_boundary_groups"),
        ("rear_outlet", "downstream_outer_groups"),
    )
    contract: dict[str, dict[str, str]] = {}
    for candidate_kind, collection_name in collections:
        groups = geometric.get(collection_name)
        if not isinstance(groups, list):
            raise MarkerConfirmationError(
                "boundary semantic source group collection is invalid"
            )
        for group in groups:
            fingerprint = group.get("fingerprint") if isinstance(group, Mapping) else None
            group_id = (
                group.get("group_fingerprint_id")
                if isinstance(group, Mapping)
                else None
            )
            if (
                not isinstance(fingerprint, Mapping)
                or not isinstance(group_id, str)
                or not _is_sha256(group_id)
                or _canonical_sha256(fingerprint) != group_id
                or group_id in contract
                or not isinstance(fingerprint.get("geometric_class"), str)
            ):
                raise MarkerConfirmationError(
                    "boundary semantic source group fingerprint is invalid"
                )
            contract[group_id] = {
                "candidate_role_kind": candidate_kind,
                "geometric_class": str(fingerprint["geometric_class"]),
            }
    if len(contract) != 4:
        raise MarkerConfirmationError(
            "boundary semantic source contract is missing complete groups"
        )
    return contract


def _review_context(marker_review_path: PathLike, *, unique: bool) -> dict[str, Any]:
    path = Path(marker_review_path).resolve()
    if not path.is_file():
        raise MarkerConfirmationError(f"marker review does not exist: {path}")
    review = _load_json(path, "marker review")
    if review.get("markers_toml_written") is not False:
        raise MarkerConfirmationError(
            "marker review must explicitly state markers_toml_written=false"
        )
    if review.get("measurement_surface_is_solver_boundary") is not False:
        raise MarkerConfirmationError(
            "measurement surface must have solver_boundary=false"
        )
    roles = _required_roles(review)
    role_reviews = review.get("roles")
    if not isinstance(role_reviews, Mapping):
        raise MarkerConfirmationError("marker review has no role evidence")
    if set(role_reviews) != {item["role"] for item in roles}:
        raise MarkerConfirmationError("marker review role evidence is incomplete")

    inputs = review.get("inputs")
    if not isinstance(inputs, Mapping):
        raise MarkerConfirmationError("marker review has no input provenance")
    project_path, project_sha = _evidence_file(path, inputs, "project")
    catalog_path, catalog_sha = _evidence_file(path, inputs, "surface_catalog")
    types_path, types_sha = _evidence_file(path, inputs, "surface_types")
    topology = inputs.get("step_topology")
    if not isinstance(topology, Mapping) or not _is_sha256(
        topology.get("source_sha256")
    ):
        raise MarkerConfirmationError("marker review lacks a valid STEP source SHA256")
    step_sha = str(topology["source_sha256"]).lower()

    project_roles, project_measurement_is_boundary = _project_roles(project_path)
    if project_roles != roles:
        raise MarkerConfirmationError(
            "marker review required roles do not match project TOML"
        )
    if project_measurement_is_boundary is not False:
        raise MarkerConfirmationError(
            "project measurement surface must have solver_boundary=false"
        )

    surfaces = _load_surface_catalog(catalog_path)
    types = _load_surface_types(
        types_path,
        {int(surface["entity_tag"]) for surface in surfaces},
        catalog_sha,
    )
    records = _build_entity_records(
        review,
        surfaces,
        types,
        step_sha256=step_sha,
        project_sha256=project_sha,
        catalog_sha256=catalog_sha,
        require_unique_fingerprints=unique,
    )
    return {
        "path": path,
        "review": review,
        "roles": roles,
        "records": records,
        "source": {
            "step_sha256": step_sha,
            "project_path": str(project_path),
            "project_sha256": project_sha,
            "surface_catalog_path": str(catalog_path),
            "surface_catalog_sha256": catalog_sha,
            "surface_types_path": str(types_path),
            "surface_types_sha256": types_sha,
            "marker_review_path": str(path),
            "marker_review_sha256": _sha256(path),
        },
    }


def _normalize_measurement_candidates(
    candidates: Sequence[Mapping[str, Any]],
    measurement_role: str,
) -> list[dict[str, Any]]:
    if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
        raise TypeError("measurement_candidates must be a sequence of mappings")
    normalized: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, item in enumerate(candidates):
        if not isinstance(item, Mapping):
            raise MarkerConfirmationError(
                f"measurement candidate {index} must be a mapping"
            )
        candidate_id = item.get("candidate_id")
        role = item.get("role")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            raise MarkerConfirmationError(
                f"measurement candidate {index} has no candidate_id"
            )
        candidate_id = candidate_id.strip()
        if candidate_id in ids:
            raise MarkerConfirmationError(
                f"measurement candidate id {candidate_id!r} is duplicated"
            )
        ids.add(candidate_id)
        if role != measurement_role:
            raise MarkerConfirmationError(
                f"measurement candidate {candidate_id!r} has the wrong role"
            )
        if item.get("solver_boundary") is not False:
            raise MarkerConfirmationError(
                f"measurement candidate {candidate_id!r} must have solver_boundary=false"
            )
        definition = item.get("definition")
        if not isinstance(definition, Mapping) or definition.get("kind") not in {
            "plane",
            "plane_loop",
        }:
            raise MarkerConfirmationError(
                f"measurement candidate {candidate_id!r} must define a plane or plane_loop"
            )
        origin = _json_number_vector(
            definition.get("origin_m"),
            length=3,
            label=f"measurement candidate {candidate_id!r} origin_m",
        )
        normal = _json_number_vector(
            definition.get("normal"),
            length=3,
            label=f"measurement candidate {candidate_id!r} normal",
        )
        norm = math.sqrt(sum(value * value for value in normal))
        if norm <= 0.0:
            raise MarkerConfirmationError(
                f"measurement candidate {candidate_id!r} has a zero normal"
            )
        source_sha = item.get("source_sha256")
        if source_sha is not None and not _is_sha256(source_sha):
            raise MarkerConfirmationError(
                f"measurement candidate {candidate_id!r} source SHA256 is invalid"
            )
        normalized_definition: dict[str, Any] = {
            "kind": str(definition["kind"]),
            "origin_m": origin,
            "normal": normal,
            "normal_magnitude": norm,
        }
        if definition["kind"] == "plane_loop":
            loop_fingerprint = definition.get("boundary_loop_fingerprint")
            if not isinstance(loop_fingerprint, Mapping):
                raise MarkerConfirmationError(
                    f"measurement candidate {candidate_id!r} lacks a boundary loop fingerprint"
                )
            fingerprint_payload = loop_fingerprint.get("payload")
            fingerprint_sha = loop_fingerprint.get("sha256")
            if not isinstance(fingerprint_payload, Mapping) or not _is_sha256(
                fingerprint_sha
            ):
                raise MarkerConfirmationError(
                    f"measurement candidate {candidate_id!r} loop fingerprint is invalid"
                )
            if _canonical_sha256(fingerprint_payload) != str(fingerprint_sha).lower():
                raise MarkerConfirmationError(
                    f"measurement candidate {candidate_id!r} loop fingerprint SHA256 mismatch"
                )
            normalized_definition["boundary_loop_fingerprint"] = {
                "payload": json.loads(json.dumps(fingerprint_payload)),
                "sha256": str(fingerprint_sha).lower(),
            }
            for optional_field in ("geometry_status", "role_status"):
                optional_value = definition.get(optional_field)
                if optional_value is not None:
                    if not isinstance(optional_value, str) or not optional_value:
                        raise MarkerConfirmationError(
                            f"measurement candidate {candidate_id!r} {optional_field} is invalid"
                        )
                    normalized_definition[optional_field] = optional_value

        canonical = {
            "candidate_id": candidate_id,
            "role": measurement_role,
            "solver_boundary": False,
            "definition": normalized_definition,
            "source_sha256": str(source_sha).lower() if source_sha else None,
        }
        candidate_sha = _canonical_sha256(canonical)
        declared_candidate_sha = item.get("candidate_sha256")
        if declared_candidate_sha is not None and (
            not _is_sha256(declared_candidate_sha)
            or str(declared_candidate_sha).lower() != candidate_sha
        ):
            raise MarkerConfirmationError(
                f"measurement candidate {candidate_id!r} SHA256 mismatch"
            )
        canonical["candidate_sha256"] = candidate_sha
        normalized.append(canonical)
    return normalized


def _measurement_candidates_from_discovery(
    report: Mapping[str, Any],
    *,
    measurement_role: str,
    expected_step_sha256: str,
) -> list[dict[str, Any]]:
    """Convert measurement-surface discovery evidence into role candidates.

    Discovery may confirm geometry, but it deliberately does not assert the
    project/business role.  This adapter preserves that distinction and only
    creates candidates for later human confirmation.
    """

    if report.get("status") != "PASS" or report.get("source_unchanged") is not True:
        raise MarkerConfirmationError(
            "measurement discovery must be PASS with source_unchanged=true"
        )
    before = report.get("source_sha256_before")
    after = report.get("source_sha256_after")
    if (
        not _is_sha256(before)
        or not _is_sha256(after)
        or str(before).lower() != str(after).lower()
        or str(before).lower() != expected_step_sha256.lower()
    ):
        raise MarkerConfirmationError(
            "measurement discovery STEP SHA256 does not match marker review"
        )
    classification = report.get("classification")
    loops = report.get("loops")
    if not isinstance(classification, Mapping) or not isinstance(loops, list):
        raise MarkerConfirmationError(
            "measurement discovery lacks classification or loop evidence"
        )
    if classification.get("business_role_asserted") is not False:
        raise MarkerConfirmationError(
            "measurement discovery must not assert a business role"
        )
    transitions = classification.get("transition_candidates")
    if not isinstance(transitions, list):
        raise MarkerConfirmationError(
            "measurement discovery transition candidates are invalid"
        )
    candidate_hashes: set[str] = set()
    for item in transitions:
        if not isinstance(item, Mapping) or not _is_sha256(
            item.get("candidate_fingerprint_sha256")
        ):
            raise MarkerConfirmationError(
                "measurement discovery contains an invalid transition candidate"
            )
        candidate_hashes.add(
            str(item["candidate_fingerprint_sha256"]).lower()
        )
    selected_hash = classification.get("selected_fingerprint_sha256")
    if selected_hash is not None:
        if not _is_sha256(selected_hash):
            raise MarkerConfirmationError(
                "measurement discovery selected fingerprint SHA256 is invalid"
            )
        candidate_hashes.add(str(selected_hash).lower())

    by_hash: dict[str, Mapping[str, Any]] = {}
    for loop in loops:
        if not isinstance(loop, Mapping):
            raise MarkerConfirmationError("measurement discovery has an invalid loop")
        fingerprint = loop.get("fingerprint")
        if not isinstance(fingerprint, Mapping) or not _is_sha256(
            fingerprint.get("sha256")
        ):
            raise MarkerConfirmationError(
                "measurement discovery loop lacks a valid fingerprint"
            )
        fingerprint_sha = str(fingerprint["sha256"]).lower()
        if fingerprint_sha in by_hash:
            raise MarkerConfirmationError(
                "measurement discovery has duplicate loop fingerprints"
            )
        by_hash[fingerprint_sha] = loop

    candidates: list[dict[str, Any]] = []
    for fingerprint_sha in sorted(candidate_hashes):
        loop = by_hash.get(fingerprint_sha)
        if loop is None:
            raise MarkerConfirmationError(
                "measurement transition candidate has zero matching loops"
            )
        fingerprint = loop["fingerprint"]
        payload = fingerprint.get("payload")
        if not isinstance(payload, Mapping) or _canonical_sha256(payload) != fingerprint_sha:
            raise MarkerConfirmationError(
                "measurement loop fingerprint content does not match its SHA256"
            )
        candidates.append(
            {
                "candidate_id": f"measurement_loop_{fingerprint_sha[:16]}",
                "role": measurement_role,
                "solver_boundary": False,
                "definition": {
                    "kind": "plane_loop",
                    "origin_m": loop.get("centroid_m"),
                    "normal": loop.get("normal_unit"),
                    "boundary_loop_fingerprint": fingerprint,
                    "geometry_status": loop.get("geometry_status"),
                    "role_status": loop.get("role_status"),
                },
                "source_sha256": str(before).lower(),
            }
        )
    return _normalize_measurement_candidates(candidates, measurement_role)


def _prepare_measurement_candidates(
    candidates: object,
    *,
    measurement_role: str,
    expected_step_sha256: str,
) -> list[dict[str, Any]]:
    if isinstance(candidates, (str, os.PathLike)):
        candidates = _load_json(
            Path(candidates).resolve(), "measurement candidate evidence"
        )
    if isinstance(candidates, Mapping):
        if "classification" in candidates or "loops" in candidates:
            return _measurement_candidates_from_discovery(
                candidates,
                measurement_role=measurement_role,
                expected_step_sha256=expected_step_sha256,
            )
        candidates = [candidates]
    if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
        raise TypeError(
            "measurement_candidates must be discovery evidence or a sequence"
        )
    return _normalize_measurement_candidates(candidates, measurement_role)


def _output_json_path(output_path: PathLike) -> Path:
    path = Path(output_path).resolve()
    if path.suffix.casefold() != ".json":
        raise MarkerConfirmationError("confirmation template output must be a .json file")
    if any(part.casefold() == "config" for part in path.parts):
        raise MarkerConfirmationError(
            "confirmation templates must not be written under a config directory"
        )
    if path.name.casefold() == "markers.json":
        raise MarkerConfirmationError("refusing a production-like marker filename")
    return path


def _atomic_write_new(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise MarkerConfirmationError(f"refusing to overwrite existing file: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary.exists():
        raise MarkerConfirmationError(f"temporary output already exists: {temporary}")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        if path.exists():
            raise MarkerConfirmationError(f"output appeared during write: {path}")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def build_marker_confirmation_template(
    marker_review_path: PathLike,
    measurement_candidates: object,
    output_path: PathLike,
    *,
    boundary_group_candidates: object = None,
) -> dict[str, Any]:
    """Create a non-production JSON template for explicit human decisions.

    The returned mapping is identical to the file payload.  The output path is
    required to be outside every directory named ``config`` and an existing
    file is never overwritten.
    """

    context = _review_context(marker_review_path, unique=True)
    roles = context["roles"]
    measurement_roles = [
        item["role"] for item in roles if item["kind"] == "measurement_surface"
    ]
    if len(measurement_roles) != 1:
        raise MarkerConfirmationError(
            "exactly one measurement_surface role is required"
        )
    measurement_role = measurement_roles[0]
    normalized_measurements = _prepare_measurement_candidates(
        measurement_candidates,
        measurement_role=measurement_role,
        expected_step_sha256=context["source"]["step_sha256"],
    )
    normalized_groups, group_evidence = _normalize_boundary_group_evidence(
        boundary_group_candidates,
        context=context,
    )
    group_ids_by_kind: dict[str, list[str]] = {
        "farfield": [],
        "wall": [],
        "rear_outlet": [],
    }
    for group in normalized_groups:
        candidate_kind = str(group["candidate_role_kind"])
        if candidate_kind not in group_ids_by_kind:
            raise MarkerConfirmationError(
                f"unsupported boundary group candidate kind {candidate_kind!r}"
            )
        group_ids_by_kind[candidate_kind].append(group["group_fingerprint_id"])
    confirmations: dict[str, dict[str, Any]] = {}
    for required in roles:
        role = required["role"]
        kind = required["kind"]
        confirmations[role] = {
            "kind": kind,
            "status": "PENDING",
            "solver_boundary": kind != "measurement_surface",
            "candidate_fingerprint_ids": [],
            "selected_fingerprint_ids": [],
            "candidate_group_fingerprint_ids": (
                list(group_ids_by_kind[kind])
                if kind in group_ids_by_kind
                else []
            ),
            "selected_group_fingerprint_ids": [],
            "candidate_measurement_ids": (
                [item["candidate_id"] for item in normalized_measurements]
                if kind == "measurement_surface"
                else []
            ),
            "selected_measurement_candidate_id": None,
            "confirmed_by": "",
            "decision_basis": "",
        }

    document: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "document_kind": _DOCUMENT_KIND,
        "status": "PENDING_HUMAN_CONFIRMATION",
        "markers_toml_written": False,
        "source": context["source"],
        "required_roles": roles,
        "matching_tolerances": dict(_DEFAULT_TOLERANCES),
        "entities": context["records"],
        "boundary_group_evidence": group_evidence,
        "boundary_group_candidates": normalized_groups,
        "measurement_candidates": normalized_measurements,
        "confirmations": confirmations,
        "rear_outlet_group_assignment": {
            "status": "PENDING",
            "role_to_group_fingerprint": {
                item["role"]: None
                for item in roles
                if item["kind"] == "rear_outlet"
            },
            "confirmed_by": "",
            "decision_basis": "",
        },
        "instructions": [
            "Select complete group fingerprints for every solver boundary role.",
            "Do not select individual surfaces for any solver boundary role.",
            "Set every role status to CONFIRMED and record the human and basis.",
            "Select one explicit non-solver-boundary measurement candidate.",
            "Map both rear roles one-to-one onto the two complete surface groups.",
            "Do not infer rear numbering from Z sign or a single surface centroid.",
            "Set top-level status to CONFIRMED_BY_HUMAN before validation.",
            "This document is not config/markers.toml and cannot authorize meshing.",
        ],
    }
    destination = _output_json_path(output_path)
    _atomic_write_new(
        destination,
        json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
    )
    return document


def _load_confirmation(value: PathLike | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, Mapping):
        try:
            return json.loads(
                json.dumps(value, ensure_ascii=False, allow_nan=False)
            )
        except (TypeError, ValueError) as error:
            raise MarkerConfirmationError(
                f"confirmation mapping is not valid JSON data: {error}"
            ) from error
    return _load_json(Path(value).resolve(), "marker confirmation")


def _validated_tolerances(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise MarkerConfirmationError("matching_tolerances must be a mapping")
    if set(value) != set(_DEFAULT_TOLERANCES):
        raise MarkerConfirmationError("matching_tolerances has unexpected keys")
    tolerances = {
        key: _finite_float(value[key], f"matching tolerance {key}")
        for key in _DEFAULT_TOLERANCES
    }
    if any(number < 0.0 for number in tolerances.values()):
        raise MarkerConfirmationError("matching tolerances cannot be negative")
    return tolerances


def _close_scalar(
    first: float,
    second: float,
    *,
    absolute: float,
    relative: float = 0.0,
) -> bool:
    return abs(first - second) <= max(
        absolute, relative * max(abs(first), abs(second))
    )


def _fingerprint_matches(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    tolerances: Mapping[str, float],
) -> bool:
    exact_fields = (
        "source_step_sha256",
        "source_project_sha256",
        "solid_shell",
        "entity_type",
        "boundary_loop_signature",
    )
    if any(expected.get(field) != actual.get(field) for field in exact_fields):
        return False
    try:
        expected_area = _finite_float(expected.get("area_m2"), "fingerprint area")
        actual_area = _finite_float(actual.get("area_m2"), "catalog area")
        expected_centroid = _json_number_vector(
            expected.get("centroid_m"), length=3, label="fingerprint centroid"
        )
        actual_centroid = _json_number_vector(
            actual.get("centroid_m"), length=3, label="catalog centroid"
        )
        expected_bounds = _json_number_vector(
            expected.get("bounding_box_m"), length=6, label="fingerprint bounds"
        )
        actual_bounds = _json_number_vector(
            actual.get("bounding_box_m"), length=6, label="catalog bounds"
        )
    except MarkerConfirmationError:
        return False
    if not _close_scalar(
        expected_area,
        actual_area,
        absolute=tolerances["area_absolute_m2"],
        relative=tolerances["area_relative"],
    ):
        return False
    coordinate_tolerance = tolerances["coordinate_absolute_m"]
    return all(
        _close_scalar(first, second, absolute=coordinate_tolerance)
        for first, second in zip(expected_centroid, actual_centroid)
    ) and all(
        _close_scalar(first, second, absolute=coordinate_tolerance)
        for first, second in zip(expected_bounds, actual_bounds)
    )


def match_surface_fingerprint(
    fingerprint: Mapping[str, Any],
    current_entities: Sequence[Mapping[str, Any]],
    matching_tolerances: Mapping[str, float] | None = None,
) -> Mapping[str, Any]:
    """Return the sole current entity matching ``fingerprint``.

    Zero and multiple matches are both errors.  Gmsh tags are intentionally
    absent from the comparison and are returned only as audit evidence.
    """

    if not isinstance(fingerprint, Mapping):
        raise TypeError("fingerprint must be a mapping")
    tolerances = _validated_tolerances(
        matching_tolerances or _DEFAULT_TOLERANCES
    )
    matches: list[Mapping[str, Any]] = []
    for entity in current_entities:
        if not isinstance(entity, Mapping) or not isinstance(
            entity.get("fingerprint"), Mapping
        ):
            raise MarkerConfirmationError("current entity record is invalid")
        if _fingerprint_matches(
            fingerprint, entity["fingerprint"], tolerances
        ):
            matches.append(entity)
    if not matches:
        raise MarkerConfirmationError("surface fingerprint has zero matches")
    if len(matches) != 1:
        raise MarkerConfirmationError(
            f"surface fingerprint has multiple matches ({len(matches)})"
        )
    return matches[0]


def _group_geometry_matches(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    tolerances: Mapping[str, float],
) -> bool:
    if expected.get("entity_type_counts") != actual.get("entity_type_counts"):
        return False
    if expected.get("solid_shell_member_counts") != actual.get(
        "solid_shell_member_counts"
    ):
        return False
    try:
        expected_total = _finite_float(
            expected.get("total_area_m2"), "expected group total area"
        )
        actual_total = _finite_float(
            actual.get("total_area_m2"), "actual group total area"
        )
        expected_areas = _json_number_vector(
            expected.get("member_area_spectrum_m2"),
            length=len(actual.get("member_area_spectrum_m2", [])),
            label="expected group area spectrum",
        )
        actual_areas = _json_number_vector(
            actual.get("member_area_spectrum_m2"),
            length=len(expected_areas),
            label="actual group area spectrum",
        )
        expected_centroid = _json_number_vector(
            expected.get("area_weighted_centroid_m"),
            length=3,
            label="expected group centroid",
        )
        actual_centroid = _json_number_vector(
            actual.get("area_weighted_centroid_m"),
            length=3,
            label="actual group centroid",
        )
        expected_bounds = _json_number_vector(
            expected.get("bounding_box_m"),
            length=6,
            label="expected group bounds",
        )
        actual_bounds = _json_number_vector(
            actual.get("bounding_box_m"),
            length=6,
            label="actual group bounds",
        )
    except MarkerConfirmationError:
        return False
    if not _close_scalar(
        expected_total,
        actual_total,
        absolute=tolerances["area_absolute_m2"],
        relative=tolerances["area_relative"],
    ):
        return False
    if not all(
        _close_scalar(
            first,
            second,
            absolute=tolerances["area_absolute_m2"],
            relative=tolerances["area_relative"],
        )
        for first, second in zip(expected_areas, actual_areas)
    ):
        return False
    coordinate_tolerance = tolerances["coordinate_absolute_m"]
    return all(
        _close_scalar(first, second, absolute=coordinate_tolerance)
        for first, second in zip(expected_centroid, actual_centroid)
    ) and all(
        _close_scalar(first, second, absolute=coordinate_tolerance)
        for first, second in zip(expected_bounds, actual_bounds)
    )


def _validate_boundary_group_candidates(
    raw_groups: object,
    *,
    current_entities: Sequence[Mapping[str, Any]],
    expected_step_sha256: str,
    tolerances: Mapping[str, float],
) -> dict[str, dict[str, Any]]:
    expected_counts = {"farfield": 1, "wall": 1, "rear_outlet": 2}
    if not isinstance(raw_groups, list) or len(raw_groups) != sum(
        expected_counts.values()
    ):
        raise MarkerConfirmationError(
            "confirmation requires complete farfield, wall and rear boundary groups"
        )
    resolved: dict[str, dict[str, Any]] = {}
    used_tags: set[int] = set()
    kind_counts = {key: 0 for key in expected_counts}
    for index, raw in enumerate(raw_groups):
        if not isinstance(raw, Mapping):
            raise MarkerConfirmationError(
                f"boundary group candidate {index} is invalid"
            )
        candidate_kind = raw.get("candidate_role_kind")
        if (
            raw.get("kind") != "solver_boundary_surface_group"
            or candidate_kind not in expected_counts
        ):
            raise MarkerConfirmationError("boundary group candidate kind is invalid")
        kind_counts[str(candidate_kind)] += 1
        if raw.get("business_role_asserted") is not False:
            raise MarkerConfirmationError(
                "boundary group candidate must not pre-assign a numbered role"
            )
        completeness = raw.get("completeness")
        if not isinstance(completeness, Mapping) or completeness.get("status") != "COMPLETE":
            raise MarkerConfirmationError("boundary group candidate is incomplete")
        group_id = raw.get("group_fingerprint_id")
        fingerprint = raw.get("group_fingerprint")
        if (
            not isinstance(group_id, str)
            or not _is_sha256(group_id)
            or not isinstance(fingerprint, Mapping)
            or _canonical_sha256(fingerprint) != group_id
        ):
            raise MarkerConfirmationError(
                "boundary group fingerprint does not match its content"
            )
        if group_id in resolved:
            raise MarkerConfirmationError("boundary group fingerprint is duplicated")
        if fingerprint.get("source_step_sha256") != expected_step_sha256:
            raise MarkerConfirmationError("boundary group STEP SHA256 is stale")
        members = fingerprint.get("members")
        member_count = fingerprint.get("member_count")
        if (
            not isinstance(members, list)
            or len(members) < 2
            or isinstance(member_count, bool)
            or not isinstance(member_count, int)
            or member_count != len(members)
            or completeness.get("expected_member_count") != len(members)
        ):
            raise MarkerConfirmationError(
                "boundary group fingerprint has incomplete membership"
            )
        matched: list[Mapping[str, Any]] = []
        expected_member_ids: set[str] = set()
        for member in members:
            if not isinstance(member, Mapping) or not isinstance(
                member.get("fingerprint"), Mapping
            ):
                raise MarkerConfirmationError(
                    "boundary group contains an invalid member fingerprint"
                )
            member_id = member.get("fingerprint_id")
            if (
                not isinstance(member_id, str)
                or _canonical_sha256(member["fingerprint"]) != member_id
                or member_id in expected_member_ids
            ):
                raise MarkerConfirmationError(
                    "boundary group member fingerprint is invalid or duplicated"
                )
            expected_member_ids.add(member_id)
            matched.append(
                match_surface_fingerprint(
                    member["fingerprint"], current_entities, tolerances
                )
            )
        current_tags = {
            int(item["audit"]["gmsh_surface_entity_tag"]) for item in matched
        }
        if len(current_tags) != len(matched):
            raise MarkerConfirmationError(
                "boundary group members do not resolve to distinct current surfaces"
            )
        overlap = sorted(used_tags & current_tags)
        if overlap:
            raise MarkerConfirmationError(
                f"boundary group candidates overlap on current surfaces {overlap}"
            )
        used_tags.update(current_tags)
        current_fingerprint = _build_group_fingerprint(
            matched, current_entities
        )
        if fingerprint.get("topology") != current_fingerprint.get("topology"):
            raise MarkerConfirmationError(
                "boundary group topology fingerprint has zero matches"
            )
        if not _group_geometry_matches(
            fingerprint.get("geometry", {}),
            current_fingerprint.get("geometry", {}),
            tolerances,
        ):
            raise MarkerConfirmationError(
                "boundary group geometry fingerprint has zero matches"
            )
        resolved[group_id] = {
            "candidate_id": raw.get("candidate_id"),
            "candidate_role_kind": candidate_kind,
            "group_fingerprint_id": group_id,
            "group_fingerprint": fingerprint,
            "matched_entities": matched,
            "current_gmsh_surface_entity_tags_audit": sorted(current_tags),
            "completeness": dict(completeness),
        }
    if kind_counts != expected_counts:
        raise MarkerConfirmationError(
            "boundary group candidate role-kind counts are incomplete"
        )
    return resolved


def validate_marker_confirmation(
    confirmation: PathLike | Mapping[str, Any],
    current_marker_review_path: PathLike,
) -> dict[str, Any]:
    """Validate a completed human document against current marker evidence.

    The function returns an audit report and performs no writes.  It does not
    create ``markers.toml`` or authorize a downstream tool invocation.
    """

    document = _load_confirmation(confirmation)
    if document.get("schema_version") != _SCHEMA_VERSION:
        raise MarkerConfirmationError("unsupported confirmation schema_version")
    if document.get("document_kind") != _DOCUMENT_KIND:
        raise MarkerConfirmationError("unexpected confirmation document_kind")
    if document.get("markers_toml_written") is not False:
        raise MarkerConfirmationError(
            "confirmation must state markers_toml_written=false"
        )
    if document.get("status") != "CONFIRMED_BY_HUMAN":
        raise MarkerConfirmationError(
            "confirmation status must be CONFIRMED_BY_HUMAN"
        )

    current = _review_context(current_marker_review_path, unique=False)
    source = document.get("source")
    if not isinstance(source, Mapping):
        raise MarkerConfirmationError("confirmation has no source provenance")
    for field in ("step_sha256", "project_sha256"):
        if source.get(field) != current["source"].get(field):
            raise MarkerConfirmationError(f"confirmation {field} is stale")

    required = current["roles"]
    required_by_name = {item["role"]: item for item in required}
    embedded_required = document.get("required_roles")
    if embedded_required != required:
        raise MarkerConfirmationError("confirmation required_roles are stale")
    confirmations = document.get("confirmations")
    if not isinstance(confirmations, Mapping):
        raise MarkerConfirmationError("confirmation has no role decisions")
    actual_names = set(confirmations)
    required_names = set(required_by_name)
    if actual_names != required_names:
        missing = sorted(required_names - actual_names)
        extra = sorted(actual_names - required_names)
        raise MarkerConfirmationError(
            f"confirmation roles mismatch; missing required roles={missing}, extra={extra}"
        )

    tolerances = _validated_tolerances(document.get("matching_tolerances"))
    raw_entities = document.get("entities")
    if not isinstance(raw_entities, list) or not raw_entities:
        raise MarkerConfirmationError("confirmation contains no fingerprint catalogue")
    embedded_by_id: dict[str, Mapping[str, Any]] = {}
    for item in raw_entities:
        if not isinstance(item, Mapping) or not isinstance(
            item.get("fingerprint"), Mapping
        ):
            raise MarkerConfirmationError("confirmation contains an invalid entity")
        fingerprint_id = item.get("fingerprint_id")
        if not isinstance(fingerprint_id, str) or not _is_sha256(fingerprint_id):
            raise MarkerConfirmationError("confirmation has an invalid fingerprint id")
        if _canonical_sha256(item["fingerprint"]) != fingerprint_id:
            raise MarkerConfirmationError(
                f"fingerprint {fingerprint_id!r} does not match its content"
            )
        if fingerprint_id in embedded_by_id:
            raise MarkerConfirmationError(
                f"confirmation repeats fingerprint id {fingerprint_id}"
            )
        embedded_by_id[fingerprint_id] = item

    measurement_roles = [
        item["role"] for item in required if item["kind"] == "measurement_surface"
    ]
    measurement_role = measurement_roles[0]
    raw_measurements = document.get("measurement_candidates")
    if not isinstance(raw_measurements, list):
        raise MarkerConfirmationError("measurement_candidates must be a list")
    measurements = _normalize_measurement_candidates(
        raw_measurements, measurement_role
    )
    measurement_by_id = {item["candidate_id"]: item for item in measurements}
    group_evidence = document.get("boundary_group_evidence")
    if (
        not isinstance(group_evidence, Mapping)
        or group_evidence.get("evidence_kind")
        != "independent_boundary_group_candidates"
        or group_evidence.get("source_schema_version")
        != _BOUNDARY_SEMANTIC_SCHEMA
        or group_evidence.get("business_role_asserted") is not False
        or group_evidence.get("source_step_sha256")
        != current["source"]["step_sha256"]
        or group_evidence.get("source_project_sha256")
        != current["source"]["project_sha256"]
        or not _is_sha256(group_evidence.get("sha256"))
    ):
        raise MarkerConfirmationError(
            "independent boundary group provenance is missing or stale"
        )
    group_evidence_path = group_evidence.get("path")
    if group_evidence_path is not None:
        if not isinstance(group_evidence_path, str) or not group_evidence_path:
            raise MarkerConfirmationError(
                "independent boundary group evidence path is invalid"
            )
        current_group_evidence_path = Path(group_evidence_path).resolve()
        if (
            not current_group_evidence_path.is_file()
            or _sha256(current_group_evidence_path) != group_evidence["sha256"]
        ):
            raise MarkerConfirmationError(
                "independent boundary group evidence file is missing or stale"
            )
        source_group_contract = _boundary_source_group_contract(
            current_group_evidence_path
        )
        embedded_groups = document.get("boundary_group_candidates")
        if not isinstance(embedded_groups, list):
            raise MarkerConfirmationError(
                "confirmation lacks embedded boundary group candidates"
            )
        seen_independent_ids: set[str] = set()
        for group in embedded_groups:
            audit = group.get("audit") if isinstance(group, Mapping) else None
            independent_id = (
                audit.get("independent_group_fingerprint_id")
                if isinstance(audit, Mapping)
                else None
            )
            expected = source_group_contract.get(str(independent_id))
            if (
                not isinstance(group, Mapping)
                or expected is None
                or group.get("candidate_role_kind")
                != expected["candidate_role_kind"]
                or group.get("geometric_class") != expected["geometric_class"]
                or independent_id in seen_independent_ids
            ):
                raise MarkerConfirmationError(
                    "embedded boundary group classification differs from canonical evidence"
                )
            seen_independent_ids.add(str(independent_id))
        if seen_independent_ids != set(source_group_contract):
            raise MarkerConfirmationError(
                "embedded boundary groups do not match canonical evidence"
            )
    resolved_group_candidates = _validate_boundary_group_candidates(
        document.get("boundary_group_candidates"),
        current_entities=current["records"],
        expected_step_sha256=current["source"]["step_sha256"],
        tolerances=tolerances,
    )

    group_ids_by_kind: dict[str, set[str]] = {
        "farfield": set(),
        "wall": set(),
        "rear_outlet": set(),
    }
    for group_id, group in resolved_group_candidates.items():
        candidate_kind = group.get("candidate_role_kind")
        if candidate_kind not in group_ids_by_kind:
            raise MarkerConfirmationError(
                "resolved boundary group has an unsupported candidate kind"
            )
        group_ids_by_kind[str(candidate_kind)].add(group_id)
        for member in group["group_fingerprint"]["members"]:
            member_id = member["fingerprint_id"]
            embedded = embedded_by_id.get(member_id)
            if embedded is None or embedded.get("fingerprint") != member.get(
                "fingerprint"
            ):
                raise MarkerConfirmationError(
                    "boundary group membership is inconsistent with the entity catalogue"
                )
    assigned_surfaces: dict[int, str] = {}
    resolved_roles: dict[str, dict[str, Any]] = {}
    for role, required_item in required_by_name.items():
        decision = confirmations[role]
        if not isinstance(decision, Mapping):
            raise MarkerConfirmationError(f"role {role!r} decision must be a mapping")
        if decision.get("kind") != required_item["kind"]:
            raise MarkerConfirmationError(f"role {role!r} kind is stale")
        if decision.get("status") != "CONFIRMED":
            raise MarkerConfirmationError(f"role {role!r} is not CONFIRMED")
        for field in ("confirmed_by", "decision_basis"):
            value = decision.get(field)
            if not isinstance(value, str) or not value.strip():
                raise MarkerConfirmationError(
                    f"role {role!r} requires nonempty {field}"
                )

        kind = required_item["kind"]
        if kind == "measurement_surface":
            if decision.get("solver_boundary") is not False:
                raise MarkerConfirmationError(
                    "measurement role must have solver_boundary=false"
                )
            selected_surfaces = decision.get("selected_fingerprint_ids")
            if selected_surfaces not in ([], None):
                raise MarkerConfirmationError(
                    "measurement role cannot select a solver boundary surface"
                )
            if decision.get("selected_group_fingerprint_ids") not in ([], None):
                raise MarkerConfirmationError(
                    "measurement role cannot select a solver boundary group"
                )
            candidate_id = decision.get("selected_measurement_candidate_id")
            if not isinstance(candidate_id, str) or candidate_id not in measurement_by_id:
                raise MarkerConfirmationError(
                    "measurement selection has zero matching candidates"
                )
            candidate = measurement_by_id[candidate_id]
            if candidate["solver_boundary"] is not False:
                raise MarkerConfirmationError(
                    "measurement candidate must have solver_boundary=false"
                )
            resolved_roles[role] = {
                "kind": kind,
                "solver_boundary": False,
                "measurement_candidate": candidate,
                "confirmed_by": decision["confirmed_by"].strip(),
                "decision_basis": decision["decision_basis"].strip(),
            }
            continue

        if decision.get("solver_boundary") is not True:
            raise MarkerConfirmationError(
                f"solver marker role {role!r} must have solver_boundary=true"
            )
        if decision.get("selected_fingerprint_ids") not in ([], None):
            raise MarkerConfirmationError(
                "solver boundary roles must select complete groups, not individual surfaces"
            )
        if decision.get("candidate_fingerprint_ids") not in ([], None):
            raise MarkerConfirmationError(
                "legacy marker-review surface candidates must not drive solver roles"
            )
        expected_group_ids = group_ids_by_kind.get(kind)
        candidate_group_ids = decision.get("candidate_group_fingerprint_ids")
        if (
            expected_group_ids is None
            or not isinstance(candidate_group_ids, list)
            or set(candidate_group_ids) != expected_group_ids
            or len(candidate_group_ids) != len(expected_group_ids)
        ):
            raise MarkerConfirmationError(
                f"solver role {role!r} has stale complete-group candidates"
            )
        selected_group_ids = decision.get("selected_group_fingerprint_ids")
        if kind == "rear_outlet":
            if selected_group_ids not in ([], None):
                raise MarkerConfirmationError(
                    "numbered rear roles must use the explicit one-to-one group assignment"
                )
            continue
        if (
            not isinstance(selected_group_ids, list)
            or len(selected_group_ids) != 1
            or selected_group_ids[0] not in expected_group_ids
        ):
            raise MarkerConfirmationError(
                f"solver role {role!r} must select exactly one complete group"
            )
        group_id = str(selected_group_ids[0])
        group = resolved_group_candidates[group_id]
        matched = group["matched_entities"]
        for entity in matched:
            tag = int(entity["audit"]["gmsh_surface_entity_tag"])
            previous = assigned_surfaces.get(tag)
            if previous is not None:
                raise MarkerConfirmationError(
                    f"surface {tag} is assigned to multiple roles: {previous!r}, {role!r}"
                )
            assigned_surfaces[tag] = role
        resolved_roles[role] = {
            "kind": kind,
            "solver_boundary": True,
            "group_fingerprint_id": group_id,
            "candidate_id": group["candidate_id"],
            "complete_member_count": len(matched),
            "matched_entities": [
                {
                    "fingerprint_id": item["fingerprint_id"],
                    "solid_shell": item["fingerprint"]["solid_shell"],
                    "audit": item["audit"],
                }
                for item in matched
            ],
            "group_topology": group["group_fingerprint"]["topology"],
            "group_geometry": group["group_fingerprint"]["geometry"],
            "confirmed_by": decision["confirmed_by"].strip(),
            "decision_basis": decision["decision_basis"].strip(),
        }

    rear_roles = [
        item["role"] for item in required if item["kind"] == "rear_outlet"
    ]
    if len(rear_roles) != 2:
        raise MarkerConfirmationError("exactly two rear_outlet roles are required")
    assignment = document.get("rear_outlet_group_assignment")
    if not isinstance(assignment, Mapping) or assignment.get("status") != "CONFIRMED":
        raise MarkerConfirmationError(
            "rear outlet surface-group assignment is not CONFIRMED"
        )
    role_to_group = assignment.get("role_to_group_fingerprint")
    if not isinstance(role_to_group, Mapping) or set(role_to_group) != set(rear_roles):
        raise MarkerConfirmationError(
            "rear group assignment must contain both rear outlet roles"
        )
    rear_group_ids = group_ids_by_kind["rear_outlet"]
    assigned_group_ids = list(role_to_group.values())
    if (
        any(
            not isinstance(group_id, str)
            or group_id not in rear_group_ids
            for group_id in assigned_group_ids
        )
        or len(set(assigned_group_ids)) != len(rear_roles)
        or set(assigned_group_ids) != rear_group_ids
    ):
        raise MarkerConfirmationError(
            "rear roles must map one-to-one onto both complete boundary groups"
        )
    for field in ("confirmed_by", "decision_basis"):
        value = assignment.get(field)
        if not isinstance(value, str) or not value.strip():
            raise MarkerConfirmationError(
                f"rear outlet group assignment requires nonempty {field}"
            )
    for role in rear_roles:
        group_id = str(role_to_group[role])
        group = resolved_group_candidates[group_id]
        matched_entities = group["matched_entities"]
        for entity in matched_entities:
            tag = int(entity["audit"]["gmsh_surface_entity_tag"])
            previous = assigned_surfaces.get(tag)
            if previous is not None:
                raise MarkerConfirmationError(
                    f"surface {tag} is assigned to multiple roles: {previous!r}, {role!r}"
                )
            assigned_surfaces[tag] = role
        decision = confirmations[role]
        resolved_roles[role] = {
            "kind": "rear_outlet",
            "solver_boundary": True,
            "group_fingerprint_id": group_id,
            "candidate_id": group["candidate_id"],
            "complete_member_count": len(matched_entities),
            "matched_entities": [
                {
                    "fingerprint_id": item["fingerprint_id"],
                    "solid_shell": item["fingerprint"]["solid_shell"],
                    "audit": item["audit"],
                }
                for item in matched_entities
            ],
            "group_topology": group["group_fingerprint"]["topology"],
            "group_geometry": group["group_fingerprint"]["geometry"],
            "confirmed_by": decision["confirmed_by"].strip(),
            "decision_basis": decision["decision_basis"].strip(),
        }

    human_confirmation = document.get("human_confirmation")
    validated_human_confirmation: dict[str, Any] | None = None
    if human_confirmation is not None:
        expected_human_fields = {
            "schema_version",
            "status",
            "confirmed_by",
            "confirmed_at_utc",
            "decision_basis",
            "original_confirmation_summary",
            "user_decision_summary",
            "template",
            "source",
            "boundary_group_evidence",
            "stable_group_fingerprint_ids_by_role",
            "measurement_selection",
            "audit",
        }
        if (
            not isinstance(human_confirmation, Mapping)
            or set(human_confirmation) != expected_human_fields
            or human_confirmation.get("schema_version") != 1
            or human_confirmation.get("status") != "CONFIRMED_BY_HUMAN"
        ):
            raise MarkerConfirmationError(
                "human_confirmation metadata schema is invalid"
            )
        human_confirmed_by = human_confirmation.get("confirmed_by")
        human_basis = human_confirmation.get("decision_basis")
        original_summary = human_confirmation.get("original_confirmation_summary")
        user_summary = human_confirmation.get("user_decision_summary")
        if (
            not isinstance(human_confirmed_by, str)
            or not human_confirmed_by.strip()
            or not isinstance(human_basis, str)
            or not human_basis.strip()
            or not isinstance(original_summary, str)
            or not original_summary.strip()
            or not isinstance(user_summary, str)
            or not user_summary.strip()
            or human_basis.strip() != user_summary.strip()
            or original_summary.strip() != user_summary.strip()
        ):
            raise MarkerConfirmationError(
                "human_confirmation identity or decision summary is invalid"
            )
        raw_timestamp = human_confirmation.get("confirmed_at_utc")
        if (
            not isinstance(raw_timestamp, str)
            or _confirmation_timestamp(raw_timestamp) != raw_timestamp
        ):
            raise MarkerConfirmationError(
                "human_confirmation timestamp is not canonical UTC"
            )
        template_provenance = human_confirmation.get("template")
        if (
            not isinstance(template_provenance, Mapping)
            or set(template_provenance) != {"path", "sha256"}
            or not _is_sha256(template_provenance.get("sha256"))
            or template_provenance.get("path") is not None
            and (
                not isinstance(template_provenance.get("path"), str)
                or not Path(str(template_provenance["path"])).is_absolute()
            )
        ):
            raise MarkerConfirmationError(
                "human_confirmation template provenance is invalid"
            )
        template_source_path = template_provenance.get("path")
        if template_source_path is not None:
            template_source = Path(str(template_source_path)).resolve()
            if (
                not template_source.is_file()
                or _sha256(template_source) != template_provenance["sha256"]
            ):
                raise MarkerConfirmationError(
                    "human_confirmation template source is missing or stale"
                )
        source_provenance = human_confirmation.get("source")
        if (
            not isinstance(source_provenance, Mapping)
            or set(source_provenance) != {"step_sha256", "project_sha256"}
            or dict(source_provenance)
            != {
                "step_sha256": current["source"]["step_sha256"],
                "project_sha256": current["source"]["project_sha256"],
            }
            or human_confirmation.get("boundary_group_evidence")
            != group_evidence
        ):
            raise MarkerConfirmationError(
                "human_confirmation source provenance is stale"
            )
        stable_assignments = human_confirmation.get(
            "stable_group_fingerprint_ids_by_role"
        )
        expected_stable_assignments = {
            role: resolved_roles[role]["group_fingerprint_id"]
            for role, item in required_by_name.items()
            if item["kind"] != "measurement_surface"
        }
        if (
            not isinstance(stable_assignments, Mapping)
            or dict(stable_assignments) != expected_stable_assignments
        ):
            raise MarkerConfirmationError(
                "human_confirmation stable group assignments are inconsistent"
            )
        measurement_selection = human_confirmation.get("measurement_selection")
        selected_measurement = resolved_roles[measurement_role][
            "measurement_candidate"
        ]
        if (
            not isinstance(measurement_selection, Mapping)
            or set(measurement_selection)
            != {"candidate_id", "candidate_sha256", "source_sha256"}
            or measurement_selection.get("candidate_id")
            != selected_measurement["candidate_id"]
            or measurement_selection.get("candidate_sha256")
            != selected_measurement.get("candidate_sha256")
            or measurement_selection.get("source_sha256")
            != selected_measurement.get("source_sha256")
        ):
            raise MarkerConfirmationError(
                "human_confirmation measurement selection is inconsistent"
            )
        human_audit = human_confirmation.get("audit")
        selected_tags_by_role = (
            human_audit.get("selected_gmsh_surface_entity_tags_by_role")
            if isinstance(human_audit, Mapping)
            else None
        )
        embedded_group_tags = {
            str(group["group_fingerprint_id"]): sorted(
                group["audit"]["gmsh_surface_entity_tags"]
            )
            for group in document["boundary_group_candidates"]
        }
        if (
            not isinstance(human_audit, Mapping)
            or set(human_audit)
            != {
                "selected_gmsh_surface_entity_tags_by_role",
                "gmsh_tags_are_matching_criteria",
            }
            or human_audit.get("gmsh_tags_are_matching_criteria") is not False
            or not isinstance(selected_tags_by_role, Mapping)
            or set(selected_tags_by_role) != set(expected_stable_assignments)
        ):
            raise MarkerConfirmationError(
                "human_confirmation audit tag selections are invalid"
            )
        for role, group_id in expected_stable_assignments.items():
            tags = _selected_audit_tags(selected_tags_by_role[role], role)
            if tags != embedded_group_tags[group_id]:
                raise MarkerConfirmationError(
                    "human_confirmation audit tags do not identify the selected complete group"
                )
        if any(
            confirmations[role]["confirmed_by"].strip()
            != human_confirmed_by.strip()
            or confirmations[role]["decision_basis"].strip()
            != human_basis.strip()
            for role in required_by_name
        ) or (
            assignment["confirmed_by"].strip() != human_confirmed_by.strip()
            or assignment["decision_basis"].strip() != human_basis.strip()
        ):
            raise MarkerConfirmationError(
                "human_confirmation metadata disagrees with role decisions"
            )
        validated_human_confirmation = {
            "schema_version": 1,
            "status": "PASS",
            "confirmed_by": human_confirmed_by.strip(),
            "confirmed_at_utc": raw_timestamp,
            "decision_basis": human_basis.strip(),
            "original_confirmation_summary": original_summary.strip(),
            "stable_group_fingerprint_ids_by_role": dict(stable_assignments),
            "measurement_candidate_id": selected_measurement["candidate_id"],
            "gmsh_tags_are_matching_criteria": False,
        }

    return {
        "status": "PASS",
        "marker_configuration_ready": True,
        "markers_toml_written": False,
        "source_step_sha256": current["source"]["step_sha256"],
        "confirmation_source": dict(source),
        "current_evidence": current["source"],
        "roles": resolved_roles,
        "rear_outlet_group_assignment": {
            "role_to_group_fingerprint": dict(role_to_group),
            "confirmed_by": assignment["confirmed_by"].strip(),
            "decision_basis": assignment["decision_basis"].strip(),
        },
        "human_confirmation": validated_human_confirmation,
        "policy": {
            "gmsh_tags_are_audit_only": True,
            "every_selected_fingerprint_uniquely_matched": True,
            "all_solver_boundaries_require_complete_surface_groups": True,
            "rear_outlets_require_complete_surface_groups": True,
            "rear_outlet_z_sign_inference_forbidden": True,
            "legacy_marker_review_rear_candidates_ignored": True,
            "legacy_marker_review_surface_candidates_ignored": True,
            "duplicate_solver_marker_assignment_forbidden": True,
            "measurement_surface_is_solver_boundary": False,
            "formal_markers_config_written": False,
        },
    }


def _confirmation_timestamp(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if not isinstance(value, str) or not value.strip():
        raise MarkerConfirmationError(
            "confirmed_at_utc must be a nonempty UTC timestamp"
        )
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError as error:
        raise MarkerConfirmationError(
            "confirmed_at_utc must be a valid ISO-8601 UTC timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise MarkerConfirmationError(
            "confirmed_at_utc must include the UTC offset"
        )
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _selected_audit_tags(value: object, role: str) -> list[int]:
    if not isinstance(value, (list, tuple, set, frozenset)) or not value:
        raise MarkerConfirmationError(
            f"role {role!r} must select a nonempty complete audit tag set"
        )
    tags: list[int] = []
    for tag in value:
        if isinstance(tag, bool) or not isinstance(tag, int) or tag <= 0:
            raise MarkerConfirmationError(
                f"role {role!r} contains an invalid audit surface tag"
            )
        tags.append(tag)
    if len(tags) != len(set(tags)):
        raise MarkerConfirmationError(
            f"role {role!r} repeats an audit surface tag"
        )
    return sorted(tags)


def solidify_marker_confirmation(
    template: PathLike | Mapping[str, Any],
    current_marker_review_path: PathLike,
    output_path: PathLike,
    *,
    selected_surface_tags_by_role: Mapping[str, Sequence[int] | set[int]],
    selected_measurement_candidate_id: str,
    confirmed_by: str,
    user_decision_summary: str,
    confirmed_at_utc: str | None = None,
) -> dict[str, Any]:
    """Write a new, independently validated human confirmation document.

    Surface tags are accepted only as the audit vocabulary of the exact
    template being confirmed.  Each supplied set must equal one complete
    candidate group's audit set.  The decisions written to the document use
    stable group fingerprints; runtime tags remain only below the explicit
    human-confirmation ``audit`` object.

    No marker configuration is written.  The destination must be a new JSON
    file outside ``config`` and the completed document is validated against
    the current marker-review evidence before the atomic write.
    """

    destination = _output_json_path(output_path)
    if destination.exists():
        raise MarkerConfirmationError(
            f"refusing to overwrite existing file: {destination}"
        )
    if not isinstance(confirmed_by, str) or not confirmed_by.strip():
        raise MarkerConfirmationError("confirmed_by must be nonempty")
    if not isinstance(user_decision_summary, str) or not user_decision_summary.strip():
        raise MarkerConfirmationError("user_decision_summary must be nonempty")
    timestamp = _confirmation_timestamp(confirmed_at_utc)
    if not isinstance(selected_surface_tags_by_role, Mapping):
        raise MarkerConfirmationError(
            "selected_surface_tags_by_role must be a role-to-tag-set mapping"
        )
    if (
        not isinstance(selected_measurement_candidate_id, str)
        or not selected_measurement_candidate_id.strip()
    ):
        raise MarkerConfirmationError(
            "selected_measurement_candidate_id must be nonempty"
        )
    selected_measurement_candidate_id = selected_measurement_candidate_id.strip()

    if isinstance(template, Mapping):
        template_path: Path | None = None
        document = _load_confirmation(template)
        template_sha256 = _canonical_sha256(document)
    else:
        template_path = Path(template).resolve()
        if not template_path.is_file():
            raise MarkerConfirmationError(
                f"marker confirmation template does not exist: {template_path}"
            )
        document = _load_confirmation(template_path)
        template_sha256 = _sha256(template_path)
    if (
        document.get("schema_version") != _SCHEMA_VERSION
        or document.get("document_kind") != _DOCUMENT_KIND
        or document.get("status") != "PENDING_HUMAN_CONFIRMATION"
        or document.get("markers_toml_written") is not False
    ):
        raise MarkerConfirmationError(
            "only an unmodified pending marker confirmation template can be solidified"
        )

    required_roles = document.get("required_roles")
    confirmations = document.get("confirmations")
    raw_groups = document.get("boundary_group_candidates")
    raw_measurements = document.get("measurement_candidates")
    if (
        not isinstance(required_roles, list)
        or not isinstance(confirmations, Mapping)
        or not isinstance(raw_groups, list)
        or not isinstance(raw_measurements, list)
    ):
        raise MarkerConfirmationError(
            "marker confirmation template is missing candidate evidence"
        )
    roles_by_name: dict[str, str] = {}
    for item in required_roles:
        if not isinstance(item, Mapping):
            raise MarkerConfirmationError("template contains an invalid required role")
        role = item.get("role")
        kind = item.get("kind")
        if (
            not isinstance(role, str)
            or not role
            or not isinstance(kind, str)
            or not kind
            or role in roles_by_name
        ):
            raise MarkerConfirmationError("template required roles are invalid")
        roles_by_name[role] = kind
    solver_roles = {
        role for role, kind in roles_by_name.items() if kind != "measurement_surface"
    }
    measurement_roles = [
        role for role, kind in roles_by_name.items() if kind == "measurement_surface"
    ]
    if set(selected_surface_tags_by_role) != solver_roles:
        missing = sorted(solver_roles - set(selected_surface_tags_by_role))
        extra = sorted(set(selected_surface_tags_by_role) - solver_roles)
        raise MarkerConfirmationError(
            f"human group selections must cover every solver role exactly; "
            f"missing={missing}, extra={extra}"
        )
    if len(measurement_roles) != 1:
        raise MarkerConfirmationError(
            "template must contain exactly one measurement_surface role"
        )

    groups_by_id: dict[str, Mapping[str, Any]] = {}
    groups_by_audit_tags: dict[tuple[int, ...], list[str]] = {}
    for group in raw_groups:
        fingerprint = group.get("group_fingerprint") if isinstance(group, Mapping) else None
        group_id = group.get("group_fingerprint_id") if isinstance(group, Mapping) else None
        audit = group.get("audit") if isinstance(group, Mapping) else None
        completeness = group.get("completeness") if isinstance(group, Mapping) else None
        raw_tags = audit.get("gmsh_surface_entity_tags") if isinstance(audit, Mapping) else None
        if (
            not isinstance(group, Mapping)
            or not isinstance(group_id, str)
            or not _is_sha256(group_id)
            or not isinstance(fingerprint, Mapping)
            or _canonical_sha256(fingerprint) != group_id
            or group_id in groups_by_id
            or group.get("kind") != "solver_boundary_surface_group"
            or group.get("business_role_asserted") is not False
            or not isinstance(completeness, Mapping)
            or completeness.get("status") != "COMPLETE"
            or not isinstance(raw_tags, list)
            or not raw_tags
            or audit.get("tag_is_matching_criterion") is not False
        ):
            raise MarkerConfirmationError(
                "template contains an invalid complete boundary group"
            )
        tags = _selected_audit_tags(
            raw_tags, f"candidate group {group.get('candidate_id')!r}"
        )
        if completeness.get("expected_member_count") != len(tags):
            raise MarkerConfirmationError(
                "template boundary group completeness count is inconsistent"
            )
        groups_by_id[group_id] = group
        groups_by_audit_tags.setdefault(tuple(tags), []).append(group_id)

    selected_group_ids: dict[str, str] = {}
    used_group_ids: set[str] = set()
    selected_tags_audit: dict[str, list[int]] = {}
    for role in sorted(solver_roles):
        decision = confirmations.get(role)
        if not isinstance(decision, Mapping):
            raise MarkerConfirmationError(f"template lacks decision for role {role!r}")
        allowed_ids = decision.get("candidate_group_fingerprint_ids")
        if not isinstance(allowed_ids, list) or len(allowed_ids) != len(set(allowed_ids)):
            raise MarkerConfirmationError(
                f"role {role!r} has invalid complete-group candidates"
            )
        requested_tags = _selected_audit_tags(
            selected_surface_tags_by_role[role], role
        )
        matches = [
            group_id
            for group_id in groups_by_audit_tags.get(tuple(requested_tags), [])
            if group_id in allowed_ids
        ]
        if len(matches) != 1:
            raise MarkerConfirmationError(
                f"role {role!r} audit tags must match exactly one complete candidate group"
            )
        group_id = matches[0]
        if group_id in used_group_ids:
            raise MarkerConfirmationError(
                f"complete group {group_id} was selected for multiple solver roles"
            )
        used_group_ids.add(group_id)
        selected_group_ids[role] = group_id
        selected_tags_audit[role] = requested_tags

    measurement_role = measurement_roles[0]
    measurement_decision = confirmations.get(measurement_role)
    if not isinstance(measurement_decision, Mapping):
        raise MarkerConfirmationError("template lacks the measurement role decision")
    allowed_measurements = measurement_decision.get("candidate_measurement_ids")
    matching_measurements = [
        candidate
        for candidate in raw_measurements
        if isinstance(candidate, Mapping)
        and candidate.get("candidate_id") == selected_measurement_candidate_id
        and candidate.get("role") == measurement_role
        and candidate.get("solver_boundary") is False
    ]
    if (
        not isinstance(allowed_measurements, list)
        or selected_measurement_candidate_id not in allowed_measurements
        or len(matching_measurements) != 1
    ):
        raise MarkerConfirmationError(
            "selected measurement candidate is absent, ambiguous or invalid"
        )
    measurement_candidate = matching_measurements[0]

    document["status"] = "CONFIRMED_BY_HUMAN"
    for role, kind in roles_by_name.items():
        decision = confirmations[role]
        decision["status"] = "CONFIRMED"
        decision["confirmed_by"] = confirmed_by.strip()
        decision["decision_basis"] = user_decision_summary.strip()
        if kind == "measurement_surface":
            decision["selected_measurement_candidate_id"] = (
                selected_measurement_candidate_id
            )
        elif kind != "rear_outlet":
            decision["selected_group_fingerprint_ids"] = [
                selected_group_ids[role]
            ]

    rear_roles = [role for role, kind in roles_by_name.items() if kind == "rear_outlet"]
    if len(rear_roles) != 2:
        raise MarkerConfirmationError("template must contain exactly two rear roles")
    rear_assignment = document.get("rear_outlet_group_assignment")
    if not isinstance(rear_assignment, Mapping):
        raise MarkerConfirmationError("template lacks rear outlet group assignment")
    rear_assignment["status"] = "CONFIRMED"
    rear_assignment["role_to_group_fingerprint"] = {
        role: selected_group_ids[role] for role in rear_roles
    }
    rear_assignment["confirmed_by"] = confirmed_by.strip()
    rear_assignment["decision_basis"] = user_decision_summary.strip()

    source = document.get("source")
    boundary_evidence = document.get("boundary_group_evidence")
    if not isinstance(source, Mapping) or not isinstance(boundary_evidence, Mapping):
        raise MarkerConfirmationError("template source provenance is incomplete")
    document["human_confirmation"] = {
        "schema_version": 1,
        "status": "CONFIRMED_BY_HUMAN",
        "confirmed_by": confirmed_by.strip(),
        "confirmed_at_utc": timestamp,
        "decision_basis": user_decision_summary.strip(),
        "original_confirmation_summary": user_decision_summary.strip(),
        "user_decision_summary": user_decision_summary.strip(),
        "template": {
            "path": None if template_path is None else str(template_path),
            "sha256": template_sha256,
        },
        "source": {
            "step_sha256": source.get("step_sha256"),
            "project_sha256": source.get("project_sha256"),
        },
        "boundary_group_evidence": dict(boundary_evidence),
        "stable_group_fingerprint_ids_by_role": dict(selected_group_ids),
        "measurement_selection": {
            "candidate_id": selected_measurement_candidate_id,
            "candidate_sha256": measurement_candidate.get("candidate_sha256"),
            "source_sha256": measurement_candidate.get("source_sha256"),
        },
        "audit": {
            "selected_gmsh_surface_entity_tags_by_role": selected_tags_audit,
            "gmsh_tags_are_matching_criteria": False,
        },
    }

    validation = validate_marker_confirmation(document, current_marker_review_path)
    if validation.get("status") != "PASS":
        raise MarkerConfirmationError(
            "solidified marker confirmation did not pass independent validation"
        )
    document["solidification_validation"] = {
        "status": "PASS",
        "validator": "validate_marker_confirmation",
        "source_step_sha256": validation["source_step_sha256"],
        "marker_configuration_ready": validation["marker_configuration_ready"],
        "markers_toml_written": validation["markers_toml_written"],
    }
    _atomic_write_new(
        destination,
        json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
    )
    return document


def write_marker_confirmation_validation(
    confirmation_path: PathLike,
    current_marker_review_path: PathLike,
    output_path: PathLike,
    *,
    validated_at_utc: str | None = None,
) -> dict[str, Any]:
    """Validate a saved confirmation and atomically write a PASS report.

    Validation is completed before any output is created.  Consequently an
    invalid, stale or tampered confirmation cannot leave behind a report that
    could be mistaken for PASS.  Like confirmation templates, the report must
    be a new JSON file outside ``config``.
    """

    destination = _output_json_path(output_path)
    confirmation_source = Path(confirmation_path).resolve()
    marker_review_source = Path(current_marker_review_path).resolve()
    if not confirmation_source.is_file():
        raise MarkerConfirmationError(
            f"marker confirmation does not exist: {confirmation_source}"
        )
    if not marker_review_source.is_file():
        raise MarkerConfirmationError(
            f"marker review does not exist: {marker_review_source}"
        )

    validation = validate_marker_confirmation(
        confirmation_source,
        marker_review_source,
    )
    if validation.get("status") != "PASS":
        raise MarkerConfirmationError(
            "marker confirmation validation did not return PASS"
        )
    report: dict[str, Any] = {
        "schema_version": 1,
        "document_kind": "cfdpipe_marker_confirmation_validation",
        "status": "PASS",
        "validated_at_utc": _confirmation_timestamp(validated_at_utc),
        "confirmation": {
            "path": str(confirmation_source),
            "sha256": _sha256(confirmation_source),
        },
        "current_marker_review": {
            "path": str(marker_review_source),
            "sha256": _sha256(marker_review_source),
        },
        "validation": validation,
    }
    _atomic_write_new(
        destination,
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
    )
    return report


# Short aliases are convenient for callers while the longer names make intent
# explicit in manifests and tests.
build_confirmation_template = build_marker_confirmation_template
validate_confirmation = validate_marker_confirmation


__all__ = [
    "MarkerConfirmationError",
    "build_confirmation_template",
    "build_marker_confirmation_template",
    "match_surface_fingerprint",
    "solidify_marker_confirmation",
    "validate_confirmation",
    "validate_marker_confirmation",
    "write_marker_confirmation_validation",
]
