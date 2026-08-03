"""Fail-closed rematching of confirmed markers after CAD topology repair.

The Boolean-fragment operation is allowed to renumber or split surfaces.  This
module therefore treats entity tags only as short-lived joins between repair
evidence and a derived surface catalog.  The JSON written by
``rematch_derived_markers`` contains no runtime tags: its identities are
canonical hashes of geometry and explicit source-surface lineage.

Only the Python standard library is used.  No Gmsh, solver, or external
process is started here.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TypeAlias


PathLike: TypeAlias = str | os.PathLike[str]

_OUTPUT_SCHEMA = "cfdpipe.derived_marker_rematch.v1"
_SURFACE_SCHEMA = "cfdpipe.derived_boundary_surface_fingerprint.v1"
_GROUP_SCHEMA = "cfdpipe.derived_boundary_group_fingerprint.v1"
_MEASUREMENT_SCHEMA = "cfdpipe.derived_measurement_surface_fingerprint.v1"

_EXTERNAL_CLASSIFICATIONS = {
    "external",
    "external_boundary",
    "external_solver_boundary",
    "exposed_boundary",
    "single_volume_boundary",
}
_ONE_TO_ONE_MODES = {
    "one_to_one",
    "exact_one_to_one",
    "one_to_one_geometry_and_volume_lineage",
    "unchanged",
}
_UNION_MODES = {
    "descendant_union",
    "one_to_many",
    "split_descendant_union",
    "exact_descendant_union",
}


class DerivedMarkerRematchError(ValueError):
    """Raised when evidence cannot prove a unique, complete rematch."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _sha256(value: object, label: str) -> str:
    if not _is_sha256(value):
        raise DerivedMarkerRematchError(f"{label} must be a SHA-256 hex digest")
    return str(value).lower()


def _load_json(
    source: PathLike | Mapping[str, Any], label: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    if isinstance(source, Mapping):
        document = json.loads(json.dumps(source, allow_nan=False))
        return document, {"sha256": _canonical_sha256(document)}
    path = Path(source).resolve()
    if not path.is_file():
        raise DerivedMarkerRematchError(f"{label} does not exist: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DerivedMarkerRematchError(f"cannot read {label}: {path}") from error
    if not isinstance(document, dict):
        raise DerivedMarkerRematchError(f"{label} must contain a JSON object")
    return document, {"path": str(path), "sha256": _file_sha256(path)}


def _first(mapping: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _list_field(
    mapping: Mapping[str, Any], names: Sequence[str], label: str
) -> list[Any]:
    value = _first(mapping, names)
    if not isinstance(value, list):
        raise DerivedMarkerRematchError(f"{label} must be a list")
    return value


def _positive_tag(value: object, label: str) -> int:
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DerivedMarkerRematchError(f"{label} must be a positive integer")
    return value


def _tag_from_mapping(mapping: Mapping[str, Any], label: str) -> int:
    return _positive_tag(
        _first(
            mapping,
            (
                "entity_tag",
                "surface_tag_audit",
                "gmsh_surface_entity_tag",
                "tag",
            ),
        ),
        label,
    )


def _tags_from_descendants(entry: Mapping[str, Any], label: str) -> list[int]:
    direct = _first(
        entry,
        (
            "descendant_surface_tags_audit",
            "derived_surface_tags_audit",
            "descendant_tags_audit",
            "descendant_tags",
        ),
    )
    tags: list[int]
    if direct is not None:
        if not isinstance(direct, list) or not direct:
            raise DerivedMarkerRematchError(f"{label} descendant tags must be nonempty")
        tags = [
            _positive_tag(value, f"{label} descendant surface tag") for value in direct
        ]
    else:
        descendants = entry.get("descendants")
        if isinstance(descendants, Mapping):
            descendants = _first(
                descendants, ("surfaces", "surface_entities", "items")
            )
        if not isinstance(descendants, list) or not descendants:
            raise DerivedMarkerRematchError(
                f"{label} must identify at least one descendant surface"
            )
        tags = []
        for item in descendants:
            if isinstance(item, Mapping):
                tags.append(_tag_from_mapping(item, f"{label} descendant"))
            else:
                tags.append(_positive_tag(item, f"{label} descendant surface tag"))
    if len(tags) != len(set(tags)):
        raise DerivedMarkerRematchError(f"{label} repeats a descendant surface")
    return sorted(tags)


def _finite_float(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise DerivedMarkerRematchError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise DerivedMarkerRematchError(f"{label} must be a finite number") from error
    if not math.isfinite(result):
        raise DerivedMarkerRematchError(f"{label} must be finite")
    return result


def _vector(value: object, length: int, label: str) -> list[float]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise DerivedMarkerRematchError(f"{label} is not valid JSON") from error
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise DerivedMarkerRematchError(f"{label} must contain {length} numbers")
    return [_finite_float(item, label) for item in value]


def _json_list(value: object, label: str) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise DerivedMarkerRematchError(f"{label} is not valid JSON") from error
    if not isinstance(value, list):
        raise DerivedMarkerRematchError(f"{label} must be a list")
    return value


def _round_float(value: float) -> float:
    rounded = round(float(value), 12)
    return 0.0 if rounded == 0.0 else rounded


def _rounded(values: Sequence[float]) -> list[float]:
    return [_round_float(value) for value in values]


def _close(a: float, b: float, *, rel: float, absolute: float) -> bool:
    return math.isclose(a, b, rel_tol=rel, abs_tol=absolute)


def _vectors_close(a: Sequence[float], b: Sequence[float], absolute: float) -> bool:
    return len(a) == len(b) and all(abs(x - y) <= absolute for x, y in zip(a, b))


def _normalise_surface(
    raw: Mapping[str, Any], label: str, *, require_entity_type: bool
) -> dict[str, Any]:
    tag = _tag_from_mapping(raw, label)
    area = _finite_float(_first(raw, ("area_m2", "area")), f"{label} area")
    if area <= 0.0:
        raise DerivedMarkerRematchError(f"{label} area must be positive")
    centroid = _vector(
        _first(raw, ("centroid_m", "center_of_mass_m", "centroid")),
        3,
        f"{label} centroid",
    )
    bounds = _vector(
        _first(raw, ("bounding_box_m", "bounds_m", "bounds")),
        6,
        f"{label} bounds",
    )
    if any(bounds[index] > bounds[index + 3] for index in range(3)):
        raise DerivedMarkerRematchError(f"{label} has inverted bounds")
    entity_type = _first(raw, ("entity_type", "surface_type", "type"))
    if not isinstance(entity_type, str) or not entity_type.strip():
        if require_entity_type:
            raise DerivedMarkerRematchError(f"{label} entity type must be nonempty")
        # The pre-repair CSV predates the separate OCC surface-type catalog.
        # Its byte hash is confirmed upstream, and rematching only needs its
        # area/centroid/bounds.  Do not invent a CAD type for that evidence.
        entity_type = "not_recorded_in_original_catalog"
    adjacent = _json_list(
        _first(raw, ("adjacent_volumes", "adjacent_volume_ids", "volume_adjacency")),
        f"{label} adjacent volumes",
    )
    normalised_adjacency: list[str] = []
    for value in adjacent:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise DerivedMarkerRematchError(
                f"{label} adjacent volume identifiers are invalid"
            )
        text = str(value).strip()
        if not text:
            raise DerivedMarkerRematchError(
                f"{label} adjacent volume identifiers are invalid"
            )
        normalised_adjacency.append(text)
    if len(normalised_adjacency) != len(set(normalised_adjacency)):
        raise DerivedMarkerRematchError(f"{label} repeats an adjacent volume")
    return {
        "entity_tag": tag,
        "area_m2": area,
        "centroid_m": centroid,
        "bounding_box_m": bounds,
        "entity_type": entity_type.strip(),
        "adjacent_volume_count": len(normalised_adjacency),
    }


def _surface_rows_from_document(document: Mapping[str, Any], label: str) -> list[Any]:
    rows = _first(document, ("surfaces", "surface_catalog", "surface_entities"))
    if isinstance(rows, Mapping):
        rows = list(rows.values())
    if not isinstance(rows, list):
        raise DerivedMarkerRematchError(f"{label} does not contain a surface list")
    return rows


def _load_surface_catalog(
    source: PathLike | Mapping[str, Any],
    label: str,
    *,
    require_pass: bool,
    require_entity_type: bool,
) -> tuple[dict[int, dict[str, Any]], dict[str, Any], dict[str, Any] | None]:
    document: dict[str, Any] | None
    if isinstance(source, Mapping):
        document = json.loads(json.dumps(source, allow_nan=False))
        provenance = {"sha256": _canonical_sha256(document)}
        rows = _surface_rows_from_document(document, label)
    else:
        path = Path(source).resolve()
        if not path.is_file():
            raise DerivedMarkerRematchError(f"{label} does not exist: {path}")
        provenance = {"path": str(path), "sha256": _file_sha256(path)}
        if path.suffix.lower() == ".csv":
            document = None
            try:
                with path.open("r", encoding="utf-8-sig", newline="") as stream:
                    rows = list(csv.DictReader(stream))
            except (OSError, UnicodeError, csv.Error) as error:
                raise DerivedMarkerRematchError(f"cannot read {label}: {path}") from error
        else:
            document, _ = _load_json(path, label)
            rows = _surface_rows_from_document(document, label)
    if require_pass and (
        document is None
        or document.get("status") not in (None, "PASS")
    ):
        raise DerivedMarkerRematchError(f"{label} reports a non-PASS status")
    surfaces: dict[int, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise DerivedMarkerRematchError(f"{label} row {index} is invalid")
        surface = _normalise_surface(
            row,
            f"{label} row {index}",
            require_entity_type=require_entity_type,
        )
        tag = surface["entity_tag"]
        if tag in surfaces:
            raise DerivedMarkerRematchError(f"{label} repeats surface {tag}")
        surfaces[tag] = surface
    if not surfaces:
        raise DerivedMarkerRematchError(f"{label} is empty")
    return surfaces, provenance, document


def _validated_confirmation(document: Mapping[str, Any]) -> dict[str, Any]:
    if document.get("status") != "PASS":
        raise DerivedMarkerRematchError("marker confirmation validation is not PASS")
    if document.get("document_kind") == "cfdpipe_marker_confirmation_validation":
        validation = document.get("validation")
        if not isinstance(validation, Mapping) or validation.get("status") != "PASS":
            raise DerivedMarkerRematchError(
                "marker confirmation validation payload is not PASS"
            )
        saved_confirmation = document.get("confirmation")
        if not isinstance(saved_confirmation, Mapping):
            raise DerivedMarkerRematchError(
                "marker confirmation validation lacks saved-confirmation provenance"
            )
        _sha256(saved_confirmation.get("sha256"), "saved confirmation SHA-256")
        validation = dict(validation)
    else:
        validation = dict(document)
    if (
        validation.get("marker_configuration_ready") is not True
        or validation.get("markers_toml_written") is not False
    ):
        raise DerivedMarkerRematchError(
            "marker confirmation is not ready for post-repair rematching"
        )
    policy = validation.get("policy")
    if (
        not isinstance(policy, Mapping)
        or policy.get("gmsh_tags_are_audit_only") is not True
        or policy.get("every_selected_fingerprint_uniquely_matched") is not True
        or policy.get("all_solver_boundaries_require_complete_surface_groups")
        is not True
        or policy.get("duplicate_solver_marker_assignment_forbidden") is not True
        or policy.get("measurement_surface_is_solver_boundary") is not False
    ):
        raise DerivedMarkerRematchError(
            "marker confirmation does not carry the required fail-closed policy"
        )
    return validation


def _confirmation_contract(validation: Mapping[str, Any]) -> dict[str, Any]:
    source_step_sha = _sha256(
        validation.get("source_step_sha256"), "confirmed source STEP SHA-256"
    )
    source = _first(validation, ("current_evidence", "confirmation_source"))
    if not isinstance(source, Mapping):
        raise DerivedMarkerRematchError("marker confirmation source evidence is absent")
    source_catalog_sha = _sha256(
        source.get("surface_catalog_sha256"), "confirmed surface catalog SHA-256"
    )
    roles = validation.get("roles")
    if not isinstance(roles, Mapping) or not roles:
        raise DerivedMarkerRematchError("marker confirmation has no resolved roles")
    human = validation.get("human_confirmation")
    if (
        not isinstance(human, Mapping)
        or human.get("status") != "PASS"
        or human.get("gmsh_tags_are_matching_criteria") is not False
    ):
        raise DerivedMarkerRematchError("human marker confirmation is invalid")
    stable_assignments = human.get("stable_group_fingerprint_ids_by_role")
    if not isinstance(stable_assignments, Mapping):
        raise DerivedMarkerRematchError("human stable group assignments are absent")

    solver_roles: dict[str, dict[str, Any]] = {}
    measurement_roles: list[tuple[str, Mapping[str, Any]]] = []
    original_fingerprints: set[str] = set()
    original_tags: set[int] = set()
    for role, raw in roles.items():
        if not isinstance(role, str) or not role or not isinstance(raw, Mapping):
            raise DerivedMarkerRematchError("marker confirmation contains an invalid role")
        kind = raw.get("kind")
        if kind == "measurement_surface":
            if raw.get("solver_boundary") is not False:
                raise DerivedMarkerRematchError(
                    f"measurement role {role!r} is incorrectly a solver boundary"
                )
            measurement_roles.append((role, raw))
            continue
        if raw.get("solver_boundary") is not True:
            raise DerivedMarkerRematchError(
                f"solver role {role!r} is not explicitly a solver boundary"
            )
        group_id = _sha256(
            raw.get("group_fingerprint_id"), f"role {role!r} group fingerprint"
        )
        if stable_assignments.get(role) != group_id:
            raise DerivedMarkerRematchError(
                f"human assignment and resolved group disagree for role {role!r}"
            )
        count = raw.get("complete_member_count")
        matched = raw.get("matched_entities")
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count <= 0
            or not isinstance(matched, list)
            or len(matched) != count
        ):
            raise DerivedMarkerRematchError(
                f"role {role!r} is not a complete confirmed group"
            )
        members: dict[str, int] = {}
        for member in matched:
            if not isinstance(member, Mapping):
                raise DerivedMarkerRematchError(
                    f"role {role!r} contains invalid matched evidence"
                )
            fingerprint_id = _sha256(
                member.get("fingerprint_id"), f"role {role!r} source fingerprint"
            )
            audit = member.get("audit")
            if not isinstance(audit, Mapping) or audit.get("tag_is_matching_criterion") is not False:
                raise DerivedMarkerRematchError(
                    f"role {role!r} lacks audit-only tag evidence"
                )
            tag = _positive_tag(
                _first(audit, ("gmsh_surface_entity_tag", "surface_tag_audit")),
                f"role {role!r} original audit tag",
            )
            if fingerprint_id in original_fingerprints or tag in original_tags:
                raise DerivedMarkerRematchError(
                    "confirmed solver groups overlap before topology repair"
                )
            original_fingerprints.add(fingerprint_id)
            original_tags.add(tag)
            members[fingerprint_id] = tag
        solver_roles[role] = {
            "kind": str(kind),
            "group_fingerprint_id": group_id,
            "members": members,
        }
    if not solver_roles or len(measurement_roles) != 1:
        raise DerivedMarkerRematchError(
            "confirmation must contain solver roles and exactly one measurement role"
        )
    if set(stable_assignments) != set(solver_roles):
        raise DerivedMarkerRematchError(
            "human stable assignments do not cover exactly the solver roles"
        )
    measurement_role, measurement = measurement_roles[0]
    candidate = measurement.get("measurement_candidate")
    if not isinstance(candidate, Mapping):
        raise DerivedMarkerRematchError("confirmed measurement candidate is absent")
    candidate_id = candidate.get("candidate_id")
    definition = candidate.get("definition")
    if (
        not isinstance(candidate_id, str)
        or not candidate_id
        or candidate.get("role") != measurement_role
        or candidate.get("solver_boundary") is not False
        or not isinstance(definition, Mapping)
    ):
        raise DerivedMarkerRematchError("confirmed measurement candidate is invalid")
    origin = _vector(definition.get("origin_m"), 3, "confirmed measurement origin")
    normal = _vector(definition.get("normal"), 3, "confirmed measurement normal")
    magnitude = math.sqrt(sum(value * value for value in normal))
    if magnitude <= 0.0:
        raise DerivedMarkerRematchError("confirmed measurement normal is zero")
    normal = [value / magnitude for value in normal]
    boundary_fingerprint = definition.get("boundary_loop_fingerprint")
    if not isinstance(boundary_fingerprint, Mapping):
        raise DerivedMarkerRematchError(
            "confirmed measurement boundary fingerprint is absent"
        )
    expected_payload = boundary_fingerprint.get("payload")
    expected_hash = _sha256(
        boundary_fingerprint.get("sha256"), "confirmed measurement fingerprint"
    )
    if (
        not isinstance(expected_payload, Mapping)
        or _canonical_sha256(expected_payload) != expected_hash
    ):
        raise DerivedMarkerRematchError(
            "confirmed measurement boundary fingerprint is invalid"
        )
    return {
        "source_step_sha256": source_step_sha,
        "source_catalog_sha256": source_catalog_sha,
        "solver_roles": solver_roles,
        "measurement": {
            "role": measurement_role,
            "candidate_id": candidate_id,
            "origin_m": origin,
            "normal": normal,
            "expected_payload": dict(expected_payload),
            "expected_hash": expected_hash,
        },
        "original_fingerprints": original_fingerprints,
        "original_tags": original_tags,
    }


def _coverage_is_pass(entry: Mapping[str, Any], label: str) -> None:
    coverage = _first(entry, ("coverage", "coverage_validation"))
    if coverage is not None:
        if not isinstance(coverage, Mapping) or coverage.get("status") != "PASS":
            raise DerivedMarkerRematchError(f"{label} coverage is not PASS")
    status = _first(entry, ("status", "match_status"))
    if status is not None and status != "PASS":
        raise DerivedMarkerRematchError(f"{label} match status is not PASS")


def _original_pair_tags(entry: Mapping[str, Any], label: str) -> list[int]:
    raw = _first(
        entry,
        (
            "original_surface_tags_audit",
            "original_surface_pair_tags_audit",
            "original_pair_tags_audit",
            "original_pair",
        ),
    )
    if isinstance(raw, Mapping):
        raw = _first(raw, ("surface_tags_audit", "surfaces", "members"))
    if not isinstance(raw, list) or len(raw) != 2:
        raise DerivedMarkerRematchError(
            f"{label} must identify exactly two original interface surfaces"
        )
    tags = [
        _tag_from_mapping(item, label) if isinstance(item, Mapping) else _positive_tag(item, label)
        for item in raw
    ]
    if len(set(tags)) != 2:
        raise DerivedMarkerRematchError(f"{label} repeats an original interface surface")
    return sorted(tags)


def _aggregate(surfaces: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    total_area = sum(float(surface["area_m2"]) for surface in surfaces)
    if total_area <= 0.0:
        raise DerivedMarkerRematchError("surface aggregate has nonpositive area")
    centroid = [
        sum(float(surface["area_m2"]) * float(surface["centroid_m"][axis]) for surface in surfaces)
        / total_area
        for axis in range(3)
    ]
    bounds = [
        min(float(surface["bounding_box_m"][axis]) for surface in surfaces)
        for axis in range(3)
    ] + [
        max(float(surface["bounding_box_m"][axis]) for surface in surfaces)
        for axis in range(3, 6)
    ]
    type_counts = Counter(str(surface["entity_type"]) for surface in surfaces)
    return {
        "total_area_m2": total_area,
        "area_weighted_centroid_m": centroid,
        "bounding_box_m": bounds,
        "entity_type_counts": [
            {"entity_type": name, "member_count": count}
            for name, count in sorted(type_counts.items())
        ],
    }


def _validate_geometry_coverage(
    original: Mapping[str, Any],
    descendants: Sequence[Mapping[str, Any]],
    *,
    area_relative: float,
    area_absolute: float,
    coordinate_absolute: float,
    label: str,
) -> dict[str, Any]:
    aggregate = _aggregate(descendants)
    if not _close(
        float(original["area_m2"]),
        float(aggregate["total_area_m2"]),
        rel=area_relative,
        absolute=area_absolute,
    ):
        raise DerivedMarkerRematchError(f"{label} descendant area is incomplete")
    if not _vectors_close(
        original["centroid_m"], aggregate["area_weighted_centroid_m"], coordinate_absolute
    ):
        raise DerivedMarkerRematchError(f"{label} descendant centroid is not conserved")
    if not _vectors_close(
        original["bounding_box_m"], aggregate["bounding_box_m"], coordinate_absolute
    ):
        raise DerivedMarkerRematchError(f"{label} descendant bounds are not conserved")
    return aggregate


def _tag_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return (
        lowered == "tag"
        or lowered.endswith("_tag")
        or lowered.endswith("_tags")
        or "entity_tag" in lowered
        or "runtime_tag" in lowered
        or lowered == "adjacent_volumes"
    )


def _assert_tag_free(value: object, label: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _tag_key(key):
                raise DerivedMarkerRematchError(
                    f"{label} contains runtime-tag field {key!r}"
                )
            _assert_tag_free(child, label)
    elif isinstance(value, list):
        for child in value:
            _assert_tag_free(child, label)


def _surface_fingerprint(
    surface: Mapping[str, Any],
    *,
    source_step_sha256: str,
    derived_cad_sha256: str,
    original_fingerprint_id: str,
) -> tuple[str, dict[str, Any]]:
    payload = {
        "schema": _SURFACE_SCHEMA,
        "source_step_sha256": source_step_sha256,
        "derived_cad_sha256": derived_cad_sha256,
        "original_surface_fingerprint_id": original_fingerprint_id,
        "geometry": {
            "area_m2": _round_float(float(surface["area_m2"])),
            "centroid_m": _rounded(surface["centroid_m"]),
            "bounding_box_m": _rounded(surface["bounding_box_m"]),
            "entity_type": str(surface["entity_type"]),
            "adjacent_volume_count": 1,
        },
        "classification": "external_boundary",
    }
    _assert_tag_free(payload, "derived surface fingerprint")
    return _canonical_sha256(payload), payload


def _fingerprint_object(item: Mapping[str, Any]) -> tuple[dict[str, Any], str] | None:
    raw = _first(item, ("fingerprint", "boundary_loop_fingerprint"))
    if not isinstance(raw, Mapping):
        definition = item.get("definition")
        if isinstance(definition, Mapping):
            raw = definition.get("boundary_loop_fingerprint")
    if not isinstance(raw, Mapping):
        return None
    payload = raw.get("payload")
    digest = raw.get("sha256")
    if not isinstance(payload, Mapping) or not _is_sha256(digest):
        raise DerivedMarkerRematchError("measurement evidence has an invalid fingerprint")
    digest = str(digest).lower()
    if _canonical_sha256(payload) != digest:
        raise DerivedMarkerRematchError("measurement fingerprint hash is inconsistent")
    _assert_tag_free(payload, "measurement fingerprint payload")
    return dict(payload), digest


def _measurement_geometry(item: Mapping[str, Any]) -> dict[str, Any]:
    fingerprint = _fingerprint_object(item)
    payload = {} if fingerprint is None else fingerprint[0]
    definition = item.get("definition")
    if not isinstance(definition, Mapping):
        definition = {}
    coordinate = _first(
        item,
        ("plane_coordinate_m", "station_m", "x_m"),
    )
    if coordinate is None:
        coordinate = _first(payload, ("plane_coordinate_m", "station_m", "x_m"))
    if coordinate is None:
        origin = _first(definition, ("origin_m", "origin"))
        if origin is not None:
            coordinate = _vector(origin, 3, "measurement origin")[0]
    axis = _first(item, ("plane_axis", "axis"))
    if axis is None:
        axis = _first(payload, ("plane_axis", "axis"))
    normal = _first(item, ("normal_unit", "normal"))
    if normal is None:
        normal = _first(payload, ("normal_unit", "normal"))
    if normal is None:
        normal = _first(definition, ("normal", "normal_unit"))
    bounds = _first(item, ("bounds_m", "bounding_box_m"))
    if bounds is None:
        bounds = _first(payload, ("bounds_m", "bounding_box_m"))
    area = _first(item, ("area_m2", "area"))
    if area is None:
        area = _first(payload, ("area_m2", "area"))
    if coordinate is None or axis is None or normal is None or bounds is None or area is None:
        raise DerivedMarkerRematchError("measurement candidate lacks geometric evidence")
    normal_vector = _vector(normal, 3, "measurement normal")
    magnitude = math.sqrt(sum(value * value for value in normal_vector))
    if magnitude <= 0.0:
        raise DerivedMarkerRematchError("measurement normal is zero")
    return {
        "plane_axis": str(axis).lower(),
        "plane_coordinate_m": _finite_float(coordinate, "measurement coordinate"),
        "normal_unit": [value / magnitude for value in normal_vector],
        "bounds_m": _vector(bounds, 6, "measurement bounds"),
        "area_m2": _finite_float(area, "measurement area"),
        "closed": item.get("closed"),
        "solver_boundary": item.get("solver_boundary"),
        "fingerprint": fingerprint,
    }


def _rematch_measurement(
    evidence: Mapping[str, Any],
    confirmed: Mapping[str, Any],
    *,
    source_step_sha256: str,
    derived_cad_sha256: str,
    coordinate_absolute: float,
    area_relative: float,
    area_absolute: float,
) -> dict[str, Any]:
    if evidence.get("status") != "PASS":
        raise DerivedMarkerRematchError("derived measurement evidence is not PASS")
    evidence_hash = _first(
        evidence,
        ("derived_cad_sha256", "source_sha256_before", "source_sha256"),
    )
    if evidence_hash is None or _sha256(
        evidence_hash, "derived measurement source SHA-256"
    ) != derived_cad_sha256:
        raise DerivedMarkerRematchError(
            "derived measurement evidence refers to unrelated geometry"
        )
    after_hash = evidence.get("source_sha256_after")
    if after_hash is not None and _sha256(
        after_hash, "derived measurement final source SHA-256"
    ) != _sha256(evidence_hash, "derived measurement source SHA-256"):
        raise DerivedMarkerRematchError("measurement discovery modified its source CAD")

    candidates = _first(
        evidence, ("candidates", "measurements", "measurement_surfaces", "loops")
    )
    if not isinstance(candidates, list) or not candidates:
        raise DerivedMarkerRematchError("derived measurement evidence has no candidates")
    expected_origin = confirmed["origin_m"]
    expected_normal = confirmed["normal"]
    expected_payload = confirmed["expected_payload"]
    expected_bounds = _vector(
        _first(expected_payload, ("bounds_m", "bounding_box_m")),
        6,
        "confirmed measurement bounds",
    )
    expected_area = _finite_float(
        expected_payload.get("area_m2"), "confirmed measurement area"
    )
    matches: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise DerivedMarkerRematchError("measurement candidate is invalid")
        geometry = _measurement_geometry(candidate)
        if (
            geometry["plane_axis"] == "x"
            and abs(geometry["plane_coordinate_m"] - expected_origin[0])
            <= coordinate_absolute
            and sum(
                geometry["normal_unit"][axis] * expected_normal[axis]
                for axis in range(3)
            )
            >= 1.0 - 1.0e-10
        ):
            matches.append((candidate, geometry))
    if len(matches) != 1:
        raise DerivedMarkerRematchError(
            "confirmed measurement plane did not rematch uniquely after repair"
        )
    candidate, geometry = matches[0]
    if geometry["closed"] is not True:
        raise DerivedMarkerRematchError("derived measurement boundary is not closed")
    if geometry["solver_boundary"] is True:
        raise DerivedMarkerRematchError(
            "derived measurement was incorrectly classified as a solver boundary"
        )
    if not _close(
        geometry["area_m2"], expected_area, rel=area_relative, absolute=area_absolute
    ) or not _vectors_close(
        geometry["bounds_m"], expected_bounds, coordinate_absolute
    ):
        raise DerivedMarkerRematchError(
            "derived measurement geometry does not match the confirmed section"
        )
    classification = evidence.get("classification")
    selected_hash: str | None = None
    if isinstance(classification, Mapping):
        if classification.get("geometry_status") not in (None, "CONFIRMED_GEOMETRY", "PASS"):
            raise DerivedMarkerRematchError(
                "derived measurement classification is not confirmed"
            )
        raw_selected = _first(
            classification,
            ("selected_fingerprint_sha256", "selected_candidate_fingerprint_sha256"),
        )
        if raw_selected is not None:
            selected_hash = _sha256(raw_selected, "selected measurement fingerprint")
    fingerprint = geometry["fingerprint"]
    if fingerprint is None:
        geometry_payload = {
            "schema": "cfdpipe.measurement_surface_geometry.v1",
            "plane_axis": "x",
            "plane_coordinate_m": _round_float(geometry["plane_coordinate_m"]),
            "normal_unit": _rounded(geometry["normal_unit"]),
            "bounds_m": _rounded(geometry["bounds_m"]),
            "area_m2": _round_float(geometry["area_m2"]),
            "closed": True,
        }
        fingerprint = (geometry_payload, _canonical_sha256(geometry_payload))
    payload, fingerprint_id = fingerprint
    if selected_hash is not None and selected_hash != fingerprint_id:
        raise DerivedMarkerRematchError(
            "measurement classification selects a different candidate"
        )
    output_payload = {
        "schema": _MEASUREMENT_SCHEMA,
        "source_step_sha256": source_step_sha256,
        "derived_cad_sha256": derived_cad_sha256,
        "confirmed_measurement_candidate_id": confirmed["candidate_id"],
        "derived_boundary_fingerprint_id": fingerprint_id,
        "geometry": {
            "plane_axis": "x",
            "plane_coordinate_m": _round_float(geometry["plane_coordinate_m"]),
            "normal_unit": _rounded(geometry["normal_unit"]),
            "bounds_m": _rounded(geometry["bounds_m"]),
            "area_m2": _round_float(geometry["area_m2"]),
            "closed": True,
        },
        "solver_boundary": False,
    }
    _assert_tag_free(output_payload, "derived measurement output")
    return {
        "kind": "measurement_surface",
        "solver_boundary": False,
        "derived_measurement_fingerprint_id": _canonical_sha256(output_payload),
        "derived_measurement_fingerprint": output_payload,
    }


def _atomic_write_new(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as error:
        raise DerivedMarkerRematchError(f"refusing to overwrite existing file: {path}") from error


def rematch_derived_markers(
    confirmation_validation: PathLike | Mapping[str, Any],
    original_surface_catalog: PathLike | Mapping[str, Any],
    repair_lineage: PathLike | Mapping[str, Any],
    derived_surface_catalog: PathLike | Mapping[str, Any],
    derived_measurement_evidence: PathLike | Mapping[str, Any],
    output_path: PathLike,
    *,
    area_relative_tolerance: float = 2.0e-8,
    area_absolute_tolerance_m2: float = 2.0e-9,
    coordinate_absolute_tolerance_m: float = 2.0e-7,
) -> dict[str, Any]:
    """Rematch confirmed semantic groups to a repaired CAD surface catalog.

    Every confirmed original surface must have exactly one lineage record.  A
    lineage may lead to one surface or to a geometry-conserving union of split
    descendants.  All solver descendants must be external (one adjacent
    volume), all interface descendants must be shared (two adjacent volumes),
    the two sets must exhaust the derived catalog, and no descendant may occur
    twice.  Validation completes before ``output_path`` is created.
    """

    destination = Path(output_path).resolve()
    if destination.suffix.lower() != ".json":
        raise DerivedMarkerRematchError("derived marker rematch output must be JSON")
    if destination.exists():
        raise DerivedMarkerRematchError(
            f"refusing to overwrite existing file: {destination}"
        )
    area_rel = _finite_float(area_relative_tolerance, "area relative tolerance")
    area_abs = _finite_float(
        area_absolute_tolerance_m2, "area absolute tolerance"
    )
    coordinate_abs = _finite_float(
        coordinate_absolute_tolerance_m, "coordinate absolute tolerance"
    )
    if area_rel < 0.0 or area_abs < 0.0 or coordinate_abs < 0.0:
        raise DerivedMarkerRematchError("rematch tolerances cannot be negative")

    confirmation_document, confirmation_provenance = _load_json(
        confirmation_validation, "marker confirmation validation"
    )
    validation = _validated_confirmation(confirmation_document)
    contract = _confirmation_contract(validation)
    original_surfaces, original_provenance, _ = _load_surface_catalog(
        original_surface_catalog,
        "original surface catalog",
        require_pass=False,
        require_entity_type=False,
    )
    if original_provenance["sha256"].lower() != contract["source_catalog_sha256"]:
        raise DerivedMarkerRematchError(
            "original surface catalog hash disagrees with confirmed evidence"
        )

    lineage, lineage_provenance = _load_json(repair_lineage, "repair lineage")
    if lineage.get("status") != "PASS":
        raise DerivedMarkerRematchError("repair lineage is not PASS")
    if _sha256(
        lineage.get("source_step_sha256"), "repair lineage source STEP SHA-256"
    ) != contract["source_step_sha256"]:
        raise DerivedMarkerRematchError("repair lineage refers to a different STEP")
    derived_cad_sha = _sha256(
        lineage.get("derived_cad_sha256"), "repair lineage derived CAD SHA-256"
    )
    derived_surfaces, derived_provenance, derived_document = _load_surface_catalog(
        derived_surface_catalog,
        "derived surface catalog",
        require_pass=True,
        require_entity_type=True,
    )
    assert derived_document is not None
    catalog_cad_sha = _first(
        derived_document,
        ("derived_cad_sha256", "source_cad_sha256", "source_sha256"),
    )
    if catalog_cad_sha is not None:
        if _sha256(
            catalog_cad_sha, "derived catalog CAD SHA-256"
        ) != derived_cad_sha:
            raise DerivedMarkerRematchError(
                "derived catalog does not prove the repaired CAD identity"
            )
    else:
        # The geometry-repair catalog intentionally remains a plain
        # ``surfaces``/``volumes`` document.  In that form every surface has a
        # canonical, tag-free fingerprint embedding the derived CAD digest.
        raw_surfaces = _surface_rows_from_document(
            derived_document, "derived surface catalog"
        )
        for index, raw_surface in enumerate(raw_surfaces):
            if not isinstance(raw_surface, Mapping):
                raise DerivedMarkerRematchError(
                    "derived catalog contains an invalid surface"
                )
            payload = raw_surface.get("surface_fingerprint")
            fingerprint_id = raw_surface.get("surface_fingerprint_id")
            if (
                not isinstance(payload, Mapping)
                or _canonical_sha256(payload)
                != _sha256(
                    fingerprint_id,
                    f"derived catalog surface {index} fingerprint",
                )
                or _sha256(
                    payload.get("derived_cad_sha256"),
                    f"derived catalog surface {index} CAD SHA-256",
                )
                != derived_cad_sha
            ):
                raise DerivedMarkerRematchError(
                    "derived catalog surface fingerprints do not prove the repaired CAD identity"
                )
            _assert_tag_free(payload, "derived catalog surface fingerprint")

    external_entries = _list_field(
        lineage,
        (
            "external_surface_lineage",
            "external_surface_lineages",
            "external_lineage",
            "external_surfaces",
        ),
        "external surface lineage",
    )
    interface_entries = _list_field(
        lineage,
        ("interface_lineages", "interface_surface_lineages", "shared_interfaces"),
        "interface lineages",
    )
    if not external_entries or not interface_entries:
        raise DerivedMarkerRematchError(
            "repair lineage must contain external and shared-interface evidence"
        )

    member_contract: dict[str, tuple[str, str, int]] = {}
    for role, role_contract in contract["solver_roles"].items():
        for fingerprint_id, tag in role_contract["members"].items():
            member_contract[fingerprint_id] = (
                role,
                role_contract["group_fingerprint_id"],
                tag,
            )
    lineage_by_fingerprint: dict[str, dict[str, Any]] = {}
    used_external_tags: set[int] = set()
    for index, raw_entry in enumerate(external_entries):
        label = f"external lineage {index}"
        if not isinstance(raw_entry, Mapping):
            raise DerivedMarkerRematchError(f"{label} is invalid")
        fingerprint_id = _sha256(
            _first(
                raw_entry,
                (
                    "original_fingerprint_id",
                    "original_surface_fingerprint_id",
                    "source_fingerprint_id",
                ),
            ),
            f"{label} original fingerprint",
        )
        if fingerprint_id not in member_contract or fingerprint_id in lineage_by_fingerprint:
            raise DerivedMarkerRematchError(
                f"{label} is unconfirmed, duplicated, or assigned more than once"
            )
        role, group_id, expected_tag = member_contract[fingerprint_id]
        original_tag = _positive_tag(
            _first(
                raw_entry,
                (
                    "original_surface_tag_audit",
                    "source_surface_tag_audit",
                    "original_entity_tag_audit",
                ),
            ),
            f"{label} original audit tag",
        )
        if (
            original_tag != expected_tag
            or raw_entry.get("semantic_role") not in (None, role)
            or raw_entry.get("role") not in (None, role)
            or _first(
                raw_entry,
                ("semantic_group_fingerprint_id", "confirmed_group_fingerprint_id"),
            )
            != group_id
        ):
            raise DerivedMarkerRematchError(
                f"{label} disagrees with the confirmed semantic assignment"
            )
        classification = raw_entry.get("classification")
        if classification is not None and str(classification).lower() not in _EXTERNAL_CLASSIFICATIONS:
            raise DerivedMarkerRematchError(f"{label} is not classified as external")
        mode = raw_entry.get("match_mode")
        if not isinstance(mode, str) or mode.lower() not in _ONE_TO_ONE_MODES | _UNION_MODES:
            raise DerivedMarkerRematchError(f"{label} has an unsupported match mode")
        descendants = _tags_from_descendants(raw_entry, label)
        if mode.lower() in _ONE_TO_ONE_MODES and len(descendants) != 1:
            raise DerivedMarkerRematchError(
                f"{label} one-to-one mode has multiple descendants"
            )
        overlap = used_external_tags.intersection(descendants)
        if overlap:
            raise DerivedMarkerRematchError(
                f"external solver groups overlap on derived surfaces: {sorted(overlap)}"
            )
        used_external_tags.update(descendants)
        _coverage_is_pass(raw_entry, label)
        lineage_by_fingerprint[fingerprint_id] = {
            "role": role,
            "group_id": group_id,
            "original_tag": original_tag,
            "match_mode": mode.lower(),
            "descendant_tags": descendants,
        }
    if set(lineage_by_fingerprint) != set(member_contract):
        missing = sorted(set(member_contract) - set(lineage_by_fingerprint))
        raise DerivedMarkerRematchError(
            f"external lineage does not cover every confirmed source surface: {missing}"
        )

    interface_original_tags: set[int] = set()
    used_interface_tags: set[int] = set()
    for index, raw_entry in enumerate(interface_entries):
        label = f"interface lineage {index}"
        if not isinstance(raw_entry, Mapping):
            raise DerivedMarkerRematchError(f"{label} is invalid")
        if raw_entry.get("classification") != "shared_interface":
            raise DerivedMarkerRematchError(
                f"{label} is not explicitly classified as shared_interface"
            )
        original_pair = _original_pair_tags(raw_entry, label)
        if interface_original_tags.intersection(original_pair):
            raise DerivedMarkerRematchError(f"{label} overlaps another original pair")
        interface_original_tags.update(original_pair)
        descendants = _tags_from_descendants(raw_entry, label)
        if used_interface_tags.intersection(descendants):
            raise DerivedMarkerRematchError(f"{label} repeats a shared descendant")
        if used_external_tags.intersection(descendants):
            raise DerivedMarkerRematchError(
                f"{label} overlaps a solver-boundary descendant"
            )
        used_interface_tags.update(descendants)
        _coverage_is_pass(raw_entry, label)

    unselected_original_tags = set(original_surfaces) - contract["original_tags"]
    if interface_original_tags != unselected_original_tags:
        raise DerivedMarkerRematchError(
            "interface lineage does not cover exactly the non-solver original surfaces"
        )
    if set(derived_surfaces) != used_external_tags | used_interface_tags:
        raise DerivedMarkerRematchError(
            "repair lineage does not exhaust the derived surface catalog"
        )
    for tag in used_external_tags:
        if derived_surfaces[tag]["adjacent_volume_count"] != 1:
            raise DerivedMarkerRematchError(
                "a solver-boundary descendant is not an external one-volume surface"
            )
    for tag in used_interface_tags:
        if derived_surfaces[tag]["adjacent_volume_count"] != 2:
            raise DerivedMarkerRematchError(
                "a shared-interface descendant is not adjacent to exactly two volumes"
            )

    members_by_role: dict[str, list[dict[str, Any]]] = {
        role: [] for role in contract["solver_roles"]
    }
    for original_fingerprint_id, rematch in lineage_by_fingerprint.items():
        original = original_surfaces.get(rematch["original_tag"])
        if original is None:
            raise DerivedMarkerRematchError(
                "confirmed original surface is absent from the original catalog"
            )
        descendants = [
            derived_surfaces[tag] for tag in rematch["descendant_tags"]
        ]
        _validate_geometry_coverage(
            original,
            descendants,
            area_relative=area_rel,
            area_absolute=area_abs,
            coordinate_absolute=coordinate_abs,
            label=f"source fingerprint {original_fingerprint_id}",
        )
        for surface in descendants:
            fingerprint_id, payload = _surface_fingerprint(
                surface,
                source_step_sha256=contract["source_step_sha256"],
                derived_cad_sha256=derived_cad_sha,
                original_fingerprint_id=original_fingerprint_id,
            )
            members_by_role[rematch["role"]].append(
                {
                    "fingerprint_id": fingerprint_id,
                    "fingerprint": payload,
                    "surface": surface,
                }
            )

    output_roles: dict[str, Any] = {}
    all_output_member_ids: set[str] = set()
    for role in sorted(members_by_role):
        members = sorted(members_by_role[role], key=lambda item: item["fingerprint_id"])
        ids = [item["fingerprint_id"] for item in members]
        if len(ids) != len(set(ids)) or all_output_member_ids.intersection(ids):
            raise DerivedMarkerRematchError(
                "derived surface fingerprints are ambiguous or overlap between groups"
            )
        all_output_member_ids.update(ids)
        geometry = _aggregate([item["surface"] for item in members])
        role_contract = contract["solver_roles"][role]
        group_payload = {
            "schema": _GROUP_SCHEMA,
            "source_step_sha256": contract["source_step_sha256"],
            "derived_cad_sha256": derived_cad_sha,
            "semantic_role": role,
            "boundary_kind": role_contract["kind"],
            "source_confirmed_group_fingerprint_id": role_contract[
                "group_fingerprint_id"
            ],
            "source_member_fingerprint_ids": sorted(role_contract["members"]),
            "derived_member_surface_fingerprint_ids": ids,
            "member_count": len(ids),
            "geometry": {
                "total_area_m2": _round_float(geometry["total_area_m2"]),
                "area_weighted_centroid_m": _rounded(
                    geometry["area_weighted_centroid_m"]
                ),
                "bounding_box_m": _rounded(geometry["bounding_box_m"]),
                "entity_type_counts": geometry["entity_type_counts"],
                "adjacent_volume_count": 1,
            },
            "classification": "complete_external_solver_boundary_group",
        }
        _assert_tag_free(group_payload, f"role {role!r} group fingerprint")
        output_roles[role] = {
            "kind": role_contract["kind"],
            "solver_boundary": True,
            "derived_group_fingerprint_id": _canonical_sha256(group_payload),
            "derived_group_fingerprint": group_payload,
            "derived_member_surface_fingerprints": [
                {
                    "fingerprint_id": item["fingerprint_id"],
                    "fingerprint": item["fingerprint"],
                }
                for item in members
            ],
        }

    measurement_document, measurement_provenance = _load_json(
        derived_measurement_evidence, "derived measurement evidence"
    )
    measurement_role = contract["measurement"]["role"]
    output_roles[measurement_role] = _rematch_measurement(
        measurement_document,
        contract["measurement"],
        source_step_sha256=contract["source_step_sha256"],
        derived_cad_sha256=derived_cad_sha,
        coordinate_absolute=coordinate_abs,
        area_relative=area_rel,
        area_absolute=area_abs,
    )

    result: dict[str, Any] = {
        "schema": _OUTPUT_SCHEMA,
        "status": "PASS",
        "source_step_sha256": contract["source_step_sha256"],
        "derived_cad_sha256": derived_cad_sha,
        "identity_policy": {
            "runtime_entity_identifiers_in_output": False,
            "surface_identity": "canonical_geometry_plus_original_surface_lineage",
            "group_identity": "canonical_complete_member_fingerprint_set",
        },
        "provenance": {
            "confirmation_validation": confirmation_provenance,
            "original_surface_catalog": original_provenance,
            "repair_lineage": lineage_provenance,
            "derived_surface_catalog": derived_provenance,
            "derived_measurement_evidence": measurement_provenance,
        },
        "counts": {
            "confirmed_original_solver_surfaces": len(member_contract),
            "derived_external_solver_surfaces": len(used_external_tags),
            "original_interface_surfaces": len(interface_original_tags),
            "derived_shared_interface_surfaces": len(used_interface_tags),
            "solver_boundary_groups": len(contract["solver_roles"]),
            "measurement_surfaces": 1,
        },
        "roles": output_roles,
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
    }
    _assert_tag_free(result, "derived marker rematch output")
    _atomic_write_new(
        destination,
        json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
    )
    return result


__all__ = ["DerivedMarkerRematchError", "rematch_derived_markers"]
