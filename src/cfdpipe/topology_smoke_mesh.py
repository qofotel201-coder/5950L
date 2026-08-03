"""Fail-closed three-dimensional topology-smoke meshing of a repaired BREP.

The marker contract consumed here is tag-free.  Runtime Gmsh entity tags are
resolved afresh from stable geometry fingerprints, used only in the owned Gmsh
session, and retained only as audit evidence in the resulting manifest.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib
import json
import math
import os
import operator
from pathlib import Path
import re
import shutil
import stat
import traceback
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .geometry_repair import _collect_model, _validate_shared_topology
from .marker_config import validate_marker_config_document


PathLike = str | os.PathLike[str]
_SCHEMA = "cfdpipe.topology_smoke_mesh.v1"
_MARKER_SCHEMA = "cfdpipe.markers.v2"
_MAX_ELEMENT_CAP = 300_000
_MAX_AREA_REL_TOL = 2.0e-8
_MAX_AREA_ABS_TOL_M2 = 2.0e-9
_MAX_COORD_TOL_M = 2.0e-7
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_NONFINITE_TEXT = re.compile(
    r"(?<![A-Za-z0-9_])(?:[+-]?(?:nan|inf(?:inity)?))(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_OVERLAPPING_FACETS = re.compile(
    r"Invalid boundary mesh \(overlapping facets\) on surface\s+(\d+)\s+surface\s+(\d+)",
    re.IGNORECASE,
)
_FACET_RECORD = re.compile(
    r"\b(1st|2nd):\s*\[([^]]*)\]\s*#(\d+)\s*$", re.IGNORECASE
)
_DIHEDRAL_DEGREES = re.compile(
    r"dihedral angle between them is\s+([0-9.eE+-]+)\s+degree",
    re.IGNORECASE,
)
_NEAR_SELF_INTERSECTION = re.compile(
    r"two nearly self-intersecting facets", re.IGNORECASE
)
_PLC_INTERSECTION = re.compile(
    r"PLC Error:\s*A segment and a facet intersect at point", re.IGNORECASE
)
_MISSING_FACETS = re.compile(
    r"Recovering\s+(\d+)\s+missing facet\(s\)", re.IGNORECASE
)
_HXT_FAILURE = re.compile(r"HXT 3D mesh failed", re.IGNORECASE)


class TopologySmokeMeshError(RuntimeError):
    """Raised after a topology-smoke failure has been recorded fail-closed."""


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
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise TopologySmokeMeshError(f"{label} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise TopologySmokeMeshError(f"{label} must be numeric") from error
    if not math.isfinite(result):
        raise TopologySmokeMeshError(f"{label} contains NaN or Inf")
    return result


def _normalize_local_refinement(
    value: object,
    *,
    characteristic_length: float,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TopologySmokeMeshError("local_refinement must be a mapping")
    expected_keys = {
        "surface_fingerprint_ids",
        "minimum_size_m",
        "distance_max_m",
        "sampling",
    }
    if set(value) != expected_keys:
        raise TopologySmokeMeshError(
            "local_refinement must contain only " + ", ".join(sorted(expected_keys))
        )
    raw_ids = value.get("surface_fingerprint_ids")
    if not isinstance(raw_ids, list) or not 1 <= len(raw_ids) <= 55:
        raise TopologySmokeMeshError(
            "local_refinement.surface_fingerprint_ids must contain 1 to 55 IDs"
        )
    fingerprint_ids: list[str] = []
    for index, raw in enumerate(raw_ids):
        if not _is_sha256(raw):
            raise TopologySmokeMeshError(
                f"local_refinement surface fingerprint {index} is invalid"
            )
        fingerprint_ids.append(str(raw).casefold())
    if len(set(fingerprint_ids)) != len(fingerprint_ids):
        raise TopologySmokeMeshError(
            "local_refinement surface fingerprints must be unique"
        )
    minimum_size = _finite(
        value.get("minimum_size_m"), "local_refinement.minimum_size_m"
    )
    distance_max = _finite(
        value.get("distance_max_m"), "local_refinement.distance_max_m"
    )
    if not 0.001 <= minimum_size < characteristic_length:
        raise TopologySmokeMeshError(
            "local_refinement.minimum_size_m must be at least 0.001 m and "
            "smaller than characteristic_length_m"
        )
    if not 0.0 < distance_max <= characteristic_length:
        raise TopologySmokeMeshError(
            "local_refinement.distance_max_m must be positive and no greater "
            "than characteristic_length_m"
        )
    sampling = value.get("sampling")
    if (
        isinstance(sampling, bool)
        or not isinstance(sampling, int)
        or not 10 <= sampling <= 1000
    ):
        raise TopologySmokeMeshError(
            "local_refinement.sampling must be an integer between 10 and 1000"
        )
    return {
        "surface_fingerprint_ids": sorted(fingerprint_ids),
        "minimum_size_m": minimum_size,
        "distance_max_m": distance_max,
        "sampling": sampling,
    }


def _vector(value: object, length: int, label: str) -> list[float]:
    if isinstance(value, (str, bytes)):
        raise TopologySmokeMeshError(f"{label} must be a numeric sequence")
    try:
        result = [_finite(item, label) for item in value]  # type: ignore[union-attr]
    except TypeError as error:
        raise TopologySmokeMeshError(f"{label} must be a numeric sequence") from error
    if len(result) != length:
        raise TopologySmokeMeshError(f"{label} must contain {length} values")
    return result


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TopologySmokeMeshError(f"{label} must be a positive integer")
    return value


def _is_read_only(path: Path) -> bool:
    metadata = path.stat()
    attributes = getattr(metadata, "st_file_attributes", None)
    if attributes is not None:
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1))
    writable = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    return not bool(metadata.st_mode & writable)


def _is_reparse(path: Path) -> bool:
    metadata = path.lstat()
    attributes = getattr(metadata, "st_file_attributes", 0)
    return path.is_symlink() or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


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


def _assert_no_stored_tags(value: object, path: str = "marker_config") -> None:
    """Reject runtime entity identifiers anywhere in the persistent contract."""

    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            normalized = key.casefold()
            if normalized in {
                "tag",
                "tags",
                "entity_tag",
                "entity_tags",
                "surface_tag",
                "surface_tags",
                "volume_tag",
                "volume_tags",
            } or normalized.endswith(("_tag_audit", "_tags_audit")):
                raise TopologySmokeMeshError(
                    f"persistent marker contract contains forbidden runtime tag at "
                    f"{path}.{key}"
                )
            _assert_no_stored_tags(child, f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, child in enumerate(value):
            _assert_no_stored_tags(child, f"{path}[{index}]")


def _fingerprint(
    value: object,
    fingerprint_id: object,
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TopologySmokeMeshError(f"{label} fingerprint must be a mapping")
    payload = dict(value)
    if not _is_sha256(fingerprint_id):
        raise TopologySmokeMeshError(f"{label} fingerprint_id is not SHA-256")
    if _canonical_sha256(payload) != str(fingerprint_id).casefold():
        raise TopologySmokeMeshError(f"{label} fingerprint hash does not match payload")
    return payload


def _geometry_payload(value: object, *, dimension: int, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TopologySmokeMeshError(f"{label} geometry must be a mapping")
    payload = dict(value)
    if dimension == 2:
        result = {
            "area_m2": _finite(payload.get("area_m2"), f"{label} area"),
            "centroid_m": _vector(payload.get("centroid_m"), 3, f"{label} centroid"),
            "bounding_box_m": _vector(
                payload.get("bounding_box_m"), 6, f"{label} bounds"
            ),
            "entity_type": str(payload.get("entity_type", "")),
            "adjacent_volume_count": _positive_int(
                payload.get("adjacent_volume_count"), f"{label} adjacency count"
            ),
        }
        if result["area_m2"] <= 0.0 or not result["entity_type"]:
            raise TopologySmokeMeshError(f"{label} surface geometry is incomplete")
        if result["adjacent_volume_count"] != 1:
            raise TopologySmokeMeshError(f"{label} is not an external surface")
        return result
    result = {
        "entity_type": str(payload.get("entity_type", "")),
        "volume_m3": _finite(payload.get("volume_m3"), f"{label} volume"),
        "centroid_m": _vector(payload.get("centroid_m"), 3, f"{label} centroid"),
        "bounding_box_m": _vector(
            payload.get("bounding_box_m"), 6, f"{label} bounds"
        ),
        "boundary_surface_count": _positive_int(
            payload.get("boundary_surface_count"), f"{label} boundary count"
        ),
    }
    if result["volume_m3"] <= 0.0 or not result["entity_type"]:
        raise TopologySmokeMeshError(f"{label} volume geometry is incomplete")
    return result


def _normalize_contract(marker_config: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize the one authoritative ``marker_config.py`` v2 document."""

    if not isinstance(marker_config, Mapping):
        raise TopologySmokeMeshError("marker_config must be a mapping")
    _assert_no_stored_tags(marker_config)
    validated = validate_marker_config_document(marker_config)
    if validated.get("schema") != _MARKER_SCHEMA:
        raise TopologySmokeMeshError("marker_config schema is not cfdpipe.markers.v2")
    pipeline_geometry = validated.get("pipeline_geometry")
    expected = validated.get("topology")
    matching = validated.get("matching")
    markers = validated.get("solver_markers")
    fluid = validated.get("fluid")
    measurement = validated.get("measurement")
    if (
        not isinstance(pipeline_geometry, Mapping)
        or not isinstance(expected, Mapping)
        or not isinstance(matching, Mapping)
        or not isinstance(markers, list)
        or not isinstance(fluid, Mapping)
        or not isinstance(measurement, Mapping)
    ):
        raise TopologySmokeMeshError("marker_config is missing required tables")
    derived_hash = pipeline_geometry.get("sha256")
    if not _is_sha256(derived_hash):
        raise TopologySmokeMeshError("pipeline_geometry.sha256 is invalid")
    if (
        str(pipeline_geometry.get("format", "")).casefold() != "brep"
        or pipeline_geometry.get("pipeline_eligible") is not True
        or pipeline_geometry.get("read_only") is not True
    ):
        raise TopologySmokeMeshError("pipeline geometry is not an eligible read-only BREP")
    counts = {
        name: _positive_int(expected.get(name), f"expected {name}")
        for name in (
            "volume_count",
            "unique_surface_count",
            "external_surface_count",
            "shared_surface_count",
        )
    }
    required_counts = {
        "volume_count": 2,
        "unique_surface_count": 57,
        "external_surface_count": 55,
        "shared_surface_count": 2,
    }
    if counts != required_counts:
        raise TopologySmokeMeshError(
            f"marker contract topology counts differ from {required_counts}"
        )
    area_relative = _finite(
        matching.get("surface_area_relative_tolerance"), "area relative tolerance"
    )
    area_absolute = _finite(
        matching.get("surface_area_absolute_tolerance_m2"), "area absolute tolerance"
    )
    coordinate_absolute = _finite(
        matching.get("coordinate_absolute_tolerance_m"),
        "coordinate absolute tolerance",
    )
    if (
        not 0.0 < area_relative <= _MAX_AREA_REL_TOL
        or not 0.0 < area_absolute <= _MAX_AREA_ABS_TOL_M2
        or not 0.0 < coordinate_absolute <= _MAX_COORD_TOL_M
        or matching.get("unique_match_required") is not True
        or matching.get("runtime_tags_are_matching_criteria") is not False
    ):
        raise TopologySmokeMeshError("marker matching policy is unsafe or too permissive")

    surface_groups: list[dict[str, Any]] = []
    physical_names: set[str] = set()
    member_ids: set[str] = set()
    for index, raw in enumerate(markers):
        if not isinstance(raw, Mapping):
            raise TopologySmokeMeshError(f"solver marker {index} must be a mapping")
        specification = dict(raw)
        name = str(specification.get("physical_name", ""))
        role = str(specification.get("semantic_role", ""))
        dimension = specification.get("dimension")
        if not _SAFE_NAME.fullmatch(name) or name in physical_names or not role:
            raise TopologySmokeMeshError(
                f"solver marker {index} has an unsafe or duplicate name"
            )
        physical_names.add(name)
        members = specification.get("members")
        member_count = specification.get("member_count")
        if not isinstance(members, list) or member_count != len(members) or not members:
            raise TopologySmokeMeshError(f"marker {name!r} has inconsistent members")
        normalized_members: list[dict[str, Any]] = []
        if (
            dimension != 2
            or specification.get("solver_boundary") is not True
            or specification.get("create_physical_group") is not True
            or not _is_sha256(specification.get("group_fingerprint_id"))
        ):
            raise TopologySmokeMeshError(f"surface marker {name!r} policy is invalid")
        configured_ids = specification.get("member_fingerprint_ids")
        if not isinstance(configured_ids, list):
            raise TopologySmokeMeshError(f"marker {name!r} member IDs are missing")
        parsed_ids: list[str] = []
        for member_index, member in enumerate(members):
            if not isinstance(member, Mapping):
                raise TopologySmokeMeshError(f"marker {name!r} member is invalid")
            fingerprint_id = member.get("fingerprint_id")
            if not _is_sha256(fingerprint_id):
                raise TopologySmokeMeshError(
                    f"marker {name!r} member {member_index} fingerprint is invalid"
                )
            normalized_id = str(fingerprint_id).casefold()
            if normalized_id in member_ids:
                raise TopologySmokeMeshError("surface fingerprint belongs to two markers")
            member_ids.add(normalized_id)
            parsed_ids.append(normalized_id)
            normalized_members.append(
                {
                    "fingerprint_id": normalized_id,
                    "geometry": _geometry_payload(
                        member,
                        dimension=2,
                        label=f"marker {name!r} member {member_index}",
                    ),
                }
            )
        if sorted(str(value).casefold() for value in configured_ids) != sorted(parsed_ids):
            raise TopologySmokeMeshError(f"marker {name!r} member list is inconsistent")
        surface_groups.append(
            {
                "physical_name": name,
                "role": role,
                "group_fingerprint_id": str(
                    specification.get("group_fingerprint_id")
                ).casefold(),
                "members": normalized_members,
            }
        )
    if len(surface_groups) != 4 or len(member_ids) != 55:
        raise TopologySmokeMeshError(
            "marker contract must contain four surface groups covering 55 members"
        )

    selectors = fluid.get("volume_selectors")
    if (
        fluid.get("physical_name") != "fluid"
        or fluid.get("dimension") != 3
        or fluid.get("solver_boundary") is not False
        or fluid.get("create_physical_group") is not True
        or fluid.get("selector_policy")
        != "canonical_volume_geometry_unique_match"
        or fluid.get("volume_count") != 2
        or not isinstance(selectors, list)
        or len(selectors) != 2
    ):
        raise TopologySmokeMeshError("fluid group policy is invalid")
    normalized_volume_members: list[dict[str, Any]] = []
    volume_ids: set[str] = set()
    for index, selector in enumerate(selectors):
        if not isinstance(selector, Mapping):
            raise TopologySmokeMeshError("fluid selector is invalid")
        fingerprint_id = selector.get("fingerprint_id")
        payload = {key: value for key, value in selector.items() if key != "fingerprint_id"}
        if (
            not _is_sha256(fingerprint_id)
            or _canonical_sha256(payload) != str(fingerprint_id).casefold()
            or selector.get("schema") != "cfdpipe.volume_geometry_selector.v1"
            or selector.get("derived_cad_sha256") != str(derived_hash).casefold()
        ):
            raise TopologySmokeMeshError(f"fluid selector {index} fingerprint is invalid")
        normalized_id = str(fingerprint_id).casefold()
        if normalized_id in volume_ids:
            raise TopologySmokeMeshError("fluid volume selectors overlap")
        volume_ids.add(normalized_id)
        normalized_volume_members.append(
            {
                "fingerprint_id": normalized_id,
                "geometry": _geometry_payload(
                    selector, dimension=3, label=f"fluid selector {index}"
                ),
            }
        )
    fluid_group = {
        "physical_name": "fluid",
        "role": str(fluid.get("semantic_role", "fluid")),
        "members": normalized_volume_members,
    }

    if (
        measurement.get("solver_boundary") is not False
        or measurement.get("create_physical_group") is not False
        or measurement.get("postprocess_only") is not True
        or measurement.get("kind") != "measurement_surface"
        or not _is_sha256(measurement.get("fingerprint_id"))
    ):
        raise TopologySmokeMeshError("measurement must remain outside mesh markers")
    return {
        "derived_sha256": str(derived_hash).casefold(),
        "pipeline_cad_path": Path(pipeline_geometry.get("path", "")),
        "counts": counts,
        "area_relative": area_relative,
        "area_absolute": area_absolute,
        "coordinate_absolute": coordinate_absolute,
        "surface_groups": surface_groups,
        "fluid_group": fluid_group,
        "measurement_role": str(measurement.get("semantic_role", "")),
    }


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


def _close_vector(first: Sequence[float], second: Sequence[float], tolerance: float) -> bool:
    return len(first) == len(second) and all(
        abs(float(left) - float(right)) <= tolerance
        for left, right in zip(first, second)
    )


def _surface_matches(
    geometry: Mapping[str, Any],
    surface: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> bool:
    return (
        str(surface.get("entity_type", "")) == geometry["entity_type"]
        and len(surface.get("adjacent_volumes", [])) == 1
        and _close_scalar(
            float(geometry["area_m2"]),
            float(surface["area_m2"]),
            relative=float(contract["area_relative"]),
            absolute=float(contract["area_absolute"]),
        )
        and _close_vector(
            geometry["centroid_m"],
            surface["centroid_m"],
            float(contract["coordinate_absolute"]),
        )
        and _close_vector(
            geometry["bounding_box_m"],
            surface["bounding_box_m"],
            float(contract["coordinate_absolute"]),
        )
    )


def _volume_matches(
    geometry: Mapping[str, Any],
    volume: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> bool:
    boundary = [int(value) for value in volume["boundary_surfaces"]]
    return (
        str(volume.get("entity_type", "")) == geometry["entity_type"]
        and len(boundary) == int(geometry["boundary_surface_count"])
        and _close_scalar(
            float(geometry["volume_m3"]),
            float(volume["volume_m3"]),
            relative=float(contract["area_relative"]),
            absolute=float(contract["area_absolute"]),
        )
        and _close_vector(
            geometry["centroid_m"],
            volume["centroid_m"],
            float(contract["coordinate_absolute"]),
        )
        and _close_vector(
            geometry["bounding_box_m"],
            volume["bounding_box_m"],
            float(contract["coordinate_absolute"]),
        )
    )


def _one_perfect_matching(
    candidate_sets: Mapping[str, set[int]],
    *,
    label: str,
    forbidden_edge: tuple[str, int] | None = None,
) -> dict[str, int] | None:
    owner_by_entity: dict[int, str] = {}

    def assign(identity: str, visited: set[int]) -> bool:
        for entity in sorted(candidate_sets[identity]):
            if forbidden_edge == (identity, entity) or entity in visited:
                continue
            visited.add(entity)
            owner = owner_by_entity.get(entity)
            if owner is None or assign(owner, visited):
                owner_by_entity[entity] = identity
                return True
        return False

    for identity in sorted(candidate_sets, key=lambda item: len(candidate_sets[item])):
        if not assign(identity, set()):
            return None
    result = {identity: entity for entity, identity in owner_by_entity.items()}
    if len(result) != len(candidate_sets):
        raise TopologySmokeMeshError(f"internal {label} matching error")
    return result


def _unique_global_matching(
    candidate_sets: Mapping[str, set[int]],
    expected_entities: set[int],
    *,
    label: str,
) -> dict[str, int]:
    if any(not candidates for candidates in candidate_sets.values()):
        missing = sorted(identity for identity, values in candidate_sets.items() if not values)
        raise TopologySmokeMeshError(f"{label} has zero matches for {missing[:3]}")
    if len(candidate_sets) != len(expected_entities):
        raise TopologySmokeMeshError(f"{label} does not exhaust current entities")
    matching = _one_perfect_matching(candidate_sets, label=label)
    if matching is None or set(matching.values()) != expected_entities:
        raise TopologySmokeMeshError(f"{label} has no complete global matching")
    for identity, entity in matching.items():
        if _one_perfect_matching(
            candidate_sets,
            label=label,
            forbidden_edge=(identity, entity),
        ) is not None:
            raise TopologySmokeMeshError(f"{label} matching is ambiguous")
    return matching


def _resolve_entities(
    catalog: Mapping[str, Any], contract: Mapping[str, Any]
) -> dict[str, Any]:
    topology = _validate_shared_topology(catalog)
    if (
        len(catalog["volumes"]) != contract["counts"]["volume_count"]
        or len(catalog["surfaces"]) != contract["counts"]["unique_surface_count"]
        or topology["external_surface_count"]
        != contract["counts"]["external_surface_count"]
        or topology["shared_patch_count"]
        != contract["counts"]["shared_surface_count"]
    ):
        raise TopologySmokeMeshError("fresh BREP topology differs from marker contract")
    surfaces_by_tag = {
        int(item["entity_tag"]): item for item in catalog["surfaces"]
    }
    external_tags = set(int(value) for value in topology["external_surface_tags_audit"])
    surface_candidates: dict[str, set[int]] = {}
    group_by_member: dict[str, str] = {}
    role_by_member: dict[str, str] = {}
    geometry_by_member: dict[str, dict[str, Any]] = {}
    for group in contract["surface_groups"]:
        for member in group["members"]:
            identity = member["fingerprint_id"]
            group_by_member[identity] = group["physical_name"]
            role_by_member[identity] = group["role"]
            geometry_by_member[identity] = dict(member["geometry"])
            surface_candidates[identity] = {
                tag
                for tag in external_tags
                if _surface_matches(member["geometry"], surfaces_by_tag[tag], contract)
            }
    surface_matching = _unique_global_matching(
        surface_candidates, external_tags, label="surface fingerprint"
    )
    surface_diagnostic_index = {
        int(entity_tag): {
            "fingerprint_id": identity,
            "physical_name": group_by_member[identity],
            "semantic_role": role_by_member[identity],
            "geometry": geometry_by_member[identity],
        }
        for identity, entity_tag in surface_matching.items()
    }
    groups: dict[str, dict[str, Any]] = {}
    for group in contract["surface_groups"]:
        identities = [item["fingerprint_id"] for item in group["members"]]
        groups[group["physical_name"]] = {
            "dimension": 2,
            "role": group["role"],
            "entity_tags": sorted(surface_matching[item] for item in identities),
            "fingerprint_ids": sorted(identities),
            "group_fingerprint_id": group["group_fingerprint_id"],
        }
    volumes_by_tag = {int(item["entity_tag"]): item for item in catalog["volumes"]}
    volume_candidates: dict[str, set[int]] = {}
    for member in contract["fluid_group"]["members"]:
        volume_candidates[member["fingerprint_id"]] = {
            tag
            for tag, volume in volumes_by_tag.items()
            if _volume_matches(member["geometry"], volume, contract)
        }
    volume_matching = _unique_global_matching(
        volume_candidates, set(volumes_by_tag), label="volume fingerprint"
    )
    fluid_name = contract["fluid_group"]["physical_name"]
    groups[fluid_name] = {
        "dimension": 3,
        "role": contract["fluid_group"]["role"],
        "entity_tags": sorted(volume_matching.values()),
        "fingerprint_ids": sorted(volume_matching),
    }
    if set().union(
        *(set(value["entity_tags"]) for value in groups.values() if value["dimension"] == 2)
    ) != external_tags:
        raise TopologySmokeMeshError("surface Physical Groups do not cover externals")
    return {
        "physical_groups": groups,
        "measurement": {
            "role": contract["measurement_role"],
            "create_physical_group": False,
        },
        "topology": topology,
        "surface_diagnostic_index": surface_diagnostic_index,
    }


def _strict_index(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise TopologySmokeMeshError(f"{label} must be an integer")
    try:
        return int(operator.index(value))  # type: ignore[arg-type]
    except TypeError as error:
        raise TopologySmokeMeshError(f"{label} must be an integer") from error


def _iterable_values(value: object, label: str) -> list[object]:
    if isinstance(value, (str, bytes, Mapping)):
        raise TopologySmokeMeshError(f"{label} must be a non-string iterable")
    try:
        return list(iter(value))  # type: ignore[arg-type]
    except TypeError as error:
        raise TopologySmokeMeshError(f"{label} must be iterable") from error


def _normalize_last_entity_errors(value: object) -> list[list[int]]:
    result: set[tuple[int, int]] = set()
    for index, raw in enumerate(_iterable_values(value, "last entity errors")):
        pair = _iterable_values(raw, f"last entity error {index}")
        if len(pair) != 2:
            raise TopologySmokeMeshError(
                f"last entity error {index} must contain dimension and tag"
            )
        dimension = _strict_index(pair[0], f"last entity error {index} dimension")
        tag = _strict_index(pair[1], f"last entity error {index} tag")
        if not 0 <= dimension <= 3 or tag <= 0:
            raise TopologySmokeMeshError(
                f"last entity error {index} has an invalid dimension or tag"
            )
        result.add((dimension, tag))
    return [[dimension, tag] for dimension, tag in sorted(result)]


def _normalize_last_node_errors(value: object) -> list[int]:
    result: set[int] = set()
    for index, raw in enumerate(_iterable_values(value, "last node errors")):
        tag = _strict_index(raw, f"last node error {index}")
        if tag <= 0:
            raise TopologySmokeMeshError(
                f"last node error {index} must be a positive tag"
            )
        result.add(tag)
    return sorted(result)


def _configure_local_refinement(
    gmsh: Any,
    resolved: Mapping[str, Any],
    refinement: Mapping[str, Any],
    *,
    characteristic_length: float,
) -> dict[str, Any]:
    diagnostic_index = resolved.get("surface_diagnostic_index")
    if not isinstance(diagnostic_index, Mapping):
        raise TopologySmokeMeshError("resolved surface diagnostic index is missing")
    tag_by_fingerprint: dict[str, int] = {}
    physical_names: set[str] = set()
    for raw_tag, raw_record in diagnostic_index.items():
        if not isinstance(raw_record, Mapping):
            raise TopologySmokeMeshError("surface diagnostic record is invalid")
        fingerprint = raw_record.get("fingerprint_id")
        if not _is_sha256(fingerprint):
            raise TopologySmokeMeshError("resolved surface fingerprint is invalid")
        normalized = str(fingerprint).casefold()
        if normalized in tag_by_fingerprint:
            raise TopologySmokeMeshError(
                "resolved surface fingerprint unexpectedly matches multiple entities"
            )
        tag_by_fingerprint[normalized] = _strict_index(
            raw_tag, "resolved surface runtime tag"
        )

    requested = [str(value).casefold() for value in refinement["surface_fingerprint_ids"]]
    missing = sorted(set(requested) - set(tag_by_fingerprint))
    if missing:
        raise TopologySmokeMeshError(
            "local_refinement has zero matches for stable surface fingerprints "
            + ", ".join(missing[:3])
        )
    surface_tags = sorted(tag_by_fingerprint[value] for value in requested)
    curve_tags: set[int] = set()
    for surface_tag in surface_tags:
        record = diagnostic_index[surface_tag]
        physical_names.add(str(record.get("physical_name", "")))
        raw_adjacencies = gmsh.model.getAdjacencies(2, surface_tag)
        if not isinstance(raw_adjacencies, Sequence) or len(raw_adjacencies) != 2:
            raise TopologySmokeMeshError(
                f"surface {surface_tag} returned invalid Gmsh adjacencies"
            )
        for raw_curve in _iterable_values(
            raw_adjacencies[1], f"surface {surface_tag} boundary curves"
        ):
            curve_tag = _strict_index(
                raw_curve, f"surface {surface_tag} boundary curve"
            )
            if curve_tag <= 0:
                raise TopologySmokeMeshError("boundary curve tag must be positive")
            curve_tags.add(curve_tag)
    if not curve_tags:
        raise TopologySmokeMeshError(
            "local_refinement selected surfaces have no boundary curves"
        )

    field_api = getattr(gmsh.model.mesh, "field", None)
    if field_api is None:
        raise TopologySmokeMeshError("Gmsh mesh field API is unavailable")
    distance_field = _strict_index(
        field_api.add("Distance"), "Distance field runtime tag"
    )
    field_api.setNumbers(distance_field, "CurvesList", sorted(curve_tags))
    field_api.setNumber(distance_field, "Sampling", int(refinement["sampling"]))
    threshold_field = _strict_index(
        field_api.add("Threshold"), "Threshold field runtime tag"
    )
    field_api.setNumber(threshold_field, "InField", distance_field)
    field_api.setNumber(
        threshold_field, "SizeMin", float(refinement["minimum_size_m"])
    )
    field_api.setNumber(threshold_field, "SizeMax", characteristic_length)
    field_api.setNumber(threshold_field, "DistMin", 0.0)
    field_api.setNumber(
        threshold_field, "DistMax", float(refinement["distance_max_m"])
    )
    field_api.setAsBackgroundMesh(threshold_field)
    return {
        **dict(refinement),
        "physical_names": sorted(physical_names),
        "resolved_surface_tags_audit": surface_tags,
        "resolved_curve_tags_audit": sorted(curve_tags),
        "distance_field_tag_audit": distance_field,
        "threshold_field_tag_audit": threshold_field,
        "runtime_tags_are_matching_criteria": False,
    }


def _stable_surface_record(
    surface_tag: int, surface_index: Mapping[int, Mapping[str, Any]]
) -> dict[str, Any] | None:
    record = surface_index.get(surface_tag)
    if record is None:
        return None
    return {
        "fingerprint_id": str(record["fingerprint_id"]),
        "physical_name": str(record["physical_name"]),
        "semantic_role": str(record["semantic_role"]),
        "runtime_entity_tag_audit": surface_tag,
        "geometry": dict(record["geometry"]),
    }


def _mesh_failure_diagnostics(
    messages: Sequence[str],
    surface_index: Mapping[int, Mapping[str, Any]],
    last_entity_errors: Sequence[Sequence[int]],
) -> dict[str, Any]:
    """Convert fatal Gmsh PLC evidence into stable, tag-free repair identities."""

    conditions: list[dict[str, Any]] = []
    overlapping_line_indices: set[int] = set()

    def append_condition(
        code: str,
        runtime_tags: Sequence[int],
        raw_lines: Sequence[str],
        *,
        facet_nodes: Mapping[str, Sequence[int]] | None = None,
        dihedral_degrees: float | None = None,
        missing_facet_count: int | None = None,
    ) -> None:
        tags = sorted(set(int(tag) for tag in runtime_tags if int(tag) > 0))
        stable = [
            record
            for tag in tags
            if (record := _stable_surface_record(tag, surface_index)) is not None
        ]
        if tags and len(stable) == len(tags):
            localization = "RESOLVED"
        elif stable:
            localization = "PARTIAL"
        else:
            localization = "UNRESOLVED"
        item: dict[str, Any] = {
            "code": code,
            "localization_status": localization,
            "runtime_surface_tags_audit": tags,
            "stable_surfaces": stable,
            "raw_log_lines": list(dict.fromkeys(str(line) for line in raw_lines)),
        }
        if facet_nodes:
            item["facet_node_tags_audit"] = {
                name: [int(tag) for tag in values]
                for name, values in facet_nodes.items()
            }
        if dihedral_degrees is not None:
            item["dihedral_angle_degrees"] = dihedral_degrees
        if missing_facet_count is not None:
            item["missing_facet_count"] = missing_facet_count
        conditions.append(item)

    for line_index, line in enumerate(messages):
        pair = _OVERLAPPING_FACETS.search(line)
        if pair is None:
            continue
        overlapping_line_indices.add(line_index)
        window_start = max(0, line_index - 8)
        window = [str(value) for value in messages[window_start : line_index + 1]]
        facet_nodes: dict[str, list[int]] = {}
        dihedral: float | None = None
        for context_line in window:
            facet = _FACET_RECORD.search(context_line)
            if facet is not None:
                try:
                    facet_nodes[facet.group(1).casefold()] = [
                        int(token.strip())
                        for token in facet.group(2).split(",")
                        if token.strip()
                    ]
                except ValueError:
                    facet_nodes = {}
            angle = _DIHEDRAL_DEGREES.search(context_line)
            if angle is not None:
                try:
                    value = float(angle.group(1))
                    if math.isfinite(value) and value >= 0.0:
                        dihedral = value
                except ValueError:
                    pass
        append_condition(
            "OVERLAPPING_FACETS",
            [int(pair.group(1)), int(pair.group(2))],
            window,
            facet_nodes=facet_nodes,
            dihedral_degrees=dihedral,
        )

    if not overlapping_line_indices:
        for line_index, line in enumerate(messages):
            if not _NEAR_SELF_INTERSECTION.search(line):
                continue
            window = [str(value) for value in messages[line_index : line_index + 6]]
            tags: list[int] = []
            facet_nodes: dict[str, list[int]] = {}
            dihedral: float | None = None
            for context_line in window:
                facet = _FACET_RECORD.search(context_line)
                if facet is not None:
                    tags.append(int(facet.group(3)))
                    try:
                        facet_nodes[facet.group(1).casefold()] = [
                            int(token.strip())
                            for token in facet.group(2).split(",")
                            if token.strip()
                        ]
                    except ValueError:
                        facet_nodes = {}
                angle = _DIHEDRAL_DEGREES.search(context_line)
                if angle is not None:
                    try:
                        value = float(angle.group(1))
                        if math.isfinite(value) and value >= 0.0:
                            dihedral = value
                    except ValueError:
                        pass
            append_condition(
                "NEAR_SELF_INTERSECTION",
                tags,
                window,
                facet_nodes=facet_nodes,
                dihedral_degrees=dihedral,
            )

    last_surface_tags = sorted(
        {
            int(item[1])
            for item in last_entity_errors
            if len(item) == 2 and int(item[0]) == 2 and int(item[1]) > 0
        }
    )
    for line in messages:
        if _PLC_INTERSECTION.search(line):
            append_condition(
                "PLC_SEGMENT_FACET_INTERSECTION", last_surface_tags, [line]
            )

    missing_records = [
        (line, match)
        for line in messages
        if (match := _MISSING_FACETS.search(line)) is not None
    ]
    hxt_lines = [line for line in messages if _HXT_FAILURE.search(line)]
    if missing_records and hxt_lines:
        line, match = missing_records[-1]
        append_condition(
            "UNRECOVERED_BOUNDARY_FACETS",
            last_surface_tags,
            [line, hxt_lines[-1]],
            missing_facet_count=int(match.group(1)),
        )

    stable_fingerprints = sorted(
        {
            str(surface["fingerprint_id"])
            for condition in conditions
            for surface in condition["stable_surfaces"]
        }
    )
    localizations = {condition["localization_status"] for condition in conditions}
    if not conditions:
        overall_localization = "NOT_APPLICABLE"
    elif localizations == {"RESOLVED"}:
        overall_localization = "RESOLVED"
    elif "PARTIAL" in localizations or "RESOLVED" in localizations:
        overall_localization = "PARTIAL"
    else:
        overall_localization = "UNRESOLVED"
    return {
        "fatal": bool(conditions),
        "classification": "CAD_BOUNDARY_PLC_FAILURE" if conditions else None,
        "localization_status": overall_localization,
        "stable_surface_fingerprints": stable_fingerprints,
        "conditions": conditions,
    }


def _cad_repair_request(manifest: Mapping[str, Any]) -> dict[str, Any]:
    diagnostics = manifest["mesh_failure_diagnostics"]
    return {
        "schema": "cfdpipe.cad_repair_request.v1",
        "status": "CAD_REPAIR_REQUIRED",
        "production_mesh_eligible": False,
        "source_brep": manifest.get("source_after") or manifest.get("source_before"),
        "gmsh_version": manifest.get("gmsh_version"),
        "gmsh_module_path": manifest.get("gmsh_module_path"),
        "mesh_options": manifest.get("mesh_options"),
        "classification": diagnostics["classification"],
        "localization_status": diagnostics["localization_status"],
        "stable_surface_fingerprints": diagnostics["stable_surface_fingerprints"],
        "conditions": diagnostics["conditions"],
        "last_mesh_error_entities": manifest.get("last_mesh_error_entities", []),
        "last_mesh_error_nodes": manifest.get("last_mesh_error_nodes", []),
        "repair_requirements": [
            "Repair the reported self-intersecting, overlapping or unrecoverable CAD boundary without changing solver-boundary semantics.",
            "Write repaired CAD only below geometry/derived; keep the source BREP and STEP read-only.",
            "Re-run topology, geometry-equivalence, shared-interface and marker fingerprint validation after any topology change.",
            "Require both fluid volumes to contain positive tetrahedra and all solver marker groups to be nonempty before SU2.",
        ],
        "constraints": {
            "source_read_only": True,
            "repair_output_must_be_derived": True,
            "runtime_tags_are_audit_only": True,
            "marker_rematch_required_after_topology_change": True,
            "must_not_merge_oppositely_oriented_wall_faces_without_authorization": True,
            "must_not_run_su2_or_paraview_until_mesh_passes": True,
        },
    }


def _element_blocks(gmsh: Any, dimension: int, tag: int = -1) -> list[dict[str, Any]]:
    element_types, element_tags, node_tags = gmsh.model.mesh.getElements(dimension, tag)
    if not (len(element_types) == len(element_tags) == len(node_tags)):
        raise TopologySmokeMeshError("Gmsh returned inconsistent element blocks")
    blocks: list[dict[str, Any]] = []
    for raw_type, raw_elements, raw_nodes in zip(
        element_types, element_tags, node_tags
    ):
        element_type = int(raw_type)
        tags = [int(value) for value in raw_elements]
        nodes = [int(value) for value in raw_nodes]
        properties = gmsh.model.mesh.getElementProperties(element_type)
        name = str(properties[0])
        element_dimension = int(properties[1])
        order = int(properties[2])
        node_count = int(properties[3])
        primary_count = int(properties[5])
        if element_dimension != dimension or order != 1:
            raise TopologySmokeMeshError(
                f"mesh contains non-first-order {name} in dimension {dimension}"
            )
        expected_nodes = 4 if dimension == 3 else 3
        if node_count != expected_nodes or primary_count != expected_nodes:
            raise TopologySmokeMeshError(
                f"mesh must contain only linear {'tetrahedra' if dimension == 3 else 'triangles'}; got {name}"
            )
        if len(nodes) != len(tags) * node_count or any(value <= 0 for value in tags + nodes):
            raise TopologySmokeMeshError(f"invalid {name} connectivity")
        blocks.append(
            {
                "element_type": element_type,
                "name": name,
                "element_tags": tags,
                "node_tags": nodes,
            }
        )
    return blocks


def _tags_from_blocks(blocks: Sequence[Mapping[str, Any]]) -> set[int]:
    result: set[int] = set()
    for block in blocks:
        for value in block["element_tags"]:
            tag = int(value)
            if tag in result:
                raise TopologySmokeMeshError("mesh repeats an element tag")
            result.add(tag)
    return result


def _connectivity_from_blocks(
    blocks: Sequence[Mapping[str, Any]], node_count: int
) -> dict[int, tuple[int, ...]]:
    """Return element connectivity while rejecting duplicate element identities."""

    result: dict[int, tuple[int, ...]] = {}
    for block in blocks:
        element_tags = [int(value) for value in block["element_tags"]]
        node_tags = [int(value) for value in block["node_tags"]]
        if len(node_tags) != len(element_tags) * node_count:
            raise TopologySmokeMeshError("mesh element connectivity is inconsistent")
        for index, element_tag in enumerate(element_tags):
            if element_tag in result:
                raise TopologySmokeMeshError("mesh repeats an element tag")
            connectivity = tuple(
                node_tags[index * node_count : (index + 1) * node_count]
            )
            if len(set(connectivity)) != node_count:
                raise TopologySmokeMeshError(
                    f"element {element_tag} has repeated node references"
                )
            result[element_tag] = connectivity
    return result


def _tetrahedron_faces(nodes: Sequence[int]) -> tuple[tuple[int, int, int], ...]:
    if len(nodes) != 4:
        raise TopologySmokeMeshError("tetrahedron connectivity must have four nodes")
    return tuple(
        tuple(sorted(face))
        for face in (
            (int(nodes[0]), int(nodes[1]), int(nodes[2])),
            (int(nodes[0]), int(nodes[1]), int(nodes[3])),
            (int(nodes[0]), int(nodes[2]), int(nodes[3])),
            (int(nodes[1]), int(nodes[2]), int(nodes[3])),
        )
    )


def _element_type_counts(blocks: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for block in blocks:
        name = str(block["name"])
        result[name] = result.get(name, 0) + len(block["element_tags"])
    return result


def _quality(gmsh: Any, element_tags: Sequence[int]) -> dict[str, Any]:
    metrics: dict[str, list[float]] = {}
    for name in ("minDetJac", "minSICN", "volume"):
        values = [
            _finite(value, f"mesh quality {name}")
            for value in gmsh.model.mesh.getElementQualities(element_tags, name)
        ]
        if len(values) != len(element_tags):
            raise TopologySmokeMeshError(f"mesh quality {name} count is inconsistent")
        metrics[name] = values
    nonpositive = {
        name: sum(value <= 0.0 for value in values)
        for name, values in metrics.items()
    }
    if any(nonpositive.values()):
        raise TopologySmokeMeshError(
            f"mesh contains non-positive Jacobian/quality/volume: {nonpositive}"
        )
    return {
        "minimum_determinant_jacobian": min(metrics["minDetJac"]),
        "minimum_signed_inverse_condition_number": min(metrics["minSICN"]),
        "minimum_element_volume_m3": min(metrics["volume"]),
        "maximum_equivalent_shape_distortion": 1.0 - min(metrics["minSICN"]),
        "equivalent_shape_metric": "1-minSICN",
        "nonpositive_determinant_jacobian_count": nonpositive["minDetJac"],
        "nonpositive_signed_quality_count": nonpositive["minSICN"],
        "nonpositive_volume_count": nonpositive["volume"],
    }


def _inspect_mesh(
    gmsh: Any,
    resolved: Mapping[str, Any],
    *,
    max_elements: int,
) -> dict[str, Any]:
    node_tags_raw, coordinates_raw, _ = gmsh.model.mesh.getNodes()
    node_tags = [int(value) for value in node_tags_raw]
    coordinates = [_finite(value, "mesh node coordinate") for value in coordinates_raw]
    if not node_tags or len(set(node_tags)) != len(node_tags) or len(coordinates) != 3 * len(node_tags):
        raise TopologySmokeMeshError("mesh nodes are empty or inconsistent")
    volume_blocks = _element_blocks(gmsh, 3)
    surface_blocks = _element_blocks(gmsh, 2)
    volume_connectivity = _connectivity_from_blocks(volume_blocks, 4)
    surface_connectivity = _connectivity_from_blocks(surface_blocks, 3)
    volume_elements = set(volume_connectivity)
    surface_elements = set(surface_connectivity)
    if not volume_elements:
        raise TopologySmokeMeshError("Gmsh generated no three-dimensional elements")
    if len(volume_elements) > max_elements:
        raise TopologySmokeMeshError(
            f"3D element count {len(volume_elements)} exceeds cap {max_elements}"
        )
    fluid = next(
        value
        for value in resolved["physical_groups"].values()
        if value["dimension"] == 3
    )
    mesh_node_tags = set(node_tags)
    referenced_nodes = {
        node
        for connectivity in (*volume_connectivity.values(), *surface_connectivity.values())
        for node in connectivity
    }
    if not referenced_nodes.issubset(mesh_node_tags):
        raise TopologySmokeMeshError("element connectivity references an unknown node")

    per_volume: dict[str, int] = {}
    per_volume_connectivity: dict[int, dict[int, tuple[int, ...]]] = {}
    used_volume_elements: set[int] = set()
    for volume_tag in fluid["entity_tags"]:
        current = _connectivity_from_blocks(
            _element_blocks(gmsh, 3, volume_tag), 4
        )
        tags = set(current)
        if not tags or used_volume_elements.intersection(tags):
            raise TopologySmokeMeshError("fluid volume meshes are empty or overlap")
        if any(
            tag not in volume_connectivity
            or sorted(nodes) != sorted(volume_connectivity[tag])
            for tag, nodes in current.items()
        ):
            raise TopologySmokeMeshError(
                "per-volume tetrahedra differ from the global mesh"
            )
        used_volume_elements.update(tags)
        per_volume_connectivity[int(volume_tag)] = current
        per_volume[str(volume_tag)] = len(tags)
    if used_volume_elements != volume_elements:
        raise TopologySmokeMeshError("fluid group does not exhaust 3D elements")

    face_owners: dict[tuple[int, int, int], list[tuple[int, int]]] = {}
    for volume_tag, connectivity_by_element in per_volume_connectivity.items():
        for element_tag, connectivity in connectivity_by_element.items():
            for face in _tetrahedron_faces(connectivity):
                owners = face_owners.setdefault(face, [])
                owners.append((volume_tag, element_tag))
                if len(owners) > 2:
                    raise TopologySmokeMeshError(
                        "tetrahedral mesh contains a non-manifold face with more than two owners"
                    )
    boundary_face_keys = {
        face for face, owners in face_owners.items() if len(owners) == 1
    }
    cross_volume_face_keys = {
        face
        for face, owners in face_owners.items()
        if len(owners) == 2 and len({owner[0] for owner in owners}) == 2
    }
    fluid_volume_tags = set(per_volume_connectivity)

    def surface_triangles(surface_tag: int) -> dict[int, tuple[int, int, int]]:
        current = _connectivity_from_blocks(
            _element_blocks(gmsh, 2, surface_tag), 3
        )
        if any(
            element_tag not in surface_connectivity
            or sorted(nodes) != sorted(surface_connectivity[element_tag])
            for element_tag, nodes in current.items()
        ):
            raise TopologySmokeMeshError(
                "per-surface triangles differ from the global mesh"
            )
        keys = {tuple(sorted(nodes)) for nodes in current.values()}
        if len(keys) != len(current):
            raise TopologySmokeMeshError(
                f"surface {surface_tag} repeats a triangle face"
            )
        return current

    marker_counts: dict[str, int] = {}
    marker_elements: set[int] = set()
    marker_triangle_keys: set[tuple[int, int, int]] = set()
    for name, group in resolved["physical_groups"].items():
        if group["dimension"] != 2:
            continue
        current: set[int] = set()
        current_keys: set[tuple[int, int, int]] = set()
        for surface_tag in group["entity_tags"]:
            triangles = surface_triangles(surface_tag)
            surface_elements_for_tag = set(triangles)
            triangle_keys = {tuple(sorted(nodes)) for nodes in triangles.values()}
            if current.intersection(surface_elements_for_tag):
                raise TopologySmokeMeshError(
                    f"marker {name!r} repeats a surface mesh element"
                )
            if current_keys.intersection(triangle_keys):
                raise TopologySmokeMeshError(
                    f"marker {name!r} repeats a tetrahedral boundary face"
                )
            current.update(surface_elements_for_tag)
            current_keys.update(triangle_keys)
        if (
            not current
            or marker_elements.intersection(current)
            or marker_triangle_keys.intersection(current_keys)
        ):
            raise TopologySmokeMeshError(f"marker {name!r} is empty or overlaps")
        if any(len(face_owners.get(key, [])) != 1 for key in current_keys):
            raise TopologySmokeMeshError(
                f"marker {name!r} contains a triangle without exactly one tetrahedron owner"
            )
        marker_elements.update(current)
        marker_triangle_keys.update(current_keys)
        marker_counts[name] = len(current)
    shared_elements: set[int] = set()
    shared_triangle_keys: set[tuple[int, int, int]] = set()
    shared_counts: dict[str, int] = {}
    for shared_tag in resolved["topology"]["shared_surface_tags_audit"]:
        triangles = surface_triangles(shared_tag)
        current = set(triangles)
        current_keys = {tuple(sorted(nodes)) for nodes in triangles.values()}
        if (
            not current
            or marker_elements.intersection(current)
            or shared_elements.intersection(current)
            or marker_triangle_keys.intersection(current_keys)
            or shared_triangle_keys.intersection(current_keys)
        ):
            raise TopologySmokeMeshError("shared interface is empty or marked as boundary")
        for key in current_keys:
            owners = face_owners.get(key, [])
            if (
                len(owners) != 2
                or {owner[0] for owner in owners} != fluid_volume_tags
            ):
                raise TopologySmokeMeshError(
                    "shared interface triangle does not have one tetrahedron owner in each fluid volume"
                )
        shared_elements.update(current)
        shared_triangle_keys.update(current_keys)
        shared_counts[str(shared_tag)] = len(current)
    if marker_elements | shared_elements != surface_elements:
        raise TopologySmokeMeshError(
            "marker and shared surface elements do not exhaust the 2D mesh"
        )
    if marker_triangle_keys != boundary_face_keys:
        raise TopologySmokeMeshError(
            "four marker groups do not exactly exhaust tetrahedral external boundary faces"
        )
    if shared_triangle_keys != cross_volume_face_keys:
        raise TopologySmokeMeshError(
            "shared surfaces do not exactly exhaust cross-volume tetrahedral faces"
        )
    element_tags = sorted(volume_elements)
    return {
        "node_count": len(node_tags),
        "element_count_3d": len(volume_elements),
        "element_count_2d": len(surface_elements),
        "element_types_3d": _element_type_counts(volume_blocks),
        "element_types_2d": _element_type_counts(surface_blocks),
        "elements_per_volume_audit": per_volume,
        "boundary_elements_per_marker": marker_counts,
        "shared_interface_elements_audit": shared_counts,
        "connectivity_proof": {
            "unique_tetrahedral_face_count": len(face_owners),
            "external_boundary_face_count": len(boundary_face_keys),
            "marker_triangle_face_count": len(marker_triangle_keys),
            "cross_volume_face_count": len(cross_volume_face_keys),
            "shared_surface_triangle_face_count": len(shared_triangle_keys),
            "external_faces_exactly_exhausted_by_four_markers": True,
            "each_shared_face_has_one_owner_per_fluid_volume": True,
        },
        "quality": _quality(gmsh, element_tags),
        "boundary_layer": {
            "requested": False,
            "actual_layer_count": 0,
            "coverage_fraction": 0.0,
            "scope": "topology-smoke mesh before boundary-layer trials",
        },
    }


def _parse_su2_summary(path: Path, expected_markers: set[str], nelem: int) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise TopologySmokeMeshError("Gmsh did not write a nonempty SU2 mesh")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise TopologySmokeMeshError("SU2 mesh is not UTF-8 text") from error
    match_nonfinite = _NONFINITE_TEXT.search(text)
    if match_nonfinite:
        raise TopologySmokeMeshError(
            f"SU2 mesh contains non-finite token {match_nonfinite.group(0)!r}"
        )
    lines = [
        content
        for raw in text.splitlines()
        if (content := raw.split("%", 1)[0].strip())
    ]
    cursor = 0

    def header(expected: str) -> str:
        nonlocal cursor
        if cursor >= len(lines) or "=" not in lines[cursor]:
            raise TopologySmokeMeshError(f"SU2 output is missing {expected}")
        key, value = (part.strip() for part in lines[cursor].split("=", 1))
        if key != expected or not value:
            raise TopologySmokeMeshError(
                f"SU2 expected {expected} at record {cursor + 1}"
            )
        cursor += 1
        return value

    def integer_header(expected: str) -> int:
        value = header(expected)
        try:
            parsed = int(value)
        except ValueError as error:
            raise TopologySmokeMeshError(f"SU2 {expected} is not an integer") from error
        if parsed < 0:
            raise TopologySmokeMeshError(f"SU2 {expected} is negative")
        return parsed

    ndime = integer_header("NDIME")
    if ndime != 3:
        raise TopologySmokeMeshError("SU2 topology-smoke mesh must have NDIME=3")
    parsed_nelem = integer_header("NELEM")
    if parsed_nelem <= 0 or parsed_nelem != nelem:
        raise TopologySmokeMeshError("SU2 NELEM differs from the Gmsh tetrahedron count")
    volume_node_indices: list[int] = []
    for element_index in range(parsed_nelem):
        if cursor >= len(lines):
            raise TopologySmokeMeshError("SU2 ended inside the NELEM records")
        tokens = lines[cursor].split()
        cursor += 1
        if len(tokens) not in {5, 6}:
            raise TopologySmokeMeshError(
                f"SU2 volume element {element_index} has an invalid field count"
            )
        try:
            integers = [int(value) for value in tokens]
        except ValueError as error:
            raise TopologySmokeMeshError(
                f"SU2 volume element {element_index} is not integral"
            ) from error
        if integers[0] != 10:
            raise TopologySmokeMeshError(
                f"SU2 volume element {element_index} is not a linear tetrahedron (code 10)"
            )
        volume_node_indices.extend(integers[1:5])

    npoin = integer_header("NPOIN")
    if npoin <= 0:
        raise TopologySmokeMeshError("SU2 NPOIN must be positive")
    point_indices: set[int] = set()
    for point_record in range(npoin):
        if cursor >= len(lines):
            raise TopologySmokeMeshError("SU2 ended inside the NPOIN records")
        tokens = lines[cursor].split()
        cursor += 1
        if len(tokens) != ndime + 1:
            raise TopologySmokeMeshError(
                f"SU2 point record {point_record} has an invalid field count"
            )
        for coordinate in tokens[:ndime]:
            _finite(coordinate, f"SU2 point {point_record} coordinate")
        try:
            point_index = int(tokens[ndime])
        except ValueError as error:
            raise TopologySmokeMeshError(
                f"SU2 point record {point_record} has an invalid index"
            ) from error
        if point_index in point_indices:
            raise TopologySmokeMeshError("SU2 point indices are not unique")
        point_indices.add(point_index)
    if point_indices != set(range(npoin)):
        raise TopologySmokeMeshError("SU2 point indices are not contiguous from zero")
    if any(index not in point_indices for index in volume_node_indices):
        raise TopologySmokeMeshError("SU2 tetrahedron references an invalid node index")

    nmark = integer_header("NMARK")
    marker_names: list[str] = []
    marker_element_counts: dict[str, int] = {}
    for marker_index in range(nmark):
        name = header("MARKER_TAG")
        if name in marker_element_counts:
            raise TopologySmokeMeshError(f"SU2 marker {name!r} is duplicated")
        marker_names.append(name)
        count = integer_header("MARKER_ELEMS")
        if count <= 0:
            raise TopologySmokeMeshError(f"SU2 marker {name!r} is empty")
        for element_index in range(count):
            if cursor >= len(lines):
                raise TopologySmokeMeshError(
                    f"SU2 ended inside marker {name!r} records"
                )
            tokens = lines[cursor].split()
            cursor += 1
            if len(tokens) not in {4, 5}:
                raise TopologySmokeMeshError(
                    f"SU2 marker {name!r} element {element_index} has an invalid field count"
                )
            try:
                integers = [int(value) for value in tokens]
            except ValueError as error:
                raise TopologySmokeMeshError(
                    f"SU2 marker {name!r} element {element_index} is not integral"
                ) from error
            if integers[0] != 5:
                raise TopologySmokeMeshError(
                    f"SU2 marker {name!r} element {element_index} is not a triangle (code 5)"
                )
            if any(index not in point_indices for index in integers[1:4]):
                raise TopologySmokeMeshError(
                    f"SU2 marker {name!r} references an invalid node index"
                )
        marker_element_counts[name] = count
    if cursor != len(lines):
        raise TopologySmokeMeshError("SU2 output contains unexpected trailing records")
    if (
        nmark != len(expected_markers)
        or set(marker_names) != expected_markers
        or len(marker_names) != len(set(marker_names))
    ):
        raise TopologySmokeMeshError("SU2 marker names do not exactly match Physical Groups")
    return {
        "ndime": ndime,
        "nelem": parsed_nelem,
        "npoin": npoin,
        "nmark": nmark,
        "markers": sorted(marker_names),
        "marker_element_counts": marker_element_counts,
        "volume_element_code": 10,
        "marker_element_code": 5,
        "node_indices_contiguous_from_zero": True,
    }


def _write_new_json(path: Path, value: Mapping[str, Any]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def _write_new_text(path: Path, value: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(value)


def _remove_staging(path: Path, parent: Path) -> None:
    if not path.exists():
        return
    if path.parent != parent or not path.name.startswith(".topology-smoke-"):
        raise TopologySmokeMeshError(f"refusing to clean unowned staging path {path}")
    shutil.rmtree(path)


def build_topology_smoke_mesh(
    brep_path: PathLike,
    output_directory: PathLike,
    marker_config: Mapping[str, Any],
    *,
    mesh_options: Mapping[str, Any],
    max_elements: int = _MAX_ELEMENT_CAP,
    gmsh_module: Any | None = None,
) -> dict[str, Any]:
    """Generate one bounded tetrahedral topology-smoke mesh from a repaired BREP."""

    if isinstance(max_elements, bool) or not isinstance(max_elements, int):
        raise TopologySmokeMeshError("max_elements must be an integer")
    if not 0 < max_elements <= _MAX_ELEMENT_CAP:
        raise TopologySmokeMeshError(
            f"max_elements must be between 1 and {_MAX_ELEMENT_CAP}"
        )
    if not isinstance(mesh_options, Mapping):
        raise TopologySmokeMeshError("mesh_options must be a mapping")
    characteristic_length = _finite(
        mesh_options.get("characteristic_length_m"), "characteristic_length_m"
    )
    if characteristic_length <= 0.0:
        raise TopologySmokeMeshError("characteristic_length_m must be positive")
    local_refinement = _normalize_local_refinement(
        mesh_options.get("local_refinement"),
        characteristic_length=characteristic_length,
    )
    facet_overlap_tolerance = _finite(
        mesh_options.get("facet_overlap_angle_tolerance_degrees", 0.1),
        "facet_overlap_angle_tolerance_degrees",
    )
    if not 1.0e-6 <= facet_overlap_tolerance <= 0.1:
        raise TopologySmokeMeshError(
            "facet_overlap_angle_tolerance_degrees must be between 1e-6 and "
            "the Gmsh default 0.1 degree"
        )
    algorithm_3d = mesh_options.get("algorithm_3d", 1)
    if (
        isinstance(algorithm_3d, bool)
        or not isinstance(algorithm_3d, int)
        or algorithm_3d not in {1, 4, 10}
    ):
        raise TopologySmokeMeshError(
            "algorithm_3d must be 1 (Delaunay), 4 (Frontal) or 10 (HXT)"
        )

    output = Path(output_directory).expanduser()
    if ".." in output.parts:
        raise TopologySmokeMeshError("output directory must not contain '..'")
    output = Path(os.path.abspath(output))
    if output.exists():
        raise TopologySmokeMeshError(f"refusing to overwrite output directory: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    if _has_reparse_component(output.parent):
        raise TopologySmokeMeshError("output directory has a reparse ancestor")
    staging = output.parent / f".topology-smoke-{os.getpid()}-{uuid4().hex}"
    staging.mkdir()

    started = _utc_now()
    log_lines = [f"[{started}] topology-smoke started"]
    manifest: dict[str, Any] = {
        "schema": _SCHEMA,
        "status": "FAIL",
        "started_at_utc": started,
        "ended_at_utc": None,
        "gmsh_version": None,
        "gmsh_module_path": None,
        "source_before": None,
        "source_after": None,
        "source_unchanged": None,
        "marker_identity_uses_runtime_tags": False,
        "max_elements": max_elements,
        "mesh_options": {
            "characteristic_length_m": characteristic_length,
            "facet_overlap_angle_tolerance_degrees": facet_overlap_tolerance,
            "algorithm_2d": 6,
            "algorithm_3d": algorithm_3d,
            "element_order": 1,
            "recombine": False,
            "boundary_layers": False,
            "local_refinement": (
                None if local_refinement is None else dict(local_refinement)
            ),
        },
        "logger_messages": [],
        "logger_started": False,
        "logger_stopped": False,
        "finalize_called": False,
        "last_mesh_error_entities": [],
        "last_mesh_error_nodes": [],
        "mesh_failure_diagnostics": {
            "fatal": False,
            "classification": None,
            "localization_status": "NOT_APPLICABLE",
            "stable_surface_fingerprints": [],
            "conditions": [],
        },
        "cad_repair_request": {"required": False, "path": None},
        "error": None,
        "secondary_errors": [],
    }
    gmsh: Any | None = None
    initialized = False
    initialization_attempted = False
    logger_started = False
    primary: BaseException | None = None
    primary_traceback = ""
    result_payload: dict[str, Any] | None = None
    source: Path | None = None
    resolved: dict[str, Any] | None = None

    def record_secondary(error: BaseException, *, context: str | None = None) -> None:
        rendered = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        payload = {
            "type": type(error).__name__,
            "message": str(error),
            "traceback": rendered,
        }
        if context is not None:
            payload["context"] = context
        manifest["secondary_errors"].append(payload)
        log_lines.append(rendered)

    try:
        contract = _normalize_contract(marker_config)
        candidate = Path(brep_path).expanduser()
        if not candidate.exists() or _has_reparse_component(candidate):
            raise TopologySmokeMeshError("BREP must be an existing non-reparse file")
        source = candidate.resolve(strict=True)
        configured_path = contract["pipeline_cad_path"].expanduser().resolve(strict=True)
        if source != configured_path or source.suffix.casefold() != ".brep":
            raise TopologySmokeMeshError("BREP path differs from the marker contract")
        if not source.is_file() or not _is_read_only(source):
            raise TopologySmokeMeshError("pipeline BREP must be an ordinary read-only file")
        before = _snapshot(source)
        manifest["source_before"] = before
        if before["sha256"] != contract["derived_sha256"]:
            raise TopologySmokeMeshError("pipeline BREP SHA-256 differs from marker contract")

        gmsh = gmsh_module or importlib.import_module("gmsh")
        manifest["gmsh_version"] = str(getattr(gmsh, "__version__", "unknown"))
        module_file = getattr(gmsh, "__file__", None)
        manifest["gmsh_module_path"] = (
            str(Path(module_file).resolve()) if module_file else "unknown"
        )
        is_initialized = getattr(gmsh, "isInitialized", None)
        if callable(is_initialized) and bool(is_initialized()):
            raise TopologySmokeMeshError("topology smoke requires a fresh Gmsh session")
        initialization_attempted = True
        gmsh.initialize(readConfigFiles=False)
        initialized = True
        logger = getattr(gmsh, "logger", None)
        if logger is not None:
            logger.start()
            logger_started = True
            manifest["logger_started"] = True
        gmsh.model.add("cfdpipe_topology_smoke")
        gmsh.option.setString("Geometry.OCCTargetUnit", "M")
        imported = gmsh.model.occ.importShapes(str(source))
        gmsh.model.occ.synchronize()
        if not imported:
            raise TopologySmokeMeshError("Gmsh imported no BREP entities")
        catalog = _collect_model(gmsh, require_normals=False)
        resolved = _resolve_entities(catalog, contract)
        for name, group in resolved["physical_groups"].items():
            physical_tag = gmsh.model.addPhysicalGroup(
                group["dimension"], group["entity_tags"]
            )
            gmsh.model.setPhysicalName(group["dimension"], physical_tag, name)

        fixed_number_options = {
            "General.NumThreads": 1.0,
            "Mesh.ElementOrder": 1.0,
            "Mesh.RecombineAll": 0.0,
            "Mesh.MeshSizeFromCurvature": 0.0,
            "Mesh.MeshSizeFromPoints": 0.0,
            "Mesh.MeshSizeExtendFromBoundary": 0.0,
            "Mesh.MeshSizeMin": (
                characteristic_length
                if local_refinement is None
                else float(local_refinement["minimum_size_m"])
            ),
            "Mesh.MeshSizeMax": characteristic_length,
            "Mesh.SaveAll": 0.0,
            "Mesh.Binary": 0.0,
            "Mesh.MshFileVersion": 4.1,
            "Mesh.AngleToleranceFacetOverlap": facet_overlap_tolerance,
            "Mesh.Algorithm": 6.0,
            "Mesh.Algorithm3D": float(algorithm_3d),
        }
        for name, value in fixed_number_options.items():
            gmsh.option.setNumber(name, value)
        if local_refinement is not None:
            manifest["mesh_options"]["local_refinement"] = (
                _configure_local_refinement(
                    gmsh,
                    resolved,
                    local_refinement,
                    characteristic_length=characteristic_length,
                )
            )
        gmsh.model.mesh.generate(3)
        mesh = _inspect_mesh(gmsh, resolved, max_elements=max_elements)
        msh_stage = staging / "mesh.msh"
        su2_stage = staging / "mesh.su2"
        gmsh.write(str(msh_stage))
        gmsh.write(str(su2_stage))
        if not msh_stage.is_file() or msh_stage.stat().st_size <= 0:
            raise TopologySmokeMeshError("Gmsh did not write a nonempty MSH file")
        expected_markers = {
            name
            for name, group in resolved["physical_groups"].items()
            if group["dimension"] == 2
        }
        su2_validation = _parse_su2_summary(
            su2_stage, expected_markers, mesh["element_count_3d"]
        )
        result_payload = {
            "resolved_physical_groups_audit": resolved["physical_groups"],
            "measurement": resolved["measurement"],
            "topology": resolved["topology"],
            "mesh": mesh,
            "su2_validation": su2_validation,
            "outputs": {
                "mesh.msh": {
                    "size_bytes": msh_stage.stat().st_size,
                    "sha256": _sha256(msh_stage),
                },
                "mesh.su2": {
                    "size_bytes": su2_stage.stat().st_size,
                    "sha256": _sha256(su2_stage),
                },
            },
        }
    except BaseException as error:
        primary = error
        primary_traceback = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        log_lines.append(primary_traceback)
    finally:
        if gmsh is not None and logger_started:
            try:
                messages = [str(item) for item in gmsh.logger.get()]
                manifest["logger_messages"] = messages
                log_lines.extend(messages)
            except BaseException as error:
                if primary is None:
                    primary = error
                    primary_traceback = "".join(
                        traceback.format_exception(type(error), error, error.__traceback__)
                    )
                else:
                    record_secondary(error)
            try:
                gmsh.logger.stop()
                manifest["logger_stopped"] = True
            except BaseException as error:
                if primary is None:
                    primary = error
                    primary_traceback = "".join(
                        traceback.format_exception(type(error), error, error.__traceback__)
                    )
                else:
                    record_secondary(error)
        if gmsh is not None and initialized:
            mesh_api = getattr(getattr(gmsh, "model", None), "mesh", None)
            entity_query = getattr(mesh_api, "getLastEntityError", None)
            if callable(entity_query):
                try:
                    manifest["last_mesh_error_entities"] = (
                        _normalize_last_entity_errors(entity_query())
                    )
                except BaseException as error:
                    record_secondary(error, context="getLastEntityError")
            node_query = getattr(mesh_api, "getLastNodeError", None)
            if callable(node_query):
                try:
                    manifest["last_mesh_error_nodes"] = _normalize_last_node_errors(
                        node_query()
                    )
                except BaseException as error:
                    record_secondary(error, context="getLastNodeError")
        try:
            surface_index = (
                resolved["surface_diagnostic_index"] if resolved is not None else {}
            )
            diagnostics = _mesh_failure_diagnostics(
                manifest["logger_messages"],
                surface_index,
                manifest["last_mesh_error_entities"],
            )
            manifest["mesh_failure_diagnostics"] = diagnostics
            if diagnostics["fatal"]:
                manifest["cad_repair_request"] = {
                    "required": True,
                    "path": "cad_repair_request.json",
                }
                if primary is None:
                    diagnostic_error = TopologySmokeMeshError(
                        "Gmsh logger reports a fatal CAD boundary PLC failure"
                    )
                    primary = diagnostic_error
                    primary_traceback = "".join(
                        traceback.format_exception_only(
                            type(diagnostic_error), diagnostic_error
                        )
                    )
                    log_lines.append(primary_traceback)
        except BaseException as error:
            if primary is None:
                primary = error
                primary_traceback = "".join(
                    traceback.format_exception(type(error), error, error.__traceback__)
                )
            else:
                record_secondary(error, context="mesh_failure_diagnostics")
        if gmsh is not None and initialized:
            try:
                gmsh.finalize()
                manifest["finalize_called"] = True
            except BaseException as error:
                if primary is None:
                    primary = error
                    primary_traceback = "".join(
                        traceback.format_exception(type(error), error, error.__traceback__)
                    )
                else:
                    record_secondary(error)
        elif gmsh is not None and initialization_attempted:
            is_initialized = getattr(gmsh, "isInitialized", None)
            if callable(is_initialized):
                try:
                    if bool(is_initialized()):
                        gmsh.finalize()
                        manifest["finalize_called"] = True
                except BaseException as error:
                    if primary is None:
                        primary = error
                        primary_traceback = "".join(
                            traceback.format_exception(type(error), error, error.__traceback__)
                        )
                    else:
                        record_secondary(error)

    try:
        if source is not None and source.exists():
            after = _snapshot(source)
            manifest["source_after"] = after
            manifest["source_unchanged"] = after == manifest["source_before"]
            if manifest["source_unchanged"] is not True and primary is None:
                raise TopologySmokeMeshError("pipeline BREP changed during meshing")
    except BaseException as error:
        if primary is None:
            primary = error
            primary_traceback = "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            )
        else:
            record_secondary(error)

    manifest["ended_at_utc"] = _utc_now()
    if primary is None and result_payload is not None:
        manifest.update(result_payload)
        manifest["status"] = "PASS"
        manifest["error"] = None
        log_lines.append(f"[{manifest['ended_at_utc']}] status=PASS")
        try:
            _write_new_text(
                staging / "gmsh_topology_smoke.log", "\n".join(log_lines) + "\n"
            )
            _write_new_json(staging / "topology_smoke_manifest.json", manifest)
            staging.rename(output)
        except BaseException as error:
            primary = error
            primary_traceback = "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            )
            log_lines.append(primary_traceback)
        else:
            return manifest

    manifest["status"] = "FAIL"
    manifest["error"] = {
        "type": type(primary).__name__ if primary is not None else "UnknownError",
        "message": str(primary) if primary is not None else "unknown failure",
        "traceback": primary_traceback,
    }
    log_lines.append(f"[{manifest['ended_at_utc']}] status=FAIL")
    try:
        _remove_staging(staging, output.parent)
        output.mkdir()
        _write_new_text(output / "gmsh_topology_smoke.log", "\n".join(log_lines) + "\n")
        _write_new_json(output / "topology_smoke_manifest.json", manifest)
        if manifest["cad_repair_request"]["required"]:
            _write_new_json(output / "cad_repair_request.json", _cad_repair_request(manifest))
    except BaseException as publication_error:
        raise TopologySmokeMeshError(
            f"topology smoke failed and failure evidence could not be published: {publication_error}"
        ) from primary
    if isinstance(primary, (KeyboardInterrupt, SystemExit)):
        raise primary
    raise TopologySmokeMeshError(
        f"topology smoke mesh failed: {manifest['error']['message']}"
    ) from primary


__all__ = [
    "TopologySmokeMeshError",
    "build_topology_smoke_mesh",
]
