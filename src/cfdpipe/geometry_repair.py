"""Fail-closed OpenCASCADE shared-topology repair for the project STEP.

The delivered geometry contains two valid volumes whose two interfaces are
geometrically coincident but represented by duplicate faces.  This module
performs one explicit OCC BooleanFragments operation, proves the resulting
interfaces are conformal, exports BREP and STEP to ``geometry/derived``, and
proves the topology again after clean re-imports.  It never creates a mesh or
physical group and never modifies the source STEP.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import stat
import sys
import traceback
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

from .derived_marker_rematch import rematch_derived_markers
from .measurement_surface import inspect_measurement_surfaces


PathLike = str | os.PathLike[str]
_STEP_SUFFIXES = {".step", ".stp"}
_AREA_REL_TOL = 2.0e-8
_AREA_ABS_TOL_M2 = 2.0e-9
# The delivered second solid changes by 1.4377e-5 m^3 (1.61e-7 relative)
# when OpenCASCADE makes its two proven coincident faces conformal.  These
# limits are measured, project-specific acceptance bounds, not user-tunable
# healing tolerances.
_VOLUME_REL_TOL = 2.0e-7
_VOLUME_ABS_TOL_M3 = 2.0e-5
_COORD_TOL_M = 2.0e-7
# The two independently parameterised copies of each proven interface have
# coincident sampled geometry to < 5e-15 m, while OCC mass properties differ
# by 1.26e-6 relative in area and 1.35e-5 m in centroid.  Interface ancestry
# therefore uses dedicated bounds; ordinary external faces remain strict.
_INTERFACE_AREA_REL_TOL = 2.0e-6
_INTERFACE_COORD_TOL_M = 2.0e-5


class GeometryRepairError(RuntimeError):
    """Raised when shared-topology repair or its evidence is not trustworthy."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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


def _is_read_only(path: Path) -> bool:
    metadata = path.stat()
    attributes = getattr(metadata, "st_file_attributes", None)
    if attributes is not None:
        flag = getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1)
        return bool(attributes & flag)
    writable = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    return not bool(metadata.st_mode & writable)


def _is_reparse(path: Path) -> bool:
    metadata = path.lstat()
    attributes = getattr(metadata, "st_file_attributes", 0)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attributes & flag)


def _has_reparse_component(path: Path) -> bool:
    current = path
    while True:
        if current.exists() and _is_reparse(current):
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _snapshot(path: Path) -> dict[str, Any]:
    metadata = path.stat()
    return {
        "path": str(path),
        "size_bytes": int(metadata.st_size),
        "mtime_ns": int(metadata.st_mtime_ns),
        "read_only": _is_read_only(path),
        "sha256": _sha256(path),
    }


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise GeometryRepairError(f"{label} is boolean")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise GeometryRepairError(f"{label} is not numeric") from error
    if not math.isfinite(number):
        raise GeometryRepairError(f"{label} contains NaN or Inf")
    return number


def _vector(value: Any, length: int, label: str) -> list[float]:
    try:
        result = [_finite(item, label) for item in value]
    except TypeError as error:
        raise GeometryRepairError(f"{label} is not a sequence") from error
    if len(result) != length:
        raise GeometryRepairError(
            f"{label} has {len(result)} values; expected {length}"
        )
    return result


def _positive_tags(value: Any, label: str) -> list[int]:
    result: list[int] = []
    try:
        items = list(value)
    except TypeError as error:
        raise GeometryRepairError(f"{label} is not a tag sequence") from error
    for item in items:
        if isinstance(item, bool):
            raise GeometryRepairError(f"{label} contains a boolean tag")
        try:
            tag = int(item)
        except (TypeError, ValueError) as error:
            raise GeometryRepairError(f"{label} contains an invalid tag") from error
        if tag <= 0:
            raise GeometryRepairError(f"{label} contains a non-positive tag")
        result.append(tag)
    return result


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise GeometryRepairError(f"cannot parse {label}: {error}") from error
    if not isinstance(value, dict):
        raise GeometryRepairError(f"{label} must contain a JSON object")
    return value


def _close_scalar(
    first: float,
    second: float,
    *,
    relative: float,
    absolute: float,
) -> bool:
    return abs(first - second) <= max(
        absolute, relative * max(abs(first), abs(second))
    )


def _close_vector(first: Sequence[float], second: Sequence[float]) -> bool:
    return len(first) == len(second) and all(
        abs(float(a) - float(b)) <= _COORD_TOL_M for a, b in zip(first, second)
    )


def _geometry_matches(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    return (
        str(first.get("entity_type", "")) == str(second.get("entity_type", ""))
        and _close_scalar(
            float(first["area_m2"]),
            float(second["area_m2"]),
            relative=_AREA_REL_TOL,
            absolute=_AREA_ABS_TOL_M2,
        )
        and _close_vector(first["centroid_m"], second["centroid_m"])
        and _close_vector(first["bounding_box_m"], second["bounding_box_m"])
    )


def _interface_geometry_matches(
    first: Mapping[str, Any], second: Mapping[str, Any]
) -> bool:
    """Compare the two already-proven coincident interface representations."""

    return (
        str(first.get("entity_type", "")) == str(second.get("entity_type", ""))
        and _close_scalar(
            float(first["area_m2"]),
            float(second["area_m2"]),
            relative=_INTERFACE_AREA_REL_TOL,
            absolute=_AREA_ABS_TOL_M2,
        )
        and _vectors_within(
            first["centroid_m"], second["centroid_m"], _INTERFACE_COORD_TOL_M
        )
        and _vectors_within(
            first["bounding_box_m"],
            second["bounding_box_m"],
            _INTERFACE_COORD_TOL_M,
        )
    )


def _vectors_within(
    first: Sequence[float], second: Sequence[float], tolerance: float
) -> bool:
    return len(first) == len(second) and all(
        abs(float(a) - float(b)) <= tolerance for a, b in zip(first, second)
    )


def _entity_name(gmsh: Any, dimension: int, tag: int) -> str:
    getter = getattr(gmsh.model, "getEntityName", None)
    if not callable(getter):
        return ""
    try:
        return str(getter(dimension, tag))
    except Exception:
        return ""


def _normal_samples(gmsh: Any, tag: int) -> list[list[float]]:
    bounds_getter = getattr(gmsh.model, "getParametrizationBounds", None)
    normal_getter = getattr(gmsh.model, "getNormal", None)
    if not callable(bounds_getter) or not callable(normal_getter):
        return []
    try:
        minimum_raw, maximum_raw = bounds_getter(2, tag)
        minimum = _vector(minimum_raw, 2, f"surface {tag} parametric minimum")
        maximum = _vector(maximum_raw, 2, f"surface {tag} parametric maximum")
    except Exception:
        return []
    samples: list[list[float]] = []
    for fraction in (0.25, 0.5, 0.75):
        parameters = [
            minimum[index] + fraction * (maximum[index] - minimum[index])
            for index in range(2)
        ]
        try:
            raw = _vector(
                normal_getter(tag, parameters), 3, f"surface {tag} normal"
            )
        except Exception:
            continue
        magnitude = math.sqrt(sum(component * component for component in raw))
        if magnitude > 0.0 and math.isfinite(magnitude):
            samples.append([component / magnitude for component in raw])
    return samples


def _collect_model(gmsh: Any, *, require_normals: bool = True) -> dict[str, Any]:
    volume_tags = sorted(int(tag) for _, tag in gmsh.model.getEntities(3))
    surface_tags = sorted(int(tag) for _, tag in gmsh.model.getEntities(2))
    if any(tag <= 0 for tag in volume_tags + surface_tags):
        raise GeometryRepairError("model contains non-positive entity tags")
    if len(set(volume_tags)) != len(volume_tags) or len(set(surface_tags)) != len(
        surface_tags
    ):
        raise GeometryRepairError("model contains duplicate entity tags")

    surfaces: list[dict[str, Any]] = []
    for tag in surface_tags:
        area = _finite(gmsh.model.occ.getMass(2, tag), f"surface {tag} area")
        if area <= 0.0:
            raise GeometryRepairError(f"surface {tag} area is not positive")
        centroid = _vector(
            gmsh.model.occ.getCenterOfMass(2, tag), 3, f"surface {tag} centroid"
        )
        bounds = _vector(
            gmsh.model.occ.getBoundingBox(2, tag), 6, f"surface {tag} bounds"
        )
        upward_raw, downward_raw = gmsh.model.getAdjacencies(2, tag)
        adjacent = sorted(set(_positive_tags(upward_raw, f"surface {tag} volumes")))
        curves = sorted(set(_positive_tags(downward_raw, f"surface {tag} curves")))
        normals = _normal_samples(gmsh, tag)
        if require_normals and not normals:
            raise GeometryRepairError(f"surface {tag} has no finite normal sample")
        surfaces.append(
            {
                "entity_tag": tag,
                "step_name": _entity_name(gmsh, 2, tag),
                "entity_type": str(gmsh.model.getType(2, tag)),
                "area_m2": area,
                "centroid_m": centroid,
                "bounding_box_m": bounds,
                "normal_samples": normals,
                "adjacent_volumes": adjacent,
                "boundary_curves": curves,
                "inferred_boundary_role": "unclassified",
                "recognition_confidence": "none",
            }
        )

    volumes: list[dict[str, Any]] = []
    for tag in volume_tags:
        volume = _finite(gmsh.model.occ.getMass(3, tag), f"volume {tag} mass")
        if volume <= 0.0:
            raise GeometryRepairError(f"volume {tag} is not positive")
        centroid = _vector(
            gmsh.model.occ.getCenterOfMass(3, tag), 3, f"volume {tag} centroid"
        )
        bounds = _vector(
            gmsh.model.occ.getBoundingBox(3, tag), 6, f"volume {tag} bounds"
        )
        oriented_raw = gmsh.model.getBoundary(
            [(3, tag)], combined=False, oriented=True, recursive=False
        )
        oriented: list[dict[str, int]] = []
        identities: set[int] = set()
        for dimension, signed_tag_raw in oriented_raw:
            if int(dimension) != 2:
                continue
            signed_tag = int(signed_tag_raw)
            identity = abs(signed_tag)
            if identity <= 0 or identity in identities:
                raise GeometryRepairError(
                    f"volume {tag} has an invalid or duplicate oriented face"
                )
            identities.add(identity)
            oriented.append(
                {
                    "surface_tag": identity,
                    "orientation": 1 if signed_tag > 0 else -1,
                }
            )
        _, adjacency_raw = gmsh.model.getAdjacencies(3, tag)
        adjacency = set(_positive_tags(adjacency_raw, f"volume {tag} surfaces"))
        if identities != adjacency:
            raise GeometryRepairError(
                f"volume {tag} boundary and adjacency surfaces disagree"
            )
        volumes.append(
            {
                "entity_tag": tag,
                "step_name": _entity_name(gmsh, 3, tag),
                "entity_type": str(gmsh.model.getType(3, tag)),
                "volume_m3": volume,
                "centroid_m": centroid,
                "bounding_box_m": bounds,
                "boundary_surfaces": sorted(identities),
                "oriented_boundary_surfaces": sorted(
                    oriented, key=lambda item: item["surface_tag"]
                ),
            }
        )
    return {"surfaces": surfaces, "volumes": volumes}


def _load_volume_catalog(path: Path) -> dict[int, dict[str, Any]]:
    try:
        stream = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as error:
        raise GeometryRepairError(f"cannot open original volume catalog: {error}") from error
    result: dict[int, dict[str, Any]] = {}
    with stream:
        reader = csv.DictReader(stream)
        required = {
            "entity_tag",
            "volume_m3",
            "centroid_m",
            "bounding_box_m",
            "boundary_surfaces",
        }
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise GeometryRepairError("original volume catalog is missing columns")
        for row in reader:
            try:
                tag = int(row["entity_tag"])
                centroid = _vector(json.loads(row["centroid_m"]), 3, "catalog centroid")
                bounds = _vector(json.loads(row["bounding_box_m"]), 6, "catalog bounds")
                boundary = _positive_tags(
                    json.loads(row["boundary_surfaces"]), "catalog boundary surfaces"
                )
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise GeometryRepairError("original volume catalog is invalid") from error
            if tag <= 0 or tag in result:
                raise GeometryRepairError("original volume catalog repeats a tag")
            result[tag] = {
                "entity_tag": tag,
                "volume_m3": _finite(row["volume_m3"], f"catalog volume {tag}"),
                "centroid_m": centroid,
                "bounding_box_m": bounds,
                "face_count": len(set(boundary)),
            }
    if len(result) != 2:
        raise GeometryRepairError("original volume catalog must contain two volumes")
    return result


def _load_confirmed_identity_contract(
    confirmation_path: Path,
    boundary_candidates_path: Path,
    *,
    source_hash: str,
) -> dict[str, Any]:
    document = _load_json(confirmation_path, "confirmed marker identity evidence")
    source = document.get("source")
    boundary_evidence = document.get("boundary_group_evidence")
    if (
        document.get("status") != "CONFIRMED_BY_HUMAN"
        or document.get("markers_toml_written") is not False
        or not isinstance(source, Mapping)
        or source.get("step_sha256") != source_hash
        or not isinstance(boundary_evidence, Mapping)
        or boundary_evidence.get("source_step_sha256") != source_hash
        or boundary_evidence.get("sha256") != _sha256(boundary_candidates_path)
    ):
        raise GeometryRepairError("confirmed marker identity evidence is stale")
    project_hash = source.get("project_sha256")
    if not isinstance(project_hash, str) or len(project_hash) != 64:
        raise GeometryRepairError("confirmed marker identity has no project SHA-256")

    raw_entities = document.get("entities")
    if not isinstance(raw_entities, list) or len(raw_entities) != 59:
        raise GeometryRepairError("confirmed marker identity must contain 59 surfaces")
    entities: dict[str, dict[str, Any]] = {}
    tag_to_fingerprint: dict[int, str] = {}
    for raw in raw_entities:
        fingerprint_id = raw.get("fingerprint_id") if isinstance(raw, Mapping) else None
        payload = raw.get("fingerprint") if isinstance(raw, Mapping) else None
        audit = raw.get("audit") if isinstance(raw, Mapping) else None
        tag = audit.get("gmsh_surface_entity_tag") if isinstance(audit, Mapping) else None
        if (
            not isinstance(fingerprint_id, str)
            or not isinstance(payload, Mapping)
            or _canonical_sha256(payload) != fingerprint_id
            or payload.get("source_step_sha256") != source_hash
            or payload.get("source_project_sha256") != project_hash
            or isinstance(tag, bool)
            or not isinstance(tag, int)
            or tag <= 0
            or audit.get("tag_is_matching_criterion") is not False
            or fingerprint_id in entities
            or tag in tag_to_fingerprint
        ):
            raise GeometryRepairError("confirmed surface fingerprint is invalid")
        entities[fingerprint_id] = dict(payload)
        tag_to_fingerprint[tag] = fingerprint_id

    raw_groups = document.get("boundary_group_candidates")
    if not isinstance(raw_groups, list) or len(raw_groups) != 4:
        raise GeometryRepairError("confirmed marker identity must contain four groups")
    groups: dict[str, dict[str, Any]] = {}
    external_ids: set[str] = set()
    for raw in raw_groups:
        group_id = raw.get("group_fingerprint_id") if isinstance(raw, Mapping) else None
        payload = raw.get("group_fingerprint") if isinstance(raw, Mapping) else None
        members = payload.get("members") if isinstance(payload, Mapping) else None
        if (
            not isinstance(group_id, str)
            or not isinstance(payload, Mapping)
            or _canonical_sha256(payload) != group_id
            or payload.get("source_step_sha256") != source_hash
            or payload.get("source_project_sha256") != project_hash
            or not isinstance(members, list)
            or not members
            or raw.get("kind") != "solver_boundary_surface_group"
            or raw.get("business_role_asserted") is not False
            or group_id in groups
        ):
            raise GeometryRepairError("confirmed group fingerprint is invalid")
        member_ids: list[str] = []
        for member in members:
            member_id = member.get("fingerprint_id") if isinstance(member, Mapping) else None
            member_payload = member.get("fingerprint") if isinstance(member, Mapping) else None
            if (
                not isinstance(member_id, str)
                or member_id not in entities
                or not isinstance(member_payload, Mapping)
                or dict(member_payload) != entities[member_id]
                or member_id in external_ids
            ):
                raise GeometryRepairError("confirmed group member is invalid")
            member_ids.append(member_id)
            external_ids.add(member_id)
        if payload.get("member_count") != len(member_ids):
            raise GeometryRepairError("confirmed group member count is invalid")
        groups[group_id] = {
            "member_surface_fingerprint_ids": sorted(member_ids),
            "fingerprint": dict(payload),
        }
    if len(external_ids) != 55 or len(set(entities) - external_ids) != 4:
        raise GeometryRepairError(
            "confirmed identity partition must contain 55 external and 4 interface faces"
        )
    return {
        "source_project_sha256": project_hash,
        "source_surface_catalog_sha256": source.get("surface_catalog_sha256"),
        "groups": groups,
        "entities": entities,
        "external_surface_fingerprint_ids": external_ids,
        "interface_surface_fingerprint_ids": set(entities) - external_ids,
        "all_surface_fingerprint_ids": set(entities),
        "tag_to_fingerprint_audit": tag_to_fingerprint,
        "evidence": {
            "confirmed_markers": {
                "path": str(confirmation_path),
                "sha256": _sha256(confirmation_path),
            },
            "boundary_semantic_candidates": {
                "path": str(boundary_candidates_path),
                "sha256": _sha256(boundary_candidates_path),
            },
        },
    }


def _validate_dependencies(
    *,
    source: Path,
    step_topology_path: Path,
    interface_evidence_path: Path,
    boundary_candidates_path: Path,
    confirmation_document_path: Path,
    confirmation_validation_path: Path,
    surface_catalog_path: Path,
    volume_catalog_path: Path,
) -> dict[str, Any]:
    source_hash = _sha256(source)
    topology = _load_json(step_topology_path, "STEP topology evidence")
    if topology.get("source_sha256") != source_hash:
        raise GeometryRepairError("STEP topology evidence is stale")
    mapping = topology.get("solid_volume_mapping")
    if not isinstance(mapping, Mapping) or mapping.get("status") != "MATCHED":
        raise GeometryRepairError("STEP solid-to-volume mapping is not MATCHED")
    matches = mapping.get("matches")
    if not isinstance(matches, list) or len(matches) != 2:
        raise GeometryRepairError("STEP topology must uniquely map two solids")
    solids_by_id = {
        int(item["id"]): item
        for item in topology.get("solids", [])
        if isinstance(item, Mapping) and isinstance(item.get("id"), int)
    }
    identity_contract = _load_confirmed_identity_contract(
        confirmation_document_path,
        boundary_candidates_path,
        source_hash=source_hash,
    )
    if identity_contract.get("source_surface_catalog_sha256") != _sha256(
        surface_catalog_path
    ):
        raise GeometryRepairError("confirmed source surface catalog is stale")
    solid_references: list[dict[str, Any]] = []
    catalog = _load_volume_catalog(volume_catalog_path)
    for item in matches:
        if not isinstance(item, Mapping):
            raise GeometryRepairError("STEP solid mapping contains an invalid item")
        solid_id = item.get("solid_id")
        audit_tag = item.get("volume_entity_tag")
        face_count = item.get("face_count")
        if (
            isinstance(solid_id, bool)
            or not isinstance(solid_id, int)
            or isinstance(audit_tag, bool)
            or not isinstance(audit_tag, int)
            or isinstance(face_count, bool)
            or not isinstance(face_count, int)
            or solid_id not in solids_by_id
            or audit_tag not in catalog
            or catalog[audit_tag]["face_count"] != face_count
        ):
            raise GeometryRepairError("STEP solid mapping disagrees with volume catalog")
        solid = solids_by_id[solid_id]
        solid_references.append(
            {
                "solid_id": solid_id,
                "solid_type": str(solid.get("type", "")),
                "solid_name": str(solid.get("name", "")),
                "face_count": face_count,
                "original_volume_tag_audit": audit_tag,
                "geometry": catalog[audit_tag],
                "shell_components": [
                    {
                        "root_shell_id": int(component["root_shell_id"]),
                        "step_shell_role": str(component["role_in_solid"]),
                        "face_count": int(component["face_count"]),
                    }
                    for component in solid.get("shell_components", [])
                    if isinstance(component, Mapping)
                ],
            }
        )
    if {item["solid_type"] for item in solid_references} != {
        "BREP_WITH_VOIDS",
        "MANIFOLD_SOLID_BREP",
    }:
        raise GeometryRepairError("unexpected STEP solid types for repair")
    for reference in solid_references:
        components = reference["shell_components"]
        if (
            not components
            or sum(int(item["face_count"]) for item in components)
            != int(reference["face_count"])
            or len({int(item["face_count"]) for item in components})
            != len(components)
        ):
            raise GeometryRepairError(
                f"STEP solid {reference['solid_id']} has ambiguous shell components"
            )

    interface = _load_json(interface_evidence_path, "interface completeness evidence")
    if interface.get("status") != "PASS" or interface.get("source_step_sha256") != source_hash:
        raise GeometryRepairError("interface completeness evidence is not current PASS")
    raw_pairs = interface.get("pairs")
    if not isinstance(raw_pairs, list) or len(raw_pairs) != 2:
        raise GeometryRepairError("exactly two proven interface pairs are required")
    interface_pairs: list[list[int]] = []
    interface_tags: set[int] = set()
    for item in raw_pairs:
        tags = item.get("surface_tags") if isinstance(item, Mapping) else None
        if (
            not isinstance(tags, list)
            or len(tags) != 2
            or any(isinstance(tag, bool) or not isinstance(tag, int) or tag <= 0 for tag in tags)
            or item.get("status") != "FULL_GEOMETRIC_COINCIDENCE"
        ):
            raise GeometryRepairError("interface pair is not fully proven")
        if interface_tags.intersection(tags):
            raise GeometryRepairError("interface pairs overlap")
        interface_tags.update(tags)
        interface_pairs.append(sorted(tags))
    interface_fingerprint_pairs: list[dict[str, Any]] = []
    interface_fingerprint_ids: set[str] = set()
    for tags, completeness in zip(interface_pairs, raw_pairs, strict=True):
        member_ids = sorted(
            identity_contract["tag_to_fingerprint_audit"].get(tag, "")
            for tag in tags
        )
        if (
            any(not value for value in member_ids)
            or interface_fingerprint_ids.intersection(member_ids)
            or not set(member_ids).issubset(
                identity_contract["interface_surface_fingerprint_ids"]
            )
        ):
            raise GeometryRepairError(
                "interface completeness cannot join to stable confirmed fingerprints"
            )
        interface_fingerprint_ids.update(member_ids)
        payload = {
            "schema": "cfdpipe.confirmed_interface_lineage.v1",
            "source_step_sha256": source_hash,
            "member_surface_fingerprint_ids": member_ids,
            "completeness_evidence_sha256": _canonical_sha256(completeness),
        }
        interface_fingerprint_pairs.append(
            {
                "interface_pair_fingerprint_id": _canonical_sha256(payload),
                "member_surface_fingerprint_ids": member_ids,
                "fingerprint": payload,
            }
        )
    if interface_fingerprint_ids != identity_contract[
        "interface_surface_fingerprint_ids"
    ]:
        raise GeometryRepairError(
            "stable interface fingerprints do not exhaust the four confirmed non-solver faces"
        )

    confirmation_report = _load_json(
        confirmation_validation_path, "marker confirmation validation"
    )
    validation = confirmation_report.get("validation")
    confirmation_reference = confirmation_report.get("confirmation")
    if (
        confirmation_report.get("status") != "PASS"
        or not isinstance(validation, Mapping)
        or validation.get("status") != "PASS"
        or validation.get("source_step_sha256") != source_hash
        or validation.get("markers_toml_written") is not False
        or not isinstance(confirmation_reference, Mapping)
        or confirmation_reference.get("sha256") != _sha256(confirmation_document_path)
    ):
        raise GeometryRepairError("marker confirmation validation is not current PASS")
    role_by_fingerprint: dict[str, dict[str, str]] = {}
    roles = validation.get("roles")
    if not isinstance(roles, Mapping):
        raise GeometryRepairError("marker confirmation validation has no roles")
    for role, decision in roles.items():
        if not isinstance(decision, Mapping) or decision.get("solver_boundary") is not True:
            continue
        group_id = decision.get("group_fingerprint_id")
        entities = decision.get("matched_entities")
        group = identity_contract["groups"].get(group_id)
        if (
            not isinstance(group_id, str)
            or not isinstance(entities, list)
            or not isinstance(group, Mapping)
        ):
            raise GeometryRepairError(f"confirmed role {role!r} is incomplete")
        confirmed_ids: list[str] = []
        for entity in entities:
            audit = entity.get("audit") if isinstance(entity, Mapping) else None
            tag = audit.get("gmsh_surface_entity_tag") if isinstance(audit, Mapping) else None
            fingerprint_id = entity.get("fingerprint_id") if isinstance(entity, Mapping) else None
            if (
                isinstance(tag, bool)
                or not isinstance(tag, int)
                or tag <= 0
                or not isinstance(fingerprint_id, str)
                or fingerprint_id in role_by_fingerprint
            ):
                raise GeometryRepairError("confirmed solver surface membership is invalid")
            confirmed_ids.append(fingerprint_id)
            role_by_fingerprint[fingerprint_id] = {
                "semantic_role": str(role),
                "semantic_group_fingerprint_id": group_id,
                "original_fingerprint_id": fingerprint_id,
                "original_surface_tag_audit": tag,
            }
        if sorted(confirmed_ids) != group["member_surface_fingerprint_ids"]:
            raise GeometryRepairError(
                f"confirmed role {role!r} does not match its stable group fingerprint"
            )
    if len(role_by_fingerprint) != 55:
        raise GeometryRepairError(
            "confirmed solver roles must cover exactly 55 stable external fingerprints"
        )
    return {
        "source_step_sha256": source_hash,
        "solid_references": solid_references,
        "interface_pairs": interface_pairs,
        "interface_tags_audit": sorted(interface_tags),
        "role_by_fingerprint": role_by_fingerprint,
        "interface_fingerprint_pairs": interface_fingerprint_pairs,
        "all_surface_fingerprint_ids": identity_contract[
            "all_surface_fingerprint_ids"
        ],
        "source_project_sha256": identity_contract["source_project_sha256"],
        "evidence": {
            "step_topology": {"path": str(step_topology_path), "sha256": _sha256(step_topology_path)},
            "interface_completeness": {"path": str(interface_evidence_path), "sha256": _sha256(interface_evidence_path)},
            **identity_contract["evidence"],
            "confirmation_validation": {"path": str(confirmation_validation_path), "sha256": _sha256(confirmation_validation_path)},
            "original_surface_catalog": {"path": str(surface_catalog_path), "sha256": _sha256(surface_catalog_path)},
            "original_volume_catalog": {"path": str(volume_catalog_path), "sha256": _sha256(volume_catalog_path)},
        },
    }


def _volume_matches(reference: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    return (
        int(reference["face_count"]) == len(current["boundary_surfaces"])
        and _close_scalar(
            float(reference["volume_m3"]),
            float(current["volume_m3"]),
            relative=_VOLUME_REL_TOL,
            absolute=_VOLUME_ABS_TOL_M3,
        )
        and _close_vector(reference["centroid_m"], current["centroid_m"])
        and _close_vector(reference["bounding_box_m"], current["bounding_box_m"])
    )


def _resolve_input_volumes(
    catalog: Mapping[str, Any], references: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    used: set[int] = set()
    for reference in references:
        candidates = [
            item
            for item in catalog["volumes"]
            if int(item["entity_tag"]) not in used
            and _volume_matches(reference["geometry"], item)
        ]
        if len(candidates) != 1:
            raise GeometryRepairError(
                f"STEP solid {reference['solid_id']} has {len(candidates)} current volume matches"
            )
        tag = int(candidates[0]["entity_tag"])
        used.add(tag)
        resolved.append({**dict(reference), "current_volume_tag_audit": tag})
    if len(used) != 2:
        raise GeometryRepairError("input volume lineage is incomplete")
    return resolved


def _surface_components(
    records: Sequence[Mapping[str, Any]],
) -> list[list[Mapping[str, Any]]]:
    by_tag = {int(item["entity_tag"]): item for item in records}
    remaining = set(by_tag)
    components: list[list[Mapping[str, Any]]] = []
    while remaining:
        seed = min(remaining)
        remaining.remove(seed)
        tags = {seed}
        queue = [seed]
        while queue:
            current = queue.pop()
            curves = set(int(value) for value in by_tag[current]["boundary_curves"])
            neighbours = {
                tag
                for tag in remaining
                if curves.intersection(
                    int(value) for value in by_tag[tag]["boundary_curves"]
                )
            }
            remaining.difference_update(neighbours)
            tags.update(neighbours)
            queue.extend(sorted(neighbours))
        components.append([by_tag[tag] for tag in sorted(tags)])
    return components


def _boundary_loop_signatures(
    surfaces: Sequence[Mapping[str, Any]],
) -> dict[int, dict[str, Any]]:
    owners: dict[int, set[int]] = {}
    by_tag = {int(item["entity_tag"]): item for item in surfaces}
    for tag, surface in by_tag.items():
        for curve in surface["boundary_curves"]:
            owners.setdefault(int(curve), set()).add(tag)
    signatures: dict[int, dict[str, Any]] = {}
    for tag, surface in by_tag.items():
        curves = [int(value) for value in surface["boundary_curves"]]
        incidences: list[int] = []
        neighbour_counts: dict[int, int] = {}
        for curve in curves:
            curve_owners = owners[curve]
            incidences.append(len(curve_owners))
            for neighbour in curve_owners - {tag}:
                neighbour_counts[neighbour] = neighbour_counts.get(neighbour, 0) + 1
        signatures[tag] = {
            "curve_count": len(curves),
            "curve_incidence_multiset": sorted(incidences),
            "neighbor_surface_count": len(neighbour_counts),
            "neighbor_shared_curve_count_multiset": sorted(
                neighbour_counts.values()
            ),
            "loop_partition_available": False,
            "basis": (
                "tag-independent boundary-curve incidence signature; the source "
                "catalog does not encode separate wire/loop partitions"
            ),
        }
    return signatures


def _resolve_input_surface_identities(
    catalog: Mapping[str, Any],
    resolved_volumes: Sequence[Mapping[str, Any]],
    dependencies: Mapping[str, Any],
) -> dict[str, Any]:
    """Recompute stable source fingerprints after a fresh import.

    Old entity tags are never selectors here.  A volume is first matched by
    geometry, its shell components are then matched by connected face count,
    and each current surface fingerprint is recomputed from geometry, shell
    lineage, type and boundary-curve count.
    """

    surfaces = list(catalog["surfaces"])
    loop_signatures = _boundary_loop_signatures(surfaces)
    current_by_fingerprint: dict[str, Mapping[str, Any]] = {}
    audit_rows: list[dict[str, Any]] = []
    for resolved in resolved_volumes:
        volume_tag = int(resolved["current_volume_tag_audit"])
        volume_surfaces = [
            item
            for item in surfaces
            if [int(value) for value in item["adjacent_volumes"]] == [volume_tag]
        ]
        components = _surface_components(volume_surfaces)
        shells_by_count = {
            int(item["face_count"]): item for item in resolved["shell_components"]
        }
        if len(components) != len(shells_by_count):
            raise GeometryRepairError(
                f"fresh volume {volume_tag} shell component count changed"
            )
        for component in components:
            shell = shells_by_count.get(len(component))
            if shell is None:
                raise GeometryRepairError(
                    f"fresh volume {volume_tag} has unmatched {len(component)}-face shell"
                )
            for surface in component:
                payload = {
                    "source_step_sha256": dependencies["source_step_sha256"],
                    "source_project_sha256": dependencies[
                        "source_project_sha256"
                    ],
                    "solid_shell": {
                        "step_solid_id": int(resolved["solid_id"]),
                        "step_root_shell_id": int(shell["root_shell_id"]),
                        "step_shell_role": str(shell["step_shell_role"]),
                        "shell_face_count": int(shell["face_count"]),
                    },
                    "area_m2": float(surface["area_m2"]),
                    "centroid_m": list(surface["centroid_m"]),
                    "bounding_box_m": list(surface["bounding_box_m"]),
                    "entity_type": str(surface["entity_type"]),
                    "boundary_loop_signature": loop_signatures[
                        int(surface["entity_tag"])
                    ],
                }
                fingerprint_id = _canonical_sha256(payload)
                if fingerprint_id in current_by_fingerprint:
                    raise GeometryRepairError(
                        "fresh import contains duplicate stable surface fingerprints"
                    )
                current_by_fingerprint[fingerprint_id] = surface
                audit_rows.append(
                    {
                        "surface_fingerprint_id": fingerprint_id,
                        "current_surface_tag_audit": int(surface["entity_tag"]),
                        "current_volume_tag_audit": volume_tag,
                        "step_solid_id": int(resolved["solid_id"]),
                        "step_root_shell_id": int(shell["root_shell_id"]),
                    }
                )
    expected = set(dependencies["all_surface_fingerprint_ids"])
    actual = set(current_by_fingerprint)
    if actual != expected:
        raise GeometryRepairError(
            "fresh import stable surface fingerprints do not exactly match evidence: "
            f"missing={len(expected - actual)}, unexpected={len(actual - expected)}"
        )

    role_membership: dict[int, dict[str, str]] = {}
    for fingerprint_id, semantics in dependencies["role_by_fingerprint"].items():
        surface = current_by_fingerprint[fingerprint_id]
        role_membership[int(surface["entity_tag"])] = dict(semantics)
    interface_pairs: list[list[int]] = []
    interface_tags: set[int] = set()
    interface_pair_evidence: list[dict[str, Any]] = []
    for pair in dependencies["interface_fingerprint_pairs"]:
        tags = sorted(
            int(current_by_fingerprint[fingerprint_id]["entity_tag"])
            for fingerprint_id in pair["member_surface_fingerprint_ids"]
        )
        if len(tags) != 2 or interface_tags.intersection(tags):
            raise GeometryRepairError("fresh interface fingerprint mapping is invalid")
        interface_tags.update(tags)
        interface_pairs.append(tags)
        interface_pair_evidence.append(
            {
                "interface_pair_fingerprint_id": pair[
                    "interface_pair_fingerprint_id"
                ],
                "member_surface_fingerprint_ids": pair[
                    "member_surface_fingerprint_ids"
                ],
                "current_surface_tags_audit": tags,
            }
        )
    if len(role_membership) != 55 or len(interface_tags) != 4:
        raise GeometryRepairError("fresh stable identity partition is incomplete")
    return {
        "status": "PASS",
        "matching_uses_old_runtime_tags": False,
        "surface_fingerprint_count": len(actual),
        "role_membership": role_membership,
        "interface_pairs": interface_pairs,
        "interface_tags": sorted(interface_tags),
        "surface_matches_audit": sorted(
            audit_rows, key=lambda item: item["surface_fingerprint_id"]
        ),
        "interface_pair_matches_audit": interface_pair_evidence,
    }


def _validate_shared_topology(catalog: Mapping[str, Any]) -> dict[str, Any]:
    volumes = catalog.get("volumes")
    surfaces = catalog.get("surfaces")
    if not isinstance(volumes, list) or len(volumes) != 2:
        raise GeometryRepairError("repaired geometry must contain exactly two volumes")
    if not isinstance(surfaces, list) or not surfaces:
        raise GeometryRepairError("repaired geometry contains no surfaces")
    volume_tags = {int(item["entity_tag"]) for item in volumes}
    shared: list[int] = []
    external: list[int] = []
    surface_by_tag = {int(item["entity_tag"]): item for item in surfaces}
    for tag, surface in surface_by_tag.items():
        adjacent = set(int(value) for value in surface["adjacent_volumes"])
        if not adjacent or not adjacent.issubset(volume_tags):
            raise GeometryRepairError(f"surface {tag} has invalid volume adjacency")
        if len(adjacent) == 2:
            shared.append(tag)
        elif len(adjacent) == 1:
            external.append(tag)
        else:
            raise GeometryRepairError(f"surface {tag} has unsupported adjacency count")
    boundaries: dict[int, dict[int, int]] = {}
    for volume in volumes:
        tag = int(volume["entity_tag"])
        boundaries[tag] = {
            int(item["surface_tag"]): int(item["orientation"])
            for item in volume["oriented_boundary_surfaces"]
        }
    first, second = sorted(volume_tags)
    intersection = set(boundaries[first]) & set(boundaries[second])
    if intersection != set(shared):
        raise GeometryRepairError("two-volume boundary intersection is not the shared set")
    for surface_tag in shared:
        if boundaries[first][surface_tag] == boundaries[second][surface_tag]:
            raise GeometryRepairError(
                f"shared surface {surface_tag} has equal orientation in both volumes"
            )
    if not shared:
        raise GeometryRepairError("fragment produced no shared interface surface")
    if len(external) != 55:
        raise GeometryRepairError(
            f"fragment must preserve 55 external surfaces; found {len(external)}"
        )
    if set(surface_by_tag) != set(shared) | set(external):
        raise GeometryRepairError("surface partition is incomplete")
    return {
        "status": "PASS",
        "volume_tags_audit": sorted(volume_tags),
        "unique_surface_count": len(surface_by_tag),
        "external_surface_count": len(external),
        "shared_patch_count": len(shared),
        "external_surface_tags_audit": sorted(external),
        "shared_surface_tags_audit": sorted(shared),
        "shared_orientations_audit": {
            str(surface_tag): {
                str(first): boundaries[first][surface_tag],
                str(second): boundaries[second][surface_tag],
            }
            for surface_tag in sorted(shared)
        },
    }


def _aggregate_geometry(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not records:
        raise GeometryRepairError("cannot aggregate an empty surface set")
    total_area = sum(float(item["area_m2"]) for item in records)
    if not math.isfinite(total_area) or total_area <= 0.0:
        raise GeometryRepairError("surface aggregate has invalid area")
    centroid = [
        sum(float(item["area_m2"]) * float(item["centroid_m"][axis]) for item in records)
        / total_area
        for axis in range(3)
    ]
    bounds = [
        min(float(item["bounding_box_m"][axis]) for item in records)
        for axis in range(3)
    ] + [
        max(float(item["bounding_box_m"][axis]) for item in records)
        for axis in range(3, 6)
    ]
    return {"area_m2": total_area, "centroid_m": centroid, "bounding_box_m": bounds}


def _bbox_contains(
    container: Sequence[float],
    item: Sequence[float],
    *,
    tolerance: float = _COORD_TOL_M,
) -> bool:
    return all(
        float(item[axis]) >= float(container[axis]) - tolerance
        and float(item[axis + 3]) <= float(container[axis + 3]) + tolerance
        for axis in range(3)
    )


def _assign_interface_lineages(
    pre_catalog: Mapping[str, Any],
    post_catalog: Mapping[str, Any],
    interface_pairs: Sequence[Sequence[int]],
    shared_tags: Sequence[int],
    *,
    original_interface_pairs_audit: Sequence[Sequence[int]] | None = None,
) -> list[dict[str, Any]]:
    pre = {int(item["entity_tag"]): item for item in pre_catalog["surfaces"]}
    post = {int(item["entity_tag"]): item for item in post_catalog["surfaces"]}
    references: list[dict[str, Any]] = []
    if original_interface_pairs_audit is not None and len(
        original_interface_pairs_audit
    ) != len(interface_pairs):
        raise GeometryRepairError("interface audit lineage count is inconsistent")
    for index, pair in enumerate(interface_pairs, start=1):
        if any(int(tag) not in pre for tag in pair):
            raise GeometryRepairError("proven interface tag is absent after import")
        first, second = (pre[int(pair[0])], pre[int(pair[1])])
        if not _interface_geometry_matches(first, second):
            raise GeometryRepairError("proven interface pair geometry is stale")
        references.append(
            {
                "interface_lineage_id": f"shared_interface_{index}",
                "original_surface_tags_audit": sorted(
                    int(tag)
                    for tag in (
                        original_interface_pairs_audit[index - 1]
                        if original_interface_pairs_audit is not None
                        else pair
                    )
                ),
                "fresh_import_surface_tags_audit": sorted(
                    int(tag) for tag in pair
                ),
                "reference": first,
                "descendants": [],
            }
        )
    for tag in shared_tags:
        surface = post[int(tag)]
        candidates = [
            item
            for item in references
            if _bbox_contains(
                item["reference"]["bounding_box_m"],
                surface["bounding_box_m"],
                tolerance=_INTERFACE_COORD_TOL_M,
            )
        ]
        if len(candidates) != 1:
            raise GeometryRepairError(
                f"shared patch {tag} has {len(candidates)} interface lineage matches"
            )
        candidates[0]["descendants"].append(surface)
    output: list[dict[str, Any]] = []
    for item in references:
        descendants = item["descendants"]
        aggregate = _aggregate_geometry(descendants)
        reference = item["reference"]
        aggregate_with_type = {
            **aggregate,
            "entity_type": reference["entity_type"],
        }
        pair_records = [
            pre[int(tag)] for tag in item["fresh_import_surface_tags_audit"]
        ]
        if not any(
            _interface_geometry_matches(candidate, aggregate_with_type)
            for candidate in pair_records
        ):
            raise GeometryRepairError(
                f"{item['interface_lineage_id']} does not conserve interface geometry"
            )
        output.append(
            {
                "interface_lineage_id": item["interface_lineage_id"],
                "classification": "shared_interface",
                "original_surface_tags_audit": item["original_surface_tags_audit"],
                "descendant_surface_tags_audit": sorted(
                    int(record["entity_tag"]) for record in descendants
                ),
                "aggregate_geometry": aggregate,
                "all_descendants_have_two_adjacent_volumes": all(
                    len(record["adjacent_volumes"]) == 2 for record in descendants
                ),
            }
        )
    return output


def _build_external_lineage(
    pre_catalog: Mapping[str, Any],
    post_catalog: Mapping[str, Any],
    *,
    input_to_output_volume: Mapping[int, int],
    interface_tags: set[int],
    role_membership: Mapping[int, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    pre_surfaces = [
        item
        for item in pre_catalog["surfaces"]
        if int(item["entity_tag"]) not in interface_tags
    ]
    post_surfaces = [
        item for item in post_catalog["surfaces"] if len(item["adjacent_volumes"]) == 1
    ]
    if len(pre_surfaces) != 55 or len(post_surfaces) != 55:
        raise GeometryRepairError("external surface count changed during fragment")
    output_to_input = {int(value): int(key) for key, value in input_to_output_volume.items()}
    used: set[int] = set()
    lineage: list[dict[str, Any]] = []
    for original in sorted(pre_surfaces, key=lambda item: int(item["entity_tag"])):
        original_tag = int(original["entity_tag"])
        adjacent = original["adjacent_volumes"]
        if len(adjacent) != 1 or original_tag not in role_membership:
            raise GeometryRepairError(
                f"original external surface {original_tag} lacks unique semantic ancestry"
            )
        original_volume = int(adjacent[0])
        candidates = [
            item
            for item in post_surfaces
            if int(item["entity_tag"]) not in used
            and output_to_input.get(int(item["adjacent_volumes"][0])) == original_volume
            and _geometry_matches(original, item)
        ]
        if len(candidates) != 1:
            raise GeometryRepairError(
                f"external surface {original_tag} has {len(candidates)} fragment descendants"
            )
        descendant = candidates[0]
        descendant_tag = int(descendant["entity_tag"])
        used.add(descendant_tag)
        semantics = role_membership[original_tag]
        lineage.append(
            {
                "classification": "external_solver_boundary",
                "original_surface_tag_audit": int(
                    semantics["original_surface_tag_audit"]
                ),
                "fresh_import_surface_tag_audit": original_tag,
                "original_fingerprint_id": semantics["original_fingerprint_id"],
                "semantic_role": semantics["semantic_role"],
                "semantic_group_fingerprint_id": semantics[
                    "semantic_group_fingerprint_id"
                ],
                "match_mode": "one_to_one_geometry_and_volume_lineage",
                "fragment_descendant_surface_tags_audit": [descendant_tag],
                "geometry": {
                    "area_m2": descendant["area_m2"],
                    "centroid_m": descendant["centroid_m"],
                    "bounding_box_m": descendant["bounding_box_m"],
                    "entity_type": descendant["entity_type"],
                },
            }
        )
    if used != {int(item["entity_tag"]) for item in post_surfaces}:
        raise GeometryRepairError("fragment produced an unassigned external surface")
    return lineage


def _match_reimported_model(
    reference: Mapping[str, Any], current: Mapping[str, Any]
) -> dict[str, Any]:
    reference_volumes = reference["volumes"]
    current_volumes = current["volumes"]
    if len(reference_volumes) != 2 or len(current_volumes) != 2:
        raise GeometryRepairError("CAD reimport did not preserve two volumes")
    volume_mapping: dict[int, int] = {}
    used_volumes: set[int] = set()
    for original in reference_volumes:
        candidates = [
            item
            for item in current_volumes
            if int(item["entity_tag"]) not in used_volumes
            and _close_scalar(
                float(original["volume_m3"]),
                float(item["volume_m3"]),
                relative=_VOLUME_REL_TOL,
                absolute=_VOLUME_ABS_TOL_M3,
            )
            and _close_vector(original["centroid_m"], item["centroid_m"])
            and _close_vector(original["bounding_box_m"], item["bounding_box_m"])
        ]
        if len(candidates) != 1:
            raise GeometryRepairError("CAD reimport volume lineage is ambiguous")
        source_tag = int(original["entity_tag"])
        current_tag = int(candidates[0]["entity_tag"])
        volume_mapping[source_tag] = current_tag
        used_volumes.add(current_tag)

    current_inverse = {value: key for key, value in volume_mapping.items()}
    used_surfaces: set[int] = set()
    surface_mapping: dict[int, list[int]] = {}
    for original in reference["surfaces"]:
        original_adjacent = set(int(value) for value in original["adjacent_volumes"])
        candidates: list[Mapping[str, Any]] = []
        for item in current["surfaces"]:
            tag = int(item["entity_tag"])
            if tag in used_surfaces:
                continue
            current_adjacent = set(int(value) for value in item["adjacent_volumes"])
            mapped_adjacent = {current_inverse[value] for value in current_adjacent}
            if mapped_adjacent == original_adjacent and _geometry_matches(original, item):
                candidates.append(item)
        if len(candidates) != 1:
            raise GeometryRepairError(
                f"CAD reimport surface {original['entity_tag']} has {len(candidates)} matches"
            )
        matched_tag = int(candidates[0]["entity_tag"])
        used_surfaces.add(matched_tag)
        surface_mapping[int(original["entity_tag"])] = [matched_tag]
    if used_surfaces != {int(item["entity_tag"]) for item in current["surfaces"]}:
        raise GeometryRepairError("CAD reimport has unassigned surfaces")
    topology = _validate_shared_topology(current)
    return {
        "status": "PASS",
        "volume_mapping_audit": {
            str(key): value for key, value in sorted(volume_mapping.items())
        },
        "surface_mapping_audit": {
            str(key): value for key, value in sorted(surface_mapping.items())
        },
        "topology": topology,
    }


def _assess_nonconformal_step_roundtrip(
    reference: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    strict_match_error: BaseException,
) -> dict[str, Any]:
    """Prove the known STEP round-trip duplication without accepting it.

    The OpenCASCADE STEP writer serializes the two solids independently.  A
    clean reimport consequently recreates two copies of each shared face.  We
    accept this function only as negative format evidence: every reference
    external face must map one-to-one, every reference shared face must map to
    exactly one copy on each volume, and all reimported faces must be consumed.
    The result is explicitly not pipeline eligible.
    """

    reference_volumes = reference["volumes"]
    current_volumes = current["volumes"]
    if len(reference_volumes) != 2 or len(current_volumes) != 2:
        raise GeometryRepairError(
            "STEP round-trip changed the required two-volume model"
        ) from strict_match_error
    volume_mapping: dict[int, int] = {}
    used_volumes: set[int] = set()
    for original in reference_volumes:
        candidates = [
            item
            for item in current_volumes
            if int(item["entity_tag"]) not in used_volumes
            and _close_scalar(
                float(original["volume_m3"]),
                float(item["volume_m3"]),
                relative=_VOLUME_REL_TOL,
                absolute=_VOLUME_ABS_TOL_M3,
            )
            and _close_vector(original["centroid_m"], item["centroid_m"])
            and _close_vector(original["bounding_box_m"], item["bounding_box_m"])
        ]
        if len(candidates) != 1:
            raise GeometryRepairError(
                "STEP round-trip volume lineage is ambiguous"
            ) from strict_match_error
        source_tag = int(original["entity_tag"])
        current_tag = int(candidates[0]["entity_tag"])
        volume_mapping[source_tag] = current_tag
        used_volumes.add(current_tag)

    inverse = {value: key for key, value in volume_mapping.items()}
    if any(len(item["adjacent_volumes"]) != 1 for item in current["surfaces"]):
        raise GeometryRepairError(
            "STEP round-trip failure is not the proven independent-solid duplication"
        ) from strict_match_error
    used_interface_copies: set[int] = set()
    surface_mapping: dict[int, list[int]] = {}
    shared_references = [
        item
        for item in reference["surfaces"]
        if len(item["adjacent_volumes"]) == 2
    ]
    for original in shared_references:
        original_adjacent = set(int(value) for value in original["adjacent_volumes"])
        candidates: list[Mapping[str, Any]] = []
        for item in current["surfaces"]:
            tag = int(item["entity_tag"])
            if tag in used_interface_copies:
                continue
            current_adjacent = {
                inverse[int(value)] for value in item["adjacent_volumes"]
            }
            if current_adjacent.issubset(
                original_adjacent
            ) and _interface_geometry_matches(original, item):
                candidates.append(item)
        mapped_volumes = {
            inverse[int(item["adjacent_volumes"][0])] for item in candidates
        }
        if len(candidates) != 2 or mapped_volumes != original_adjacent:
            raise GeometryRepairError(
                f"STEP round-trip shared surface {original['entity_tag']} is not "
                "duplicated once on each independently serialized solid"
            ) from strict_match_error
        tags = sorted(int(item["entity_tag"]) for item in candidates)
        used_interface_copies.update(tags)
        surface_mapping[int(original["entity_tag"])] = tags
    all_current = {int(item["entity_tag"]) for item in current["surfaces"]}
    shared_count = len(shared_references)
    external_count = len(reference["surfaces"]) - shared_count
    expected_surface_count = external_count + 2 * shared_count
    if (
        len(current["surfaces"]) != expected_surface_count
        or len(all_current - used_interface_copies) != external_count
    ):
        raise GeometryRepairError(
            "STEP round-trip surface count is not the exact duplicated-interface count"
        ) from strict_match_error
    return {
        "status": "NONCONFORMAL_AFTER_STEP_ROUNDTRIP",
        "pipeline_eligible": False,
        "strict_shared_topology_error": str(strict_match_error),
        "reason": (
            "STEP serialized the two solids independently and recreated one "
            "single-adjacency copy of every shared interface on each volume"
        ),
        "volume_mapping_audit": {
            str(key): value for key, value in sorted(volume_mapping.items())
        },
        "surface_mapping_audit": {
            str(key): value for key, value in sorted(surface_mapping.items())
        },
        "external_surface_lineage_asserted": False,
        "unassigned_single_adjacency_external_count": len(
            all_current - used_interface_copies
        ),
        "topology": {
            "status": "NONCONFORMAL_AFTER_STEP_ROUNDTRIP",
            "volume_count": len(current_volumes),
            "unique_surface_count": len(current["surfaces"]),
            "single_adjacency_surface_count": len(current["surfaces"]),
            "shared_surface_count": 0,
            "expected_external_ancestry_count": external_count,
            "duplicated_interface_lineage_count": shared_count,
        },
    }


def _run_owned_session(
    gmsh: Any,
    model_name: str,
    action: Callable[[], Any],
    log_lines: list[str],
) -> Any:
    initialized = False
    logger_started = False
    result: Any = None
    primary: tuple[BaseException, Any] | None = None
    finalize_error: BaseException | None = None
    try:
        checker = getattr(gmsh, "isInitialized", None)
        if callable(checker) and int(checker()) != 0:
            raise GeometryRepairError("refusing to take ownership of an existing Gmsh session")
        gmsh.initialize()
        initialized = True
        gmsh.model.add(model_name)
        logger = getattr(gmsh, "logger", None)
        if logger is not None and callable(getattr(logger, "start", None)):
            logger.start()
            logger_started = True
        result = action()
    except BaseException as error:
        primary = (error, sys.exc_info()[2])
    finally:
        logger = getattr(gmsh, "logger", None)
        if logger_started and logger is not None:
            try:
                messages = logger.get()
                log_lines.extend(f"[{model_name}] {message}" for message in messages)
            except Exception as error:
                log_lines.append(f"[{model_name}] logger.get failed: {error}")
            try:
                logger.stop()
            except Exception as error:
                log_lines.append(f"[{model_name}] logger.stop failed: {error}")
        if initialized:
            try:
                gmsh.finalize()
            except BaseException as error:
                finalize_error = error
    if primary is not None:
        error, tb = primary
        if finalize_error is not None:
            raise GeometryRepairError(
                f"Gmsh operation failed: {error}; finalize also failed: {finalize_error}"
            ) from error
        raise error.with_traceback(tb)
    if finalize_error is not None:
        raise GeometryRepairError(f"Gmsh finalize failed: {finalize_error}") from finalize_error
    return result


def _write_new_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(content)


def _write_new_json(path: Path, value: Mapping[str, Any]) -> None:
    _write_new_text(
        path,
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
    )


def _remove_owned_directory(
    path: Path, *, expected_parent: Path, expected_name: str
) -> None:
    """Remove only one directory whose exact path was created by this run."""

    if not path.exists():
        return
    if path.is_symlink() or _is_reparse(path):
        raise GeometryRepairError(f"refusing to remove reparse directory: {path}")
    resolved = path.resolve(strict=True)
    parent = expected_parent.resolve(strict=True)
    if resolved.parent != parent or resolved.name != expected_name:
        raise GeometryRepairError(f"refusing to remove unsafe repair directory: {resolved}")
    descendants = list(resolved.rglob("*"))
    if any(item.is_symlink() or _is_reparse(item) for item in descendants):
        raise GeometryRepairError(
            f"refusing to remove repair directory containing a reparse point: {resolved}"
        )
    for item in reversed(descendants):
        try:
            item.chmod(item.stat().st_mode | stat.S_IWUSR)
        except OSError:
            pass
    resolved.chmod(resolved.stat().st_mode | stat.S_IWUSR)
    shutil.rmtree(resolved)


def _write_catalog_csv(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    fields = [
        "entity_tag",
        "step_name",
        "entity_type",
        "area_m2",
        "centroid_m",
        "bounding_box_m",
        "normal_samples",
        "adjacent_volumes",
        "boundary_curves",
        "inferred_boundary_role",
        "recognition_confidence",
        "surface_fingerprint_id",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    field: (
                        json.dumps(record.get(field), separators=(",", ":"))
                        if isinstance(record.get(field), (list, dict))
                        else record.get(field, "")
                    )
                    for field in fields
                }
            )


def _write_volume_catalog_csv(
    path: Path, records: Sequence[Mapping[str, Any]]
) -> None:
    fields = [
        "entity_tag",
        "step_name",
        "entity_type",
        "volume_m3",
        "centroid_m",
        "bounding_box_m",
        "boundary_surfaces",
        "oriented_boundary_surfaces",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    field: (
                        json.dumps(record.get(field), separators=(",", ":"))
                        if isinstance(record.get(field), (list, dict))
                        else record.get(field, "")
                    )
                    for field in fields
                }
            )


def _validate_paths(
    source_step: PathLike,
    derived_directory: PathLike,
    evidence_directory: PathLike,
    *,
    repository_root: PathLike | None,
) -> tuple[Path, Path, Path, Path]:
    root = (
        Path(repository_root).expanduser().resolve(strict=True)
        if repository_root is not None
        else Path(__file__).resolve().parents[2]
    )
    source_raw = Path(source_step).expanduser()
    derived_raw = Path(derived_directory).expanduser()
    evidence_raw = Path(evidence_directory).expanduser()
    if any(".." in value.parts for value in (source_raw, derived_raw, evidence_raw)):
        raise GeometryRepairError("repair paths must not contain '..'")
    source = source_raw.resolve(strict=True)
    if (
        not _within(source, root)
        or source.suffix.casefold() not in _STEP_SUFFIXES
        or _is_reparse(source)
        or _has_reparse_component(source.parent)
        or not stat.S_ISREG(source.lstat().st_mode)
        or not _is_read_only(source)
    ):
        raise GeometryRepairError(
            f"repair source must be an ordinary read-only repository STEP: {source}"
        )
    derived = Path(os.path.abspath(derived_raw))
    evidence = Path(os.path.abspath(evidence_raw))
    allowed_derived = root / "geometry" / "derived"
    allowed_evidence = root / "runs" / "real_connection"
    if derived.parent != allowed_derived or derived.name != "model1_shared_topology":
        raise GeometryRepairError(
            f"derived output must exactly be {allowed_derived / 'model1_shared_topology'}"
        )
    if evidence != allowed_evidence / "geometry_repair":
        raise GeometryRepairError(
            f"repair evidence must exactly be {allowed_evidence / 'geometry_repair'}"
        )
    for path in (derived, evidence):
        if path.exists() or path.is_symlink():
            raise GeometryRepairError(f"refusing to overwrite repair output: {path}")
        if _has_reparse_component(path.parent):
            raise GeometryRepairError(f"repair output has a reparse ancestor: {path}")
    return root, source, derived, evidence


def _enrich_surface_fingerprints(
    catalog: Mapping[str, Any], derived_sha256: str
) -> dict[str, Any]:
    enriched: list[dict[str, Any]] = []
    for surface in catalog["surfaces"]:
        payload = {
            "schema": "cfdpipe.derived_surface_geometry.v1",
            "derived_cad_sha256": derived_sha256,
            "entity_type": surface["entity_type"],
            "area_m2": surface["area_m2"],
            "centroid_m": surface["centroid_m"],
            "bounding_box_m": surface["bounding_box_m"],
            "boundary_curve_count": len(surface["boundary_curves"]),
            "adjacent_volume_count": len(surface["adjacent_volumes"]),
        }
        item = dict(surface)
        item["surface_fingerprint"] = payload
        item["surface_fingerprint_id"] = _canonical_sha256(payload)
        enriched.append(item)
    return {"surfaces": enriched, "volumes": list(catalog["volumes"])}


def repair_shared_topology(
    source_step: PathLike,
    derived_directory: PathLike,
    evidence_directory: PathLike,
    *,
    step_topology_path: PathLike,
    interface_evidence_path: PathLike,
    boundary_candidates_path: PathLike,
    confirmation_document_path: PathLike,
    confirmation_validation_path: PathLike,
    original_surface_catalog_path: PathLike,
    original_volume_catalog_path: PathLike,
    repository_root: PathLike | None = None,
    gmsh_module: Any | None = None,
) -> dict[str, Any]:
    """Repair and independently revalidate the two-volume shared topology."""

    root, source, derived, evidence = _validate_paths(
        source_step,
        derived_directory,
        evidence_directory,
        repository_root=repository_root,
    )
    confirmation_validation_resolved = (
        Path(confirmation_validation_path).expanduser().resolve(strict=True)
    )
    original_surface_catalog_resolved = (
        Path(original_surface_catalog_path).expanduser().resolve(strict=True)
    )
    dependencies = _validate_dependencies(
        source=source,
        step_topology_path=Path(step_topology_path).expanduser().resolve(strict=True),
        interface_evidence_path=Path(interface_evidence_path).expanduser().resolve(strict=True),
        boundary_candidates_path=Path(boundary_candidates_path).expanduser().resolve(strict=True),
        confirmation_document_path=Path(confirmation_document_path).expanduser().resolve(strict=True),
        confirmation_validation_path=confirmation_validation_resolved,
        surface_catalog_path=original_surface_catalog_resolved,
        volume_catalog_path=Path(original_volume_catalog_path).expanduser().resolve(strict=True),
    )
    before = _snapshot(source)
    derived.parent.mkdir(parents=True, exist_ok=True)
    evidence.parent.mkdir(parents=True, exist_ok=True)
    staging = derived.parent / f".{derived.name}.{uuid4().hex}.staging"
    evidence_staging = evidence.parent / f".{evidence.name}.{uuid4().hex}.staging"
    for candidate in (staging, evidence_staging):
        if candidate.exists() or candidate.is_symlink():
            raise GeometryRepairError(f"repair staging path already exists: {candidate}")
    brep_stage = staging / "model1_shared_topology.brep"
    step_stage = staging / "model1_shared_topology.step"
    started_at = _utc_now()
    log_lines: list[str] = [
        f"[{started_at}] shared-topology repair started",
        f"source={source}",
        "operation=gmsh.model.occ.fragment removeObject=True removeTool=True",
    ]
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "status": "FAIL",
        "started_at_utc": started_at,
        "ended_at_utc": None,
        "source": before,
        "dependencies": dependencies["evidence"],
        "operation": {
            "api": "gmsh.model.occ.fragment",
            "tag": -1,
            "removeObject": True,
            "removeTool": True,
            "removeAllDuplicates_called": False,
            "healShapes_called": False,
            "mesh_generated": False,
            "physical_groups_created": False,
            "gui_started": False,
        },
        "exception": None,
    }
    gmsh: Any | None = None
    pre_catalog: dict[str, Any] | None = None
    fragment_catalog: dict[str, Any] | None = None
    fragment_lineage: dict[str, Any] | None = None
    reimports: dict[str, Any] = {}
    reimport_catalogs: dict[str, dict[str, Any]] = {}
    published_derived = False
    try:
        staging.mkdir()
        evidence_staging.mkdir()
        if gmsh_module is None:
            try:
                import gmsh as gmsh_module
            except Exception as error:
                raise GeometryRepairError(f"cannot import gmsh: {error}") from error
        gmsh = gmsh_module
        manifest["gmsh_version"] = str(
            getattr(
                gmsh,
                "__version__",
                getattr(gmsh, "GMSH_API_VERSION", "unknown"),
            )
        )
        module_path = getattr(gmsh, "__file__", None)
        manifest["gmsh_module_path"] = (
            None if module_path is None else str(Path(module_path).resolve())
        )

        def fragment_action() -> dict[str, Any]:
            gmsh.option.setString("Geometry.OCCTargetUnit", "M")
            imported = gmsh.model.occ.importShapes(str(source))
            gmsh.model.occ.synchronize()
            if not imported:
                raise GeometryRepairError("Gmsh imported no entities from source STEP")
            pre = _collect_model(gmsh)
            if len(pre["volumes"]) != 2 or len(pre["surfaces"]) != 59:
                raise GeometryRepairError("source import is not the proven 2-volume/59-surface model")
            resolved = _resolve_input_volumes(pre, dependencies["solid_references"])
            input_surface_identities = _resolve_input_surface_identities(
                pre, resolved, dependencies
            )
            by_type = {item["solid_type"]: item for item in resolved}
            object_tag = int(by_type["BREP_WITH_VOIDS"]["current_volume_tag_audit"])
            tool_tag = int(by_type["MANIFOLD_SOLID_BREP"]["current_volume_tag_audit"])
            out_raw, map_raw = gmsh.model.occ.fragment(
                [(3, object_tag)],
                [(3, tool_tag)],
                tag=-1,
                removeObject=True,
                removeTool=True,
            )
            gmsh.model.occ.synchronize()
            out = [(int(dim), int(tag)) for dim, tag in out_raw]
            output_volumes = sorted(tag for dim, tag in out if dim == 3)
            mapping = [
                [(int(dim), int(tag)) for dim, tag in items]
                for items in map_raw
            ]
            if len(mapping) != 2:
                raise GeometryRepairError("fragment did not return two parent mappings")
            mapped_volumes = [
                [tag for dim, tag in items if dim == 3] for items in mapping
            ]
            if (
                any(len(items) != 1 for items in mapped_volumes)
                or len(set(items[0] for items in mapped_volumes)) != 2
                or sorted(items[0] for items in mapped_volumes) != output_volumes
            ):
                raise GeometryRepairError("fragment volume lineage is not one-to-one")
            input_to_output = {
                object_tag: mapped_volumes[0][0],
                tool_tag: mapped_volumes[1][0],
            }
            post = _collect_model(gmsh)
            topology = _validate_shared_topology(post)
            pre_volume = {int(item["entity_tag"]): item for item in pre["volumes"]}
            post_volume = {int(item["entity_tag"]): item for item in post["volumes"]}
            volume_conservation: list[dict[str, Any]] = []
            for input_tag, output_tag in input_to_output.items():
                first = pre_volume[input_tag]
                second = post_volume[output_tag]
                before_volume = float(first["volume_m3"])
                after_volume = float(second["volume_m3"])
                absolute_delta = abs(after_volume - before_volume)
                relative_delta = absolute_delta / abs(before_volume)
                if not (
                    _close_scalar(
                        before_volume,
                        after_volume,
                        relative=_VOLUME_REL_TOL,
                        absolute=_VOLUME_ABS_TOL_M3,
                    )
                    and _close_vector(first["centroid_m"], second["centroid_m"])
                    and _close_vector(first["bounding_box_m"], second["bounding_box_m"])
                ):
                    raise GeometryRepairError("fragment changed a fluid volume geometry")
                volume_conservation.append(
                    {
                        "status": "PASS",
                        "input_volume_tag_audit": input_tag,
                        "output_volume_tag_audit": output_tag,
                        "before_m3": before_volume,
                        "after_m3": after_volume,
                        "absolute_delta_m3": absolute_delta,
                        "relative_delta": relative_delta,
                        "acceptance": {
                            "relative_tolerance": _VOLUME_REL_TOL,
                            "absolute_tolerance_m3": _VOLUME_ABS_TOL_M3,
                            "centroid_and_bounds_tolerance_m": _COORD_TOL_M,
                        },
                    }
                )
            interfaces = _assign_interface_lineages(
                pre,
                post,
                input_surface_identities["interface_pairs"],
                topology["shared_surface_tags_audit"],
                original_interface_pairs_audit=dependencies["interface_pairs"],
            )
            external = _build_external_lineage(
                pre,
                post,
                input_to_output_volume=input_to_output,
                interface_tags=set(input_surface_identities["interface_tags"]),
                role_membership=input_surface_identities["role_membership"],
            )
            gmsh.write(str(brep_stage))
            gmsh.write(str(step_stage))
            if not brep_stage.is_file() or brep_stage.stat().st_size <= 0:
                raise GeometryRepairError("Gmsh did not write a nonempty BREP")
            if not step_stage.is_file() or step_stage.stat().st_size <= 0:
                raise GeometryRepairError("Gmsh did not write a nonempty STEP")
            return {
                "pre": pre,
                "post": post,
                "resolved_inputs": resolved,
                "input_surface_identity_resolution": input_surface_identities,
                "out_dimtags_audit": out,
                "out_dimtags_map_audit": mapping,
                "input_to_output_volume_tags_audit": {
                    str(key): value for key, value in input_to_output.items()
                },
                "topology": topology,
                "volume_conservation": volume_conservation,
                "interface_lineages": interfaces,
                "external_surface_lineage": external,
            }

        fragment_result = _run_owned_session(
            gmsh, "cfdpipe_shared_topology_fragment", fragment_action, log_lines
        )
        pre_catalog = fragment_result["pre"]
        fragment_catalog = fragment_result["post"]
        fragment_lineage = fragment_result

        for format_name, path in (("brep", brep_stage), ("step", step_stage)):
            def reimport_action(path: Path = path) -> dict[str, Any]:
                gmsh.option.setString("Geometry.OCCTargetUnit", "M")
                imported = gmsh.model.occ.importShapes(str(path))
                gmsh.model.occ.synchronize()
                if not imported:
                    raise GeometryRepairError(f"Gmsh reimported no entities from {path}")
                return _collect_model(gmsh)

            catalog = _run_owned_session(
                gmsh,
                f"cfdpipe_reimport_{format_name}",
                reimport_action,
                log_lines,
            )
            reimport_catalogs[format_name] = catalog
            if format_name == "brep":
                validation = _match_reimported_model(fragment_catalog, catalog)
                validation["pipeline_eligible"] = True
            else:
                try:
                    validation = _match_reimported_model(fragment_catalog, catalog)
                    validation["pipeline_eligible"] = True
                except GeometryRepairError as strict_error:
                    validation = _assess_nonconformal_step_roundtrip(
                        fragment_catalog,
                        catalog,
                        strict_match_error=strict_error,
                    )
            reimports[format_name] = validation

        brep_final = derived / brep_stage.name
        step_final = derived / step_stage.name
        writable = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
        for path in (brep_stage, step_stage):
            path.chmod(path.stat().st_mode & ~writable)
            if not _is_read_only(path):
                raise GeometryRepairError(f"derived CAD is not read-only: {path}")
        derived_files = {
            "brep": {
                "path": str(brep_final),
                "sha256": _sha256(brep_stage),
                "size_bytes": brep_stage.stat().st_size,
                "reimport_status": reimports["brep"]["status"],
                "pipeline_eligible": True,
            },
            "step": {
                "path": str(step_final),
                "sha256": _sha256(step_stage),
                "size_bytes": step_stage.stat().st_size,
                "reimport_status": reimports["step"]["status"],
                "pipeline_eligible": bool(
                    reimports["step"].get("pipeline_eligible", False)
                ),
            },
        }
        pipeline_catalog = _enrich_surface_fingerprints(
            reimport_catalogs["brep"], derived_files["brep"]["sha256"]
        )
        pipeline_mapping = reimports["brep"]["surface_mapping_audit"]
        enriched_by_tag = {
            int(item["entity_tag"]): item for item in pipeline_catalog["surfaces"]
        }
        external_final: list[dict[str, Any]] = []
        for item in fragment_lineage["external_surface_lineage"]:
            fragment_tag = int(item["fragment_descendant_surface_tags_audit"][0])
            derived_tags = [
                int(value) for value in pipeline_mapping[str(fragment_tag)]
            ]
            derived_records = [enriched_by_tag[tag] for tag in derived_tags]
            external_final.append(
                {
                    **item,
                    "fragment_descendant_surface_tags_audit": item[
                        "fragment_descendant_surface_tags_audit"
                    ],
                    "descendant_surface_tags_audit": derived_tags,
                    "descendant_surface_fingerprint_ids": [
                        record["surface_fingerprint_id"] for record in derived_records
                    ],
                }
            )
        interfaces_final: list[dict[str, Any]] = []
        for item in fragment_lineage["interface_lineages"]:
            derived_tags = sorted(
                tag
                for fragment_tag in item["descendant_surface_tags_audit"]
                for tag in pipeline_mapping[str(fragment_tag)]
            )
            interfaces_final.append(
                {
                    **item,
                    "fragment_descendant_surface_tags_audit": item[
                        "descendant_surface_tags_audit"
                    ],
                    "descendant_surface_tags_audit": derived_tags,
                    "descendant_surface_fingerprint_ids": [
                        enriched_by_tag[tag]["surface_fingerprint_id"]
                        for tag in derived_tags
                    ],
                }
            )
        lineage_document = {
            "schema_version": 1,
            "status": "PASS",
            "source_step_sha256": dependencies["source_step_sha256"],
            "derived_cad_path": str(brep_final),
            "derived_cad_sha256": derived_files["brep"]["sha256"],
            "pipeline_geometry_format": "brep",
            "volume_lineage": fragment_lineage[
                "input_to_output_volume_tags_audit"
            ],
            "external_surface_lineage": external_final,
            "interface_lineages": interfaces_final,
            "policy": {
                "runtime_tags_are_audit_only": True,
                "all_external_surfaces_have_one_semantic_ancestor": True,
                "shared_interfaces_are_not_solver_boundaries": True,
            },
        }
        measurement_report = inspect_measurement_surfaces(brep_stage)
        measurement_report["source_cad_path"] = str(brep_final)
        measurement_report["inspection_used_prepublication_staging"] = True
        measurement_report["pipeline_geometry_format"] = "brep"
        measurement_path = evidence_staging / "measurement_surface_candidates.json"
        _write_new_json(measurement_path, measurement_report)
        marker_rematch_path = evidence_staging / "marker_rematch.json"
        marker_rematch = rematch_derived_markers(
            confirmation_validation_resolved,
            original_surface_catalog_resolved,
            lineage_document,
            pipeline_catalog,
            measurement_report,
            marker_rematch_path,
        )
        if marker_rematch.get("status") != "PASS":
            raise GeometryRepairError("derived marker rematch did not return PASS")
        log_lines.append(
            "derived BREP measurement discovery and tag-free marker rematch PASS"
        )
        _write_new_json(evidence_staging / "pre_topology.json", pre_catalog)
        _write_new_json(evidence_staging / "fragment_topology.json", fragment_catalog)
        _write_new_json(
            evidence_staging / "brep_reimport_topology.json",
            reimport_catalogs["brep"],
        )
        _write_new_json(
            evidence_staging / "step_reimport_topology.json",
            reimport_catalogs["step"],
        )
        _write_new_json(
            evidence_staging / "pipeline_brep_topology.json", pipeline_catalog
        )
        _write_catalog_csv(
            evidence_staging / "surface_catalog.csv", pipeline_catalog["surfaces"]
        )
        _write_volume_catalog_csv(
            evidence_staging / "volume_catalog.csv", pipeline_catalog["volumes"]
        )
        _write_new_json(
            evidence_staging / "fragment_lineage.json", lineage_document
        )
        _write_new_json(
            evidence_staging / "shared_interface.json",
            {
                "status": "PASS",
                "fragment": fragment_lineage["interface_lineages"],
                "brep_reimport": reimports["brep"]["topology"],
                "step_reimport": reimports["step"]["topology"],
                "pipeline_geometry_format": "brep",
            },
        )
        fatal_log_lines = [
            line
            for line in log_lines
            if any(token in line.casefold() for token in ("error", "fatal"))
        ]
        if fatal_log_lines:
            raise GeometryRepairError(
                "Gmsh repair log contains error/fatal diagnostics: "
                + " | ".join(fatal_log_lines[:4])
            )
        warning_log_lines = [
            line for line in log_lines if "warning" in line.casefold()
        ]
        after = _snapshot(source)
        manifest.update(
            {
                "status": "PASS",
                "ended_at_utc": _utc_now(),
                "source_after": after,
                "source_unchanged": after == before,
                "resolved_input_volumes": fragment_lineage["resolved_inputs"],
                "input_surface_identity_resolution": fragment_lineage[
                    "input_surface_identity_resolution"
                ],
                "out_dimtags_audit": fragment_lineage["out_dimtags_audit"],
                "out_dimtags_map_audit": fragment_lineage[
                    "out_dimtags_map_audit"
                ],
                "fragment_topology": fragment_lineage["topology"],
                "volume_conservation": fragment_lineage["volume_conservation"],
                "reimport_validation": reimports,
                "derived_files": derived_files,
                "pipeline_geometry": derived_files["brep"],
                "measurement_surface": {
                    "status": measurement_report["status"],
                    "path": str(evidence / measurement_path.name),
                    "derived_cad_sha256": measurement_report[
                        "derived_cad_sha256"
                    ],
                },
                "marker_rematch": {
                    "status": marker_rematch["status"],
                    "path": str(evidence / marker_rematch_path.name),
                    "derived_cad_sha256": marker_rematch["derived_cad_sha256"],
                },
                "gmsh_warnings": warning_log_lines,
                "warning_disposition": (
                    "OCC warnings retained; accepted only because two-volume "
                    "adjacency, opposite shared orientations, external ancestry, "
                    "volume bounds, and the pipeline BREP clean reimport all passed; "
                    "the STEP round-trip topology is separately marked nonconformal"
                    if warning_log_lines
                    else "no Gmsh warnings"
                ),
                "outputs": [
                    "pre_topology.json",
                    "fragment_topology.json",
                    "brep_reimport_topology.json",
                    "step_reimport_topology.json",
                    "pipeline_brep_topology.json",
                    "surface_catalog.csv",
                    "volume_catalog.csv",
                    "fragment_lineage.json",
                    "shared_interface.json",
                    "measurement_surface_candidates.json",
                    "marker_rematch.json",
                    "gmsh_repair.log",
                    "repair_manifest.json",
                ],
            }
        )
        if manifest["source_unchanged"] is not True:
            raise GeometryRepairError("source STEP changed during topology repair")
        log_lines.append(f"[{manifest['ended_at_utc']}] repair validation PASS")
        _write_new_text(
            evidence_staging / "gmsh_repair.log", "\n".join(log_lines) + "\n"
        )
        _write_new_json(evidence_staging / "repair_manifest.json", manifest)
        if derived.exists() or evidence.exists():
            raise GeometryRepairError("repair output appeared before atomic publication")
        staging.rename(derived)
        published_derived = True
        try:
            evidence_staging.rename(evidence)
        except BaseException:
            _remove_owned_directory(
                derived,
                expected_parent=derived.parent,
                expected_name=derived.name,
            )
            published_derived = False
            raise
        return manifest
    except BaseException as error:
        exception_text = traceback.format_exc()
        cleanup_errors: list[str] = []
        for candidate, parent in (
            (staging, derived.parent),
            (evidence_staging, evidence.parent),
        ):
            try:
                _remove_owned_directory(
                    candidate,
                    expected_parent=parent,
                    expected_name=candidate.name,
                )
            except BaseException as cleanup_error:
                cleanup_errors.append(str(cleanup_error))
        if published_derived and derived.exists():
            try:
                _remove_owned_directory(
                    derived,
                    expected_parent=derived.parent,
                    expected_name=derived.name,
                )
                published_derived = False
            except BaseException as cleanup_error:
                cleanup_errors.append(str(cleanup_error))
        after = _snapshot(source)
        manifest.update(
            {
                "status": "FAIL",
                "ended_at_utc": _utc_now(),
                "source_after": after,
                "source_unchanged": after == before,
                "exception": exception_text,
                "cleanup_errors": cleanup_errors,
                "derived_published": derived.exists(),
            }
        )
        if not evidence.exists():
            evidence.mkdir(parents=True, exist_ok=False)
            _write_new_text(evidence / "gmsh_repair.log", "\n".join(log_lines) + "\n")
            _write_new_json(evidence / "repair_manifest.json", manifest)
        if cleanup_errors:
            raise GeometryRepairError(
                "shared-topology repair failed and owned-output cleanup was incomplete: "
                + " | ".join(cleanup_errors)
            ) from error
        if after != before:
            raise GeometryRepairError(
                "source STEP changed during failed topology repair"
            ) from error
        if derived.exists():
            raise GeometryRepairError(
                "topology repair failed after publishing derived geometry"
            ) from error
        raise GeometryRepairError(f"shared-topology repair failed: {error}") from error


__all__ = ["GeometryRepairError", "repair_shared_topology"]
