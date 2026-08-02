"""Deterministic, tag-free marker configuration derived from proven evidence.

The module deliberately does not import Gmsh.  It converts the already
validated geometry-repair, marker-rematch, interface-persistence and BREP
topology evidence into ``cfdpipe.markers.v2`` TOML.  Runtime CAD tags and STEP
names are forbidden: re-import matching is driven by canonical geometry and
lineage fingerprints only.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import tomllib
from typing import Any, Mapping, Sequence


PathLike = str | os.PathLike[str]
SCHEMA = "cfdpipe.markers.v2"

_AREA_RELATIVE_TOLERANCE = 2.0e-8
_AREA_ABSOLUTE_TOLERANCE_M2 = 2.0e-9
_COORDINATE_ABSOLUTE_TOLERANCE_M = 2.0e-7
_REQUIRED_PROVENANCE = (
    "project",
    "source_step",
    "pipeline_brep",
    "repair_manifest",
    "marker_rematch",
    "interface_persistence",
    "pipeline_topology",
)
_PERSISTENCE_FLAGS = (
    "stable_source_identity_recomputed",
    "matching_uses_old_runtime_tags",
    "source_copy_to_derived_projection_complete",
    "boundary_curve_geometry_complete",
    "absolute_parametric_normals_aligned",
    "derived_oriented_boundaries_opposite",
    "shared_face_mapping_bijective_and_exhaustive",
    "no_patch_overlap",
)


class MarkerConfigError(RuntimeError):
    """Raised when marker configuration evidence or TOML is not trustworthy."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise MarkerConfigError(f"payload is not canonically serializable: {error}") from error


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.casefold())
    )


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise MarkerConfigError(f"{label} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise MarkerConfigError(f"{label} must be numeric") from error
    if not math.isfinite(result):
        raise MarkerConfigError(f"{label} contains NaN or Inf")
    return result


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MarkerConfigError(f"{label} must be a positive integer")
    return value


def _vector(value: object, length: int, label: str) -> list[float]:
    if isinstance(value, (str, bytes)):
        raise MarkerConfigError(f"{label} must be a numeric array")
    try:
        result = [_finite(item, label) for item in value]  # type: ignore[arg-type]
    except TypeError as error:
        raise MarkerConfigError(f"{label} must be a numeric array") from error
    if len(result) != length:
        raise MarkerConfigError(f"{label} must contain {length} values")
    return result


def _round_float(value: object) -> float:
    rounded = round(_finite(value, "geometry value"), 12)
    return 0.0 if rounded == 0.0 else rounded


def _rounded(value: object, length: int, label: str) -> list[float]:
    return [_round_float(item) for item in _vector(value, length, label)]


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MarkerConfigError(f"{label} must be a table/object")
    return value


def _list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise MarkerConfigError(f"{label} must be an array")
    return value


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MarkerConfigError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise MarkerConfigError(f"{label} must contain an object")
    return value


def _is_read_only(path: Path) -> bool:
    metadata = path.stat()
    attributes = getattr(metadata, "st_file_attributes", None)
    if attributes is not None:
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1))
    writable = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    return not bool(metadata.st_mode & writable)


def _input_file(
    value: PathLike,
    label: str,
    suffixes: set[str],
    *,
    read_only: bool = False,
) -> Path:
    raw = Path(value).expanduser()
    if ".." in raw.parts or raw.is_symlink():
        raise MarkerConfigError(f"{label} must not traverse or be a symlink")
    try:
        path = raw.resolve(strict=True)
    except OSError as error:
        raise MarkerConfigError(f"{label} does not exist: {raw}") from error
    if not path.is_file() or path.suffix.casefold() not in suffixes:
        raise MarkerConfigError(f"{label} has an invalid file type: {path}")
    if read_only and not _is_read_only(path):
        raise MarkerConfigError(f"{label} must be read-only: {path}")
    return path


def _repository_root(project: Path, explicit: PathLike | None) -> Path:
    root = (
        Path(explicit).expanduser().resolve(strict=True)
        if explicit is not None
        else project.parent.parent.resolve(strict=True)
    )
    if not root.is_dir() or root not in project.parents:
        raise MarkerConfigError("project configuration is outside repository root")
    return root


def _relative(path: Path, root: Path, label: str) -> str:
    try:
        result = path.relative_to(root)
    except ValueError as error:
        raise MarkerConfigError(f"{label} is outside repository root: {path}") from error
    if not result.parts or ".." in result.parts:
        raise MarkerConfigError(f"{label} has an invalid repository-relative path")
    return result.as_posix()


def _resolve_relative(root: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise MarkerConfigError(f"{label} path must be nonempty")
    raw = Path(value)
    if raw.is_absolute() or ".." in raw.parts:
        raise MarkerConfigError(f"{label} path must be repository-relative")
    candidate = (root / raw).resolve(strict=True)
    if root != candidate and root not in candidate.parents:
        raise MarkerConfigError(f"{label} escapes repository root")
    if not candidate.is_file() or candidate.is_symlink():
        raise MarkerConfigError(f"{label} must resolve to an ordinary file")
    return candidate


def _assert_tag_free(value: object, label: str = "marker configuration") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in {
                "entity_tag",
                "entity_tags",
                "step_name",
                "step_names",
                "runtime_tag",
                "runtime_tags",
                "gmsh_tag",
                "gmsh_tags",
            } or normalized.endswith("_tag_audit") or normalized.endswith("_tags_audit"):
                raise MarkerConfigError(f"{label} contains forbidden field {key!r}")
            _assert_tag_free(child, label)
    elif isinstance(value, list):
        for child in value:
            _assert_tag_free(child, label)


def _project_contract(project_path: Path, source: Path) -> dict[str, Any]:
    try:
        with project_path.open("rb") as stream:
            project = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise MarkerConfigError(f"cannot parse project.toml: {error}") from error
    project_table = _mapping(project.get("project"), "[project]")
    geometry_file = project_table.get("geometry_file")
    if not isinstance(geometry_file, str) or not geometry_file.strip():
        raise MarkerConfigError("project.geometry_file is missing")
    declared = (project_path.parent.parent / geometry_file).resolve(strict=True)
    if declared != source:
        raise MarkerConfigError("source STEP does not match project.geometry_file")
    boundaries = _mapping(project.get("boundaries"), "[boundaries]")
    farfield = boundaries.get("farfield_role")
    wall = boundaries.get("wall_role")
    rear = boundaries.get("rear_outlet_roles")
    measurement = boundaries.get("measurement_surface_role")
    if not isinstance(farfield, str) or not farfield.strip():
        raise MarkerConfigError("farfield_role is missing")
    if not isinstance(wall, str) or not wall.strip():
        raise MarkerConfigError("wall_role is missing")
    if (
        not isinstance(rear, list)
        or len(rear) != 2
        or any(not isinstance(item, str) or not item.strip() for item in rear)
    ):
        raise MarkerConfigError("rear_outlet_roles must contain exactly two names")
    if not isinstance(measurement, str) or not measurement.strip():
        raise MarkerConfigError("measurement_surface_role is missing")
    roles = [farfield.strip(), wall.strip(), *(item.strip() for item in rear)]
    if len(set(roles + [measurement.strip()])) != 5:
        raise MarkerConfigError("project boundary roles must be unique")
    if boundaries.get("measurement_surface_is_solver_boundary") is not False:
        raise MarkerConfigError("project measurement surface must not be a solver boundary")
    return {
        "solver_roles": roles,
        "role_kinds": {
            farfield.strip(): "farfield",
            wall.strip(): "wall",
            rear[0].strip(): "rear_outlet",
            rear[1].strip(): "rear_outlet",
        },
        "measurement_role": measurement.strip(),
    }


def _geometry_key(geometry: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(geometry.get("entity_type", "")),
        _round_float(geometry.get("area_m2")),
        tuple(_rounded(geometry.get("centroid_m"), 3, "surface centroid")),
        tuple(_rounded(geometry.get("bounding_box_m"), 6, "surface bounds")),
        int(geometry.get("adjacent_volume_count", 0)),
    )


def _volume_selector(volume: Mapping[str, Any], derived_sha256: str) -> dict[str, Any]:
    boundary = _list(volume.get("boundary_surfaces"), "volume boundary_surfaces")
    payload = {
        "schema": "cfdpipe.volume_geometry_selector.v1",
        "derived_cad_sha256": derived_sha256,
        "entity_type": str(volume.get("entity_type", "")),
        "volume_m3": _round_float(volume.get("volume_m3")),
        "centroid_m": _rounded(volume.get("centroid_m"), 3, "volume centroid"),
        "bounding_box_m": _rounded(volume.get("bounding_box_m"), 6, "volume bounds"),
        "boundary_surface_count": len(boundary),
    }
    if not payload["entity_type"] or payload["volume_m3"] <= 0.0:
        raise MarkerConfigError("pipeline topology contains an invalid volume")
    return {"fingerprint_id": _canonical_sha256(payload), **payload}


def _topology_contract(
    topology: Mapping[str, Any], derived_sha256: str
) -> tuple[dict[str, int], list[dict[str, Any]], Counter[tuple[Any, ...]]]:
    surfaces = _list(topology.get("surfaces"), "topology surfaces")
    volumes = _list(topology.get("volumes"), "topology volumes")
    if len(volumes) != 2:
        raise MarkerConfigError("pipeline topology must contain exactly two volumes")
    external: list[Mapping[str, Any]] = []
    shared = 0
    for raw in surfaces:
        surface = _mapping(raw, "topology surface")
        adjacency = _list(surface.get("adjacent_volumes"), "surface adjacency")
        if len(adjacency) == 1:
            external.append(surface)
        elif len(adjacency) == 2:
            shared += 1
        else:
            raise MarkerConfigError("topology surface has invalid volume adjacency")
    if len(external) != 55 or shared != 2 or len(surfaces) != 57:
        raise MarkerConfigError(
            "pipeline topology must contain 55 external and 2 shared surfaces"
        )
    keys = Counter(
        _geometry_key(
            {
                "entity_type": item.get("entity_type"),
                "area_m2": item.get("area_m2"),
                "centroid_m": item.get("centroid_m"),
                "bounding_box_m": item.get("bounding_box_m"),
                "adjacent_volume_count": 1,
            }
        )
        for item in external
    )
    if any(count != 1 for count in keys.values()):
        raise MarkerConfigError("external geometry selectors are not unique")
    selectors = sorted(
        (_volume_selector(_mapping(item, "topology volume"), derived_sha256) for item in volumes),
        key=lambda item: item["fingerprint_id"],
    )
    if len({item["fingerprint_id"] for item in selectors}) != 2:
        raise MarkerConfigError("volume geometry selectors are not unique")
    return (
        {
            "volume_count": 2,
            "unique_surface_count": 57,
            "external_surface_count": 55,
            "shared_surface_count": 2,
        },
        selectors,
        keys,
    )


def _validate_repair(
    repair: Mapping[str, Any], source: Path, derived: Path
) -> None:
    source_record = _mapping(repair.get("source"), "repair source")
    pipeline = _mapping(repair.get("pipeline_geometry"), "repair pipeline_geometry")
    topology = _mapping(repair.get("fragment_topology"), "repair fragment_topology")
    rematch = _mapping(repair.get("marker_rematch"), "repair marker_rematch")
    if (
        repair.get("status") != "PASS"
        or repair.get("source_unchanged") is not True
        or source_record.get("sha256") != _sha256(source)
        or Path(str(source_record.get("path", ""))).resolve(strict=True) != source
        or pipeline.get("sha256") != _sha256(derived)
        or Path(str(pipeline.get("path", ""))).resolve(strict=True) != derived
        or pipeline.get("pipeline_eligible") is not True
        or pipeline.get("reimport_status") != "PASS"
        or topology.get("status") != "PASS"
        or topology.get("external_surface_count") != 55
        or topology.get("shared_patch_count") != 2
        or len(topology.get("volume_tags_audit", [])) != 2
        or rematch.get("status") != "PASS"
        or rematch.get("derived_cad_sha256") != _sha256(derived)
    ):
        raise MarkerConfigError("repair manifest is not a current marker-ready PASS")


def _validate_persistence(
    persistence: Mapping[str, Any], source: Path, derived: Path, project: Path
) -> dict[str, float]:
    source_record = _mapping(persistence.get("source_step"), "persistence source_step")
    derived_record = _mapping(persistence.get("derived_brep"), "persistence derived_brep")
    project_record = _mapping(persistence.get("project"), "persistence project")
    source_before = _mapping(source_record.get("before"), "persistence source snapshot")
    derived_before = _mapping(derived_record.get("before"), "persistence BREP snapshot")
    topology = _mapping(persistence.get("derived_topology"), "persistence topology")
    validation = _mapping(persistence.get("validation"), "persistence validation")
    policy = _mapping(persistence.get("policy"), "persistence policy")
    if (
        persistence.get("schema_version") != "cfdpipe.interface_persistence.v1"
        or persistence.get("status") != "PASS"
        or source_record.get("unchanged") is not True
        or derived_record.get("unchanged") is not True
        or derived_record.get("pipeline_eligible") is not True
        or source_before.get("sha256") != _sha256(source)
        or derived_before.get("sha256") != _sha256(derived)
        or project_record.get("sha256") != _sha256(project)
        or topology.get("external_surface_count") != 55
        or topology.get("shared_patch_count") != 2
        or len(topology.get("volume_tags_audit", [])) != 2
        or policy.get("physical_groups_created") is not False
    ):
        raise MarkerConfigError("interface persistence evidence is not a current PASS")
    for flag in _PERSISTENCE_FLAGS:
        expected = False if flag == "matching_uses_old_runtime_tags" else True
        if validation.get(flag) is not expected:
            raise MarkerConfigError(f"interface persistence flag {flag!r} is invalid")
    parameters = _mapping(persistence.get("input_parameters"), "persistence parameters")
    result = {
        "interface_surface_distance_tolerance_m": _finite(
            parameters.get("surface_distance_tolerance_m"), "surface distance tolerance"
        ),
        "interface_boundary_distance_tolerance_m": _finite(
            parameters.get("boundary_distance_tolerance_m"), "boundary distance tolerance"
        ),
        "interface_normal_dot_tolerance": _finite(
            parameters.get("normal_dot_tolerance"), "normal dot tolerance"
        ),
    }
    if any(value <= 0.0 for value in result.values()):
        raise MarkerConfigError("interface persistence tolerances must be positive")
    return result


def _validate_rematch(
    rematch: Mapping[str, Any],
    contract: Mapping[str, Any],
    source_sha256: str,
    derived_sha256: str,
    external_topology: Counter[tuple[Any, ...]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    counts = _mapping(rematch.get("counts"), "rematch counts")
    validation = _mapping(rematch.get("validation"), "rematch validation")
    identity = _mapping(rematch.get("identity_policy"), "rematch identity policy")
    roles = _mapping(rematch.get("roles"), "rematch roles")
    required_validation = (
        "all_confirmed_original_surfaces_rematched",
        "all_descendant_unions_geometry_conserving",
        "solver_groups_complete_and_nonoverlapping",
        "all_solver_surfaces_external",
        "all_interface_surfaces_shared",
        "derived_catalog_fully_classified",
        "measurement_surface_unique",
    )
    if (
        rematch.get("schema") != "cfdpipe.derived_marker_rematch.v1"
        or rematch.get("status") != "PASS"
        or rematch.get("source_step_sha256") != source_sha256
        or rematch.get("derived_cad_sha256") != derived_sha256
        or identity.get("runtime_entity_identifiers_in_output") is not False
        or counts.get("confirmed_original_solver_surfaces") != 55
        or counts.get("derived_external_solver_surfaces") != 55
        or counts.get("solver_boundary_groups") != 4
        or counts.get("measurement_surfaces") != 1
        or validation.get("measurement_surface_is_solver_boundary") is not False
        or any(validation.get(key) is not True for key in required_validation)
    ):
        raise MarkerConfigError("marker rematch evidence is not a complete tag-free PASS")

    solver_roles = list(contract["solver_roles"])
    measurement_role = str(contract["measurement_role"])
    if set(roles) != set(solver_roles) | {measurement_role}:
        raise MarkerConfigError("rematch roles do not exactly match project roles")
    markers: list[dict[str, Any]] = []
    all_derived_ids: set[str] = set()
    all_source_ids: set[str] = set()
    rematch_geometry: Counter[tuple[Any, ...]] = Counter()
    for role in solver_roles:
        item = _mapping(roles.get(role), f"rematch role {role}")
        group = _mapping(item.get("derived_group_fingerprint"), f"group {role}")
        group_id = item.get("derived_group_fingerprint_id")
        members = _list(item.get("derived_member_surface_fingerprints"), f"members {role}")
        kind = str(contract["role_kinds"][role])
        if (
            item.get("kind") != kind
            or item.get("solver_boundary") is not True
            or not _is_sha256(group_id)
            or _canonical_sha256(group) != str(group_id).casefold()
            or group.get("semantic_role") != role
            or group.get("boundary_kind") != kind
            or group.get("source_step_sha256") != source_sha256
            or group.get("derived_cad_sha256") != derived_sha256
            or group.get("member_count") != len(members)
            or group.get("classification") != "complete_external_solver_boundary_group"
        ):
            raise MarkerConfigError(f"solver role {role!r} group fingerprint is invalid")
        member_rows: list[dict[str, Any]] = []
        current_ids: list[str] = []
        current_source_ids: list[str] = []
        for raw_member in members:
            member = _mapping(raw_member, f"member of {role}")
            fingerprint = _mapping(member.get("fingerprint"), "surface fingerprint")
            fingerprint_id = member.get("fingerprint_id")
            geometry = _mapping(fingerprint.get("geometry"), "surface geometry")
            original_id = fingerprint.get("original_surface_fingerprint_id")
            if (
                not _is_sha256(fingerprint_id)
                or _canonical_sha256(fingerprint) != str(fingerprint_id).casefold()
                or not _is_sha256(original_id)
                or fingerprint.get("source_step_sha256") != source_sha256
                or fingerprint.get("derived_cad_sha256") != derived_sha256
                or fingerprint.get("classification") != "external_boundary"
                or geometry.get("adjacent_volume_count") != 1
            ):
                raise MarkerConfigError(f"solver role {role!r} has an invalid member")
            fingerprint_id = str(fingerprint_id).casefold()
            original_id = str(original_id).casefold()
            if fingerprint_id in all_derived_ids or original_id in all_source_ids:
                raise MarkerConfigError("solver marker members overlap between roles")
            all_derived_ids.add(fingerprint_id)
            all_source_ids.add(original_id)
            current_ids.append(fingerprint_id)
            current_source_ids.append(original_id)
            rematch_geometry[_geometry_key(geometry)] += 1
            member_rows.append(
                {
                    "fingerprint_id": fingerprint_id,
                    "original_surface_fingerprint_id": original_id,
                    "entity_type": str(geometry.get("entity_type", "")),
                    "area_m2": _round_float(geometry.get("area_m2")),
                    "centroid_m": _rounded(geometry.get("centroid_m"), 3, "member centroid"),
                    "bounding_box_m": _rounded(
                        geometry.get("bounding_box_m"), 6, "member bounds"
                    ),
                    "adjacent_volume_count": 1,
                }
            )
        expected_ids = sorted(group.get("derived_member_surface_fingerprint_ids", []))
        expected_source = sorted(group.get("source_member_fingerprint_ids", []))
        if sorted(current_ids) != expected_ids or sorted(current_source_ids) != expected_source:
            raise MarkerConfigError(f"solver role {role!r} group membership is inconsistent")
        markers.append(
            {
                "physical_name": role,
                "semantic_role": role,
                "kind": kind,
                "dimension": 2,
                "solver_boundary": True,
                "create_physical_group": True,
                "group_fingerprint_id": str(group_id).casefold(),
                "member_count": len(member_rows),
                "member_fingerprint_ids": sorted(current_ids),
                "members": sorted(member_rows, key=lambda value: value["fingerprint_id"]),
            }
        )
    if len(all_derived_ids) != 55 or len(all_source_ids) != 55:
        raise MarkerConfigError("four solver markers do not exhaust 55 stable members")
    if rematch_geometry != external_topology:
        raise MarkerConfigError("solver marker geometry does not exhaust pipeline external faces")

    measurement_item = _mapping(roles.get(measurement_role), "measurement role")
    fingerprint = _mapping(
        measurement_item.get("derived_measurement_fingerprint"),
        "measurement fingerprint",
    )
    fingerprint_id = measurement_item.get("derived_measurement_fingerprint_id")
    geometry = _mapping(fingerprint.get("geometry"), "measurement geometry")
    if (
        measurement_item.get("kind") != "measurement_surface"
        or measurement_item.get("solver_boundary") is not False
        or not _is_sha256(fingerprint_id)
        or _canonical_sha256(fingerprint) != str(fingerprint_id).casefold()
        or fingerprint.get("source_step_sha256") != source_sha256
        or fingerprint.get("derived_cad_sha256") != derived_sha256
        or fingerprint.get("solver_boundary") is not False
        or geometry.get("closed") is not True
    ):
        raise MarkerConfigError("measurement surface is not a current non-solver PASS")
    normal = _rounded(geometry.get("normal_unit"), 3, "measurement normal")
    magnitude = math.sqrt(sum(value * value for value in normal))
    if abs(magnitude - 1.0) > 1.0e-9:
        raise MarkerConfigError("measurement normal is not a unit vector")
    measurement = {
        "name": measurement_role,
        "semantic_role": measurement_role,
        "kind": "measurement_surface",
        "dimension": 2,
        "solver_boundary": False,
        "create_physical_group": False,
        "postprocess_only": True,
        "fingerprint_id": str(fingerprint_id).casefold(),
        "confirmed_measurement_candidate_id": str(
            fingerprint.get("confirmed_measurement_candidate_id", "")
        ),
        "derived_boundary_fingerprint_id": str(
            fingerprint.get("derived_boundary_fingerprint_id", "")
        ),
        "geometry": {
            "plane_axis": str(geometry.get("plane_axis", "")),
            "plane_coordinate_m": _round_float(geometry.get("plane_coordinate_m")),
            "normal_unit": normal,
            "bounds_m": _rounded(geometry.get("bounds_m"), 6, "measurement bounds"),
            "area_m2": _round_float(geometry.get("area_m2")),
            "closed": True,
        },
    }
    if (
        not measurement["confirmed_measurement_candidate_id"]
        or not _is_sha256(measurement["derived_boundary_fingerprint_id"])
        or not measurement["geometry"]["plane_axis"]
        or measurement["geometry"]["area_m2"] <= 0.0
    ):
        raise MarkerConfigError("measurement stable selector is incomplete")
    return sorted(markers, key=lambda value: value["semantic_role"]), measurement


def _build_payload(
    *,
    project_path: Path,
    source_step_path: Path,
    pipeline_brep_path: Path,
    repair_manifest_path: Path,
    marker_rematch_path: Path,
    interface_persistence_path: Path,
    pipeline_topology_path: Path,
    repository_root: Path,
) -> dict[str, Any]:
    source_sha = _sha256(source_step_path)
    derived_sha = _sha256(pipeline_brep_path)
    project_contract = _project_contract(project_path, source_step_path)
    repair = _load_json(repair_manifest_path, "repair manifest")
    rematch = _load_json(marker_rematch_path, "marker rematch")
    persistence = _load_json(interface_persistence_path, "interface persistence")
    topology = _load_json(pipeline_topology_path, "pipeline topology")
    _validate_repair(repair, source_step_path, pipeline_brep_path)
    persistence_tolerances = _validate_persistence(
        persistence, source_step_path, pipeline_brep_path, project_path
    )
    topology_counts, volume_selectors, external_geometry = _topology_contract(
        topology, derived_sha
    )
    persistence_topology = _mapping(
        persistence.get("derived_topology"), "persistence topology"
    )
    if (
        persistence_topology.get("unique_surface_count") != topology_counts["unique_surface_count"]
        or persistence_topology.get("external_surface_count") != topology_counts["external_surface_count"]
        or persistence_topology.get("shared_patch_count") != topology_counts["shared_surface_count"]
    ):
        raise MarkerConfigError("persistence and pipeline topology counts disagree")
    markers, measurement = _validate_rematch(
        rematch,
        project_contract,
        source_sha,
        derived_sha,
        external_geometry,
    )
    provenance_paths = {
        "project": project_path,
        "source_step": source_step_path,
        "pipeline_brep": pipeline_brep_path,
        "repair_manifest": repair_manifest_path,
        "marker_rematch": marker_rematch_path,
        "interface_persistence": interface_persistence_path,
        "pipeline_topology": pipeline_topology_path,
    }
    provenance = {
        name: {
            "path": _relative(path, repository_root, f"provenance {name}"),
            "sha256": _sha256(path),
        }
        for name, path in provenance_paths.items()
    }
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "PASS",
        "source": {
            "path": provenance["source_step"]["path"],
            "sha256": source_sha,
            "read_only": True,
        },
        "pipeline_geometry": {
            "path": provenance["pipeline_brep"]["path"],
            "sha256": derived_sha,
            "format": "brep",
            "pipeline_eligible": True,
            "read_only": True,
        },
        "topology": topology_counts,
        "matching": {
            "surface_area_relative_tolerance": _AREA_RELATIVE_TOLERANCE,
            "surface_area_absolute_tolerance_m2": _AREA_ABSOLUTE_TOLERANCE_M2,
            "coordinate_absolute_tolerance_m": _COORDINATE_ABSOLUTE_TOLERANCE_M,
            **persistence_tolerances,
            "unique_match_required": True,
            "runtime_tags_are_matching_criteria": False,
        },
        "provenance": provenance,
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
        "measurement": measurement,
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
    _assert_tag_free(payload)
    return payload


def build_marker_config(
    *,
    project_path: PathLike,
    source_step_path: PathLike,
    pipeline_brep_path: PathLike,
    repair_manifest_path: PathLike,
    marker_rematch_path: PathLike,
    interface_persistence_path: PathLike,
    pipeline_topology_path: PathLike,
    repository_root: PathLike | None = None,
) -> dict[str, Any]:
    """Build and fully validate an in-memory ``cfdpipe.markers.v2`` document."""

    project = _input_file(project_path, "project", {".toml"})
    source = _input_file(source_step_path, "source STEP", {".step", ".stp"}, read_only=True)
    derived = _input_file(pipeline_brep_path, "pipeline BREP", {".brep"}, read_only=True)
    repair = _input_file(repair_manifest_path, "repair manifest", {".json"})
    rematch = _input_file(marker_rematch_path, "marker rematch", {".json"})
    persistence = _input_file(
        interface_persistence_path, "interface persistence", {".json"}
    )
    topology = _input_file(pipeline_topology_path, "pipeline topology", {".json"})
    root = _repository_root(project, repository_root)
    payload = _build_payload(
        project_path=project,
        source_step_path=source,
        pipeline_brep_path=derived,
        repair_manifest_path=repair,
        marker_rematch_path=rematch,
        interface_persistence_path=persistence,
        pipeline_topology_path=topology,
        repository_root=root,
    )
    return {
        **payload,
        "integrity": {
            "algorithm": "SHA-256",
            "canonicalization": "UTF-8 JSON, sorted keys, compact separators, integrity excluded",
            "payload_sha256": _canonical_sha256(payload),
        },
    }


def _toml_string(value: object) -> str:
    if not isinstance(value, str):
        raise MarkerConfigError("TOML string value is invalid")
    return json.dumps(value, ensure_ascii=False)


def _toml_value(value: object) -> str:
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MarkerConfigError("TOML contains NaN or Inf")
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise MarkerConfigError(f"unsupported TOML scalar type: {type(value).__name__}")


def _section(lines: list[str], name: str, values: Mapping[str, Any]) -> None:
    lines.append(f"[{name}]")
    for key, value in values.items():
        lines.append(f"{key} = {_toml_value(value)}")
    lines.append("")


def render_marker_config(document: Mapping[str, Any]) -> str:
    """Render a validated marker document with deterministic ordering."""

    validate_marker_config_document(document)
    lines = [
        f"schema = {_toml_string(document['schema'])}",
        f"status = {_toml_string(document['status'])}",
        "",
    ]
    for name in ("source", "pipeline_geometry", "topology", "matching", "integrity"):
        _section(lines, name, _mapping(document[name], name))
    provenance = _mapping(document["provenance"], "provenance")
    for name in _REQUIRED_PROVENANCE:
        _section(lines, f"provenance.{name}", _mapping(provenance[name], name))
    for marker in _list(document["solver_markers"], "solver_markers"):
        item = _mapping(marker, "solver marker")
        lines.append("[[solver_markers]]")
        for key in (
            "physical_name",
            "semantic_role",
            "kind",
            "dimension",
            "solver_boundary",
            "create_physical_group",
            "group_fingerprint_id",
            "member_count",
            "member_fingerprint_ids",
        ):
            lines.append(f"{key} = {_toml_value(item[key])}")
        lines.append("")
        for member in _list(item["members"], "marker members"):
            lines.append("[[solver_markers.members]]")
            for key, value in _mapping(member, "marker member").items():
                lines.append(f"{key} = {_toml_value(value)}")
            lines.append("")
    fluid = _mapping(document["fluid"], "fluid")
    _section(
        lines,
        "fluid",
        {key: value for key, value in fluid.items() if key != "volume_selectors"},
    )
    for selector in _list(fluid["volume_selectors"], "volume selectors"):
        lines.append("[[fluid.volume_selectors]]")
        for key, value in _mapping(selector, "volume selector").items():
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
    measurement = _mapping(document["measurement"], "measurement")
    _section(
        lines,
        "measurement",
        {key: value for key, value in measurement.items() if key != "geometry"},
    )
    _section(lines, "measurement.geometry", _mapping(measurement["geometry"], "measurement geometry"))
    _section(lines, "policy", _mapping(document["policy"], "policy"))
    return "\n".join(lines).rstrip() + "\n"


def validate_marker_config_document(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate embedded integrity and all tag-free structural invariants."""

    if not isinstance(document, Mapping):
        raise MarkerConfigError("marker configuration must be a table")
    _assert_tag_free(document)
    integrity = _mapping(document.get("integrity"), "integrity")
    if (
        document.get("schema") != SCHEMA
        or document.get("status") != "PASS"
        or integrity.get("algorithm") != "SHA-256"
        or not _is_sha256(integrity.get("payload_sha256"))
    ):
        raise MarkerConfigError("marker configuration schema/integrity header is invalid")
    payload = {key: value for key, value in document.items() if key != "integrity"}
    if _canonical_sha256(payload) != str(integrity["payload_sha256"]).casefold():
        raise MarkerConfigError("marker configuration canonical payload SHA-256 is invalid")
    topology = _mapping(document.get("topology"), "topology")
    if (
        topology.get("volume_count") != 2
        or topology.get("unique_surface_count") != 57
        or topology.get("external_surface_count") != 55
        or topology.get("shared_surface_count") != 2
    ):
        raise MarkerConfigError("embedded topology counts are invalid")
    markers = _list(document.get("solver_markers"), "solver_markers")
    if len(markers) != 4:
        raise MarkerConfigError("exactly four solver markers are required")
    all_ids: set[str] = set()
    roles: set[str] = set()
    for raw in markers:
        marker = _mapping(raw, "solver marker")
        members = _list(marker.get("members"), "solver marker members")
        ids = _list(marker.get("member_fingerprint_ids"), "member fingerprint IDs")
        role = marker.get("semantic_role")
        if (
            not isinstance(role, str)
            or role in roles
            or marker.get("physical_name") != role
            or marker.get("dimension") != 2
            or marker.get("solver_boundary") is not True
            or marker.get("create_physical_group") is not True
            or marker.get("member_count") != len(members)
            or sorted(ids) != sorted(member.get("fingerprint_id") for member in members)
        ):
            raise MarkerConfigError("solver marker structure is invalid")
        roles.add(role)
        for fingerprint_id in ids:
            if not _is_sha256(fingerprint_id) or fingerprint_id in all_ids:
                raise MarkerConfigError("solver marker members overlap or are invalid")
            all_ids.add(fingerprint_id)
    if len(all_ids) != 55:
        raise MarkerConfigError("solver markers do not exhaust 55 external members")
    fluid = _mapping(document.get("fluid"), "fluid")
    selectors = _list(fluid.get("volume_selectors"), "fluid volume selectors")
    if (
        fluid.get("physical_name") != "fluid"
        or fluid.get("dimension") != 3
        or fluid.get("create_physical_group") is not True
        or fluid.get("volume_count") != 2
        or len(selectors) != 2
        or len({item.get("fingerprint_id") for item in selectors}) != 2
    ):
        raise MarkerConfigError("fluid volume selector structure is invalid")
    measurement = _mapping(document.get("measurement"), "measurement")
    if (
        measurement.get("solver_boundary") is not False
        or measurement.get("create_physical_group") is not False
        or measurement.get("postprocess_only") is not True
    ):
        raise MarkerConfigError("measurement surface must remain postprocess-only")
    policy = _mapping(document.get("policy"), "policy")
    if (
        policy.get("runtime_entity_identifiers_present") is not False
        or policy.get("step_names_present") is not False
        or policy.get("external_surface_members_disjoint_and_exhaustive") is not True
        or policy.get("measurement_is_physical_group") is not False
    ):
        raise MarkerConfigError("tag-free marker policy is invalid")
    return dict(document)


def load_marker_config(
    path: PathLike,
    *,
    repository_root: PathLike | None = None,
) -> dict[str, Any]:
    """Strictly parse, hash-check and rebuild a marker configuration."""

    source_path = _input_file(path, "marker configuration", {".toml"})
    try:
        with source_path.open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise MarkerConfigError(f"cannot parse marker configuration: {error}") from error
    validate_marker_config_document(document)
    root = (
        Path(repository_root).expanduser().resolve(strict=True)
        if repository_root is not None
        else (
            source_path.parent.parent.resolve(strict=True)
            if source_path.parent.name.casefold() == "config"
            else source_path.parent.resolve(strict=True)
        )
    )
    provenance = _mapping(document.get("provenance"), "provenance")
    if set(provenance) != set(_REQUIRED_PROVENANCE):
        raise MarkerConfigError("provenance set is incomplete or contains extras")
    resolved: dict[str, Path] = {}
    for name in _REQUIRED_PROVENANCE:
        record = _mapping(provenance[name], f"provenance {name}")
        resolved[name] = _resolve_relative(root, record.get("path"), f"provenance {name}")
        if not _is_sha256(record.get("sha256")) or _sha256(resolved[name]) != str(
            record["sha256"]
        ).casefold():
            raise MarkerConfigError(f"provenance {name!r} SHA-256 is stale")
    rebuilt = build_marker_config(
        project_path=resolved["project"],
        source_step_path=resolved["source_step"],
        pipeline_brep_path=resolved["pipeline_brep"],
        repair_manifest_path=resolved["repair_manifest"],
        marker_rematch_path=resolved["marker_rematch"],
        interface_persistence_path=resolved["interface_persistence"],
        pipeline_topology_path=resolved["pipeline_topology"],
        repository_root=root,
    )
    if document != rebuilt:
        raise MarkerConfigError("marker configuration does not exactly rebuild from provenance")
    return dict(document)


def _publish_new_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise MarkerConfigError(f"refusing to overwrite marker configuration: {path}")
    temporary: Path | None = None
    try:
        descriptor, raw_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(raw_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise MarkerConfigError(
                f"refusing to overwrite marker configuration: {path}"
            ) from error
        except OSError as error:
            raise MarkerConfigError(
                "atomic new-only publication requires same-filesystem hard-link support"
            ) from error
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def write_marker_config(
    output_path: PathLike,
    *,
    project_path: PathLike,
    source_step_path: PathLike,
    pipeline_brep_path: PathLike,
    repair_manifest_path: PathLike,
    marker_rematch_path: PathLike,
    interface_persistence_path: PathLike,
    pipeline_topology_path: PathLike,
    repository_root: PathLike | None = None,
) -> dict[str, Any]:
    """Build and atomically publish a new marker TOML; never overwrite."""

    output = Path(os.path.abspath(Path(output_path).expanduser()))
    if ".." in Path(output_path).parts or output.suffix.casefold() != ".toml":
        raise MarkerConfigError("marker output must be a non-traversing .toml path")
    if output.exists() or output.is_symlink():
        raise MarkerConfigError(f"refusing to overwrite marker configuration: {output}")
    document = build_marker_config(
        project_path=project_path,
        source_step_path=source_step_path,
        pipeline_brep_path=pipeline_brep_path,
        repair_manifest_path=repair_manifest_path,
        marker_rematch_path=marker_rematch_path,
        interface_persistence_path=interface_persistence_path,
        pipeline_topology_path=pipeline_topology_path,
        repository_root=repository_root,
    )
    content = render_marker_config(document)
    _publish_new_atomic(output, content)
    root = repository_root
    if root is None:
        project = _input_file(project_path, "project", {".toml"})
        root = project.parent.parent
    try:
        loaded = load_marker_config(output, repository_root=root)
    except BaseException:
        if output.exists() and output.is_file() and not output.is_symlink():
            output.unlink()
        raise
    return loaded


__all__ = [
    "MarkerConfigError",
    "SCHEMA",
    "build_marker_config",
    "load_marker_config",
    "render_marker_config",
    "validate_marker_config_document",
    "write_marker_config",
]
